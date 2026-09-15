"""Vigila una carpeta compartida de iCloud Drive y lee la placa de cada foto nueva.

    ciclo_icloud(), cada ICLOUD_INTERVALO_REVISION s ──┐
    GET /icloud/lecturas ──────────────────────────────┤
                                                       ▼
    CloudKit changes/zone ──▶ Postgres ("pendiente") ──▶ procesar_frame() ──▶ Postgres (placa)

Apple no tiene una API pública de iCloud Drive ni avisa cuando llega un
archivo, así que esto funciona distinto que con Google Drive:

- La sesión se abre con pyicloud, con una cuenta de Apple que agregó la
  carpeta compartida a su iCloud Drive. La primera vez, y cuando Apple deja
  de confiar en la sesión (unos 30 días), pide un código de verificación.
- Si Apple pide código o rechaza la contraseña, la API no vuelve a intentar
  sola: cada intento manda otro SMS o acerca a Apple a bloquear la cuenta.
- La carpeta se lee por CloudKit y no por el servicio Drive de pyicloud: su
  descarga (docws download/by_id) da 404 con carpetas que compartió otra
  cuenta. CloudKit, además, devuelve solo lo que cambió desde la vez
  anterior (syncToken): revisar seguido cuesta una petición.
- Sin avisos, la carpeta se revisa cada ICLOUD_INTERVALO_REVISION segundos
  mientras el contenedor está despierto, y antes de responder
  GET /icloud/lecturas, que es lo que despierta a Render en el plan gratis.
- Render gratis no tiene disco: la sesión, el syncToken y las lecturas van a
  Postgres (db.py). Al despertar se restaura la sesión sin pedir código.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAcceptTermsException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
    PyiCloudFailedLoginException,
)

from app import alpr_service, db
from app.config import settings

log = logging.getLogger("alpr.icloud")

# Resuelve el enlace de la carpeta sin sesión: de ahí sale la zona del dueño
URL_ENLACE = "https://ckdatabasews.icloud.com/database/1/com.apple.cloudkit/production/public/records/resolve"
# iCloud Drive en CloudKit. Lo que otra cuenta te comparte está en la base "shared".
RUTA_CLOUDKIT = "/database/1/com.apple.clouddocs/production/shared"
# HEIC también: sale como "no_decodificable", que avisa que hay que cambiar el formato
EXTENSIONES_FOTO = {"jpg", "jpeg", "png", "webp", "bmp", "heic"}
# GET /icloud/lecturas revisa la carpeta si la última revisión es más vieja que esto
FRESCURA_LECTURAS = 15
# Códigos con los que Apple da la sesión por vencida
CODIGOS_SESION_VENCIDA = {"401", "421", "450"}

# pyicloud lee y escribe aquí su sesión; Postgres guarda una copia
DIR_SESION = Path(tempfile.gettempdir()) / "alpr-icloud"

_estado: dict = {"ultimo_escaneo": None, "ultimo_error": None, "ultima_revision": 0.0, "rechazada": False}
_carpeta: dict | None = None
_api: PyiCloudService | None = None
# El cliente al que Apple le pidió código; POST /icloud/codigo lo completa
_esperando_codigo: PyiCloudService | None = None
_sesion_guardada: str | None = None

# pyicloud comparte una sesión de requests y reescribe sus archivos en cada
# petición: una sola llamada a Apple a la vez. RLock porque las funciones
# públicas lo toman y llaman a otras que también lo toman.
_lock_icloud = threading.RLock()
# Una revisión de la carpeta a la vez. Aparte del de escaneo para que
# GET /icloud/lecturas pueda esperar una revisión sin esperar las lecturas.
_lock_revision = threading.RLock()
_lock_escaneo = threading.Lock()
_pendiente = threading.Event()


class IcloudError(RuntimeError):
    """Error de configuración o permisos que el usuario tiene que arreglar."""


class FaltaCodigo(IcloudError):
    """Apple pide el código de verificación y la API no entra hasta tenerlo."""


class CredencialesRechazadas(IcloudError):
    """Apple rechazó el correo o la contraseña; no se reintenta solo."""


class SinCodigoPendiente(IcloudError):
    """Llegó un código pero no hay un inicio de sesión esperándolo."""


class CodigoInvalido(IcloudError):
    """Apple rechazó el código de verificación."""


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _describir(e: Exception) -> str:
    return str(e) if isinstance(e, IcloudError) else f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ carpeta
def _carpeta_compartida() -> dict:
    """Zona del dueño y nombre de la carpeta, a partir del enlace. No pide sesión."""
    global _carpeta
    if _carpeta is None:
        r = httpx.post(
            URL_ENLACE,
            content=json.dumps({"shortGUIDs": [{"value": settings.icloud_carpeta}]}),
            headers={"Content-Type": "text/plain", "Origin": "https://www.icloud.com"},
            timeout=20,
        )
        if r.status_code >= 500:
            r.raise_for_status()
        res = r.json().get("results", [{}])[0] if r.is_success else {}
        if "zoneID" not in res:
            raise IcloudError("el enlace de ICLOUD_CARPETA no existe o la carpeta ya no está compartida")
        _carpeta = {
            "zona": {k: res["zoneID"][k] for k in ("zoneName", "ownerRecordName")},
            "nombre": res.get("share", {}).get("fields", {}).get("cloudkit.title", {}).get("value"),
        }
    return _carpeta


# ------------------------------------------------------------------- sesión
def _restaurar_sesion() -> None:
    """Pone en disco los archivos de sesión de pyicloud guardados en Postgres."""
    global _sesion_guardada
    guardada = db.leer("sesion")
    DIR_SESION.mkdir(parents=True, exist_ok=True)
    if guardada:
        for nombre, contenido in json.loads(guardada).items():
            (DIR_SESION / nombre).write_text(contenido, encoding="utf-8")
    _sesion_guardada = guardada


def _guardar_sesion() -> None:
    """Copia a Postgres los archivos de sesión, si cambiaron desde la última vez."""
    global _sesion_guardada
    with _lock_icloud:
        archivos = {f.name: f.read_text(encoding="utf-8") for f in DIR_SESION.glob("*") if f.is_file()}
        valor = json.dumps(archivos, sort_keys=True)
        if archivos and valor != _sesion_guardada:
            db.guardar("sesion", valor)
            _sesion_guardada = valor


def _entrar() -> PyiCloudService:
    """Abre la sesión con la contraseña, reusando la guardada si Apple la acepta.

    Si Apple pide código, pyicloud ya se lo mandó al usuario (a sus
    dispositivos o por SMS). Queda marcado en Postgres para no volver a
    intentarlo solo: cada intento manda otro código.
    """
    global _esperando_codigo
    try:
        api = PyiCloudService(settings.icloud_apple_id, settings.icloud_password, cookie_directory=str(DIR_SESION))
    except PyiCloudAcceptTermsException as e:
        raise IcloudError("Apple pide aceptar sus términos: entra una vez a icloud.com con esa cuenta") from e
    except PyiCloudFailedLoginException as e:
        # Cada intento fallido acerca a Apple a bloquear la cuenta: no se repite
        # solo. La marca vive en memoria porque al corregir las variables
        # Render reinicia la API, y ese reinicio es el que debe reintentar.
        _estado["rechazada"] = True
        raise CredencialesRechazadas(f"Apple rechazó ICLOUD_APPLE_ID o ICLOUD_PASSWORD ({e})") from e
    if api.requires_2fa:
        if api.security_key_names:
            raise IcloudError("la cuenta usa llaves de seguridad; usa una que reciba el código por SMS o en un dispositivo")
        _esperando_codigo = api
        db.guardar("falta_codigo", _ahora())
        raise FaltaCodigo("Apple mandó un código de verificación: pásalo a POST /icloud/codigo")
    return api


def _cliente() -> PyiCloudService:
    """El cliente con sesión. Si no hay uno, restaura la sesión de Postgres y entra."""
    global _api
    with _lock_icloud:
        if _api is None:
            if _estado["rechazada"]:
                raise CredencialesRechazadas(
                    "Apple rechazó ICLOUD_APPLE_ID o ICLOUD_PASSWORD: corrígelos en Render"
                    " o reintenta con POST /icloud/sesion"
                )
            if db.leer("falta_codigo"):
                raise FaltaCodigo("falta el código de verificación de Apple: pídelo con POST /icloud/sesion")
            _restaurar_sesion()
            _api = _entrar()
            _guardar_sesion()
            log.info("Sesión de iCloud abierta")
        return _api


def iniciar_sesion() -> str:
    """POST /icloud/sesion: entra de nuevo, aunque la vez anterior faltara el
    código o Apple rechazara la contraseña.

    Devuelve "activa", o "falta_codigo" si Apple mandó un código.
    """
    global _api, _esperando_codigo
    with _lock_icloud:
        _api = _esperando_codigo = None
        _estado["rechazada"] = False
        db.guardar("falta_codigo", None)
        try:
            _cliente()
        except FaltaCodigo:
            return "falta_codigo"
    return "activa"


def validar_codigo(codigo: str) -> None:
    """POST /icloud/codigo: completa el inicio de sesión que quedó esperando el código."""
    global _api, _esperando_codigo
    with _lock_icloud:
        api = _esperando_codigo
        if api is None:
            raise SinCodigoPendiente(
                "no hay un inicio de sesión esperando código (la API pudo reiniciarse): pide otro con POST /icloud/sesion"
            )
        try:
            valido = api.validate_2fa_code(codigo)
        except PyiCloudException as e:
            raise CodigoInvalido(f"Apple rechazó el código ({e})") from e
        if not valido:
            raise CodigoInvalido("Apple rechazó el código")
        if not api.is_trusted_session:
            api.trust_session()
        _api, _esperando_codigo = api, None
        db.guardar("falta_codigo", None)
        _guardar_sesion()
    log.info("Sesión de iCloud abierta con código de verificación")


# ----------------------------------------------------------------- CloudKit
def _cloudkit(ruta: str, cuerpo: dict) -> dict:
    """POST a la base "shared" de iCloud Drive en CloudKit.

    Si Apple dio la sesión por vencida, entra de nuevo y reintenta una vez:
    con el trust token guardado no hace falta código.
    """
    global _api
    with _lock_icloud:
        try:
            return _post_cloudkit(ruta, cuerpo)
        except PyiCloudException as e:
            if not _es_sesion_vencida(e):
                raise
            log.info("Apple dio la sesión por vencida (%s); se entra de nuevo", e)
            _api = None
            return _post_cloudkit(ruta, cuerpo)


def _post_cloudkit(ruta: str, cuerpo: dict) -> dict:
    api = _cliente()
    url = api.data["webservices"]["ckdatabasews"]["url"] + RUTA_CLOUDKIT + ruta
    r = api.session.post(url, params=api.params, data=json.dumps(cuerpo), headers={"Content-Type": "text/plain"})
    return r.json()


def _es_sesion_vencida(e: PyiCloudException) -> bool:
    if isinstance(e, (PyiCloudAuthRequiredException, PyiCloud2FARequiredException)):
        return True
    return isinstance(e, PyiCloudAPIResponseException) and str(e.code) in CODIGOS_SESION_VENCIDA


def _cambios(token: str | None) -> tuple[list[dict], str, bool]:
    """Los records que cambiaron desde `token`, el token nuevo y si la lista es completa.

    Sin token, o si CloudKit ya no acepta el guardado, trae la carpeta
    entera: esa lista es completa y sirve para borrar lo que ya no está.
    """
    zona = _carpeta_compartida()["zona"]
    records: list[dict] = []
    completa = token is None
    while True:
        pedido = {"zoneID": zona} if token is None else {"zoneID": zona, "syncToken": token}
        z = _cloudkit("/changes/zone", {"zones": [pedido], "resultsLimit": 200})["zones"][0]
        if "serverErrorCode" in z:
            if completa:
                raise IcloudError(f"CloudKit no deja leer la carpeta: {z['serverErrorCode']} {z.get('reason', '')}")
            log.warning("CloudKit no aceptó el syncToken (%s); se lee la carpeta entera", z["serverErrorCode"])
            records, token, completa = [], None, True
            continue
        records += z.get("records", [])
        token = z["syncToken"]
        if not z.get("moreComing"):
            return records, token, completa


def _asset(id_: str) -> dict | None:
    """El archivo actual de una foto (downloadURL, size, fileChecksum), o None si ya no existe.

    Se pide cada vez en lugar de guardar la URL: así se baja la versión
    vigente aunque la hayan reemplazado desde la última revisión.
    """
    pedido = {"zoneID": _carpeta_compartida()["zona"], "records": [{"recordName": f"documentContent/{id_}"}]}
    rec = _cloudkit("/records/lookup", pedido)["records"][0]
    if rec.get("serverErrorCode") == "NOT_FOUND":
        return None
    if "serverErrorCode" in rec:
        raise IcloudError(f"CloudKit no dejó leer la foto: {rec['serverErrorCode']} {rec.get('reason', '')}")
    return rec["fields"]["fileContent"]["value"]


def _bajar(asset: dict) -> bytes:
    # La URL viene firmada: no hace falta la sesión, y la descarga no frena a pyicloud
    r = httpx.get(asset["downloadURL"].replace("${f}", "foto"), timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.content


# ------------------------------------------------------------------ revisión
def _sincronizar() -> None:
    """Pasa a Postgres las fotos nuevas, cambiadas o borradas desde la última revisión."""
    with _lock_revision:
        records, token, completa = _cambios(db.leer("sync_token"))
        fotos, borradas = [], []
        for rec in records:
            tipo, _, id_ = rec["recordName"].partition("/")
            if tipo not in ("documentContent", "documentStructure"):
                continue
            if rec.get("deleted"):
                borradas.append(id_)
            elif tipo == "documentContent" and (foto := _a_foto(id_, rec)):
                fotos.append(foto)
        db.aplicar_cambios(fotos, borradas, token, completa)
        _estado["ultima_revision"] = time.time()
    if fotos or borradas:
        log.info("iCloud: %d fotos nuevas o cambiadas, %d borradas", len(fotos), len(borradas))


def _a_foto(id_: str, rec: dict) -> dict | None:
    """Los datos de un record de contenido, o None si no es una foto."""
    campos = rec.get("fields", {})
    ext = campos.get("extension", {}).get("value", "")
    if ext.lower() not in EXTENSIONES_FOTO or "fileContent" not in campos:
        return None
    archivo = campos["fileContent"]["value"]
    # Con la protección de datos estándar de iCloud el nombre llega en base64, sin cifrar
    base = campos.get("encryptedBasename", {}).get("value")
    nombre = base64.b64decode(base).decode("utf-8", "replace") if base else id_
    return {
        "id": id_,
        "nombre": f"{nombre}.{ext}",
        "subida": datetime.fromtimestamp(rec["created"]["timestamp"] / 1000, timezone.utc),
        "tamano": archivo.get("size"),
        "checksum": archivo.get("fileChecksum"),
    }


def traer_novedades() -> None:
    """Para GET /icloud/lecturas: registra las fotos nuevas antes de listar.

    Así una foto que llegó mientras Render dormía sale como "pendiente" en la
    primera respuesta, como pasaba con Drive. Solo la registra, que es rápido;
    la placa se lee después con escanear(). Si falla (ej. falta el código de
    Apple) no pasa nada: se listan las lecturas que ya hay.
    """
    if not _lock_revision.acquire(timeout=20):
        return
    try:
        # Si justo terminó otra revisión (ej. la del arranque), ya está al día
        if time.time() - _estado["ultima_revision"] >= FRESCURA_LECTURAS:
            _sincronizar()
    except Exception as e:
        _estado["ultimo_error"] = _describir(e)
        log.warning("No se pudo revisar iCloud antes de listar: %s", e)
    finally:
        _lock_revision.release()


# ------------------------------------------------------------------ lectura
def _procesar_foto(foto: dict) -> tuple[dict, str | None] | None:
    """Baja la foto y lee sus placas. Devuelve el resultado y el checksum de lo que se leyó.

    None si la foto ya no existe. Se guarda solo la placa más clara
    (`placas[0]`) y cuántas se encontraron.
    """
    asset = _asset(foto["id"])
    if asset is None:
        return None
    checksum = asset.get("fileChecksum")
    if (asset.get("size") or 0) > settings.max_mb_imagen * 1024 * 1024:
        return {"estado": "muy_grande"}, checksum

    frame = alpr_service.decodificar(_bajar(asset))
    if frame is None:
        return {"estado": "no_decodificable"}, checksum

    placas, _ = alpr_service.procesar_frame(frame)
    if not placas:
        return {"estado": "sin_placa"}, checksum

    p = placas[0]
    bb = p["bbox"]
    return {
        "estado": "ok",
        "placa": p["texto"],
        "fuente": p["fuente"],
        "conf_det": p["confianza_deteccion"],
        "conf_ocr": p["confianza_ocr"],
        "bbox": [bb["x1"], bb["y1"], bb["x2"], bb["y2"]],
        "n_placas": len(placas),
    }, checksum


def _procesar_pendientes() -> int:
    procesadas = 0
    for foto in db.pendientes():
        try:
            leido = _procesar_foto(foto)
        except (IcloudError, PyiCloudException):
            raise  # sesión, permisos o Apple caído: no es culpa de la foto
        except (httpx.HTTPError, OSError) as e:
            log.warning("Error de red al bajar %s (%s); se reintenta luego", foto["nombre"], e)
            continue
        except Exception as e:
            # Una foto que rompe el pipeline se marca para no reintentarla en cada revisión
            log.exception("Falló la lectura de %s", foto["nombre"])
            leido = {"estado": "error", "nota": type(e).__name__}, foto["checksum"]

        if leido is None:
            db.borrar(foto["id"])  # la borraron entre la revisión y la lectura
            continue
        resultado, checksum = leido
        db.guardar_resultado(foto["id"], resultado, checksum)
        procesadas += 1
        log.info("iCloud: %s -> %s %s", foto["nombre"], resultado["estado"], resultado.get("placa") or "")
    return procesadas


def escanear() -> int:
    """Revisa la carpeta y lee las fotos pendientes. Devuelve cuántas leyó este llamado.

    Si ya hay un escaneo corriendo no arranca otro: le avisa al que corre
    que dé una vuelta más al terminar.
    """
    _pendiente.set()
    if not _lock_escaneo.acquire(blocking=False):
        return 0
    procesadas = 0
    try:
        while _pendiente.is_set():
            _pendiente.clear()
            _sincronizar()
            procesadas += _procesar_pendientes()
        _guardar_sesion()
        _estado["ultimo_error"] = None
    except Exception as e:
        _estado["ultimo_error"] = _describir(e)
        raise
    finally:
        _estado["ultimo_escaneo"] = _ahora()
        _lock_escaneo.release()
    return procesadas


def escanear_sin_excepciones() -> None:
    """Para tareas de fondo, donde una excepción solo ensuciaría el log."""
    try:
        escanear()
    except (FaltaCodigo, CredencialesRechazadas) as e:
        log.warning("iCloud: %s", e)
    except Exception:
        log.exception("Falló el escaneo de la carpeta de iCloud")


# ------------------------------------------------------------------ consulta
def _a_lectura(foto: dict) -> dict:
    bbox = None
    if foto["bbox"]:
        x1, y1, x2, y2 = foto["bbox"]
        bbox = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    return {
        "id": foto["id"],
        "nombre": foto["nombre"],
        "subida": foto["subida"],
        "estado": foto["estado"],
        "placa": foto["placa"],
        "fuente": foto["fuente"],
        "confianza_ocr": foto["conf_ocr"],
        "confianza_deteccion": foto["conf_det"],
        "n_placas": foto["n_placas"],
        "bbox": bbox,
        "procesada": foto["procesada"],
        "nota": foto["nota"],
        "propietario": foto.get("propietario"),
    }


def listar_lecturas(limite: int, placa: str | None = None) -> list[dict]:
    """Las fotos más recientes con su resultado. `placa` ya viene normalizada."""
    return [_a_lectura(f) for f in db.listar(limite, placa)]


def recorte_de(id_: str) -> bytes | None:
    """Recorte JPEG de la placa de una foto ya leída.

    Se vuelve a recortar desde la foto original con el bbox guardado, sin
    correr otra vez el modelo. None si la foto no está registrada (no es de
    la carpeta vigilada) o no tiene placa.
    """
    foto = db.obtener(id_)
    if foto is None or not foto["bbox"]:
        return None
    asset = _asset(id_)
    if asset is None:
        return None
    frame = alpr_service.decodificar(_bajar(asset))
    if frame is None:
        return None
    x1, y1, x2, y2 = foto["bbox"]
    recorte = alpr_service._recortar(frame, x1, y1, x2, y2, settings.margen_recorte)
    return alpr_service._a_jpeg(recorte)


def estado() -> dict:
    if _api is not None:
        sesion = "activa"
    else:
        sesion = "rechazada" if _estado["rechazada"] else "sin_iniciar"
    datos = {
        "carpeta": None,
        "sesion": sesion,
        "ultimo_escaneo": _estado["ultimo_escaneo"],
        "ultimo_error": _estado["ultimo_error"],
        "fotos": None,
        "pendientes": None,
    }
    try:
        datos["carpeta"] = _carpeta_compartida()["nombre"]
    except Exception as e:
        datos["ultimo_error"] = datos["ultimo_error"] or _describir(e)
    try:
        if sesion == "sin_iniciar" and (_esperando_codigo is not None or db.leer("falta_codigo")):
            datos["sesion"] = "falta_codigo"
        datos.update(db.contar())
    except Exception as e:
        datos["ultimo_error"] = datos["ultimo_error"] or f"Postgres: {_describir(e)}"
    return datos


# ------------------------------------------------------------------- fondo
async def ciclo_icloud() -> None:
    """Tarea de fondo: revisa la carpeta cada ICLOUD_INTERVALO_REVISION segundos.

    La primera vuelta corre al arrancar y recoge las fotos que llegaron
    mientras el contenedor dormía.
    """
    while True:
        await asyncio.to_thread(escanear_sin_excepciones)
        await asyncio.sleep(settings.icloud_intervalo_revision)

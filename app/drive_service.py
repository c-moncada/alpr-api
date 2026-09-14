"""Vigila una carpeta de Google Drive y lee las placas de cada foto nueva.

    foto nueva en Drive ──aviso──▶ POST /drive/webhook ──▶ escanear_carpeta()
                                                                │
    appProperties de la foto ◀── placa ◀── procesar_frame() ◀───┘

El aviso de Google no dice QUÉ archivo cambió, solo que algo cambió. Por eso
cada aviso dispara un escaneo de la carpeta que procesa las fotos que todavía
no tienen resultado.

El resultado se guarda en la propia foto como appProperties: metadatos
privados de esta app que el usuario no ve en Drive. Así la API sigue sin
estado propio: si Render reinicia o duerme el contenedor, al despertar
escanea la carpeta y retoma donde se quedó, sin base de datos ni disco.

Red de seguridad: `ciclo_drive` escanea cada DRIVE_INTERVALO_REVISION
segundos aunque no llegue ningún aviso, y renueva el canal antes de que
caduque (Google lo cierra a los 7 días como máximo).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import alpr_service
from app.config import BASE_DIR, settings

log = logging.getLogger("alpr.drive")

# Escribir appProperties pide el scope completo; drive.readonly no alcanza
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Un canal de changes.watch dura como máximo 7 días
DURACION_CANAL = 7 * 24 * 3600
# Se renueva cuando le queda menos de esto
MARGEN_RENOVACION = 24 * 3600

# Todo lo que se escribe en cada foto. Al guardar un resultado se mandan
# todas: las que quedan en None se borran, así no sobreviven datos de una
# versión anterior de la misma foto (ej. la placa vieja en una foto sin placa).
CLAVES_RESULTADO = (
    "alpr_estado",
    "alpr_placa",
    "alpr_fuente",
    "alpr_conf_det",
    "alpr_conf_ocr",
    "alpr_bbox",
    "alpr_n_placas",
    "alpr_nota",
    "alpr_procesado",
    "alpr_md5",
)

_estado: dict = {
    "canal_expira": 0.0,
    "ultimo_escaneo": None,
    "ultimo_error": None,
}

_credenciales = None
_token_canal: str | None = None
# El cliente de googleapiclient no es seguro entre hilos (usa httplib2), y
# aquí se llama desde el threadpool de FastAPI y desde asyncio.to_thread.
_local = threading.local()

_lock_escaneo = threading.Lock()
_pendiente = threading.Event()


class DriveError(RuntimeError):
    """Error de configuración o permisos que el usuario tiene que arreglar."""


# ------------------------------------------------------------------- cliente
def _obtener_credenciales():
    global _credenciales
    if _credenciales is None:
        if settings.google_service_account_json.strip():
            info = json.loads(settings.google_service_account_json)
            _credenciales = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            ruta = BASE_DIR / settings.google_service_account_file
            _credenciales = service_account.Credentials.from_service_account_file(ruta, scopes=SCOPES)
    return _credenciales


def _drive():
    if not hasattr(_local, "drive"):
        _local.drive = build("drive", "v3", credentials=_obtener_credenciales(), cache_discovery=False)
    return _local.drive


def token_canal() -> str:
    """Secreto que Google devuelve en cada aviso; así se descartan POST ajenos.

    Se deriva firmando un texto fijo con la clave de la service account (la
    firma RSA PKCS#1 v1.5 es determinista): da el mismo valor en cada arranque
    y en cada instancia sin guardarlo en ningún lado, y sin la clave nadie
    puede calcularlo. Si fuera aleatorio, durante un despliegue de Render los
    avisos que llegan a la instancia vieja (sigue viva unos segundos, con
    otro token) se rechazarían y la foto esperaría a la revisión periódica.
    """
    global _token_canal
    if _token_canal is None:
        texto = f"alpr-api/drive-webhook/{settings.drive_carpeta_id}".encode()
        _token_canal = hashlib.sha256(_obtener_credenciales().sign_bytes(texto)).hexdigest()
    return _token_canal


def _query_carpeta() -> str:
    return f"'{settings.drive_carpeta_id}' in parents and mimeType contains 'image/' and trashed = false"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------- canal
def asegurar_canal() -> None:
    """Crea el canal de avisos si no hay uno o si está por caducar.

    El ID del canal se guarda en las appProperties de la carpeta: después de
    un reinicio es la única forma de saber cuál detener, porque la API no
    guarda nada en disco.
    """
    url = settings.drive_webhook_url
    if not url or _estado["canal_expira"] - time.time() > MARGEN_RENOVACION:
        return

    drive = _drive()
    carpeta = settings.drive_carpeta_id
    anterior = (
        drive.files()
        .get(fileId=carpeta, fields="appProperties", supportsAllDrives=True)
        .execute()
        .get("appProperties", {})
    )

    token_pagina = drive.changes().getStartPageToken(supportsAllDrives=True).execute()["startPageToken"]
    canal = (
        drive.changes()
        .watch(
            pageToken=token_pagina,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            body={
                "id": str(uuid.uuid4()),
                "type": "web_hook",
                "address": url,
                "token": token_canal(),
                "expiration": int((time.time() + DURACION_CANAL) * 1000),
            },
        )
        .execute()
    )
    _estado["canal_expira"] = int(canal["expiration"]) / 1000

    # Primero se crea el nuevo y luego se detiene el viejo: así no hay hueco
    if anterior.get("alpr_canal_id"):
        try:
            drive.channels().stop(
                body={"id": anterior["alpr_canal_id"], "resourceId": anterior["alpr_canal_res"]}
            ).execute()
        except HttpError:
            pass  # ya había caducado

    try:
        drive.files().update(
            fileId=carpeta,
            body={"appProperties": {"alpr_canal_id": canal["id"], "alpr_canal_res": canal["resourceId"]}},
            fields="id",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        log.warning("No se pudo guardar el ID del canal en la carpeta (%s)", e.resp.status)

    log.info(
        "Canal de Drive activo hasta %s -> %s",
        datetime.fromtimestamp(_estado["canal_expira"], timezone.utc).isoformat(timespec="minutes"),
        url,
    )


# ------------------------------------------------------------------ escaneo
def escanear_carpeta() -> int:
    """Procesa las fotos de la carpeta que todavía no tienen resultado.

    Si ya hay un escaneo corriendo no arranca otro: le avisa al que corre que
    dé una vuelta más al terminar. Google manda varios avisos por cada foto,
    y así una ráfaga se resuelve con uno o dos escaneos. Devuelve cuántas
    fotos procesó este llamado.
    """
    _pendiente.set()
    if not _lock_escaneo.acquire(blocking=False):
        return 0
    procesadas = 0
    try:
        while _pendiente.is_set():
            _pendiente.clear()
            procesadas += _escanear_una_vez()
        _estado["ultimo_error"] = None
    except Exception as e:
        _estado["ultimo_error"] = str(e) if isinstance(e, DriveError) else f"{type(e).__name__}: {e}"
        raise
    finally:
        _estado["ultimo_escaneo"] = _ahora()
        _lock_escaneo.release()
    return procesadas


def escanear_sin_excepciones() -> None:
    """Para tareas de fondo, donde una excepción solo ensuciaría el log."""
    try:
        escanear_carpeta()
    except Exception:
        log.exception("Falló el escaneo de la carpeta de Drive")


def _fotos_pendientes() -> list[dict]:
    pendientes: list[dict] = []
    token = None
    while True:
        r = (
            _drive()
            .files()
            .list(
                q=_query_carpeta(),
                fields="nextPageToken, files(id, name, size, md5Checksum, appProperties)",
                orderBy="createdTime",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        pendientes += [f for f in r.get("files", []) if _pendiente_de_leer(f)]
        token = r.get("nextPageToken")
        if not token:
            return pendientes


def _pendiente_de_leer(archivo: dict) -> bool:
    """Sin resultado, o con el resultado de otra versión de la foto.

    Si subes una foto con el mismo nombre y eliges reemplazar, Drive guarda
    una versión nueva del MISMO archivo: mismo ID y mismas appProperties.
    Por eso se compara el MD5 del contenido y no solo si ya hay estado.
    """
    props = archivo.get("appProperties", {})
    return "alpr_estado" not in props or props.get("alpr_md5") != archivo.get("md5Checksum")


def _escanear_una_vez() -> int:
    procesadas = 0
    for archivo in _fotos_pendientes():
        try:
            props = _procesar_foto(archivo)
        except HttpError as e:
            if e.resp.status == 429 or e.resp.status >= 500:
                log.warning("Drive %s al bajar %s; se reintenta luego", e.resp.status, archivo["name"])
                continue
            props = {"alpr_estado": "error", "alpr_nota": f"drive_http_{e.resp.status}"}
        except (OSError, httplib2.HttpLib2Error) as e:
            log.warning("Error de red al bajar %s (%s); se reintenta luego", archivo["name"], e)
            continue
        except Exception as e:
            # Una foto que rompe el pipeline se marca para no reintentarla en cada escaneo
            log.exception("Falló la lectura de %s", archivo["name"])
            props = {"alpr_estado": "error", "alpr_nota": type(e).__name__}

        props["alpr_procesado"] = _ahora()
        props["alpr_md5"] = archivo.get("md5Checksum")
        props = {k: props.get(k) for k in CLAVES_RESULTADO}
        try:
            _drive().files().update(
                fileId=archivo["id"], body={"appProperties": props}, fields="id", supportsAllDrives=True
            ).execute()
        except HttpError as e:
            if e.resp.status == 403:
                raise DriveError(
                    "la service account no puede escribir en la carpeta: compártela con permiso de Editor"
                ) from e
            raise
        procesadas += 1
        log.info("Drive: %s -> %s %s", archivo["name"], props["alpr_estado"], props["alpr_placa"] or "")
    return procesadas


def _procesar_foto(archivo: dict) -> dict[str, str]:
    """Baja la foto, lee sus placas y devuelve las appProperties a guardar.

    Cada appProperty admite 124 bytes entre clave y valor, así que se guarda
    solo la placa más clara (`placas[0]`) y cuántas se encontraron.
    """
    if int(archivo.get("size", 0)) > settings.max_mb_imagen * 1024 * 1024:
        return {"alpr_estado": "muy_grande"}

    contenido = _drive().files().get_media(fileId=archivo["id"], supportsAllDrives=True).execute()
    frame = alpr_service.decodificar(contenido)
    if frame is None:
        return {"alpr_estado": "no_decodificable"}

    placas, _ = alpr_service.procesar_frame(frame)
    if not placas:
        return {"alpr_estado": "sin_placa"}

    p = placas[0]
    bb = p["bbox"]
    props = {
        "alpr_estado": "ok",
        "alpr_placa": p["texto"],
        "alpr_fuente": p["fuente"],
        "alpr_conf_det": str(p["confianza_deteccion"]),
        "alpr_conf_ocr": str(p["confianza_ocr"]) if p["confianza_ocr"] is not None else None,
        "alpr_bbox": f"{bb['x1']},{bb['y1']},{bb['x2']},{bb['y2']}",
        "alpr_n_placas": str(len(placas)),
    }
    return {k: v for k, v in props.items() if v is not None}


# ------------------------------------------------------------------ lectura
def _a_lectura(archivo: dict) -> dict:
    props = archivo.get("appProperties", {})
    bbox = None
    if props.get("alpr_bbox"):
        x1, y1, x2, y2 = (int(v) for v in props["alpr_bbox"].split(","))
        bbox = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    return {
        "drive_id": archivo["id"],
        "nombre": archivo["name"],
        "subida": archivo["createdTime"],
        "enlace": archivo.get("webViewLink"),
        "estado": props.get("alpr_estado", "pendiente"),
        "placa": props.get("alpr_placa"),
        "fuente": props.get("alpr_fuente"),
        "confianza_ocr": props.get("alpr_conf_ocr"),
        "confianza_deteccion": props.get("alpr_conf_det"),
        "n_placas": props.get("alpr_n_placas"),
        "bbox": bbox,
        "procesada": props.get("alpr_procesado"),
        "nota": props.get("alpr_nota"),
    }


def listar_lecturas(limite: int, placa: str | None = None) -> list[dict]:
    """Las fotos más recientes de la carpeta con su resultado.

    Con `placa` (ya normalizada) Drive filtra por la appProperty, así que la
    búsqueda funciona aunque la foto sea vieja.
    """
    q = _query_carpeta()
    if placa:
        q += f" and appProperties has {{ key='alpr_placa' and value='{placa}' }}"
    r = (
        _drive()
        .files()
        .list(
            q=q,
            fields="files(id, name, createdTime, webViewLink, appProperties)",
            orderBy="createdTime desc",
            pageSize=limite,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return [_a_lectura(f) for f in r.get("files", [])]


def recorte_de(file_id: str) -> bytes | None:
    """Recorte JPEG de la placa de una foto ya procesada.

    Se vuelve a recortar desde la foto original con el bbox guardado, sin
    correr otra vez el modelo. None si la foto no es de la carpeta vigilada
    o no tiene placa: la service account puede ver otros archivos y este
    endpoint no debe servir para leerlos.
    """
    drive = _drive()
    try:
        archivo = drive.files().get(fileId=file_id, fields="parents, appProperties", supportsAllDrives=True).execute()
    except HttpError as e:
        if e.resp.status == 404:
            return None
        raise
    bbox = archivo.get("appProperties", {}).get("alpr_bbox")
    if settings.drive_carpeta_id not in archivo.get("parents", []) or not bbox:
        return None

    frame = alpr_service.decodificar(drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute())
    if frame is None:
        return None
    x1, y1, x2, y2 = (int(v) for v in bbox.split(","))
    recorte = alpr_service._recortar(frame, x1, y1, x2, y2, settings.margen_recorte)
    return alpr_service._a_jpeg(recorte)


def estado() -> dict:
    expira = _estado["canal_expira"]
    return {
        "carpeta_id": settings.drive_carpeta_id,
        "webhook_url": settings.drive_webhook_url,
        "canal_expira": datetime.fromtimestamp(expira, timezone.utc) if expira else None,
        "ultimo_escaneo": _estado["ultimo_escaneo"],
        "ultimo_error": _estado["ultimo_error"],
    }


# ------------------------------------------------------------------- fondo
async def ciclo_drive() -> None:
    """Tarea de fondo: mantiene vivo el canal y escanea como red de seguridad.

    El primer escaneo corre al arrancar y recoge las fotos que llegaron
    mientras el contenedor estaba dormido o reiniciándose.
    """
    while True:
        try:
            await asyncio.to_thread(asegurar_canal)
        except Exception:
            log.exception("No se pudo registrar el canal de Drive; se reintenta en la próxima vuelta")
        await asyncio.to_thread(escanear_sin_excepciones)
        await asyncio.sleep(settings.drive_intervalo_revision)

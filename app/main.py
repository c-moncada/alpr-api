"""API de reconocimiento de placas vehiculares (ALPR).

Le mandas una foto y te devuelve el texto de cada placa y su recorte en JPEG.
/detect no guarda nada (ni imágenes ni historial), así que corre en
cualquier contenedor sin disco persistente.

Arquitectura en dos etapas, que es la razón por la que esto funciona:

    detección  ->  ¿DÓNDE está la placa?   (YOLOv9, devuelve un rectángulo)
    OCR        ->  ¿QUÉ DICE ese recorte?  (CCT entrenado solo en placas)

Ambas etapas corren locales con modelos ONNX: sin API key, sin tokens y sin
cuota. El fallback a Groq es opcional y degrada solo: si falla, se usa el
resultado local y el cliente no se entera.

Opcional: vigilar una carpeta compartida de iCloud Drive y leer la placa de
cada foto nueva (endpoints /icloud/*). iCloud no deja guardar el resultado
en la propia foto, así que las lecturas y la sesión de Apple van a Postgres.

Opcional: registrar el dueño de cada placa (endpoints /propietarios, con
ADMIN_API_KEY). /detect y /icloud/lecturas lo devuelven cuando leen una
placa registrada.

Levantar en local:
    uvicorn app.main:app --reload --port 8000
Docs interactivas:
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pyicloud.exceptions import PyiCloudException

from app import __version__, alpr_service, db, icloud_service
from app.config import settings
from app.groq_fallback import normalizar_placa
from app.models import (
    CodigoRequest,
    DetectResponse,
    EscaneoResponse,
    HealthResponse,
    IcloudEstadoResponse,
    LecturasResponse,
    Placa,
    Propietario,
    PropietarioRegistrado,
    PropietariosResponse,
    SesionResponse,
)
from app.nombre_archivo import velocidad_kmh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("alpr.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not alpr_service.modelos_en_cache():
        log.warning(
            "Los modelos ONNX no están en caché: se van a descargar ahora "
            "(requiere internet). En Docker esto no debería pasar."
        )
    # Se cargan al arrancar y no en la primera petición: uvicorn no abre el
    # puerto hasta que esto termina, así Render no manda tráfico antes de tiempo.
    alpr_service.obtener_alpr()
    log.info("Fallback Groq activo: %s", settings.groq_activo)
    log.info("API key requerida: %s", bool(settings.api_key))
    log.info("Registro de propietarios activo: %s", bool(settings.admin_api_key and settings.database_url))

    if settings.database_url:
        try:
            # Antes de abrir el puerto: la petición que despierta a Render ya encuentra las tablas
            await asyncio.to_thread(db.crear_tablas)
        except Exception:
            log.exception("No se pudo preparar Postgres: revisa DATABASE_URL")

    tarea_icloud = None
    if settings.icloud_activo:
        log.info(
            "Vigilando la carpeta de iCloud %s (revisión cada %d s)",
            settings.icloud_carpeta,
            settings.icloud_intervalo_revision,
        )
        tarea_icloud = asyncio.create_task(icloud_service.ciclo_icloud())
    elif settings.icloud_habilitado:
        log.warning("ICLOUD_HABILITADO=true pero falta ICLOUD_CARPETA, ICLOUD_APPLE_ID, ICLOUD_PASSWORD o DATABASE_URL")

    yield

    if tarea_icloud:
        tarea_icloud.cancel()


app = FastAPI(
    title="API de Reconocimiento de Placas (ALPR)",
    description=__doc__,
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.lista_cors,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_header_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def _clave_valida(clave: str | None, esperada: str) -> bool:
    return bool(esperada) and bool(clave) and secrets.compare_digest(clave.encode(), esperada.encode())


def verificar_api_key(clave: str | None = Security(_header_api_key)) -> None:
    """Si API_KEY está definida, exige el header X-API-Key con ese valor.

    ADMIN_API_KEY también sirve, así en /docs basta con autorizar una vez.
    """
    if not settings.api_key:
        return
    if not (_clave_valida(clave, settings.api_key) or _clave_valida(clave, settings.admin_api_key)):
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


def _a_placa(p: dict, propietario: dict | None) -> Placa:
    jpeg = p.pop("recorte_jpeg")
    return Placa(imagen_base64=base64.b64encode(jpeg).decode("ascii"), propietario=propietario, **p)


def _propietarios(placas: list[str]) -> dict[str, dict]:
    """Los dueños registrados de estas placas. Sin Postgres, /detect sigue igual."""
    if not settings.database_url or not placas:
        return {}
    try:
        return db.propietarios_de(placas)
    except psycopg.Error:
        log.exception("No se pudo buscar el dueño de las placas")
        return {}


# ----------------------------------------------------------------- endpoints
@app.get("/health", response_model=HealthResponse, tags=["sistema"])
def health() -> HealthResponse:
    """Estado del servicio. Render lo usa como health check; no pide API key."""
    return HealthResponse(
        estado="ok",
        version=__version__,
        modelos_cargados=alpr_service.modelos_cargados(),
        detector_model=settings.detector_model,
        ocr_model=settings.ocr_model,
        umbral_confianza=settings.umbral_confianza,
        groq_activo=settings.groq_activo,
        icloud_activo=settings.icloud_activo,
    )


@app.post(
    "/detect",
    response_model=DetectResponse,
    tags=["deteccion"],
    dependencies=[Depends(verificar_api_key)],
)
def detect(
    archivo: UploadFile = File(..., description="Foto de un vehículo (JPG, PNG, WEBP o BMP)"),
) -> DetectResponse:
    """Detecta las placas de una imagen y devuelve el texto y el recorte de cada una.

    `placas` viene vacía si no se encontró ninguna; si hay varias, `placas[0]`
    es la de mayor confianza. El recorte es un JPEG en base64: en un navegador
    se muestra con `<img src="data:image/jpeg;base64,{imagen_base64}">`. Si la
    placa está registrada en `/propietarios`, `propietario` trae los datos del dueño.
    `velocidad_kmh` sale del nombre del archivo que pone la cámara
    (`..._003-5kmh.jpg` = 3.5); es None si el nombre no la trae.
    """
    limite = int(settings.max_mb_imagen * 1024 * 1024)
    contenido = archivo.file.read(limite + 1)
    if not contenido:
        raise HTTPException(status_code=400, detail="el archivo llegó vacío")
    if len(contenido) > limite:
        raise HTTPException(
            status_code=413,
            detail=f"la imagen pesa más de {settings.max_mb_imagen:g} MB",
        )

    frame = alpr_service.decodificar(contenido)
    if frame is None:
        raise HTTPException(status_code=400, detail="no se pudo decodificar la imagen")

    placas, ms = alpr_service.procesar_frame(frame)
    duenos = _propietarios([p["texto"] for p in placas if p["texto"]])
    return DetectResponse(
        placas=[_a_placa(p, duenos.get(p["texto"])) for p in placas],
        velocidad_kmh=velocidad_kmh(archivo.filename),
        ms_procesamiento=ms,
    )


# -------------------------------------------------------------- iCloud Drive
def requiere_icloud() -> None:
    if not settings.icloud_activo:
        raise HTTPException(status_code=503, detail="la vigilancia de iCloud Drive no está configurada")


_deps_icloud = [Depends(verificar_api_key), Depends(requiere_icloud)]


@app.exception_handler(icloud_service.IcloudError)
@app.exception_handler(PyiCloudException)
@app.exception_handler(httpx.HTTPError)
async def _error_icloud(request: Request, e: Exception) -> JSONResponse:
    """409 si falta el código de Apple, 400 si el código no sirve, 502 lo demás."""
    if isinstance(e, (icloud_service.FaltaCodigo, icloud_service.SinCodigoPendiente)):
        codigo = 409
    elif isinstance(e, icloud_service.CodigoInvalido):
        codigo = 400
    else:
        codigo = 502
    return JSONResponse(status_code=codigo, content={"detail": str(e)})


@app.exception_handler(psycopg.Error)
async def _error_postgres(request: Request, e: psycopg.Error) -> JSONResponse:
    log.error("Postgres: %s", e)
    return JSONResponse(status_code=503, content={"detail": "no se pudo usar Postgres: revisa DATABASE_URL"})


@app.get("/icloud/lecturas", response_model=LecturasResponse, tags=["icloud"], dependencies=_deps_icloud)
def icloud_lecturas(
    tareas: BackgroundTasks,
    limite: int = Query(50, ge=1, le=1000),
    placa: str | None = Query(None, description="Busca solo las fotos con esta placa exacta"),
) -> LecturasResponse:
    """Las fotos más recientes de la carpeta con la placa leída en cada una, y
    su dueño en `propietario` si la placa está registrada.

    Antes de responder revisa si llegaron fotos nuevas. Esas salen con
    `estado: "pendiente"` y su placa se lee en segundo plano: vuelve a pedir
    la lista en unos segundos.
    """
    icloud_service.traer_novedades()
    lecturas = icloud_service.listar_lecturas(limite, normalizar_placa(placa))
    if any(lec["estado"] == "pendiente" for lec in lecturas):
        tareas.add_task(icloud_service.escanear_sin_excepciones)
    return LecturasResponse(lecturas=lecturas)


@app.get(
    "/icloud/lecturas/{foto_id}/recorte",
    tags=["icloud"],
    dependencies=_deps_icloud,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def icloud_recorte(foto_id: str = Path(..., pattern=r"^[A-Za-z0-9-]+$")) -> Response:
    """Recorte JPEG de la placa de una foto de la carpeta.

    En React Native: `<Image source={{ uri, headers: { "X-API-Key": ... } }} />`.
    """
    jpeg = icloud_service.recorte_de(foto_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="la foto no existe, no es de la carpeta o no tiene placa")
    return Response(content=jpeg, media_type="image/jpeg")


@app.post("/icloud/escanear", response_model=EscaneoResponse, tags=["icloud"], dependencies=_deps_icloud)
def icloud_escanear() -> EscaneoResponse:
    """Revisa la carpeta y lee las fotos pendientes ya, sin esperar la revisión periódica.

    Si ya hay un escaneo corriendo devuelve 0: ese escaneo va a recoger
    también las fotos nuevas.
    """
    return EscaneoResponse(procesadas=icloud_service.escanear())


@app.get("/icloud/estado", response_model=IcloudEstadoResponse, tags=["icloud"], dependencies=_deps_icloud)
def icloud_estado() -> IcloudEstadoResponse:
    """Si hay sesión con Apple, cuántas fotos faltan por leer y el último error."""
    return IcloudEstadoResponse(**icloud_service.estado())


@app.post("/icloud/sesion", response_model=SesionResponse, tags=["icloud"], dependencies=_deps_icloud)
def icloud_sesion(tareas: BackgroundTasks) -> SesionResponse:
    """Entra a iCloud con la cuenta configurada. Úsalo cuando `/icloud/estado` diga `falta_codigo` o `rechazada`.

    Si Apple pide verificación, manda un código a los dispositivos de la
    cuenta o por SMS: pásalo a `POST /icloud/codigo`.
    """
    if icloud_service.iniciar_sesion() == "activa":
        tareas.add_task(icloud_service.escanear_sin_excepciones)
        return SesionResponse(sesion="activa", mensaje="sesión activa; revisando la carpeta")
    return SesionResponse(sesion="falta_codigo", mensaje="Apple mandó un código: pásalo a POST /icloud/codigo")


@app.post("/icloud/codigo", response_model=SesionResponse, tags=["icloud"], dependencies=_deps_icloud)
def icloud_codigo(pedido: CodigoRequest, tareas: BackgroundTasks) -> SesionResponse:
    """Completa el inicio de sesión con el código de verificación de Apple.

    Apple deja de confiar en la sesión cada unos 30 días: entonces toca
    `POST /icloud/sesion` y después este.
    """
    icloud_service.validar_codigo(pedido.codigo)
    tareas.add_task(icloud_service.escanear_sin_excepciones)
    return SesionResponse(sesion="activa", mensaje="sesión activa; revisando la carpeta")


# -------------------------------------------------------------- propietarios
def requiere_postgres() -> None:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="el registro de propietarios necesita DATABASE_URL")


def verificar_admin(clave: str | None = Security(_header_api_key)) -> None:
    """Los /propietarios piden ADMIN_API_KEY: la API_KEY va dentro de la app."""
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="define ADMIN_API_KEY para administrar los propietarios")
    if not _clave_valida(clave, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="esta ruta pide ADMIN_API_KEY en el header X-API-Key")


_deps_admin = [Depends(verificar_admin), Depends(requiere_postgres)]


def _placa_de_ruta(placa: str) -> str:
    normalizada = normalizar_placa(placa)
    if not normalizada:
        raise HTTPException(status_code=422, detail="la placa tiene que tener letras o números")
    return normalizada


@app.get("/propietarios", response_model=PropietariosResponse, tags=["propietarios"], dependencies=_deps_admin)
def propietarios_listar() -> PropietariosResponse:
    """Todas las placas registradas con los datos de su dueño."""
    return PropietariosResponse(propietarios=db.listar_propietarios())


@app.put(
    "/propietarios/{placa}",
    response_model=PropietarioRegistrado,
    tags=["propietarios"],
    dependencies=_deps_admin,
)
def propietarios_guardar(
    datos: Propietario,
    placa: str = Path(..., description="Con o sin espacios o guiones: se guarda como la lee el OCR (CVL65718)"),
) -> PropietarioRegistrado:
    """Registra el dueño de una placa, o lo reemplaza si ya estaba.

    Desde ese momento `/detect` y `/icloud/lecturas` devuelven estos datos en
    `propietario` cuando leen la placa. La comparación es exacta: si el OCR
    confunde un carácter (0/O, 1/I), no coincide.
    """
    return PropietarioRegistrado(**db.guardar_propietario(_placa_de_ruta(placa), datos.model_dump()))


@app.delete("/propietarios/{placa}", status_code=204, tags=["propietarios"], dependencies=_deps_admin)
def propietarios_borrar(placa: str = Path(..., description="Con o sin espacios o guiones")) -> Response:
    """Saca una placa del registro."""
    if not db.borrar_propietario(_placa_de_ruta(placa)):
        raise HTTPException(status_code=404, detail="esa placa no está registrada")
    return Response(status_code=204)

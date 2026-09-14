"""API de reconocimiento de placas vehiculares (ALPR).

Le mandas una foto y te devuelve el texto de cada placa y su recorte en JPEG.
No guarda nada (ni imágenes ni historial), así que corre en cualquier
contenedor sin disco persistente.

Arquitectura en dos etapas, que es la razón por la que esto funciona:

    detección  ->  ¿DÓNDE está la placa?   (YOLOv9, devuelve un rectángulo)
    OCR        ->  ¿QUÉ DICE ese recorte?  (CCT entrenado solo en placas)

Ambas etapas corren locales con modelos ONNX: sin API key, sin tokens y sin
cuota. El fallback a Groq es opcional y degrada solo: si falla, se usa el
resultado local y el cliente no se entera.

Opcional: vigilar una carpeta de Google Drive y leer la placa de cada foto
nueva (endpoints /drive/*). El resultado se guarda en la propia foto de
Drive, así que la API sigue sin disco propio.

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

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from app import __version__, alpr_service, drive_service
from app.config import settings
from app.groq_fallback import normalizar_placa
from app.models import (
    DetectResponse,
    DriveEstadoResponse,
    EscaneoResponse,
    HealthResponse,
    LecturasResponse,
    Placa,
)

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

    tarea_drive = None
    if settings.drive_activo:
        log.info(
            "Vigilando la carpeta de Drive %s (webhook: %s)",
            settings.drive_carpeta_id,
            settings.drive_webhook_url or "no, solo revisión periódica",
        )
        tarea_drive = asyncio.create_task(drive_service.ciclo_drive())
    elif settings.drive_habilitado:
        log.warning("DRIVE_HABILITADO=true pero falta DRIVE_CARPETA_ID o las credenciales")

    yield

    # El canal de Drive NO se detiene al apagar: en el plan gratis de Render,
    # el aviso de una foto nueva es justo lo que despierta al contenedor.
    if tarea_drive:
        tarea_drive.cancel()


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


def verificar_api_key(clave: str | None = Security(_header_api_key)) -> None:
    """Si API_KEY está definida, exige el header X-API-Key con ese valor."""
    if not settings.api_key:
        return
    if not clave or not secrets.compare_digest(clave.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


def _a_placa(p: dict) -> Placa:
    jpeg = p.pop("recorte_jpeg")
    return Placa(imagen_base64=base64.b64encode(jpeg).decode("ascii"), **p)


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
        drive_activo=settings.drive_activo,
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
    se muestra con `<img src="data:image/jpeg;base64,{imagen_base64}">`.
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
    return DetectResponse(placas=[_a_placa(p) for p in placas], ms_procesamiento=ms)


# ------------------------------------------------------------- Google Drive
def requiere_drive() -> None:
    if not settings.drive_activo:
        raise HTTPException(status_code=503, detail="la vigilancia de Google Drive no está configurada")


_deps_drive = [Depends(verificar_api_key), Depends(requiere_drive)]


@app.post("/drive/webhook", include_in_schema=False)
def drive_webhook(
    tareas: BackgroundTasks,
    x_goog_channel_token: str | None = Header(None),
    x_goog_resource_state: str | None = Header(None),
) -> Response:
    """Recibe los avisos de Google. No pide API key: Google no la manda.

    En su lugar se valida el token que se registró al crear el canal. Se
    responde de inmediato y se escanea en segundo plano, porque Google da el
    aviso por fallido si tardas.
    """
    if not settings.drive_activo or not x_goog_channel_token or not secrets.compare_digest(
        x_goog_channel_token.encode(), drive_service.TOKEN_CANAL.encode()
    ):
        return Response(status_code=403)
    if x_goog_resource_state != "sync":  # "sync" solo confirma que el canal quedó creado
        tareas.add_task(drive_service.escanear_sin_excepciones)
    return Response(status_code=200)


@app.get("/drive/lecturas", response_model=LecturasResponse, tags=["drive"], dependencies=_deps_drive)
def drive_lecturas(
    limite: int = Query(50, ge=1, le=1000),
    placa: str | None = Query(None, description="Busca solo las fotos con esta placa exacta"),
) -> LecturasResponse:
    """Las fotos más recientes de la carpeta con la placa leída en cada una.

    Las fotos que todavía no se procesan salen con `estado: "pendiente"`.
    """
    placa = normalizar_placa(placa)
    return LecturasResponse(lecturas=drive_service.listar_lecturas(limite, placa))


@app.get(
    "/drive/lecturas/{drive_id}/recorte",
    tags=["drive"],
    dependencies=_deps_drive,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def drive_recorte(drive_id: str = Path(..., pattern=r"^[A-Za-z0-9_-]+$")) -> Response:
    """Recorte JPEG de la placa de una foto de la carpeta.

    En React Native: `<Image source={{ uri, headers: { "X-API-Key": ... } }} />`.
    """
    jpeg = drive_service.recorte_de(drive_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="la foto no existe, no es de la carpeta o no tiene placa")
    return Response(content=jpeg, media_type="image/jpeg")


@app.post("/drive/escanear", response_model=EscaneoResponse, tags=["drive"], dependencies=_deps_drive)
def drive_escanear() -> EscaneoResponse:
    """Revisa la carpeta ya, sin esperar el aviso de Google.

    Útil en local sin webhook. Si ya hay un escaneo corriendo devuelve 0: ese
    escaneo va a recoger también las fotos nuevas.
    """
    try:
        return EscaneoResponse(procesadas=drive_service.escanear_carpeta())
    except drive_service.DriveError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/drive/estado", response_model=DriveEstadoResponse, tags=["drive"], dependencies=_deps_drive)
def drive_estado() -> DriveEstadoResponse:
    """Si el canal de avisos está vivo, cuándo se revisó la carpeta y el último error."""
    return DriveEstadoResponse(**drive_service.estado())

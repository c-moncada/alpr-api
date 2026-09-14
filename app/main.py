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

Levantar en local:
    uvicorn app.main:app --reload --port 8000
Docs interactivas:
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from app import __version__, alpr_service
from app.config import settings
from app.models import DetectResponse, HealthResponse, Placa

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
    yield


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

"""Esquemas Pydantic de entrada y salida de la API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# De dónde salió el texto final de la placa
Fuente = Literal[
    "local",                 # OCR local con confianza suficiente
    "groq",                  # el VLM corrigió al OCR local
    "local_confirmado",      # Groq coincidió con el OCR local
    "local_baja_confianza",  # confianza baja y Groq no pudo ayudar
]


class BoundingBox(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int


class Placa(BaseModel):
    """Una placa encontrada en la imagen."""

    texto: str | None = Field(None, description="Texto final de la placa, solo A-Z y 0-9")
    imagen_base64: str = Field(..., description="Recorte de la placa en JPEG, codificado en base64")
    fuente: Fuente

    confianza_deteccion: float = Field(..., description="Qué tan seguro está el detector de que hay una placa")
    confianza_ocr: float | None = Field(None, description="Confianza del carácter más débil: el número que de verdad importa")
    region: str | None = Field(None, description="País que el OCR cree que es la placa")
    bbox: BoundingBox = Field(..., description="Posición de la placa en la imagen original, en píxeles")

    texto_local: str | None = Field(None, description="Lo que leyó el OCR local, antes de cualquier fallback")
    texto_groq: str | None = Field(None, description="Lo que leyó el VLM, si se consultó")
    nota_fallback: str | None = Field(None, description="Por qué se usó o falló el fallback")


class DetectResponse(BaseModel):
    placas: list[Placa] = Field(
        ...,
        description="Vacía si no se encontró ninguna placa. Ordenada de más a menos confianza del detector.",
    )
    ms_procesamiento: float


class HealthResponse(BaseModel):
    estado: str
    version: str
    modelos_cargados: bool
    detector_model: str
    ocr_model: str
    umbral_confianza: float
    groq_activo: bool
    icloud_activo: bool


# -------------------------------------------------------------- iCloud Drive
# Qué pasó con cada foto de la carpeta vigilada
EstadoLectura = Literal[
    "pendiente",         # todavía no se procesa
    "ok",                # se encontró al menos una placa
    "sin_placa",         # el detector no encontró ninguna
    "no_decodificable",  # no es una imagen que OpenCV pueda abrir (ej. HEIC)
    "muy_grande",        # pesa más de MAX_MB_IMAGEN
    "error",             # falló el pipeline; el detalle va en `nota`
]

# Si la API puede entrar a iCloud
Sesion = Literal[
    "activa",        # hay sesión y Apple confía en ella
    "falta_codigo",  # Apple pidió el código de verificación: POST /icloud/codigo
    "sin_iniciar",   # todavía no se intentó entrar (la API recién arrancó)
]


class Lectura(BaseModel):
    """Resultado de una foto de la carpeta de iCloud Drive."""

    id: str = Field(..., description="ID de la foto en iCloud; sirve para pedir el recorte")
    nombre: str
    subida: datetime = Field(..., description="Cuándo se subió la foto a iCloud")
    estado: EstadoLectura
    placa: str | None = Field(None, description="Texto de la placa más clara de la foto")
    fuente: Fuente | None = None
    confianza_ocr: float | None = None
    confianza_deteccion: float | None = None
    n_placas: int | None = Field(None, description="Cuántas placas se detectaron en la foto")
    bbox: BoundingBox | None = None
    procesada: datetime | None = Field(None, description="Cuándo se leyó la placa")
    nota: str | None = None


class LecturasResponse(BaseModel):
    lecturas: list[Lectura] = Field(..., description="De la foto más reciente a la más antigua")


class EscaneoResponse(BaseModel):
    procesadas: int = Field(..., description="Fotos nuevas procesadas en este escaneo")


class IcloudEstadoResponse(BaseModel):
    carpeta: str | None = Field(None, description="Nombre de la carpeta compartida")
    sesion: Sesion
    ultimo_escaneo: datetime | None
    ultimo_error: str | None
    fotos: int | None = Field(None, description="Fotos registradas; None si no se pudo leer Postgres")
    pendientes: int | None = Field(None, description="Fotos que todavía no se leen")


class CodigoRequest(BaseModel):
    codigo: str = Field(..., pattern=r"^\d{6}$", description="Los 6 dígitos que mandó Apple")


class SesionResponse(BaseModel):
    sesion: Sesion
    mensaje: str

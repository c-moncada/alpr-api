"""Esquemas Pydantic de salida de la API."""

from __future__ import annotations

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

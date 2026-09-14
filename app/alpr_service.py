"""Núcleo del sistema: detección local + decisión de fallback + recorte.

No toca el disco: recibe la imagen en memoria y devuelve, por cada placa, el
texto y el recorte en JPEG. El modelo se carga una sola vez y se reutiliza.
Las sesiones de ONNX Runtime no son seguras para uso concurrente sin
cuidado, así que toda la inferencia pasa por un lock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

from app.config import settings
from app.groq_fallback import leer_placa_con_groq, normalizar_placa

log = logging.getLogger("alpr")

_alpr = None
_lock = threading.Lock()

CALIDAD_JPEG = 95


# --------------------------------------------------------------------- modelo
def obtener_alpr():
    """Carga el modelo la primera vez que se necesita y lo memoriza.

    La API lo llama al arrancar (ver `lifespan` en main.py), así la primera
    petición no paga los ~2 s de inicializar ONNX.
    """
    global _alpr
    if _alpr is None:
        with _lock:
            if _alpr is None:
                from fast_alpr import ALPR  # import tardío: pesa ~2s

                t0 = time.perf_counter()
                _alpr = ALPR(
                    detector_model=settings.detector_model,
                    ocr_model=settings.ocr_model,
                    detector_conf_thresh=settings.detector_conf_thresh,
                    ocr_device=settings.ocr_device,
                )
                log.info(
                    "Modelos cargados en %.2fs (%s + %s)",
                    time.perf_counter() - t0,
                    settings.detector_model,
                    settings.ocr_model,
                )
    return _alpr


def modelos_cargados() -> bool:
    return _alpr is not None


def modelos_en_cache() -> bool:
    """¿Están los .onnx en disco? Si es False, cargar el modelo pide internet."""
    home = Path.home()
    det = home / ".cache" / "open-image-models" / settings.detector_model
    ocr = home / ".cache" / "fast-plate-ocr" / settings.ocr_model
    return any(det.glob("*.onnx")) and any(ocr.glob("*.onnx"))


# ---------------------------------------------------------------- utilidades
def decodificar(contenido: bytes) -> np.ndarray | None:
    """Bytes de un JPG/PNG/WEBP/BMP a imagen BGR. None si no es una imagen válida."""
    return cv2.imdecode(np.frombuffer(contenido, np.uint8), cv2.IMREAD_COLOR)


def _confianza_min(valor, largo_texto: int) -> float | None:
    """Confianza del carácter más débil de la lectura.

    OJO: fast-plate-ocr devuelve una LISTA con la confianza de cada carácter,
    y la lista puede ser más larga que el texto porque el modelo trabaja con
    longitud fija y rellena. Las posiciones de relleno traen confianzas
    altísimas, así que nos quedamos con las primeras `largo_texto`.

    Se usa el mínimo y no el promedio: una placa con seis caracteres al 0.99
    y uno al 0.40 está mal leída, aunque promedie 0.91.
    """
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, Sequence):
        vals = [float(v) for v in valor]
        if largo_texto and len(vals) >= largo_texto:
            vals = vals[:largo_texto]
        return min(vals) if vals else None
    return None


def _recortar(frame: np.ndarray, x1: int, y1: int, x2: int, y2: int, margen: int) -> np.ndarray:
    h, w = frame.shape[:2]
    return frame[
        max(0, y1 - margen) : min(h, y2 + margen),
        max(0, x1 - margen) : min(w, x2 + margen),
    ]


def _a_jpeg(img: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), CALIDAD_JPEG])
    if not ok:
        raise RuntimeError("no se pudo codificar el recorte a JPEG")
    return buffer.tobytes()


# ------------------------------------------------------------------ pipeline
def procesar_frame(frame: np.ndarray) -> tuple[list[dict], float]:
    """Corre el pipeline completo sobre una imagen ya cargada en memoria.

    Devuelve (placas, ms_totales). Cada placa trae el texto final, de dónde
    salió, las confianzas y `recorte_jpeg` con los bytes del recorte. La
    lista va ordenada de más a menos confianza del detector y viene vacía si
    no se encontró ninguna placa.
    """
    t0 = time.perf_counter()
    alpr = obtener_alpr()
    with _lock:
        resultados = alpr.predict(frame)

    placas: list[dict] = []
    for r in resultados:
        bb = r.detection.bounding_box
        x1, y1, x2, y2 = int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)

        recorte = _recortar(frame, x1, y1, x2, y2, settings.margen_recorte)
        if recorte.size == 0:
            continue  # caja degenerada, fuera de la imagen
        recorte_jpeg = _a_jpeg(recorte)

        texto_local = normalizar_placa(r.ocr.text if r.ocr else None)
        conf_min = _confianza_min(r.ocr.confidence if r.ocr else None, len(texto_local or ""))

        texto = texto_local
        texto_groq = None
        nota = None

        # ----------------------------------------------- decisión
        if conf_min is not None and conf_min >= settings.umbral_confianza:
            fuente = "local"
        elif settings.groq_activo:
            texto_groq, nota = leer_placa_con_groq(recorte_jpeg)
            if texto_groq and texto_groq == texto_local:
                fuente = "local_confirmado"
            elif texto_groq:
                fuente = "groq"
                texto = texto_groq
            else:
                fuente = "local_baja_confianza"
        else:
            fuente = "local_baja_confianza"
            nota = "confianza bajo el umbral y el fallback está desactivado"

        placas.append(
            {
                "texto": texto,
                "recorte_jpeg": recorte_jpeg,
                "fuente": fuente,
                "confianza_deteccion": round(float(r.detection.confidence), 4),
                "confianza_ocr": round(conf_min, 4) if conf_min is not None else None,
                "region": getattr(r.ocr, "region", None) if r.ocr else None,
                "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "texto_local": texto_local,
                "texto_groq": texto_groq,
                "nota_fallback": nota,
            }
        )

    placas.sort(key=lambda p: p["confianza_deteccion"], reverse=True)
    return placas, round((time.perf_counter() - t0) * 1000, 2)

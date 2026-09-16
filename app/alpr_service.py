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


# ----------------------------------------------------------------- detección
# Caja detectada en coordenadas de la imagen completa: (x1, y1, x2, y2, confianza)
Caja = tuple[int, int, int, int, float]

# Ancho / alto mínimo de una caja hallada en un mosaico. Las placas de moto
# rondan 1.5; los falsos positivos de zonas lisas son cuadrados (~1.0).
PROPORCION_MIN_PLACA = 1.3


def _posiciones(largo: int, tamano: int, paso: int) -> list[int]:
    """Inicios de los mosaicos sobre un eje; el último queda pegado al borde."""
    if largo <= tamano:
        return [0]
    pos = list(range(0, largo - tamano, paso))
    pos.append(largo - tamano)
    return pos


def _detectar_en(detector, img: np.ndarray, dx: int = 0, dy: int = 0) -> list[Caja]:
    cajas = []
    for d in detector.predict(img):
        bb = d.bounding_box
        cajas.append((int(bb.x1) + dx, int(bb.y1) + dy, int(bb.x2) + dx, int(bb.y2) + dy, float(d.confidence)))
    return cajas


def _parece_placa(c: Caja) -> bool:
    """En zonas lisas (pared, piso) el detector marca casi todo el mosaico como
    placa, con una caja cuadrada. Una placa siempre es más ancha que alta."""
    return (c[2] - c[0]) >= PROPORCION_MIN_PLACA * (c[3] - c[1])


def _cortada(c: Caja, x: int, y: int, tamano: int, w: int, h: int, tolerancia: int = 2) -> bool:
    """¿La caja toca un borde del mosaico que no es borde de la foto?

    Esa placa quedó partida: el detector suele dar una caja más chica con buena
    confianza, y el OCR lee caracteres de menos. Como el solape es mayor que la
    placa, la versión entera aparece en el mosaico vecino.
    """
    return (
        (x > 0 and c[0] - x <= tolerancia)
        or (y > 0 and c[1] - y <= tolerancia)
        or (x + tamano < w and x + tamano - c[2] <= tolerancia)
        or (y + tamano < h and y + tamano - c[3] <= tolerancia)
    )


def _fusionar(cajas: list[Caja]) -> list[Caja]:
    """Quita las cajas repetidas, quedándose con la de más confianza.

    Se compara contra el área de la caja MÁS CHICA y no con IoU: una placa
    cortada por el borde de un mosaico sale como una caja dentro de la caja
    completa, y su IoU puede ser bajo aunque sea la misma placa.
    """
    elegidas: list[Caja] = []
    for c in sorted(cajas, key=lambda c: c[4], reverse=True):
        repetida = False
        for e in elegidas:
            ancho = min(c[2], e[2]) - max(c[0], e[0])
            alto = min(c[3], e[3]) - max(c[1], e[1])
            if ancho <= 0 or alto <= 0:
                continue
            menor = min((c[2] - c[0]) * (c[3] - c[1]), (e[2] - e[0]) * (e[3] - e[1]))
            if menor > 0 and ancho * alto / menor > 0.5:
                repetida = True
                break
        if not repetida:
            elegidas.append(c)
    return elegidas


def detectar_cajas(detector, frame: np.ndarray) -> list[Caja]:
    """Detecta placas en la foto completa y, según `mosaicos`, también por mosaicos.

    El detector reduce la imagen a su resolución de entrada (512 px): en una
    foto de 1920 px una placa de 130 px le llega de ~35 px y no la ve.
    Corriéndolo sobre mosaicos solapados de `mosaico_tamano` px, la placa le
    llega más grande que en la foto original. El solape tiene que ser mayor
    que la placa más ancha que se busca, para que quede entera en algún mosaico.
    """
    cajas = _detectar_en(detector, frame)

    h, w = frame.shape[:2]
    tamano = settings.mosaico_tamano
    modo = settings.mosaicos
    if modo == "nunca" or (modo == "si_no_detecta" and cajas) or max(h, w) <= tamano:
        return cajas

    paso = max(1, int(tamano * (1 - settings.mosaico_solape)))
    for y in _posiciones(h, tamano, paso):
        for x in _posiciones(w, tamano, paso):
            encontradas = _detectar_en(detector, frame[y : y + tamano, x : x + tamano], x, y)
            cajas.extend(
                c for c in encontradas if _parece_placa(c) and not _cortada(c, x, y, tamano, w, h)
            )

    return _fusionar(cajas)


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
    h, w = frame.shape[:2]
    with _lock:
        cajas = detectar_cajas(alpr.detector, frame)
        # El OCR lee el recorte sin margen, igual que ALPR.predict
        lecturas = []
        for x1, y1, x2, y2, conf in cajas:
            x1, y1, x2, y2 = max(x1, 0), max(y1, 0), min(x2, w), min(y2, h)
            placa = frame[y1:y2, x1:x2]
            ocr = alpr.ocr.predict(placa) if placa.size else None
            lecturas.append((x1, y1, x2, y2, conf, ocr))

    placas: list[dict] = []
    for x1, y1, x2, y2, conf_det, ocr in lecturas:
        recorte = _recortar(frame, x1, y1, x2, y2, settings.margen_recorte)
        if recorte.size == 0 or x2 <= x1 or y2 <= y1:
            continue  # caja degenerada, fuera de la imagen
        recorte_jpeg = _a_jpeg(recorte)

        texto_local = normalizar_placa(ocr.text if ocr else None)
        conf_min = _confianza_min(ocr.confidence if ocr else None, len(texto_local or ""))

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
                "confianza_deteccion": round(conf_det, 4),
                "confianza_ocr": round(conf_min, 4) if conf_min is not None else None,
                "region": getattr(ocr, "region", None) if ocr else None,
                "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                "texto_local": texto_local,
                "texto_groq": texto_groq,
                "nota_fallback": nota,
            }
        )

    placas.sort(key=lambda p: p["confianza_deteccion"], reverse=True)
    return placas, round((time.perf_counter() - t0) * 1000, 2)

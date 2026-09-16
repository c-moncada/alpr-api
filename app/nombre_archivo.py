"""Datos que la cámara escribe en el nombre de la foto.

La app de la cámara guarda cada foto como

    2026-09-16_15-38-17-681_003-5kmh.jpg

donde `003-5kmh` es la velocidad medida: 3.5 km/h. Como en el nombre de un
archivo no conviene un punto de más, el decimal va separado con un guion.
"""

from __future__ import annotations

import re

# 003-5kmh -> 3.5 | 12kmh -> 12 | 3.5kmh o 3,5kmh también se aceptan
_VELOCIDAD = re.compile(r"(\d+)(?:[-.,](\d+))?\s*km/?h", re.IGNORECASE)


def velocidad_kmh(nombre: str | None) -> float | None:
    """La velocidad en km/h del nombre de la foto, o None si no la trae."""
    if not nombre:
        return None
    coincidencias = _VELOCIDAD.findall(nombre)
    if not coincidencias:
        return None
    entero, decimales = coincidencias[-1]  # la última: la fecha va antes
    return float(f"{int(entero)}.{decimales or 0}")

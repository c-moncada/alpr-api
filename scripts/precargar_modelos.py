"""Descarga los modelos ONNX al caché local y verifica que todo corre.

El Dockerfile lo corre durante el build, así los modelos quedan dentro de
la imagen y el contenedor arranca sin descargar nada. En local, córrelo una
vez con internet después de instalar las dependencias.

    python scripts/precargar_modelos.py

Sale con código 1 si algo falla, para que un build roto no llegue a producción.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app.alpr_service import modelos_en_cache, obtener_alpr, procesar_frame  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> int:
    print("=" * 62)
    print("PRECARGA DE MODELOS ALPR")
    print("=" * 62)
    print(f"Detector : {settings.detector_model}")
    print(f"OCR      : {settings.ocr_model}")
    print(f"Caché    : {Path.home() / '.cache'}")
    print()

    print("Descargando/cargando modelos...", flush=True)
    t0 = time.perf_counter()
    try:
        obtener_alpr()
    except Exception as e:
        print(f"  FALLO: {type(e).__name__}: {e}")
        print("\n  Si el error es de red, necesitas internet para ESTA corrida.")
        return 1
    print(f"  OK en {time.perf_counter() - t0:.2f}s")

    print("\nProbando inferencia con una imagen sintética...", flush=True)
    dummy = np.zeros((480, 640, 3), dtype=np.uint8)
    try:
        placas, ms = procesar_frame(dummy)
        print(f"  OK en {ms:.1f}ms ({len(placas)} placa(s), se esperaban 0)")
    except Exception as e:
        print(f"  FALLO: {type(e).__name__}: {e}")
        return 1

    print()
    print("=" * 62)
    if not modelos_en_cache():
        print("ERROR: los modelos cargaron pero no están en el caché esperado:")
        print(f"  {Path.home() / '.cache' / 'open-image-models'}")
        print(f"  {Path.home() / '.cache' / 'fast-plate-ocr'}")
        print("Sin eso, cada arranque los volvería a descargar.")
        print("=" * 62)
        return 1

    print("LISTO: los modelos están en disco.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

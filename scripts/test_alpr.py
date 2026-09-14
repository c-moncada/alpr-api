"""Prueba el pipeline directamente, sin levantar la API.

Útil para medir precisión sobre tu propio set de fotos antes de decidir
si necesitas fine-tuning, y para depurar sin meter HTTP en medio.

    python scripts/test_alpr.py ruta/a/una/carpeta
    python scripts/test_alpr.py foto.jpg
    python scripts/test_alpr.py ruta/a/una/carpeta --recortes salida/

Con --recortes guarda el recorte de cada placa: es exactamente la imagen
que /detect devuelve en `imagen_base64`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alpr_service import decodificar, procesar_frame  # noqa: E402
from app.config import settings  # noqa: E402

EXTENSIONES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Corre el ALPR sobre fotos locales, sin HTTP.")
    parser.add_argument("objetivo", type=Path, help="una imagen o una carpeta con imágenes")
    parser.add_argument("--recortes", type=Path, help="carpeta donde guardar el recorte de cada placa")
    args = parser.parse_args()

    objetivo = args.objetivo.expanduser()
    if objetivo.is_file():
        imagenes = [objetivo]
    elif objetivo.is_dir():
        imagenes = sorted(
            p for p in objetivo.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONES
        )
    else:
        print(f"ERROR: no existe {objetivo}")
        return 1

    if not imagenes:
        print(f"No hay imágenes en {objetivo}")
        print(f"Extensiones aceptadas: {', '.join(sorted(EXTENSIONES))}")
        return 1

    if args.recortes:
        args.recortes.mkdir(parents=True, exist_ok=True)

    print(f"Detector: {settings.detector_model}")
    print(f"OCR     : {settings.ocr_model}")
    print(f"Umbral  : {settings.umbral_confianza}   Groq: {settings.groq_activo}")
    print("-" * 78)
    print(f"{'archivo':<28} {'placa':<12} {'conf.ocr':>9} {'fuente':<22} {'ms':>6}")
    print("-" * 78)

    total = leidas = 0
    ms_acum = 0.0

    for ruta in imagenes:
        # read_bytes + imdecode en vez de cv2.imread: imread devuelve None en
        # silencio con rutas que traen acentos o ñ en Windows.
        try:
            frame = decodificar(ruta.read_bytes())
            if frame is None:
                raise ValueError("no es una imagen")
            placas, ms = procesar_frame(frame)
        except Exception as e:
            print(f"{ruta.name[:27]:<28} {'ERROR':<12} {'':>9} {type(e).__name__:<22}")
            continue

        ms_acum += ms
        if not placas:
            print(f"{ruta.name[:27]:<28} {'--':<12} {'--':>9} {'sin_deteccion':<22} {ms:>6.0f}")
            continue

        for i, p in enumerate(placas):
            total += 1
            if p["texto"]:
                leidas += 1
            conf = p["confianza_ocr"]
            print(
                f"{ruta.name[:27]:<28} "
                f"{(p['texto'] or '--'):<12} "
                f"{(f'{conf:.4f}' if conf is not None else '--'):>9} "
                f"{p['fuente']:<22} "
                f"{ms:>6.0f}"
            )
            if p["texto_groq"] and p["texto_groq"] != p["texto_local"]:
                print(f"{'':<28} └─ local leyó '{p['texto_local']}', Groq corrigió a '{p['texto_groq']}'")
            if p["nota_fallback"]:
                print(f"{'':<28} └─ {p['nota_fallback']}")
            if args.recortes:
                (args.recortes / f"{ruta.stem}_placa{i}.jpg").write_bytes(p["recorte_jpeg"])

    print("-" * 78)
    print(f"Imágenes: {len(imagenes)}   Placas: {total}   Con texto: {leidas}")
    print(f"Promedio: {ms_acum / len(imagenes):.0f} ms por imagen")
    if args.recortes:
        print(f"Recortes en: {args.recortes}")
    print()
    print("Compara estas placas contra las reales a mano. Ese porcentaje es")
    print("tu accuracy base, y es lo que decide si necesitas fine-tuning.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

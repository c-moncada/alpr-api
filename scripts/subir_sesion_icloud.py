"""Abre la sesión de iCloud desde esta PC y la guarda en Postgres.

Para cuando la API en Render no puede completar el inicio de sesión: Apple
no manda el código a tiempo, o el contenedor se duerme entre pedir el
código y pasárselo. Aquí el código se escribe en la terminal, y si en esta
PC ya hay una sesión de confianza de la cuenta, se reusa sin pedir nada.

    python scripts/subir_sesion_icloud.py
    python scripts/subir_sesion_icloud.py --apple-id cuenta@icloud.com

Usa DATABASE_URL del entorno o del .env; si no está, la pide (no se ve al
escribirla). Después llama POST /icloud/sesion en la API.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyicloud import PyiCloudService  # noqa: E402
from pyicloud.base import resolve_cookie_directory  # noqa: E402
from pyicloud.exceptions import PyiCloudFailedLoginException  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402


def abrir_sesion(apple_id: str) -> PyiCloudService | None:
    """La sesión de confianza de esta PC, o una nueva con contraseña y código."""
    carpeta = resolve_cookie_directory()
    # Primero sin contraseña: si la sesión guardada no sirve, falla sin intentar entrar
    try:
        api = PyiCloudService(apple_id, None, cookie_directory=carpeta)
        if api.is_trusted_session and not api.requires_2fa:
            print("Hay una sesión de confianza en esta PC: se reusa.")
            return api
    except PyiCloudFailedLoginException:
        pass

    password = getpass.getpass("Contraseña de la cuenta de Apple (no se ve al escribirla): ")
    try:
        api = PyiCloudService(apple_id, password, cookie_directory=carpeta)
    except PyiCloudFailedLoginException as e:
        print(f"ERROR: Apple rechazó el correo o la contraseña ({e})")
        return None
    if api.requires_2fa:
        if api.security_key_names:
            print("ERROR: la cuenta usa llaves de seguridad; este script solo maneja códigos")
            return None
        api.request_2fa_code()
        codigo = input("Código de verificación que te llegó: ").strip()
        if not api.validate_2fa_code(codigo):
            print("ERROR: Apple rechazó el código")
            return None
        if not api.is_trusted_session:
            api.trust_session()
    return api


def main() -> int:
    parser = argparse.ArgumentParser(description="Abre la sesión de iCloud en esta PC y la guarda en Postgres.")
    parser.add_argument("--apple-id", help="el mismo correo que ICLOUD_APPLE_ID en Render")
    args = parser.parse_args()
    apple_id = (args.apple_id or input("Apple ID (igual que ICLOUD_APPLE_ID en Render): ")).strip()

    api = abrir_sesion(apple_id)
    if api is None:
        return 1

    # Los mismos dos archivos que la API restaura en Render
    rutas = [Path(api.session.session_path), Path(api.session.cookiejar_path)]
    archivos = {r.name: r.read_text(encoding="utf-8") for r in rutas}

    settings.database_url = settings.database_url or getpass.getpass(
        "DATABASE_URL de Neon (no se ve al escribirla): "
    ).strip()
    db.crear_tablas()
    db.guardar("sesion", json.dumps(archivos, sort_keys=True))
    db.guardar("falta_codigo", None)

    print(f"Listo: sesión de confianza guardada en Postgres ({', '.join(archivos)}).")
    print('Ahora llama POST /icloud/sesion en /docs: debe responder "sesion": "activa".')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

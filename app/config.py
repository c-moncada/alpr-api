"""Configuración central del backend ALPR.

Todo se controla por variables de entorno (o un archivo .env en local), así
en Render se ajusta desde el panel sin tocar una sola línea de código.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raíz del proyecto (carpeta que contiene app/)
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----------------------------------------------------------------- modelos
    # Detectores, de más rápido a más preciso:
    #   yolo-v9-t-256 / 384 / 416 / 512 / 640 / yolo-v9-s-608
    # El Dockerfile hornea en la imagen los modelos que estén como default aquí.
    detector_model: str = "yolo-v9-t-512-license-plate-end2end"
    # OCR: cct-xs-v2-global-model (más rápido) | cct-s-v2-global-model (más preciso)
    #      european-plates-mobile-vit-v2-model | global-plates-mobile-vit-v2-model
    #      argentinian-plates-cnn-model
    ocr_model: str = "cct-s-v2-global-model"
    # Umbral del detector: por debajo de esto no se considera placa
    detector_conf_thresh: float = 0.4
    # cpu | cuda | auto
    ocr_device: str = "cpu"

    # -------------------------------------------------------------- decisiones
    # Si la confianza del OCR local baja de esto, se intenta el fallback.
    # Se compara contra el carácter MÁS DÉBIL de la placa, no contra el promedio.
    umbral_confianza: float = 0.85

    # ----------------------------------------------------------------- entrada
    # Tamaño máximo de la imagen subida. En el plan gratis de Render (512 MB
    # de RAM) una foto enorme ya decodificada puede tumbar el proceso.
    max_mb_imagen: float = 10.0
    # Píxeles de margen alrededor del bbox al recortar la placa. Aplica al
    # recorte que se devuelve y al que se manda a Groq: un poco de contexto
    # ayuda a leerla, demasiado distrae.
    margen_recorte: int = 12

    # --------------------------------------------------------------- seguridad
    # Si se define, POST /detect exige el header X-API-Key con este valor.
    api_key: str = ""
    # Orígenes permitidos para CORS, separados por coma. "*" = cualquiera.
    cors_origenes: str = "*"

    # ------------------------------------------------------------ Groq (opcional)
    groq_habilitado: bool = False
    groq_api_key: str = ""
    groq_model: str = "qwen/qwen3.6-27b"
    # Modelo al que se cae si el principal responde 404 (modelos en preview
    # pueden desaparecer con poco aviso)
    groq_model_respaldo: str = "qwen/qwen3.8-27b"
    groq_timeout: float = 8.0
    groq_base_url: str = "https://api.groq.com/openai/v1/chat/completions"

    # ---------------------------------------------------- iCloud Drive (opcional)
    # Vigila una carpeta compartida y lee las placas de cada foto nueva (ver icloud_service.py)
    icloud_habilitado: bool = False
    # El enlace de la carpeta compartida: https://www.icloud.com/iclouddrive/...
    icloud_carpeta: str = ""
    # Cuenta de Apple con la que entra la API. Tiene que haber agregado la
    # carpeta a su iCloud Drive. Mejor una cuenta solo para esto que la personal.
    icloud_apple_id: str = ""
    icloud_password: str = ""
    # Postgres donde se guardan la sesión de Apple y las lecturas. En Render
    # gratis no hay disco, así que tiene que ser externo (ej. Neon, gratis).
    database_url: str = ""
    # Cada cuántos segundos se revisa la carpeta mientras el contenedor está
    # despierto. Si no hay cambios, cada revisión es una sola petición a Apple.
    icloud_intervalo_revision: int = 120

    @field_validator("icloud_carpeta")
    @classmethod
    def _codigo_de_carpeta(cls, valor: str) -> str:
        """Acepta el enlace completo, que es lo que se suele pegar, o solo el código."""
        m = re.search(r"/iclouddrive/([A-Za-z0-9_-]+)", valor)
        return m.group(1) if m else valor.strip()

    @field_validator("icloud_apple_id", "icloud_password")
    @classmethod
    def _sin_espacios(cls, valor: str) -> str:
        """Quita los espacios que se cuelan al pegar en el panel de Render."""
        return valor.strip()

    @property
    def groq_activo(self) -> bool:
        """Groq solo se usa si está habilitado Y hay API key."""
        return self.groq_habilitado and bool(self.groq_api_key.strip())

    @property
    def icloud_activo(self) -> bool:
        """iCloud solo se vigila si está habilitado y no falta ningún dato."""
        datos = (self.icloud_carpeta, self.icloud_apple_id, self.icloud_password, self.database_url)
        return self.icloud_habilitado and all(d.strip() for d in datos)

    @property
    def lista_cors(self) -> list[str]:
        return [o.strip() for o in self.cors_origenes.split(",") if o.strip()]


settings = Settings()

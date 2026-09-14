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

    # ---------------------------------------------------- Google Drive (opcional)
    # Vigila una carpeta y lee las placas de cada foto nueva (ver drive_service.py)
    drive_habilitado: bool = False
    # Lo que va después de /folders/ en la URL de la carpeta
    drive_carpeta_id: str = ""
    # Credenciales de la service account: el JSON completo pegado en una
    # variable (cómodo en Render) o la ruta al archivo (cómodo en local).
    google_service_account_json: str = ""
    google_service_account_file: str = ""
    # URL pública HTTPS de esta API, sin ruta. Google manda los avisos a
    # {url_publica}/drive/webhook. En Render se toma sola de RENDER_EXTERNAL_URL.
    # Si no hay ninguna, no se registra el webhook y queda solo la revisión periódica.
    url_publica: str = ""
    render_external_url: str = ""
    # Cada cuántos segundos se escanea la carpeta aunque no llegue ningún aviso
    drive_intervalo_revision: int = 600

    @field_validator("drive_carpeta_id")
    @classmethod
    def _id_de_carpeta(cls, valor: str) -> str:
        """Acepta el ID o la URL completa de la carpeta, que es lo que se suele pegar."""
        m = re.search(r"/folders/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)", valor)
        return (m.group(1) or m.group(2)) if m else valor.strip()

    @property
    def groq_activo(self) -> bool:
        """Groq solo se usa si está habilitado Y hay API key."""
        return self.groq_habilitado and bool(self.groq_api_key.strip())

    @property
    def drive_activo(self) -> bool:
        """Drive solo se vigila si está habilitado, hay carpeta y hay credenciales."""
        credenciales = self.google_service_account_json.strip() or self.google_service_account_file.strip()
        return self.drive_habilitado and bool(self.drive_carpeta_id.strip()) and bool(credenciales)

    @property
    def drive_webhook_url(self) -> str | None:
        """URL que se registra en Google. None si no hay una URL HTTPS pública."""
        base = (self.url_publica or self.render_external_url).strip().rstrip("/")
        return f"{base}/drive/webhook" if base.startswith("https://") else None

    @property
    def lista_cors(self) -> list[str]:
        return [o.strip() for o in self.cors_origenes.split(",") if o.strip()]


settings = Settings()

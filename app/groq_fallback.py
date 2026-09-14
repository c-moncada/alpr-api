"""Fallback opcional: segunda opinión de un VLM sobre el RECORTE de la placa.

Por qué sobre el recorte y no sobre la foto completa: Groq comprime cada
imagen a ~2,048 tokens sin importar su resolución. Si la placa es el 1% de la
foto, le tocan ~20 tokens y el modelo adivina. Mandando solo el recorte, esos
2,048 tokens son todos placa.

Reglas de oro de este módulo:
  1. NUNCA lanza una excepción hacia arriba. Si Groq falla, se cae con
     elegancia al resultado local y el cliente no se entera.
  2. Timeout corto y explícito. Una API colgada no bloquea la respuesta.
  3. Si el modelo principal ya no existe (404, común en modelos preview),
     reintenta una sola vez con el modelo de respaldo.
"""

from __future__ import annotations

import base64
import json
import re

import httpx

from app.config import settings

PROMPT = (
    "Esta imagen es el recorte de una placa vehicular. "
    "Devuelve únicamente JSON con esta forma exacta: "
    '{"placa": "TEXTO", "legible": true}. '
    "Escribe la placa en mayúsculas, sin espacios, guiones ni puntos, "
    "solo los caracteres alfanuméricos que realmente ves. "
    "Si no puedes leerla con seguridad, devuelve "
    '{"placa": null, "legible": false}. '
    "No expliques nada, no agregues texto fuera del JSON."
)


def normalizar_placa(texto: str | None) -> str | None:
    """Deja solo A-Z y 0-9 en mayúsculas, para poder comparar dos lecturas."""
    if not texto:
        return None
    limpio = re.sub(r"[^A-Z0-9]", "", texto.upper())
    return limpio or None


def _extraer_json(contenido: str) -> dict:
    """Saca el primer objeto JSON del texto.

    Los modelos con modo 'thinking' a veces envuelven la respuesta en prosa,
    así que no confiamos en que el body sea JSON puro.
    """
    try:
        return json.loads(contenido)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*?\}", contenido, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"respuesta no parseable: {contenido[:200]!r}")


def _pedir(b64: str, modelo: str) -> str:
    payload = {
        "model": modelo,
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=settings.groq_timeout) as cliente:
        resp = cliente.post(settings.groq_base_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def leer_placa_con_groq(recorte_jpeg: bytes) -> tuple[str | None, str]:
    """Devuelve (placa_normalizada_o_None, nota_explicativa).

    Nunca propaga excepciones: cualquier problema se reporta en la nota.
    """
    if not settings.groq_activo:
        return None, "groq_desactivado"
    if not recorte_jpeg:
        return None, "recorte_vacio"

    b64 = base64.b64encode(recorte_jpeg).decode("ascii")

    modelos = [settings.groq_model]
    if settings.groq_model_respaldo and settings.groq_model_respaldo != settings.groq_model:
        modelos.append(settings.groq_model_respaldo)

    ultimo_error = "sin_intentos"
    for i, modelo in enumerate(modelos):
        try:
            datos = _extraer_json(_pedir(b64, modelo))
            if not datos.get("legible", True):
                return None, f"groq_dice_ilegible ({modelo})"
            placa = normalizar_placa(datos.get("placa"))
            if not placa:
                return None, f"groq_sin_placa ({modelo})"
            return placa, f"groq_ok ({modelo})"

        except httpx.HTTPStatusError as e:
            codigo = e.response.status_code
            ultimo_error = f"groq_http_{codigo}"
            # 404 = el modelo ya no existe (típico en preview). 400 puede ser
            # que el modelo dejó de aceptar imágenes. En ambos casos vale
            # reintentar con el respaldo; en los demás, no.
            if codigo in (404, 400) and i + 1 < len(modelos):
                continue
            return None, ultimo_error
        except httpx.TimeoutException:
            return None, f"groq_timeout_{settings.groq_timeout}s"
        except httpx.HTTPError as e:
            return None, f"groq_red: {type(e).__name__}"
        except (ValueError, KeyError, TypeError) as e:
            return None, f"groq_respuesta_invalida: {type(e).__name__}"

    return None, ultimo_error

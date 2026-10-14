"""Cliente mínimo de la API de Gemini (sin librerías externas).

Clave en /var/lib/healthsync/secrets/gemini_api_key (600, usuario healthsync). Proyecto de Google
Cloud en el nivel de pago: así Google no usa lo que se le manda para entrenar sus modelos.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import SECRETS

KEY_FILE = SECRETS / "gemini_api_key"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"


class GeminiError(Exception):
    pass


def generar(modelo: str, prompt: str, *, buscar: bool = False, esquema: dict | None = None,
            timeout: int = 180) -> str:
    """Texto de la respuesta. buscar=True deja a Gemini buscar en Google; esquema = JSON Schema
    de la respuesta (con esquema no busca: por eso son dos llamadas distintas)."""
    cuerpo: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if buscar:
        cuerpo["tools"] = [{"google_search": {}}]
    if esquema:
        cuerpo["generationConfig"] = {"responseMimeType": "application/json", "responseJsonSchema": esquema}
    pet = urllib.request.Request(URL.format(modelo=modelo), json.dumps(cuerpo).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-goog-api-key": KEY_FILE.read_text().strip()})
    for intento in range(4):
        try:
            with urllib.request.urlopen(pet, timeout=timeout) as r:
                datos = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and intento < 3:
                time.sleep(10 * 2 ** intento)
                continue
            raise GeminiError(f"Gemini {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if intento < 1:                                # una respuesta lenta no se repite más de una vez
                time.sleep(10 * 2 ** intento)
                continue
            raise GeminiError(f"Gemini no responde: {e}") from e
    try:
        partes = datos["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise GeminiError(f"Respuesta sin contenido: {json.dumps(datos)[:300]}") from e
    return "".join(p.get("text", "") for p in partes if not p.get("thought"))

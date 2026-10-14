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
            razonamiento: str | None = None, max_salida: int = 16384, timeout: int = 180) -> str:
    """Texto de la respuesta. buscar=True deja a Gemini buscar en Google; esquema = JSON Schema
    de la respuesta (con esquema no busca: por eso son dos llamadas distintas). razonamiento: low,
    medium o high (cuánto piensa antes de responder; más tarda más). max_salida: tope de tokens generados
    (razonamiento incluido), que es lo que más cuesta."""
    cuerpo: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if buscar:
        cuerpo["tools"] = [{"google_search": {}}]
    conf: dict = {"maxOutputTokens": max_salida}     # el razonamiento también gasta de aquí
    if esquema:
        conf |= {"responseMimeType": "application/json", "responseJsonSchema": esquema}
    if razonamiento:
        conf["thinkingConfig"] = {"thinkingLevel": razonamiento}
    cuerpo["generationConfig"] = conf
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
        cand = datos["candidates"][0]
        partes = cand["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise GeminiError(f"Respuesta sin contenido: {json.dumps(datos)[:300]}") from e
    uso = datos.get("usageMetadata", {})
    print(f"[gemini] {datos.get('modelVersion', modelo)}: {uso.get('promptTokenCount', '?')} de entrada, "
          f"{uso.get('candidatesTokenCount', '?')} de salida, {uso.get('thoughtsTokenCount', 0)} pensando", flush=True)
    if cand.get("finishReason") not in (None, "STOP"):
        raise GeminiError(f"Respuesta incompleta ({cand['finishReason']}, "
                          f"{datos.get('usageMetadata', {}).get('thoughtsTokenCount', '?')} tokens pensando)")
    return "".join(p.get("text", "") for p in partes if not p.get("thought"))

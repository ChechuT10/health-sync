"""Cliente mínimo de Google Sheets (registro del gimnasio).

Usa el mismo token que Google Health, con el permiso drive.file: la Pi solo ve los archivos que
crea ella (el Sheet del gimnasio), no el resto de tu Drive.
"""
from __future__ import annotations

import urllib.parse

from google.auth.transport.requests import AuthorizedSession

from .api import request

URL = "https://sheets.googleapis.com/v4/spreadsheets"


def crear(s: AuthorizedSession, titulo: str, primera_pestana: str) -> str:
    """Crea el Sheet (en español y hora de Madrid, para que «22,5» sea un número) y devuelve su id."""
    r = request(s, "POST", URL, json={"properties": {"title": titulo, "locale": "es_ES", "timeZone": "Europe/Madrid"},
                                      "sheets": [{"properties": {"title": primera_pestana}}]})
    return r["spreadsheetId"]


def enlace(id_hoja: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{id_hoja}"


def pestanas(s: AuthorizedSession, id_hoja: str) -> dict[str, int]:
    r = request(s, "GET", f"{URL}/{id_hoja}", params={"fields": "sheets.properties(sheetId,title)"})
    return {h["properties"]["title"]: h["properties"]["sheetId"] for h in r.get("sheets", [])}


def lote(s: AuthorizedSession, id_hoja: str, peticiones: list[dict]) -> dict:
    return request(s, "POST", f"{URL}/{id_hoja}:batchUpdate", json={"requests": peticiones})


def nueva_pestana(s: AuthorizedSession, id_hoja: str, titulo: str, indice: int = 0) -> int:
    existentes = pestanas(s, id_hoja)
    if titulo in existentes:
        return existentes[titulo]
    r = lote(s, id_hoja, [{"addSheet": {"properties": {"title": titulo, "index": indice,
                                                       "gridProperties": {"frozenRowCount": 1}}}}])
    return r["replies"][0]["addSheet"]["properties"]["sheetId"]


def _rango(rango: str) -> str:
    return urllib.parse.quote(rango, safe="")


def escribir(s: AuthorizedSession, id_hoja: str, rango: str, valores: list[list]) -> None:
    request(s, "PUT", f"{URL}/{id_hoja}/values/{_rango(rango)}", params={"valueInputOption": "USER_ENTERED"},
            json={"values": valores})


def escribir_varios(s: AuthorizedSession, id_hoja: str, datos: list[tuple[str, list[list]]]) -> None:
    if datos:
        request(s, "POST", f"{URL}/{id_hoja}/values:batchUpdate",
                json={"valueInputOption": "USER_ENTERED",
                      "data": [{"range": r, "values": v} for r, v in datos]})


def leer(s: AuthorizedSession, id_hoja: str, rango: str) -> list[list]:
    r = request(s, "GET", f"{URL}/{id_hoja}/values/{_rango(rango)}",
                params={"valueRenderOption": "UNFORMATTED_VALUE"})
    return r.get("values", [])

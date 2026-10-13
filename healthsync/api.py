"""Cliente mínimo de la Google Health API v4: sesión OAuth, reintentos y paginación."""
from __future__ import annotations

import time
from datetime import date, timedelta

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from .config import API, TOKEN_FILE


class ApiError(Exception):
    pass


def session() -> AuthorizedSession:
    """Sesión autorizada con el token de 'fase0.py auth'. Renueva y guarda el token si caducó."""
    if not TOKEN_FILE.exists():
        raise ApiError(f"No hay token en {TOKEN_FILE}: ejecuta 'fase0.py auth'")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"No se pudo renovar el token ({e}); vuelve a ejecutar 'fase0.py auth'") from e
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(creds.to_json())
        tmp.chmod(0o600)
        tmp.replace(TOKEN_FILE)
    return AuthorizedSession(creds)


def request(s: AuthorizedSession, method: str, url: str, **kw) -> dict:
    for intento in range(5):
        r = s.request(method, url, timeout=60, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** intento)
            continue
        if not r.ok:
            raise ApiError(f"{r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}
    raise ApiError(f"Demasiados reintentos en {url}")


def list_all(s: AuthorizedSession, data_type: str, params: dict | None = None,
             max_pages: int = 200) -> list[dict]:
    """Todas las páginas de un 'list' (siguiendo nextPageToken)."""
    out, token = [], None
    for _ in range(max_pages):
        p = dict(params or {})
        if token:
            p["pageToken"] = token
        data = request(s, "GET", f"{API}/dataTypes/{data_type}/dataPoints", params=p)
        out.extend(data.get("dataPoints", []))
        token = data.get("nextPageToken")
        if not token:
            return out
    raise ApiError(f"{data_type}: límite de {max_pages} páginas alcanzado")


def list_recientes(s: AuthorizedSession, data_type: str, fecha_de, desde: date,
                   page_size: int = 25, max_pages: int = 400) -> list[dict]:
    """'list' sin filtro (p. ej. sleep, que no admite filtrar por fecha). La API devuelve primero
    lo más reciente: para cuando dos páginas seguidas son enteras anteriores a 'desde'."""
    out, token, viejas = [], None, 0
    for _ in range(max_pages):
        p = {"pageSize": page_size}
        if token:
            p["pageToken"] = token
        data = request(s, "GET", f"{API}/dataTypes/{data_type}/dataPoints", params=p)
        pagina = [x for x in data.get("dataPoints", []) if (fecha_de(x) or "9999") >= desde.isoformat()]
        out.extend(pagina)
        viejas = 0 if pagina else viejas + 1
        token = data.get("nextPageToken")
        if not token or viejas >= 2:
            return out
    raise ApiError(f"{data_type}: límite de {max_pages} páginas alcanzado")


def daily_rollup(s: AuthorizedSession, data_type: str, start: date, end_excl: date,
                 max_days: int) -> list[dict]:
    """dailyRollUp troceado en bloques de max_days (límite de la API para cada tipo)."""
    out, cur = [], start
    while cur < end_excl:
        nxt = min(cur + timedelta(days=max_days), end_excl)
        body = {
            "range": {
                "start": {"date": {"year": cur.year, "month": cur.month, "day": cur.day}},
                "end": {"date": {"year": nxt.year, "month": nxt.month, "day": nxt.day}},
            },
            "windowSizeDays": 1,
        }
        while True:
            data = request(s, "POST", f"{API}/dataTypes/{data_type}/dataPoints:dailyRollUp", json=body)
            out.extend(data.get("rollupDataPoints", []))
            if not data.get("nextPageToken"):
                break
            body["pageToken"] = data["nextPageToken"]
        cur = nxt
    return out

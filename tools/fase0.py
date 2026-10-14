#!/usr/bin/env python3
"""
Health Sync - Herramienta de fase 0: autorización OAuth y descarga de prueba.

Uso (en la Raspberry, como usuario healthsync):
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py auth
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py test
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py test --desde 2026-09-01 --hasta 2026-09-30
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py dump
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py dump --intradia
  sudo -u healthsync /opt/healthsync/venv/bin/python /opt/healthsync/app/tools/fase0.py dump --desde 2026-06-01

'auth' necesita un túnel SSH abierto desde tu ordenador:
  ssh -L 8765:localhost:8765 usuario@IP_DE_TU_RASPBERRY

Solo lee datos: 'test' y 'dump' no escriben nada en Google Health.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
DATA_DIR = Path("/var/lib/healthsync")
SECRETS = DATA_DIR / "secrets"
CLIENT_FILE = SECRETS / "client_secret.json"
TOKEN_FILE = SECRETS / "token.json"
OUT_DIR = DATA_DIR / "db" / "fase0"
API = "https://health.googleapis.com/v4/users/me"
TZ = ZoneInfo("Europe/Madrid")
PORT = 8765

P = "https://www.googleapis.com/auth/googlehealth."
SCOPES = [
    P + "activity_and_fitness.readonly",
    P + "health_metrics_and_measurements.readonly",
    P + "health_metrics_and_measurements.writeonly",  # peso y % de grasa (fases siguientes)
    P + "sleep.readonly",
    P + "nutrition.readonly",
    P + "nutrition.writeonly",                         # comidas (fases siguientes)
    P + "profile.readonly",
    "https://www.googleapis.com/auth/drive.file",   # solo los archivos que crea la Pi (Sheet del gimnasio)
]


def info(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def ok(msg: str) -> None:
    print(f"\033[1;32m  ✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"\033[1;31m  ✗\033[0m {msg}")


# --------------------------------------------------------------------------
# Credenciales
# --------------------------------------------------------------------------
def save_token(creds: Credentials) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(creds.to_json())
    tmp.chmod(0o600)
    tmp.replace(TOKEN_FILE)


def load_session() -> AuthorizedSession:
    if not TOKEN_FILE.exists():
        fail(f"No hay token en {TOKEN_FILE}. Ejecuta primero el comando 'auth'.")
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            fail(f"No se pudo renovar el token: {e}")
            fail("Si la app estuvo en 'Prueba', el token caducó: vuelve a ejecutar 'auth'.")
            sys.exit(1)
        save_token(creds)
    return AuthorizedSession(creds)


def cmd_auth(_args) -> None:
    if not CLIENT_FILE.exists():
        fail(f"Falta {CLIENT_FILE}. Copia ahí el JSON del cliente OAuth de escritorio.")
        sys.exit(1)
    info("Autorización OAuth")
    print(f"""
  1. Asegúrate de tener abierto en tu ordenador el túnel:
       ssh -L {PORT}:localhost:{PORT} usuario@IP_DE_TU_RASPBERRY
  2. Abre en el navegador de tu ordenador el enlace que aparece abajo.
  3. Elige tu cuenta de Google. Si sale "Google no ha verificado esta aplicación",
     pulsa "Configuración avanzada" y luego "Ir a ... (no seguro)": es tu propia app.
  4. Marca TODAS las casillas de permisos y pulsa "Continuar".
""")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=PORT,
        open_browser=False,
        authorization_prompt_message="  Enlace de autorización:\n\n  {url}\n",
        success_message="Autorización completada. Ya puedes cerrar esta pestaña y volver a la terminal.",
        access_type="offline",
        prompt="consent",
    )
    if not creds.refresh_token:
        fail("Google no devolvió refresh token. Revoca el acceso en myaccount.google.com/permissions y repite.")
        sys.exit(1)
    granted = set(creds.granted_scopes or [])
    missing = [s.replace(P, "") for s in SCOPES if granted and s not in granted]
    save_token(creds)
    ok(f"Token guardado en {TOKEN_FILE} (permisos 600)")
    if missing:
        fail("No se concedieron estos permisos (marca todas las casillas y repite 'auth'):")
        for m in missing:
            print(f"      - {m}")

    s = AuthorizedSession(creds)
    r = s.get(f"{API}/identity", timeout=30)
    if r.ok:
        ok("Cuenta vinculada a Google Health correctamente")
    else:
        fail(f"La llamada de identidad falló ({r.status_code}): {r.text[:300]}")


# --------------------------------------------------------------------------
# Llamadas a la API (con reintentos y paginación)
# --------------------------------------------------------------------------
class ApiError(Exception):
    pass


def request(s: AuthorizedSession, method: str, url: str, **kw) -> dict:
    for intento in range(5):
        r = s.request(method, url, timeout=60, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            espera = 2 ** intento
            print(f"     ({r.status_code}, reintento en {espera} s)")
            time.sleep(espera)
            continue
        if not r.ok:
            raise ApiError(f"{r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}
    raise ApiError(f"Demasiados reintentos en {url}")


def list_all(s, data_type: str, params: dict | None = None, max_pages: int = 60) -> list[dict]:
    """Recorre todas las páginas de un 'list' (siguiendo nextPageToken)."""
    out, token = [], None
    for _ in range(max_pages):
        p = dict(params or {})
        if token:
            p["pageToken"] = token
        data = request(s, "GET", f"{API}/dataTypes/{data_type}/dataPoints", params=p)
        out.extend(data.get("dataPoints", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def daily_rollup(s, data_type: str, start: date, end_excl: date, max_days: int) -> list[dict]:
    """dailyRollUp troceado en bloques de max_days (límite de la API)."""
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
        token = None
        while True:
            if token:
                body["pageToken"] = token
            data = request(s, "POST", f"{API}/dataTypes/{data_type}/dataPoints:dailyRollUp", json=body)
            out.extend(data.get("rollupDataPoints", []))
            token = data.get("nextPageToken")
            if not token:
                break
        body.pop("pageToken", None)
        cur = nxt
    return out


# --------------------------------------------------------------------------
# Utilidades de fechas y valores
# --------------------------------------------------------------------------
def civil_to_date(c: dict | None) -> date | None:
    d = (c or {}).get("date")
    if not d:
        return None
    return date(int(d["year"]), int(d["month"]), int(d["day"]))


def ts_local(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)


def first_number(obj) -> float | None:
    """Devuelve el primer valor numérico de un objeto (las APIs nuevas varían los nombres)."""
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, str):
        try:
            return float(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        for v in obj.values():
            n = first_number(v)
            if n is not None:
                return n
    return None


# --------------------------------------------------------------------------
# Prueba de descarga
# --------------------------------------------------------------------------
def cmd_test(args) -> None:
    start = date.fromisoformat(args.desde)
    end = date.fromisoformat(args.hasta)
    end_excl = end + timedelta(days=1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = load_session()
    days = [start + timedelta(days=i) for i in range((end_excl - start).days)]
    table: dict[date, dict] = {d: {} for d in days}
    errores = 0

    info(f"Descarga de prueba del {start:%d/%m/%Y} al {end:%d/%m/%Y}")

    # Identidad
    try:
        request(s, "GET", f"{API}/identity")
        ok("Acceso a la API correcto")
    except ApiError as e:
        fail(f"Identidad: {e}")
        sys.exit(1)

    # Pasos: agregación diaria (unifica reloj y móvil sin duplicar)
    try:
        pts = daily_rollup(s, "steps", start, end_excl, 90)
        (OUT_DIR / "steps_rollup.json").write_text(json.dumps(pts, indent=1))
        for p in pts:
            d = civil_to_date(p.get("civilStartTime"))
            if d in table and "steps" in p:
                table[d]["pasos"] = first_number(p["steps"])
        ok(f"Pasos: {sum(1 for d in days if 'pasos' in table[d])} días con datos")
    except ApiError as e:
        errores += 1
        fail(f"Pasos: {e}")

    # Calorías totales: agregación diaria (máximo 14 días por llamada)
    try:
        pts = daily_rollup(s, "total-calories", start, end_excl, 14)
        (OUT_DIR / "total_calories_rollup.json").write_text(json.dumps(pts, indent=1))
        for p in pts:
            d = civil_to_date(p.get("civilStartTime"))
            if d in table and "totalCalories" in p:
                table[d]["kcal"] = first_number(p["totalCalories"])
        ok(f"Calorías: {sum(1 for d in days if 'kcal' in table[d])} días con datos")
    except ApiError as e:
        errores += 1
        fail(f"Calorías: {e}")

    # Pulso en reposo diario
    try:
        pts = list_all(s, "daily-resting-heart-rate", {"pageSize": 1000})
        (OUT_DIR / "resting_hr.json").write_text(json.dumps(pts, indent=1))
        for p in pts:
            v = p.get("dailyRestingHeartRate", {})
            d = civil_to_date({"date": v.get("date")}) if v.get("date") else None
            if d in table and v.get("beatsPerMinute") is not None:
                table[d]["fc_reposo"] = float(v["beatsPerMinute"])
        ok(f"Pulso en reposo: {sum(1 for d in days if 'fc_reposo' in table[d])} días con datos")
    except ApiError as e:
        errores += 1
        fail(f"Pulso en reposo: {e}")

    # Sueño: todas las páginas; cada noche cuenta en el día en que te despiertas
    try:
        flt = f'sleep.interval.civil_end_time >= "{start.isoformat()}T00:00:00"'
        try:
            pts = list_all(s, "sleep", {"filter": flt, "pageSize": 25})
        except ApiError:
            pts = list_all(s, "sleep", {"pageSize": 25})  # por si el filtro no se admite
        (OUT_DIR / "sleep.json").write_text(json.dumps(pts, indent=1))
        sleep_min: dict[date, float] = defaultdict(float)
        naps = 0
        for p in pts:
            sl = p.get("sleep", {})
            meta = sl.get("metadata", {})
            if meta.get("nap"):
                naps += 1
                continue
            iv = sl.get("interval", {})
            wake = civil_to_date(iv.get("civilEndTime"))
            if wake is None:
                end_local = ts_local(iv.get("endTime"))
                wake = end_local.date() if end_local else None
            mins = first_number((sl.get("summary") or {}).get("minutesAsleep"))
            if wake in table and mins is not None:
                sleep_min[wake] += mins
        for d, m in sleep_min.items():
            table[d]["sueno_h"] = m / 60
        ok(f"Sueño: {len(sleep_min)} noches con datos ({naps} siestas excluidas)")
    except ApiError as e:
        errores += 1
        fail(f"Sueño: {e}")

    # Entrenamientos: una fila por sesión, asignada al día (hora de Madrid) en que empieza
    sesiones: list[dict] = []
    metric_keys: set[str] = set()
    try:
        flt = f'exercise.interval.civil_start_time >= "{start.isoformat()}T00:00:00"'
        try:
            pts = list_all(s, "exercise", {"filter": flt, "pageSize": 25})
        except ApiError:
            pts = list_all(s, "exercise", {"pageSize": 25})
        (OUT_DIR / "exercise.json").write_text(json.dumps(pts, indent=1))
        for p in pts:
            ex = p.get("exercise", {})
            iv = ex.get("interval", {})
            ini = ts_local(iv.get("startTime"))
            if ini is None or not (start <= ini.date() < end_excl):
                continue
            fin = ts_local(iv.get("endTime"))
            ms = ex.get("metricsSummary") or {}
            metric_keys.update(ms.keys())
            dur = first_number(str(ex.get("activeDuration", "")).rstrip("s")) if ex.get("activeDuration") else None
            if dur is None and fin:
                dur = (fin - ini).total_seconds()
            fc = next((first_number(v) for k, v in ms.items()
                       if "heart" in k.lower() and ("avg" in k.lower() or "average" in k.lower())), None)
            sesiones.append({
                "inicio": ini,
                "tipo": ex.get("displayName") or ex.get("exerciseType") or "?",
                "min": dur / 60 if dur else None,
                "kcal": first_number(ms.get("caloriesKcal")) if "caloriesKcal" in ms else None,
                "fc_media": fc,
            })
        sesiones.sort(key=lambda x: x["inicio"])
        for x in sesiones:
            table[x["inicio"].date()].setdefault("entrenos", 0)
            table[x["inicio"].date()]["entrenos"] += 1
        ok(f"Entrenamientos: {len(sesiones)} sesiones")
    except ApiError as e:
        errores += 1
        fail(f"Entrenamientos: {e}")

    # Tabla resumen
    info("Resumen por día (vacío = sin dato, nunca 0)")
    def fmt(v, f: str, width: int) -> str:
        return f.format(v) if v is not None else "—".rjust(width)
    print(f"\n  {'Fecha':<12}{'Pasos':>9}{'Kcal':>8}{'Sueño':>8}{'FC rep.':>9}{'Entr.':>7}  Tipo de día")
    print("  " + "-" * 72)
    for d in days:
        r = table[d]
        if "sueno_h" in r:
            tipo = "completo"
        elif "fc_reposo" in r:
            tipo = "sin noche (reloj solo de día)"
        elif "pasos" in r:
            tipo = "solo móvil"
        else:
            tipo = "sin datos"
        print(
            f"  {d:%d/%m/%Y}  "
            f"{fmt(r.get('pasos'), '{:>9,.0f}', 9).replace(',', '.')}"
            f"{fmt(r.get('kcal'), '{:>8,.0f}', 8).replace(',', '.')}"
            f"{fmt(r.get('sueno_h'), '{:>7.1f}h', 8)}"
            f"{fmt(r.get('fc_reposo'), '{:>9.0f}', 9)}"
            f"{fmt(r.get('entrenos'), '{:>7d}', 7)}"
            f"  {tipo}"
        )
    print()

    if sesiones:
        info("Sesiones de entrenamiento")
        print(f"\n  {'Fecha':<12}{'Hora':<7}{'Tipo':<30}{'Min':>5}{'Kcal':>7}{'FC media':>10}")
        print("  " + "-" * 71)
        for x in sesiones:
            print(
                f"  {x['inicio']:%d/%m/%Y}  {x['inicio']:%H:%M}  "
                f"{str(x['tipo'])[:28]:<30}"
                f"{fmt(x['min'], '{:>5.0f}', 5)}"
                f"{fmt(x['kcal'], '{:>7.0f}', 7)}"
                f"{fmt(x['fc_media'], '{:>10.0f}', 10)}"
            )
        print()
        if metric_keys:
            print(f"  Métricas disponibles por sesión: {', '.join(sorted(metric_keys))}\n")
    ok(f"JSON en bruto guardado en {OUT_DIR} para revisarlo")
    if errores:
        fail(f"{errores} tipo(s) de dato fallaron; mándame el mensaje de error.")
    else:
        ok("Prueba completada sin errores")


# --------------------------------------------------------------------------
# Descarga completa: todos los tipos de dato que permite la API
# --------------------------------------------------------------------------
# (tipo, método, límite de días por llamada)
#   "list"    -> registros tal cual (sesiones, muestras, resúmenes diarios)
#   "rollup"  -> agregado por día (tipos minuto a minuto: evita millones de filas)
DUMP_TYPES: list[tuple[str, str, int]] = [
    # Resúmenes diarios del reloj
    ("daily-resting-heart-rate", "list", 0),
    ("daily-heart-rate-variability", "list", 0),
    ("daily-heart-rate-zones", "list", 0),
    ("daily-oxygen-saturation", "list", 0),
    ("daily-respiratory-rate", "list", 0),
    ("daily-sleep-temperature-derivations", "list", 0),
    ("daily-vo2-max", "list", 0),
    # Sesiones
    ("sleep", "list", 0),
    ("exercise", "list", 0),
    ("nutrition-log", "list", 0),
    ("hydration-log", "list", 0),
    # Muestras puntuales
    ("weight", "list", 0),
    ("body-fat", "list", 0),
    ("height", "list", 0),
    ("vo2-max", "list", 0),
    ("run-vo2-max", "list", 0),
    ("respiratory-rate-sleep-summary", "list", 0),
    ("blood-glucose", "list", 0),
    ("core-body-temperature", "list", 0),
    # Actividad minuto a minuto, agregada por día
    ("steps", "rollup", 90),
    ("distance", "rollup", 90),
    ("floors", "rollup", 90),
    ("altitude", "rollup", 90),
    ("active-minutes", "rollup", 14),
    ("active-zone-minutes", "rollup", 90),
    ("active-energy-burned", "rollup", 90),
    ("sedentary-period", "rollup", 90),
    ("time-in-heart-rate-zone", "rollup", 90),
    ("swim-lengths-data", "rollup", 90),
    ("total-calories", "rollup", 14),
    ("calories-in-heart-rate-zone", "rollup", 14),
    ("heart-rate", "rollup", 14),
]
# Datos de alta resolución: solo con --intradia (pueden ser cientos de miles de filas al mes)
INTRADAY_TYPES = ["heart-rate", "heart-rate-variability", "oxygen-saturation", "steps", "activity-level"]
# Tipos que se descargan mes a mes (si una página falla, solo se reintenta ese mes)
MONTHLY_TYPES = {"exercise", "sleep"}

ISO_DATE = re.compile(r'"(20\d\d-\d\d-\d\d)T')
CIVIL_DATE = re.compile(r'"year":\s*(\d{4}),\s*"month":\s*(\d{1,2}),\s*"day":\s*(\d{1,2})')


def dates_in(point: dict) -> list[date]:
    txt = json.dumps(point)
    out = [date.fromisoformat(m) for m in ISO_DATE.findall(txt)]
    out += [date(int(y), int(mo), int(d)) for y, mo, d in CIVIL_DATE.findall(txt)]
    return out


def list_to_file(s, data_type: str, path: Path, params: dict, max_pages: int,
                 mode: str = "w", seen: set | None = None,
                 since: date | None = None) -> tuple[int, list[date], ApiError | None]:
    """Descarga un 'list' página a página y lo escribe en JSONL sin cargarlo entero en memoria.
    Con 'since', solo guarda registros desde esa fecha y para cuando dos páginas seguidas son anteriores.
    Si una página falla, conserva lo ya descargado y devuelve el error."""
    n, rango, token, pages, err, old_pages = 0, [], None, 0, None, 0
    with path.open(mode) as f:
        while pages < max_pages:
            p = dict(params)
            if token:
                p["pageToken"] = token
            try:
                data = request(s, "GET", f"{API}/dataTypes/{data_type}/dataPoints", params=p)
            except ApiError as e:
                err = e
                break
            page_pts = data.get("dataPoints", [])
            if since is not None and page_pts:
                recientes = []
                for pt in page_pts:
                    ds = [d for d in dates_in(pt) if d.year >= 2000]  # ignora fechas de nacimiento
                    if ds and max(ds) >= since:
                        recientes.append(pt)
                old_pages = old_pages + 1 if not recientes else 0
                page_pts = recientes
            for pt in page_pts:
                if seen is not None:
                    key = pt.get("name") or json.dumps(pt, sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                f.write(json.dumps(pt) + "\n")
                ds = [d for d in dates_in(pt) if d.year >= 2000]
                if ds:
                    rango += [min(ds), max(ds)]
                n += 1
            pages += 1
            token = data.get("nextPageToken")
            if not token or old_pages >= 2:
                break
            if pages % 20 == 0:
                print(f"     ... {n:,} registros".replace(",", "."))
        else:
            err = ApiError(f"límite de {max_pages} páginas alcanzado (descarga incompleta)")
    return n, rango, err


def list_monthly(s, data_type: str, path: Path, since: date) -> tuple[int, list[date], list[str]]:
    """Descarga un tipo de sesión mes a mes con filtro de fecha. Devuelve los meses que fallaron."""
    campo = data_type.replace("-", "_")
    total, rango, fallidos, seen = 0, [], [], set()
    path.write_text("")
    cur = date(since.year, since.month, 1)
    hoy = date.today()
    while cur <= hoy:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        flt = (f'{campo}.interval.civil_start_time >= "{cur.isoformat()}T00:00:00" AND '
               f'{campo}.interval.civil_start_time < "{nxt.isoformat()}T00:00:00"')
        n, r, err = list_to_file(s, data_type, path, {"filter": flt, "pageSize": 25}, 100, mode="a", seen=seen)
        total += n
        rango += r
        if err is not None:
            if str(err).startswith("400") and total == 0 and not fallidos:
                raise err  # el filtro no se admite: lo gestiona quien llama
            # Un registro dañado tumba el mes entero: se reintenta día a día para aislarlo
            print(f"     {data_type} {cur:%m/%Y} falla; reintentando día a día…")
            d = cur
            while d < nxt:
                d2 = d + timedelta(days=1)
                fd = (f'{campo}.interval.civil_start_time >= "{d.isoformat()}T00:00:00" AND '
                      f'{campo}.interval.civil_start_time < "{d2.isoformat()}T00:00:00"')
                n, r, err_d = list_to_file(s, data_type, path, {"filter": fd, "pageSize": 25}, 20, mode="a", seen=seen)
                total += n
                rango += r
                if err_d is not None:
                    fallidos.append(f"{d:%d/%m/%Y}")
                d = d2
        cur = nxt
    return total, rango, fallidos


def classify_error(e: ApiError) -> str:
    msg = str(e)
    if msg.startswith("403"):
        return "sin permiso"
    if msg.startswith("400") or msg.startswith("404") or msg.startswith("501"):
        return "no admitido"
    return "error"


def cmd_dump(args) -> None:
    start = date.fromisoformat(args.desde)
    end_excl = date.fromisoformat(args.hasta) + timedelta(days=1)
    raw = DATA_DIR / "db" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for f in raw.iterdir():  # empieza limpio: borra la descarga anterior
        if f.is_file():
            f.unlink()
    s = load_session()
    info(f"Descarga completa del {start:%d/%m/%Y} al {end_excl - timedelta(days=1):%d/%m/%Y}")
    print(f"  Los archivos se guardan en {raw}\n")
    resumen: list[tuple[str, str, int, str, str]] = []

    def fila(tipo, metodo, n, rango, estado):
        r = f"{min(rango):%d/%m/%y}–{max(rango):%d/%m/%y}" if rango else ""
        resumen.append((tipo, metodo, n, r, estado))
        marca = "\033[1;32m✓\033[0m" if estado == "ok" else ("·" if estado == "sin datos" else "\033[1;33m!\033[0m")
        print(f"  {marca} {tipo:<38}{n:>8}  {estado}")

    # Perfil y dispositivos
    for nombre, url in [("perfil", f"{API}/profile"), ("dispositivos", f"{API}/pairedDevices")]:
        try:
            data = request(s, "GET", url)
            (raw / f"{nombre}.json").write_text(json.dumps(data, indent=1))
            fila(nombre, "get", 1, [], "ok")
        except ApiError as e:
            fila(nombre, "get", 0, [], classify_error(e))

    # Todos los tipos
    for tipo, metodo, max_days in DUMP_TYPES:
        nombre_metodo = "por día" if metodo == "rollup" else "registros"
        try:
            if metodo == "rollup":
                pts = daily_rollup(s, tipo, start, end_excl, max_days)
                pts = [p for p in pts if len(p) > 2]  # quita días sin valor
                (raw / f"{tipo}.daily.json").write_text(json.dumps(pts, indent=1))
                rango = [d for p in pts for d in [civil_to_date(p.get("civilStartTime"))] if d]
                fila(tipo, nombre_metodo, len(pts), rango, "ok" if pts else "sin datos")
                continue
            if tipo in MONTHLY_TYPES:
                try:
                    n, rango, fallidos = list_monthly(s, tipo, raw / f"{tipo}.jsonl", start)
                    estado = ("ok" if n else "sin datos") if not fallidos else f"parcial: fallan {', '.join(fallidos)}"
                    fila(tipo, "mes a mes", n, rango, estado)
                    continue
                except ApiError:
                    pass  # sin filtro por fechas: descarga normal
            n, rango, err = list_to_file(s, tipo, raw / f"{tipo}.jsonl", {"pageSize": 1000}, 200, since=start)
            if err is not None and n == 0 and "pageSize" in str(err):
                n, rango, err = list_to_file(s, tipo, raw / f"{tipo}.jsonl", {"pageSize": 25}, 400, since=start)
            if err is None:
                fila(tipo, nombre_metodo, n, rango, "ok" if n else "sin datos")
            else:
                (raw / f"{tipo}.error.txt").write_text(str(err))
                fila(tipo, nombre_metodo, n, rango, "parcial" if n else classify_error(err))
        except ApiError as e:
            fila(tipo, nombre_metodo, 0, [], classify_error(e))
            (raw / f"{tipo}.error.txt").write_text(str(e))

    # Alta resolución
    if args.intradia:
        print()
        info("Datos de alta resolución (puede tardar varios minutos)")
        civ = f"{start.isoformat()}T00:00:00"
        civ_end = f"{end_excl.isoformat()}T00:00:00"
        for tipo in INTRADAY_TYPES:
            campo = tipo.replace("-", "_")
            filtros = [
                f'{campo}.interval.civil_start_time >= "{civ}" AND {campo}.interval.civil_start_time < "{civ_end}"',
                f'{campo}.sample_time.civil_time >= "{civ}" AND {campo}.sample_time.civil_time < "{civ_end}"',
            ]
            ultimo = None
            for flt in filtros:
                n, rango, err = list_to_file(s, tipo, raw / f"{tipo}.intradia.jsonl",
                                             {"pageSize": 10000, "filter": flt}, 2000)
                if err is None or n:
                    estado = ("ok" if n else "sin datos") if err is None else "parcial"
                    fila(f"{tipo} (intradía)", "registros", n, rango, estado)
                    ultimo = None
                    break
                ultimo = err
            if ultimo is not None:
                fila(f"{tipo} (intradía)", "registros", 0, [], classify_error(ultimo))
                (raw / f"{tipo}.intradia.error.txt").write_text(str(ultimo))

    # Resumen
    print()
    info("Resumen")
    print(f"\n  {'Tipo de dato':<40}{'Método':<11}{'Filas':>9}  {'Desde–hasta':<19}Estado")
    print("  " + "-" * 92)
    for tipo, metodo, n, r, estado in resumen:
        print(f"  {tipo:<40}{metodo:<11}{n:>9}  {r:<19}{estado}")
    total = sum(x[2] for x in resumen)
    con = sum(1 for x in resumen if x[2] > 0)
    tam = sum(f.stat().st_size for f in raw.iterdir() if f.is_file()) / 1e6
    print()
    total_txt = f"{total:,}".replace(",", ".")
    ok(f"{con} tipos con datos, {total_txt} filas, {tam:.1f} MB en disco")
    print("  Notas:")
    print("   - 'sin datos' es normal si tu reloj o tu cuenta no registran ese tipo.")
    print("   - Si algo sale como 'error' o 'no admitido', mándame la línea y el archivo .error.txt.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Health Sync, fase 0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="Autoriza el acceso a Google Health (una vez)")
    t = sub.add_parser("test", help="Descarga de prueba de un rango de fechas")
    t.add_argument("--desde", default="2026-09-01")
    t.add_argument("--hasta", default="2026-09-30")
    d = sub.add_parser("dump", help="Descarga todos los tipos de dato disponibles")
    d.add_argument("--desde", default="2026-01-01")
    d.add_argument("--hasta", default=(date.today() - timedelta(days=1)).isoformat())
    d.add_argument("--intradia", action="store_true", help="Incluye datos minuto a minuto y segundo a segundo")
    args = ap.parse_args()
    {"auth": cmd_auth, "test": cmd_test, "dump": cmd_dump}[args.cmd](args)


if __name__ == "__main__":
    main()

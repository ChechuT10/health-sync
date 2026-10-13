"""De los registros de la API (capa raw) a las tablas limpias.

Reglas (comprobadas con los datos reales, ver docs/datos.md):
- Un dato que falta es NULL, nunca 0. Los 0 de las métricas fisiológicas también son "sin dato".
- La API manda muchos enteros como texto ("76"): se convierten.
- Las fechas son civiles de Madrid. El sueño cuenta para el día en que te despiertas.
- Noche = sesión con mainSleep y sin nap (hay siestas de tarde con las dos marcas).
- Solo se usa lo posterior a FECHA_INICIO y lo que no está en la tabla descartes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import COBERTURA_COMPLETO, COBERTURA_SIN_RELOJ, FECHA_INICIO

ROLLUP = ":dia"   # sufijo de los tipos agregados por día (dailyRollUp), p. ej. "steps:dia"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def campo(tipo: str) -> str:
    """'daily-resting-heart-rate:dia' -> 'dailyRestingHeartRate' (nombre del objeto en el JSON)."""
    base = tipo.removesuffix(ROLLUP)
    return re.sub(r"-(.)", lambda m: m.group(1).upper(), base)


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).rstrip("s"))   # admite duraciones "1310.964s"
    except ValueError:
        return None


def pos(v) -> float | None:
    """Número > 0, o None: para métricas en las que 0 significa "sin dato"."""
    x = num(v)
    return x if x is not None and x > 0 else None


def entero(v) -> int | None:
    x = num(v)
    return round(x) if x is not None else None


def red(x: float | None, decimales: int = 0) -> float | int | None:
    """Redondea respetando el None."""
    if x is None:
        return None
    return round(x, decimales) if decimales else round(x)


def fecha_civil(d: dict | None) -> str | None:
    if not d or "year" not in d:
        return None
    return f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}"


def hora_local(ts: str | None, offset: str | None) -> datetime | None:
    """Marca UTC + desfase ('7200s') -> hora local sin zona (la que marcaba el reloj)."""
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (t + timedelta(seconds=num(offset) or 0)).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Capa raw
# --------------------------------------------------------------------------
def fecha_de(tipo: str, p: dict) -> str | None:
    if "civilStartTime" in p:                         # agregados diarios
        return fecha_civil(p["civilStartTime"].get("date"))
    obj = p.get(campo(tipo), {})
    if "date" in obj:                                 # resúmenes diarios (daily-*)
        return fecha_civil(obj["date"])
    if "sampleTime" in obj:                           # muestras puntuales (peso...)
        return fecha_civil(obj["sampleTime"].get("civilTime", {}).get("date"))
    iv = obj.get("interval", {})
    if tipo == "sleep":                               # día en que te despiertas
        fin = hora_local(iv.get("endTime"), iv.get("endUtcOffset"))
        return fin.date().isoformat() if fin else None
    if "civilStartTime" in iv:
        return fecha_civil(iv["civilStartTime"].get("date"))
    ini = hora_local(iv.get("startTime"), iv.get("startUtcOffset"))
    return ini.date().isoformat() if ini else None


def clave_de(tipo: str, p: dict, fecha: str | None) -> str:
    """Clave estable: el id de la API si lo trae; si no, la fecha o la hora de la muestra."""
    name = p.get("name") or ""
    if name and not name.endswith("/"):               # hay ids vacíos ("…/dataPoints/")
        return name
    obj = p.get(campo(tipo), {})
    if "sampleTime" in obj and obj["sampleTime"].get("physicalTime"):
        return obj["sampleTime"]["physicalTime"]
    if fecha and (tipo.endswith(ROLLUP) or "date" in obj):
        return fecha
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()


def guardar_raw(con: sqlite3.Connection, tipo: str, puntos: list[dict]) -> int:
    """Upsert en raw. Devuelve cuántos registros eran nuevos o cambiaron."""
    antes = con.total_changes
    for p in puntos:
        if tipo.endswith(ROLLUP) and len(p) <= 2:     # día sin valor (solo las fechas)
            continue
        f = fecha_de(tipo, p)
        con.execute(
            """INSERT INTO raw (tipo, clave, fecha, json) VALUES (?, ?, ?, ?)
               ON CONFLICT (tipo, clave) DO UPDATE
               SET json = excluded.json, fecha = excluded.fecha, actualizado = datetime('now')
               WHERE raw.json != excluded.json""",
            (tipo, clave_de(tipo, p, f), f, json.dumps(p, sort_keys=True, ensure_ascii=False)),
        )
    return con.total_changes - antes


def leer_raw(con: sqlite3.Connection, tipo: str):
    """Registros de un tipo desde FECHA_INICIO, sin los descartados."""
    for fecha, txt in con.execute(
            """SELECT r.fecha, r.json FROM raw r
               LEFT JOIN descartes d ON d.tipo = r.tipo AND d.clave = r.clave
               WHERE r.tipo = ? AND r.fecha >= ? AND d.clave IS NULL
               ORDER BY r.fecha""", (tipo, FECHA_INICIO.isoformat())):
        yield fecha, json.loads(txt)


# --------------------------------------------------------------------------
# Capa limpia
# --------------------------------------------------------------------------
def filas_sueno(con) -> list[tuple]:
    filas = []
    for fecha, p in leer_raw(con, "sleep"):
        s = p["sleep"]
        iv, md, sm = s.get("interval", {}), s.get("metadata", {}), s.get("summary", {})
        fases = {x.get("type"): entero(x.get("minutes")) for x in sm.get("stagesSummary", [])}
        noche = bool(md.get("mainSleep")) and not md.get("nap")
        filas.append((
            p["name"], fecha,
            hora_local(iv.get("startTime"), iv.get("startUtcOffset")).isoformat(timespec="minutes"),
            hora_local(iv.get("endTime"), iv.get("endUtcOffset")).isoformat(timespec="minutes"),
            0 if noche else 1, s.get("type"),
            entero(sm.get("minutesAsleep")), entero(sm.get("minutesAwake")),
            fases.get("LIGHT"), fases.get("DEEP"), fases.get("REM"), fases.get("RESTLESS"),
        ))
    return filas


def filas_entrenamientos(con) -> list[tuple]:
    filas = []
    for fecha, p in leer_raw(con, "exercise"):
        ex = p["exercise"]
        iv, ms = ex.get("interval", {}), ex.get("metricsSummary") or {}
        ini = hora_local(iv.get("startTime"), iv.get("startUtcOffset"))
        fin = hora_local(iv.get("endTime"), iv.get("endUtcOffset"))
        dur = num(ex.get("activeDuration")) or ((fin - ini).total_seconds() if fin else None)
        zonas = ms.get("heartRateZoneDurations") or {}
        zona = lambda k: red(num(zonas.get(k)) and num(zonas.get(k)) / 60, 1)  # noqa: E731
        dist = pos(ms.get("distanceMillimeters"))
        filas.append((
            p["name"], fecha, ini.isoformat(timespec="minutes"),
            fin.isoformat(timespec="minutes") if fin else None,
            ex.get("exerciseType"), ex.get("displayName"),
            red(dur and dur / 60, 1), pos(ms.get("caloriesKcal")),
            entero(pos(ms.get("averageHeartRateBeatsPerMinute"))), entero(pos(ms.get("steps"))),
            red(dist and dist / 1e6, 2), entero(ms.get("activeZoneMinutes")),
            zona("lightTime"), zona("moderateTime"), zona("vigorousTime"), zona("peakTime"),
        ))
    return filas


def filas_pesos(con) -> list[tuple]:
    """Una fila por medición: el peso y el % de grasa de la misma hora van juntos."""
    medidas: dict[str, dict] = {}
    for tipo in ("weight", "body-fat"):
        for fecha, p in leer_raw(con, tipo):
            obj = p[campo(tipo)]
            if tipo == "weight":
                valor = {"kg": red((pos(obj.get("weightGrams")) or 0) / 1000, 2) or None}  # hay pesos de 0 g
            else:
                valor = {"grasa": pos(obj.get("percentage"))}
            if not any(valor.values()):
                continue
            st = obj.get("sampleTime", {})
            t = st.get("civilTime", {}).get("time", {})
            hora = f"{t.get('hours', 0):02d}:{t.get('minutes', 0):02d}"
            m = medidas.setdefault(st.get("physicalTime") or f"{fecha}T{hora}", {
                "id": p.get("name"), "fecha": fecha, "hora": hora,
                "origen": p.get("dataSource", {}).get("recordingMethod")})
            m.update(valor)
    return [(m["id"], m["fecha"], m["hora"], m.get("kg"), m.get("grasa"), m["origen"])
            for _, m in sorted(medidas.items())]


def filas_dias(con, sueno: list[tuple]) -> list[tuple]:
    d: dict[str, dict] = {}

    def poner(tipo, f):
        for fecha, p in leer_raw(con, tipo):
            if fecha:
                d.setdefault(fecha, {}).update(f(p[campo(tipo)]))

    poner("steps" + ROLLUP, lambda o: {"pasos": entero(pos(o.get("countSum")))})
    poner("distance" + ROLLUP, lambda o: {"km": red((pos(o.get("millimetersSum")) or 0) / 1e6, 2) or None})
    poner("floors" + ROLLUP, lambda o: {"pisos": entero(o.get("countSum"))})
    poner("total-calories" + ROLLUP, lambda o: {"kcal": red(pos(o.get("kcalSum")))})
    poner("active-energy-burned" + ROLLUP, lambda o: {"kcal_act": red(num(o.get("kcalSum")))})
    poner("sedentary-period" + ROLLUP, lambda o: {"sed": red((num(o.get("durationSum")) or 0) / 60)})
    poner("active-zone-minutes" + ROLLUP, lambda o: {
        "azm_q": entero(o.get("sumInFatBurnHeartZone")) or 0,
        "azm_c": entero(o.get("sumInCardioHeartZone")) or 0,
        "azm_p": entero(o.get("sumInPeakHeartZone")) or 0})
    poner("time-in-heart-rate-zone" + ROLLUP, lambda o: {"cob": round(min(1.0, sum(
        num(z.get("duration")) or 0 for z in o.get("timeInHeartRateZones", [])) / 86400), 3)})
    poner("heart-rate" + ROLLUP, lambda o: {"fc_med": red(pos(o.get("beatsPerMinuteAvg")), 1),
                                           "fc_max": entero(pos(o.get("beatsPerMinuteMax")))})
    poner("daily-resting-heart-rate", lambda o: {
        "fc_rep": entero(pos(o.get("beatsPerMinute"))),
        "fc_rep_noche": int((o.get("dailyRestingHeartRateMetadata") or {}).get("calculationMethod") == "WITH_SLEEP")})
    poner("daily-heart-rate-variability", lambda o: {
        "vfc": pos(o.get("averageHeartRateVariabilityMilliseconds")),
        "vfc_prof": pos(o.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds"))})
    poner("daily-oxygen-saturation", lambda o: {"spo2": pos(o.get("averagePercentage"))})
    poner("daily-respiratory-rate", lambda o: {"resp": pos(o.get("breathsPerMinute"))})

    for fila in sueno:                                 # solo noches, no siestas
        fecha, siesta, dormido = fila[1], fila[4], fila[6]
        if not siesta and dormido:
            x = d.setdefault(fecha, {})
            x["sueno"] = (x.get("sueno") or 0) + dormido

    filas = []
    for fecha in sorted(d):
        x = d[fecha]
        noche, cob = x.get("sueno") is not None, x.get("cob") or 0
        if noche:
            tipo_dia = "completo" if cob >= COBERTURA_COMPLETO else "solo_noche"
        elif cob >= COBERTURA_SIN_RELOJ:
            tipo_dia = "sin_noche"
        elif x.get("pasos"):
            tipo_dia = "solo_movil"
        else:
            tipo_dia = "sin_datos"
        filas.append((
            fecha, tipo_dia, x.get("pasos"), x.get("km"), x.get("pisos"), x.get("kcal"),
            x.get("kcal_act"), x.get("sed"), x.get("azm_q"), x.get("azm_c"), x.get("azm_p"),
            x.get("cob"), x.get("fc_med"), x.get("fc_max"), x.get("fc_rep"), x.get("fc_rep_noche"),
            x.get("vfc"), x.get("vfc_prof"), x.get("spo2"), x.get("resp"), x.get("sueno"),
        ))
    return filas


def reconstruir(con: sqlite3.Connection) -> dict[str, int]:
    """Rehace las tablas limpias desde raw (unos segundos en la Pi). Va en una sola transacción."""
    sueno = filas_sueno(con)
    tablas = {
        "sueno": sueno,
        "entrenamientos": filas_entrenamientos(con),
        "pesos": filas_pesos(con),
        "dias": filas_dias(con, sueno),
    }
    with con:
        for tabla, filas in tablas.items():
            con.execute(f"DELETE FROM {tabla}")
            if filas:
                con.executemany(f"INSERT INTO {tabla} VALUES ({','.join('?' * len(filas[0]))})", filas)
    return {t: len(f) for t, f in tablas.items()}

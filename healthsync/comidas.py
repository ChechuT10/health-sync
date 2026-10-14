"""Comidas: lo que se come de verdad, en la base de datos y en Google Health.

- Cada noche (23:30) se escriben en Google Health las comidas del menú de ese día y los fijos (los cafés),
  salvo las que se hayan saltado (/saltar) o sustituido por una comida de fuera.
- Comida de fuera: una foto al bot o /fuera con una descripción. Gemini estima kcal y macros con un
  rango; tú eliges qué comida sustituye (o «extra») y se escribe en ese momento en Google Health.
- El último día del mes, a la misma hora, llega el resumen mensual por Telegram.
- healthsync.calorias usa lo registrado aquí como «lo comido» para calcular el gasto real.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.comidas --escribir          # el temporizador de las 23:30
  python -m healthsync.comidas --resumen 2026-10   # resumen de un mes (sin mandarlo)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from . import calorias, menu
from .config import DB_FILE, TZ
from .db import conectar
from .medidas import es

TIPO_GOOGLE = {"desayuno": "BREAKFAST", "media_mañana": "SNACK", "comida": "LUNCH", "merienda": "SNACK",
               "cena": "DINNER", "fijos": "SNACK", "extra": "SNACK"}
SUSTITUIBLES = ["desayuno", "media_mañana", "comida", "merienda", "cena"]
MACROS = ("kcal", "proteina", "grasa", "hidratos")
MODELO = "gemini-flash-latest"


# --------------------------------------------------------------------------
# Menú del día
# --------------------------------------------------------------------------
def menu_del_dia(con: sqlite3.Connection, fecha: date) -> list[dict]:
    """Comidas planificadas ese día con sus macros (las suma la Pi con los valores del menú) y los fijos."""
    semana = calorias.martes_en_vigor(fecha)
    r = menu.cargar(con, semana)
    if r is None:
        return []
    prefs = menu.preferencias()
    m = r["menu"]
    alim = {a["id"]: a for a in m["alimentos"]}
    platos = {p["id"]: p for p in m["platos"]}
    dia = menu.DIAS[(fecha - semana).days]
    d = next((x for x in m["dias"] if x["dia"] == dia), None)
    if d is None:
        return []
    salida = []
    for c in sorted(d["comidas"], key=lambda x: menu.COMIDAS.index(x["comida"])):
        p = platos.get(c["plato"])
        if p is None:
            continue
        t = dict.fromkeys(MACROS, 0.0)
        for i in p["ingredientes"]:
            a = alim.get(i["alimento"])
            if a:
                for k in MACROS:
                    t[k] += a[k] * i["gramos"] / 100
        salida.append({"comida": c["comida"], "nombre": p["nombre"], "hora": prefs["horario"].get(c["comida"], "12:00"),
                       **{k: round(v, 1) for k, v in t.items()}})
    fijos = menu.suma_fijos(prefs)
    salida.append({"comida": "fijos", "nombre": "Cafés con leche (fijos del día)", "hora": "16:00",
                   **{k: round(v, 1) for k, v in fijos.items()}})
    return salida


# --------------------------------------------------------------------------
# Google Health
# --------------------------------------------------------------------------
def cuerpo_google(fecha: date, hora: str, comida: str, nombre: str, macros: dict) -> dict:
    h, m = (int(x) for x in hora.split(":"))
    inicio = datetime(fecha.year, fecha.month, fecha.day, h, m, tzinfo=TZ)
    fin = inicio + timedelta(minutes=20)
    desfase = f"{int(inicio.utcoffset().total_seconds())}s"
    z = lambda t: t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"nutritionLog": {
        "interval": {"startTime": z(inicio), "startUtcOffset": desfase, "endTime": z(fin), "endUtcOffset": desfase},
        "energy": {"kcal": round(macros["kcal"])},
        "nutrients": [{"nutrient": "PROTEIN", "quantity": {"grams": round(macros["proteina"], 1)}}],
        "totalFat": {"grams": round(macros["grasa"], 1)},
        "totalCarbohydrate": {"grams": round(macros["hidratos"], 1)},
        "mealType": TIPO_GOOGLE[comida],
        "foodDisplayName": nombre[:100],
    }}


def subir(con: sqlite3.Connection, fila_id: int) -> str | None:
    """Escribe una comida en Google Health. Devuelve el error (None si fue bien)."""
    from . import api
    f = con.execute("SELECT fecha, hora, comida, nombre, kcal, proteina, grasa, hidratos FROM comidas WHERE id = ?",
                    (fila_id,)).fetchone()
    cuerpo = cuerpo_google(date.fromisoformat(f[0]), f[1], f[2], f[3], dict(zip(MACROS, f[4:8])))
    try:
        r = api.request(api.session(), "POST", f"{api.API}/dataTypes/nutrition-log/dataPoints", json=cuerpo)
    except api.ApiError as e:
        return str(e)[:300]
    nombre = r.get("name") or (r.get("response") or {}).get("name") or "?"
    con.execute("UPDATE comidas SET gh_name = ? WHERE id = ?", (nombre, fila_id))
    con.commit()
    return None


def registrar(con: sqlite3.Connection, fecha: date, comida: str, origen: str, nombre: str, macros: dict,
              hora: str, rango: tuple[float, float] | None = None) -> int:
    cur = con.execute(
        "INSERT INTO comidas (fecha, comida, origen, nombre, kcal, proteina, grasa, hidratos, kcal_min, kcal_max, hora) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fecha.isoformat(), comida, origen, nombre, *(macros.get(k) for k in MACROS),
         *(rango or (None, None)), hora))
    con.commit()
    return cur.lastrowid


def ocupadas(con: sqlite3.Connection, fecha: date) -> set[str]:
    """Comidas de ese día que ya no se escriben del menú (saltadas, sustituidas o ya escritas)."""
    return {f[0] for f in con.execute("SELECT comida FROM comidas WHERE fecha = ? AND comida != 'extra'",
                                      (fecha.isoformat(),))}


def escribir_dia(con: sqlite3.Connection, fecha: date, log=print) -> list[str]:
    """Registra y escribe en Google Health las comidas del menú que falten. Devuelve los errores."""
    # Hoy y, por si alguna noche la Pi estuvo apagada, los 3 días anteriores
    for dia in (fecha - timedelta(days=n) for n in range(3, -1, -1)):
        hechas = ocupadas(con, dia)
        for c in menu_del_dia(con, dia):
            if c["comida"] not in hechas:
                registrar(con, dia, c["comida"], "plan", c["nombre"], c, c["hora"])
    # Todo lo pendiente de escribir de los últimos días (así se reintenta lo que falló otra noche)
    errores = []
    for fid, dia, comida in con.execute(
            "SELECT id, fecha, comida FROM comidas WHERE fecha > ? AND fecha <= ? AND gh_name IS NULL "
            "AND origen != 'saltada' ORDER BY fecha, id",
            ((fecha - timedelta(days=4)).isoformat(), fecha.isoformat())).fetchall():
        if err := subir(con, fid):
            errores.append(f"{dia} {comida}: {err}")
    log(f"Comidas del {fecha:%d/%m}: escritas en Google Health" + (f", con errores: {errores}" if errores else "."))
    return errores


# --------------------------------------------------------------------------
# Comidas de fuera y saltadas (desde el bot)
# --------------------------------------------------------------------------
def esquema_estimacion() -> dict:
    num = {"type": "number"}
    props = {"descripcion": {"type": "string"}, "kcal": num, "kcal_min": num, "kcal_max": num,
             "proteina": num, "grasa": num, "hidratos": num,
             "confianza": {"type": "string", "enum": ["alta", "media", "baja"]}, "nota": {"type": "string"}}
    return {"type": "object", "properties": props, "required": list(props)}


def estimar(descripcion: str = "", foto: bytes | None = None) -> dict:
    """Estimación de Gemini de una comida (foto y/o descripción), con rango."""
    from .gemini import generar
    prompt = f"""Eres un dietista-nutricionista en España. Estima la energía y los macros de esta comida{' (foto)' if foto else ''}
para una persona adulta. {('Pistas de quien la ha comido: ' + descripcion) if descripcion else ''}
Ten en cuenta aceites, salsas y bebidas visibles o mencionadas, que es donde más se suele fallar.
Da un valor central y un rango realista (kcal_min-kcal_max); con foto, el error típico es del ±30-50 %.
"descripcion": qué es, en pocas palabras. "nota": un consejo breve si hace falta (sin sermones)."""
    datos = json.loads(generar(MODELO, prompt, esquema=esquema_estimacion(), razonamiento="medium",
                               max_salida=8192, timeout=120, imagen=foto))
    if not 0 < datos["kcal_min"] <= datos["kcal"] <= datos["kcal_max"] < 5000:
        raise ValueError("estimación sin sentido")
    return datos


def guardar_fuera(con: sqlite3.Connection, fecha: date, comida: str, est: dict, hora: str) -> tuple[int, str | None]:
    """Guarda la comida de fuera (sustituye a la del menú si es una comida del plan) y la escribe en Google Health."""
    if comida != "extra":
        previa = con.execute("SELECT id, gh_name FROM comidas WHERE fecha = ? AND comida = ?",
                             (fecha.isoformat(), comida)).fetchone()
        if previa and previa[1]:
            comida = "extra"                         # la del menú ya está escrita: esta cuenta como extra
        elif previa:
            con.execute("DELETE FROM comidas WHERE id = ?", (previa[0],))
    fid = registrar(con, fecha, comida, "fuera", est["descripcion"], est, hora, (est["kcal_min"], est["kcal_max"]))
    return fid, subir(con, fid)


def saltar(con: sqlite3.Connection, fecha: date, comida: str) -> str:
    if comida not in SUSTITUIBLES:
        return f"No conozco «{comida}». Puedes saltar: {', '.join(c.replace('_', ' ') for c in SUSTITUIBLES)}."
    previa = con.execute("SELECT origen, gh_name FROM comidas WHERE fecha = ? AND comida = ?",
                         (fecha.isoformat(), comida)).fetchone()
    if previa and previa[1]:
        return f"La {comida.replace('_', ' ')} de hoy ya está escrita en Google Health; no se puede saltar."
    if previa:
        return f"La {comida.replace('_', ' ')} de hoy ya estaba marcada ({previa[0]})."
    registrar(con, fecha, comida, "saltada", "(saltada)", dict.fromkeys(MACROS, 0), "00:00")
    return f"Hecho: la {comida.replace('_', ' ')} de hoy no se apunta. Si comiste otra cosa, mándame una foto."


def texto_estimacion(est: dict) -> str:
    return (f"🍽️ {est['descripcion']}\n≈ {calorias.kc(est['kcal'])} kcal (entre {calorias.kc(est['kcal_min'])} y "
            f"{calorias.kc(est['kcal_max'])}) · P {est['proteina']:.0f} · G {est['grasa']:.0f} · H {est['hidratos']:.0f}"
            f" · confianza {est['confianza']}" + (f"\n{est['nota']}" if est.get("nota") else ""))


# --------------------------------------------------------------------------
# «Lo comido» para el gasto real y resumen mensual
# --------------------------------------------------------------------------
def kcal_por_dia(con: sqlite3.Connection, desde: date, hasta: date) -> dict[str, float]:
    """Kcal registradas por día (solo días con alguna comida del plan o de fuera), entre desde y hasta incluidos."""
    filas = con.execute("SELECT fecha, sum(kcal) FROM comidas WHERE fecha >= ? AND fecha <= ? AND origen != 'saltada' "
                        "GROUP BY fecha", (desde.isoformat(), hasta.isoformat())).fetchall()
    return {f: k for f, k in filas if k}


def resumen_mes(con: sqlite3.Connection, anio: int, mes: int) -> str:
    desde = date(anio, mes, 1)
    hasta = (date(anio + mes // 12, mes % 12 + 1, 1)) - timedelta(days=1)
    dias = kcal_por_dia(con, desde, hasta)
    if not dias:
        return f"📅 {desde:%m/%Y}: aún no hay comidas registradas."
    fuera = con.execute("SELECT count(*), sum(kcal) FROM comidas WHERE fecha BETWEEN ? AND ? AND origen = 'fuera'",
                        (desde.isoformat(), hasta.isoformat())).fetchone()
    saltadas = con.execute("SELECT count(*) FROM comidas WHERE fecha BETWEEN ? AND ? AND origen = 'saltada'",
                           (desde.isoformat(), hasta.isoformat())).fetchone()[0]
    dias_fuera = {f[0] for f in con.execute("SELECT DISTINCT fecha FROM comidas WHERE fecha BETWEEN ? AND ? "
                                             "AND origen IN ('fuera', 'saltada')", (desde.isoformat(), hasta.isoformat()))}
    objetivos = []
    for f in dias:
        p = calorias.plan_guardado(con, calorias.martes_en_vigor(date.fromisoformat(f)))
        if p:
            objetivos.append(dias[f] - p.kcal)
    media = sum(dias.values()) / len(dias)
    lineas = [f"📅 Resumen de {desde:%m/%Y}",
              f"Días registrados: {len(dias)} · según el plan: {len(dias) - len(dias_fuera & set(dias))}",
              f"Comidas fuera: {fuera[0]}" + (f" (≈ {calorias.kc(fuera[1])} kcal en total)" if fuera[0] else ""),
              f"Comidas saltadas: {saltadas}",
              f"Media: {calorias.kc(media)} kcal al día"]
    if objetivos:
        dif = sum(objetivos) / len(objetivos)
        lineas.append(f"Frente al objetivo: {'+' if dif >= 0 else '−'}{calorias.kc(abs(dif))} kcal al día de media")
    lineas.append("Solo informa: las comidas de fuera no se compensan con más déficit.")
    return "\n".join(lineas)


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: comidas en Google Health")
    ap.add_argument("--escribir", action="store_true", help="escribe las comidas del menú de hoy (23:30)")
    ap.add_argument("--fecha", type=date.fromisoformat, help="otro día (para --escribir)")
    ap.add_argument("--resumen", metavar="AAAA-MM", help="enseña el resumen de ese mes")
    args = ap.parse_args()
    con = conectar(DB_FILE)
    try:
        if args.resumen:
            a, m = (int(x) for x in args.resumen.split("-"))
            print(resumen_mes(con, a, m))
            return 0
        if args.escribir:
            from . import telegram
            hoy = args.fecha or datetime.now(TZ).date()
            errores = escribir_dia(con, hoy)
            if errores:
                telegram.enviar("⚠️ No se pudieron escribir algunas comidas en Google Health (se reintenta mañana):\n"
                                + "\n".join(errores))
            if (hoy + timedelta(days=1)).day == 1:
                telegram.enviar(resumen_mes(con, hoy.year, hoy.month))
            return 0
        ap.print_help()
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

"""Análisis de cada sesión de gimnasio: lo que apuntas en el Sheet + lo que registra el reloj.

El código prepara los hechos (objetivo frente a lo hecho, RIR, caída de repeticiones, la sesión anterior
con esa variante, duración y pulso del reloj, sueño y VFC) y Gemini solo redacta la valoración: qué salió
bien, qué mejorar y qué toca la próxima vez. Los pesos los sigue decidiendo el código (entreno.objetivo).

Se lanza con la sincronización (entreno --leer, cada 4 h). Una sesión se analiza una sola vez, cuando el
reloj ya la ha cerrado y tienes apuntados todos los ejercicios; si falta alguno, a las 12 h de parar el
reloj. Sin reloj, al día siguiente. El análisis llega por Telegram y queda para /analisis.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.analisis                   # analiza lo pendiente y lo manda por Telegram
  python -m healthsync.analisis --ver             # enseña los hechos y el prompt, sin Gemini ni Telegram
  python -m healthsync.analisis --rehacer         # vuelve a analizar la última sesión (p. ej. tras corregir el Sheet)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

from . import entreno
from .config import DB_FILE, TZ
from .db import conectar
from .medidas import es

ESPERA_H = 12           # con ejercicios sin apuntar, se analiza igual a las 12 h de parar el reloj
DIAS_ATRAS = 7          # no se analizan sesiones más antiguas (al desplegar no llega una avalancha)
TIPOS_FUERZA = ("STRENGTH_TRAINING", "WEIGHTLIFTING")
MAX_SALIDA = 8192


def _fuerza(con: sqlite3.Connection, desde: date, hasta: date) -> list[dict]:
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    filas = cur.execute(
        f"SELECT * FROM entrenamientos WHERE fecha >= ? AND fecha <= ? AND tipo IN ({','.join('?' * len(TIPOS_FUERZA))}) "
        "ORDER BY inicio", (desde.isoformat(), hasta.isoformat(), *TIPOS_FUERZA)).fetchall()
    return [dict(f) for f in filas]


def sesiones_reloj(con: sqlite3.Connection, p: dict, dias_hechos: list[str]) -> dict[str, dict]:
    """Empareja cada día de la rutina con series apuntadas con una sesión de fuerza del reloj de esa semana:
    la del día previsto o, si lo cambiaste (p. ej. al miércoles), la más cercana que no sea de otro día."""
    semana = date.fromisoformat(p["semana"])
    libres = _fuerza(con, semana, semana + timedelta(days=6))
    previstos = {d["id"]: date.fromisoformat(d["fecha"]) for d in p["dias"] if d["id"] in dias_hechos}
    out: dict[str, dict] = {}
    # Primero los que coinciden con su día; luego el resto, por cercanía
    for exacto in (True, False):
        for dia, fecha in sorted(previstos.items(), key=lambda x: x[1]):
            if dia in out:
                continue
            cand = [s for s in libres if (s["fecha"] == fecha.isoformat() if exacto
                                          else abs((date.fromisoformat(s["fecha"]) - fecha).days) <= 2)]
            if cand:
                s = min(cand, key=lambda s: abs((date.fromisoformat(s["fecha"]) - fecha).days))
                out[dia] = s
                libres.remove(s)
    return out


def _series(con: sqlite3.Connection, semana: str, dia: str) -> dict[str, list[tuple]]:
    """Series apuntadas de ese día, por ejercicio (en orden): (variante, kg, reps, rir, notas, objetivo)."""
    out: dict[str, list[tuple]] = {}
    for ej, var, kg, reps, rir, notas, obj in con.execute(
            "SELECT ejercicio, variante, kg, reps, rir, notas, objetivo FROM series WHERE semana = ? AND dia = ? "
            "ORDER BY ejercicio, serie", (semana, dia)):
        out.setdefault(ej, []).append((var, kg, reps, rir, notas or "", obj or ""))
    return out


def pendientes(con: sqlite3.Connection, ahora: datetime) -> list[tuple[dict, dict, dict | None]]:
    """(plan de la semana, día, sesión del reloj o None) de cada sesión lista para analizar."""
    hoy = ahora.date()
    hechos = {f[0] for f in con.execute("SELECT id FROM analisis_entreno")}
    out = []
    for (txt,) in con.execute("SELECT plan FROM entreno_semana WHERE semana >= ? ORDER BY semana",
                              ((hoy - timedelta(days=DIAS_ATRAS + 6)).isoformat(),)).fetchall():
        p = json.loads(txt)
        con_series = [d["id"] for d in p["dias"] if _series(con, p["semana"], d["id"])]
        reloj = sesiones_reloj(con, p, con_series)
        for d in p["dias"]:
            if d["id"] not in con_series or f"{p['semana']}|{d['id']}" in hechos:
                continue
            s = reloj.get(d["id"])
            fecha = date.fromisoformat(s["fecha"] if s else d["fecha"])
            if fecha < hoy - timedelta(days=DIAS_ATRAS):
                continue
            apuntados = set(_series(con, p["semana"], d["id"]))
            completo = all(e["id"] in apuntados for e in d["ejercicios"])
            if s:
                fin = datetime.fromisoformat(s["fin"] or s["inicio"]).replace(tzinfo=TZ)
                listo = completo or ahora >= fin + timedelta(hours=ESPERA_H)
            else:                                   # sin reloj: se espera al día siguiente por si sincroniza tarde
                listo = hoy > fecha
            if listo:
                out.append((p, d, s))
    return out


def _txt_serie(kg, reps, rir, notas="") -> str:
    return f"{es(kg)} kg × {reps}" + (f" @RIR {es(rir)}" if rir is not None else "") + (f" ({notas})" if notas else "")


def hechos(con: sqlite3.Connection, p: dict, d: dict, sesion: dict | None, ahora: datetime) -> str:
    """Resumen en texto de la sesión, con las comprobaciones hechas por el código."""
    semana = date.fromisoformat(p["semana"])
    rutina = entreno.rutina_semana(con, semana)              # con el ajuste del lunes, si lo hubo
    rutina_dia = next((x for x in rutina["dias"] if x["id"] == d["id"]), {}) if rutina else {}
    defs = {e["id"]: e for e in rutina_dia.get("ejercicios", [])}
    apuntado = _series(con, p["semana"], d["id"])
    elegidas = {ej: filas[0][0] for ej, filas in apuntado.items()}
    try:                                            # lo que toca la próxima vez (lo calcula el código)
        sig = entreno.plan(con, semana + timedelta(days=7), ahora.date(), elegidas)
        sig_dia = next((x for x in sig["dias"] if x["id"] == d["id"]), None)
    except entreno.EntrenoError:
        sig_dia = None
    fecha = date.fromisoformat(sesion["fecha"] if sesion else d["fecha"])
    lineas = [f"SESIÓN: {d['nombre']}, {entreno.ORDEN_DIAS[fecha.weekday() - 1]} {fecha:%d/%m}"
              + ("" if fecha.isoformat() == d["fecha"] else f" (prevista el {date.fromisoformat(d['fecha']):%d/%m})"),
              f"Semana {p['semana_bloque']} del bloque" + (" (DESCARGA)" if p["descarga"] else "")
              + f". Recuperación de la semana: {p['recuperacion'].replace('_', ' ')} ({p['recuperacion_texto']})."]
    noche = con.execute("SELECT sueno_min, vfc_ms, fc_reposo FROM dias WHERE fecha = ?", (fecha.isoformat(),)).fetchone()
    if noche and any(noche):
        sueno, vfc, fcr = noche
        lineas.append("Noche anterior: " + ", ".join(x for x in (
            f"{es(sueno / 60, 1)} h de sueño" if sueno else "", f"VFC {es(vfc, 0)} ms" if vfc else "",
            f"FC en reposo {fcr}" if fcr else "") if x) + ".")
    lineas.append("")
    for e in d["ejercicios"]:
        filas = apuntado.get(e["id"])
        # El objetivo es el que ponía el Sheet al apuntar: el plan se recalcula durante la semana
        o = e["objetivos"].get(filas[0][0] if filas else e["variante"]) or e["objetivos"][e["variante"]]
        visto = next((f[5] for f in filas or [] if f[5]), "")
        lineas.append(f"• {filas[0][0] if filas else e['variante']}")
        lineas.append(f"  Objetivo en su hoja: {visto or o['texto']}")
        if not filas:
            lineas.append("  NO APUNTADO (no lo hizo o no lo anotó)")
            continue
        lineas.append("  Hecho: " + " · ".join(_txt_serie(kg, reps, rir) for _, kg, reps, rir, *_ in filas))
        notas = sorted({f[4] for f in filas if f[4]})
        if notas:
            lineas.append(f"  Notas: {'; '.join(notas)}")
        # Comprobaciones
        avisos = []
        rmin, rmax = (defs.get(e["id"]) or {}).get("reps", [None, None])
        reps = [f[2] for f in filas]
        rirs = [f[3] for f in filas if f[3] is not None]
        kgs = [f[1] for f in filas]
        m = re.match(r"([\d.,]+) kg", visto or o["texto"])
        kg_obj = float(m.group(1).replace(",", ".")) if m else None
        m = re.search(r"@RIR (\d+)", visto or o["texto"])
        rir_obj = int(m.group(1)) if m else o["rir"]
        if kg_obj is not None and max(kgs) != kg_obj:
            avisos.append(f"peso distinto del objetivo ({es(max(kgs))} frente a {es(kg_obj)} kg)")
        if len(set(kgs)) > 1:
            avisos.append("peso distinto entre series")
        if rmax and max(reps) > rmax:
            avisos.append(f"pasa del rango ({max(reps)} > {rmax}): el peso se queda corto")
        if rmin and min(reps) < rmin:
            avisos.append(f"por debajo del rango ({min(reps)} < {rmin})")
        if len(reps) > 1 and reps[0] - reps[-1] >= 3:
            avisos.append(f"las repeticiones caen {reps[0]}→{reps[-1]}")
        if rirs and max(rirs) >= rir_obj + 2:
            avisos.append(f"RIR {es(max(rirs))} en alguna serie con objetivo {rir_obj}: más fácil de lo previsto")
        if rirs and min(rirs) == 0:
            avisos.append("alguna serie al fallo (RIR 0)")
        if avisos:
            lineas.append("  Comprobaciones: " + "; ".join(avisos))
        antes = entreno.sesiones(con, filas[0][0], antes_de=semana)
        if antes:
            ult = antes[-1]
            vol_ult, vol = sum(k * r for k, r, *_ in ult), sum(k * r for _, k, r, *_ in filas)
            lineas.append("  Sesión anterior: " + " · ".join(_txt_serie(k, r, ri) for k, r, ri, _ in ult)
                          + f" (volumen {es(round(vol_ult))} → {es(round(vol))} kg·rep)")
        else:
            lineas.append("  Primera vez con esta variante en el registro")
        if sig_dia:
            sig_e = next((x for x in sig_dia["ejercicios"] if x["id"] == e["id"]), None)
            if sig_e:
                so = sig_e["objetivos"][sig_e["variante"]]
                lineas.append(f"  Próxima vez (lo calcula el código; puede cambiar con la recuperación del lunes): {so['series']} × {so['texto']}"
                              + (f" — {so['nota']}" if so.get("nota") else ""))
    lineas.append("")
    estimado = entreno.minutos(rutina_dia) if rutina_dia else None
    if sesion:
        z = [sesion.get(f"zona_{n}_min") or 0 for n in ("ligera", "moderada", "intensa", "pico")]
        lineas.append(
            f"RELOJ: {sesion['inicio'][11:16]}-{(sesion['fin'] or '')[11:16]}, {es(round(sesion['duracion_min'] or 0))} min"
            + (f" (estimado por la rutina: {es(round(estimado))}; máximo {entreno.MAX_MINUTOS})" if estimado else "")
            + f". FC media {sesion['fc_media'] or '?'} lpm. Zonas: {es(round(z[0]))} min ligera, {es(round(z[1]))} moderada, "
            f"{es(round(z[2]))} intensa, {es(round(z[3]))} pico. {es(round(sesion['kcal'] or 0))} kcal según el reloj"
            + (f", {sesion['azm']} minutos en zona activa." if sesion.get("azm") is not None else "."))
    else:
        lineas.append("RELOJ: no hay sesión de fuerza registrada ese día (sin datos de duración ni pulso).")
    return "\n".join(lineas)


def prompt(texto_hechos: str) -> str:
    return f"""Eres el entrenador personal de un hombre de 27 años en recomposición corporal (déficit calórico moderado),
con la rodilla derecha operada de menisco (nunca más de 90° de flexión con carga), el manguito rotador derecho
delicado y asma. Acaba de entrenar. Valora la sesión con los datos de abajo (lo que apuntó en su hoja y lo que
registró su reloj) y dile qué hizo bien y qué hizo mal, para que mejore.

Reglas:
- Básate solo en los datos. No inventes series, pesos ni sensaciones. Las «Comprobaciones» ya las ha hecho el
  código y son fiables; úsalas, pero explica qué significan y qué hacer.
- NO cambies pesos ni ejercicios: «Próxima vez» lo calcula el código; si lo mencionas, explícalo, no lo contradigas.
- RIR = repeticiones que le quedaban en la recámara. Si apunta RIR alto con repeticiones que caen, o RIR que no
  cuadra con el rango, dile cómo calibrarlo. Con recuperación en rojo es normal ir algo más conservador.
- El pulso en fuerza es una señal débil: úsalo solo para la duración, los descansos (mucho tiempo en zona ligera
  suele ser descansos largos o charla) y el asma; nunca para juzgar el esfuerzo de una serie.
- Si una nota habla de dolor o molestia, sobre todo en la rodilla o el hombro, que lo vigile y, si se repite, que
  consulte al fisio. Si la nota apunta a un problema de material o agarre (p. ej. straps), da una solución práctica.
- Si la sesión pasa de {entreno.MAX_MINUTOS} min, di dónde se va el tiempo (descansos, superseries).

Formato (texto plano para Telegram, sin Markdown ni asteriscos, en español de España, sin saludos ni despedidas,
1.200 caracteres como mucho):
Veredicto: 🟢, 🟡 o 🔴 y una frase.
Bien:
- 1 a 3 puntos
A mejorar:
- 2 a 4 puntos concretos (qué y cómo)
Próxima sesión:
- 1 o 2 puntos

DATOS:
{texto_hechos}"""


def guardar(con: sqlite3.Connection, p: dict, d: dict, sesion: dict | None, texto: str) -> None:
    fecha = sesion["fecha"] if sesion else d["fecha"]
    con.execute("INSERT INTO analisis_entreno (id, fecha, sesion, texto) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET fecha = excluded.fecha, sesion = excluded.sesion, "
                "texto = excluded.texto, creado = datetime('now')",
                (f"{p['semana']}|{d['id']}", fecha, sesion["id"] if sesion else None, texto))
    con.commit()


def cabecera(nombre: str, fecha: str) -> str:
    f = date.fromisoformat(fecha)
    return f"🏋️ Análisis · {nombre} · {entreno.ORDEN_DIAS[f.weekday() - 1][:3]} {f:%d/%m}"


def analizar(con: sqlite3.Connection, p: dict, d: dict, sesion: dict | None, ahora: datetime) -> str:
    """Pide la valoración a Gemini, la guarda y devuelve el mensaje para Telegram."""
    from .gemini import generar
    texto = generar(entreno.MODELO, prompt(hechos(con, p, d, sesion, ahora)), razonamiento="medium",
                    max_salida=MAX_SALIDA, timeout=300).strip()
    guardar(con, p, d, sesion, texto)
    return f"{cabecera(d['nombre'], sesion['fecha'] if sesion else d['fecha'])}\n\n{texto}"


def analizar_pendientes(con: sqlite3.Connection, ahora: datetime, log=print) -> int:
    """Analiza lo pendiente y lo manda por Telegram. Un fallo de Gemini no tumba la sincronización:
    se vuelve a intentar a las 4 h."""
    from . import telegram
    from .gemini import GeminiError
    n = 0
    for p, d, s in pendientes(con, ahora):
        try:
            msg = analizar(con, p, d, s, ahora)
        except GeminiError as e:
            log(f"Análisis de {d['id']} ({p['semana']}): Gemini falló, se reintenta luego: {e}")
            continue
        try:
            telegram.enviar(msg)
        except Exception as e:  # noqa: BLE001
            log(f"Análisis de {d['id']} guardado, pero no se pudo mandar por Telegram: {e}")
        log(f"Análisis de {d['nombre']} ({p['semana']}) hecho.")
        n += 1
    return n


def revision(con: sqlite3.Connection, desde: date, hasta: date, ahora: datetime) -> tuple[str, int]:
    """Hechos y valoración de cada sesión entre desde y hasta (sin incluir), para el ajuste del lunes.
    Devuelve (texto, número de sesiones con series)."""
    bloques, n = [], 0
    for txt, creado in con.execute("SELECT plan, creado FROM entreno_semana WHERE semana > ? AND semana < ? ORDER BY semana",
                                   ((desde - timedelta(days=7)).isoformat(), hasta.isoformat())).fetchall():
        p = json.loads(txt)
        creada = date.fromisoformat(creado[:10])          # días anteriores a la pestaña (rutina adelantada): no cuentan
        con_series = [d["id"] for d in p["dias"] if _series(con, p["semana"], d["id"])]
        reloj = sesiones_reloj(con, p, con_series)
        for d in p["dias"]:
            s = reloj.get(d["id"])
            fecha = date.fromisoformat(s["fecha"] if s else d["fecha"])
            if not desde <= fecha < hasta:
                continue
            if d["id"] not in con_series:
                if fecha < creada:
                    continue
                bloques.append(f"SESIÓN: {d['nombre']}, prevista el {fecha:%d/%m}: SIN SERIES APUNTADAS "
                               "(no entrenó o no lo anotó).")
                continue
            texto = hechos(con, p, d, s, ahora)
            f = con.execute("SELECT texto FROM analisis_entreno WHERE id = ?", (f"{p['semana']}|{d['id']}",)).fetchone()
            bloques.append(texto + (f"\n\nVALORACIÓN DESPUÉS DE LA SESIÓN:\n{f[0]}" if f else ""))
            n += 1
    return "\n\n-----\n\n".join(bloques), n


def ultimo(con: sqlite3.Connection) -> str | None:
    """El último análisis guardado (para /analisis)."""
    f = con.execute("SELECT a.id, a.fecha, a.texto, s.plan FROM analisis_entreno a "
                    "LEFT JOIN entreno_semana s ON s.semana = substr(a.id, 1, 10) "
                    "ORDER BY a.fecha DESC, a.creado DESC LIMIT 1").fetchone()
    if not f:
        return None
    dia = f[0].split("|")[1]
    nombre = next((d["nombre"] for d in json.loads(f[3])["dias"] if d["id"] == dia), dia) if f[3] else dia
    return f"{cabecera(nombre, f[1])}\n\n{f[2]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: análisis de las sesiones de gimnasio")
    ap.add_argument("--ver", action="store_true", help="enseña hechos y prompt de lo pendiente (sin Gemini)")
    ap.add_argument("--rehacer", action="store_true", help="vuelve a analizar la última sesión analizada")
    args = ap.parse_args()
    ahora = datetime.now(TZ)
    con = conectar(DB_FILE)
    try:
        if args.rehacer:
            f = con.execute("SELECT id FROM analisis_entreno ORDER BY fecha DESC, creado DESC LIMIT 1").fetchone()
            if not f:
                print("Aún no hay ningún análisis.")
                return 1
            con.execute("DELETE FROM analisis_entreno WHERE id = ?", (f[0],))
            con.commit()
        if args.ver:
            lista = pendientes(con, ahora)
            for p, d, s in lista:
                print(prompt(hechos(con, p, d, s, ahora)), end="\n\n" + "=" * 70 + "\n\n")
            if not lista:
                print("No hay sesiones pendientes de analizar.")
            return 0
        analizar_pendientes(con, ahora)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

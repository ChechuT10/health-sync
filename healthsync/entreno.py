"""Gimnasio: rutina por bloques, pesos con doble progresión (código) y cambios de ejercicios con Gemini.

- Los ejercicios se mantienen durante un bloque (12 semanas). Los pesos de cada semana los calcula el
  código con la doble progresión de la nota 04 (sin IA): cada variante tiene su propio historial.
- Al acabar el bloque, o antes si 3 ejercicios se estancan o uno da dolor dos veces, Gemini propone
  qué cambiar. La Pi revisa la propuesta (rodilla ≤ 90°, nada de lo prohibido para el hombro, ≤ 6 series
  de press por encima de la cabeza, volumen por músculo, duración) y, si falla, se la devuelve.
- Cada lunes, Gemini escribe 3-5 consejos para la semana a partir del resumen (no decide pesos).
- Registro en un Google Sheet: una pestaña por semana, una fila por serie. Tú rellenas kg, reps y RIR;
  la Pi lo lee cada 4 h y actualiza los objetivos de lo que aún no has hecho.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.entreno --iniciar pesos.json   # una vez: rutina base, pesos de partida y Sheet
  python -m healthsync.entreno                        # enseña el plan de la semana que viene
  python -m healthsync.entreno --guardar              # lunes: plan, pestaña nueva, consejos y Telegram
  python -m healthsync.entreno --leer                 # cada 4 h (con la sincronización): lee el Sheet
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import calorias
from .config import DB_FILE, TOKEN_DRIVE_FILE, TZ
from .db import conectar
from .medidas import es

RUTINA_BASE = Path(__file__).with_name("rutina_base.json")
TITULO_HOJA = "Health Sync · Gimnasio"
ORDEN_DIAS = ["martes", "miércoles", "jueves", "viernes", "sábado", "domingo", "lunes"]
SEMANAS_BLOQUE = 12
DESCARGA_CADA = 6            # cada 6.ª semana del bloque: mitad de series, mismo peso, RIR 3-4
ESTANCADOS_PARA_CAMBIAR = 3
# Versión fija (no un alias «latest»): un cambio de Google no nos pasa a un modelo más caro sin decidirlo.
# Probado el 14/10/2026: 3.8 Flash cumple las reglas a la primera; 3.1 Pro tarda el doble y necesitó 3 intentos.
MODELO = "gemini-3.8-flash"

# ---- Reglas de seguridad (nota 04): la Pi rechaza cualquier rutina que las incumpla ----
RODILLA_MAX = 90
SERIES_SOBRE_CABEZA_MAX = 6
PROHIBIDO = [r"sentadilla (profunda|completa|libre)", r"b[uú]lgara", r"zancada(?! corta)", r"sissy", r"pistol",
             r"tras nuca", r"remo al ment[oó]n", r"\bfondos\b", r"prensa sin tope", r"press militar con barra",
             r"press (de )?banca con barra", r"jefferson", r"saltos?", r"pliom", r"burpee", r"sprint"]
VOLUMEN = {"pecho": (6, 18), "espalda": (8, 20), "hombro": (6, 18), "cuadriceps": (6, 18),
           "isquios": (6, 18), "gluteo": (6, 20), "biceps": (4, 16), "triceps": (4, 16)}
MAX_MINUTOS = 60
CALENTAMIENTO_MIN = 8        # bici o remo suave
PREHAB_TORSO_MIN = 4         # rotación externa y face pull ligero, los días de torso


def minutos(dia: dict) -> float:
    """Duración estimada de una sesión: 2,5 min por serie en multiarticulares (más descanso), 2 en
    aislados; una superserie cuenta como el más largo de los dos más un 30 %."""
    total = CALENTAMIENTO_MIN + (PREHAB_TORSO_MIN if str(dia.get("id", "")).startswith("T") else 0)
    ejercicios = {e.get("id"): e for e in dia.get("ejercicios", [])}
    vistos = set()
    for e in ejercicios.values():
        if e.get("id") in vistos:
            continue
        t = e.get("series", 0) * (2.5 if len(e.get("musculos") or []) >= 2 else 2.0)
        pareja = ejercicios.get(e.get("superserie") or "")
        if pareja and pareja.get("id") not in vistos:
            t2 = pareja.get("series", 0) * (2.5 if len(pareja.get("musculos") or []) >= 2 else 2.0)
            t = max(t, t2) * 1.3
            vistos.add(pareja.get("id"))
        vistos.add(e.get("id"))
        total += t
    return total
MAX_SALIDA_RUTINA = 49152   # medido el 14/10/2026: ~32 k (2,9 k + 29 k pensando)


class EntrenoError(Exception):
    pass


def _clave(variante: str) -> str:
    return re.sub(r"\s+", " ", variante.strip().lower())


def _kg(x: float) -> float:
    return round(x * 2) / 2


# --------------------------------------------------------------------------
# Rutina (bloques)
# --------------------------------------------------------------------------
def bloque_en_vigor(con: sqlite3.Connection, semana: date) -> tuple[date, dict, str] | None:
    f = con.execute("SELECT inicio, rutina, origen FROM rutina_bloque WHERE inicio <= ? ORDER BY inicio DESC LIMIT 1",
                    (semana.isoformat(),)).fetchone()
    return (date.fromisoformat(f[0]), json.loads(f[1]), f[2]) if f else None


def guardar_bloque(con: sqlite3.Connection, inicio: date, rutina: dict, origen: str, motivo: str = "") -> None:
    con.execute("INSERT INTO rutina_bloque (inicio, rutina, origen, motivo) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (inicio) DO UPDATE SET rutina = excluded.rutina, origen = excluded.origen, "
                "motivo = excluded.motivo, creado = datetime('now')",
                (inicio.isoformat(), json.dumps(rutina, ensure_ascii=False), origen, motivo))
    con.commit()


def validar_rutina(rutina: dict) -> list[str]:
    """Errores de una rutina (vacío = cumple las reglas)."""
    errores: list[str] = []
    prohibido = re.compile("|".join(PROHIBIDO), re.IGNORECASE)
    dias = rutina.get("dias", [])
    if len(dias) != 4:
        errores.append(f"Tiene que haber 4 días (hay {len(dias)}).")
    volumen = dict.fromkeys(VOLUMEN, 0.0)
    sobre_cabeza = 0
    ids = set()
    for d in dias:
        ejercicios = d.get("ejercicios", [])
        if not 4 <= len(ejercicios) <= 8:
            errores.append(f"{d.get('id')}: {len(ejercicios)} ejercicios (entre 4 y 8).")
        if (m := minutos(d)) > MAX_MINUTOS:
            errores.append(f"{d.get('id')}: ~{m:.0f} min con calentamiento, más de {MAX_MINUTOS}. Quita series o "
                           "ejercicios, o junta dos en superserie.")
        for e in ejercicios:
            nombre = f"{d.get('id')}/{e.get('id')}"
            if e.get("id") in ids:
                errores.append(f"{nombre}: id repetido.")
            ids.add(e.get("id"))
            variantes = e.get("variantes") or []
            if not 1 <= len(variantes) <= 4:
                errores.append(f"{nombre}: entre 1 y 4 variantes.")
            for v in variantes:
                if m := prohibido.search(v):
                    errores.append(f"{nombre}: «{v}» incluye «{m.group(0)}», que está prohibido por la rodilla o el hombro.")
            if e.get("rodilla_max", 999) > RODILLA_MAX:
                errores.append(f"{nombre}: flexión de rodilla de {e.get('rodilla_max')}° (máximo {RODILLA_MAX}°).")
            r = e.get("reps") or [0, 0]
            if not (2 <= e.get("series", 0) <= 5 and 3 <= r[0] < r[1] <= 30 and 0 <= e.get("rir", -1) <= 4):
                errores.append(f"{nombre}: series, repeticiones o RIR fuera de rango.")
            if not 0 < e.get("incremento", 0) <= 10:
                errores.append(f"{nombre}: incremento de peso no válido.")
            if e.get("sobre_cabeza"):
                sobre_cabeza += e.get("series", 0)
            for i, musc in enumerate(e.get("musculos") or []):
                if musc in volumen:
                    volumen[musc] += e.get("series", 0) * (1 if i == 0 else 0.5)
    if sobre_cabeza > SERIES_SOBRE_CABEZA_MAX:
        errores.append(f"{sobre_cabeza} series de press por encima de la cabeza a la semana (máximo {SERIES_SOBRE_CABEZA_MAX}).")
    for musc, (lo, hi) in VOLUMEN.items():
        if not lo <= volumen[musc] <= hi:
            errores.append(f"Volumen de {musc}: {volumen[musc]:g} series efectivas a la semana (entre {lo} y {hi}).")
    return errores


# --------------------------------------------------------------------------
# Historial y progresión (solo código)
# --------------------------------------------------------------------------
def sesiones(con: sqlite3.Connection, variante: str, antes_de: date | None = None) -> list[list[tuple]]:
    """Sesiones con esa variante (más antigua primero); cada una, lista de (kg, reps, rir, notas)."""
    filas = con.execute(
        "SELECT semana, dia, kg, reps, rir, notas FROM series WHERE lower(variante) = ? AND kg IS NOT NULL "
        "AND reps IS NOT NULL" + (" AND semana < ?" if antes_de else "") + " ORDER BY semana, dia, serie",
        (_clave(variante),) + ((antes_de.isoformat(),) if antes_de else ())).fetchall()
    grupos: dict[tuple, list] = {}
    for sem, dia, kg, reps, rir, notas in filas:
        grupos.setdefault((sem, dia), []).append((kg, reps, rir, notas))
    return list(grupos.values())


def peso_inicial(con: sqlite3.Connection, variante: str) -> tuple[float | None, str]:
    f = con.execute("SELECT kg, nota FROM pesos_iniciales WHERE lower(variante) = ?", (_clave(variante),)).fetchone()
    return (f[0], f[1] or "") if f else (None, "")


def objetivo(ej: dict, hist: list[list[tuple]], inicial: tuple[float | None, str], recuperacion: str,
             descarga: bool) -> dict:
    """Objetivo de la próxima sesión de una variante: {kg, series, reps, rir, texto, nota}."""
    rmin, rmax = ej["reps"]
    series, rir = ej["series"], ej["rir"]
    nota = ""
    if not hist:
        kg, nota = inicial
        reps = f"{rmin}-{rmax}"
        if kg is None:
            texto = f"Calibrar: {rmin}-{rmax} reps @RIR {max(rir, 2)}"
            nota = nota or "Primera vez: elige un peso con el que te queden 2 repeticiones"
            return {"kg": None, "series": series, "reps": reps, "rir": max(rir, 2), "texto": texto, "nota": nota}
        nota = nota or "Punto de partida"
    else:
        ult = hist[-1]
        kg_ult = statistics.median(s[0] for s in ult)
        reps_ult = [s[1] for s in ult]
        rir_ult = [s[2] for s in ult if s[2] is not None]
        dolor = any(s[3] and re.search(r"dolor|molest", str(s[3]), re.IGNORECASE) for s in ult)
        medias = [sum(s[1] for s in h) / len(h) for h in hist[-3:]]
        cae = len(medias) == 3 and medias[1] <= medias[0] - 2 and medias[2] <= medias[1] - 2
        kg, reps = kg_ult, f"{rmin}-{rmax}"
        if dolor:
            kg, nota = _kg(kg_ult * 0.8), "↓ −20 % por dolor. Si se repite, se cambia el ejercicio y avisa al fisio"
        elif cae:
            kg, nota = _kg(kg_ult * 0.9), "↓ −10 %: las repeticiones bajan dos sesiones seguidas"
        elif min(reps_ult) >= rmax and (not rir_ult or min(rir_ult) >= 1):
            if recuperacion in ("ambar", "rojo"):
                nota = f"Tocaba subir, pero la recuperación está en {recuperacion}: repite el peso"
            elif descarga:
                nota = "Tocaba subir: se hará después de la descarga"
            else:
                kg, nota = _kg(kg_ult + ej["incremento"]), f"↑ +{es(ej['incremento'])} kg: llegaste al máximo de repeticiones"
        else:
            meta = min(rmax, max(reps_ult) + 1)
            reps = f"{rmin}-{rmax}, busca {meta}+"
            nota = f"Última vez: {'/'.join(str(r) for r in reps_ult)} con {es(kg_ult)} kg"
    if descarga:
        series, rir, nota = math.ceil(series / 2), 3, "Semana de descarga: mitad de series, mismo peso, RIR 3-4"
    elif recuperacion == "rojo":
        series, rir = max(1, series - 1), rir + 1
        nota = (nota + ". " if nota else "") + "Recuperación en rojo: una serie menos y RIR +1"
    texto = f"{es(kg)} kg × {reps} @RIR {rir}"
    return {"kg": kg, "series": series, "reps": reps, "rir": rir, "texto": texto, "nota": nota}


def estancado(hist: list[list[tuple]]) -> bool:
    """4 sesiones sin subir ni el peso ni las repeticiones totales."""
    if len(hist) < 4:
        return False
    ult = hist[-4:]
    kgs = [statistics.median(s[0] for s in h) for h in ult]
    totales = [sum(s[1] for s in h) for h in ult]
    return max(kgs[1:]) <= kgs[0] and max(totales[1:]) <= totales[0]


def recuperacion(con: sqlite3.Connection, hoy: date) -> tuple[str, str]:
    """Semáforo de la nota 04: VFC y sueño de 7 días frente a los 60 anteriores."""
    def filas(desde: date, hasta: date):
        return con.execute("SELECT vfc_ms, sueno_min FROM dias WHERE fecha > ? AND fecha <= ?",
                           (desde.isoformat(), hasta.isoformat())).fetchall()
    semana = filas(hoy - timedelta(days=7), hoy)
    base = filas(hoy - timedelta(days=67), hoy - timedelta(days=7))
    vfc7 = [v for v, _ in semana if v]
    sueno7 = [s for _, s in semana if s]
    vfc60 = [v for v, _ in base if v]
    if len(sueno7) < 4 or len(vfc7) < 4 or len(vfc60) < 14:
        return "sin_datos", f"Pocas noches con reloj ({len(sueno7)} de 7): se decide por el rendimiento"
    vfc_ok = statistics.mean(vfc7) >= statistics.mean(vfc60) - 0.5 * statistics.stdev(vfc60)
    sueno_h = statistics.mean(sueno7) / 60
    sueno_ok = sueno_h >= 6.5
    texto = f"VFC {statistics.mean(vfc7):.0f} ms (media {statistics.mean(vfc60):.0f}), sueño {es(sueno_h, 1)} h"
    estado = "verde" if vfc_ok and sueno_ok else "rojo" if not (vfc_ok or sueno_ok) else "ambar"
    return estado, texto


# --------------------------------------------------------------------------
# Plan de la semana
# --------------------------------------------------------------------------
def plan(con: sqlite3.Connection, semana: date, hoy: date, elegidas: dict[str, str] | None = None) -> dict:
    """Objetivos de cada ejercicio (para la variante elegida y para las alternativas)."""
    bloque = bloque_en_vigor(con, semana)
    if bloque is None:
        raise EntrenoError("No hay rutina: ejecuta primero «healthsync.entreno --iniciar».")
    inicio, rutina, _ = bloque
    n = (semana - inicio).days // 7 + 1
    descarga = n % DESCARGA_CADA == 0
    rec, rec_texto = recuperacion(con, hoy)
    elegidas = elegidas or {}
    dias = []
    for d in sorted(rutina["dias"], key=lambda x: ORDEN_DIAS.index(x["dia"])):
        fecha = semana + timedelta(days=ORDEN_DIAS.index(d["dia"]))
        ejercicios = []
        for e in d["ejercicios"]:
            # Solo cuenta lo hecho antes de esta semana (una sesión a medias no cambia el objetivo)
            obj = {v: objetivo(e, sesiones(con, v, antes_de=semana), peso_inicial(con, v), rec, descarga)
                   for v in e["variantes"]}
            elegida = elegidas.get(e["id"]) if elegidas.get(e["id"]) in obj else e["variantes"][0]
            ejercicios.append({"id": e["id"], "variante": elegida, "variantes": e["variantes"],
                               "objetivos": obj, "nota": e.get("nota", "")})
        dias.append({"id": d["id"], "nombre": d["nombre"], "dia": d["dia"], "fecha": fecha.isoformat(),
                     "ejercicios": ejercicios})
    return {"semana": semana.isoformat(), "bloque_inicio": inicio.isoformat(), "semana_bloque": n,
            "descarga": descarga, "recuperacion": rec, "recuperacion_texto": rec_texto,
            "calentamiento": rutina.get("calentamiento", ""), "dias": dias}


def texto_plan(p: dict, solo_dia: str | None = None) -> str:
    cab = (f"🏋️ Semana {p['semana_bloque']} del bloque" + (" · DESCARGA" if p["descarga"] else "")
           + f" · recuperación {p['recuperacion'].replace('_', ' ')}")
    lineas = [cab] if not solo_dia else []
    for d in p["dias"]:
        if solo_dia and d["id"] != solo_dia:
            continue
        f = date.fromisoformat(d["fecha"])
        lineas += ["", f"{d['dia'].capitalize()} {f:%d/%m} · {d['nombre']}"]
        for e in d["ejercicios"]:
            o = e["objetivos"][e["variante"]]
            lineas.append(f"  • {e['variante']}: {o['series']} × {o['texto']}" + (f" — {o['nota']}" if o["nota"] else ""))
    if solo_dia:
        lineas.append(f"\nCalentamiento: {p['calentamiento']}")
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Gemini: consejos y cambios de ejercicios
# --------------------------------------------------------------------------
def esquema_rutina() -> dict:
    txt, num = {"type": "string"}, {"type": "number"}

    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props)}
    return obj({
        "motivo": txt,
        "dias": {"type": "array", "items": obj({
            "id": {"type": "string", "enum": ["P1", "T1", "P2", "T2"]},
            "dia": {"type": "string", "enum": ["martes", "jueves", "viernes", "lunes"]},
            "nombre": txt,
            "ejercicios": {"type": "array", "items": obj({
                "id": txt, "variantes": {"type": "array", "items": txt}, "series": {"type": "integer"},
                "reps": {"type": "array", "items": {"type": "integer"}}, "rir": {"type": "integer"},
                "incremento": num, "musculos": {"type": "array", "items": txt}, "rodilla_max": {"type": "integer"},
                "sobre_cabeza": {"type": "boolean"}, "superserie": txt, "nota": txt, "kg_estimado": num})}})},
        "calentamiento": txt,
    })


def prompt_cambio(rutina: dict, motivo: str, resumen: str) -> str:
    return f"""Eres un entrenador personal y readaptador con experiencia en lesiones. Revisa la rutina de un hombre de
27 años, avanzado antes de una operación de rodilla, que entrena en un Basic-Fit (máquinas, poleas, mancuernas,
multipower) 4 días a la semana, unos 60 minutos, en déficit calórico moderado (recomposición corporal).

MOTIVO DEL CAMBIO: {motivo}

REGLAS QUE NO SE PUEDEN SALTAR (un programa las comprueba):
- Rodilla derecha operada (menisco externo): NUNCA más de {RODILLA_MAX}° de flexión con carga. Nada de sentadilla
  profunda, búlgara, zancada larga, sissy, prensa sin tope ni saltos. Pon en "rodilla_max" la flexión máxima de
  rodilla de cada ejercicio (0 si no se flexiona).
- Manguito rotador derecho: nada de press tras nuca, remo al mentón, fondos ni press con barra con codos abiertos.
  Mejor agarre neutro, mancuernas, landmine, elevaciones en el plano escapular. Como mucho
  {SERIES_SOBRE_CABEZA_MAX} series a la semana de press por encima de la cabeza ("sobre_cabeza": true).
- Asma: nada de HIIT ni sprints.
- Estructura: 4 días, Pierna 1 (martes, cadera), Torso 1 (jueves, empuje horizontal), Pierna 2 (viernes,
  cuádriceps) y Torso 2 (lunes, tirón y hombro). 4-8 ejercicios por día, 2-5 series, como mucho ~60 min.
- Volumen semanal en series efectivas (el primer músculo de "musculos" cuenta 1, los demás 0,5): 10-14 para
  los grandes (pecho, espalda, hombro, cuadriceps, isquios, gluteo) y 4-10 para los pequeños (biceps, triceps,
  gemelo, core, aductores). Usa solo esos nombres de músculo. 3-4 series por ejercicio (5 solo excepcionalmente).
- TIEMPO: cada sesión como mucho {MAX_MINUTOS} min, contando {CALENTAMIENTO_MIN} min de calentamiento (+{PREHAB_TORSO_MIN} de
  prevención del hombro los días de torso), 2,5 min por serie en multiarticulares y 2 en aislados. Una
  superserie ("superserie": id del otro ejercicio, en los dos) cuenta como el más largo de los dos + 30 %.
  Usa superseries de antagonistas (bíceps/tríceps, abductores/aductores) para ahorrar tiempo.
- Cada ejercicio con 1-4 variantes (la primera es la principal; las demás, por si la máquina está ocupada).

QUÉ HACER: cambia los ejercicios que lo necesiten (estancados, con dolor o, si se renueva el bloque, un 30-50 %
para dar variedad), y mantén los que van bien. Si un ejercicio sigue, conserva EXACTAMENTE su id y sus nombres de
variante (así no se pierde su historial). Para ejercicios nuevos pon un "kg_estimado" prudente (0 si no sabes).
"motivo": 2-3 frases explicando los cambios.

RUTINA ACTUAL (JSON):
{json.dumps(rutina, ensure_ascii=False)}

RESUMEN DEL BLOQUE (sesiones por ejercicio):
{resumen}"""


def resumen_bloque(con: sqlite3.Connection, rutina: dict, semana: date) -> tuple[str, list[str], list[str]]:
    """Texto para Gemini, ejercicios estancados y ejercicios con dolor repetido."""
    lineas, estancados, dolor = [], [], []
    for d in rutina["dias"]:
        for e in d["ejercicios"]:
            for v in e["variantes"]:
                h = sesiones(con, v, antes_de=semana)
                if not h:
                    continue
                n_dolor = sum(1 for s in h[-4:] if any(x[3] and re.search(r"dolor|molest", str(x[3]), re.I) for x in s))
                marca = []
                if estancado(h):
                    estancados.append(e["id"])
                    marca.append("ESTANCADO")
                if n_dolor >= 2:
                    dolor.append(e["id"])
                    marca.append("DOLOR REPETIDO")
                ses = "; ".join(f"{es(statistics.median(x[0] for x in s))} kg × {'/'.join(str(x[1]) for x in s)}" for s in h[-6:])
                lineas.append(f"{d['id']} {e['id']} — {v}: {ses} {' '.join(marca)}")
    return "\n".join(lineas) or "(sin registros todavía)", sorted(set(estancados)), sorted(set(dolor))


def proponer_cambio(rutina: dict, motivo: str, resumen: str, log=print) -> dict | None:
    from .gemini import GeminiError, generar
    texto = prompt_cambio(rutina, motivo, resumen)
    errores: list[str] = []
    nueva = None
    for intento in range(3):
        log(f"Gemini: revisando la rutina (intento {intento + 1})…")
        pet = texto
        if nueva is not None:
            pet += ("\n\nTU PROPUESTA ANTERIOR:\n" + json.dumps(nueva, ensure_ascii=False) +
                    "\n\nINCUMPLE ESTAS REGLAS; corrígela y devuélvela completa:\n- " + "\n- ".join(errores))
        try:
            nueva = json.loads(generar(MODELO, pet, esquema=esquema_rutina(), razonamiento="high",
                                       max_salida=MAX_SALIDA_RUTINA, timeout=1200))
        except (GeminiError, json.JSONDecodeError) as e:
            log(f"Respuesta no válida: {e}")
            nueva = None
            continue
        errores = validar_rutina(nueva)
        if not errores:
            return nueva
        log("La revisión encontró:\n- " + "\n- ".join(errores))
    return None


def consejos(p: dict, resumen: str, cambios: str = "") -> str:
    from .gemini import GeminiError, generar
    prompt = f"""Eres el entrenador de un hombre de 27 años en recomposición corporal (déficit moderado), con la rodilla
derecha operada (nunca más de 90° con carga), el manguito rotador derecho delicado y asma. Escribe 3-5 consejos
breves y concretos para su semana de gimnasio, en español de España, sin saludos ni relleno, en viñetas «- ».
NO cambies pesos ni ejercicios: eso ya está decidido; explica y motiva (qué sube y por qué, técnica, descanso).

Semana {p['semana_bloque']} del bloque{' (DESCARGA)' if p['descarga'] else ''}. Recuperación: {p['recuperacion']}
({p['recuperacion_texto']}).
{('Cambios de ejercicios esta semana: ' + cambios) if cambios else ''}

PLAN:
{texto_plan(p)}

HISTORIAL RECIENTE:
{resumen}"""
    try:
        return generar(MODELO, prompt, razonamiento="low", max_salida=4096, timeout=300).strip()
    except GeminiError as e:
        return f"(Sin consejos esta semana: {e})"


# --------------------------------------------------------------------------
# Google Sheet
# --------------------------------------------------------------------------
COLUMNAS = ["id", "Ejercicio / variante", "Serie", "Objetivo", "Kg", "Reps", "RIR", "Notas", "Si cambias de variante"]


def nombre_pestana(semana: date) -> str:
    return f"Semana {semana:%d-%m}"


def filas_pestana(p: dict) -> tuple[list[list], list[tuple[int, list[str]]], list[int]]:
    """Valores de la pestaña, desplegables (fila, opciones) y filas de cabecera de día (0 = encabezado)."""
    filas = [COLUMNAS]
    desplegables, cabeceras = [], []
    for d in p["dias"]:
        f = date.fromisoformat(d["fecha"])
        cabeceras.append(len(filas))
        filas.append(["", f"{d['dia'].upper()} {f:%d/%m} · {d['nombre']}", "", "", "", "", "", "", ""])
        for e in d["ejercicios"]:
            o = e["objetivos"][e["variante"]]
            alternativas = " · ".join(f"{v}: {e['objetivos'][v]['texto']}" for v in e["variantes"] if v != e["variante"])
            info = {1: alternativas + (f" ({e['nota']})" if e["nota"] else ""), 2: o["nota"]}
            for n in range(1, o["series"] + 1):
                if n == 1:
                    desplegables.append((len(filas), e["variantes"]))
                filas.append([f"{p['semana']}|{d['id']}|{e['id']}|{n}", e["variante"] if n == 1 else "", n,
                              o["texto"], "", "", "", "", info.get(n, "")])
    return filas, desplegables, cabeceras


def crear_pestana(s, id_hoja: str, p: dict) -> str:
    from . import sheets
    titulo = nombre_pestana(date.fromisoformat(p["semana"]))
    hoja = sheets.nueva_pestana(s, id_hoja, titulo)
    filas, desplegables, cabeceras = filas_pestana(p)
    sheets.escribir(s, id_hoja, f"'{titulo}'!A1", filas)
    rango = lambda f0, f1, c0, c1: {"sheetId": hoja, "startRowIndex": f0, "endRowIndex": f1,
                                    "startColumnIndex": c0, "endColumnIndex": c1}
    peticiones = [
        {"updateDimensionProperties": {"range": {"sheetId": hoja, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
        {"repeatCell": {"range": rango(0, 1, 0, 9), "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"repeatCell": {"range": rango(1, len(filas), 4, 7),
                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 0.97, "blue": 0.85}}},
                        "fields": "userEnteredFormat.backgroundColor"}},
    ]
    for ancho, c in ((240, 1), (45, 2), (190, 3), (55, 4), (55, 5), (50, 6), (150, 7), (320, 8)):
        peticiones.append({"updateDimensionProperties": {
            "range": {"sheetId": hoja, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
            "properties": {"pixelSize": ancho}, "fields": "pixelSize"}})
    for f in cabeceras:
        peticiones.append({"repeatCell": {"range": rango(f, f + 1, 0, 9), "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.85, "green": 0.88, "blue": 0.93}, "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
    for f, opciones in desplegables:
        peticiones.append({"setDataValidation": {"range": rango(f, f + 1, 1, 2), "rule": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in opciones]},
            "strict": True, "showCustomUi": True}}})
    sheets.lote(s, id_hoja, peticiones)
    return titulo


def _num(x) -> float | None:
    if x in ("", None):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(",", ".").strip())
    except ValueError:
        return None


def leer_filas(filas: list[list]) -> tuple[list[dict], dict[str, str]]:
    """Series rellenas y variante elegida por ejercicio (clave semana|día|ejercicio)."""
    hechas, elegidas = [], {}
    variante = ""
    for f in filas[1:]:
        f = list(f) + [""] * (9 - len(f))
        fid = str(f[0])
        if fid.count("|") != 3:
            continue
        sem, dia, ej, n = fid.split("|")
        if f[1]:
            variante = str(f[1])
            elegidas[f"{sem}|{dia}|{ej}"] = variante
        kg, reps, rir = _num(f[4]), _num(f[5]), _num(f[6])
        if kg is not None and reps is not None:
            hechas.append({"id": fid, "semana": sem, "dia": dia, "ejercicio": ej, "variante": variante,
                           "serie": int(n), "kg": kg, "reps": int(reps), "rir": rir, "notas": str(f[7] or "")})
    return hechas, elegidas


def guardar_series(con: sqlite3.Connection, hechas: list[dict], semana: str) -> int:
    con.execute("DELETE FROM series WHERE semana = ?", (semana,))      # el Sheet manda (borrados incluidos)
    con.executemany("INSERT INTO series (id, semana, dia, ejercicio, variante, serie, kg, reps, rir, notas) "
                    "VALUES (:id, :semana, :dia, :ejercicio, :variante, :serie, :kg, :reps, :rir, :notas)", hechas)
    con.commit()
    return len(hechas)


def hoja_id(con: sqlite3.Connection) -> str | None:
    f = con.execute("SELECT valor FROM estado WHERE clave = 'sheet_gimnasio'").fetchone()
    return f[0] if f else None


def leer(con: sqlite3.Connection, hoy: date, log=print) -> int:
    """Lee las pestañas de esta semana y la siguiente, guarda las series y refresca objetivos pendientes."""
    from . import api, sheets
    id_hoja = hoja_id(con)
    if not id_hoja:
        log("Aún no hay Sheet del gimnasio («--iniciar»).")
        return 0
    if not TOKEN_DRIVE_FILE.exists():
        log("Falta el token de Drive («fase0.py auth-drive»): no se lee el Sheet.")
        return 0
    s = api.session(TOKEN_DRIVE_FILE)
    existentes = sheets.pestanas(s, id_hoja)
    total = 0
    for semana in sorted({calorias.martes_en_vigor(hoy), calorias.proximo_martes(hoy)}):
        titulo = nombre_pestana(semana)
        if titulo not in existentes:
            continue
        filas = sheets.leer(s, id_hoja, f"'{titulo}'!A1:I400")
        hechas, elegidas_ids = leer_filas(filas)
        total += guardar_series(con, hechas, semana.isoformat())
        # Objetivos de lo que aún no has hecho, con la variante que tengas elegida y lo último registrado
        elegidas = {k.split("|")[2]: v for k, v in elegidas_ids.items()}
        p = plan(con, semana, hoy, elegidas)
        nuevas, _, _ = filas_pestana(p)
        hechas_ids = {h["id"] for h in hechas}
        cambios = []
        actual = {str(f[0]): i for i, f in enumerate(filas) if f}
        for fila in nuevas[1:]:
            i = actual.get(str(fila[0]))
            if i is None or not fila[0] or fila[0] in hechas_ids:
                continue
            viejo = list(filas[i]) + [""] * 9
            if str(viejo[3]) != str(fila[3]) or str(viejo[8]) != str(fila[8]):
                cambios.append((f"'{titulo}'!D{i + 1}", [[fila[3]]]))
                cambios.append((f"'{titulo}'!I{i + 1}", [[fila[8]]]))
        sheets.escribir_varios(s, id_hoja, cambios)
        con.execute("UPDATE entreno_semana SET plan = ? WHERE semana = ?",
                    (json.dumps(p, ensure_ascii=False), semana.isoformat()))
        con.commit()
    log(f"Gimnasio: {total} series leídas.")
    return total


# --------------------------------------------------------------------------
# Lunes: semana nueva
# --------------------------------------------------------------------------
def preparar_semana(con: sqlite3.Connection, hoy: date, log=print) -> tuple[dict, str, str]:
    """Renueva la rutina si toca, calcula el plan y pide los consejos. Devuelve (plan, consejos, cambios)."""
    semana = calorias.proximo_martes(hoy)
    bloque = bloque_en_vigor(con, semana)
    if bloque is None:
        raise EntrenoError("No hay rutina: ejecuta primero «healthsync.entreno --iniciar».")
    inicio, rutina, _ = bloque
    n = (semana - inicio).days // 7 + 1
    resumen, estancados, dolor = resumen_bloque(con, rutina, semana)
    cambios = ""
    motivo = ""
    if n > SEMANAS_BLOQUE:
        motivo = f"Han pasado {SEMANAS_BLOQUE} semanas con esta rutina: toca renovar el bloque."
    elif len(estancados) >= ESTANCADOS_PARA_CAMBIAR or dolor:
        motivo = ("Cambiar solo estos ejercicios (y mantener el resto igual): "
                  + ", ".join(sorted(set(estancados + dolor)))
                  + (f". Estancados: {', '.join(estancados)}." if estancados else "")
                  + (f" Con dolor repetido: {', '.join(dolor)}." if dolor else ""))
    if motivo:
        nueva = proponer_cambio(rutina, motivo, resumen, log)
        if nueva:
            for d in nueva["dias"]:
                for e in d["ejercicios"]:
                    kg = e.pop("kg_estimado", 0)
                    for v in e["variantes"]:
                        if kg and not sesiones(con, v) and peso_inicial(con, v)[0] is None:
                            con.execute("INSERT OR REPLACE INTO pesos_iniciales VALUES (?, ?, ?)",
                                        (v, kg, "Estimado por Gemini: ajústalo si hace falta"))
            cambios = nueva.pop("motivo", "")
            guardar_bloque(con, semana if n > SEMANAS_BLOQUE else inicio, nueva,
                           "ia" if n > SEMANAS_BLOQUE else "ia_parcial", cambios)
        else:
            cambios = "(Gemini no dio una rutina que cumpla las reglas: se mantiene la actual)"
    p = plan(con, semana, hoy)
    return p, consejos(p, resumen, cambios), cambios


def guardar_semana(con: sqlite3.Connection, p: dict, pestana: str | None, consejos_txt: str) -> None:
    con.execute("INSERT INTO entreno_semana (semana, plan, pestana, recuperacion, consejos) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (semana) DO UPDATE SET plan = excluded.plan, pestana = excluded.pestana, "
                "recuperacion = excluded.recuperacion, consejos = excluded.consejos, creado = datetime('now')",
                (p["semana"], json.dumps(p, ensure_ascii=False), pestana, p["recuperacion"], consejos_txt))
    con.commit()


def iniciar(con: sqlite3.Connection, pesos: Path | None, hoy: date, log=print, rehacer: bool = False) -> None:
    semana = calorias.proximo_martes(hoy)
    actual = bloque_en_vigor(con, semana)
    if rehacer and actual:
        inicio, _, origen = actual
        if origen != "base" or con.execute("SELECT 1 FROM series WHERE semana >= ?", (inicio.isoformat(),)).fetchone():
            raise EntrenoError("Solo se puede rehacer la rutina base antes de apuntar ninguna serie.")
        con.execute("DELETE FROM rutina_bloque WHERE inicio = ?", (inicio.isoformat(),))
        con.execute("DELETE FROM entreno_semana WHERE semana >= ?", (inicio.isoformat(),))
        con.commit()
        if hoja_id(con) and TOKEN_DRIVE_FILE.exists():
            from . import api, sheets
            sheets.borrar_pestana(api.session(TOKEN_DRIVE_FILE), hoja_id(con), nombre_pestana(inicio))
        log(f"Rutina base anterior y pestaña «{nombre_pestana(inicio)}» borradas.")
    if bloque_en_vigor(con, semana) is None:
        rutina = json.loads(RUTINA_BASE.read_text())
        errores = validar_rutina(rutina)
        if errores:
            raise EntrenoError("La rutina base no cumple las reglas:\n- " + "\n- ".join(errores))
        guardar_bloque(con, semana, rutina, "base", "Rutina de la nota 04")
        log(f"Rutina base guardada (empieza el martes {semana:%d/%m}).")
    if pesos:
        datos = json.loads(pesos.read_text())
        con.executemany("INSERT OR REPLACE INTO pesos_iniciales VALUES (?, ?, ?)",
                        [(v, d.get("kg"), d.get("nota", "")) for v, d in datos.items()])
        con.commit()
        log(f"{len(datos)} pesos de partida guardados.")
    if not hoja_id(con):
        from . import api, sheets
        s = api.session(TOKEN_DRIVE_FILE)
        id_hoja = sheets.crear(s, TITULO_HOJA, "Cómo se usa")
        con.execute("INSERT INTO estado VALUES ('sheet_gimnasio', ?) "
                    "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor", (id_hoja,))
        con.commit()
        sheets.escribir(s, id_hoja, "'Cómo se usa'!A1", [
            ["Cómo se usa"], [""],
            ["Cada lunes aparece una pestaña nueva con la semana (de martes a lunes): una fila por serie."],
            ["Rellena Kg, Reps y RIR (repeticiones que te quedaban en la recámara). «22,5» vale."],
            ["Si la máquina está ocupada, cambia la variante en el desplegable de la primera serie."],
            ["La columna «Si cambias de variante» te dice el objetivo de las alternativas."],
            ["Si algo duele o molesta, escríbelo en Notas («dolor rodilla 4/10»): la siguiente vez baja un 20 %."],
            ["La Pi lee el Sheet cada 4 h y actualiza los objetivos de lo que aún no has hecho."],
            ["Si un día cambias el entreno al miércoles, haz esa sesión y ya: el orden no importa."]])
        log(f"Sheet creado: {sheets.enlace(id_hoja)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: gimnasio")
    ap.add_argument("--iniciar", nargs="?", const="", metavar="PESOS.json",
                    help="rutina base, pesos de partida (opcional) y Sheet")
    ap.add_argument("--guardar", action="store_true", help="semana nueva: plan, pestaña, consejos y Telegram")
    ap.add_argument("--leer", action="store_true", help="lee el Sheet y actualiza los objetivos pendientes")
    ap.add_argument("--rehacer", action="store_true",
                    help="con --iniciar: sustituye la rutina base (y su pestaña) si aún no hay series apuntadas")
    ap.add_argument("--fecha", type=date.fromisoformat, help="como si hoy fuera esta fecha")
    args = ap.parse_args()
    hoy = args.fecha or datetime.now(TZ).date()
    con = conectar(DB_FILE)
    try:
        if args.iniciar is not None:
            iniciar(con, Path(args.iniciar) if args.iniciar else None, hoy, rehacer=args.rehacer)
            if hoja_id(con):
                from . import api
                p = plan(con, calorias.proximo_martes(hoy), hoy)
                titulo = crear_pestana(api.session(TOKEN_DRIVE_FILE), hoja_id(con), p)
                guardar_semana(con, p, titulo, "")
                print(texto_plan(p))
            return 0
        if args.leer:
            leer(con, hoy)
            return 0
        if args.guardar:
            from . import api, sheets, telegram
            try:
                leer(con, hoy)                             # lo último antes de calcular
            except Exception as e:  # noqa: BLE001
                print(f"No se pudo leer el Sheet: {e}")
            p, cons, cambios = preparar_semana(con, hoy)
            titulo = crear_pestana(api.session(TOKEN_DRIVE_FILE), hoja_id(con), p) if hoja_id(con) else None
            guardar_semana(con, p, titulo, cons)
            msg = (f"🏋️ Gimnasio: semana {p['semana_bloque']} del bloque" + (" (descarga)" if p["descarga"] else "")
                   + f", recuperación {p['recuperacion'].replace('_', ' ')}.\n"
                   + (f"\n🔄 Cambios: {cambios}\n" if cambios else "")
                   + f"\n{cons}\n\n" + (f"Sheet: {sheets.enlace(hoja_id(con))}\n" if hoja_id(con) else "")
                   + "/entreno para la sesión del día")
            telegram.enviar(msg)
            print(texto_plan(p))
            print(cons)
            return 0
        print(texto_plan(plan(con, calorias.proximo_martes(hoy), hoy)))
        return 0
    except EntrenoError as e:
        print(e)
        if args.guardar:
            from . import telegram
            telegram.enviar(f"⚠️ Gimnasio: {e}")
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())

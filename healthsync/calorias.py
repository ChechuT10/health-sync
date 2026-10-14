"""Calculadora de calorías y macros (código determinista; la IA nunca decide estos números).

Cada lunes propone el objetivo diario de la semana siguiente, de martes a lunes (el martes se
compra la comida), a partir del peso, el % de grasa, los pasos y la historia de semanas
anteriores, y lo explica. Los límites de seguridad están aquí, en el código, a
propósito: para saltárselos hay que cambiar el código, no un archivo de configuración.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.calorias              # muestra la propuesta de la semana que viene
  python -m healthsync.calorias --guardar    # la guarda como plan de la semana y la manda por Telegram
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import DB_FILE, TZ
from .db import conectar
from .medidas import es, persona


def kc(x: float) -> str:
    """Kilocalorías con punto de miles: 2250 -> '2.250'."""
    return f"{round(x):,}".replace(",", ".")

# ---- Límites de seguridad (ver la nota 03 de la bóveda) ----
DEFICIT_MAX = 0.25          # déficit máximo sobre el gasto (real si se conoce)
SUELO_MIN_KCAL = 1800       # el suelo es el metabolismo basal actual, y nunca menos de esto
CAMBIO_MAX = 150            # kcal por ajuste
DIAS_ENTRE_CAMBIOS = 14
RITMO_MAX = 1.0             # % del peso por semana: por encima, se suben las calorías
RITMO_MIN = 0.3             # por debajo dos revisiones seguidas, se bajan
PROTEINA_MIN, GRASA_MIN = 150, 60
CICLO_SEMANAS, DESCANSO_SEMANAS = 14, 2   # 12 semanas de déficit + 2 a mantenimiento
DEFICIT_INICIAL = 0.21

# ---- Ventanas de datos ----
DIAS_TENDENCIA = 28         # pesadas que entran en la tendencia
PESADAS_MIN = 3             # sin esto no se ajusta nada
DIAS_PESADAS_MIN = 14       # las pesadas tienen que cubrir al menos este periodo
KCAL_POR_KG = 7700
SEMANAS_AGUA = 2            # al empezar, el peso baja por agua: ni se ajusta ni cuenta en la tendencia


@dataclass
class Propuesta:
    semana: date                       # martes en que empieza (va de martes a lunes)
    numero: int                        # semana del plan (1, 2…)
    kcal: int
    proteina_g: int
    grasa_g: int
    hidratos_g: int
    peso_kg: float
    metabolismo_basal: int
    gasto_estimado: int
    gasto_real: int | None
    ritmo_pct: float | None            # % del peso perdido por semana (positivo = baja)
    fase: str                          # deficit / descanso
    motivo: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Datos
# --------------------------------------------------------------------------
def proximo_martes(d: date) -> date:
    """Inicio del plan que se prepara el día d: el martes siguiente (el lunes, mañana)."""
    return d + timedelta(days=(1 - d.weekday()) % 7 or 7)


def martes_en_vigor(d: date) -> date:
    """Inicio del plan que se está siguiendo el día d: el último martes (el lunes es su último día)."""
    return d - timedelta(days=(d.weekday() - 1) % 7)


def pesadas(con: sqlite3.Connection, hasta: date, dias: int = DIAS_TENDENCIA) -> list[tuple[date, float]]:
    filas = con.execute(
        "SELECT fecha, avg(peso_kg) FROM pesos WHERE peso_kg IS NOT NULL AND fecha > ? AND fecha <= ? "
        "GROUP BY fecha ORDER BY fecha", ((hasta - timedelta(days=dias)).isoformat(), hasta.isoformat()))
    return [(date.fromisoformat(f), p) for f, p in filas]


def tendencia(puntos: list[tuple[date, float]]) -> tuple[float, float] | None:
    """Recta por mínimos cuadrados: (peso estimado en la última fecha, kg por semana).
    None si no hay suficientes pesadas."""
    if len(puntos) < PESADAS_MIN or (puntos[-1][0] - puntos[0][0]).days < DIAS_PESADAS_MIN:
        return None
    x = [(d - puntos[0][0]).days for d, _ in puntos]
    y = [p for _, p in puntos]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    pendiente = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sum((a - mx) ** 2 for a in x)
    return my + pendiente * (x[-1] - mx), pendiente * 7


def ultima_grasa(con: sqlite3.Connection, hasta: date) -> float | None:
    fila = con.execute("SELECT grasa_pct FROM pesos WHERE grasa_pct IS NOT NULL AND fecha <= ? "
                       "AND fecha > ? ORDER BY fecha DESC LIMIT 1",
                       (hasta.isoformat(), (hasta - timedelta(days=60)).isoformat())).fetchone()
    return fila[0] if fila else None


def pasos_medios(con: sqlite3.Connection, hasta: date) -> float | None:
    fila = con.execute("SELECT avg(pasos) FROM dias WHERE pasos IS NOT NULL AND fecha > ? AND fecha < ?",
                       ((hasta - timedelta(days=28)).isoformat(), hasta.isoformat())).fetchone()
    return fila[0]


def planes(con: sqlite3.Connection, antes_de: date) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    filas = con.execute("SELECT * FROM plan_semana WHERE semana < ? ORDER BY semana",
                        (antes_de.isoformat(),)).fetchall()
    con.row_factory = None
    return filas


# --------------------------------------------------------------------------
# Fórmulas
# --------------------------------------------------------------------------
def edad(nacimiento: str, hoy: date) -> int:
    n = date.fromisoformat(nacimiento)
    return hoy.year - n.year - ((hoy.month, hoy.day) < (n.month, n.day))


def metabolismo_basal(peso: float, altura: float, anos: int, grasa: float | None) -> float:
    """Media de Mifflin-St Jeor y Katch-McArdle (esta solo si hay % de grasa reciente)."""
    mifflin = 10 * peso + 6.25 * altura - 5 * anos + 5
    if grasa is None:
        return mifflin
    katch = 370 + 21.6 * peso * (1 - grasa / 100)
    return (mifflin + katch) / 2


def factor_actividad(pasos: float | None) -> float:
    """Incluye 4 días de fuerza por semana; lo que cambia son los pasos."""
    if pasos is None:
        return 1.45
    for limite, factor in ((5000, 1.40), (7500, 1.45), (10000, 1.50)):
        if pasos < limite:
            return factor
    return 1.55


def redondear(kcal: float) -> int:
    return int(round(kcal / 50) * 50)


def macros(kcal: int, peso: float) -> tuple[int, int, int]:
    proteina = max(PROTEINA_MIN, round(1.65 * peso))
    grasa = max(GRASA_MIN, round(0.28 * kcal / 9))
    hidratos = max(0, round((kcal - 4 * proteina - 9 * grasa) / 4))
    return proteina, grasa, hidratos


# --------------------------------------------------------------------------
# Propuesta semanal
# --------------------------------------------------------------------------
def proponer(con: sqlite3.Connection, hoy: date | None = None) -> Propuesta | None:
    hoy = hoy or datetime.now(TZ).date()
    semana = proximo_martes(hoy)
    p = persona()
    puntos = pesadas(con, hoy, dias=3650)
    if not puntos:
        return None                                    # falta el primer peso
    historia = planes(con, semana)
    inicio = date.fromisoformat(historia[0]["semana"]) if historia else semana
    # Las dos primeras semanas del plan se pierde agua y glucógeno (1-3 kg que no son grasa):
    # esas pesadas no entran en la tendencia, o inflarían el ritmo y el gasto real. Cuentan desde
    # el lunes en que se prepara la semana 3 (último día de la 2)
    fin_agua = inicio + timedelta(days=SEMANAS_AGUA * 7 - 1)
    utiles = [x for x in puntos if (hoy - x[0]).days < DIAS_TENDENCIA and (not historia or x[0] >= fin_agua)]
    tend = tendencia(utiles)
    peso = round(tend[0] if tend else puntos[-1][1], 1)
    ritmo = round(-tend[1] / peso * 100, 2) if tend else None
    grasa = ultima_grasa(con, hoy)
    basal = metabolismo_basal(peso, float(p["altura_cm"]), edad(p["fecha_nacimiento"], hoy), grasa)
    pasos = pasos_medios(con, hoy)
    gasto_est = basal * factor_actividad(pasos)
    suelo = max(basal, SUELO_MIN_KCAL)

    numero = (semana - inicio).days // 7 + 1
    descanso = (numero - 1) % CICLO_SEMANAS >= CICLO_SEMANAS - DESCANSO_SEMANAS
    motivo: list[str] = []

    # Gasto real: lo que se ha comido (los planes, a falta del registro de comidas) + lo perdido
    gasto_real = None
    ultimas = [h for h in historia if date.fromisoformat(h["semana"]) >= semana - timedelta(weeks=4)]
    if tend and len(ultimas) >= 3:
        comido = sum(h["kcal"] for h in ultimas) / len(ultimas)
        gasto_real = comido - tend[1] * KCAL_POR_KG / 7
        motivo.append(f"Gasto real ≈ {kc(gasto_real)} kcal: comes ~{kc(comido)} y "
                      f"{'pierdes' if tend[1] < 0 else 'ganas'} {es(abs(tend[1]), 2)} kg/semana.")
    gasto = gasto_real or gasto_est

    if not historia:
        kcal = redondear(gasto_est * (1 - DEFICIT_INICIAL))
        motivo.append(f"Primera semana: gasto estimado {kc(gasto_est)} kcal (metabolismo basal "
                      f"{kc(basal)} × {es(factor_actividad(pasos))} por actividad) menos un "
                      f"{round(DEFICIT_INICIAL * 100)} %.")
    else:
        previo = historia[-1]
        kcal = previo["kcal"] if previo["fase"] == "deficit" else _ultimo_deficit(historia, previo["kcal"])
        cambio = _ultimo_cambio(historia)
        dias_sin_cambio = (semana - cambio).days if cambio else 999
        if descanso:
            pass
        elif numero <= SEMANAS_AGUA:
            motivo.append("Semanas 1-2: no se ajusta nada (el peso baja por agua y glucógeno).")
        elif tend is None:
            motivo.append(f"Faltan pesadas para ver la tendencia: hacen falta {PESADAS_MIN} en "
                          f"{DIAS_PESADAS_MIN} días o más (sin contar las 2 primeras semanas). Se mantiene.")
        elif ritmo > RITMO_MAX:                    # subir siempre es seguro: no espera
            kcal += CAMBIO_MAX
            motivo.append(f"Bajas un {es(ritmo, 2)} % por semana (> {es(RITMO_MAX)} %): +{CAMBIO_MAX} kcal.")
        elif dias_sin_cambio < DIAS_ENTRE_CAMBIOS:
            motivo.append(f"Último cambio hace {dias_sin_cambio} días: se espera a {DIAS_ENTRE_CAMBIOS}.")
        elif ritmo < RITMO_MIN:
            if previo["ritmo_pct"] is not None and previo["ritmo_pct"] < RITMO_MIN:
                kcal -= 100
                motivo.append(f"Segunda revisión seguida por debajo del {es(RITMO_MIN)} % "
                              f"({es(ritmo, 2)} %): −100 kcal (o +2.000 pasos al día).")
            else:
                motivo.append(f"Ritmo bajo ({es(ritmo, 2)} %). Si se repite en la próxima revisión, "
                              f"−100 kcal.")
        else:
            motivo.append(f"Ritmo de {es(ritmo, 2)} % por semana: dentro de lo previsto, se mantiene.")
        kcal = max(previo["kcal"] - CAMBIO_MAX, min(previo["kcal"] + CAMBIO_MAX, kcal)) \
            if previo["fase"] == "deficit" else kcal

    if descanso:
        kcal = redondear(gasto)
        motivo.append(f"Semana {numero}: descanso de dieta, a mantenimiento ({kc(kcal)} kcal).")
    # Límites que mandan sobre todo lo anterior
    if kcal < gasto * (1 - DEFICIT_MAX):
        necesario = redondear(gasto * (1 - DEFICIT_MAX) + 25)
        tope = historia[-1]["kcal"] + CAMBIO_MAX if historia else necesario
        kcal = max(kcal, min(necesario, tope))       # sube de 150 en 150, no de golpe
        motivo.append(f"Límite: déficit máximo del {round(DEFICIT_MAX * 100)} % → hace falta "
                      f"{kc(necesario)} kcal" + ("." if kcal == necesario else
                                                  f"; se sube poco a poco ({kc(kcal)} esta semana)."))
    if kcal < suelo:
        kcal = redondear(suelo + 25)
        motivo.append(f"Límite: nunca por debajo del metabolismo basal ({kc(suelo)} kcal).")

    proteina, grasa_g, hidratos = macros(kcal, peso)
    return Propuesta(semana, numero, kcal, proteina, grasa_g, hidratos, peso, round(basal),
                     round(gasto_est), round(gasto_real) if gasto_real else None, ritmo,
                     "descanso" if descanso else "deficit", motivo)


def _ultimo_deficit(historia, por_defecto: int) -> int:
    """Al volver de un descanso, se retoma el último objetivo de déficit."""
    return next((h["kcal"] for h in reversed(historia) if h["fase"] == "deficit"), por_defecto)


def _ultimo_cambio(historia) -> date | None:
    for anterior, actual in zip(reversed(historia[:-1]), reversed(historia)):
        if actual["kcal"] != anterior["kcal"] and actual["fase"] == anterior["fase"] == "deficit":
            return date.fromisoformat(actual["semana"])
    return date.fromisoformat(historia[0]["semana"]) if historia else None


def guardar(con: sqlite3.Connection, pr: Propuesta) -> None:
    con.execute(
        """INSERT INTO plan_semana (semana, numero, kcal, proteina_g, grasa_g, hidratos_g, peso_kg,
               metabolismo_basal, gasto_estimado, gasto_real, ritmo_pct, fase, motivo)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (semana) DO UPDATE SET numero = excluded.numero, kcal = excluded.kcal,
               proteina_g = excluded.proteina_g, grasa_g = excluded.grasa_g,
               hidratos_g = excluded.hidratos_g, peso_kg = excluded.peso_kg,
               metabolismo_basal = excluded.metabolismo_basal, gasto_estimado = excluded.gasto_estimado,
               gasto_real = excluded.gasto_real, ritmo_pct = excluded.ritmo_pct, fase = excluded.fase,
               motivo = excluded.motivo, creado = datetime('now')""",
        (pr.semana.isoformat(), pr.numero, pr.kcal, pr.proteina_g, pr.grasa_g, pr.hidratos_g, pr.peso_kg,
         pr.metabolismo_basal, pr.gasto_estimado, pr.gasto_real, pr.ritmo_pct, pr.fase, "\n".join(pr.motivo)))
    con.commit()


def plan_guardado(con: sqlite3.Connection, semana: date) -> Propuesta | None:
    con.row_factory = sqlite3.Row
    f = con.execute("SELECT * FROM plan_semana WHERE semana = ?", (semana.isoformat(),)).fetchone()
    con.row_factory = None
    if f is None:
        return None
    return Propuesta(semana, f["numero"], f["kcal"], f["proteina_g"], f["grasa_g"], f["hidratos_g"],
                     f["peso_kg"], f["metabolismo_basal"], f["gasto_estimado"], f["gasto_real"],
                     f["ritmo_pct"], f["fase"], (f["motivo"] or "").splitlines())


def texto(pr: Propuesta | None) -> str:
    if pr is None:
        return "Aún no hay ningún peso desde el 01/09: mándalo con /peso y calculo tus calorías."
    lineas = [
        f"🍽️ Semana {pr.numero} (martes {pr.semana:%d/%m} – lunes {pr.semana + timedelta(days=6):%d/%m})" + (" · descanso de dieta" if pr.fase == "descanso" else ""),
        f"Objetivo: {kc(pr.kcal)} kcal al día",
        f"Proteína {pr.proteina_g} g · grasa {pr.grasa_g} g · hidratos {pr.hidratos_g} g",
        "",
        f"Peso de referencia {es(pr.peso_kg)} kg" + (f", ritmo {es(pr.ritmo_pct, 2)} %/semana" if pr.ritmo_pct is not None else ""),
        f"Metabolismo basal ~{kc(pr.metabolismo_basal)} kcal; gasto estimado ~{kc(pr.gasto_estimado)} kcal"
        + (f"; gasto real ~{kc(pr.gasto_real)} kcal" if pr.gasto_real else ""),
        "",
        *pr.motivo,
    ]
    return "\n".join(lineas)


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: calorías y macros de la semana")
    ap.add_argument("--guardar", action="store_true", help="guarda el plan de la semana y lo manda por Telegram")
    ap.add_argument("--fecha", type=date.fromisoformat, help="calcular como si hoy fuera esta fecha")
    args = ap.parse_args()
    con = conectar(DB_FILE)
    pr = proponer(con, args.fecha)
    print(texto(pr))
    if args.guardar:
        from . import telegram
        if pr is not None:
            guardar(con, pr)
        telegram.enviar(texto(pr))       # sin peso, al menos te lo recuerda
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

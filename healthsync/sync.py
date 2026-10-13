"""Sincronización de Google Health con la base de datos SQLite.

Uso (en la Raspberry, como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.sync                     # últimos 3 días (lo que lanza el temporizador)
  python -m healthsync.sync --dias 10
  python -m healthsync.sync --desde 2026-01-01  # carga histórica
  python -m healthsync.sync --importar /var/lib/healthsync/db/raw   # desde una descarga de 'fase0.py dump'
  python -m healthsync.sync --reconstruir       # solo rehace las tablas limpias
  python -m healthsync.sync --descartar weight 2026-09-23 --motivo "pesos de prueba"
                                                # ignora esos registros (clave o fecha)

Sale con código 1 si algún tipo de dato falla (para que systemd avise).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DB_FILE
from .db import conectar
from .normalizar import ROLLUP, fecha_de, guardar_raw, reconstruir

# Resúmenes diarios y muestras: pocos registros, se piden enteros en cada pasada
TIPOS_LISTA = [
    "daily-resting-heart-rate",
    "daily-heart-rate-variability",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
    "weight",
    "body-fat",
]
# Sesiones (máximo 25 por página). exercise admite filtro de fecha; sleep no (error 400
# INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER), así que se lee de lo más nuevo hacia atrás.
TIPOS_SESION = ["exercise"]
# Agregados por día: (tipo, días máximos por llamada)
TIPOS_ROLLUP = [
    ("steps", 90),
    ("distance", 90),
    ("floors", 90),
    ("active-energy-burned", 90),
    ("active-zone-minutes", 90),
    ("sedentary-period", 90),
    ("time-in-heart-rate-zone", 90),
    ("total-calories", 14),
    ("heart-rate", 14),
]


def descargar(con: sqlite3.Connection, desde: date, hasta_excl: date) -> list[str]:
    """Descarga de la API y guarda en raw. Devuelve los errores (vacío si todo fue bien)."""
    from . import api   # solo aquí: --importar y --reconstruir no necesitan google-auth

    s = api.session()
    errores = []

    def paso(tipo, obtener):
        try:
            n = guardar_raw(con, tipo, obtener())
            con.commit()
            print(f"  ✓ {tipo:<34} {n:>5} nuevos o cambiados")
        except api.ApiError as e:
            con.rollback()
            errores.append(f"{tipo}: {e}")
            print(f"  ✗ {tipo:<34} {e}")

    for tipo in TIPOS_LISTA:
        paso(tipo, lambda t=tipo: api.list_all(s, t, {"pageSize": 1000}))
    for tipo in TIPOS_SESION:
        # Una noche puede empezar el día anterior al primero que se pide
        campo = tipo.replace("-", "_")
        flt = f'{campo}.interval.civil_start_time >= "{(desde - timedelta(days=1)).isoformat()}T00:00:00"'
        paso(tipo, lambda t=tipo, f=flt: api.list_all(s, t, {"filter": f, "pageSize": 25}))
    paso("sleep", lambda: api.list_recientes(s, "sleep", lambda p: fecha_de("sleep", p), desde))
    for tipo, max_dias in TIPOS_ROLLUP:
        paso(tipo + ROLLUP, lambda t=tipo, m=max_dias: api.daily_rollup(s, t, desde, hasta_excl, m))
    return errores


def importar(con: sqlite3.Connection, carpeta: Path) -> None:
    """Carga una descarga completa de 'fase0.py dump' (archivos .jsonl y .daily.json)."""
    for f in sorted(carpeta.glob("*.jsonl")):
        if f.name.endswith(".intradia.jsonl"):
            continue
        puntos = [json.loads(linea) for linea in f.read_text().splitlines() if linea.strip()]
        print(f"  {f.stem:<38} {guardar_raw(con, f.stem, puntos):>6} nuevos o cambiados")
    for f in sorted(carpeta.glob("*.daily.json")):
        tipo = f.name.removesuffix(".daily.json") + ROLLUP
        print(f"  {tipo:<38} {guardar_raw(con, tipo, json.loads(f.read_text())):>6} nuevos o cambiados")
    con.commit()


def resumen(con: sqlite3.Connection, dias: int = 10) -> None:
    filas = con.execute(
        """SELECT fecha, pasos, kcal_total, sueno_min, fc_reposo, vfc_ms, tipo_dia,
                  (SELECT count(*) FROM entrenamientos e WHERE e.fecha = d.fecha)
           FROM dias d ORDER BY fecha DESC LIMIT ?""", (dias,)).fetchall()
    f = lambda v, fmt: fmt.format(v) if v is not None else "—"  # noqa: E731
    print(f"\n  {'Fecha':<12}{'Pasos':>8}{'Kcal':>7}{'Sueño':>7}{'FC rep':>8}{'VFC':>6}{'Entr':>6}  Tipo de día")
    for fecha, pasos, kcal, sueno, fcr, vfc, tipo, entr in reversed(filas):
        print(f"  {fecha:<12}{f(pasos, '{:,}').replace(',', '.'):>8}{f(kcal, '{:,.0f}').replace(',', '.'):>7}"
              f"{f(sueno and sueno / 60, '{:.1f}h'):>7}{f(fcr, '{}'):>8}{f(vfc, '{:.0f}'):>6}"
              f"{entr or '—':>6}  {tipo}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: sincronización con SQLite")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dias", type=int, default=3, help="días hacia atrás que se vuelven a pedir (3)")
    g.add_argument("--desde", type=date.fromisoformat, help="fecha inicial (carga histórica)")
    g.add_argument("--importar", type=Path, help="carpeta de 'fase0.py dump' en vez de la API")
    g.add_argument("--reconstruir", action="store_true", help="solo rehace las tablas limpias")
    g.add_argument("--descartar", nargs=2, metavar=("TIPO", "CLAVE_O_FECHA"),
                   help="no usar ese registro, o todos los de ese tipo en esa fecha (AAAA-MM-DD)")
    ap.add_argument("--motivo", help="motivo del descarte")
    ap.add_argument("--db", type=Path, default=DB_FILE)
    args = ap.parse_args()

    con = conectar(args.db)
    inicio = datetime.now().isoformat(timespec="seconds")
    run = con.execute("INSERT INTO ejecuciones (inicio) VALUES (?)", (inicio,)).lastrowid
    con.commit()
    errores: list[str] = []
    try:
        if args.descartar:
            tipo, clave = args.descartar
            columna = "fecha" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clave) else "clave"
            n = con.execute(f"""INSERT OR IGNORE INTO descartes (tipo, clave, motivo)
                                SELECT tipo, clave, ? FROM raw WHERE tipo = ? AND {columna} = ?""",
                            (args.motivo, tipo, clave)).rowcount
            con.commit()
            print(f"==> {n} registro(s) de {tipo} descartados")
        elif args.importar:
            print(f"==> Importando {args.importar}")
            importar(con, args.importar)
        elif not args.reconstruir:
            hoy = date.today()
            desde = args.desde or hoy - timedelta(days=args.dias)
            print(f"==> Descargando del {desde:%d/%m/%Y} a hoy")
            errores = descargar(con, desde, hoy + timedelta(days=1))
        totales = reconstruir(con)
        print("==> Tablas limpias: " + ", ".join(f"{t} {n}" for t, n in totales.items()))
        resumen(con)
        estado, detalle = ("parcial", "\n".join(errores)) if errores else ("ok", json.dumps(totales))
    except Exception as e:  # noqa: BLE001
        estado, detalle = "error", f"{type(e).__name__}: {e}"
        print(f"  ✗ {detalle}", file=sys.stderr)
    con.execute("UPDATE ejecuciones SET fin = ?, estado = ?, detalle = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), estado, detalle, run))
    con.commit()
    con.close()
    return 0 if estado == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

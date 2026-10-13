"""Peso y medidas que mandas por el bot: lectura del texto, % de grasa y guardado.

Cada medida se guarda primero en la Pi (tabla medidas) y luego se intenta escribir en
Google Health (peso y % de grasa), que es la copia que sobrevive si la Pi falla.
"""
from __future__ import annotations

import math
import re
import sqlite3
import tomllib
from datetime import datetime, timezone

from .config import CONFIG_FILE, TZ

# Rangos razonables: fuera de ellos es casi seguro un error al escribir
PESO_KG = (40, 250)
CINTURA_CM = (50, 200)
CUELLO_CM = (25, 70)


class MedidaError(ValueError):
    pass


def es(x: float, decimales: int | None = None) -> str:
    """Número con coma decimal: 99.4 -> '99,4'."""
    return (f"{x:.{decimales}f}" if decimales is not None else f"{x:g}").replace(".", ",")


def persona() -> dict:
    if not CONFIG_FILE.exists():
        raise MedidaError(f"Falta {CONFIG_FILE} (copia config.example.toml y pon tu altura)")
    return tomllib.loads(CONFIG_FILE.read_text())["persona"]


def numero(texto: str, nombre: str, rango: tuple[float, float]) -> float:
    try:
        x = float(texto.replace(",", "."))
    except ValueError:
        raise MedidaError(f"No entiendo «{texto}» como {nombre}") from None
    if not rango[0] <= x <= rango[1]:
        raise MedidaError(f"{nombre.capitalize()} de {es(x)} fuera de rango ({rango[0]}-{rango[1]})")
    return x


def leer_peso(args: str) -> float:
    """'/peso 99,4' -> 99.4"""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*", args)
    if not m:
        raise MedidaError("Escribe el peso así: /peso 99,4")
    return numero(m.group(1), "peso", PESO_KG)


def leer_medidas(args: str) -> dict[str, float]:
    """'/medidas cintura 102 cuello 41' o '/medidas 102 41' (cintura y cuello, en cm).
    Opcionalmente 'peso 99,4'."""
    texto = args.lower().replace("cm", " ")
    patron = r"(cintura|cuello|peso)\s*:?\s*(\d+(?:[.,]\d+)?)"
    valores = dict(re.findall(patron, texto))
    # Números sin nombre: cintura y luego cuello (p. ej. "/medidas 102 41 peso 99,4")
    sueltos = re.findall(r"\d+(?:[.,]\d+)?", re.sub(patron, " ", texto))
    for nombre in ("cintura", "cuello"):
        if nombre not in valores and sueltos:
            valores[nombre] = sueltos.pop(0)
    if "cintura" not in valores or "cuello" not in valores:
        raise MedidaError("Escribe las medidas así: /medidas cintura 102 cuello 41")
    out = {"cintura": numero(valores["cintura"], "cintura", CINTURA_CM),
           "cuello": numero(valores["cuello"], "cuello", CUELLO_CM)}
    if "peso" in valores:
        out["peso"] = numero(valores["peso"], "peso", PESO_KG)
    return out


def grasa_marina(cintura: float, cuello: float, altura: float, sexo: str = "hombre") -> float:
    """% de grasa con el método de la Marina de EE. UU. (hombres, en cm). Error de ±3-4 puntos,
    pero la tendencia es fiable si se mide siempre igual."""
    if sexo != "hombre":
        raise MedidaError("La fórmula para mujeres necesita también la cadera (aún no está)")
    if cintura <= cuello:
        raise MedidaError("La cintura tiene que ser mayor que el cuello")
    return round(495 / (1.0324 - 0.19077 * math.log10(cintura - cuello)
                        + 0.15456 * math.log10(altura)) - 450, 1)


def guardar(con: sqlite3.Connection, peso: float | None = None, cintura: float | None = None,
            cuello: float | None = None) -> dict:
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    local = ahora.astimezone(TZ)
    grasa = None
    if cintura is not None and cuello is not None:
        p = persona()
        grasa = grasa_marina(cintura, cuello, float(p["altura_cm"]), p.get("sexo", "hombre"))
    cur = con.execute(
        """INSERT INTO medidas (momento, fecha, hora, peso_kg, cintura_cm, cuello_cm, grasa_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ahora.isoformat().replace("+00:00", "Z"), local.date().isoformat(),
         local.strftime("%H:%M"), peso, cintura, cuello, grasa))
    con.commit()
    return {"id": cur.lastrowid, "grasa": grasa, "momento": ahora, "local": local}


def subir_a_google(con: sqlite3.Connection, m: dict, peso: float | None) -> list[str]:
    """Escribe peso y % de grasa en Google Health. Devuelve los errores (vacío si fue bien)."""
    from . import api   # google-auth solo hace falta aquí

    sample = {"physicalTime": m["momento"].isoformat().replace("+00:00", "Z"),
              "utcOffset": f"{int(m['local'].utcoffset().total_seconds())}s"}
    envios = []
    if peso is not None:
        envios.append(("weight", "gh_peso", {"weight": {"sampleTime": sample, "weightGrams": round(peso * 1000)}}))
    if m["grasa"] is not None:
        envios.append(("body-fat", "gh_grasa", {"bodyFat": {"sampleTime": sample, "percentage": m["grasa"]}}))
    errores = []
    try:
        s = api.session()
        for tipo, columna, cuerpo in envios:
            try:
                r = api.request(s, "POST", f"{api.API}/dataTypes/{tipo}/dataPoints", json=cuerpo)
                # Puede devolver el punto creado o una operación que lo contiene
                nombre = r.get("name") or (r.get("response") or {}).get("name") or "?"
                con.execute(f"UPDATE medidas SET {columna} = ? WHERE id = ?", (nombre, m["id"]))
            except api.ApiError as e:
                errores.append(f"{tipo}: {str(e)[:300]}")
    except api.ApiError as e:
        errores.append(str(e)[:300])
    con.commit()
    return errores

"""Tabla de alimentos (alimentos.csv): la referencia con la que el código recalcula los menús.

El LLM propone platos y gramos; los macros siempre se calculan aquí, con esta tabla.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

TABLA = Path(__file__).with_name("alimentos.csv")
GRUPOS = {"proteina", "pescado", "huevo", "lacteo", "cereal", "tuberculo", "legumbre", "verdura",
          "fruta", "grasa", "suplemento", "otro"}


@dataclass(frozen=True)
class Alimento:
    id: str
    nombre: str
    grupo: str
    estado: str
    kcal: float                 # por 100 g
    proteina: float
    grasa: float
    hidratos: float
    fibra: float
    unidad_g: float | None      # peso de una unidad o ración típica
    max_semana: int | None      # veces por semana como mucho
    notas: str

    def macros(self, gramos: float) -> dict[str, float]:
        f = gramos / 100
        return {"kcal": self.kcal * f, "proteina": self.proteina * f, "grasa": self.grasa * f,
                "hidratos": self.hidratos * f, "fibra": self.fibra * f}


@cache
def tabla(excluir: frozenset[str] = frozenset()) -> dict[str, Alimento]:
    with TABLA.open(encoding="utf-8") as f:
        filas = csv.DictReader(l for l in f if not l.startswith("#"))
        out = {}
        for r in filas:
            if r["id"] in excluir:
                continue
            num = {k: float(r[k]) for k in ("kcal", "proteina", "grasa", "hidratos", "fibra")}
            out[r["id"]] = Alimento(r["id"], r["nombre"], r["grupo"], r["estado"], **num,
                                    unidad_g=float(r["unidad_g"]) if r["unidad_g"] else None,
                                    max_semana=int(r["max_semana"]) if r["max_semana"] else None,
                                    notas=r["notas"])
    return out

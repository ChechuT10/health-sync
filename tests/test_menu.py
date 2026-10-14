"""Revisión del menú de Gemini (lo único que calcula la Pi).  python3 -m unittest

tests/datos/menu_ejemplo.json es un menú real de Gemini para 2.150 kcal (169 P / 67 G / 218 H).
"""
import copy
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from healthsync import bot, calorias, menu as m
from healthsync.db import conectar

EJEMPLO = json.loads((Path(__file__).parent / "datos" / "menu_ejemplo.json").read_text())
OBJETIVO = {"kcal": 2150, "proteina": 169, "grasa": 67, "hidratos": 218}
PREFS = m.PREFERENCIAS


class Revision(unittest.TestCase):
    def revisar(self, menu):
        return m.revisar(menu, OBJETIVO, m.suma_fijos(PREFS), PREFS)

    def setUp(self):
        self.menu = copy.deepcopy(EJEMPLO)

    def test_el_ejemplo_cuadra(self):
        errores, totales = self.revisar(self.menu)
        self.assertEqual(errores, [])
        self.assertEqual(list(totales), m.DIAS)
        for t in totales.values():
            self.assertAlmostEqual(t["kcal"], 2150, delta=2150 * 0.05)

    def test_detecta_un_dia_que_no_cuadra(self):
        plato = next(p for p in self.menu["platos"] if p["comida"] == "comida")
        plato["ingredientes"][0]["gramos"] *= 4
        errores, _ = self.revisar(self.menu)
        self.assertTrue(any("no cuadra" in e for e in errores))

    def test_detecta_valores_inventados(self):
        a = next(a for a in self.menu["alimentos"] if a["id_tabla"] == "pollo_pechuga")
        a["kcal"], a["proteina"] = 165, 31            # pollo cocinado haciéndose pasar por crudo
        errores, _ = self.revisar(self.menu)
        self.assertTrue(any("valores distintos de la tabla" in e for e in errores))

    def test_kcal_que_no_cuadran_con_los_macros(self):
        self.menu["alimentos"][0]["kcal"] *= 2
        errores, _ = self.revisar(self.menu)
        self.assertTrue(any("no cuadra con sus macros" in e for e in errores))

    def test_lo_que_no_come(self):
        self.menu["platos"][0]["nombre"] = "Tostada con crema de cacao"
        self.menu["alimentos"][0]["nombre"] = "Lentejas cocidas"
        errores = " ".join(self.revisar(self.menu)[0])
        self.assertIn("cacao", errores)
        self.assertIn("Lentejas", errores)

    def test_sin_coliflor_no_cuenta(self):
        self.menu["platos"][0]["nombre"] = "Salteado de verduras sin coliflor"
        self.assertFalse(any("que no come" in e for e in self.revisar(self.menu)[0]))
        self.menu["platos"][0]["nombre"] = "Salteado con coliflor"
        self.assertTrue(any("coliflor" in e for e in self.revisar(self.menu)[0]))

    def test_judias_verdes_si(self):
        self.assertFalse(any("que no come" in e for e in self.revisar(self.menu)[0]))
        self.assertTrue(any("Judías verdes" in a["nombre"] or "judías verdes" in a["nombre"].lower()
                            for a in self.menu["alimentos"]))

    def test_dia_de_entreno_mal_marcado(self):
        self.menu["dias"][1]["tipo"] = "entreno"      # miércoles
        self.assertTrue(any("miércoles es de descanso" in e for e in self.revisar(self.menu)[0]))

    def test_faltan_dias_o_platos(self):
        self.menu["dias"].pop()
        self.menu["dias"][0]["comidas"][0]["plato"] = "NO_EXISTE"
        errores = " ".join(self.revisar(self.menu)[0])
        self.assertIn("7 días", errores)
        self.assertIn("NO_EXISTE", errores)


class Montar(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        self.plan = calorias.Propuesta(date(2026, 10, 20), 1, 2150, 169, 67, 218, 102.5, 1889, 2739,
                                       None, None, "deficit")

    def tearDown(self):
        self.con.close()

    def test_corrige_con_los_errores_de_la_revision(self):
        malo = copy.deepcopy(EJEMPLO)
        malo["platos"][0]["ingredientes"][0]["gramos"] *= 6
        respuestas = iter(["productos", json.dumps(malo), json.dumps(EJEMPLO)])
        with mock.patch.object(m, "generar", side_effect=lambda *a, **k: next(respuestas)) as g:
            r = m.montar(self.con, date(2026, 10, 20), self.plan, log=lambda *_: None)
        self.assertEqual(r["origen"], "ia_corregido")
        self.assertIn("ERRORES", g.call_args_list[2].args[1])   # la 2.ª petición lleva los errores

    def test_respuesta_cortada_se_vuelve_a_pedir(self):
        respuestas = iter(["productos", '{"resumen": "cort', json.dumps(EJEMPLO)])
        with mock.patch.object(m, "generar", side_effect=lambda *a, **k: next(respuestas)) as g:
            r = m.montar(self.con, date(2026, 10, 20), self.plan, log=lambda *_: None)
        self.assertEqual(r["origen"], "ia_corregido")
        self.assertNotIn("PROPUESTA ANTERIOR", g.call_args_list[2].args[1])

    def test_si_falla_repite_el_anterior(self):
        m.guardar(self.con, date(2026, 10, 13), {"menu": EJEMPLO, "investigacion": "", "revision": {}, "origen": "ia"})
        with mock.patch.object(m, "generar", return_value="{}"):
            r = m.montar(self.con, date(2026, 10, 20), self.plan, log=lambda *_: None)
        self.assertEqual(r["origen"], "repetido")

    def test_si_falla_sin_anterior_avisa(self):
        with mock.patch.object(m, "generar", return_value="{}"):
            with self.assertRaises(m.MenuError):
                m.montar(self.con, date(2026, 10, 20), self.plan, log=lambda *_: None)

    def test_bot_hoy_y_compra(self):
        m.guardar(self.con, date(2026, 10, 20), {"menu": EJEMPLO, "investigacion": "", "revision": {}, "origen": "ia"})
        with mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value.date.return_value = date(2026, 10, 21)          # miércoles
            self.assertIn("Miércoles (descanso)", bot.cmd_hoy(self.con, ""))
            self.assertIn("Lista de la compra", bot.cmd_compra(self.con, ""))
            dt.now.return_value.date.return_value = date(2026, 10, 19)          # lunes antes
            self.assertIn("Aún no hay menú para esta semana", bot.cmd_hoy(self.con, ""))
            self.assertIn("Lista de la compra", bot.cmd_compra(self.con, ""))   # la de mañana


if __name__ == "__main__":
    unittest.main()

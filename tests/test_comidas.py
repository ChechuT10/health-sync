"""Comidas: menú del día, escritura nocturna, comidas de fuera, saltadas y resumen.  python3 -m unittest"""
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from healthsync import bot, calorias, comidas as c, menu
from healthsync.db import conectar

EJEMPLO = json.loads((Path(__file__).parent / "datos" / "menu_ejemplo.json").read_text())
MARTES = date(2026, 10, 20)
EST = {"descripcion": "Hamburguesa con patatas y una caña", "kcal": 1250, "kcal_min": 950, "kcal_max": 1600,
       "proteina": 45, "grasa": 60, "hidratos": 120, "confianza": "media", "nota": ""}


class Comidas(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        menu.guardar(self.con, MARTES, {"menu": EJEMPLO, "investigacion": "", "revision": {}, "origen": "ia"})
        self.subidas = []
        self.p = mock.patch.object(c, "subir", side_effect=self._subir)
        self.p.start()
        bot.pendiente, bot.estimacion = None, None

    def _subir(self, con, fid):
        self.subidas.append(fid)
        con.execute("UPDATE comidas SET gh_name = ? WHERE id = ?", (f"gh/{fid}", fid))
        return None

    def tearDown(self):
        self.p.stop()
        self.con.close()

    def test_menu_del_dia_cuadra_con_la_revision(self):
        dia = c.menu_del_dia(self.con, MARTES)
        self.assertEqual([x["comida"] for x in dia], ["desayuno", "comida", "merienda", "cena", "fijos"])
        total = sum(x["kcal"] for x in dia)
        self.assertAlmostEqual(total, 2150, delta=2150 * 0.05)      # lo mismo que revisó la Pi
        self.assertEqual(c.menu_del_dia(self.con, MARTES - timedelta(days=1)), [])   # semana sin menú

    def test_cuerpo_para_google_health(self):
        b = c.cuerpo_google(MARTES, "14:00", "comida", "Pollo con arroz", {"kcal": 650.4, "proteina": 52.33,
                                                                           "grasa": 15, "hidratos": 70})
        n = b["nutritionLog"]
        self.assertEqual(n["interval"]["startTime"], "2026-10-20T12:00:00Z")    # 14:00 en Madrid (verano)
        self.assertEqual(n["interval"]["startUtcOffset"], "7200s")
        self.assertEqual((n["mealType"], n["energy"]["kcal"]), ("LUNCH", 650))
        self.assertEqual(n["nutrients"][0], {"nutrient": "PROTEIN", "quantity": {"grams": 52.3}})

    def test_escribir_dia_no_duplica(self):
        c.escribir_dia(self.con, MARTES, log=lambda *_: None)
        self.assertEqual(len(self.subidas), 5)
        c.escribir_dia(self.con, MARTES, log=lambda *_: None)
        self.assertEqual(len(self.subidas), 5)

    def test_reintenta_lo_que_fallo_otro_dia(self):
        with mock.patch.object(c, "subir", return_value="503"):
            c.escribir_dia(self.con, MARTES, log=lambda *_: None)
        c.escribir_dia(self.con, MARTES + timedelta(days=1), log=lambda *_: None)
        pendientes = self.con.execute("SELECT count(*) FROM comidas WHERE gh_name IS NULL").fetchone()[0]
        self.assertEqual(pendientes, 0)

    def test_saltar_y_fuera_no_se_escriben_del_menu(self):
        self.assertIn("no se apunta", c.saltar(self.con, MARTES, "merienda"))
        c.guardar_fuera(self.con, MARTES, "cena", EST, "21:30")
        c.escribir_dia(self.con, MARTES, log=lambda *_: None)
        filas = dict(self.con.execute("SELECT comida, origen FROM comidas WHERE fecha = ?", (MARTES.isoformat(),)))
        self.assertEqual(filas, {"merienda": "saltada", "cena": "fuera", "desayuno": "plan",
                                 "comida": "plan", "fijos": "plan"})

    def test_fuera_despues_de_escrita_cuenta_como_extra(self):
        c.escribir_dia(self.con, MARTES, log=lambda *_: None)
        c.guardar_fuera(self.con, MARTES, "cena", EST, "23:50")
        self.assertEqual(self.con.execute("SELECT comida FROM comidas WHERE origen = 'fuera'").fetchone()[0], "extra")
        self.assertIn("ya está escrita", c.saltar(self.con, MARTES, "cena"))

    def test_bot_foto_y_botones(self):
        with mock.patch.object(c, "estimar", return_value=EST), mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 20, 21, 30)
            texto, botones = bot.atender_foto(self.con, b"jpg", "")
            self.assertIn("1.250 kcal", texto)
            self.assertIn("Cena", botones)
            self.assertIn("Elige una", bot.atender(self.con, "postre"))     # opción no válida: sigue preguntando
            r = bot.atender(self.con, "Cena")
        self.assertIn("en lugar de la cena", r)
        self.assertEqual(self.con.execute("SELECT kcal_min, kcal_max FROM comidas WHERE origen = 'fuera'").fetchone(),
                         (950, 1600))

    def test_bot_saltar_pregunta_con_botones(self):
        texto, botones = bot.atender(self.con, "/saltar")
        self.assertEqual(botones[0], "Desayuno")

    def test_resumen_y_gasto_real(self):
        for d in range(7):
            c.escribir_dia(self.con, MARTES + timedelta(days=d), log=lambda *_: None)
        c.guardar_fuera(self.con, MARTES + timedelta(days=8), "comida", EST, "14:30")
        r = c.resumen_mes(self.con, 2026, 10)
        self.assertIn("Comidas fuera: 1", r)
        self.assertIn("Días registrados: 8", r)
        self.assertEqual(len(c.kcal_por_dia(self.con, MARTES, MARTES + timedelta(days=30))), 8)


if __name__ == "__main__":
    unittest.main()

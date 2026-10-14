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
       "proteina": 45, "grasa": 60, "hidratos": 120, "confianza": "media", "nota": "",
       "componentes": [{"nombre": "Hamburguesa", "medida": "1", "gramos": 250, "kcal": 700, "proteina": 40,
                        "grasa": 40, "hidratos": 40}]}


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

    def test_fotos_se_guardan_y_se_borran_a_las_2_semanas(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(c, "FOTOS", Path(d) / "fotos"):
            c.guardar_foto(1, MARTES, b"jpg")
            c.guardar_foto(2, MARTES + timedelta(days=10), b"jpg")
            self.assertEqual(c.borrar_fotos_viejas(MARTES + timedelta(days=14)), 0)
            self.assertEqual(c.borrar_fotos_viejas(MARTES + timedelta(days=15)), 1)
            self.assertEqual([f.name for f in c.FOTOS.glob("*.jpg")], ["2026-10-30_2.jpg"])
            with mock.patch.object(c, "estimar", return_value=EST), mock.patch.object(bot, "datetime") as dt:
                dt.now.return_value = __import__("datetime").datetime(2026, 10, 30, 21, 30)
                bot.atender_foto(self.con, b"foto", "")
                bot.atender(self.con, "Extra")
            self.assertEqual(len(list(c.FOTOS.glob("2026-10-30_*.jpg"))), 2)

    def test_estimar_suma_componentes_y_rango(self):
        respuesta = {"descripcion": "Tostada", "incertidumbre_pct": 20, "confianza": "media", "nota": "",
                     "componentes": [{"nombre": "Pan", "medida": "media barra", "gramos": 100, "kcal": 260,
                                      "proteina": 8, "grasa": 1, "hidratos": 50},
                                     {"nombre": "Aceite", "medida": "1 cucharada", "gramos": 12, "kcal": 106,
                                      "proteina": 0, "grasa": 12, "hidratos": 0}]}
        with mock.patch("healthsync.gemini.generar", return_value=json.dumps(respuesta)) as g:
            e = c.estimar(foto=b"x", correccion="sin aceite", previa=respuesta)    # corregida: sin ajuste
            foto = c.estimar(foto=b"x")                                              # foto sin corregir: +15 %
            texto = c.estimar(descripcion="tostada con aceite")                      # descripción: sin ajuste
        self.assertEqual((e["kcal"], e["grasa"], e["kcal_min"], e["kcal_max"]), (366, 13, 293, 439))
        self.assertEqual((foto["kcal"], foto["ajuste"]), (420.9, 1.15))
        self.assertIn("+15 %", c.texto_estimacion(foto))
        self.assertEqual(texto["kcal"], 366)
        self.assertIn("CORRECCIÓN DE QUIEN LA COMIÓ (manda sobre lo que veas en la foto): sin aceite", g.call_args.args[1])

    def test_bot_corregir(self):
        nueva = dict(EST, kcal=1500, descripcion="Hamburguesa doble")
        with mock.patch.object(c, "estimar", side_effect=[EST, nueva]) as est, mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value = __import__("datetime").datetime(2026, 10, 20, 22, 30)
            bot.atender_foto(self.con, b"foto", "")
            self.assertIn("¿Qué corrijo?", bot.atender(self.con, "Corregir"))
            texto, botones = bot.atender(self.con, "era doble")
            self.assertEqual(est.call_args.kwargs["correccion"], "era doble")
            self.assertEqual(est.call_args.kwargs["foto"], b"foto")
            self.assertIn("Hamburguesa doble", texto)
            self.assertIn("en lugar de la cena", bot.atender(self.con, "Cena"))
        self.assertEqual(self.con.execute("SELECT kcal FROM comidas WHERE origen = 'fuera'").fetchone()[0], 1500)

    def test_bot_saltar_pregunta_con_botones(self):
        texto, botones = bot.atender(self.con, "/saltar")
        self.assertEqual(botones[0], "Desayuno")

    def test_resumen_y_gasto_real(self):
        for d in range(7):
            c.escribir_dia(self.con, MARTES + timedelta(days=d), log=lambda *_: None)
        c.guardar_fuera(self.con, MARTES + timedelta(days=8), "comida", EST, "14:30")
        r = c.resumen_mes(self.con, 2026, 10)
        self.assertIn("Comidas fuera: 1", r)
        self.assertIn("Días registrados: 7", r)               # solo días completos
        # El día suelto con solo una comida de fuera (sin cierre de las 23:30) no cuenta para el gasto real
        self.assertEqual(len(c.kcal_por_dia(self.con, MARTES, MARTES + timedelta(days=30))), 7)

    def test_hora_de_la_comida_de_fuera(self):
        noche = __import__("datetime").datetime(2026, 10, 20, 22, 40)
        self.assertEqual(c.hora_de("desayuno", noche), "08:00")
        self.assertEqual(c.hora_de("extra", noche), "22:40")
        self.assertEqual(c.hora_de("cena", noche.replace(hour=21)), "21:40")    # la cena (22:00) aún no ha llegado


if __name__ == "__main__":
    unittest.main()

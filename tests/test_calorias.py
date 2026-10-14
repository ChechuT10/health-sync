"""Pruebas de la calculadora con semanas simuladas (datos inventados).  python3 -m unittest"""
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from healthsync import calorias as c
from healthsync.db import conectar

PERSONA = {"sexo": "hombre", "altura_cm": 174, "fecha_nacimiento": "1999-03-22"}
LUNES = date(2026, 10, 19)


class Calorias(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        self.p = mock.patch.object(c, "persona", return_value=PERSONA)
        self.p.start()
        for i in range(28):                                     # ~8.000 pasos al día
            d = LUNES - timedelta(days=28 - i)
            self.con.execute("INSERT INTO dias (fecha, tipo_dia, pasos) VALUES (?, 'completo', 8000)",
                             (d.isoformat(),))

    def tearDown(self):
        self.p.stop()
        self.con.close()

    def pesar(self, d: date, kg: float, grasa=None):
        self.con.execute("INSERT INTO pesos VALUES (?, ?, '08:00', ?, ?, 'BOT')",
                         (f"p/{d}", d.isoformat(), kg, grasa))

    def semana(self, n: int) -> c.Propuesta:
        """Propuesta del lunes antes de la semana n (1 = primera) y la guarda."""
        pr = c.proponer(self.con, LUNES + timedelta(weeks=n - 1))
        c.guardar(self.con, pr)
        return pr

    def test_semana_de_martes_a_lunes(self):
        martes = LUNES + timedelta(days=1)
        for d in range(7):                                # de martes 13 a lunes 19: el plan del 20
            self.assertEqual(c.proximo_martes(LUNES - timedelta(days=d)), martes)
        self.assertEqual(c.martes_en_vigor(LUNES), martes - timedelta(days=7))   # el lunes es el último día
        self.assertEqual(c.martes_en_vigor(martes), martes)
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        pr = self.semana(1)
        self.assertEqual(pr.semana, martes)
        self.assertIn("martes 20/10 – lunes 26/10", c.texto(pr))
        self.assertEqual(c.plan_guardado(self.con, martes), pr)

    def test_sin_peso_no_hay_propuesta(self):
        self.assertIsNone(c.proponer(self.con, LUNES))

    def test_primera_semana_como_la_boveda(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        pr = self.semana(1)
        self.assertEqual(pr.numero, 1)
        self.assertEqual(pr.kcal, 2250)                  # nota 03: 2.250 kcal
        self.assertEqual((pr.proteina_g, pr.grasa_g), (165, 70))
        self.assertAlmostEqual(pr.proteina_g * 4 + pr.grasa_g * 9 + pr.hidratos_g * 4, 2250, delta=4)

    def test_semanas_1_y_2_no_se_ajusta(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        self.semana(1)
        for d, kg in ((5, 98.5), (12, 97.2)):           # bajada rápida de agua
            self.pesar(LUNES + timedelta(days=d), kg)
        self.assertEqual(self.semana(2).kcal, 2250)

    def test_agua_de_las_dos_primeras_semanas_no_cuenta(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        for d, kg in ((6, 98.0), (13, 97.0), (20, 96.7)):  # bajada de agua y luego normal
            self.pesar(LUNES + timedelta(days=d), kg)
        for n in (1, 2, 3):
            self.semana(n)
        pr = self.semana(4)
        self.assertIsNone(pr.ritmo_pct)                   # solo 1 pesada útil: no hay tendencia
        self.assertEqual(pr.kcal, 2250)

    def test_ritmo_rapido_sube_aunque_sea_pronto(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        for n in range(1, 5):
            self.semana(n)
        for d, kg in ((14, 98.0), (21, 96.8), (28, 95.6)):  # ~1,2 %/semana desde la semana 3
            self.pesar(LUNES + timedelta(days=d), kg)
        pr = self.semana(5)
        self.assertGreater(pr.ritmo_pct, 1.0)
        self.assertEqual(pr.kcal, 2400)

    def test_ritmo_lento_baja_solo_a_la_segunda(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        for n in (1, 2, 3):
            self.semana(n)
        for d in range(14, 49, 7):                        # estancado en 100
            self.pesar(LUNES + timedelta(days=d), 100 - (d % 2) * 0.1)
        self.semana(4)                                    # 1 pesada útil: aún sin tendencia
        self.assertEqual(self.semana(5).kcal, 2250)       # primer aviso
        self.assertEqual(self.semana(6).kcal, 2150)       # segunda vez: −100

    def test_nunca_por_debajo_del_suelo(self):
        self.pesar(LUNES - timedelta(days=1), 70)
        self.con.execute("DELETE FROM dias")              # sin pasos: factor bajo
        pr = self.semana(1)
        self.assertGreaterEqual(pr.kcal, c.SUELO_MIN_KCAL)
        self.assertGreaterEqual(pr.kcal, pr.metabolismo_basal)
        self.assertGreaterEqual(pr.proteina_g, c.PROTEINA_MIN)
        self.assertGreaterEqual(pr.grasa_g, c.GRASA_MIN)

    def test_descanso_de_dieta_y_vuelta(self):
        self.pesar(LUNES - timedelta(days=1), 100, grasa=33)
        for n in range(1, 13):
            self.pesar(LUNES + timedelta(weeks=n - 1, days=6), 100 - 0.5 * n)   # 0,5 %/semana
            self.semana(n)
        pr13 = self.semana(13)
        self.assertEqual(pr13.fase, "descanso")
        self.assertGreater(pr13.kcal, 2250)
        self.assertEqual(self.semana(14).fase, "descanso")
        pr15 = self.semana(15)
        self.assertEqual(pr15.fase, "deficit")
        self.assertEqual(pr15.kcal, 2250)                 # retoma el último déficit


if __name__ == "__main__":
    unittest.main()

"""Coherencia de la tabla de alimentos (detecta erratas).  python3 -m unittest"""
import unittest

from healthsync import alimentos as a


class Tabla(unittest.TestCase):
    def test_kcal_cuadran_con_los_macros(self):
        # Atwater: 4 kcal/g proteína e hidratos, 9 grasa, 2 fibra. Margen: 12 % o 15 kcal
        for al in a.tabla().values():
            calc = 4 * al.proteina + 9 * al.grasa + 4 * al.hidratos + 2 * al.fibra
            with self.subTest(al.id):
                self.assertLessEqual(abs(calc - al.kcal), max(15, 0.12 * al.kcal), f"calculado {calc:.0f}")

    def test_valores_validos(self):
        for al in a.tabla().values():
            with self.subTest(al.id):
                self.assertIn(al.grupo, a.GRUPOS)
                self.assertLessEqual(al.proteina + al.grasa + al.hidratos + al.fibra, 100)

    def test_excluir(self):
        self.assertNotIn("salmon", a.tabla(frozenset({"salmon"})))
        self.assertIn("salmon", a.tabla())

    def test_macros_por_gramos(self):
        m = a.tabla()["pollo_pechuga"].macros(200)
        self.assertEqual((m["kcal"], m["proteina"]), (220, 46))


if __name__ == "__main__":
    unittest.main()

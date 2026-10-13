"""Pruebas de la lectura de medidas y del % de grasa.  python3 -m unittest"""
import unittest

from healthsync.medidas import MedidaError, grasa_marina, leer_medidas, leer_peso


class Medidas(unittest.TestCase):
    def test_peso(self):
        self.assertEqual(leer_peso("99,4"), 99.4)
        self.assertEqual(leer_peso(" 100.2 kg"), 100.2)
        for malo in ("", "noventa", "994", "99,4 100"):
            with self.assertRaises(MedidaError):
                leer_peso(malo)

    def test_medidas(self):
        self.assertEqual(leer_medidas("cintura 102 cuello 41"), {"cintura": 102, "cuello": 41})
        self.assertEqual(leer_medidas("cuello 41,5 cintura 101 peso 99,4"),
                         {"cintura": 101, "cuello": 41.5, "peso": 99.4})
        self.assertEqual(leer_medidas("102 41"), {"cintura": 102, "cuello": 41})
        self.assertEqual(leer_medidas("101 41 peso 99,4"), {"cintura": 101, "cuello": 41, "peso": 99.4})
        with self.assertRaises(MedidaError):
            leer_medidas("cintura 102")
        with self.assertRaises(MedidaError):
            leer_medidas("cintura 102 cuello 410")

    def test_grasa_marina(self):
        # Hombre de 174 cm, cintura 102, cuello 41 -> ~26,8 % (cálculo a mano)
        self.assertAlmostEqual(grasa_marina(102, 41, 174), 26.8, delta=0.1)
        self.assertLess(grasa_marina(90, 41, 174), grasa_marina(102, 41, 174))
        with self.assertRaises(MedidaError):
            grasa_marina(40, 41, 174)


if __name__ == "__main__":
    unittest.main()

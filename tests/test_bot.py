"""Conversación del bot sin Telegram (solo la lógica de atender).  python3 -m unittest"""
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from healthsync import bot
from healthsync.db import conectar


class Conversacion(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        bot.pendiente = None
        self.peso = mock.patch.object(bot, "cmd_peso", side_effect=lambda con, a: f"peso {a}")
        self.peso.start()
        bot.COMANDOS["/peso"] = bot.cmd_peso

    def tearDown(self):
        self.peso.stop()
        bot.COMANDOS["/peso"] = bot.cmd_peso
        self.con.close()

    def test_pregunta_y_respuesta(self):
        self.assertIn("¿Cuánto pesas?", bot.atender(self.con, "/peso"))
        self.assertEqual(bot.atender(self.con, "99,4"), "peso 99,4")
        self.assertIsNone(bot.pendiente)
        self.assertIn("No te entiendo", bot.atender(self.con, "99,4"))   # ya no espera nada

    def test_otro_comando_cancela_la_pregunta(self):
        bot.atender(self.con, "/peso")
        bot.atender(self.con, "/ayuda")
        self.assertIsNone(bot.pendiente)

    def test_con_argumentos_no_pregunta(self):
        self.assertEqual(bot.atender(self.con, "/peso@jt_salud_pi_bot 100"), "peso 100")


class AvisoPesarse(unittest.TestCase):
    LUNES = date(2026, 10, 19)

    def test_sin_ninguna_pesada_avisa_cada_dia(self):
        for d in range(7):
            self.assertIn("ninguna pesada", bot.aviso_pesarse(self.LUNES + timedelta(days=d), None))

    def test_lunes_si_no_te_has_pesado_desde_el_viernes(self):
        self.assertIn("toca pesarse", bot.aviso_pesarse(self.LUNES, self.LUNES - timedelta(days=7)))
        self.assertIsNone(bot.aviso_pesarse(self.LUNES, self.LUNES - timedelta(days=2)))   # sábado

    def test_otros_dias_solo_si_llevas_mas_de_10(self):
        martes = self.LUNES + timedelta(days=1)
        self.assertIsNone(bot.aviso_pesarse(martes, martes - timedelta(days=8)))
        avisos = [d for d in range(9, 21) if bot.aviso_pesarse(martes, martes - timedelta(days=d))]
        self.assertEqual(avisos, [11, 14, 17, 20])


if __name__ == "__main__":
    unittest.main()

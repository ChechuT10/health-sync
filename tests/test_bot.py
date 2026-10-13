"""Conversación del bot sin Telegram (solo la lógica de atender).  python3 -m unittest"""
import unittest
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


if __name__ == "__main__":
    unittest.main()

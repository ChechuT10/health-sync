"""Análisis de las sesiones de gimnasio (Sheet + reloj), sin Google ni Gemini.  python3 -m unittest"""
import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from healthsync import analisis as a, bot, entreno as e
from healthsync.config import TZ
from healthsync.db import conectar

BASE = json.loads(e.RUTINA_BASE.read_text())
MARTES = date(2026, 10, 20)
JUEVES = MARTES + timedelta(days=2)                     # Torso 1


def hora(d: date, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{d.isoformat()}T{hhmm}").replace(tzinfo=TZ)


class Analisis(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        e.guardar_bloque(self.con, MARTES, BASE, "base")
        self.con.execute("INSERT INTO pesos_iniciales VALUES ('Press inclinado 30° con mancuernas, agarre neutro', 28, '')")
        self.p = e.plan(self.con, MARTES, MARTES - timedelta(days=1))
        e.guardar_semana(self.con, self.p, e.nombre_pestana(MARTES), "")
        self.t1 = next(d for d in self.p["dias"] if d["id"] == "T1")

    def tearDown(self):
        self.con.close()

    def apuntar(self, ejercicios=None, notas=""):
        for ej in self.t1["ejercicios"]:
            if ejercicios is not None and ej["id"] not in ejercicios:
                continue
            for n, (kg, reps, rir) in enumerate([(28, 10, 2), (28, 8, 2), (28, 6, 4)], 1):
                self.con.execute("INSERT INTO series (id, semana, dia, ejercicio, variante, serie, kg, reps, rir, notas, "
                                 "objetivo) VALUES (?, ?, 'T1', ?, ?, ?, ?, ?, ?, ?, '30 kg × 6-10 @RIR 2')",
                                 (f"{MARTES}|T1|{ej['id']}|{n}", MARTES.isoformat(), ej["id"], ej["variante"], n,
                                  kg, reps, rir, notas))

    def reloj(self, dia: date, ini="18:48", fin="19:53", tipo="STRENGTH_TRAINING"):
        self.con.execute("INSERT INTO entrenamientos (id, fecha, inicio, fin, tipo, duracion_min, kcal, fc_media, azm, "
                         "zona_ligera_min, zona_moderada_min, zona_intensa_min, zona_pico_min) "
                         "VALUES (?, ?, ?, ?, ?, 65.5, 444, 117, 28, 36, 29, 0, 0)",
                         (f"ses-{dia}-{ini}", dia.isoformat(), f"{dia}T{ini}", f"{dia}T{fin}", tipo))

    def test_completa_con_reloj(self):
        self.apuntar(notas="agarre de antebrazo, straps")
        self.assertEqual(a.pendientes(self.con, hora(JUEVES, "19:00")), [])      # el reloj aún no la ha cerrado
        self.reloj(JUEVES)
        self.reloj(JUEVES, "10:00", "10:40", "WALKING")                          # otro tipo: no cuenta
        lista = a.pendientes(self.con, hora(JUEVES, "20:20"))
        self.assertEqual([(d["id"], s["inicio"]) for _, d, s in lista], [("T1", "2026-10-22T18:48")])
        h = a.hechos(self.con, *lista[0], hora(JUEVES, "20:20"))
        self.assertIn("jueves 22/10", h)
        self.assertIn("las repeticiones caen 10→6", h)
        self.assertIn("Objetivo en su hoja: 30 kg × 6-10 @RIR 2", h)        # lo que ponía el Sheet, no el plan
        self.assertIn("peso distinto del objetivo (28 frente a 30 kg)", h)
        self.assertIn("más fácil de lo previsto", h)
        self.assertIn("Notas: agarre de antebrazo, straps", h)
        self.assertIn("Próxima vez (lo calcula el código", h)
        self.assertIn("66 min (estimado por la rutina", h)
        self.assertNotIn("kg de peso corporal", a.prompt(h))

    def test_a_medias_espera_12_h(self):
        self.apuntar(ejercicios={"press_inclinado", "remo_apoyado"})
        self.reloj(JUEVES)
        self.assertEqual(a.pendientes(self.con, hora(JUEVES, "20:20")), [])
        lista = a.pendientes(self.con, hora(JUEVES + timedelta(days=1), "08:00"))
        self.assertEqual(len(lista), 1)
        self.assertIn("NO APUNTADO", a.hechos(self.con, *lista[0], hora(JUEVES + timedelta(days=1), "08:00")))

    def test_cambiada_al_miercoles(self):
        self.apuntar()
        self.reloj(JUEVES - timedelta(days=1))
        (_, _, s), = a.pendientes(self.con, hora(JUEVES - timedelta(days=1), "20:20"))
        self.assertEqual(s["fecha"], "2026-10-21")
        self.assertIn("prevista el 22/10", a.hechos(self.con, self.p, self.t1, s, hora(JUEVES, "08:00")))

    def test_sin_reloj_al_dia_siguiente(self):
        self.apuntar()
        self.assertEqual(a.pendientes(self.con, hora(JUEVES, "23:00")), [])
        (_, _, s), = a.pendientes(self.con, hora(JUEVES + timedelta(days=1), "00:20"))
        self.assertIsNone(s)

    def test_una_sola_vez_y_bot(self):
        self.apuntar()
        self.reloj(JUEVES)
        self.assertIn("Aún no hay", bot.cmd_analisis(self.con, ""))
        with mock.patch("healthsync.gemini.generar", return_value="Veredicto: 🟢 Buena sesión.") as g, \
                mock.patch("healthsync.telegram.enviar") as tg:
            self.assertEqual(a.analizar_pendientes(self.con, hora(JUEVES, "20:20"), log=lambda *_: None), 1)
            self.assertEqual(a.analizar_pendientes(self.con, hora(JUEVES, "23:59"), log=lambda *_: None), 0)
        self.assertEqual(g.call_count, 1)
        self.assertTrue(tg.call_args[0][0].startswith("🏋️ Análisis · Torso 1 (empuje horizontal) · jue 22/10"))
        self.assertIn("Buena sesión", bot.cmd_analisis(self.con, ""))

    def test_si_gemini_falla_se_reintenta(self):
        from healthsync.gemini import GeminiError
        self.apuntar()
        self.reloj(JUEVES)
        with mock.patch("healthsync.gemini.generar", side_effect=GeminiError("503")), \
                mock.patch("healthsync.telegram.enviar") as tg:
            self.assertEqual(a.analizar_pendientes(self.con, hora(JUEVES, "20:20"), log=lambda *_: None), 0)
        tg.assert_not_called()
        self.assertEqual(len(a.pendientes(self.con, hora(JUEVES, "23:59"))), 1)


if __name__ == "__main__":
    unittest.main()

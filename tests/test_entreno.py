"""Gimnasio: progresión, reglas de seguridad, recuperación y Sheet (sin Google).  python3 -m unittest"""
import copy
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from healthsync import bot, entreno as e
from healthsync.db import conectar

BASE = json.loads(e.RUTINA_BASE.read_text())
EJ = {"id": "x", "variantes": ["Press"], "series": 3, "reps": [6, 10], "rir": 2, "incremento": 2.5}
LUNES = date(2026, 10, 19)
MARTES = LUNES + timedelta(days=1)


def ses(kg, reps, rir=2, notas=""):
    return [(kg, r, rir, notas) for r in reps]


class Reglas(unittest.TestCase):
    def test_la_rutina_base_cumple(self):
        self.assertEqual(e.validar_rutina(BASE), [])

    def test_rodilla_y_hombro(self):
        r = copy.deepcopy(BASE)
        r["dias"][0]["ejercicios"][1]["variantes"] = ["Sentadilla búlgara"]
        r["dias"][0]["ejercicios"][1]["rodilla_max"] = 120
        r["dias"][3]["ejercicios"][0]["variantes"] = ["Press tras nuca"]
        errores = " ".join(e.validar_rutina(r))
        self.assertIn("búlgara", errores)
        self.assertIn("120°", errores)
        self.assertIn("tras nuca", errores)

    def test_zancada_corta_si_larga_no(self):
        r = copy.deepcopy(BASE)
        self.assertEqual(e.validar_rutina(r), [])        # la base tiene «Zancada corta hacia atrás»
        r["dias"][2]["ejercicios"][2]["variantes"] = ["Zancada caminando"]
        self.assertTrue(any("zancada" in x.lower() for x in e.validar_rutina(r)))

    def test_maximo_de_press_por_encima_de_la_cabeza(self):
        r = copy.deepcopy(BASE)
        self.assertEqual(e.validar_rutina(r), [])                 # la base: 3 series (press de hombro)
        r["dias"][1]["ejercicios"][0]["sobre_cabeza"] = True       # + press inclinado: 6, aún vale
        self.assertEqual(e.validar_rutina(r), [])
        r["dias"][1]["ejercicios"][4]["sobre_cabeza"] = True       # + elevaciones: 9
        self.assertTrue(any("por encima de la cabeza" in x for x in e.validar_rutina(r)))

    def test_volumen_y_duracion(self):
        r = copy.deepcopy(BASE)
        for d in r["dias"]:
            for x in d["ejercicios"]:
                if x["musculos"][0] == "pecho":
                    x["series"] = 2                               # pecho: 4 series a la semana
        self.assertIn("Volumen de pecho", " ".join(e.validar_rutina(r)))
        r = copy.deepcopy(BASE)
        for x in r["dias"][1]["ejercicios"]:
            x["series"] = 5                                       # Torso 1 a 5 series: más de 60 min
        self.assertIn("T1: ~", " ".join(e.validar_rutina(r)))


class Progresion(unittest.TestCase):
    def obj(self, hist, inicial=(None, ""), rec="verde", descarga=False):
        return e.objetivo(EJ, hist, inicial, rec, descarga)

    def test_punto_de_partida_y_calibracion(self):
        self.assertEqual(self.obj([], (28, ""))["kg"], 28)
        o = self.obj([], (None, "Antes 140 kg"))
        self.assertIsNone(o["kg"])
        self.assertIn("Calibrar", o["texto"])
        self.assertEqual(o["nota"], "Antes 140 kg")

    def test_sube_al_llegar_al_maximo(self):
        o = self.obj([ses(28, [10, 10, 10], rir=1)])
        self.assertEqual(o["kg"], 30.5)
        self.assertIn("↑", o["nota"])

    def test_no_sube_si_falta_alguna_serie_o_rir_0(self):
        self.assertEqual(self.obj([ses(28, [10, 10, 9])])["kg"], 28)
        self.assertEqual(self.obj([ses(28, [10, 10, 10], rir=0)])["kg"], 28)
        self.assertIn("busca 10+", self.obj([ses(28, [9, 8, 8])])["reps"])

    def test_dolor_baja_20(self):
        o = self.obj([ses(50, [8, 8, 8], notas="dolor rodilla 4/10")])
        self.assertEqual(o["kg"], 40)

    def test_dos_caidas_seguidas_bajan_10(self):
        o = self.obj([ses(50, [10, 10, 10]), ses(50, [8, 8, 8]), ses(50, [6, 6, 6])])
        self.assertEqual(o["kg"], 45)

    def test_recuperacion_y_descarga(self):
        h = [ses(28, [10, 10, 10])]
        self.assertEqual(self.obj(h, rec="ambar")["kg"], 28)
        rojo = self.obj([ses(28, [8, 8, 8])], rec="rojo")
        self.assertEqual((rojo["series"], rojo["rir"]), (2, 3))
        d = self.obj(h, descarga=True)
        self.assertEqual((d["kg"], d["series"], d["rir"]), (28, 2, 3))

    def test_estancado(self):
        self.assertTrue(e.estancado([ses(50, [8, 8, 8])] * 4))
        self.assertFalse(e.estancado([ses(50, [8, 8, 8])] * 3 + [ses(52.5, [6, 6, 6])]))


class ConBase(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))
        e.guardar_bloque(self.con, MARTES, BASE, "base")
        self.con.execute("INSERT INTO pesos_iniciales VALUES ('Press inclinado 30° con mancuernas, agarre neutro', 28, '')")

    def tearDown(self):
        self.con.close()

    def test_rehacer_base_solo_sin_series(self):
        self.con.execute("INSERT INTO estado VALUES ('sheet_gimnasio', 'falso')")   # sin Google en las pruebas
        with mock.patch.object(e, "TOKEN_DRIVE_FILE", Path("/no/existe")):
            e.iniciar(self.con, None, LUNES, log=lambda *_: None, rehacer=True)
            self.assertEqual(e.bloque_en_vigor(self.con, MARTES)[2], "base")
            self.con.execute("INSERT INTO series (id, semana, dia, ejercicio, variante, serie, kg, reps) "
                             "VALUES ('x', ?, 'P1', 'rdl', 'Peso muerto rumano con barra', 1, 60, 8)", (MARTES.isoformat(),))
            with self.assertRaises(e.EntrenoError):
                e.iniciar(self.con, None, LUNES, log=lambda *_: None, rehacer=True)

    def test_duracion_de_la_base(self):
        for d in BASE["dias"]:
            self.assertLessEqual(e.minutos(d), e.MAX_MINUTOS, d["id"])

    def test_recuperacion(self):
        self.assertEqual(e.recuperacion(self.con, LUNES)[0], "sin_datos")
        for i in range(67):
            d = LUNES - timedelta(days=i)
            vfc = 25 if i < 7 else 40 + (i % 5)
            self.con.execute("INSERT INTO dias (fecha, tipo_dia, vfc_ms, sueno_min) VALUES (?, 'completo', ?, ?)",
                             (d.isoformat(), vfc, 420))
        self.assertEqual(e.recuperacion(self.con, LUNES)[0], "ambar")    # VFC baja, sueño bien

    def test_plan_y_sheet_ida_y_vuelta(self):
        p = e.plan(self.con, MARTES, LUNES)
        self.assertEqual([d["dia"] for d in p["dias"]], ["martes", "jueves", "viernes", "lunes"])
        filas, desplegables, cabeceras = e.filas_pestana(p)
        self.assertEqual(len(cabeceras), 4)
        press = next(i for i, f in enumerate(filas) if f[1].startswith("Press inclinado"))
        self.assertIn("28 kg", filas[press][3])
        # Rellenas dos series y cambias la variante
        filas[press][1] = "Press convergente en máquina"
        filas[press][4:8] = [40, 9, 2, ""]
        filas[press + 1][4:8] = ["42,5", 8, "", "molestia hombro"]
        hechas, elegidas = e.leer_filas(filas)
        self.assertEqual(len(hechas), 2)
        self.assertEqual({h["variante"] for h in hechas}, {"Press convergente en máquina"})
        self.assertEqual(hechas[1]["kg"], 42.5)
        e.guardar_series(self.con, hechas, p["semana"])
        sig = e.plan(self.con, MARTES + timedelta(days=7), LUNES + timedelta(days=7))
        t1 = next(d for d in sig["dias"] if d["id"] == "T1")
        o = t1["ejercicios"][0]["objetivos"]["Press convergente en máquina"]
        self.assertIn("−20 %", o["nota"])                                   # «molestia» cuenta como dolor

    def test_renovar_bloque_con_gemini(self):
        nueva = copy.deepcopy(BASE) | {"motivo": "Cambio el rumano por buenos días"}
        mala = copy.deepcopy(nueva)
        mala["dias"][0]["ejercicios"][1]["variantes"] = ["Sentadilla búlgara"]
        respuestas = iter([json.dumps(mala), json.dumps(nueva), "- Consejo"])
        hoy = LUNES + timedelta(weeks=12)
        with mock.patch("healthsync.gemini.generar", side_effect=lambda *a, **k: next(respuestas)):
            p, consejos, cambios = e.preparar_semana(self.con, hoy, log=lambda *_: None)
        self.assertEqual(cambios, "Cambio el rumano por buenos días")
        self.assertEqual(e.bloque_en_vigor(self.con, date.fromisoformat(p["semana"]))[2], "ia")
        self.assertEqual(p["semana_bloque"], 1)

    def test_si_gemini_falla_se_mantiene(self):
        hoy = LUNES + timedelta(weeks=12)
        with mock.patch("healthsync.gemini.generar", return_value="{}"):
            p, _, cambios = e.preparar_semana(self.con, hoy, log=lambda *_: None)
        self.assertIn("se mantiene", cambios)
        self.assertEqual(p["semana_bloque"], 13)

    def test_bot_entreno(self):
        p = e.plan(self.con, MARTES, LUNES)
        e.guardar_semana(self.con, p, "Semana 20-10", "")
        with mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value.date.return_value = MARTES + timedelta(days=1)    # miércoles → jueves
            r = bot.cmd_entreno(self.con, "")
        self.assertIn("Jueves 22/10", r)
        self.assertIn("Press inclinado", r)


if __name__ == "__main__":
    unittest.main()

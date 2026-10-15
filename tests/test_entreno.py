"""Gimnasio: progresión, reglas de seguridad, recuperación y Sheet (sin Google).  python3 -m unittest"""
import copy
import json
import unittest
from datetime import date, datetime, timedelta
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

    def test_adelantar_la_rutina_a_la_semana_en_curso(self):
        self.con.execute("INSERT INTO estado VALUES ('sheet_gimnasio', 'falso')")
        miercoles = MARTES - timedelta(days=6)                            # 14/10
        with mock.patch.object(e, "TOKEN_DRIVE_FILE", Path("/no/existe")):
            inicio = e.iniciar(self.con, None, miercoles, log=lambda *_: None, rehacer=True,
                               desde=MARTES - timedelta(days=7))
        self.assertEqual(inicio, date(2026, 10, 13))
        self.assertEqual(e.plan(self.con, MARTES, LUNES)["semana_bloque"], 2)   # la del 20/10 ya es la 2.ª
        with self.assertRaises(e.EntrenoError):
            e.iniciar(self.con, None, miercoles, log=lambda *_: None, desde=miercoles)   # no es martes

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
        self.assertIn("28 kg", hechas[0]["objetivo"])                      # el objetivo que ponía el Sheet
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

    def test_pestanas_terminadas(self):
        for n in range(3):
            semana = MARTES + timedelta(weeks=n)
            e.guardar_semana(self.con, e.plan(self.con, semana, LUNES), e.nombre_pestana(semana), "")
        lunes_3 = LUNES + timedelta(weeks=2)              # 2 de nov.: en vigor la del 27/10, ya creada la del 3/11
        self.assertEqual(e.pestanas_terminadas(self.con, lunes_3), {"Semana 20-10"})
        self.assertEqual(e.pestanas_terminadas(self.con, lunes_3 + timedelta(days=1)), {"Semana 20-10", "Semana 27-10"})

    def test_bot_entreno(self):
        p = e.plan(self.con, MARTES, LUNES)
        e.guardar_semana(self.con, p, "Semana 20-10", "")
        with mock.patch.object(bot, "datetime") as dt:
            dt.now.return_value.date.return_value = MARTES + timedelta(days=1)    # miércoles → jueves
            r = bot.cmd_entreno(self.con, "")
        self.assertIn("Jueves 22/10", r)
        self.assertIn("Press inclinado", r)

    def test_objetivos_congelados_toda_la_semana(self):
        e.guardar_semana(self.con, e.plan(self.con, MARTES, LUNES), "Semana 20-10", "")     # sin datos: 3 series
        for i in range(67):                                  # a mitad de semana la recuperación pasa a rojo
            d = MARTES + timedelta(days=2) - timedelta(days=i)
            self.con.execute("INSERT INTO dias (fecha, tipo_dia, vfc_ms, sueno_min) VALUES (?, 'completo', ?, ?)",
                             (d.isoformat(), 20 if i < 7 else 40 + (i % 5), 300 if i < 7 else 450))
        jueves = MARTES + timedelta(days=2)
        self.assertEqual(e.recuperacion(self.con, jueves)[0], "rojo")
        p = e.plan(self.con, MARTES, jueves)                 # lo que hace la lectura del Sheet cada 4 h
        self.assertEqual(p["recuperacion"], "sin_datos")
        press = p["dias"][1]["ejercicios"][0]["objetivos"]["Press inclinado 30° con mancuernas, agarre neutro"]
        self.assertEqual(press["series"], 3)

    def test_cambios_de_estructura(self):
        nueva = copy.deepcopy(BASE)
        t1 = next(d for d in nueva["dias"] if d["id"] == "T1")
        t1["ejercicios"][-1]["series"] = 2                    # 1
        t1["ejercicios"].pop(0)                               # 2
        for d in nueva["dias"]:
            for x in d["ejercicios"]:
                x["descanso_s"] = 90                          # el descanso no cuenta
        self.assertEqual(len(e.cambios_estructura(BASE, nueva)), 2)
        a, b = t1["ejercicios"][-2:]
        a["superserie"], b["superserie"] = b["id"], a["id"]   # una superserie nueva: 1 cambio más
        self.assertEqual(len(e.cambios_estructura(BASE, nueva)), 3)
        self.assertEqual([e._min(s) for s in (90, 120, 75, 45)], ["1,5 min", "2 min", "75 s", "45 s"])


class AjusteSemanal(unittest.TestCase):
    """El lunes, Gemini ajusta la semana que empieza con las sesiones de la anterior; solo vale esa semana."""

    def setUp(self):
        self.con = conectar(Path(":memory:"))
        e.guardar_bloque(self.con, MARTES, BASE, "base")
        e.guardar_semana(self.con, e.plan(self.con, MARTES, LUNES), "Semana 20-10", "")
        p = e.plan_guardado(self.con, MARTES)
        t1 = next(d for d in p["dias"] if d["id"] == "T1")
        for ej in t1["ejercicios"]:
            for n in (1, 2, 3):
                self.con.execute("INSERT INTO series (id, semana, dia, ejercicio, variante, serie, kg, reps, rir) "
                                 "VALUES (?, ?, 'T1', ?, ?, ?, 30, ?, 2)",
                                 (f"{MARTES}|T1|{ej['id']}|{n}", MARTES.isoformat(), ej["id"], ej["variante"], n, 11 - n))
        self.con.execute("INSERT INTO entrenamientos (id, fecha, inicio, fin, tipo, duracion_min, fc_media) "
                         "VALUES ('s1', '2026-10-22', '2026-10-22T18:30', '2026-10-22T19:45', 'STRENGTH_TRAINING', 75, 115)")
        self.lunes = LUNES + timedelta(days=7)                # 26/10: prepara la semana del 27/10
        self.ahora = datetime(2026, 10, 26, 10, 0, tzinfo=e.TZ)

    def tearDown(self):
        self.con.close()

    def respuesta(self, quitar_series: int = 1) -> str:
        r = copy.deepcopy(BASE)
        t1 = next(d for d in r["dias"] if d["id"] == "T1")
        for x in t1["ejercicios"][-quitar_series:]:
            x["series"] = 2
        for d in r["dias"]:
            for x in d["ejercicios"]:
                x["descanso_s"] = 90
        return json.dumps(r | {"bien": ["Constancia"], "mejorar": ["75 min: descansos <largos>"],
                               "consejos": ["Cronómetro"],
                               "cambios": [{"dia": "T1", "que": "Curl a 2 series", "por_que": "Tiempo"}]})

    def test_ajuste_solo_para_esa_semana(self):
        with mock.patch("healthsync.gemini.generar", return_value=self.respuesta()) as g:
            p, cons, _ = e.preparar_semana(self.con, self.lunes, log=lambda *_: None, ahora=self.ahora)
        prompt = g.call_args[0][1]
        self.assertIn("75 min", prompt)                       # la sesión del jueves, con el reloj
        self.assertEqual(cons, "- Cronómetro")
        t1 = next(d for d in p["dias"] if d["id"] == "T1")
        curl = t1["ejercicios"][-1]
        self.assertEqual(curl["objetivos"][curl["variante"]]["series"], 2)
        self.assertIn("descanso 1,5 min", curl["objetivos"][curl["variante"]]["texto"])
        e.guardar_semana(self.con, p, "Semana 27-10", cons)
        # La lectura del Sheet conserva el ajuste; la semana siguiente vuelve a partir del bloque
        self.assertEqual(e.plan(self.con, MARTES + timedelta(days=7), self.lunes)["ajuste"]["cambios"][0]["dia"], "T1")
        self.assertEqual(e.rutina_semana(self.con, MARTES + timedelta(days=14)), BASE)
        self.assertIn("Curl a 2 series", e.historial_ajustes(self.con, MARTES + timedelta(days=14)))
        msg = e.mensaje_semana(p, cons, "", "https://docs.google.com/x")
        self.assertIn('<a href="https://docs.google.com/x">Abrir el Sheet</a>', msg)
        self.assertIn("descansos &lt;largos&gt;", msg)
        self.assertNotIn("Sheet: https", msg)

    def test_demasiados_cambios_no_se_aplican(self):
        respuestas = iter([self.respuesta(4)] * 3 + ["- Consejo"])
        with mock.patch("healthsync.gemini.generar", side_effect=lambda *a, **k: next(respuestas)):
            p, cons, _ = e.preparar_semana(self.con, self.lunes, log=lambda *_: None, ahora=self.ahora)
        self.assertNotIn("ajuste", p)
        self.assertEqual(cons, "- Consejo")                   # consejos de siempre
        self.assertEqual(e.cambios_estructura(BASE, BASE), [])

    def test_sin_sesiones_no_hay_ajuste(self):
        self.con.execute("DELETE FROM series")
        with mock.patch("healthsync.gemini.generar", return_value="- Consejo") as g:
            p, _, _ = e.preparar_semana(self.con, self.lunes, log=lambda *_: None, ahora=self.ahora)
        self.assertEqual(g.call_count, 1)
        self.assertNotIn("ajuste", p)


if __name__ == "__main__":
    unittest.main()

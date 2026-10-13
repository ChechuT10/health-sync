"""Pruebas de la normalización con registros inventados (mismo formato que la API).

Ejecutar desde la raíz del repositorio:  python3 -m unittest
"""
import unittest
from pathlib import Path

from healthsync.db import conectar
from healthsync.normalizar import ROLLUP, guardar_raw, reconstruir


def dia(y, m, d):
    return {"year": y, "month": m, "day": d}


def rollup(fecha, campo, valor):
    return {"civilStartTime": {"date": fecha, "time": {}},
            "civilEndTime": {"date": fecha, "time": {}}, campo: valor}


def noche(nombre, inicio, fin, main=True, nap=None, dormido="420"):
    md = {"mainSleep": main}
    if nap is not None:
        md["nap"] = nap
    return {"name": nombre, "sleep": {
        "interval": {"startTime": inicio, "startUtcOffset": "7200s", "endTime": fin, "endUtcOffset": "7200s"},
        "type": "STAGES", "metadata": md,
        "summary": {"minutesAsleep": dormido, "minutesAwake": "10", "stagesSummary": [
            {"type": "LIGHT", "minutes": "250"}, {"type": "DEEP", "minutes": "70"}, {"type": "REM", "minutes": "100"}]}}}


class Normalizar(unittest.TestCase):
    def setUp(self):
        self.con = conectar(Path(":memory:"))

    def tearDown(self):
        self.con.close()

    def dias(self):
        reconstruir(self.con)
        cols = [c[1] for c in self.con.execute("PRAGMA table_info(dias)")]
        return {r[0]: dict(zip(cols, r)) for r in self.con.execute("SELECT * FROM dias")}

    def test_sueno_cuenta_el_dia_de_despertar_y_sin_siestas(self):
        guardar_raw(self.con, "sleep", [
            noche("a", "2026-10-01T21:30:00Z", "2026-10-02T05:30:00Z"),            # 23:30 → 07:30
            noche("b", "2026-10-02T13:00:00Z", "2026-10-02T14:30:00Z", nap=True),   # siesta marcada como main
            noche("c", "2026-10-02T15:00:00Z", "2026-10-02T16:00:00Z", main=None),  # siesta sin marcas
        ])
        d = self.dias()
        self.assertEqual(list(d), ["2026-10-02"])
        self.assertEqual(d["2026-10-02"]["sueno_min"], 420)
        siestas = self.con.execute("SELECT count(*) FROM sueno WHERE siesta = 1").fetchone()[0]
        self.assertEqual(siestas, 2)

    def test_cero_y_ausente_son_null(self):
        guardar_raw(self.con, "steps" + ROLLUP, [rollup(dia(2026, 10, 3), "steps", {"countSum": "5000"})])
        guardar_raw(self.con, "daily-respiratory-rate", [
            {"dailyRespiratoryRate": {"date": dia(2026, 10, 3), "breathsPerMinute": 0}}])
        d = self.dias()["2026-10-03"]
        self.assertEqual(d["pasos"], 5000)
        self.assertIsNone(d["resp_rpm"])
        self.assertIsNone(d["fc_reposo"])
        self.assertEqual(d["tipo_dia"], "solo_movil")

    def test_upsert_no_duplica(self):
        p = rollup(dia(2026, 10, 4), "steps", {"countSum": "100"})
        self.assertEqual(guardar_raw(self.con, "steps" + ROLLUP, [p]), 1)
        self.assertEqual(guardar_raw(self.con, "steps" + ROLLUP, [p]), 0)        # igual: no cambia
        p2 = rollup(dia(2026, 10, 4), "steps", {"countSum": "8000"})            # llegó tarde: se corrige
        self.assertEqual(guardar_raw(self.con, "steps" + ROLLUP, [p2]), 1)
        self.assertEqual(self.dias()["2026-10-04"]["pasos"], 8000)

    def test_tipo_dia_por_cobertura(self):
        zonas = lambda h: {"timeInHeartRateZones": [{"heartRateZone": "LIGHT", "duration": f"{h * 3600}s"}]}  # noqa: E731
        guardar_raw(self.con, "time-in-heart-rate-zone" + ROLLUP, [
            rollup(dia(2026, 10, 2), "timeInHeartRateZone", zonas(20)),
            rollup(dia(2026, 10, 3), "timeInHeartRateZone", zonas(12)),
        ])
        guardar_raw(self.con, "sleep", [noche("a", "2026-10-01T21:30:00Z", "2026-10-02T05:30:00Z")])
        d = self.dias()
        self.assertEqual(d["2026-10-02"]["tipo_dia"], "completo")
        self.assertEqual(d["2026-10-03"]["tipo_dia"], "sin_noche")

    def test_pesos_sin_ceros_y_con_grasa(self):
        peso = lambda g, s: {"name": f"w/{s}", "dataSource": {"recordingMethod": "MANUAL"}, "weight": {  # noqa: E731
            "sampleTime": {"physicalTime": f"2026-10-05T07:{s}:00Z",
                           "civilTime": {"date": dia(2026, 10, 5), "time": {"hours": 9, "minutes": int(s)}}},
            "weightGrams": g}}
        grasa = {"name": "g/10", "bodyFat": {"percentage": 31.5, "sampleTime": {  # misma medición
            "physicalTime": "2026-10-05T07:10:00Z",
            "civilTime": {"date": dia(2026, 10, 5), "time": {"hours": 9, "minutes": 10}}}}}
        guardar_raw(self.con, "weight", [peso(99500, "10"), peso(0, "11")])
        guardar_raw(self.con, "body-fat", [grasa])
        reconstruir(self.con)
        self.assertEqual(self.con.execute("SELECT peso_kg, grasa_pct, hora FROM pesos").fetchall(),
                         [(99.5, 31.5, "09:10")])

    def test_antes_de_inicio_y_descartes_no_cuentan(self):
        guardar_raw(self.con, "steps" + ROLLUP, [
            rollup(dia(2025, 7, 1), "steps", {"countSum": "9000"}),     # antes de FECHA_INICIO
            rollup(dia(2026, 10, 6), "steps", {"countSum": "7000"}),
            rollup(dia(2026, 10, 7), "steps", {"countSum": "3"}),       # dato malo
        ])
        self.con.execute("INSERT INTO descartes (tipo, clave) VALUES (?, ?)", ("steps" + ROLLUP, "2026-10-07"))
        self.assertEqual(list(self.dias()), ["2026-10-06"])


if __name__ == "__main__":
    unittest.main()

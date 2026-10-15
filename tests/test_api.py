"""Pruebas de los reintentos de api.request.  python3 -m unittest

Necesitan requests y google-auth (el venv de la Pi); sin ellos se saltan.
"""
import unittest
from unittest import mock

try:
    import requests

    from healthsync import api
except ImportError:
    requests = None


class Respuesta:
    def __init__(self, status=200, datos=b'{"ok": true}'):
        self.status_code, self.content = status, datos
        self.ok = status < 400
        self.text = datos.decode()

    def json(self):
        return {"ok": True}


class Sesion:
    """Devuelve (o lanza) lo que haya en la lista, en orden."""
    def __init__(self, *salidas):
        self.salidas, self.llamadas = list(salidas), 0

    def request(self, *a, **kw):
        self.llamadas += 1
        s = self.salidas.pop(0)
        if isinstance(s, Exception):
            raise s
        return s


@unittest.skipIf(requests is None, "sin requests ni google-auth (venv de la Pi)")
@mock.patch("healthsync.api.time.sleep")
class Reintentos(unittest.TestCase):
    def test_get_reintenta_timeout(self, _):
        s = Sesion(requests.ReadTimeout("lento"), requests.ConnectionError("corte"), Respuesta())
        self.assertEqual(api.request(s, "GET", "u"), {"ok": True})
        self.assertEqual(s.llamadas, 3)

    def test_get_se_rinde(self, _):
        s = Sesion(*[requests.ReadTimeout("lento")] * 3)
        with self.assertRaises(requests.ReadTimeout):
            api.request(s, "GET", "u")
        self.assertEqual(s.llamadas, 3)

    def test_post_no_reintenta_si_llego(self, _):
        s = Sesion(requests.ReadTimeout("lento"), Respuesta())
        with self.assertRaises(requests.ReadTimeout):
            api.request(s, "POST", "u")
        self.assertEqual(s.llamadas, 1)

    def test_post_reintenta_si_no_conecto(self, _):
        s = Sesion(requests.ConnectTimeout("sin red"), Respuesta())
        self.assertEqual(api.request(s, "POST", "u"), {"ok": True})

    def test_5xx_sigue_reintentando(self, _):
        s = Sesion(Respuesta(503, b"x"), Respuesta())
        self.assertEqual(api.request(s, "GET", "u"), {"ok": True})


if __name__ == "__main__":
    unittest.main()

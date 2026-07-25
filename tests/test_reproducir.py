"""Tests del arranque de reproduccion en la TV: que no spamee avisos.

El caso que motiva estos tests: mientras la TV reproducia bien, no contestaba
el control, y el codigo le mandaba Play a ciegas una y otra vez, llenando el
panel de "(aviso: intento de Play fallo...)".
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import caster


class ErrorSOAPFalso(Exception):
    """Imita a upnpclient.soap.SOAPError, que trae (codigo, descripcion)."""

    def __init__(self, codigo, descripcion):
        super().__init__(codigo, descripcion)


class AVFalso:
    """AVTransport simulado, con una secuencia guionada de estados."""

    def __init__(self, estados, play_falla_con=None):
        self.estados = list(estados)
        self.play_falla_con = play_falla_con
        self.plays = 0
        self.uris = 0

    def SetAVTransportURI(self, **kw):
        self.uris += 1

    def GetTransportInfo(self, **kw):
        estado = self.estados.pop(0) if self.estados else "STOPPED"
        if isinstance(estado, Exception):
            raise estado
        return {"CurrentTransportState": estado}

    def Play(self, **kw):
        self.plays += 1
        if self.play_falla_con is not None:
            raise self.play_falla_con


class TVFalsa:
    def __init__(self, av):
        self.avtransport = av
        self.nombre = "[LG] webOS TV"
        self.host = "192.168.0.3"


def reproducir(av, **kw):
    """Corre caster.reproducir sin esperas, juntando los mensajes logueados."""
    mensajes = []
    tv = TVFalsa(av)
    with mock.patch.object(caster.time, "sleep", lambda *_: None):
        ok = caster.reproducir(tv, "http://x/stream.ts", "titulo", log=mensajes.append, **kw)
    return ok, mensajes


class TestReproducir(unittest.TestCase):
    def test_si_ya_esta_reproduciendo_no_manda_play_ni_avisa(self):
        av = AVFalso(["PLAYING"])
        ok, mensajes = reproducir(av)
        self.assertTrue(ok)
        self.assertEqual(av.plays, 0)
        self.assertEqual(mensajes, [])

    def test_tv_muda_no_dispara_avisos_repetidos(self):
        # El caso reportado: la TV no contesta el control porque esta ocupada
        # reproduciendo. Antes esto generaba un aviso por vuelta.
        timeout = TimeoutError("read timed out")
        av = AVFalso([timeout, timeout, timeout, timeout, timeout])
        ok, mensajes = reproducir(av)
        self.assertEqual(len(mensajes), 1, f"deberia avisar una sola vez: {mensajes}")
        self.assertEqual(av.plays, 0, "no hay que mandar Play a ciegas si la TV no contesta")
        self.assertFalse(ok)

    def test_tv_muda_que_despues_confirma_termina_bien(self):
        av = AVFalso([TimeoutError("timeout"), "PLAYING"])
        ok, mensajes = reproducir(av)
        self.assertTrue(ok)
        self.assertEqual(av.plays, 0)
        self.assertEqual(len(mensajes), 1)

    def test_error_701_significa_que_ya_reproduce(self):
        # "Transition not available": la TV rechaza el Play porque ya arranco.
        av = AVFalso(["STOPPED"], play_falla_con=ErrorSOAPFalso(701, "Transition not available"))
        ok, mensajes = reproducir(av)
        self.assertTrue(ok, "el error 701 es un exito disfrazado")
        self.assertEqual(mensajes, [], f"no deberia avisar nada: {mensajes}")

    def test_si_esta_detenida_manda_play_una_sola_vez(self):
        av = AVFalso(["STOPPED", "STOPPED", "STOPPED"], play_falla_con=ErrorSOAPFalso(501, "Action Failed"))
        ok, mensajes = reproducir(av)
        self.assertEqual(av.plays, 1, "el Play no debe reintentarse en cadena")
        self.assertEqual(len(mensajes), 1)
        self.assertFalse(ok)

    def test_play_exitoso_desde_detenida(self):
        av = AVFalso(["STOPPED"])
        ok, mensajes = reproducir(av)
        self.assertTrue(ok)
        self.assertEqual(av.plays, 1)
        self.assertEqual(mensajes, [])

    def test_siempre_manda_la_uri(self):
        av = AVFalso(["PLAYING"])
        reproducir(av)
        self.assertEqual(av.uris, 1)

    def test_transitioning_cuenta_como_arrancado(self):
        av = AVFalso(["TRANSITIONING"])
        ok, _ = reproducir(av)
        self.assertTrue(ok)


class TestCodigoUPnP(unittest.TestCase):
    def test_extrae_el_codigo_entero(self):
        self.assertEqual(caster.codigo_upnp(ErrorSOAPFalso(701, "Transition not available")), 701)

    def test_acepta_codigo_como_texto(self):
        self.assertEqual(caster.codigo_upnp(Exception("701", "algo")), 701)

    def test_sin_codigo_devuelve_none(self):
        self.assertIsNone(caster.codigo_upnp(TimeoutError("read timed out")))
        self.assertIsNone(caster.codigo_upnp(Exception()))


class TestComandoStreamlink(unittest.TestCase):
    def test_fuerza_mpegts_para_streams_que_hay_que_muxear(self):
        # Sin esto, YouTube en calidades altas sale en matroska y la TV no lo
        # reproduce, porque se lo anunciamos como video/mp2t.
        cmd = caster.comando_streamlink("https://youtube.com/watch?v=x")
        self.assertIn("--ffmpeg-fout", cmd)
        self.assertEqual(cmd[cmd.index("--ffmpeg-fout") + 1], "mpegts")

    def test_low_latency_solo_en_twitch(self):
        self.assertIn("--twitch-low-latency", caster.comando_streamlink("https://twitch.tv/x", low_latency=True))
        self.assertNotIn("--twitch-low-latency", caster.comando_streamlink("https://kick.com/x", low_latency=True))

    def test_la_url_y_la_calidad_van_al_final(self):
        cmd = caster.comando_streamlink("https://kick.com/x", "720p")
        self.assertEqual(cmd[-2:], ["https://kick.com/x", "720p"])


if __name__ == "__main__":
    unittest.main()

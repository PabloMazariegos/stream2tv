"""Tests de favoritos en vivo: consultas simuladas a cada plataforma, sin red."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import config, discovery, envivo
from stream2tv.controlador import Controlador
from test_stream2tv import tv_falsa


class Respuesta:
    def __init__(self, json=None, texto="", codigo=200):
        self._json = json
        self.text = texto
        self.status_code = codigo

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


TWITCH_OK = {"data": {"users": [
    {"login": "auronplay", "stream": {"viewersCount": 235411, "title": "AMONG US", "game": {"name": "Among Us"}}},
    {"login": "xqc", "stream": None},
    None,  # canal que no existe
]}}


class SesionKick:
    """Imita la sesion de requests con el adaptador de Kick."""

    def __init__(self, respuestas):
        self.respuestas = respuestas
        self.pedidas = []

    def get(self, url, **kw):
        self.pedidas.append(url)
        slug = url.split("/channels/")[1].split("/")[0]
        respuesta = self.respuestas[slug]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


class TestConsultar(unittest.TestCase):
    def consultar(self, favoritos, twitch=None, kick=None, youtube=None):
        sesion = SesionKick(kick or {})
        with mock.patch.object(envivo.requests, "post", side_effect=lambda *a, **kw: twitch or Respuesta(TWITCH_OK)), \
                mock.patch.object(envivo.requests, "get", side_effect=lambda url, **kw: youtube[url.split("@")[1].split("/")[0]]), \
                mock.patch.object(envivo, "_sesion_kick", return_value=sesion):
            return envivo.consultar(favoritos), sesion

    def test_twitch_en_vivo_offline_e_inexistente(self):
        resultado, _ = self.consultar([
            {"canal": "AuronPlay", "plataforma": "twitch"},
            {"canal": "xqc", "plataforma": "twitch"},
            {"canal": "noexiste", "plataforma": "twitch"},
        ])
        self.assertEqual(resultado["twitch:auronplay"], {
            "vivo": True, "espectadores": 235411, "titulo": "AMONG US", "categoria": "Among Us"})
        self.assertFalse(resultado["twitch:xqc"]["vivo"])
        # Un canal que no existe no es "offline": no se sabe.
        self.assertNotIn("twitch:noexiste", resultado)

    def test_kick_en_vivo_y_offline_con_slug_de_guiones(self):
        resultado, sesion = self.consultar(
            [{"canal": "la_cobra", "plataforma": "kick"}, {"canal": "spreen", "plataforma": "kick"}],
            kick={
                "la-cobra": Respuesta({"data": {"viewers": 5000, "session_title": "charlando", "category": {"name": "Just Chatting"}}}),
                "spreen": Respuesta({"data": None}),
            },
        )
        self.assertEqual(resultado["kick:la_cobra"]["espectadores"], 5000)
        self.assertEqual(resultado["kick:la_cobra"]["categoria"], "Just Chatting")
        self.assertFalse(resultado["kick:spreen"]["vivo"])
        self.assertTrue(any("/channels/la-cobra/" in u for u in sesion.pedidas))

    def test_youtube_lee_la_pagina_live(self):
        pagina = '<meta name="title" content="24/7 lofi &amp; chill">"isLiveNow":true,"originalViewCount":"4314"'
        resultado, _ = self.consultar(
            [{"canal": "LofiGirl", "plataforma": "youtube"}, {"canal": "midudev", "plataforma": "youtube"}],
            youtube={"lofigirl": Respuesta(texto=pagina), "midudev": Respuesta(texto="sin directo")},
        )
        self.assertEqual(resultado["youtube:lofigirl"]["titulo"], "24/7 lofi & chill")
        self.assertEqual(resultado["youtube:lofigirl"]["espectadores"], 4314)
        self.assertFalse(resultado["youtube:midudev"]["vivo"])

    def test_una_plataforma_caida_no_tapa_a_las_otras(self):
        resultado, _ = self.consultar(
            [{"canal": "xqc", "plataforma": "twitch"}, {"canal": "spreen", "plataforma": "kick"}],
            twitch=Respuesta(codigo=503),
            kick={"spreen": Respuesta({"data": None})},
        )
        self.assertNotIn("twitch:xqc", resultado)
        self.assertIn("kick:spreen", resultado)

    def test_un_canal_de_kick_con_error_queda_sin_dato(self):
        resultado, _ = self.consultar(
            [{"canal": "uno", "plataforma": "kick"}, {"canal": "dos", "plataforma": "kick"}],
            kick={"uno": RuntimeError("cloudflare"), "dos": Respuesta({"data": None})},
        )
        self.assertNotIn("kick:uno", resultado)
        self.assertIn("kick:dos", resultado)

    def test_un_link_de_canal_se_consulta_por_su_nombre(self):
        link = "https://kick.com/davooxeneize"
        resultado, sesion = self.consultar(
            [{"canal": link, "plataforma": "kick"}],
            kick={"davooxeneize": Respuesta({"data": {"viewers": 900, "session_title": "hola"}})},
        )
        # La clave es el favorito tal como esta guardado: asi lo busca la UI.
        self.assertEqual(resultado[envivo.clave("kick", link)]["espectadores"], 900)

    def test_reconoce_los_links_de_cada_sitio(self):
        casos = {
            "https://www.twitch.tv/XQC": ("twitch", "xqc"),
            "https://kick.com/la-cobra/": ("kick", "la-cobra"),
            "https://www.youtube.com/@LofiGirl/live": ("youtube", "lofigirl"),
            "xqc": ("twitch", "xqc"),
            "https://www.youtube.com/watch?v=abc": None,
            "https://vimeo.com/123": None,
        }
        for canal, esperado in casos.items():
            self.assertEqual(envivo.canal_consultable(canal, "twitch"), esperado, canal)

    def test_los_links_que_no_son_de_un_canal_no_se_consultan(self):
        with mock.patch.object(envivo.requests, "post") as post, mock.patch.object(envivo.requests, "get") as get:
            resultado = envivo.consultar([{"canal": "https://www.youtube.com/watch?v=abc", "plataforma": "youtube"}])
        self.assertEqual(resultado, {})
        post.assert_not_called()
        get.assert_not_called()


class TestFormato(unittest.TestCase):
    def test_espectadores_corto(self):
        casos = {None: "", 812: "812", 1000: "1 mil", 4314: "4,3 mil", 11437: "11,4 mil",
                 235411: "235 mil", 1234567: "1,2 M"}
        for n, esperado in casos.items():
            self.assertEqual(envivo.espectadores_corto(n), esperado, n)

    def test_ordenar_pone_primero_los_vivos_y_respeta_el_orden(self):
        favs = [{"canal": c, "plataforma": "twitch"} for c in ("a", "b", "c", "d")]
        en_vivo = {"twitch:c": {"vivo": True}, "twitch:b": {"vivo": False}, "twitch:d": {"vivo": True}}
        orden = [f["canal"] for f, _ in envivo.ordenar(favs, en_vivo)]
        self.assertEqual(orden, ["c", "d", "a", "b"])


class TestSondeoEnElControlador(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        for p in (
            mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "c.json")),
            mock.patch.object(discovery, "descubrir", return_value=[tv_falsa("TV", "192.168.1.63", udn="u")]),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.ctrl = Controlador()
        self.addCleanup(self.ctrl.limpiar)

    def test_actualizar_publica_el_estado(self):
        eventos = []
        self.ctrl.suscribir(lambda e, d: eventos.append(d) if e == "estado" else None)
        with mock.patch.object(envivo, "consultar", return_value={"twitch:xqc": {"vivo": True}}):
            self.ctrl.actualizar_en_vivo()
        self.assertEqual(eventos[-1]["en_vivo"], {"twitch:xqc": {"vivo": True}})

    def test_una_falla_avisa_una_sola_vez_y_conserva_el_dato_anterior(self):
        self.ctrl.en_vivo = {"twitch:xqc": {"vivo": True}}
        with mock.patch.object(envivo, "consultar", side_effect=RuntimeError("sin red")):
            for _ in range(3):
                self.ctrl.actualizar_en_vivo()
        avisos = [l for l in self.ctrl.log_reciente if "en vivo" in l["texto"]]
        self.assertEqual(len(avisos), 1)
        self.assertEqual(self.ctrl.en_vivo, {"twitch:xqc": {"vivo": True}})

    def test_agregar_un_favorito_despierta_al_sondeo(self):
        llamadas = []
        with mock.patch.object(envivo, "consultar", side_effect=lambda favs: llamadas.append(len(favs)) or {}):
            self.ctrl.iniciar_sondeo(intervalo=60)
            limite = time.time() + 5
            while not llamadas and time.time() < limite:
                time.sleep(0.01)
            self.ctrl.alternar_favorito("xqc", "twitch")
            while len(llamadas) < 2 and time.time() < limite:
                time.sleep(0.01)
            self.ctrl.detener_sondeo()
        # Sin esperar los 60 s: la segunda consulta ya incluye al favorito nuevo.
        self.assertEqual(llamadas[:2], [0, 1])

    def test_limpiar_detiene_el_sondeo(self):
        with mock.patch.object(envivo, "consultar", return_value={}):
            self.ctrl.iniciar_sondeo(intervalo=60)
            hilo = self.ctrl._sondeo
            self.ctrl.limpiar()
            hilo.join(2)
        self.assertFalse(hilo.is_alive())


if __name__ == "__main__":
    unittest.main()

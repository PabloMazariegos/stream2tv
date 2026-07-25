"""Tests de la eleccion de backend y del selector de formato.

Motivo: el plugin de YouTube de streamlink solo consigue 360p, asi que YouTube
se baja con yt-dlp. Y hay que pedirle H.264 explicitamente, porque los formatos
mas altos de YouTube son VP9/AV1 y la TV no los reproduce.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import caster


class TestPrefiereYtdlp(unittest.TestCase):
    def setUp(self):
        self.hay_ytdlp = mock.patch.object(caster, "encontrar_ytdlp", return_value="/bin/yt-dlp")
        self.hay_ytdlp.start()
        self.addCleanup(self.hay_ytdlp.stop)

    def test_youtube_usa_ytdlp(self):
        self.assertTrue(caster.prefiere_ytdlp("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(caster.prefiere_ytdlp("https://youtu.be/abc"))

    def test_twitch_y_kick_siguen_con_streamlink(self):
        self.assertFalse(caster.prefiere_ytdlp("https://twitch.tv/xqc"))
        self.assertFalse(caster.prefiere_ytdlp("https://kick.com/lacobraaa"))

    def test_sin_ytdlp_instalado_no_lo_prefiere(self):
        with mock.patch.object(caster, "encontrar_ytdlp", return_value=None):
            self.assertFalse(caster.prefiere_ytdlp("https://www.youtube.com/watch?v=abc"))


class TestElegirBackend(unittest.TestCase):
    """yt-dlp tambien hace de red de contencion: cubre ~1750 sitios contra ~134."""

    def setUp(self):
        mock.patch.object(caster, "encontrar_ytdlp", return_value="/bin/yt-dlp").start()
        self.addCleanup(mock.patch.stopall)

    def test_sitio_conocido_por_streamlink_va_a_streamlink(self):
        with mock.patch.object(caster, "streamlink_soporta", return_value=True):
            self.assertEqual(caster.elegir_backend("https://twitch.tv/xqc"), "streamlink")

    def test_sitio_desconocido_cae_en_ytdlp(self):
        with mock.patch.object(caster, "streamlink_soporta", return_value=False):
            self.assertEqual(caster.elegir_backend("https://sitio-raro.net/algo"), "yt-dlp")

    def test_youtube_siempre_va_a_ytdlp_aunque_streamlink_lo_soporte(self):
        with mock.patch.object(caster, "streamlink_soporta", return_value=True):
            self.assertEqual(caster.elegir_backend("https://youtu.be/abc"), "yt-dlp")

    def test_sin_ytdlp_se_queda_con_streamlink_para_que_de_su_error(self):
        with mock.patch.object(caster, "encontrar_ytdlp", return_value=None), \
             mock.patch.object(caster, "streamlink_soporta", return_value=False):
            self.assertEqual(caster.elegir_backend("https://sitio-raro.net/algo"), "streamlink")


class TestStreamlinkSoporta(unittest.TestCase):
    """Consulta real a la API de streamlink, sin tocar la red."""

    def test_reconoce_sitios_con_plugin(self):
        self.assertTrue(caster.streamlink_soporta("https://twitch.tv/xqc"))

    def test_rechaza_sitios_sin_plugin(self):
        self.assertFalse(caster.streamlink_soporta("https://sitio-inventado-12345.net/x"))

    def test_ante_un_error_raro_asume_que_si(self):
        # Mejor dejar que el binario intente y de su propio mensaje.
        with mock.patch("streamlink.Streamlink", side_effect=RuntimeError("boom")):
            caster._sesion_streamlink = None
            self.assertTrue(caster.streamlink_soporta("https://loquesea.net/x"))
        caster._sesion_streamlink = None


class TestComandoStream(unittest.TestCase):
    def setUp(self):
        mock.patch.object(caster, "encontrar_ytdlp", return_value="/bin/yt-dlp").start()
        mock.patch.object(caster, "encontrar_streamlink", return_value="/bin/streamlink").start()
        mock.patch.object(caster, "runtime_js", return_value="node").start()
        self.addCleanup(mock.patch.stopall)

    def test_auto_manda_youtube_a_ytdlp(self):
        cmd = caster.comando_stream("https://www.youtube.com/watch?v=abc")
        self.assertEqual(cmd[0], "/bin/yt-dlp")

    def test_auto_manda_twitch_a_streamlink(self):
        cmd = caster.comando_stream("https://twitch.tv/xqc")
        self.assertEqual(cmd[0], "/bin/streamlink")

    def test_se_puede_forzar_streamlink_en_youtube(self):
        cmd = caster.comando_stream("https://www.youtube.com/watch?v=abc", backend="streamlink")
        self.assertEqual(cmd[0], "/bin/streamlink")

    def test_se_puede_forzar_ytdlp_en_cualquier_sitio(self):
        cmd = caster.comando_stream("https://twitch.tv/xqc", backend="yt-dlp")
        self.assertEqual(cmd[0], "/bin/yt-dlp")

    def test_forzar_ytdlp_sin_tenerlo_avisa_claro(self):
        with mock.patch.object(caster, "encontrar_ytdlp", return_value=None):
            with self.assertRaises(caster.FaltaBackend):
                caster.comando_stream("https://twitch.tv/xqc", backend="yt-dlp")

    def test_ytdlp_escribe_a_stdout(self):
        cmd = caster.comando_ytdlp("https://www.youtube.com/watch?v=abc")
        self.assertEqual(cmd[-1], "https://www.youtube.com/watch?v=abc")
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], "-")

    def test_ytdlp_pasa_el_runtime_de_javascript(self):
        # Sin runtime, YouTube devuelve 403 en los formatos de alta calidad.
        cmd = caster.comando_ytdlp("https://www.youtube.com/watch?v=abc")
        self.assertIn("--js-runtimes", cmd)
        self.assertEqual(cmd[cmd.index("--js-runtimes") + 1], "node")

    def test_sin_runtime_no_pasa_la_opcion(self):
        with mock.patch.object(caster, "runtime_js", return_value=None):
            self.assertNotIn("--js-runtimes", caster.comando_ytdlp("https://youtu.be/x"))


class TestFormatoYtdlp(unittest.TestCase):
    def test_pide_h264_y_aac(self):
        # La TV no decodifica VP9 ni AV1, y ffmpeg los mete en el TS como
        # "private data stream", que la TV directamente no reconoce.
        fmt = caster.formato_ytdlp("best")
        self.assertIn("vcodec^=avc1", fmt)
        self.assertIn("acodec^=mp4a", fmt)

    def test_respeta_el_tope_de_altura(self):
        fmt = caster.formato_ytdlp("720p")
        self.assertIn("height<=720", fmt)

    def test_acepta_la_calidad_sin_la_p(self):
        self.assertIn("height<=1080", caster.formato_ytdlp("1080"))

    def test_best_no_pone_tope(self):
        self.assertNotIn("height", caster.formato_ytdlp("best"))

    def test_worst_pide_lo_peor(self):
        self.assertTrue(caster.formato_ytdlp("worst").startswith("wv*"))

    def test_siempre_deja_un_fallback(self):
        # Si no hay H.264, mejor bajar algo que fallar.
        self.assertTrue(caster.formato_ytdlp("best").endswith("/b"))


class TestComandoVerificacion(unittest.TestCase):
    def setUp(self):
        mock.patch.object(caster, "encontrar_ytdlp", return_value="/bin/yt-dlp").start()
        mock.patch.object(caster, "encontrar_streamlink", return_value="/bin/streamlink").start()
        mock.patch.object(caster, "runtime_js", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def test_youtube_se_verifica_con_ytdlp(self):
        cmd = caster.comando_verificacion("https://youtu.be/abc")
        self.assertEqual(cmd[0], "/bin/yt-dlp")
        self.assertIn("-g", cmd)

    def test_twitch_se_verifica_con_streamlink(self):
        cmd = caster.comando_verificacion("https://twitch.tv/xqc")
        self.assertEqual(cmd[0], "/bin/streamlink")
        self.assertIn("--stream-url", cmd)

    def test_el_backend_faltante_no_rompe_la_verificacion(self):
        with mock.patch.object(caster, "encontrar_ytdlp", return_value=None):
            vivo, motivo = caster.canal_en_vivo("https://twitch.tv/x", backend="yt-dlp")
        self.assertFalse(vivo)
        self.assertIn("yt-dlp", motivo)


if __name__ == "__main__":
    unittest.main()

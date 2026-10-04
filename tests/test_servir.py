"""Tests del modo normal: `stream2tv` sin canal levanta el panel web."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import cli, config, controlador, web


class PanelFalso:
    instancias = []

    def __init__(self, ctrl, puerto):
        self.ctrl = ctrl
        self.puerto = puerto
        self.url = f"http://192.168.1.18:{puerto}/"
        self.url_con_clave = self.url + "?t=clave"
        self.detenido = False
        PanelFalso.instancias.append(self)

    def iniciar(self):
        pass

    def detener(self):
        self.detenido = True


class TestServir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        PanelFalso.instancias = []
        self.abiertos = []
        self.limpiezas = []
        parches = [
            mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "c.json")),
            mock.patch.object(web, "PanelWeb", PanelFalso),
            mock.patch.object(controlador.Controlador, "buscar_tvs"),
            mock.patch.object(controlador.Controlador, "iniciar_sondeo"),
            mock.patch.object(controlador.Controlador, "limpiar", lambda c: self.limpiezas.append(c)),
            mock.patch.object(cli, "abrir_navegador", lambda url: self.abiertos.append(url)),
            # La primera espera "recibe" el Ctrl+C: el bucle termina enseguida.
            mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)

    def correr(self, *argv):
        salida = io.StringIO()
        with redirect_stdout(salida):
            codigo = cli.main(argv=list(argv))
        return codigo, salida.getvalue()

    def test_sin_canal_abre_el_panel_en_el_navegador_y_muestra_el_qr(self):
        codigo, salida = self.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.abiertos, ["http://192.168.1.18:8091/?t=clave"])
        self.assertIn("panel en http://192.168.1.18:8091/", salida)
        self.assertIn("escanea este codigo", salida)
        self.assertGreater(salida.count("\n"), 15)  # el QR ocupa varias lineas

    def test_ctrl_c_apaga_el_panel_y_detiene_todo(self):
        self.correr()
        self.assertTrue(PanelFalso.instancias[0].detenido)
        self.assertEqual(len(self.limpiezas), 1)

    def test_no_browser_y_otro_puerto(self):
        self.correr("--no-browser", "--web-port", "9001")
        self.assertEqual(self.abiertos, [])
        self.assertEqual(PanelFalso.instancias[0].puerto, 9001)

    def test_puerto_del_panel_ocupado_avisa(self):
        class Ocupado(PanelFalso):
            def iniciar(self):
                raise web.PanelOcupado("el 8091 esta ocupado")

        with mock.patch.object(web, "PanelWeb", Ocupado):
            codigo, salida = self.correr()
        self.assertEqual(codigo, 1)
        self.assertIn("ocupado", salida)


class TestOpciones(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        parche = mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "c.json"))
        parche.start()
        self.addCleanup(parche.stop)

    def test_reset_key_renueva_la_clave(self):
        vieja = config.token_web()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(argv=["--reset-key"]), 0)
        self.assertNotEqual(config.token_web(), vieja)

    def test_twitch2tv_arranca_el_panel_con_twitch(self):
        recibidos = []
        with mock.patch.object(cli, "servir", lambda args, plataforma_default=None: recibidos.append(plataforma_default) or 0):
            cli.main(plataforma_default="twitch", argv=[])
        self.assertEqual(recibidos, ["twitch"])
        ctrl = controlador.Controlador(plataforma_inicial="twitch")
        self.assertEqual(ctrl.estado()["plataforma_inicial"], "twitch")


class TestSenales(unittest.TestCase):
    def test_sigterm_y_sighup_salen_como_ctrl_c_y_se_restauran(self):
        import signal
        antes = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        anteriores = cli.salir_con_senales()
        try:
            for senal in (signal.SIGTERM, signal.SIGHUP):
                with self.assertRaises(KeyboardInterrupt):
                    signal.getsignal(senal)(senal, None)
        finally:
            for senal, manejador in anteriores.items():
                signal.signal(senal, manejador)
        self.assertEqual({s: signal.getsignal(s) for s in antes}, antes)


class TestAbrirNavegador(unittest.TestCase):
    def test_sin_escritorio_no_abre_un_navegador_de_texto(self):
        with mock.patch.object(cli.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), \
                mock.patch("webbrowser.open") as abrir, redirect_stdout(io.StringIO()):
            self.assertFalse(cli.abrir_navegador("http://x/"))
        abrir.assert_not_called()

    def test_con_escritorio_abre(self):
        with mock.patch.object(cli.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("webbrowser.open", return_value=True) as abrir:
            self.assertTrue(cli.abrir_navegador("http://x/"))
        abrir.assert_called_once_with("http://x/")


if __name__ == "__main__":
    unittest.main()

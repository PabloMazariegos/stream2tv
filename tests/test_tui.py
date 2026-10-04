"""Tests de la TUI, corriendo la app de verdad en modo headless.

Correr con:  python3 -m unittest discover -s tests
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_stream2tv import tv_falsa

from stream2tv import caster, config, discovery, tui
from textual.worker import WorkerCancelled


class ServidorFalso:
    """Reemplaza al servidor HTTP: no abre puertos ni lanza streamlink."""

    detenidos = []

    def __init__(self, url, puerto, calidad="best", low_latency=False, log=print, backend="auto", **kw):
        self.url = url
        self.backend = backend
        self.host = kw.get("host")
        self.cliente_permitido = kw.get("cliente_permitido")

    def iniciar(self):
        pass

    def detener(self):
        ServidorFalso.detenidos.append(self.url)


class BaseTUI(unittest.IsolatedAsyncioTestCase):
    """Entorno comun sin tests propios: si tuviera tests, cada subclase los
    heredaria y se correrian dos veces."""

    def setUp(self):
        self.tvs = [
            tv_falsa("[LG] webOS TV UP7710PSB", "192.168.0.50", udn="uuid:lg"),
            tv_falsa("Chromecast sala", "192.168.0.77", udn="uuid:cc"),
        ]
        ServidorFalso.detenidos.clear()
        self.reproducidos = []
        self.stops = []

        # Config aislada: los tests no deben tocar la config real del usuario.
        self.config_tmp = os.path.join(os.path.dirname(__file__), "_config_test.json")
        self.patches = [
            mock.patch.object(config, "RUTA_CONFIG", self.config_tmp),
            mock.patch.object(discovery, "descubrir", return_value=self.tvs),
            mock.patch.object(caster, "canal_en_vivo", return_value=(True, "")),
            mock.patch.object(caster, "Servidor", ServidorFalso),
            mock.patch.object(
                caster, "detener_tv", lambda tv, log=print: self.stops.append(tv.nombre) or True
            ),
            mock.patch.object(discovery, "ip_local_hacia", return_value="192.168.0.2"),
            mock.patch.object(
                caster,
                "reproducir",
                lambda tv, url, titulo, log=print, **kw: self.reproducidos.append((tv.nombre, titulo)) or True,
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if os.path.exists(self.config_tmp):
            os.remove(self.config_tmp)

    async def _esperar(self, app, pilot):
        """Deja que terminen los workers en thread y se repinte la UI."""
        while True:
            try:
                await app.workers.wait_for_complete()
                break
            except WorkerCancelled:
                # Un worker cancelado a proposito (Detener durante un arranque)
                # corta la espera; se sigue esperando al resto.
                continue
        await pilot.pause()


class TestTUI(BaseTUI):
    async def test_arranca_y_lista_las_tvs(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            lista = app.query_one("#lista-tvs")
            self.assertEqual(len(lista.children), 2)
            self.assertIs(app.tv_actual, self.tvs[0])

    async def test_escribir_canal_y_enter_reproduce(self):
        app = tui.StreamTVApp(plataforma_inicial="twitch")
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"xqc")
            await pilot.press("enter")
            await self._esperar(app, pilot)
            self.assertTrue(app.reproduciendo)
            self.assertEqual(app.url_actual, "https://twitch.tv/xqc")
            self.assertEqual(self.reproducidos, [("[LG] webOS TV UP7710PSB", "xqc")])

    async def test_detener_libera_el_servidor(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"lacobraaa", "enter")
            await self._esperar(app, pilot)
            app.action_detener()
            await self._esperar(app, pilot)
            self.assertFalse(app.reproduciendo)
            self.assertEqual(ServidorFalso.detenidos, ["https://kick.com/lacobraaa"])

    async def test_doble_reproducir_con_sitio_lento_abre_un_solo_servidor(self):
        # Kick a veces tarda en responder la verificacion; si el usuario aprieta
        # Reproducir de nuevo, los dos arranques no deben pelear por el puerto.
        iniciados = []
        liberar = threading.Event()

        def verificacion_lenta(*a, **kw):
            liberar.wait(5)
            return True, ""

        class ServidorContado(ServidorFalso):
            def iniciar(self):
                iniciados.append(self.url)
                if len(iniciados) > 1 and not ServidorFalso.detenidos:
                    raise caster.PuertoOcupado("puerto ocupado")

        with mock.patch.object(caster, "canal_en_vivo", verificacion_lenta), \
                mock.patch.object(caster, "Servidor", ServidorContado):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                await pilot.click("#canal")
                await pilot.press(*"lacobraaa", "enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                liberar.set()
                await self._esperar(app, pilot)
                self.assertEqual(iniciados, ["https://kick.com/lacobraaa"])
                self.assertTrue(app.reproduciendo)

    async def test_detener_durante_la_verificacion_no_manda_nada_a_la_tv(self):
        liberar = threading.Event()

        def verificacion_lenta(*a, **kw):
            liberar.wait(5)
            return True, ""

        with mock.patch.object(caster, "canal_en_vivo", verificacion_lenta):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                await pilot.click("#canal")
                await pilot.press(*"lacobraaa", "enter")
                await pilot.pause()
                app.action_detener()
                liberar.set()
                await self._esperar(app, pilot)
                self.assertEqual(self.reproducidos, [])
                self.assertFalse(app.reproduciendo)
                self.assertIsNone(app.servidor)

    async def test_el_servidor_solo_atiende_a_la_tv(self):
        creados = []

        class ServidorAnotado(ServidorFalso):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                creados.append(self)

        with mock.patch.object(caster, "Servidor", ServidorAnotado):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                await pilot.click("#canal")
                await pilot.press(*"lacobraaa", "enter")
                await self._esperar(app, pilot)
        self.assertEqual(creados[0].host, "192.168.0.2")
        self.assertEqual(creados[0].cliente_permitido, "192.168.0.50")

    async def test_tv_parada_sola_limpia_el_estado(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"lacobraaa", "enter")
            await self._esperar(app, pilot)
            with mock.patch.object(caster, "estado_tv", return_value="STOPPED"):
                app.verificar_estado()
                await self._esperar(app, pilot)
            self.assertFalse(app.reproduciendo)
            self.assertIsNone(app.url_actual)
            self.assertIsNone(app.servidor)

    async def test_canal_offline_no_deja_el_stream_colgado(self):
        with mock.patch.object(caster, "canal_en_vivo", return_value=(False, "offline")):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                await pilot.click("#canal")
                await pilot.press(*"nadie", "enter")
                await self._esperar(app, pilot)
                self.assertFalse(app.reproduciendo)
                self.assertIn("no esta en vivo", app.estado)

    async def test_puerto_ocupado_se_reporta_sin_crashear(self):
        class ServidorQueFalla(ServidorFalso):
            def iniciar(self):
                raise caster.PuertoOcupado("puerto 8090 ocupado")

        with mock.patch.object(caster, "Servidor", ServidorQueFalla):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                await pilot.click("#canal")
                await pilot.press(*"algo", "enter")
                await self._esperar(app, pilot)
                self.assertFalse(app.reproduciendo)
                self.assertIn("Puerto ocupado", app.estado)

    async def test_favorito_se_guarda_y_aparece_en_la_lista(self):
        app = tui.StreamTVApp(plataforma_inicial="twitch")
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"xqc")
            app.action_favorito()
            await pilot.pause()
            self.assertTrue(config.es_favorito("xqc", "twitch"))
            self.assertEqual(len(app.query_one("#lista-favoritos").children), 1)
            # Segunda vez lo quita.
            app.action_favorito()
            await pilot.pause()
            self.assertFalse(config.es_favorito("xqc", "twitch"))

    async def test_guardar_tv_por_defecto(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            app.tv_actual = self.tvs[1]
            app.action_predeterminar()
            await self._esperar(app, pilot)
            self.assertEqual(config.tv_por_defecto()[0], "uuid:cc")
            # Guardar la TV redibuja la lista; la elegida no debe saltar a la primera.
            self.assertIs(app.tv_actual, self.tvs[1])
            self.assertEqual(app.query_one("#lista-tvs").index, 1)

    async def test_sin_tvs_avisa_en_vez_de_romperse(self):
        with mock.patch.object(discovery, "descubrir", return_value=[]):
            app = tui.StreamTVApp()
            async with app.run_test() as pilot:
                await self._esperar(app, pilot)
                self.assertIsNone(app.tv_actual)
                self.assertIn("No se encontro", app.estado)

    async def test_atajo_detener_funciona_con_el_foco_en_el_input(self):
        # Si el binding fuera una letra suelta, se escribiria en el campo.
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"lacobraaa", "enter")
            await self._esperar(app, pilot)
            self.assertTrue(app.reproduciendo)
            await pilot.press("ctrl+s")
            await self._esperar(app, pilot)
            self.assertFalse(app.reproduciendo)
            self.assertEqual(app.query_one("#canal").value, "lacobraaa")

    async def test_cambiar_de_canal_detiene_el_anterior(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"uno", "enter")
            await self._esperar(app, pilot)
            entrada = app.query_one("#canal")
            entrada.value = "dos"
            app.action_reproducir()
            await self._esperar(app, pilot)
            self.assertEqual(ServidorFalso.detenidos, ["https://kick.com/uno"])
            self.assertEqual(app.url_actual, "https://kick.com/dos")


class PanelFalso:
    def __init__(self):
        self.iniciado = False
        self.detenido = False
        self.url = "http://192.168.1.18:8091/"
        self.url_con_clave = self.url + "?t=clave"

    def iniciar(self):
        self.iniciado = True

    def detener(self):
        self.detenido = True


class TestPanelEnLaTUI(BaseTUI):
    async def test_sin_panel_ctrl_w_avisa(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.press("ctrl+w")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.PantallaQR)

    async def test_con_panel_muestra_el_qr_y_lo_apaga_al_salir(self):
        panel = PanelFalso()
        app = tui.StreamTVApp(panel=panel)
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            self.assertTrue(panel.iniciado)
            await pilot.press("ctrl+w")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.PantallaQR)
            self.assertIn("?t=clave", str(app.screen.query_one("#url-panel").render()))
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.PantallaQR)
            app.action_salir()
            await pilot.pause()
        self.assertTrue(panel.detenido)


class TestSalida(BaseTUI):
    """Salir no debe dejar el stream corriendo ni la TV reproduciendo."""

    async def test_salir_mientras_reproduce_detiene_todo(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"lacobraaa", "enter")
            await self._esperar(app, pilot)
            self.assertTrue(app.reproduciendo)

            app.action_salir()
            await pilot.pause()

        self.assertEqual(ServidorFalso.detenidos, ["https://kick.com/lacobraaa"])
        self.assertIn("[LG] webOS TV UP7710PSB", self.stops)
        self.assertFalse(app.reproduciendo)

    async def test_detener_y_salir_enseguida_igual_apaga_la_tv(self):
        # El caso real reportado: Ctrl+S y Ctrl+Q casi juntos. El worker del
        # stop se cancelaba a mitad y la TV seguia reproduciendo.
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"lacobraaa", "enter")
            await self._esperar(app, pilot)

            app.action_detener()      # se lanza el worker...
            app.action_salir()        # ...y salimos sin esperarlo
            await pilot.pause()

        self.assertIn("[LG] webOS TV UP7710PSB", self.stops)
        self.assertEqual(ServidorFalso.detenidos, ["https://kick.com/lacobraaa"])

    async def test_limpiar_es_idempotente(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            await pilot.click("#canal")
            await pilot.press(*"uno", "enter")
            await self._esperar(app, pilot)
            app.limpiar()
            app.limpiar()
            app.limpiar()
        self.assertEqual(ServidorFalso.detenidos, ["https://kick.com/uno"])
        self.assertEqual(len(self.stops), 1)

    async def test_limpiar_sin_haber_reproducido_no_molesta_a_la_tv(self):
        app = tui.StreamTVApp()
        async with app.run_test() as pilot:
            await self._esperar(app, pilot)
            app.limpiar()
        self.assertEqual(self.stops, [])


class TestLanzar(unittest.TestCase):
    """lanzar() debe limpiar aunque la app muera por Ctrl+C o por una excepcion."""

    def test_limpia_si_run_lanza_excepcion(self):
        app = mock.Mock()
        app.run.side_effect = RuntimeError("boom")
        with mock.patch.object(tui, "StreamTVApp", return_value=app):
            with self.assertRaises(RuntimeError):
                tui.lanzar()
        app.limpiar.assert_called_once()

    def test_limpia_si_el_usuario_hace_ctrl_c(self):
        app = mock.Mock()
        app.run.side_effect = KeyboardInterrupt()
        with mock.patch.object(tui, "StreamTVApp", return_value=app):
            with self.assertRaises(KeyboardInterrupt):
                tui.lanzar()
        app.limpiar.assert_called_once()

    def test_limpia_en_la_salida_normal(self):
        app = mock.Mock()
        with mock.patch.object(tui, "StreamTVApp", return_value=app):
            self.assertEqual(tui.lanzar(), 0)
        app.limpiar.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Tests de la TUI, corriendo la app de verdad en modo headless.

Correr con:  python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_stream2tv import tv_falsa

from stream2tv import caster, config, tui


class ServidorFalso:
    """Reemplaza al servidor HTTP: no abre puertos ni lanza streamlink."""

    detenidos = []

    def __init__(self, url, puerto, calidad="best", low_latency=False, log=print, backend="auto"):
        self.url = url
        self.backend = backend

    def iniciar(self):
        pass

    def detener(self):
        ServidorFalso.detenidos.append(self.url)


class TestTUI(unittest.IsolatedAsyncioTestCase):
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
            mock.patch.object(tui.discovery, "descubrir", return_value=self.tvs),
            mock.patch.object(tui.caster, "canal_en_vivo", return_value=(True, "")),
            mock.patch.object(tui.caster, "Servidor", ServidorFalso),
            mock.patch.object(
                tui.caster, "detener_tv", lambda tv, log=print: self.stops.append(tv.nombre) or True
            ),
            mock.patch.object(tui.discovery, "ip_local_hacia", return_value="192.168.0.2"),
            mock.patch.object(
                tui.caster,
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
        await app.workers.wait_for_complete()
        await pilot.pause()

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

    async def test_canal_offline_no_deja_el_stream_colgado(self):
        with mock.patch.object(tui.caster, "canal_en_vivo", return_value=(False, "offline")):
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

        with mock.patch.object(tui.caster, "Servidor", ServidorQueFalla):
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

    async def test_sin_tvs_avisa_en_vez_de_romperse(self):
        with mock.patch.object(tui.discovery, "descubrir", return_value=[]):
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


class TestSalida(TestTUI):
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

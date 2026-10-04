"""Tests del Controlador: la logica de reproduccion, sin ninguna pantalla."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import caster, config, discovery
from stream2tv.controlador import Controlador
from test_stream2tv import tv_falsa


class ServidorFalso:
    creados = []

    def __init__(self, url, puerto, calidad="best", low_latency=False, log=print, backend="auto", **kw):
        self.url = url
        self.host = kw.get("host")
        self.cliente_permitido = kw.get("cliente_permitido")
        self.detenciones = 0
        ServidorFalso.creados.append(self)

    def iniciar(self):
        pass

    def detener(self):
        self.detenciones += 1


class BaseControlador(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tvs = [
            tv_falsa("Samsung sala", "192.168.1.63", udn="uuid:samsung"),
            tv_falsa("Kodi (Mi Box)", "192.168.1.75", udn="uuid:kodi"),
        ]
        self.enviados = []
        self.stops = []
        ServidorFalso.creados = []
        parches = [
            mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "config.json")),
            mock.patch.object(discovery, "descubrir", return_value=self.tvs),
            mock.patch.object(discovery, "ip_local_hacia", return_value="192.168.1.18"),
            mock.patch.object(caster, "canal_en_vivo", return_value=(True, "")),
            mock.patch.object(caster, "Servidor", ServidorFalso),
            mock.patch.object(caster, "apagar_todo"),
            mock.patch.object(
                caster, "reproducir",
                lambda tv, url, titulo, log=print, **kw: self.enviados.append((tv.nombre, titulo)) or True,
            ),
            mock.patch.object(
                caster, "detener_tv", lambda tv, log=print: self.stops.append(tv.nombre) or True
            ),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir)
        self.ctrl = Controlador()
        self.eventos = []
        self.ctrl.suscribir(lambda e, d: self.eventos.append((e, d)))
        self.ctrl.buscar_tvs()

    def fases(self):
        return [d["fase"] for e, d in self.eventos if e == "estado"]

    def en_thread(self, funcion, *args):
        hilo = threading.Thread(target=funcion, args=args, daemon=True)
        hilo.start()
        return hilo

    def esperar(self, condicion, timeout=5):
        limite = time.time() + timeout
        while time.time() < limite:
            if condicion():
                return True
            time.sleep(0.01)
        return False


class TestReproducir(BaseControlador):
    def test_pasa_por_las_fases_y_queda_reproduciendo(self):
        self.assertTrue(self.ctrl.reproducir("xqc", "twitch"))
        self.assertEqual(self.ctrl.fase, "reproduciendo")
        self.assertEqual(self.ctrl.url_actual, "https://twitch.tv/xqc")
        self.assertEqual(self.enviados, [("Samsung sala", "xqc")])
        fases = self.fases()
        self.assertLess(fases.index("verificando"), fases.index("enviando"))
        self.assertLess(fases.index("enviando"), fases.index("reproduciendo"))

    def test_el_servidor_solo_atiende_a_la_tv_elegida(self):
        self.ctrl.elegir_tv("uuid:kodi")
        self.ctrl.reproducir("xqc", "twitch")
        servidor = ServidorFalso.creados[0]
        self.assertEqual(servidor.host, "192.168.1.18")
        self.assertEqual(servidor.cliente_permitido, "192.168.1.75")

    def test_canal_offline_queda_en_error_sin_servidor(self):
        with mock.patch.object(caster, "canal_en_vivo", return_value=(False, "offline")):
            self.assertFalse(self.ctrl.reproducir("nadie", "kick"))
        self.assertEqual(self.ctrl.fase, "error")
        self.assertIn("no esta en vivo", self.ctrl.mensaje)
        self.assertEqual(ServidorFalso.creados, [])

    def test_puerto_ocupado_se_reporta(self):
        class Ocupado(ServidorFalso):
            def iniciar(self):
                raise caster.PuertoOcupado("ocupado")

        with mock.patch.object(caster, "Servidor", Ocupado):
            self.assertFalse(self.ctrl.reproducir("algo", "kick"))
        self.assertEqual(self.ctrl.mensaje, "Puerto ocupado")
        self.assertIsNone(self.ctrl.servidor)

    def test_cambiar_de_canal_detiene_el_anterior(self):
        self.ctrl.reproducir("uno", "kick")
        primero = ServidorFalso.creados[0]
        self.ctrl.reproducir("dos", "kick")
        self.assertEqual(primero.detenciones, 1)
        self.assertEqual(self.ctrl.url_actual, "https://kick.com/dos")


class TestConcurrencia(BaseControlador):
    def verificacion_lenta(self):
        liberar = threading.Event()

        def verificar(*a, **kw):
            liberar.wait(5)
            return True, ""

        return liberar, verificar

    def test_dos_arranques_seguidos_abren_un_solo_servidor(self):
        liberar, verificar = self.verificacion_lenta()
        with mock.patch.object(caster, "canal_en_vivo", verificar):
            a = self.en_thread(self.ctrl.reproducir, "uno", "kick", self.ctrl.reservar())
            b = self.en_thread(self.ctrl.reproducir, "dos", "kick", self.ctrl.reservar())
            liberar.set()
            a.join(5)
            b.join(5)
        self.assertEqual([s.url for s in ServidorFalso.creados], ["https://kick.com/dos"])
        self.assertEqual(self.enviados, [("Samsung sala", "dos")])

    def test_detener_durante_la_verificacion_no_toca_la_tv(self):
        liberar, verificar = self.verificacion_lenta()
        with mock.patch.object(caster, "canal_en_vivo", verificar):
            arranque = self.en_thread(self.ctrl.reproducir, "uno", "kick", self.ctrl.reservar())
            # La mitad sincrona del stop, como la llama la UI al click.
            self.assertTrue(self.ctrl.anunciar_detencion())
            liberar.set()
            self.ctrl.detener(anunciado=True)
            arranque.join(5)
        self.assertEqual(self.enviados, [])
        self.assertEqual(ServidorFalso.creados, [])
        self.assertEqual(self.ctrl.fase, "libre")
        self.assertFalse(self.ctrl.arrancando())

    def test_detener_sin_nada_no_hace_nada(self):
        self.assertFalse(self.ctrl.detener())
        self.assertEqual(self.stops, [])

    def test_reserva_cuenta_como_arranque_antes_de_que_arranque_el_thread(self):
        reserva = self.ctrl.reservar()
        self.assertTrue(self.ctrl.arrancando())
        self.assertTrue(self.ctrl.anunciar_detencion())
        # El thread llega tarde: ya lo cancelaron.
        self.assertFalse(self.ctrl.reproducir("uno", "kick", reserva))
        self.assertEqual(self.enviados, [])


class TestTVs(BaseControlador):
    def test_el_stop_va_a_la_tv_que_reproduce_aunque_se_elija_otra(self):
        self.ctrl.reproducir("xqc", "twitch")  # en la Samsung
        self.ctrl.elegir_tv("uuid:kodi")
        self.ctrl.detener()
        self.assertEqual(self.stops, ["Samsung sala"])

    def test_buscar_conserva_la_tv_elegida(self):
        self.ctrl.elegir_tv("uuid:kodi")
        self.ctrl.buscar_tvs()
        self.assertEqual(self.ctrl.tv_actual.udn, "uuid:kodi")

    def test_usa_la_tv_por_defecto_al_arrancar(self):
        config.set_tv_por_defecto("uuid:kodi", "Kodi (Mi Box)")
        ctrl = Controlador()
        ctrl.buscar_tvs()
        self.assertEqual(ctrl.tv_actual.udn, "uuid:kodi")

    def test_busqueda_vacia_mientras_reproduce_no_corta(self):
        self.ctrl.reproducir("xqc", "twitch")
        with mock.patch.object(discovery, "descubrir", return_value=[]):
            self.ctrl.buscar_tvs()
        self.assertEqual(self.ctrl.fase, "reproduciendo")
        self.assertEqual(len(self.ctrl.tvs), 2)

    def test_sin_tvs_queda_en_error(self):
        with mock.patch.object(discovery, "descubrir", return_value=[]):
            self.ctrl.buscar_tvs()
        self.assertIsNone(self.ctrl.tv_actual)
        self.assertIn("No se encontro", self.ctrl.mensaje)


class TestMonitorYSalida(BaseControlador):
    def test_tv_parada_sola_limpia_el_estado(self):
        self.ctrl.reproducir("xqc", "twitch")
        with mock.patch.object(caster, "estado_tv", return_value="STOPPED"):
            self.assertTrue(self.ctrl.verificar_estado())
        self.assertFalse(self.ctrl.reproduciendo)
        self.assertIsNone(self.ctrl.servidor)
        self.assertEqual(self.ctrl.fase, "error")
        # Ya estaba parada: salir no le manda otro Stop.
        self.ctrl.limpiar()
        self.assertEqual(self.stops, [])

    def test_limpiar_es_idempotente(self):
        self.ctrl.reproducir("uno", "kick")
        for _ in range(3):
            self.ctrl.limpiar()
        self.assertEqual(ServidorFalso.creados[0].detenciones, 1)
        self.assertEqual(self.stops, ["Samsung sala"])

    def test_stop_y_limpiar_a_la_vez_detienen_el_servidor_una_vez(self):
        self.ctrl.reproducir("uno", "kick")
        hilo = self.en_thread(self.ctrl.detener)
        self.ctrl.limpiar()
        hilo.join(5)
        self.assertEqual(ServidorFalso.creados[0].detenciones, 1)


class TestEventos(BaseControlador):
    def test_estado_es_serializable(self):
        self.ctrl.alternar_favorito("xqc", "twitch")
        self.ctrl.reproducir("xqc", "twitch")
        datos = json.loads(json.dumps(self.ctrl.estado()))
        self.assertEqual(datos["fase"], "reproduciendo")
        self.assertEqual(datos["tv"], "uuid:samsung")
        self.assertEqual(datos["favoritos"], [{"canal": "xqc", "plataforma": "twitch"}])
        self.assertEqual({tv["ip"] for tv in datos["tvs"]}, {"192.168.1.63", "192.168.1.75"})

    def test_un_suscriptor_roto_no_frena_a_los_demas(self):
        def roto(e, d):
            raise RuntimeError("pantalla rota")

        recibidos = []
        self.ctrl.suscribir(roto)
        self.ctrl.suscribir(lambda e, d: recibidos.append(e))
        self.assertTrue(self.ctrl.reproducir("xqc", "twitch"))
        self.assertIn("estado", recibidos)

    def test_el_log_reciente_tiene_tope(self):
        for i in range(80):
            self.ctrl.log(f"linea {i}")
        self.assertEqual(len(self.ctrl.log_reciente), 50)
        self.assertEqual(self.ctrl.log_reciente[-1]["texto"], "linea 79")


if __name__ == "__main__":
    unittest.main()

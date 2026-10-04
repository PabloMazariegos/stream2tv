"""Tests de la auditoria: metadata escapada, errores visibles del backend,
servidor restringido a la TV, config que no se pierde y monitoreo en la CLI.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.dom.minidom
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import caster, cli, config, discovery
from test_limpieza import esperar_muerte, puerto_libre
from test_stream2tv import tv_falsa


class TestMetadata(unittest.TestCase):
    def test_url_con_ampersand_da_xml_valido(self):
        didl = caster.metadata_didl(
            "https://www.youtube.com/watch?v=abc&t=10", "http://192.168.0.2:8090/stream.ts"
        )
        doc = xml.dom.minidom.parseString(didl)
        titulo = doc.getElementsByTagName("dc:title")[0].firstChild.data
        self.assertEqual(titulo, "https://www.youtube.com/watch?v=abc&t=10")

    def test_titulo_con_signos_menor_mayor(self):
        xml.dom.minidom.parseString(caster.metadata_didl("<canal> & cia", "http://x/stream.ts"))


class TestFaltaStreamlink(unittest.TestCase):
    def test_lanza_falta_backend_en_vez_de_salir(self):
        with mock.patch.object(caster.os.path, "isfile", return_value=False), \
                mock.patch.object(caster.shutil, "which", return_value=None):
            with self.assertRaises(caster.FaltaBackend):
                caster.encontrar_streamlink()

    def test_canal_en_vivo_lo_reporta_como_motivo(self):
        with mock.patch.object(caster.os.path, "isfile", return_value=False), \
                mock.patch.object(caster.shutil, "which", return_value=None), \
                mock.patch.object(caster, "streamlink_soporta", return_value=True):
            vivo, motivo = caster.canal_en_vivo("https://kick.com/x", backend="streamlink")
        self.assertFalse(vivo)
        self.assertIn("streamlink", motivo)


class TestServidor(unittest.TestCase):
    def setUp(self):
        self.puerto = puerto_libre()
        self.mensajes = []
        self.servidor = None

    def tearDown(self):
        if self.servidor is not None:
            self.servidor.detener()

    def _servidor(self, comando, **kw):
        self.servidor = caster.Servidor(
            "http://ejemplo", self.puerto, log=self.mensajes.append, host="127.0.0.1",
            backend="streamlink", **kw
        )
        self.parche = mock.patch.object(caster, "comando_streamlink", return_value=comando)
        self.parche.start()
        self.addCleanup(self.parche.stop)
        self.servidor.iniciar()
        return self.servidor

    def _url(self):
        return f"http://127.0.0.1:{self.puerto}{caster.RUTA_STREAM}"

    def _esperar(self, condicion, timeout=5):
        limite = time.time() + timeout
        while time.time() < limite:
            if condicion():
                return True
            time.sleep(0.05)
        return False

    def test_rechaza_clientes_que_no_son_la_tv(self):
        self._servidor(["bash", "-c", "sleep 300"], cliente_permitido="192.168.0.50")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url(), timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.servidor._procesos, [])

    def test_reporta_por_que_se_corto_el_backend(self):
        comando = ["bash", "-c", "echo 'error: HTTPSConnectionPool port=443 timed out' >&2; exit 1"]
        with mock.patch.object(caster, "ESPERAS_REINTENTO", (0,)):
            self._servidor(comando)
            with urllib.request.urlopen(self._url(), timeout=5) as r:
                r.read()
        self.assertTrue(self._esperar(lambda: any("no se pudo recuperar" in m for m in self.mensajes)))
        mensaje = next(m for m in self.mensajes if "se corto" in m)
        self.assertIn("codigo 1", mensaje)
        self.assertIn("443", mensaje)

    def test_un_segundo_pedido_corta_el_primero(self):
        self._servidor(["bash", "-c", "while true; do echo x; sleep 0.1; done"])

        def pedir():
            try:
                with urllib.request.urlopen(self._url(), timeout=10) as r:
                    r.read(1)
                    time.sleep(10)
            except Exception:
                pass

        threading.Thread(target=pedir, daemon=True).start()
        self.assertTrue(self._esperar(lambda: len(self.servidor._procesos) == 1))
        primero = self.servidor._procesos[0]
        threading.Thread(target=pedir, daemon=True).start()
        self.assertTrue(esperar_muerte(primero.pid), "el primer backend siguio corriendo")
        self.assertTrue(self._esperar(lambda: len(self.servidor._procesos) == 1))
        self.assertIsNot(self.servidor._procesos[0], primero)
        # Cortarlo a proposito no es un error que haya que reportar.
        self.assertFalse(any("se corto" in m for m in self.mensajes))


class TestReconexion(unittest.TestCase):
    """Si el backend se cae a mitad del stream, se relanza sin cortar la TV."""

    def setUp(self):
        self.puerto = puerto_libre()
        self.mensajes = []
        self.avisos = []
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        parche = mock.patch.object(caster, "ESPERAS_REINTENTO", (0.05, 0.05, 0.05))
        parche.start()
        self.addCleanup(parche.stop)

    def _servidor(self, script):
        self.servidor = caster.Servidor(
            "http://ejemplo", self.puerto, log=self.mensajes.append, host="127.0.0.1",
            backend="streamlink", al_estado=lambda tipo, **d: self.avisos.append(tipo),
        )
        self.addCleanup(self.servidor.detener)
        parche = mock.patch.object(caster, "comando_streamlink", return_value=["bash", "-c", script])
        parche.start()
        self.addCleanup(parche.stop)
        self.servidor.iniciar()

    def _url(self):
        return f"http://127.0.0.1:{self.puerto}{caster.RUTA_STREAM}"

    def _esperar(self, condicion, timeout=5):
        limite = time.time() + timeout
        while time.time() < limite:
            if condicion():
                return True
            time.sleep(0.02)
        return False

    def test_se_cae_y_vuelve_por_la_misma_conexion(self):
        contador = os.path.join(self.dir, "corridas")
        # Primera corrida: falla como un timeout de Kick. Las siguientes: video.
        self._servidor(
            f"n=$(cat {contador} 2>/dev/null || echo 0); echo $((n+1)) > {contador}; "
            "if [ $n -eq 0 ]; then echo 'error: 443 timed out' >&2; exit 1; fi; "
            "while true; do echo video; sleep 0.05; done"
        )
        with urllib.request.urlopen(self._url(), timeout=5) as r:
            datos = r.read(20)
        self.assertIn(b"video", datos)
        self.assertTrue(self._esperar(lambda: "recuperado" in self.avisos))
        self.assertEqual(self.avisos[:2], ["reconectando", "recuperado"])
        self.assertTrue(any("intento 1 de 3" in m for m in self.mensajes))

    def test_se_rinde_despues_de_los_intentos(self):
        self._servidor("echo 'error: No playable streams found' >&2; exit 1")
        with urllib.request.urlopen(self._url(), timeout=5) as r:
            self.assertEqual(r.read(), b"")  # la respuesta termina: no queda colgada
        self.assertTrue(self._esperar(lambda: "terminado" in self.avisos))
        self.assertEqual(self.avisos.count("reconectando"), 3)
        self.assertTrue(any("No playable streams" in m for m in self.mensajes if "no se pudo recuperar" in m))

    def test_detener_durante_la_espera_no_relanza(self):
        contador = os.path.join(self.dir, "corridas")
        with mock.patch.object(caster, "ESPERAS_REINTENTO", (2, 2)):
            self._servidor(f"echo x >> {contador}; exit 1")

            def pedir():
                try:
                    urllib.request.urlopen(self._url(), timeout=10).read()
                except Exception:
                    pass

            threading.Thread(target=pedir, daemon=True).start()
            self.assertTrue(self._esperar(lambda: "reconectando" in self.avisos))
            self.servidor.detener()
            time.sleep(2.5)
        with open(contador) as f:
            self.assertEqual(len(f.read().split()), 1)

    def test_si_la_tv_corta_no_se_reintenta(self):
        self._servidor("while true; do echo video; sleep 0.02; done")
        with urllib.request.urlopen(self._url(), timeout=5) as r:
            r.read(10)
        self.assertTrue(self._esperar(lambda: any("La TV cerro" in m for m in self.mensajes)))
        time.sleep(0.3)
        self.assertNotIn("reconectando", self.avisos)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ruta = os.path.join(self.dir, "config.json")
        self.parche = mock.patch.object(config, "RUTA_CONFIG", self.ruta)
        self.parche.start()

    def tearDown(self):
        self.parche.stop()
        shutil.rmtree(self.dir)

    def test_json_corrupto_se_aparta_en_vez_de_pisarse(self):
        with open(self.ruta, "w") as f:
            f.write('{"favoritos": [{"canal": "uno", "plataforma": "kick"}')
        config.alternar_favorito("dos", "kick")
        with open(self.ruta + ".corrupto") as f:
            self.assertIn('"uno"', f.read())
        self.assertEqual(config.favoritos(), [{"canal": "dos", "plataforma": "kick"}])

    def test_guardar_no_deja_temporales(self):
        config.guardar({"a": 1})
        self.assertEqual(os.listdir(self.dir), ["config.json"])
        with open(self.ruta) as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_si_falla_la_escritura_queda_el_archivo_anterior(self):
        config.guardar({"a": 1})
        with mock.patch.object(config.json, "dump", side_effect=RuntimeError("corte")):
            with self.assertRaises(RuntimeError):
                config.guardar({"a": 2})
        with open(self.ruta) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["config.json"])


class TestEsperarFin(unittest.TestCase):
    def setUp(self):
        self.tv = tv_falsa("TV", "192.168.0.50")
        mock.patch.object(cli.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_termina_tras_dos_stopped_seguidos(self):
        estados = iter(["PLAYING", "STOPPED", "STOPPED"])
        with mock.patch.object(caster, "estado_tv", side_effect=lambda tv: next(estados)) as m:
            cli.esperar_fin(self.tv)
        self.assertEqual(m.call_count, 3)

    def test_un_stopped_suelto_no_corta(self):
        estados = iter(["STOPPED", "PLAYING", "STOPPED", "STOPPED"])
        with mock.patch.object(caster, "estado_tv", side_effect=lambda tv: next(estados)) as m:
            cli.esperar_fin(self.tv)
        self.assertEqual(m.call_count, 4)


class TestDesdeIp(unittest.TestCase):
    def test_gana_la_primera_combinacion_que_responde(self):
        def construir(location):
            if location.endswith(":8080/") or location.endswith(":49152/dmr.xml"):
                return tv_falsa(location, "192.168.0.50")
            return None

        with mock.patch.object(discovery, "_construir", side_effect=construir):
            tv = discovery.desde_ip("192.168.0.50")
        self.assertEqual(tv.nombre, "http://192.168.0.50:8080/")

    def test_sin_respuesta_devuelve_none(self):
        with mock.patch.object(discovery, "_construir", return_value=None):
            self.assertIsNone(discovery.desde_ip("192.168.0.50"))


if __name__ == "__main__":
    unittest.main()

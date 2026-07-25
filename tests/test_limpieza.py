"""Tests de que no queden procesos ni streams corriendo al detener o salir.

Usan procesos de verdad (bash + sleep) en lugar de streamlink, para verificar
que se mata el arbol completo y no solo el proceso padre.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import caster


def sigue_vivo(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def esperar_muerte(pid, timeout=5):
    limite = time.time() + timeout
    while time.time() < limite:
        if not sigue_vivo(pid):
            return True
        time.sleep(0.05)
    return not sigue_vivo(pid)


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    puerto = s.getsockname()[1]
    s.close()
    return puerto


class TestMatarArbol(unittest.TestCase):
    def test_mata_tambien_a_los_hijos(self):
        # Simula streamlink lanzando un ffmpeg: si solo se mata al padre, el
        # nieto sigue bajando el stream para siempre.
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 300 & echo $!; wait"],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        nieto = int(proc.stdout.readline().strip())
        self.assertTrue(sigue_vivo(nieto))
        self.addCleanup(proc.stdout.close)

        caster.matar_arbol(proc)

        self.assertTrue(esperar_muerte(proc.pid), "el proceso padre sobrevivio")
        self.assertTrue(esperar_muerte(nieto), "el proceso hijo quedo huerfano")

    def test_es_seguro_llamarlo_dos_veces(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        caster.matar_arbol(proc)
        caster.matar_arbol(proc)  # no debe explotar
        self.assertFalse(sigue_vivo(proc.pid))


class TestServidorLimpia(unittest.TestCase):
    """El servidor HTTP, con un proceso falso en lugar de streamlink."""

    def setUp(self):
        self.puerto = puerto_libre()
        self.pids = []
        # 'streamlink' falso que a su vez lanza un hijo y anuncia ambos PIDs.
        self.comando = ["bash", "-c", "sleep 300 & echo $! >&2; sleep 300"]

    def _arrancar_y_conectar(self, servidor):
        servidor.iniciar()
        url = f"http://127.0.0.1:{self.puerto}{caster.RUTA_STREAM}"

        def pedir():
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    r.read(10)
            except Exception:
                pass

        hilo = threading.Thread(target=pedir, daemon=True)
        hilo.start()
        # Esperar a que el handler lance el proceso.
        limite = time.time() + 5
        while time.time() < limite and not servidor._procesos:
            time.sleep(0.05)
        self.assertTrue(servidor._procesos, "el servidor nunca lanzo el proceso")
        return servidor._procesos[0]

    def test_detener_mata_el_proceso_del_stream(self):
        with mock.patch.object(caster, "comando_streamlink", return_value=self.comando):
            servidor = caster.Servidor("http://ejemplo", self.puerto, log=lambda *_: None)
            proc = self._arrancar_y_conectar(servidor)
            pid = proc.pid
            servidor.detener()
            self.assertTrue(esperar_muerte(pid), "streamlink siguio corriendo tras detener")

    def test_detener_libera_el_puerto(self):
        with mock.patch.object(caster, "comando_streamlink", return_value=self.comando):
            servidor = caster.Servidor("http://ejemplo", self.puerto, log=lambda *_: None)
            self._arrancar_y_conectar(servidor)
            servidor.detener()
            # Si el puerto no se libero, este bind falla.
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", self.puerto))
            finally:
                s.close()

    def test_apagar_todo_alcanza_servidores_sin_referencia(self):
        with mock.patch.object(caster, "comando_streamlink", return_value=self.comando):
            servidor = caster.Servidor("http://ejemplo", self.puerto, log=lambda *_: None)
            proc = self._arrancar_y_conectar(servidor)
            pid = proc.pid
            del servidor  # se pierde la referencia, como al salir de golpe
            caster.apagar_todo()
            self.assertTrue(esperar_muerte(pid), "apagar_todo no limpio el servidor")

    def test_puerto_ocupado_avisa_en_vez_de_matar_el_proceso(self):
        bloqueador = socket.socket()
        bloqueador.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bloqueador.bind(("0.0.0.0", self.puerto))
        bloqueador.listen(1)
        try:
            servidor = caster.Servidor("http://ejemplo", self.puerto, log=lambda *_: None)
            with self.assertRaises(caster.PuertoOcupado):
                servidor.iniciar()
        finally:
            bloqueador.close()


if __name__ == "__main__":
    unittest.main()

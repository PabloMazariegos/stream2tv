"""Tests del panel web: servidor HTTP real contra un Controlador real con la red falseada."""

import http.client
import io
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

from stream2tv import caster, config, discovery, web
from stream2tv.controlador import Controlador
from test_controlador import ServidorFalso
from test_limpieza import puerto_libre
from test_stream2tv import tv_falsa


class BaseWeb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tvs = [
            tv_falsa("Samsung sala", "192.168.1.63", udn="uuid:samsung"),
            tv_falsa("Kodi (Mi Box)", "192.168.1.75", udn="uuid:kodi"),
        ]
        self.enviados = []
        self.verificar = mock.Mock(return_value=(True, ""))
        parches = [
            mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "config.json")),
            mock.patch.object(discovery, "descubrir", return_value=self.tvs),
            mock.patch.object(discovery, "ip_local_hacia", return_value="192.168.1.18"),
            mock.patch.object(caster, "canal_en_vivo", lambda *a, **kw: self.verificar(*a, **kw)),
            mock.patch.object(caster, "Servidor", ServidorFalso),
            mock.patch.object(caster, "apagar_todo"),
            mock.patch.object(
                caster, "reproducir",
                lambda tv, url, titulo, log=print, **kw: self.enviados.append((tv.nombre, titulo)) or True,
            ),
            mock.patch.object(caster, "detener_tv", lambda tv, log=print: True),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir)

        self.ctrl = Controlador()
        self.ctrl.buscar_tvs()
        self.panel = web.PanelWeb(self.ctrl, puerto_libre(), host="127.0.0.1")
        self.panel.iniciar()
        self.addCleanup(self.panel.detener)
        self.addCleanup(self.ctrl.limpiar)
        self.clave = config.token_web()

    # ---------- helpers ----------

    def pedir(self, metodo, ruta, cuerpo=None, cookie=True, propio=True, headers=None):
        conexion = http.client.HTTPConnection("127.0.0.1", self.panel.puerto, timeout=5)
        cabeceras = dict(headers or {})
        if cookie:
            cabeceras["Cookie"] = f"{web.COOKIE}={self.clave if cookie is True else cookie}"
        if propio:
            cabeceras[web.ENCABEZADO_PROPIO] = "1"
        datos = None
        if cuerpo is not None:
            datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
            cabeceras["Content-Type"] = "application/json"
        conexion.request(metodo, ruta, body=datos, headers=cabeceras)
        respuesta = conexion.getresponse()
        contenido = respuesta.read()
        conexion.close()
        return respuesta, contenido

    def json_de(self, contenido):
        return json.loads(contenido)

    def esperar(self, condicion, timeout=5):
        limite = time.time() + timeout
        while time.time() < limite:
            if condicion():
                return True
            time.sleep(0.02)
        return False


class TestAcceso(BaseWeb):
    def test_la_pagina_se_sirve_sin_clave(self):
        r, contenido = self.pedir("GET", "/", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"stream2tv", contenido)
        self.assertIn("text/html", r.getheader("Content-Type"))

    def test_la_api_exige_la_clave(self):
        r, _ = self.pedir("GET", "/api/estado", cookie=False)
        self.assertEqual(r.status, 401)
        r, _ = self.pedir("GET", "/api/estado", cookie="otra")
        self.assertEqual(r.status, 401)
        r, contenido = self.pedir("GET", "/api/estado")
        self.assertEqual(r.status, 200)
        self.assertEqual(self.json_de(contenido)["tv"], "uuid:samsung")

    def test_el_link_del_qr_deja_la_cookie(self):
        r, _ = self.pedir("GET", f"/?t={self.clave}", cookie=False, propio=False)
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/")
        cookie = r.getheader("Set-Cookie")
        self.assertIn(f"{web.COOKIE}={self.clave}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_una_clave_falsa_no_deja_cookie(self):
        r, _ = self.pedir("GET", "/?t=adivinada", cookie=False, propio=False)
        self.assertEqual(r.status, 303)
        self.assertIsNone(r.getheader("Set-Cookie"))

    def test_una_clave_con_caracteres_raros_se_rechaza_sin_romper(self):
        r, _ = self.pedir("GET", "/?t=%C3%B1and%C3%BA", cookie=False, propio=False)
        self.assertEqual(r.status, 303)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertFalse(web.clave_valida("ñandú"))

    def test_regenerar_la_clave_desautoriza_al_celular_viejo(self):
        vieja = self.clave
        config.regenerar_token_web()
        r, _ = self.pedir("GET", "/api/estado", cookie=vieja)
        self.assertEqual(r.status, 401)

    def test_las_acciones_exigen_el_encabezado_propio(self):
        r, _ = self.pedir("POST", "/api/detener", {}, propio=False)
        self.assertEqual(r.status, 403)

    def test_las_acciones_exigen_la_clave(self):
        r, _ = self.pedir("POST", "/api/reproducir", {"canal": "xqc", "plataforma": "twitch"}, cookie=False)
        self.assertEqual(r.status, 401)
        self.assertEqual(self.enviados, [])

    def test_la_pagina_es_la_de_cuando_arranco_la_app(self):
        # Actualizar stream2tv con la app abierta no debe mezclar una pagina
        # nueva con un servidor viejo: se sigue sirviendo la cargada al inicio.
        original = self.panel.archivos["/"]
        with mock.patch.object(web, "CARPETA_WEB", tempfile.mkdtemp()):
            r, contenido = self.pedir("GET", "/", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(contenido, original)

    def test_solo_sirve_los_archivos_del_panel(self):
        for ruta in ("/index.html", "/../config.json", "/web/index.html", "/stream2tv/config.py"):
            r, _ = self.pedir("GET", ruta)
            self.assertEqual(r.status, 404, ruta)
        r, contenido = self.pedir("GET", "/manifest.json", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.json_de(contenido)["name"], "stream2tv")


class TestAcciones(BaseWeb):
    def test_reproducir_responde_enseguida_y_arranca_en_segundo_plano(self):
        liberar = threading.Event()
        self.verificar.side_effect = lambda *a, **kw: (liberar.wait(5), (True, ""))[1]
        r, contenido = self.pedir("POST", "/api/reproducir", {"canal": "xqc", "plataforma": "twitch"})
        self.assertEqual(r.status, 202)
        self.assertTrue(self.json_de(contenido)["ok"])
        self.assertTrue(self.ctrl.arrancando())
        liberar.set()
        self.assertTrue(self.esperar(lambda: self.ctrl.fase == "reproduciendo"))
        self.assertEqual(self.enviados, [("Samsung sala", "xqc")])

    def test_detener_desde_el_celular_cancela_el_arranque(self):
        liberar = threading.Event()
        self.verificar.side_effect = lambda *a, **kw: (liberar.wait(5), (True, ""))[1]
        self.pedir("POST", "/api/reproducir", {"canal": "xqc", "plataforma": "twitch"})
        r, _ = self.pedir("POST", "/api/detener", {})
        self.assertEqual(r.status, 202)
        liberar.set()
        self.assertTrue(self.esperar(lambda: self.ctrl.fase == "libre" and not self.ctrl.arrancando()))
        self.assertEqual(self.enviados, [])

    def test_detener_sin_nada_avisa(self):
        r, contenido = self.pedir("POST", "/api/detener", {})
        self.assertEqual(r.status, 200)
        self.assertFalse(self.json_de(contenido)["ok"])

    def test_pedidos_invalidos(self):
        casos = [
            ({"canal": "", "plataforma": "kick"}, 400),
            ({"canal": "x" * 201, "plataforma": "kick"}, 400),
            ({"canal": "xqc", "plataforma": "netflix"}, 400),
            ({"canal": "xqc"}, 400),
        ]
        for cuerpo, codigo in casos:
            r, _ = self.pedir("POST", "/api/reproducir", cuerpo)
            self.assertEqual(r.status, codigo, cuerpo)
        r, _ = self.pedir("POST", "/api/reproducir", b"no es json")
        self.assertEqual(r.status, 400)
        r, _ = self.pedir("POST", "/api/reproducir", b"[1, 2]")
        self.assertEqual(r.status, 400)
        r, _ = self.pedir("POST", "/api/reproducir", b"{" + b" " * 5000 + b"}")
        self.assertEqual(r.status, 413)
        r, _ = self.pedir("POST", "/api/no-existe", {})
        self.assertEqual(r.status, 404)
        self.assertEqual(self.enviados, [])

    def test_elegir_tv(self):
        r, _ = self.pedir("POST", "/api/tv", {"udn": "uuid:kodi"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.ctrl.tv_actual.udn, "uuid:kodi")
        # Queda como la TV de siempre para la proxima vez que se abra stream2tv.
        self.assertEqual(config.tv_por_defecto()[0], "uuid:kodi")
        r, _ = self.pedir("POST", "/api/tv", {"udn": "uuid:inventada"})
        self.assertEqual(r.status, 404)

    def test_favoritos_se_agregan_y_se_quitan(self):
        r, contenido = self.pedir("POST", "/api/favoritos", {"canal": "xqc", "plataforma": "twitch"})
        self.assertTrue(self.json_de(contenido)["agregado"])
        self.assertTrue(config.es_favorito("xqc", "twitch"))
        r, contenido = self.pedir("POST", "/api/favoritos", {"canal": "xqc", "plataforma": "twitch"})
        self.assertFalse(self.json_de(contenido)["agregado"])

    def test_reordenar_favoritos(self):
        for canal in ("uno", "dos", "tres"):
            config.alternar_favorito(canal, "kick")
        orden = [{"canal": c, "plataforma": "kick"} for c in ("tres", "uno", "dos")]
        r, _ = self.pedir("POST", "/api/favoritos/orden", {"orden": orden})
        self.assertEqual(r.status, 200)
        self.assertEqual([f["canal"] for f in config.favoritos()], ["tres", "uno", "dos"])
        # Si la lista no coincide (alguien agrego o quito uno), no se toca nada.
        r, _ = self.pedir("POST", "/api/favoritos/orden", {"orden": orden[:2]})
        self.assertEqual(r.status, 409)
        self.assertEqual(len(config.favoritos()), 3)
        r, _ = self.pedir("POST", "/api/favoritos/orden", {"orden": "x"})
        self.assertEqual(r.status, 400)

    def test_sortable_se_sirve_sin_internet(self):
        r, contenido = self.pedir("GET", "/sortable.min.js", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"Sortable", contenido[:200])

    def test_buscar_tvs(self):
        r, _ = self.pedir("POST", "/api/buscar-tvs", {})
        self.assertEqual(r.status, 202)

    def test_el_estado_incluye_el_log(self):
        self.ctrl.log("hola desde la laptop")
        _, contenido = self.pedir("GET", "/api/estado")
        textos = [l["texto"] for l in self.json_de(contenido)["log"]]
        self.assertIn("hola desde la laptop", textos)

    def test_el_estado_incluye_quien_esta_en_vivo(self):
        self.ctrl.en_vivo = {"twitch:xqc": {"vivo": True, "espectadores": 10, "titulo": "t", "categoria": "c"}}
        _, contenido = self.pedir("GET", "/api/estado")
        self.assertEqual(self.json_de(contenido)["en_vivo"]["twitch:xqc"]["espectadores"], 10)


class TestCortesDeConexion(unittest.TestCase):
    def test_un_cliente_que_corta_no_ensucia_la_terminal(self):
        servidor = web._Servidor.__new__(web._Servidor)
        errores = io.StringIO()
        with mock.patch.object(sys, "stderr", errores):
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except ConnectionResetError:
                servidor.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(errores.getvalue(), "")


class TestEventos(BaseWeb):
    def abrir_eventos(self):
        conexion = http.client.HTTPConnection("127.0.0.1", self.panel.puerto, timeout=5)
        conexion.request("GET", "/api/eventos", headers={"Cookie": f"{web.COOKIE}={self.clave}"})
        respuesta = conexion.getresponse()
        self.addCleanup(conexion.close)
        return respuesta

    def leer_evento(self, respuesta):
        evento, datos = None, None
        while True:
            linea = respuesta.fp.readline().decode().rstrip("\n")
            if linea.startswith("event: "):
                evento = linea[7:]
            elif linea.startswith("data: "):
                datos = json.loads(linea[6:])
            elif linea == "" and evento:
                return evento, datos

    def test_manda_el_estado_al_conectar_y_los_cambios_en_vivo(self):
        respuesta = self.abrir_eventos()
        self.assertEqual(respuesta.status, 200)
        self.assertEqual(respuesta.getheader("Content-Type"), "text/event-stream")
        evento, datos = self.leer_evento(respuesta)
        self.assertEqual(evento, "estado")
        self.assertEqual(datos["tv"], "uuid:samsung")

        # Un cambio hecho desde otra pantalla (la TUI) llega al celular.
        self.ctrl.log("cambio desde la TUI")
        evento, datos = self.leer_evento(respuesta)
        self.assertEqual((evento, datos["texto"]), ("log", "cambio desde la TUI"))

    def test_sin_clave_no_abre_eventos(self):
        conexion = http.client.HTTPConnection("127.0.0.1", self.panel.puerto, timeout=5)
        conexion.request("GET", "/api/eventos")
        self.assertEqual(conexion.getresponse().status, 401)
        conexion.close()

    def test_cerrar_el_panel_corta_los_eventos_y_libera_al_suscriptor(self):
        respuesta = self.abrir_eventos()
        self.leer_evento(respuesta)
        suscriptores = len(self.ctrl._suscriptores)
        self.panel.detener()
        self.assertTrue(self.esperar(lambda: len(self.ctrl._suscriptores) == suscriptores - 1))


class TestAnuncios(BaseWeb):
    def test_los_avisos_de_streamlink_cuentan_anuncios(self):
        self.ctrl.reproducir("xqc", "twitch")
        self.ctrl._aviso_backend("[plugins.twitch][info] Detected advertisement break of 30 seconds")
        self.ctrl._aviso_backend("[plugins.twitch][info] Detected advertisement break of 60 seconds")
        self.ctrl._aviso_backend("[cli][info] Opening stream: 1080p60 (hls)")
        self.assertEqual(self.ctrl.estado()["anuncios"], 2)
        textos = [l["texto"] for l in self.ctrl.log_reciente]
        self.assertTrue(any("Anuncio filtrado (60 s)" in t for t in textos))

    def test_un_canal_nuevo_reinicia_el_contador(self):
        self.ctrl.reproducir("xqc", "twitch")
        self.ctrl._aviso_backend("Detected advertisement break of 30 seconds")
        self.ctrl.reproducir("otro", "twitch")
        self.assertEqual(self.ctrl.anuncios, 0)


class TestVerEnElNavegador(BaseWeb):
    # ffmpeg de mentira: copia la entrada a la salida tal cual.
    REMUX = [sys.executable, "-c", "import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)"]

    def setUp(self):
        super().setUp()
        parche = mock.patch.object(web, "REMUX_MP4", self.REMUX)
        parche.start()
        self.addCleanup(parche.stop)

    def backend(self, codigo):
        """Hace que el "streamlink" del panel sea un python -c con este codigo."""
        comando = mock.Mock(return_value=[sys.executable, "-c", codigo])
        parche = mock.patch.object(caster, "comando_stream", comando)
        parche.start()
        self.addCleanup(parche.stop)
        return comando

    def abrir(self, canal="xqc", plataforma="kick"):
        conexion = http.client.HTTPConnection("127.0.0.1", self.panel.puerto, timeout=5)
        conexion.request("GET", f"/api/ver?canal={canal}&plataforma={plataforma}", headers={"Cookie": f"{web.COOKIE}={self.clave}"})
        respuesta = conexion.getresponse()
        self.addCleanup(conexion.close)
        return conexion, respuesta

    def test_manda_lo_que_sale_del_backend_pasado_por_ffmpeg(self):
        comando = self.backend("import sys; sys.stdout.buffer.write(b'G' * 376)")
        r, contenido = self.pedir("GET", "/api/ver?canal=xqc&plataforma=twitch", propio=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "video/mp4")
        self.assertEqual(contenido, b"G" * 376)
        self.assertEqual(comando.call_args[0][0], "https://twitch.tv/xqc")
        self.assertTrue(self.esperar(lambda: not self.panel._vistas))

    def test_exige_clave_y_un_pedido_valido(self):
        comando = self.backend("pass")
        r, _ = self.pedir("GET", "/api/ver?canal=xqc&plataforma=twitch", cookie=False)
        self.assertEqual(r.status, 401)
        r, _ = self.pedir("GET", "/api/ver?canal=xqc&plataforma=otra")
        self.assertEqual(r.status, 400)
        r, _ = self.pedir("GET", "/api/ver?plataforma=twitch")
        self.assertEqual(r.status, 400)
        comando.assert_not_called()

    def test_cerrar_el_reproductor_mata_backend_y_ffmpeg(self):
        self.backend("import sys\nwhile True: sys.stdout.buffer.write(b'G' * 18800)")
        conexion, respuesta = self.abrir()
        self.assertEqual(respuesta.read(188), b"G" * 188)
        procesos = list(self.panel._vistas)
        self.assertEqual(len(procesos), 2)
        respuesta.close()
        conexion.close()
        self.assertTrue(self.esperar(lambda: all(p.poll() is not None for p in procesos) and not self.panel._vistas))

    def test_cerrar_el_panel_mata_lo_que_se_esta_viendo(self):
        self.backend("import sys\nwhile True: sys.stdout.buffer.write(b'G' * 18800)")
        _, respuesta = self.abrir()
        respuesta.read(188)
        procesos = list(self.panel._vistas)
        self.panel.detener()
        self.assertTrue(self.esperar(lambda: all(p.poll() is not None for p in procesos)))

    def test_un_canal_que_no_arranca_queda_en_la_actividad(self):
        self.backend("import sys; sys.stderr.write('error: No playable streams found\\n'); sys.exit(1)")
        r, contenido = self.pedir("GET", "/api/ver?canal=nadie&plataforma=kick")
        self.assertEqual(contenido, b"")
        self.assertTrue(self.esperar(lambda: any(
            "No se pudo ver nadie" in l["texto"] and "No playable streams" in l["texto"]
            for l in self.ctrl.log_reciente
        )))

    def test_sin_ffmpeg_avisa_en_vez_de_lanzar_el_backend(self):
        self.backend("pass")
        with mock.patch.object(web, "REMUX_MP4", ["ffmpeg-que-no-existe"]):
            r, contenido = self.pedir("GET", "/api/ver?canal=xqc&plataforma=kick")
        self.assertEqual(r.status, 503)
        self.assertIn("ffmpeg", self.json_de(contenido)["error"])
        self.assertFalse(self.panel._vistas)
        self.assertTrue(any("ffmpeg" in l["texto"] for l in self.ctrl.log_reciente))


class TestQREnElPanel(BaseWeb):
    def test_el_qr_para_el_celular_sale_como_imagen_solo_con_clave(self):
        r, _ = self.pedir("GET", "/api/qr", cookie=False)
        self.assertEqual(r.status, 401)
        r, contenido = self.pedir("GET", "/api/qr")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/svg+xml")
        self.assertTrue(contenido.lstrip().startswith(b"<svg"))


class TestQR(unittest.TestCase):
    def test_el_qr_codifica_la_url_con_la_clave(self):
        texto = web.qr_terminal("http://192.168.1.18:8091/?t=abc")
        self.assertIsNotNone(texto)
        self.assertGreater(texto.count("\n"), 10)


if __name__ == "__main__":
    unittest.main()

"""Panel web: el control remoto en el celular.

Sirve una sola pagina y una API JSON sobre el mismo Controlador que usa la TUI.
Va en un puerto propio (8091) porque tiene que aceptar al celular, mientras el
servidor del video (8090) solo le contesta a la TV.

Acceso: la TUI muestra un QR con http://<ip>:8091/?t=<clave>. Esa visita deja
la clave en una cookie del celular, y la API la exige en cada pedido.
"""

import collections
import hmac
import http.server
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

from . import caster, config, discovery
from .plataformas import PLATAFORMAS, resolver_url

PUERTO_DEFECTO = 8091
COOKIE = "s2t"
# Las acciones exigen este encabezado: un formulario o un <img> de otra pagina
# abierta en el celular no puede agregarlo, asi que no puede disparar pedidos.
ENCABEZADO_PROPIO = "X-Stream2tv"
MAX_CUERPO = 4096
MAX_CANAL = 200
LATIDO_SSE = 15  # segundos entre pings para que el celular no corte la conexion

CARPETA_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# Lista cerrada: nada fuera de esto se sirve, asi no hay forma de pedir otros archivos.
ARCHIVOS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/icono-192.png": ("icono-192.png", "image/png"),
    "/icono-512.png": ("icono-512.png", "image/png"),
    "/sortable.min.js": ("sortable.min.js", "text/javascript; charset=utf-8"),
}

# "Ver aqui": el navegador no reproduce MPEG-TS (Kick, YouTube) y Twitch ya
# manda MP4 fragmentado. ffmpeg pasa todo a MP4 fragmentado sin recodificar,
# que un <video> reproduce solo, sin librerias.
REMUX_MP4 = [
    "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
    # Solo video y audio: el MPEG-TS de Kick trae ademas un stream de metadatos.
    "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
    # El AAC del MPEG-TS viene en ADTS y el MP4 no lo acepta asi.
    "-bsf:a", "aac_adtstoasc",
    "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
]


class _Servidor(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Un celular que cierra la pestana o pierde el wifi a mitad de un pedido
        # no es un error: sin esto, cada corte imprime un traceback en la terminal.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class PanelOcupado(Exception):
    """El puerto del panel ya esta en uso."""


class PanelWeb:
    def __init__(self, controlador, puerto=PUERTO_DEFECTO, host=None):
        self.ctrl = controlador
        self.puerto = puerto
        self.host = host or discovery.ip_lan()
        self._httpd = None
        self._hilo = None
        self._cerrando = threading.Event()
        # Procesos (backend y ffmpeg) que le mandan un stream a un navegador ("Ver aqui").
        self._vistas = set()
        self._lock_vistas = threading.Lock()
        # La pagina se lee una sola vez, al arrancar: si se leyera del disco en
        # cada visita, despues de actualizar stream2tv el celular recibiria la
        # pagina nueva hablando con una app vieja (y botones que "no existen").
        self.archivos = cargar_archivos()

    @property
    def url(self):
        return f"http://{self.host}:{self.puerto}/"

    @property
    def url_con_clave(self):
        return f"{self.url}?t={config.token_web()}"

    def iniciar(self):
        try:
            self._httpd = _Servidor((self.host, self.puerto), self._handler())
        except OSError as e:
            raise PanelOcupado(
                f"No se pudo abrir el panel web en el puerto {self.puerto}: {e}. "
                "Usa otro con --web-port o apagalo con --no-web."
            ) from e
        self._cerrando.clear()
        self._hilo = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._hilo.start()

    def detener(self):
        self._cerrando.set()
        # Como con la TV: un streamlink que quede vivo sigue bajando el stream.
        with self._lock_vistas:
            vistas = list(self._vistas)
        for proc in vistas:
            caster.matar_arbol(proc)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _handler(self):
        panel = self
        ctrl = self.ctrl

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            # ---------- respuestas ----------

            def _enviar(self, codigo, cuerpo=b"", tipo="application/json", extra=()):
                self.send_response(codigo)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for nombre, valor in extra:
                    self.send_header(nombre, valor)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(cuerpo)

            def _json(self, codigo, datos):
                self._enviar(codigo, json.dumps(datos).encode())

            # ---------- acceso ----------

            def _autorizado(self):
                cookies = self.headers.get("Cookie", "")
                for parte in cookies.split(";"):
                    nombre, _, valor = parte.strip().partition("=")
                    if nombre == COOKIE:
                        return clave_valida(valor)
                return False

            # ---------- GET ----------

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/" and "t" in parse_qs(url.query):
                    return self._entrar_con_clave(parse_qs(url.query)["t"][0])
                if url.path in ARCHIVOS:
                    return self._archivo(url.path)
                if url.path == "/api/estado":
                    if not self._autorizado():
                        return self._json(401, {"error": "sin acceso"})
                    datos = ctrl.estado()
                    datos["log"] = list(ctrl.log_reciente)
                    return self._json(200, datos)
                if url.path == "/api/qr":
                    if not self._autorizado():
                        return self._json(401, {"error": "sin acceso"})
                    svg = qr_svg(panel.url_con_clave)
                    if svg is None:
                        return self._json(404, {"error": "falta segno para dibujar el QR"})
                    return self._enviar(200, svg, "image/svg+xml")
                if url.path == "/api/eventos":
                    if not self._autorizado():
                        return self._json(401, {"error": "sin acceso"})
                    return self._eventos()
                if url.path == "/api/ver":
                    if not self._autorizado():
                        return self._json(401, {"error": "sin acceso"})
                    return self._ver({k: v[0] for k, v in parse_qs(url.query).items()})
                self._json(404, {"error": "no existe"})

            def _entrar_con_clave(self, clave):
                extra = [("Location", "/")]
                if clave_valida(clave):
                    # Un ano: escanear el QR una sola vez alcanza.
                    extra.append((
                        "Set-Cookie",
                        f"{COOKIE}={clave}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict",
                    ))
                # Sin clave valida igual se redirige: la pagina muestra como pedir acceso.
                self._enviar(303, extra=extra)

            def _archivo(self, ruta):
                cuerpo = panel.archivos.get(ruta)
                if cuerpo is None:
                    return self._json(404, {"error": "no existe"})
                self._enviar(200, cuerpo, ARCHIVOS[ruta][1])

            def _eventos(self):
                """Server-Sent Events: empuja cada cambio del Controlador al celular."""
                cola = queue.Queue(maxsize=200)

                def recibir(evento, datos):
                    try:
                        cola.put_nowait((evento, datos))
                    except queue.Full:
                        pass  # un celular colgado no debe frenar al resto

                # Sin settimeout en el socket a proposito: con timeout, Chrome no
                # llega a abrir el EventSource y el panel queda "sin conexion".
                # Un celular que desaparece igual se detecta: el latido escribe
                # cada 15 s y esa escritura termina fallando.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                ctrl.suscribir(recibir)
                try:
                    self._evento_sse("estado", ctrl.estado())
                    esperado = 0
                    while not panel._cerrando.is_set():
                        try:
                            evento, datos = cola.get(timeout=1)
                        except queue.Empty:
                            esperado += 1
                            if esperado >= LATIDO_SSE:
                                esperado = 0
                                self.wfile.write(b": latido\n\n")
                                self.wfile.flush()
                            continue
                        self._evento_sse(evento, datos)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    ctrl.desuscribir(recibir)

            def _evento_sse(self, evento, datos):
                self.wfile.write(f"event: {evento}\ndata: {json.dumps(datos)}\n\n".encode())
                self.wfile.flush()

            def _ver(self, datos):
                """El mismo stream que recibe la TV (con los anuncios de Twitch
                filtrados), para verlo en el navegador. Es otra descarga, aparte
                de la de la TV: se puede ver un canal aca y otro en la TV."""
                pedido = self._canal_y_plataforma(datos)
                if pedido is None:
                    return
                canal, plataforma = pedido
                try:
                    comando = caster.comando_stream(
                        resolver_url(canal, plataforma), ctrl.calidad, ctrl.low_latency, ctrl.backend
                    )
                    if shutil.which(REMUX_MP4[0]) is None:
                        raise caster.FaltaBackend("ffmpeg no esta instalado: hace falta para ver en el navegador")
                except caster.FaltaBackend as e:
                    ctrl.log(str(e), "error")
                    return self._json(503, {"error": str(e)})
                backend = subprocess.Popen(
                    comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                remux = subprocess.Popen(
                    REMUX_MP4,
                    stdin=backend.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=True,
                )
                # ffmpeg queda como unico lector: si muere, el backend recibe SIGPIPE.
                backend.stdout.close()
                procesos = (backend, remux)
                errores = collections.deque(maxlen=caster.LINEAS_STDERR)
                lectores = [
                    threading.Thread(target=caster._juntar_stderr, args=(p, errores), daemon=True)
                    for p in procesos
                ]
                for lector in lectores:
                    lector.start()
                with panel._lock_vistas:
                    panel._vistas.update(procesos)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                recibio = fin_normal = False
                # ponytail: sin reintentos, a diferencia de la TV; si el backend
                # se cae, el navegador avisa y se vuelve a tocar "Ver aqui".
                try:
                    while True:
                        chunk = remux.stdout.read(64 * 1024)
                        if not chunk:
                            fin_normal = True
                            break
                        recibio = True
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # el navegador cerro el reproductor
                finally:
                    for proc in procesos:
                        caster.matar_arbol(proc)
                    for lector in lectores:
                        lector.join(timeout=2)
                    remux.stdout.close()
                    for proc in procesos:
                        proc.stderr.close()
                    with panel._lock_vistas:
                        panel._vistas.difference_update(procesos)
                if fin_normal and not panel._cerrando.is_set():
                    motivo = " | ".join(list(errores)[-3:]) or "sin detalle"
                    if recibio:
                        ctrl.log(f"Se corto {canal} en el navegador: {motivo}", "error")
                    else:
                        ctrl.log(f"No se pudo ver {canal} en el navegador: {motivo}", "error")

            # ---------- POST ----------

            def do_POST(self):
                if not self._autorizado():
                    return self._json(401, {"error": "sin acceso"})
                if self.headers.get(ENCABEZADO_PROPIO) != "1":
                    return self._json(403, {"error": "falta el encabezado del panel"})
                try:
                    largo = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._json(400, {"error": "largo invalido"})
                if largo > MAX_CUERPO:
                    return self._json(413, {"error": "pedido demasiado grande"})
                try:
                    datos = json.loads(self.rfile.read(largo) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "JSON invalido"})
                if not isinstance(datos, dict):
                    return self._json(400, {"error": "se esperaba un objeto JSON"})

                acciones = {
                    "/api/reproducir": self._reproducir,
                    "/api/detener": self._detener,
                    "/api/tv": self._tv,
                    "/api/tv/nombre": self._renombrar_tv,
                    "/api/buscar-tvs": self._buscar_tvs,
                    "/api/favoritos": self._favorito,
                    "/api/favoritos/orden": self._orden_favoritos,
                    "/api/agregados": self._agregar,
                    "/api/agregados/quitar": self._quitar_agregado,
                }
                accion = acciones.get(urlparse(self.path).path)
                if accion is None:
                    return self._json(404, {"error": "no existe"})
                accion(datos)

            def _canal_y_plataforma(self, datos):
                canal = str(datos.get("canal", "")).strip()
                plataforma = datos.get("plataforma")
                if not canal or len(canal) > MAX_CANAL:
                    self._json(400, {"error": "escribi un canal"})
                    return None
                if plataforma not in PLATAFORMAS:
                    self._json(400, {"error": "plataforma desconocida"})
                    return None
                return canal, plataforma

            def _reproducir(self, datos):
                pedido = self._canal_y_plataforma(datos)
                if pedido is None:
                    return
                if ctrl.tv_actual is None:
                    return self._json(409, {"error": "no hay TV elegida"})
                # Se reserva aca, en el pedido, para que un Detener inmediato lo vea.
                reserva = ctrl.reservar()
                en_segundo_plano(ctrl.reproducir, *pedido, reserva)
                self._json(202, {"ok": True})

            def _detener(self, datos):
                if not ctrl.anunciar_detencion():
                    return self._json(200, {"ok": False, "motivo": "no hay nada reproduciendose"})
                en_segundo_plano(ctrl.detener, anunciado=True)
                self._json(202, {"ok": True})

            def _tv(self, datos):
                if not ctrl.elegir_tv(str(datos.get("udn", "")), recordar=True):
                    return self._json(404, {"error": "esa TV no esta en la red"})
                self._json(200, {"ok": True})

            def _renombrar_tv(self, datos):
                alias = datos.get("alias", "")
                if not isinstance(alias, str):
                    return self._json(400, {"error": "el nombre tiene que ser texto"})
                if not ctrl.renombrar_tv(str(datos.get("udn", "")), alias):
                    return self._json(404, {"error": "esa TV no esta en la red"})
                self._json(200, {"ok": True})

            def _buscar_tvs(self, datos):
                en_segundo_plano(ctrl.buscar_tvs)
                self._json(202, {"ok": True})

            def _favorito(self, datos):
                pedido = self._canal_y_plataforma(datos)
                if pedido is None:
                    return
                agregado = ctrl.alternar_favorito(*pedido)
                self._json(200, {"ok": True, "agregado": agregado})

            def _agregar(self, datos):
                pedido = self._canal_y_plataforma(datos)
                if pedido is None:
                    return
                ctrl.agregar(*pedido)
                self._json(200, {"ok": True})

            def _quitar_agregado(self, datos):
                pedido = self._canal_y_plataforma(datos)
                if pedido is None:
                    return
                ctrl.quitar_agregado(*pedido)
                self._json(200, {"ok": True})

            def _orden_favoritos(self, datos):
                orden = datos.get("orden")
                if not isinstance(orden, list) or not all(
                    isinstance(f, dict) and isinstance(f.get("canal"), str) and isinstance(f.get("plataforma"), str)
                    for f in orden
                ):
                    return self._json(400, {"error": "se esperaba una lista de favoritos"})
                if not ctrl.reordenar_favoritos(orden):
                    return self._json(409, {"error": "los favoritos cambiaron, recarga el panel"})
                self._json(200, {"ok": True})

        return Handler


def cargar_archivos():
    """{ruta: bytes} de los archivos del panel que existen en disco."""
    archivos = {}
    for ruta, (nombre, _tipo) in ARCHIVOS.items():
        try:
            with open(os.path.join(CARPETA_WEB, nombre), "rb") as f:
                archivos[ruta] = f.read()
        except OSError:
            pass
    return archivos


def qr_terminal(texto):
    """El QR en caracteres de bloque, para imprimir en la terminal; None si falta segno."""
    try:
        import segno
    except ImportError:
        return None
    buffer = io.StringIO()
    segno.make(texto, error="m").terminal(out=buffer, compact=True)
    return buffer.getvalue().rstrip("\n")


def qr_svg(texto):
    """El QR como SVG (bytes), o None si falta segno."""
    try:
        import segno
    except ImportError:
        return None
    buffer = io.BytesIO()
    segno.make(texto, error="m").save(buffer, kind="svg", scale=4, border=2, xmldecl=False)
    return buffer.getvalue()


def clave_valida(clave):
    """Compara en tiempo constante. En bytes: con un str no ASCII (una cookie
    armada a mano) compare_digest lanzaria TypeError en vez de decir que no."""
    return hmac.compare_digest(
        clave.encode("utf-8", "replace"), config.token_web().encode("utf-8")
    )


def en_segundo_plano(funcion, *args, **kwargs):
    """Las acciones lentas (verificar un canal tarda hasta 40 s) no bloquean la respuesta."""
    threading.Thread(target=funcion, args=args, kwargs=kwargs, daemon=True).start()

"""Panel web: el control remoto en el celular.

Sirve una sola pagina y una API JSON sobre el mismo Controlador que usa la TUI.
Va en un puerto propio (8091) porque tiene que aceptar al celular, mientras el
servidor del video (8090) solo le contesta a la TV.

Acceso: la TUI muestra un QR con http://<ip>:8091/?t=<clave>. Esa visita deja
la clave en una cookie del celular, y la API la exige en cada pedido.
"""

import hmac
import http.server
import json
import os
import queue
import threading
from urllib.parse import parse_qs, urlparse

from . import config, discovery
from .plataformas import PLATAFORMAS

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
}


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

    @property
    def url(self):
        return f"http://{self.host}:{self.puerto}/"

    @property
    def url_con_clave(self):
        return f"{self.url}?t={config.token_web()}"

    def iniciar(self):
        try:
            self._httpd = http.server.ThreadingHTTPServer((self.host, self.puerto), self._handler())
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
                if url.path == "/api/eventos":
                    if not self._autorizado():
                        return self._json(401, {"error": "sin acceso"})
                    return self._eventos()
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
                nombre, tipo = ARCHIVOS[ruta]
                try:
                    with open(os.path.join(CARPETA_WEB, nombre), "rb") as f:
                        cuerpo = f.read()
                except OSError:
                    return self._json(404, {"error": "no existe"})
                self._enviar(200, cuerpo, tipo)

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
                if not ctrl.elegir_tv(str(datos.get("udn", ""))):
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

        return Handler


def clave_valida(clave):
    """Compara en tiempo constante. En bytes: con un str no ASCII (una cookie
    armada a mano) compare_digest lanzaria TypeError en vez de decir que no."""
    return hmac.compare_digest(
        clave.encode("utf-8", "replace"), config.token_web().encode("utf-8")
    )


def en_segundo_plano(funcion, *args, **kwargs):
    """Las acciones lentas (verificar un canal tarda hasta 40 s) no bloquean la respuesta."""
    threading.Thread(target=funcion, args=args, kwargs=kwargs, daemon=True).start()

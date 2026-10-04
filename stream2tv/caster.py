"""Servidor HTTP local que alimenta el stream, y control DLNA de la TV."""

import atexit
import collections
import http.server
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

RUTA_STREAM = "/stream.ts"
ESTADOS_REPRODUCIENDO = ("PLAYING", "TRANSITIONING")
# Codigo UPnP "Transition not available": la TV rechaza el Play porque ya esta
# reproduciendo. Es un exito disfrazado de error.
ERROR_TRANSICION_NO_DISPONIBLE = 701

# Todos los servidores activos del proceso. Sirve para garantizar la limpieza
# aunque se salga por un camino inesperado (Ctrl+C, excepcion, cierre de la
# terminal): sin esto quedaban streamlink huerfanos consumiendo red.
_SERVIDORES_VIVOS = set()
_LOCK_VIVOS = threading.Lock()


class PuertoOcupado(Exception):
    """El puerto HTTP local ya esta en uso."""


class FaltaBackend(Exception):
    """Se pidio un backend que no esta instalado."""


def matar_arbol(proc):
    """Mata al proceso y a sus hijos.

    streamlink puede levantar un ffmpeg para muxear video y audio; matando solo
    al padre, ese nieto seguia corriendo y bajando el stream para siempre.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def apagar_todo():
    """Detiene todos los servidores vivos. Se llama tambien al salir del proceso."""
    with _LOCK_VIVOS:
        servidores = list(_SERVIDORES_VIVOS)
    for servidor in servidores:
        try:
            servidor.detener()
        except Exception:
            pass


atexit.register(apagar_todo)


def encontrar_streamlink():
    """Busca streamlink junto al interprete actual (venv) y despues en el PATH."""
    candidato = os.path.join(os.path.dirname(sys.executable), "streamlink")
    if os.path.isfile(candidato) and os.access(candidato, os.X_OK):
        return candidato
    ruta = shutil.which("streamlink")
    if ruta:
        return ruta
    # Excepcion y no SystemExit: esto corre en threads (el handler HTTP, los
    # workers de la TUI), donde un SystemExit mata el hilo sin avisar nada.
    raise FaltaBackend("No se encontro streamlink. Instalalo con: pip install streamlink")


def encontrar_ytdlp():
    """Ruta a yt-dlp, o None si no esta instalado (es opcional)."""
    candidato = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.isfile(candidato) and os.access(candidato, os.X_OK):
        return candidato
    return shutil.which("yt-dlp")


def runtime_js():
    """Runtime de JavaScript para yt-dlp, o None.

    Sin uno, YouTube devuelve 403 en los formatos de alta calidad y solo queda
    el progresivo de 360p.
    """
    for runtime in ("deno", "node", "bun"):
        if shutil.which(runtime):
            return runtime
    return None


def formato_ytdlp(calidad="best"):
    """Selector de formato para yt-dlp, priorizando lo que la TV puede reproducir.

    Se pide H.264 + AAC explicitamente: los formatos de mayor calidad de YouTube
    son VP9 o AV1, que la TV no decodifica y que ademas ffmpeg mete en el
    MPEG-TS como "private data stream" (la TV no los reconoce).
    """
    tope = ""
    if calidad and calidad not in ("best", "worst"):
        digitos = "".join(c for c in calidad if c.isdigit())
        if digitos:
            tope = f"[height<={digitos}]"
    if calidad == "worst":
        return "wv*[vcodec^=avc1]+wa/w"
    return (
        f"bv*[vcodec^=avc1]{tope}+ba[acodec^=mp4a]"
        f"/b[vcodec^=avc1]{tope}"
        f"/bv*{tope}+ba"
        "/b"
    )


def comando_ytdlp(url, calidad="best"):
    """yt-dlp escribiendo a stdout: al mezclar hacia un pipe usa MPEG-TS solo."""
    cmd = [encontrar_ytdlp()]
    runtime = runtime_js()
    if runtime:
        cmd.extend(["--js-runtimes", runtime])
    cmd.extend(["--no-part", "--quiet", "-f", formato_ytdlp(calidad), "-o", "-", url])
    return cmd


def prefiere_ytdlp(url):
    """YouTube se baja con yt-dlp: el plugin de streamlink solo consigue 360p."""
    return ("youtube.com" in url or "youtu.be" in url) and encontrar_ytdlp() is not None


_sesion_streamlink = None
_LOCK_SESION = threading.Lock()


def streamlink_soporta(url):
    """True si streamlink tiene un plugin para esa URL.

    Se consulta con su API en vez de ejecutarlo: es local y no toca la red.
    Ante la duda devuelve True, para que el binario intente y de su propio error.
    """
    global _sesion_streamlink
    try:
        from streamlink import NoPluginError, Streamlink
    except ImportError:
        return True
    try:
        # La TUI puede consultar desde varios workers a la vez.
        with _LOCK_SESION:
            if _sesion_streamlink is None:
                _sesion_streamlink = Streamlink()
            _sesion_streamlink.resolve_url_no_redirect(url)
        return True
    except NoPluginError:
        return False
    except Exception:
        return True


def elegir_backend(url):
    """Decide quien baja el stream.

    streamlink cubre ~134 sitios y yt-dlp ~1750, asi que yt-dlp tambien hace de
    red de contencion cuando streamlink no reconoce la URL.
    """
    hay_ytdlp = encontrar_ytdlp() is not None
    if prefiere_ytdlp(url):
        return "yt-dlp"
    if streamlink_soporta(url):
        return "streamlink"
    return "yt-dlp" if hay_ytdlp else "streamlink"


def comando_stream(url, calidad="best", low_latency=False, backend="auto"):
    """Arma el comando que produce el MPEG-TS, segun el backend elegido."""
    if backend == "auto":
        backend = elegir_backend(url)
    if backend == "yt-dlp":
        if encontrar_ytdlp() is None:
            raise FaltaBackend("yt-dlp no esta instalado (pip install yt-dlp)")
        return comando_ytdlp(url, calidad)
    return comando_streamlink(url, calidad, low_latency)


def comando_streamlink(url, calidad="best", low_latency=False, extra=()):
    cmd = [encontrar_streamlink(), "--stdout"]
    # Cuando el sitio manda video y audio por separado (YouTube en calidades
    # altas, DASH en general), streamlink los muxea con ffmpeg y por defecto
    # saca matroska. La TV recibe el stream anunciado como video/mp2t y no lo
    # reproduce, asi que se fuerza MPEG-TS. En Twitch y Kick no cambia nada:
    # esos streams ya vienen muxeados y no pasan por ffmpeg.
    cmd.extend(["--ffmpeg-fout", "mpegts"])
    if low_latency and "twitch.tv" in url:
        cmd.append("--twitch-low-latency")
    cmd.extend(extra)
    cmd.extend([url, calidad])
    return cmd


def comando_verificacion(url, calidad="best", backend="auto"):
    """Comando que resuelve la URL del stream sin bajarlo, para ver si hay algo."""
    if backend == "auto":
        backend = elegir_backend(url)
    if backend == "yt-dlp":
        ejecutable = encontrar_ytdlp()
        if ejecutable is None:
            raise FaltaBackend("yt-dlp no esta instalado (pip install yt-dlp)")
        cmd = [ejecutable]
        runtime = runtime_js()
        if runtime:
            cmd.extend(["--js-runtimes", runtime])
        cmd.extend(["--quiet", "--no-warnings", "-g", "-f", formato_ytdlp(calidad), url])
        return cmd
    return [encontrar_streamlink(), "--stream-url", url, calidad]


def canal_en_vivo(url, calidad="best", timeout=40, backend="auto"):
    """Chequea si hay stream disponible antes de molestar a la TV.

    Devuelve (True, "") si esta en vivo, o (False, motivo) si no.
    """
    try:
        cmd = comando_verificacion(url, calidad, backend)
    except FaltaBackend as e:
        return False, str(e)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "el sitio tardo demasiado en responder"
    if proc.returncode == 0 and proc.stdout.strip():
        return True, ""
    motivo = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (motivo[-1] if motivo else "el canal no parece estar en vivo")


LINEAS_STDERR = 15
# Si el backend se cae a mitad del stream (un timeout, un 443 de Kick), se
# relanza sobre la misma conexion con la TV. Espera antes de cada intento:
ESPERAS_REINTENTO = (1, 2, 4, 4, 4)
# Una corrida que duro al menos esto se considera sana: la cuenta de intentos
# vuelve a cero, asi un corte cada media hora no agota los reintentos.
CORRIDA_SANA = 60


def _juntar_stderr(proc, destino, avisar=None):
    """Guarda las ultimas lineas de stderr del backend, para explicar un corte.

    Con avisar, ademas se pasa cada linea en vivo (ahi vienen los avisos de
    anuncios filtrados de Twitch).
    """
    try:
        for linea in iter(proc.stderr.readline, b""):
            texto = linea.decode(errors="replace").strip()
            if texto:
                destino.append(texto)
                if avisar is not None:
                    try:
                        avisar(texto)
                    except Exception:
                        pass
    except (OSError, ValueError):
        pass


class Servidor:
    """Sirve el MPEG-TS de streamlink en http://<ip_local>:<puerto>/stream.ts.

    streamlink recien arranca cuando la TV pide el archivo, asi no se consume
    ancho de banda mientras la TV decide conectarse.

    Se atiende un solo stream a la vez: si llega otro pedido (la TV que
    reconecta), se corta el anterior en vez de lanzar otra descarga en paralelo.
    Con cliente_permitido, solo esa IP (la de la TV) puede pedir el stream.
    """

    def __init__(
        self,
        url,
        puerto,
        calidad="best",
        low_latency=False,
        log=print,
        backend="auto",
        host="0.0.0.0",
        cliente_permitido=None,
        al_aviso=None,
        al_estado=None,
    ):
        self.url = url
        self.al_aviso = al_aviso
        # al_estado(tipo, **datos): "reconectando", "recuperado" o "terminado".
        self.al_estado = al_estado
        self.puerto = puerto
        self.host = host
        self.cliente_permitido = cliente_permitido
        self.calidad = calidad
        self.low_latency = low_latency
        self.backend = backend
        # En la TUI los mensajes van a un panel, no a stdout: escribir con print
        # ahi encima romperia el dibujado de la pantalla.
        self.log = log
        self._procesos = []
        # Procesos que cortamos a proposito: su fin no es un error a reportar.
        self._cortados = set()
        # Cada corte (Detener, otra conexion) abre un turno nuevo: un handler
        # de un turno viejo no reintenta aunque su backend se haya caido.
        self._turno = 0
        self._parar = threading.Event()
        self._lock = threading.Lock()
        self._httpd = None
        self._hilo = None

    def _handler(self):
        servidor = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _cabeceras(self, con_largo):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Accept-Ranges", "none")
                if con_largo:
                    self.send_header("Content-Length", "0")
                else:
                    self.send_header("Connection", "close")
                self.end_headers()

            def do_HEAD(self):
                if self.path != RUTA_STREAM:
                    self.send_response(404)
                    self.end_headers()
                    return
                self._cabeceras(con_largo=True)

            def do_GET(self):
                if self.path != RUTA_STREAM:
                    self.send_response(404)
                    self.end_headers()
                    return
                if servidor.cliente_permitido and self.client_address[0] != servidor.cliente_permitido:
                    servidor.log(f"Pedido rechazado desde {self.client_address[0]} (no es la TV).")
                    self.send_response(403)
                    self.end_headers()
                    return

                try:
                    comando = comando_stream(
                        servidor.url, servidor.calidad, servidor.low_latency, servidor.backend
                    )
                except FaltaBackend as e:
                    servidor.log(str(e))
                    self.send_response(500)
                    self.end_headers()
                    return

                servidor._cortar_procesos()
                with servidor._lock:
                    mi_turno = servidor._turno
                backend = os.path.basename(comando[0])
                servidor.log(f"TV conectada, bajando {servidor.url} con {backend}")
                self._cabeceras(con_largo=False)
                intentos = 0
                try:
                    while True:
                        proc, errores, lector = servidor._lanzar(comando)
                        inicio = time.monotonic()
                        recibio = fin_normal = False
                        try:
                            while True:
                                # 188 = tamano de paquete MPEG-TS; se manda en bloques enteros.
                                chunk = proc.stdout.read(188 * 100)
                                if not chunk:
                                    fin_normal = True
                                    break
                                if not recibio:
                                    recibio = True
                                    if intentos:
                                        servidor.log(f"{backend} se recupero: el stream sigue.")
                                        servidor._avisar("recuperado")
                                self.wfile.write(chunk)
                        finally:
                            cortado = proc in servidor._cortados
                            if fin_normal and not cortado:
                                # El stdout puede terminar antes de que el hilo de
                                # stderr lea la ultima linea, justo la que dice por
                                # que se corto. Se lo espera un poco.
                                lector.join(timeout=2)
                            codigo = servidor._soltar(proc, esperar=fin_normal and not cortado)
                        if cortado or not servidor._vigente(mi_turno):
                            return  # lo cortamos nosotros: Detener, otro canal u otra conexion
                        # El backend termino solo: sin su stderr no habia forma de
                        # saber si fue un timeout, un 403 o el fin del stream.
                        motivo = " | ".join(list(errores)[-3:]) or "sin detalle"
                        if time.monotonic() - inicio >= CORRIDA_SANA:
                            intentos = 0
                        total = len(ESPERAS_REINTENTO)
                        if intentos >= total:
                            servidor.log(f"{backend} se corto y no se pudo recuperar (codigo {codigo}): {motivo}")
                            servidor._avisar("terminado", motivo=motivo)
                            return
                        espera = ESPERAS_REINTENTO[intentos]
                        intentos += 1
                        servidor.log(
                            f"{backend} se corto (codigo {codigo}): {motivo}. "
                            f"Reconectando, intento {intentos} de {total}."
                        )
                        servidor._avisar("reconectando", intento=intentos, total=total, motivo=motivo)
                        if servidor._parar.wait(espera) or not servidor._vigente(mi_turno):
                            return
                except (BrokenPipeError, ConnectionResetError):
                    servidor.log("La TV cerro la conexion.")

            def log_message(self, *args):
                pass

        return Handler

    def iniciar(self):
        try:
            self._httpd = http.server.ThreadingHTTPServer(
                (self.host, self.puerto), self._handler()
            )
        except OSError as e:
            raise PuertoOcupado(
                f"No se pudo abrir el puerto {self.puerto}: {e}. "
                "Puede haber quedado un proceso viejo (pkill -f stream2tv) "
                "o podes usar otro puerto con --port."
            ) from e
        self._hilo = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._hilo.start()
        with _LOCK_VIVOS:
            _SERVIDORES_VIVOS.add(self)

    def _lanzar(self, comando):
        proc = subprocess.Popen(
            comando,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            # Grupo de procesos propio, para poder matarlo entero despues.
            start_new_session=True,
        )
        errores = collections.deque(maxlen=LINEAS_STDERR)
        lector = threading.Thread(target=_juntar_stderr, args=(proc, errores, self.al_aviso), daemon=True)
        lector.start()
        with self._lock:
            self._procesos.append(proc)
        return proc, errores, lector

    def _soltar(self, proc, esperar=False):
        """Termina y limpia un backend. Devuelve su codigo de salida si se conoce."""
        codigo = proc.poll()
        if codigo is None and esperar:
            try:
                codigo = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        matar_arbol(proc)
        self._cortados.discard(proc)
        if proc.stderr is not None:
            proc.stderr.close()
        # Se cierra aca y no en matar_arbol: este es el unico hilo que lee el
        # pipe, y cerrarlo mientras lee daria ValueError.
        if proc.stdout is not None:
            proc.stdout.close()
        with self._lock:
            if proc in self._procesos:
                self._procesos.remove(proc)
        return codigo

    def _vigente(self, turno):
        return turno == self._turno and not self._parar.is_set()

    def _avisar(self, tipo, **datos):
        if self.al_estado is None:
            return
        try:
            self.al_estado(tipo, **datos)
        except Exception:
            pass

    def _cortar_procesos(self):
        with self._lock:
            self._turno += 1
            procesos, self._procesos = list(self._procesos), []
            self._cortados.update(procesos)
        for proc in procesos:
            matar_arbol(proc)

    def detener(self):
        self._parar.set()
        self._cortar_procesos()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        with _LOCK_VIVOS:
            _SERVIDORES_VIVOS.discard(self)


def metadata_didl(titulo, stream_url):
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{escape(titulo)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:video/mp2t:*">{escape(stream_url)}</res>'
        "</item></DIDL-Lite>"
    )


def reproducir(tv, stream_url, titulo, intentos=5, espera=2, log=print):
    """Le manda la URL a la TV y se asegura de que arranque.

    Todos los llamados van protegidos: la TV se pone lenta mientras reproduce
    y puede tirar timeouts de red (no solo errores SOAP). Una excepcion sin
    capturar aca mataba el proceso entero, incluido el servidor HTTP, y cortaba
    la transmision.
    """
    av = tv.avtransport
    av.SetAVTransportURI(
        InstanceID=0, CurrentURI=stream_url, CurrentURIMetaData=metadata_didl(titulo, stream_url)
    )

    play_enviado = False
    aviso_mudez_dado = False

    for _ in range(intentos):
        time.sleep(espera)
        estado = estado_tv(tv)

        if estado in ESTADOS_REPRODUCIENDO:
            return True

        if estado is None:
            # La TV no contesta el control. Casi siempre es porque esta ocupada
            # arrancando el video, no porque haya fallado: mandarle un Play aca
            # solo generaba avisos en cadena mientras el stream ya corria bien.
            if not aviso_mudez_dado:
                log("La TV no responde al control mientras arranca el video; esperando.")
                aviso_mudez_dado = True
            continue

        if play_enviado:
            continue

        # La TV contesto y no esta reproduciendo: recien ahi tiene sentido el Play.
        play_enviado = True
        try:
            av.Play(InstanceID=0, Speed="1")
            return True
        except Exception as e:
            if codigo_upnp(e) == ERROR_TRANSICION_NO_DISPONIBLE:
                # 701: la TV rechaza el Play porque ya esta reproduciendo.
                return True
            log(f"La TV rechazo el Play ({e}); puede arrancar igual.")

    return estado_tv(tv) in ESTADOS_REPRODUCIENDO


def codigo_upnp(error):
    """Codigo numerico de un error UPnP, o None si no lo trae.

    upnpclient lanza SOAPError(codigo, descripcion), con el codigo ya como int.
    """
    for arg in getattr(error, "args", ()):
        if isinstance(arg, int):
            return arg
        if isinstance(arg, str) and arg.strip().isdigit():
            return int(arg.strip())
    return None


def estado_tv(tv):
    """Estado de transporte actual, o None si la TV no contesta."""
    try:
        return tv.avtransport.GetTransportInfo(InstanceID=0)["CurrentTransportState"]
    except Exception:
        return None


def detener_tv(tv, log=print):
    try:
        tv.avtransport.Stop(InstanceID=0)
        return True
    except Exception as e:
        log(f"No se pudo enviar Stop (puede que ya estuviera detenida): {e}")
        return False

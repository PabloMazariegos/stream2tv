"""Interfaz de linea de comandos: modo directo y modo interactivo."""

import argparse
import os
import signal
import sys
import threading
import time

from . import __version__, caster, config, discovery
from .plataformas import PLATAFORMAS, etiqueta, resolver_url  # noqa: F401  (reexport)

PUERTO_DEFECTO = 8090
PUERTO_WEB_DEFECTO = 8091
# Cada cuanto se le pregunta a la TV si sigue reproduciendo.
INTERVALO_MONITOR = 10
# Lecturas STOPPED seguidas antes de dar el stream por terminado: una sola
# puede ser un parpadeo mientras la TV re-bufferea.
STOPPED_PARA_CORTAR = 2


def buscar_tvs(mostrar=True):
    if mostrar:
        print("Buscando TVs en la red...", flush=True)
    return config.aplicar_alias(discovery.descubrir())


def imprimir_tvs(tvs, udn_defecto=None):
    for i, tv in enumerate(tvs, 1):
        marca = "  <- por defecto" if udn_defecto and tv.udn == udn_defecto else ""
        extra = f"  [{tv.nombre}]" if tv.alias else ""
        print(f"  {i}) {tv.visible}{extra}  ({tv.host}){marca}")


def obtener_tv(args):
    """Resuelve la TV a usar segun los flags, la config y lo que haya en la red."""
    if args.tv_ip:
        print(f"Conectando directo a {args.tv_ip}...")
        tv = discovery.desde_ip(args.tv_ip)
        if tv is not None:
            config.aplicar_alias([tv])
        if tv is None:
            print(f"No se pudo hablar DLNA con {args.tv_ip}.")
            raise SystemExit(1)
        print(f"TV: {tv.visible}")
        return tv

    udn_defecto, _ = config.tv_por_defecto()
    tvs = buscar_tvs()
    tv = discovery.elegir(tvs, filtro=args.tv, udn_preferido=udn_defecto)
    print(f"TV: {tv.visible}  ({tv.host})")
    return tv


def transmitir(tv, url, args, titulo=None):
    """Levanta el servidor, le manda el stream a la TV y bloquea hasta que termine.

    Termina con Ctrl+C o cuando la TV deja de reproducir (el stream se corto o
    alguien la paro). Devuelve True si llego a transmitir, False si no se pudo
    arrancar.
    """
    if not args.no_check:
        print("Verificando que el canal este en vivo...", flush=True)
        vivo, motivo = caster.canal_en_vivo(url, args.quality, backend=args.backend)
        if not vivo:
            print(f"No hay stream disponible: {motivo}")
            return False

    mi_ip = discovery.ip_local_hacia(tv.host)
    servidor = caster.Servidor(
        url,
        args.port,
        args.quality,
        args.low_latency,
        backend=args.backend,
        host=mi_ip,
        cliente_permitido=tv.host,
    )
    try:
        servidor.iniciar()
    except caster.PuertoOcupado as e:
        print(e)
        return False

    stream_url = f"http://{mi_ip}:{args.port}{caster.RUTA_STREAM}"

    print(f"Enviando {url} a la TV...")
    try:
        arranco = caster.reproducir(tv, stream_url, titulo or url)
    except Exception as e:
        print(f"Error hablando con la TV: {e}")
        servidor.detener()
        return False

    if not arranco:
        print("La TV no confirmo la reproduccion. Puede que igual arranque sola.")

    print("Reproduciendo. Ctrl+C para detener.")
    try:
        esperar_fin(tv)
    except KeyboardInterrupt:
        print("\nDeteniendo...")
    finally:
        caster.detener_tv(tv)
        servidor.detener()
    return True


def esperar_fin(tv):
    """Bloquea mientras la TV siga reproduciendo."""
    stopped_seguidos = 0
    segundos = 0
    while True:
        time.sleep(1)
        segundos += 1
        if segundos % INTERVALO_MONITOR:
            continue
        if caster.estado_tv(tv) == "STOPPED":
            stopped_seguidos += 1
            if stopped_seguidos >= STOPPED_PARA_CORTAR:
                print("La TV detuvo la reproduccion (se corto el stream o alguien la paro).")
                return
        else:
            stopped_seguidos = 0


def servir(args, plataforma_default=None):
    """Arranca el panel web y queda corriendo hasta Ctrl+C.

    Es el modo normal: el panel se abre en el navegador de la laptop y, con el
    QR que se imprime aca, tambien en el celular.
    """
    from .controlador import Controlador
    from .web import PanelOcupado, PanelWeb, qr_terminal

    ctrl = Controlador(args.port, args.quality, args.low_latency, args.backend, plataforma_default)
    panel = PanelWeb(ctrl, args.web_port)
    try:
        panel.iniciar()
    except PanelOcupado as e:
        print(e)
        return 1
    anteriores = salir_con_senales()
    try:
        print(f"stream2tv {__version__}  ·  panel en {panel.url}")
        qr = qr_terminal(panel.url_con_clave)
        if qr:
            print("\nPara el celular, escanea este codigo con la camara:\n")
            print(qr)
        print("\nCtrl+C para salir (tambien detiene la TV).\n")
        # La actividad del panel tambien queda en la terminal.
        ctrl.suscribir(lambda evento, d: print(f"{d['hora']}  {d['texto']}", flush=True) if evento == "log" else None)
        threading.Thread(target=ctrl.buscar_tvs, daemon=True).start()
        ctrl.iniciar_sondeo()
        if not args.no_browser:
            abrir_navegador(panel.url_con_clave)
        while True:
            time.sleep(INTERVALO_MONITOR)
            # Si la TV se detuvo sola, que el panel lo muestre.
            ctrl.verificar_estado()
    except KeyboardInterrupt:
        print("\nSaliendo...")
    finally:
        # Un segundo Ctrl+C no debe cortar la limpieza a mitad: la TV podria
        # quedarse sin recibir el Stop.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        panel.detener()
        ctrl.limpiar()
        for senal, manejador in anteriores.items():
            signal.signal(senal, manejador)
    return 0


def salir_con_senales():
    """Cerrar la terminal (SIGHUP) o un kill (SIGTERM) salen como un Ctrl+C.

    Sin esto el proceso moria sin limpiar: la TV seguia reproduciendo y
    streamlink, que corre en su propio grupo de procesos, quedaba huerfano.
    Tambien se reactiva SIGINT por si llego ignorado (lanzado en segundo plano).
    Devuelve los manejadores anteriores, para restaurarlos al salir.
    """
    def salir(senal, cuadro):
        raise KeyboardInterrupt

    anteriores = {}
    for nombre in ("SIGINT", "SIGTERM", "SIGHUP"):
        senal = getattr(signal, nombre, None)
        if senal is None:
            continue
        try:
            anteriores[senal] = signal.signal(senal, signal.default_int_handler if nombre == "SIGINT" else salir)
        except ValueError:
            pass  # fuera del hilo principal no se pueden cambiar
    return anteriores


def abrir_navegador(url):
    """Abre el panel en el navegador de la compu, si hay uno grafico."""
    import webbrowser

    # Sin escritorio (por SSH, por ejemplo) webbrowser abriria un navegador de
    # texto adentro de esta misma terminal: mejor no abrir nada.
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print(f"Abri el panel en un navegador: {url}")
        return False
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def construir_parser(nombre_programa, plataforma_default):
    descripcion = "Transmite un stream en vivo a una TV DLNA (LG webOS y compatibles)."
    if plataforma_default:
        descripcion = f"Transmite un canal de {PLATAFORMAS[plataforma_default][0]} a una TV DLNA."
    p = argparse.ArgumentParser(prog=nombre_programa, description=descripcion)
    p.add_argument("canal", nargs="?", help="canal o URL. Usa 'stop' para detener la TV.")
    p.add_argument("--tv", metavar="TEXTO", help="elegir TV por nombre, UDN o IP (parcial)")
    p.add_argument("--tv-ip", metavar="IP", help="conectar directo a esa IP, sin descubrimiento")
    p.add_argument("--list-tvs", action="store_true", help="listar las TVs de la red y salir")
    p.add_argument("--set-default", action="store_true", help="guardar la TV elegida como predeterminada")
    p.add_argument("--forget-default", action="store_true", help="olvidar la TV predeterminada")
    p.add_argument("--port", type=int, default=PUERTO_DEFECTO, help=f"puerto HTTP local (default {PUERTO_DEFECTO})")
    p.add_argument("--quality", default="best", help="calidad de streamlink (default: best)")
    p.add_argument("--low-latency", action="store_true", help="baja latencia en Twitch (puede bufferear)")
    p.add_argument("--no-check", action="store_true", help="no verificar si el canal esta en vivo")
    p.add_argument(
        "--backend",
        choices=("auto", "streamlink", "yt-dlp"),
        default="auto",
        help="quien baja el stream (auto: yt-dlp para YouTube, streamlink para el resto)",
    )
    p.add_argument(
        "--web-port", type=int, default=PUERTO_WEB_DEFECTO,
        help=f"puerto del panel web para el celular (default {PUERTO_WEB_DEFECTO})",
    )
    p.add_argument("--no-browser", action="store_true", help="no abrir el panel en el navegador al arrancar")
    p.add_argument("--reset-key", action="store_true", help="renovar la clave del panel (desautoriza a todos los celulares)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(plataforma_default=None, argv=None, nombre_programa=None):
    parser = construir_parser(nombre_programa or "stream2tv", plataforma_default)
    args = parser.parse_args(argv)

    if args.reset_key:
        config.regenerar_token_web()
        print("Clave del panel renovada: los celulares autorizados antes ya no entran.")
        print("Abri stream2tv y volve a escanear el QR con los que si queres usar.")
        return 0

    if args.forget_default:
        config.limpiar_tv_por_defecto()
        print("TV predeterminada olvidada.")
        if not args.canal:
            return 0

    if args.list_tvs:
        udn_defecto, _ = config.tv_por_defecto()
        tvs = buscar_tvs()
        if not tvs:
            print("No se encontro ninguna TV con DLNA en la red.")
            return 1
        print("\nTVs encontradas:")
        imprimir_tvs(tvs, udn_defecto)
        return 0

    if args.canal == "stop":
        tv = obtener_tv(args)
        caster.detener_tv(tv)
        print("Stop enviado.")
        return 0

    if args.set_default and not args.canal:
        tv = obtener_tv(args)
        config.set_tv_por_defecto(tv.udn, tv.nombre)
        print(f"'{tv.visible}' guardada como TV por defecto.")
        return 0

    if args.canal is None:
        return servir(args, plataforma_default)

    tv = obtener_tv(args)
    if args.set_default:
        config.set_tv_por_defecto(tv.udn, tv.nombre)
        print(f"'{tv.visible}' guardada como TV por defecto.")

    url = resolver_url(args.canal, plataforma_default)
    ok = transmitir(tv, url, args, titulo=url)
    return 0 if ok else 1


def main_kick():
    """Entry point de kick2tv: un nombre suelto se toma como canal de Kick."""
    return main(plataforma_default="kick", nombre_programa="kick2tv")


def main_twitch():
    """Entry point de twitch2tv: un nombre suelto se toma como canal de Twitch."""
    return main(plataforma_default="twitch", nombre_programa="twitch2tv")

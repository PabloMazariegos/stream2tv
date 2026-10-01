"""Interfaz de linea de comandos: modo directo y modo interactivo."""

import argparse
import sys
import time

from . import __version__, caster, config, discovery
from .plataformas import PLATAFORMAS, etiqueta, resolver_url  # noqa: F401  (reexport)

PUERTO_DEFECTO = 8090
# Cada cuanto se le pregunta a la TV si sigue reproduciendo.
INTERVALO_MONITOR = 10
# Lecturas STOPPED seguidas antes de dar el stream por terminado: una sola
# puede ser un parpadeo mientras la TV re-bufferea.
STOPPED_PARA_CORTAR = 2


def buscar_tvs(mostrar=True):
    if mostrar:
        print("Buscando TVs en la red...", flush=True)
    return discovery.descubrir()


def imprimir_tvs(tvs, udn_defecto=None):
    for i, tv in enumerate(tvs, 1):
        marca = "  <- por defecto" if udn_defecto and tv.udn == udn_defecto else ""
        print(f"  {i}) {tv.nombre}  ({tv.host}){marca}")


def obtener_tv(args):
    """Resuelve la TV a usar segun los flags, la config y lo que haya en la red."""
    if args.tv_ip:
        print(f"Conectando directo a {args.tv_ip}...")
        tv = discovery.desde_ip(args.tv_ip)
        if tv is None:
            print(f"No se pudo hablar DLNA con {args.tv_ip}.")
            raise SystemExit(1)
        print(f"TV: {tv.nombre}")
        return tv

    udn_defecto, _ = config.tv_por_defecto()
    tvs = buscar_tvs()
    tv = discovery.elegir(tvs, filtro=args.tv, udn_preferido=udn_defecto)
    print(f"TV: {tv.nombre}  ({tv.host})")
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


def pedir_opcion(items, etiqueta, indice_defecto=None):
    """Menu numerado. Devuelve el item elegido, o None si el usuario cancela."""
    sugerencia = f" [{indice_defecto + 1}]" if indice_defecto is not None else ""
    while True:
        try:
            respuesta = input(f"{etiqueta}{sugerencia}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not respuesta and indice_defecto is not None:
            return items[indice_defecto]
        if respuesta.isdigit() and 1 <= int(respuesta) <= len(items):
            return items[int(respuesta) - 1]
        print("Opcion invalida.")


def modo_interactivo(args, plataforma_default=None):
    print(f"=== stream2tv v{__version__} ===\n")

    if args.tv_ip:
        tv = obtener_tv(args)
    else:
        udn_defecto, nombre_defecto = config.tv_por_defecto()
        tvs = buscar_tvs()
        if not tvs:
            print("No se encontro ninguna TV con DLNA en la red.")
            print("Revisa que este encendida y en la misma red, o usa --tv-ip <ip>.")
            return 1
        print("\nTVs disponibles:")
        imprimir_tvs(tvs, udn_defecto)
        indice_defecto = next(
            (i for i, tv in enumerate(tvs) if tv.udn == udn_defecto), 0 if len(tvs) == 1 else None
        )
        tv = pedir_opcion(tvs, "\nElegi una TV", indice_defecto)
        if tv is None:
            return 1
        if tv.udn != udn_defecto:
            try:
                if input(f"Guardar '{tv.nombre}' como TV por defecto? [s/N]: ").strip().lower() in ("s", "si", "y"):
                    config.set_tv_por_defecto(tv.udn, tv.nombre)
                    print("Guardada.")
            except (EOFError, KeyboardInterrupt):
                print()
                return 1

    print(f"\nUsando: {tv.nombre}  ({tv.host})")

    plataforma = plataforma_default
    if plataforma is None:
        claves = list(PLATAFORMAS)
        print("\nPlataforma:")
        for i, clave in enumerate(claves, 1):
            print(f"  {i}) {PLATAFORMAS[clave][0]}")
        plataforma = pedir_opcion(claves, "Elegi plataforma", 0)
        if plataforma is None:
            return 1

    nombre_plataforma = PLATAFORMAS[plataforma][0]
    while True:
        try:
            canal = input(f"\nCanal de {nombre_plataforma} (enter para salir): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not canal:
            return 0
        url = resolver_url(canal, plataforma)
        transmitir(tv, url, args, titulo=f"{nombre_plataforma} - {canal}")
        print("\nStream detenido.")


def lanzar_tui(args, plataforma_default):
    """Abre la TUI. Si textual no esta instalado, cae al modo pregunta-respuesta."""
    try:
        from . import tui
    except ImportError:
        print("La TUI necesita 'textual' (pip install textual). Uso el modo simple.\n")
        return modo_interactivo(args, plataforma_default)
    return tui.lanzar(
        plataforma_inicial=plataforma_default or "kick",
        puerto=args.port,
        calidad=args.quality,
        low_latency=args.low_latency,
        backend=args.backend,
    )


def construir_parser(nombre_programa, plataforma_default):
    descripcion = "Transmite un stream en vivo a una TV DLNA (LG webOS y compatibles)."
    if plataforma_default:
        descripcion = f"Transmite un canal de {PLATAFORMAS[plataforma_default][0]} a una TV DLNA."
    p = argparse.ArgumentParser(prog=nombre_programa, description=descripcion)
    p.add_argument("canal", nargs="?", help="canal o URL. Usa 'stop' para detener la TV.")
    p.add_argument("-i", "--interactive", action="store_true", help="abrir la TUI aunque pases un canal")
    p.add_argument("--wizard", action="store_true", help="modo pregunta-respuesta, sin TUI")
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
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(plataforma_default=None, argv=None, nombre_programa=None):
    parser = construir_parser(nombre_programa or "stream2tv", plataforma_default)
    args = parser.parse_args(argv)

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
        print(f"'{tv.nombre}' guardada como TV por defecto.")
        return 0

    if args.canal is None or args.interactive:
        if not sys.stdin.isatty():
            parser.print_help()
            return 1
        if args.wizard:
            return modo_interactivo(args, plataforma_default)
        return lanzar_tui(args, plataforma_default)

    tv = obtener_tv(args)
    if args.set_default:
        config.set_tv_por_defecto(tv.udn, tv.nombre)
        print(f"'{tv.nombre}' guardada como TV por defecto.")

    url = resolver_url(args.canal, plataforma_default)
    ok = transmitir(tv, url, args, titulo=url)
    return 0 if ok else 1


def main_kick():
    """Entry point de kick2tv: un nombre suelto se toma como canal de Kick."""
    return main(plataforma_default="kick", nombre_programa="kick2tv")


def main_twitch():
    """Entry point de twitch2tv: un nombre suelto se toma como canal de Twitch."""
    return main(plataforma_default="twitch", nombre_programa="twitch2tv")

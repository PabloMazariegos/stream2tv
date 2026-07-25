"""Descubrimiento de TVs DLNA en la red local via SSDP."""

import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import upnpclient

SSDP_DIRECCION = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
SSDP_MX = 3
SSDP_TIMEOUT = 5


@dataclass
class TV:
    """Un MediaRenderer descubierto, con lo minimo para identificarlo y usarlo."""

    udn: str
    nombre: str
    location: str
    device: object

    @property
    def host(self):
        """IP de la TV, extraida de la URL de descripcion."""
        return self.location.split("/")[2].split(":")[0]

    @property
    def avtransport(self):
        return self.device["AVTransport"]

    def coincide(self, texto):
        """True si el texto (case-insensitive) matchea el nombre, el UDN o la IP."""
        t = texto.lower()
        return t in self.nombre.lower() or t in self.udn.lower() or t == self.host


def _buscar_locations(timeout=SSDP_TIMEOUT):
    """Manda un M-SEARCH y junta las URLs de descripcion que respondan."""
    mensaje = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_DIRECCION[0]}:{SSDP_DIRECCION[1]}",
        'MAN: "ssdp:discover"',
        f"MX: {SSDP_MX}",
        f"ST: {SSDP_ST}",
        "", "",
    ])
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 0))
    s.settimeout(timeout)
    # Se manda dos veces: SSDP es UDP y el primer paquete se pierde seguido.
    for _ in range(2):
        try:
            s.sendto(mensaje.encode(), SSDP_DIRECCION)
        except OSError:
            pass

    locations = set()
    try:
        while True:
            data, _addr = s.recvfrom(4096)
            texto = data.decode(errors="replace")
            for linea in texto.split("\r\n"):
                if linea.lower().startswith("location:"):
                    locations.add(linea.split(":", 1)[1].strip())
    except socket.timeout:
        pass
    finally:
        s.close()
    return locations


def _construir(location):
    """Baja la descripcion del dispositivo. Devuelve una TV o None."""
    try:
        d = upnpclient.Device(location)
        if "AVTransport" not in d.service_map:
            return None
        udn = getattr(d, "udn", "") or location
        nombre = getattr(d, "friendly_name", "") or "(sin nombre)"
        return TV(udn=udn, nombre=nombre, location=location, device=d)
    except Exception:
        return None


def descubrir(timeout=SSDP_TIMEOUT):
    """Devuelve todas las TVs con AVTransport, ordenadas por nombre.

    Las descripciones se bajan en paralelo: con varias TVs, hacerlo en serie
    suma varios segundos porque cada una responde lento.
    """
    locations = _buscar_locations(timeout)
    if not locations:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(locations))) as pool:
        encontradas = list(pool.map(_construir, locations))
    tvs = [tv for tv in encontradas if tv is not None]
    # Una misma TV puede anunciarse por varias interfaces; deduplicar por UDN.
    unicas = {}
    for tv in tvs:
        unicas.setdefault(tv.udn, tv)
    return sorted(unicas.values(), key=lambda tv: tv.nombre)


def desde_ip(ip, puertos=(1700, 8080, 9197, 49152, 49153, 55000)):
    """Intenta armar una TV apuntando directo a una IP, sin SSDP.

    Sirve cuando el router filtra multicast entre bandas y el descubrimiento
    no encuentra nada, pero la TV si es alcanzable.
    """
    rutas = ("/", "/description.xml", "/dmr.xml", "/MediaRenderer.xml", "/upnp/desc.xml")
    for puerto in puertos:
        for ruta in rutas:
            tv = _construir(f"http://{ip}:{puerto}{ruta}")
            if tv is not None:
                return tv
    return None


def ip_local_hacia(destino):
    """IP de esta maquina en la interfaz que alcanza a la TV."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((destino, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def elegir(tvs, filtro=None, udn_preferido=None, interactivo=None):
    """Decide con que TV quedarse.

    Orden: filtro explicito > TV por defecto guardada > unica disponible >
    menu interactivo. Devuelve una TV o lanza SystemExit con un mensaje util.
    """
    if not tvs:
        print("No se encontro ninguna TV con DLNA (MediaRenderer) en la red.")
        print("Revisa que la TV este encendida y en la misma red/banda de wifi.")
        print("Si la red filtra multicast, proba: --tv-ip <ip_de_la_tv>")
        raise SystemExit(1)

    if filtro:
        candidatas = [tv for tv in tvs if tv.coincide(filtro)]
        if not candidatas:
            print(f"Ninguna TV coincide con '{filtro}'. Disponibles:")
            for tv in tvs:
                print(f"  - {tv.nombre}  ({tv.host})")
            raise SystemExit(1)
        if len(candidatas) > 1:
            print(f"'{filtro}' es ambiguo, coincide con varias TVs:")
            for tv in candidatas:
                print(f"  - {tv.nombre}  ({tv.host})")
            raise SystemExit(1)
        return candidatas[0]

    if udn_preferido:
        for tv in tvs:
            if tv.udn == udn_preferido:
                return tv
        # La TV guardada no esta en la red: seguimos con el resto de la logica.

    if len(tvs) == 1:
        return tvs[0]

    if interactivo is None:
        interactivo = sys.stdin.isatty()
    if not interactivo:
        print("Hay varias TVs en la red y no se indico cual usar:")
        for tv in tvs:
            print(f"  - {tv.nombre}  ({tv.host})")
        print("Usa --tv <nombre> o guarda una por defecto con --set-default.")
        raise SystemExit(1)

    print("Se encontraron varias TVs:")
    for i, tv in enumerate(tvs, 1):
        print(f"  {i}) {tv.nombre}  ({tv.host})")
    while True:
        try:
            respuesta = input(f"Cual usar? [1-{len(tvs)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if respuesta.isdigit() and 1 <= int(respuesta) <= len(tvs):
            return tvs[int(respuesta) - 1]
        print("Opcion invalida.")

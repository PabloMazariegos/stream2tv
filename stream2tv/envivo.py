"""Quien de los favoritos esta transmitiendo ahora.

Cada plataforma se consulta por su via mas liviana:
- Twitch: su API GraphQL publica, todos los canales en un solo pedido.
- Kick: su API de canal, con el adaptador TLS del plugin de streamlink (sin
  el, Cloudflare corta el pedido).
- YouTube: la pagina /live del canal; no hay API sin clave.

Un canal que no se pudo consultar queda fuera del resultado ("no se sabe"), que
no es lo mismo que offline: la UI no lo marca en gris por un error de red.
"""

import html
import re
from concurrent.futures import ThreadPoolExecutor

import requests

TIMEOUT = 8
# El mismo Client-ID publico que usa la web de Twitch (y streamlink).
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_QUERY = "query($l:[String!]){users(logins:$l){login stream{viewersCount title game{name}}}}"
TWITCH_MAX_POR_PEDIDO = 100
# Twitch publica la miniatura de cada canal en vivo en una direccion fija.
TWITCH_MINIATURA = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{}-640x360.jpg"
YOUTUBE_MINIATURA = "https://i.ytimg.com/vi/{}/hqdefault_live.jpg"
KICK_API = "https://kick.com/api/v2/channels/{}/livestream"
YOUTUBE_LIVE = "https://www.youtube.com/@{}/live"
NAVEGADOR = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def clave(plataforma, canal):
    """Como se identifica un favorito en el resultado (y en el JSON del panel)."""
    return f"{plataforma}:{canal.strip().lower()}"


def ordenar(favoritos, en_vivo):
    """Pares (favorito, estado o None), los que estan en vivo primero.

    Dentro de cada grupo se respeta el orden en que se agregaron.
    """
    pares = [(fav, en_vivo.get(clave(fav["plataforma"], fav["canal"]))) for fav in favoritos]
    return [p for p in pares if p[1] and p[1]["vivo"]] + [p for p in pares if not (p[1] and p[1]["vivo"])]


def espectadores_corto(n):
    """235411 -> '235 mil', 11437 -> '11,4 mil', 812 -> '812'."""
    if n is None:
        return ""
    if n < 1000:
        return str(n)
    if n < 100_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " mil"
    if n < 1_000_000:
        return f"{n // 1000} mil"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " M"


def _estado(vivo, espectadores=None, titulo=None, categoria=None, miniatura=None):
    return {
        "vivo": vivo,
        "espectadores": espectadores,
        "titulo": titulo,
        "categoria": categoria,
        "miniatura": miniatura,
    }


# Links de canal que se reconocen, para favoritos guardados como URL completa.
_LINKS = [
    ("twitch", re.compile(r"^https?://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]+)/?(?:[?#].*)?$")),
    ("kick", re.compile(r"^https?://(?:www\.)?kick\.com/([A-Za-z0-9_-]+)/?(?:[?#].*)?$")),
    ("youtube", re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/@([A-Za-z0-9_.-]+)(?:/live)?/?(?:[?#].*)?$")),
]


def canal_consultable(canal, plataforma):
    """(plataforma, nombre) a consultar, o None si no se sabe que canal es.

    Un nombre suelto se consulta tal cual; un link de canal conocido
    (kick.com/x, twitch.tv/x, youtube.com/@x) se traduce a su nombre.
    """
    canal = canal.strip()
    if not canal:
        return None
    if not canal.startswith(("http://", "https://")):
        return plataforma, canal.lower()
    for sitio, patron in _LINKS:
        encontrado = patron.match(canal)
        if encontrado:
            return sitio, encontrado.group(1).lower()
    return None


def consultar(favoritos):
    """{clave: estado} para los favoritos que se pudieron consultar.

    La clave usa el canal tal como esta guardado (link incluido), que es como
    lo busca la UI.
    """
    por_plataforma = {}
    destinos = {}  # clave consultada -> claves de los favoritos que la comparten
    for fav in favoritos:
        consulta = canal_consultable(fav.get("canal", ""), fav.get("plataforma"))
        if consulta is None:
            continue
        sitio, nombre = consulta
        por_plataforma.setdefault(sitio, set()).add(nombre)
        destinos.setdefault(clave(sitio, nombre), set()).add(clave(fav["plataforma"], fav["canal"]))

    crudo = {}
    tareas = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        if por_plataforma.get("twitch"):
            tareas.append(pool.submit(_twitch, sorted(por_plataforma["twitch"])))
        sesion_kick = _sesion_kick() if por_plataforma.get("kick") else None
        for canal in sorted(por_plataforma.get("kick", ())):
            tareas.append(pool.submit(_kick, sesion_kick, canal))
        for canal in sorted(por_plataforma.get("youtube", ())):
            tareas.append(pool.submit(_youtube, canal))
        for tarea in tareas:
            try:
                crudo.update(tarea.result())
            except Exception:
                # Una plataforma caida no tapa a las otras.
                pass
    resultado = {}
    for consultada, estado in crudo.items():
        for destino in destinos.get(consultada, ()):
            resultado[destino] = estado
    return resultado


# ---------- Twitch ----------

def _twitch(canales):
    resultado = {}
    for i in range(0, len(canales), TWITCH_MAX_POR_PEDIDO):
        lote = canales[i:i + TWITCH_MAX_POR_PEDIDO]
        r = requests.post(
            TWITCH_GQL,
            json={"query": TWITCH_QUERY, "variables": {"l": lote}},
            headers={"Client-ID": TWITCH_CLIENT_ID},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for usuario in (r.json().get("data") or {}).get("users") or []:
            if not usuario:
                continue  # el canal no existe
            stream = usuario.get("stream")
            if stream:
                juego = (stream.get("game") or {}).get("name")
                resultado[clave("twitch", usuario["login"])] = _estado(
                    True, stream.get("viewersCount"), stream.get("title"), juego,
                    TWITCH_MINIATURA.format(usuario["login"].lower()),
                )
            else:
                resultado[clave("twitch", usuario["login"])] = _estado(False)
    return resultado


# ---------- Kick ----------

def _sesion_kick():
    sesion = requests.Session()
    try:
        from streamlink.plugins.kick import KickAdapter

        sesion.mount("https://kick.com/", KickAdapter())
    except Exception:
        pass  # sin el adaptador puede andar igual; si Cloudflare corta, queda "no se sabe"
    return sesion


def _kick(sesion, canal):
    # En Kick los slugs usan guiones donde el nombre visible tiene guion bajo.
    slug = canal.replace("_", "-")
    r = sesion.get(
        KICK_API.format(slug),
        headers={"User-Agent": NAVEGADOR, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    datos = r.json().get("data")
    if not datos:
        return {clave("kick", canal): _estado(False)}
    categorias = datos.get("categories") or []
    categoria = (datos.get("category") or {}).get("name") or (categorias[0].get("name") if categorias else None)
    miniatura = (datos.get("thumbnail") or {}).get("src")
    return {clave("kick", canal): _estado(True, datos.get("viewers"), datos.get("session_title"), categoria, miniatura)}


# ---------- YouTube ----------

_YT_TITULO = re.compile(r'<meta name="title" content="([^"]*)"')
_YT_ESPECTADORES = re.compile(r'"originalViewCount":"(\d+)"')
_YT_VIDEO = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"')


def _youtube(canal):
    r = requests.get(
        YOUTUBE_LIVE.format(canal.lstrip("@")),
        headers={"User-Agent": NAVEGADOR, "Accept-Language": "es"},
        cookies={"CONSENT": "YES+"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    if '"isLiveNow":true' not in r.text:
        return {clave("youtube", canal): _estado(False)}
    titulo = _YT_TITULO.search(r.text)
    espectadores = _YT_ESPECTADORES.search(r.text)
    video = _YT_VIDEO.search(r.text)
    return {
        clave("youtube", canal): _estado(
            True,
            int(espectadores.group(1)) if espectadores else None,
            html.unescape(titulo.group(1)) if titulo else None,
            None,
            YOUTUBE_MINIATURA.format(video.group(1)) if video else None,
        )
    }

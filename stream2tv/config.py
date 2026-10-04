"""Preferencias persistentes: cual TV usar por defecto."""

import json
import os
import secrets
import tempfile

RUTA_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "stream2tv",
    "config.json",
)


def cargar():
    try:
        with open(RUTA_CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Se aparta el archivo roto en vez de pisarlo con el proximo guardado:
        # ahi adentro pueden estar los favoritos, recuperables a mano.
        try:
            os.replace(RUTA_CONFIG, RUTA_CONFIG + ".corrupto")
        except OSError:
            pass
        return {}


def guardar(datos):
    """Escribe a un temporal y lo renombra: un corte a mitad no deja el JSON roto."""
    directorio = os.path.dirname(RUTA_CONFIG)
    os.makedirs(directorio, exist_ok=True)
    fd, temporal = tempfile.mkstemp(dir=directorio, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(datos, f, indent=2)
        os.replace(temporal, RUTA_CONFIG)
    except BaseException:
        if os.path.exists(temporal):
            os.remove(temporal)
        raise


def tv_por_defecto():
    """Devuelve (udn, nombre) de la TV preferida, o (None, None)."""
    datos = cargar()
    tv = datos.get("tv_por_defecto") or {}
    return tv.get("udn"), tv.get("nombre")


def set_tv_por_defecto(udn, nombre):
    datos = cargar()
    datos["tv_por_defecto"] = {"udn": udn, "nombre": nombre}
    guardar(datos)


def limpiar_tv_por_defecto():
    datos = cargar()
    datos.pop("tv_por_defecto", None)
    guardar(datos)


def token_web():
    """Clave del panel web; se crea la primera vez. 128 bits al azar."""
    datos = cargar()
    token = datos.get("token_web")
    if not token:
        token = secrets.token_urlsafe(16)
        datos["token_web"] = token
        guardar(datos)
    return token


def regenerar_token_web():
    """Clave nueva: los celulares autorizados con la anterior pierden el acceso."""
    datos = cargar()
    datos["token_web"] = secrets.token_urlsafe(16)
    guardar(datos)
    return datos["token_web"]


def favoritos():
    """Lista de {'canal': str, 'plataforma': str}, en el orden en que se agregaron."""
    return cargar().get("favoritos", [])


def es_favorito(canal, plataforma):
    return any(
        f["canal"].lower() == canal.lower() and f["plataforma"] == plataforma
        for f in favoritos()
    )


def alternar_favorito(canal, plataforma):
    """Agrega o quita un favorito. Devuelve True si quedo agregado."""
    datos = cargar()
    lista = datos.get("favoritos", [])
    restantes = [
        f for f in lista
        if not (f["canal"].lower() == canal.lower() and f["plataforma"] == plataforma)
    ]
    agregado = len(restantes) == len(lista)
    if agregado:
        restantes.append({"canal": canal, "plataforma": plataforma})
    datos["favoritos"] = restantes
    guardar(datos)
    return agregado

"""Preferencias persistentes: cual TV usar por defecto."""

import json
import os

RUTA_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "stream2tv",
    "config.json",
)


def cargar():
    try:
        with open(RUTA_CONFIG) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar(datos):
    os.makedirs(os.path.dirname(RUTA_CONFIG), exist_ok=True)
    with open(RUTA_CONFIG, "w") as f:
        json.dump(datos, f, indent=2)


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

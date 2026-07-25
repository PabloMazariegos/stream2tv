"""Plataformas conocidas y como se arma la URL a partir de lo que se escribe.

Cualquier URL completa se pasa tal cual a streamlink, que soporta muchos mas
sitios que estos; estos son solo los que aceptan un nombre de canal pelado.
"""

# clave -> (etiqueta visible, prefijo corto, plantilla de URL)
PLATAFORMAS = {
    "kick": ("Kick", "ki", "https://kick.com/{}"),
    "twitch": ("Twitch", "tw", "https://twitch.tv/{}"),
    "youtube": ("YouTube", "yt", "https://www.youtube.com/@{}/live"),
}

ETIQUETAS = {clave: datos[0] for clave, datos in PLATAFORMAS.items()}
POR_PREFIJO = {datos[1]: clave for clave, datos in PLATAFORMAS.items()}


def etiqueta(plataforma):
    return ETIQUETAS.get(plataforma, plataforma)


def opciones_select():
    """Pares (etiqueta, valor) para el Select de la TUI."""
    return [(datos[0], clave) for clave, datos in PLATAFORMAS.items()]


def resolver_url(entrada, plataforma=None):
    """Convierte 'canal', 'tw:canal' o una URL completa en una URL usable."""
    entrada = entrada.strip()
    if entrada.startswith("http://") or entrada.startswith("https://"):
        return entrada
    if ":" in entrada:
        prefijo, resto = entrada.split(":", 1)
        clave = POR_PREFIJO.get(prefijo.lower())
        if clave and resto:
            return PLATAFORMAS[clave][2].format(resto)
    plantilla = PLATAFORMAS.get(plataforma or "kick", PLATAFORMAS["kick"])[2]
    return plantilla.format(entrada)

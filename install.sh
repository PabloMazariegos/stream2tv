#!/usr/bin/env bash
# Instala (o actualiza) stream2tv, kick2tv y twitch2tv en un venv propio, con
# comandos en ~/.local/bin. Volver a correrlo actualiza todas las dependencias:
# Kick y YouTube cambian seguido y un streamlink/yt-dlp viejo deja de andar.
set -euo pipefail

PROYECTO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${STREAM2TV_VENV:-$HOME/.local/share/stream2tv-venv}"
VENV_VIEJO="$HOME/.local/share/kick-cast-venv"
BIN="$HOME/.local/bin"

echo "Instalando/actualizando dependencias en $VENV ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# Editable: los comandos usan el codigo del repo, asi un git pull alcanza.
"$VENV/bin/pip" install --quiet --upgrade --upgrade-strategy eager -e "$PROYECTO"

mkdir -p "$BIN"
for cmd in stream2tv kick2tv twitch2tv; do
  # Se reemplaza cualquier instalacion previa (wrappers viejos incluidos).
  rm -f "$BIN/$cmd"
  ln -s "$VENV/bin/$cmd" "$BIN/$cmd"
  echo "  instalado: $BIN/$cmd"
done

echo
"$VENV/bin/streamlink" --version
echo "yt-dlp $("$VENV/bin/yt-dlp" --version)"

echo
echo "Listo. Proba:"
echo "  stream2tv              # abre el panel en el navegador"
echo "  stream2tv --list-tvs   # ver las TVs de la red"
if [ -d "$VENV_VIEJO" ] && [ "$VENV_VIEJO" != "$VENV" ]; then
  echo
  echo "Quedo el venv de la version anterior, ya no se usa. Podes borrarlo con:"
  echo "  rm -rf $VENV_VIEJO"
fi
if ! echo ":$PATH:" | grep -q ":$BIN:"; then
  echo
  echo "Ojo: $BIN no esta en tu PATH. Agregalo a tu ~/.bashrc:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

#!/usr/bin/env bash
# Instala stream2tv, kick2tv y twitch2tv en un venv propio, con comandos en ~/.local/bin
set -euo pipefail

PROYECTO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${STREAM2TV_VENV:-$HOME/.local/share/kick-cast-venv}"
BIN="$HOME/.local/bin"

echo "Instalando dependencias en $VENV ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROYECTO/requirements.txt"

mkdir -p "$BIN"
for cmd in stream2tv kick2tv twitch2tv; do
  # Se reemplaza cualquier instalacion previa (incluido el symlink viejo).
  rm -f "$BIN/$cmd"
  {
    echo "#!$VENV/bin/python3"
    echo "import runpy, sys"
    echo "sys.argv[0] = \"$cmd\""
    echo "runpy.run_path(\"$PROYECTO/bin/$cmd\", run_name=\"__main__\")"
  } > "$BIN/$cmd"
  chmod +x "$BIN/$cmd"
  echo "  instalado: $BIN/$cmd"
done

echo
echo "Listo. Proba:"
echo "  twitch2tv              # modo interactivo"
echo "  twitch2tv --list-tvs   # ver las TVs de la red"
if ! echo ":$PATH:" | grep -q ":$BIN:"; then
  echo
  echo "Ojo: $BIN no esta en tu PATH. Agregalo a tu ~/.bashrc:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Manual de stream2tv

Guía completa de instalación y uso. Si querés saber cómo funciona por dentro,
mirá [`COMO-FUNCIONA.md`](COMO-FUNCIONA.md).

---

## 1. ¿Esto me sirve?

Sirve si querés ver streams en vivo (Twitch, Kick, YouTube y ~1750 sitios más) en
una TV que **no puede abrirlos por su cuenta** — típicamente una smart TV vieja o
con poca RAM — y no tenés Chromecast.

Tu TV tiene que soportar **DLNA**. Casi todas las smart TV de los últimos 12 años lo
traen, a veces con otro nombre en el menú:

| Marca | Cómo aparece en el menú |
|---|---|
| LG | *Screen Share* / *SmartShare* / *Content Share* |
| Samsung | *AllShare* / *DLNA* |
| Sony | *Home Network* / *Renderer* |
| Philips, Panasonic, TCL, Hisense | *DLNA* / *Media Renderer* |

La forma rápida de saberlo: instalá esto y corré `stream2tv --list-tvs`. Si tu TV
aparece, funciona.

**Importante:** la computadora es la que baja el stream y se lo pasa a la TV, así que
tiene que quedarse prendida y en la misma red mientras mirás.

---

## 2. Requisitos

- **Linux o macOS.** En Windows no funciona directo; en WSL tampoco, porque el
  descubrimiento por multicast no sale del subsistema a la red real.
- **Python 3.8+** con el módulo `venv`.
  En Debian/Ubuntu: `sudo apt install python3-venv`
- **La compu y la TV en la misma red.** Misma banda de wifi, sin red de invitados.
- **Opcional pero recomendado: `node`, `deno` o `bun`.** Sin uno de estos, YouTube
  queda limitado a 360p. Los demás sitios no lo necesitan.

---

## 3. Instalación

```bash
git clone https://github.com/<usuario>/stream-limitless.git
cd stream-limitless
./install.sh
```

El script crea un entorno virtual en `~/.local/share/stream2tv-venv`, instala las
dependencias e instala tres comandos en `~/.local/bin`:

| Comando | Qué hace |
|---|---|
| `stream2tv` | Genérico: acepta URLs de cualquier plataforma |
| `twitch2tv` | Igual, pero un nombre suelto se interpreta como canal de Twitch |
| `kick2tv` | Igual, con Kick |

Si al terminar te avisa que `~/.local/bin` no está en tu `PATH`, agregalo:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

**No muevas la carpeta del proyecto después de instalar** — los comandos apuntan a
donde estaba. Si la movés, volvé a correr `./install.sh`.

**Para actualizar**, volvé a correr `./install.sh`: actualiza streamlink, yt-dlp y el
resto de las dependencias. Conviene hacerlo cuando Kick, Twitch o YouTube dejan de
andar de golpe, porque suelen cambiar cosas que rompen las versiones viejas.

Verificá que quedó bien:

```bash
stream2tv --version
stream2tv --list-tvs
```

---

## 4. Uso

### La forma normal: el panel

```bash
stream2tv
```

Se abre el panel en el navegador de la compu, ya autorizado, y la terminal muestra la
dirección y un código QR para el celular. Dejá esa terminal abierta mientras lo usás:
`Ctrl+C` (o cerrarla) apaga todo y detiene la TV.

El panel es tu plataforma de streaming: mirás los streams ahí mismo y, cuando
querés, los mandás a la TV.

- **El reproductor** (a la izquierda en la compu, arriba en el celular): tocá un
  stream de la lista y se ve ahí, el mismo que baja la laptop para la TV (con los
  anuncios de Twitch filtrados). Necesita `ffmpeg` instalado en la laptop. Abajo,
  **Enviar a <TV>** lo pone en la TV y la ☆ lo guarda en favoritos.
- **El buscador de arriba**: pegás un link de cualquier sitio, o escribís un canal y
  elegís la plataforma con el logo de la derecha. Se ve en el reproductor, no hace
  falta tener una TV elegida.
- **Favoritos**: los que están transmitiendo aparecen arriba, con miniatura, juego,
  título y espectadores; los que no, abajo. Se consulta a Twitch, Kick y YouTube cada
  90 segundos. **Editar** sirve para quitarlos.
- **Agregados**: los links y canales que abriste y no son favoritos (los últimos 12).
  La ☆ los pasa a favoritos y la ✕ los quita de la lista.
- **Enviar a** (el botón de la TV, arriba a la derecha): las TVs de la red. Tocás una
  para elegirla (queda como la de siempre) y el lápiz para ponerle un nombre. Si no hay
  ninguna, el panel sigue funcionando para ver aquí.
- **En la TV**: debajo del reproductor, lo que está sonando en la TV, con **Detener**.
- **Actividad**: todo lo que pasó (también se ve en la terminal).

Cada fila de la lista tiene también el botón de la TV para mandarla directo. Podés
cambiar de canal en la TV sin detener: el anterior se corta solo. Si Kick, Twitch o
YouTube dejan de responder un momento, el stream de la TV se reconecta solo
("Reconectando…"). Ver aquí es una descarga aparte: podés ver un canal acá y otro en
la TV.

En la compu, además:

- **Atajos de teclado** (`?` muestra la lista): `/` buscar, `1`–`9` ver el favorito
  en vivo de esa posición, `E` enviar a la TV lo que estás viendo, `S` detener la TV,
  `T` cambiar a la otra TV.
- **Pegar y listo**: copiá un link de Twitch, Kick o YouTube y apretá `Ctrl+V` en
  cualquier parte del panel. Se ve en el reproductor.
- **Reordenar**: arrastrá los favoritos para cambiar el orden.

### En el celular

1. Escaneá con la cámara el QR que muestra la terminal al arrancar, o el del botón
   **Celular** del panel en la compu.
2. Listo: el celular queda autorizado por un año. Desde el menú del navegador podés
   agregarlo a la pantalla de inicio y abre como una app.

El QR lleva una clave: sin ella, alguien conectado a tu wifi no puede usar el panel.
Para desautorizar a todos los celulares, corré `stream2tv --reset-key` y volvé a
escanear el QR con los que sí querés. El panel solo funciona dentro de tu red de casa. Si el celular no llega a la
dirección, revisá el firewall de la laptop (ver *Problemas frecuentes*).

### Modo directo

```bash
twitch2tv xqc                          # canal de Twitch
kick2tv lacobraaa                      # canal de Kick
stream2tv https://twitch.tv/xqc        # URL completa, cualquier plataforma
twitch2tv tw:xqc                       # prefijo explícito: tw: / ki: / yt:
twitch2tv stop                         # detener la TV
```

Queda corriendo en la terminal hasta que le des `Ctrl+C` o hasta que la TV deje de reproducir.

### Otros sitios

Pegá la URL completa:

```bash
stream2tv "https://www.youtube.com/watch?v=..."
stream2tv yt:algun_canal        # atajo a youtube.com/@canal/live
```

El selector de plataforma **solo aplica cuando escribís un nombre pelado**. Si pegás
una URL completa se respeta tal cual.

## 5. Elegir la TV

```bash
twitch2tv --list-tvs                   # ver qué hay en la red
twitch2tv xqc --tv webos               # por nombre parcial, UDN o IP
twitch2tv xqc --tv-ip 192.168.0.50     # directo, sin descubrimiento
twitch2tv --set-default                # guardar una como predeterminada
twitch2tv --forget-default             # olvidarla
```

La predeterminada se guarda en `~/.config/stream2tv/config.json` **por UDN, no por
IP**, así que sigue funcionando aunque el router le cambie la dirección.

Si `--tv` coincide con más de una TV, el comando falla y te muestra las opciones en
vez de adivinar.

---

### Nombres propios para las TVs

En vez de `65" Crystal UHD` o `Kodi (Mi Box)` podés llamarlas "Sala" y "Cuarto":
tocando el lápiz al lado de la TV en el panel (se abre una hoja con sugerencias como
Sala, Cuarto o Comedor). El nombre se
guarda atado al identificador de la TV (no a su IP), así sobrevive a que el router le
cambie la dirección. Vacío vuelve al nombre de fábrica. También sirve para elegir:
`stream2tv --tv sala`.

## 6. Todas las opciones

| Flag | Para qué |
|---|---|
| `--port N` | Puerto HTTP local del video (default `8090`) |
| `--web-port N` | Puerto del panel web para el celular (default `8091`) |
| `--no-browser` | No abrir el panel en el navegador al arrancar |
| `--reset-key` | Renovar la clave del panel (desautoriza a todos los celulares) |
| `--quality Q` | `best`, `1080p`, `720p`, `worst`… (default `best`) |
| `--low-latency` | Baja latencia en Twitch. Con poca RAM en la TV puede bufferear |
| `--no-check` | No verificar si el canal está en vivo antes de castear |
| `--backend B` | `auto` (default), `streamlink` o `yt-dlp` |
| `--tv TEXTO` | Elegir TV por nombre, UDN o IP |
| `--tv-ip IP` | Conectar directo a una IP |
| `--list-tvs` | Listar TVs y salir |
| `--set-default` / `--forget-default` | Manejar la TV predeterminada |
| `--version` | Ver la versión |

---

## 7. Problemas frecuentes

### No encuentra ninguna TV

En orden de probabilidad:

1. **La TV está apagada o en otra red.** Si tenés wifi de 2.4 y 5 GHz con nombres
   distintos, poné la compu en la misma banda que la TV.
2. **DLNA desactivado en la TV.** Buscá en el menú *Screen Share* / *SmartShare* /
   *AllShare* y activalo.
3. **El router filtra multicast.** Es común. Buscá la IP de la TV en su menú de red
   (Configuración → Red → Estado) y usá:
   ```bash
   twitch2tv xqc --tv-ip 192.168.0.50
   ```
4. **Red de invitados o VLAN separada.** Ahí no hay nada que hacer: la TV y la compu
   tienen que verse.
5. **Firewall.** El puerto 8090/TCP tiene que estar accesible desde la LAN:
   ```bash
   sudo ufw allow from 192.168.0.0/24 to any port 8090
   ```

### El celular no abre el panel

- El celular tiene que estar en el mismo wifi que la laptop. Los datos móviles no sirven.
- Si la laptop usa `ufw`, puede estar bloqueando el puerto. Permití solo tu red de casa
  (cambiá `192.168.1.0/24` por la tuya):

  ```bash
  sudo ufw allow from 192.168.1.0/24 to any port 8091 proto tcp
  ```
- Si dice *Este celular no tiene acceso*, volvé a escanear el QR (el de la terminal o
  el del botón **Celular** del panel). Pasa si
  se renovó la clave o si borraste los datos del navegador.

### `Address already in use`

Quedó un proceso viejo ocupando el puerto:

```bash
pkill -f stream2tv
```

O usá otro puerto: `--port 8091`.

### La TV muestra "no compatible" o pantalla negra

Probá bajar la calidad — con poca RAM, 1080p a veces no da:

```bash
twitch2tv xqc --quality 720p
```

### YouTube se ve en 360p

Falta un runtime de JavaScript. Instalá cualquiera de los tres:

```bash
sudo apt install nodejs        # Debian/Ubuntu
brew install node              # macOS
```

Para confirmar que era eso:

```bash
~/.local/share/stream2tv-venv/bin/yt-dlp -F "https://www.youtube.com/watch?v=..."
```

Si solo lista formatos chicos, era eso. El techo con H.264 es 1080p: los formatos de
1440p y 4K son VP9/AV1 y la TV no los decodifica.

### Se cortó un momento y volvió solo

Es la reconexión automática: si Kick, Twitch o YouTube dejan de responder a mitad
del stream, stream2tv relanza la descarga sin cortar la conexión con la TV (hasta 5
intentos, esperando 1, 2 y 4 segundos). En el panel se ve "Reconectando…" en ámbar.
Si el canal terminó de verdad, se rinde y lo avisa en el log.

### El video va cortado o se traba

- Probá `--quality 720p`.
- Si estás por wifi, cable ayuda mucho — la compu está sirviendo video a la red.
- Sacá `--low-latency` si lo estabas usando.

### El canal está offline

Antes de castear se verifica que haya stream, así que si te dice que no hay nada
disponible, es que el canal no está en vivo. Si creés que sí está, saltá el chequeo
con `--no-check`.

### ¿Un sitio está soportado?

```bash
~/.local/share/stream2tv-venv/bin/streamlink --plugins
~/.local/share/stream2tv-venv/bin/streamlink "https://sitio/lo-que-sea"
```

Si streamlink no lo tiene, se intenta con yt-dlp automáticamente. Si ninguno lo
reconoce, no va a funcionar: el proyecto no extrae video de páginas arbitrarias,
solo le pasa la URL a esas dos herramientas.

---

## 8. Desinstalar

```bash
rm ~/.local/bin/stream2tv ~/.local/bin/kick2tv ~/.local/bin/twitch2tv
rm -rf ~/.local/share/stream2tv-venv
rm -rf ~/.config/stream2tv
```

Y borrá la carpeta del proyecto.

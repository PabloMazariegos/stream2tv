# stream2tv

Transmite streams en vivo de **Twitch**, **Kick** (o cualquier cosa que soporte `streamlink`) a una **LG webOS TV** usando DLNA — sin Chromecast y sin espejar la pantalla del celular.

Instala tres comandos: `twitch2tv`, `kick2tv` y `stream2tv`.

## Por qué existe

La TV (LG webOS UP7710PSB) tiene poca RAM y no acepta apps externas. Los caminos obvios fallaron:

- El navegador de la TV no abre `kick.com`.
- El cast de pestaña de Brave solo funciona en sitios permitidos ("available for specific sites").
- Las apps tipo Web Video Caster tampoco cargan la página de Kick.
- Espejar la pantalla del Android funcionaba, pero ocupaba el teléfono entero.

La solución usa DLNA/UPnP, que la LG sí trae de fábrica: la laptop hace de servidor de video y la TV solo reproduce.

## Cómo funciona

1. Descubre las TVs de la red por SSDP, buscando `MediaRenderer`.
2. Levanta un servidor HTTP local (puerto `8090`) que sirve `/stream.ts`.
3. Le manda esa URL a la TV por DLNA (`AVTransport.SetAVTransportURI` + `Play`).
4. Cuando la TV pide el stream, lanza `streamlink --stdout <url> best` y le hace pipe del MPEG-TS directo.

La TV nunca toca Twitch ni Kick: solo ve un archivo de video servido desde la red local.

## Instalación

Requisitos: **Linux o macOS**, Python 3.8+ con `venv`, y una TV con DLNA en la misma red.

```bash
git clone https://github.com/<usuario>/stream-limitless.git
cd stream-limitless
./install.sh
```

Crea un venv en `~/.local/share/stream2tv-venv` e instala los comandos en `~/.local/bin`.

Opcional pero recomendado: tener `node`, `deno` o `bun` en el sistema. yt-dlp los usa para desbloquear las calidades altas de YouTube (sin uno, YouTube queda en 360p).

Instrucciones paso a paso, compatibilidad de TVs y solución de problemas: **[`docs/MANUAL.md`](docs/MANUAL.md)**.

## Uso

### TUI (por defecto)

Corré el comando sin argumentos y se abre una app que queda viva en la terminal:

```
$ twitch2tv
```

```
┌─ stream2tv ───────────────────────────────── v0.4.0 ── 09:09 ─┐
│ TVs en la red          │ Que ver                              │
│                        │ ┌──────────────────────────────────┐ │
│ [LG] webOS TV UP7710 * │ │ Twitch                         ▾ │ │
│   192.168.0.50         │ └──────────────────────────────────┘ │
│ Chromecast sala        │ ┌──────────────────────────────────┐ │
│   192.168.0.77         │ │ xqc                              │ │
│                        │ └──────────────────────────────────┘ │
│                        │ Favoritos                            │
│                        │ ┌──────────────────────────────────┐ │
│                        │ │ xqc  (Twitch)                    │ │
│                        │ └──────────────────────────────────┘ │
├────────────────────────┴──────────────────────────────────────┤
│ ● Reproduciendo xqc en [LG] webOS TV UP7710PSB                │
├───────────────────────────────────────────────────────────────┤
│ 09:09:16  2 TV(s) encontrada(s).                              │
│ 09:09:17  Agregado a favoritos: xqc                           │
│ 09:09:17  Reproduciendo https://twitch.tv/xqc                 │
├───────────────────────────────────────────────────────────────┤
│ ^d TV por defecto  ^q Salir  ^s Detener  ^r Refrescar  ^f Fav │
└───────────────────────────────────────────────────────────────┘
```

Las TVs de la red aparecen a la izquierda (flechas para moverte), escribís el canal a la derecha y `Enter` lo manda a la TV. El panel de abajo va registrando todo lo que pasa: cuándo se conectó la TV, si el canal está offline, si se cortó el stream.

| Atajo | Qué hace |
|---|---|
| `Enter` | Reproducir el canal escrito |
| `Ctrl+S` | Detener |
| `Ctrl+R` | Volver a buscar TVs |
| `Ctrl+D` | Guardar la TV seleccionada como predeterminada |
| `Ctrl+F` | Marcar/desmarcar el canal como favorito |
| `Ctrl+W` | Mostrar el QR del panel web para el celular |
| `Ctrl+Q` | Salir (detiene el stream antes) |

Los atajos usan `Ctrl` a propósito: con letras sueltas se escribirían dentro del campo de canal.

Podés cambiar de canal sin detener a mano — escribís otro y `Enter`, y corta el anterior solo. Los favoritos se guardan y con `Enter` sobre uno arranca directo. Cada 15 segundos chequea la TV: si se detuvo sola, lo ves en pantalla.

**Panel web para el celular.** Junto con la TUI arranca un panel en `http://<ip-de-la-laptop>:8091`. Con `Ctrl+W` aparece un QR: lo escaneás una vez y manejás todo desde el celular (TV, canal, favoritos, detener). El celular y la TUI muestran siempre lo mismo. Detalles en el [manual](docs/MANUAL.md).

### Modo pregunta-respuesta

Si preferís el flujo viejo de prompts en secuencia, sin TUI:

```bash
twitch2tv --wizard
```

### Modo directo

```bash
twitch2tv xqc                          # canal de Twitch
kick2tv lacobraaa                      # canal de Kick
stream2tv https://twitch.tv/xqc        # URL completa, cualquier plataforma
twitch2tv tw:xqc                       # prefijo explícito (tw: / ki: / yt:)
twitch2tv stop                         # detiene la reproducción en la TV
```

### Otros sitios

Pegá la URL completa y listo:

```bash
stream2tv "https://www.youtube.com/watch?v=..."
stream2tv yt:algun_canal                  # atajo a youtube.com/@canal/live
```

Hay dos backends y se elige solo:

| Sitio | Backend | Por qué |
|---|---|---|
| YouTube | `yt-dlp` | El plugin de YouTube de streamlink solo consigue **360p** |
| Los que streamlink reconoce | `streamlink` | Kick, Twitch, Vimeo, Dailymotion, HLS/DASH genéricos |
| Cualquier otro | `yt-dlp` | streamlink cubre ~134 sitios, yt-dlp ~1750 |

La detección es por URL y no hay que configurar nada: se le pregunta a streamlink si tiene plugin para esa dirección (por su API, sin tocar la red) y, si no lo tiene, se usa yt-dlp como red de contención. Se puede forzar con `--backend streamlink` o `--backend yt-dlp`.

El selector de plataforma de la TUI **solo se usa cuando escribís un nombre de canal pelado** (`lacobraaa`). Si pegás una URL completa, se respeta tal cual, sin importar qué diga el selector.

**Para que YouTube dé más de 360p hace falta un runtime de JavaScript** (`node`, `deno` o `bun`) instalado en el sistema. Sin uno, YouTube responde `403` en los formatos altos y solo queda el progresivo de 360p. Si tenés `node`, ya está.

En YouTube se pide **H.264 + AAC** explícitamente, aunque haya calidades mayores: los formatos de 1440p y 4K son VP9 o AV1, que la TV no decodifica — y ffmpeg los mete en el MPEG-TS como *private data stream*, que la TV directamente no reconoce. El techo práctico es 1080p, que es hasta donde YouTube ofrece H.264.

Para ver si un sitio está soportado:

```bash
~/.local/share/stream2tv-venv/bin/streamlink --plugins
~/.local/share/stream2tv-venv/bin/streamlink "https://sitio/lo-que-sea"   # lista las calidades
```

Si el sitio no tiene plugin, streamlink igual puede con un `.m3u8` o `.mpd` directo. Si tampoco, no va a funcionar: la app no extrae video de páginas arbitrarias, solo le pasa la URL a streamlink.

### Elegir la TV

```bash
twitch2tv --list-tvs                   # ver qué hay en la red
twitch2tv xqc --tv webos               # por nombre parcial (o UDN, o IP exacta)
twitch2tv xqc --tv-ip 192.168.0.50     # directo, salteando el descubrimiento
twitch2tv --set-default                # guardar una TV como predeterminada
twitch2tv --forget-default             # olvidarla
```

La TV predeterminada se guarda en `~/.config/stream2tv/config.json` por **UDN**, no por IP, así que sigue funcionando aunque el router le cambie la dirección.

Si el filtro `--tv` coincide con más de una TV, el comando falla y te muestra las opciones en vez de adivinar.

### Otras opciones

| Flag | Para qué |
|---|---|
| `--port N` | Cambiar el puerto HTTP local del video (default `8090`) |
| `--web-port N` | Puerto del panel web (default `8091`) |
| `--no-web` | No abrir el panel web |
| `--quality Q` | Calidad de streamlink: `best`, `720p`, `worst`… (default `best`) |
| `--low-latency` | Baja latencia en Twitch. Con poca RAM en la TV puede bufferear |
| `--no-check` | No verificar si el canal está en vivo antes de castear |
| `--backend B` | `auto` (default), `streamlink` o `yt-dlp` |
| `-i` | Forzar modo interactivo aunque pases un canal |

## Estructura

```
stream2tv/
  discovery.py   SSDP, descubrimiento y selección de TV
  caster.py      servidor HTTP, pipe de streamlink y control DLNA
  config.py      TV predeterminada y favoritos
  controlador.py estado y logica de reproduccion, compartidos por la TUI y la web
  cli.py         argumentos, modo directo y wizard
  tui.py         la app de terminal (Textual)
  web.py         panel web para el celular (API + eventos en vivo)
  web/           la pagina del panel, su manifest e iconos
bin/             entry points: stream2tv, kick2tv, twitch2tv
tests/           tests de selección, URLs y de la TUI
```

## Tests

```bash
~/.local/share/stream2tv-venv/bin/python3 -m unittest discover -s tests
```

95 tests. Cubren resolución de URLs, toda la lógica de selección de TV (filtros, ambigüedad, predeterminada, menú), la TUI corriendo de verdad en modo headless (reproducir, detener, cambiar de canal, favoritos, canal offline, puerto ocupado, quedarse sin TVs) y la limpieza al salir, con procesos reales: que se mate el árbol completo, que se libere el puerto y que salir por cualquier vía no deje nada corriendo.

El descubrimiento real por red y el casteo a la TV se prueban a mano.

## Problemas conocidos

**`Address already in use`.** Quedó un proceso viejo ocupando el puerto:

```bash
pkill -f stream2tv
```

O usá otro puerto con `--port`.

**El stream seguía corriendo después de detener y salir.** Tres causas, todas corregidas:

- El stop corría en un worker en segundo plano; al salir enseguida, Textual lo cancelaba a mitad y la TV nunca recibía el `Stop`. Ahora la limpieza es sincrónica.
- `streamlink` puede lanzar un `ffmpeg` para muxear: matar solo al padre dejaba al nieto bajando el stream. Ahora cada stream corre en su propio grupo de procesos y se mata el árbol completo.
- Salir con `Ctrl+C` o cerrando la terminal no pasaba por la limpieza. Ahora hay un `finally` en el arranque y un `atexit` que apaga todo servidor vivo.

**El stream arrancaba y después moría.** Los llamados de control a la TV (`GetTransportInfo`, `Play`) pueden tirar un **timeout de red** — no solo errores SOAP — porque la TV se pone lenta mientras reproduce. Esa excepción sin capturar mataba el proceso entero, incluido el servidor HTTP, y cortaba la transmisión. Corregido: van envueltos en `try/except` amplio con reintentos.

**No encuentra la TV.** Tiene que estar encendida y en la misma red (no en una VLAN de invitados). SSDP usa multicast y algunos routers lo filtran entre bandas de wifi; en ese caso usá `--tv-ip`.

**Twitch y los ads.** `streamlink` 8.x salta los segmentos de publicidad por defecto (por eso desapareció `--twitch-disable-ads`). Los pre-roll no se saltan: el stream espera a que terminen. En un corte de mitad de transmisión puede haber un hueco, porque Twitch no manda contenido real durante ese rato.

**Avisos de `Play` repetidos.** Mientras la TV reproduce, a veces no contesta el control. El código asumía que había que mandarle `Play` de nuevo y llenaba el log de avisos justo cuando todo andaba bien. Ahora, si la TV no contesta, se espera en vez de insistir; y el error UPnP `701` ("Transition not available") se trata como éxito, porque significa que ya está reproduciendo.

**YouTube y otros sitios con audio y video separados.** streamlink los muxea con ffmpeg y su salida por defecto es matroska, que la TV no reproduce si se lo anunciamos como `video/mp2t`. Por eso se le pasa siempre `--ffmpeg-fout mpegts`.

**YouTube en 360p.** Si te sale en baja resolución, falta un runtime de JavaScript (`node`, `deno` o `bun`): yt-dlp lo necesita para resolver los formatos altos, y sin él YouTube devuelve `403` en todos salvo el progresivo de 360p. Comprobalo con:

```bash
~/.local/share/stream2tv-venv/bin/yt-dlp -F "https://www.youtube.com/watch?v=..."
```

Si solo lista formatos chicos, es eso.

## Documentación

- **[`docs/MANUAL.md`](docs/MANUAL.md)** — manual de usuario: instalación, uso y solución de problemas.
- **[`docs/COMO-FUNCIONA.md`](docs/COMO-FUNCIONA.md)** — cómo funciona por dentro: DLNA/SSDP/UPnP, qué son streamlink y yt-dlp, el pipe MPEG-TS y las decisiones de diseño.

## Aviso

Herramienta para uso personal en tu propia red. No aloja, redistribuye ni descifra contenido: solo orquesta [streamlink](https://streamlink.github.io/) y [yt-dlp](https://github.com/yt-dlp/yt-dlp), y le pasa el resultado a un dispositivo DLNA de tu LAN. El uso que le des a cada plataforma se rige por los términos de servicio de esa plataforma, y es responsabilidad de quien la usa.

## Licencia

MIT — ver [`LICENSE`](LICENSE).

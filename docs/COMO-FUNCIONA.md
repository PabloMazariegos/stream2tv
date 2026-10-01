# Cómo funciona por dentro

Este documento explica la maquinaria de `stream2tv`: qué hace cada pieza, qué son
`streamlink` y `yt-dlp`, y por qué el diseño terminó siendo este y no otro.

---

## La idea en una frase

La TV no sabe entrar a Twitch ni a Kick. Pero **sí sabe reproducir un archivo de
video servido por HTTP desde la red local**, porque trae DLNA de fábrica. Entonces
la laptop se disfraza de servidor de video: convierte el stream en vivo en algo que
parece un archivo, y le pasa a la TV una URL de su propia red.

```
  Twitch/Kick/YouTube          tu laptop                        la TV
  ───────────────────          ─────────                        ─────
                          ┌────────────────────┐
   HLS (.m3u8) ──────────▶│ streamlink/yt-dlp  │
                          │        │ stdout    │
                          │        ▼           │
                          │  servidor HTTP     │──── MPEG-TS ────▶ reproduce
                          │  :8090/stream.ts   │
                          └────────────────────┘
                                   ▲
                                   │  "reproducí http://192.168.0.2:8090/stream.ts"
                                   └──────────── DLNA / UPnP ─────────────┘
```

La TV nunca habla con Twitch. Solo ve un video servido desde una IP de su LAN.

---

## Qué es streamlink

Un sitio de streaming no te da un archivo de video. Te da una **página web con
JavaScript** que, tras autenticarse y resolver un par de redirecciones, termina
pidiendo un **manifiesto HLS** (`.m3u8`): un archivo de texto que lista los
fragmentos de 2–6 segundos que componen el stream, y que se re-descarga cada pocos
segundos porque la emisión sigue creciendo.

`streamlink` es un programa de línea de comandos que hace justo ese trabajo sucio.
Tiene un **plugin por sitio** (~134: Twitch, Kick, Vimeo, Dailymotion…) que sabe
cómo llegar del `kick.com/canal` al `.m3u8` real. Después descarga los fragmentos
en orden y los va escribiendo a donde le digas — un reproductor, un archivo, o
`stdout`.

Lo que usa el proyecto (`caster.py:188`):

```bash
streamlink --stdout --ffmpeg-fout mpegts https://kick.com/lacobraaa best
```

- `--stdout` → escribe el video crudo a la salida estándar, para poder hacerle pipe.
- `best` → de todas las calidades del manifiesto, agarrá la mejor.
- `--ffmpeg-fout mpegts` → **importante**, ver más abajo.

Detalle no obvio: streamlink también se usa **como librería** para preguntar, sin
tocar la red, si tiene un plugin para cierta URL (`caster.py:141`). Es una consulta
local e instantánea, y sirve para decidir el backend sin ejecutar nada.

## Qué es yt-dlp

El descendiente vivo de `youtube-dl`. Mismo problema, escala distinta: **~1750
sitios** soportados, y con un extractor de YouTube mucho mejor que el de streamlink.

Está por dos razones:

1. **YouTube**. El plugin de YouTube de streamlink solo consigue 360p. yt-dlp llega a 1080p.
2. **Red de contención**. Si streamlink no reconoce la URL, se intenta con yt-dlp
   antes de rendirse (`caster.py:163`).

Dos cosas que hay que saber de yt-dlp acá:

**Necesita un runtime de JavaScript.** YouTube protege sus formatos altos con un
desafío en JS que hay que *ejecutar* para derivar una firma. Sin `node`, `deno` o
`bun` en el sistema, YouTube devuelve `403` en todo salvo el progresivo de 360p.
Por eso `runtime_js()` (`caster.py:89`) busca uno y se lo pasa con `--js-runtimes`.

**Se le pide H.264 + AAC a propósito**, aunque haya calidades mayores
(`formato_ytdlp`, `caster.py:101`). Los formatos de 1440p y 4K de YouTube son VP9 o
AV1: la TV no los decodifica, y además ffmpeg los mete en el MPEG-TS como *private
data stream*, que la TV directamente no reconoce. El techo práctico es 1080p, que
es hasta donde YouTube ofrece H.264.

**Quién baja qué** (`elegir_backend`, `caster.py:163`):

| Sitio | Backend | Por qué |
|---|---|---|
| YouTube | `yt-dlp` | streamlink solo llega a 360p |
| Los que streamlink reconoce | `streamlink` | Kick, Twitch, Vimeo, HLS/DASH genérico |
| Cualquier otro | `yt-dlp` | 1750 sitios vs. 134 |

---

## Qué es DLNA (y SSDP, y UPnP)

Tres nombres para capas de lo mismo:

- **UPnP** — el protocolo base. Dispositivos de la red se anuncian, publican un XML
  con sus servicios, y se les mandan órdenes por SOAP (XML sobre HTTP).
- **SSDP** — la parte de *descubrimiento* de UPnP. Corre sobre **UDP multicast**.
- **DLNA** — un perfil de UPnP para media: define roles como *MediaRenderer*
  (algo que reproduce) y *MediaServer* (algo que sirve archivos).

La TV es un **MediaRenderer**. La laptop actúa como MediaServer improvisado.

### Paso 1: encontrar la TV (`discovery.py`)

Se manda un paquete UDP al grupo multicast `239.255.255.250:1900`
(`_buscar_locations`, `discovery.py:40`):

```
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 3
ST: urn:schemas-upnp-org:device:MediaRenderer:1
```

`ST` (*search target*) filtra: solo contestan los que reproducen media. Todo
dispositivo que responde manda un header `LOCATION:` con la URL de su XML de
descripción. **Se manda dos veces**: UDP no garantiza entrega y el primer paquete
se pierde seguido.

Después se baja cada XML en paralelo (`_construir`, `discovery.py:76`) y se descarta
lo que no exponga el servicio `AVTransport` — sin eso no se le puede ordenar nada.
Una misma TV suele anunciarse por varias interfaces, así que se **deduplica por
UDN** (el identificador único del dispositivo).

Cuando el multicast no llega (routers que lo filtran entre bandas de wifi, redes de
invitados), está `desde_ip()` (`discovery.py:108`): prueba a mano los puertos y rutas
típicos (`/description.xml`, `/dmr.xml`…) contra una IP dada. Eso es lo que hace `--tv-ip`.

La TV predeterminada se guarda **por UDN, no por IP** (`config.py:34`), así sobrevive
a que el router le cambie la dirección.

### Paso 2: saber qué IP tengo *yo* para esa TV

Sutil pero necesario. Hay que darle a la TV una URL con la IP de la laptop, y la
laptop puede tener varias (wifi, ethernet, docker0, VPN). `ip_local_hacia()`
(`discovery.py:123`) abre un socket UDP hacia la TV **sin mandar nada** y le pregunta
al kernel qué dirección de origen eligió. Es la forma barata de consultar la tabla
de ruteo.

---

## El servidor: el corazón del asunto (`caster.py:239`)

Un `ThreadingHTTPServer` en `<ip_local>:8090` que responde una sola ruta: `/stream.ts`.
Escucha solo en la interfaz que llega a la TV y **solo le contesta a la IP de la TV**
(`cliente_permitido`): cualquier otro equipo de la red recibe un `403` y no puede
disparar descargas.

**El truco central: `streamlink` no arranca al iniciar el servidor, sino cuando la
TV pide el archivo** — dentro del `do_GET` (`caster.py:283`). Así no se consume ancho
de banda mientras la TV decide si se conecta o no.

Cuando la TV hace el GET:

1. Si había otro stream en curso (la TV que reconecta), se corta: **una descarga a la vez**.
   Después se lanza `streamlink`/`yt-dlp` con `subprocess.Popen(..., stdout=PIPE, stderr=PIPE)`.
2. Se responde `200` con `Content-Type: video/mp2t` y **sin `Content-Length`** —
   es un stream infinito, no se sabe cuánto mide.
3. Bucle: leer del pipe del proceso, escribir al socket de la TV. Se lee en bloques
   de `188 * 100` bytes porque **188 es el tamaño de un paquete MPEG-TS**; así nunca
   se manda medio paquete.

Cuando la TV corta la conexión salta `BrokenPipeError`, y el `finally` mata el
proceso.

Si en cambio es **el backend el que termina solo** (Kick tiró un timeout, un `403`, se
cortó el stream), se loguea su código de salida y las últimas líneas de su `stderr`,
que se van juntando en un hilo aparte. Antes ese `stderr` iba a `/dev/null` y un corte
a mitad de stream no dejaba ninguna pista.

### Por qué MPEG-TS y no otra cosa

MPEG-TS es un formato de transporte pensado para transmisión: es un flujo de
paquetitos de 188 bytes, sin índice ni header global, donde **podés empezar a leer
desde cualquier punto**. Un MP4 necesita su tabla de índices — normalmente al final
del archivo — así que no sirve para algo que todavía se está generando.

Twitch y Kick ya entregan MPEG-TS, así que pasa derecho. Pero cuando el sitio manda
video y audio por separado (YouTube, DASH en general), streamlink los tiene que
**muxear** con ffmpeg, y su salida por defecto es **matroska**. La TV recibía algo
anunciado como `video/mp2t` que en realidad era `.mkv`, y no reproducía nada. De ahí
el `--ffmpeg-fout mpegts` (`caster.py:195`).

### Por qué se verifica antes de castear

`canal_en_vivo()` (`caster.py:220`) corre `streamlink --stream-url` (o `yt-dlp -g`),
que **resuelve la URL del stream sin descargarlo**. Si el canal está offline, te
enterás en la terminal en vez de ver a la TV quedarse en negro sin explicación.

---

## Paso 3: darle la orden a la TV (`caster.py:378`)

Dos llamadas SOAP al servicio `AVTransport`:

```python
av.SetAVTransportURI(InstanceID=0, CurrentURI="http://192.168.0.2:8090/stream.ts",
                     CurrentURIMetaData=<DIDL-Lite ...>)
av.Play(InstanceID=0, Speed="1")
```

El **DIDL-Lite** (`metadata_didl`) es un XML que describe el
contenido: título, que es un `videoItem`, y el `protocolInfo`
`http-get:*:video/mp2t:*`. El título y la URL van **escapados**: una URL de YouTube
con `&t=10` generaba un XML inválido y la TV lo rechazaba. Varias TVs se niegan a reproducir si no viene, o lo usan
para decidir el decodificador.

Este paso parece trivial y fue el que más problemas dio, por dos razones:

**La TV se vuelve lenta mientras reproduce.** `GetTransportInfo` y `Play` pueden
tirar **timeout de red** — no solo errores SOAP. Una excepción sin capturar acá
mataba el proceso entero, incluido el servidor HTTP, y cortaba la transmisión que ya
estaba andando bien. Por eso todo va envuelto y con reintentos.

**El error UPnP `701` es un éxito disfrazado.** Significa *"transition not
available"*: la TV rechaza el `Play` porque **ya está reproduciendo**. Se trata como
éxito (`caster.py:419`).

Y una lección relacionada: si la TV **no contesta** el control, casi siempre es
porque está ocupada arrancando el video, no porque haya fallado. La versión vieja le
mandaba `Play` otra vez y llenaba el log de avisos justo cuando todo andaba. Ahora
espera en vez de insistir.

---

## La limpieza (la parte que más costó)

El bug fue: *"el stream seguía corriendo después de detener y salir"*. Tres causas
independientes, cada una suficiente por sí sola:

**1. `streamlink` tiene nietos.** Para muxear levanta un `ffmpeg`. Matando solo al
padre, ese ffmpeg quedaba huérfano bajando el stream para siempre. Solución: cada
backend arranca con `start_new_session=True` (grupo de procesos propio) y se mata el
grupo entero con `os.killpg` (`matar_arbol`, `caster.py:34`).

**2. La limpieza dependía de un worker.** En la TUI, `detener_stream` corría en
segundo plano; al salir enseguida, Textual lo cancelaba a mitad y la TV nunca recibía
el `Stop`. Ahora es sincrónica.

**4. Arranques en paralelo.** Los workers de Textual corren en threads y no se pueden
matar: `exclusive=True` solo los *marca* como cancelados. Apretar Reproducir dos veces
mientras Kick tardaba dejaba dos arranques compitiendo por el puerto ("Puerto
ocupado"). Ahora arrancar, detener y el monitor de estado pasan por un mismo lock, y
un arranque cancelado se descarta antes de tocar la TV.

**3. Salir por caminos raros no pasaba por la limpieza.** `Ctrl+C`, una excepción,
cerrar la terminal. Solución: un registro global de servidores vivos
(`_SERVIDORES_VIVOS`) y un `atexit.register(apagar_todo)` (`caster.py:66`) que apaga
todo pase lo que pase.

---

## Recorrido completo de `twitch2tv xqc`

1. `twitch2tv` (entry point de `pyproject.toml`) → `cli.main_twitch()` → `cli.main(plataforma_default="twitch")`.
2. `resolver_url("xqc", "twitch")` → `https://twitch.tv/xqc` (`plataformas.py:27`).
3. `obtener_tv()` → SSDP, filtros, TV guardada, o menú (`discovery.elegir`).
4. `canal_en_vivo()` → ¿hay stream? Si no, sale con el motivo.
5. `ip_local_hacia(tv.host)` → `192.168.0.2`.
6. `Servidor.iniciar()` → HTTP en `192.168.0.2:8090`, solo para la TV, todavía sin bajar nada.
7. `reproducir()` → `SetAVTransportURI` + `Play` hacia `http://192.168.0.2:8090/stream.ts`.
8. La TV hace `GET /stream.ts` → recién ahí arranca `streamlink` y empieza el pipe.
9. Cada 10 s se le pregunta el estado a la TV; si queda en `STOPPED` dos veces
   seguidas, el stream se da por terminado.
10. `Ctrl+C` (o la TV parada) → `detener_tv()` (Stop por DLNA) + `servidor.detener()`
   (mata el árbol, libera el puerto).

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| `discovery.py` | SSDP, construcción de `TV`, IP local, selección de TV |
| `caster.py` | Backends, servidor HTTP, pipe, control DLNA, limpieza |
| `plataformas.py` | `canal` / `tw:canal` / URL → URL final |
| `config.py` | TV predeterminada y favoritos en `~/.config/stream2tv/config.json` |
| `cli.py` | Argumentos, modo directo, wizard |
| `tui.py` | App de terminal (Textual) |
| `pyproject.toml` | Dependencias y entry points; `twitch2tv` y `kick2tv` son `stream2tv` con plataforma fijada |
| `bin/*` | Los mismos comandos, para correrlos desde el repo sin instalar |

---

## Límites del diseño

- **Un stream a la vez por puerto.** El servidor sirve una sola ruta. Para dos TVs
  simultáneas hacen falta dos procesos con `--port` distintos.
- **La laptop tiene que quedarse prendida.** Es quien baja el stream; si se suspende,
  la TV se queda sin video.
- **No extrae video de páginas arbitrarias.** Solo le pasa la URL a streamlink/yt-dlp.
  Si ninguno de los dos la reconoce, no funciona.
- **Latencia.** Hay un retraso natural de segundos: el stream se baja, se remuxea y se
  vuelve a servir. `--low-latency` ayuda en Twitch a costa de buffering.

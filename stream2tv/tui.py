"""Interfaz de terminal (TUI) que queda abierta: elegis TV, canal, y controlas el stream."""

from datetime import datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
)

from . import __version__, caster, config, discovery
from .plataformas import etiqueta, opciones_select, resolver_url

url_de = resolver_url


class ItemTV(ListItem):
    """Fila de la lista de TVs, que recuerda a que TV corresponde."""

    def __init__(self, tv, es_defecto):
        marca = " *" if es_defecto else ""
        super().__init__(Label(f"{tv.nombre}{marca}\n  {tv.host}"))
        self.tv = tv


class ItemFavorito(ListItem):
    def __init__(self, canal, plataforma):
        super().__init__(Label(f"{canal}  ({etiqueta(plataforma)})"))
        self.canal = canal
        self.plataforma = plataforma


class StreamTVApp(App):
    """App principal. Mantiene el stream vivo mientras la ventana este abierta."""

    TITLE = "stream2tv"
    SUB_TITLE = f"v{__version__}"

    CSS = """
    Screen { layers: base; }
    #cuerpo { height: 1fr; }
    #panel-tvs { width: 40%; border: round $primary; padding: 0 1; }
    #panel-canal { width: 1fr; border: round $primary; padding: 0 1; }
    .titulo { text-style: bold; color: $accent; padding: 0 0 1 0; }
    #estado { height: 3; border: round $secondary; padding: 0 1; content-align: left middle; }
    #estado.reproduciendo { border: round $success; color: $success; text-style: bold; }
    #estado.trabajando { border: round $warning; color: $warning; }
    #estado.error { border: round $error; color: $error; }
    #log { height: 12; border: round $panel; }
    ListView { height: 1fr; }
    Input { margin: 1 0; }
    """

    # Con prioridad y modificador: si fueran letras sueltas, se escribirian en
    # el campo de canal en vez de accionar, porque el Input se queda las teclas.
    BINDINGS = [
        Binding("ctrl+s", "detener", "Detener", priority=True),
        Binding("ctrl+r", "refrescar", "Refrescar TVs", priority=True),
        Binding("ctrl+d", "predeterminar", "TV por defecto", priority=True),
        Binding("ctrl+f", "favorito", "Favorito", priority=True),
        Binding("ctrl+q", "salir", "Salir", priority=True),
    ]

    estado = reactive("Iniciando...")

    def __init__(
        self,
        plataforma_inicial="kick",
        puerto=8090,
        calidad="best",
        low_latency=False,
        backend="auto",
    ):
        super().__init__()
        self.plataforma = plataforma_inicial
        self.puerto = puerto
        self.calidad = calidad
        self.low_latency = low_latency
        self.backend = backend
        self.tv_actual = None
        self.servidor = None
        self.url_actual = None
        self.reproduciendo = False
        # Queda en True desde que se le manda algo a la TV hasta que un Stop
        # termina de verdad. Sin esto, apretar Detener y salir enseguida dejaba
        # la TV reproduciendo: el worker del stop se cancelaba a mitad.
        self._tv_tocada = False
        self.udn_defecto, _ = config.tv_por_defecto()

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="cuerpo"):
            with Vertical(id="panel-tvs"):
                yield Static("TVs en la red", classes="titulo")
                yield ListView(id="lista-tvs")
            with Vertical(id="panel-canal"):
                yield Static("Que ver", classes="titulo")
                yield Select(
                    opciones_select(), value=self.plataforma, id="plataforma", allow_blank=False
                )
                yield Input(placeholder="canal o URL, y Enter", id="canal")
                yield Static("Favoritos", classes="titulo")
                yield ListView(id="lista-favoritos")
        yield Static("", id="estado")
        yield RichLog(id="log", markup=True, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self):
        self.query_one("#canal", Input).focus()
        self.cargar_favoritos()
        self.log_ui("Bienvenido. Buscando TVs en la red...")
        self.refrescar_tvs()
        # Chequeo periodico: si la TV deja de reproducir sola, que se note en pantalla.
        self.set_interval(15, self.verificar_estado)

    # ---------- helpers de UI ----------

    def log_ui(self, mensaje, color="white"):
        hora = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{hora}[/dim] [{color}]{mensaje}[/{color}]")

    def log_desde_hilo(self, mensaje):
        """Los workers en thread no pueden tocar la UI directo."""
        self.call_from_thread(self.log_ui, mensaje)

    def watch_estado(self, valor):
        try:
            widget = self.query_one("#estado", Static)
        except Exception:
            return
        widget.update(valor)

    def marcar_estado(self, texto, clase=""):
        self.estado = texto
        widget = self.query_one("#estado", Static)
        widget.set_classes(clase)

    def cargar_favoritos(self):
        lista = self.query_one("#lista-favoritos", ListView)
        lista.clear()
        for fav in config.favoritos():
            lista.append(ItemFavorito(fav["canal"], fav["plataforma"]))

    # ---------- descubrimiento ----------

    def action_refrescar(self):
        self.refrescar_tvs()

    @work(thread=True, exclusive=True, group="descubrir")
    def refrescar_tvs(self):
        self.call_from_thread(self.marcar_estado, "Buscando TVs...", "trabajando")
        tvs = discovery.descubrir()
        self.call_from_thread(self.mostrar_tvs, tvs)

    def mostrar_tvs(self, tvs):
        lista = self.query_one("#lista-tvs", ListView)
        lista.clear()
        if not tvs:
            self.marcar_estado("No se encontro ninguna TV. Prendela y apreta 'r'.", "error")
            self.log_ui("Sin TVs. Revisa que este encendida y en la misma red.", "red")
            return
        for tv in tvs:
            lista.append(ItemTV(tv, tv.udn == self.udn_defecto))
        indice = next((i for i, tv in enumerate(tvs) if tv.udn == self.udn_defecto), 0)
        lista.index = indice
        self.tv_actual = tvs[indice]
        self.log_ui(f"{len(tvs)} TV(s) encontrada(s).", "green")
        self.marcar_estado(f"Listo. TV: {self.tv_actual.nombre}")

    @on(ListView.Highlighted, "#lista-tvs")
    def cambiar_tv(self, evento):
        if isinstance(evento.item, ItemTV):
            self.tv_actual = evento.item.tv
            if not self.reproduciendo:
                self.marcar_estado(f"Listo. TV: {self.tv_actual.nombre}")

    # ---------- seleccion ----------

    @on(Select.Changed, "#plataforma")
    def cambiar_plataforma(self, evento):
        if evento.value is not Select.BLANK:
            self.plataforma = evento.value

    @on(ListView.Selected, "#lista-favoritos")
    def usar_favorito(self, evento):
        if not isinstance(evento.item, ItemFavorito):
            return
        self.query_one("#plataforma", Select).value = evento.item.plataforma
        self.plataforma = evento.item.plataforma
        entrada = self.query_one("#canal", Input)
        entrada.value = evento.item.canal
        entrada.focus()
        self.action_reproducir()

    @on(Input.Submitted, "#canal")
    def enviar_canal(self):
        self.action_reproducir()

    def action_favorito(self):
        canal = self.query_one("#canal", Input).value.strip()
        if not canal:
            self.log_ui("Escribi un canal para marcarlo como favorito.", "yellow")
            return
        agregado = config.alternar_favorito(canal, self.plataforma)
        self.cargar_favoritos()
        self.log_ui(f"{'Agregado a' if agregado else 'Quitado de'} favoritos: {canal}", "cyan")

    def action_predeterminar(self):
        if self.tv_actual is None:
            return
        config.set_tv_por_defecto(self.tv_actual.udn, self.tv_actual.nombre)
        self.udn_defecto = self.tv_actual.udn
        self.log_ui(f"'{self.tv_actual.nombre}' guardada como TV por defecto.", "cyan")
        self.refrescar_tvs()

    # ---------- reproduccion ----------

    def action_reproducir(self):
        canal = self.query_one("#canal", Input).value.strip()
        if not canal:
            self.log_ui("Escribi un canal primero.", "yellow")
            return
        if self.tv_actual is None:
            self.log_ui("No hay TV seleccionada.", "red")
            return
        self.iniciar_stream(canal, self.plataforma)

    def _detener_actual(self):
        """Corta el stream en curso. Sincrono: se llama desde workers en thread.

        No es un worker aparte a proposito: encadenar dos workers del mismo grupo
        exclusivo hace que el segundo cancele al primero a mitad del stop.
        """
        if self.tv_actual is not None:
            caster.detener_tv(self.tv_actual, log=self.log_desde_hilo)
        if self.servidor is not None:
            self.servidor.detener()
            self.servidor = None
        self.reproduciendo = False
        self.url_actual = None
        self._tv_tocada = False

    @work(thread=True, exclusive=True, group="stream")
    def iniciar_stream(self, canal, plataforma):
        url = url_de(canal, plataforma)
        tv = self.tv_actual

        if self.reproduciendo:
            self.log_desde_hilo("Ya habia un stream activo, lo detengo primero.")
            self.call_from_thread(self.marcar_estado, "Cambiando de canal...", "trabajando")
            self._detener_actual()

        self.call_from_thread(self.marcar_estado, f"Verificando {canal}...", "trabajando")
        vivo, motivo = caster.canal_en_vivo(url, self.calidad, backend=self.backend)
        if not vivo:
            self.call_from_thread(self.marcar_estado, f"{canal} no esta en vivo", "error")
            self.log_desde_hilo(f"No hay stream: {motivo}")
            return

        servidor = caster.Servidor(
            url,
            self.puerto,
            self.calidad,
            self.low_latency,
            log=self.log_desde_hilo,
            backend=self.backend,
        )
        try:
            servidor.iniciar()
        except caster.PuertoOcupado as e:
            self.call_from_thread(self.marcar_estado, "Puerto ocupado", "error")
            self.log_desde_hilo(str(e))
            return

        self.servidor = servidor
        self._tv_tocada = True
        try:
            mi_ip = discovery.ip_local_hacia(tv.host)
            stream_url = f"http://{mi_ip}:{self.puerto}{caster.RUTA_STREAM}"
            self.call_from_thread(self.marcar_estado, f"Enviando {canal} a la TV...", "trabajando")
            arranco = caster.reproducir(tv, stream_url, f"{canal}", log=self.log_desde_hilo)
        except Exception as e:
            servidor.detener()
            self.servidor = None
            self.call_from_thread(self.marcar_estado, "Error hablando con la TV", "error")
            self.log_desde_hilo(f"Error: {e}")
            return

        self.reproduciendo = True
        self.url_actual = url
        if arranco:
            self.call_from_thread(
                self.marcar_estado, f"Reproduciendo {canal} en {tv.nombre}", "reproduciendo"
            )
            self.log_desde_hilo(f"Reproduciendo {url}")
        else:
            self.call_from_thread(
                self.marcar_estado, f"{canal}: la TV no confirmo, puede arrancar sola", "trabajando"
            )

    def action_detener(self):
        if not self.reproduciendo:
            self.log_ui("No hay nada reproduciendose.", "yellow")
            return
        self.detener_stream()

    @work(thread=True, group="stop")
    def detener_stream(self):
        self.call_from_thread(self.marcar_estado, "Deteniendo...", "trabajando")
        self._detener_actual()
        self.log_desde_hilo("Stream detenido.")
        self.call_from_thread(self.marcar_estado, "Detenido.")

    @work(thread=True, group="monitor", exclusive=True)
    def verificar_estado(self):
        """Si la TV se detuvo por su cuenta, reflejarlo en pantalla."""
        if not self.reproduciendo or self.tv_actual is None:
            return
        estado = caster.estado_tv(self.tv_actual)
        if estado == "STOPPED":
            self.reproduciendo = False
            if self.servidor is not None:
                self.servidor.detener()
                self.servidor = None
            self.call_from_thread(self.marcar_estado, "La TV detuvo la reproduccion.", "error")
            self.log_desde_hilo("La TV paso a STOPPED (se corto el stream o alguien la paro).")

    # ---------- salida ----------

    def limpiar(self):
        """Deja todo apagado. Idempotente, y se llama salga por donde salga.

        Va sincrono a proposito: si esto viviera en un worker, al salir Textual
        lo cancelaria a mitad y quedaria el stream corriendo.
        """
        if self._tv_tocada and self.tv_actual is not None:
            try:
                caster.detener_tv(self.tv_actual, log=lambda *_: None)
            except Exception:
                pass
            self._tv_tocada = False
        if self.servidor is not None:
            self.servidor.detener()
            self.servidor = None
        # Por si quedo algun servidor sin referencia (ej. un cambio de canal
        # interrumpido justo antes de salir).
        caster.apagar_todo()
        self.reproduciendo = False

    def action_salir(self):
        self.limpiar()
        self.exit()


def lanzar(
    plataforma_inicial="kick", puerto=8090, calidad="best", low_latency=False, backend="auto"
):
    app = StreamTVApp(plataforma_inicial, puerto, calidad, low_latency, backend)
    try:
        app.run()
    finally:
        # Cubre Ctrl+C, cierre de la terminal y cualquier excepcion: sin este
        # finally, salir por esos caminos dejaba streamlink corriendo y la TV
        # reproduciendo.
        app.limpiar()
    return 0

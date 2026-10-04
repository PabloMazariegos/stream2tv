"""Interfaz de terminal (TUI) que queda abierta: elegis TV, canal, y controlas el stream."""

import io
import threading
from datetime import datetime

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
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

from . import __version__, config
from .controlador import Controlador
from .plataformas import etiqueta, opciones_select

# Como se ve cada fase del Controlador en la barra de estado.
CLASES_FASE = {
    "reproduciendo": "reproduciendo",
    "verificando": "trabajando",
    "enviando": "trabajando",
    "buscando": "trabajando",
    "deteniendo": "trabajando",
    "error": "error",
}
COLORES_LOG = {"info": "white", "ok": "green", "aviso": "yellow", "error": "red"}


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


def qr_texto(url):
    """El QR en caracteres de bloque, o None si falta segno."""
    try:
        import segno
    except ImportError:
        return None
    buffer = io.StringIO()
    segno.make(url, error="m").terminal(out=buffer, compact=True)
    return buffer.getvalue().rstrip("\n")


class PantallaQR(ModalScreen):
    """Muestra la direccion del panel con su QR para escanear desde el celular."""

    BINDINGS = [
        Binding("escape", "cerrar", "Cerrar"),
        Binding("ctrl+w", "cerrar", "Cerrar", show=False),
        Binding("n", "regenerar", "Clave nueva"),
    ]
    DEFAULT_CSS = """
    PantallaQR { align: center middle; }
    #caja-qr { width: auto; height: auto; max-width: 100%; border: round $accent; padding: 1 2; background: $surface; }
    #qr { width: auto; height: auto; }
    .ayuda { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        with Vertical(id="caja-qr"):
            yield Static("Escanealo con la camara del celular", classes="titulo")
            yield Static("", id="qr")
            yield Static("", id="url-panel")
            yield Static(
                "Una sola vez por celular. [b]n[/b]: clave nueva (desautoriza a todos). [b]Esc[/b]: cerrar.",
                classes="ayuda",
            )

    def on_mount(self):
        self._dibujar()

    def _dibujar(self):
        url = self.panel.url_con_clave
        qr = qr_texto(url)
        self.query_one("#qr", Static).update(
            qr if qr is not None else "(Para ver el QR instala segno: pip install segno)"
        )
        self.query_one("#url-panel", Static).update(url)

    def action_regenerar(self):
        config.regenerar_token_web()
        self._dibujar()
        self.app.log_ui("Clave del panel renovada: los celulares autorizados antes pierden el acceso.", "yellow")

    def action_cerrar(self):
        self.app.pop_screen()


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
        Binding("ctrl+w", "panel", "Panel web", priority=True),
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
        controlador=None,
        panel=None,
    ):
        super().__init__()
        self.plataforma = plataforma_inicial
        # La TUI es solo una vista: el estado y la logica viven en el
        # Controlador, que puede estar compartido con el panel web.
        self.ctrl = controlador or Controlador(puerto, calidad, low_latency, backend)
        self.panel = panel
        self._panel_activo = False
        self._hilo_ui = None
        self._firma_tvs = None
        self._firma_favs = None

    # El estado vive en el Controlador; estas propiedades lo exponen tal cual.
    @property
    def tv_actual(self):
        return self.ctrl.tv_actual

    @tv_actual.setter
    def tv_actual(self, tv):
        self.ctrl.elegir_tv(tv)

    @property
    def servidor(self):
        return self.ctrl.servidor

    @property
    def url_actual(self):
        return self.ctrl.url_actual

    @property
    def reproduciendo(self):
        return self.ctrl.reproduciendo

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
        self._hilo_ui = threading.get_ident()
        self.ctrl.suscribir(self._evento)
        self.query_one("#canal", Input).focus()
        self.cargar_favoritos()
        self.log_ui("Bienvenido. Buscando TVs en la red...")
        self._iniciar_panel()
        self.refrescar_tvs()
        # Chequeo periodico: si la TV deja de reproducir sola, que se note en pantalla.
        self.set_interval(15, self.verificar_estado)

    def on_unmount(self):
        self.ctrl.desuscribir(self._evento)

    def _iniciar_panel(self):
        if self.panel is None:
            return
        # Import tardio: web importa config y discovery, que la TUI no necesita sin panel.
        from .web import PanelOcupado

        try:
            self.panel.iniciar()
        except PanelOcupado as e:
            self.log_ui(str(e), "red")
            return
        self._panel_activo = True
        self.log_ui(f"Panel web en {self.panel.url}  (Ctrl+W muestra el QR para el celular)", "cyan")

    def action_panel(self):
        if not self._panel_activo:
            self.log_ui("El panel web esta apagado (se inicio con --no-web o su puerto estaba ocupado).", "yellow")
            return
        self.push_screen(PantallaQR(self.panel))

    # ---------- eventos del controlador ----------

    def _evento(self, evento, datos):
        """Llega desde cualquier thread; la UI solo se toca desde el suyo."""
        if threading.get_ident() == self._hilo_ui:
            self._aplicar(evento, datos)
            return
        try:
            self.call_from_thread(self._aplicar, evento, datos)
        except RuntimeError:
            # La app ya cerro: no hay nada que redibujar.
            pass

    def _aplicar(self, evento, datos):
        if evento == "log":
            color = COLORES_LOG.get(datos["nivel"], "white")
            self.log_ui(datos["texto"], color, hora=datos["hora"])
        elif evento == "estado":
            self.marcar_estado(datos["mensaje"], CLASES_FASE.get(datos["fase"], ""))
            self._sincronizar_tvs()
            self._sincronizar_favoritos()

    # ---------- helpers de UI ----------

    def log_ui(self, mensaje, color="white", hora=None):
        hora = hora or datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{hora}[/dim] [{color}]{mensaje}[/{color}]")

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
        favs = self.ctrl.favoritos()
        for fav in favs:
            lista.append(ItemFavorito(fav["canal"], fav["plataforma"]))
        self._firma_favs = tuple((f["canal"], f["plataforma"]) for f in favs)

    def _sincronizar_favoritos(self):
        firma = tuple((f["canal"], f["plataforma"]) for f in self.ctrl.favoritos())
        if firma != self._firma_favs:
            self.cargar_favoritos()

    def _sincronizar_tvs(self):
        """Redibuja la lista de TVs solo si cambio, y sigue a la TV elegida."""
        ctrl = self.ctrl
        lista = self.query_one("#lista-tvs", ListView)
        firma = (tuple(tv.udn for tv in ctrl.tvs), ctrl.udn_defecto)
        # Los Highlighted que genera el propio redibujado no son elecciones del
        # usuario: si llegaran, la TV elegida saltaria a la primera de la lista.
        with lista.prevent(ListView.Highlighted):
            if firma != self._firma_tvs:
                self._firma_tvs = firma
                lista.clear()
                for tv in ctrl.tvs:
                    lista.append(ItemTV(tv, tv.udn == ctrl.udn_defecto))
            if ctrl.tv_actual is not None:
                indice = next(
                    (i for i, tv in enumerate(ctrl.tvs) if tv.udn == ctrl.tv_actual.udn), None
                )
                if indice is not None and lista.index != indice:
                    lista.index = indice

    # ---------- descubrimiento ----------

    def action_refrescar(self):
        self.refrescar_tvs()

    @work(thread=True, exclusive=True, group="descubrir")
    def refrescar_tvs(self):
        self.ctrl.buscar_tvs()

    @on(ListView.Highlighted, "#lista-tvs")
    def cambiar_tv(self, evento):
        if isinstance(evento.item, ItemTV):
            if self.ctrl.tv_actual is None or evento.item.tv.udn != self.ctrl.tv_actual.udn:
                self.ctrl.elegir_tv(evento.item.tv)

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
        self.ctrl.alternar_favorito(canal, self.plataforma)

    def action_predeterminar(self):
        self.ctrl.predeterminar()

    # ---------- reproduccion ----------

    def action_reproducir(self):
        canal = self.query_one("#canal", Input).value.strip()
        if not canal:
            self.log_ui("Escribi un canal primero.", "yellow")
            return
        if self.ctrl.tv_actual is None:
            self.log_ui("No hay TV seleccionada.", "red")
            return
        self.iniciar_stream(canal, self.plataforma, self.ctrl.reservar())

    @work(thread=True, group="stream")
    def iniciar_stream(self, canal, plataforma, reserva):
        # El Controlador serializa los arranques y descarta los reemplazados.
        self.ctrl.reproducir(canal, plataforma, reserva)

    def action_detener(self):
        if self.ctrl.anunciar_detencion():
            self.detener_stream()

    @work(thread=True, group="stop")
    def detener_stream(self):
        self.ctrl.detener(anunciado=True)

    @work(thread=True, group="monitor", exclusive=True)
    def verificar_estado(self):
        """Si la TV se detuvo por su cuenta, reflejarlo en pantalla."""
        self.ctrl.verificar_estado()

    # ---------- salida ----------

    def limpiar(self):
        """Deja todo apagado. Va sincrono a proposito: un worker se cancelaria al salir."""
        if self._panel_activo:
            self.panel.detener()
            self._panel_activo = False
        self.ctrl.limpiar()

    def action_salir(self):
        self.limpiar()
        self.exit()


def lanzar(
    plataforma_inicial="kick",
    puerto=8090,
    calidad="best",
    low_latency=False,
    backend="auto",
    puerto_web=None,
):
    """Abre la TUI. Con puerto_web, tambien el panel para el celular."""
    ctrl = Controlador(puerto, calidad, low_latency, backend)
    panel = None
    if puerto_web:
        from .web import PanelWeb

        panel = PanelWeb(ctrl, puerto_web)
    app = StreamTVApp(plataforma_inicial, controlador=ctrl, panel=panel)
    try:
        app.run()
    finally:
        # Cubre Ctrl+C, cierre de la terminal y cualquier excepcion: sin este
        # finally, salir por esos caminos dejaba streamlink corriendo y la TV
        # reproduciendo.
        app.limpiar()
    return 0

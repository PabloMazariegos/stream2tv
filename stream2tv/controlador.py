"""Logica de reproduccion compartida por la TUI y el panel web.

El Controlador es el unico dueno del estado: que TVs hay, cual esta elegida,
que se reproduce y el servidor que alimenta a la TV. Las pantallas (TUI, web)
le piden acciones y se suscriben a sus eventos para redibujarse; asi dos
pantallas abiertas a la vez nunca se pelean por la TV.

Todos los metodos que tocan la red son bloqueantes: se llaman desde un thread.
"""

import collections
import re
import threading
from datetime import datetime

from . import caster, config, discovery, envivo
from .plataformas import resolver_url

# Fases que ve la UI. "verificando" y "enviando" son el arranque en curso.
LIBRE = "libre"
BUSCANDO = "buscando"
VERIFICANDO = "verificando"
ENVIANDO = "enviando"
REPRODUCIENDO = "reproduciendo"
# El backend se cayo y se esta relanzando sin cortar la conexion con la TV.
RECONECTANDO = "reconectando"
DETENIENDO = "deteniendo"
ERROR = "error"

LINEAS_LOG = 50
# Cada cuanto se consulta quien de los favoritos esta en vivo.
INTERVALO_EN_VIVO = 90

# Lo que imprime el plugin de Twitch de streamlink al filtrar publicidad.
_AVISO_ANUNCIO = re.compile(r"Detected advertisement break of (\d+) second")
_AVISO_PREROLL = "Waiting for pre-roll ads to finish"


class Controlador:
    def __init__(self, puerto=8090, calidad="best", low_latency=False, backend="auto", plataforma_inicial=None):
        self.puerto = puerto
        # Con que plataforma arranca el campo "Otro canal" (kick2tv, twitch2tv).
        self.plataforma_inicial = plataforma_inicial
        self.calidad = calidad
        self.low_latency = low_latency
        self.backend = backend

        self.tvs = []
        self.tv_actual = None
        self.udn_defecto, _ = config.tv_por_defecto()

        self.fase = LIBRE
        self.mensaje = "Iniciando..."
        self.canal = None
        self.plataforma = None
        self.url_actual = None
        self.desde = None
        self.servidor = None
        self.reproduciendo = False
        self.anuncios = 0
        # La TV donde esta el stream, que puede no ser la elegida: si el
        # usuario cambia de TV mientras mira, el Stop va a la que reproduce.
        self._tv_en_uso = None
        # Queda en True desde que se le manda algo a la TV hasta que un Stop
        # termina de verdad, para que salir siempre la apague.
        self._tv_tocada = False

        # Serializa arranque, stop y monitor. Un arranque puede tenerlo hasta
        # ~40 s mientras verifica el canal.
        self._lock = threading.Lock()
        # Protege los cambios cortos de estado (contadores, swap del servidor).
        self._lock_estado = threading.Lock()
        # Cada reproducir() o detener() lo incrementa; un arranque que al
        # terminar de verificar ve otro numero sabe que lo reemplazaron y se
        # descarta sin tocar la TV, venga el pedido de donde venga (panel, CLI).
        self._pedido = 0
        self._arranques = 0

        self.log_reciente = collections.deque(maxlen=LINEAS_LOG)
        self._suscriptores = []

        # Quien de los favoritos esta en vivo: {envivo.clave(...): estado}.
        self.en_vivo = {}
        self._sondeo = None
        self._fin_sondeo = threading.Event()
        self._despertar_sondeo = threading.Event()
        self._sondeo_fallando = False

    # ---------- eventos ----------

    def suscribir(self, funcion):
        """funcion(evento, datos): evento es "log" (dict) o "estado" (dict de estado())."""
        self._suscriptores.append(funcion)

    def desuscribir(self, funcion):
        if funcion in self._suscriptores:
            self._suscriptores.remove(funcion)

    def _emitir(self, evento, datos):
        for funcion in list(self._suscriptores):
            try:
                funcion(evento, datos)
            except Exception:
                # Una pantalla rota no debe frenar el stream ni a las demas.
                pass

    def log(self, texto, nivel="info"):
        entrada = {"hora": datetime.now().strftime("%H:%M:%S"), "texto": texto, "nivel": nivel}
        self.log_reciente.append(entrada)
        self._emitir("log", entrada)

    def _fase(self, fase, mensaje):
        self.fase = fase
        self.mensaje = mensaje
        self._emitir("estado", self.estado())

    def estado(self):
        """Foto del estado, serializable a JSON."""
        return {
            "fase": self.fase,
            "mensaje": self.mensaje,
            "canal": self.canal if self.fase != LIBRE else None,
            "plataforma": self.plataforma if self.fase != LIBRE else None,
            "url": self.url_actual,
            "desde": self.desde.isoformat(timespec="seconds") if self.desde else None,
            "tv": self.tv_actual.udn if self.tv_actual else None,
            "tv_defecto": self.udn_defecto,
            "tvs": [
                {"udn": tv.udn, "nombre": tv.visible, "original": tv.nombre, "alias": tv.alias, "ip": tv.host}
                for tv in self.tvs
            ],
            "favoritos": config.favoritos(),
            "anuncios": self.anuncios,
            "plataforma_inicial": self.plataforma_inicial,
            "en_vivo": self.en_vivo,
            "arrancando": self._arranques > 0,
        }

    # ---------- TVs ----------

    def buscar_tvs(self):
        # Mientras se reproduce, buscar no cambia la fase: el stream sigue.
        if not self.reproduciendo:
            self._fase(BUSCANDO, "Buscando TVs...")
        tvs = config.aplicar_alias(discovery.descubrir())
        if not tvs:
            self.log("Sin TVs. Revisa que este encendida y en la misma red.", "error")
            if self.reproduciendo:
                # Una busqueda que falla no corta lo que ya se esta viendo.
                self._emitir("estado", self.estado())
                return tvs
            self.tvs = []
            self.tv_actual = None
            self._fase(ERROR, "No se encontro ninguna TV. Prendela y apreta Ctrl+R.")
            return tvs
        self.tvs = tvs
        # Se conserva la elegida si sigue en la red; si no, la por defecto o la primera.
        udn_previo = self.tv_actual.udn if self.tv_actual else None
        self.tv_actual = (
            next((tv for tv in tvs if tv.udn == udn_previo), None)
            or next((tv for tv in tvs if tv.udn == self.udn_defecto), None)
            or tvs[0]
        )
        self.log(f"{len(tvs)} TV(s) encontrada(s).", "ok")
        if self.reproduciendo:
            self._emitir("estado", self.estado())
        else:
            self._fase(LIBRE, f"Listo. TV: {self.tv_actual.visible}")
        return tvs

    def elegir_tv(self, tv_o_udn, recordar=False):
        """Elige la TV para el proximo stream. No mueve el stream en curso.

        Con recordar=True queda ademas como TV por defecto para la proxima vez
        que se abra stream2tv (es lo que hace el panel al tocar una TV).
        """
        udn = getattr(tv_o_udn, "udn", tv_o_udn)
        tv = next((t for t in self.tvs if t.udn == udn), None)
        if tv is None and hasattr(tv_o_udn, "udn"):
            tv = tv_o_udn
        if tv is None:
            return False
        self.tv_actual = tv
        if recordar and tv.udn != self.udn_defecto:
            config.set_tv_por_defecto(tv.udn, tv.nombre)
            self.udn_defecto = tv.udn
        if self.fase in (LIBRE, ERROR):
            self._fase(LIBRE, f"Listo. TV: {tv.visible}")
        else:
            self._emitir("estado", self.estado())
        return True

    def predeterminar(self):
        if self.tv_actual is None:
            return False
        config.set_tv_por_defecto(self.tv_actual.udn, self.tv_actual.nombre)
        self.udn_defecto = self.tv_actual.udn
        self.log(f"'{self.tv_actual.visible}' guardada como TV por defecto.", "ok")
        self._emitir("estado", self.estado())
        return True

    def renombrar_tv(self, udn, alias):
        """Pone (o borra, con alias vacio) el nombre propio de una TV."""
        tv = next((t for t in self.tvs if t.udn == udn), None)
        if tv is None:
            return False
        anterior = tv.visible
        tv.alias = config.set_alias_tv(udn, alias)
        if tv.alias:
            self.log(f"'{tv.nombre}' ahora se llama '{tv.alias}'.", "ok")
        else:
            self.log(f"'{anterior}' vuelve a llamarse '{tv.nombre}'.", "ok")
        if self.fase == LIBRE and self.tv_actual is tv:
            self._fase(LIBRE, f"Listo. TV: {tv.visible}")
        else:
            self._emitir("estado", self.estado())
        return True

    # ---------- favoritos ----------

    def favoritos(self):
        return config.favoritos()

    def alternar_favorito(self, canal, plataforma):
        agregado = config.alternar_favorito(canal, plataforma)
        if agregado:
            # Que el nuevo muestre si esta en vivo sin esperar la proxima vuelta.
            self._despertar_sondeo.set()
        self.log(f"{'Agregado a' if agregado else 'Quitado de'} favoritos: {canal}", "ok")
        self._emitir("estado", self.estado())
        return agregado

    def reordenar_favoritos(self, orden):
        if not config.reordenar_favoritos(orden):
            return False
        self._emitir("estado", self.estado())
        return True

    # ---------- favoritos en vivo ----------

    def actualizar_en_vivo(self):
        """Consulta a las plataformas quien de los favoritos transmite. Bloqueante."""
        try:
            self.en_vivo = envivo.consultar(config.favoritos())
        except Exception as e:
            # Se avisa una vez por racha de fallas, no en cada vuelta.
            if not self._sondeo_fallando:
                self.log(f"No se pudo consultar quien esta en vivo: {e}", "aviso")
            self._sondeo_fallando = True
            return
        self._sondeo_fallando = False
        self._emitir("estado", self.estado())

    def iniciar_sondeo(self, intervalo=INTERVALO_EN_VIVO):
        """Consulta en segundo plano cada `intervalo` segundos hasta limpiar()."""
        if self._sondeo is not None:
            return
        self._fin_sondeo.clear()

        def bucle():
            while not self._fin_sondeo.is_set():
                self.actualizar_en_vivo()
                self._despertar_sondeo.wait(intervalo)
                self._despertar_sondeo.clear()

        self._sondeo = threading.Thread(target=bucle, daemon=True)
        self._sondeo.start()

    def detener_sondeo(self):
        self._fin_sondeo.set()
        self._despertar_sondeo.set()
        self._sondeo = None

    # ---------- reproduccion ----------

    def arrancando(self):
        return self._arranques > 0

    def reservar(self):
        """Anota un arranque y devuelve su numero. Rapido: se llama desde la UI.

        Se separa de reproducir() para que el arranque cuente desde el instante
        del click: si el thread tardara en empezar, un Detener inmediato no lo
        veria y el canal arrancaria igual.
        """
        with self._lock_estado:
            self._pedido += 1
            self._arranques += 1
            return self._pedido

    def reproducir(self, canal, plataforma, reserva=None):
        """Verifica el canal y lo manda a la TV elegida. Bloqueante.

        Devuelve True si quedo reproduciendo (o la TV lo acepto sin confirmar).
        """
        mio = reserva if reserva is not None else self.reservar()
        try:
            with self._lock:
                return self._reproducir(canal, plataforma, mio)
        finally:
            with self._lock_estado:
                self._arranques -= 1
            # Sin arranques pendientes, la UI tiene que enterarse (habilitar botones).
            if self._arranques == 0:
                self._emitir("estado", self.estado())

    def _vigente(self, mio):
        return mio == self._pedido

    def _reproducir(self, canal, plataforma, mio):
        if not self._vigente(mio):
            return False
        tv = self.tv_actual
        if tv is None:
            self.log("No hay TV seleccionada.", "error")
            self._fase(ERROR, "No hay TV seleccionada.")
            return False
        url = resolver_url(canal, plataforma)

        if self.reproduciendo or self.servidor is not None:
            self.log("Ya habia un stream activo, lo detengo primero.")
            self._fase(DETENIENDO, "Cambiando de canal...")
            self._detener_actual()

        self.canal, self.plataforma = canal, plataforma
        self._fase(VERIFICANDO, f"Verificando {canal}...")
        vivo, motivo = caster.canal_en_vivo(url, self.calidad, backend=self.backend)
        if not self._vigente(mio):
            # Mientras se verificaba se pidio otro canal o se apreto Detener.
            return False
        if not vivo:
            self.log(f"No hay stream: {motivo}", "error")
            self._fase(ERROR, f"{canal} no esta en vivo")
            return False

        try:
            mi_ip = discovery.ip_local_hacia(tv.host)
        except OSError as e:
            self.log(f"Error: {e}", "error")
            self._fase(ERROR, "No se llega a la TV")
            return False

        servidor = caster.Servidor(
            url,
            self.puerto,
            self.calidad,
            self.low_latency,
            log=self.log,
            backend=self.backend,
            host=mi_ip,
            cliente_permitido=tv.host,
            al_aviso=self._aviso_backend,
            al_estado=self._estado_backend,
        )
        try:
            servidor.iniciar()
        except caster.PuertoOcupado as e:
            self.log(str(e), "error")
            self._fase(ERROR, "Puerto ocupado")
            return False

        self.servidor = servidor
        self._tv_en_uso = tv
        self._tv_tocada = True
        try:
            stream_url = f"http://{mi_ip}:{self.puerto}{caster.RUTA_STREAM}"
            self._fase(ENVIANDO, f"Enviando {canal} a la TV...")
            arranco = caster.reproducir(tv, stream_url, f"{canal}", log=self.log)
        except Exception as e:
            self._soltar_servidor()
            self.log(f"Error: {e}", "error")
            self._fase(ERROR, "Error hablando con la TV")
            return False

        self.reproduciendo = True
        self.url_actual = url
        self.desde = datetime.now()
        self.anuncios = 0
        if arranco:
            self.log(f"Reproduciendo {url}", "ok")
            self._fase(REPRODUCIENDO, f"Reproduciendo {canal} en {tv.visible}")
        else:
            self._fase(REPRODUCIENDO, f"{canal}: la TV no confirmo, puede arrancar sola")
        return True

    def _aviso_backend(self, linea):
        """Lineas de stderr de streamlink, en vivo: se traducen las que importan."""
        encontrado = _AVISO_ANUNCIO.search(linea)
        if encontrado:
            self.anuncios += 1
            segundos = encontrado.group(1)
            self.log(f"Anuncio filtrado ({segundos} s): la imagen se congela mientras dura.", "aviso")
            self._emitir("estado", self.estado())
        elif _AVISO_PREROLL in linea:
            self.log("Twitch esta pasando un anuncio de entrada: el video arranca cuando termine.", "aviso")

    def _estado_backend(self, tipo, intento=None, total=None, motivo=None):
        """El servidor avisa que el backend se cayo, volvio o se rindio."""
        if not self.reproduciendo:
            return
        if tipo == "reconectando":
            self._fase(RECONECTANDO, f"Reconectando... intento {intento} de {total}")
        elif tipo == "recuperado":
            tv = self._tv_en_uso.visible if self._tv_en_uso else "la TV"
            self._fase(REPRODUCIENDO, f"Reproduciendo {self.canal} en {tv}")
        elif tipo == "terminado":
            self._fase(ERROR, "Se corto el stream y no se pudo recuperar")

    def _soltar_servidor(self):
        """Detiene el servidor una sola vez aunque lo pidan dos caminos a la vez."""
        with self._lock_estado:
            servidor, self.servidor = self.servidor, None
        if servidor is not None:
            servidor.detener()

    def _detener_actual(self):
        tv = self._tv_en_uso or self.tv_actual
        if tv is not None and self._tv_tocada:
            caster.detener_tv(tv, log=self.log)
        self._soltar_servidor()
        self.reproduciendo = False
        self.url_actual = None
        self.desde = None
        self._tv_en_uso = None
        self._tv_tocada = False

    def anunciar_detencion(self):
        """Invalida los arranques en curso. Rapido: se llama desde la UI al click.

        Es la mitad sincrona de detener(): si esperara al thread del stop, un
        arranque que termina de verificar en ese momento llegaria a la TV.
        Devuelve False si no habia nada que detener.
        """
        with self._lock_estado:
            hay_arranque = self._arranques > 0
            if not hay_arranque and not self.reproduciendo and self.servidor is None:
                nada = True
            else:
                nada = False
                self._pedido += 1
        if nada:
            self.log("No hay nada reproduciendose.", "aviso")
            return False
        if hay_arranque:
            self.log("Se cancela el arranque en curso.", "aviso")
        return True

    def detener(self, anunciado=False):
        """Corta el stream o cancela el arranque en curso. Bloqueante.

        Con anunciado=True se asume que ya se llamo a anunciar_detencion().
        Devuelve False si no habia nada que detener.
        """
        if not anunciado and not self.anunciar_detencion():
            return False
        self._fase(DETENIENDO, "Deteniendo...")
        with self._lock:
            self._detener_actual()
        self.log("Stream detenido.")
        self._fase(LIBRE, "Detenido.")
        return True

    def verificar_estado(self):
        """Si la TV se detuvo sola, lo refleja. Devuelve True si detecto el corte."""
        if not self.reproduciendo or self._tv_en_uso is None:
            return False
        # Mientras se reconecta, la TV puede mostrarse parada un momento: no
        # hay que soltar el servidor justo cuando el stream esta por volver.
        if self.fase == RECONECTANDO:
            return False
        # Si hay un arranque o un stop en curso, esta pasada no aplica.
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self.reproduciendo:
                return False
            if caster.estado_tv(self._tv_en_uso) != "STOPPED":
                return False
            self.reproduciendo = False
            self.url_actual = None
            self.desde = None
            # La TV ya esta parada: no hace falta mandarle Stop al salir.
            self._tv_tocada = False
            self._tv_en_uso = None
            self._soltar_servidor()
        finally:
            self._lock.release()
        self.log("La TV paso a STOPPED (se corto el stream o alguien la paro).", "error")
        self._fase(ERROR, "La TV detuvo la reproduccion.")
        return True

    # ---------- salida ----------

    def limpiar(self):
        """Deja todo apagado. Idempotente y sincrono: se llama salga por donde salga."""
        self.detener_sondeo()
        with self._lock_estado:
            self._pedido += 1
        tv = self._tv_en_uso or self.tv_actual
        if self._tv_tocada and tv is not None:
            try:
                caster.detener_tv(tv, log=lambda *_: None)
            except Exception:
                pass
            self._tv_tocada = False
        self._soltar_servidor()
        # Por si quedo algun servidor sin referencia (ej. un cambio de canal
        # interrumpido justo antes de salir).
        caster.apagar_todo()
        self.reproduciendo = False
        self._tv_en_uso = None

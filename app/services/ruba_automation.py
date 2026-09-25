"""
Carga integral de un servicio en RUBA a partir del payload de
`app/services/ruba_payload.py`, con los selectores de config/ruba_mapping.json.

Flujo (un paso por pantalla de RUBA):
    SESION         login, o reutiliza la sesión guardada (data/ruba_sesion.json)
                   o un Chrome ya abierto (cdp_url)
    INICIALIZACION /agregar: N° de parte, Tipo, Categoría
    GENERAL        /editar/{id}: ubicación, solicitante, reseña, civiles,
                   seguro y campos condicionales (forestal / estructural / accidente)
    DAMNIFICADOS   filas de heridos, solo si hay civiles heridos
    PARTICIPACION  horarios, lesionados y flags de intervención
    BOMBEROS       filas de bomberos con tipo de tarea y Encargado
    VEHICULOS      móviles con chofer y horarios + guardado final

Cada paso informa su avance por `on_progreso(EventoProgreso)`; si falla se
guarda captura + HTML en logs/screenshots/ y se lanza `RubaAutomationError`
con el paso, el detalle y la ruta de la captura.

Las claves del payload se llaman igual que los selectores del mapping, así
que cada paso recorre el payload y resuelve el selector por nombre.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.core.catalogos import leer_mapping
from app.services.ruba_payload import TIPO_VEHICULO_DEFAULT
from app.paths import configurar_entorno_playwright, get_writable_dir, is_frozen
from app.services.ruba_helpers import (
    SELECTORES_CLAVE,
    URL_INCIDENTE_DEFAULT,
    URL_LOGIN_DEFAULT,
    VIEWPORT,
    _sembrar_tab_activa,
    cargar_credenciales,
    detectar_campos_invalidos,
    detectar_error_servidor,
    extraer_errores_validacion,
    fijar_intervinientes_personas,
    fijar_punto_ubicacion,
    iniciar_sesion,
    leer_punto_ubicacion,
    liberar_condicionales_vacios,
    esperar_nuevo_indice,
    indices_coleccion,
    liberar_required_vacios,
    limpiar_required_ocultos,
)

log = logging.getLogger(__name__)


class PasoRuba(str, enum.Enum):
    SESION = "Sesión"
    INICIALIZACION = "Inicialización"
    GENERAL = "Datos generales"
    DAMNIFICADOS = "Damnificados"
    PARTICIPACION = "Participación"
    BOMBEROS = "Bomberos intervinientes"
    VEHICULOS = "Vehículos intervinientes"


PASOS = list(PasoRuba)


@dataclass(frozen=True)
class EventoProgreso:
    paso: PasoRuba
    estado: str        # "inicio" | "ok" | "omitido" | "error"
    porcentaje: int    # avance total 0-100
    mensaje: str = ""


class RubaAutomationError(RuntimeError):
    def __init__(self, paso: PasoRuba, detalle: str, captura: Optional[Path] = None) -> None:
        self.paso = paso
        self.detalle = detalle
        self.captura = captura
        texto = f"[{paso.value}] {detalle}"
        if captura:
            texto += f" (captura: {captura})"
        super().__init__(texto)


@dataclass
class ResultadoRuba:
    ruba_id_remoto: Optional[str]
    url_final: str
    advertencias: List[str] = field(default_factory=list)


# Listas de sugerencias de los autocompletes más comunes (jQuery UI, typeahead,
# Select2, ARIA). RUBA usa jQuery UI; el resto es red de seguridad.
SELECTORES_SUGERENCIA = [
    ".ui-autocomplete li",
    ".ui-menu-item",
    ".tt-suggestion",
    ".autocomplete-suggestion",
    ".select2-results__option",
    "[role='option']",
]

_JS_SETEAR_VALOR = r"""
([sel, valor]) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.value = valor;
    for (const tipo of ["input", "change", "blur"]) el.dispatchEvent(new Event(tipo, { bubbles: true }));
    if (window.jQuery) { try { window.jQuery(el).trigger("change"); } catch (e) {} }
    return true;
}
"""

_JS_SETEAR_PICKER = r"""
([sel, valor]) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    el.value = valor;
    const $ = window.jQuery;
    if ($) {
        const $el = $(el);
        try { if ($el.data('datepicker')) $el.datepicker('update', valor); } catch (e) {}
        try { if ($el.data('timepicker')) $el.timepicker('setTime', valor); } catch (e) {}
        $el.trigger('input').trigger('change');
        try { $('.datepicker.dropdown-menu, .bootstrap-timepicker-widget.dropdown-menu').hide(); } catch (e) {}
    }
    for (const tipo of ["input", "change"]) el.dispatchEvent(new Event(tipo, { bubbles: true }));
    return el.value;
}
"""

# Campos accesorios: nunca se espera el timeout general (30 s) por ellos.
TIMEOUT_OPCIONAL_MS = 1500
TIMEOUT_SEGURO_MS = 2000
# Solo si RUBA muestra "Datos del Seguro" como OBLIGATORIO (formulario de
# Incendios: inputs required, sin checkbox) y el parte no tiene seguro: lo
# que RUBA ya aceptó en cargas reales (captura del 24/09).
SEGURO_SIN_DATOS = {"compania_seguro": "Sin datos", "numero_poliza": "00000000"}

_JS_OPCIONES = "(el) => Array.from(el.options || []).map(o => ({value: o.value, text: o.textContent.trim()}))"


def _normalizar(texto: str) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", sin_acentos).upper().split())


def _limpiar_para_archivo(texto: str) -> str:
    return re.sub(r"[^\w.-]+", "_", texto).strip("_") or "servicio"


# ---------------------------------------------------------------------------
# Lanzamiento de Chromium con fallback
# ---------------------------------------------------------------------------

class NavegadorNoDisponibleError(RuntimeError):
    """Ninguna de las 3 estrategias pudo abrir un navegador. No es un problema
    del parte: la UI lo muestra aparte, con las instrucciones."""


MENSAJE_SIN_NAVEGADOR = (
    "No se encontró un navegador para conectarse a RUBA.\n\n"
    "Fire Station necesita UNO de estos:\n"
    "  • Google Chrome o Microsoft Edge instalados en esta PC (lo más simple), o\n"
    "  • el Chromium de Playwright: en una consola, ejecutar\n"
    "        playwright install chromium\n"
    "  • o reinstalar Fire Station con el instalador completo (trae su propio Chromium).\n\n"
    "Los partes no se modificaron: siguen pendientes de carga."
)

# Ejecutables dentro de %LOCALAPPDATA%\ms-playwright (por revisión). El
# headless shell es el que usa Playwright con headless=True; chrome.exe sirve
# para ambos modos. `headless_shell.exe` es el nombre en versiones viejas.
_PATRONES_HEADLESS = ("chromium_headless_shell-*/*/chrome-headless-shell.exe",
                      "chromium_headless_shell-*/*/headless_shell.exe")
_PATRONES_CHROME = ("chromium-*/*/chrome.exe",)


def _revision_esperada() -> Optional[str]:
    """Revisión de Chromium que pide el paquete playwright en uso (la del
    bundle o la del entorno de desarrollo), leída de su browsers.json."""
    try:
        import playwright

        ruta = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        for navegador in json.loads(ruta.read_text(encoding="utf-8"))["browsers"]:
            if navegador.get("name") == "chromium":
                return str(navegador["revision"])
    except Exception:  # noqa: BLE001 - es solo para ordenar candidatos
        return None
    return None


def _revision_de(exe: Path) -> int:
    m = re.search(r"-(\d+)$", exe.parent.parent.name)
    return int(m.group(1)) if m else 0


def ejecutables_en_cache_usuario(headless: bool = True) -> List[Path]:
    """Chromium instalados con `playwright install` en la cuenta del usuario
    (%LOCALAPPDATA%\\ms-playwright). Primero la revisión que espera Playwright,
    después las demás de la más nueva a la más vieja."""
    base_local = os.environ.get("LOCALAPPDATA")
    if not base_local:
        return []
    base = Path(base_local) / "ms-playwright"
    if not base.is_dir():
        return []
    patrones = (_PATRONES_HEADLESS + _PATRONES_CHROME) if headless else _PATRONES_CHROME
    encontrados: List[Path] = []
    for patron in patrones:
        encontrados.extend(sorted(base.glob(patron), key=_revision_de, reverse=True))
    esperada = _revision_esperada()
    return sorted(encontrados, key=lambda exe: 0 if esperada and exe.parent.parent.name.endswith(f"-{esperada}") else 1)


def lanzar_chromium(p, headless: bool = True):
    """Abre Chromium probando, en orden:

      A. El Chromium de Playwright que corresponde a esta instalación: en el
         .exe, el embebido en _internal/ms-playwright (configurar_entorno_
         playwright apunta PLAYWRIGHT_BROWSERS_PATH ahí); en desarrollo, el
         caché del usuario.
      B. El navegador de Windows: Google Chrome (channel="chrome") y, si no,
         Microsoft Edge (channel="msedge", viene con Windows 10/11).
      C. Cualquier Chromium de `playwright install` en %LOCALAPPDATA%\\ms-playwright,
         por ruta directa (executable_path).

    Devuelve (browser, descripción). Si todo falla: NavegadorNoDisponibleError."""
    intentos: List[str] = []

    def intentar(descripcion: str, **opciones):
        try:
            navegador = p.chromium.launch(headless=headless, **opciones)
        except PlaywrightError as e:
            primera_linea = (str(e).strip().splitlines() or [type(e).__name__])[0]
            intentos.append(f"{descripcion}: {primera_linea}")
            log.warning("RUBA: no se pudo abrir %s -> %s", descripcion, primera_linea)
            return None
        log.info("RUBA: navegador en uso -> %s", descripcion)
        return navegador

    # A
    embebido = "Chromium embebido (_internal/ms-playwright)" if is_frozen() else "Chromium de Playwright"
    navegador = intentar(embebido)
    if navegador:
        return navegador, embebido
    # B
    for canal, nombre in (("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")):
        navegador = intentar(nombre, channel=canal)
        if navegador:
            return navegador, nombre
    # C
    for exe in ejecutables_en_cache_usuario(headless):
        descripcion = f"Chromium del usuario ({exe.parent.parent.name})"
        navegador = intentar(descripcion, executable_path=str(exe))
        if navegador:
            return navegador, descripcion
    if not any(i.startswith("Chromium del usuario") for i in intentos):
        intentos.append("Chromium del usuario: no hay ninguno en %LOCALAPPDATA%\\ms-playwright")

    log.error("RUBA: ningún navegador disponible:\n  %s", "\n  ".join(intentos))
    raise NavegadorNoDisponibleError(MENSAJE_SIN_NAVEGADOR + "\n\nDetalle técnico:\n• " + "\n• ".join(intentos))


@contextmanager
def navegador_compartido(
    *, headless: bool = True, cdp_url: Optional[str] = None, ruta_sesion: Optional[Path] = None,
) -> Iterator[tuple]:
    """Abre Playwright + Chromium una sola vez y entrega (contexto, propio).
    `propio=False` si se conectó por CDP a un Chrome del usuario: en ese caso
    no se cierra el navegador (solo nuestras pestañas). Todo en el mismo
    hilo: la API sync de Playwright no se puede compartir entre hilos."""
    configurar_entorno_playwright()
    with sync_playwright() as p:
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            contexto = browser.contexts[0] if browser.contexts else browser.new_context(viewport=VIEWPORT)
            yield contexto, False
            return
        browser, _ = lanzar_chromium(p, headless=headless)
        estado = str(ruta_sesion) if ruta_sesion is not None and ruta_sesion.exists() else None
        try:
            yield browser.new_context(viewport=VIEWPORT, storage_state=estado), True
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass


class RubaServiceAutomation:
    """Carga un servicio completo en RUBA.

        automatizacion = RubaServiceAutomation(payload, on_progreso=print)
        resultado = automatizacion.ejecutar()   # ResultadoRuba o RubaAutomationError
    """

    def __init__(
        self,
        payload: Dict[str, Any],
        *,
        credenciales: Optional[Dict[str, str]] = None,
        headless: bool = True,
        on_progreso: Optional[Callable[[EventoProgreso], None]] = None,
        dir_capturas: Optional[Path] = None,
        ruta_sesion: Optional[Path] = None,
        cdp_url: Optional[str] = None,
        mapping: Optional[Dict[str, Any]] = None,
        timeout_ms: int = 30000,
        timeout_navegacion_ms: int = 45000,
        ruba_id_existente: Optional[str] = None,
        on_id_remoto: Optional[Callable[[str], None]] = None,
    ) -> None:
        """`ruba_id_existente`: si una corrida anterior ya creó el incidente en
        RUBA, se retoma en /editar/{id} en vez de inicializar otro (evita
        partes duplicados). `on_id_remoto` se llama apenas RUBA asigna el ID,
        para persistirlo aunque un paso posterior falle.

        `timeout_ms`: espera de elementos/acciones. `timeout_navegacion_ms`:
        cargas de página y guardados que navegan (POST + redirect) -- RUBA
        suele tardar bastante más en eso, sobre todo al primer acceso."""
        self.payload = payload
        self.credenciales = cargar_credenciales() if credenciales is None else credenciales
        self.headless = headless
        self.on_progreso = on_progreso
        self.dir_capturas = dir_capturas or get_writable_dir("logs") / "screenshots"
        self.ruta_sesion = ruta_sesion or get_writable_dir("data") / "ruba_sesion.json"
        self.cdp_url = cdp_url
        self.timeout_ms = timeout_ms
        self.timeout_navegacion_ms = timeout_navegacion_ms

        self.mapping = leer_mapping() if mapping is None else mapping
        self.sel: Dict[str, Any] = self.mapping.get("selectores", {})
        self.url_login = (self.credenciales.get("url_login") or "").strip() or URL_LOGIN_DEFAULT
        self.url_incidentes = (self.credenciales.get("url_incidentes") or "").strip() or URL_INCIDENTE_DEFAULT
        self.url_agregar = self.url_incidentes.rstrip("/") + "/agregar"

        self.page: Optional[Page] = None
        self.advertencias: List[str] = []
        self.ruba_id_remoto: Optional[str] = ruba_id_existente
        self.on_id_remoto = on_id_remoto
        self._paso_activo = PasoRuba.SESION

    # ------------------------------------------------------------------
    # Orquestación
    # ------------------------------------------------------------------

    def ejecutar(self) -> ResultadoRuba:
        """Un servicio suelto: abre el navegador, carga y lo cierra."""
        with self.abrir_navegador() as (contexto, propio):
            return self.ejecutar_en_contexto(contexto, guardar_sesion=propio)

    def abrir_navegador(self):
        """Context manager -> (BrowserContext, propio) con la configuración de
        ESTA automatización (headless, sesión guardada, CDP). Sirve para
        reutilizar un mismo navegador en una carga en lote (ruba_service)."""
        return navegador_compartido(headless=self.headless, cdp_url=self.cdp_url, ruta_sesion=self.ruta_sesion)

    def ejecutar_en_contexto(self, contexto: BrowserContext, guardar_sesion: bool = True) -> ResultadoRuba:
        """Carga el servicio en una pestaña NUEVA de un navegador ya abierto y
        la cierra al terminar (el navegador sigue vivo para el próximo parte).
        Si el contexto ya tiene la sesión de RUBA, el paso Sesión no vuelve a
        loguear. `guardar_sesion`: persistir el storage_state tras un login."""
        self.page = contexto.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.page.set_default_navigation_timeout(self.timeout_navegacion_ms)
        try:
            with self._paso(PasoRuba.SESION):
                self._asegurar_sesion(contexto, guardar=guardar_sesion)
            with self._paso(PasoRuba.INICIALIZACION):
                self._inicializar()
            with self._paso(PasoRuba.GENERAL):
                self._cargar_general()
            with self._paso(PasoRuba.DAMNIFICADOS):
                self._cargar_damnificados()
            with self._paso(PasoRuba.PARTICIPACION):
                self._cargar_participacion()
            with self._paso(PasoRuba.BOMBEROS):
                self._cargar_bomberos()
            with self._paso(PasoRuba.VEHICULOS):
                self._cargar_vehiculos_y_guardar()
            return ResultadoRuba(self.ruba_id_remoto, self.page.url, list(self.advertencias))
        finally:
            try:
                self.page.close()
            except PlaywrightError:
                pass  # el navegador ya se cayó: lo detecta el que lo abrió

    @contextmanager
    def _paso(self, paso: PasoRuba) -> Iterator[None]:
        indice = PASOS.index(paso)
        self._paso_activo = paso
        self._emitir(paso, "inicio", int(100 * indice / len(PASOS)), f"{paso.value}…")
        try:
            yield
        except _PasoOmitido as omision:
            self._emitir(paso, "omitido", int(100 * (indice + 1) / len(PASOS)), str(omision))
            return
        except Exception as e:  # noqa: BLE001 - se re-lanza tipado, con captura
            captura = self._capturar(paso)
            detalle = e.detalle if isinstance(e, RubaAutomationError) else f"{type(e).__name__}: {e}"
            self._emitir(paso, "error", int(100 * indice / len(PASOS)), detalle)
            raise RubaAutomationError(paso, detalle, captura) from e
        self._emitir(paso, "ok", int(100 * (indice + 1) / len(PASOS)), f"{paso.value} completo.")

    def _emitir(self, paso: PasoRuba, estado: str, porcentaje: int, mensaje: str) -> None:
        log.info("RUBA %s [%s] %s%% %s", paso.value, estado, porcentaje, mensaje)
        if self.on_progreso:
            try:
                self.on_progreso(EventoProgreso(paso, estado, porcentaje, mensaje))
            except Exception:  # noqa: BLE001 - un callback de UI roto no corta la carga
                log.exception("on_progreso falló")

    def _capturar(self, paso: PasoRuba) -> Optional[Path]:
        if self.page is None:
            return None
        self.dir_capturas.mkdir(parents=True, exist_ok=True)
        base = self.dir_capturas / (
            f"{datetime.now():%Y%m%d_%H%M%S}_{_limpiar_para_archivo(self.payload.get('numero_parte_local', ''))}"
            f"_{paso.name.lower()}"
        )
        try:
            self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".html").write_text(self.page.content(), encoding="utf-8")
            return base.with_suffix(".png")
        except Exception:  # noqa: BLE001 - la captura es diagnóstico, nunca tapa el error real
            log.exception("No se pudo guardar la captura de %s", paso.value)
            return None

    # ------------------------------------------------------------------
    # Pasos
    # ------------------------------------------------------------------

    def _asegurar_sesion(self, contexto: BrowserContext, guardar: bool) -> None:
        page = self.page
        self._ir_a(self.url_incidentes)
        self._esperar_red()
        if not self._pide_login():
            return  # sesión reutilizada
        usuario = (self.credenciales.get("usuario") or "").strip()
        clave = self.credenciales.get("clave") or ""
        if not usuario or not clave:
            raise RubaAutomationError(PasoRuba.SESION, "Faltan usuario/clave de RUBA en data/config.json.")
        if not iniciar_sesion(page, usuario, clave, self.url_login) or self._pide_login():
            raise RubaAutomationError(PasoRuba.SESION, "RUBA rechazó el login (revisá usuario/clave).")
        if guardar:
            self.ruta_sesion.parent.mkdir(parents=True, exist_ok=True)
            contexto.storage_state(path=str(self.ruta_sesion))

    def _pide_login(self) -> bool:
        if "login" in self.page.url.lower():
            return True
        return any(self._visible(sel) for sel in SELECTORES_CLAVE)

    def _inicializar(self) -> None:
        datos = self.payload["inicializacion"]
        sel = self.sel["inicializacion"]
        page = self.page
        if self.ruba_id_remoto:
            self._ir_a(f"{self.url_incidentes.rstrip('/')}/editar/{self.ruba_id_remoto}")
            self._esperar_red()
            if "/editar/" not in page.url:
                raise RubaAutomationError(
                    PasoRuba.INICIALIZACION,
                    f"No se pudo abrir el incidente existente {self.ruba_id_remoto} en RUBA (quedó en {page.url}).",
                )
            raise _PasoOmitido(f"El incidente ya existía en RUBA (ID {self.ruba_id_remoto}): se retoma la carga.")

        self._ir_a(self.url_agregar)
        page.locator(sel["numero_parte"]).first.wait_for(state="visible")

        self._llenar(sel["numero_parte"], datos["numero_parte"])
        self._seleccionar(sel["tipo_incidente"], datos["tipo_incidente"])
        # Categoría se repuebla por AJAX al cambiar el Tipo.
        page.wait_for_function(
            "([s, v]) => { const el = document.querySelector(s); "
            "return !!el && Array.from(el.options).some(o => o.value === v); }",
            arg=[sel["categoria_incidente"], datos["categoria_incidente"]],
        )
        self._seleccionar(sel["categoria_incidente"], datos["categoria_incidente"])
        self._tildar(sel["hay_participaciones"], bool(datos.get("hay_participaciones")))
        limpiar_required_ocultos(page)

        url_antes = page.url
        page.locator(sel["btn_guardar"]).first.click()
        self._esperar_salida_de(url_antes, "/agregar")
        self.ruba_id_remoto = _extraer_id(page.url)
        if self.ruba_id_remoto and self.on_id_remoto:
            self.on_id_remoto(self.ruba_id_remoto)

    def _cargar_general(self) -> None:
        datos = self.payload["editar_general"]
        sel = self.sel["editar_general"]
        self._esperar_formulario_general(sel["calle"])

        if datos.get("localidad_autocomplete"):
            self._cargar_localidad(sel["localidad_autocomplete"], datos["localidad_autocomplete"])
        self._llenar(sel["calle"], datos.get("calle"))
        self._llenar(sel["altura"], datos.get("altura"))
        if datos.get("tipo_zona"):
            self._seleccionar_por_texto(sel["tipo_zona"], datos["tipo_zona"])
        self._confirmar_punto_en_mapa(sel)
        self._asegurar_punto_fijado(sel, datos)

        for clave in ("nombre_solicitante", "apellido_solicitante", "descripcion"):
            self._llenar(sel[clave], datos.get(clave))
        for clave in ("telefono_solicitante", "dni_solicitante"):  # accesorios: sin esperas largas
            self._llenar_opcional(sel[clave], datos.get(clave))
        self._cargar_seguro(sel, datos)
        self._cargar_personas_damnificadas(sel, datos)

        self._cargar_condicionales(datos.get("condicionales"))
        self._cargar_vehiculos_accidente(self.payload.get("vehiculos_accidentes") or [])
        if self.payload.get("damnificados", {}).get("bienes"):
            self.advertencias.append(
                "Hay bienes afectados cargados, pero ruba_mapping.json no tiene sus selectores: "
                "completarlos a mano en RUBA."
            )
        liberados = liberar_condicionales_vacios(
            self.page, sel.get("liberar_si_vacios") or ("otraLocalidad", "fechaVencimientoSeguro", "otroTipoLugarForestal")
        )
        if liberados:
            log.info("Paso 3: condicionales vacíos liberados antes del guardado: %s", liberados)
        # Sin víctimas RUBA saltea Damnificados y va directo a Participación.
        self._guardar_y_continuar(
            sel["btn_guardar_continuar"],
            destinos=(self.sel["damnificados"]["url_patron"], self.sel["participacion"]["url_patron"]),
        )

    def _cargar_seguro(self, sel: Dict[str, Any], datos: Dict[str, Any]) -> None:
        """"Datos del Seguro", condicionado al parte local.

        - Parte SIN seguro: no se toca ningún checkbox ni se espera nada. Solo
          si el formulario de este tipo YA tiene el bloque en pantalla como
          obligatorio (Incendios) se escribe "Sin datos" para que el guardado
          no rebote; si no existe (otros tipos) se sigue de largo al instante.
        - Parte CON seguro: se tilda el checkbox si el mapping define uno
          (`check_seguro`), se espera el campo como máximo 2 s y se completan
          compañía y póliza. Si RUBA no muestra el bloque, advertencia."""
        sel_compania, sel_poliza = sel["compania_seguro"], sel["numero_poliza"]
        if not datos.get("tiene_seguro"):
            compania = self.page.locator(sel_compania).first
            if compania.count() == 0:  # sin esperar: el formulario no tiene seguro
                log.info("Paso 3: parte sin seguro y formulario sin 'Datos del Seguro': se omite.")
                return
            if not (compania.is_visible() and compania.get_attribute("required") is not None):
                log.info("Paso 3: parte sin seguro; 'Datos del Seguro' no es obligatorio acá: no se toca.")
                return
            for clave, selector in (("compania_seguro", sel_compania), ("numero_poliza", sel_poliza)):
                self._llenar_opcional(selector, SEGURO_SIN_DATOS[clave], timeout_ms=0)
            return

        check = sel.get("check_seguro")
        if check and self.page.locator(check).count():
            self._tildar(check, True)
        # UNA sola espera corta para todo el bloque (máx. 2 s): que aparezca y, si
        # lo revela un checkbox, que quede visible antes de escribir.
        try:
            self.page.locator(sel_compania).first.wait_for(state="visible", timeout=TIMEOUT_SEGURO_MS)
        except PlaywrightError:
            pass  # oculto o ausente: _llenar_opcional decide sin volver a esperar
        cargado = False
        for clave, selector in (("compania_seguro", sel_compania), ("numero_poliza", sel_poliza)):
            cargado = self._llenar_opcional(selector, datos.get(clave), timeout_ms=0) or cargado
        if not cargado:
            self.advertencias.append(
                "El parte tiene seguro pero RUBA no mostró 'Datos del Seguro' para este tipo de incidente: "
                "compañía y póliza no se cargaron."
            )

    def _cargar_localidad(self, selector: str, texto: str) -> None:
        """Localidad es un par Symfony: <input type=hidden id=..._localidad>
        (el ID que se envía) + el input visible autocomplete_..._localidad
        (jQuery UI, filtra por la Provincia ya elegida). Se escribe en el
        visible y se elige la sugerencia; si RUBA solo ofrece "Otra
        Localidad", se completa ese campo de texto libre."""
        campo = self._visible_de(selector)
        if (campo.get_attribute("id") or "").startswith("autocomplete_"):
            objetivo = _normalizar(texto)
            self._autocompletar(campo, texto, lambda s: _normalizar(s).startswith(objetivo), texto,
                                permitir_primero=True)
        else:
            campo.fill(texto)
            campo.dispatch_event("change")

    def _confirmar_punto_en_mapa(self, sel: Dict[str, Any]) -> None:
        """Georreferencia por dirección (Buscar puntos -> primer resultado).
        Si RUBA no ofrece resultados queda como advertencia: el guardado
        dirá si el punto era obligatorio."""
        if not self._existe(sel["btn_buscar_puntos"]):
            self.advertencias.append("RUBA no mostró el botón 'Buscar puntos': ubicación sin georreferenciar.")
            return
        try:
            self._tildar(sel["radio_direccion"], True)
            self.page.locator(sel["btn_buscar_puntos"]).first.click()
            combo = sel["combo_direccion_confirmar"]
            self.page.wait_for_function(
                "(s) => { const el = document.querySelector(s); "
                "return !!el && Array.from(el.options).some(o => o.value); }",
                arg=combo, timeout=min(self.timeout_ms, 8000),
            )
            primera = next(o["value"] for o in self.page.locator(combo).first.evaluate(_JS_OPCIONES) if o["value"])
            self._seleccionar(combo, primera)
        except (PlaywrightError, StopIteration) as e:
            if "Timeout" in str(e):
                log.info("Buscar Puntos sin resultados: %s", e)  # _asegurar_punto_fijado avisa y fija el punto
            else:
                self.advertencias.append(f"No se pudo confirmar el punto en el mapa: {e}")

    ESPERA_REINTENTO_500_MS = 3000

    def _esperar_formulario_general(self, selector_calle: str) -> None:
        """Espera el formulario de Datos Generales. Si RUBA respondió con su
        página de error (500), recarga UNA vez -- suele ser transitorio -- y
        si sigue fallando corta con un mensaje claro (el parte queda
        pendiente y el ID remoto guardado para retomar), en vez del
        TimeoutError genérico de esperar un campo que no existe."""
        for intento in (1, 2):
            error = detectar_error_servidor(self.page)
            if error is None:
                try:
                    self.page.locator(selector_calle).first.wait_for(state="attached", timeout=self.timeout_ms)
                    return
                except PlaywrightError:
                    error = detectar_error_servidor(self.page)
                    if error is None:
                        titulo = _evaluar_titulo(self.page)
                        raise RubaAutomationError(
                            PasoRuba.GENERAL,
                            f"La pantalla de Datos Generales no mostró el bloque de ubicación ({selector_calle}). "
                            f"URL: {self.page.url} -- título: '{titulo}'. Ver la captura guardada.",
                        ) from None
            if intento == 1:
                log.warning("RUBA devolvió error del servidor en %s (%s); se reintenta.", self.page.url, error)
                self.page.wait_for_timeout(self.ESPERA_REINTENTO_500_MS)
                try:
                    self.page.reload(wait_until="domcontentloaded")
                except PlaywrightError:
                    pass
                self._esperar_red()
        incidente = f"el incidente {self.ruba_id_remoto}" if self.ruba_id_remoto else "el incidente"
        raise RubaAutomationError(
            PasoRuba.GENERAL,
            f"RUBA devolvió un error del servidor al abrir los Datos Generales ({error}), también al "
            f"reintentar. Es una falla del portal, no de los datos del parte. Probá abrir {incidente} a mano "
            f"en RUBA ({self.page.url}); si tampoco abre, avisale al soporte de RUBA. El parte queda "
            f"pendiente para reintentar.",
        )

    def _asegurar_punto_fijado(self, sel: Dict[str, Any], datos: Dict[str, Any]) -> None:
        """Si "Buscar Puntos" no fijó el punto (sin resultados: zona rural,
        calle sin numeración), Symfony rechaza el guardado. Se fija a mano
        -- como arrastrar el pin -- con las coordenadas del parte o, si el
        parte no las tiene, con las del cuartel (queda advertencia)."""
        sel_lat = sel.get("latitud") or "input[id$='_ubicacion_latitud']"
        sel_lng = sel.get("longitud") or "input[id$='_ubicacion_longitud']"
        actual = leer_punto_ubicacion(self.page, sel_lat, sel_lng)
        if actual is None:
            self.advertencias.append("RUBA no mostró los campos de latitud/longitud: ubicación sin fijar.")
            return
        if all(v.strip() for v in actual):
            return  # Buscar Puntos ya lo fijó
        lat, lng = datos.get("latitud"), datos.get("longitud")
        if lat is None or lng is None:
            from app.services.cartografia import cargar_calibracion

            calibracion = cargar_calibracion()
            lat, lng = calibracion.cuartel_lat, calibracion.cuartel_lon
            self.advertencias.append(
                f"Sin punto de Google ni coordenadas en el parte: se fijó la ubicación del cuartel "
                f"({lat:.5f}, {lng:.5f}). Corregir el pin en RUBA si hace falta."
            )
        else:
            self.advertencias.append(
                f"'Buscar Puntos' no encontró la dirección: se fijó el punto marcado en el mapa del parte "
                f"({lat:.5f}, {lng:.5f})."
            )
        if fijar_punto_ubicacion(self.page, lat, lng, sel_lat, sel_lng) is None:
            self.advertencias.append("No se pudieron escribir latitud/longitud en RUBA: ubicación sin fijar.")

    def _cargar_personas_damnificadas(self, sel: Dict[str, Any], datos: Dict[str, Any]) -> None:
        """"Personas Damnificadas": el combo si/no va ANTES que los contadores
        (su JS `setearCeros` los resetea al cambiar). Sin víctimas: "no" y los
        tres en "0" explícito -- en "si" con todo en 0 RUBA responde "Debe
        indicar alguna persona"."""
        contadores = {clave: int(datos.get(clave) or 0)
                      for clave in ("civiles_heridos", "civiles_fallecidos", "civiles_desaparecidos")}
        selector = sel.get("hay_intervinientes_personas") or "select[id$='_hayIntervinientesPersonas']"
        if not fijar_intervinientes_personas(self.page, any(contadores.values()), selector):
            self.advertencias.append("RUBA no mostró el combo '¿Intervinientes personas?': se cargan solo los contadores.")
        for clave, valor in contadores.items():
            self._llenar(sel[clave], str(valor))  # "0" también se escribe

    def _cargar_condicionales(self, condicionales: Optional[Dict[str, Any]]) -> None:
        if not condicionales or not condicionales.get("formulario"):
            return
        formulario = condicionales["formulario"]
        bloque = self.sel["editar_general"].get("condicionales", {}).get(formulario)
        if not bloque:
            raise RubaAutomationError(PasoRuba.GENERAL, f"ruba_mapping.json no define '{formulario}'.")
        campos = self._completar_obligatorios(formulario, bloque, dict(condicionales.get("campos") or {}))
        if not campos:
            return  # nada que cargar: no se espera un bloque que quizá RUBA no muestre
        primer_selector = next(
            v for k, v in bloque.items() if isinstance(v, str) and not k.endswith(("_default", "_opciones"))
        )
        self.page.locator(primer_selector).first.wait_for(state="attached")
        for campo, valor in campos.items():
            selector = bloque.get(campo)
            if selector is None:
                self.advertencias.append(f"Campo condicional sin selector en el mapping: {formulario}.{campo}")
            elif isinstance(selector, dict):  # grupo de radios {"si": "#..._0", ...}
                self._tildar(selector[valor], True)
            elif f"{campo}_opciones" in bloque:
                self._seleccionar(selector, str(valor))
            else:
                self._llenar(selector, valor)

    def _completar_obligatorios(self, formulario: str, bloque: Dict[str, Any],
                                campos: Dict[str, Any]) -> Dict[str, Any]:
        """Campos obligatorios de RUBA sin dato en el parte (clima y causa del
        accidente, causa del incendio): se carga `<campo>_default` del mapping
        para que el guardado no rebote, y queda una advertencia para revisar."""
        for clave, valor in bloque.items():
            if not clave.endswith("_default") or valor in (None, ""):
                continue
            campo = clave[: -len("_default")]
            if campos.get(campo) not in (None, ""):
                continue
            campos[campo] = str(valor)
            nombre = next((k.replace("_", " ").capitalize()
                           for k, v in (bloque.get(f"{campo}_opciones") or {}).items() if str(v) == str(valor)),
                          str(valor))
            self.advertencias.append(
                f"{formulario}: '{campo}' no estaba cargado en el parte; en RUBA se cargó '{nombre}' por defecto."
            )
        return campos

    def _cargar_vehiculos_accidente(self, vehiculos: List[Dict[str, Any]]) -> None:
        """Vehículos damnificados del formulario de Accidente (incidenteAccidenteType):
        por cada uno, "agregar vehículo" -> esperar la fila nueva -> completar
        marca, dominio, modelo y año; el seguro solo si está asegurado."""
        if not vehiculos:
            return
        sel = self.sel.get("vehiculos_accidentes")
        if not sel:
            self.advertencias.append("Hay vehículos del accidente pero ruba_mapping.json no tiene "
                                     "'vehiculos_accidentes': cargarlos a mano en RUBA.")
            return
        prefijo, campos = sel["prefijo_id"], sel["campos"]
        if not self._existe(sel["boton_agregar"]):
            raise RubaAutomationError(PasoRuba.GENERAL, "RUBA no muestra el botón para agregar vehículos del accidente.")

        for n, v in enumerate(vehiculos, start=1):
            anteriores = indices_coleccion(self.page, prefijo)
            self.page.locator(sel["boton_agregar"]).first.click()
            try:
                indice = esperar_nuevo_indice(self.page, prefijo, anteriores, timeout=self.timeout_ms)
            except PlaywrightError as e:
                raise RubaAutomationError(PasoRuba.GENERAL, f"RUBA no generó la fila del vehículo {n}: {e}") from e
            fila = f"{prefijo}_{indice}_"
            campo = lambda clave: f"#{fila}{campos[clave]}"  # noqa: E731
            self.page.locator(campo("dominio")).first.wait_for(state="attached")

            if v.get("marca"):
                try:
                    self._seleccionar(campo("marca"), v["marca"])
                except RubaAutomationError:
                    otra = sel.get("marcas", {}).get("Otra")
                    if not otra:
                        raise
                    self._seleccionar(campo("marca"), otra)
                    self.advertencias.append(f"Vehículo {n}: la marca '{v.get('marca_nombre')}' no existe en RUBA; "
                                             "se cargó como 'Otra'.")
            self._seleccionar_tipo_vehiculo(campo("tipo"), v.get("tipo"), n)
            for clave in ("dominio", "modelo", "anio"):
                self._llenar(campo(clave), v.get(clave))

            asegurado = bool(v.get("asegurado"))
            self._setear_asegurado(campo("asegurado"), asegurado)
            if campos.get("airbag") and self.page.locator(campo("airbag")).count():
                self._elegir_si_vacio(campo("airbag"), ("no posee", "sin datos", "no"))
            if asegurado:
                for clave in ("aseguradora", "poliza"):
                    if v.get(clave) and not self._llenar_opcional(campo(clave), v.get(clave),
                                                                  timeout_ms=TIMEOUT_SEGURO_MS):
                        self.advertencias.append(f"Vehículo {n}: RUBA no mostró el campo '{clave}' del seguro.")

            liberados = liberar_required_vacios(self.page, fila)
            if liberados:
                log.info("Vehículo %s: sin dato en %s (required liberado)", n, ", ".join(liberados))
        self._completar_selects_vehiculos()

    # -- Selects de cada vehículo del Accidente (tipo / asegurado / airbag) ---------------

    SEL_VEHICULOS_ACCIDENTE = "select[name*='[datosVehiculosAccidentes]'][name$='[{campo}]']"

    def _seleccionar_tipo_vehiculo(self, selector: str, valor: Optional[str], n: int) -> None:
        """select_option(value=...) con el value ya mapeado en el payload
        (ruba_payload.tipo_vehiculo_ruba). Nunca queda en "Seleccionar"."""
        loc = self.page.locator(selector).first
        if not loc.count():
            return  # formulario sin tipo: lo cubre la pasada final por name
        valor = valor or TIPO_VEHICULO_DEFAULT
        disponibles = [o["value"] for o in loc.evaluate(_JS_OPCIONES)]
        if valor not in disponibles:
            self.advertencias.append(f"Vehículo {n}: RUBA no ofrece el tipo {valor}; se cargó 'Transito > Autos'.")
            valor = TIPO_VEHICULO_DEFAULT if TIPO_VEHICULO_DEFAULT in disponibles else next(
                (v for v in disponibles if v), "")
        if valor:
            loc.select_option(value=valor)

    def _setear_asegurado(self, selector: str, asegurado: bool) -> None:
        """"Asegurado" puede ser checkbox o <select> (Si / No / Sin datos)."""
        loc = self.page.locator(selector).first
        if not loc.count():
            return
        if loc.evaluate("(e) => e.tagName") == "SELECT":
            preferidos = ("si",) if asegurado else ("no", "sin datos")
            self._elegir_opcion(selector, preferidos)
        else:
            self._tildar(selector, asegurado)

    def _elegir_opcion(self, selector: str, preferidos: tuple, solo_si_vacio: bool = False) -> Optional[str]:
        """Elige en un <select> la primera opción cuyo texto coincide con
        `preferidos` (sin acentos ni mayúsculas); si ninguna coincide, la
        primera opción NO vacía. Devuelve el value elegido (None si no tocó)."""
        loc = self.page.locator(selector).first
        if solo_si_vacio and (loc.input_value() or "").strip():
            return None
        opciones = [o for o in loc.evaluate(_JS_OPCIONES) if (o["value"] or "").strip()]
        if not opciones:
            return None
        textos = {o["value"]: _normalizar(o["text"]) for o in opciones}
        for preferido in preferidos:
            objetivo = _normalizar(preferido)
            elegida = next((v for v, t in textos.items() if t == objetivo), None) or next(
                (v for v, t in textos.items() if t.startswith(objetivo)), None)
            if elegida:
                break
        else:
            elegida = opciones[0]["value"]
        loc.select_option(value=elegida)
        return elegida

    def _elegir_si_vacio(self, selector: str, preferidos: tuple) -> Optional[str]:
        return self._elegir_opcion(selector, preferidos, solo_si_vacio=True)

    def _completar_selects_vehiculos(self) -> None:
        """Red de seguridad sobre TODAS las filas (select_loc.nth(i)): ningún
        tipo / asegurado / airbag de un vehículo del Accidente se envía vacío."""
        por_defecto = (("tipo", None), ("asegurado", ("no", "sin datos")), ("airbag", ("no posee", "sin datos", "no")))
        for campo, preferidos in por_defecto:
            selects = self.page.locator(self.SEL_VEHICULOS_ACCIDENTE.format(campo=campo))
            for i in range(selects.count()):
                select = selects.nth(i)
                if (select.input_value() or "").strip():
                    continue
                nombre = select.get_attribute("name") or f"{campo} #{i + 1}"
                if campo == "tipo":
                    select.select_option(value=TIPO_VEHICULO_DEFAULT)
                    elegido = TIPO_VEHICULO_DEFAULT
                else:
                    selector = f"select[name='{nombre}']"
                    elegido = self._elegir_opcion(selector, preferidos)
                log.info("Vehículo del accidente: %s vacío -> %s", nombre, elegido)

    def _cargar_damnificados(self) -> None:
        sel = self.sel["damnificados"]
        url = self.page.url
        if self.sel["participacion"]["url_patron"] in url:
            raise _PasoOmitido("Sin víctimas: RUBA salteó Damnificados y pasó a Participación.")
        if sel["url_patron"] not in url:
            raise RubaAutomationError(PasoRuba.DAMNIFICADOS, f"RUBA quedó en una pantalla inesperada: {url}")
        heridos = self.payload["damnificados"]["heridos"]
        if self.payload["editar_general"].get("civiles_heridos", 0) > 0 and heridos:
            base = self._indice_base(sel["fila_herido"]["nombre"])
            if base is None:
                raise RubaAutomationError(PasoRuba.DAMNIFICADOS, "RUBA no generó las filas de heridos.")
            for i, herido in enumerate(heridos):
                fila = {k: v.format(i=base + i) for k, v in sel["fila_herido"].items()}
                for clave in ("nombre", "apellido", "dni"):
                    self._llenar(fila[clave], herido.get(clave))
                if herido.get("genero"):
                    self._seleccionar(fila["genero"], herido["genero"])
        if any(f.get("nombre") or f.get("apellido") for f in self.payload["damnificados"]["fallecidos"]):
            self.advertencias.append("Hay fallecidos individualizados pero el mapping no tiene selectores para ellos.")
        self._guardar_y_continuar(sel["btn_guardar_continuar"], destinos=(self.sel["participacion"]["url_patron"],))
        if not heridos:
            raise _PasoOmitido("Sin civiles heridos: se continuó sin cargar filas.")

    def _cargar_participacion(self) -> None:
        datos = self.payload["participacion"]
        sel = self.sel["participacion"]
        self.page.locator(sel["numero_parte"]).first.wait_for(state="attached")
        for clave, valor in datos.items():
            if clave not in sel:
                continue
            if isinstance(valor, bool):
                self._setear_booleano(sel[clave], valor)
            else:
                self._llenar(sel[clave], valor)
        self._guardar_y_continuar(sel["btn_guardar_continuar"])

    def _cargar_bomberos(self) -> None:
        datos = self.payload["intervencion_bomberos"]
        sel = self.sel["intervencion_bomberos"]
        if not datos["bomberos"]:
            if self._existe(sel["cantidad_bomberos"]):
                self._guardar_y_continuar(sel["btn_guardar_continuar"])
            raise _PasoOmitido("Sin bomberos intervinientes.")

        self.page.locator(sel["cantidad_bomberos"]).first.wait_for(state="attached")
        self._llenar(sel["cantidad_bomberos"], datos["cantidad_bomberos"])
        base = self._asegurar_filas(sel["fila_bombero"]["autocomplete_nombre"], len(datos["bomberos"]),
                                    sel["btn_agregar_filas"])
        for i, bombero in enumerate(datos["bomberos"]):
            fila = {k: v.format(i=base + i) for k, v in sel["fila_bombero"].items()}
            self._autocompletar_persona(fila["autocomplete_nombre"], bombero["autocomplete_nombre"])
            for clave in ("fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin"):
                self._llenar(fila[clave], bombero.get(clave))
            if bombero.get("tipo_tarea"):
                self._seleccionar(fila["tipo_tarea"], bombero["tipo_tarea"])
            self._tildar(fila["is_encargado"], bool(bombero.get("is_encargado")))
        self._guardar_y_continuar(sel["btn_guardar_continuar"])

    def _cargar_vehiculos_y_guardar(self) -> None:
        vehiculos = self.payload["intervencion_vehiculos"]["vehiculos"]
        sel = self.sel["intervencion_vehiculos"]
        if not vehiculos:
            raise _PasoOmitido("Sin vehículos intervinientes.")
        self.page.locator(sel["btn_agregar_fila"]).first.wait_for(state="attached")
        base = self._asegurar_filas(sel["fila_vehiculo"]["select_vehiculo"], len(vehiculos), sel["btn_agregar_fila"])
        for i, vehiculo in enumerate(vehiculos):
            fila = {k: v.format(i=base + i) for k, v in sel["fila_vehiculo"].items()}
            self._seleccionar(fila["select_vehiculo"], vehiculo["select_vehiculo"])
            if vehiculo.get("autocomplete_chofer"):
                self._autocompletar_persona(fila["autocomplete_chofer"], vehiculo["autocomplete_chofer"])
            for clave in ("fecha_salida", "hora_salida", "fecha_llegada", "hora_llegada"):
                self._llenar(fila[clave], vehiculo.get(clave))

        _sembrar_tab_activa(self.page)
        url_antes = self.page.url
        self.page.locator(sel["btn_guardar_cambios"]).first.click()
        try:
            self.page.wait_for_url(lambda u: u != url_antes, timeout=self.timeout_navegacion_ms)
        except PlaywrightError:
            pass  # RUBA puede quedarse en la misma pantalla tras un guardado AJAX
        self._esperar_red()
        errores = extraer_errores_validacion(self.page)
        if errores:
            raise RubaAutomationError(
                PasoRuba.VEHICULOS,
                f"RUBA rechazó el guardado final: {errores} campos_invalidos={detectar_campos_invalidos(self.page)}",
            )
        self.ruba_id_remoto = self.ruba_id_remoto or _extraer_id(self.page.url)

    # ------------------------------------------------------------------
    # Primitivas de interacción
    # ------------------------------------------------------------------

    def _ir_a(self, url: str) -> None:
        """Navegación principal: espera solo el DOM ('domcontentloaded'), no
        el evento 'load' -- RUBA carga Google Maps y scripts pesados que no
        hacen falta para operar el formulario Symfony y pueden demorar (o no
        terminar nunca). Si ni el DOM llega a tiempo, el error lo dice claro."""
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_navegacion_ms)
        except PlaywrightTimeoutError:
            raise RubaAutomationError(
                self._paso_en_curso(),
                f"RUBA no respondió en {self.timeout_navegacion_ms // 1000} s al abrir {url} "
                "(portal lento o caído, o sin internet). El parte queda pendiente: reintentá en unos minutos.",
            ) from None

    def _esperar_red(self) -> None:
        for estado, timeout in (("domcontentloaded", self.timeout_ms), ("networkidle", 5000)):
            try:
                self.page.wait_for_load_state(estado, timeout=timeout)
            except PlaywrightError:
                pass

    def _existe(self, selector: str) -> bool:
        return self.page.locator(selector).count() > 0

    def _visible(self, selector: str) -> bool:
        loc = self.page.locator(selector)
        return loc.count() > 0 and loc.first.is_visible()

    def _llenar(self, selector: str, valor: Any) -> None:
        """Escribe un valor. Los date/timepicker de RUBA suelen ser readonly:
        ahí se setea por JS y se disparan los eventos que escuchan."""
        if valor is None or valor == "":
            return
        texto = str(valor)
        loc = self.page.locator(selector).first
        loc.wait_for(state="attached")
        if _es_picker(selector):
            self._setear_picker(selector, texto)
            return
        if loc.is_visible() and loc.is_editable():
            loc.fill(texto)
            loc.dispatch_event("change")
        elif not self.page.evaluate(_JS_SETEAR_VALOR, [selector, texto]):
            raise RubaAutomationError(self._paso_en_curso(), f"No existe el campo {selector}.")

    def _llenar_opcional(self, selector: str, valor: Any, timeout_ms: int = TIMEOUT_OPCIONAL_MS) -> bool:
        """Como `_llenar`, para campos ACCESORIOS: si el campo no aparece en
        `timeout_ms` (nunca el timeout general de 30 s) se sigue sin error.
        Devuelve True si se escribió algo."""
        if valor is None or valor == "":
            return False
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            if timeout_ms <= 0:
                return False
            try:
                loc.wait_for(state="attached", timeout=timeout_ms)
            except PlaywrightError:
                log.info("Campo accesorio ausente, se omite: %s", selector)
                return False
        texto = str(valor)
        if _es_picker(selector):
            self._setear_picker(selector, texto)
            return True
        try:
            if loc.is_visible() and loc.is_editable():
                loc.fill(texto, timeout=max(timeout_ms, TIMEOUT_OPCIONAL_MS))
                loc.dispatch_event("change")
                return True
        except PlaywrightError as e:
            log.info("No se pudo escribir %s (%s): se intenta por JS.", selector, e)
        return bool(self.page.evaluate(_JS_SETEAR_VALOR, [selector, texto]))

    def _setear_picker(self, selector: str, valor: str) -> None:
        """Bootstrap date/timepicker: readonly y con plugin -- el valor se
        inyecta por JS (y por la API del plugin si está), con input/change."""
        resultado = self.page.evaluate(_JS_SETEAR_PICKER, [selector, valor])
        if resultado is None:
            raise RubaAutomationError(self._paso_en_curso(), f"No existe el campo {selector}.")
        if resultado != valor:
            self.advertencias.append(f"{selector}: RUBA reformateó '{valor}' como '{resultado}'.")

    def _visible_de(self, selector: str):
        """Primer elemento VISIBLE e interactuable (nunca un type=hidden) que
        matchea el selector; espera hasta el timeout."""
        limite = datetime.now().timestamp() + self.timeout_ms / 1000
        while True:
            candidatos = self.page.locator(selector)
            for k in range(candidatos.count()):
                el = candidatos.nth(k)
                try:
                    if el.get_attribute("type") != "hidden" and el.is_visible() and el.is_enabled():
                        return el
                except PlaywrightError:
                    continue
            if datetime.now().timestamp() >= limite:
                raise RubaAutomationError(self._paso_en_curso(), f"No hay ningún campo visible para {selector}.")
            self.page.wait_for_timeout(150)

    def _seleccionar(self, selector: str, valor: str) -> None:
        loc = self.page.locator(selector).first
        loc.wait_for(state="attached")
        opciones = loc.evaluate(_JS_OPCIONES)
        if not any(o["value"] == str(valor) for o in opciones):
            raise RubaAutomationError(
                self._paso_en_curso(), f"La opción '{valor}' no existe en {selector} (¿cambió el portal?).",
            )
        self.page.evaluate(_JS_SETEAR_VALOR, [selector, str(valor)])
        if self.page.evaluate("(s) => !!window.jQuery", selector):
            self.page.evaluate(
                "([s, v]) => window.jQuery(document.querySelector(s))"
                ".trigger({type: 'select2:select', params: {data: {id: v}}})",
                [selector, str(valor)],
            )

    def _seleccionar_por_texto(self, selector: str, texto: str) -> None:
        opciones = self.page.locator(selector).first.evaluate(_JS_OPCIONES)
        objetivo = _normalizar(texto)
        elegida = next((o for o in opciones if o["value"] and _normalizar(o["text"]) == objetivo), None) or next(
            (o for o in opciones if o["value"] and objetivo in _normalizar(o["text"])), None
        )
        if elegida is None:
            self.advertencias.append(f"'{texto}' no está entre las opciones de {selector}.")
            return
        self._seleccionar(selector, elegida["value"])

    def _tildar(self, selector: str, marcado: bool) -> None:
        loc = self.page.locator(selector).first
        loc.wait_for(state="attached")
        try:
            loc.set_checked(marcado, timeout=3000)
        except PlaywrightError:  # oculto tras un widget: se fuerza por JS
            self.page.evaluate(
                "([s, m]) => { const el = document.querySelector(s); el.checked = m; "
                "el.dispatchEvent(new Event('change', {bubbles: true})); }",
                [selector, marcado],
            )

    def _setear_booleano(self, selector: str, valor: bool) -> None:
        """Los Sí/No de RUBA pueden ser checkbox o <select>."""
        loc = self.page.locator(selector).first
        loc.wait_for(state="attached")
        etiqueta = loc.evaluate("(el) => el.tagName.toLowerCase() + ':' + (el.type || '')")
        if etiqueta.startswith("select"):
            opciones = loc.evaluate(_JS_OPCIONES)
            buscados = ("SI", "1", "TRUE") if valor else ("NO", "0", "FALSE")
            elegida = next((o for o in opciones if _normalizar(o["text"]) in buscados or o["value"] in buscados), None)
            if elegida is None:
                raise RubaAutomationError(self._paso_en_curso(), f"No hay opción Sí/No reconocible en {selector}.")
            self._seleccionar(selector, elegida["value"])
        else:
            self._tildar(selector, valor)

    def _autocompletar_persona(self, selector: str, persona: Dict[str, Any]) -> None:
        """Tipea el apellido y elige la sugerencia que coincide con la persona:
        primero por DNI, si no por apellido + primer nombre."""
        apellido = _normalizar(persona["apellido"])
        primer_nombre = (_normalizar(persona.get("nombre") or "").split() or [""])[0]
        dni = persona.get("dni")

        def coincide(sugerencia: str) -> bool:
            s = _normalizar(sugerencia)
            if dni and dni in re.sub(r"\D", "", sugerencia):
                return True
            return apellido in s and primer_nombre in s

        self._autocompletar(self._visible_de(selector), persona["texto_busqueda"], coincide, persona["nombre_completo"])

    def _autocompletar(
        self, campo, busqueda: str, coincide: Callable[[str], bool], descripcion: str,
        permitir_primero: bool = False,
    ) -> None:
        """Autocomplete de RUBA (jQuery UI): escribe en el input VISIBLE con
        delay, espera la lista de sugerencias y elige la que coincide (o la
        primera, si `permitir_primero`). Si no aparece la lista, prueba
        ArrowDown + Enter. Reintenta sin acentos si con acentos no hay
        resultados. Al final verifica el <input type=hidden> asociado."""
        sin_acentos = "".join(
            c for c in unicodedata.normalize("NFKD", busqueda) if not unicodedata.combining(c)
        )
        for intento in dict.fromkeys((busqueda, sin_acentos)):
            campo.click()
            campo.fill("")
            campo.press_sequentially(intento, delay=60)
            sugerencias = self._esperar_sugerencias(min(self.timeout_ms, 8000) / 2)
            if sugerencias is None:
                continue
            textos = sugerencias.all_inner_texts()
            indice = next((k for k, t in enumerate(textos) if coincide(t)), None)
            if indice is None and permitir_primero:
                indice = 0
                self.advertencias.append(f"'{descripcion}': se eligió la primera sugerencia de RUBA ({textos[0]}).")
            if indice is None:
                raise RubaAutomationError(
                    self._paso_en_curso(), f"Ninguna sugerencia coincide con '{descripcion}': {textos[:5]}",
                )
            sugerencias.nth(indice).click()
            self._verificar_oculto_asociado(campo, descripcion)
            return

        # Sin lista visible: último recurso con teclado sobre el propio widget.
        campo.press("ArrowDown")
        campo.press("Enter")
        if not self._verificar_oculto_asociado(campo, descripcion):
            raise RubaAutomationError(self._paso_en_curso(), f"RUBA no sugirió resultados para '{busqueda}'.")

    def _verificar_oculto_asociado(self, campo, descripcion: str) -> bool:
        """El autocomplete visible (autocomplete_X) guarda el ID elegido en el
        hidden X. Si quedó vacío se le saca `required` (no debe bloquear el
        navegador) y se deja constancia: el servidor dirá si era obligatorio."""
        id_visible = campo.get_attribute("id") or ""
        if not id_visible.startswith("autocomplete_"):
            return True
        vinculado = self.page.evaluate(
            """(id) => { const el = document.getElementById(id);
                if (!el) return true;
                if (el.value) return true;
                el.removeAttribute('required');
                return false; }""",
            id_visible[len("autocomplete_"):],
        )
        if not vinculado:
            self.advertencias.append(f"'{descripcion}': RUBA no vinculó la selección (campo oculto vacío).")
        return bool(vinculado)

    def _esperar_sugerencias(self, timeout_ms: float):
        limite = datetime.now().timestamp() + timeout_ms / 1000
        while datetime.now().timestamp() < limite:
            for css in SELECTORES_SUGERENCIA:
                candidatas = self.page.locator(f"{css}:visible")
                if candidatas.count():
                    return candidatas
            self.page.wait_for_timeout(150)
        return None

    def _indice_base(self, patron: str) -> Optional[int]:
        """Las colecciones de Symfony numeran desde 0 (a veces desde 1)."""
        for base in (0, 1):
            if self._existe(patron.format(i=base)):
                return base
        return None

    def _asegurar_filas(self, patron: str, cantidad: int, selector_boton: str) -> int:
        """Hace click en "agregar fila" hasta que exista la fila `cantidad`.
        Devuelve el índice base de las filas."""
        for _ in range(cantidad + 2):
            base = self._indice_base(patron)
            if base is not None and self._existe(patron.format(i=base + cantidad - 1)):
                return base
            self.page.locator(selector_boton).first.click()
            self.page.wait_for_timeout(200)
        raise RubaAutomationError(self._paso_en_curso(), f"No se pudieron generar {cantidad} filas ({patron}).")

    def _guardar_y_continuar(self, selector_boton: str, destinos: Optional[tuple] = None) -> None:
        """Guarda y verifica ADÓNDE llevó RUBA. El form hace POST a la misma
        URL: si hay errores de validación, RUBA re-renderiza esa URL; si
        salió bien, redirige (302) a la pantalla siguiente. `destinos`
        restringe las pantallas válidas (ej. damnificados o participación)."""
        _sembrar_tab_activa(self.page)
        url_antes = self.page.url
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=self.timeout_navegacion_ms):
                self.page.locator(selector_boton).first.click()
        except PlaywrightError:
            raise RubaAutomationError(
                self._paso_en_curso(),
                f"El guardado no envió el formulario: {extraer_errores_validacion(self.page) or 'sin errores visibles'}",
            ) from None
        self._esperar_red()
        url = self.page.url
        if destinos and any(d in url for d in destinos):
            return
        errores = extraer_errores_validacion(self.page)
        if url == url_antes or errores:
            raise RubaAutomationError(
                self._paso_en_curso(),
                f"RUBA rechazó el guardado: {errores or 'sin errores visibles'} "
                f"campos_invalidos={detectar_campos_invalidos(self.page)}",
            )
        if destinos:
            raise RubaAutomationError(
                self._paso_en_curso(), f"RUBA llevó a {url}; se esperaba alguna de {list(destinos)}.",
            )

    def _esperar_salida_de(self, url_antes: str, fragmento: str) -> None:
        try:
            self.page.wait_for_url(lambda u: fragmento not in u, timeout=self.timeout_navegacion_ms)
        except PlaywrightError:
            raise RubaAutomationError(
                PasoRuba.INICIALIZACION,
                f"RUBA no aceptó la inicialización: {extraer_errores_validacion(self.page) or 'sin errores visibles'}",
            ) from None
        self._esperar_red()

    def _paso_en_curso(self) -> PasoRuba:
        return self._paso_activo


def _evaluar_titulo(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


class _PasoOmitido(Exception):
    """Un paso que no aplica a este servicio (sin heridos, sin bomberos...)."""


def _extraer_id(url: str) -> Optional[str]:
    coincidencia = re.search(r"/incidente/(?:editar/|damnificados/)?(\d+)(?:[/?#]|$)", url)
    return coincidencia.group(1) if coincidencia else None


def _es_picker(selector: str) -> bool:
    """IDs de los Bootstrap date/timepicker de RUBA (#datepicker_..., #timepicker_...)."""
    return "datepicker_" in selector or "timepicker_" in selector

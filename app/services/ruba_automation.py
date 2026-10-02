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
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.core.catalogos import leer_mapping
from app.services.ruba_payload import GENERO_DEFAULT, TIPO_VEHICULO_DEFAULT, genero_ruba
from app.paths import configurar_entorno_playwright, get_writable_dir, is_frozen
from app.services.ruba_helpers import (
    CONDICIONALES_LIBERABLES_DEFAULT,
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
    estado: str        # "inicio" | "ok" | "omitido" | "aviso" | "error"
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
    # ID viejo que no abrió en RUBA (500 / timeout / sin formulario) y se
    # reemplazó por un servicio creado desde cero; None si no hubo reemplazo.
    ruba_id_descartado: Optional[str] = None
    aviso_recreado: Optional[str] = None


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
# Campos condicionales de Tipo/Subtipo (tipoLugarForestal, tipoLugar, causaIncendio...):
# si RUBA no los dibuja en 2 s, no aplican a este subtipo y se omiten.
TIMEOUT_CONDICIONAL_MS = 2000
# Pantalla de Vehículos intervinientes. Las filas se buscan por su row_id real
# (la colección Symfony no arranca en 0) y nunca en el prototipo `__name__`.
PREFIJO_VEHICULOS = "bomberos_estructurabundle_intervencionType_vehiculos"
SEL_FILAS_VEHICULO = (
    "select[name*='[vehiculos]'][name$='[vehiculo]']:not([name*='__name__']), "
    f"select[id^='{PREFIJO_VEHICULOS}_'][id$='_vehiculo']:not([id*='__name__'])"
)
SEL_BTN_AGREGAR_VEHICULO = "a:has-text('+ agregar'), button:has-text('+ agregar')"
PATRONES_ROW_VEHICULO = (r"_vehiculos_(\d+)_vehiculo", r"\[vehiculos\]\[(\d+)\]")
# Pantalla de Bomberos intervinientes: misma idea. Cada fila se reconoce por
# su autocomplete de bombero o su select de Tipo de tarea (lo que esté visible).
PREFIJO_BOMBEROS = "bomberos_estructurabundle_intervencionType_bomberos"
SEL_FILAS_BOMBERO = ", ".join((
    f"input[id^='autocomplete_{PREFIJO_BOMBEROS}_'][id$='_bombero']:not([id*='__name__'])",
    f"select[id^='{PREFIJO_BOMBEROS}_'][id$='_tipoTarea']:not([id*='__name__'])",
    "select[name*='[bomberos]'][name$='[tipoTarea]']:not([name*='__name__'])",
    "input[name*='[bomberos]'][name$='[bombero]']:not([name*='__name__'])",
))
PATRONES_ROW_BOMBERO = (r"_bomberos_(\d+)_(?:bombero|tipoTarea)$", r"\[bomberos\]\[(\d+)\]")
SEL_BTN_AGREGAR_BOMBERO = "a:has-text('+ agregar'), button:has-text('+ agregar'), a:has-text('Agregar bomberos')"
TEXTO_TIPO_TAREA = {"1": "INTERVINIENTE", "2": "APRESTO"}
# Pantalla de Damnificados: filas de heridos (Heridos_{row_id}_nombre...). RUBA
# las arma según "Civiles heridos" y tampoco las numera desde 0 fijo.
SEL_FILAS_HERIDO = ", ".join((
    "input[id^='Heridos_'][id$='_nombre']:not([id*='__name__'])",
    "input[id^='Heridos_'][id$='_apellido']:not([id*='__name__'])",
    "select[id^='Heridos_'][id$='_genero']:not([id*='__name__'])",
    "[name^='Heridos'][name*='[nombre]']:not([name*='__name__'])",
))
PATRONES_ROW_HERIDO = (r"^Heridos_(\d+)_(?:nombre|apellido|dni|genero)$", r"^Heridos_?\[?(\d+)\]?\[")
SEL_BTN_AGREGAR_HERIDO = "a:has-text('+ agregar'), button:has-text('+ agregar')"

# Botón/ícono de eliminar una fila de colección: se busca DENTRO del
# contenedor de esa fila (el ancestro más grande que no contiene controles de
# otra fila). Clases, title, onclick o texto con estas palabras.
_JS_MARCAR_BORRAR_FILA = r"""
([selAnclas, patrones, rowId, marca]) => {
    const regexes = patrones.map((p) => new RegExp(p));
    const rowDe = (el) => {
        for (const a of [el.id || "", el.getAttribute("name") || ""])
            for (const r of regexes) { const m = a.match(r); if (m) return m[1]; }
        return null;
    };
    const anclas = Array.from(document.querySelectorAll(selAnclas)).filter((el) => rowDe(el) === rowId);
    if (!anclas.length) return "sin_fila";
    // "minus": el círculo rojo con "-" de RUBA (glyphicon-minus-sign / fa-minus-circle).
    const palabras = /(delete|remove|eliminar|borrar|quitar|trash|tacho|remover|minus)/i;
    const esBorrar = (el) => /^[-−–]$/.test((el.textContent || "").trim()) || palabras.test([
        el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className,
        el.getAttribute("title"), el.getAttribute("onclick"), el.getAttribute("data-action"),
        el.getAttribute("aria-label"), (el.textContent || "").trim().slice(0, 30),
    ].join(" "));
    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    let nodo = anclas[0].parentElement;
    while (nodo && nodo !== document.body) {
        const deOtraFila = Array.from(nodo.querySelectorAll(selAnclas))
            .some((el) => { const r = rowDe(el); return r !== null && r !== rowId; });
        if (deOtraFila) return "sin_boton";
        const candidato = Array.from(nodo.querySelectorAll("a, button, input[type=button], i, span"))
            .filter(esBorrar)
            .map((el) => el.closest("a, button, input[type=button]") || el)
            .find(visible);
        if (candidato) { candidato.setAttribute("data-ruba-borrar", marca); return "ok"; }
        nodo = nodo.parentElement;
    }
    return "sin_boton";
}
"""
SEL_BTN_GUARDAR_CAMBIOS = "button:has-text('Guardar cambios'), input[value='Guardar cambios']"
# Contadores de "Bomberos Damnificados de éste cuerpo" (pantalla Participación).
CONTADORES_BOMBEROS_DAMNIFICADOS = ("bomberos_heridos", "bomberos_fallecidos", "bomberos_desaparecidos")
TIMEOUT_SEGURO_MS = 2000
# select_option / check sobre filas dinámicas de vehículos: nunca 30 s.
TIMEOUT_FILA_MS = 3000
# "Datos del Seguro" de RUBA. Sin seguro en el parte quedan en blanco (nunca
# se escriben valores ficticios); solo se les quita el `required` HTML5 para
# que el navegador no frene el guardado por un campo que el parte no tiene.
SEGURO_CAMPOS_RUBA = ("companiaSeguro", "numeroPoliza", "fechaVencimientoSeguro")
_JS_SEGURO_SIN_REQUIRED = r"""(nombres) => {
    const liberados = [];
    nombres.forEach((n) => {
        document.querySelectorAll('[id$="_' + n + '"]').forEach((el) => {
            if (!["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) return;
            if ((el.value || "").trim() || !el.hasAttribute("required")) return;
            el.removeAttribute("required");
            liberados.push(el.id);
        });
    });
    return liberados;
}"""

# "Vehículos Afectados / Medios aéreos" del formulario de Incendio. Sin
# medios aéreos en el parte el combo "Seleccionar Intervinientes" va en "No"
# y los contadores se deshabilitan (no viajan ni se validan); con apoyo
# aéreo explícito va en "Sí" y se cargan las cantidades.
MEDIOS_AEREOS_CAMPOS_RUBA = {
    "aviones": "cantidadAviones",
    "avionetas": "cantidadAvionetas",
    "helicopteros": "cantidadHelicopteros",
    "otros": "cantidadOtrosAereo",
}
_JS_MEDIOS_AEREOS = r"""([campos, hay, cantidades]) => {
    const inputs = campos.map((n) => document.querySelector('[id$="_' + n + '"]')).filter(Boolean);
    if (!inputs.length) return "sin_campos";
    const esSiNo = (s) => {
        const vals = Array.from(s.options || []).map((o) => (o.value || "").toLowerCase());
        return vals.some((v) => ["si", "sí", "1", "true"].includes(v))
            && vals.some((v) => ["no", "0", "false"].includes(v));
    };
    const ajeno = (s) => /personas|bomberos/i.test(s.id || s.name || "");
    let combo = Array.from(document.querySelectorAll("select"))
        .find((s) => /hay\w*(aere|medio|avion)/i.test(s.id || s.name || ""));
    // Sin id reconocible: el combo si/no más cercano que comparte bloque con los contadores.
    for (let nodo = inputs[0].parentElement; !combo && nodo && nodo !== document.body; nodo = nodo.parentElement) {
        combo = Array.from(nodo.querySelectorAll("select")).find((s) => esSiNo(s) && !ajeno(s));
    }
    const disparar = (el) => {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        if (window.jQuery) { try { window.jQuery(el).trigger("change"); } catch (e) {} }
    };
    if (combo) {
        const buscados = hay ? ["si", "sí", "1", "true"] : ["no", "0", "false"];
        const opcion = Array.from(combo.options).find((o) => buscados.includes((o.value || "").toLowerCase()));
        if (opcion && combo.value !== opcion.value) { combo.value = opcion.value; disparar(combo); }
    }
    inputs.forEach((el, i) => {
        if (hay) {
            el.disabled = false;
            el.value = String(cantidades[i] || 0);
            disparar(el);
        } else {
            el.value = "";
            el.removeAttribute("required");
            el.removeAttribute("pattern");
            el.disabled = true;
        }
    });
    return combo ? (hay ? "si" : "no") : "sin_combo";
}"""

# Opcionales/condicionales de Datos Generales que RUBA puede marcar
# `required` (o con `pattern`) aunque el parte no los use. Antes del submit,
# si están VACÍOS se les quita `required`/`pattern`; si además su bloque está
# inactivo (oculto o con el combo en "No") se deshabilitan.
CAMPOS_OPCIONALES_PRE_SUBMIT = (
    "otraLocalidad", *SEGURO_CAMPOS_RUBA, *MEDIOS_AEREOS_CAMPOS_RUBA.values(),
    "otroTipoLugarForestal", "otroTipoLugar",
)
_JS_LIBERAR_OPCIONALES = r"""([nombres, inactivos]) => {
    const liberados = [], deshabilitados = [];
    const oculto = (el) => {
        if (el.getClientRects().length === 0) return true;
        const caja = el.closest(".control-group, .form-group, fieldset");
        return !!caja && (caja.getClientRects().length === 0 || getComputedStyle(caja).visibility === "hidden");
    };
    nombres.forEach((n) => {
        document.querySelectorAll('[id$="_' + n + '"]').forEach((el) => {
            if (!["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.disabled) return;
            if ((el.value || "").trim()) return;
            if (el.hasAttribute("required") || el.hasAttribute("pattern")) {
                el.removeAttribute("required");
                el.removeAttribute("pattern");
                liberados.push(el.id);
            }
            if (inactivos.includes(n) || oculto(el)) {
                el.disabled = true;
                deshabilitados.push(el.id);
            }
        });
    });
    return { liberados, deshabilitados };
}"""

_JS_OPCIONES = "(el) => Array.from(el.options || []).map(o => ({value: o.value, text: o.textContent.trim()}))"
# Un <select> oculto por select2/chosen sigue aplicando si su contenedor se ve.
_JS_CONTENEDOR_VISIBLE = """(el) => {
    const caja = el.closest('.control-group, .form-group, .controls');
    return !!caja && caja.offsetParent !== null && getComputedStyle(caja).visibility !== 'hidden';
}"""


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
        on_id_descartado: Optional[Callable[[str], None]] = None,
    ) -> None:
        """`ruba_id_existente`: si una corrida anterior ya creó el incidente en
        RUBA, se retoma en /editar/{id} en vez de inicializar otro (evita
        partes duplicados). `on_id_remoto` se llama apenas RUBA asigna el ID,
        para persistirlo aunque un paso posterior falle.

        Si el incidente existente no abre (error 500, timeout, redirige fuera
        de /editar/ o no muestra el formulario), se descarta ese ID -- se
        avisa por `on_id_descartado(id_viejo)` para limpiarlo en la base -- y
        se crea el servicio desde cero en /agregar, con una advertencia para
        que el usuario verifique que el borrador viejo no quedó duplicado.

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
        # Un ID vacío o en blanco (parte desvinculado a mano) = crear desde cero.
        self.ruba_id_remoto: Optional[str] = (str(ruba_id_existente).strip() or None) if ruba_id_existente else None
        self.on_id_remoto = on_id_remoto
        self.on_id_descartado = on_id_descartado
        self.ruba_id_descartado: Optional[str] = None
        self.aviso_recreado: Optional[str] = None
        self._paso_activo = PasoRuba.SESION
        # Campos de bloques condicionales inactivos (seguro, medios aéreos en "No").
        self._campos_inactivos: set = set()

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
            if self.ruba_id_descartado:
                self.aviso_recreado = (
                    f"⚠️ Atención: Se creó un nuevo servicio en RUBA (ID {self.ruba_id_remoto or '—'}). "
                    f"Verificá manualmente en el listado de RUBA que el borrador {self.ruba_id_descartado} "
                    "no haya quedado duplicado."
                )
                self.advertencias.append(self.aviso_recreado)
                log.warning("RUBA: %s", self.aviso_recreado)
            return ResultadoRuba(self.ruba_id_remoto, self.page.url, list(self.advertencias),
                                 self.ruba_id_descartado, self.aviso_recreado)
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
            motivo = self._abrir_edicion_existente(self.ruba_id_remoto)
            if motivo is None:
                raise _PasoOmitido(f"El incidente ya existía en RUBA (ID {self.ruba_id_remoto}): se retoma la carga.")
            self._descartar_id_existente(motivo)

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

    def _abrir_edicion_existente(self, ruba_id: str) -> Optional[str]:
        """Abre /editar/{id} y verifica que muestre el formulario de Datos
        Generales. None si abrió bien; si no, el motivo (error del servidor,
        timeout, redirección). Ante un 500 recarga UNA vez (suele ser
        transitorio) antes de darlo por perdido. Nunca lanza: el que llama
        decide crear el servicio desde cero."""
        url = f"{self.url_incidentes.rstrip('/')}/editar/{ruba_id}"
        selector_calle = self.sel["editar_general"]["calle"]
        motivo = "sin respuesta"
        for intento in (1, 2):
            try:
                if intento == 1:
                    self._ir_a(url)
                else:
                    self.page.wait_for_timeout(self.ESPERA_REINTENTO_500_MS)
                    self.page.reload(wait_until="domcontentloaded", timeout=self.timeout_navegacion_ms)
                self._esperar_red()
            except RubaAutomationError as e:
                motivo = e.detalle
                continue
            except PlaywrightError as e:
                motivo = f"{type(e).__name__}: {e}"
                continue
            error = detectar_error_servidor(self.page)
            if error is not None:
                motivo = f"error del servidor ({error})"
                continue
            if "/editar/" not in self.page.url:
                return f"RUBA redirigió a {self.page.url}"  # borrado o sin permiso: recargar no cambia nada
            try:
                self.page.locator(selector_calle).first.wait_for(state="attached", timeout=self.timeout_ms)
                return None
            except PlaywrightError:
                error = detectar_error_servidor(self.page)
                motivo = (f"error del servidor ({error})" if error
                          else f"el formulario de edición no cargó (título: '{_evaluar_titulo(self.page)}')")
        return motivo

    def _descartar_id_existente(self, motivo: str) -> None:
        """El incidente viejo no responde: se olvida su ID (en memoria y, vía
        `on_id_descartado`, en la base local) para crear uno nuevo."""
        id_viejo = self.ruba_id_remoto
        self.ruba_id_descartado = id_viejo
        self.ruba_id_remoto = None
        aviso = (f"El incidente anterior ({id_viejo}) no responde o fue eliminado en RUBA. "
                 "Creando nuevo servicio desde cero...")
        log.warning("RUBA: %s Motivo: %s", aviso, motivo)
        self._capturar(PasoRuba.INICIALIZACION)  # diagnóstico de la pantalla que falló
        if self.on_id_descartado:
            try:
                self.on_id_descartado(id_viejo)
            except Exception:  # noqa: BLE001 - no poder limpiar la base no frena la creación
                log.exception("RUBA: no se pudo limpiar el ID descartado %s en la base local", id_viejo)
        indice = PASOS.index(PasoRuba.INICIALIZACION)
        self._emitir(PasoRuba.INICIALIZACION, "aviso", int(100 * indice / len(PASOS)), aviso)

    def _cargar_general(self) -> None:
        datos = self.payload["editar_general"]
        sel = self.sel["editar_general"]
        self._esperar_formulario_general(sel["calle"])
        self._campos_inactivos.clear()  # reintento: se recalcula con el formulario nuevo

        if datos.get("localidad_autocomplete"):
            self._cargar_localidad(sel["localidad_autocomplete"], datos["localidad_autocomplete"])
        self._llenar(sel["calle"], datos.get("calle"))
        self._llenar(sel["altura"], datos.get("altura"))
        if datos.get("tipo_zona"):
            self._seleccionar_por_texto(sel["tipo_zona"], datos["tipo_zona"])
        self._confirmar_punto_en_mapa(sel)
        self._asegurar_punto_fijado(sel, datos)

        self._llenar(sel["descripcion"], datos.get("descripcion"))  # obligatoria en RUBA
        # Denunciante: NINGÚN dato es obligatorio. Vacío -> no se toca; campo
        # ausente en RUBA -> se sigue sin error (espera corta, nunca 30 s).
        for clave in ("nombre_solicitante", "apellido_solicitante", "telefono_solicitante", "dni_solicitante"):
            self._llenar_opcional(sel[clave], datos.get(clave))
        self._cargar_seguro(sel, datos)
        self._cargar_personas_damnificadas(sel, datos)

        self._cargar_condicionales(datos.get("condicionales"))
        self._cargar_medios_aereos(datos)
        self._cargar_vehiculos_accidente(self.payload.get("vehiculos_accidentes") or [])
        if self.payload.get("damnificados", {}).get("bienes"):
            self.advertencias.append(
                "Hay bienes afectados cargados, pero ruba_mapping.json no tiene sus selectores: "
                "completarlos a mano en RUBA."
            )
        liberados = liberar_condicionales_vacios(
            self.page, sel.get("liberar_si_vacios") or CONDICIONALES_LIBERABLES_DEFAULT
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

        - Parte SIN seguro: Compañía, N° de Póliza y Fecha de Vencimiento
          quedan EN BLANCO, tal como los trae RUBA: no se escribe nada (ni
          "Sin datos" ni "00000000"), no se tilda ningún checkbox ni se
          espera. Si el formulario (Incendios) los marca `required`, solo se
          quita ese atributo para que el navegador no bloquee el guardado.
        - Parte CON seguro: se tilda el checkbox si el mapping define uno
          (`check_seguro`), se espera el campo como máximo 2 s y se completan
          compañía y póliza. Si RUBA no muestra el bloque, advertencia."""
        sel_compania, sel_poliza = sel["compania_seguro"], sel["numero_poliza"]
        if not datos.get("tiene_seguro"):
            # Bloque inactivo: el pre-submit deshabilita sus inputs vacíos.
            self._campos_inactivos.update(SEGURO_CAMPOS_RUBA)
            try:
                liberados = self.page.evaluate(_JS_SEGURO_SIN_REQUIRED, list(SEGURO_CAMPOS_RUBA))
            except PlaywrightError as e:  # nunca frena la carga por el seguro
                liberados = []
                log.info("Paso 3: no se pudo revisar 'Datos del Seguro' (%s).", e)
            log.info("Paso 3: parte sin seguro: 'Datos del Seguro' queda en blanco%s.",
                     f" (required quitado en {', '.join(liberados)})" if liberados else "")
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

    def _cargar_medios_aereos(self, datos: Dict[str, Any]) -> None:
        """"Vehículos Afectados / Medios aéreos" (formulario de Incendio).

        Solo si el parte trae explícitamente apoyo aéreo (`medios_aereos`
        con alguna cantidad > 0) el combo "Seleccionar Intervinientes" va en
        "Sí" y se cargan las cantidades. Si no, va en "No" y los contadores
        quedan vacíos y deshabilitados: Symfony/HTML5 no los valida."""
        medios = datos.get("medios_aereos") or {}
        cantidades = [_entero(medios.get(clave)) for clave in MEDIOS_AEREOS_CAMPOS_RUBA]
        hay = any(cantidades)
        try:
            estado = self.page.evaluate(
                _JS_MEDIOS_AEREOS, [list(MEDIOS_AEREOS_CAMPOS_RUBA.values()), hay, cantidades])
        except PlaywrightError as e:  # nunca frena la carga por los medios aéreos
            log.info("Paso 3: no se pudo revisar 'Medios aéreos' (%s).", e)
            estado = None
        if estado == "sin_campos":
            return  # este tipo de incidente no tiene el bloque
        if not hay:
            self._campos_inactivos.update(MEDIOS_AEREOS_CAMPOS_RUBA.values())
        if estado == "sin_combo":
            log.info("Paso 3: RUBA no mostró el combo de medios aéreos; contadores %s.",
                     "cargados" if hay else "deshabilitados")
        else:
            log.info("Paso 3: medios aéreos -> '%s' %s", estado, cantidades if hay else "")
        if hay and estado == "sin_combo":
            self.advertencias.append(
                "El parte tiene medios aéreos pero RUBA no mostró 'Seleccionar Intervinientes': revisalo en RUBA.")

    def _liberar_opcionales_pre_submit(self) -> None:
        """Justo antes del submit: a los opcionales/condicionales VACÍOS se les
        quita `required` y `pattern`; los de bloques inactivos (seguro o
        medios aéreos en "No", o contenedor oculto) se deshabilitan para que
        ni el navegador ni Symfony los marquen como inválidos."""
        try:
            resultado = self.page.evaluate(
                _JS_LIBERAR_OPCIONALES, [list(CAMPOS_OPCIONALES_PRE_SUBMIT), sorted(self._campos_inactivos)])
        except PlaywrightError as e:
            log.info("No se pudieron liberar los opcionales antes del guardado (%s).", e)
            return
        if isinstance(resultado, dict) and (resultado.get("liberados") or resultado.get("deshabilitados")):
            log.info("Pre-submit: required/pattern quitado en %s; deshabilitados %s",
                     resultado.get("liberados"), resultado.get("deshabilitados"))

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
        # Cada campo condicional (tipoLugarForestal, tipoLugar, evacuación,
        # causaIncendio...) depende del Subtipo: RUBA puede no dibujarlo. Nunca
        # se espera 30 s por uno: presencia con espera corta y, si no aplica,
        # se omite con advertencia y se sigue con el resto del formulario.
        espera_ms = TIMEOUT_CONDICIONAL_MS  # la 1ra vez da tiempo a que RUBA dibuje el bloque por JS
        for campo, valor in campos.items():
            if valor in (None, ""):
                continue
            selector = bloque.get(campo)
            if selector is None:
                self.advertencias.append(f"Campo condicional sin selector en el mapping: {formulario}.{campo}")
                continue
            if campo.endswith("_otro") and not _es_opcion_otro(bloque, campo[:-len("_otro")],
                                                                campos.get(campo[:-len("_otro")])):
                # Texto "Otro" (otroTipoLugar...): solo con la opción Otro/Otros elegida.
                log.info("Paso 3: %s.%s se ignora: no se eligió la opción 'Otro'.", formulario, campo)
                continue
            if isinstance(selector, dict):  # grupo de radios {"si": "#..._0", ...}
                selector = selector.get(str(valor))
                if selector is None:
                    self.advertencias.append(f"{formulario}.{campo}: opción '{valor}' sin selector en el mapping.")
                    continue
            presente = self._condicional_presente(selector, espera_ms)
            espera_ms = min(espera_ms, TIMEOUT_OPCIONAL_MS // 3)  # el bloque ya tuvo su oportunidad
            if not presente:
                log.info("Paso 3: %s.%s no está en pantalla (%s): se omite.", formulario, campo, selector)
                self.advertencias.append(
                    f"{formulario}: RUBA no mostró '{campo}' para este tipo de incidente; se omitió.")
                continue
            try:
                if isinstance(bloque.get(campo), dict):
                    self._tildar(selector, True)
                elif f"{campo}_opciones" in bloque:
                    self._seleccionar(selector, str(valor))
                else:
                    self._llenar_opcional(selector, valor, timeout_ms=0)
            except (RubaAutomationError, PlaywrightError) as e:
                detalle = e.detalle if isinstance(e, RubaAutomationError) else str(e).splitlines()[0]
                log.warning("Paso 3: no se pudo cargar %s.%s: %s", formulario, campo, detalle)
                self.advertencias.append(f"{formulario}: no se pudo cargar '{campo}' ({detalle}); revisalo en RUBA.")
        self._avisar_combos_obligatorios_vacios(formulario, bloque, campos)

    def _avisar_combos_obligatorios_vacios(self, formulario: str, bloque: Dict[str, Any],
                                           campos: Dict[str, Any]) -> None:
        """Combos del bloque (p. ej. Tipo de Lugar) que el parte no trae: quedan
        en "Seleccionar" -- no se inventa una opción sin código confirmado. Si
        RUBA los marca obligatorios, se avisa para completarlos a mano."""
        for campo in bloque:
            if not campo.endswith("_opciones"):
                continue
            nombre = campo[: -len("_opciones")]
            selector = bloque.get(nombre)
            if campos.get(nombre) not in (None, "") or not isinstance(selector, str):
                continue
            try:
                loc = self.page.locator(selector).first
                if not loc.count() or loc.get_attribute("required") is None or (loc.input_value() or "").strip():
                    continue
            except PlaywrightError:
                continue
            self.advertencias.append(
                f"{formulario}: '{nombre}' quedó en 'Seleccionar' (el parte no lo trae) y RUBA lo marca "
                "obligatorio: si rechaza el guardado, completalo en el parte o en RUBA.")

    def _condicional_presente(self, selector: str, timeout_ms: int) -> bool:
        """¿El campo condicional existe y aplica (visible, o su contenedor
        visible si es un <select> reemplazado por select2 / chosen)? Espera
        como máximo `timeout_ms`; nunca lanza."""
        loc = self.page.locator(selector).first
        try:
            if loc.count() == 0:
                if timeout_ms <= 0:
                    return False
                loc.wait_for(state="attached", timeout=timeout_ms)
            if loc.is_visible():
                return True
            if timeout_ms > 0:
                try:
                    loc.wait_for(state="visible", timeout=timeout_ms)
                    return True
                except PlaywrightError:
                    pass
            return bool(loc.evaluate(_JS_CONTENEDOR_VISIBLE))
        except PlaywrightError:
            return False

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

        filas_cargadas: List[str] = []   # una por vehículo del parte, nunca más
        for n, v in enumerate(vehiculos, start=1):
            anteriores = indices_coleccion(self.page, prefijo)
            self.page.locator(sel["boton_agregar"]).first.click()
            try:
                indice = esperar_nuevo_indice(self.page, prefijo, anteriores, timeout=self.timeout_ms)
            except PlaywrightError as e:
                raise RubaAutomationError(PasoRuba.GENERAL, f"RUBA no generó la fila del vehículo {n}: {e}") from e
            fila = f"{prefijo}_{indice}_"
            filas_cargadas.append(fila)
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
            for clave in ("dominio", "modelo", "anio"):  # opcionales: vacío -> no se tipea
                self._llenar_opcional(campo(clave), v.get(clave), timeout_ms=TIMEOUT_FILA_MS)

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
        self._completar_selects_vehiculos(filas_cargadas)

    # -- Selects de cada vehículo del Accidente (tipo / asegurado / airbag) ---------------

    # `:not([name*='__name__'])`: Symfony deja en el DOM un prototype OCULTO de la
    # fila (id ..._datosVehiculosAccidentes___name___tipo, name [__name__][tipo])
    # que matchea el mismo selector; tocarlo = esperar 30 s a que sea visible.
    SEL_VEHICULOS_ACCIDENTE = "select[name*='[datosVehiculosAccidentes]'][name$='[{campo}]']:not([name*='__name__'])"

    def _seleccionar_tipo_vehiculo(self, selector: str, valor: Optional[str], n: int) -> None:
        """select_option(value=...) con el value ya mapeado en el payload
        (ruba_payload.tipo_vehiculo_ruba). Nunca queda en "Seleccionar"."""
        loc = self.page.locator(selector).first
        if not loc.count() or not loc.is_visible():
            return  # formulario sin tipo (o fila no visible): lo cubre la pasada final
        valor = valor or TIPO_VEHICULO_DEFAULT
        disponibles = [o["value"] for o in loc.evaluate(_JS_OPCIONES)]
        if valor not in disponibles:
            self.advertencias.append(f"Vehículo {n}: RUBA no ofrece el tipo {valor}; se cargó 'Transito > Autos'.")
            valor = TIPO_VEHICULO_DEFAULT if TIPO_VEHICULO_DEFAULT in disponibles else next(
                (v for v in disponibles if v), "")
        if valor:
            loc.select_option(value=valor, timeout=TIMEOUT_FILA_MS)

    def _setear_asegurado(self, selector: str, asegurado: bool) -> None:
        """"Asegurado" puede ser checkbox o <select> (Si / No / Sin datos)."""
        loc = self.page.locator(selector).first
        if not loc.count() or not loc.is_visible():
            return
        if loc.evaluate("(e) => e.tagName") == "SELECT":
            preferidos = ("si",) if asegurado else ("no", "sin datos")
            self._elegir_opcion(selector, preferidos)
        else:
            self._tildar(selector, asegurado)

    def _elegir_opcion(self, selector: str, preferidos: tuple, solo_si_vacio: bool = False) -> Optional[str]:
        """Elige en un <select> la primera opción cuyo texto coincide con
        `preferidos` (sin acentos ni mayúsculas); si ninguna coincide, la
        primera opción NO vacía. Devuelve el value elegido (None si no tocó).
        Un select invisible (p. ej. el prototype de Symfony) no se toca."""
        loc = self.page.locator(selector).first
        if not loc.count() or not loc.is_visible():
            return None
        if solo_si_vacio and (loc.input_value(timeout=TIMEOUT_FILA_MS) or "").strip():
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
        loc.select_option(value=elegida, timeout=TIMEOUT_FILA_MS)
        return elegida

    def _elegir_si_vacio(self, selector: str, preferidos: tuple) -> Optional[str]:
        return self._elegir_opcion(selector, preferidos, solo_si_vacio=True)

    def _completar_selects_vehiculos(self, filas_cargadas: List[str]) -> None:
        """Red de seguridad: ningún tipo / asegurado / airbag de los vehículos
        que se acaban de cargar queda vacío. Solo recorre selects VISIBLES,
        sin el prototype `__name__`, de las filas creadas para este parte:
        con 1 vehículo se revisa 1 fila y nunca se accede a un índice mayor."""
        if not filas_cargadas:
            return
        por_defecto = (("tipo", None), ("asegurado", ("no", "sin datos")), ("airbag", ("no posee", "sin datos", "no")))
        for campo, preferidos in por_defecto:
            candidatos = self.page.locator(self.SEL_VEHICULOS_ACCIDENTE.format(campo=campo)).all()
            visibles = [s for s in candidatos if self._visible_y_de(s, filas_cargadas)]
            for select in visibles[: len(filas_cargadas)]:
                if (select.input_value(timeout=TIMEOUT_FILA_MS) or "").strip():
                    continue
                identificador = select.get_attribute("id") or select.get_attribute("name") or campo
                if campo == "tipo":
                    select.select_option(value=TIPO_VEHICULO_DEFAULT, timeout=TIMEOUT_FILA_MS)
                    elegido: Optional[str] = TIPO_VEHICULO_DEFAULT
                else:
                    elegido = self._elegir_opcion(f"[id='{identificador}']", preferidos)
                log.info("Vehículo del accidente: %s vacío -> %s", identificador, elegido)

    @staticmethod
    def _visible_y_de(select, filas: List[str]) -> bool:
        """El select es visible y pertenece a una de las filas cargadas (su id
        empieza con '{prefijo}_{indice}_'): el prototype nunca califica."""
        try:
            identificador = select.get_attribute("id", timeout=TIMEOUT_FILA_MS) or ""
            return identificador.startswith(tuple(filas)) and select.is_visible()
        except PlaywrightError:
            return False

    def _cargar_damnificados(self) -> None:
        sel = self.sel["damnificados"]
        url = self.page.url
        if self.sel["participacion"]["url_patron"] in url:
            raise _PasoOmitido("Sin víctimas: RUBA salteó Damnificados y pasó a Participación.")
        if sel["url_patron"] not in url:
            raise RubaAutomationError(PasoRuba.DAMNIFICADOS, f"RUBA quedó en una pantalla inesperada: {url}")
        heridos = self.payload["damnificados"]["heridos"]
        if self.payload["editar_general"].get("civiles_heridos", 0) > 0 and heridos:
            self._cargar_filas_heridos(sel, heridos)
        if any(f.get("nombre") or f.get("apellido") for f in self.payload["damnificados"]["fallecidos"]):
            self.advertencias.append("Hay fallecidos individualizados pero el mapping no tiene selectores para ellos.")
        self._guardar_y_continuar(sel["btn_guardar_continuar"], destinos=(self.sel["participacion"]["url_patron"],))
        if not heridos:
            raise _PasoOmitido("Sin civiles heridos: se continuó sin cargar filas.")

    def _cargar_filas_heridos(self, sel: Dict[str, Any], heridos: List[Dict[str, Any]]) -> None:
        """Una fila por herido del parte, por su row_id real (RUBA puede
        arrancar en Heridos_1_, Heridos_12_...). RUBA arma las filas según
        "Civiles heridos" del paso 3; si faltan y la pantalla tiene "+ agregar"
        se generan, si no se cargan las que hay y se avisa."""
        listar = lambda: self._row_ids_visibles(SEL_FILAS_HERIDO, PATRONES_ROW_HERIDO)  # noqa: E731
        limite = time.monotonic() + self.timeout_ms / 1000
        filas = listar()
        while not filas and time.monotonic() < limite:  # RUBA las dibuja por JS al cargar
            self.page.wait_for_timeout(200)
            filas = listar()
        if not filas:
            raise RubaAutomationError(PasoRuba.DAMNIFICADOS, "RUBA no generó las filas de heridos.")
        boton = ", ".join(filter(None, (sel.get("btn_agregar_fila"), SEL_BTN_AGREGAR_HERIDO)))
        if len(filas) < len(heridos):
            if self.page.locator(boton).count():
                filas = self._generar_filas(listar, len(heridos), boton, "herido")
            else:
                self.advertencias.append(
                    f"RUBA mostró {len(filas)} fila(s) de heridos y el parte tiene {len(heridos)}: "
                    f"se cargan los primeros {len(filas)} (revisá 'Civiles heridos')."
                )
        a_cargar = heridos[:len(filas)]
        valores = {r: self._valor_de(sel["fila_herido"]["apellido"].format(i=r)) for r in filas}
        asignadas = _asignar_filas(
            filas, a_cargar, valores,
            ya_cargado=lambda h, valor: bool(h.get("apellido")) and _normalizar(valor) == _normalizar(h["apellido"]),
        )
        log.info("Heridos: filas visibles %s -> asignadas %s", filas, asignadas)
        for n, (herido, row_id) in enumerate(zip(a_cargar, asignadas), start=1):
            fila = {k: v.format(i=row_id) for k, v in sel["fila_herido"].items()}
            self._llenar(fila["apellido"], herido.get("apellido"))
            for clave in ("nombre", "dni"):  # opcionales: sin dato (o sin campo) se sigue
                self._llenar_opcional(fila[clave], herido.get(clave))
            self._seleccionar_genero(fila["genero"], herido.get("genero"), n)
        self._completar_generos_vacios()

    # `:not([name*='__name__'])`: nunca el prototype oculto de Symfony.
    SEL_GENEROS_HERIDOS = "select[name^='Heridos_'][name*='[genero]']:not([name*='__name__'])"

    def _seleccionar_genero(self, selector: str, genero: Optional[str], n: int) -> None:
        """Género de la fila de un herido: select_option(value) con el código
        mapeado (1 / 2 / 3), timeout corto, nunca la opción vacía."""
        loc = self.page.locator(f"{selector}:not([name*='__name__'])").first
        if not loc.count() or not loc.is_visible():
            self.advertencias.append(f"Herido {n}: RUBA no mostró el campo género; se completa al final si aparece.")
            return
        valor = genero_ruba(genero)
        disponibles = [o["value"] for o in loc.evaluate(_JS_OPCIONES)]
        if valor not in disponibles:
            respaldo = GENERO_DEFAULT if GENERO_DEFAULT in disponibles else next((v for v in disponibles if v), None)
            if respaldo is None:
                self.advertencias.append(f"Herido {n}: el combo de género de RUBA no tiene opciones.")
                return
            self.advertencias.append(f"Herido {n}: RUBA no ofrece el género {valor}; se cargó {respaldo}.")
            valor = respaldo
        loc.select_option(value=valor, timeout=TIMEOUT_FILA_MS)

    def _completar_generos_vacios(self) -> None:
        """Red de seguridad: RUBA arma tantas filas como "Civiles heridos" diga
        el contador, aunque el parte individualice menos. Todo género VISIBLE
        que quedó en "Seleccionar" pasa a "Se desconoce" (es obligatorio)."""
        for select in self.page.locator(self.SEL_GENEROS_HERIDOS).all():
            try:
                if not select.is_visible() or (select.input_value(timeout=TIMEOUT_FILA_MS) or "").strip():
                    continue
                disponibles = [o["value"] for o in select.evaluate(_JS_OPCIONES)]
                if GENERO_DEFAULT in disponibles:
                    select.select_option(value=GENERO_DEFAULT, timeout=TIMEOUT_FILA_MS)
                    log.info("Heridos: %s vacío -> %s (Se desconoce)", select.get_attribute("name"), GENERO_DEFAULT)
            except PlaywrightError as e:
                log.info("Heridos: no se pudo completar un género vacío (%s)", e)

    def _cargar_participacion(self) -> None:
        datos = self.payload["participacion"]
        sel = self.sel["participacion"]
        self.page.locator(sel["numero_parte"]).first.wait_for(state="attached")
        tratados_aparte = {"hay_intervinientes_bomberos", "intervencion_comision", *CONTADORES_BOMBEROS_DAMNIFICADOS}
        for clave, valor in datos.items():
            if clave not in sel or clave in tratados_aparte:
                continue
            if isinstance(valor, bool):
                self._setear_booleano(sel[clave], valor)
            else:
                self._llenar(sel[clave], valor)
        # Comisión Directiva: No salvo que el parte diga explícitamente que participó.
        if sel.get("intervencion_comision") and self._existe(sel["intervencion_comision"]):
            self._setear_booleano(sel["intervencion_comision"], bool(datos.get("intervencion_comision")))
        self._cargar_bomberos_damnificados(sel, datos)
        self._guardar_y_continuar(sel["btn_guardar_continuar"])

    def _cargar_bomberos_damnificados(self, sel: Dict[str, Any], datos: Dict[str, Any]) -> None:
        """"Bomberos Damnificados de éste cuerpo" (`hayIntervinientesBomberos`).

        NO significa "hubo bomberos en el servicio" (eso es el paso Bomberos):
        es si hubo bomberos del cuartel heridos/fallecidos/desaparecidos. Si
        queda en "Si" con los tres contadores en 0, Symfony rechaza con
        "Debe indicar algun bombero" (igual que `hayIntervinientesPersonas`
        del paso 3). Por eso el Sí/No se deriva SIEMPRE de los contadores del
        parte, no de un flag del payload, y los contadores se escriben
        siempre -- "0" si vienen vacíos, aunque RUBA los oculte con "No"."""
        contadores = {clave: _entero(datos.get(clave)) for clave in CONTADORES_BOMBEROS_DAMNIFICADOS}
        hay_damnificados = any(contadores.values())
        selector = sel.get("hay_intervinientes_bomberos") or "[name*='[hayIntervinientesBomberos]']"
        if self._existe(selector):
            self._setear_booleano(selector, hay_damnificados)
            self.page.wait_for_timeout(300)  # RUBA muestra/oculta los contadores por JS
        elif hay_damnificados:
            self.advertencias.append(
                "RUBA no mostró 'Bomberos Damnificados de éste cuerpo': se cargan solo los contadores."
            )
        for clave, valor in contadores.items():
            if sel.get(clave):
                self._llenar(sel[clave], str(valor))  # "0" también se escribe
        if hay_damnificados:
            log.info("Participación: bomberos damnificados del cuartel %s", contadores)

    def _cargar_bomberos(self) -> None:
        """Bomberos intervinientes (dotación + apresto).

        Igual que en Vehículos, la colección no arranca en 0 ni en 1: se leen
        las filas VISIBLES (sin el prototipo `__name__`), se generan las que
        falten con "Agregar bomberos" y cada bombero va a la fila de su
        row_id real."""
        datos = self.payload["intervencion_bomberos"]
        sel = self.sel["intervencion_bomberos"]
        bomberos = datos["bomberos"]
        if not bomberos:
            if self._existe(sel["cantidad_bomberos"]):
                self._guardar_y_continuar(sel["btn_guardar_continuar"])
            raise _PasoOmitido("Sin bomberos intervinientes.")

        self.page.locator(sel["cantidad_bomberos"]).first.wait_for(state="attached")
        boton_agregar = ", ".join(filter(None, (sel.get("btn_agregar_filas"), SEL_BTN_AGREGAR_BOMBERO)))
        listar = lambda: self._row_ids_visibles(SEL_FILAS_BOMBERO, PATRONES_ROW_BOMBERO)  # noqa: E731
        filas_actuales = len(listar())
        faltan = max(0, len(bomberos) - filas_actuales)
        if faltan:
            def antes_de_click(pendientes: int, intento: int) -> None:
                # 1er click: #cantBomberos = exactamente las filas que faltan. Si
                # RUBA no agregó ninguna (su JS completa HASTA el total), se
                # reintenta con el total del parte.
                total = len(bomberos) if intento else pendientes
                self._llenar(sel["cantidad_bomberos"], total)
            filas = self._generar_filas(listar, len(bomberos), boton_agregar, "bombero", antes_de_click)
        else:
            filas = listar()  # alcanzan: no se toca "Agregar bomberos"

        valores = {r: self._valor_de(self._sel_fila(sel["fila_bombero"], "autocomplete_nombre", r)) for r in filas}
        asignadas = _asignar_filas(
            filas, bomberos, valores,
            ya_cargado=lambda bombero, valor: _persona_coincide(bombero["autocomplete_nombre"], valor),
        )
        log.info("Bomberos: filas visibles %s -> asignadas %s", filas, asignadas)
        sobrantes = [r for r in filas if r not in asignadas and not valores[r]]
        if sobrantes:
            quedan = self._eliminar_filas(sobrantes, SEL_FILAS_BOMBERO, PATRONES_ROW_BOMBERO)
            if quedan:
                self.advertencias.append(
                    f"Quedaron filas de bomberos vacías que no se pudieron eliminar ({quedan}): "
                    "pueden bloquear el guardado en RUBA."
                )

        for bombero, row_id in zip(bomberos, asignadas):
            fila = {k: self._sel_fila(sel["fila_bombero"], k, row_id) for k in sel["fila_bombero"]}
            persona = bombero["autocomplete_nombre"]
            if not _persona_coincide(persona, valores[row_id]):
                self._autocompletar_persona(fila["autocomplete_nombre"], persona)
            for clave in ("fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin"):
                self._llenar(fila[clave], bombero.get(clave))
            if bombero.get("tipo_tarea"):
                self._seleccionar_tipo_tarea(fila["tipo_tarea"], str(bombero["tipo_tarea"]))
            if self._existe(fila["is_encargado"]):
                self._tildar(fila["is_encargado"], bool(bombero.get("is_encargado")))
            elif bombero.get("is_encargado"):
                raise RubaAutomationError(PasoRuba.BOMBEROS, f"No se encontró 'Encargado' en la fila {row_id}.")
        self._guardar_y_continuar(sel["btn_guardar_continuar"])

    def _sel_fila(self, patrones: Dict[str, str], clave: str, row_id: str) -> str:
        return patrones[clave].format(i=row_id)

    def _valor_de(self, selector: str) -> str:
        loc = self.page.locator(selector).first
        try:
            return (loc.input_value() or "").strip() if loc.count() else ""
        except PlaywrightError:
            return ""

    def _seleccionar_tipo_tarea(self, selector: str, valor: str) -> None:
        """Tipo de tarea por value ("1" Interviniente / "2" Apresto) y, si el
        portal cambió los values, por el texto de la opción."""
        opciones = self.page.locator(selector).first.evaluate(_JS_OPCIONES)
        if any(o["value"] == valor for o in opciones):
            self._seleccionar(selector, valor)
            return
        texto = TEXTO_TIPO_TAREA.get(valor)
        elegida = next((o for o in opciones if texto and o["value"] and texto in _normalizar(o["text"])), None)
        if elegida is None:
            raise RubaAutomationError(
                PasoRuba.BOMBEROS, f"Tipo de tarea '{valor}' no existe en {selector}: {[o['text'] for o in opciones]}",
            )
        self._seleccionar(selector, elegida["value"])

    def _cargar_vehiculos_y_guardar(self) -> None:
        """Vehículos intervinientes + guardado final.

        Las filas de la colección Symfony NO se numeran desde 0: RUBA arrastra
        el contador (ej. la primera fila visible es `..._vehiculos_11_vehiculo`).
        Por eso no se asume ningún índice: se leen las filas VISIBLES (sin el
        prototipo `__name__`), se agregan las que falten y cada vehículo se
        carga en la fila de su `row_id` real."""
        vehiculos = self.payload["intervencion_vehiculos"]["vehiculos"]
        sel = self.sel["intervencion_vehiculos"]
        if not vehiculos:
            raise _PasoOmitido("Sin vehículos intervinientes.")
        boton_agregar = ", ".join(filter(None, (sel.get("btn_agregar_fila"), SEL_BTN_AGREGAR_VEHICULO)))
        self.page.locator(boton_agregar).first.wait_for(state="attached")

        filas = self._asignar_filas_vehiculos(vehiculos, boton_agregar)
        for vehiculo, row_id in zip(vehiculos, filas):
            fila = {k: v.format(i=row_id) for k, v in sel["fila_vehiculo"].items()}
            self._seleccionar_vehiculo(f"#{PREFIJO_VEHICULOS}_{row_id}_vehiculo", vehiculo)
            if vehiculo.get("autocomplete_chofer"):
                self._autocompletar_persona(self._selector_chofer(fila, row_id), vehiculo["autocomplete_chofer"])
            for clave in ("fecha_salida", "hora_salida", "fecha_llegada", "hora_llegada"):
                if fila.get(clave):
                    self._llenar_si_vacio(fila[clave], vehiculo.get(clave))
        self._eliminar_vehiculos_sobrantes(filas)

        _sembrar_tab_activa(self.page)
        self._liberar_opcionales_pre_submit()
        url_antes = self.page.url
        boton_guardar = ", ".join(filter(None, (sel.get("btn_guardar_cambios"), SEL_BTN_GUARDAR_CAMBIOS)))
        self._primero_visible(boton_guardar).click()
        try:
            self.page.wait_for_url(lambda u: u != url_antes, timeout=self.timeout_navegacion_ms)
        except PlaywrightError:
            pass  # RUBA puede quedarse en la misma pantalla tras un guardado AJAX
        self._esperar_red()
        errores = extraer_errores_validacion(self.page)
        if errores:
            raise RubaAutomationError(
                PasoRuba.VEHICULOS,
                f"RUBA rechazó el guardado final: {errores} campos_invalidos={detectar_campos_invalidos(self.page, CAMPOS_OPCIONALES_PRE_SUBMIT)}",
            )
        self.ruba_id_remoto = self.ruba_id_remoto or _extraer_id(self.page.url)

    def _row_ids_visibles(self, selector: str, patrones: tuple) -> List[str]:
        """row_id real (número de la colección Symfony) de cada fila con algún
        control VISIBLE que matchee `selector`, en orden de pantalla. Cada
        patrón es una regex con el row_id en el grupo 1; se prueba sobre el
        id y el name de cada elemento."""
        row_ids: List[str] = []
        for elemento in self.page.locator(selector).all():
            try:
                if not elemento.is_visible():
                    continue
                atributos = (elemento.get_attribute("id") or "", elemento.get_attribute("name") or "")
            except PlaywrightError:
                continue  # la fila se re-renderizó entre el listado y la lectura
            for patron in patrones:
                coincidencia = next(filter(None, (re.search(patron, a) for a in atributos)), None)
                if coincidencia:
                    if coincidencia.group(1) not in row_ids:
                        row_ids.append(coincidencia.group(1))
                    break
        return row_ids

    def _generar_filas(
        self, listar: Callable[[], List[str]], cantidad: int, boton: str, que: str,
        antes_de_click: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """Click en "+ agregar" hasta tener `cantidad` filas visibles, esperando
        tras cada click que aparezca al menos una nueva. `antes_de_click(faltan,
        intento)` prepara el click (ej. #cantBomberos). Si un click no generó
        filas se reintenta una vez más antes de fallar. Devuelve los row_id."""
        filas = listar()
        sin_efecto = 0
        for intento in range(max(cantidad - len(filas), 0) + 1):
            if len(filas) >= cantidad:
                break
            antes = len(filas)
            if antes_de_click:
                antes_de_click(cantidad - antes, intento)
            self._primero_visible(boton).click()
            limite = time.monotonic() + min(self.timeout_ms, 10000) / 1000
            while len(filas) <= antes and time.monotonic() < limite:
                self.page.wait_for_timeout(150)
                filas = listar()
            if len(filas) <= antes:
                sin_efecto += 1
                if sin_efecto > 1:
                    break
        if len(filas) < cantidad:
            raise RubaAutomationError(
                self._paso_en_curso(),
                f"'+ agregar' no generó las filas de {que} necesarias (hay {len(filas)}, hacen falta {cantidad}).",
            )
        return filas

    def _eliminar_filas(self, row_ids: List[str], sel_anclas: str, patrones: tuple) -> List[str]:
        """Elimina filas de colección vacías con su botón/ícono de borrar
        (buscado dentro del contenedor de ESA fila). Acepta el confirm() que
        pueda abrir el portal. Devuelve los row_id que no se pudieron quitar."""
        quedan: List[str] = []
        aceptar = lambda dialogo: dialogo.accept()  # noqa: E731
        self.page.on("dialog", aceptar)
        try:
            for row_id in row_ids:
                marca = f"borrar-{row_id}"
                estado = self.page.evaluate(_JS_MARCAR_BORRAR_FILA, [sel_anclas, list(patrones), row_id, marca])
                if estado != "ok":
                    log.info("Fila %s: no se encontró cómo eliminarla (%s)", row_id, estado)
                    quedan.append(row_id)
                    continue
                boton = self.page.locator(f"[data-ruba-borrar='{marca}']").first
                try:
                    boton.click(timeout=TIMEOUT_FILA_MS)
                except PlaywrightError as e:
                    # Ícono sin tamaño (fuente no cargada) o tapado: click por JS.
                    log.info("Fila %s: click en eliminar falló (%s); se reintenta por JS", row_id, e)
                    try:
                        boton.evaluate("(el) => el.click()")
                    except PlaywrightError:
                        quedan.append(row_id)
                        continue
                self.page.wait_for_timeout(300)
                if row_id in self._row_ids_visibles(sel_anclas, patrones):
                    quedan.append(row_id)
                else:
                    log.info("Fila vacía %s eliminada", row_id)
        finally:
            self.page.remove_listener("dialog", aceptar)
        return quedan

    def _asignar_filas_vehiculos(self, vehiculos: List[Dict[str, Any]], boton_agregar: str) -> List[str]:
        """Una fila (row_id) por vehículo del parte, en el mismo orden (ver
        `_asignar_filas`: primero las que ya tienen ESE vehículo)."""
        filas = self._generar_filas(
            lambda: self._row_ids_visibles(SEL_FILAS_VEHICULO, PATRONES_ROW_VEHICULO),
            len(vehiculos), boton_agregar, "vehículo",
        )
        valores = {r: self._valor_de(f"#{PREFIJO_VEHICULOS}_{r}_vehiculo") for r in filas}
        asignadas = _asignar_filas(
            filas, vehiculos, valores,
            ya_cargado=lambda v, valor: bool(v.get("select_vehiculo")) and valor == str(v["select_vehiculo"]),
        )
        log.info("Vehículos: filas visibles %s -> asignadas %s", filas, asignadas)
        return asignadas

    def _eliminar_vehiculos_sobrantes(self, asignadas: List[str]) -> None:
        """Filas de vehículo visibles que no recibieron un móvil del parte y
        quedaron en "Seleccionar": RUBA rechaza el guardado (select required),
        así que se quitan con su botón de eliminar (círculo rojo "-")."""
        visibles = self._row_ids_visibles(SEL_FILAS_VEHICULO, PATRONES_ROW_VEHICULO)
        sobrantes = [r for r in visibles
                     if r not in asignadas and not self._valor_de(f"#{PREFIJO_VEHICULOS}_{r}_vehiculo")]
        if not sobrantes:
            return
        quedan = self._eliminar_filas(sobrantes, SEL_FILAS_VEHICULO, PATRONES_ROW_VEHICULO)
        log.info("Vehículos: filas vacías sobrantes %s, sin poder eliminar %s", sobrantes, quedan)
        if quedan:
            self.advertencias.append(
                f"Quedaron filas de vehículos vacías que no se pudieron eliminar ({quedan}): "
                "pueden bloquear el guardado en RUBA."
            )

    def _seleccionar_vehiculo(self, selector: str, vehiculo: Dict[str, Any]) -> None:
        """Elige el móvil: por value (id de RUBA) y, si no está, por el Nº de
        móvil en el texto de la opción ("Nº Móvil: Rojo 24 (Man - ...)")."""
        opciones = self.page.locator(selector).first.evaluate(_JS_OPCIONES)
        valor = _opcion_vehiculo(opciones, vehiculo.get("select_vehiculo"), vehiculo.get("numero_movil"))
        if valor is None:
            raise RubaAutomationError(
                PasoRuba.VEHICULOS,
                f"El móvil '{vehiculo.get('numero_movil') or vehiculo.get('select_vehiculo')}' no coincide con "
                f"ninguno de los que ofrece RUBA en {selector}: {[o['text'] for o in opciones if o['value']][:12]}",
            )
        self._seleccionar(selector, valor)

    def _selector_chofer(self, fila: Dict[str, str], row_id: str) -> str:
        """Input visible del chofer de ESA fila: el autocomplete del mapping,
        o por id/name con el row_id si el portal cambió."""
        candidatos = (
            fila.get("autocomplete_chofer"),
            f"#autocomplete_{PREFIJO_VEHICULOS}_{row_id}_chofer",
            f"#{PREFIJO_VEHICULOS}_{row_id}_chofer",
            f"input[name*='[vehiculos][{row_id}][chofer]']",
        )
        for selector in filter(None, candidatos):
            loc = self.page.locator(selector).first
            if loc.count() and loc.is_visible():
                return selector
        raise RubaAutomationError(PasoRuba.VEHICULOS, f"No se encontró el campo Chofer de la fila {row_id}.")

    def _llenar_si_vacio(self, selector: str, valor: Any) -> None:
        """Fechas/horas del móvil: si RUBA ya las trae completas (las hereda
        de Participación) se respetan; solo se escriben las vacías."""
        loc = self.page.locator(selector).first
        if not loc.count():
            return
        try:
            actual = (loc.input_value() or "").strip()
        except PlaywrightError:
            actual = ""
        if not actual:
            self._llenar(selector, valor)

    def _primero_visible(self, selector: str):
        """El primer elemento VISIBLE que matchea (el template de RUBA tiene
        botones ocultos o duplicados); si ninguno lo es, el primero."""
        loc = self.page.locator(selector)
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                return loc.nth(i)
        return loc.first

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
        ahí se setea por JS y se disparan los eventos que escuchan. Un valor
        vacío (None, "" o solo espacios) no se escribe."""
        if _vacio(valor):
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
        Devuelve True si se escribió algo; vacío/None/espacios -> False sin
        tocar la página."""
        if _vacio(valor):
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
        """Tipea el apellido y elige la sugerencia que coincide con la persona
        (`_persona_coincide`: DNI, o apellido + nombre en cualquier orden)."""
        self._autocompletar(self._visible_de(selector), persona["texto_busqueda"],
                            lambda sugerencia: _persona_coincide(persona, sugerencia), persona["nombre_completo"])

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

    def _guardar_y_continuar(self, selector_boton: str, destinos: Optional[tuple] = None) -> None:
        """Guarda y verifica ADÓNDE llevó RUBA. El form hace POST a la misma
        URL: si hay errores de validación, RUBA re-renderiza esa URL; si
        salió bien, redirige (302) a la pantalla siguiente. `destinos`
        restringe las pantallas válidas (ej. damnificados o participación)."""
        _sembrar_tab_activa(self.page)
        self._liberar_opcionales_pre_submit()
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
                f"campos_invalidos={detectar_campos_invalidos(self.page, CAMPOS_OPCIONALES_PRE_SUBMIT)}",
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


def _es_opcion_otro(bloque: Dict[str, Any], campo: str, valor: Any) -> bool:
    """¿`valor` es la opción "Otro"/"Otros" del combo `campo` (código 999 en
    tipo_lugar_opciones)? Decide si se llena el texto `<campo>_otro`."""
    if valor in (None, ""):
        return False
    opciones = bloque.get(f"{campo}_opciones") or {}
    return any(str(codigo) == str(valor) and _normalizar(clave).startswith("OTRO")
               for clave, codigo in opciones.items())


def _vacio(valor: Any) -> bool:
    """Dato opcional sin cargar en el parte local: se omite en RUBA."""
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _extraer_id(url: str) -> Optional[str]:
    coincidencia = re.search(r"/incidente/(?:editar/|damnificados/)?(\d+)(?:[/?#]|$)", url)
    return coincidencia.group(1) if coincidencia else None


def _asignar_filas(filas: List[str], items: List[Dict[str, Any]], valores: Dict[str, str],
                   ya_cargado: Callable[[Dict[str, Any], str], bool]) -> List[str]:
    """row_id para cada item, en el orden de `items`. Prioridad: 1) la fila
    que ya tiene ESE item (reintento tras un guardado parcial: no duplica),
    2) filas vacías, 3) filas con otro dato (se pisan). `filas` debe tener
    al menos len(items) elementos."""
    libres = list(filas)
    asignadas: List[Optional[str]] = [None] * len(items)
    for n, item in enumerate(items):
        fila = next((r for r in libres if valores.get(r) and ya_cargado(item, valores[r])), None)
        if fila is not None:
            asignadas[n] = fila
            libres.remove(fila)
    libres.sort(key=lambda r: bool(valores.get(r)))  # sort estable: respeta el orden de pantalla
    for n in range(len(items)):
        if asignadas[n] is None:
            asignadas[n] = libres.pop(0)
    return [r for r in asignadas if r is not None]


def _persona_coincide(persona: Optional[Dict[str, Any]], texto: str) -> bool:
    """¿El texto (sugerencia del autocomplete o valor ya cargado) es esta
    persona? Por DNI (sin puntos) si está en el texto; si no, todas las
    palabras del apellido + el primer nombre, en cualquier orden
    ("PEREZ, Juan", "Juan Pérez (12.345.678)", "PEREZ GOMEZ JUAN CARLOS")."""
    if not persona or not texto:
        return False
    dni = re.sub(r"\D", "", str(persona.get("dni") or ""))
    if dni and len(dni) >= 6 and dni in re.sub(r"\D", "", texto):
        return True
    palabras = set(_normalizar(texto).split())
    apellido = _normalizar(persona.get("apellido") or "").split()
    primer_nombre = _normalizar(persona.get("nombre") or "").split()[:1]
    return bool(apellido) and all(p in palabras for p in apellido + primer_nombre)


def _numero_de_opcion(texto: str) -> str:
    """"Nº Móvil: Rojo 24 (Ford - F-100 4x4) - Sociedad..." -> "ROJO 24"."""
    m = re.search(r"m[oó]vil\s*:?\s*([^(]+)", texto, re.IGNORECASE)
    return _normalizar(m.group(1) if m else texto.split("(")[0])


def _ultimo_numero(texto: str) -> str:
    numeros = re.findall(r"\d+", texto)
    return numeros[-1] if numeros else ""


def _opcion_vehiculo(opciones: List[Dict[str, str]], id_ruba: Any, numero_movil: Any) -> Optional[str]:
    """value de la opción del móvil, en este orden:
    1. value == id de RUBA del móvil;
    2. Nº de móvil idéntico ("Rojo 24" == "ROJO 24");
    3. mismo número final ("Móvil 24", "B-24" o "24" -> "Rojo 24");
    4. el número como palabra suelta en cualquier parte del texto.
    En 3 y 4 solo se elige si hay UNA coincidencia ("24" nunca matchea
    "Rojo 245"); si es ambigua devuelve None y el paso falla con la lista."""
    validas = [o for o in opciones if o.get("value")]
    if id_ruba not in (None, ""):
        directa = next((o for o in validas if o["value"] == str(id_ruba)), None)
        if directa:
            return directa["value"]
    if not numero_movil:
        return None
    buscado = _normalizar(str(numero_movil))
    digitos = _ultimo_numero(buscado)

    exactas = [o for o in validas if _numero_de_opcion(o["text"]) == buscado]
    if len(exactas) == 1:
        return exactas[0]["value"]
    if not digitos:
        return None
    por_numero = [o for o in validas if _ultimo_numero(_numero_de_opcion(o["text"])) == digitos]
    if len(por_numero) == 1:
        return por_numero[0]["value"]
    parciales = [o for o in validas if re.search(rf"(?<!\d){digitos}(?!\d)", _normalizar(o["text"]))]
    if len(parciales) == 1:
        return parciales[0]["value"]
    return None


def _entero(valor: Any) -> int:
    """Contador del parte como entero >= 0 (None, "" o basura -> 0)."""
    try:
        return max(int(valor or 0), 0)
    except (TypeError, ValueError):
        return 0


def _es_picker(selector: str) -> bool:
    """IDs de los Bootstrap date/timepicker de RUBA (#datepicker_..., #timepicker_...)."""
    return "datepicker_" in selector or "timepicker_" in selector

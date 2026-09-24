"""
Rutinas Playwright de bajo nivel para operar el portal RUBA (Gestión
Bomberil) desde la app: login, selección sincronizada de <select> con
Select2/jQuery, limpieza de `required` ocultos, click en "Guardar y
continuar", avance de paneles y lectura de errores de validación Symfony.
El flujo completo pantalla por pantalla está en `ruba_automation.py`.

Es parte del flujo productivo: NO importa nada de `tools/` (esos son scripts
sueltos de depuración / ingeniería inversa). Las URLs y selectores oficiales
salen de `config/ruba_mapping.json`; si ese archivo falta o está roto, se usan
los valores por defecto de abajo (los mismos ya verificados contra el RUBA real)
y se deja un warning en el log.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from app.core.catalogos import leer_mapping
from app.paths import get_writable_dir

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuración: config/ruba_mapping.json + data/config.json
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def cargar_mapping() -> Dict[str, Any]:
    try:
        return leer_mapping()
    except (OSError, json.JSONDecodeError) as e:
        log.warning("No se pudo leer config/ruba_mapping.json (%s); se usan los selectores por defecto.", e)
        return {}


def _selector(seccion: str, clave: str, por_defecto: str) -> str:
    valor = cargar_mapping().get("selectores", {}).get(seccion, {}).get(clave)
    return valor if isinstance(valor, str) and valor.strip() else por_defecto


def _meta(clave: str, por_defecto: str) -> str:
    valor = cargar_mapping().get("_meta", {}).get(clave)
    return valor if isinstance(valor, str) and valor.strip() else por_defecto


URL_LOGIN_DEFAULT = _meta("url_login", "https://www.gestionbomberos.org/login")
URL_INCIDENTE_DEFAULT = _meta("url_incidentes", "https://www.gestionbomberos.org/estructura/incidente/")

SEL_NUMERO_PARTE = _selector(
    "inicializacion", "numero_parte", "#bomberos_estructurabundle_inicializacionIncidenteType_numeroParte"
)
SEL_TIPO = _selector(
    "inicializacion", "tipo_incidente", "#bomberos_estructurabundle_inicializacionIncidenteType_tipoIncidente"
)
SEL_CATEGORIA = _selector(
    "inicializacion", "categoria_incidente", "#bomberos_estructurabundle_inicializacionIncidenteType_categoriaIncidente"
)
SEL_HAY_PARTICIPACIONES = _selector(
    "inicializacion", "hay_participaciones", "#bomberos_estructurabundle_inicializacionIncidenteType_hayParticipciones"
)
SEL_CUERPOS = _selector(
    "inicializacion", "cuerpos", "#bomberos_estructurabundle_inicializacionIncidenteType_cuerpos"
)

VIEWPORT = {"width": 1366, "height": 768}


def cargar_credenciales() -> Dict[str, str]:
    """Sección 'ruba' de data/config.json ({} si no existe o está mal formado)."""
    ruta = get_writable_dir("data") / "config.json"
    if not ruta.exists():
        return {}
    try:
        with ruta.open("r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("No se pudo leer %s: %s", ruta, e)
        return {}
    return config.get("ruba", {}) or {}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

SELECTORES_USUARIO = [
    "input[name='_username']", "#username", "input[name='username']",
    "input[type='email']", "#email", "input[name='email']",
]
SELECTORES_CLAVE = [
    "input[name='_password']", "#password", "input[name='password']", "input[type='password']",
]
SELECTORES_SUBMIT_LOGIN = [
    "button[type='submit']", "input[type='submit']",
    "button:has-text('Entrar')", "button:has-text('Ingresar')",
    "button:has-text('Iniciar sesión')", "button:has-text('Iniciar Sesion')", "button:has-text('Login')",
]


def _primer_visible(page: Page, selectores: List[str], timeout: int = 2000) -> Optional[Locator]:
    for selector in selectores:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except PlaywrightError:
            continue
    return None


def iniciar_sesion(page: Page, usuario: str, clave: str, url_login: str) -> bool:
    """Loguea en RUBA. True si la URL dejó de ser la de login."""
    if not usuario or not clave:
        return False

    page.goto(url_login, wait_until="domcontentloaded")

    campo_usuario = _primer_visible(page, SELECTORES_USUARIO)
    campo_clave = _primer_visible(page, SELECTORES_CLAVE)
    if campo_usuario is None or campo_clave is None:
        log.warning("No se detectaron los campos de usuario/contraseña en %s", page.url)
        return False

    campo_usuario.fill(usuario)
    campo_clave.fill(clave)

    boton = _primer_visible(page, SELECTORES_SUBMIT_LOGIN, timeout=1500)
    try:
        if boton is not None:
            boton.click()
        else:
            campo_clave.press("Enter")
    except PlaywrightError:
        try:
            campo_clave.press("Enter")
        except PlaywrightError:
            return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightError:
        pass

    return "login" not in page.url.lower()


# ---------------------------------------------------------------------------
# Primitivas
# ---------------------------------------------------------------------------

def _evaluar_con_reintentos(page: Page, js: str, arg: Any = None, intentos: int = 3, espera: float = 0.5) -> Any:
    """page.evaluate() tolerante a "Execution context was destroyed" (el JS
    corre justo mientras la página navega). Devuelve None si nunca pudo."""
    for _ in range(intentos):
        try:
            return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except PlaywrightError:
            time.sleep(espera)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightError:
                pass
    return None


def seleccionar_con_sincronizacion(page: Page, selector: str, value: str) -> None:
    """Setea un <select> y notifica tanto al DOM como a jQuery/Select2 (sin
    esto, los combos dependientes de Symfony -- p. ej. Categoría según Tipo --
    no se repueblan)."""
    page.locator(selector).first.wait_for(state="visible", timeout=10000)
    page.evaluate("""([sel, valor]) => {
        const el = document.querySelector(sel);
        if (!el) return;
        el.value = valor;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        if (window.jQuery) {
            try {
                window.jQuery(el).trigger('change');
                window.jQuery(el).trigger({ type: 'select2:select', params: { data: { id: valor } } });
            } catch (e) {}
        }
    }""", [selector, value])


_JS_QUITAR_REQUIRED_OCULTOS = r"""
(selector) => {
    const raiz = (selector && document.querySelector(selector)) || document;
    function oculto(el) {
        if (el.getClientRects().length === 0) return true;  // display:none propio o de un ancestro
        const estilo = window.getComputedStyle(el);
        return estilo.display === "none" || estilo.visibility === "hidden";
    }
    let quitados = 0;
    raiz.querySelectorAll("input[required], select[required], textarea[required]").forEach((el) => {
        // Un <select> reemplazado por Select2 se oculta a propósito pero sigue
        // siendo operable: no se le saca el required.
        const conWidget = el.classList.contains("select2-hidden-accessible")
            || (el.nextElementSibling && /select2|chosen|selectize/i.test(el.nextElementSibling.className || ""));
        if (oculto(el) && !conWidget) {
            el.removeAttribute("required");
            quitados += 1;
        }
    });
    return quitados;
}
"""


def _sembrar_tab_activa(page: Page, selector: Optional[str] = None) -> int:
    """Saca el atributo `required` de los campos ocultos (display:none /
    visibility:hidden, propios o heredados) para que la validación nativa
    del navegador no bloquee el submit por campos de solapas inactivas del
    formulario Symfony. `selector` acota la limpieza a un contenedor (p. ej.
    la solapa activa); sin él se recorre todo el documento. Devuelve cuántos
    campos se liberaron (0 si la página estaba navegando)."""
    resultado = _evaluar_con_reintentos(page, _JS_QUITAR_REQUIRED_OCULTOS, arg=selector or "")
    return resultado if isinstance(resultado, int) else 0


def limpiar_required_ocultos(page: Page) -> None:
    """Variante para la pantalla de inicialización: además de los ocultos,
    el multiselect de 'Cuerpos' se libera siempre (RUBA lo completa solo)."""
    _sembrar_tab_activa(page)
    try:
        page.evaluate("(sel) => { const el = document.querySelector(sel); if (el) el.removeAttribute('required'); }",
                      SEL_CUERPOS)
    except PlaywrightError:
        pass


_JS_INDICES_COLECCION = r"""
(prefijo) => {
  const re = new RegExp('^' + prefijo + '_(\\d+)_');
  const indices = new Set();
  document.querySelectorAll('[id^="' + prefijo + '_"]').forEach(el => {
    const m = el.id.match(re);
    if (m) indices.add(parseInt(m[1], 10));
  });
  return Array.from(indices);
}
"""


def indices_coleccion(page: Page, prefijo_id: str) -> List[int]:
    """Índices ya generados de una colección Symfony cuyos inputs se llaman
    `{prefijo_id}_{indice}_{campo}` (ej. vehículos de un Accidente)."""
    return sorted(_evaluar_con_reintentos(page, _JS_INDICES_COLECCION, prefijo_id) or [])


def esperar_nuevo_indice(page: Page, prefijo_id: str, anteriores: List[int], timeout: int = 10000) -> int:
    """Después de "agregar fila": espera a que aparezca un índice nuevo y lo devuelve."""
    page.wait_for_function(
        "([prefijo, antes]) => (" + _JS_INDICES_COLECCION.strip() + ")(prefijo).some(i => !antes.includes(i))",
        arg=[prefijo_id, anteriores], timeout=timeout,
    )
    return max(set(indices_coleccion(page, prefijo_id)) - set(anteriores))


def liberar_required_vacios(page: Page, prefijo_fila: str) -> List[str]:
    """Quita `required` de los campos VACÍOS de una fila dinámica (opcionales
    que quedaron sin dato), para que la validación HTML5 no trabe el
    submit. Devuelve los ids liberados."""
    try:
        return page.evaluate(
            "(p) => Array.from(document.querySelectorAll('[id^=\"' + p + '\"]'))"
            ".filter(el => el.required && el.type !== 'checkbox' && !(el.value || '').trim())"
            ".map(el => { el.removeAttribute('required'); return el.id; })",
            prefijo_fila,
        ) or []
    except PlaywrightError:
        return []


# ---------------------------------------------------------------------------
# Errores del servidor
# ---------------------------------------------------------------------------
# Captura real del 24/09 (parte 003, al retomar /editar/{id}): RUBA devolvió
# la página estándar de Symfony "Oops! An Error Occurred / The server
# returned a "500 Internal Server Error"." -- sin ningún formulario. La URL
# sigue conteniendo /editar/, así que sin esto la automatización esperaba
# 15 s un campo que no podía aparecer y reportaba un TimeoutError engañoso.

_JS_ERROR_SERVIDOR = r"""
() => {
    const titulo = (document.title || "").trim();
    const h2 = document.querySelector("h2");
    const detalle = h2 ? h2.innerText.replace(/\s+/g, " ").trim() : "";
    const patron = /An Error Occurred|Internal Server Error|Service Unavailable|Bad Gateway|Error 50\d/i;
    if (patron.test(titulo) || patron.test(detalle)) return detalle || titulo;
    return null;
}
"""


def detectar_error_servidor(page: Page) -> Optional[str]:
    """Texto del error si la pantalla actual es una página de error del
    servidor de RUBA (500/502/503), o None si es una pantalla normal."""
    resultado = _evaluar_con_reintentos(page, _JS_ERROR_SERVIDOR)
    return str(resultado) if resultado else None


# ---------------------------------------------------------------------------
# Paso 3 (Editar General): ubicación, personas damnificadas, condicionales
# ---------------------------------------------------------------------------
# Verificado contra la captura real de RUBA (logs/screenshots/*_general.html):
#   - El punto se guarda en `..._ubicacion_latitud` / `..._ubicacion_longitud`
#     (inputs de texto dentro de un div oculto). "Buscar Puntos" (función
#     codeAddressAuto del portal) los completa solo si Google encuentra la
#     dirección; `map` y `marker` son globales de Google Maps en la página.
#   - `hayIntervinientesPersonas` es un <select> "si"/"no". Si queda en "si"
#     con los tres contadores en 0, Symfony rechaza con "Debe indicar alguna
#     persona".

SEL_LATITUD_DEFAULT = "input[id$='_ubicacion_latitud']"
SEL_LONGITUD_DEFAULT = "input[id$='_ubicacion_longitud']"
SEL_HAY_PERSONAS_DEFAULT = "select[id$='_hayIntervinientesPersonas']"
CONDICIONALES_LIBERABLES_DEFAULT = ("otraLocalidad", "fechaVencimientoSeguro", "otroTipoLugarForestal")

_JS_LEER_PUNTO = r"""
([selLat, selLng]) => {
    const lat = document.querySelector(selLat), lng = document.querySelector(selLng);
    if (!lat || !lng) return null;
    return [lat.value || "", lng.value || ""];
}
"""

_JS_FIJAR_PUNTO = r"""
([selLat, selLng, lat, lng]) => {
    const inLat = document.querySelector(selLat), inLng = document.querySelector(selLng);
    if (!inLat || !inLng) return "sin_campos";
    for (const [el, v] of [[inLat, lat], [inLng, lng]]) {
        el.value = String(v);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    // Mismo efecto que arrastrar el pin en el mapa del portal (su 'dragend'
    // escribe exactamente estos dos inputs). Si Google Maps no cargó, alcanza
    // con los inputs: son lo único que viaja en el POST.
    try {
        if (window.google && window.google.maps && window.marker) {
            const pos = new google.maps.LatLng(lat, lng);
            window.marker.setPosition(pos);
            if (window.map) window.map.setCenter(pos);
            return "inputs_y_mapa";
        }
    } catch (e) {}
    return "inputs";
}
"""


def leer_punto_ubicacion(page: Page, sel_lat: str = SEL_LATITUD_DEFAULT,
                         sel_lng: str = SEL_LONGITUD_DEFAULT) -> Optional[tuple]:
    """(lat, lng) como texto que tiene hoy el formulario ("" si vacío), o
    None si la pantalla no tiene esos campos."""
    resultado = _evaluar_con_reintentos(page, _JS_LEER_PUNTO, [sel_lat, sel_lng])
    return tuple(resultado) if isinstance(resultado, list) else None


def fijar_punto_ubicacion(page: Page, lat: float, lng: float, sel_lat: str = SEL_LATITUD_DEFAULT,
                          sel_lng: str = SEL_LONGITUD_DEFAULT) -> Optional[str]:
    """Fija el punto del incidente a mano (como arrastrar el pin): escribe
    latitud/longitud y mueve el marcador de Google Maps si está. Devuelve
    "inputs_y_mapa", "inputs", o None si la pantalla no tiene los campos."""
    resultado = _evaluar_con_reintentos(page, _JS_FIJAR_PUNTO, [sel_lat, sel_lng, float(lat), float(lng)])
    return resultado if resultado in ("inputs_y_mapa", "inputs") else None


def fijar_intervinientes_personas(page: Page, hay_personas: bool,
                                  selector: str = SEL_HAY_PERSONAS_DEFAULT) -> bool:
    """Pone "¿Intervinientes personas?" en "si"/"no" según haya o no civiles
    heridos/fallecidos/desaparecidos. False si la pantalla no tiene el combo."""
    valor = "si" if hay_personas else "no"
    resultado = _evaluar_con_reintentos(page, """([sel, valor]) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        if (!Array.from(el.options).some(o => o.value === valor)) return false;
        el.value = valor;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        if (window.jQuery) { try { window.jQuery(el).trigger('change'); } catch (e) {} }
        return true;
    }""", [selector, valor])
    return bool(resultado)


def liberar_condicionales_vacios(page: Page, nombres: Any = CONDICIONALES_LIBERABLES_DEFAULT) -> List[str]:
    """Campos condicionales de Symfony que el portal marca `required` aunque
    no apliquen (Otra Localidad si se eligió una del listado, Vencimiento
    del seguro, "Otro" tipo de lugar forestal): si están VACÍOS se les quita
    el `required` y se deshabilitan (no viajan en el POST). Con dato se
    dejan como están. Devuelve los ids liberados."""
    resultado = _evaluar_con_reintentos(page, r"""(nombres) => {
        const liberados = [];
        nombres.forEach((n) => {
            document.querySelectorAll('[id$="_' + n + '"]').forEach((el) => {
                if (!["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) return;
                if ((el.value || "").trim()) return;
                el.removeAttribute("required");
                el.disabled = true;
                liberados.push(el.id);
            });
        });
        return liberados;
    }""", list(nombres))
    return [str(x) for x in resultado] if isinstance(resultado, list) else []


# ---------------------------------------------------------------------------
# Guardado y avance
# ---------------------------------------------------------------------------

def _selectores_guardado() -> List[str]:
    """Botones de guardado en orden de prioridad: primero "Guardar y
    continuar" (avanza de panel), después los genéricos, y al final los que
    declare el mapping para cada pantalla."""
    base = [
        "button[name='save_&_continue']",
        "button.js-submit",
        "button:has-text('Guardar y Continuar')",
        "input[type='submit'][value*='Guardar y Continuar' i]",
        "button:has-text('Guardar cambios')",
        "input[type='submit'][value*='Guardar cambios' i]",
    ]
    for seccion in cargar_mapping().get("selectores", {}).values():
        if not isinstance(seccion, dict):
            continue
        for clave in ("btn_guardar_continuar", "btn_guardar", "btn_guardar_cambios"):
            valor = seccion.get(clave)
            if isinstance(valor, str) and valor not in base:
                base.append(valor)
    return base


def _primer_boton_visible(page: Page) -> Optional[Locator]:
    for selector in _selectores_guardado():
        try:
            candidatos = page.locator(selector)
            for i in range(candidatos.count()):
                boton = candidatos.nth(i)
                if boton.is_visible() and boton.is_enabled():
                    return boton
        except PlaywrightError:
            continue
    return None


def hacer_click_guardar(page: Page) -> bool:
    """Hace click en el botón de guardado de la pantalla actual. Si el click
    real falla (overlay, animación), dispara el submit del formulario desde
    JS con ese mismo botón como `submitter` -- así Symfony recibe igual el
    `name` del botón (p. ej. save_&_continue). True si se disparó algo."""
    boton = _primer_boton_visible(page)
    if boton is None:
        return False
    try:
        boton.click(timeout=5000)
        return True
    except PlaywrightError:
        pass
    try:
        return bool(boton.evaluate("""(el) => {
            const form = el.form || el.closest("form");
            if (!form) { el.click(); return true; }
            try { form.requestSubmit(el); } catch (e) { form.submit(); }
            return true;
        }"""))
    except PlaywrightError:
        return False


def _intentar_avanzar_un_paso(page: Page, timeout_navegacion: int = 10000) -> Optional[str]:
    """Libera `required` ocultos, guarda, y espera a que la página cambie de
    URL (o al menos a que la red quede quieta). Devuelve la URL resultante,
    o None si en la pantalla no había ningún botón de guardado."""
    url_antes = page.url
    _sembrar_tab_activa(page)
    if not hacer_click_guardar(page):
        return None

    try:
        page.wait_for_url(lambda u: u != url_antes, timeout=timeout_navegacion)
    except PlaywrightError:
        pass  # guardado vía AJAX o rechazado por validación: se queda en la misma URL
    for estado, timeout in (("domcontentloaded", 10000), ("networkidle", 8000)):
        try:
            page.wait_for_load_state(estado, timeout=timeout)
        except PlaywrightError:
            pass
    return page.url


# ---------------------------------------------------------------------------
# Diagnóstico de validación
# ---------------------------------------------------------------------------

_JS_ERRORES_VALIDACION = r"""
() => {
    function visible(el) { return el.getClientRects().length > 0; }
    function limpiar(t) { return (t || "").replace(/\s+/g, " ").trim(); }
    const textos = [];
    const agregar = (t) => { t = limpiar(t); if (t && !textos.includes(t)) textos.push(t); };

    // span.help-inline solo cuenta dentro de un grupo marcado con error: RUBA
    // también lo usa para textos de ayuda fijos (p. ej. el de "Buscar puntos").
    document.querySelectorAll(".alert-error, .alert-danger, .error span.help-inline, ul.errors li, .form-error-message")
        .forEach((el) => { if (visible(el)) agregar(el.innerText); });

    document.querySelectorAll(".has-error").forEach((grupo) => {
        if (!visible(grupo)) return;
        const ayudas = grupo.querySelectorAll(".help-block, .help-inline, .invalid-feedback");
        if (ayudas.length) { ayudas.forEach((a) => agregar(a.innerText)); return; }
        const label = grupo.querySelector("label");
        if (label) agregar(`Campo con error: ${limpiar(label.innerText)}`);
    });
    return textos.slice(0, 30);
}
"""

_JS_CAMPOS_INVALIDOS = r"""
() => {
    const nombres = [];
    const agregar = (el) => {
        const tipo = (el.getAttribute("type") || "").toLowerCase();
        if (el.disabled || ["hidden", "submit", "button"].includes(tipo)) return;
        const n = el.name || el.id;
        if (n && !nombres.includes(n)) nombres.push(n);
    };
    const visible = (el) => el.getClientRects().length > 0;
    document.querySelectorAll("input, select, textarea").forEach((el) => {
        if ((el.matches(":invalid") && visible(el)) || el.classList.contains("error")) agregar(el);
    });
    // Bootstrap 2 marca el contenedor (div.control-group.error), no el control.
    document.querySelectorAll(".error:not(input):not(select):not(textarea)").forEach((grupo) => {
        grupo.querySelectorAll("input, select, textarea").forEach(agregar);
    });
    return nombres.slice(0, 50);
}
"""


def extraer_errores_validacion(page: Page) -> List[str]:
    """Textos de error visibles (alertas Bootstrap y errores de formulario
    Symfony) en la pantalla actual."""
    resultado = _evaluar_con_reintentos(page, _JS_ERRORES_VALIDACION)
    return [str(t) for t in resultado] if isinstance(resultado, list) else []


def detectar_campos_invalidos(page: Page) -> List[str]:
    """name (o id) de los controles que el navegador considera `:invalid` o
    que el servidor marcó con la clase `error`."""
    resultado = _evaluar_con_reintentos(page, _JS_CAMPOS_INVALIDOS)
    return [str(n) for n in resultado] if isinstance(resultado, list) else []

"""
Inspección automática del formulario de carga de incidentes del RUBA.

Inicia sesión solo, usando las credenciales de `data/config.json`, y navega
directo hasta la pantalla de alta de incidente. Si el login automático no
encuentra los campos (el portal cambió el DOM, o cambiaron las credenciales),
cae de nuevo al flujo manual: vos iniciás sesión a mano en la ventana y
presionás ENTER para seguir.

Una vez ahí, cada vez que presionás ENTER en esta consola escanea la
pantalla actual extrayendo todos los inputs, selects, textareas,
checkboxes/radios y botones visibles, con un selector CSS robusto y el
label asociado a cada campo.

El resultado consolidado (todas las pantallas escaneadas) se guarda en
`config/ruba_campos_detectados.json`, para usarlo como insumo al completar
`config/ruba_mapping.json` con los selectores reales del portal.

Requisito previo (una sola vez): `playwright install chromium`

Uso:
    python tools/inspect_ruba.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

from app.paths import get_writable_dir

# Reintentos para page.evaluate() cuando el contexto de ejecución se destruye
# a mitad de una navegación (típico "Execution context was destroyed").
REINTENTOS_ESCANEO = 3
ESPERA_ENTRE_REINTENTOS_SEG = 0.5

URL_LOGIN_DEFAULT = "https://www.gestionbomberos.org/login"
URL_INCIDENTE_DEFAULT = "https://www.gestionbomberos.org/estructura/incidente/"

BASE_DIR = Path(__file__).resolve().parent.parent
# CONFIG_PATH (credenciales reales) es escribible: si la app corre
# empaquetada, se resuelve al lado del .exe, no dentro del bundle de
# solo lectura de PyInstaller -- ver app/paths.py.
CONFIG_PATH = get_writable_dir("data") / "config.json"
SALIDA_PATH = BASE_DIR / "config" / "ruba_campos_detectados.json"

VIEWPORT = {"width": 1366, "height": 768}

# Selectores habituales de los campos de login, en orden de prioridad. El
# primero que aparezca visible en la página se usa para completar el login.
SELECTORES_USUARIO = [
    "input[name='_username']",
    "#username",
    "input[name='username']",
    "input[type='email']",
    "#email",
    "input[name='email']",
]
SELECTORES_CLAVE = [
    "input[name='_password']",
    "#password",
    "input[name='password']",
    "input[type='password']",
]
SELECTORES_SUBMIT = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Entrar')",
    "button:has-text('Ingresar')",
    "button:has-text('Iniciar sesión')",
    "button:has-text('Iniciar Sesion')",
    "button:has-text('Login')",
]

MENSAJE_LOGIN_MANUAL = (
    "\nNo se pudo completar el login automático.\n"
    "Inicia sesión a mano en la ventana de Chromium y navegá hasta el formulario "
    "de carga de incidente.\n"
    "Cuando la pantalla esté lista, presiona ENTER en esta consola para escanear...\n> "
)

MENSAJE_LISTO_PARA_ESCANEAR = (
    "\nSesión iniciada y ya estás en la pantalla de carga de incidente.\n"
    "Si necesitás cambiar de pestaña/paso primero, hacelo en la ventana.\n"
    "Presiona ENTER en esta consola para escanear la pantalla actual...\n> "
)

MENSAJE_SIGUIENTE = (
    "\nCambiá de pestaña/paso/acordeón si corresponde y presioná ENTER para "
    "escanear la siguiente sección, o escribí 'fin' para guardar todo y cerrar.\n> "
)

# ---------------------------------------------------------------------------
# JavaScript inyectado: extrae la estructura del formulario visible.
# ---------------------------------------------------------------------------

JS_ESCANEAR_PANTALLA = r"""
() => {
    const TIPOS_TEXTO = new Set([
        "text", "number", "date", "datetime-local", "time", "email", "tel",
        "search", "password", "month", "week", "url", "color", "range",
    ]);

    function cssEscape(valor) {
        if (window.CSS && CSS.escape) return CSS.escape(valor);
        return String(valor).replace(/([ #.;?%&,+*~':"!^$\[\]()=>|\/])/g, "\\$1");
    }

    function construirSelector(el) {
        if (el.id) return "#" + cssEscape(el.id);

        const tipo = (el.getAttribute("type") || "").toLowerCase();
        if (el.name && (tipo === "radio" || tipo === "checkbox")) {
            // varios checkboxes/radios comparten el mismo name: el value los distingue.
            return `${el.tagName.toLowerCase()}[name="${el.name}"][value="${el.value}"]`;
        }
        if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;

        const partes = [];
        let actual = el;
        let profundidad = 0;
        while (actual && actual.nodeType === 1 && profundidad < 6) {
            let parte = actual.tagName.toLowerCase();
            if (actual.classList && actual.classList.length) {
                parte += "." + Array.from(actual.classList).slice(0, 2).map(cssEscape).join(".");
            }
            const padre = actual.parentElement;
            if (padre) {
                const hermanos = Array.from(padre.children).filter(h => h.tagName === actual.tagName);
                if (hermanos.length > 1) {
                    parte += `:nth-of-type(${hermanos.indexOf(actual) + 1})`;
                }
            }
            partes.unshift(parte);
            if (actual.id) break;
            actual = padre;
            profundidad += 1;
        }
        return partes.join(" > ");
    }

    function textoLimpioDeLabel(labelEl) {
        // Clona el <label> y le saca los controles de formulario anidados (ej. un
        // <select> con muchas <option>) para no arrastrar su contenido al texto.
        const clon = labelEl.cloneNode(true);
        clon.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
        return clon.innerText.trim();
    }

    function textoLabel(el) {
        if (el.id) {
            const porFor = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
            if (porFor) {
                const texto = textoLimpioDeLabel(porFor);
                if (texto) return texto;
            }
        }
        const labelEnvolvente = el.closest("label");
        if (labelEnvolvente) {
            const texto = textoLimpioDeLabel(labelEnvolvente);
            if (texto) return texto;
        }
        if (el.previousElementSibling && el.previousElementSibling.tagName === "LABEL") {
            return el.previousElementSibling.innerText.trim();
        }
        const contenedor = el.closest(
            '.form-group, .field, .mb-3, .form-field, .input-group, [class*="field"], [class*="form-group"]'
        );
        if (contenedor) {
            const lbl = contenedor.querySelector("label");
            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
        }
        return (el.getAttribute("aria-label") || "").trim();
    }

    function esVisible(el) {
        const rect = el.getBoundingClientRect();
        const estilo = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && estilo.visibility !== "hidden" && estilo.display !== "none";
    }

    const inputs = [];
    const checkboxesRadios = [];

    document.querySelectorAll("input").forEach((el) => {
        const tipo = (el.getAttribute("type") || "text").toLowerCase();
        if (tipo === "hidden") return;

        if (tipo === "checkbox" || tipo === "radio") {
            checkboxesRadios.push({
                id: el.id || null,
                name: el.name || null,
                type: tipo,
                value: el.value || null,
                checked: el.checked,
                label: textoLabel(el),
                visible: esVisible(el),
                selector: construirSelector(el),
            });
            return;
        }

        if (tipo === "submit" || tipo === "button" || tipo === "image" || tipo === "reset" || tipo === "file") {
            return; // se listan junto con los botones
        }

        if (!TIPOS_TEXTO.has(tipo)) return;

        inputs.push({
            id: el.id || null,
            name: el.name || null,
            type: tipo,
            placeholder: el.getAttribute("placeholder") || null,
            valor_actual: el.value || null,
            required: !!el.required,
            label: textoLabel(el),
            visible: esVisible(el),
            selector: construirSelector(el),
        });
    });

    const selects = [];
    document.querySelectorAll("select").forEach((el) => {
        selects.push({
            id: el.id || null,
            name: el.name || null,
            required: !!el.required,
            label: textoLabel(el),
            visible: esVisible(el),
            selector: construirSelector(el),
            opciones: Array.from(el.options).map((o) => ({ value: o.value, text: o.text.trim() })),
        });
    });

    const textareas = [];
    document.querySelectorAll("textarea").forEach((el) => {
        textareas.push({
            id: el.id || null,
            name: el.name || null,
            placeholder: el.getAttribute("placeholder") || null,
            required: !!el.required,
            label: textoLabel(el),
            visible: esVisible(el),
            selector: construirSelector(el),
        });
    });

    const elementosBoton = new Set([
        ...document.querySelectorAll("button"),
        ...document.querySelectorAll('input[type="submit"], input[type="button"]'),
        ...document.querySelectorAll('a[role="button"], a[class*="btn"]'),
    ]);
    const botones = Array.from(elementosBoton).map((el) => ({
        id: el.id || null,
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute("type") || null,
        texto: (el.innerText || el.value || el.getAttribute("aria-label") || "").trim(),
        clase: el.className || null,
        disabled: !!el.disabled,
        visible: esVisible(el),
        selector: construirSelector(el),
    }));

    return {
        url_actual: window.location.href,
        titulo_pagina: document.title,
        inputs,
        selects,
        textareas,
        checkboxes_radios: checkboxesRadios,
        botones,
    };
}
"""


def escanear_pagina(page: Page) -> Dict[str, Any]:
    """Ejecuta el escaneo JS sobre la pantalla actual y devuelve el resultado crudo.

    Si justo antes hubo una navegación (redirect, cambio de paso vía JS, etc.)
    el contexto de ejecución puede destruirse a mitad del `evaluate` y
    Playwright tira "Execution context was destroyed". Se espera a que el DOM
    esté listo y, si aun así vuelve a pasar, se reintenta unas pocas veces.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PlaywrightError:
        pass  # si no hay navegación en curso, wait_for_load_state puede no tener nada que esperar

    ultimo_error: Optional[Exception] = None
    for intento in range(1, REINTENTOS_ESCANEO + 1):
        try:
            return page.evaluate(JS_ESCANEAR_PANTALLA)
        except PlaywrightError as e:
            ultimo_error = e
            print(f"Aviso: el contexto de la página se destruyó durante el escaneo "
                  f"(intento {intento}/{REINTENTOS_ESCANEO}): {e}")
            time.sleep(ESPERA_ENTRE_REINTENTOS_SEG)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError:
                pass

    raise RuntimeError(
        f"No se pudo escanear la pantalla tras {REINTENTOS_ESCANEO} intentos: {ultimo_error}"
    )


def imprimir_resumen(nombre_paso: str, datos: Dict[str, Any]) -> None:
    print(f"\n--- Resumen: {nombre_paso} ---")
    print(f"URL: {datos['url_actual']}")
    print(f"Título de pantalla: {datos['titulo_pagina']}")
    print(f"Inputs de texto/número/fecha encontrados: {len(datos['inputs'])}")

    if datos["selects"]:
        print(f"Desplegables encontrados: {len(datos['selects'])}")
        for s in datos["selects"]:
            etiqueta = s["label"] or s["name"] or s["id"] or "(sin nombre)"
            print(f"   - {etiqueta}: {len(s['opciones'])} opción(es)")
    else:
        print("Desplegables encontrados: 0")

    print(f"Áreas de texto (textarea): {len(datos['textareas'])}")
    print(f"Checkboxes / radios: {len(datos['checkboxes_radios'])}")

    if datos["botones"]:
        print(f"Botones de navegación/acción detectados: {len(datos['botones'])}")
        for b in datos["botones"]:
            if b["texto"]:
                print(f"   - {b['texto']}")
    else:
        print("Botones de navegación/acción detectados: 0")


def guardar_resultados(resultados: Dict[str, Any]) -> None:
    resultados["actualizado_en"] = datetime.now().isoformat(timespec="seconds")
    SALIDA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SALIDA_PATH.open("w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    print(f"\nGuardado en: {SALIDA_PATH}  ({len(resultados['pasos'])} paso(s) en total)")


def cargar_resultados_previos() -> Optional[Dict[str, Any]]:
    """Si `config/ruba_campos_detectados.json` ya existe, lo carga para
    continuar anexando pasos en vez de empezar de cero."""
    if not SALIDA_PATH.exists():
        return None
    try:
        with SALIDA_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Aviso: no se pudo leer {SALIDA_PATH.name} ({e}); se empieza un archivo nuevo.")
        return None


def cargar_credenciales() -> Dict[str, str]:
    """Lee la sección 'ruba' de data/config.json. Si el archivo o la clave no
    existen, devuelve un dict vacío (el script sigue funcionando en modo manual)."""
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("ruba", {}) or {}


def _primer_campo_visible(page: Page, selectores: list[str], timeout: int = 2000) -> Optional[Locator]:
    for selector in selectores:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def iniciar_sesion(page: Page, usuario: str, clave: str, url_login: str) -> bool:
    """Intenta loguearse solo. Devuelve True si parece haber funcionado
    (la URL dejó de ser la de login), False si hay que hacerlo a mano."""
    if not usuario or not clave:
        print("No hay usuario/clave configurados en data/config.json: se salta el login automático.")
        return False

    print(f"Abriendo login: {url_login}")
    page.goto(url_login)
    page.wait_for_load_state("domcontentloaded")

    campo_usuario = _primer_campo_visible(page, SELECTORES_USUARIO)
    campo_clave = _primer_campo_visible(page, SELECTORES_CLAVE)

    if campo_usuario is None or campo_clave is None:
        print("No se detectaron automáticamente los campos de usuario/contraseña.")
        return False

    campo_usuario.fill(usuario)
    campo_clave.fill(clave)

    boton = _primer_campo_visible(page, SELECTORES_SUBMIT, timeout=1500)
    try:
        if boton is not None:
            boton.click()
        else:
            campo_clave.press("Enter")
    except Exception as e:
        print(f"No se pudo hacer click en el botón de login ({e}); se intenta con Enter.")
        try:
            campo_clave.press("Enter")
        except Exception:
            return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    if "login" in page.url.lower():
        print(f"La URL sigue pareciendo la de login ({page.url}); el login automático puede haber fallado.")
        return False

    print(f"Sesión iniciada. URL actual: {page.url}")
    return True


def main() -> None:
    credenciales = cargar_credenciales()
    usuario = credenciales.get("usuario", "").strip()
    clave = credenciales.get("clave", "")
    url_login = credenciales.get("url_login", "").strip() or URL_LOGIN_DEFAULT
    url_incidentes = credenciales.get("url_incidentes", "").strip() or URL_INCIDENTE_DEFAULT
    url_agregar = url_incidentes.rstrip("/") + "/agregar"

    resultados = cargar_resultados_previos()
    if resultados is not None:
        pasos_previos = resultados.setdefault("pasos", [])
        paso_num = max((p.get("paso", 0) for p in pasos_previos), default=0)
        resultados.setdefault("creado_en", resultados.pop("generado_en", None)
                               or datetime.now().isoformat(timespec="seconds"))
        resultados["url_login"] = url_login
        resultados["url_incidentes"] = url_incidentes
        print(f"Se encontró {SALIDA_PATH.name} con {len(pasos_previos)} paso(s) ya guardado(s). "
              f"Se continúa desde el paso {paso_num + 1}.")
    else:
        resultados = {
            "url_login": url_login,
            "url_incidentes": url_incidentes,
            "creado_en": datetime.now().isoformat(timespec="seconds"),
            "pasos": [],
        }
        paso_num = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        contexto = browser.new_context(viewport=VIEWPORT)
        page = contexto.new_page()

        sesion_ok = iniciar_sesion(page, usuario, clave, url_login)

        if sesion_ok:
            print(f"Redirigiendo a: {url_agregar}")
            page.goto(url_agregar)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            mensaje_inicial = MENSAJE_LISTO_PARA_ESCANEAR
        else:
            page.goto(url_incidentes)
            mensaje_inicial = MENSAJE_LOGIN_MANUAL

        try:
            respuesta = input(mensaje_inicial)

            while respuesta.strip().lower() != "fin":
                paso_num += 1
                nombre_paso = f"Paso {paso_num}"
                print(f"\nEscaneando {nombre_paso}...")

                datos = escanear_pagina(page)
                datos["paso"] = paso_num
                datos["nombre"] = nombre_paso
                resultados["pasos"].append(datos)

                imprimir_resumen(nombre_paso, datos)

                respuesta = input(MENSAJE_SIGUIENTE)

        except (KeyboardInterrupt, EOFError):
            print("\nInterrumpido por el usuario. Se guarda lo escaneado hasta ahora.")
        finally:
            guardar_resultados(resultados)
            browser.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        # En consolas de Windows sin UTF-8 activo, evita UnicodeEncodeError al imprimir acentos.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()

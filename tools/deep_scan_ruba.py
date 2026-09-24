from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_ruba import (  # noqa: E402
    BASE_DIR,
    URL_INCIDENTE_DEFAULT,
    URL_LOGIN_DEFAULT,
    VIEWPORT,
    cargar_credenciales,
    escanear_pagina,
    iniciar_sesion,
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import Locator, Page, sync_playwright  # noqa: E402

SALIDA_PATH = BASE_DIR / "config" / "ruba_formularios_por_tipo.json"
SALIDA_TIPOS_PATH = BASE_DIR / "config" / "ruba_mapeo_completo_tipos.json"

TIPOS_INCIDENTE: Dict[int, str] = {
    1: "Accidentes",
    2: "Factores Climáticos",
    3: "Incendios",
    4: "Materiales Peligrosos",
    5: "Rescates",
    6: "Servicios Especiales",
}

PREFIJO_NUMERO_PARTE = "9990"

SEL_NUMERO_PARTE = "#bomberos_estructurabundle_inicializacionIncidenteType_numeroParte"
SEL_TIPO = "#bomberos_estructurabundle_inicializacionIncidenteType_tipoIncidente"
SEL_CATEGORIA = "#bomberos_estructurabundle_inicializacionIncidenteType_categoriaIncidente"
SEL_HAY_PARTICIPACIONES = "#bomberos_estructurabundle_inicializacionIncidenteType_hayParticipciones"
SEL_CUERPOS = "#bomberos_estructurabundle_inicializacionIncidenteType_cuerpos"
SEL_BOTON_GUARDAR = "button.js-submit"

CATEGORIA_POR_TIPO: Dict[int, str] = {1: "3", 2: "9", 3: "23", 4: "25", 5: "28", 6: "32"}

MAX_INTENTOS_INICIALIZACION = 3
MAX_PANELES_POR_TIPO = 15

DATOS_FICTICIOS: Dict[str, str] = {
    "direccion": "San Martín 100",
    "localidad": "Adelia María",
    "telefono": "3585000000",
    "dni": "00000000",
    "email": "prueba.qa@example.com",
    "texto_generico": "Dato de prueba (inspeccion_ruba)",
}

# Personas reales para buscar en los autocomplete de RUBA: salen de
# data/seed_bomberos.json (fuera del repo), no del código.
from app.core.semilla import personal_inicial  # noqa: E402
import unicodedata  # noqa: E402

POOL_PERSONAL: List[Dict[str, str]] = personal_inicial()
APELLIDOS_PERSONAL: List[str] = sorted({
    variante
    for p in POOL_PERSONAL
    for variante in (p["apellido"].lower(),
                     unicodedata.normalize("NFKD", p["apellido"].lower()).encode("ascii", "ignore").decode())
})

HORARIOS_MAESTROS: Dict[str, str] = {
    "fecha": "22/09/2026",
    "fecha_iso": "2026-09-22",
    "hora_llamado": "08:15",
    "hora_toque": "08:17",
    "hora_salida": "08:20",
    "hora_regreso": "09:45",
}

RESENA_MAESTRA = "Servicio de prueba de relevamiento técnico."

DATOS_VICTIMA: Dict[str, str] = {
    "nombre": "Carlos",
    "apellido": "Gómez",
    "dni": "28456123",
    "edad": "45",
    "sexo": "Masculino",
    "gravedad": "Leve",
    "traslado": "Ambulancia local",
    "destino": "Hospital Municipal Adelia María",
}

DATOS_SEGURO: Dict[str, str] = {
    "compania": "La Segunda",
    "poliza": "12345678",
}

JS_DETECTAR_SECCIONES = r"""
() => {
    function cssEscape(v) {
        if (window.CSS && CSS.escape) return CSS.escape(v);
        return String(v).replace(/([ #.;?%&,+*~':"!^$\[\]()=>|\/])/g, "\\$1");
    }
    function selectorDe(el) {
        if (el.id) return "#" + cssEscape(el.id);
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
                const hermanos = Array.from(padre.children).filter((h) => h.tagName === actual.tagName);
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

    const SELECTORES_POSIBLES = [
        '[role="tab"]', ".nav-tabs a", ".nav-tabs .nav-link", ".nav-pills .nav-link",
        "[data-toggle='tab']", "[data-bs-toggle='tab']", ".tabs li", ".tab-header",
        ".accordion-header button", ".accordion-button", ".panel-heading a",
        ".wizard-step", ".step", "[aria-selected]",
    ];

    const vistos = new Set();
    const secciones = [];
    SELECTORES_POSIBLES.forEach((sel) => {
        let elementos;
        try {
            elementos = document.querySelectorAll(sel);
        } catch (e) {
            return;
        }
        elementos.forEach((el) => {
            if (vistos.has(el)) return;
            vistos.add(el);
            const texto = (el.innerText || el.textContent || "").trim();
            if (!texto) return;
            secciones.push({
                texto,
                selector: selectorDe(el),
                activa: el.classList.contains("active") || el.getAttribute("aria-selected") === "true",
            });
        });
    });
    return secciones;
}
"""

JS_DETECTAR_PERSONAL_Y_DOTACIONES = r"""
() => {
    const hallazgos = [];
    const SELECTORES_DOTACION = [
        'table[class*="personal"]', 'table[class*="dotacion"]', 'div[id*="personal"]',
        'div[id*="dotacion"]', 'div[id*="apresto"]', 'div[id*="reserva"]',
        'select[multiple]', '[data-role="personal"]', '.js-personal', '.js-dotacion'
    ];
    
    document.querySelectorAll('input, select, table, div').forEach((el) => {
        const id = (el.id || '').toLowerCase();
        const name = (el.getAttribute('name') || '').toLowerCase();
        const clase = (el.className || '').toLowerCase();
        const texto = (el.innerText || '').toLowerCase().slice(0, 150);
        
        const parecePersonal = /personal|dotaci[oó]n|bombero|interviniente|apresto|reserva|a\s*cargo|chofer/i;
        if (parecePersonal.test(id) || parecePersonal.test(name) || parecePersonal.test(clase) || parecePersonal.test(texto)) {
            hallazgos.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                name: el.getAttribute('name') || null,
                clases: el.className || null,
                texto_muestra: (el.innerText || '').slice(0, 100).trim(),
                tipo_input: el.getAttribute('type') || null
            });
        }
    });
    return hallazgos.slice(0, 30);
}
"""

JS_AUTOCOMPLETAR_REQUERIDOS = r"""
(datos) => {
    function esVisible(el) {
        const rect = el.getBoundingClientRect();
        const estilo = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && estilo.visibility !== "hidden" && estilo.display !== "none";
    }
    function esSelectOperable(el) {
        if (esVisible(el)) return true;
        const hermano = el.nextElementSibling;
        if (hermano && esVisible(hermano) && /select2|chosen|selectize/i.test(hermano.className || "")) return true;
        return !!el.closest('.select2-container, .chosen-container, .selectize-control, [class*="select2"]');
    }
    function textoLabelDe(el) {
        if (el.id) {
            const porFor = document.querySelector(`label[for="${el.id}"]`);
            if (porFor) {
                const clon = porFor.cloneNode(true);
                clon.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
                const t = clon.innerText.trim();
                if (t) return t;
            }
        }
        const env = el.closest("label");
        if (env) {
            const clon = env.cloneNode(true);
            clon.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
            const t = clon.innerText.trim();
            if (t) return t;
        }
        const contenedor = el.closest('.form-group, .field, .mb-3, [class*="form-group"], [class*="field"]');
        if (contenedor) {
            const lbl = contenedor.querySelector("label");
            if (lbl) return lbl.innerText.trim();
        }
        return "";
    }
    function pistas(el) {
        return [textoLabelDe(el), el.name || "", el.id || "", el.getAttribute("placeholder") || ""]
            .join(" ").toLowerCase();
    }
    function disparar(el, eventos) {
        eventos.forEach((tipo) => el.dispatchEvent(new Event(tipo, { bubbles: true })));
    }

    const rellenados = [];
    document.querySelectorAll("input, textarea").forEach((el) => {
        const tipo = (el.getAttribute("type") || "text").toLowerCase();
        if (["checkbox", "radio", "submit", "button", "hidden", "file", "image", "reset"].includes(tipo)) return;
        if (!esVisible(el)) return;
        if (!el.required) return;
        if (el.value && el.value.trim() !== "") return;

        let valor = datos.texto_generico;
        const p = pistas(el);
        if (/fecha/i.test(p)) valor = datos.horarios.fecha_iso || "2026-09-22";
        else if (/hora/i.test(p)) valor = "08:30";
        else if (/tel[eé]fono/i.test(p)) valor = datos.telefono;
        else if (/dni|documento/i.test(p)) valor = datos.dni;

        el.value = valor;
        disparar(el, ["input", "change", "blur"]);
        rellenados.push({ campo: p.slice(0, 50), valor });
    });

    document.querySelectorAll("select").forEach((el) => {
        if (!esSelectOperable(el)) return;
        if (!el.required) return;
        if (el.value && el.value.trim() !== "") return;

        const opciones = Array.from(el.options).filter((o) => o.value.trim() !== "" && !o.disabled);
        if (opciones.length === 0) return;

        const elegida = opciones[0];
        el.value = elegida.value;
        disparar(el, ["input", "change"]);
        if (window.jQuery) {
            try {
                window.jQuery(el).trigger("change");
                window.jQuery(el).trigger({ type: "select2:select", params: { data: { id: elegida.value } } });
            } catch (e) {}
        }
        rellenados.push({ campo: pistas(el).slice(0, 50), valor: elegida.value });
    });

    return rellenados;
}
"""

JS_UBICACION_LOCALIDAD_Y_BUSCAR = r"""
(datos) => {
    function esVisible(el) {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== "none";
    }
    const RE_LOCALIDAD = /localidad|ciudad/i;
    const RE_BUSCAR_PUNTOS = /buscar\s*puntos?/i;
    const resultado = { localidad_completada: false, buscar_puntos_click: false };

    let campoLocalidad = null;
    document.querySelectorAll('input[type="text"], input:not([type])').forEach((el) => {
        if (campoLocalidad || !esVisible(el)) return;
        if (RE_LOCALIDAD.test([el.name, el.id, el.placeholder].join(" "))) campoLocalidad = el;
    });
    if (campoLocalidad) {
        campoLocalidad.value = datos.localidad;
        campoLocalidad.dispatchEvent(new Event('input', { bubbles: true }));
        campoLocalidad.dispatchEvent(new Event('change', { bubbles: true }));
        resultado.localidad_completada = true;
    }

    let boton = document.querySelector(".js-buscar-puntos");
    if (!boton || !esVisible(boton)) {
        boton = Array.from(document.querySelectorAll('button, a, input[type="button"]'))
            .find((el) => esVisible(el) && RE_BUSCAR_PUNTOS.test((el.innerText || el.value || "").trim()));
    }
    if (boton && esVisible(boton)) {
        boton.click();
        resultado.buscar_puntos_click = true;
    }
    return resultado;
}
"""

JS_UBICACION_PUNTO_DIRECCION_ZONA = r"""
(datos) => {
    function esVisible(el) {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== "none";
    }
    const SELECTORES_PUNTOS = [
        ".js-punto-sugerido", ".punto-resultado", ".resultado-punto", ".list-group-item",
        '[role="option"]', ".leaflet-marker-icon"
    ];
    let punto = null;
    for (const sel of SELECTORES_PUNTOS) {
        const candidatos = Array.from(document.querySelectorAll(sel)).filter(esVisible);
        if (candidatos.length) { punto = candidatos[0]; break; }
    }
    if (punto) punto.click();

    let campoDireccion = null;
    document.querySelectorAll('input[type="text"], input:not([type])').forEach((el) => {
        if (campoDireccion || !esVisible(el)) return;
        if (/direcci[oó]n|domicilio|calle/i.test([el.name, el.id].join(" "))) campoDireccion = el;
    });
    if (campoDireccion) {
        campoDireccion.value = datos.calle;
        campoDireccion.dispatchEvent(new Event('input', { bubbles: true }));
        campoDireccion.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
}
"""

def _evaluar_con_reintentos(page: Page, js: str, arg: Any = None, intentos: int = 3, espera: float = 0.5) -> Any:
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

def detectar_secciones(page: Page) -> List[Dict[str, Any]]:
    res = _evaluar_con_reintentos(page, JS_DETECTAR_SECCIONES)
    return res if isinstance(res, list) else []

def detectar_personal(page: Page) -> List[Dict[str, Any]]:
    res = _evaluar_con_reintentos(page, JS_DETECTAR_PERSONAL_Y_DOTACIONES)
    return res if isinstance(res, list) else []

def limpiar_required_ocultos(page: Page) -> None:
    try:
        page.evaluate("""() => {
            document.querySelectorAll("select, input").forEach((el) => {
                if (el.offsetParent === null) el.removeAttribute("required");
            });
            const cuerpos = document.querySelector("#bomberos_estructurabundle_inicializacionIncidenteType_cuerpos");
            if (cuerpos) cuerpos.removeAttribute("required");
        }""")
    except PlaywrightError:
        pass

def seleccionar_con_sincronizacion(page: Page, selector: str, value: str) -> None:
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

def extraer_arbol_tipos_y_subtipos(page: Page, url_agregar: str) -> Dict[str, Any]:
    print("\n--- EXTRAYENDO ÁRBOL COMPLETO DE TIPOS Y SUBTIPOS ---")
    page.goto(url_agregar)
    page.wait_for_load_state("domcontentloaded", timeout=15000)

    arbol: Dict[str, Any] = {}
    for tipo_id, tipo_nombre in TIPOS_INCIDENTE.items():
        print(f"Mapeando Tipo {tipo_id}: {tipo_nombre}...")
        seleccionar_con_sincronizacion(page, SEL_TIPO, str(tipo_id))
        page.wait_for_timeout(1000)

        opciones = page.eval_on_selector_all(
            f"{SEL_CATEGORIA} option",
            "els => els.map(o => ({id: o.value, nombre: (o.textContent || '').trim()}))"
        )
        validas = [o for o in opciones if o["id"].strip() != ""]
        arbol[str(tipo_id)] = {
            "id": tipo_id,
            "nombre": tipo_nombre,
            "subtipos": validas,
        }
        print(f"  -> {len(validas)} subtipos encontrados.")

    SALIDA_TIPOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SALIDA_TIPOS_PATH.open("w", encoding="utf-8") as f:
        json.dump(arbol, f, ensure_ascii=False, indent=2)
    print(f"Mapeo de tipos guardado con éxito en: {SALIDA_TIPOS_PATH}\n")
    return arbol

def buscar_boton_avance(page: Page) -> Optional[Locator]:
    textos_validos = ["guardar y continuar", "siguiente", "continuar", "guardar"]
    for t in textos_validos:
        loc = page.locator(f'button:has-text("{t}"), a.btn:has-text("{t}"), input[type="submit"]:has-text("{t}")')
        for i in range(loc.count()):
            item = loc.nth(i)
            if item.is_visible():
                return item
    return None

def ejecutar_inspeccion_tipo(page: Page, url_agregar: str, tipo_id: int) -> Dict[str, Any]:
    numero_parte = f"{PREFIJO_NUMERO_PARTE}{tipo_id}"
    page.goto(url_agregar)
    page.wait_for_load_state("domcontentloaded", timeout=15000)

    page.fill(SEL_NUMERO_PARTE, numero_parte)
    seleccionar_con_sincronizacion(page, SEL_TIPO, str(tipo_id))
    page.wait_for_timeout(1000)

    cat_val = CATEGORIA_POR_TIPO.get(tipo_id, "23")
    seleccionar_con_sincronizacion(page, SEL_CATEGORIA, cat_val)
    page.uncheck(SEL_HAY_PARTICIPACIONES)
    limpiar_required_ocultos(page)

    btn_guardar = page.locator(SEL_BOTON_GUARDAR).first
    btn_guardar.click()
    page.wait_for_url(lambda u: "/agregar" not in u, timeout=12000)
    page.wait_for_load_state("networkidle", timeout=8000)

    url_incidente = page.url
    print(f"Incidente inicializado. URL: {url_incidente}")

    paneles_escaneados = []
    for paso in range(1, MAX_PANELES_POR_TIPO + 1):
        print(f"\n--- PANEL {paso} ---")
        _evaluar_con_reintentos(page, JS_UBICACION_LOCALIDAD_Y_BUSCAR, {"localidad": "Adelia María"})
        page.wait_for_timeout(800)
        _evaluar_con_reintentos(page, JS_UBICACION_PUNTO_DIRECCION_ZONA, {"calle": "Belgrano 58"})

        _evaluar_con_reintentos(page, JS_AUTOCOMPLETAR_REQUERIDOS, {
            "texto_generico": "Prueba", "telefono": "3585000000", "dni": "00000000", "horarios": HORARIOS_MAESTROS
        })

        secciones = detectar_secciones(page)
        sec_activa = next((s["texto"] for s in secciones if s.get("activa")), f"Solapa {paso}")
        print(f"Solapa activa detectada: '{sec_activa}'")

        personal_detectado = detectar_personal(page)
        if personal_detectado:
            print(f"  [!] ATENCIÓN: Se detectaron {len(personal_detectado)} elementos de Personal/Dotación en esta solapa.")

        paneles_escaneados.append({
            "paso": paso,
            "solapa": sec_activa,
            "url": page.url,
            "campos": escanear_pagina(page),
            "deteccion_dotacion_y_personal": personal_detectado
        })

        print("\nOpciones de control:")
        print("  [ENTER]      -> Intentar autocompletar y avanzar a la siguiente solapa.")
        print("  'p' + ENTER  -> PAUSA manual: abre el inspector (F12 en Chrome) para ver selectores.")
        print("  'q' + ENTER  -> Cortar y guardar lo escaneado hasta acá.")
        opcion = input("Comando > ").strip().lower()

        if opcion == "q":
            break
        elif opcion == "p":
            input("Navegador PAUSADO. Inspecciona en Chrome y presiona ENTER para continuar...")

        boton = buscar_boton_avance(page)
        limpiar_required_ocultos(page)
        if boton:
            try:
                boton.click()
                page.wait_for_load_state("domcontentloaded", timeout=6000)
                page.wait_for_load_state("networkidle", timeout=4000)
            except PlaywrightError:
                pass
        else:
            print("No se detectó botón de avance automático.")
            break

    return {
        "tipo_id": tipo_id,
        "numero_parte": numero_parte,
        "url_incidente": url_incidente,
        "paneles": paneles_escaneados
    }

def main() -> None:
    credenciales = cargar_credenciales()
    usuario = credenciales.get("usuario", "").strip()
    clave = credenciales.get("clave", "")
    url_login = credenciales.get("url_login", "").strip() or URL_LOGIN_DEFAULT
    url_incidentes = credenciales.get("url_incidentes", "").strip() or URL_INCIDENTE_DEFAULT
    url_agregar = url_incidentes.rstrip("/") + "/agregar"

    print("=" * 72)
    print("PROSPECCIÓN Y MAPEO VISUAL DE RUBA (Headless=False)")
    print("=" * 72)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250)
        contexto = browser.new_context(viewport={"width": 1280, "height": 850})
        page = contexto.new_page()

        if not iniciar_sesion(page, usuario, clave, url_login):
            input("Inicia sesión manualmente en el navegador y presiona ENTER...")

        extraer_arbol_tipos_y_subtipos(page, url_agregar)

        print("Iniciando creación de incidente exploratorio Tipo 3 (Incendios - Parte 99903)...")
        resultado = ejecutar_inspeccion_tipo(page, url_agregar, 3)

        SALIDA_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SALIDA_PATH.open("w", encoding="utf-8") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        print(f"\nReporte completo guardado en: {SALIDA_PATH}")

        browser.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
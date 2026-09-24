import json
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "data" / "config.json"
OUTPUT_DIR = BASE_DIR / "config"
OUTPUT_REPORTE = OUTPUT_DIR / "ruba_escaneo_interactivo.json"

def cargar_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"No se encontró {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def extraer_radiografia_completa(page, paso_num, nombre_paso):
    """Extrae todos los inputs, selects, radios, checkboxes y tablas de la pantalla visible."""
    print(f"\n[Analizando pantalla #{paso_num}: '{nombre_paso}']...")

    datos = page.evaluate("""() => {
        function textoLimpio(t) {
            return (t || '').replace(/\\s+/g, ' ').trim();
        }

        // 1. Extraer todos los Selects (desplegables)
        const selects = Array.from(document.querySelectorAll('select')).map(s => {
            const opciones = Array.from(s.options).map(o => ({
                id: o.value,
                texto: textoLimpio(o.textContent)
            })).filter(o => o.id !== "");

            return {
                id: s.id || '',
                name: s.name || '',
                label_cercano: textoLimpio(s.closest('.form-group, td, tr, label')?.querySelector('label')?.innerText || ''),
                cantidad_opciones: opciones.length,
                opciones: opciones
            };
        });

        // 2. Extraer Checkboxes y Radios (muy importante para Intervinientes vs Apresto)
        const checks = Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"]')).map(c => ({
            tag: 'input',
            tipo: c.type,
            id: c.id || '',
            name: c.name || '',
            value: c.value || '',
            checked: c.checked,
            etiqueta: textoLimpio(c.closest('label, tr, td, .form-group')?.innerText || '')
        }));

        // 3. Extraer Inputs de Texto / Fechas / Horas
        const inputs_texto = Array.from(document.querySelectorAll('input:not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"]), textarea')).map(i => ({
            tag: i.tagName.toLowerCase(),
            tipo: i.type || 'text',
            id: i.id || '',
            name: i.name || '',
            placeholder: i.placeholder || '',
            label_cercano: textoLimpio(i.closest('.form-group, td, tr, label')?.querySelector('label')?.innerText || '')
        }));

        // 4. Extraer Botones Visibles
        const botones = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a.btn')).map(b => ({
            id: b.id || '',
            name: b.name || '',
            texto: textoLimpio(b.innerText || b.value || ''),
            clases: b.className || ''
        })).filter(b => b.texto.length > 0);

        // 5. Detectar específicamente tablas o bloques de Personal / Dotación
        const tablas_personal = Array.from(document.querySelectorAll('table')).filter(t => {
            const txt = (t.innerText || '').toLowerCase();
            return /personal|bombero|legajo|dotaci|apresto|reserva|interviniente/i.test(txt);
        }).map(t => ({
            id: t.id || '',
            headers: Array.from(t.querySelectorAll('th')).map(th => textoLimpio(th.innerText)),
            filas_muestra: Array.from(t.querySelectorAll('tr')).slice(0, 5).map(tr => 
                Array.from(tr.querySelectorAll('td')).map(td => textoLimpio(td.innerText))
            )
        }));

        return {
            titulo_pagina: document.title,
            url: window.location.href,
            selects,
            checks,
            inputs_texto,
            botones,
            tablas_personal
        };
    }""")

    return datos

def main():
    config = cargar_config()
    ruba_cfg = config.get("ruba", {})
    usuario = ruba_cfg.get("usuario", "").strip()
    clave = ruba_cfg.get("clave", "").strip()
    url_base = ruba_cfg.get("url_base", "https://www.gestionbomberos.org").rstrip("/")
    url_login = f"{url_base}/login"

    print("=" * 75)
    print("ESCANEO ASISTIDO INTERACTIVO DE RUBA (Gestión Bomberil)")
    print("=" * 75)
    print(f"URL: {url_login}")
    print(f"Usuario: {usuario}")
    print("\nAbriendo navegador Chromium en tu pantalla...")

    historial_escaneos = []
    paso = 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(viewport={"width": 1366, "height": 850})
        page = context.new_page()

        page.goto(url_login, timeout=30000)

        # Intento de login rápido
        try:
            user_in = page.locator('input[name="_username"], input[name="username"], input[type="email"], #username').first
            pass_in = page.locator('input[name="_password"], input[name="password"], input[type="password"], #password').first
            btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Ingresar"), button:has-text("Iniciar")').first

            if user_in.is_visible(timeout=4000):
                user_in.fill(usuario)
                pass_in.fill(clave)
                btn.click()
                page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        print("\n" + "*" * 75)
        print("MODO INTERACTIVO LISTO:")
        print("- Navegá libremente en el navegador como si fueras a cargar un servicio.")
        print("- Cada vez que cambies de pantalla, pestaña, o selecciones un Tipo distinto:")
        print("  Venís a esta consola y apretás [ENTER].")
        print("- El script leerá todo lo que haya en esa pantalla y lo guardará.")
        print("- Cuando termines todo, escribí 'q' y presioná [ENTER] para salir.")
        print("*" * 75 + "\n")

        while True:
            accion = input(f"[{paso}] Presioná ENTER para escanear la pantalla actual (o 'q' para terminar): ").strip()
            if accion.lower() == 'q':
                break

            # Podés ponerle una etiqueta rápida si querés
            nombre_seccion = input(f"     Nombre o nota para esta pantalla (ej: 'Tipo Incendios', 'Solapa Dotación', etc. o Enter para omitir): ").strip()
            if not nombre_seccion:
                nombre_seccion = f"Paso {paso}"

            resultado = extraer_radiografia_completa(page, paso, nombre_seccion)
            resultado["numero_paso"] = paso
            resultado["etiqueta_usuario"] = nombre_seccion

            historial_escaneos.append(resultado)

            # Resumen en consola de lo detectado
            print(f"     -> {len(resultado['selects'])} Desplegables capturados.")
            for s in resultado['selects']:
                print(f"        * Select '{s['id'] or s['name']}' ({s['label_cercano']}) -> {s['cantidad_opciones']} opciones.")

            print(f"     -> {len(resultado['checks'])} Checkboxes/Radios.")
            if resultado['tablas_personal']:
                print(f"     -> [!] {len(resultado['tablas_personal'])} Tabla(s) de personal/dotación detectada(s).")

            # Guardar incrementalmente en cada paso
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(OUTPUT_REPORTE, "w", encoding="utf-8") as f:
                json.dump(historial_escaneos, f, ensure_ascii=False, indent=2)

            print(f"     [Guardado en {OUTPUT_REPORTE.name}]")
            paso += 1

        print("\n" + "=" * 75)
        print(f"Escaneo finalizado. Se capturaron {len(historial_escaneos)} pantallas.")
        print(f"Archivo completo disponible en: {OUTPUT_REPORTE}")
        print("=" * 75)
        browser.close()

if __name__ == "__main__":
    main()
    
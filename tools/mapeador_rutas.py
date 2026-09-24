import json
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_FILE = BASE_DIR / "config" / "rutas_mapeadas.json"


def capturar_estructura_pantalla(page):
    """Extrae la URL, título, botones, selects e inputs de la pantalla actual sin interactuar."""
    return page.evaluate(
        """() => {
        const url = window.location.href;
        const titulo = document.querySelector('h1, h2, .page-header, .title')?.innerText?.trim() || document.title;

        // Botones y enlaces principales
        const botones = Array.from(document.querySelectorAll('button, input[type="submit"], a.btn')).map(b => ({
            tag: b.tagName.toLowerCase(),
            texto: (b.innerText || b.value || '').trim(),
            name: b.getAttribute('name'),
            id: b.id || null,
            class: b.className || null,
            onclick: b.getAttribute('onclick') || null
        })).filter(b => b.texto.length > 0 || b.name || b.id);

        // Inputs y áreas de texto
        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea')).map(i => {
            let labelText = '';
            if (i.id) {
                const l = document.querySelector(`label[for="${i.id}"]`);
                if (l) labelText = l.innerText.trim();
            }
            if (!labelText && i.closest('label')) {
                labelText = i.closest('label').innerText.trim();
            }
            if (!labelText && i.placeholder) {
                labelText = `Placeholder: ${i.placeholder}`;
            }

            return {
                label: labelText || null,
                id: i.id || null,
                name: i.getAttribute('name') || null,
                type: i.getAttribute('type') || 'text',
                class: i.className || null
            };
        }).filter(i => i.id || i.name);

        // Desplegables / Selects
        const selects = Array.from(document.querySelectorAll('select')).map(s => {
            let labelText = '';
            if (s.id) {
                const l = document.querySelector(`label[for="${s.id}"]`);
                if (l) labelText = l.innerText.trim();
            }
            const opciones = Array.from(s.options).map(o => ({
                valor: o.value,
                texto: o.innerText.trim()
            })).filter(o => o.valor !== '');

            return {
                label: labelText || null,
                id: s.id || null,
                name: s.getAttribute('name') || null,
                opciones: opciones.slice(0, 10)
            };
        }).filter(s => s.id || s.name);

        return {
            url,
            titulo,
            botones,
            inputs,
            selects
        };
    }"""
    )


def main():
    print("=" * 70)
    print("MAPILADOR DE RUTAS Y PANTALLAS RUBA (MODO OBSERVADOR)")
    print("=" * 70)
    print("Este script NO toca nada en la página.")
    print("Navegá vos por el sistema y apretá ENTER cada vez que quieras mapear.")
    print("Escribí 'q' y ENTER cuando termines.")
    print("=" * 70)

    historial = []
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                historial = json.load(f)
        except Exception:
            historial = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print("\n[ERROR] No se pudo conectar a Chrome.")
            print("Asegurate de haber abierto Chrome con:")
            print(
                '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\ChromePerfilRUBA"'
            )
            return

        context = browser.contexts[0]
        page = context.pages[0]

        print(f"\n[OK] Conectado a la pestaña activa: {page.url}\n")

        paso = len(historial) + 1

        while True:
            cmd = input(
                f"\n>>> [Paso {paso}] Parate en una pantalla de RUBA y presiona ENTER (o 'q' para salir): "
            )
            if cmd.strip().lower() == "q":
                break

            try:
                for pg in context.pages:
                    if "gestionbomberos" in pg.url:
                        page = pg
                        break

                datos = capturar_estructura_pantalla(page)
                datos["paso"] = paso
                historial.append(datos)

                OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(historial, f, indent=4, ensure_ascii=False)

                print("\n" + "-" * 60)
                print("PANTALLA MAPEADA CON ÉXITO:")
                print(f"URL: {datos['url']}")
                print(f"Título: {datos['titulo']}")
                print(f"Inputs encontrados: {len(datos['inputs'])}")
                print(f"Selects encontrados: {len(datos['selects'])}")
                print(f"Botones detectados: {len(datos['botones'])}")
                print(f"Guardado en: {OUTPUT_FILE}")
                print("-" * 60)

                print("Botones disponibles en esta pantalla:")
                for b in datos["botones"]:
                    print(f"  * [{b['texto']}] id={b['id']} name={b['name']}")

                paso += 1

            except Exception as e:
                print(f"[Error capturando pantalla]: {e}")

        print("\n" + "=" * 70)
        print(
            f"Mapeo finalizado. Se registraron {len(historial)} pantallas en {OUTPUT_FILE}"
        )
        print("=" * 70)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
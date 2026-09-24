import sys
from playwright.sync_api import sync_playwright

def seleccionar_valor(page, selector, valor):
    page.evaluate("""([sel, val]) => {
        const el = document.querySelector(sel);
        if (!el) return;
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        if (window.jQuery) {
            try {
                window.jQuery(el).trigger('change');
            } catch(e) {}
        }
    }""", [selector, str(valor)])

def click_guardar_y_continuar(page):
    page.evaluate("""() => {
        const botones = Array.from(document.querySelectorAll('button, input[type="submit"]'));
        const btn = botones.find(b => 
            b.getAttribute('name') === 'save_&_continue' || 
            (b.innerText || '').trim().toLowerCase().includes('guardar y continuar')
        );
        if (btn) {
            btn.scrollIntoView();
            btn.click();
        }
    }""")

def main():
    print("=" * 70)
    print("CONECTANDO AL NAVEGADOR ABIERTO (Puerto 9222)...")
    print("=" * 70)

    with sync_playwright() as p:
        try:
            # Se conecta a la ventana de Chrome que ya tenés abierta
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print("\n[ERROR] No se pudo conectar a Chrome.")
            print("Asegurate de haber cerrado Chrome y abierto con:")
            print('"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\ChromePerfilRUBA"')
            return

        context = browser.contexts[0]
        # Toma la pestaña activa
        page = context.pages[0]
        print(f"[OK] Conectado exitosamente.")
        print(f"Pestaña activa: {page.url}\n")

        # ---------------------------------------------------------
        # PASO 1: CARGAR CANTIDAD DE BOMBEROS Y CREAR FILAS
        # ---------------------------------------------------------
        input(">>> Presiona ENTER para poner 5 bomberos y apretar '+ Agregar'...")
        
        page.evaluate("""() => {
            const inputCant = document.querySelector("#cantBomberos") || document.querySelector("input[style*='width:40px'], input[style*='width: 40px']");
            if (inputCant) {
                inputCant.value = "5";
                inputCant.dispatchEvent(new Event('input', { bubbles: true }));
                inputCant.dispatchEvent(new Event('change', { bubbles: true }));
            }
            const btnAdd = document.querySelector('a[onclick*="agregarBombero"]');
            if (btnAdd) btnAdd.click();
        }""")
        print("[OK] Acción enviada. Mirá la pantalla para verificar las 5 filas.")

        # ---------------------------------------------------------
        # PASO 2: COMPLETAR NOMBRES, ROLES Y ENCARGADO
        # ---------------------------------------------------------
        input("\n>>> Presiona ENTER para completar los 5 bomberos y marcar al Encargado...")

        # Apellidos reales del padrón (data/, fuera del repo); el primero es el Encargado.
        from app.core.catalogos import obtener_padron
        dotacion = [
            {"apellido": b.apellido.title(), "tarea": "1", "encargado": i == 0}
            for i, b in enumerate(obtener_padron().bomberos[:5])
        ]

        for idx, b in enumerate(dotacion, start=1):
            # Input de texto del bombero
            sel_auto = f"#autocomplete_bomberos_estructurabundle_intervencionType_bomberos_{idx}_bombero"
            campo = page.locator(sel_auto).first
            if campo.is_visible():
                campo.fill(b["apellido"])
                page.wait_for_timeout(600)
                # Seleccionar de la lista desplegable UI
                try:
                    sug = page.locator(".ui-autocomplete li:visible, .ui-menu-item:visible").first
                    if sug.is_visible():
                        sug.click()
                except Exception:
                    pass

            # Tipo de tarea (1: Interviniente)
            sel_tarea = f"#bomberos_estructurabundle_intervencionType_bomberos_{idx}_tipoTarea"
            seleccionar_valor(page, sel_tarea, b["tarea"])

            # Encargado
            if b["encargado"]:
                chk = page.locator(f"#bomberos_estructurabundle_intervencionType_bomberos_{idx}_is_encargado").first
                if chk.is_visible() and not chk.is_checked():
                    chk.check()

        print("[OK] Dotación completada.")

        # ---------------------------------------------------------
        # PASO 3: GUARDAR Y CONTINUAR A LA SIGUIENTE PANTALLA
        # ---------------------------------------------------------
        input("\n>>> Presiona ENTER para hacer clic en 'Guardar y Continuar'...")
        click_guardar_y_continuar(page)
        page.wait_for_load_state("networkidle")
        print(f"[OK] Enviado. Nueva URL actual: {page.url}")

        print("\n" + "=" * 70)
        print("Interacción terminada. Tu ventana de Chrome sigue intacta.")
        print("=" * 70)

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
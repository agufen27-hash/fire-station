import json
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "data" / "config.json"
MAPPING_PATH = BASE_DIR / "config" / "ruba_mapping.json"


def cargar_json(ruta: Path):
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo: {ruta}")
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


def seleccionar_valor(page, selector, valor):
    """Selecciona en un <select> y dispara eventos para select2/Symfony."""
    page.locator(selector).first.wait_for(state="visible", timeout=10000)
    page.evaluate(
        """([sel, val]) => {
        const el = document.querySelector(sel);
        if (!el) return;
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        if (window.jQuery) {
            try {
                window.jQuery(el).trigger('change');
                window.jQuery(el).trigger({ type: 'select2:select', params: { data: { id: val } } });
            } catch(e) {}
        }
    }""",
        [selector, str(valor)],
    )


def limpiar_required_ocultos(page):
    """Evita que campos display:none bloqueen el submit en silencio."""
    try:
        page.evaluate(
            """() => {
            document.querySelectorAll("select, input").forEach(el => {
                if (el.offsetParent === null) el.removeAttribute("required");
            });
            const c = document.querySelector("#bomberos_estructurabundle_inicializacionIncidenteType_cuerpos");
            if (c) c.removeAttribute("required");
        }"""
        )
    except Exception:
        pass


def click_guardar_y_continuar(page):
    """Hace clic de forma infalible en el botón 'Guardar y Continuar'."""
    limpiar_required_ocultos(page)
    clickeado = page.evaluate("""() => {
        const botones = Array.from(document.querySelectorAll('button, input[type="submit"]'));
        const btn = botones.find(b => 
            b.getAttribute('name') === 'save_&_continue' || 
            (b.innerText || '').trim().toLowerCase().includes('guardar y continuar')
        );
        if (btn) {
            btn.scrollIntoViewIfNeeded ? btn.scrollIntoViewIfNeeded() : btn.scrollIntoView();
            btn.click();
            return true;
        }
        return false;
    }""")

    if not clickeado:
        loc = page.locator('//button[@name="save_&_continue" or contains(normalize-space(.), "Guardar y Continuar")]').first
        loc.click()


def ejecutar_carga_simulada():
    cfg = cargar_json(CONFIG_PATH)
    mapping = cargar_json(MAPPING_PATH)

    ruba_cfg = cfg.get("ruba", {})
    usuario = ruba_cfg.get("usuario", "").strip()
    clave = ruba_cfg.get("clave", "").strip()
    url_base = ruba_cfg.get("url_base", "https://www.gestionbomberos.org").rstrip("/")

    url_login = f"{url_base}/login"
    url_agregar = f"{url_base}/estructura/incidente/agregar"

    sel_init = mapping["selectores"]["inicializacion"]
    sel_edit = mapping["selectores"]["editar_general"]
    sel_part = mapping["selectores"]["participacion"]
    sel_cond = sel_edit["condicionales"]["accidente"]

    print("=" * 70)
    print("EJECUTANDO SIMULACIÓN COMPLETA DE SALIDA: ACCIDENTE RUTA 24")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=180)
        context = browser.new_context(viewport={"width": 1366, "height": 850})
        page = context.new_page()

        # 1. LOGIN
        print(f"\n[1/6] Iniciando sesión en {url_login}...")
        page.goto(url_login, timeout=30000)
        page.wait_for_load_state("domcontentloaded")

        try:
            page.fill('input[name="_username"], input[name="username"], #username', usuario)
            page.fill('input[name="_password"], input[name="password"], #password', clave)
            page.click('button[type="submit"], input[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        if "login" in page.url.lower():
            input("\n>>> Si te pide captcha o confirmación, hazlo en pantalla y presiona ENTER...")

        print("[OK] Sesión confirmada.")

        # 2. ALTA / INICIALIZACIÓN (/agregar)
        print("\n[2/6] Completando pantalla de inicialización...")
        page.goto(url_agregar)
        page.wait_for_load_state("domcontentloaded")

        numero_parte_prueba = "99945"
        page.fill(sel_init["numero_parte"], numero_parte_prueba)

        seleccionar_valor(page, sel_init["tipo_incidente"], "1")
        page.wait_for_timeout(1500)
        seleccionar_valor(page, sel_init["categoria_incidente"], "3")

        page.uncheck(sel_init["hay_participaciones"])
        limpiar_required_ocultos(page)

        print("Guardando inicialización...")
        page.click(sel_init["btn_guardar"])
        page.wait_for_url(lambda u: "/agregar" not in u, timeout=12000)
        page.wait_for_load_state("networkidle")
        print(f"[OK] Incidente inicializado. URL: {page.url}")

        # 3. DETALLE GENERAL DEL INCIDENTE
        print("\n[3/6] Cargando Ubicación, Denunciante, Datos de Tránsito y 2 Víctimas...")

        try:
            page.fill(sel_edit["localidad_autocomplete"], "Adelia María")
            page.fill(sel_edit["otra_localidad"], "Adelia María")
        except Exception:
            pass

        page.fill(sel_edit["calle"], "Ruta 24 km 42")
        seleccionar_valor(page, sel_edit["tipo_zona"], "2")  # 2: Rural

        try:
            page.click(sel_edit["btn_buscar_puntos"])
            page.wait_for_timeout(1500)
            page.click("#combo_direccion option")
        except Exception:
            pass

        page.fill(sel_edit["nombre_solicitante"], "Juan Carlos")
        page.fill(sel_edit["apellido_solicitante"], "Morales")
        page.fill(sel_edit["telefono_solicitante"], "3585123456")
        page.fill(sel_edit["dni_solicitante"], "30111222")

        page.fill(
            sel_edit["descripcion"],
            "Colisión por alcance entre dos vehículos particulares sobre Ruta 24 km 42 con dos personas lesionadas."
        )

        page.fill(sel_edit["civiles_heridos"], "2")
        page.fill(sel_edit["civiles_fallecidos"], "0")
        page.fill(sel_edit["civiles_desaparecidos"], "0")

        try:
            seleccionar_valor(page, sel_cond["causa"], "2")  # 2: Choque
            seleccionar_valor(page, sel_cond["clima"], "3")  # 3: Soleado
        except Exception as e:
            print(f"Aviso en campos condicionales de accidente: {e}")

        url_antes_guardar = page.url
        print("Guardando panel general del incidente y esperando navegación...")
        click_guardar_y_continuar(page)

        page.wait_for_url(lambda u: u != url_antes_guardar, timeout=15000)
        page.wait_for_load_state("networkidle")
        print(f"URL tras guardar general: {page.url}")

        # 4. SUBFORMULARIO CONDICIONAL DE DAMNIFICADOS (/damnificados/)
        if "damnificados" in page.url:
            print("\n[4/6] Pantalla /damnificados/ detectada. Cargando las 2 víctimas...")

            # Víctima 1
            try:
                page.locator("#Heridos_1_nombre, input[id*='Heridos'][id*='nombre']").first.fill("Marcos")
                page.locator("#Heridos_1_apellido, input[id*='Heridos'][id*='apellido']").first.fill("Pereyra")
                page.locator("#Heridos_1_dni, input[id*='Heridos'][id*='dni']").first.fill("35111222")
                
                sel_genero_1 = page.locator("#Heridos_1_genero, select[id*='Heridos'][id*='genero']").first
                if sel_genero_1.is_visible():
                    sel_genero_1.select_option("1")  # 1: Masculino
            except Exception as e:
                print(f"Aviso llenando víctima 1: {e}")

            # Víctima 2
            try:
                inputs_nombre = page.locator("input[id*='Heridos'][id*='nombre']")
                if inputs_nombre.count() > 1:
                    inputs_nombre.nth(1).fill("Laura")
                    page.locator("input[id*='Heridos'][id*='apellido']").nth(1).fill("Gómez")
                    page.locator("input[id*='Heridos'][id*='dni']").nth(1).fill("38333444")
                    
                    sel_genero_2 = page.locator("select[id*='Heridos'][id*='genero']").nth(1)
                    if sel_genero_2.is_visible():
                        sel_genero_2.select_option("2")  # 2: Femenino
            except Exception as e:
                print(f"Aviso llenando víctima 2: {e}")

            url_antes_damnificados = page.url
            print("Guardando damnificados y esperando avance a participación...")
            click_guardar_y_continuar(page)

            page.wait_for_url(lambda u: u != url_antes_damnificados, timeout=15000)
            page.wait_for_load_state("networkidle")
            print(f"URL tras damnificados: {page.url}")

        # 5. PARTICIPACIÓN DEL CUARTEL (/participacion/)
        print("\n[5/6] Completando participación base (horarios)...")

        try:
            page.locator(sel_part["numero_parte"]).first.wait_for(state="visible", timeout=15000)
            page.fill(sel_part["numero_parte"], numero_parte_prueba)

            page.fill(sel_part["hora_llamado"], "14:30")
            page.fill(sel_part["hora_toque"], "14:32")

            fecha_servicio = "22/09/2026"

            page.evaluate(
                """([f, h]) => {
                const elF = document.querySelector("#datepicker_bomberos_estructurabundle_participacionType_fechaHoraSalida_date");
                if (elF) {
                    elF.value = f;
                    elF.dispatchEvent(new Event('input', { bubbles: true }));
                    elF.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const elH = document.querySelector("#timepicker_bomberos_estructurabundle_participacionType_fechaHoraSalida_time");
                if (elH) {
                    elH.value = h;
                    elH.dispatchEvent(new Event('input', { bubbles: true }));
                    elH.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
                [fecha_servicio, "14:35"],
            )

            page.evaluate(
                """([f, h]) => {
                const elF = document.querySelector("#datepicker_bomberos_estructurabundle_participacionType_fechaHoraLlegada_date");
                if (elF) {
                    elF.value = f;
                    elF.dispatchEvent(new Event('input', { bubbles: true }));
                    elF.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const elH = document.querySelector("#timepicker_bomberos_estructurabundle_participacionType_fechaHoraLlegada_time");
                if (elH) {
                    elH.value = h;
                    elH.dispatchEvent(new Event('input', { bubbles: true }));
                    elH.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
                [fecha_servicio, "16:00"],
            )

            # Activar intervención de vehículos
            page.check(sel_part["intervencion_vehiculos"])

            # Bomberos damnificados en No (oculta inputs de heridos en el DOM)
            seleccionar_valor(page, sel_part["hay_intervinientes_bomberos"], "no")
            page.wait_for_timeout(600)

            # Asignar 0 a campos ocultos por JS
            page.evaluate("""() => {
                const h = document.querySelector("#bomberos_estructurabundle_participacionType_cantidadBomberosHeridos");
                if (h) h.value = "0";
                const f = document.querySelector("#bomberos_estructurabundle_participacionType_cantidadBomberosFallecidos");
                if (f) f.value = "0";
                const d = document.querySelector("#bomberos_estructurabundle_participacionType_cantidadBomberosDesaparecidos");
                if (d) d.value = "0";
            }""")

            url_antes_part = page.url
            print("Presionando 'Guardar y Continuar' en participación...")
            click_guardar_y_continuar(page)

            page.wait_for_url(lambda u: u != url_antes_part, timeout=15000)
            page.wait_for_load_state("networkidle")
            print(f"[OK] Participación base guardada. URL actual: {page.url}")

        except Exception as e:
            print(f"Aviso durante la carga de participación: {e}")

        # 6. ASIGNACIÓN DE PERSONAL (INTERVENCIONES DE BOMBEROS)
        print("\n[6/6] Gestionando grilla de Intervenciones de Bomberos...")

        try:
            # Apellidos reales del padrón (data/, fuera del repo); el primero es el Encargado.
            from app.core.catalogos import obtener_padron
            dotacion = [
                {"apellido": b.apellido.title(), "tarea": "1", "encargado": i == 0}
                for i, b in enumerate(obtener_padron().bomberos[:5])
            ]

            total_deseado = len(dotacion)
            print(f"Creando {total_deseado} filas para la dotación...")

            # 1. Poner el número total (5) en #cantBomberos
            input_cant = page.locator("#cantBomberos, input[style*='width:40px'], input[style*='width: 40px']").first
            input_cant.wait_for(state="visible", timeout=10000)
            input_cant.fill(str(total_deseado))
            page.wait_for_timeout(300)

            # 2. Hacer clic en '+ Agregar' para generar las 5 filas
            btn_add = page.locator('a[onclick*="agregarBombero"], a:has-text("Agregar")').first
            btn_add.click()
            page.wait_for_timeout(1800)

            # 3. Rellenar cada fila (índices 1 a 5)
            for idx, bombero in enumerate(dotacion, start=1):
                print(f"  * Completando fila #{idx}: {bombero['apellido']} (Encargado: {bombero['encargado']})")

                # Campo predictivo de bombero
                campo_nom = page.locator(f"input[id*='intervencionType_bomberos_{idx}_bombero']").first
                campo_nom.fill(bombero["apellido"])
                page.wait_for_timeout(700)

                # Clic en sugerencia del menú flotante si aparece
                try:
                    sug = page.locator(".ui-autocomplete li:visible, .ui-menu-item:visible").first
                    if sug.is_visible():
                        sug.click()
                        page.wait_for_timeout(300)
                except Exception:
                    pass

                # Tipo de tarea (1: Interviniente, 2: Apresto)
                sel_tarea = f"select[id*='intervencionType_bomberos_{idx}_tipoTarea']"
                seleccionar_valor(page, sel_tarea, bombero["tarea"])

                # Checkbox de Encargado
                if bombero["encargado"]:
                    chk = page.locator(f"input[id*='intervencionType_bomberos_{idx}_is_encargado']").first
                    if not chk.is_checked():
                        chk.check()

            limpiar_required_ocultos(page)

            print("\nGuardando grilla de bomberos y avanzando...")
            url_antes_personal = page.url
            click_guardar_y_continuar(page)

            try:
                page.wait_for_url(lambda u: u != url_antes_personal, timeout=15000)
                page.wait_for_load_state("networkidle")
                print(f"[OK] Personal guardado con éxito. URL actual: {page.url}")
            except Exception:
                print(f"URL resultante: {page.url}")

        except Exception as e:
            print(f"Aviso en carga de personal: {e}")

        print("\n" + "=" * 70)
        print("SIMULACIÓN COMPLETADA DE PUNTA A PUNTA")
        print(f"URL final: {page.url}")
        print("=" * 70)
        print("El navegador permanece abierto para verificar el cierre del servicio.")
        input("Presiona ENTER para cerrar el navegador...")

        browser.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ejecutar_carga_simulada()
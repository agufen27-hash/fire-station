"""
Un RUBA falso servido por HTTP local (127.0.0.1, puerto libre) para probar
la automatización sin tocar el portal real. Reproduce el DOM y el
comportamiento relevados del RUBA real (logs/screenshots de la corrida del
23/09/2026):

- Login con cookie de sesión.
- /agregar: Categoría que se repuebla "por AJAX" al elegir el Tipo; al
  guardar, 302 a /editar/{id}.
- /editar/{id}: el form hace POST a la MISMA URL (novalidate). Localidad es
  un par Symfony: <input type="hidden" id="..._localidad"> + un input visible
  "autocomplete_..._localidad" (jQuery UI) que depende de la Provincia.
  "Buscar Puntos" es button[onclick="codeAddressAuto()"]. Si falta algo
  obligatorio el servidor RE-RENDERIZA la misma URL con el error; si está
  todo, 302 a /damnificados/{id} o, si no hay víctimas, directo a
  /participacion/{id} (RUBA saltea Damnificados).
- Participación / bomberos / vehículos con date/timepickers readonly y
  filas que se agregan por botón.

Cada POST queda registrado en `FakeRuba.envios[path]`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

P_INI = "bomberos_estructurabundle_inicializacionIncidenteType"
P_GEN = "bomberos_estructurabundle_incidenteIncendioType"
P_PART = "bomberos_estructurabundle_participacionType"
P_BOM = "bomberos_estructurabundle_intervencionType_bomberos"
P_VEH = "bomberos_estructurabundle_intervencionType_vehiculos"

JS_COMUN = r"""
<script>
function autocompletar(input, opciones, oculto) {
  input.addEventListener('input', () => {
    document.querySelectorAll('ul.ui-autocomplete').forEach(u => u.remove());
    const q = input.value.toUpperCase();
    if (q.length < 2) return;
    setTimeout(() => {                       // "AJAX"
      const ul = document.createElement('ul');
      ul.className = 'ui-autocomplete ui-menu';
      opciones.filter(o => o.texto.toUpperCase().includes(q)).forEach(o => {
        const li = document.createElement('li');
        li.className = 'ui-menu-item';
        li.textContent = o.texto;
        li.onclick = () => { input.value = o.texto; if (oculto) oculto.value = o.id; ul.remove(); };
        ul.appendChild(li);
      });
      if (ul.children.length) document.body.appendChild(ul);
    }, 150);
  });
}
</script>
"""


def _pagina(cuerpo: str, extra_js: str = "") -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>{JS_COMUN}</head>"
            f"<body>{cuerpo}{extra_js}</body></html>")


class FakeRuba:
    ID_INCIDENTE = "555"
    ID_PARTICIPACION = "77"

    def __init__(self, personas: List[Tuple[int, str]], localidades: Optional[List[Tuple[int, str]]] = None,
                 categorias: Optional[Dict[str, List[str]]] = None) -> None:
        self.personas = [{"id": i, "texto": t} for i, t in personas]
        self.localidades = [{"id": i, "texto": t} for i, t in (localidades or [
            (1001, "ADELIA MARIA"), (1002, "ADELIA (San Luis)")])]
        self.categorias = categorias or {"1": ["3"], "3": ["15", "19", "21", "23"]}
        self.envios: Dict[str, Dict[str, List[str]]] = {}
        self.logins = 0
        # False = "Buscar Puntos" sin resultados (zona rural): como el real, no fija el punto.
        self.geocoder_encuentra = True
        # Cuántos GET a /editar/ responden con la página 500 de Symfony (como el real el 24/09).
        self.errores_500_editar = 0
        self.inicializaciones = 0
        self._servidor = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._hilo = threading.Thread(target=self._servidor.serve_forever, daemon=True)

    # -- ciclo de vida --------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._servidor.server_address[1]}"

    @property
    def url_login(self) -> str:
        return f"{self.base}/login"

    @property
    def url_incidentes(self) -> str:
        return f"{self.base}/estructura/incidente/"

    def iniciar(self) -> "FakeRuba":
        self._hilo.start()
        return self

    def detener(self) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()

    def _handler(self):
        sitio = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silencio en los tests
                pass

            def _responder(self, metodo: str) -> None:
                datos = {}
                if metodo == "POST":
                    largo = int(self.headers.get("Content-Length", 0))
                    datos = parse_qs(self.rfile.read(largo).decode("utf-8"), keep_blank_values=True)
                estado, encabezados, cuerpo = sitio._atender(metodo, urlparse(self.path).path, datos,
                                                             self.headers.get("Cookie", ""))
                self.send_response(estado)
                for clave, valor in encabezados.items():
                    self.send_header(clave, valor)
                cuerpo_bytes = cuerpo.encode("utf-8")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo_bytes)))
                self.end_headers()
                self.wfile.write(cuerpo_bytes)

            def do_GET(self):  # noqa: N802
                self._responder("GET")

            def do_POST(self):  # noqa: N802
                self._responder("POST")

        return Handler

    # -- ruteo -------------------------------------------------------------------

    def _atender(self, metodo: str, path: str, datos: Dict[str, List[str]], cookie: str):
        def redirigir(destino: str):
            return 302, {"Location": destino}, ""

        if metodo == "POST":
            self.envios[path] = datos

        if path == "/login":
            return 200, {}, self._login()
        if path == "/login_check":
            self.logins += 1
            return 302, {"Location": "/estructura/incidente/", "Set-Cookie": "sesion=ok; Path=/"}, ""
        if "sesion=ok" not in cookie:
            return redirigir("/login")
        if path == "/estructura/incidente/":
            return 200, {}, _pagina("<p id='listado'>Incidentes</p>")
        if path == "/estructura/incidente/agregar":
            if metodo == "POST":
                self.inicializaciones += 1
                return redirigir(f"/estructura/incidente/editar/{self.ID_INCIDENTE}")
            return 200, {}, self._agregar()
        if path.startswith("/estructura/incidente/editar/"):
            if metodo == "POST":
                errores = self._validar_general(datos)
                if errores:
                    return 200, {}, self._editar(errores)  # misma URL, con errores
                hay_victimas = any(int((datos.get(c) or ["0"])[0] or 0) for c in ("heridos", "fallecidos"))
                destino = "damnificados" if hay_victimas else "participacion"
                return redirigir(f"/estructura/incidente/{destino}/{self.ID_INCIDENTE}")
            if self.errores_500_editar > 0:
                self.errores_500_editar -= 1
                return 500, {}, ("<!DOCTYPE html><html><head><title>An Error Occurred: Internal Server Error</title>"
                                 "</head><body><h1>Oops! An Error Occurred</h1>"
                                 '<h2>The server returned a "500 Internal Server Error".</h2></body></html>')
            return 200, {}, self._editar()
        if path.startswith("/estructura/incidente/damnificados/"):
            if metodo == "POST":
                return redirigir(f"/estructura/incidente/participacion/{self.ID_INCIDENTE}")
            return 200, {}, self._damnificados()
        if path.startswith("/estructura/incidente/participacion/intervenciones/bomberos/"):
            if metodo == "POST":
                return redirigir(f"/estructura/incidente/participacion/intervenciones/vehiculos/{self.ID_PARTICIPACION}")
            return 200, {}, self._bomberos()
        if path.startswith("/estructura/incidente/participacion/intervenciones/vehiculos/"):
            if metodo == "POST":
                return redirigir(f"/estructura/incidente/{self.ID_INCIDENTE}")
            return 200, {}, self._vehiculos()
        if path.startswith("/estructura/incidente/participacion/"):
            if metodo == "POST":
                return redirigir(f"/estructura/incidente/participacion/intervenciones/bomberos/{self.ID_PARTICIPACION}")
            return 200, {}, self._participacion()
        return 200, {}, _pagina("<h1 id='fin'>Incidente guardado</h1>")

    @staticmethod
    def _validar_general(datos: Dict[str, List[str]]) -> List[str]:
        errores = []
        if not (datos.get("localidad") or [""])[0] and not (datos.get("otraLocalidad") or [""])[0]:
            errores.append("Debe indicar la localidad")
        if not (datos.get("calle") or [""])[0]:
            errores.append("La calle es obligatoria")
        if not (datos.get("descripcion") or [""])[0]:
            errores.append("La descripción es obligatoria")
        # Como el RUBA real (captura del 24/09): "si" con los tres contadores en 0.
        contadores = [int((datos.get(c) or ["0"])[0] or 0) for c in ("heridos", "fallecidos", "desaparecidos")]
        if (datos.get("hayIntervinientesPersonas") or [""])[0] == "si" and not any(contadores):
            errores.append("Debe indicar alguna persona")
        if not (datos.get("latitud") or [""])[0] or not (datos.get("longitud") or [""])[0]:
            errores.append("Haz clic en 'Buscar puntos' para obtener una lista de posibilidades")
        return errores

    # -- pantallas ----------------------------------------------------------------

    def _login(self) -> str:
        return _pagina("""
<form method="post" action="/login_check">
  <input name="_username"><input name="_password" type="password">
  <button type="submit">Ingresar</button>
</form>""")

    def _agregar(self) -> str:
        return _pagina(f"""
<form method="post" action="/estructura/incidente/agregar" novalidate>
  <input id="{P_INI}_numeroParte" name="numeroParte" required>
  <select id="{P_INI}_tipoIncidente" name="tipo" required>
    <option value=""></option><option value="1">Accidentes</option><option value="3">Incendios</option>
  </select>
  <select id="{P_INI}_categoriaIncidente" name="categoria" required><option value=""></option></select>
  <input type="checkbox" id="{P_INI}_hayParticipciones" name="hayParticipaciones" value="1" checked>
  <select id="{P_INI}_cuerpos" name="cuerpos" multiple required style="display:none"></select>
  <button type="submit" class="js-submit">Guardar</button>
</form>""", f"""<script>
const CATEGORIAS = {json.dumps(self.categorias)};
document.getElementById('{P_INI}_tipoIncidente').addEventListener('change', (e) => {{
  const combo = document.getElementById('{P_INI}_categoriaIncidente');
  setTimeout(() => {{
    combo.innerHTML = '<option value=""></option>' +
      (CATEGORIAS[e.target.value] || []).map(c => `<option value="${{c}}">${{c}}</option>`).join('');
  }}, 250);
}});
</script>""")

    def _editar(self, errores: Optional[List[str]] = None) -> str:
        alerta = "".join(f'<div class="alert alert-error">{e}</div>' for e in (errores or []))
        return _pagina(f"""
<form method="post" action="/estructura/incidente/editar/{self.ID_INCIDENTE}" novalidate>
  {alerta}
  <select id="{P_GEN}_provincia" name="provincia" required>
    <option value="">Seleccionar</option><option value="6" selected>Córdoba</option><option value="19">San Luis</option>
  </select>
  <input type="hidden" id="{P_GEN}_localidad" name="localidad" required class="js-nested-autocomplete">
  <input id="autocomplete_{P_GEN}_localidad" type="text" class="js-nested-autocomplete ui-autocomplete-input" required
         data-parentcontrolid="{P_GEN}_provincia" autocomplete="off">
  <input type="text" id="{P_GEN}_otraLocalidad" name="otraLocalidad" required>
  <input type="radio" name="optionsUbicacion" class="optionsUbicacion" value="option_direccion">
  <input type="text" id="{P_GEN}_ubicacion_calle" name="calle">
  <input type="text" id="{P_GEN}_ubicacion_altura" name="altura">
  <select id="{P_GEN}_tipoZona" name="tipoZona" required>
    <option value="" selected>Seleccionar</option><option value="2">Rural</option><option value="1">Urbana</option>
  </select>
  <button class="btn btn-primary" type="button" onclick="codeAddressAuto()">Buscar Puntos</button>
  <select id="combo_direccion" name="punto"></select>
  <span class="help-inline">Haz clic en 'Buscar puntos' para obtener una lista de posibilidades.</span>
  <div id="ubicacionLat" style="display:none;">
    <input type="text" id="{P_GEN}_ubicacion_latitud" name="latitud" value="">
    <input type="text" id="{P_GEN}_ubicacion_longitud" name="longitud" value="">
  </div>
  <input type="text" id="{P_GEN}_nombreSolicitante" name="nombreSolicitante" required>
  <input type="text" id="{P_GEN}_apellidoSolicitante" name="apellidoSolicitante" required>
  <input type="text" id="{P_GEN}_telefonoSolicitante" name="telefonoSolicitante" required>
  <input type="text" id="{P_GEN}_dniSolicitante" name="dniSolicitante" required>
  <select id="{P_GEN}_hayIntervinientesPersonas" name="hayIntervinientesPersonas" required class="setearCeros">
    <option value="">Seleccionar</option><option value="si" selected>Si</option><option value="no">No</option>
  </select>
  <textarea id="{P_GEN}_descripcion" name="descripcion" required></textarea>
  <input type="text" id="{P_GEN}_cantidadCivilesHeridos" name="heridos" required>
  <input type="text" id="{P_GEN}_cantidadCivilesFallecidos" name="fallecidos" required>
  <input type="text" id="{P_GEN}_cantidadCivilesDesaparecidos" name="desaparecidos" required>
  <input type="text" id="{P_GEN}_companiaSeguro" name="compania" required>
  <input type="text" id="{P_GEN}_numeroPoliza" name="poliza" required>
  <select id="{P_GEN}_tipoLugarForestal" name="tipoLugarForestal"><option value=""></option><option value="1">Campo</option><option value="7">Pastizal</option></select>
  <select id="{P_GEN}_evacuacion" name="evacuacion"><option value=""></option><option value="3">Hectáreas</option></select>
  <input id="{P_GEN}_superficieEvacuada" name="superficie">
  <input type="text" id="{P_GEN}_otroTipoLugarForestal" name="otroTipoLugarForestal" required>
  <input type="text" id="datepicker_{P_GEN}_fechaVencimientoSeguro" name="fechaVencimientoSeguro" required>
  <select id="{P_GEN}_causaIncendio" name="causa"><option value=""></option><option value="1">Negligencia</option><option value="4">Desconocida</option></select>
  <button class="btn btn-success" name="save_&amp;_continue" type="submit">Guardar y continuar</button>
</form>""", """<script>
autocompletar(document.getElementById('autocomplete_%(g)s_localidad'), %(loc)s,
              document.getElementById('%(g)s_localidad'));
function codeAddressAuto() {
  if (!%(encuentra)s) return;  // el real hace alert('No se han encontrado resultados...')
  setTimeout(() => { document.getElementById('combo_direccion').innerHTML =
    "<option value='0'>Belgrano 58, Adelia María</option>";
    document.getElementById('%(g)s_ubicacion_latitud').value = '-33.6305911';
    document.getElementById('%(g)s_ubicacion_longitud').value = '-64.0211957'; }, 200);
}
</script>""" % {"g": P_GEN, "loc": json.dumps(self.localidades),
   "encuentra": "true" if self.geocoder_encuentra else "false"})

    def _damnificados(self) -> str:
        return _pagina(f"""
<form method="post" action="/estructura/incidente/damnificados/{self.ID_INCIDENTE}" novalidate>
  <input id="Heridos_0_nombre" name="h0_nombre"><input id="Heridos_0_apellido" name="h0_apellido">
  <input id="Heridos_0_dni" name="h0_dni">
  <select id="Heridos_0_genero" name="h0_genero"><option value=""></option><option value="1">F</option><option value="2">M</option></select>
  <button class="btn btn-success" name="save_&amp;_continue" type="submit">Guardar y continuar</button>
</form>""")

    def _participacion(self) -> str:
        campos_texto = "".join(
            f'<input id="{P_PART}_{c}" name="{c}">' for c in (
                "numeroParte", "cantidadBomberosHeridos", "cantidadBomberosFallecidos",
                "cantidadBomberosDesaparecidos")
        )
        # Bootstrap date/timepicker: readonly, el valor solo entra por el plugin (o por JS).
        pickers = "".join(
            f'<input id="{prefijo}_{P_PART}_{c}" name="{c}" readonly class="{prefijo}">' for prefijo, c in (
                ("timepicker", "horaLlamado"), ("timepicker", "horaToque"),
                ("timepicker", "fechaHoraSalida_time"), ("datepicker", "fechaHoraSalida_date"),
                ("timepicker", "fechaHoraLlegada_time"), ("datepicker", "fechaHoraLlegada_date"))
        )
        checks = "".join(
            f'<input type="checkbox" id="{P_PART}_{c}" name="{c}" value="1">'
            for c in ("intervencionVehiculo", "intervencionComision", "hayIntervinientesBomberos")
        )
        return _pagina(f"""
<form method="post" action="/estructura/incidente/participacion/{self.ID_INCIDENTE}" novalidate>
  {campos_texto}{pickers}{checks}
  <div class="form-actions"><button type="submit" class="btn-success">Guardar</button></div>
  <button class="btn btn-success" name="save_&amp;_continue" type="submit">Guardar y continuar</button>
</form>""")

    def _bomberos(self) -> str:
        return _pagina(f"""
<form method="post" action="/estructura/incidente/participacion/intervenciones/bomberos/{self.ID_PARTICIPACION}" novalidate>
  <input id="cantBomberos" name="cantBomberos" type="number">
  <a href="#" onclick="agregarBombero(); return false;">Agregar bomberos</a>
  <div id="filas"></div>
  <button class="btn btn-success" name="save_&amp;_continue" type="submit">Guardar y continuar</button>
</form>""", f"""<script>
const PERSONAS = {json.dumps(self.personas)};
function agregarBombero() {{
  const filas = document.getElementById('filas');
  const n = parseInt(document.getElementById('cantBomberos').value || '0');
  for (let i = filas.children.length; i < n; i++) {{
    const d = document.createElement('div');
    d.innerHTML = `
      <input type="hidden" id="{P_BOM}_${{i}}_bombero" name="b${{i}}_bombero_id">
      <input id="autocomplete_{P_BOM}_${{i}}_bombero" name="b${{i}}_bombero">
      <input id="datepicker_{P_BOM}_${{i}}_fechaHoraInicio_date" name="b${{i}}_fi" readonly>
      <input id="timepicker_{P_BOM}_${{i}}_fechaHoraInicio_time" name="b${{i}}_hi" readonly>
      <input id="datepicker_{P_BOM}_${{i}}_fechaHoraFin_date" name="b${{i}}_ff" readonly>
      <input id="timepicker_{P_BOM}_${{i}}_fechaHoraFin_time" name="b${{i}}_hf" readonly>
      <select id="{P_BOM}_${{i}}_tipoTarea" name="b${{i}}_tarea">
        <option value="1">Interviniente</option><option value="2">Apresto</option></select>
      <input type="checkbox" id="{P_BOM}_${{i}}_is_encargado" name="b${{i}}_encargado" value="1">`;
    filas.appendChild(d);
    autocompletar(d.querySelector('input[id^=autocomplete]'), PERSONAS, d.querySelector('input[type=hidden]'));
  }}
}}
</script>""")

    def _vehiculos(self) -> str:
        opciones = "".join(f'<option value="{v}">{v}</option>' for v in (
            "4326", "4327", "5629", "9181", "9182", "9906", "11699", "11706", "12416", "14795"))
        return _pagina(f"""
<form method="post" action="/estructura/incidente/participacion/intervenciones/vehiculos/{self.ID_PARTICIPACION}" novalidate>
  <a href="#" onclick="agregarVehiculo(); return false;">Agregar vehículo</a>
  <div id="filas"></div>
  <button type="submit" class="js-submit">Guardar cambios</button>
</form>""", f"""<script>
const PERSONAS = {json.dumps(self.personas)};
function agregarVehiculo() {{
  const filas = document.getElementById('filas');
  const i = filas.children.length;
  const d = document.createElement('div');
  d.innerHTML = `
    <select id="{P_VEH}_${{i}}_vehiculo" name="v${{i}}_vehiculo"><option value=""></option>{opciones}</select>
    <input type="hidden" id="{P_VEH}_${{i}}_chofer" name="v${{i}}_chofer_id">
    <input id="autocomplete_{P_VEH}_${{i}}_chofer" name="v${{i}}_chofer">
    <input id="datepicker_{P_VEH}_${{i}}_fechaHoraSalida_date" name="v${{i}}_fs" readonly>
    <input id="timepicker_{P_VEH}_${{i}}_fechaHoraSalida_time" name="v${{i}}_hs" readonly>
    <input id="datepicker_{P_VEH}_${{i}}_fechaHoraLlegada_date" name="v${{i}}_fl" readonly>
    <input id="timepicker_{P_VEH}_${{i}}_fechaHoraLlegada_time" name="v${{i}}_hl" readonly>`;
  filas.appendChild(d);
  autocompletar(d.querySelector('input[id^=autocomplete]'), PERSONAS, d.querySelector('input[type=hidden]'));
}}
</script>""")

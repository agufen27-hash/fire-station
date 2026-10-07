"""
Generador de planillas físicas de guardia (PCS.xlsx / PCD2.xlsx) a partir de
un `Incidente` de Fire Station, escribiendo con openpyxl sobre la plantilla
oficial (solo se pisa el `.value` de cada celda; estilos, bordes, logo y
formato de impresión quedan intactos).

Mapeo celda por celda (Fase 15, definido por el cuerpo):

PCS  Cabecera   N° parte C6 · Día J6 · Fecha T6
     Alerta     Denunciante D9 · Hora llamado W9 · Domicilio D10 · Medio de
                contacto D11 · DNI D12 · Recibió D13 · Alarma D14 · Hora alarma Q14
     Siniestro  Tipo D17 ("Tipo - Subtipo") · Localidad D19 · Calle D20 · Zona D21
     Operativa  Autorizó A23 · Descripción A26 · Operador 1 F31 · Operador 2 F32 ·
                Reserva (apresto) B35:B47 y O35:O47 (13 + 13)
     Resumen    Total en cuartel (apresto) A51 · Total en servicio E51 (ancla de
                E51:H51) · Total general I51 · Confeccionó (validado con PIN) N51

PCD2 Página S6 · Total páginas V6 · N° parte C6. Dos dotaciones por página
     (superior 1, 3, 5... / inferior 2, 4, 6...); con más de 2 dotaciones se
     agregan hojas copiando la plantilla (logo y área de impresión incluidos).
     Superior: Dot A9 · Unidad C9 · Salida I9/L9 · Arribo O9 · Llegada R9/U9 ·
               Jefe B11 · Grado O11 · Chofer E14 · Embarcados E15:E24 · Firma P26
     Inferior: Dot A32 · Unidad C32 · Salida I32/L32 · Arribo O32 · Llegada R32/U32 ·
               Jefe B34 · Grado O34 · Chofer E37 · Embarcados E38:E47 · Firma P49

TIPOGRAFÍA: todo dato escrito en las planillas Excel va en Arial 10 pt
(la de las plantillas oficiales, definida en app/reports/tipografia.py y
compartida con el Informe PDF); texto a la izquierda y números / fechas /
horas centrados (tipografia.es_numerico decide qué es numérico). Los
totales de la fila 51 de la PCS van además en negrita.

PIE Y ENCABEZADO DE PÁGINA: el pie izquierdo lleva quién confeccionó la
planilla (validado con PIN al guardarla) y la PCD2 lleva en el encabezado
"Total Efectivos: X" (personas distintas en todas las dotaciones).

VERIFICACIÓN DE PLANTILLA: antes de escribir cada dato se comprueba que la
celda destino sea escribible en la plantilla en uso. Si cae dentro de un
rango combinado (no es su celda ancla) o ya trae un rótulo impreso, NO se
escribe y queda una advertencia ("D17 está dentro de C17:F17 'Colision'").
Así una plantilla que todavía no coincide con el mapeo nunca se rompe, y en
cuanto se guarda la plantilla rediseñada todo se completa solo. Los
placeholders de la plantilla ('/', ':', vacíos) sí se sobrescriben.
"""

from __future__ import annotations

import io
import math
import re
import sys
from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.drawing.image import Image as ImagenExcel
from openpyxl.styles import Alignment, Color, Font
from openpyxl.worksheet.worksheet import Worksheet

from app.db import get_session
from app.models import MEDIOS_CONTACTO, FuncionBase, Incidente, RolDotacion
from app.paths import get_resource_path, get_writable_dir
from app.reports.tipografia import FUENTE_PLANILLAS, TAMANO_PT, es_numerico

# Las plantillas son de solo lectura (viajan embebidas en el bundle); las
# planillas completadas son escribibles y viven al lado del .exe.
TEMPLATES_DIR = get_resource_path("templates")
OUTPUT_DIR = get_writable_dir("output") / "planillas"

PCS_TEMPLATE_NOMBRE = "PCS.xlsx"
PCD2_TEMPLATE_NOMBRE = "PCD2.xlsx"

# Tipografía de todo dato escrito en las planillas Excel (la de las plantillas):
# la misma que usa el Informe PDF (app/reports/tipografia.py).
FUENTE_EXCEL = FUENTE_PLANILLAS
TAMANO_EXCEL_PT = TAMANO_PT

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# ---------------------------------------------------------------------------
# Mapeo de celdas
# ---------------------------------------------------------------------------

MAPEO_PCS: Dict[str, Any] = {
    "numero_parte": "C6", "dia": "J6", "fecha": "T6",
    "denunciante": "D9", "hora_llamado": "W9", "domicilio": "D10", "contacto": "D11", "dni": "D12",
    "recibio": "D13", "alarma": "D14", "hora_alarma": "Q14",
    "tipo_siniestro": "D17", "localidad": "D19", "calle": "D20", "zona": "D21",
    "autorizo": "A23", "descripcion": "A26", "operador_1": "F31", "operador_2": "F32",
    "reserva": [f"B{f}" for f in range(35, 48)] + [f"O{f}" for f in range(35, 48)],  # 13 + 13
    # Fila 51: "Total en Servicio" es el rango E51:H51, su celda escribible es E51.
    "total_apresto": "A51", "total_servicio": "E51", "total_general": "I51", "confecciono": "N51",
}

MAPEO_PCD2: Dict[str, Any] = {
    "numero_parte": "C6", "pagina": "S6", "total_paginas": "V6",
    "bloques": [
        {"dotacion": "A9", "unidad": "C9", "fecha_salida": "I9", "hora_salida": "L9", "arribo": "O9",
         "fecha_llegada": "R9", "hora_llegada": "U9", "jefe": "B11", "grado": "O11", "chofer": "E14",
         "embarcados": [f"E{f}" for f in range(15, 25)], "firma": "P26"},
        {"dotacion": "A32", "unidad": "C32", "fecha_salida": "I32", "hora_salida": "L32", "arribo": "O32",
         "fecha_llegada": "R32", "hora_llegada": "U32", "jefe": "B34", "grado": "O34", "chofer": "E37",
         "embarcados": [f"E{f}" for f in range(38, 48)], "firma": "P49"},
    ],
}
DOTACIONES_POR_PAGINA = len(MAPEO_PCD2["bloques"])
MAX_EMBARCADOS_PCD2 = len(MAPEO_PCD2["bloques"][0]["embarcados"])

# "Accidentes" -> "Accidente" para el formato corrido "Accidente - Tránsito".
TIPO_SINGULAR = {
    "Accidentes": "Accidente", "Incendios": "Incendio", "Rescates": "Rescate",
    "Factores Climáticos": "Factor Climático", "Servicios Especiales": "Servicio Especial",
}

_PLACEHOLDER = re.compile(r"^[\s/:.\-]*$")


# ---------------------------------------------------------------------------
# Escritura verificada
# ---------------------------------------------------------------------------

class EscritorVerificado:
    """Escribe solo en celdas que la plantilla deja escribir; lo demás lo
    reporta como advertencia (sin romper la plantilla)."""

    def __init__(self, hoja: Worksheet, planilla: str) -> None:
        self.hoja = hoja
        self.planilla = planilla
        self.advertencias: List[str] = []

    def _problema(self, coordenada: str) -> Optional[str]:
        celda = self.hoja[coordenada]
        if isinstance(celda, MergedCell):
            rango = next(str(r) for r in self.hoja.merged_cells.ranges if coordenada in r)
            rotulo = self.hoja[rango.split(":")[0]].value
            return f"está dentro del rango combinado {rango}" + (f" ('{str(rotulo).strip()}')" if rotulo else "")
        valor = celda.value
        if valor is not None and not _PLACEHOLDER.match(str(valor)):
            return f"la plantilla trae el rótulo '{str(valor).strip()}'"
        return None

    def escribir(self, coordenada: Optional[str], valor: Any, campo: str, negrita: bool = False) -> bool:
        if not coordenada or valor in (None, ""):
            return False
        problema = self._problema(coordenada)
        if problema:
            self.advertencias.append(f"{self.planilla} {coordenada} ({campo}): {problema} — no se escribió.")
            return False
        celda = self.hoja[coordenada]
        celda.value = valor
        _ajustar_formato(celda, valor, negrita)
        return True

    def escribir_lista(self, coordenadas: List[str], valores: List[str], campo: str) -> None:
        for coordenada, valor in zip(coordenadas, valores):
            self.escribir(coordenada, valor, campo)
        if len(valores) > len(coordenadas):
            self.advertencias.append(
                f"{self.planilla} ({campo}): hay {len(valores)} y la planilla admite {len(coordenadas)}; "
                f"no se imprimieron: {', '.join(valores[len(coordenadas):])}."
            )


def _mismo_color(a, b) -> bool:
    if a is None or b is None or a.type != b.type:
        return False
    if a.type == "theme":
        return a.theme == b.theme and (a.tint or 0) == (b.tint or 0)
    return a.type == "rgb" and a.rgb == b.rgb


def _ajustar_formato(celda, valor: Any = None, negrita: bool = False) -> None:
    """Solo en la planilla generada (la plantilla no se toca): tipografía
    unificada (Arial 10 pt; números/fechas/horas centrados, texto a la
    izquierda; `negrita` para los totales), y el dato nunca queda invisible (letra del mismo color que
    el relleno) ni cortado (si no entra en la celda, Excel achica la letra
    en vez de recortarla; los textos largos que ya ajustaban siguen igual)."""
    fuente = copy(celda.font)
    fuente.name = FUENTE_EXCEL
    fuente.sz = TAMANO_EXCEL_PT
    if negrita:
        fuente.b = True
    if celda.fill.fill_type == "solid" and _mismo_color(celda.font.color, celda.fill.fgColor):
        fuente.color = Color(rgb="FF000000")
    celda.font = fuente
    a = celda.alignment
    horizontal = "center" if es_numerico(valor if valor is not None else celda.value) else "left"
    celda.alignment = Alignment(horizontal=horizontal, vertical=a.vertical or "center", indent=a.indent,
                                text_rotation=a.text_rotation, wrap_text=a.wrap_text,
                                shrink_to_fit=not a.wrap_text)


ANCHO_MAX_FIRMA_PX = 200
ALTO_MAX_FIRMA_PX = 42


def _insertar_firma(escritor: EscritorVerificado, coordenada: str, ruta_firma: Optional[str], aclaracion: str) -> None:
    """Estampa la firma validada por PIN en el recuadro y su aclaración
    (quién y cuándo) al pie de la misma celda combinada."""
    if not ruta_firma or not Path(ruta_firma).exists():
        return
    if not escritor.escribir(coordenada, aclaracion, "firma"):
        return
    celda = escritor.hoja[coordenada]
    celda.alignment = Alignment(horizontal="center", vertical="bottom", wrap_text=True)
    celda.font = Font(name=FUENTE_EXCEL, size=TAMANO_EXCEL_PT, italic=True)
    imagen = ImagenExcel(ruta_firma)
    escala = min(ANCHO_MAX_FIRMA_PX / imagen.width, ALTO_MAX_FIRMA_PX / imagen.height, 1.0)
    imagen.width, imagen.height = int(imagen.width * escala), int(imagen.height * escala)
    escritor.hoja.add_image(imagen, coordenada)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _ubicar_plantilla(nombre_archivo: str) -> Path:
    candidato = TEMPLATES_DIR / nombre_archivo
    if candidato.exists():
        return candidato
    raise FileNotFoundError(f"No se encontró la plantilla '{nombre_archivo}'. Copiala a '{TEMPLATES_DIR}'.")


def _limpiar_para_archivo(texto: str) -> str:
    limpio = texto.replace("/", "-").replace("\\", "-").strip()
    for caracter in '<>:"|?*':
        limpio = limpio.replace(caracter, "")
    return limpio.replace(" ", "_")


def _formatear_fecha(valor: Optional[date]) -> str:
    return valor.strftime("%d/%m/%Y") if valor else ""


def _formatear_hora(valor: Optional[time]) -> str:
    return valor.strftime("%H:%M") if valor else ""


def _ruta_salida(incidente: Incidente, sufijo_archivo: str) -> Path:
    anio = incidente.fecha.year if incidente.fecha else datetime.now().year
    tipo_nombre = _limpiar_para_archivo(incidente.tipo.nombre if incidente.tipo else "SinTipo")
    carpeta = OUTPUT_DIR / str(anio)
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"{_limpiar_para_archivo(incidente.numero_parte)}_{tipo_nombre}_{sufijo_archivo}.xlsx"


def _nombre(persona) -> Optional[str]:
    return persona.nombre_completo() if persona else None


def _tipo_siniestro(incidente: Incidente) -> Optional[str]:
    if not incidente.tipo:
        return None
    tipo = TIPO_SINGULAR.get(incidente.tipo.nombre, incidente.tipo.nombre)
    return f"{tipo} - {incidente.categoria.nombre}" if incidente.categoria else tipo


def _medio_contacto(incidente: Incidente) -> Optional[str]:
    medio = MEDIOS_CONTACTO.get(incidente.via_comunicacion, incidente.via_comunicacion)
    if medio and incidente.contacto_detalle:
        return f"{medio}: {incidente.contacto_detalle}"
    return medio or incidente.contacto_detalle


def _zona_y_coordenadas(incidente: Incidente) -> Optional[str]:
    partes = [incidente.zona.capitalize()] if incidente.zona else []
    referencia = incidente.referencia_ubicacion or ""
    if incidente.latitud is not None and incidente.longitud is not None:
        coordenadas = f"{incidente.latitud:.5f}, {incidente.longitud:.5f}"
        # El mapa ya escribe estas coordenadas al principio de la referencia.
        if not referencia.startswith(coordenadas):
            partes.append(coordenadas)
    if referencia:
        partes.append(referencia)
    return " · ".join(partes) or None


def _agrupar_dotaciones(incidente: Incidente) -> List[Dict[str, Any]]:
    """Una entrada por dotación (SalidaUnidad, en orden): móvil, chofer, Jefe
    y su grado, embarcados, horarios y firma. Compatible con incidentes
    viejos (chofer como fila CHOFER). También la usa el informe PDF."""
    dotaciones: List[Dict[str, Any]] = []
    for numero, su in enumerate(incidente.salidas_unidad, start=1):
        filas = sorted(su.dotacion, key=lambda f: f.id)
        jefe = next((f.personal for f in filas if f.rol == RolDotacion.A_CARGO.value), None)
        chofer = su.chofer or next((f.personal for f in filas if f.rol == RolDotacion.CHOFER.value), None)
        dotaciones.append({
            "numero": numero,
            "movil": su.movil.nombre_identificador if su.movil else None,
            "chofer": _nombre(chofer),
            "a_cargo": _nombre(jefe),
            "grado": su.jefe_grado or (jefe.jerarquia if jefe else None),
            "bomberos": [f.personal.nombre_completo() for f in filas if f.rol == RolDotacion.BOMBERO.value],
            "fecha": su.fecha_salida or incidente.fecha_salida or incidente.fecha,
            "fecha_regreso": su.fecha_llegada or incidente.fecha_llegada,
            "hora_salida": su.hora_salida, "hora_arribo": su.hora_arribo, "hora_regreso": su.hora_regreso,
            "ruta_firma": su.ruta_firma_auditoria,
            "firma_validada_en": su.firma_validada_en,
        })
    return dotaciones


def total_efectivos(dotaciones: List[Dict[str, Any]]) -> int:
    """Personas distintas en todas las dotaciones (Jefe, chofer y embarcados)."""
    personas = set()
    for d in dotaciones:
        personas.update(x for x in (d.get("a_cargo"), d.get("chofer"), *d.get("bomberos", [])) if x)
    return len(personas)


def texto_autor(incidente: Incidente) -> Optional[str]:
    """"Confeccionó: PÉREZ, Juan (validado con PIN) el 12/03/2026 08:45"."""
    if incidente.confecciono is None:
        return None
    cuando = f" el {incidente.confeccionado_en:%d/%m/%Y %H:%M}" if incidente.confeccionado_en else ""
    return f"Confeccionó: {incidente.confecciono.nombre_completo()} (validado con PIN){cuando}"


def _pie_y_encabezado(wb, autor: Optional[str], encabezado: Optional[str] = None) -> None:
    """Pie izquierdo = autor; encabezado derecho = resumen (en todas las hojas)."""
    for hoja in wb.worksheets:
        for item, texto in ((hoja.oddFooter.left, autor), (hoja.oddHeader.right, encabezado)):
            if texto:
                item.text = texto.replace("&", "&&")  # & es código de formato en Excel
                item.font = f"{FUENTE_EXCEL},Regular"
                item.size = TAMANO_EXCEL_PT


def _una_hoja_por_pagina(wb) -> None:
    """Cada hoja del libro se imprime en UNA hoja A4 (la plantilla a escala
    100% no entra y Excel la partía en dos páginas del PDF)."""
    for hoja in wb.worksheets:
        hoja.sheet_properties.pageSetUpPr.fitToPage = True
        hoja.page_setup.fitToWidth = 1
        hoja.page_setup.fitToHeight = 1


def _sin_repetir(textos: List[str]) -> List[str]:
    return list(dict.fromkeys(textos))


@dataclass
class ResultadoPlanilla:
    """Planilla generada + advertencias de celdas que la plantilla no dejó escribir."""
    ruta: Path
    advertencias: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return str(self.ruta)


def resumen_advertencias(nombre_planilla: str, resultado: Any) -> Optional[str]:
    """Una línea para el aviso al usuario: qué celdas no coinciden con la
    plantilla en uso (el detalle completo va a la consola)."""
    advertencias = getattr(resultado, "advertencias", None)
    if not advertencias:
        return None
    for texto in advertencias:
        print(f"[planillas] {texto}")
    celdas = _sin_repetir(re.findall(rf"^{nombre_planilla} ([A-Z]+\d+)", t)[0] for t in advertencias
                          if re.match(rf"^{nombre_planilla} [A-Z]+\d+", t))
    otras = [t for t in advertencias if not re.match(rf"^{nombre_planilla} [A-Z]+\d+", t)]
    partes = []
    if celdas:
        partes.append(f"{len(celdas)} celda(s) no coinciden con la plantilla y quedaron sin completar "
                      f"({', '.join(celdas)}) — revisá/guardá la plantilla rediseñada")
    partes.extend(otras)
    return f"{nombre_planilla}: " + "; ".join(partes)


# ---------------------------------------------------------------------------
# PCS
# ---------------------------------------------------------------------------

def _completar_pcs(incidente: Incidente) -> ResultadoPlanilla:
    wb = load_workbook(_ubicar_plantilla(PCS_TEMPLATE_NOMBRE))
    e = EscritorVerificado(wb.active, "PCS")
    m = MAPEO_PCS

    e.escribir(m["numero_parte"], incidente.numero_parte, "N° de parte")
    if incidente.fecha:
        e.escribir(m["dia"], DIAS_SEMANA[incidente.fecha.weekday()], "Día")
        e.escribir(m["fecha"], _formatear_fecha(incidente.fecha), "Fecha")

    denunciante = ", ".join(x for x in (incidente.denunciante_apellido, incidente.denunciante_nombre) if x)
    e.escribir(m["denunciante"], denunciante, "Denunciante")
    e.escribir(m["hora_llamado"], _formatear_hora(incidente.hora_llamado), "Hora del llamado")
    e.escribir(m["domicilio"], incidente.denunciante_domicilio, "Domicilio del denunciante")
    e.escribir(m["contacto"], _medio_contacto(incidente), "Medio de contacto")
    e.escribir(m["dni"], incidente.denunciante_dni, "DNI del denunciante")
    e.escribir(m["recibio"], _nombre(incidente.recibio), "Recibió el llamado")
    if incidente.alarma_general is not None:
        e.escribir(m["alarma"], "SÍ" if incidente.alarma_general else "NO", "Alarma general")
    e.escribir(m["hora_alarma"], _formatear_hora(incidente.hora_toque), "Hora de alarma")

    e.escribir(m["tipo_siniestro"], _tipo_siniestro(incidente), "Tipo de siniestro")
    e.escribir(m["localidad"], incidente.localidad, "Localidad")
    e.escribir(m["calle"], incidente.calle_altura, "Calle / Lugar")
    e.escribir(m["zona"], _zona_y_coordenadas(incidente), "Zona / Coordenadas")

    e.escribir(m["autorizo"], _nombre(incidente.autorizo), "Autorizó la salida")
    e.escribir(m["descripcion"], incidente.resena_operativa, "Descripción")
    base = incidente.personal_base
    operadores = {b.funcion: b.personal for b in base}
    e.escribir(m["operador_1"], _nombre(operadores.get(FuncionBase.OPERADOR_1.value)), "Operador 1")
    e.escribir(m["operador_2"], _nombre(operadores.get(FuncionBase.OPERADOR_2.value)), "Operador 2")
    reserva = [b.personal.nombre_completo() for b in sorted(base, key=lambda b: b.orden)
               if b.funcion == FuncionBase.APRESTO.value]
    e.escribir_lista(m["reserva"], reserva, "Personal de reserva")

    # Fila 51: apresto (quedaron en cuartel) + en servicio (salieron en las
    # dotaciones) = total general; y quién confeccionó la planilla con su PIN.
    en_apresto = len(reserva)
    en_servicio = total_efectivos(_agrupar_dotaciones(incidente))
    e.escribir(m["total_apresto"], en_apresto, "Total en cuartel (apresto)", negrita=True)
    e.escribir(m["total_servicio"], en_servicio, "Total en servicio", negrita=True)
    e.escribir(m["total_general"], en_apresto + en_servicio, "Total general", negrita=True)
    e.escribir(m["confecciono"], _nombre(incidente.confecciono), "Confeccionó la planilla")

    ruta = _ruta_salida(incidente, "PCS")
    _pie_y_encabezado(wb, texto_autor(incidente))
    _una_hoja_por_pagina(wb)
    wb.save(ruta)
    return ResultadoPlanilla(ruta, _sin_repetir(e.advertencias))


# ---------------------------------------------------------------------------
# PCD2 (paginada)
# ---------------------------------------------------------------------------

def _paginas_pcd2(wb, cantidad: int) -> List[Worksheet]:
    """La plantilla es la página 1; las demás son copias con su logo y su
    área de impresión (copy_worksheet no copia ninguna de las dos)."""
    base = wb.active
    area = base.print_area.split("!")[-1] if base.print_area else None
    paginas = [base]
    # Image._data() cierra el stream de origen: se leen los bytes una vez y
    # cada imagen (la original y sus copias) queda con su propio buffer.
    logos = []
    for imagen in base._images:
        datos = imagen._data()
        imagen.ref = io.BytesIO(datos)
        logos.append((imagen, datos))
    for numero in range(2, cantidad + 1):
        copia = wb.copy_worksheet(base)
        copia.title = f"Página {numero}"
        if area:
            copia.print_area = area
        for imagen, datos in logos:
            nueva = ImagenExcel(io.BytesIO(datos))
            nueva.width, nueva.height = imagen.width, imagen.height
            nueva.anchor = deepcopy(imagen.anchor)
            copia.add_image(nueva)
        paginas.append(copia)
    if cantidad > 1:
        base.title = "Página 1"
    return paginas


def _completar_pcd2(incidente: Incidente) -> ResultadoPlanilla:
    wb = load_workbook(_ubicar_plantilla(PCD2_TEMPLATE_NOMBRE))
    dotaciones = _agrupar_dotaciones(incidente)
    total = max(1, math.ceil(len(dotaciones) / DOTACIONES_POR_PAGINA))
    advertencias: List[str] = []

    for indice, hoja in enumerate(_paginas_pcd2(wb, total)):
        e = EscritorVerificado(hoja, "PCD2")
        e.escribir(MAPEO_PCD2["numero_parte"], incidente.numero_parte, "N° de parte")
        e.escribir(MAPEO_PCD2["pagina"], indice + 1, "Página")
        e.escribir(MAPEO_PCD2["total_paginas"], total, "Total de páginas")
        en_pagina = dotaciones[indice * DOTACIONES_POR_PAGINA:(indice + 1) * DOTACIONES_POR_PAGINA]
        for bloque, d in zip(MAPEO_PCD2["bloques"], en_pagina):
            e.escribir(bloque["dotacion"], d["numero"], "Dotación N°")
            e.escribir(bloque["unidad"], d["movil"], "Unidad")
            e.escribir(bloque["fecha_salida"], _formatear_fecha(d["fecha"]), "Fecha de salida")
            e.escribir(bloque["hora_salida"], _formatear_hora(d["hora_salida"]), "Hora de salida")
            e.escribir(bloque["arribo"], _formatear_hora(d["hora_arribo"]), "Arribo a QTH")
            e.escribir(bloque["fecha_llegada"], _formatear_fecha(d["fecha_regreso"]), "Fecha de llegada")
            e.escribir(bloque["hora_llegada"], _formatear_hora(d["hora_regreso"]), "Hora de llegada")
            e.escribir(bloque["jefe"], d["a_cargo"], "Jefe de dotación")
            e.escribir(bloque["grado"], d["grado"], "Grado / cargo")
            e.escribir(bloque["chofer"], d["chofer"], "Chofer")
            e.escribir_lista(bloque["embarcados"], d["bomberos"], f"Embarcados de la Dotación N° {d['numero']}")
            if d["ruta_firma"] and d["a_cargo"]:
                cuando = f" el {d['firma_validada_en']:%d/%m/%Y %H:%M}" if d["firma_validada_en"] else ""
                _insertar_firma(e, bloque["firma"], d["ruta_firma"],
                                f"{d['a_cargo']} — firma digital validada con PIN{cuando}")
        advertencias.extend(e.advertencias)

    ruta = _ruta_salida(incidente, "PCD2")
    _pie_y_encabezado(wb, texto_autor(incidente), f"Total Efectivos: {total_efectivos(dotaciones)}")
    _una_hoja_por_pagina(wb)
    wb.save(ruta)
    return ResultadoPlanilla(ruta, _sin_repetir(advertencias))


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def generar_planilla(nombre: str, incidente_id: int) -> ResultadoPlanilla:
    """nombre: 'PCS' o 'PCD2'. Arma el Excel con la sesión abierta."""
    completar = {"PCS": _completar_pcs, "PCD2": _completar_pcd2}[nombre]
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            raise ValueError(f"No existe un incidente con id={incidente_id}.")
        return completar(incidente)


def generar_pcs(incidente_id: int) -> Path:
    return generar_planilla("PCS", incidente_id).ruta


def generar_pcd2(incidente_id: int) -> Path:
    return generar_planilla("PCD2", incidente_id).ruta


def abrir_para_impresion(ruta_archivo: Path) -> None:
    """Abre la planilla (PDF o .xlsx) con el visor predeterminado de Windows."""
    if sys.platform != "win32":
        print(f"[abrir_para_impresion] No estamos en Windows; abrí el archivo a mano: {ruta_archivo}")
        return
    try:
        import os
        os.startfile(str(ruta_archivo))  # noqa: S606 - abrir con la app asociada es la funcionalidad pedida
    except OSError as e:
        print(f"[abrir_para_impresion] No se pudo abrir '{ruta_archivo}' automáticamente: {e}")


def exportar_pdf(ruta_xlsx: Path) -> Optional[Path]:
    """Convierte el .xlsx (todas sus hojas = todas sus páginas) a PDF con
    Microsoft Excel (COM, pywin32). None si no hay Excel en esta máquina."""
    if sys.platform != "win32":
        return None
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None

    ruta_pdf = ruta_xlsx.with_suffix(".pdf")
    pythoncom.CoInitialize()
    excel = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        libro = excel.Workbooks.Open(str(ruta_xlsx.resolve()), ReadOnly=True)
        try:
            libro.ExportAsFixedFormat(0, str(ruta_pdf.resolve()))  # 0 = xlTypePDF
        finally:
            libro.Close(SaveChanges=False)
        return ruta_pdf if ruta_pdf.exists() else None
    except Exception as e:  # noqa: BLE001 - Excel ausente/bloqueado: se usa el .xlsx
        print(f"[exportar_pdf] No se pudo convertir '{ruta_xlsx.name}' a PDF: {e}")
        return None
    finally:
        if excel is not None:
            excel.Quit()
        pythoncom.CoUninitialize()


def generar_e_imprimir(nombre: str, incidente_id: int, abrir: bool = True) -> ResultadoPlanilla:
    """Genera, exporta a PDF (si hay Excel) y abre. `ruta` es la del PDF si se pudo."""
    resultado = generar_planilla(nombre, incidente_id)
    resultado.ruta = exportar_pdf(resultado.ruta) or resultado.ruta
    if abrir:
        abrir_para_impresion(resultado.ruta)
    return resultado


def generar_e_imprimir_pcs(incidente_id: int) -> ResultadoPlanilla:
    return generar_e_imprimir("PCS", incidente_id)


def generar_e_imprimir_pcd2(incidente_id: int) -> ResultadoPlanilla:
    return generar_e_imprimir("PCD2", incidente_id)

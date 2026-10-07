"""
Generador del Informe Técnico de Intervención en PDF (Fase 8) a partir de un
`Incidente`, usando el motor de impresión nativo de Qt (`QTextDocument` +
`QPrinter` en modo PDF) -- se eligió esta vía en vez de sumar ReportLab
como dependencia nueva del proyecto, ya que Qt alcanza de sobra para un
documento con encabezado institucional, tablas e imágenes (firma, y en el
futuro una captura del mapa), y la app ya lo trae adentro (PySide6).

Reutiliza el agrupado de dotaciones (`_agrupar_dotaciones`) y los
formateadores de fecha/hora/nombre de archivo de
`app.reports.excel_generator` -- viven en el mismo paquete `app.reports` y
son exactamente la misma lógica que ya arma correctamente las Dotaciones,
sus horarios propios (Fase 7) y su firma electrónica (Fase 8); no tiene
sentido duplicarla acá.

Tipografía unificada con las planillas Excel (commit 9b45bcd): Arial 10 pt
-- FUENTE_EXCEL / TAMANO_EXCEL_PT de excel_generator, que salen de
app/reports/tipografia.py --, texto a la izquierda y números / fechas /
horas centrados. El pie lleva quién confeccionó la planilla (validado con PIN).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QTextDocument
from PySide6.QtPrintSupport import QPrinter

from app.db import get_session
from app.models import Incidente
from app.paths import get_writable_dir
from app.reports.excel_generator import (
    FUENTE_EXCEL,
    TAMANO_EXCEL_PT,
    _agrupar_dotaciones,
    _formatear_fecha,
    _formatear_hora,
    _limpiar_para_archivo,
    abrir_para_impresion,
    texto_autor,
    total_efectivos,
)
from app.reports.tipografia import es_numerico, familia_css

OUTPUT_DIR = get_writable_dir("output") / "informes"


def _fila_tabla(etiqueta: str, valor) -> str:
    """Rótulo a la izquierda; el valor a la izquierda si es texto y centrado
    si es número, fecha u hora."""
    texto = str(valor) if valor not in (None, "") else "—"
    alineacion = "center" if es_numerico(valor) else "left"
    return (
        f"<tr><td align='left' style='padding:3px 10px;color:#64748b;width:35%;'>{etiqueta}</td>"
        f"<td align='{alineacion}' style='padding:3px 10px;'><b>{texto}</b></td></tr>"
    )


def _estilo_base() -> str:
    familia = familia_css()
    return (
        "<style>"
        f"body, p, div, td, span {{ font-family: {familia}; font-size: {TAMANO_EXCEL_PT}pt; text-align: left; }}"
        f"h3 {{ font-family: {familia}; }}"
        "td.num { text-align: center; }"
        "</style>"
    )


def _construir_html(incidente: Incidente) -> str:
    dotaciones = _agrupar_dotaciones(incidente)

    partes = [
        _estilo_base(),
        """
        <div style="text-align:center; border-bottom:3px solid #b91c1c; padding-bottom:10px; margin-bottom:16px;">
          <div style="font-size:32px;">🚒</div>
          <div style="font-size:16px; font-weight:700;">Sociedad de Bomberos Voluntarios "Osvaldo R. Rossi"</div>
          <div style="font-size:12px; color:#64748b;">Adelia María, Córdoba — C 59 / R 3</div>
          <div style="font-size:14px; font-weight:700; margin-top:8px;">INFORME TÉCNICO DE INTERVENCIÓN</div>
        </div>
        """
    ]

    tipo_categoria = incidente.tipo.nombre if incidente.tipo else "—"
    if incidente.categoria:
        tipo_categoria += f" — {incidente.categoria.nombre}"
    denunciante = f"{incidente.denunciante_apellido or ''}, {incidente.denunciante_nombre or ''}".strip(", ")
    ubicacion = f"{incidente.calle_altura or '—'}, {incidente.localidad} ({incidente.zona or '—'})"

    partes.append("<h3>Datos del Servicio</h3><table width='100%' cellspacing='0'>")
    partes.append(_fila_tabla("N° de Parte", incidente.numero_parte))
    partes.append(_fila_tabla("Fecha", _formatear_fecha(incidente.fecha)))
    partes.append(_fila_tabla("Tipo / Categoría", tipo_categoria))
    partes.append(_fila_tabla("Hora del Llamado", _formatear_hora(incidente.hora_llamado)))
    partes.append(_fila_tabla("Hora de la Sirena / Alarma", _formatear_hora(incidente.hora_toque)))
    partes.append(_fila_tabla("Ubicación", ubicacion))
    partes.append(_fila_tabla("Denunciante", denunciante))
    partes.append(_fila_tabla("Teléfono de contacto", incidente.denunciante_telefono))
    partes.append("</table>")

    partes.append("<h3>Dotaciones Intervinientes</h3>")
    if dotaciones:
        partes.append(
            f"<p><b>Total Efectivos: {total_efectivos(dotaciones)}</b> "
            f"&nbsp;·&nbsp; Dotaciones: {len(dotaciones)}</p>"
        )
    if not dotaciones:
        partes.append("<p style='color:#64748b;'>No se registraron dotaciones despachadas.</p>")
    for i, d in enumerate(dotaciones, start=1):
        partes.append(
            "<div style='margin-bottom:10px; border:1px solid #e2e8f0; border-radius:6px; padding:8px 10px;'>"
        )
        partes.append(f"<b>Dotación {i} — Unidad {d.get('movil') or '—'}</b><br>")
        horas = [_formatear_hora(d.get(k)) or "—" for k in ("hora_salida", "hora_arribo", "hora_regreso")]
        partes.append(
            "<table width='100%' cellspacing='0' cellpadding='2'><tr>"
            + "".join(f"<td align='center' style='color:#64748b;'>{r}</td>"
                      for r in ("Salida", "Arribo a QTH", "Regreso a Base"))
            + "</tr><tr>" + "".join(f"<td align='center'><b>{h}</b></td>" for h in horas) + "</tr></table>"
        )
        partes.append(f"Jefe de Dotación: {d.get('a_cargo') or '—'} &nbsp;&nbsp; Chofer: {d.get('chofer') or '—'}<br>")
        if d.get("bomberos"):
            partes.append("Bomberos: " + ", ".join(d["bomberos"]) + "<br>")
        partes.append("</div>")

    partes.append("<h3>Cartografía Táctica</h3><table width='100%' cellspacing='0'>")
    if incidente.latitud is not None and incidente.longitud is not None:
        partes.append(_fila_tabla("Coordenadas GPS", f"{incidente.latitud:.6f}, {incidente.longitud:.6f}"))
    else:
        partes.append(_fila_tabla("Coordenadas GPS", "Sin marcar"))
    superficie = f"{incidente.superficie_ha:.2f} ha" if incidente.superficie_ha else None
    partes.append(_fila_tabla("Superficie afectada", superficie))
    partes.append("</table>")
    if incidente.ruta_imagen_mapa and Path(incidente.ruta_imagen_mapa).is_file():
        uri_mapa = Path(incidente.ruta_imagen_mapa).resolve().as_uri()
        partes.append(f"<p style='margin-top:8px;'><img src='{uri_mapa}' width='600'></p>")
    if incidente.geometria_geojson:
        partes.append(
            "<p style='color:#64748b; font-size:11px;'>Se registró un polígono del área afectada -- "
            "ver el módulo de Cartografía Táctica para el detalle georreferenciado.</p>"
        )

    partes.append("<h3>Reseña Operativa</h3>")
    resena = (incidente.resena_operativa or "Sin reseña cargada.").replace("\n", "<br>")
    partes.append(f"<p>{resena}</p>")

    partes.append("<h3>Damnificados y Seguros</h3><table width='100%' cellspacing='0'>")
    partes.append(_fila_tabla("Civiles heridos", incidente.damnificados_heridos))
    partes.append(_fila_tabla("Civiles fallecidos", incidente.damnificados_muertos))
    partes.append(_fila_tabla("Bomberos lesionados", incidente.bomberos_lesionados))
    partes.append(_fila_tabla("Compañía de seguros", incidente.seguro_compania))
    partes.append(_fila_tabla("N° de póliza", incidente.seguro_poliza))
    partes.append("</table>")

    partes.append("<h3>Autorización</h3>")
    firmas_presentes = [
        (i, d) for i, d in enumerate(dotaciones, start=1)
        if d.get("ruta_firma") and Path(d["ruta_firma"]).exists()
    ]
    if not firmas_presentes:
        partes.append("<p style='color:#64748b;'>Ninguna dotación firmó electrónicamente esta intervención.</p>")
    for i, d in firmas_presentes:
        uri_firma = Path(d["ruta_firma"]).resolve().as_uri()
        partes.append(
            "<div style='margin-top:12px;'>"
            f"<img src='{uri_firma}' height='50'><br>"
            f"<span style='font-size:11px;'>Firmado por: <b>{d.get('a_cargo') or '—'}</b> "
            f"(Jefe de Dotación {i})</span></div>"
        )

    autor = texto_autor(incidente)
    partes.append(
        "<p style='margin-top:24px;'>"
        + (f"{autor}<br>" if autor else "Confeccionó: — (sin autor validado con PIN)<br>")
        + "<span style='font-size:10pt; color:#94a3b8;'>Informe generado automáticamente por Fire Station el "
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}.</span></p>"
    )

    return "".join(partes)


def _ruta_salida_pdf(incidente: Incidente) -> Path:
    anio = incidente.fecha.year if incidente.fecha else datetime.now().year
    numero_limpio = _limpiar_para_archivo(incidente.numero_parte)
    carpeta = OUTPUT_DIR / str(anio)
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"{numero_limpio}_INFORME.pdf"


def generar_informe_pdf(incidente_id: int) -> Path:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            raise ValueError(f"No existe el incidente id={incidente_id}")
        html = _construir_html(incidente)
        ruta_salida = _ruta_salida_pdf(incidente)

    documento = QTextDocument()
    documento.setDefaultFont(QFont(FUENTE_EXCEL, TAMANO_EXCEL_PT))
    documento.setHtml(html)

    impresora = QPrinter(QPrinter.PrinterMode.HighResolution)
    impresora.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    impresora.setOutputFileName(str(ruta_salida))
    impresora.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    impresora.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)

    documento.print_(impresora)
    return ruta_salida


def generar_e_imprimir_informe_pdf(incidente_id: int) -> Path:
    ruta = generar_informe_pdf(incidente_id)
    abrir_para_impresion(ruta)
    return ruta

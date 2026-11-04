"""
Códigos QR de los activos del cuartel (inventario_cuartel.codigo_qr).

  * Contenido del QR: el código tal cual (ej. "FS-9A1B2C3D"), sin URL ni
    prefijos, para que cualquier lector (pistola USB, celular) lo lea igual.
  * Generación: `segno` (Python puro) -> QImage / PNG / etiquetas en PDF.
  * Lectura: `normalizar_codigo` limpia lo que tipea una pistola lectora USB
    (que actúa como teclado) o lo que se pega a mano; `decodificar_qimage`
    decodifica un cuadro de la cámara con `zxing-cpp` (opcional: si no está
    instalado, la app sigue funcionando con pistola o texto).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

RE_CODIGO = re.compile(r"[A-Z0-9][A-Z0-9-]{2,39}")
# Pistola configurada en distribución US sobre Windows en español: la tecla
# "-" llega como "'" o "?" y algunos lectores agregan prefijos/sufijos.
_TRADUCCION_TECLADO = str.maketrans({"'": "-", "?": "-", "_": "-", "–": "-", "—": "-"})


def normalizar_codigo(texto: Optional[str]) -> Optional[str]:
    """'  fs-9a1b2c3d\\r\\n' -> 'FS-9A1B2C3D'. Acepta también una URL o texto
    que CONTENGA el código (se toma el último candidato válido). None si no
    hay ningún código plausible."""
    if not texto:
        return None
    limpio = "".join(c for c in texto if c.isprintable()).strip().upper().translate(_TRADUCCION_TECLADO)
    if not limpio:
        return None
    if RE_CODIGO.fullmatch(limpio):
        return limpio
    candidatos = [c.strip("-") for c in re.split(r"[^A-Z0-9-]+", limpio) if c]
    candidatos = [c for c in candidatos if RE_CODIGO.fullmatch(c) and any(ch.isdigit() for ch in c)]
    return candidatos[-1] if candidatos else None


# -----------------------------------------------------------------------------
# Generación
# -----------------------------------------------------------------------------

def _qr(codigo: str):
    import segno

    return segno.make(codigo, error="m", micro=False)


def qr_png(codigo: str, escala: int = 10, borde: int = 4) -> bytes:
    buffer = io.BytesIO()
    _qr(codigo).save(buffer, kind="png", scale=escala, border=borde)
    return buffer.getvalue()


def qr_svg(codigo: str, escala: int = 10, borde: int = 4) -> bytes:
    buffer = io.BytesIO()
    _qr(codigo).save(buffer, kind="svg", scale=escala, border=borde, xmldecl=True)
    return buffer.getvalue()


def qr_qimage(codigo: str, lado_px: int = 220):
    """QImage cuadrada de ~lado_px (múltiplo entero de módulos: bordes nítidos)."""
    from PySide6.QtGui import QImage

    qr = _qr(codigo)
    modulos = qr.symbol_size(scale=1, border=2)[0]
    escala = max(1, lado_px // modulos)
    imagen = QImage.fromData(qr_png(codigo, escala=escala, borde=2))
    return imagen


def guardar_qr(codigo: str, ruta: Path) -> Path:
    ruta = Path(ruta)
    datos = qr_svg(codigo) if ruta.suffix.lower() == ".svg" else qr_png(codigo, escala=16)
    ruta.write_bytes(datos)
    return ruta


# -----------------------------------------------------------------------------
# Etiquetas para rotular (PDF A4 o impresora)
# -----------------------------------------------------------------------------

ETIQUETAS_POR_FILA = 3
FILAS_POR_PAGINA = 7
INSTITUCION_CORTA = 'Bomberos Voluntarios "Osvaldo R. Rossi" · Adelia María'


def _fuente_que_entra(fuente, texto: str, ancho: float):
    """Achica la fuente hasta que el código entre completo en el ancho."""
    from PySide6.QtGui import QFont, QFontMetricsF

    fuente = QFont(fuente)
    while fuente.pixelSize() > 8 and QFontMetricsF(fuente).horizontalAdvance(texto) > ancho:
        fuente.setPixelSize(int(fuente.pixelSize() * 0.92))
    return fuente


def dibujar_etiquetas(dispositivo, activos: Sequence[Tuple[str, str, str]]) -> int:
    """Dibuja etiquetas (código, nombre, ubicación) en un QPagedPaintDevice
    (QPdfWriter o QPrinter): grilla de 3 x 7 por hoja A4, cada una con el QR,
    el código en grande, el nombre y la institución. Devuelve las páginas."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QFont, QPainter, QPen

    pintor = QPainter(dispositivo)
    try:
        pagina = dispositivo.pageLayout().paintRectPixels(dispositivo.resolution())
        ancho_celda = pagina.width() / ETIQUETAS_POR_FILA
        alto_celda = pagina.height() / FILAS_POR_PAGINA
        margen = ancho_celda * 0.05
        dpi = dispositivo.resolution()
        fuente_codigo = QFont("Arial")
        fuente_codigo.setPixelSize(int(dpi * 0.16))
        fuente_codigo.setBold(True)
        fuente_texto = QFont("Arial")
        fuente_texto.setPixelSize(int(dpi * 0.11))
        fuente_chica = QFont("Arial")
        fuente_chica.setPixelSize(int(dpi * 0.075))
        paginas = 1
        por_pagina = ETIQUETAS_POR_FILA * FILAS_POR_PAGINA
        for indice, (codigo, nombre, ubicacion) in enumerate(activos):
            if indice and indice % por_pagina == 0:
                dispositivo.newPage()
                paginas += 1
            pos = indice % por_pagina
            x = (pos % ETIQUETAS_POR_FILA) * ancho_celda
            y = (pos // ETIQUETAS_POR_FILA) * alto_celda
            celda = QRectF(x + margen, y + margen, ancho_celda - 2 * margen, alto_celda - 2 * margen)
            pintor.setPen(QPen(Qt.GlobalColor.lightGray, max(1.0, dpi / 300), Qt.PenStyle.DashLine))
            pintor.drawRect(celda)
            lado_qr = min(celda.height() * 0.86, celda.width() * 0.45)
            rect_qr = QRectF(celda.left() + celda.height() * 0.07, celda.top() + (celda.height() - lado_qr) / 2,
                             lado_qr, lado_qr)
            pintor.drawImage(rect_qr, qr_qimage(codigo, int(lado_qr)))
            texto = QRectF(rect_qr.right() + margen, celda.top() + margen,
                           celda.right() - rect_qr.right() - 2 * margen, celda.height() - 2 * margen)
            pintor.setPen(Qt.GlobalColor.black)
            pintor.setFont(_fuente_que_entra(fuente_codigo, codigo, texto.width()))
            pintor.drawText(texto, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, codigo)
            pintor.setFont(fuente_texto)
            cuerpo = QRectF(texto.left(), texto.top() + dpi * 0.22, texto.width(), texto.height() - dpi * 0.36)
            pintor.drawText(cuerpo, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                            f"{nombre}\n{ubicacion}" if ubicacion else nombre)
            pintor.setFont(fuente_chica)
            pintor.drawText(texto, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                            INSTITUCION_CORTA)
        return paginas
    finally:
        pintor.end()


def exportar_etiquetas_pdf(activos: Iterable[Tuple[str, str, str]], ruta: Path) -> Tuple[Path, int]:
    """PDF A4 con las etiquetas. Devuelve (ruta, páginas)."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter

    activos = list(activos)
    if not activos:
        raise ValueError("No hay activos para etiquetar.")
    ruta = Path(ruta)
    escritor = QPdfWriter(str(ruta))
    escritor.setResolution(300)
    escritor.setTitle("Etiquetas QR — Fire Station")
    escritor.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                       QMarginsF(8, 10, 8, 10), QPageLayout.Unit.Millimeter))
    paginas = dibujar_etiquetas(escritor, activos)
    return ruta, paginas


# -----------------------------------------------------------------------------
# Lectura por cámara
# -----------------------------------------------------------------------------

def decodificador_disponible() -> bool:
    try:
        import numpy  # noqa: F401
        import zxingcpp  # noqa: F401
    except Exception:  # noqa: BLE001 - ImportError o DLL faltante
        return False
    return True


def decodificar_qimage(imagen) -> List[str]:
    """Textos de los QR / códigos de barras presentes en la imagen."""
    import numpy
    import zxingcpp
    from PySide6.QtGui import QImage

    gris = imagen.convertToFormat(QImage.Format.Format_Grayscale8)
    if gris.isNull() or gris.width() == 0:
        return []
    bits = gris.constBits()
    matriz = numpy.frombuffer(bits, dtype=numpy.uint8, count=gris.bytesPerLine() * gris.height())
    matriz = matriz.reshape(gris.height(), gris.bytesPerLine())[:, : gris.width()]
    return [r.text for r in zxingcpp.read_barcodes(numpy.ascontiguousarray(matriz)) if r.text]

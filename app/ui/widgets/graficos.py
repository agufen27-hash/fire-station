"""
Gráficos livianos para la página Estadísticas, dibujados con QPainter (sin
QtCharts: no suma dependencias al ejecutable y respeta el tema claro/oscuro
de app/ui/theme.py, porque leen los colores en cada repintado).

  GraficoColumnas  serie temporal (servicios por mes): columnas verticales.
  GraficoBarras    ranking (horas por móvil, por bombero): barras horizontales
                   con la etiqueta a la izquierda y el valor a la derecha.
  GraficoDona      composición (servicios por tipo): dona + leyenda con
                   nombre, cantidad y porcentaje (la identidad nunca es solo
                   el color). Más de 8 categorías se agrupan en "Otros".

Todos muestran un tooltip con el valor exacto al pasar el mouse por encima.
Paleta categórica en orden fijo (nunca ciclada), con pasos propios para el
tema oscuro.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.ui import theme

# Paleta categórica validada (orden fijo): claro / oscuro.
PALETA_CLARO = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
PALETA_OSCURO = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]
MAX_CATEGORIAS = len(PALETA_CLARO)
TEXTO_OTROS = "Otros"

Dato = Tuple[str, float, str]   # (etiqueta, valor, valor formateado para mostrar)


def color_serie(indice: int) -> QColor:
    paleta = PALETA_OSCURO if theme.PALETA.nombre == "oscuro" else PALETA_CLARO
    return QColor(paleta[indice % len(paleta)])


def agrupar_otros(datos: Sequence[Dato], maximo: int = MAX_CATEGORIAS,
                  formato=lambda v: f"{v:g}") -> List[Dato]:
    """Las primeras `maximo - 1` categorías y el resto sumado en "Otros"."""
    datos = list(datos)
    if len(datos) <= maximo:
        return datos
    resto = sum(d[1] for d in datos[maximo - 1:])
    return datos[:maximo - 1] + [(TEXTO_OTROS, resto, formato(resto))]


class _GraficoBase(QWidget):
    def __init__(self, titulo: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._titulo = titulo
        self._datos: List[Dato] = []
        self._zonas: List[Tuple[QRectF, str]] = []   # hit targets del tooltip
        self._resaltado: Optional[int] = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(220)

    def set_datos(self, datos: Sequence[Dato]) -> None:
        self._datos = list(datos)
        self._resaltado = None
        self.update()

    def datos(self) -> List[Dato]:
        return list(self._datos)

    def sizeHint(self) -> QSize:  # noqa: N802 - override de Qt
        return QSize(420, 260)

    # -- helpers de dibujo -------------------------------------------------------

    def _fuente(self, puntos: float, negrita: bool = False) -> QFont:
        fuente = QFont(theme.FUENTE)
        fuente.setPointSizeF(puntos)
        fuente.setBold(negrita)
        return fuente

    def _pintar_fondo_y_titulo(self, p: QPainter) -> QRectF:
        """Dibuja el título y devuelve el área libre para el gráfico."""
        rect = QRectF(self.rect())
        p.setPen(QColor(theme.color("texto")))
        p.setFont(self._fuente(10, negrita=True))
        p.drawText(rect.adjusted(4, 2, -4, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self._titulo)
        return rect.adjusted(4, 28, -4, -4)

    def _pintar_vacio(self, p: QPainter, area: QRectF) -> None:
        p.setPen(QColor(theme.color("texto_tenue")))
        p.setFont(self._fuente(9))
        p.drawText(area, Qt.AlignmentFlag.AlignCenter, "Sin datos para el período y los filtros elegidos")

    @staticmethod
    def _barra_redondeada(rect: QRectF, radio: float, vertical: bool) -> QPainterPath:
        """Rectángulo con el extremo de dato redondeado y la base recta."""
        camino = QPainterPath()
        radio = min(radio, rect.width() / 2, rect.height() / 2)
        if radio <= 0:
            camino.addRect(rect)
            return camino
        camino.addRoundedRect(rect, radio, radio)
        recto = QPainterPath()
        if vertical:   # base abajo, recta
            recto.addRect(QRectF(rect.left(), rect.bottom() - radio, rect.width(), radio))
        else:          # base a la izquierda, recta
            recto.addRect(QRectF(rect.left(), rect.top(), radio, rect.height()))
        return camino.united(recto)

    # -- tooltip ----------------------------------------------------------------

    def mouseMoveEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        pos = evento.position()
        indice = next((i for i, (zona, _t) in enumerate(self._zonas) if zona.contains(pos)), None)
        if indice != self._resaltado:
            self._resaltado = indice
            self.update()
        if indice is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(evento.globalPosition().toPoint(), self._zonas[indice][1], self)
        super().mouseMoveEvent(evento)

    def leaveEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        self._resaltado = None
        self.update()
        super().leaveEvent(evento)


class GraficoColumnas(_GraficoBase):
    """Una serie en el tiempo (un solo color: el título la nombra)."""

    def paintEvent(self, _evento) -> None:  # noqa: N802 - override de Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._pintar_fondo_y_titulo(p)
        self._zonas = []
        if not self._datos or not any(d[1] for d in self._datos):
            self._pintar_vacio(p, area)
            return
        maximo = max(d[1] for d in self._datos) or 1
        p.setFont(self._fuente(8))
        metricas = p.fontMetrics()
        ancho_eje = max(metricas.horizontalAdvance(_formato_eje(maximo * f)) for f in (0.5, 1.0)) + 10
        alto_etiquetas = metricas.height() + 6
        plot = QRectF(area.left() + ancho_eje, area.top() + 14, area.width() - ancho_eje,
                      area.height() - alto_etiquetas - 14)

        # Grilla recesiva: 0, mitad y máximo.
        p.setPen(QPen(QColor(theme.color("borde_suave")), 1))
        for fraccion in (0.0, 0.5, 1.0):
            y = plot.bottom() - fraccion * plot.height()
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(theme.color("texto_tenue")))
            p.drawText(QRectF(area.left(), y - 8, ancho_eje - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _formato_eje(maximo * fraccion))
            p.setPen(QPen(QColor(theme.color("borde_suave")), 1))

        n = len(self._datos)
        paso = plot.width() / n
        ancho = max(2.0, min(36.0, paso - 2))   # 2 px de separación entre columnas
        cada = max(1, math.ceil(n / max(1, int(plot.width() // 52))))  # etiquetas del eje sin pisarse
        indice_max = max(range(n), key=lambda i: self._datos[i][1])
        for i, (etiqueta, valor, texto) in enumerate(self._datos):
            x = plot.left() + i * paso + (paso - ancho) / 2
            alto = plot.height() * valor / maximo
            barra = QRectF(x, plot.bottom() - alto, ancho, alto)
            color = color_serie(0)
            if self._resaltado is not None and self._resaltado != i:
                color.setAlpha(150)
            if alto > 0:
                p.fillPath(self._barra_redondeada(barra, 4, vertical=True), color)
            self._zonas.append((QRectF(plot.left() + i * paso, plot.top(), paso, plot.height()),
                                f"{etiqueta}: {texto}"))
            if i % cada == 0:
                p.setPen(QColor(theme.color("texto_secundario")))
                p.drawText(QRectF(plot.left() + i * paso - 20, plot.bottom() + 4, paso + 40, alto_etiquetas),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, etiqueta)
            if i == indice_max:  # rótulo selectivo: solo el pico
                p.setPen(QColor(theme.color("texto")))
                p.drawText(QRectF(x - 30, barra.top() - 16, ancho + 60, 14),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, texto)


class GraficoBarras(_GraficoBase):
    """Ranking horizontal. `multicolor` pinta cada categoría con su color de la
    paleta (cuando las barras son entidades distintas a identificar en otro
    gráfico); por defecto, un solo color."""

    def __init__(self, titulo: str = "", parent: Optional[QWidget] = None, maximo_filas: int = 10,
                 multicolor: bool = False) -> None:
        super().__init__(titulo, parent)
        self._maximo_filas = maximo_filas
        self._multicolor = multicolor

    def paintEvent(self, _evento) -> None:  # noqa: N802 - override de Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._pintar_fondo_y_titulo(p)
        self._zonas = []
        datos = [d for d in self._datos if d[1] > 0][: self._maximo_filas]
        if not datos:
            self._pintar_vacio(p, area)
            return
        p.setFont(self._fuente(8.5))
        metricas = p.fontMetrics()
        ancho_etiqueta = min(area.width() * 0.38, max(metricas.horizontalAdvance(d[0]) for d in datos) + 10)
        ancho_valor = max(metricas.horizontalAdvance(d[2]) for d in datos) + 10
        maximo = max(d[1] for d in datos) or 1
        alto_fila = min(28.0, area.height() / len(datos))
        alto_barra = max(6.0, alto_fila - 8)
        x0 = area.left() + ancho_etiqueta
        ancho_util = max(10.0, area.width() - ancho_etiqueta - ancho_valor)
        for i, (etiqueta, valor, texto) in enumerate(datos):
            y = area.top() + i * alto_fila
            p.setPen(QColor(theme.color("texto_secundario")))
            p.drawText(QRectF(area.left(), y, ancho_etiqueta - 8, alto_fila),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       metricas.elidedText(etiqueta, Qt.TextElideMode.ElideRight, int(ancho_etiqueta - 8)))
            largo = ancho_util * valor / maximo
            barra = QRectF(x0, y + (alto_fila - alto_barra) / 2, largo, alto_barra)
            color = color_serie(i if self._multicolor else 0)
            if self._resaltado is not None and self._resaltado != i:
                color.setAlpha(150)
            p.fillPath(self._barra_redondeada(barra, 4, vertical=False), color)
            p.setPen(QColor(theme.color("texto")))
            p.drawText(QRectF(x0 + largo + 6, y, ancho_valor, alto_fila),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, texto)
            self._zonas.append((QRectF(area.left(), y, area.width(), alto_fila), f"{etiqueta}: {texto}"))


class GraficoDona(_GraficoBase):
    """Composición: dona a la izquierda y leyenda (color + nombre + valor + %)."""

    def paintEvent(self, _evento) -> None:  # noqa: N802 - override de Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._pintar_fondo_y_titulo(p)
        self._zonas = []
        datos = [d for d in agrupar_otros(self._datos) if d[1] > 0]
        total = sum(d[1] for d in datos)
        if not total:
            self._pintar_vacio(p, area)
            return
        lado = min(area.height(), area.width() * 0.45)
        circulo = QRectF(area.left() + 4, area.top() + (area.height() - lado) / 2, lado, lado)
        grosor = max(14.0, lado * 0.2)
        anillo = circulo.adjusted(grosor / 2, grosor / 2, -grosor / 2, -grosor / 2)
        inicio = 90.0
        for i, (_etiqueta, valor, _texto) in enumerate(datos):
            barrido = -360.0 * valor / total
            color = color_serie(i)
            if self._resaltado is not None and self._resaltado != i:
                color.setAlpha(110)
            p.setPen(QPen(color, grosor, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(anillo, int(inicio * 16), int(barrido * 16))
            # 2 px de separación entre porciones, del color de la superficie.
            if len(datos) > 1:
                p.setPen(QPen(QColor(theme.color("superficie")), 2))
                angulo = math.radians(inicio)
                centro = anillo.center()
                radio_int, radio_ext = anillo.width() / 2 - grosor / 2, anillo.width() / 2 + grosor / 2
                p.drawLine(QPointF(centro.x() + radio_int * math.cos(angulo), centro.y() - radio_int * math.sin(angulo)),
                           QPointF(centro.x() + radio_ext * math.cos(angulo), centro.y() - radio_ext * math.sin(angulo)))
            inicio += barrido

        # Total en el centro.
        p.setPen(QColor(theme.color("texto")))
        p.setFont(self._fuente(14, negrita=True))
        p.drawText(circulo, Qt.AlignmentFlag.AlignCenter, f"{total:g}")

        # Leyenda.
        p.setFont(self._fuente(8.5))
        metricas = p.fontMetrics()
        x = circulo.right() + 18
        alto_fila = min(22.0, area.height() / len(datos))
        y0 = area.top() + max(0.0, (area.height() - alto_fila * len(datos)) / 2)
        ancho_leyenda = area.right() - x
        for i, (etiqueta, valor, texto) in enumerate(datos):
            y = y0 + i * alto_fila
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color_serie(i))
            p.drawRoundedRect(QRectF(x, y + alto_fila / 2 - 5, 10, 10), 2, 2)
            porcentaje = 100.0 * valor / total
            p.setPen(QColor(theme.color("texto") if self._resaltado in (None, i) else theme.color("texto_tenue")))
            linea = f"{etiqueta} · {texto} ({porcentaje:.0f}%)"
            p.drawText(QRectF(x + 16, y, ancho_leyenda - 16, alto_fila),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       metricas.elidedText(linea, Qt.TextElideMode.ElideRight, int(ancho_leyenda - 16)))
            self._zonas.append((QRectF(x, y, ancho_leyenda, alto_fila), f"{etiqueta}: {texto} ({porcentaje:.1f}%)"))
        p.setBrush(Qt.BrushStyle.NoBrush)


def _formato_eje(valor: float) -> str:
    return f"{valor:.0f}" if valor >= 10 or float(valor).is_integer() else f"{valor:.1f}"

"""
Tarjeta "🚨 SERVICIO EN CURSO" del dashboard: una por cada servicio
guardado como borrador (estado_operativo = EN_CURSO), con N° de parte, tipo,
móviles despachados, hora de salida y un cronómetro en tiempo real. El
botón "Continuar Carga / Cerrar Servicio" abre el formulario precargado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.models import Incidente
from app.ui import theme


@dataclass
class ResumenEnCurso:
    incidente_id: int
    numero_parte: str
    tipo: str
    moviles: List[str] = field(default_factory=list)
    inicio: Optional[datetime] = None  # salida del primer móvil (o lo mejor que haya)
    direccion: str = ""


def _combinar(dia: Optional[date], hora: Optional[time]) -> Optional[datetime]:
    return datetime.combine(dia, hora) if dia and hora else None


def resumen_de(incidente: Incidente) -> ResumenEnCurso:
    """Lee (con la sesión abierta) lo que muestra la tarjeta. El cronómetro
    arranca en la salida más temprana de sus unidades; si todavía no hay
    ninguna, en la salida general, el llamado o la creación del borrador."""
    tipo = incidente.tipo.nombre if incidente.tipo else "Sin tipo"
    if incidente.categoria:
        tipo += f" — {incidente.categoria.nombre}"
    salidas = [
        _combinar(su.fecha_salida or incidente.fecha_salida or incidente.fecha, su.hora_salida)
        for su in incidente.salidas_unidad
    ]
    candidatos = [s for s in salidas if s] or [
        _combinar(incidente.fecha_salida or incidente.fecha, incidente.hora_salida),
        _combinar(incidente.fecha, incidente.hora_llamado),
        incidente.creado_en,
    ]
    return ResumenEnCurso(
        incidente_id=incidente.id,
        numero_parte=incidente.numero_parte,
        tipo=tipo,
        moviles=[su.movil.nombre_identificador for su in incidente.salidas_unidad if su.movil],
        inicio=min((c for c in candidatos if c), default=None),
        direccion=", ".join(x for x in (incidente.calle_altura, incidente.localidad) if x),
    )


def formatear_transcurrido(inicio: Optional[datetime], ahora: Optional[datetime] = None) -> str:
    if inicio is None:
        return "--:--:--"
    segundos = max(0, int(((ahora or datetime.now()) - inicio).total_seconds()))
    horas, resto = divmod(segundos, 3600)
    return f"{horas:02d}:{resto // 60:02d}:{resto % 60:02d}"


class TarjetaServicioEnCurso(QFrame):
    continuar = Signal(int)  # incidente_id

    def __init__(self, resumen: ResumenEnCurso, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaEnCurso")
        self.resumen = resumen
        theme.aplicar_sombra(self, "rojo")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(24)

        izquierda = QVBoxLayout()
        izquierda.setSpacing(4)
        titulo = QLabel("🚨 SERVICIO EN CURSO", self)
        titulo.setObjectName("tituloEnCurso")
        izquierda.addWidget(titulo)
        self.label_parte = QLabel(f"N° {resumen.numero_parte} · {resumen.tipo}", self)
        self.label_parte.setObjectName("parteEnCurso")
        izquierda.addWidget(self.label_parte)
        detalle = []
        if resumen.direccion:
            detalle.append(f"📍 {resumen.direccion}")
        detalle.append("🚒 " + (", ".join(resumen.moviles) if resumen.moviles else "sin móviles cargados"))
        detalle.append("Salida " + (resumen.inicio.strftime("%d/%m %H:%M") if resumen.inicio else "sin registrar"))
        self.label_detalle = QLabel("   ·   ".join(detalle), self)
        self.label_detalle.setObjectName("detalleEnCurso")
        self.label_detalle.setWordWrap(True)
        izquierda.addWidget(self.label_detalle)
        layout.addLayout(izquierda, 1)

        centro = QVBoxLayout()
        centro.setSpacing(0)
        etiqueta = QLabel("TIEMPO TRANSCURRIDO", self)
        etiqueta.setObjectName("etiquetaCronometro")
        etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_cronometro = QLabel(formatear_transcurrido(resumen.inicio), self)
        self.label_cronometro.setObjectName("cronometroEnCurso")
        self.label_cronometro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        centro.addWidget(etiqueta)
        centro.addWidget(self.label_cronometro)
        layout.addLayout(centro)

        self.boton_continuar = QPushButton("Continuar Carga / Cerrar Servicio", self)
        self.boton_continuar.setObjectName("botonPrimarioRojo")
        self.boton_continuar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton_continuar.clicked.connect(lambda: self.continuar.emit(resumen.incidente_id))
        layout.addWidget(self.boton_continuar, 0, Qt.AlignmentFlag.AlignVCenter)

    def tick(self, ahora: Optional[datetime] = None) -> None:
        self.label_cronometro.setText(formatear_transcurrido(self.resumen.inicio, ahora))

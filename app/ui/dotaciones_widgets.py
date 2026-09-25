"""
Dotaciones (formato planilla PCD2) y Personal en Base (planilla PCS).

DOTACIONES MÓVILES -- una tarjeta "Dotación N° n" por unidad al lugar:
  Móvil, Chofer, Fecha/Hora de salida, Arribo a QTH, Fecha/Hora de llegada,
  Jefe de Dotación (su grado/cargo se completa solo) con validación de firma
  por PIN, y "Personal embarcado". Todos son Intervinientes (tarea '1' en
  RUBA); el Jefe de la Dotación N° 1 es el Encargado general del servicio.

PERSONAL EN BASE -- panel fijo: Operador de Guardia 1 y 2 y el personal en
  Apresto / Reserva. Todos computan como Apresto (tarea '2' en RUBA).

Una persona no puede figurar dos veces en todo el servicio.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.catalogos import Bombero, Movil as MovilRuba
from app.db import get_session
from app.models import Personal
from app.services.personal_info import grado_de
from app.ui import theme
from app.ui.damnificados_widgets import _ajustar_alto, _boton_quitar, _tabla
from app.ui.participacion_widgets import HorarioServicio, SelectorBombero, texto_movil
from app.ui.widgets import DateField, TimeField

COL_BOMBERO, COL_QUITAR = range(2)
MAX_EMBARCADOS_PCD2 = 10
MAX_APRESTO_PCS = 26


def personal_id_por_id_ruba(id_ruba: int) -> Optional[int]:
    with get_session() as session:
        persona = session.query(Personal).filter(Personal.id_ruba == id_ruba).first()
        return persona.id if persona else None


class _FilaEmbarcado:
    def __init__(self, tarjeta: "TarjetaUnidad") -> None:
        self.selector = SelectorBombero(tarjeta._padron, tarjeta.tabla)
        self.boton_quitar = _boton_quitar(tarjeta.tabla, "Quitar de la dotación")

    def datos(self) -> Dict[str, Any]:
        return {"id_ruba": self.selector.id_ruba(), "texto": self.selector.text().strip()}


class TarjetaUnidad(QFrame):
    """Una dotación: tarjeta "Dotación N° n"."""

    quitar_solicitado = Signal(object)   # self
    dotacion_cambiada = Signal()
    llegada_editada = Signal()           # fecha/hora de llegada cargada a mano

    def __init__(
        self, numero: int, moviles: Sequence[MovilRuba], padron: Sequence[Bombero],
        parent: Optional[QWidget] = None, resolver_personal: Optional[Callable[[int], Optional[int]]] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaUnidad")
        self._padron = list(padron)
        self._por_id = {b.id_ruba: b for b in self._padron}
        self._filas: List[_FilaEmbarcado] = []
        self._editados: set[str] = set()
        self._resolver_personal = resolver_personal or personal_id_por_id_ruba
        self._firmado_por: Optional[int] = None
        self.firma_validada_en: Optional[datetime] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        cabecera = QHBoxLayout()
        self.label_titulo = QLabel("", self)
        self.label_titulo.setObjectName("tituloUnidad")
        cabecera.addWidget(self.label_titulo)
        self.label_encargado = QLabel("Su Jefe es el Encargado general del servicio", self)
        self.label_encargado.setObjectName("chip")
        theme.set_tono(self.label_encargado, "info")
        cabecera.addWidget(self.label_encargado)
        cabecera.addStretch(1)
        boton_quitar = QPushButton("Quitar dotación", self)
        boton_quitar.setObjectName("botonQuitarFila")
        boton_quitar.clicked.connect(lambda: self.quitar_solicitado.emit(self))
        cabecera.addWidget(boton_quitar)
        layout.addLayout(cabecera)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        for col in range(5):
            grid.setColumnStretch(col, 1)
        grid.addWidget(QLabel(theme.etiqueta_requerida("Móvil"), self), 0, 0, 1, 2)
        grid.addWidget(QLabel(theme.etiqueta_requerida("Chofer"), self), 0, 2, 1, 3)
        self.combo_movil = QComboBox(self)
        self.set_moviles(moviles)
        self.selector_chofer = SelectorBombero(self._padron, self)
        self.selector_chofer.cambiado.connect(self.dotacion_cambiada.emit)
        grid.addWidget(self.combo_movil, 1, 0, 1, 2)
        grid.addWidget(self.selector_chofer, 1, 2, 1, 3)

        for col, texto in enumerate(("Fecha salida", "Hora salida", "Arribo a QTH", "Fecha llegada", "Hora llegada")):
            grid.addWidget(QLabel(texto, self), 2, col)
        self.fecha_salida = DateField(self, con_boton=False)
        self.hora_salida = TimeField(self, con_boton=False)
        self.hora_arribo = TimeField(self, con_boton=False)
        self.fecha_regreso = DateField(self, con_boton=False)
        self.hora_regreso = TimeField(self, con_boton=False)
        for col, campo in enumerate((self.fecha_salida, self.hora_salida, self.hora_arribo,
                                     self.fecha_regreso, self.hora_regreso)):
            grid.addWidget(campo, 3, col)
        for nombre, campo in self._campos_horarios().items():
            campo.valorCambiado.connect(lambda n=nombre: self._marcar_editado(n))

        grid.addWidget(QLabel(theme.etiqueta_requerida("Jefe de Dotación"), self), 4, 0, 1, 3)
        grid.addWidget(QLabel("Grado / cargo (del padrón)", self), 4, 3, 1, 2)
        self.selector_jefe = SelectorBombero(self._padron, self)
        self.selector_jefe.cambiado.connect(self._on_jefe_cambiado)
        self.label_grado = QLabel("—", self)
        self.label_grado.setObjectName("valorDatoClima")
        grid.addWidget(self.selector_jefe, 5, 0, 1, 3)
        grid.addWidget(self.label_grado, 5, 3, 1, 2)
        layout.addLayout(grid)

        fila_firma = QHBoxLayout()
        fila_firma.addWidget(QLabel("Firma digital:", self))
        self.entry_pin = QLineEdit(self)
        self.entry_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry_pin.setMaxLength(10)
        self.entry_pin.setPlaceholderText("PIN del Jefe")
        self.entry_pin.setFixedWidth(140)
        self.entry_pin.returnPressed.connect(self._validar_desde_ui)
        fila_firma.addWidget(self.entry_pin)
        self.boton_firmar = QPushButton("✍️ Validar y firmar", self)
        self.boton_firmar.setObjectName("botonAhora")
        self.boton_firmar.clicked.connect(self._validar_desde_ui)
        fila_firma.addWidget(self.boton_firmar)
        self.label_firma = QLabel("Sin firmar", self)
        self.label_firma.setObjectName("badgeSinFirmar")
        fila_firma.addWidget(self.label_firma)
        fila_firma.addStretch(1)
        layout.addLayout(fila_firma)

        subtitulo = QLabel(f"Personal embarcado (hasta {MAX_EMBARCADOS_PCD2} en la PCD2)", self)
        subtitulo.setObjectName("subtituloBloque")
        layout.addWidget(subtitulo)
        self.tabla = _tabla(self, ["Apellido y Nombre", ""], estirar=(COL_BOMBERO,), anchos={COL_QUITAR: 44})
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar Bombero", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_bombero)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)
        self.set_numero(numero)

    def set_moviles(self, moviles: Sequence[MovilRuba]) -> None:
        """(Re)carga las unidades ofrecidas sin perder la elegida, aunque ya
        no esté en la lista (se agrega al final para no vaciar la dotación)."""
        actual_dato, actual_texto = self.combo_movil.currentData(), self.combo_movil.currentText()
        self.combo_movil.blockSignals(True)
        self.combo_movil.clear()
        self.combo_movil.addItem("— Seleccionar —", None)
        for movil in moviles:
            self.combo_movil.addItem(texto_movil(movil), movil.id_ruba)
        if actual_dato is not None:
            if self.combo_movil.findData(actual_dato) < 0:
                self.combo_movil.addItem(actual_texto, actual_dato)
            self.combo_movil.setCurrentIndex(self.combo_movil.findData(actual_dato))
        self.combo_movil.blockSignals(False)

    def set_numero(self, numero: int) -> None:
        self.numero = numero
        self.label_titulo.setText(f"🚒  Dotación N° {numero}")
        self.label_encargado.setVisible(numero == 1)

    # -- Horarios ---------------------------------------------------------------

    def _campos_horarios(self) -> Dict[str, QWidget]:
        return {"fecha_salida": self.fecha_salida, "hora_salida": self.hora_salida,
                "fecha_llegada": self.fecha_regreso, "hora_llegada": self.hora_regreso}

    def _marcar_editado(self, nombre: str) -> None:
        self._editados.add(nombre)
        if nombre in ("fecha_llegada", "hora_llegada"):
            self.llegada_editada.emit()

    def llegada_propia(self) -> Optional[Tuple[date, time]]:
        """(fecha, hora) de llegada cargada a mano en ESTA dotación (no la
        heredada del horario general), o None."""
        hora = self.hora_regreso.value()
        if "hora_llegada" not in self._editados or hora is None:
            return None
        return self.fecha_regreso.value(), hora

    def aplicar_horario(self, horario: HorarioServicio) -> None:
        """Copia el horario general en los campos que no se editaron a mano."""
        for nombre, campo in self._campos_horarios().items():
            if nombre in self._editados:
                continue
            valor = getattr(horario, nombre)
            if isinstance(campo, DateField):
                if valor is not None:
                    campo.set_value(valor)
            else:
                campo.set_value(valor)

    def cargar_horario(self, valores: HorarioServicio, general: HorarioServicio) -> None:
        for nombre, campo in self._campos_horarios().items():
            valor = getattr(valores, nombre)
            if valor is None and isinstance(campo, DateField):
                continue
            campo.set_value(valor)
            if valor != getattr(general, nombre):
                self._editados.add(nombre)

    # -- Jefe, grado y firma ----------------------------------------------------

    def _on_jefe_cambiado(self) -> None:
        self.label_grado.setText(grado_de(self._por_id.get(self.selector_jefe.id_ruba())) or "—")
        if self._firmado_por is not None and self._firmado_por != self.selector_jefe.id_ruba():
            self._resetear_firma()
        self.dotacion_cambiada.emit()

    def grado_jefe(self) -> Optional[str]:
        texto = self.label_grado.text()
        return None if texto == "—" else texto

    def _validar_desde_ui(self) -> None:
        motivo = self.validar_pin(self.entry_pin.text())
        self.entry_pin.clear()
        if motivo:
            self.label_firma.setText(f"✖ {motivo}")
            self.label_firma.setObjectName("badgeSinFirmar")
            theme.repulir(self.label_firma)

    def validar_pin(self, pin: str) -> Optional[str]:
        """Valida el PIN del Jefe de Dotación contra su legajo. Devuelve None
        si la dotación quedó firmada, o el motivo por el que no."""
        id_ruba = self.selector_jefe.id_ruba()
        if id_ruba is None:
            return "Primero elegí el Jefe de Dotación."
        personal_id = self._resolver_personal(id_ruba)
        if personal_id is None:
            return "El Jefe no tiene legajo local."
        with get_session() as session:
            persona = session.get(Personal, personal_id)
            pin_real = persona.pin
            tiene_firma = bool(persona.ruta_firma) and Path(persona.ruta_firma).exists()
        if not tiene_firma:
            return "No tiene firma registrada en su legajo (Documentación)."
        if pin != pin_real:
            return "PIN incorrecto."
        self._marcar_firmado(id_ruba, datetime.now())
        return None

    def _marcar_firmado(self, id_ruba: int, cuando: datetime) -> None:
        self._firmado_por = id_ruba
        self.firma_validada_en = cuando
        self.label_firma.setText(f"✅ Firmada {cuando:%d/%m %H:%M}")
        self.label_firma.setObjectName("badgeFirmado")
        theme.repulir(self.label_firma)

    def _resetear_firma(self) -> None:
        self._firmado_por = None
        self.firma_validada_en = None
        self.label_firma.setText("Sin firmar")
        self.label_firma.setObjectName("badgeSinFirmar")
        theme.repulir(self.label_firma)

    def esta_firmada(self) -> bool:
        return self._firmado_por is not None and self._firmado_por == self.selector_jefe.id_ruba()

    # -- Personal embarcado ------------------------------------------------------

    def agregar_bombero(self) -> _FilaEmbarcado:
        fila = _FilaEmbarcado(self)
        fila.selector.cambiado.connect(self.dotacion_cambiada.emit)
        fila.boton_quitar.clicked.connect(lambda _=False, f=fila: self.quitar_bombero(f))
        indice = self.tabla.rowCount()
        self.tabla.insertRow(indice)
        self.tabla.setCellWidget(indice, COL_BOMBERO, fila.selector)
        self.tabla.setCellWidget(indice, COL_QUITAR, fila.boton_quitar)
        self._filas.append(fila)
        _ajustar_alto(self.tabla)
        return fila

    def quitar_bombero(self, fila: _FilaEmbarcado) -> None:
        if fila not in self._filas:
            return
        self.tabla.removeRow(self._filas.index(fila))
        self._filas.remove(fila)
        _ajustar_alto(self.tabla)
        self.dotacion_cambiada.emit()

    def filas(self) -> List[_FilaEmbarcado]:
        return list(self._filas)

    def datos(self) -> Dict[str, Any]:
        firmada = self.esta_firmada()
        return {
            "numero": self.numero,
            "movil_id_ruba": self.combo_movil.currentData(),
            "chofer_id_ruba": self.selector_chofer.id_ruba(),
            "chofer_texto": self.selector_chofer.text().strip(),
            "jefe_id_ruba": self.selector_jefe.id_ruba(),
            "jefe_texto": self.selector_jefe.text().strip(),
            "jefe_grado": self.grado_jefe(),
            "fecha_salida": self.fecha_salida.value(),
            "hora_salida": self.hora_salida.value(),
            "hora_arribo": self.hora_arribo.value(),
            "fecha_llegada": self.fecha_regreso.value(),
            "hora_llegada": self.hora_regreso.value(),
            "firmada": firmada,
            "firma_validada_en": self.firma_validada_en if firmada else None,
            "bomberos": [f.datos() for f in self._filas],
        }


class PanelDotaciones(QWidget):
    """Dotaciones numeradas consecutivamente (Dotación N° 1, 2, ...)."""

    llegadas_cambiadas = Signal()  # para recalcular el fin del horario general

    def __init__(
        self, moviles: Sequence[MovilRuba], padron: Sequence[Bombero], parent: Optional[QWidget] = None,
        resolver_personal: Optional[Callable[[int], Optional[int]]] = None,
    ) -> None:
        super().__init__(parent)
        self._moviles = list(moviles)
        self._padron = list(padron)
        self._tarjetas: List[TarjetaUnidad] = []
        self._horario = HorarioServicio()
        self._resolver_personal = resolver_personal

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._layout_tarjetas = QVBoxLayout()
        self._layout_tarjetas.setSpacing(12)
        layout.addLayout(self._layout_tarjetas)

        fila = QHBoxLayout()
        self.boton_despachar = QPushButton("+ Agregar Dotación", self)
        self.boton_despachar.setObjectName("botonAhora")
        self.boton_despachar.clicked.connect(self.agregar_unidad)
        fila.addWidget(self.boton_despachar)
        self.label_resumen = QLabel("", self)
        self.label_resumen.setProperty("muted", True)
        fila.addWidget(self.label_resumen)
        fila.addStretch(1)
        layout.addLayout(fila)
        self._actualizar_resumen()

    def actualizar_moviles(self, moviles: Sequence[MovilRuba]) -> None:
        """Tras importar / eliminar / dar de baja unidades: las dotaciones
        nuevas y las abiertas ofrecen el parque actual de la base."""
        self._moviles = list(moviles)
        for tarjeta in self._tarjetas:
            tarjeta.set_moviles(self._moviles)

    def agregar_unidad(self) -> TarjetaUnidad:
        tarjeta = TarjetaUnidad(len(self._tarjetas) + 1, self._moviles, self._padron, self, self._resolver_personal)
        tarjeta.aplicar_horario(self._horario)
        tarjeta.quitar_solicitado.connect(self.quitar_unidad)
        tarjeta.dotacion_cambiada.connect(self._actualizar_resumen)
        tarjeta.llegada_editada.connect(self.llegadas_cambiadas.emit)
        self._tarjetas.append(tarjeta)
        self._layout_tarjetas.addWidget(tarjeta)
        self._actualizar_resumen()
        return tarjeta

    def quitar_unidad(self, tarjeta: TarjetaUnidad) -> None:
        if tarjeta not in self._tarjetas:
            return
        self._tarjetas.remove(tarjeta)
        tarjeta.setParent(None)
        tarjeta.deleteLater()
        for numero, restante in enumerate(self._tarjetas, start=1):
            restante.set_numero(numero)
        self._actualizar_resumen()
        self.llegadas_cambiadas.emit()

    def unidades(self) -> List[TarjetaUnidad]:
        return list(self._tarjetas)

    def llegada_mas_tardia(self) -> Optional[Tuple[date, time]]:
        """La llegada más tardía entre las cargadas a mano en cada dotación."""
        llegadas = [ll for ll in (t.llegada_propia() for t in self._tarjetas) if ll is not None]
        return max(llegadas) if llegadas else None

    def set_horario_general(self, horario: HorarioServicio) -> None:
        self._horario = horario
        for tarjeta in self._tarjetas:
            tarjeta.aplicar_horario(horario)

    def _actualizar_resumen(self) -> None:
        personas = sum(
            len([f for f in t.filas() if f.selector.text().strip()])
            + bool(t.selector_chofer.text().strip()) + bool(t.selector_jefe.text().strip())
            for t in self._tarjetas
        )
        self.label_resumen.setText(
            f"{len(self._tarjetas)} dotación(es) · {personas} bombero(s) al lugar" if self._tarjetas
            else "Todavía no se despachó ninguna dotación."
        )

    def datos(self) -> List[Dict[str, Any]]:
        return [t.datos() for t in self._tarjetas]

    def validar(self, parcial: bool = False) -> List[str]:
        """`parcial=True` (servicio en curso): se puede guardar sin chofer, sin
        Jefe y sin llegada, pero no con datos inconsistentes."""
        errores: List[str] = []
        unidades = self.datos()
        if not unidades and not parcial:
            errores.append("Agregá al menos una Dotación.")
        moviles = set()
        for u in unidades:
            n = u["numero"]
            if u["movil_id_ruba"] is None:
                errores.append(f"Dotación N° {n}: elegí el móvil.")
            elif u["movil_id_ruba"] in moviles:
                errores.append(f"Dotación N° {n}: el móvil ya está en otra dotación.")
            moviles.add(u["movil_id_ruba"])
            for rol, clave in (("Chofer", "chofer"), ("Jefe de Dotación", "jefe")):
                if u[f"{clave}_id_ruba"] is None and (u[f"{clave}_texto"] or not parcial):
                    errores.append(f"Dotación N° {n}: elegí el {rol} del padrón" + (
                        f" ('{u[f'{clave}_texto']}' no coincide con nadie)." if u[f"{clave}_texto"] else "."))
            for i, b in enumerate(u["bomberos"], start=1):
                if b["texto"] and b["id_ruba"] is None:
                    errores.append(f"Dotación N° {n}, embarcado {i}: '{b['texto']}' no coincide con nadie del padrón.")
            if len([b for b in u["bomberos"] if b["id_ruba"]]) > MAX_EMBARCADOS_PCD2:
                errores.append(f"Dotación N° {n}: la PCD2 admite hasta {MAX_EMBARCADOS_PCD2} embarcados.")
            if (u["fecha_salida"] and u["hora_salida"] and u["fecha_llegada"] and u["hora_llegada"]
                    and (u["fecha_llegada"], u["hora_llegada"]) < (u["fecha_salida"], u["hora_salida"])):
                errores.append(f"Dotación N° {n}: la llegada es anterior a la salida.")
            if not parcial:
                if u["hora_salida"] is None:
                    errores.append(f"Dotación N° {n}: falta la hora de salida.")
                if u["hora_llegada"] is None:
                    errores.append(f"Dotación N° {n}: falta la hora de llegada "
                                   "(si sigue en el lugar, guardá como \"Salida en Curso\").")
        return errores

    def limpiar(self) -> None:
        for tarjeta in list(self._tarjetas):
            self.quitar_unidad(tarjeta)

    def cargar(self, unidades: List[Dict[str, Any]], general: HorarioServicio) -> None:
        """Repone las dotaciones de un servicio guardado (con su firma si la tenía)."""
        self.limpiar()
        self.set_horario_general(general)
        ids = {u["movil_id_ruba"] for u in unidades if u.get("movil_id_ruba") is not None}
        if ids - {m.id_ruba for m in self._moviles}:
            # El parte usa una unidad que hoy está de baja: se la ofrece (marcada)
            # para no perderla al volver a guardar.
            from app.db import moviles_para_despacho

            self.actualizar_moviles(moviles_para_despacho(incluir_id_ruba=tuple(ids)))
        for u in unidades:
            tarjeta = self.agregar_unidad()
            tarjeta.combo_movil.setCurrentIndex(max(tarjeta.combo_movil.findData(u["movil_id_ruba"]), 0))
            tarjeta.selector_chofer.set_id_ruba(u.get("chofer_id_ruba"))
            tarjeta.selector_jefe.set_id_ruba(u.get("jefe_id_ruba"))
            tarjeta.cargar_horario(HorarioServicio(u["fecha_salida"], u["hora_salida"],
                                                   u["fecha_llegada"], u["hora_llegada"]), general)
            tarjeta.hora_arribo.set_value(u.get("hora_arribo"))
            for b in u["bomberos"]:
                tarjeta.agregar_bombero().selector.set_id_ruba(b["id_ruba"])
            if u.get("firmada") and u.get("jefe_id_ruba") is not None:
                tarjeta._marcar_firmado(u["jefe_id_ruba"], u.get("firma_validada_en") or datetime.now())


# ---------------------------------------------------------------------------
# Personal en Base (Guardia / Reserva)
# ---------------------------------------------------------------------------

class PanelPersonalBase(QFrame):
    def __init__(self, padron: Sequence[Bombero], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaUnidad")
        self._padron = list(padron)
        self._apresto: List[SelectorBombero] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        titulo = QLabel("🏠  Personal en Base (Guardia / Reserva) · computa como Apresto", self)
        titulo.setObjectName("tituloUnidad")
        layout.addWidget(titulo)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("Operador de Guardia 1", self), 0, 0)
        grid.addWidget(QLabel("Operador de Guardia 2", self), 0, 1)
        self.selector_operador_1 = SelectorBombero(self._padron, self)
        self.selector_operador_2 = SelectorBombero(self._padron, self)
        grid.addWidget(self.selector_operador_1, 1, 0)
        grid.addWidget(self.selector_operador_2, 1, 1)
        layout.addLayout(grid)

        subtitulo = QLabel(f"Personal en Apresto / Reserva (hasta {MAX_APRESTO_PCS} en la PCS)", self)
        subtitulo.setObjectName("subtituloBloque")
        layout.addWidget(subtitulo)
        self.tabla = _tabla(self, ["Apellido y Nombre", ""], estirar=(0,), anchos={1: 44})
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar en apresto", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_apresto)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)

    def agregar_apresto(self) -> SelectorBombero:
        selector = SelectorBombero(self._padron, self.tabla)
        quitar = _boton_quitar(self.tabla, "Quitar de la reserva")
        quitar.clicked.connect(lambda _=False, s=selector: self._quitar(s))
        fila = self.tabla.rowCount()
        self.tabla.insertRow(fila)
        self.tabla.setCellWidget(fila, 0, selector)
        self.tabla.setCellWidget(fila, 1, quitar)
        self._apresto.append(selector)
        _ajustar_alto(self.tabla)
        return selector

    def _quitar(self, selector: SelectorBombero) -> None:
        if selector in self._apresto:
            self.tabla.removeRow(self._apresto.index(selector))
            self._apresto.remove(selector)
            _ajustar_alto(self.tabla)

    def datos(self) -> Dict[str, Any]:
        return {
            "operador_1": self.selector_operador_1.id_ruba(),
            "operador_2": self.selector_operador_2.id_ruba(),
            "apresto": [s.id_ruba() for s in self._apresto if s.id_ruba() is not None],
            "textos_invalidos": [
                s.text().strip() for s in (self.selector_operador_1, self.selector_operador_2, *self._apresto)
                if s.text().strip() and s.id_ruba() is None
            ],
        }

    def validar(self) -> List[str]:
        datos = self.datos()
        errores = [f"Personal en base: '{t}' no coincide con nadie del padrón." for t in datos["textos_invalidos"]]
        if len(datos["apresto"]) > MAX_APRESTO_PCS:
            errores.append(f"Personal en base: la PCS admite hasta {MAX_APRESTO_PCS} en apresto.")
        return errores

    def limpiar(self) -> None:
        self.selector_operador_1.clear()
        self.selector_operador_2.clear()
        for selector in list(self._apresto):
            self._quitar(selector)

    def cargar(self, datos: Dict[str, Any]) -> None:
        self.limpiar()
        self.selector_operador_1.set_id_ruba(datos.get("operador_1"))
        self.selector_operador_2.set_id_ruba(datos.get("operador_2"))
        for id_ruba in datos.get("apresto", []):
            self.agregar_apresto().set_id_ruba(id_ruba)


def personas_repetidas(unidades: List[Dict[str, Any]], base: Dict[str, Any]) -> List[str]:
    """Nadie puede figurar dos veces en el servicio (entre dotaciones y base)."""
    lugares: Dict[int, List[str]] = {}
    for u in unidades:
        n = u["numero"]
        if u["chofer_id_ruba"] is not None:
            lugares.setdefault(u["chofer_id_ruba"], []).append(f"chofer de la Dotación N° {n}")
        if u["jefe_id_ruba"] is not None:
            lugares.setdefault(u["jefe_id_ruba"], []).append(f"Jefe de la Dotación N° {n}")
        for b in u["bomberos"]:
            if b["id_ruba"] is not None:
                lugares.setdefault(b["id_ruba"], []).append(f"embarcado en la Dotación N° {n}")
    for clave, rotulo in (("operador_1", "Operador 1"), ("operador_2", "Operador 2")):
        if base.get(clave) is not None:
            lugares.setdefault(base[clave], []).append(rotulo)
    for id_ruba in base.get("apresto", []):
        lugares.setdefault(id_ruba, []).append("apresto en base")
    return [f"Una misma persona figura dos veces ({' y '.join(v)})." for v in lugares.values() if len(v) > 1]

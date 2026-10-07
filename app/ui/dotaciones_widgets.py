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

VALIDACIÓN EN CALIENTE (sin esperar al guardado):
  - Conflicto de personal: al elegir un bombero (chofer, Jefe o embarcado)
    que ya está en otra dotación, el campo se marca en rojo y la tarjeta
    avisa "El bombero X ya se encuentra asignado al Móvil Y en esa franja
    horaria" si los rangos [salida, regreso] se solapan (app/core/horarios),
    o que ya figura en otra franja si no. El guardado lo sigue bloqueando
    (personas_repetidas).
  - Salida vs. regreso del móvil: si el regreso queda antes de la salida,
    los campos se marcan en rojo y se ofrece "Regresó al día siguiente"
    (cruce de medianoche). La validación del guardado lo bloquea.

MODO GUARDIA ÁGIL: cada tarjeta tiene un campo "⚡ Carga rápida" que filtra
el padrón por legajo o por prefijo de apellido / nombre. Enter agrega al
bombero como embarcado y deja el campo vacío con el foco puesto, así se
cargan "12 [Enter] 45 [Enter] 08 [Enter]" sin el mouse. Escape (con el
campo vacío) o Ctrl+Retroceso quita al último embarcado.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
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
from app.core.horarios import combinar, fecha_fin_ajustada
from app.db import get_session
from app.models import Personal
from app.services.personal_info import bombero_historico, grado_de
from app.services.personal_service import verificar_pin_personal
from app.ui import theme
from app.ui.damnificados_widgets import _ajustar_alto, _boton_quitar, _tabla
from app.ui.participacion_widgets import HorarioServicio, SelectorBombero, texto_bombero, texto_movil
from app.ui.widgets import DateField, TimeField

COL_BOMBERO, COL_QUITAR = range(2)
MAX_EMBARCADOS_PCD2 = 10
MAX_APRESTO_PCS = 26


MAX_SUGERENCIAS_RAPIDAS = 12


def _clave(texto: str) -> str:
    """Mayúsculas y sin acentos, para comparar 'Gomez' con 'GÓMEZ'."""
    sin_acentos = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in sin_acentos if not unicodedata.combining(c)).upper().strip()


def _legajo_normalizado(legajo: Optional[str]) -> str:
    """'008' y '8' son el mismo legajo."""
    texto = (legajo or "").strip()
    return texto.lstrip("0") or ("0" if texto else "")


def buscar_en_padron(padron: Sequence[Bombero], texto: str, limite: int = MAX_SUGERENCIAS_RAPIDAS) -> List[Bombero]:
    """Coincidencias para la carga rápida, en este orden: legajo exacto,
    legajo que empieza con lo tipeado, apellido que empieza con lo tipeado y
    nombre que empieza con lo tipeado (sin distinguir mayúsculas ni acentos)."""
    buscado = _clave(texto)
    if not buscado:
        return []
    es_numero = buscado.isdigit()
    legajo_buscado = _legajo_normalizado(buscado) if es_numero else ""
    grupos: List[List[Bombero]] = [[], [], [], []]
    for b in padron:
        legajo = _legajo_normalizado(b.legajo)
        if es_numero and legajo:
            if legajo == legajo_buscado:
                grupos[0].append(b)
            elif legajo.startswith(legajo_buscado) or (b.legajo or "").strip().startswith(buscado):
                grupos[1].append(b)
        elif not es_numero:
            if _clave(b.apellido).startswith(buscado) or _clave(b.nombre_completo).startswith(buscado):
                grupos[2].append(b)
            elif any(_clave(parte).startswith(buscado) for parte in (b.nombre or "").split()):
                grupos[3].append(b)
    resultado = [b for grupo in grupos for b in sorted(grupo, key=lambda x: (x.apellido, x.nombre))]
    return resultado[:limite]


class CampoCargaRapida(QLineEdit):
    """Entrada de teclado del Modo Guardia Ágil: sugiere bomberos del padrón
    por legajo o prefijo de apellido / nombre, con la primera sugerencia ya
    resaltada; Enter la elige. Escape con el campo vacío (o Ctrl+Retroceso)
    pide quitar al último cargado."""

    elegido = Signal(int)          # id_ruba
    quitar_ultimo = Signal()

    def __init__(self, padron: Sequence[Bombero], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._padron = list(padron)
        self._id_por_texto: Dict[str, int] = {}
        self._modelo = QStringListModel(self)
        self._completer = QCompleter(self._modelo, self)
        # El filtrado lo hace buscar_en_padron(); el completer solo muestra la lista.
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(MAX_SUGERENCIAS_RAPIDAS)
        self._completer.setWidget(self)
        self._completer.activated[str].connect(self._on_activado)
        self.setPlaceholderText("⚡ Carga rápida: legajo o apellido + Enter  ·  Esc quita el último")
        self.setToolTip("Modo Guardia Ágil: tipeá legajo o apellido y Enter para embarcar; "
                        "el foco queda acá para el siguiente. Esc (campo vacío) o Ctrl+Retroceso "
                        "quita al último embarcado.")
        self._pendiente: Optional[int] = None
        self.textEdited.connect(self._actualizar_sugerencias)
        self.returnPressed.connect(self._on_enter)

    def set_padron(self, padron: Sequence[Bombero]) -> None:
        self._padron = list(padron)
        self._actualizar_sugerencias(self.text())

    def _actualizar_sugerencias(self, texto: str) -> None:
        candidatos = buscar_en_padron(self._padron, texto)
        self._id_por_texto = {texto_bombero(b): b.id_ruba for b in candidatos}
        self._modelo.setStringList(list(self._id_por_texto))
        theme.marcar_invalido(self, bool(texto.strip()) and not candidatos)
        if not candidatos:
            self._completer.popup().hide()
            return
        self._completer.complete()
        popup = self._completer.popup()
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))

    def _on_activado(self, texto: str) -> None:
        id_ruba = self._id_por_texto.get(texto)
        if id_ruba is not None:
            # Se emite al volver al event loop, cuando el completer terminó con la tecla.
            self._pendiente = id_ruba
            QTimer.singleShot(0, self._emitir_pendiente)

    def _emitir_pendiente(self) -> None:
        id_ruba, self._pendiente = self._pendiente, None
        if id_ruba is not None:
            self._emitir(id_ruba)

    def _on_enter(self) -> None:
        """Enter con el popup cerrado: legajo exacto o única coincidencia."""
        texto = self.text().strip()
        if not texto or self._pendiente is not None:  # ya lo resuelve la sugerencia elegida
            return
        if texto in self._id_por_texto:
            self._emitir(self._id_por_texto[texto])
            return
        candidatos = buscar_en_padron(self._padron, texto)
        exacto = [b for b in candidatos if texto.isdigit() and _legajo_normalizado(b.legajo) == _legajo_normalizado(texto)]
        elegido = exacto[:1] or (candidatos if len(candidatos) == 1 else [])
        if elegido:
            self._emitir(elegido[0].id_ruba)
        else:
            self._actualizar_sugerencias(texto)

    def _emitir(self, id_ruba: int) -> None:
        self._completer.popup().hide()
        self.clear()
        theme.marcar_invalido(self, False)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.elegido.emit(id_ruba)

    def keyPressEvent(self, evento: QKeyEvent) -> None:  # noqa: N802 - override de Qt
        teclas_del_popup = (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
        if self._completer.popup().isVisible() and evento.key() in teclas_del_popup:
            if evento.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and self._pendiente is None:
                # Enter con la lista abierta: la sugerencia resaltada (la 1ra por defecto).
                indice = self._completer.popup().currentIndex()
                id_ruba = self._id_por_texto.get(indice.data()) if indice.isValid() else None
                if id_ruba is not None:
                    self._emitir(id_ruba)
                    return
            evento.ignore()  # el resto lo resuelve el completer (cerrar la lista, Tab)
            return
        quitar = (evento.key() == Qt.Key.Key_Escape and not self.text()) or (
            evento.key() == Qt.Key.Key_Backspace and evento.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if quitar:
            self.quitar_ultimo.emit()
            return
        if evento.key() == Qt.Key.Key_Escape:
            self.clear()
            theme.marcar_invalido(self, False)
            return
        super().keyPressEvent(evento)


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
    horario_cambiado = Signal()          # cualquier cambio de salida / regreso (revisa solapamientos)

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
        # Lo inyecta PanelDotaciones: (id_ruba, tarjeta) -> aviso de conflicto o None.
        self.conflicto_de: Optional[Callable[[int, "TarjetaUnidad"], Optional[str]]] = None

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
        self.combo_movil.currentIndexChanged.connect(lambda _i: self.dotacion_cambiada.emit())
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

        # Regreso anterior a la salida: aviso + atajo para confirmar el cruce de medianoche.
        fila_horario = QHBoxLayout()
        self.label_horario = QLabel("", self)
        self.label_horario.setWordWrap(True)
        theme.set_tono(self.label_horario, "error")
        self.boton_dia_siguiente = QPushButton("Regresó al día siguiente", self)
        self.boton_dia_siguiente.setObjectName("botonAhora")
        self.boton_dia_siguiente.setToolTip("Confirma el cruce de medianoche: la fecha de llegada pasa al día siguiente")
        self.boton_dia_siguiente.clicked.connect(self._confirmar_dia_siguiente)
        fila_horario.addWidget(self.label_horario, 1)
        fila_horario.addWidget(self.boton_dia_siguiente)
        self._fila_horario = fila_horario

        grid.addWidget(QLabel(theme.etiqueta_requerida("Jefe de Dotación"), self), 4, 0, 1, 3)
        grid.addWidget(QLabel("Grado / cargo (del padrón)", self), 4, 3, 1, 2)
        self.selector_jefe = SelectorBombero(self._padron, self)
        self.selector_jefe.cambiado.connect(self._on_jefe_cambiado)
        self.label_grado = QLabel("—", self)
        self.label_grado.setObjectName("valorDatoClima")
        grid.addWidget(self.selector_jefe, 5, 0, 1, 3)
        grid.addWidget(self.label_grado, 5, 3, 1, 2)
        grid.addLayout(self._fila_horario, 6, 0, 1, 5)
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
        self.campo_rapido = CampoCargaRapida(self._padron, self)
        self.campo_rapido.elegido.connect(self._agregar_rapido)
        self.campo_rapido.quitar_ultimo.connect(self.quitar_ultimo_bombero)
        layout.addWidget(self.campo_rapido)
        self.label_conflicto = QLabel("", self)
        self.label_conflicto.setWordWrap(True)
        theme.set_tono(self.label_conflicto, "error")
        self.label_conflicto.hide()
        layout.addWidget(self.label_conflicto)
        self._texto_aviso_rapido = ""
        self._timer_aviso_rapido = QTimer(self)  # muere con la tarjeta: nunca dispara sobre un widget borrado
        self._timer_aviso_rapido.setSingleShot(True)
        self._timer_aviso_rapido.setInterval(4000)
        self._timer_aviso_rapido.timeout.connect(self._limpiar_aviso_rapido)
        self.tabla = _tabla(self, ["Apellido y Nombre", ""], estirar=(COL_BOMBERO,), anchos={COL_QUITAR: 44})
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar Bombero", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_bombero)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)
        self.set_numero(numero)

        # Teclado: Enter en Chofer pasa al Jefe y Enter en el Jefe a la carga rápida.
        self.selector_chofer.returnPressed.connect(lambda: self._siguiente(self.selector_chofer, self.selector_jefe))
        self.selector_jefe.returnPressed.connect(lambda: self._siguiente(self.selector_jefe, self.campo_rapido))
        self._revisar_horario()

    @staticmethod
    def _siguiente(actual: SelectorBombero, destino: QWidget) -> None:
        if actual.id_ruba() is not None:
            destino.setFocus(Qt.FocusReason.TabFocusReason)

    def destino(self, contraccion: str) -> str:
        """Para los avisos: 'al Móvil 12 (Dotación N° 1)' / 'en el Móvil ...',
        o 'a la Dotación N° 1' / 'en la Dotación ...' si todavía no hay móvil.
        `contraccion`: "a" o "en"."""
        if self.combo_movil.currentData() is None:
            return f"{contraccion} la Dotación N° {self.numero}"
        movil = self.combo_movil.currentText().split(" — ")[0]
        articulo = "al" if contraccion == "a" else "en el"
        return f"{articulo} Móvil {movil} (Dotación N° {self.numero})"

    def horario_cargado(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        """[salida, regreso] tal como están en los campos. La fecha de llegada
        ya viene ajustada por la regla de la medianoche (_ajustar_fecha_llegada)
        salvo que se la haya fijado a mano: por eso acá NO se vuelve a aplicar
        horarios.rango(), que escondería un regreso anterior a la salida."""
        return (combinar(self.fecha_salida.value(), self.hora_salida.value()),
                combinar(self.fecha_regreso.value(), self.hora_regreso.value()))

    def rango_horario(self) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Franja para detectar solapamientos: un regreso inválido (anterior a
        la salida) o sin cargar deja la franja abierta."""
        salida, regreso = self.horario_cargado()
        return salida, (regreso if salida is not None and regreso is not None and regreso >= salida else None)

    def selectores_personal(self) -> List[Tuple[str, SelectorBombero]]:
        return [("chofer", self.selector_chofer), ("Jefe de Dotación", self.selector_jefe),
                *(("embarcado", f.selector) for f in self._filas)]
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

    def set_conflictos(self, avisos: Dict[SelectorBombero, str]) -> None:
        """Marca en rojo (con tooltip) a cada selector en conflicto y lista los
        avisos en la tarjeta. Los selectores sin conflicto vuelven a su estado
        normal (si su texto es una persona del padrón)."""
        for _rol, selector in self.selectores_personal():
            aviso = avisos.get(selector)
            if aviso:
                theme.marcar_invalido(selector, True)
                selector.setToolTip(aviso)
            elif selector.id_ruba() is not None or selector.esta_vacio():
                theme.marcar_invalido(selector, False)
                selector.setToolTip("")
        textos = list(dict.fromkeys(avisos.values()))
        self.label_conflicto.setText("\n".join(f"⚠ {t}" for t in textos))
        self.label_conflicto.setVisible(bool(textos))
    # -- Horarios ---------------------------------------------------------------

    def _campos_horarios(self) -> Dict[str, QWidget]:
        return {"fecha_salida": self.fecha_salida, "hora_salida": self.hora_salida,
                "fecha_llegada": self.fecha_regreso, "hora_llegada": self.hora_regreso}

    def _marcar_editado(self, nombre: str) -> None:
        self._editados.add(nombre)
        self._ajustar_fecha_llegada()
        self._revisar_horario()
        if nombre in ("fecha_llegada", "hora_llegada", "fecha_salida", "hora_salida"):
            self.llegada_editada.emit()
        self.horario_cambiado.emit()

    def problema_horario(self) -> Optional[str]:
        """Motivo por el que el regreso no cierra contra la salida, o None."""
        salida, regreso = self.horario_cargado()
        if salida is None or regreso is None or regreso >= salida:
            return None
        return (f"El regreso ({regreso:%d/%m %H:%M}) es anterior a la salida ({salida:%d/%m %H:%M}). "
                "Corregilo o, si cruzó la medianoche, confirmá que regresó al día siguiente.")

    def _revisar_horario(self) -> None:
        """Borde rojo en la llegada y aviso con el atajo de 'día siguiente'."""
        problema = self.problema_horario()
        for campo in (self.fecha_regreso.date_edit, self.hora_regreso.time_edit):
            theme.marcar_invalido(campo, bool(problema))
            campo.setToolTip(problema or "")
        self.label_horario.setText(f"⚠ {problema}" if problema else "")
        self.label_horario.setVisible(bool(problema))
        self.boton_dia_siguiente.setVisible(bool(problema))

    def _confirmar_dia_siguiente(self) -> None:
        salida = self.fecha_salida.value()
        if salida is None:
            return
        self.fecha_regreso.set_value(salida + timedelta(days=1))
        self._marcar_editado("fecha_llegada")

    def _ajustar_fecha_llegada(self) -> None:
        """Fecha + hora completas: si esta dotación tiene horario propio y la
        fecha de llegada no se tocó a mano, se deduce de la salida -- el mismo
        día, o el siguiente si cruzó la medianoche (sale 23:30, vuelve 01:15).
        Sin horario propio sigue la fecha del horario general."""
        if "fecha_llegada" in self._editados:
            return
        if not {"fecha_salida", "hora_salida", "hora_llegada"} & self._editados:
            return
        nueva = fecha_fin_ajustada(self.fecha_salida.value(), self.hora_salida.value(),
                                   None, self.hora_regreso.value())
        if nueva is not None and nueva != self.fecha_regreso.value():
            self.fecha_regreso.set_value(nueva)

    def llegada_propia(self) -> Optional[Tuple[date, time]]:
        """(fecha, hora) de llegada cargada a mano en ESTA dotación (no la
        heredada del horario general), o None. La fecha ya contempla el cruce
        de medianoche, así el máximo entre dotaciones es cronológico."""
        hora = self.hora_regreso.value()
        if not {"hora_llegada", "fecha_llegada"} & self._editados or hora is None:
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
        self._ajustar_fecha_llegada()
        self._revisar_horario()

    def cargar_horario(self, valores: HorarioServicio, general: HorarioServicio) -> None:
        for nombre, campo in self._campos_horarios().items():
            valor = getattr(valores, nombre)
            if valor is None and isinstance(campo, DateField):
                continue
            campo.set_value(valor)
            if valor != getattr(general, nombre):
                self._editados.add(nombre)
        self._revisar_horario()

    # -- Jefe, grado y firma ----------------------------------------------------

    def set_padron(self, padron: Sequence[Bombero]) -> None:
        """Padrón recargado (🔄 Refrescar / baja de un bombero): nuevas opciones
        en todos los selectores, conservando a quien ya estaba elegido."""
        self._padron = list(padron)
        self._por_id = {b.id_ruba: b for b in self._padron}
        for selector in (self.selector_chofer, self.selector_jefe, *(f.selector for f in self._filas)):
            selector.set_padron(self._padron)
        self.campo_rapido.set_padron(self._padron)

    def _on_jefe_cambiado(self) -> None:
        id_jefe = self.selector_jefe.id_ruba()
        jefe = self._por_id.get(id_jefe) or (bombero_historico(id_jefe) if id_jefe is not None else None)
        self.label_grado.setText(grado_de(jefe) or "—")
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
            tiene_firma = bool(persona.ruta_firma) and Path(persona.ruta_firma).exists()
        if not tiene_firma:
            return "No tiene firma registrada en su legajo (Documentación)."
        if not verificar_pin_personal(personal_id, pin):
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

    def _agregar_rapido(self, id_ruba: int) -> None:
        """Enter en la carga rápida: embarca al bombero (reusando una fila
        vacía si la hay) salvo que ya esté en esta dotación, en otra con la
        franja solapada o que la PCD2 esté completa. El foco queda en el campo."""
        nombre = self._texto_de(id_ruba)
        en_esta = [rol for rol, sel in self.selectores_personal() if sel.id_ruba() == id_ruba]
        if en_esta:
            self._aviso_rapido(f"{nombre} ya está en esta dotación ({en_esta[0]}).")
            return
        aviso = self.conflicto_de(id_ruba, self) if self.conflicto_de else None
        if aviso:
            self._aviso_rapido(aviso)
            return
        vacia = next((f for f in self._filas if f.selector.esta_vacio()), None)
        if vacia is None and len(self._filas) >= MAX_EMBARCADOS_PCD2:
            self._aviso_rapido(f"La PCD2 admite hasta {MAX_EMBARCADOS_PCD2} embarcados por dotación.")
            return
        (vacia or self.agregar_bombero()).selector.set_id_ruba(id_ruba)
        self.campo_rapido.setFocus(Qt.FocusReason.OtherFocusReason)

    def _texto_de(self, id_ruba: int) -> str:
        bombero = self._por_id.get(id_ruba)
        return bombero.nombre_completo if bombero else f"Id {id_ruba}"

    def _aviso_rapido(self, texto: str) -> None:
        """Rechazo de la carga rápida: campo en rojo con el motivo (sin modal,
        para no cortar el tipeo)."""
        theme.marcar_invalido(self.campo_rapido, True)
        self.campo_rapido.setToolTip(texto)
        self._texto_aviso_rapido = f"⚠ {texto}"
        self.label_conflicto.setText(self._texto_aviso_rapido)
        self.label_conflicto.show()
        self._timer_aviso_rapido.start()

    def _limpiar_aviso_rapido(self) -> None:
        theme.marcar_invalido(self.campo_rapido, False)
        if self.label_conflicto.text() == self._texto_aviso_rapido:
            self.label_conflicto.hide()

    def quitar_ultimo_bombero(self) -> None:
        """Esc / Ctrl+Retroceso en la carga rápida: quita al último embarcado."""
        if self._filas:
            self.quitar_bombero(self._filas[-1])
        self.campo_rapido.setFocus(Qt.FocusReason.OtherFocusReason)

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

    def actualizar_padron(self, padron: Sequence[Bombero]) -> None:
        self._padron = list(padron)
        for tarjeta in self._tarjetas:
            tarjeta.set_padron(self._padron)

    def agregar_unidad(self) -> TarjetaUnidad:
        tarjeta = TarjetaUnidad(len(self._tarjetas) + 1, self._moviles, self._padron, self, self._resolver_personal)
        tarjeta.conflicto_de = self.conflicto_de
        tarjeta.aplicar_horario(self._horario)
        tarjeta.quitar_solicitado.connect(self.quitar_unidad)
        tarjeta.dotacion_cambiada.connect(self._actualizar_resumen)
        tarjeta.dotacion_cambiada.connect(self.revisar_conflictos)
        tarjeta.horario_cambiado.connect(self.revisar_conflictos)
        tarjeta.llegada_editada.connect(self.llegadas_cambiadas.emit)
        self._tarjetas.append(tarjeta)
        self._layout_tarjetas.addWidget(tarjeta)
        self._actualizar_resumen()
        return tarjeta

    # -- Conflictos de personal entre dotaciones (en caliente) --------------------

    @staticmethod
    def _se_solapan(a: TarjetaUnidad, b: TarjetaUnidad) -> bool:
        """True si las franjas [salida, regreso] se pisan. Un horario
        incompleto (sin regreso todavía) cuenta como abierto: se solapa."""
        (a_ini, a_fin), (b_ini, b_fin) = a.rango_horario(), b.rango_horario()
        if a_ini is None or b_ini is None:
            return True
        a_fin = a_fin or datetime.max
        b_fin = b_fin or datetime.max
        return a_ini < b_fin and b_ini < a_fin

    def _aviso(self, nombre: str, otra: TarjetaUnidad, rol: str, solapa: bool) -> str:
        if solapa:
            return (f"El bombero {nombre} ya se encuentra asignado {otra.destino('a')} "
                    f"como {rol} en esa franja horaria.")
        return (f"El bombero {nombre} ya figura {otra.destino('en')} como {rol} "
                "(otra franja horaria): cada persona va una sola vez por parte.")

    def conflicto_de(self, id_ruba: int, tarjeta: TarjetaUnidad) -> Optional[str]:
        """Aviso si `id_ruba` ya está en OTRA dotación del parte, o None."""
        for otra in self._tarjetas:
            if otra is tarjeta:
                continue
            for rol, selector in otra.selectores_personal():
                if selector.id_ruba() == id_ruba:
                    return self._aviso(tarjeta._texto_de(id_ruba), otra, rol, self._se_solapan(tarjeta, otra))
        return None

    def revisar_conflictos(self) -> None:
        """Recorre todas las dotaciones y marca a quien figura en más de un
        lugar (en la misma dotación o en otra, solapada o no)."""
        ubicaciones: Dict[int, List[Tuple[TarjetaUnidad, str, SelectorBombero]]] = {}
        for tarjeta in self._tarjetas:
            for rol, selector in tarjeta.selectores_personal():
                id_ruba = selector.id_ruba()
                if id_ruba is not None:
                    ubicaciones.setdefault(id_ruba, []).append((tarjeta, rol, selector))
        avisos: Dict[TarjetaUnidad, Dict[SelectorBombero, str]] = {t: {} for t in self._tarjetas}
        for id_ruba, lugares in ubicaciones.items():
            if len(lugares) < 2:
                continue
            for tarjeta, rol, selector in lugares[1:]:  # el primero en cargarse queda como válido
                origen, rol_origen, _ = lugares[0]
                nombre = tarjeta._texto_de(id_ruba)
                if origen is tarjeta:
                    aviso = f"{nombre} ya está en esta dotación como {rol_origen}."
                else:
                    aviso = self._aviso(nombre, origen, rol_origen, self._se_solapan(tarjeta, origen))
                avisos[tarjeta][selector] = aviso
        for tarjeta, de_tarjeta in avisos.items():
            tarjeta.set_conflictos(de_tarjeta)

    def quitar_unidad(self, tarjeta: TarjetaUnidad) -> None:
        if tarjeta not in self._tarjetas:
            return
        self._tarjetas.remove(tarjeta)
        tarjeta.setParent(None)
        tarjeta.deleteLater()
        for numero, restante in enumerate(self._tarjetas, start=1):
            restante.set_numero(numero)
        self._actualizar_resumen()
        self.revisar_conflictos()
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
        self.revisar_conflictos()

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
                errores.append(f"Dotación N° {n}: la llegada ({u['fecha_llegada']:%d/%m} {u['hora_llegada']:%H:%M}) "
                               f"es anterior a la salida ({u['fecha_salida']:%d/%m} {u['hora_salida']:%H:%M}); "
                               "si cruzó la medianoche, la fecha de llegada es la del día siguiente.")
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

    def set_padron(self, padron: Sequence[Bombero]) -> None:
        self._padron = list(padron)
        for selector in (self.selector_operador_1, self.selector_operador_2, *self._apresto):
            selector.set_padron(self._padron)

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

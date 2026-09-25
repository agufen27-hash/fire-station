"""
Sección Damnificados: tres tarjetas con conmutador No / Sí (No por defecto).

  a) Personas civiles  -> cantidad + grilla (Nombre, Apellido, DNI, Género, Lesión).
                          Las cantidades que pide RUBA (heridos / fallecidos /
                          desaparecidos) se derivan de la columna Lesión.
  b) Bienes / vehículos -> grilla (Tipo, Descripción, Titular, Seguro).
  c) Bomberos           -> grilla (Bombero del padrón, Detalle de atención médica).
  d) Vehículos (solo Accidentes) -> grilla (Marca, Dominio, Modelo, Año, Asegurado,
                          Aseguradora, Póliza) = "Vehículos damnificados" de RUBA.

Si una tarjeta está en "No" su contenido se conserva en pantalla (por si se
tocó sin querer) pero NO se guarda ni se envía.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.catalogos import Bombero, leer_mapping
from app.models import CondicionDamnificado
from app.ui import theme
from app.ui.participacion_widgets import SelectorBombero
from app.ui.siniestro_widgets import _poblar_combo, opciones_genero

# Lesión -> condición que entiende RUBA (heridos / fallecidos / desaparecidos).
LESIONES: Dict[str, str] = {
    "Herido leve": CondicionDamnificado.HERIDO.value,
    "Herido grave": CondicionDamnificado.HERIDO.value,
    "Fallecido": CondicionDamnificado.FALLECIDO.value,
    "Desaparecido": CondicionDamnificado.DESAPARECIDO.value,
}
TIPOS_BIEN = ["Inmueble", "Rodado", "Rastrojo", "Otro"]
ALTO_FILA = 40


def _tabla(parent: QWidget, encabezados: List[str], estirar: Sequence[int],
           anchos: Optional[Dict[int, int]] = None) -> QTableWidget:
    """Tabla de filas editables: las columnas `estirar` reparten el ancho;
    las de `anchos` quedan fijas (combos y campos cortos legibles)."""
    tabla = QTableWidget(0, len(encabezados), parent)
    tabla.setHorizontalHeaderLabels(encabezados)
    tabla.verticalHeader().setVisible(False)
    tabla.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    tabla.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    cabecera = tabla.horizontalHeader()
    cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for col in estirar:
        cabecera.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
    for col, ancho in (anchos or {}).items():
        cabecera.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        cabecera.resizeSection(col, ancho)
    theme.estilizar_tabla(tabla, alto_fila=ALTO_FILA)
    return tabla


def _ajustar_alto(tabla: QTableWidget) -> None:
    tabla.setFixedHeight(tabla.horizontalHeader().height() + ALTO_FILA * tabla.rowCount() + 4)


def _boton_quitar(parent: QWidget, tooltip: str) -> QPushButton:
    boton = QPushButton("✕", parent)
    boton.setObjectName("botonQuitarFila")
    boton.setToolTip(tooltip)
    boton.setFixedWidth(32)
    return boton


# ---------------------------------------------------------------------------
# Tarjeta con conmutador No / Sí
# ---------------------------------------------------------------------------

class TarjetaConmutable(QFrame):
    cambiado = Signal(bool)

    def __init__(self, icono: str, pregunta: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaConmutable")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        cabecera = QHBoxLayout()
        cabecera.setSpacing(10)
        etiqueta_icono = QLabel(icono, self)
        etiqueta_icono.setObjectName("iconoConmutable")
        cabecera.addWidget(etiqueta_icono)
        self.label_pregunta = QLabel(pregunta, self)
        self.label_pregunta.setObjectName("preguntaConmutable")
        cabecera.addWidget(self.label_pregunta)
        cabecera.addStretch(1)

        self.boton_no = QPushButton("No", self)
        self.boton_si = QPushButton("Sí", self)
        self._grupo = QButtonGroup(self)
        for boton, lado in ((self.boton_no, "izq"), (self.boton_si, "der")):
            boton.setObjectName("segmento")
            boton.setProperty("lado", lado)
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            self._grupo.addButton(boton)
            cabecera.addWidget(boton)
        cabecera.setSpacing(0)
        layout.addLayout(cabecera)

        self.contenido = QWidget(self)
        self.layout_contenido = QVBoxLayout(self.contenido)
        self.layout_contenido.setContentsMargins(0, 4, 0, 0)
        self.layout_contenido.setSpacing(10)
        layout.addWidget(self.contenido)

        self.boton_no.setChecked(True)
        self.contenido.setVisible(False)
        self.boton_si.toggled.connect(self._on_toggle)

    def _on_toggle(self, activa: bool) -> None:
        self.contenido.setVisible(activa)
        theme.set_propiedad(self, "activa", activa)
        self.cambiado.emit(activa)

    def activa(self) -> bool:
        return self.boton_si.isChecked()

    def set_activa(self, activa: bool) -> None:
        (self.boton_si if activa else self.boton_no).setChecked(True)


# ---------------------------------------------------------------------------
# a) Personas civiles
# ---------------------------------------------------------------------------

COL_C_NOMBRE, COL_C_APELLIDO, COL_C_DNI, COL_C_GENERO, COL_C_LESION = range(5)


class GrillaCiviles(QWidget):
    def __init__(self, mapping: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._opciones_genero = opciones_genero(mapping)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        fila = QHBoxLayout()
        fila.addWidget(QLabel(theme.etiqueta_requerida("Cantidad de personas"), self))
        self.spin_cantidad = QSpinBox(self)
        self.spin_cantidad.setRange(0, 99)
        self.spin_cantidad.valueChanged.connect(self.set_cantidad)
        fila.addWidget(self.spin_cantidad)
        fila.addStretch(1)
        layout.addLayout(fila)

        self.tabla = _tabla(self, ["Nombre", "Apellido", "DNI", "Género", "Lesión"], estirar=(0, 1),
                            anchos={COL_C_DNI: 130, COL_C_GENERO: 190, COL_C_LESION: 160})
        layout.addWidget(self.tabla)
        _ajustar_alto(self.tabla)

    def set_cantidad(self, cantidad: int) -> None:
        """Agrega filas vacías o quita las últimas (conserva las ya cargadas)."""
        if self.spin_cantidad.value() != cantidad:
            self.spin_cantidad.setValue(cantidad)  # vuelve a entrar por valueChanged
            return
        while self.tabla.rowCount() < cantidad:
            self._agregar_fila()
        while self.tabla.rowCount() > cantidad:
            self.tabla.removeRow(self.tabla.rowCount() - 1)
        _ajustar_alto(self.tabla)

    def _agregar_fila(self) -> None:
        fila = self.tabla.rowCount()
        self.tabla.insertRow(fila)
        for col, placeholder in ((COL_C_NOMBRE, "Nombre"), (COL_C_APELLIDO, "Apellido"), (COL_C_DNI, "DNI")):
            campo = QLineEdit(self.tabla)
            campo.setPlaceholderText(placeholder)
            self.tabla.setCellWidget(fila, col, campo)
        genero = QComboBox(self.tabla)
        _poblar_combo(genero, self._opciones_genero, "damnificados.genero_opciones")
        self.tabla.setCellWidget(fila, COL_C_GENERO, genero)
        lesion = QComboBox(self.tabla)
        lesion.addItems(list(LESIONES))
        self.tabla.setCellWidget(fila, COL_C_LESION, lesion)

    def campo(self, fila: int, col: int):
        return self.tabla.cellWidget(fila, col)

    def cargar_fila(self, fila: int, civil: Dict[str, Any]) -> None:
        for col, clave in ((COL_C_NOMBRE, "nombre"), (COL_C_APELLIDO, "apellido"), (COL_C_DNI, "dni")):
            self.campo(fila, col).setText(civil.get(clave) or "")
        genero = self.campo(fila, COL_C_GENERO)
        if civil.get("genero") is not None and genero.findData(civil["genero"]) >= 0:
            genero.setCurrentIndex(genero.findData(civil["genero"]))
        lesion = civil.get("lesion") or {  # incidentes de antes de la columna Lesión
            CondicionDamnificado.FALLECIDO.value: "Fallecido",
            CondicionDamnificado.DESAPARECIDO.value: "Desaparecido",
        }.get(civil.get("condicion"), "Herido leve")
        self.campo(fila, COL_C_LESION).setCurrentText(lesion)

    def filas(self) -> List[Dict[str, Optional[str]]]:
        resultado = []
        for f in range(self.tabla.rowCount()):
            lesion = self.campo(f, COL_C_LESION).currentText()
            resultado.append({
                "nombre": self.campo(f, COL_C_NOMBRE).text().strip() or None,
                "apellido": self.campo(f, COL_C_APELLIDO).text().strip() or None,
                "dni": self.campo(f, COL_C_DNI).text().strip() or None,
                "genero": self.campo(f, COL_C_GENERO).currentData(),
                "lesion": lesion,
                "condicion": LESIONES[lesion],
            })
        return resultado

    def limpiar(self) -> None:
        self.tabla.setRowCount(0)
        self.spin_cantidad.setValue(0)
        _ajustar_alto(self.tabla)


# ---------------------------------------------------------------------------
# b) Bienes / vehículos
# ---------------------------------------------------------------------------

COL_B_TIPO, COL_B_DESCRIPCION, COL_B_TITULAR, COL_B_SEGURO, COL_B_QUITAR = range(5)


class GrillaBienes(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.tabla = _tabla(self, ["Tipo de bien", "Descripción", "Titular", "Seguro", ""], estirar=(1, 2, 3),
                            anchos={COL_B_TIPO: 150, COL_B_QUITAR: 44})
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar bien", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_fila)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)

    def agregar_fila(self) -> None:
        fila = self.tabla.rowCount()
        self.tabla.insertRow(fila)
        tipo = QComboBox(self.tabla)
        tipo.addItems(TIPOS_BIEN)
        self.tabla.setCellWidget(fila, COL_B_TIPO, tipo)
        for col, placeholder in (
            (COL_B_DESCRIPCION, "Ej: vivienda familiar, 80 m² afectados"),
            (COL_B_TITULAR, "Apellido y nombre"),
            (COL_B_SEGURO, "Compañía / póliza (si tiene)"),
        ):
            campo = QLineEdit(self.tabla)
            campo.setPlaceholderText(placeholder)
            self.tabla.setCellWidget(fila, col, campo)
        quitar = _boton_quitar(self.tabla, "Quitar este bien")
        quitar.clicked.connect(lambda _=False, b=quitar: self._quitar(b))
        self.tabla.setCellWidget(fila, COL_B_QUITAR, quitar)
        _ajustar_alto(self.tabla)

    def cargar_fila(self, fila: int, bien: Dict[str, Any]) -> None:
        self.tabla.cellWidget(fila, COL_B_TIPO).setCurrentText(bien.get("tipo") or TIPOS_BIEN[0])
        for col, clave in ((COL_B_DESCRIPCION, "descripcion"), (COL_B_TITULAR, "titular"), (COL_B_SEGURO, "seguro")):
            self.tabla.cellWidget(fila, col).setText(bien.get(clave) or "")

    def _quitar(self, boton: QPushButton) -> None:
        for f in range(self.tabla.rowCount()):
            if self.tabla.cellWidget(f, COL_B_QUITAR) is boton:
                self.tabla.removeRow(f)
                break
        _ajustar_alto(self.tabla)

    def filas(self) -> List[Dict[str, Optional[str]]]:
        return [
            {
                "tipo": self.tabla.cellWidget(f, COL_B_TIPO).currentText(),
                "descripcion": self.tabla.cellWidget(f, COL_B_DESCRIPCION).text().strip() or None,
                "titular": self.tabla.cellWidget(f, COL_B_TITULAR).text().strip() or None,
                "seguro": self.tabla.cellWidget(f, COL_B_SEGURO).text().strip() or None,
            }
            for f in range(self.tabla.rowCount())
        ]

    def limpiar(self) -> None:
        self.tabla.setRowCount(0)
        _ajustar_alto(self.tabla)


# ---------------------------------------------------------------------------
# c) Bomberos damnificados
# ---------------------------------------------------------------------------

COL_F_BOMBERO, COL_F_DETALLE, COL_F_QUITAR = range(3)


class GrillaBomberosDamnificados(QWidget):
    def __init__(self, padron: Sequence[Bombero], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._padron = list(padron)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.tabla = _tabla(self, ["Bombero", "Detalle de atención médica", ""], estirar=(0, 1),
                            anchos={COL_F_QUITAR: 44})
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar bombero", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_fila)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)

    def agregar_fila(self) -> SelectorBombero:
        fila = self.tabla.rowCount()
        self.tabla.insertRow(fila)
        selector = SelectorBombero(self._padron, self.tabla)
        self.tabla.setCellWidget(fila, COL_F_BOMBERO, selector)
        detalle = QLineEdit(self.tabla)
        detalle.setPlaceholderText("Ej: quemadura leve en mano, atendido en el lugar por SEM")
        self.tabla.setCellWidget(fila, COL_F_DETALLE, detalle)
        quitar = _boton_quitar(self.tabla, "Quitar este bombero")
        quitar.clicked.connect(lambda _=False, b=quitar: self._quitar(b))
        self.tabla.setCellWidget(fila, COL_F_QUITAR, quitar)
        _ajustar_alto(self.tabla)
        return selector

    def _quitar(self, boton: QPushButton) -> None:
        for f in range(self.tabla.rowCount()):
            if self.tabla.cellWidget(f, COL_F_QUITAR) is boton:
                self.tabla.removeRow(f)
                break
        _ajustar_alto(self.tabla)

    def filas(self) -> List[Dict[str, Any]]:
        return [
            {
                "id_ruba": self.tabla.cellWidget(f, COL_F_BOMBERO).id_ruba(),
                "texto": self.tabla.cellWidget(f, COL_F_BOMBERO).text().strip(),
                "detalle": self.tabla.cellWidget(f, COL_F_DETALLE).text().strip() or None,
            }
            for f in range(self.tabla.rowCount())
        ]

    def limpiar(self) -> None:
        self.tabla.setRowCount(0)
        _ajustar_alto(self.tabla)


# ---------------------------------------------------------------------------
# d) Vehículos involucrados (solo Accidentes)
# ---------------------------------------------------------------------------

(COL_V_MARCA, COL_V_TIPO, COL_V_DOMINIO, COL_V_MODELO, COL_V_ANIO, COL_V_ASEGURADO,
 COL_V_ASEGURADORA, COL_V_POLIZA, COL_V_QUITAR) = range(9)

# Tipos que ofrece RUBA ("Transito > ..."); ruba_payload.tipo_vehiculo_ruba los
# traduce al value del <option>. El primero es el valor por defecto.
TIPOS_VEHICULO = ["Auto", "Camioneta / Pick-up", "Camión", "Moto", "Bicicleta", "Colectivo / Ómnibus",
                  "Micro", "Cuatriciclo", "Otro"]


def marcas_vehiculo(mapping: Dict[str, Any]) -> Dict[str, str]:
    """Marca -> ID numérico del <select> de RUBA."""
    return dict(mapping.get("selectores", {}).get("vehiculos_accidentes", {}).get("marcas") or {})


class GrillaVehiculosAccidente(QWidget):
    def __init__(self, mapping: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._marcas = marcas_vehiculo(mapping)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.tabla = _tabla(
            self, ["Marca", "Tipo", "Dominio", "Modelo", "Año", "Asegurado", "Aseguradora", "Póliza", ""],
            estirar=(COL_V_MODELO, COL_V_ASEGURADORA, COL_V_POLIZA),
            anchos={COL_V_MARCA: 160, COL_V_TIPO: 150, COL_V_DOMINIO: 110, COL_V_ANIO: 70,
                    COL_V_ASEGURADO: 90, COL_V_QUITAR: 44},
        )
        layout.addWidget(self.tabla)
        self.boton_agregar = QPushButton("+ Agregar vehículo", self)
        self.boton_agregar.setObjectName("botonAhora")
        self.boton_agregar.clicked.connect(self.agregar_fila)
        layout.addWidget(self.boton_agregar, 0, Qt.AlignmentFlag.AlignLeft)
        _ajustar_alto(self.tabla)

    def agregar_fila(self) -> None:
        fila = self.tabla.rowCount()
        self.tabla.insertRow(fila)
        marca = QComboBox(self.tabla)
        marca.addItem("Seleccionar", None)
        for nombre, id_ruba in self._marcas.items():
            marca.addItem(nombre, id_ruba)
        self.tabla.setCellWidget(fila, COL_V_MARCA, marca)
        tipo = QComboBox(self.tabla)
        tipo.addItems(TIPOS_VEHICULO)
        tipo.setToolTip("Tipo de vehículo para RUBA (Transito > ...)")
        self.tabla.setCellWidget(fila, COL_V_TIPO, tipo)
        for col, placeholder, largo in (
            (COL_V_DOMINIO, "AB123CD", 10), (COL_V_MODELO, "Ej: Cronos 1.3", 60), (COL_V_ANIO, "2019", 4),
            (COL_V_ASEGURADORA, "Compañía", 80), (COL_V_POLIZA, "N° de póliza", 40),
        ):
            campo = QLineEdit(self.tabla)
            campo.setPlaceholderText(placeholder)
            campo.setMaxLength(largo)
            self.tabla.setCellWidget(fila, col, campo)
        asegurado = QCheckBox("Sí", self.tabla)
        asegurado.toggled.connect(lambda marcado, c=asegurado: self._on_asegurado(c, marcado))
        self.tabla.setCellWidget(fila, COL_V_ASEGURADO, asegurado)
        quitar = _boton_quitar(self.tabla, "Quitar este vehículo")
        quitar.clicked.connect(lambda _=False, b=quitar: self._quitar(b))
        self.tabla.setCellWidget(fila, COL_V_QUITAR, quitar)
        self._on_asegurado(asegurado, False)
        _ajustar_alto(self.tabla)

    def _fila_de(self, widget: QWidget, col: int) -> Optional[int]:
        return next((f for f in range(self.tabla.rowCount()) if self.tabla.cellWidget(f, col) is widget), None)

    def _on_asegurado(self, check: QCheckBox, marcado: bool) -> None:
        """Aseguradora y póliza solo se habilitan si el vehículo tiene seguro."""
        fila = self._fila_de(check, COL_V_ASEGURADO)
        if fila is None:
            return
        for col in (COL_V_ASEGURADORA, COL_V_POLIZA):
            self.tabla.cellWidget(fila, col).setEnabled(marcado)

    def _quitar(self, boton: QPushButton) -> None:
        fila = self._fila_de(boton, COL_V_QUITAR)
        if fila is not None:
            self.tabla.removeRow(fila)
        _ajustar_alto(self.tabla)

    def cargar_fila(self, fila: int, vehiculo: Dict[str, Any]) -> None:
        marca = self.tabla.cellWidget(fila, COL_V_MARCA)
        marca.setCurrentIndex(max(marca.findText(vehiculo.get("marca") or ""), 0))
        tipo = self.tabla.cellWidget(fila, COL_V_TIPO)
        tipo.setCurrentIndex(max(tipo.findText(vehiculo.get("tipo") or ""), 0))  # partes viejos: Auto
        for col, clave in ((COL_V_DOMINIO, "dominio"), (COL_V_MODELO, "modelo"), (COL_V_ANIO, "anio"),
                           (COL_V_ASEGURADORA, "aseguradora"), (COL_V_POLIZA, "poliza")):
            self.tabla.cellWidget(fila, col).setText(str(vehiculo.get(clave) or ""))
        self.tabla.cellWidget(fila, COL_V_ASEGURADO).setChecked(bool(vehiculo.get("asegurado")))

    def filas(self) -> List[Dict[str, Any]]:
        resultado = []
        for f in range(self.tabla.rowCount()):
            def texto(col: int, f: int = f) -> Optional[str]:
                return self.tabla.cellWidget(f, col).text().strip() or None

            marca = self.tabla.cellWidget(f, COL_V_MARCA)
            asegurado = self.tabla.cellWidget(f, COL_V_ASEGURADO).isChecked()
            resultado.append({
                "marca": marca.currentText() if marca.currentData() is not None else None,
                "tipo": self.tabla.cellWidget(f, COL_V_TIPO).currentText(),
                "dominio": (texto(COL_V_DOMINIO) or "").replace(" ", "").upper() or None,
                "modelo": texto(COL_V_MODELO),
                "anio": texto(COL_V_ANIO),
                "asegurado": asegurado,
                "aseguradora": texto(COL_V_ASEGURADORA) if asegurado else None,
                "poliza": texto(COL_V_POLIZA) if asegurado else None,
            })
        return resultado

    def limpiar(self) -> None:
        self.tabla.setRowCount(0)
        _ajustar_alto(self.tabla)


# ---------------------------------------------------------------------------
# Panel completo
# ---------------------------------------------------------------------------

class PanelDamnificados(QWidget):
    def __init__(self, padron: Sequence[Bombero], parent: Optional[QWidget] = None,
                 mapping: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(parent)
        mapping = leer_mapping() if mapping is None else mapping
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.tarjeta_civiles = TarjetaConmutable("🧍", "¿Hubo personas civiles damnificadas?", self)
        self.grilla_civiles = GrillaCiviles(mapping, self.tarjeta_civiles.contenido)
        self.tarjeta_civiles.layout_contenido.addWidget(self.grilla_civiles)
        self.tarjeta_civiles.cambiado.connect(
            lambda activa: self.grilla_civiles.set_cantidad(max(1, self.grilla_civiles.spin_cantidad.value()))
            if activa else None
        )

        self.tarjeta_bienes = TarjetaConmutable("🏠", "¿Hubo bienes o vehículos afectados?", self)
        self.grilla_bienes = GrillaBienes(self.tarjeta_bienes.contenido)
        self.tarjeta_bienes.layout_contenido.addWidget(self.grilla_bienes)
        self.tarjeta_bienes.cambiado.connect(
            lambda activa: self.grilla_bienes.agregar_fila() if activa and not self.grilla_bienes.filas() else None
        )

        self.tarjeta_bomberos = TarjetaConmutable("🚒", "¿Hubo bomberos damnificados?", self)
        self.grilla_bomberos = GrillaBomberosDamnificados(padron, self.tarjeta_bomberos.contenido)
        self.tarjeta_bomberos.layout_contenido.addWidget(self.grilla_bomberos)
        self.tarjeta_bomberos.cambiado.connect(
            lambda activa: self.grilla_bomberos.agregar_fila() if activa and not self.grilla_bomberos.filas() else None
        )

        self.tarjeta_vehiculos = TarjetaConmutable("🚗", "¿Hubo vehículos involucrados en el accidente?", self)
        self.grilla_vehiculos = GrillaVehiculosAccidente(mapping, self.tarjeta_vehiculos.contenido)
        self.tarjeta_vehiculos.layout_contenido.addWidget(self.grilla_vehiculos)
        self.tarjeta_vehiculos.cambiado.connect(
            lambda activa: self.grilla_vehiculos.agregar_fila() if activa and not self.grilla_vehiculos.filas() else None
        )
        self.tarjeta_vehiculos.setVisible(False)  # solo para Accidentes (set_es_accidente)

        for tarjeta in (self.tarjeta_civiles, self.tarjeta_bienes, self.tarjeta_vehiculos, self.tarjeta_bomberos):
            layout.addWidget(tarjeta)

    def set_es_accidente(self, es_accidente: bool) -> None:
        """La tarjeta de vehículos solo aparece (y solo se envía) en Accidentes."""
        self.tarjeta_vehiculos.setVisible(es_accidente)

    def es_accidente(self) -> bool:
        return not self.tarjeta_vehiculos.isHidden()

    # -- Datos --------------------------------------------------------------------

    def civiles(self) -> List[Dict[str, Optional[str]]]:
        return self.grilla_civiles.filas() if self.tarjeta_civiles.activa() else []

    def bienes(self) -> List[Dict[str, Optional[str]]]:
        return self.grilla_bienes.filas() if self.tarjeta_bienes.activa() else []

    def bomberos(self) -> List[Dict[str, Any]]:
        return self.grilla_bomberos.filas() if self.tarjeta_bomberos.activa() else []

    def vehiculos(self) -> List[Dict[str, Any]]:
        if not self.es_accidente() or not self.tarjeta_vehiculos.activa():
            return []
        return self.grilla_vehiculos.filas()

    def conteos(self) -> Dict[str, int]:
        """Cantidades para RUBA / planillas, derivadas de la columna Lesión."""
        civiles = self.civiles()
        return {
            "heridos": sum(1 for c in civiles if c["condicion"] == CondicionDamnificado.HERIDO.value),
            "fallecidos": sum(1 for c in civiles if c["condicion"] == CondicionDamnificado.FALLECIDO.value),
            "desaparecidos": sum(1 for c in civiles if c["condicion"] == CondicionDamnificado.DESAPARECIDO.value),
            "bomberos": len(self.bomberos()),
        }

    def validar(self, parcial: bool = False) -> List[str]:
        """`parcial=True` (servicio en curso): solo lo que impediría guardar
        (bomberos que no están en el padrón o repetidos), no lo incompleto."""
        errores: List[str] = []
        if not parcial and self.tarjeta_civiles.activa() and not self.civiles():
            errores.append("Damnificados civiles: indicá cuántas personas (o marcá \"No\").")
        if not parcial and self.tarjeta_bienes.activa():
            if not self.bienes():
                errores.append("Bienes afectados: agregá al menos un bien (o marcá \"No\").")
            for i, bien in enumerate(self.bienes(), start=1):
                if not bien["descripcion"]:
                    errores.append(f"Bien {i}: describí el bien afectado.")
        if self.tarjeta_bomberos.activa():
            if not parcial and not self.bomberos():
                errores.append("Bomberos damnificados: agregá al menos uno (o marcá \"No\").")
            vistos = set()
            for i, b in enumerate(self.bomberos(), start=1):
                if b["id_ruba"] is None:
                    errores.append(f"Bombero damnificado {i}: elegí un bombero del padrón.")
                elif b["id_ruba"] in vistos:
                    errores.append(f"Bombero damnificado {i}: está repetido.")
                vistos.add(b["id_ruba"])
        if self.es_accidente() and self.tarjeta_vehiculos.activa():
            if not parcial and not self.vehiculos():
                errores.append("Vehículos involucrados: agregá al menos uno (o marcá \"No\").")
            for i, v in enumerate(self.vehiculos(), start=1):
                if not parcial and v["marca"] is None:
                    errores.append(f"Vehículo {i}: elegí la marca.")
                if v["anio"] and not (v["anio"].isdigit() and len(v["anio"]) == 4):
                    errores.append(f"Vehículo {i}: el año debe tener 4 dígitos.")
                if not parcial and v["asegurado"] and not v["aseguradora"]:
                    errores.append(f"Vehículo {i}: indicá la aseguradora (o destildá \"Asegurado\").")
        return errores

    def limpiar(self) -> None:
        for tarjeta in (self.tarjeta_civiles, self.tarjeta_bienes, self.tarjeta_vehiculos, self.tarjeta_bomberos):
            tarjeta.set_activa(False)
        self.grilla_civiles.limpiar()
        self.grilla_bienes.limpiar()
        self.grilla_vehiculos.limpiar()
        self.grilla_bomberos.limpiar()

    def cargar(self, civiles: List[Dict[str, Any]], bienes: List[Dict[str, Any]],
               bomberos: List[Dict[str, Any]], vehiculos: Optional[List[Dict[str, Any]]] = None) -> None:
        """Repone un servicio guardado: cada tarjeta queda en "Sí" solo si tiene datos."""
        self.limpiar()
        if civiles:
            self.tarjeta_civiles.set_activa(True)
            self.grilla_civiles.set_cantidad(len(civiles))
            for fila, civil in enumerate(civiles):
                self.grilla_civiles.cargar_fila(fila, civil)
        if bienes:
            self.tarjeta_bienes.set_activa(True)
            self.grilla_bienes.limpiar()
            for bien in bienes:
                self.grilla_bienes.agregar_fila()
                self.grilla_bienes.cargar_fila(self.grilla_bienes.tabla.rowCount() - 1, bien)
        if bomberos:
            self.tarjeta_bomberos.set_activa(True)
            self.grilla_bomberos.limpiar()
            for bombero in bomberos:
                selector = self.grilla_bomberos.agregar_fila()
                selector.set_id_ruba(bombero.get("id_ruba"))
                fila = self.grilla_bomberos.tabla.rowCount() - 1
                self.grilla_bomberos.tabla.cellWidget(fila, COL_F_DETALLE).setText(bombero.get("detalle") or "")
        if vehiculos:
            self.tarjeta_vehiculos.set_activa(True)
            self.grilla_vehiculos.limpiar()
            for vehiculo in vehiculos:
                self.grilla_vehiculos.agregar_fila()
                self.grilla_vehiculos.cargar_fila(self.grilla_vehiculos.tabla.rowCount() - 1, vehiculo)

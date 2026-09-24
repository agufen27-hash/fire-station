"""
Widgets del formulario de siniestro que dependen del catálogo de RUBA:

- `PanelDatosEspecificos`: QStackedWidget con los campos condicionales del
  panel "General" de RUBA según Tipo/Subtipo (incendio forestal, incendio
  estructural, accidente). Las opciones de cada combo y de cada grupo de
  radios salen de `selectores.editar_general.condicionales` en
  config/ruba_mapping.json; el valor guardado es el código oficial de RUBA.

Si el mapping no trae opciones para un combo (`<campo>_opciones`), el combo
queda deshabilitado con un aviso: no se inventan códigos de RUBA. Si trae
`<campo>_default`, el combo lo anuncia ("si queda vacío: Soleado"): la
automatización lo carga en RUBA cuando el operador no eligió nada, para no
trabar un campo obligatorio (ver ruba_automation._cargar_condicionales).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QWidget,
)

# Reglas Tipo/Subtipo -> formulario condicional: viven en app/core/catalogos
# (también las usa el payload de RUBA); se re-exportan acá por compatibilidad.
from app.core.catalogos import (  # noqa: F401
    FORM_ACCIDENTE,
    FORM_ESTRUCTURAL,
    FORM_FORESTAL,
    FORM_INCENDIO,
    SUBTIPO_INCENDIO_FORESTAL,
    SUBTIPOS_INCENDIO_ESTRUCTURAL,
    TIPO_ACCIDENTES,
    TIPO_INCENDIOS,
    formulario_para,
    leer_mapping,
)


@dataclass(frozen=True)
class Campo:
    clave: str        # clave en ruba_mapping.json (y en datos_especificos_json)
    etiqueta: str
    tipo: str         # "combo" | "entero" | "decimal" | "radio"


CAMPOS_POR_FORMULARIO: Dict[str, List[Campo]] = {
    FORM_FORESTAL: [
        Campo("tipo_lugar", "Tipo de lugar forestal", "combo"),
        Campo("unidad_superficie", "Unidad de superficie", "combo"),
        Campo("cantidad_superficie", "Cantidad de superficie", "decimal"),
        Campo("causa", "Causa del incendio", "combo"),
    ],
    FORM_ESTRUCTURAL: [
        Campo("tipo_lugar", "Tipo de lugar", "combo"),
        Campo("cantidad_pisos", "Cantidad de pisos", "entero"),
        Campo("cantidad_ambientes", "Cantidad de ambientes", "entero"),
        Campo("numero_piso", "Piso N°", "entero"),
        Campo("tipo_techo", "Tipo de techo", "combo"),
        Campo("tipo_abertura", "Tipo de abertura", "combo"),
        Campo("causa", "Causa del incendio", "combo"),
        Campo("deteccion_automatica", "¿Hubo detección automática?", "radio"),
        Campo("habia_extintores", "¿Había extintores?", "radio"),
        Campo("habia_nichos", "¿Había nichos hidrantes?", "radio"),
    ],
    FORM_INCENDIO: [
        Campo("causa", "Causa del incendio", "combo"),
    ],
    FORM_ACCIDENTE: [
        Campo("clima", "Condiciones climáticas", "combo"),
        Campo("causa", "Causa del accidente", "combo"),
    ],
}

TITULOS_FORMULARIO = {
    FORM_FORESTAL: "Incendio Forestal",
    FORM_ESTRUCTURAL: "Incendio Estructural",
    FORM_INCENDIO: "Incendio",
    FORM_ACCIDENTE: "Accidente",
}

# Etiquetas legibles para las claves del mapping que no quedan bien con la
# regla genérica (reemplazar "_" por espacio y capitalizar).
ETIQUETAS_OPCION = {
    "si": "Sí",
    "desconoce": "Desconoce",
    "montana": "Montaña",
    "bosque_nativo": "Bosque nativo",
    "bosque_cultivado": "Bosque cultivado",
    "arbustal_matorral": "Arbustal / Matorral",
    "interfase": "Interfase",
    "no_se_evacuo": "No se evacuó",
    "kilometros": "Kilómetros",
    "hectareas": "Hectáreas",
    "evacuacion_parcial": "Evacuación parcial",
    "evacuacion_total": "Evacuación total",
    "dpto": "Departamento",
    "multifamiliar": "Multifamiliar",
    "madera_paja": "Madera / Paja",
    "chapa_metalica": "Chapa metálica",
    "chapa_carton": "Chapa de cartón",
    "acero_hierro": "Acero / Hierro",
    "plastica": "Plástica",
}

TEXTO_SIN_OPCIONES = "Sin opciones oficiales en ruba_mapping.json"
TEXTO_SELECCIONAR = "— Seleccionar —"
VALOR_VACIO_NUMERICO = -1  # los spin boxes arrancan acá y muestran "—" (= sin dato)


def etiqueta_opcion(clave: str) -> str:
    return ETIQUETAS_OPCION.get(clave) or clave.replace("_", " ").capitalize()


def _condicionales(mapping: Dict[str, Any]) -> Dict[str, Any]:
    return (mapping.get("selectores", {}).get("editar_general", {}).get("condicionales", {})) or {}


def opciones_combo(mapping: Dict[str, Any], formulario: str, clave: str) -> List[Tuple[str, str]]:
    """[(etiqueta, código RUBA)] de `<clave>_opciones`, en el orden del JSON."""
    opciones = _condicionales(mapping).get(formulario, {}).get(f"{clave}_opciones") or {}
    return [(etiqueta_opcion(k), str(v)) for k, v in opciones.items()]


def valor_por_defecto(mapping: Dict[str, Any], formulario: str, clave: str) -> Optional[str]:
    """`<clave>_default` del bloque: lo que la automatización carga en RUBA
    si el operador deja el campo vacío."""
    valor = _condicionales(mapping).get(formulario, {}).get(f"{clave}_default")
    return str(valor) if valor not in (None, "") else None


def opciones_radio(mapping: Dict[str, Any], formulario: str, clave: str) -> List[Tuple[str, str]]:
    """[(etiqueta, clave)] para un grupo de radios: en el mapping son un
    dict {"si": "#selector_0", "no": "#selector_1", ...}; se guarda la clave."""
    opciones = _condicionales(mapping).get(formulario, {}).get(clave) or {}
    return [(etiqueta_opcion(k), k) for k in opciones] if isinstance(opciones, dict) else []


def _poblar_combo(combo: QComboBox, opciones: List[Tuple[str, str]], clave_mapping: str,
                  por_defecto: Optional[str] = None) -> None:
    combo.clear()
    if not opciones:
        combo.addItem(TEXTO_SIN_OPCIONES, None)
        combo.setEnabled(False)
        combo.setToolTip(f"Agregá '{clave_mapping}' en config/ruba_mapping.json con los códigos oficiales de RUBA.")
        return
    combo.setEnabled(True)
    combo.setToolTip("")
    etiqueta_defecto = next((etiqueta for etiqueta, valor in opciones if valor == por_defecto), None)
    if etiqueta_defecto:
        combo.addItem(f"{TEXTO_SELECCIONAR} (si queda vacío: {etiqueta_defecto})", None)
        combo.setToolTip(f"Si no se elige, en RUBA se carga '{etiqueta_defecto}' (campo obligatorio).")
    else:
        combo.addItem(TEXTO_SELECCIONAR, None)
    for etiqueta, valor in opciones:
        combo.addItem(etiqueta, valor)


def _spin_entero(parent: QWidget) -> QSpinBox:
    spin = QSpinBox(parent)
    spin.setRange(VALOR_VACIO_NUMERICO, 500)
    spin.setSpecialValueText("—")
    spin.setValue(VALOR_VACIO_NUMERICO)
    return spin


def _spin_decimal(parent: QWidget) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(parent)
    spin.setRange(VALOR_VACIO_NUMERICO, 1_000_000)
    spin.setDecimals(2)
    spin.setSpecialValueText("—")
    spin.setValue(VALOR_VACIO_NUMERICO)
    return spin


class _PaginaFormulario(QWidget):
    """Los campos de un bloque condicional, construidos a partir de su
    lista de `Campo` y de las opciones del mapping."""

    COLUMNAS = 2

    def __init__(self, formulario: str, mapping: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.formulario = formulario
        self._widgets: Dict[str, Any] = {}

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for col in range(self.COLUMNAS):
            grid.setColumnStretch(col, 1)

        titulo = QLabel(f"Datos específicos — {TITULOS_FORMULARIO[formulario]}", self)
        titulo.setObjectName("subtituloBloque")
        grid.addWidget(titulo, 0, 0, 1, self.COLUMNAS)

        campos = CAMPOS_POR_FORMULARIO[formulario]
        no_radios = [c for c in campos if c.tipo != "radio"]
        radios = [c for c in campos if c.tipo == "radio"]

        fila = 1
        for i, campo in enumerate(no_radios):
            col = i % self.COLUMNAS
            if i and col == 0:
                fila += 2
            grid.addWidget(QLabel(campo.etiqueta, self), fila, col)
            grid.addWidget(self._crear_widget(campo, mapping), fila + 1, col)
        fila += 2

        for campo in radios:
            grid.addWidget(QLabel(campo.etiqueta, self), fila, 0)
            grid.addWidget(self._crear_widget(campo, mapping), fila, 1)
            fila += 1

    def _crear_widget(self, campo: Campo, mapping: Dict[str, Any]) -> QWidget:
        if campo.tipo == "combo":
            combo = QComboBox(self)
            _poblar_combo(combo, opciones_combo(mapping, self.formulario, campo.clave), f"{campo.clave}_opciones",
                          valor_por_defecto(mapping, self.formulario, campo.clave))
            combo.setObjectName(f"{self.formulario}__{campo.clave}")
            self._widgets[campo.clave] = combo
            return combo
        if campo.tipo == "entero":
            spin = _spin_entero(self)
            self._widgets[campo.clave] = spin
            return spin
        if campo.tipo == "decimal":
            spin = _spin_decimal(self)
            self._widgets[campo.clave] = spin
            return spin

        # radio
        contenedor = QWidget(self)
        fila = QHBoxLayout(contenedor)
        fila.setContentsMargins(0, 0, 0, 0)
        grupo = QButtonGroup(contenedor)
        grupo.setExclusive(True)
        opciones = opciones_radio(mapping, self.formulario, campo.clave)
        for etiqueta, valor in opciones:
            radio = QRadioButton(etiqueta, contenedor)
            radio.setProperty("valor_ruba", valor)
            grupo.addButton(radio)
            fila.addWidget(radio)
        if not opciones:
            fila.addWidget(QLabel(TEXTO_SIN_OPCIONES, contenedor))
        fila.addStretch(1)
        self._widgets[campo.clave] = grupo
        return contenedor

    def valores(self) -> Dict[str, Any]:
        """Solo los campos completados: {clave: código RUBA | número | clave de radio}."""
        datos: Dict[str, Any] = {}
        for clave, widget in self._widgets.items():
            if isinstance(widget, QComboBox):
                valor = widget.currentData()
            elif isinstance(widget, QDoubleSpinBox):
                valor = None if widget.value() < 0 else round(widget.value(), 2)
            elif isinstance(widget, QSpinBox):
                valor = None if widget.value() < 0 else widget.value()
            else:  # QButtonGroup
                boton = widget.checkedButton()
                valor = boton.property("valor_ruba") if boton else None
            if valor is not None:
                datos[clave] = valor
        return datos

    def limpiar(self) -> None:
        for widget in self._widgets.values():
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(0)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(VALOR_VACIO_NUMERICO)
            else:
                # Con un grupo exclusivo no se puede "des-tildar" directamente.
                widget.setExclusive(False)
                for boton in widget.buttons():
                    boton.setChecked(False)
                widget.setExclusive(True)

    def widget(self, clave: str) -> Any:
        return self._widgets[clave]

    def cargar(self, campos: Dict[str, Any]) -> None:
        """Inversa de `valores()`: repone un bloque ya guardado."""
        self.limpiar()
        for clave, valor in (campos or {}).items():
            widget = self._widgets.get(clave)
            if isinstance(widget, QComboBox):
                indice = widget.findData(str(valor))
                if indice >= 0:
                    widget.setCurrentIndex(indice)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(valor)
            elif widget is not None:  # QButtonGroup
                for boton in widget.buttons():
                    if boton.property("valor_ruba") == valor:
                        boton.setChecked(True)


class PanelDatosEspecificos(QStackedWidget):
    """Muestra el bloque condicional que corresponde al Tipo/Subtipo elegido.
    Página 0: aviso de "sin datos adicionales"; una página por formulario."""

    def __init__(self, parent: Optional[QWidget] = None, mapping: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(parent)
        mapping = leer_mapping() if mapping is None else mapping

        aviso = QLabel("Este tipo de siniestro no requiere datos adicionales en RUBA.", self)
        aviso.setObjectName("pageSubtitle")
        aviso.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.addWidget(aviso)

        self._paginas: Dict[str, _PaginaFormulario] = {}
        for formulario in CAMPOS_POR_FORMULARIO:
            pagina = _PaginaFormulario(formulario, mapping, self)
            self._paginas[formulario] = pagina
            self.addWidget(pagina)

        self.currentChanged.connect(self._ajustar_alto)
        self._ajustar_alto()

    def _ajustar_alto(self, *_args) -> None:
        # Un QStackedWidget toma el alto de su página más grande; así se
        # ajusta a la página visible y no deja un hueco con el aviso corto.
        self.setFixedHeight(self.currentWidget().sizeHint().height())

    def actualizar(self, tipo_id: Optional[int], subtipo_codigo: Optional[str | int]) -> None:
        formulario = formulario_para(tipo_id, subtipo_codigo)
        self.setCurrentWidget(self._paginas[formulario] if formulario else self.widget(0))

    def formulario_activo(self) -> Optional[str]:
        pagina = self.currentWidget()
        return pagina.formulario if isinstance(pagina, _PaginaFormulario) else None

    def pagina(self, formulario: str) -> _PaginaFormulario:
        return self._paginas[formulario]

    def datos(self) -> Optional[Dict[str, Any]]:
        """{"formulario": ..., "campos": {...}} del bloque visible, o None si
        el siniestro no tiene bloque condicional."""
        pagina = self.currentWidget()
        if not isinstance(pagina, _PaginaFormulario):
            return None
        return {"formulario": pagina.formulario, "campos": pagina.valores()}

    def limpiar(self) -> None:
        for pagina in self._paginas.values():
            pagina.limpiar()

    def cargar(self, datos: Optional[Dict[str, Any]]) -> None:
        """Repone lo guardado (llamar después de elegir Tipo/Subtipo)."""
        self.limpiar()
        if datos and datos.get("formulario") in self._paginas:
            self._paginas[datos["formulario"]].cargar(datos.get("campos"))


# -- Opciones de género (damnificados) --------------------------------------------

def opciones_genero(mapping: Dict[str, Any]) -> List[Tuple[str, str]]:
    opciones = mapping.get("selectores", {}).get("damnificados", {}).get("genero_opciones") or {}
    return [(etiqueta_opcion(k), str(v)) for k, v in opciones.items()]

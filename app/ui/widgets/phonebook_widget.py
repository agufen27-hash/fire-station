"""
Guía telefónica operativa de la guardia (tabla `contactos`).

- Buscador instantáneo: cada palabra tipeada tiene que aparecer en el
  nombre, entidad, rubro, localidad, categoría o notas (sin distinguir
  mayúsculas ni acentos); si se tipean dígitos también busca en los números.
- Filtro por categoría.
- Números en tipografía grande, botón "Copiar" y Enter = copiar el primer
  resultado (para dictarlo o pegarlo en el celular de guardia).
- Alta, edición y baja de contactos desde la misma tarjeta.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db import get_session
from app.models import Contacto
from app.ui import theme

CATEGORIAS = [
    "Cuarteles Vecinos",
    "Servicios de Emergencia",
    "Autoridades Municipales",
    "Defensa Civil",
    "Productores Rurales",
]
TODAS = "Todas las categorías"
ICONOS_CATEGORIA = {
    "Cuarteles Vecinos": "🚒",
    "Servicios de Emergencia": "🚨",
    "Autoridades Municipales": "🏛️",
    "Defensa Civil": "🛡️",
    "Productores Rurales": "🌾",
}
_RE_TELEFONO = re.compile(r"^[0-9+()\-\s/.]{3,40}$")


@dataclass
class DatosContacto:
    id: Optional[int]
    categoria: str
    nombre: str
    telefono: str
    entidad: Optional[str] = None
    rubro: Optional[str] = None
    localidad: Optional[str] = None
    telefono_alt: Optional[str] = None
    notas: Optional[str] = None

    @classmethod
    def desde_modelo(cls, c: Contacto) -> "DatosContacto":
        return cls(c.id, c.categoria, c.nombre, c.telefono, c.entidad, c.rubro, c.localidad, c.telefono_alt, c.notas)


# ---------------------------------------------------------------------------
# Datos y búsqueda
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))
    return " ".join(sin_acentos.lower().split())


def _digitos(texto: Optional[str]) -> str:
    return re.sub(r"\D", "", texto or "")


def coincide(contacto: DatosContacto, consulta: str, categoria: Optional[str] = None) -> bool:
    if categoria and contacto.categoria != categoria:
        return False
    texto = normalizar(" ".join(filter(None, (
        contacto.nombre, contacto.entidad, contacto.rubro, contacto.localidad, contacto.categoria, contacto.notas,
    ))))
    telefonos = _digitos(contacto.telefono) + " " + _digitos(contacto.telefono_alt)
    for palabra in normalizar(consulta).split():
        if palabra in texto:
            continue
        if _digitos(palabra) and _digitos(palabra) == palabra.replace("-", "") and _digitos(palabra) in telefonos:
            continue
        return False
    return True


def listar_contactos() -> List[DatosContacto]:
    with get_session() as session:
        contactos = session.query(Contacto).order_by(Contacto.categoria, Contacto.nombre).all()
        return [DatosContacto.desde_modelo(c) for c in contactos]


def guardar_contacto(datos: DatosContacto) -> int:
    campos = dict(
        categoria=datos.categoria, nombre=datos.nombre.strip(), telefono=datos.telefono.strip(),
        entidad=(datos.entidad or "").strip() or None, rubro=(datos.rubro or "").strip() or None,
        localidad=(datos.localidad or "").strip() or None, telefono_alt=(datos.telefono_alt or "").strip() or None,
        notas=(datos.notas or "").strip() or None,
    )
    with get_session() as session:
        if datos.id is not None:
            contacto = session.get(Contacto, datos.id)
            for clave, valor in campos.items():
                setattr(contacto, clave, valor)
        else:
            contacto = Contacto(**campos)
            session.add(contacto)
        session.flush()
        return contacto.id


def eliminar_contacto(contacto_id: int) -> None:
    with get_session() as session:
        contacto = session.get(Contacto, contacto_id)
        if contacto is not None:
            session.delete(contacto)


def validar_contacto(datos: DatosContacto) -> List[str]:
    errores = []
    if not datos.nombre.strip():
        errores.append("Falta el nombre.")
    if not _RE_TELEFONO.match(datos.telefono.strip()):
        errores.append("El teléfono solo puede tener números, espacios, +, -, ( ) y /.")
    if datos.telefono_alt and not _RE_TELEFONO.match(datos.telefono_alt.strip()):
        errores.append("El teléfono alternativo solo puede tener números, espacios, +, -, ( ) y /.")
    if datos.categoria not in CATEGORIAS:
        errores.append("Elegí una categoría.")
    return errores


# ---------------------------------------------------------------------------
# Diálogo de alta / edición
# ---------------------------------------------------------------------------

class DialogoContacto(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, datos: Optional[DatosContacto] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Editar contacto" if datos and datos.id else "Nuevo contacto")
        self.setMinimumWidth(460)
        self._id = datos.id if datos else None
        form = QFormLayout(self)
        form.setSpacing(10)

        self.combo_categoria = QComboBox(self)
        self.combo_categoria.addItems(CATEGORIAS)
        self.entry_nombre = QLineEdit(self)
        self.entry_nombre.setPlaceholderText("Ej: Cuartel de Bomberos Voluntarios")
        self.entry_entidad = QLineEdit(self)
        self.entry_entidad.setPlaceholderText("Ej: Federación de Córdoba · Regional 3")
        self.entry_rubro = QLineEdit(self)
        self.entry_rubro.setPlaceholderText("Ej: Guardia 24 h, Cisterna, Maquinaria vial…")
        self.entry_localidad = QLineEdit(self)
        self.entry_telefono = QLineEdit(self)
        self.entry_telefono.setPlaceholderText("Ej: 0358 15-123-4567")
        self.entry_telefono_alt = QLineEdit(self)
        self.texto_notas = QTextEdit(self)
        self.texto_notas.setFixedHeight(70)

        form.addRow(theme.etiqueta_requerida("Categoría"), self.combo_categoria)
        form.addRow(theme.etiqueta_requerida("Nombre"), self.entry_nombre)
        form.addRow("Entidad", self.entry_entidad)
        form.addRow("Rubro", self.entry_rubro)
        form.addRow("Localidad", self.entry_localidad)
        form.addRow(theme.etiqueta_requerida("Teléfono"), self.entry_telefono)
        form.addRow("Teléfono alternativo", self.entry_telefono_alt)
        form.addRow("Notas", self.texto_notas)

        self.label_errores = QLabel("", self)
        self.label_errores.setObjectName("mensajeProgreso")
        theme.set_tono(self.label_errores, "error")
        self.label_errores.setWordWrap(True)
        form.addRow(self.label_errores)

        botones = QHBoxLayout()
        cancelar = QPushButton("Cancelar", self)
        cancelar.clicked.connect(self.reject)
        guardar = QPushButton("Guardar contacto", self)
        guardar.setObjectName("botonGuardar")
        guardar.clicked.connect(self._aceptar)
        botones.addStretch(1)
        botones.addWidget(cancelar)
        botones.addWidget(guardar)
        form.addRow(botones)

        if datos:
            self.combo_categoria.setCurrentText(datos.categoria)
            self.entry_nombre.setText(datos.nombre)
            self.entry_entidad.setText(datos.entidad or "")
            self.entry_rubro.setText(datos.rubro or "")
            self.entry_localidad.setText(datos.localidad or "")
            self.entry_telefono.setText(datos.telefono)
            self.entry_telefono_alt.setText(datos.telefono_alt or "")
            self.texto_notas.setPlainText(datos.notas or "")

    def datos(self) -> DatosContacto:
        return DatosContacto(
            id=self._id, categoria=self.combo_categoria.currentText(), nombre=self.entry_nombre.text(),
            telefono=self.entry_telefono.text(), entidad=self.entry_entidad.text(), rubro=self.entry_rubro.text(),
            localidad=self.entry_localidad.text(), telefono_alt=self.entry_telefono_alt.text(),
            notas=self.texto_notas.toPlainText(),
        )

    def _aceptar(self) -> None:
        errores = validar_contacto(self.datos())
        self.label_errores.setText("\n".join(errores))
        if not errores:
            self.accept()


# ---------------------------------------------------------------------------
# Tarjeta de la guía
# ---------------------------------------------------------------------------

class FilaContacto(QFrame):
    copiar = Signal(str, str)        # teléfono, nombre
    editar = Signal(object)          # DatosContacto
    eliminar = Signal(object)        # DatosContacto

    def __init__(self, c: DatosContacto, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("filaContacto")
        self.contacto = c
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(12)

        izquierda = QVBoxLayout()
        izquierda.setSpacing(2)
        nombre = QLabel(f"{ICONOS_CATEGORIA.get(c.categoria, '📞')}  {c.nombre}", self)
        nombre.setObjectName("nombreContacto")
        izquierda.addWidget(nombre)
        detalle = " · ".join(x for x in (c.entidad, c.rubro, c.localidad) if x)
        self.label_detalle = QLabel(detalle or c.categoria, self)
        self.label_detalle.setObjectName("detalleContacto")
        self.label_detalle.setWordWrap(True)
        izquierda.addWidget(self.label_detalle)
        if c.notas:
            notas = QLabel(c.notas, self)
            notas.setObjectName("detalleContacto")
            notas.setWordWrap(True)
            izquierda.addWidget(notas)
        layout.addLayout(izquierda, 1)

        telefonos = QVBoxLayout()
        telefonos.setSpacing(0)
        self.label_telefono = QLabel(c.telefono, self)
        self.label_telefono.setObjectName("telefonoContacto")
        self.label_telefono.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label_telefono.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        telefonos.addWidget(self.label_telefono)
        if c.telefono_alt:
            alt = QLabel(f"alt. {c.telefono_alt}", self)
            alt.setObjectName("detalleContacto")
            alt.setAlignment(Qt.AlignmentFlag.AlignRight)
            alt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            telefonos.addWidget(alt)
        layout.addLayout(telefonos)

        self.boton_copiar = QPushButton("📋 Copiar", self)
        self.boton_copiar.setObjectName("botonAhora")
        self.boton_copiar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton_copiar.clicked.connect(lambda: self.copiar.emit(c.telefono, c.nombre))
        layout.addWidget(self.boton_copiar)
        for texto, senal, ayuda in (("✏️", self.editar, "Editar"), ("🗑", self.eliminar, "Eliminar")):
            boton = QPushButton(texto, self)
            boton.setObjectName("botonQuitarFila")
            boton.setToolTip(ayuda)
            boton.setFixedWidth(34)
            boton.clicked.connect(lambda _=False, s=senal: s.emit(c))
            layout.addWidget(boton)

    def marcar_copiado(self) -> None:
        self.boton_copiar.setText("✅ Copiado")
        QTimer.singleShot(1500, self._restaurar_boton)

    def _restaurar_boton(self) -> None:
        try:
            self.boton_copiar.setText("📋 Copiar")
        except RuntimeError:
            pass  # la fila se reconstruyó (filtro nuevo) antes de que venza el aviso


class PhonebookWidget(QFrame):
    """Tarjeta de guía telefónica. `confirmar` se puede inyectar (tests)."""

    def __init__(self, parent: Optional[QWidget] = None,
                 confirmar: Optional[Callable[[str], bool]] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaGuia")
        self._confirmar = confirmar or self._confirmar_con_dialogo
        self._contactos: List[DatosContacto] = []
        self._filas: List[FilaContacto] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        cabecera = QHBoxLayout()
        titulo = QLabel("📞  Guía telefónica operativa", self)
        titulo.setObjectName("tituloTarjeta")
        cabecera.addWidget(titulo)
        cabecera.addStretch(1)
        self.boton_nuevo = QPushButton("+ Contacto", self)
        self.boton_nuevo.setObjectName("botonAhora")
        self.boton_nuevo.clicked.connect(lambda: self._abrir_dialogo(None))
        cabecera.addWidget(self.boton_nuevo)
        layout.addLayout(cabecera)

        filtros = QHBoxLayout()
        self.entry_buscar = QLineEdit(self)
        self.entry_buscar.setObjectName("buscadorGuia")
        self.entry_buscar.setPlaceholderText("🔎  Buscar por nombre, entidad, rubro, localidad o número…  (Enter copia el 1°)")
        self.entry_buscar.setClearButtonEnabled(True)
        self.entry_buscar.textChanged.connect(self._filtrar)
        self.entry_buscar.returnPressed.connect(self._copiar_primero)
        filtros.addWidget(self.entry_buscar, 1)
        self.combo_categoria = QComboBox(self)
        self.combo_categoria.addItem(TODAS, None)
        for categoria in CATEGORIAS:
            self.combo_categoria.addItem(f"{ICONOS_CATEGORIA[categoria]} {categoria}", categoria)
        self.combo_categoria.currentIndexChanged.connect(self._filtrar)
        filtros.addWidget(self.combo_categoria)
        layout.addLayout(filtros)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._lista = QWidget()
        self._layout_lista = QVBoxLayout(self._lista)
        self._layout_lista.setContentsMargins(0, 0, 4, 0)
        self._layout_lista.setSpacing(6)
        self._scroll.setWidget(self._lista)
        layout.addWidget(self._scroll, 1)

        self.label_resumen = QLabel("", self)
        self.label_resumen.setProperty("muted", True)
        layout.addWidget(self.label_resumen)

        self.recargar()

    # -- Datos ---------------------------------------------------------------------

    def recargar(self) -> None:
        self._contactos = listar_contactos()
        self._filtrar()

    def visibles(self) -> List[DatosContacto]:
        return [f.contacto for f in self._filas]

    def _filtrar(self, *_args) -> None:
        consulta = self.entry_buscar.text()
        categoria = self.combo_categoria.currentData()
        coincidencias = [c for c in self._contactos if coincide(c, consulta, categoria)]

        for fila in self._filas:
            fila.setParent(None)
            fila.deleteLater()
        self._filas = []
        while self._layout_lista.count():
            self._layout_lista.takeAt(0)
        for c in coincidencias:
            fila = FilaContacto(c, self._lista)
            fila.copiar.connect(lambda tel, nombre, f=fila: self._copiar(tel, nombre, f))
            fila.editar.connect(self._abrir_dialogo)
            fila.eliminar.connect(self._eliminar)
            self._layout_lista.addWidget(fila)
            self._filas.append(fila)
        if not coincidencias:
            vacio = QLabel("Sin resultados. Probá con otra palabra o agregá el contacto con “+ Contacto”."
                           if self._contactos else "La guía está vacía: agregá contactos con “+ Contacto”.", self._lista)
            vacio.setObjectName("detalleContacto")
            vacio.setWordWrap(True)
            self._layout_lista.addWidget(vacio)
        self._layout_lista.addStretch(1)
        self.label_resumen.setText(f"{len(coincidencias)} de {len(self._contactos)} contactos")

    # -- Acciones -----------------------------------------------------------------

    def _copiar(self, telefono: str, nombre: str, fila: Optional[FilaContacto] = None) -> None:
        QApplication.clipboard().setText(telefono)
        if fila is not None:
            fila.marcar_copiado()
        self.label_resumen.setText(f"📋 Copiado: {telefono} ({nombre})")

    def _copiar_primero(self) -> None:
        if self._filas:
            primero = self._filas[0]
            self._copiar(primero.contacto.telefono, primero.contacto.nombre, primero)

    def _abrir_dialogo(self, datos: Optional[DatosContacto]) -> None:
        dialogo = DialogoContacto(self, datos)
        if dialogo.exec() == QDialog.DialogCode.Accepted:
            guardar_contacto(dialogo.datos())
            self.recargar()

    def _confirmar_con_dialogo(self, pregunta: str) -> bool:
        return QMessageBox.question(self, "Eliminar contacto", pregunta) == QMessageBox.StandardButton.Yes

    def _eliminar(self, datos: DatosContacto) -> None:
        if self._confirmar(f"¿Eliminar a “{datos.nombre}” ({datos.telefono}) de la guía?"):
            eliminar_contacto(datos.id)
            self.recargar()

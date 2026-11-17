"""La rueda del mouse no cambia fechas, horas ni combos sin foco: el evento
sigue al padre para que el formulario se scrollee."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt, QTime
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QDateEdit, QLineEdit, QTimeEdit, QVBoxLayout, QWidget

from app.ui.widgets import EditorHoraRapido, instalar_filtro_rueda

pytestmark = pytest.mark.rapido


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    instalar_filtro_rueda(app)
    return app


def _rueda(widget, arriba: bool = True) -> QWheelEvent:
    delta = 120 if arriba else -120
    centro = QPointF(widget.rect().center())
    evento = QWheelEvent(centro, QPointF(widget.mapToGlobal(QPoint(0, 0))) + centro, QPoint(0, 0),
                         QPoint(0, delta), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, evento)
    return evento


@pytest.fixture
def formulario(qapp):
    ventana = QWidget()
    layout = QVBoxLayout(ventana)
    combo = QComboBox()
    combo.addItems(["uno", "dos", "tres"])
    fecha = QDateEdit(QDate(2026, 11, 17))
    hora = QTimeEdit(QTime(10, 30))
    hora_rapida = EditorHoraRapido()
    hora_rapida.setTime(QTime(8, 15))
    otro = QLineEdit()
    for w in (combo, fecha, hora, hora_rapida, otro):
        layout.addWidget(w)
    ventana.show()
    qapp.processEvents()
    yield {"ventana": ventana, "combo": combo, "fecha": fecha, "hora": hora, "rapida": hora_rapida, "otro": otro}
    ventana.close()


def _valor(w):
    if isinstance(w, QComboBox):
        return w.currentIndex()
    return w.dateTime()


@pytest.mark.parametrize("clave", ["combo", "fecha", "hora", "rapida"])
def test_rueda_sin_foco_no_cambia_el_valor(qapp, formulario, clave):
    widget = formulario[clave]
    formulario["otro"].setFocus()
    qapp.processEvents()
    antes = _valor(widget)
    evento = _rueda(widget, arriba=False)
    assert _valor(widget) == antes
    assert not evento.isAccepted()  # queda para el padre (scroll del formulario)


@pytest.mark.parametrize("clave", ["combo", "fecha", "hora", "rapida"])
def test_rueda_con_foco_sigue_funcionando(qapp, formulario, clave):
    widget = formulario[clave]
    widget.setFocus()
    qapp.processEvents()
    if not widget.hasFocus():
        pytest.skip("la plataforma offscreen no dio el foco")
    antes = _valor(widget)
    _rueda(widget, arriba=False)
    assert _valor(widget) != antes


def test_rueda_no_da_foco(formulario):
    for clave in ("combo", "fecha", "hora", "rapida"):
        assert formulario[clave].focusPolicy() == Qt.FocusPolicy.StrongFocus

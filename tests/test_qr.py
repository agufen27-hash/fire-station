from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.services import qr_service
from app.services.inventario_service import item_desde_fila
from app.services.mantenimiento_service import (
    FichaActivo,
    MantenimientoCliente,
    MantenimientoError,
    estado_item_desde_fila,
)
from app.services.supabase_client import SupabaseCredenciales


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("crudo, esperado", [
    ("FS-9A1B2C3D", "FS-9A1B2C3D"),
    ("  fs-9a1b2c3d\r\n", "FS-9A1B2C3D"),
    ("FS'9A1B2C3D", "FS-9A1B2C3D"),               # pistola en distribución US sobre Windows en español
    ("ERA-001", "ERA-001"),
    ("https://firestation.local/activo?qr=FS-9A1B2C3D", "FS-9A1B2C3D"),
    ("", None), (None, None), ("¡¡", None), ("ab", None),
])
def test_normalizar_codigo(crudo, esperado):
    assert qr_service.normalizar_codigo(crudo) == esperado


def test_qr_generado_se_decodifica(app):
    imagen = qr_service.qr_qimage("FS-9A1B2C3D", 240)
    assert imagen.width() >= 200 and imagen.width() == imagen.height()
    assert qr_service.decodificador_disponible()
    assert qr_service.decodificar_qimage(imagen) == ["FS-9A1B2C3D"]
    assert qr_service.qr_svg("ERA-001").startswith(b"<?xml")


def test_etiquetas_pdf_paginadas(app, tmp_path):
    activos = [(f"FS-{i:08d}", f"Activo {i}", "Pañol") for i in range(22)]   # 21 por hoja
    ruta, paginas = qr_service.exportar_etiquetas_pdf(activos, tmp_path / "etiquetas.pdf")
    assert paginas == 2 and ruta.read_bytes().startswith(b"%PDF") and ruta.stat().st_size > 5000
    with pytest.raises(ValueError):
        qr_service.exportar_etiquetas_pdf([], tmp_path / "vacio.pdf")


# -- Pistola lectora (teclado simulado) ----------------------------------------------

class _Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _tipear(lector, reloj, texto, ms_por_tecla):
    for c in texto:
        reloj.t += ms_por_tecla / 1000
        lector.procesar_tecla(0, c)
    reloj.t += ms_por_tecla / 1000
    return lector.procesar_tecla(Qt.Key.Key_Return, "\r")


def test_pistola_rapida_se_reconoce_y_humano_no(app):
    from app.ui.qr_escaner import LectorPistolaQr

    reloj = _Reloj()
    lector = LectorPistolaQr(reloj=reloj)
    leidos = []
    lector.codigo_leido.connect(leidos.append)
    assert _tipear(lector, reloj, "fs'9a1b2c3d", 8) is True          # consume el Enter
    assert _tipear(lector, reloj, "FS-9A1B2C3D", 150) is False       # tipeo humano
    assert _tipear(lector, reloj, "AB1", 8) is False                  # demasiado corto
    assert leidos == ["FS-9A1B2C3D"]


# -- Ficha por código ---------------------------------------------------------------

ITEM = {"id": "i1", "codigo_qr": "FS-9A1B2C3D", "nombre": "Autobomba", "categoria": "VEHICULO",
        "ubicacion": "Sala", "estado": "OPERATIVO", "activo": True}
ESTADO = {**ITEM, "estado_general": "OPERATIVO",
          "por_frecuencia": {"DIARIA": {"total": 5, "al_dia": 5}, "SEMANAL": {"total": 2, "vencidas": 1},
                             "MENSUAL": {"total": 1, "pendientes": 1}}}


def _cliente(*respuestas):
    session = mock.Mock()
    session.headers = {}
    lista = []
    for r in respuestas:
        resp = mock.Mock(status_code=200, content=b"x")
        resp.json.return_value = r
        resp.raise_for_status.return_value = None
        lista.append(resp)
    session.request.side_effect = lista
    return MantenimientoCliente(SupabaseCredenciales("https://x.supabase.co", "k"), session=session), session


def test_ficha_por_codigo():
    cliente, session = _cliente([ITEM], [ESTADO], [])
    ficha = cliente.ficha_por_codigo("FS-9A1B2C3D")
    assert ficha.item.nombre == "Autobomba" and ficha.frecuencia_mas_urgente == "SEMANAL"
    assert session.request.call_args_list[0].kwargs["params"]["codigo_qr"] == "eq.FS-9A1B2C3D"
    cliente, _ = _cliente([])
    with pytest.raises(MantenimientoError, match="No hay ningún activo"):
        cliente.ficha_por_codigo("FS-0000")
    cliente, _ = _cliente([{**ITEM, "activo": False, "motivo_baja": "Rota"}])
    with pytest.raises(MantenimientoError, match="dado de baja: Rota"):
        cliente.ficha_por_codigo("FS-9A1B2C3D")


def _ficha():
    return FichaActivo(item=item_desde_fila(ITEM), estado=estado_item_desde_fila(ESTADO), tareas=[])


def test_dialogo_ficha_resalta_la_cadencia_urgente(app):
    from app.ui.mantenimiento_view import DialogoFichaActivo

    d = DialogoFichaActivo(_ficha(), MantenimientoCliente, operador_id=4, operador_nombre="Agustín")
    assert d.botones_revision["SEMANAL"].objectName() == "botonPrimarioRojo"
    assert d.botones_revision["DIARIA"].objectName() == "botonLimpiar"
    assert all(b.isEnabled() for b in d.botones_revision.values())
    sin_operador = DialogoFichaActivo(_ficha(), MantenimientoCliente, operador_id=None, operador_nombre="")
    assert not any(b.isEnabled() for b in sin_operador.botones_revision.values())


def test_inventario_muestra_qr_y_pide_la_ficha(app):
    from app.ui.inventario_view import PaginaInventario

    class Falso:
        def listar(self, incluir_bajas=False):
            return [item_desde_fila(ITEM)]

    pagina = PaginaInventario(cliente_factory=Falso)
    pagina._al_cargar(Falso().listar())
    assert not pagina.tabla.item(0, 0).icon().isNull()
    pedidas = []
    pagina.ficha_solicitada.connect(pedidas.append)
    pagina.tabla.selectRow(0)
    pagina.tabla.cellDoubleClicked.emit(0, 1)          # usuario estándar: abre la ficha
    assert pedidas == ["FS-9A1B2C3D"]


def test_dialogo_escanear_normaliza_lo_tipeado(app):
    from app.ui.qr_escaner import DialogoEscanearQr

    d = DialogoEscanearQr()
    d.campo.setText("fs'9a1b2c3d")
    d._aceptar_texto()
    assert d.codigo == "FS-9A1B2C3D" and d.result() == d.DialogCode.Accepted
    d2 = DialogoEscanearQr()
    d2.campo.setText("??")
    d2._aceptar_texto()
    assert d2.codigo is None and "no parece un código" in d2.label_estado.text()

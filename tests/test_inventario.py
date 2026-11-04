from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication

from app.services.inventario_service import (
    InventarioCliente,
    InventarioError,
    ItemInventario,
    exigir_admin,
    item_desde_fila,
)
from app.services.supabase_client import SupabaseCredenciales


def _cliente(respuesta):
    session = mock.Mock()
    session.headers = {}
    resp = mock.Mock(status_code=200, content=b"x")
    resp.json.return_value = respuesta
    resp.raise_for_status.return_value = None
    session.request.return_value = resp
    return InventarioCliente(SupabaseCredenciales("https://x.supabase.co", "k"), session=session), session


def _fila(**kw):
    base = {"id": "i1", "codigo_qr": "ERA-001", "nombre": "Equipo ERA", "categoria": "PROTECCION_RESPIRATORIA",
            "ubicacion": "Pañol", "estado": "OPERATIVO", "activo": True, "fecha_alta": "2026-11-04T19:00:00+00:00",
            "updated_at": "2026-11-04T19:00:00+00:00", "creado_por": "Agustín Fenoglio"}
    base.update(kw)
    return base


@pytest.mark.parametrize("rol, operador", [("BOMBERO", 1), ("JEFATURA", 1), (None, 1), ("ADMIN", None)])
def test_escritura_bloqueada_sin_admin_y_sin_salir_a_la_red(rol, operador):
    cliente, session = _cliente({"ok": True})
    with pytest.raises(InventarioError):
        cliente.guardar(operador, rol, {"nombre": "x", "categoria": "OTRO", "ubicacion": "y"})
    with pytest.raises(InventarioError):
        cliente.dar_de_baja(operador, rol, "i1", "rota")
    session.request.assert_not_called()


def test_guardar_como_admin_llama_rpc_con_operador():
    cliente, session = _cliente({"ok": True, "item": _fila()})
    item = cliente.guardar(7, "admin", {"nombre": " Equipo ERA ", "categoria": "PROTECCION_RESPIRATORIA",
                                        "ubicacion": "Pañol", "ignorado": "x"}, None)
    assert item.codigo_qr == "ERA-001" and item.categoria_legible.startswith("Protección")
    metodo, url = session.request.call_args.args
    assert metodo == "POST" and url.endswith("/rpc/rpc_inventario_guardar")
    cuerpo = session.request.call_args.kwargs["json"]
    assert cuerpo["p_operador_id"] == 7 and cuerpo["p_id"] is None
    assert cuerpo["p_datos"] == {"nombre": "Equipo ERA", "categoria": "PROTECCION_RESPIRATORIA", "ubicacion": "Pañol"}


def test_rechazo_de_la_base_se_traduce():
    cliente, _ = _cliente({"ok": False, "motivo": "OPERADOR_NO_ADMIN"})
    with pytest.raises(InventarioError, match="Sincronizar a Cloud"):
        cliente.dar_de_baja(7, "ADMIN", "i1", "rota")


def test_listar_filtra_bajas():
    cliente, session = _cliente([_fila()])
    assert len(cliente.listar()) == 1
    assert session.request.call_args.kwargs["params"]["activo"] == "is.true"
    cliente.listar(incluir_bajas=True)
    assert "activo" not in session.request.call_args.kwargs["params"]


def test_exigir_admin_acepta_admin():
    exigir_admin("ADMIN", 3)


# -- Vista ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Falso:
    items = [item_desde_fila(_fila()),
             item_desde_fila(_fila(id="i2", codigo_qr="FS-1", nombre="Motosierra", categoria="HERRAMIENTA",
                                   ubicacion="M-12", estado="EN_REPARACION")),
             item_desde_fila(_fila(id="i3", codigo_qr="FS-2", nombre="Handy", categoria="COMUNICACIONES",
                                   ubicacion="Guardia", estado="FUERA_DE_SERVICIO"))]

    def listar(self, incluir_bajas=False):
        return self.items


def _pagina(app):
    from app.ui.inventario_view import PaginaInventario

    pagina = PaginaInventario(cliente_factory=_Falso)
    pagina._al_cargar(_Falso.items)
    return pagina


def test_controles_ocultos_para_usuario_estandar(app):
    pagina = _pagina(app)
    for rol, operador in ((None, None), ("BOMBERO", 4), ("JEFATURA", 4), ("ADMIN", None)):
        pagina.set_operador(operador, rol)
        assert not pagina.puede_editar
        assert all(b.isHidden() for b in (pagina.boton_nuevo, pagina.boton_editar, pagina.boton_baja))
        assert not pagina.label_solo_lectura.isHidden()
    with mock.patch("app.ui.inventario_view.QMessageBox.warning") as aviso:
        pagina._nuevo()
        aviso.assert_called_once()


def test_admin_ve_controles_y_editar_requiere_seleccion(app):
    pagina = _pagina(app)
    pagina.set_operador(4, "ADMIN")
    assert not pagina.boton_nuevo.isHidden() and pagina.boton_nuevo.isEnabled()
    assert not pagina.boton_editar.isEnabled()
    pagina.tabla.selectRow(0)
    assert pagina.boton_editar.isEnabled() and pagina.boton_baja.isEnabled()


def test_filtros_estado_ubicacion_y_busqueda(app):
    pagina = _pagina(app)
    assert pagina.tarjeta_total.label_valor.text() == "3" and pagina.tarjeta_reparacion.label_valor.text() == "1"
    pagina._filtrar_estado("EN_REPARACION")
    assert [i.nombre for i in pagina.items_visibles()] == ["Motosierra"]
    pagina._filtrar_estado("__todos__")
    pagina.combo_ubicacion.setCurrentIndex(pagina.combo_ubicacion.findData("Guardia"))
    assert [i.nombre for i in pagina.items_visibles()] == ["Handy"]
    pagina.combo_ubicacion.setCurrentIndex(0)
    pagina.campo_buscar.setText("era-0")
    assert [i.codigo_qr for i in pagina.items_visibles()] == ["ERA-001"]
    assert isinstance(pagina.items_visibles()[0], ItemInventario)

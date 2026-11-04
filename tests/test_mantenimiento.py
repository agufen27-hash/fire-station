from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication

from app.services import mantenimiento_service as ms
from app.services.mantenimiento_service import (
    MantenimientoCliente,
    MantenimientoError,
    ResultadoChecklist,
    TareaItem,
    estado_item_desde_fila,
    get_estado_general,
    prever_estado_general,
    resultado_checklist,
    validar_respuestas,
)
from app.services.supabase_client import SupabaseCredenciales


def _tarea(tid, estado="AL_DIA", critica=False, frecuencia="DIARIA", titulo=None):
    return TareaItem(tarea_id=tid, frecuencia=frecuencia, titulo=titulo or f"Tarea {tid}", descripcion=None,
                     critica=critica, orden=0, estado_tarea=estado, ultimo_estado=None, ultima_fecha=None,
                     ultimo_responsable=None, ultima_observacion=None, vence_at=None)


# -- Motor (getEstadoGeneral) ------------------------------------------------------

@pytest.mark.parametrize("tareas, esperado", [
    ([_tarea("a"), _tarea("b", "PENDIENTE"), _tarea("c", "POR_VENCER", critica=True)], "OPERATIVO"),
    ([_tarea("a", "FALLA")], "CON_OBSERVACIONES"),
    ([_tarea("a", "VENCIDA")], "CON_OBSERVACIONES"),
    ([_tarea("a", "FALLA", critica=True)], "FUERA_DE_SERVICIO"),
    ([_tarea("a", "VENCIDA", critica=True, frecuencia="MENSUAL"), _tarea("b")], "FUERA_DE_SERVICIO"),
    ([], "OPERATIVO"),
])
def test_get_estado_general(tareas, esperado):
    assert get_estado_general(tareas) == esperado


def test_resultado_y_prevision_del_checklist():
    diarias = [_tarea("comb", critica=True), _tarea("cubiertas")]
    semanal_vencida = _tarea("bomba", "VENCIDA", critica=True, frecuencia="SEMANAL")
    assert resultado_checklist(diarias, {"comb": "OK", "cubiertas": "OK"}) == "APROBADO"
    assert resultado_checklist(diarias, {"comb": "OK", "cubiertas": "FALLA"}) == "CON_OBSERVACIONES"
    assert resultado_checklist(diarias, {"comb": "FALLA", "cubiertas": "OK"}) == "FALLIDO"
    assert prever_estado_general(diarias, {"comb": "FALLA", "cubiertas": "OK"}) == "FUERA_DE_SERVICIO"
    # Un diario perfecto no tapa una semanal crítica vencida.
    assert prever_estado_general(diarias + [semanal_vencida], {"comb": "OK", "cubiertas": "OK"}) == "FUERA_DE_SERVICIO"
    # Revisar lo que estaba vencido / con falla lo regulariza.
    assert prever_estado_general([_tarea("comb", "FALLA", critica=True)], {"comb": "OK"}) == "OPERATIVO"


def test_validar_respuestas():
    tareas = [_tarea("a", titulo="Aceite"), _tarea("b", titulo="Luces")]
    assert "Falta responder 1" in validar_respuestas(tareas, {"a": "OK"}, {})
    assert "Luces" in validar_respuestas(tareas, {"a": "OK", "b": "FALLA"}, {})
    assert validar_respuestas(tareas, {"a": "OK", "b": "FALLA"}, {"b": "Giro izquierdo quemado"}) is None


def test_estado_item_y_resumen_por_cadencia():
    item = estado_item_desde_fila({
        "id": "i1", "codigo_qr": "AB-1", "nombre": "Autobomba", "categoria": "VEHICULO", "ubicacion": "Sala",
        "estado": "FUERA_DE_SERVICIO", "fuera_servicio_automatico": True, "motivo_estado": "Mantenimiento: x",
        "estado_general": "FUERA_DE_SERVICIO", "motivos": [{"titulo": "x", "critica": True}],
        "por_frecuencia": {"DIARIA": {"total": 5, "al_dia": 4, "fallas": 1,
                                      "ultima_revision": "2026-11-04T11:00:00+00:00"},
                           "SEMANAL": {"total": 2, "pendientes": 2}}})
    assert item.resumen("DIARIA").estado == "FALLA" and item.resumen("SEMANAL").estado == "PENDIENTE"
    assert item.resumen("DIARIA").ultima_revision.hour == 8  # hora del cuartel
    assert item.resumen("MENSUAL") is None


# -- Cliente -----------------------------------------------------------------------

def _cliente(respuesta):
    session = mock.Mock()
    session.headers = {}
    resp = mock.Mock(status_code=200, content=b"x")
    resp.json.return_value = respuesta
    resp.raise_for_status.return_value = None
    session.request.return_value = resp
    return MantenimientoCliente(SupabaseCredenciales("https://x.supabase.co", "k"), session=session), session


def test_registrar_sin_operador_no_sale_a_la_red():
    cliente, session = _cliente({"ok": True})
    with pytest.raises(MantenimientoError, match="Identificate"):
        cliente.registrar(None, "i1", "DIARIA", {"a": "OK"}, {})
    session.request.assert_not_called()


def test_registrar_arma_la_rpc():
    cliente, session = _cliente({"ok": True, "registro_id": "r1", "resultado": "FALLIDO",
                                 "estado_general": "FUERA_DE_SERVICIO", "estado_item": "FUERA_DE_SERVICIO",
                                 "cambio_estado": True, "motivos": []})
    r = cliente.registrar(4, "i1", "DIARIA", {"a": "OK", "b": "FALLA"}, {"b": " sin combustible "}, " nota ")
    assert r.cambio_estado and r.estado_item == "FUERA_DE_SERVICIO"
    assert session.request.call_args.args[1].endswith("/rpc/rpc_mantenimiento_registrar")
    cuerpo = session.request.call_args.kwargs["json"]
    assert cuerpo["p_operador_id"] == 4 and cuerpo["p_frecuencia"] == "DIARIA" and cuerpo["p_observaciones"] == "nota"
    assert {"tarea_id": "b", "estado": "FALLA", "observacion": "sin combustible"} in cuerpo["p_respuestas"]


def test_rechazo_traducido_y_plantillas_solo_admin():
    cliente, session = _cliente({"ok": False, "motivo": "FALTA_OBSERVACION_FALLA"})
    with pytest.raises(MantenimientoError, match="observación"):
        cliente.registrar(4, "i1", "DIARIA", {"a": "FALLA"}, {})
    session.request.reset_mock()
    with pytest.raises(MantenimientoError, match="Administrador"):
        cliente.guardar_plantilla(4, "BOMBERO", {"titulo": "x", "frecuencia": "DIARIA", "categoria": "OTRO"})
    with pytest.raises(MantenimientoError):
        cliente.desactivar_plantilla(None, "ADMIN", "t1")
    session.request.assert_not_called()


# -- Vista -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


ITEM = {"id": "i1", "codigo_qr": "AB-1", "nombre": "Autobomba", "categoria": "VEHICULO", "ubicacion": "Sala",
        "estado": "OPERATIVO", "estado_general": "OPERATIVO",
        "por_frecuencia": {"DIARIA": {"total": 2, "pendientes": 2}, "SEMANAL": {"total": 1, "al_dia": 1}}}
ITEM_EPP = {"id": "i2", "codigo_qr": "EPP-1", "nombre": "Chaquetón", "categoria": "EPP", "ubicacion": "Pañol",
            "estado": "OPERATIVO", "estado_general": "OPERATIVO", "por_frecuencia": {"MENSUAL": {"total": 1}}}


class _Falso:
    def items(self):
        return [estado_item_desde_fila(ITEM), estado_item_desde_fila(ITEM_EPP)]

    def tareas_item(self, item_id, frecuencia=None):
        return [_tarea("comb", "PENDIENTE", critica=True), _tarea("luces", "PENDIENTE")]


def _pagina(app):
    from app.ui.mantenimiento_view import PaginaMantenimiento

    pagina = PaginaMantenimiento(cliente_factory=_Falso)
    pagina._al_cargar(_Falso().items())
    return pagina


def test_pestanas_por_cadencia_y_solo_atencion(app):
    pagina = _pagina(app)
    assert [i.nombre for i in pagina.items_visibles()] == ["Autobomba"]
    assert pagina.tarjeta_pendientes.label_valor.text() == "1"
    pagina.set_frecuencia("MENSUAL")
    assert [i.nombre for i in pagina.items_visibles()] == ["Chaquetón"]
    pagina.set_frecuencia("SEMANAL")
    pagina.check_pendientes.setChecked(True)
    assert pagina.items_visibles() == []          # la semanal está al día


def test_completar_requiere_operador(app):
    pagina = _pagina(app)
    pagina.tabla.selectRow(0)
    with mock.patch("app.ui.mantenimiento_view.QMessageBox.information") as aviso, \
            mock.patch.object(pagina, "_ejecutar") as ejecutar:
        pagina.completar_revision()
    aviso.assert_called_once()
    ejecutar.assert_not_called()


def test_formulario_checklist(app):
    from app.ui.mantenimiento_view import DialogoChecklist

    item = estado_item_desde_fila(ITEM)
    tareas = _Falso().tareas_item("i1")
    enviados = []

    def enviar(resp, obs, general):
        enviados.append((resp, obs, general))
        return ResultadoChecklist("r1", "FALLIDO", "FUERA_DE_SERVICIO", "FUERA_DE_SERVICIO", True, [])

    d = DialogoChecklist(item, "DIARIA", tareas, "Agustín Fenoglio", enviar)
    assert not d.boton_ok.isEnabled()
    d._todo_ok()
    assert d.boton_ok.isEnabled() and "APROBADO" in d.chip_prevision.text()
    d.filas[0].set_respuesta(ms.FALLA_RESPUESTA)
    assert "FUERA DE SERVICIO" in d.chip_prevision.text()
    d._firmar()                                    # falla sin observación: no envía
    assert not enviados and "Describí la falla" in d.label_error.text()

    from PySide6.QtWidgets import QMessageBox

    with mock.patch("app.ui.mantenimiento_view.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
            mock.patch.object(d, "_ejecutar", side_effect=lambda f, ok, err: ok(f())):
        d.filas[0].campo_observacion.setText("Tanque a 1/4")
        d._firmar()
    assert enviados[0][0] == {"comb": "FALLA", "luces": "OK"}
    assert enviados[0][1] == {"comb": "Tanque a 1/4"}
    assert d.resultado is not None and d.result() == d.DialogCode.Accepted


def test_formulario_conserva_lo_cargado_si_falla_la_red(app):
    from app.ui.mantenimiento_view import DialogoChecklist

    d = DialogoChecklist(estado_item_desde_fila(ITEM), "DIARIA", _Falso().tareas_item("i1"), "X",
                         lambda *a: None)
    d._todo_ok()
    with mock.patch.object(d, "_ejecutar", side_effect=lambda f, ok, err: err("Sin conexión con Supabase")):
        d._firmar()
    assert d.isVisible() is False and d.result() != d.DialogCode.Accepted
    assert "Sin conexión" in d.label_error.text() and d.respuestas() == {"comb": "OK", "luces": "OK"}
    assert d.boton_ok.text() == "Reintentar" and d.boton_ok.isEnabled()

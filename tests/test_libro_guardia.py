from datetime import datetime, timedelta
from unittest import mock

import pytest

from app.reports.libro_guardia_generator import construir_libro, resumen_por_bombero
from app.services.libro_guardia_service import (
    TZ_CUARTEL,
    LibroGuardiaCliente,
    LibroGuardiaError,
    RegistroBitacora,
    fin_de_mes,
    inicio_de_mes,
    mes_anterior,
    registro_desde_fila,
)
from app.services.supabase_client import SupabaseCredenciales
from app.ui.libro_guardia_view import formatear_duracion


def _fila(**kw):
    base = {"id": "a1", "tipo_registro": "VIAJE", "bombero_id": 1, "bombero": "Agustín Fenoglio",
            "legajo": "59-027", "jerarquia": "Bombero", "actividad": "Viaje a Córdoba", "vehiculo": "M-7",
            "inicio": "2026-11-03T18:00:00+00:00", "fin": "2026-11-04T13:00:00+00:00", "minutos": 1140,
            "estado": "FINALIZADO", "metodo_registro": "ESCRITORIO"}
    base.update(kw)
    return base


def test_registro_desde_fila_pasa_a_hora_del_cuartel():
    r = registro_desde_fila(_fila())
    assert r.inicio == datetime(2026, 11, 3, 15, 0, tzinfo=TZ_CUARTEL)
    assert r.fin.hour == 10 and r.es_viaje and not r.abierto
    assert r.metodo_legible == "Escritorio"
    abierto = registro_desde_fila(_fila(fin=None, minutos=None, estado="EN_CURSO"))
    assert abierto.fin is None and abierto.abierto


def test_limites_de_mes_y_mes_anterior():
    assert inicio_de_mes(2026, 12).isoformat() == "2026-12-01T00:00:00-03:00"
    assert fin_de_mes(2026, 12) == datetime(2027, 1, 1, tzinfo=TZ_CUARTEL)
    assert mes_anterior(datetime(2026, 1, 15).date()) == (2025, 12)


def _cliente(respuesta):
    session = mock.Mock()
    session.headers = {}
    resp = mock.Mock(status_code=200, content=b"x")
    resp.json.return_value = respuesta
    resp.raise_for_status.return_value = None
    session.request.return_value = resp
    return LibroGuardiaCliente(SupabaseCredenciales("https://x.supabase.co", "k"), session=session), session


def test_registrar_salida_llama_rpc_con_origen_escritorio():
    cliente, session = _cliente({"ok": True, "viaje_id": "v1"})
    assert cliente.registrar_salida(7, " Río Cuarto ", " M-12 ")["viaje_id"] == "v1"
    metodo, url = session.request.call_args.args
    assert metodo == "POST" and url.endswith("/rest/v1/rpc/rpc_registrar_viaje")
    cuerpo = session.request.call_args.kwargs["json"]
    assert {k: cuerpo[k] for k in ("p_bombero_id", "p_destino", "p_vehiculo", "p_origen")} == {
        "p_bombero_id": 7, "p_destino": "Río Cuarto", "p_vehiculo": "M-12", "p_origen": "ESCRITORIO"}
    assert cuerpo["p_cliente_id"] and cuerpo["p_fecha_salida"]   # hora real + idempotencia (modo offline)


def test_motivo_de_rechazo_se_traduce():
    cliente, _ = _cliente({"ok": False, "motivo": "LLEGADA_EN_EL_FUTURO"})
    with pytest.raises(LibroGuardiaError, match="posterior al momento actual"):
        cliente.marcar_regreso("v1", datetime(2026, 11, 4, 12, 0))


def test_bitacora_incluye_en_curso_y_ordena():
    cliente, session = _cliente([_fila()])
    regs = cliente.bitacora(datetime(2026, 10, 5, tzinfo=TZ_CUARTEL))
    params = session.request.call_args.kwargs["params"]
    assert params["or"] == "(inicio.gte.2026-10-05T00:00:00-03:00,estado.eq.EN_CURSO)"
    assert params["order"] == "inicio.desc" and len(regs) == 1


def _reg(i, bombero_id, viaje, minutos, fin=True):
    inicio = datetime(2026, 11, i, 8, 0, tzinfo=TZ_CUARTEL)
    return RegistroBitacora(str(i), "VIAJE" if viaje else "ASISTENCIA", bombero_id, f"B{bombero_id}", "L",
                            "Bombero", "x", None, inicio, inicio + timedelta(minutes=minutos) if fin else None,
                            minutos if fin else None, "FINALIZADO" if fin else "EN_CURSO", "BOT")


def test_planilla_mensual_arial_10_y_horas_solo_cerrados():
    regs = [_reg(1, 1, True, 90), _reg(2, 1, False, 30), _reg(3, 2, False, 0, fin=False)]
    wb = construir_libro(regs, 2026, 11)
    hoja = wb["Planilla de Presencia"]
    assert hoja.cell(6, 1).value == "Fecha" and hoja.cell(6, 9).value == "Horas"
    assert hoja.cell(7, 9).value == 1.5 and hoja.cell(9, 9).value is None   # abierto: sin horas
    assert hoja.cell(10, 9).value == "=SUM(I7:I9)"
    assert {(c.font.name, c.font.sz) for c in hoja[7] if c.value is not None} == {("Arial", 10)}
    assert hoja.page_setup.orientation == "landscape"
    assert resumen_por_bombero(regs)[0] == ("L", "B1", "Bombero", 2, 1, 1, 2, 2.0)


@pytest.mark.parametrize("minutos, texto", [(None, "—"), (45, "45 min"), (150, "2 h 30 min"), (1960, "1 d 8 h")])
def test_formatear_duracion(minutos, texto):
    assert formatear_duracion(minutos) == texto

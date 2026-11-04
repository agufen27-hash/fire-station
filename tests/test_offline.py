"""Modo offline: caché local de lecturas, cola de escrituras y sincronización."""

from unittest import mock

import requests

from app.services import cache_offline
from app.services.libro_guardia_service import LibroGuardiaCliente
from app.services.supabase_client import SupabaseCredenciales
from app.services.supabase_rest import SupabaseSinConexion


def _respuesta(datos):
    resp = mock.Mock(status_code=200, content=b"x")
    resp.json.return_value = datos
    resp.raise_for_status.return_value = None
    return resp


def test_sin_red_lee_la_cache_y_encola_la_escritura():
    session = mock.Mock()
    session.headers = {}
    cliente = LibroGuardiaCliente(SupabaseCredenciales("https://x.supabase.co", "k"), session=session)
    fila = {"id": "v1", "tipo_registro": "VIAJE", "bombero_id": 1, "bombero": "A", "actividad": "Viaje a X",
            "inicio": "2026-11-04T12:00:00+00:00", "estado": "EN_CURSO", "metodo_registro": "BOT"}

    # Con red: la lectura queda guardada en la caché local.
    session.request.return_value = _respuesta({"viajes_activos": 1})
    assert cliente.resumen().viajes_activos == 1
    from datetime import datetime, timedelta, timezone
    session.request.return_value = _respuesta([fila])
    assert len(cliente.bitacora(datetime.now(timezone.utc) - timedelta(days=30))) == 1
    assert cache_offline.estado().en_linea

    # Se corta la red: las lecturas salen de la caché (aunque cambie `desde`)...
    session.request.side_effect = requests.exceptions.ConnectionError("sin red")
    assert cliente.resumen().viajes_activos == 1
    assert cliente.bitacora(datetime.now(timezone.utc) - timedelta(days=30))[0].id == "v1"
    assert not cache_offline.estado().en_linea and cache_offline.estado().dato_cacheado_at is not None

    # ...y la escritura de la guardia queda en la cola con su hora real e id de idempotencia.
    r = cliente.registrar_salida(7, "Río Cuarto", "M-12")
    assert r["ok"] and r["encolado"]
    [op] = cache_offline.pendientes()
    assert op.operacion == "rpc_registrar_viaje" and op.argumentos["p_bombero_id"] == 7
    assert op.argumentos["p_cliente_id"] and op.argumentos["p_fecha_salida"].startswith(r["fecha_salida"][:16])


def test_sincronizacion_envia_en_orden_y_aisla_rechazos():
    for i in range(3):
        cache_offline.encolar("rpc_registrar_viaje", {"p_bombero_id": i, "p_cliente_id": f"c{i}"}, f"viaje {i}")

    # Primer intento: sigue sin red -> no se pierde nada.
    def sin_red(_op, _args):
        raise SupabaseSinConexion("sin red")

    r = cache_offline.sincronizar(sin_red, lambda e: isinstance(e, SupabaseSinConexion))
    assert r.sin_conexion and r.enviadas == 0 and cache_offline.contar() == {"PENDIENTE": 3}

    # Vuelve la red: se envía en orden; un rechazo de negocio no traba a los demás.
    enviados = []

    def con_red(op, args):
        enviados.append(args["p_cliente_id"])
        return {"ok": False, "motivo": "YA_TIENE_VIAJE_ABIERTO"} if args["p_bombero_id"] == 1 else {"ok": True}

    r = cache_offline.sincronizar(con_red, lambda e: isinstance(e, SupabaseSinConexion))
    assert enviados == ["c0", "c1", "c2"]          # mismo cliente_id que al encolar: la base no duplica
    assert (r.enviadas, r.rechazadas) == (2, 1) and cache_offline.estado().en_linea
    [rechazada] = cache_offline.pendientes()
    assert rechazada.estado == "ERROR" and "YA_TIENE_VIAJE_ABIERTO" in rechazada.ultimo_error
    assert cache_offline.sincronizar(con_red, lambda e: False).enviadas == 0   # los ERROR no se reintentan solos

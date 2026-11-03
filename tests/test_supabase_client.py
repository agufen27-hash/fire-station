from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from app.services.supabase_client import (
    SupabaseBridge,
    SupabaseCredenciales,
    SupabaseSyncError,
    cargar_credenciales,
    SupabaseConfigError,
    normalizar_telefono_ar,
)


@pytest.mark.parametrize("crudo, esperado", [
    ("3585646300", "5493585646300"),            # 10 dígitos -> 549
    ("0358 564-6300", "5493585646300"),         # quita 0
    ("(0358) 15-564-6300", "5493585646300"),    # quita 0 y 15 (área de 3)
    ("358 15 5646300", "5493585646300"),        # 15 sin 0
    ("011 15 4123-4567", "5491141234567"),      # AMBA (área de 2)
    ("03385 15 412345", "5493385412345"),       # área de 4 (Laboulaye)
    ("+54 9 358 564-6300", "5493585646300"),    # ya internacional
    ("+54 358 564 6300", "5493585646300"),      # internacional sin 9
    ("+54 9 358 15 564 6300", "5493585646300"), # internacional con 15 de más
    ("3581512345", "5493581512345"),            # 10 dígitos con '15' propio: no se toca
    ("", None),
    ("   ", None),
    (None, None),
    ("4123456", None),                          # sin código de área
])
def test_normalizar_telefono_ar(crudo, esperado):
    assert normalizar_telefono_ar(crudo) == esperado
    assert SupabaseBridge.normalizar_telefono(crudo) == esperado


def _persona(id_, tel, **kw):
    p = SimpleNamespace(id=id_, legajo=kw.get("legajo"), id_ruba=kw.get("id_ruba"),
                        nombre="Ana", apellido=f"Perez{id_}", jerarquia=None,
                        telefono=tel, rol=kw.get("rol", "BOMBERO"), activo=kw.get("activo", True))
    p.nombre_completo = lambda: f"{p.apellido}, {p.nombre}"
    return p


def _bridge(session):
    return SupabaseBridge(SupabaseCredenciales("https://x.supabase.co/", "srv-key"), session=session)


def test_mapeo_y_telefono_duplicado_queda_sin_telefono():
    filas = SupabaseBridge.mapear_personal([
        _persona(1, "0358 15 564-6300", legajo="59-027", id_ruba=10),
        _persona(2, "3585646300", activo=False, legajo=""),
    ])
    assert filas[0] == {"id": 1, "legajo": "59-027", "id_ruba": 10, "nombre_completo": "Perez1, Ana",
                        "jerarquia": None, "telefono_whatsapp": "5493585646300",
                        "rol": "BOMBERO", "activo": True}
    assert filas[1]["telefono_whatsapp"] is None and filas[1]["legajo"] is None and not filas[1]["activo"]


def test_upsert_headers_y_url():
    session = requests.Session()
    resp = mock.Mock(status_code=201)
    resp.raise_for_status.return_value = None
    with mock.patch.object(session, "post", return_value=resp) as post:
        assert _bridge(session).upsert_personal([{"id": 1}]) == 1
    args, kwargs = post.call_args
    assert args[0] == "https://x.supabase.co/rest/v1/personal_cloud"
    assert kwargs["params"] == {"on_conflict": "id"}
    assert "resolution=merge-duplicates" in kwargs["headers"]["Prefer"]
    assert kwargs["timeout"]
    assert session.headers["apikey"] == "srv-key"
    assert session.headers["Authorization"] == "Bearer srv-key"


@pytest.mark.parametrize("error", [requests.exceptions.Timeout(), requests.exceptions.ConnectionError("x")])
def test_upsert_errores_de_red(error):
    session = requests.Session()
    with mock.patch.object(session, "post", side_effect=error):
        with pytest.raises(SupabaseSyncError):
            _bridge(session).upsert_personal([{"id": 1}])


def test_upsert_error_http():
    session = requests.Session()
    resp = requests.Response()
    resp.status_code = 409
    resp._content = b'{"message":"duplicate key"}'
    with mock.patch.object(session, "post", return_value=resp):
        with pytest.raises(SupabaseSyncError, match="409"):
            _bridge(session).upsert_personal([{"id": 1}])


def test_credenciales_env_y_faltantes(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.supabase_client.get_writable_dir", lambda _: tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SupabaseConfigError):
        cargar_credenciales()
    (tmp_path / "config.json").write_text('{"supabase": {"url": "https://cfg.co/", "service_key": "k1"}}',
                                          encoding="utf-8")
    assert cargar_credenciales() == SupabaseCredenciales("https://cfg.co", "k1")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k-env")
    assert cargar_credenciales().service_key == "k-env"

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from app.services import gee_service
from app.services.gee_service import (
    GeeApiError,
    GeeAuthError,
    GeeConfigError,
    GeeService,
    GeeSinConexion,
    cargar_info_cuenta_servicio,
    payload_mapa,
)

PAYLOAD = payload_mapa({"result": "0", "values": {"0": {"constantValue": 1}}}, file_format="PNG",
                       visualizacion={"ranges": [{"min": 0, "max": 1}]})


def _utc_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # google-auth guarda `expiry` naive en UTC


class CredFalsas:
    """Imita google.oauth2.service_account.Credentials."""

    def __init__(self, error=None):
        self.token = None
        self.expiry = None
        self.refrescos = 0
        self._error = error

    @property
    def valid(self):
        return self.token is not None and (self.expiry is None or self.expiry > _utc_naive())

    def refresh(self, _request):
        if self._error:
            raise self._error
        self.refrescos += 1
        self.token = f"tok-{self.refrescos}"
        self.expiry = _utc_naive() + timedelta(hours=1)


def _resp(status, cuerpo=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(cuerpo).encode() if cuerpo is not None else b""
    r.reason = "x"
    return r


def _servicio(respuestas, cred=None):
    session = requests.Session()
    cred = cred or CredFalsas()
    srv = GeeService(credenciales=cred, project_id="mi-proyecto", session=session)
    pedir = mock.patch.object(session, "request", side_effect=respuestas)
    return srv, cred, pedir


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(gee_service.time, "sleep", lambda s: None)
    monkeypatch.delenv("GEE_PROJECT_ID", raising=False)


def test_solicitar_mapa_devuelve_url_format():
    srv, cred, pedir = _servicio([_resp(200, {"name": "projects/mi-proyecto/maps/abc123"})])
    with pedir as req:
        mapa = srv.solicitar_mapa(PAYLOAD)
    assert mapa.url_format == "https://earthengine.googleapis.com/v1/projects/mi-proyecto/maps/abc123/tiles/{z}/{x}/{y}"
    assert mapa.url_tesela(5, 10, 20).endswith("/tiles/5/10/20")
    args, kwargs = req.call_args
    assert args == ("POST", "https://earthengine.googleapis.com/v1/projects/mi-proyecto/maps")
    assert kwargs["json"]["fileFormat"] == "PNG" and "expression" in kwargs["json"]
    assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
    assert cred.refrescos == 1


def test_usa_url_format_de_la_respuesta_si_viene():
    srv, _, pedir = _servicio([_resp(200, {"name": "projects/p/maps/x", "urlFormat": "https://t/{z}/{x}/{y}"})])
    with pedir:
        assert srv.solicitar_mapa(PAYLOAD).url_format == "https://t/{z}/{x}/{y}"


def test_token_expirado_se_renueva_y_reintenta():
    srv, cred, pedir = _servicio([_resp(401, {"error": {"message": "expired"}}),
                                  _resp(200, {"name": "projects/p/maps/x"})])
    with pedir as req:
        srv.solicitar_mapa(PAYLOAD)
    assert cred.refrescos == 2
    assert [c.kwargs["headers"]["Authorization"] for c in req.call_args_list] == ["Bearer tok-1", "Bearer tok-2"]


def test_token_por_vencer_se_renueva_antes_de_llamar():
    cred = CredFalsas()
    cred.token, cred.expiry = "viejo", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=10)
    srv, _, pedir = _servicio([_resp(200, {"name": "projects/p/maps/x"})], cred)
    with pedir as req:
        srv.solicitar_mapa(PAYLOAD)
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-1"


def test_401_persistente_es_error_de_autenticacion():
    srv, _, pedir = _servicio([_resp(401, {"error": {"message": "no"}})] * 2)
    with pedir, pytest.raises(GeeAuthError):
        srv.solicitar_mapa(PAYLOAD)


def test_errores_transitorios_reintentan_con_backoff():
    srv, _, pedir = _servicio([_resp(503), requests.exceptions.ConnectionError("caida"),
                               _resp(200, {"name": "projects/p/maps/x"})])
    with pedir as req:
        srv.solicitar_mapa(PAYLOAD)
    assert req.call_count == 3


def test_sin_red_tras_reintentos():
    srv, _, pedir = _servicio([requests.exceptions.Timeout()] * 3)
    with pedir, pytest.raises(GeeSinConexion):
        srv.solicitar_mapa(PAYLOAD)


def test_error_de_api_con_mensaje_de_google():
    cuerpo = {"error": {"code": 400, "message": "Invalid expression", "status": "INVALID_ARGUMENT"}}
    srv, _, pedir = _servicio([_resp(400, cuerpo)])
    with pedir, pytest.raises(GeeApiError) as exc:
        srv.solicitar_mapa(PAYLOAD)
    assert exc.value.status_http == 400 and exc.value.status_api == "INVALID_ARGUMENT"
    assert "Invalid expression" in str(exc.value)


def test_403_explica_permisos():
    srv, _, pedir = _servicio([_resp(403, {"error": {"message": "denied", "status": "PERMISSION_DENIED"}})])
    with pedir, pytest.raises(GeeApiError, match="permiso"):
        srv.solicitar_mapa(PAYLOAD)


def test_refresh_rechazado_por_google():
    from google.auth.exceptions import RefreshError

    srv, _, pedir = _servicio([], CredFalsas(error=RefreshError("invalid_grant")))
    with pedir, pytest.raises(GeeAuthError, match="invalid_grant"):
        srv.solicitar_mapa(PAYLOAD)


def test_payload_sin_expresion():
    srv, _, pedir = _servicio([])
    with pedir as req, pytest.raises(GeeApiError):
        srv.solicitar_mapa({"fileFormat": "PNG"})
    req.assert_not_called()


# -- Credenciales ----------------------------------------------------------------------

def _clave(tmp_path, **cambios):
    datos = {"type": "service_account", "project_id": "proy-json", "client_email": "bot@proy.iam.gserviceaccount.com",
             "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
             "token_uri": "https://oauth2.googleapis.com/token", **cambios}
    ruta = tmp_path / "gee_service_account.json"
    ruta.write_text(json.dumps(datos), encoding="utf-8")
    return ruta


def test_credenciales_desde_variable_de_entorno_con_ruta(tmp_path, monkeypatch):
    monkeypatch.delenv("GEE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GEE_SERVICE_ACCOUNT_FILE", str(_clave(tmp_path)))
    assert cargar_info_cuenta_servicio()["project_id"] == "proy-json"


def test_credenciales_desde_json_en_entorno(monkeypatch, tmp_path):
    monkeypatch.setenv("GEE_SERVICE_ACCOUNT_JSON", _clave(tmp_path).read_text(encoding="utf-8"))
    assert cargar_info_cuenta_servicio()["client_email"].startswith("bot@")


def test_credenciales_faltantes_o_invalidas(tmp_path, monkeypatch):
    for var in ("GEE_SERVICE_ACCOUNT_JSON", "GEE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gee_service, "ruta_credenciales_por_defecto", lambda: tmp_path / "no_existe.json")
    with pytest.raises(GeeConfigError, match="No se encontraron"):
        cargar_info_cuenta_servicio()
    with pytest.raises(GeeConfigError, match="service_account"):
        cargar_info_cuenta_servicio(_clave(tmp_path, type="authorized_user"))
    with pytest.raises(GeeConfigError, match="faltan"):
        cargar_info_cuenta_servicio(_clave(tmp_path, private_key=""))


def test_clave_privada_mal_formada_es_error_de_configuracion(tmp_path):
    info = json.loads(_clave(tmp_path).read_text(encoding="utf-8"))
    with pytest.raises(GeeConfigError, match="inválida"):
        GeeService(info_cuenta=info)


def test_cuenta_de_servicio_real_firma_con_rsa():
    """Con una clave RSA válida se construyen Credentials de google-auth con los scopes de EE."""
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    srv = GeeService(info_cuenta={"type": "service_account", "project_id": "p", "private_key": clave,
                                  "client_email": "a@p.iam.gserviceaccount.com",
                                  "token_uri": "https://oauth2.googleapis.com/token"})
    assert srv.project_id == "p"
    assert "https://www.googleapis.com/auth/earthengine" in srv._cred.scopes

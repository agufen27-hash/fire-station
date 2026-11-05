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
    GeePayloadError,
    GeeService,
    GeeSinConexion,
    cargar_info_cuenta_servicio,
    expresion_asset,
    payload_asset,
    payload_mapa,
    validar_payload_mapa,
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
    cuerpo = json.loads(kwargs["data"])
    assert cuerpo["fileFormat"] == "PNG" and "expression" in cuerpo
    assert kwargs["params"] == {"fields": "name"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok-1"
    assert kwargs["headers"]["Content-Type"] == "application/json"
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


# -- Esquema del payload de projects.maps.create (API v1) -------------------------------

SRTM_ESPERADO = {
    "expression": {
        "values": {"0": {"functionInvocationValue": {
            "functionName": "Image.load",
            "arguments": {"id": {"constantValue": "USGS/SRTMGL1_003"}},
        }}},
        "result": "0",
    },
    "fileFormat": "PNG",
    "bandIds": ["elevation"],
    "visualizationOptions": {
        "ranges": [{"min": 0, "max": 3000}],
        "paletteColors": ["006633", "E5FFCC", "662A00", "D8D8D8", "F5F5F5"],
        "gamma": 1.2,
        "opacity": 0.8,
    },
}


def _expr(values, result="0"):
    return {"expression": {"values": values, "result": result}}


def test_payload_asset_srtm_coincide_con_el_esquema_v1():
    payload = payload_asset("USGS/SRTMGL1_003", minimo=0, maximo=3000, banda="elevation",
                            paleta=SRTM_ESPERADO["visualizationOptions"]["paletteColors"],
                            gamma=1.2, opacidad=0.8)
    assert payload == SRTM_ESPERADO


def test_asset_srtm_se_envia_tal_cual_con_respuesta_mockeada():
    srv, _, pedir = _servicio([_resp(200, {"name": "projects/mi-proyecto/maps/srtm1"})])
    with pedir as req:
        mapa = srv.solicitar_mapa(SRTM_ESPERADO)
    kwargs = req.call_args.kwargs
    enviado = json.loads(kwargs["data"].decode("utf-8"))
    assert enviado == SRTM_ESPERADO                      # sin campos agregados ni perdidos
    assert set(enviado) <= {"expression", "fileFormat", "bandIds", "visualizationOptions"}
    assert "json" not in kwargs and kwargs["params"] == {"fields": "name"}
    assert mapa.url_format.endswith("/v1/projects/mi-proyecto/maps/srtm1/tiles/{z}/{x}/{y}")


def test_expresion_asset_con_version_usa_integer_value_string():
    expr = expresion_asset("USGS/SRTMGL1_003", version=3)
    args = expr["values"]["0"]["functionInvocationValue"]["arguments"]
    assert args == {"id": {"constantValue": "USGS/SRTMGL1_003"}, "version": {"integerValue": "3"}}


def test_normaliza_integer_value_y_descarta_visualizacion_vacia():
    cuerpo = validar_payload_mapa({**_expr({"0": {"integerValue": 7}}), "visualizationOptions": {}})
    assert cuerpo == {"expression": {"values": {"0": {"integerValue": "7"}}, "result": "0"}}


def test_expresion_compuesta_valida_con_referencias_y_funciones():
    payload = {
        **_expr({
            "img": {"functionInvocationValue": {"functionName": "Image.load",
                                                "arguments": {"id": {"constantValue": "USGS/SRTMGL1_003"}}}},
            "f": {"functionDefinitionValue": {"argumentNames": ["x"], "body": "cuerpo"}},
            "cuerpo": {"functionInvocationValue": {"functionName": "Image.select",
                                                   "arguments": {"input": {"argumentReference": "x"},
                                                                 "bandSelectors": {"arrayValue": {"values": [
                                                                     {"constantValue": "elevation"}]}}}}},
            "0": {"functionInvocationValue": {"functionReference": "f",
                                              "arguments": {"x": {"valueReference": "img"}}}},
        }),
        "fileFormat": "AUTO_JPEG_PNG",
    }
    assert validar_payload_mapa(payload) == payload


@pytest.mark.parametrize("payload, fragmento", [
    ({**_expr({"0": {"constantValue": 1}}), "name": "projects/p/maps/x"}, "campo de salida"),
    ({**_expr({"0": {"constantValue": 1}}), "min": 0}, "desconocido"),
    ({**_expr({"0": {"constantValue": 1}}), "palette": ["red"]}, "desconocido"),
    ({"fileFormat": "PNG"}, "falta la expresión"),
    ({"expression": {"values": {"0": {"constantValue": 1}}, "result": "0", "extra": 1}}, "desconocido"),
    (_expr({"0": {"constantValue": 1}}, result="1"), "no es una clave"),
    (_expr({}), "no vacío"),
    (_expr({"0": {}}), "exactamente una variante"),
    (_expr({"0": {"constantValue": 1, "integerValue": "1"}}), "exactamente una variante"),
    (_expr({"0": {"nullValue": None}}), "desconocido"),
    (_expr({"0": {"constantValue": float("nan")}}), "número finito"),
    (_expr({"0": {"integerValue": "1.5"}}), "entero"),
    (_expr({"0": {"bytesValue": "no es base64!"}}), "base64"),
    (_expr({"0": {"valueReference": "nada"}}), "no existe"),
    (_expr({"0": {"valueReference": "1"}, "1": {"valueReference": "0"}}), "cíclicas"),
    (_expr({"0": {"functionInvocationValue": {"arguments": {}}}}), "uno y solo uno"),
    (_expr({"0": {"functionInvocationValue": {"functionName": "Image.load", "functionReference": "0"}}}),
     "uno y solo uno"),
    (_expr({"0": {"functionInvocationValue": {"functionName": "Image.load", "args": {}}}}), "desconocido"),
    ({**_expr({"0": {"constantValue": 1}}), "fileFormat": "TF_RECORD_IMAGE"}, "no sirve para teselas"),
    ({**_expr({"0": {"constantValue": 1}}), "fileFormat": "png"}, "no sirve para teselas"),
    ({**_expr({"0": {"constantValue": 1}}), "fileFormat": "PNG", "bandIds": ["a", "b"]}, "1 o 3 bandas"),
    ({**_expr({"0": {"constantValue": 1}}), "bandIds": []}, "lista no vacía"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"min": 0}}, "desconocido"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"gamma": {"value": 1}}}, "número finito"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"gamma": 0}}, "mayor que 0"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"opacity": 1.5}}, "entre 0 y 1"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"ranges": [{"min": 0}]}}, "'min' y 'max'"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"ranges": [{"min": 0, "max": True}]}},
     "número finito"),
    ({**_expr({"0": {"constantValue": 1}}), "bandIds": ["r", "g", "b"],
      "visualizationOptions": {"paletteColors": ["red", "blue"]}}, "1 banda"),
    ({**_expr({"0": {"constantValue": 1}}), "bandIds": ["r", "g", "b"],
      "visualizationOptions": {"ranges": [{"min": 0, "max": 1}] * 2}}, "rangos para 3 bandas"),
    ({**_expr({"0": {"constantValue": 1}}), "visualizationOptions": {"paletteColors": ["rgb(1,2,3)"]}},
     "color CSS"),
])
def test_payload_invalido_se_rechaza_sin_llamar_a_la_api(payload, fragmento):
    srv, _, pedir = _servicio([])
    with pedir as req, pytest.raises(GeePayloadError, match=fragmento.replace("(", r"\(").replace(")", r"\)")):
        srv.solicitar_mapa(payload)
    req.assert_not_called()


def test_payload_error_sigue_siendo_gee_api_error():
    with pytest.raises(GeeApiError):
        validar_payload_mapa({"expression": {"values": {"0": {}}, "result": "0"}})


def test_400_del_servidor_con_payload_valido_se_traduce():
    cuerpo = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                        "message": "Image.load: Image asset 'USGS/NO_EXISTE' not found."}}
    srv, _, pedir = _servicio([_resp(400, cuerpo)])
    with pedir, pytest.raises(GeeApiError) as exc:
        srv.solicitar_mapa(payload_asset("USGS/NO_EXISTE", minimo=0, maximo=1))
    assert not isinstance(exc.value, GeePayloadError)
    assert exc.value.status_api == "INVALID_ARGUMENT" and "not found" in str(exc.value)

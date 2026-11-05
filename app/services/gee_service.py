"""
Cliente de la API REST de Google Earth Engine (v1) para la app de escritorio.

Autenticación: OAuth 2.0 con una CUENTA DE SERVICIO de Google Cloud
(`google.oauth2.service_account.Credentials`). La clave privada NUNCA va en
el código ni dentro del .exe; se busca, en este orden:

  1. GEE_SERVICE_ACCOUNT_JSON   contenido JSON completo de la clave (útil en CI)
  2. GEE_SERVICE_ACCOUNT_FILE   ruta a la clave .json
     (o GOOGLE_APPLICATION_CREDENTIALS, la variable estándar de Google)
  3. config/gee_service_account.json   al lado del .exe (o en la raíz del
     proyecto en desarrollo). Está en .gitignore.

Proyecto de Cloud: GEE_PROJECT_ID, o el `project_id` de la propia clave.
La cuenta de servicio debe estar registrada para usar Earth Engine en ese
proyecto (rol "Earth Engine Resource Viewer" o superior).

Uso:
    servicio = GeeService()
    mapa = servicio.solicitar_mapa({"expression": {...}, "fileFormat": "PNG"})
    mapa.url_format   # https://earthengine.googleapis.com/v1/projects/.../maps/.../tiles/{z}/{x}/{y}

Todas las llamadas son BLOQUEANTES: desde la UI se corren en un QThread.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import requests

from app.paths import get_writable_dir

log = logging.getLogger(__name__)

GEE_API_URL = "https://earthengine.googleapis.com/v1"
GEE_SCOPES = (
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform",
)
ARCHIVO_CREDENCIALES = "gee_service_account.json"
TIMEOUT_SEGUNDOS = (5, 30)            # (conexión, lectura)
REINTENTOS_TRANSITORIOS = 2           # 429 / 500 / 502 / 503 / 504
ESPERA_BASE_SEGUNDOS = 1.0            # backoff exponencial: 1 s, 2 s
HTTP_TRANSITORIOS = {429, 500, 502, 503, 504}
MARGEN_EXPIRACION = 60                # renovar el token si vence en < 60 s


# -- Errores ----------------------------------------------------------------------

class GeeError(RuntimeError):
    """Base de todos los errores de este módulo (texto listo para la UI)."""


class GeeConfigError(GeeError):
    """No hay credenciales, la clave es ilegible o falta el project id."""


class GeeAuthError(GeeError):
    """Google rechazó la cuenta de servicio o no se pudo renovar el token."""


class GeeSinConexion(GeeError):
    """Sin red, timeout o servicio no disponible tras los reintentos."""


class GeeApiError(GeeError):
    """La API respondió con un error (payload inválido, permisos, cuota...)."""

    def __init__(self, mensaje: str, status_http: Optional[int] = None,
                 status_api: Optional[str] = None) -> None:
        super().__init__(mensaje)
        self.status_http = status_http
        self.status_api = status_api


# -- Resultado ----------------------------------------------------------------------

@dataclass(frozen=True)
class MapaGee:
    """Mapa creado por `projects.maps.create`."""
    name: str          # projects/{project}/maps/{map-id}
    url_format: str    # plantilla XYZ de teselas: .../tiles/{z}/{x}/{y}

    def url_tesela(self, z: int, x: int, y: int) -> str:
        return self.url_format.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


# -- Credenciales ---------------------------------------------------------------------

def ruta_credenciales_por_defecto() -> Path:
    return get_writable_dir("config") / ARCHIVO_CREDENCIALES


def cargar_info_cuenta_servicio(ruta: Optional[Path] = None) -> dict:
    """Devuelve el dict de la clave de la cuenta de servicio (ver el orden de
    búsqueda en el docstring del módulo)."""
    if ruta is None:
        crudo = os.environ.get("GEE_SERVICE_ACCOUNT_JSON", "").strip()
        if crudo:
            try:
                return _validar_info(json.loads(crudo), "GEE_SERVICE_ACCOUNT_JSON")
            except json.JSONDecodeError as exc:
                raise GeeConfigError("GEE_SERVICE_ACCOUNT_JSON no contiene un JSON válido.") from exc
        archivo = (os.environ.get("GEE_SERVICE_ACCOUNT_FILE", "").strip()
                   or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())
        ruta = Path(archivo) if archivo else ruta_credenciales_por_defecto()

    ruta = Path(ruta)
    if not ruta.is_file():
        raise GeeConfigError(
            "No se encontraron las credenciales de Google Earth Engine.\n"
            f"Copiá la clave JSON de la cuenta de servicio en:\n{ruta}\n"
            "o definí la variable de entorno GEE_SERVICE_ACCOUNT_FILE."
        )
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GeeConfigError(f"No se pudo leer la clave de la cuenta de servicio ({ruta}): {exc}") from exc
    return _validar_info(datos, str(ruta))


def _validar_info(datos: Any, origen: str) -> dict:
    if not isinstance(datos, dict) or datos.get("type") != "service_account":
        raise GeeConfigError(f"{origen} no es una clave de cuenta de servicio de Google (type != service_account).")
    faltan = [c for c in ("client_email", "private_key", "token_uri") if not datos.get(c)]
    if faltan:
        raise GeeConfigError(f"A la clave de {origen} le faltan campos: {', '.join(faltan)}.")
    return datos


# -- Servicio ----------------------------------------------------------------------------

class GeeService:
    """Cliente REST de Earth Engine con renovación automática del token."""

    def __init__(self, info_cuenta: Optional[Mapping[str, Any]] = None,
                 project_id: Optional[str] = None,
                 session: Optional[requests.Session] = None,
                 credenciales=None,
                 timeout=TIMEOUT_SEGUNDOS) -> None:
        """`credenciales` permite inyectar un objeto de google-auth ya creado
        (pruebas); si no, se arma desde `info_cuenta` o desde la config."""
        self._http = session or requests.Session()
        self._timeout = timeout
        self._lock = threading.Lock()

        if credenciales is None:
            info = dict(info_cuenta) if info_cuenta is not None else cargar_info_cuenta_servicio()
            credenciales = self._crear_credenciales(info)
            project_id = project_id or os.environ.get("GEE_PROJECT_ID", "").strip() or info.get("project_id")
        else:
            project_id = project_id or os.environ.get("GEE_PROJECT_ID", "").strip()
        if not project_id:
            raise GeeConfigError("Falta el ID del proyecto de Google Cloud (GEE_PROJECT_ID o project_id en la clave).")
        self.project_id = project_id
        self._cred = credenciales

    @staticmethod
    def _crear_credenciales(info: dict):
        from google.oauth2 import service_account

        try:
            return service_account.Credentials.from_service_account_info(info, scopes=list(GEE_SCOPES))
        except (ValueError, KeyError) as exc:  # clave privada mal formada
            raise GeeConfigError(f"La clave privada de la cuenta de servicio es inválida: {exc}") from exc

    # -- Token ---------------------------------------------------------------------

    def _token(self, forzar: bool = False) -> str:
        """Access token vigente; lo renueva si venció, está por vencer o `forzar`."""
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request

        with self._lock:
            expira = getattr(self._cred, "expiry", None)
            if expira is not None and expira.tzinfo is None:
                expira = expira.replace(tzinfo=timezone.utc)  # google-auth guarda `expiry` naive en UTC
            por_vencer = expira is not None and expira.timestamp() - time.time() < MARGEN_EXPIRACION
            if forzar or not self._cred.valid or por_vencer:
                try:
                    self._cred.refresh(Request(session=self._http))
                except RefreshError as exc:
                    raise GeeAuthError(
                        "Google rechazó la cuenta de servicio de Earth Engine "
                        f"(clave revocada, reloj de la PC desfasado o cuenta deshabilitada): {exc}"
                    ) from exc
                except TransportError as exc:
                    raise GeeSinConexion(f"Sin conexión con Google para renovar el token: {exc}") from exc
            return self._cred.token

    # -- HTTP --------------------------------------------------------------------------

    def _pedir(self, metodo: str, ruta: str, **kwargs) -> dict:
        """Llamada autenticada. Renueva el token y reintenta UNA vez ante 401;
        reintenta con backoff los errores transitorios (429 / 5xx / red)."""
        url = f"{GEE_API_URL}/{ruta.lstrip('/')}"
        renovado = False
        intento = 0
        while True:
            headers = {"Authorization": f"Bearer {self._token()}",
                       "Content-Type": "application/json"}
            try:
                resp = self._http.request(metodo, url, headers=headers, timeout=self._timeout, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if intento < REINTENTOS_TRANSITORIOS:
                    intento += 1
                    time.sleep(ESPERA_BASE_SEGUNDOS * 2 ** (intento - 1))
                    continue
                raise GeeSinConexion(f"Sin conexión con Google Earth Engine: {exc}") from exc
            except requests.exceptions.RequestException as exc:
                raise GeeSinConexion(f"Error de red con Google Earth Engine: {exc}") from exc

            if resp.status_code == 401 and not renovado:
                # Token expirado o revocado entre la validación y la llamada.
                log.info("Earth Engine respondió 401: se renueva el token y se reintenta.")
                renovado, intento = True, 0
                self._token(forzar=True)
                continue
            if resp.status_code in HTTP_TRANSITORIOS and intento < REINTENTOS_TRANSITORIOS:
                intento += 1
                time.sleep(ESPERA_BASE_SEGUNDOS * 2 ** (intento - 1))
                continue
            if resp.status_code >= 400:
                raise self._error_api(resp)
            try:
                return resp.json() if resp.content else {}
            except ValueError as exc:
                raise GeeApiError(f"Respuesta no JSON de Earth Engine: {resp.text[:200]}",
                                  resp.status_code) from exc

    @staticmethod
    def _error_api(resp: requests.Response) -> GeeError:
        """Traduce el formato de error de Google ({"error": {code, message, status}})."""
        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        mensaje = error.get("message") or resp.text[:300] or resp.reason
        status_api = error.get("status")
        codigo = resp.status_code
        if codigo in HTTP_TRANSITORIOS:
            return GeeSinConexion(f"Earth Engine no disponible ({codigo}): {mensaje}")
        if codigo == 401:
            return GeeAuthError(f"Earth Engine rechazó el token de la cuenta de servicio: {mensaje}")
        if codigo == 403:
            return GeeApiError(
                "La cuenta de servicio no tiene permiso para Earth Engine en el proyecto "
                f"(¿está registrada y con la API habilitada?): {mensaje}", codigo, status_api)
        return GeeApiError(f"Earth Engine respondió {codigo} {status_api or ''}: {mensaje}".replace("  ", " "),
                           codigo, status_api)

    # -- API pública -----------------------------------------------------------------------

    def solicitar_mapa(self, payload: Mapping[str, Any]) -> MapaGee:
        """`POST /v1/projects/{project}/maps` con un payload de Earth Engine
        (`{"expression": {...}, "fileFormat": ..., "bandIds": [...],
        "visualizationOptions": {...}}`) y devuelve el mapa con su plantilla
        de teselas XYZ (`urlFormat`)."""
        if not isinstance(payload, Mapping) or not isinstance(payload.get("expression"), Mapping):
            raise GeeApiError("El payload del mapa debe incluir un objeto 'expression' de Earth Engine.")
        datos = self._pedir("POST", f"projects/{self.project_id}/maps", json=dict(payload))
        nombre = datos.get("name")
        if not nombre:
            raise GeeApiError(f"Earth Engine no devolvió el nombre del mapa: {json.dumps(datos)[:200]}")
        url_format = datos.get("urlFormat") or f"{GEE_API_URL}/{nombre}/tiles/{{z}}/{{x}}/{{y}}"
        log.info("Mapa de Earth Engine creado: %s", nombre)
        return MapaGee(name=nombre, url_format=url_format)


def payload_mapa(expression: Mapping[str, Any], *, file_format: str = "AUTO_JPEG_PNG",
                 band_ids: Optional[list] = None,
                 visualizacion: Optional[Mapping[str, Any]] = None) -> dict:
    """Arma el cuerpo de `projects.maps.create` a partir de una expresión
    serializada de Earth Engine."""
    cuerpo: dict = {"expression": dict(expression), "fileFormat": file_format}
    if band_ids:
        cuerpo["bandIds"] = list(band_ids)
    if visualizacion:
        cuerpo["visualizationOptions"] = dict(visualizacion)
    return cuerpo

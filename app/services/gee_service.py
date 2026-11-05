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
    mapa = servicio.solicitar_mapa(payload_asset("USGS/SRTMGL1_003", minimo=0, maximo=3000,
                                                 banda="elevation"))
    # o un EarthEngineMap armado a mano: {"expression": {...}, "fileFormat": "PNG", ...}
    # (se valida contra el esquema v1 antes de enviarlo: ver validar_payload_mapa)
    mapa.url_format   # https://earthengine.googleapis.com/v1/projects/.../maps/.../tiles/{z}/{x}/{y}

Todas las llamadas son BLOQUEANTES: desde la UI se corren en un QThread.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

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
TIMEOUT_CALCULO = (5, 240)            # computePixels: un compuesto de meses puede tardar minutos
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


class GeePayloadError(GeeApiError):
    """El payload no respeta el esquema de la API v1: se rechaza ANTES de
    enviarlo (Google respondería 400 INVALID_ARGUMENT)."""


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

    def _pedir(self, metodo: str, ruta: str, *, crudo: bool = False, timeout=None, **kwargs):
        """Llamada autenticada. Renueva el token y reintenta UNA vez ante 401;
        reintenta con backoff los errores transitorios (429 / 5xx / red).
        Devuelve el JSON de la respuesta, o los bytes tal cual si `crudo`
        (píxeles de computePixels)."""
        url = f"{GEE_API_URL}/{ruta.lstrip('/')}"
        renovado = False
        intento = 0
        while True:
            headers = {"Authorization": f"Bearer {self._token()}",
                       "Content-Type": "application/json"}
            try:
                resp = self._http.request(metodo, url, headers=headers, timeout=timeout or self._timeout,
                                          **kwargs)
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
            if crudo:
                return resp.content
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
        """`POST /v1/projects/{project}/maps` con un EarthEngineMap
        (`{"expression": {...}, "fileFormat": ..., "bandIds": [...],
        "visualizationOptions": {...}}`) y devuelve el mapa con su plantilla
        de teselas XYZ (`urlFormat`).

        El payload se valida y normaliza con `validar_payload_mapa` (un error
        de esquema es `GeePayloadError`, sin llamar a la API) y se serializa
        como JSON estricto (sin NaN / Infinity). `fields=name`: Google
        devuelve solo el nombre del mapa en vez de repetir todo el pedido."""
        cuerpo = validar_payload_mapa(payload)
        datos = self._pedir(
            "POST", f"projects/{self.project_id}/maps",
            params={"fields": "name"},
            data=json.dumps(cuerpo, allow_nan=False, ensure_ascii=False).encode("utf-8"),
        )
        nombre = datos.get("name")
        if not nombre:
            raise GeeApiError(f"Earth Engine no devolvió el nombre del mapa: {json.dumps(datos)[:200]}")
        url_format = datos.get("urlFormat") or f"{GEE_API_URL}/{nombre}/tiles/{{z}}/{{x}}/{{y}}"
        log.info("Mapa de Earth Engine creado: %s", nombre)
        return MapaGee(name=nombre, url_format=url_format)

    def calcular_pixeles(self, cuerpo: Mapping[str, Any], timeout=TIMEOUT_CALCULO) -> bytes:
        """`POST /v1/projects/{project}/image:computePixels`: calcula la
        expresión sobre la grilla pedida y devuelve los píxeles codificados
        en `fileFormat` (p. ej. los bytes de un PNG). Ver `validar_pedido_pixeles`."""
        cuerpo = validar_pedido_pixeles(cuerpo)
        datos = self._pedir(
            "POST", f"projects/{self.project_id}/image:computePixels", crudo=True, timeout=timeout,
            data=json.dumps(cuerpo, allow_nan=False, ensure_ascii=False).encode("utf-8"),
        )
        if not datos:
            raise GeeApiError("Earth Engine devolvió una imagen vacía.")
        return datos


# =============================================================================
# Esquema de la API v1 (developers.google.com/earth-engine/reference/rest/v1)
#
#   EarthEngineMap        {expression, fileFormat, bandIds[], visualizationOptions}
#                         (`name` es de salida: no va en el pedido)
#   Expression            {values: {clave: ValueNode}, result: clave}
#   ValueNode             EXACTAMENTE una variante de VARIANTES_VALUE_NODE
#   FunctionInvocation    {functionName | functionReference, arguments: {nombre: ValueNode}}
#   FunctionDefinition    {argumentNames: [str], body: clave}
#   VisualizationOptions  {ranges: [{min, max}], paletteColors: [str], gamma: número, opacity: 0-1}
#
# Un asset se carga como lo serializa el cliente oficial (ee.Image('ID')):
#   {"functionInvocationValue": {"functionName": "Image.load",
#                                "arguments": {"id": {"constantValue": "USGS/SRTMGL1_003"}}}}
# =============================================================================

CAMPOS_MAPA = {"expression", "fileFormat", "bandIds", "visualizationOptions"}
CAMPOS_EXPRESION = {"values", "result"}
VARIANTES_VALUE_NODE = {
    "constantValue", "integerValue", "bytesValue", "arrayValue", "dictionaryValue",
    "functionDefinitionValue", "functionInvocationValue", "argumentReference", "valueReference",
}
CAMPOS_VISUALIZACION = {"ranges", "paletteColors", "gamma", "opacity"}
# ImageFileFormat válidos para teselas (TF_RECORD_IMAGE es solo de exportación y
# ZIPPED_GEO_TIFF* solo de miniaturas; UNSPECIFIED no es un valor útil).
FORMATOS_MAPA = {"JPEG", "PNG", "AUTO_JPEG_PNG", "NPY", "GEO_TIFF"}
FORMATOS_8_BITS = {"JPEG", "PNG", "AUTO_JPEG_PNG"}   # salida de 1 o 3 bandas
# projects.image.computePixels
CAMPOS_PIXELES = {"expression", "fileFormat", "grid", "bandIds", "visualizationOptions", "workloadTag"}
FORMATOS_PIXELES = FORMATOS_MAPA
CAMPOS_GRILLA = {"dimensions", "affineTransform", "crsCode", "crsWkt"}
CAMPOS_AFIN = {"scaleX", "shearX", "translateX", "shearY", "scaleY", "translateY"}
MAX_LADO_PIXELES = 32768                   # "at most 32K pixels in either dimension"
MAX_BYTES_PIXELES = 48 * 1024 * 1024       # "limited to 48MB in uncompressed data per request"
_RE_COLOR = re.compile(r"^(#?[0-9A-Fa-f]{3}|#?[0-9A-Fa-f]{6}|[A-Za-z]+)$")
_RE_ENTERO = re.compile(r"^-?\d+$")


def _error(ruta: str, mensaje: str) -> GeePayloadError:
    return GeePayloadError(f"Payload de Earth Engine inválido en '{ruta}': {mensaje}")


def _es_numero(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numero_finito(v: Any, ruta: str) -> float:
    if not _es_numero(v) or not math.isfinite(v):
        raise _error(ruta, f"se esperaba un número finito y llegó {v!r}")
    return v


def _solo_campos(obj: Any, permitidos: set, ruta: str) -> Mapping:
    if not isinstance(obj, Mapping):
        raise _error(ruta, f"se esperaba un objeto JSON y llegó {type(obj).__name__}")
    desconocidos = sorted(set(obj) - permitidos)
    if desconocidos:
        raise _error(ruta, f"campo(s) desconocido(s) {desconocidos}; permitidos: {sorted(permitidos)}")
    return obj


def _json_constante(v: Any, ruta: str) -> Any:
    """constantValue = google.protobuf.Value: null, bool, número finito, string,
    lista u objeto con claves string. Devuelve una copia JSON pura."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if _es_numero(v):
        return _numero_finito(v, ruta)
    if isinstance(v, Mapping):
        if not all(isinstance(k, str) for k in v):
            raise _error(ruta, "las claves de un objeto constante deben ser strings")
        return {k: _json_constante(x, f"{ruta}.{k}") for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_constante(x, f"{ruta}[{i}]") for i, x in enumerate(v)]
    raise _error(ruta, f"tipo no serializable como constante: {type(v).__name__}")


class _ValidadorExpresion:
    def __init__(self, valores: Mapping[str, Any]) -> None:
        self.claves = set(valores)
        self.referencias: dict = {k: set() for k in valores}   # para detectar ciclos

    def nodo(self, nodo: Any, ruta: str, origen: str) -> dict:
        nodo = _solo_campos(nodo, VARIANTES_VALUE_NODE, ruta)
        if len(nodo) != 1:
            raise _error(ruta, f"un ValueNode lleva exactamente una variante y tiene {len(nodo)}: {sorted(nodo)}")
        (variante, v), = nodo.items()
        r = f"{ruta}.{variante}"
        if variante == "constantValue":
            return {variante: _json_constante(v, r)}
        if variante == "integerValue":  # int64 -> string en JSON
            if _es_numero(v) and float(v).is_integer():
                v = str(int(v))
            if not isinstance(v, str) or not _RE_ENTERO.match(v):
                raise _error(r, f"se esperaba un entero (como string) y llegó {v!r}")
            return {variante: v}
        if variante == "bytesValue":
            try:
                base64.b64decode(v, validate=True)
            except (binascii.Error, TypeError, ValueError) as exc:
                raise _error(r, "se esperaba un string en base64") from exc
            return {variante: v}
        if variante == "arrayValue":
            v = _solo_campos(v, {"values"}, r)
            items = v.get("values", [])
            if not isinstance(items, (list, tuple)):
                raise _error(f"{r}.values", "se esperaba una lista de ValueNode")
            return {variante: {"values": [self.nodo(x, f"{r}.values[{i}]", origen) for i, x in enumerate(items)]}}
        if variante == "dictionaryValue":
            v = _solo_campos(v, {"values"}, r)
            return {variante: {"values": self.mapa_nodos(v.get("values", {}), f"{r}.values", origen)}}
        if variante == "functionInvocationValue":
            v = _solo_campos(v, {"functionName", "functionReference", "arguments"}, r)
            nombres = [c for c in ("functionName", "functionReference") if c in v]
            if len(nombres) != 1:
                raise _error(r, "lleva 'functionName' o 'functionReference' (uno y solo uno)")
            clave = nombres[0]
            if not isinstance(v[clave], str) or not v[clave].strip():
                raise _error(f"{r}.{clave}", "debe ser un string no vacío")
            if clave == "functionReference":
                self.referencia(v[clave], f"{r}.{clave}", origen)
            salida = {clave: v[clave]}
            if "arguments" in v:
                salida["arguments"] = self.mapa_nodos(v["arguments"], f"{r}.arguments", origen)
            return {variante: salida}
        if variante == "functionDefinitionValue":
            v = _solo_campos(v, {"argumentNames", "body"}, r)
            nombres = v.get("argumentNames", [])
            if not isinstance(nombres, (list, tuple)) or not all(isinstance(n, str) and n for n in nombres):
                raise _error(f"{r}.argumentNames", "se esperaba una lista de strings")
            self.referencia(v.get("body"), f"{r}.body", origen)
            return {variante: {"argumentNames": list(nombres), "body": v["body"]}}
        if variante == "argumentReference":
            if not isinstance(v, str) or not v:
                raise _error(r, "debe ser el nombre (string) de un argumento de la función")
            return {variante: v}
        # valueReference
        self.referencia(v, r, origen)
        return {variante: v}

    def mapa_nodos(self, valores: Any, ruta: str, origen: str) -> dict:
        if not isinstance(valores, Mapping) or not all(isinstance(k, str) for k in valores):
            raise _error(ruta, "se esperaba un objeto {nombre: ValueNode}")
        return {k: self.nodo(x, f"{ruta}.{k}", origen) for k, x in valores.items()}

    def referencia(self, clave: Any, ruta: str, origen: str) -> None:
        if not isinstance(clave, str) or clave not in self.claves:
            raise _error(ruta, f"referencia a {clave!r}, que no existe en expression.values")
        self.referencias[origen].add(clave)

    def sin_ciclos(self) -> None:
        estado: dict = {}

        def visitar(k: str, camino: list) -> None:
            if estado.get(k) == "listo":
                return
            if estado.get(k) == "visitando":
                raise _error("expression.values", f"referencias cíclicas: {' -> '.join(camino + [k])}")
            estado[k] = "visitando"
            for dest in self.referencias[k]:
                visitar(dest, camino + [k])
            estado[k] = "listo"

        for k in self.referencias:
            visitar(k, [])


def validar_expresion(expresion: Any, ruta: str = "expression") -> dict:
    """Valida un Expression ({values, result}) y devuelve una copia JSON pura."""
    expresion = _solo_campos(expresion, CAMPOS_EXPRESION, ruta)
    valores = expresion.get("values")
    if not isinstance(valores, Mapping) or not valores:
        raise _error(f"{ruta}.values", "debe ser un objeto no vacío {clave: ValueNode}")
    if not all(isinstance(k, str) and k for k in valores):
        raise _error(f"{ruta}.values", "las claves deben ser strings no vacíos")
    resultado = expresion.get("result")
    if not isinstance(resultado, str) or resultado not in valores:
        raise _error(f"{ruta}.result", f"{resultado!r} no es una clave de {ruta}.values")
    validador = _ValidadorExpresion(valores)
    salida = {k: validador.nodo(n, f"{ruta}.values.{k}", origen=k) for k, n in valores.items()}
    validador.sin_ciclos()
    return {"values": salida, "result": resultado}


def validar_visualizacion(vis: Any, bandas: Optional[int] = None,
                          ruta: str = "visualizationOptions") -> dict:
    vis = _solo_campos(vis, CAMPOS_VISUALIZACION, ruta)
    salida: dict = {}
    if "ranges" in vis:
        rangos = vis["ranges"]
        if not isinstance(rangos, (list, tuple)) or not rangos:
            raise _error(f"{ruta}.ranges", "se esperaba una lista no vacía de {min, max}")
        if bandas is not None and len(rangos) not in (1, bandas):
            raise _error(f"{ruta}.ranges", f"hay {len(rangos)} rangos para {bandas} bandas (va 1 o uno por banda)")
        salida["ranges"] = []
        for i, rango in enumerate(rangos):
            r = f"{ruta}.ranges[{i}]"
            rango = _solo_campos(rango, {"min", "max"}, r)
            if set(rango) != {"min", "max"}:
                raise _error(r, "cada rango lleva 'min' y 'max'")
            salida["ranges"].append({"min": _numero_finito(rango["min"], f"{r}.min"),
                                     "max": _numero_finito(rango["max"], f"{r}.max")})
    if "paletteColors" in vis:
        paleta = vis["paletteColors"]
        if not isinstance(paleta, (list, tuple)) or not paleta:
            raise _error(f"{ruta}.paletteColors", "se esperaba una lista no vacía de colores")
        for i, color in enumerate(paleta):
            if not isinstance(color, str) or not _RE_COLOR.match(color):
                raise _error(f"{ruta}.paletteColors[{i}]", f"color CSS inválido: {color!r}")
        if bandas is not None and bandas != 1:
            raise _error(f"{ruta}.paletteColors", f"una paleta solo se aplica a 1 banda (bandIds tiene {bandas})")
        salida["paletteColors"] = list(paleta)
    if "gamma" in vis:  # número, no un objeto {"value": ...}
        gamma = _numero_finito(vis["gamma"], f"{ruta}.gamma")
        if gamma <= 0:
            raise _error(f"{ruta}.gamma", "debe ser mayor que 0")
        salida["gamma"] = gamma
    if "opacity" in vis:
        opacidad = _numero_finito(vis["opacity"], f"{ruta}.opacity")
        if not 0 <= opacidad <= 1:
            raise _error(f"{ruta}.opacity", "debe estar entre 0 y 1")
        salida["opacity"] = opacidad
    return salida


def validar_payload_mapa(payload: Any) -> dict:
    """Valida un EarthEngineMap para `projects.maps.create` contra el esquema
    v1 y devuelve una copia JSON pura y normalizada. Campos desconocidos,
    variantes mezcladas, referencias rotas o números no finitos se rechazan
    con `GeePayloadError` (lo que la API respondería con 400)."""
    if isinstance(payload, Mapping) and "name" in payload:
        raise _error("name", "es un campo de salida (lo asigna Google); no va en el pedido")
    payload = _solo_campos(payload, CAMPOS_MAPA, "payload")
    return _validar_imagen_comun(payload, FORMATOS_MAPA)


def _validar_grilla(grid: Any, ruta: str = "grid") -> dict:
    """PixelGrid {dimensions {width, height}, affineTransform {...}, crsCode | crsWkt}."""
    grid = _solo_campos(grid, CAMPOS_GRILLA, ruta)
    salida: dict = {}
    if "dimensions" in grid:
        dim = _solo_campos(grid["dimensions"], {"width", "height"}, f"{ruta}.dimensions")
        for lado in ("width", "height"):
            v = dim.get(lado)
            if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_LADO_PIXELES:
                raise _error(f"{ruta}.dimensions.{lado}", f"entero entre 1 y {MAX_LADO_PIXELES} (llegó {v!r})")
        salida["dimensions"] = {"width": dim["width"], "height": dim["height"]}
    if "affineTransform" in grid:
        at = _solo_campos(grid["affineTransform"], CAMPOS_AFIN, f"{ruta}.affineTransform")
        salida["affineTransform"] = {k: _numero_finito(at.get(k, 0), f"{ruta}.affineTransform.{k}")
                                     for k in ("scaleX", "shearX", "translateX", "shearY", "scaleY", "translateY")}
        if salida["affineTransform"]["scaleX"] == 0 or salida["affineTransform"]["scaleY"] == 0:
            raise _error(f"{ruta}.affineTransform", "scaleX / scaleY no pueden ser 0")
    crs = [c for c in ("crsCode", "crsWkt") if c in grid]
    if len(crs) > 1:
        raise _error(ruta, "lleva 'crsCode' o 'crsWkt', no ambos")
    for c in crs:
        if not isinstance(grid[c], str) or not grid[c].strip():
            raise _error(f"{ruta}.{c}", "debe ser un string no vacío (ej. 'EPSG:4326')")
        salida[c] = grid[c]
    return salida


def validar_pedido_pixeles(pedido: Any) -> dict:
    """Valida el cuerpo de `projects.image.computePixels` (esquema v1):
    {expression, fileFormat, grid, bandIds, visualizationOptions, workloadTag}.
    Controla además el límite documentado: 48 MB sin comprimir (ancho x alto x
    bandas x bytes por banda; 1 byte en PNG / JPEG visualizados) y 32K píxeles
    por lado."""
    pedido = _solo_campos(pedido, CAMPOS_PIXELES, "pedido")
    cuerpo = _validar_imagen_comun(pedido, FORMATOS_PIXELES)
    if pedido.get("grid") is not None:
        cuerpo["grid"] = _validar_grilla(pedido["grid"])
    if pedido.get("workloadTag") is not None:
        if not isinstance(pedido["workloadTag"], str):
            raise _error("workloadTag", "debe ser un string")
        cuerpo["workloadTag"] = pedido["workloadTag"]
    dim = cuerpo.get("grid", {}).get("dimensions")
    if dim and cuerpo.get("fileFormat") in FORMATOS_8_BITS:
        bandas = len(cuerpo.get("bandIds") or [None, None, None])
        if dim["width"] * dim["height"] * bandas > MAX_BYTES_PIXELES:
            raise _error("grid.dimensions",
                         f"{dim['width']}x{dim['height']}x{bandas} bandas supera el límite de 48 MB por pedido")
    return cuerpo


def _validar_imagen_comun(payload: Mapping, formatos: set) -> dict:
    """Campos compartidos por maps.create y computePixels."""
    if "expression" not in payload:
        raise _error("expression", "falta la expresión de Earth Engine")
    cuerpo: dict = {"expression": validar_expresion(payload["expression"])}

    bandas: Optional[int] = None
    if payload.get("bandIds") is not None:
        ids = payload["bandIds"]
        if (not isinstance(ids, (list, tuple)) or not ids
                or not all(isinstance(b, str) and b.strip() for b in ids)):
            raise _error("bandIds", "se esperaba una lista no vacía de nombres de banda")
        cuerpo["bandIds"] = list(ids)
        bandas = len(ids)

    formato = payload.get("fileFormat")
    if formato is not None:
        if not isinstance(formato, str) or formato not in formatos:
            raise _error("fileFormat", f"{formato!r} no sirve para teselas; usar uno de {sorted(formatos)}")
        if formato in FORMATOS_8_BITS and bandas is not None and bandas not in (1, 3):
            raise _error("fileFormat", f"{formato} necesita 1 o 3 bandas y bandIds tiene {bandas}")
        cuerpo["fileFormat"] = formato

    if payload.get("visualizationOptions") is not None:
        vis = validar_visualizacion(payload["visualizationOptions"], bandas)
        if vis:  # como el cliente oficial: vacío no se envía (cambia el comportamiento del servidor)
            cuerpo["visualizationOptions"] = vis
    return cuerpo


# -- Constructores --------------------------------------------------------------------

def expresion_asset(asset_id: str, version: Optional[int] = None) -> dict:
    """Expression que carga un asset de imagen, igual que ee.Image('ID')."""
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise _error("expression", "el ID del asset no puede estar vacío")
    argumentos: dict = {"id": {"constantValue": asset_id.strip()}}
    if version is not None:
        argumentos["version"] = {"integerValue": str(int(version))}
    return {"values": {"0": {"functionInvocationValue": {"functionName": "Image.load", "arguments": argumentos}}},
            "result": "0"}


def payload_mapa(expression: Mapping[str, Any], *, file_format: str = "AUTO_JPEG_PNG",
                 band_ids: Optional[Sequence[str]] = None,
                 visualizacion: Optional[Mapping[str, Any]] = None) -> dict:
    """Arma (y valida) el cuerpo de `projects.maps.create` a partir de una
    expresión serializada de Earth Engine."""
    cuerpo: dict = {"expression": expression, "fileFormat": file_format}
    if band_ids:
        cuerpo["bandIds"] = list(band_ids)
    if visualizacion:
        cuerpo["visualizationOptions"] = visualizacion
    return validar_payload_mapa(cuerpo)


def payload_asset(asset_id: str, *, minimo: float, maximo: float, banda: Optional[str] = None,
                  paleta: Optional[Sequence[str]] = None, gamma: Optional[float] = None,
                  opacidad: Optional[float] = None, file_format: str = "PNG") -> dict:
    """Mapa de una banda de un asset con estirado min/max y paleta opcional.
    Ej. relieve SRTM: payload_asset("USGS/SRTMGL1_003", minimo=0, maximo=3000,
    banda="elevation", paleta=["006633", "E5FFCC", "662A00", "D8D8D8", "F5F5F5"])."""
    vis: dict = {"ranges": [{"min": minimo, "max": maximo}]}
    if paleta:
        vis["paletteColors"] = list(paleta)
    if gamma is not None:
        vis["gamma"] = gamma
    if opacidad is not None:
        vis["opacity"] = opacidad
    return payload_mapa(expresion_asset(asset_id), file_format=file_format,
                        band_ids=[banda] if banda else None, visualizacion=vis)

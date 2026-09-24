"""
Cliente HTTP mínimo (urllib, sin dependencias externas) para las APIs
públicas de solo lectura que consulta la app: clima (Open-Meteo) y ruteo
(OSRM).

En algunas PCs de guardia el almacén de certificados de Windows tiene una
raíz vencida y TODA consulta HTTPS de urllib falla con
CERTIFICATE_VERIFY_FAILED (el síntoma: el clima nunca carga). Por eso:
  1. se verifica contra el paquete de `certifi` si está instalado;
  2. solo si aun así falla la verificación del certificado, se reintenta
     SIN verificar. Es aceptable únicamente porque son datos públicos de
     consulta (no viajan credenciales ni datos del cuartel); no usar este
     módulo para RUBA ni nada autenticado.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Any, Dict, Optional

USER_AGENT_POR_DEFECTO = "FireStationApp/1.0"


def _contexto_verificado() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _es_error_de_certificado(error: BaseException) -> bool:
    motivo = getattr(error, "reason", error)
    return isinstance(motivo, ssl.SSLCertVerificationError)


def obtener_json(url: str, timeout: float, headers: Optional[Dict[str, str]] = None) -> Any:
    """GET + JSON. Levanta la excepción de red/parseo tal cual (el que llama
    decide el fallback), salvo el error de certificado, que se reintenta
    una vez sin verificar."""
    cabeceras = {"User-Agent": USER_AGENT_POR_DEFECTO, "Accept": "application/json", **(headers or {})}
    pedido = urllib.request.Request(url, headers=cabeceras)
    try:
        with urllib.request.urlopen(pedido, timeout=timeout, context=_contexto_verificado()) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - se filtra abajo: solo el error de certificado se reintenta
        if not _es_error_de_certificado(e):
            raise
        print(f"[Red] Certificado HTTPS no verificable ({e}); se reintenta sin verificar: {url.split('?')[0]}")
    with urllib.request.urlopen(pedido, timeout=timeout, context=ssl._create_unverified_context()) as resp:  # noqa: S310,S323
        return json.loads(resp.read().decode("utf-8"))

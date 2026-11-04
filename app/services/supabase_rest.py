"""
Cliente REST mínimo de Supabase (PostgREST) compartido por los servicios de
la app de escritorio: Libro de Guardia (libro_guardia_service) e Inventario
(inventario_service). Todas las llamadas son BLOQUEANTES: la UI las corre en
un QThread (app/workers/supabase_tarea.py).

Credenciales: app/services/supabase_client.cargar_credenciales (variables de
entorno o data/config.json).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import requests

from app.services.supabase_client import (
    TIMEOUT_SEGUNDOS,
    SupabaseCredenciales,
    SupabaseSyncError,
    cargar_credenciales,
)


class SupabaseRechazo(RuntimeError):
    """La base rechazó la operación por una regla de negocio (motivo de una
    RPC). El texto ya está listo para mostrarle al operador."""


class ClienteSupabaseRest:
    def __init__(self, credenciales: Optional[SupabaseCredenciales] = None,
                 session: Optional[requests.Session] = None, timeout=TIMEOUT_SEGUNDOS) -> None:
        self._cred = credenciales or cargar_credenciales()
        self._base = self._cred.url.rstrip("/") + "/rest/v1"
        self._http = session or requests.Session()
        self._timeout = timeout
        self._http.headers.update({
            "apikey": self._cred.service_key,
            "Authorization": f"Bearer {self._cred.service_key}",
            "Content-Type": "application/json",
        })

    def _pedir(self, metodo: str, ruta: str, **kwargs) -> Any:
        try:
            resp = self._http.request(metodo, f"{self._base}/{ruta}", timeout=self._timeout, **kwargs)
            resp.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise SupabaseSyncError("Supabase no respondió a tiempo (timeout).") from exc
        except requests.exceptions.HTTPError as exc:
            r = exc.response
            detalle = ""
            if r is not None:
                try:
                    cuerpo = r.json()
                    detalle = cuerpo.get("message") or cuerpo.get("details") or r.text
                except ValueError:
                    detalle = r.text
            raise SupabaseSyncError(
                f"Supabase respondió {r.status_code if r is not None else '?'}: {str(detalle)[:400]}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise SupabaseSyncError(f"Sin conexión con Supabase: {exc}") from exc
        return resp.json() if resp.content else None

    def _rpc(self, nombre: str, args: Optional[Dict[str, Any]] = None) -> Any:
        return self._pedir("POST", f"rpc/{nombre}", json=args or {})

    @staticmethod
    def _exigir_ok(resultado: Any, motivos: Mapping[str, str],
                   error: type = SupabaseRechazo) -> Dict[str, Any]:
        """Resultado {"ok": true, ...} de una RPC; si no, `error` con el texto
        del motivo (o uno genérico si el motivo no está en `motivos`)."""
        if not isinstance(resultado, dict):
            raise SupabaseSyncError(f"Respuesta inesperada de Supabase: {resultado!r}")
        if not resultado.get("ok"):
            motivo = resultado.get("motivo") or "DESCONOCIDO"
            raise error(motivos.get(motivo, f"Operación rechazada ({motivo})."))
        return resultado

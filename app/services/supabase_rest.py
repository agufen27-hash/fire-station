"""
Cliente REST mínimo de Supabase (PostgREST) compartido por los servicios de
la app de escritorio: Libro de Guardia (libro_guardia_service) e Inventario
(inventario_service). Todas las llamadas son BLOQUEANTES: la UI las corre en
un QThread (app/workers/supabase_tarea.py).

Credenciales: app/services/supabase_client.cargar_credenciales (variables de
entorno o data/config.json).

Modo offline (app/services/cache_offline.py):
  * Cada GET exitoso queda en la caché local; sin red se devuelve lo último
    guardado (y `cache_offline.estado().en_linea` pasa a False).
  * `_rpc_escritura` encola la operación si no hay red y devuelve None.
  * "Sin red" = timeout, error de conexión o 502/503/504 (SupabaseSinConexion).
    Un 4xx (clave inválida, permisos) NO es falta de red: se informa tal cual.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import requests

from app.services import cache_offline
from app.services.supabase_client import (
    TIMEOUT_SEGUNDOS,
    SupabaseCredenciales,
    SupabaseSyncError,
    cargar_credenciales,
)


class SupabaseSinConexion(SupabaseSyncError):
    """No se pudo hablar con Supabase (red caída, timeout, servicio no disponible)."""


HTTP_SIN_SERVICIO = {502, 503, 504}


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

    def _pedir(self, metodo: str, ruta: str, clave_cache: Optional[str] = None, **kwargs) -> Any:
        """GET: con caché local (clave = ruta + params, o `clave_cache`)."""
        if metodo != "GET":
            return self._pedir_red(metodo, ruta, **kwargs)
        clave = clave_cache or cache_offline.clave_lectura(ruta, kwargs.get("params"))
        try:
            datos = self._pedir_red(metodo, ruta, **kwargs)
        except SupabaseSinConexion:
            guardado = cache_offline.leer_lectura(clave)
            if guardado is None:
                raise
            cache_offline.estado().dato_cacheado_at = guardado[1]
            return guardado[0]
        cache_offline.guardar_lectura(clave, datos)
        return datos

    def _pedir_red(self, metodo: str, ruta: str, **kwargs) -> Any:
        try:
            resp = self._http.request(metodo, f"{self._base}/{ruta}", timeout=self._timeout, **kwargs)
            resp.raise_for_status()
        except requests.exceptions.Timeout as exc:
            cache_offline.marcar_sin_conexion("timeout")
            raise SupabaseSinConexion("Supabase no respondió a tiempo (timeout).") from exc
        except requests.exceptions.HTTPError as exc:
            r = exc.response
            detalle = ""
            if r is not None:
                try:
                    cuerpo = r.json()
                    detalle = cuerpo.get("message") or cuerpo.get("details") or r.text
                except ValueError:
                    detalle = r.text
            codigo = r.status_code if r is not None else None
            mensaje = f"Supabase respondió {codigo if codigo is not None else '?'}: {str(detalle)[:400]}"
            if codigo in HTTP_SIN_SERVICIO:
                cache_offline.marcar_sin_conexion(mensaje)
                raise SupabaseSinConexion(mensaje) from exc
            raise SupabaseSyncError(mensaje) from exc
        except requests.exceptions.RequestException as exc:
            cache_offline.marcar_sin_conexion(str(exc))
            raise SupabaseSinConexion(f"Sin conexión con Supabase: {exc}") from exc
        cache_offline.marcar_en_linea()
        return resp.json() if resp.content else None

    def _rpc(self, nombre: str, args: Optional[Dict[str, Any]] = None) -> Any:
        return self._pedir_red("POST", f"rpc/{nombre}", json=args or {})

    def _rpc_lectura(self, nombre: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """RPC de solo lectura: con caché local como los GET."""
        clave = cache_offline.clave_lectura(f"rpc/{nombre}", args)
        try:
            datos = self._rpc(nombre, args)
        except SupabaseSinConexion:
            guardado = cache_offline.leer_lectura(clave)
            if guardado is None:
                raise
            cache_offline.estado().dato_cacheado_at = guardado[1]
            return guardado[0]
        cache_offline.guardar_lectura(clave, datos)
        return datos

    def _rpc_escritura(self, nombre: str, args: Dict[str, Any], descripcion: str = "") -> Any:
        """Escritura de la guardia: sin red se guarda en la cola local (se envía
        sola al volver la conexión) y devuelve None."""
        try:
            return self._rpc(nombre, args)
        except SupabaseSinConexion:
            cache_offline.encolar(nombre, args, descripcion)
            return None

    def enviar_operacion(self, operacion: str, argumentos: Dict[str, Any]) -> Any:
        """Para el sincronizador de la cola offline."""
        return self._rpc(operacion, argumentos)

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

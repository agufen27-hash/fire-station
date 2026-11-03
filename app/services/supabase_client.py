"""
Puente local -> cloud: sincroniza el padrón de SQLite (`personal`) con la
tabla `public.personal_cloud` de Supabase vía la API REST (PostgREST).

Credenciales (NUNCA en el código fuente), en este orden de prioridad:
  1. Variables de entorno  SUPABASE_URL  y  SUPABASE_SERVICE_KEY
  2. data/config.json  ->  {"supabase": {"url": "...", "service_key": "..."}}
     (data/config.json está en .gitignore y no se embebe en el .exe).

La service key saltea RLS: este módulo corre solo en la PC del cuartel.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

from app.paths import get_writable_dir

log = logging.getLogger(__name__)

TABLA_PERSONAL = "personal_cloud"
TIMEOUT_SEGUNDOS = (5, 20)          # (conexión, lectura)
TAMANIO_LOTE = 500


class SupabaseConfigError(RuntimeError):
    """Faltan SUPABASE_URL / SUPABASE_SERVICE_KEY."""


class SupabaseSyncError(RuntimeError):
    """Falló la comunicación con Supabase (red, timeout o HTTP != 2xx)."""


@dataclass(frozen=True)
class SupabaseCredenciales:
    url: str
    service_key: str


def cargar_credenciales() -> SupabaseCredenciales:
    """Env vars primero; si faltan, la sección "supabase" de data/config.json."""
    seccion: dict = {}
    try:
        datos = json.loads((get_writable_dir("data") / "config.json").read_text(encoding="utf-8"))
        if isinstance(datos, dict) and isinstance(datos.get("supabase"), dict):
            seccion = datos["supabase"]
    except (OSError, ValueError):
        pass

    url = (os.environ.get("SUPABASE_URL") or seccion.get("url") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or seccion.get("service_key") or "").strip()
    if not url or not key:
        raise SupabaseConfigError(
            "Faltan credenciales de Supabase: definí SUPABASE_URL y SUPABASE_SERVICE_KEY "
            "(variables de entorno) o la sección \"supabase\" de data/config.json."
        )
    return SupabaseCredenciales(url=url, service_key=key)


# -- Teléfonos argentinos -> formato WhatsApp Cloud (549 + área + número) -------

_NO_DIGITOS = re.compile(r"\D")


def normalizar_telefono_ar(crudo: Optional[str]) -> Optional[str]:
    """'0358 15-412-3456' -> '5493584123456'. None si vacío o no se puede
    llevar a 10 dígitos nacionales (área + abonado)."""
    if crudo is None:
        return None
    digitos = _NO_DIGITOS.sub("", str(crudo))          # a) espacios, guiones, (), +
    if not digitos:
        return None

    # Ya viene con código de país (+54 / +54 9): se quita y se vuelve a armar.
    if digitos.startswith("549") and len(digitos) >= 13:
        digitos = digitos[3:]
    elif digitos.startswith("54") and len(digitos) >= 12:
        digitos = digitos[2:]

    digitos = digitos.lstrip("0")                        # b) prefijo troncal 0

    # c) '15' de celular después del código de área (2, 3 o 4 dígitos).
    #    Solo cuando sobran exactamente 2 dígitos (10 + '15' = 12).
    if len(digitos) == 12:
        for largo_area in (2, 3, 4):
            if largo_area == 2 and not digitos.startswith("11"):
                continue                                  # único código de 2 dígitos: 11 (AMBA)
            if digitos[largo_area:largo_area + 2] == "15":
                digitos = digitos[:largo_area] + digitos[largo_area + 2:]
                break

    if len(digitos) == 10:                               # d)
        return "549" + digitos
    log.warning("Teléfono no normalizable (quedan %d dígitos): se sube sin teléfono.", len(digitos))
    return None


# -- Bridge ------------------------------------------------------------------------

class SupabaseBridge:
    def __init__(self, credenciales: Optional[SupabaseCredenciales] = None,
                 session: Optional[requests.Session] = None,
                 timeout=TIMEOUT_SEGUNDOS) -> None:
        self._cred = credenciales or cargar_credenciales()
        self._http = session or requests.Session()
        self._timeout = timeout
        self._http.headers.update({
            "apikey": self._cred.service_key,
            "Authorization": f"Bearer {self._cred.service_key}",
            "Content-Type": "application/json",
        })

    normalizar_telefono = staticmethod(normalizar_telefono_ar)

    @staticmethod
    def mapear_personal(personas: Iterable) -> list[dict]:
        """Personal (ORM) -> filas de personal_cloud. Si dos personas quedan
        con el mismo teléfono normalizado, la segunda se sube sin teléfono
        (personal_cloud.telefono_whatsapp es UNIQUE y rompería el lote)."""
        filas: list[dict] = []
        vistos: set[str] = set()
        for p in personas:
            tel = normalizar_telefono_ar(p.telefono)
            if tel and tel in vistos:
                log.warning("Teléfono repetido en personal id=%s: se sube sin teléfono.", p.id)
                tel = None
            if tel:
                vistos.add(tel)
            filas.append({
                "id": p.id,
                "legajo": (p.legajo or None),
                "id_ruba": p.id_ruba,
                "nombre_completo": p.nombre_completo(),
                "jerarquia": p.jerarquia,
                "telefono_whatsapp": tel,
                "rol": (p.rol or "BOMBERO").upper(),
                "activo": bool(p.activo),
            })
        return filas

    def upsert_personal(self, filas: list[dict]) -> int:
        """UPSERT masivo por PK `id` (Prefer: resolution=merge-duplicates)."""
        url = f"{self._cred.url.rstrip('/')}/rest/v1/{TABLA_PERSONAL}"
        headers = {"Prefer": "resolution=merge-duplicates,return=minimal"}
        for i in range(0, len(filas), TAMANIO_LOTE):
            lote = filas[i:i + TAMANIO_LOTE]
            try:
                resp = self._http.post(url, params={"on_conflict": "id"}, json=lote,
                                       headers=headers, timeout=self._timeout)
                resp.raise_for_status()
            except requests.exceptions.Timeout as exc:
                raise SupabaseSyncError(f"Timeout sincronizando {TABLA_PERSONAL}.") from exc
            except requests.exceptions.HTTPError as exc:
                detalle = exc.response.text[:500] if exc.response is not None else ""
                raise SupabaseSyncError(
                    f"Supabase respondió {exc.response.status_code if exc.response is not None else '?'}: {detalle}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise SupabaseSyncError(f"Error de red con Supabase: {exc}") from exc
        return len(filas)

    def sincronizar_padron_completo(self) -> int:
        """Sube TODO el personal local (activo e inactivo). Devuelve filas enviadas."""
        from app.db import get_session
        from app.models import Personal

        with get_session() as session:
            filas = self.mapear_personal(session.query(Personal).order_by(Personal.id).all())
        if not filas:
            return 0
        enviadas = self.upsert_personal(filas)
        log.info("Padrón sincronizado con Supabase: %d personas.", enviadas)
        return enviadas

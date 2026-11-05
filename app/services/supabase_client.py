"""
Puente local -> cloud: sincroniza el padrón de SQLite (`personal`) con la
tabla `public.personal_cloud` de Supabase vía la API REST (PostgREST), y los
documentos del legajo digital (`documentos_personal` + archivos en
data/legajos/) con el bucket privado `legajos` de Storage y la tabla
`public.legajo_documentos` (los consulta el bot de WhatsApp).

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
import mimetypes
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import requests

from app.paths import get_writable_dir

log = logging.getLogger(__name__)

TABLA_PERSONAL = "personal_cloud"
TABLA_DOCUMENTOS = "legajo_documentos"
BUCKET_LEGAJOS = "legajos"
TAMANIO_MAX_DOCUMENTO = 25 * 1024 * 1024   # = file_size_limit del bucket
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


# Nombres aceptados (env var y clave dentro de la sección "supabase").
_ENV_URL = ("SUPABASE_URL",)
_ENV_KEY = ("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY")
_CLAVES_URL = ("url", "supabase_url")
_CLAVES_KEY = ("service_key", "service_role_key", "supabase_service_key")


def _ruta_config() -> "os.PathLike":
    return get_writable_dir("data") / "config.json"


def _primero(fuente, claves) -> str:
    for clave in claves:
        valor = fuente.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return ""


def _seccion_supabase(datos: dict) -> dict:
    """La sección "supabase" (sin distinguir mayúsculas); si no hay, claves
    sueltas en la raíz (SUPABASE_URL / SUPABASE_SERVICE_KEY copiadas tal cual)."""
    for nombre, valor in datos.items():
        if str(nombre).lower() == "supabase" and isinstance(valor, dict):
            return valor
    return {k.lower(): v for k, v in datos.items() if isinstance(k, str) and k.lower().startswith("supabase_")}


def cargar_credenciales() -> SupabaseCredenciales:
    """Variables de entorno primero; si faltan, data/config.json (sección
    "supabase"). La URL tiene valor por defecto (la del proyecto, no es
    secreta); la service key no: se carga una vez por PC."""
    from app.paths import URL_SUPABASE_PREDETERMINADA, leer_config_json

    seccion = _seccion_supabase(leer_config_json(_ruta_config()))
    url = (_primero(os.environ, _ENV_URL) or _primero(seccion, _CLAVES_URL)
           or URL_SUPABASE_PREDETERMINADA).rstrip("/")
    key = _primero(os.environ, _ENV_KEY) or _primero(seccion, _CLAVES_KEY)
    if not key:
        raise SupabaseConfigError(
            "Falta la service key de Supabase en esta PC. Cargala desde Libro de Guardia → "
            "🔑 Configurar conexión, o en data/config.json → \"supabase\": {\"service_key\": \"...\"} "
            "(también sirve la variable de entorno SUPABASE_SERVICE_KEY)."
        )
    return SupabaseCredenciales(url=url, service_key=key)


def guardar_credenciales(url: str, service_key: str) -> None:
    """Escribe la sección "supabase" de data/config.json conservando todo lo
    demás (RUBA, cuartel, mapa, actualizaciones...)."""
    from app.paths import leer_config_json

    ruta = _ruta_config()
    datos = leer_config_json(ruta)
    seccion = dict(_seccion_supabase(datos)) if isinstance(datos.get("supabase"), dict) else {}
    seccion.update({"url": url.strip().rstrip("/"), "service_key": service_key.strip()})
    datos["supabase"] = seccion
    ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")


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


def _nombre_storage(nombre: str) -> str:
    """Nombre de archivo apto para una clave de Storage (sin tildes ni símbolos raros)."""
    base = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "documento"
    return base[-120:]


def ruta_storage_documento(personal_id: int, documento_id: int, nombre_archivo: str) -> str:
    """Clave del archivo en el bucket `legajos`: {personal_id}/{documento_id}_{archivo}."""
    return f"{personal_id}/{documento_id}_{_nombre_storage(nombre_archivo)}"


@dataclass
class ResultadoDocumentos:
    subidos: int = 0
    sin_cambios: int = 0
    eliminados: int = 0
    omitidos: list[str] = field(default_factory=list)   # faltantes / muy grandes / con error


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
                "dni": (str(getattr(p, "dni", "") or "").strip() or None),
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
        # Los documentos van después (legajo_documentos.personal_id -> personal_cloud).
        # Un fallo acá no invalida el padrón ya subido: se registra y sigue.
        try:
            res = self.sincronizar_documentos_legajo()
            log.info("Legajos digitales: %d subidos, %d sin cambios, %d eliminados, %d omitidos.",
                     res.subidos, res.sin_cambios, res.eliminados, len(res.omitidos))
        except SupabaseSyncError as exc:
            log.warning("No se pudieron sincronizar los documentos de legajo: %s", exc)
        return enviadas

    # -- Documentos del legajo digital -------------------------------------------

    def _llamar(self, metodo: str, ruta: str, **kwargs) -> requests.Response:
        """`ruta` relativa a la URL del proyecto (rest/v1/..., storage/v1/...)."""
        url = f"{self._cred.url.rstrip('/')}/{ruta}"
        kwargs.setdefault("timeout", self._timeout)
        try:
            resp = self._http.request(metodo, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout as exc:
            raise SupabaseSyncError(f"Timeout con Supabase ({ruta}).") from exc
        except requests.exceptions.HTTPError as exc:
            r = exc.response
            raise SupabaseSyncError(
                f"Supabase respondió {r.status_code if r is not None else '?'}: "
                f"{(r.text if r is not None else '')[:500]}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise SupabaseSyncError(f"Error de red con Supabase: {exc}") from exc

    @staticmethod
    def mapear_documentos(documentos: Iterable) -> list[dict]:
        """DocumentoPersonal (ORM) -> filas de legajo_documentos. Incluye
        `_ruta_local` (no es columna) para que el llamador lea el archivo."""
        filas: list[dict] = []
        for d in documentos:
            ruta = Path(d.ruta_archivo)
            filas.append({
                "id": d.id,
                "personal_id": d.personal_id,
                "tipo_documento": d.tipo_documento,
                "nombre_archivo": d.nombre_archivo,
                "storage_path": ruta_storage_documento(d.personal_id, d.id, d.nombre_archivo),
                "tamanio_bytes": ruta.stat().st_size if ruta.is_file() else None,
                "mime_type": mimetypes.guess_type(d.nombre_archivo)[0] or "application/octet-stream",
                "fecha_subida": d.fecha_subida.isoformat() if d.fecha_subida else None,
                "_ruta_local": ruta,
            })
        return filas

    def sincronizar_documentos_legajo(self) -> ResultadoDocumentos:
        """Sube a Storage los documentos nuevos o modificados (cambió el tamaño
        o la ruta), actualiza `legajo_documentos` y borra de la nube los que
        ya no existen en la base local."""
        from app.db import get_session
        from app.models import DocumentoPersonal

        with get_session() as session:
            locales = self.mapear_documentos(
                session.query(DocumentoPersonal).order_by(DocumentoPersonal.id).all())
        return self.subir_documentos(locales)

    def subir_documentos(self, locales: list[dict]) -> ResultadoDocumentos:
        remotos = {
            f["id"]: f for f in self._llamar(
                "GET", f"rest/v1/{TABLA_DOCUMENTOS}",
                params={"select": "id,storage_path,tamanio_bytes"}).json()
        }
        res = ResultadoDocumentos()
        a_registrar: list[dict] = []
        for fila in locales:
            fila = dict(fila)
            ruta: Path = fila.pop("_ruta_local")
            if fila["tamanio_bytes"] is None:
                res.omitidos.append(f"{fila['nombre_archivo']} (no se encuentra el archivo)")
                continue
            if fila["tamanio_bytes"] > TAMANIO_MAX_DOCUMENTO:
                res.omitidos.append(f"{fila['nombre_archivo']} (supera 25 MB)")
                continue
            previo = remotos.get(fila["id"])
            if (previo and previo["storage_path"] == fila["storage_path"]
                    and previo["tamanio_bytes"] == fila["tamanio_bytes"]):
                res.sin_cambios += 1
                continue
            try:
                self._llamar(
                    "POST", f"storage/v1/object/{BUCKET_LEGAJOS}/{quote(fila['storage_path'])}",
                    data=ruta.read_bytes(),
                    headers={"Content-Type": fila["mime_type"], "x-upsert": "true"},
                    timeout=(5, 120),
                )
            except (OSError, SupabaseSyncError) as exc:
                log.warning("No se pudo subir %s: %s", ruta, exc)
                res.omitidos.append(f"{fila['nombre_archivo']} ({exc})")
                continue
            if previo and previo["storage_path"] != fila["storage_path"]:
                self._borrar_objetos([previo["storage_path"]])
            a_registrar.append(fila)
            res.subidos += 1

        for i in range(0, len(a_registrar), TAMANIO_LOTE):
            self._llamar("POST", f"rest/v1/{TABLA_DOCUMENTOS}", params={"on_conflict": "id"},
                         json=a_registrar[i:i + TAMANIO_LOTE],
                         headers={"Prefer": "resolution=merge-duplicates,return=minimal"})

        ids_locales = {f["id"] for f in locales}
        huerfanos = [f for id_, f in remotos.items() if id_ not in ids_locales]
        if huerfanos:
            self._borrar_objetos([f["storage_path"] for f in huerfanos])
            ids = ",".join(str(f["id"]) for f in huerfanos)
            self._llamar("DELETE", f"rest/v1/{TABLA_DOCUMENTOS}", params={"id": f"in.({ids})"})
            res.eliminados = len(huerfanos)
        return res

    def _borrar_objetos(self, rutas: list[str]) -> None:
        if rutas:
            self._llamar("DELETE", f"storage/v1/object/{BUCKET_LEGAJOS}", json={"prefixes": rutas})

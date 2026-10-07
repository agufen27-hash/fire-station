"""
Seguridad de los PIN del personal y de la clave maestra del cuartel.

PIN: se guardan con PBKDF2-HMAC-SHA256 y sal aleatoria por persona, en el
formato "salt$hash" (los dos en hex). Nunca se guarda ni se compara el PIN
en texto plano; las comparaciones son de tiempo constante.

Un PIN de 4 a 10 dígitos es corto: el hash protege contra leer los PIN de
un vistazo en la base (o en un respaldo), no contra un ataque de fuerza
bruta de quien ya tiene la base en la mano. Por eso además de hashear se
obliga a cambiar el PIN de fábrica (PIN_DEFAULT).

Clave maestra: permite resetear el PIN de un bombero sin conocer el
anterior. Se busca su hash (mismo formato "salt$hash") en este orden:
  1. data/config.json            -> {"seguridad": {"clave_maestra_hash": "..."}}
     (escribible, propio de cada cuartel; ver establecer_clave_maestra())
  2. config/app_config.json      -> misma clave (configuración del repo)
  3. HASH_MAESTRO_EMERGENCIA     -> constante de emergencia (solo el hash)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from app.paths import get_resource_path, get_writable_dir

PIN_DEFAULT = "5903"
LARGO_MIN_PIN = 4
LARGO_MAX_PIN = 10

ITERACIONES = 100_000
BYTES_SAL = 16
SEPARADOR = "$"

# Hash de la clave maestra de emergencia (la clave en sí NO está en el
# código). Se usa solo si ningún archivo de configuración define otra.
HASH_MAESTRO_EMERGENCIA = (
    "f459c4026d49c30054d23a2d51c3d096$"
    "17130d5f565a0c8d31ed3d20b15776a4adfe457c6c73e44c4c1c9cfc131dd531"
)

# Freno a la fuerza bruta de la clave maestra (por proceso).
MAX_INTENTOS_MAESTRA = 5
BLOQUEO_MAESTRA_SEG = 60

_RE_HEX = re.compile(r"^[0-9a-f]+$")
_intentos_maestra = {"fallidos": 0, "bloqueado_hasta": 0.0}


# ---------------------------------------------------------------------------
# Hash de PIN
# ---------------------------------------------------------------------------

def _derivar(texto: str, sal: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", texto.encode("utf-8"), sal, ITERACIONES)


def hash_pin(pin: str) -> str:
    """'1234' -> 'salt$hash' (hex), con sal aleatoria nueva en cada llamada."""
    sal = secrets.token_bytes(BYTES_SAL)
    return f"{sal.hex()}{SEPARADOR}{_derivar(pin, sal).hex()}"


def es_hash(valor: Optional[str]) -> bool:
    """True si `valor` tiene el formato 'salt$hash' (y no es un PIN legado
    en texto plano: <= 10 caracteres o sin '$')."""
    if not valor or len(valor) <= LARGO_MAX_PIN or valor.count(SEPARADOR) != 1:
        return False
    sal, digest = valor.split(SEPARADOR)
    return bool(sal and digest and _RE_HEX.match(sal) and _RE_HEX.match(digest))


def verificar_pin_hash(pin: str, hashed: str) -> bool:
    """Compara `pin` contra un 'salt$hash' en tiempo constante."""
    if not es_hash(hashed):
        return False
    sal_hex, digest_hex = hashed.split(SEPARADOR)
    try:
        sal = bytes.fromhex(sal_hex)
    except ValueError:
        return False
    return hmac.compare_digest(_derivar(pin or "", sal).hex(), digest_hex)


def verificar_pin(pin: str, almacenado: Optional[str]) -> bool:
    """Verificación central de PIN contra lo guardado en `Personal.pin`.

    Si el valor guardado todavía es un PIN legado en texto plano (base sin
    migrar, ver app/db.py::_migrar_pines_a_hash) se compara igual en tiempo
    constante, así la app no queda inutilizable en la transición."""
    if not almacenado:
        return False
    if es_hash(almacenado):
        return verificar_pin_hash(pin, almacenado)
    return hmac.compare_digest((pin or "").encode("utf-8"), almacenado.encode("utf-8"))


def es_pin_default(pin: str) -> bool:
    """True si el PIN (en claro, recién validado) es el de fábrica."""
    return hmac.compare_digest((pin or "").encode("utf-8"), PIN_DEFAULT.encode("utf-8"))


def almacenado_es_default(almacenado: Optional[str]) -> bool:
    """True si lo guardado (hash o legado) corresponde al PIN de fábrica."""
    return verificar_pin(PIN_DEFAULT, almacenado)


def problema_pin_nuevo(pin: str, permitir_default: bool = False) -> Optional[str]:
    """Motivo por el que `pin` no sirve como PIN nuevo, o None si es válido."""
    if not pin or not pin.isdigit():
        return "El PIN tiene que ser numérico."
    if not LARGO_MIN_PIN <= len(pin) <= LARGO_MAX_PIN:
        return f"El PIN tiene que tener entre {LARGO_MIN_PIN} y {LARGO_MAX_PIN} dígitos."
    if not permitir_default and es_pin_default(pin):
        return "No se puede usar el PIN de fábrica: elegí uno propio."
    return None


# ---------------------------------------------------------------------------
# Clave maestra
# ---------------------------------------------------------------------------

def _ruta_config_escribible() -> Path:
    return get_writable_dir("data") / "config.json"


def _leer_json(ruta: Path) -> dict:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def _hash_maestro_de(datos: dict) -> Optional[str]:
    seccion = datos.get("seguridad")
    valor = seccion.get("clave_maestra_hash") if isinstance(seccion, dict) else None
    return valor if es_hash(valor) else None


def hash_maestro_configurado() -> str:
    """Hash vigente de la clave maestra (config del cuartel > app_config > emergencia)."""
    for ruta in (_ruta_config_escribible(), get_resource_path("config/app_config.json")):
        valor = _hash_maestro_de(_leer_json(ruta))
        if valor:
            return valor
    return HASH_MAESTRO_EMERGENCIA


def segundos_bloqueo_maestra() -> int:
    """Segundos que faltan para poder reintentar la clave maestra (0 = libre)."""
    return max(0, int(_intentos_maestra["bloqueado_hasta"] - time.monotonic() + 0.999))


def verificar_clave_maestra(clave: str) -> bool:
    """True si `clave` es la clave maestra. Tras MAX_INTENTOS_MAESTRA fallos
    seguidos se bloquea BLOQUEO_MAESTRA_SEG segundos (devuelve False sin
    siquiera comparar)."""
    if segundos_bloqueo_maestra():
        return False
    if clave and verificar_pin_hash(clave, hash_maestro_configurado()):
        _intentos_maestra["fallidos"] = 0
        return True
    _intentos_maestra["fallidos"] += 1
    if _intentos_maestra["fallidos"] >= MAX_INTENTOS_MAESTRA:
        _intentos_maestra["fallidos"] = 0
        _intentos_maestra["bloqueado_hasta"] = time.monotonic() + BLOQUEO_MAESTRA_SEG
    return False


def establecer_clave_maestra(clave: str) -> None:
    """Guarda el hash de una clave maestra propia del cuartel en
    data/config.json (sección "seguridad"), conservando el resto del archivo."""
    if not clave or len(clave) < 8:
        raise ValueError("La clave maestra tiene que tener al menos 8 caracteres.")
    ruta = _ruta_config_escribible()
    datos = _leer_json(ruta)
    seccion = datos.get("seguridad") if isinstance(datos.get("seguridad"), dict) else {}
    seccion["clave_maestra_hash"] = hash_pin(clave)
    datos["seguridad"] = seccion
    ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")

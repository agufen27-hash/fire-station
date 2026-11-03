"""
Autenticación del personal: punto único para validar y cambiar PIN.

Toda la UI (autoría de planilla, firma de dotación, acceso al legajo,
cambio y reseteo de PIN) pasa por acá; ninguna pantalla lee ni compara
`Personal.pin` por su cuenta.
"""

from __future__ import annotations

import hmac
from typing import Dict, List, Optional, Tuple

from app.core.security import (
    PIN_DEFAULT,
    almacenado_es_default,
    hash_pin,
    problema_pin_nuevo,
    verificar_clave_maestra,
    verificar_pin,
)
from app.core.roles import ROL_DEFAULT, ROLES, normalizar_rol
from app.db import get_session
from app.models import Personal


class PinError(ValueError):
    """Motivo legible por el que no se pudo cambiar o resetear un PIN."""


def verificar_pin_personal(personal_id: Optional[int], pin: str) -> bool:
    """True si `pin` es el PIN del bombero `personal_id`."""
    if personal_id is None:
        return False
    with get_session() as session:
        persona = session.get(Personal, personal_id)
        almacenado = persona.pin if persona is not None else None
    return verificar_pin(pin, almacenado)


def tiene_pin_default(personal_id: int) -> bool:
    """True si el bombero todavía tiene el PIN de fábrica."""
    with get_session() as session:
        persona = session.get(Personal, personal_id)
        almacenado = persona.pin if persona is not None else None
    return almacenado_es_default(almacenado)


def _guardar_pin(personal_id: int, nuevo: str) -> None:
    with get_session() as session:
        persona = session.get(Personal, personal_id)
        if persona is None:
            raise PinError("El bombero ya no existe en la base.")
        persona.pin = hash_pin(nuevo)


def cambiar_pin(personal_id: int, pin_actual: str, nuevo: str) -> None:
    """Cambio por el propio bombero: exige el PIN actual."""
    if not verificar_pin_personal(personal_id, pin_actual):
        raise PinError("El PIN actual no es correcto.")
    motivo = problema_pin_nuevo(nuevo)
    if motivo:
        raise PinError(motivo)
    _guardar_pin(personal_id, nuevo)


def resetear_pin_con_clave_maestra(personal_id: int, clave_maestra: str, pin_temporal: str = "") -> str:
    """Fija un PIN temporal sin conocer el anterior. Sin `pin_temporal`
    vuelve al de fábrica (que obliga a cambiarlo en el próximo uso).
    Devuelve el PIN que quedó."""
    if not verificar_clave_maestra(clave_maestra):
        raise PinError("Clave maestra incorrecta.")
    temporal = pin_temporal or PIN_DEFAULT
    motivo = problema_pin_nuevo(temporal, permitir_default=True)
    if motivo:
        raise PinError(motivo)
    _guardar_pin(personal_id, temporal)
    return temporal


# ---------------------------------------------------------------------------
# Roles (RBAC local, app/core/roles.py)
# ---------------------------------------------------------------------------

def rol_de(personal_id: Optional[int]) -> str:
    """Rol del bombero; BOMBERO si no existe o no se indicó."""
    if personal_id is None:
        return ROL_DEFAULT
    with get_session() as session:
        persona = session.get(Personal, personal_id)
        return normalizar_rol(persona.rol if persona is not None else None)


def personal_con_roles() -> List[Tuple[int, str, str, str]]:
    """(id, nombre completo, legajo, rol) del personal activo, por apellido."""
    with get_session() as session:
        personas = (session.query(Personal).filter(Personal.activo.is_(True))
                    .order_by(Personal.apellido, Personal.nombre).all())
        return [(p.id, p.nombre_completo(), p.legajo_display(), normalizar_rol(p.rol)) for p in personas]


def asignar_roles(cambios: Dict[int, str]) -> int:
    """Guarda {personal_id: rol} en una sola transacción (todo o nada).
    Valida cada rol antes de tocar la base. Devuelve cuántos cambiaron."""
    invalidos = [r for r in cambios.values() if (r or "").strip().upper() not in ROLES]
    if invalidos:
        raise ValueError(f"Rol inválido: {invalidos[0]!r}.")
    cambiados = 0
    with get_session() as session:
        for personal_id, rol in cambios.items():
            persona = session.get(Personal, personal_id)
            if persona is None:
                raise ValueError(f"El bombero {personal_id} ya no existe en la base.")
            if normalizar_rol(persona.rol) != rol.strip().upper():
                persona.rol = rol
                cambiados += 1
    return cambiados


def _credenciales_ruba() -> Dict[str, str]:
    # Import diferido: ruba_helpers arrastra Playwright, que el PIN no necesita.
    from app.services.ruba_helpers import cargar_credenciales

    return cargar_credenciales()


def autorizacion_roles_usa_ruba(credenciales: Optional[Dict[str, str]] = None) -> bool:
    """True si hay cuenta institucional de RUBA guardada (usuario y clave):
    entonces es ella la que autoriza los cambios de rol. Si no, la clave maestra."""
    cred = _credenciales_ruba() if credenciales is None else credenciales
    return bool((cred.get("usuario") or "").strip() and cred.get("clave"))


def autorizar_cambio_roles(usuario: str, clave: str, credenciales: Optional[Dict[str, str]] = None) -> bool:
    """Valida la autorización para cambiar roles, sin red: contra la cuenta
    de RUBA guardada en data/config.json (usuario sin distinguir mayúsculas,
    clave exacta, comparación en tiempo constante) o, si no hay cuenta
    guardada, contra la clave maestra (con su bloqueo por intentos)."""
    cred = _credenciales_ruba() if credenciales is None else credenciales
    if not autorizacion_roles_usa_ruba(cred):
        return verificar_clave_maestra(clave)
    usuario_ok = hmac.compare_digest((usuario or "").strip().lower().encode(),
                                     (cred.get("usuario") or "").strip().lower().encode())
    clave_ok = hmac.compare_digest((clave or "").encode(), (cred.get("clave") or "").encode())
    return usuario_ok and clave_ok

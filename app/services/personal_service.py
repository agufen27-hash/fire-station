"""
Autenticación del personal: punto único para validar y cambiar PIN.

Toda la UI (autoría de planilla, firma de dotación, acceso al legajo,
cambio y reseteo de PIN) pasa por acá; ninguna pantalla lee ni compara
`Personal.pin` por su cuenta.
"""

from __future__ import annotations

from typing import Optional

from app.core.security import (
    PIN_DEFAULT,
    almacenado_es_default,
    hash_pin,
    problema_pin_nuevo,
    verificar_clave_maestra,
    verificar_pin,
)
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

"""
Semáforo Pre-RUBA: validación estática de un parte ANTES de abrir Chromium.

Trabaja sobre el payload que ya consume la automatización (la misma
estructura sale del formulario abierto -- MainWindow.obtener_payload_servicio
-- y de un parte guardado -- ruba_payload.payload_desde_incidente), así que
no hay una segunda interpretación de los datos.

    ok, faltantes = validar_parte_para_ruba(payload)
    semaforo = revisar_parte_para_ruba(payload)     # + avisos no bloqueantes

Dos niveles:
  - FALTANTES: lo que RUBA exige y el parte no trae (horarios de los móviles,
    chofer, Tipo / Categoría, descripción, Tipo de lugar del incendio...).
    Con faltantes no tiene sentido lanzar Playwright: RUBA rechazaría el
    guardado después de varios minutos de navegador.
  - AVISOS: campos obligatorios vacíos que la automatización completa con el
    `<campo>_default` de config/ruba_mapping.json (p. ej. la causa), o que la
    UI todavía no permite cargar. No frenan, pero conviene revisarlos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.catalogos import FORM_ACCIDENTE, FORM_ESTRUCTURAL, FORM_FORESTAL, FORM_INCENDIO, leer_mapping
from app.services.ruba_payload import validar_payload

# Campos condicionales que RUBA exige por formulario (claves de
# selectores.editar_general.condicionales en ruba_mapping.json).
REQUERIDOS_POR_FORMULARIO: Dict[str, Tuple[Tuple[str, str], ...]] = {
    FORM_FORESTAL: (("tipo_lugar", "el Tipo de lugar forestal"), ("causa", "la Causa del incendio")),
    FORM_ESTRUCTURAL: (("tipo_lugar", "el Tipo de lugar"), ("causa", "la Causa del incendio")),
    FORM_INCENDIO: (("causa", "la Causa del incendio"),),
    FORM_ACCIDENTE: (("clima", "las Condiciones climáticas"), ("causa", "la Causa del accidente")),
    "rescate": (("tipo_lugar", "el Tipo de lugar del rescate"),),
}


@dataclass
class SemaforoRuba:
    faltantes: List[str] = field(default_factory=list)
    avisos: List[str] = field(default_factory=list)

    @property
    def listo(self) -> bool:
        return not self.faltantes


def _fecha_hora(fecha: Optional[str], hora: Optional[str]) -> Optional[datetime]:
    """'07/10/2026' + '23:30' -> datetime (formato del payload); None si falta."""
    if not fecha or not hora:
        return None
    try:
        return datetime.strptime(f"{fecha} {hora}", "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def _nombre_movil(vehiculo: Dict[str, Any], indice: int) -> str:
    numero = (vehiculo.get("numero_movil") or "").strip() if isinstance(vehiculo.get("numero_movil"), str) else None
    return f"Móvil {numero}" if numero else f"Móvil {indice}"


def _revisar_horario(etiqueta: str, datos: Dict[str, Any], claves: Tuple[str, str, str, str],
                     faltantes: List[str]) -> None:
    f_ini, h_ini, f_fin, h_fin = (datos.get(c) for c in claves)
    if not h_ini:
        faltantes.append(f"{etiqueta} sin hora de salida")
    if not h_fin:
        faltantes.append(f"{etiqueta} sin hora de regreso")
    inicio, fin = _fecha_hora(f_ini, h_ini), _fecha_hora(f_fin, h_fin)
    if inicio and fin and fin < inicio:
        faltantes.append(f"{etiqueta}: el regreso ({f_fin} {h_fin}) es anterior a la salida ({f_ini} {h_ini})")


def _revisar_condicionales(condicionales: Optional[Dict[str, Any]], mapping: Dict[str, Any],
                           semaforo: SemaforoRuba) -> None:
    if not condicionales or not condicionales.get("formulario"):
        return
    formulario = condicionales["formulario"]
    campos = condicionales.get("campos") or {}
    bloque = (mapping.get("selectores", {}).get("editar_general", {}).get("condicionales", {}) or {}).get(formulario) or {}
    for campo, descripcion in REQUERIDOS_POR_FORMULARIO.get(formulario, ()):
        if campos.get(campo) not in (None, ""):
            continue
        falta = "Faltan" if descripcion.startswith("las ") else "Falta"
        defecto = bloque.get(f"{campo}_default")
        opciones = bloque.get(f"{campo}_opciones") or {}
        if defecto not in (None, ""):
            nombre = next((k.replace("_", " ").capitalize() for k, v in opciones.items() if str(v) == str(defecto)),
                          str(defecto))
            semaforo.avisos.append(f"{falta} {descripcion}: en RUBA se cargará '{nombre}' por defecto")
        elif not opciones:
            # La UI no ofrece opciones para este campo (no hay códigos en el mapping):
            # no se puede exigir al operador; se completa a mano en RUBA.
            semaforo.avisos.append(f"{falta} {descripcion} (sin opciones en ruba_mapping.json: completalo en RUBA)")
        else:
            semaforo.faltantes.append(f"{falta} {descripcion}")


def revisar_parte_para_ruba(parte: Dict[str, Any], mapping: Optional[Dict[str, Any]] = None) -> SemaforoRuba:
    """Faltantes (bloquean) y avisos (no bloquean) del payload de un parte."""
    semaforo = SemaforoRuba()
    faltantes = semaforo.faltantes
    if not parte:
        faltantes.append("El parte está vacío")
        return semaforo
    if mapping is None:
        try:
            mapping = leer_mapping()
        except (OSError, ValueError):
            mapping = {}

    ini = parte.get("inicializacion") or {}
    if not ini.get("numero_parte"):
        faltantes.append("El N° de parte no tiene dígitos")
    if not ini.get("tipo_incidente"):
        faltantes.append("Falta el Tipo de siniestro")
    if not ini.get("categoria_incidente"):
        faltantes.append("Falta la Categoría (subtipo) del siniestro")

    general = parte.get("editar_general") or {}
    if not (general.get("localidad_autocomplete") or "").strip():
        faltantes.append("Falta la Localidad del siniestro")
    if not (general.get("calle") or "").strip():
        faltantes.append("Falta la Calle / lugar del siniestro")
    if not (general.get("descripcion") or "").strip():
        faltantes.append("Falta la Reseña operativa (Descripción del incidente)")
    _revisar_condicionales(general.get("condicionales"), mapping, semaforo)

    participacion = parte.get("participacion") or {}
    _revisar_horario("El servicio", participacion, ("fecha_salida", "hora_salida", "fecha_llegada", "hora_llegada"),
                     faltantes)

    vehiculos = (parte.get("intervencion_vehiculos") or {}).get("vehiculos") or []
    if not vehiculos:
        faltantes.append("No hay ningún móvil despachado")
    for i, v in enumerate(vehiculos, start=1):
        etiqueta = _nombre_movil(v, i)
        if not v.get("select_vehiculo") and not v.get("numero_movil"):
            faltantes.append(f"{etiqueta}: no es un vehículo oficial de RUBA")
        if not v.get("autocomplete_chofer"):
            faltantes.append(f"{etiqueta} sin chofer")
        _revisar_horario(etiqueta, v, ("fecha_salida", "hora_salida", "fecha_llegada", "hora_llegada"), faltantes)

    bomberos = (parte.get("intervencion_bomberos") or {}).get("bomberos") or []
    if not bomberos:
        faltantes.append("No hay bomberos intervinientes")
    elif sum(1 for b in bomberos if b.get("is_encargado")) != 1:
        faltantes.append("Tiene que haber exactamente un Encargado (Jefe de la Dotación N° 1)")
    for i, b in enumerate(bomberos, start=1):
        persona = b.get("autocomplete_nombre") or {}
        nombre = persona.get("nombre_completo") or f"Bombero {i}"
        if not b.get("hora_inicio") or not b.get("hora_fin"):
            faltantes.append(f"{nombre} sin horario de intervención completo")

    for i, v in enumerate(parte.get("vehiculos_accidentes") or [], start=1):
        if not v.get("marca"):
            faltantes.append(f"Vehículo damnificado {i} sin marca")

    # Lo que ya controlaba ruba_payload (mismo criterio que la automatización),
    # sin repetir lo que este semáforo ya dijo con otras palabras.
    for error in validar_payload(parte):
        if error.startswith(("El N° de parte", "Falta el Tipo", "Falta la Reseña", "Tiene que haber")):
            continue
        if "falta el chofer" in error or "no es un vehículo oficial" in error:
            continue
        faltantes.append(error.rstrip("."))

    semaforo.faltantes = list(dict.fromkeys(faltantes))
    semaforo.avisos = list(dict.fromkeys(semaforo.avisos))
    return semaforo


def validar_parte_para_ruba(parte_dict: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """(listo, faltantes): True si se puede lanzar Playwright sin que RUBA
    rechace el parte por datos que faltan. No abre el navegador."""
    semaforo = revisar_parte_para_ruba(parte_dict)
    return semaforo.listo, semaforo.faltantes


def validar_incidente_para_ruba(incidente_id: int) -> SemaforoRuba:
    """Semáforo de un parte guardado (lee la base; no abre el navegador)."""
    from app.services.ruba_payload import payload_desde_incidente

    try:
        return revisar_parte_para_ruba(payload_desde_incidente(incidente_id))
    except Exception as e:  # noqa: BLE001 - un parte ilegible se informa como faltante
        return SemaforoRuba(faltantes=[f"No se pudo armar el parte: {type(e).__name__}: {e}"])

"""
Piezas compartidas de la participación del servicio:

- `SelectorBombero`: QLineEdit + QCompleter sobre el padrón oficial
  (data/Reporte de bomberos.xlsx); resuelve el texto a un `id_ruba`.
- `HorarioServicio`: horario general (salida / llegada) que siguen las unidades.
- Textos y opciones de móviles y tipos de tarea (de ruba_mapping.json).

Las tarjetas "Dotaciones por Unidad" están en app/ui/dotaciones_widgets.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCompleter, QLineEdit, QWidget

from app.core.catalogos import Bombero, Movil as MovilRuba
from app.ui import theme


def texto_bombero(bombero: Bombero) -> str:
    base = bombero.nombre_completo
    return f"{base} — Leg. {bombero.legajo}" if bombero.legajo else base


def texto_movil(movil: MovilRuba) -> str:
    detalle = " ".join(x for x in (movil.marca, movil.modelo) if x)
    return f"{movil.numero} — {detalle}" if detalle else movil.numero


def opciones_tipo_tarea(mapping: Dict) -> List[Tuple[str, str]]:
    opciones = mapping.get("selectores", {}).get("intervencion_bomberos", {}).get("tipo_tarea_opciones") or {}
    return [(clave.replace("_", " ").capitalize(), str(valor)) for clave, valor in opciones.items()]


# ---------------------------------------------------------------------------
# Selector predictivo de bombero
# ---------------------------------------------------------------------------

class SelectorBombero(QLineEdit):
    """Escribir parte del apellido, nombre o legajo sugiere coincidencias
    del padrón. `id_ruba()` devuelve None mientras el texto no sea
    exactamente una persona del padrón."""

    cambiado = Signal()

    def __init__(self, padron: Sequence[Bombero], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._id_por_texto: Dict[str, int] = {}
        self._texto_por_id: Dict[int, str] = {}
        for b in padron:
            texto = texto_bombero(b)
            self._id_por_texto[texto.upper()] = b.id_ruba
            self._texto_por_id[b.id_ruba] = texto

        completer = QCompleter(list(self._texto_por_id.values()), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(completer)
        self.setPlaceholderText("Escribí apellido, nombre o legajo…")
        self.textChanged.connect(self._on_texto)

    def _on_texto(self, _texto: str) -> None:
        texto_vacio_o_valido = not self.text().strip() or self.id_ruba() is not None
        theme.marcar_invalido(self, not texto_vacio_o_valido)
        self.setToolTip("" if texto_vacio_o_valido else "No coincide con nadie del padrón: elegí una sugerencia.")
        self.cambiado.emit()

    def id_ruba(self) -> Optional[int]:
        return self._id_por_texto.get(self.text().strip().upper())

    def set_id_ruba(self, id_ruba: Optional[int]) -> None:
        self.setText(self._texto_por_id.get(id_ruba, "") if id_ruba is not None else "")

    def esta_vacio(self) -> bool:
        return not self.text().strip()


# ---------------------------------------------------------------------------
# Vehículos intervinientes
# ---------------------------------------------------------------------------

@dataclass
class HorarioServicio:
    fecha_salida: Optional[date] = None
    hora_salida: Optional[time] = None
    fecha_llegada: Optional[date] = None
    hora_llegada: Optional[time] = None

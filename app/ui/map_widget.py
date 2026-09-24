"""
Compatibilidad: el mapa táctico vive en `app/ui/widgets/map_widget.py`.
Este módulo solo re-exporta sus nombres públicos para los imports viejos.
"""

from app.ui.widgets.map_widget import (  # noqa: F401
    CUARTEL_LAT,
    CUARTEL_LON,
    MapWidget,
    OperationsMapWindow,
)

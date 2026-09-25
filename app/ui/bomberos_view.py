"""
Importación del 'Reporte de bomberos' de RUBA desde la página Personal y
Unidades. El Excel lo elige SIEMPRE el usuario con QFileDialog (nunca una
ruta fija); la lógica vive en app/services/ruba_importer.py.

Qué hace: upsert de TODO el personal del reporte en la base (Id de RUBA ->
id_ruba, Legajo, DNI, Apellido, Nombre, Jerarquía/Formación y Estado
Activo/Reserva/Baja) y actualiza la copia local del padrón que usan los
selectores de las dotaciones de RUBA.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from app.db import importar_bomberos_excel


def importar_bomberos_desde_excel(parent: QWidget) -> bool:
    """Diálogo completo (elegir archivo -> importar -> resumen). Devuelve
    True si se importó algo (el que llama refresca tablas y padrón)."""
    ruta_texto, _ = QFileDialog.getOpenFileName(
        parent, "Importar bomberos (Reporte de bomberos de RUBA)", "", "Excel (*.xlsx *.xlsm)"
    )
    if not ruta_texto:
        return False
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        resumen, activos = importar_bomberos_excel(Path(ruta_texto))
    except (OSError, ValueError) as e:
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(parent, "No se pudo importar el personal", str(e))
        return False
    except Exception as e:  # noqa: BLE001 - base u openpyxl: la transacción ya se revirtió
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(parent, "No se pudo importar el personal",
                             f"{type(e).__name__}: {e}\n\nNo se modificó ningún bombero.")
        return False
    QApplication.restoreOverrideCursor()

    texto = f"{resumen.total} bomberos importados ({resumen.nuevas} nuevos, {resumen.actualizadas} actualizados)."
    if activos is not None:
        texto += (f"\nPadrón de RUBA: {activos} activos. Los selectores del formulario de parte "
                  "lo toman al reiniciar la app.")
    if resumen.avisos:
        texto += "\n\nAvisos:\n• " + "\n• ".join(resumen.avisos)
    QMessageBox.information(parent, "Personal importado", texto)
    return True


def recargar_padron() -> Optional[list]:
    """Padrón activo recién importado (para el chip de la barra superior)."""
    from app.core.catalogos import obtener_padron

    try:
        return obtener_padron().bomberos
    except Exception:  # noqa: BLE001 - sin padrón la app sigue funcionando
        return None

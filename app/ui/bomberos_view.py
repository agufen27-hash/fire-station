"""
Importación del 'Reporte de bomberos' de RUBA desde la página Personal y
Unidades. El Excel lo elige SIEMPRE el usuario con QFileDialog (nunca una
ruta fija); la lógica vive en app/services/ruba_importer.py.

Qué hace: upsert de TODO el personal del reporte en la base (Id de RUBA,
Legajo, DNI, Apellido, Nombre, fechas de nacimiento / ingreso / último
ascenso, contacto, grupo sanguíneo, cargo, jerarquía, formación, estudios y
situación de revista) sin tocar los PIN existentes, y actualiza la copia
local del padrón que usan los selectores de las dotaciones de RUBA.
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

    texto = resumen.texto_personal() + "."
    if resumen.nuevas:
        texto += "\nLos dados de alta tienen el PIN de fábrica y se les pedirá cambiarlo al usarlo."
    if activos is not None:
        texto += f"\nPadrón de RUBA: {activos} activos (los selectores de la planilla ya lo toman)."
    if resumen.avisos:
        texto += "\n\nAvisos:\n• " + "\n• ".join(resumen.avisos[:15])
        if len(resumen.avisos) > 15:
            texto += f"\n• … y {len(resumen.avisos) - 15} más"
    if resumen.ignoradas or resumen.avisos:
        texto += "\n\nEl detalle fila por fila quedó en logs/app.log."
    QMessageBox.information(parent, "Personal importado", texto)
    return True


def recargar_padron() -> Optional[list]:
    """Padrón activo recién importado (para el chip de la barra superior)."""
    from app.core.catalogos import obtener_padron

    try:
        return obtener_padron().bomberos
    except Exception:  # noqa: BLE001 - sin padrón la app sigue funcionando
        return None

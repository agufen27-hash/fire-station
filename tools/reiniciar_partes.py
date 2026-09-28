"""
Deja la base de Fire Station con CERO partes/incidentes para arrancar la
carga limpia del año. Conserva personal, móviles, contactos, catálogos y la
configuración del cuartel. Antes guarda un respaldo completo de la base en
data/respaldos/.

    python -m tools.reiniciar_partes          # pide confirmación
    python -m tools.reiniciar_partes --si     # sin preguntar

La misma acción está en la app: Configuración -> Mantenimiento.
"""

from __future__ import annotations

import sys

from app.db import DB_PATH, get_session, init_db, reiniciar_partes
from app.models import Incidente


def main(argv: list) -> int:
    init_db()
    with get_session() as session:
        cantidad = session.query(Incidente).count()
    print(f"Base: {DB_PATH}\nPartes cargados: {cantidad}")
    if cantidad == 0:
        print("No hay partes para borrar.")
        return 0
    if "--si" not in argv:
        respuesta = input("Se borran TODOS los partes (personal y móviles se conservan). Escribí BORRAR para seguir: ")
        if respuesta.strip() != "BORRAR":
            print("Cancelado: no se modificó nada.")
            return 1
    borrados, respaldo = reiniciar_partes()
    print(f"Listo: {borrados} parte(s) borrados. Respaldo previo: {respaldo}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

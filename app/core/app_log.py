"""
Log de la aplicación en logs/app.log (rotativo).

La app no configura `logging` global y el .exe no tiene consola. Los módulos
que necesitan dejar rastro para soporte (respaldo al cerrar, importación del
padrón...) llaman a `asegurar_log_app()`: se agrega UNA sola vez un
RotatingFileHandler al logger raíz del paquete ("app"), así todos los
loggers `app.*` (logging.getLogger(__name__)) escriben en el mismo archivo
sin pelearse por la rotación.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.paths import get_writable_dir

NOMBRE_LOG = "app.log"
_LOGGER_PAQUETE = "app"


def asegurar_log_app() -> None:
    raiz = logging.getLogger(_LOGGER_PAQUETE)
    if getattr(raiz, "_con_app_log", False):
        return
    raiz._con_app_log = True  # aunque falle abajo: no reintentar en cada mensaje
    try:
        manejador = RotatingFileHandler(get_writable_dir("logs") / NOMBRE_LOG, maxBytes=1024 * 1024,
                                        backupCount=3, encoding="utf-8")
        manejador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        raiz.addHandler(manejador)
        if raiz.level == logging.NOTSET or raiz.level > logging.INFO:
            raiz.setLevel(logging.INFO)
    except OSError as e:
        print(f"[Log] No se pudo abrir logs/{NOMBRE_LOG}: {e}")

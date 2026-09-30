"""
Rangos de fecha + hora (salida -> regreso) de un servicio o una dotación.

La base guarda fecha y hora en columnas separadas; acá se combinan en un
`datetime` completo para no comparar nunca solo "HH:MM". Regla de la
medianoche: si el regreso no tiene fecha propia (o tiene la misma que la
salida) y su hora es anterior a la de salida, el servicio cruzó la
medianoche -> el regreso es al día siguiente (23:30 -> 01:15 = 1 h 45 min).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple


def combinar(dia: Optional[date], hora: Optional[time]) -> Optional[datetime]:
    return datetime.combine(dia, hora) if dia is not None and hora is not None else None


def fecha_fin_ajustada(fecha_ini: Optional[date], hora_ini: Optional[time],
                       fecha_fin: Optional[date], hora_fin: Optional[time]) -> Optional[date]:
    """Fecha de regreso coherente con la salida: sin fecha usa la de salida,
    y si la hora de regreso es anterior a la de salida el mismo día, pasa al
    día siguiente. Una fecha de regreso posterior a la salida se respeta."""
    fin = fecha_fin or fecha_ini
    if fin is None or fecha_ini is None:
        return fin
    if fin < fecha_ini:
        fin = fecha_ini
    if fin == fecha_ini and hora_ini is not None and hora_fin is not None and hora_fin < hora_ini:
        fin = fecha_ini + timedelta(days=1)
    return fin


def rango(fecha_ini: Optional[date], hora_ini: Optional[time],
          fecha_fin: Optional[date], hora_fin: Optional[time]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(inicio, fin) como datetime completos, con la regla de la medianoche."""
    return (combinar(fecha_ini, hora_ini),
            combinar(fecha_fin_ajustada(fecha_ini, hora_ini, fecha_fin, hora_fin), hora_fin))


def duracion(fecha_ini: Optional[date], hora_ini: Optional[time],
             fecha_fin: Optional[date], hora_fin: Optional[time]) -> Optional[timedelta]:
    """Duración del servicio (nunca negativa); None si falta algún dato."""
    inicio, fin = rango(fecha_ini, hora_ini, fecha_fin, hora_fin)
    if inicio is None or fin is None:
        return None
    return max(fin - inicio, timedelta(0))

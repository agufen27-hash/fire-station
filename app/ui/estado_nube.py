"""Texto del chip de sincronización de las páginas que leen de Supabase."""

from __future__ import annotations

from typing import Tuple

from app.services import cache_offline


def chip_tras_carga() -> Tuple[str, str, str]:
    """(texto, tono, tooltip) después de una lectura: en línea o desde la caché local."""
    from app.ui.libro_guardia_view import ahora_cuartel
    from app.services.libro_guardia_service import TZ_CUARTEL

    estado = cache_offline.estado()
    if estado.en_linea:
        return f"☁️ Sincronizado {ahora_cuartel():%H:%M}", "ok", ""
    guardado = estado.dato_cacheado_at
    hora = f" de las {guardado.astimezone(TZ_CUARTEL):%d/%m %H:%M}" if guardado else ""
    return (f"📴 Sin conexión · datos guardados{hora}", "alerta",
            "No hay conexión con la nube: se muestran los últimos datos guardados en esta PC.\n"
            "Las salidas, regresos y revisiones que registres se guardan acá y se envían solas "
            "al volver la conexión.\n\n" + (estado.ultimo_error or ""))


def texto_encolado(que: str) -> str:
    return (f"📴 Sin conexión: {que} quedó guardado en esta PC con su hora real y se envía solo "
            "apenas vuelva la conexión (chip ⏳ de la barra superior).")

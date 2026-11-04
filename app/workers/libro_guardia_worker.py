"""Compatibilidad: el QThread del Libro de Guardia es el genérico de Supabase."""

from app.workers.supabase_tarea import TareaSupabase as TareaLibroGuardia

__all__ = ["TareaLibroGuardia"]

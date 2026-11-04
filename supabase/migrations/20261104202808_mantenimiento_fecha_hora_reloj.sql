-- La "última revisión" de cada tarea se decide por fecha_hora: con now() (hora
-- de inicio de la transacción) dos checklists de una misma transacción
-- empataban. clock_timestamp() es la hora real de cada inserción.
alter table public.mantenimiento_registros alter column fecha_hora set default clock_timestamp();

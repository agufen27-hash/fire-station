-- El proyecto no otorga DML por defecto en public: el backend (service_role)
-- necesita privilegios explícitos. anon / authenticated quedan sin acceso.
-- (Detectado en el primer sync real: 403 / 42501 "permission denied for table personal_cloud".)
grant usage on schema public to service_role;

grant select, insert, update, delete on
  public.zonas_geocerca,
  public.personal_cloud,
  public.bot_sesiones_pendientes,
  public.asistencias_eventos,
  public.flota_moviles,
  public.novedades_taller_moviles,
  public.movimientos_moviles
to service_role;

grant select on public.vw_powerbi_horas_personal, public.vw_estado_flota_resumen to service_role;

revoke all on
  public.zonas_geocerca,
  public.personal_cloud,
  public.bot_sesiones_pendientes,
  public.asistencias_eventos,
  public.flota_moviles,
  public.novedades_taller_moviles,
  public.movimientos_moviles
from anon, authenticated;

-- Tablas futuras creadas por postgres en public: mismo criterio.
alter default privileges for role postgres in schema public
  grant select, insert, update, delete on tables to service_role;
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;

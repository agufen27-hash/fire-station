-- =============================================================================
-- Rol ESTADISTICA en personal_cloud
--
-- La app de escritorio suma el rol ESTADISTICA (app/core/roles.py): carga /
-- sincronización con RUBA y acceso total a Estadísticas. El CHECK original de
-- personal_cloud.rol solo admitía BOMBERO / JEFATURA / ADMIN, así que el
-- UPSERT del padrón fallaría al subir a alguien con el rol nuevo.
--
-- Al aplicarla con apply_migration, renombrar este archivo con la versión que
-- registre Supabase (ver list_migrations).
-- =============================================================================

do $$
declare
  restriccion text;
begin
  for restriccion in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where nsp.nspname = 'public'
      and rel.relname = 'personal_cloud'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) ilike '%rol%JEFATURA%'
  loop
    execute format('alter table public.personal_cloud drop constraint %I', restriccion);
  end loop;
end $$;

alter table public.personal_cloud
  add constraint personal_cloud_rol_check
  check (rol in ('BOMBERO', 'JEFATURA', 'ADMIN', 'ESTADISTICA'));

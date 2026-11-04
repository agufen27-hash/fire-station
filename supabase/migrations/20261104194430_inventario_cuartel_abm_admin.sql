-- =============================================================================
-- Inventario del cuartel (ABM de activos: vehículos, herramientas, equipos de
-- protección respiratoria, EPP, comunicaciones...).
--
-- Seguridad: la tabla es de SOLO LECTURA para service_role (el rol que usa la
-- app de escritorio). Toda escritura pasa por las RPC rpc_inventario_*,
-- SECURITY DEFINER, que exigen un operador con rol ADMIN y activo en
-- personal_cloud y dejan registrado quién creó / modificó / dio de baja.
-- Así, aunque alguien saltee la UI, la base rechaza la escritura de un
-- usuario estándar (la UI además oculta / deshabilita los controles).
--
-- Baja = lógica (activo = false + fecha_baja + motivo): el historial del
-- activo nunca se borra.
-- =============================================================================

create table public.inventario_cuartel (
  id                  uuid primary key default gen_random_uuid(),
  codigo_qr           varchar(40) not null unique
                      default ('FS-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8)))
                      check (codigo_qr ~ '^[A-Z0-9][A-Z0-9-]{2,39}$'),
  nombre              text not null check (length(trim(nombre)) between 1 and 120),
  categoria           varchar(20) not null
                      check (categoria in ('VEHICULO', 'HERRAMIENTA', 'PROTECCION_RESPIRATORIA', 'EPP',
                                           'COMUNICACIONES', 'RESCATE', 'MEDICO', 'OTRO')),
  ubicacion           text not null check (length(trim(ubicacion)) between 1 and 120),
  estado              varchar(20) not null default 'OPERATIVO'
                      check (estado in ('OPERATIVO', 'EN_REPARACION', 'FUERA_DE_SERVICIO')),
  marca_modelo        text,
  numero_serie        text,
  observaciones       text,
  activo              boolean not null default true,
  motivo_baja         text,
  fecha_baja          timestamptz,
  fecha_alta          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  creado_por_id       integer references public.personal_cloud(id),
  modificado_por_id   integer references public.personal_cloud(id),
  constraint inventario_baja_coherente check (activo = (fecha_baja is null))
);
comment on table public.inventario_cuartel is
  'Activos del cuartel. Escritura solo vía rpc_inventario_* (operador ADMIN).';

create index inventario_estado_idx on public.inventario_cuartel (estado) where activo;
create index inventario_ubicacion_idx on public.inventario_cuartel (ubicacion) where activo;

create trigger inventario_cuartel_updated_at before update on public.inventario_cuartel
  for each row execute function public.tg_set_updated_at();

alter table public.inventario_cuartel enable row level security;
revoke all on public.inventario_cuartel from anon, authenticated, service_role;
grant select on public.inventario_cuartel to service_role;

-- -----------------------------------------------------------------------------
-- Guardia de rol
-- -----------------------------------------------------------------------------
create or replace function public.inventario_operador_motivo(p_operador_id integer)
returns text
language sql
stable
security definer
set search_path = public
as $$
  select case
    when p_operador_id is null then 'SIN_OPERADOR'
    when not exists (select 1 from public.personal_cloud where id = p_operador_id) then 'OPERADOR_DESCONOCIDO'
    when exists (select 1 from public.personal_cloud where id = p_operador_id and rol = 'ADMIN' and activo) then null
    else 'OPERADOR_NO_ADMIN'
  end
$$;

-- -----------------------------------------------------------------------------
-- Alta / edición (p_id null = alta)
-- -----------------------------------------------------------------------------
create or replace function public.rpc_inventario_guardar(p_operador_id integer, p_id uuid, p_datos jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_motivo text := public.inventario_operador_motivo(p_operador_id);
  v_codigo text := nullif(upper(trim(coalesce(p_datos->>'codigo_qr', ''))), '');
  v_fila   public.inventario_cuartel;
begin
  if v_motivo is not null then
    return jsonb_build_object('ok', false, 'motivo', v_motivo);
  end if;
  if length(trim(coalesce(p_datos->>'nombre', ''))) = 0
     or length(trim(coalesce(p_datos->>'ubicacion', ''))) = 0
     or coalesce(p_datos->>'categoria', '') = '' then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INCOMPLETOS');
  end if;

  if p_id is null then
    insert into public.inventario_cuartel
      (codigo_qr, nombre, categoria, ubicacion, estado, marca_modelo, numero_serie, observaciones,
       creado_por_id, modificado_por_id)
    values
      (coalesce(v_codigo, 'FS-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8))),
       trim(p_datos->>'nombre'), p_datos->>'categoria', trim(p_datos->>'ubicacion'),
       coalesce(nullif(p_datos->>'estado', ''), 'OPERATIVO'),
       nullif(trim(coalesce(p_datos->>'marca_modelo', '')), ''),
       nullif(trim(coalesce(p_datos->>'numero_serie', '')), ''),
       nullif(trim(coalesce(p_datos->>'observaciones', '')), ''),
       p_operador_id, p_operador_id)
    returning * into v_fila;
  else
    update public.inventario_cuartel set
      codigo_qr         = coalesce(v_codigo, codigo_qr),
      nombre            = trim(p_datos->>'nombre'),
      categoria         = p_datos->>'categoria',
      ubicacion         = trim(p_datos->>'ubicacion'),
      estado            = coalesce(nullif(p_datos->>'estado', ''), estado),
      marca_modelo      = nullif(trim(coalesce(p_datos->>'marca_modelo', '')), ''),
      numero_serie      = nullif(trim(coalesce(p_datos->>'numero_serie', '')), ''),
      observaciones     = nullif(trim(coalesce(p_datos->>'observaciones', '')), ''),
      modificado_por_id = p_operador_id
    where id = p_id and activo
    returning * into v_fila;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'ITEM_INEXISTENTE_O_DADO_DE_BAJA');
    end if;
  end if;
  return jsonb_build_object('ok', true, 'item', to_jsonb(v_fila));
exception
  when unique_violation then
    return jsonb_build_object('ok', false, 'motivo', 'CODIGO_QR_DUPLICADO');
  when check_violation then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INVALIDOS', 'detalle', sqlerrm);
end
$$;

-- -----------------------------------------------------------------------------
-- Baja lógica
-- -----------------------------------------------------------------------------
create or replace function public.rpc_inventario_baja(p_operador_id integer, p_id uuid, p_motivo text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_motivo text := public.inventario_operador_motivo(p_operador_id);
begin
  if v_motivo is not null then
    return jsonb_build_object('ok', false, 'motivo', v_motivo);
  end if;
  if length(trim(coalesce(p_motivo, ''))) = 0 then
    return jsonb_build_object('ok', false, 'motivo', 'FALTA_MOTIVO_BAJA');
  end if;
  update public.inventario_cuartel
     set activo = false, fecha_baja = now(), motivo_baja = trim(p_motivo), modificado_por_id = p_operador_id
   where id = p_id and activo;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'ITEM_INEXISTENTE_O_DADO_DE_BAJA');
  end if;
  return jsonb_build_object('ok', true, 'id', p_id);
end
$$;

-- Vista de lectura con los nombres de quién creó / modificó.
create or replace view public.vw_inventario_cuartel
with (security_invoker = true) as
select i.*,
       public.nombre_legible(c.nombre_completo) as creado_por,
       public.nombre_legible(m.nombre_completo) as modificado_por
from public.inventario_cuartel i
left join public.personal_cloud c on c.id = i.creado_por_id
left join public.personal_cloud m on m.id = i.modificado_por_id;

grant select on public.vw_inventario_cuartel to service_role;
revoke all on public.vw_inventario_cuartel from anon, authenticated;

revoke all on function public.inventario_operador_motivo(integer) from public, anon, authenticated;
revoke all on function public.rpc_inventario_guardar(integer, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.rpc_inventario_baja(integer, uuid, text) from public, anon, authenticated;
grant execute on function public.rpc_inventario_guardar(integer, uuid, jsonb) to service_role;
grant execute on function public.rpc_inventario_baja(integer, uuid, text) to service_role;

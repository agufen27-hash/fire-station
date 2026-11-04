-- =============================================================================
-- Checklists y mantenimiento preventivo.
--
--   mantenimiento_tareas            plantillas de tareas, por CATEGORÍA del
--                                   inventario o por ÍTEM puntual, con
--                                   frecuencia DIARIA / SEMANAL / MENSUAL y
--                                   marca de tarea CRÍTICA.
--   mantenimiento_registros         cada checklist completado (ítem, cadencia,
--                                   responsable, fecha/hora, resultado).
--   mantenimiento_registro_tareas   estado de cada tarea en ese checklist
--                                   (OK / FALLA / NO_APLICA + observación),
--                                   con copia del título y de "crítica" para
--                                   que editar la plantilla no reescriba el
--                                   historial.
--
-- Motor de estado (mantenimiento_get_estado_general): por ítem evalúa cada
-- tarea aplicable -> AL_DIA / POR_VENCER / PENDIENTE / VENCIDA / FALLA.
--   * tarea CRÍTICA con FALLA o VENCIDA  -> FUERA_DE_SERVICIO
--   * otra tarea con FALLA o VENCIDA     -> CON_OBSERVACIONES
--   * si no                              -> OPERATIVO
-- mantenimiento_aplicar_estado lo lleva a inventario_cuartel: un ítem
-- OPERATIVO pasa solo a FUERA_DE_SERVICIO (fuera_servicio_automatico = true,
-- motivo_estado = tareas que fallaron / vencieron) y vuelve solo a OPERATIVO
-- cuando se regulariza. Nunca toca un ítem EN_REPARACION ni uno puesto fuera
-- de servicio a mano. Corre al registrar cada checklist y cada 15 min
-- (pg_cron), para detectar los plazos vencidos aunque nadie use la app.
--
-- Plazos: DIARIA 1 día, SEMANAL 7 días, MENSUAL 1 mes desde la última
-- revisión de cada tarea (o desde el alta del ítem / de la tarea, si nunca
-- se hizo). POR_VENCER = queda menos de 1/4 del plazo.
--
-- Escritura: solo vía RPC. Registrar checklist: cualquier integrante activo.
-- Plantillas: solo ADMIN (misma guarda que el inventario).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Inventario: marca de "fuera de servicio por mantenimiento"
-- -----------------------------------------------------------------------------
alter table public.inventario_cuartel
  add column fuera_servicio_automatico boolean not null default false,
  add column motivo_estado text;

-- Un cambio de estado hecho por una persona (no por el motor) borra la marca.
create or replace function public.tg_inventario_estado_manual()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.estado is distinct from old.estado
     and coalesce(current_setting('fs.motor_mantenimiento', true), 'off') <> 'on' then
    new.fuera_servicio_automatico := false;
    new.motivo_estado := null;
  end if;
  return new;
end
$$;
create trigger inventario_estado_manual before update on public.inventario_cuartel
  for each row execute function public.tg_inventario_estado_manual();

-- La vista del inventario se recrea para que incluya las columnas nuevas.
drop view public.vw_inventario_cuartel;
create view public.vw_inventario_cuartel
with (security_invoker = true) as
select i.*,
       public.nombre_legible(c.nombre_completo) as creado_por,
       public.nombre_legible(m.nombre_completo) as modificado_por
from public.inventario_cuartel i
left join public.personal_cloud c on c.id = i.creado_por_id
left join public.personal_cloud m on m.id = i.modificado_por_id;
grant select on public.vw_inventario_cuartel to service_role;
revoke all on public.vw_inventario_cuartel from anon, authenticated;

-- -----------------------------------------------------------------------------
-- Tablas
-- -----------------------------------------------------------------------------
create table public.mantenimiento_tareas (
  id             uuid primary key default gen_random_uuid(),
  titulo         text not null check (length(trim(titulo)) between 1 and 120),
  descripcion    text,
  frecuencia     varchar(10) not null check (frecuencia in ('DIARIA', 'SEMANAL', 'MENSUAL')),
  critica        boolean not null default false,
  categoria      varchar(30)
                 check (categoria in ('VEHICULO', 'HERRAMIENTA', 'PROTECCION_RESPIRATORIA', 'EPP',
                                      'COMUNICACIONES', 'RESCATE', 'MEDICO', 'OTRO')),
  item_id        uuid references public.inventario_cuartel(id),
  orden          integer not null default 100,
  activo         boolean not null default true,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  creado_por_id  integer references public.personal_cloud(id),
  constraint mantenimiento_tarea_alcance check ((categoria is null) <> (item_id is null))
);
create index mantenimiento_tareas_categoria_idx on public.mantenimiento_tareas (categoria) where activo;
create index mantenimiento_tareas_item_idx on public.mantenimiento_tareas (item_id) where activo;
create trigger mantenimiento_tareas_updated_at before update on public.mantenimiento_tareas
  for each row execute function public.tg_set_updated_at();

create table public.mantenimiento_registros (
  id              uuid primary key default gen_random_uuid(),
  item_id         uuid not null references public.inventario_cuartel(id),
  frecuencia      varchar(10) not null check (frecuencia in ('DIARIA', 'SEMANAL', 'MENSUAL')),
  responsable_id  integer not null references public.personal_cloud(id),
  fecha_hora      timestamptz not null default now(),
  resultado       varchar(20) not null check (resultado in ('APROBADO', 'CON_OBSERVACIONES', 'FALLIDO')),
  observaciones   text,
  origen          varchar(12) not null default 'ESCRITORIO' check (origen in ('ESCRITORIO', 'BOT', 'SQL'))
);
create index mantenimiento_registros_item_idx on public.mantenimiento_registros (item_id, fecha_hora desc);

create table public.mantenimiento_registro_tareas (
  registro_id   uuid not null references public.mantenimiento_registros(id) on delete cascade,
  tarea_id      uuid not null references public.mantenimiento_tareas(id),
  titulo        text not null,          -- copia al momento del checklist
  critica       boolean not null,
  estado        varchar(10) not null check (estado in ('OK', 'FALLA', 'NO_APLICA')),
  observacion   text,
  primary key (registro_id, tarea_id),
  constraint mantenimiento_falla_con_observacion check (estado <> 'FALLA' or length(trim(coalesce(observacion, ''))) > 0)
);
create index mantenimiento_registro_tareas_tarea_idx on public.mantenimiento_registro_tareas (tarea_id);

alter table public.mantenimiento_tareas enable row level security;
alter table public.mantenimiento_registros enable row level security;
alter table public.mantenimiento_registro_tareas enable row level security;
revoke all on public.mantenimiento_tareas, public.mantenimiento_registros, public.mantenimiento_registro_tareas
  from anon, authenticated, service_role;
grant select on public.mantenimiento_tareas, public.mantenimiento_registros, public.mantenimiento_registro_tareas
  to service_role;

-- -----------------------------------------------------------------------------
-- Motor
-- -----------------------------------------------------------------------------
create or replace function public.mantenimiento_plazo(p_frecuencia text)
returns interval
language sql
immutable
as $$
  select case p_frecuencia
    when 'DIARIA'  then interval '1 day'
    when 'SEMANAL' then interval '7 days'
    when 'MENSUAL' then interval '1 month'
  end
$$;

-- Una fila por (ítem activo, tarea activa que le aplica), con su última
-- ejecución y el estado de cumplimiento.
create view public.vw_mantenimiento_tareas_item
with (security_invoker = true) as
select
  i.id                                   as item_id,
  t.id                                   as tarea_id,
  t.frecuencia,
  t.titulo,
  t.descripcion,
  t.critica,
  t.orden,
  (t.item_id is not null)                as especifica_del_item,
  u.estado                               as ultimo_estado,
  u.fecha_hora                           as ultima_fecha,
  u.responsable                          as ultimo_responsable,
  u.observacion                          as ultima_observacion,
  coalesce(u.fecha_hora, greatest(i.fecha_alta, t.created_at)) + public.mantenimiento_plazo(t.frecuencia)
                                         as vence_at,
  case
    when u.estado = 'FALLA' then 'FALLA'
    when now() > coalesce(u.fecha_hora, greatest(i.fecha_alta, t.created_at))
                 + public.mantenimiento_plazo(t.frecuencia) then 'VENCIDA'
    when u.fecha_hora is null then 'PENDIENTE'
    when now() > u.fecha_hora + public.mantenimiento_plazo(t.frecuencia)
                 - public.mantenimiento_plazo(t.frecuencia) / 4 then 'POR_VENCER'
    else 'AL_DIA'
  end                                    as estado_tarea
from public.inventario_cuartel i
join public.mantenimiento_tareas t
  on t.activo and (t.item_id = i.id or (t.item_id is null and t.categoria = i.categoria))
left join lateral (
  select rt.estado, r.fecha_hora, public.nombre_legible(pc.nombre_completo) as responsable, rt.observacion
    from public.mantenimiento_registro_tareas rt
    join public.mantenimiento_registros r on r.id = rt.registro_id
    left join public.personal_cloud pc on pc.id = r.responsable_id
   where rt.tarea_id = t.id and r.item_id = i.id
   order by r.fecha_hora desc
   limit 1
) u on true
where i.activo;

-- getEstadoGeneral: evaluación completa de un ítem.
create or replace function public.mantenimiento_get_estado_general(p_item_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  with t as (
    select * from public.vw_mantenimiento_tareas_item where item_id = p_item_id
  ), f as (
    select frecuencia,
           count(*)                                                    as total,
           count(*) filter (where estado_tarea in ('AL_DIA', 'POR_VENCER')) as al_dia,
           count(*) filter (where estado_tarea = 'POR_VENCER')         as por_vencer,
           count(*) filter (where estado_tarea = 'PENDIENTE')          as pendientes,
           count(*) filter (where estado_tarea = 'VENCIDA')            as vencidas,
           count(*) filter (where estado_tarea = 'FALLA')              as fallas,
           count(*) filter (where critica and estado_tarea in ('FALLA', 'VENCIDA')) as criticas_mal,
           max(ultima_fecha)                                           as ultima_revision,
           min(vence_at)                                               as proximo_vencimiento
      from t group by frecuencia
  )
  select jsonb_build_object(
    'item_id', p_item_id,
    'estado_general', case
        when exists (select 1 from t where critica and estado_tarea in ('FALLA', 'VENCIDA')) then 'FUERA_DE_SERVICIO'
        when exists (select 1 from t where estado_tarea in ('FALLA', 'VENCIDA')) then 'CON_OBSERVACIONES'
        else 'OPERATIVO' end,
    'motivos', coalesce((
        select jsonb_agg(jsonb_build_object('tarea_id', tarea_id, 'titulo', titulo, 'frecuencia', frecuencia,
                                            'estado', estado_tarea, 'critica', critica)
                         order by critica desc, frecuencia, orden)
          from t where estado_tarea in ('FALLA', 'VENCIDA')), '[]'::jsonb),
    'por_frecuencia', coalesce((
        select jsonb_object_agg(frecuencia, jsonb_build_object(
                 'total', total, 'al_dia', al_dia, 'por_vencer', por_vencer, 'pendientes', pendientes,
                 'vencidas', vencidas, 'fallas', fallas, 'criticas_mal', criticas_mal,
                 'ultima_revision', ultima_revision, 'proximo_vencimiento', proximo_vencimiento))
          from f), '{}'::jsonb),
    'evaluado_at', now()
  )
$$;

-- Aplica la evaluación al estado del inventario (ver reglas arriba).
create or replace function public.mantenimiento_aplicar_estado(p_item_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_eval   jsonb := public.mantenimiento_get_estado_general(p_item_id);
  v_item   record;
  v_fuera  boolean := v_eval->>'estado_general' = 'FUERA_DE_SERVICIO';
  v_motivo text;
  v_nuevo  text;
begin
  select estado, fuera_servicio_automatico into v_item
    from public.inventario_cuartel where id = p_item_id and activo
    for update;
  if not found then
    return jsonb_build_object('cambio', false, 'evaluacion', v_eval);
  end if;

  select left(string_agg(format('%s (%s, %s)', m->>'titulo', lower(m->>'frecuencia'),
                                case m->>'estado' when 'FALLA' then 'falla' else 'vencida' end), '; '), 500)
    into v_motivo
    from jsonb_array_elements(v_eval->'motivos') m
   where (m->>'critica')::boolean;

  perform set_config('fs.motor_mantenimiento', 'on', true);
  if v_fuera and v_item.estado = 'OPERATIVO' then
    update public.inventario_cuartel
       set estado = 'FUERA_DE_SERVICIO', fuera_servicio_automatico = true,
           motivo_estado = 'Mantenimiento: ' || v_motivo
     where id = p_item_id;
    v_nuevo := 'FUERA_DE_SERVICIO';
  elsif v_fuera and v_item.fuera_servicio_automatico then
    update public.inventario_cuartel set motivo_estado = 'Mantenimiento: ' || v_motivo
     where id = p_item_id and motivo_estado is distinct from 'Mantenimiento: ' || v_motivo;
  elsif not v_fuera and v_item.fuera_servicio_automatico then
    update public.inventario_cuartel
       set estado = 'OPERATIVO', fuera_servicio_automatico = false, motivo_estado = null
     where id = p_item_id;
    v_nuevo := 'OPERATIVO';
  end if;
  perform set_config('fs.motor_mantenimiento', 'off', true);

  return jsonb_build_object('cambio', v_nuevo is not null, 'estado_anterior', v_item.estado,
                            'estado_nuevo', coalesce(v_nuevo, v_item.estado), 'evaluacion', v_eval);
end
$$;

create or replace function public.mantenimiento_evaluar_todo()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r record;
  n integer := 0;
begin
  for r in select distinct item_id from public.vw_mantenimiento_tareas_item
           union
           select id from public.inventario_cuartel where activo and fuera_servicio_automatico
  loop
    if (public.mantenimiento_aplicar_estado(r.item_id)->>'cambio')::boolean then
      n := n + 1;
    end if;
  end loop;
  return n;
end
$$;

-- Resumen por ítem para la grilla de cumplimiento.
create view public.vw_mantenimiento_items
with (security_invoker = true) as
select i.id, i.codigo_qr, i.nombre, i.categoria, i.ubicacion, i.estado,
       i.fuera_servicio_automatico, i.motivo_estado,
       e.eval->>'estado_general' as estado_general,
       e.eval->'motivos'         as motivos,
       e.eval->'por_frecuencia'  as por_frecuencia
from public.inventario_cuartel i
cross join lateral (select public.mantenimiento_get_estado_general(i.id) as eval) e
where i.activo
  and exists (select 1 from public.vw_mantenimiento_tareas_item t where t.item_id = i.id);

-- Historial de checklists.
create view public.vw_mantenimiento_historial
with (security_invoker = true) as
select r.id, r.item_id, i.nombre as item, i.codigo_qr, r.frecuencia, r.fecha_hora, r.resultado,
       r.observaciones, r.responsable_id, public.nombre_legible(pc.nombre_completo) as responsable,
       (select count(*) from public.mantenimiento_registro_tareas x where x.registro_id = r.id)::integer as tareas,
       (select count(*) from public.mantenimiento_registro_tareas x
         where x.registro_id = r.id and x.estado = 'FALLA')::integer as fallas
from public.mantenimiento_registros r
join public.inventario_cuartel i on i.id = r.item_id
left join public.personal_cloud pc on pc.id = r.responsable_id;

-- Plantillas con el nombre del ítem (si son específicas).
create view public.vw_mantenimiento_tareas
with (security_invoker = true) as
select t.*, i.nombre as item_nombre, i.codigo_qr as item_codigo_qr
from public.mantenimiento_tareas t
left join public.inventario_cuartel i on i.id = t.item_id;

-- -----------------------------------------------------------------------------
-- RPC: registrar un checklist (cualquier integrante activo)
-- p_respuestas: [{"tarea_id": "...", "estado": "OK|FALLA|NO_APLICA", "observacion": "..."}]
-- -----------------------------------------------------------------------------
create or replace function public.rpc_mantenimiento_registrar(
  p_operador_id integer, p_item_id uuid, p_frecuencia text, p_respuestas jsonb, p_observaciones text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_esperadas  uuid[];
  v_respondidas uuid[];
  v_faltantes  integer;
  v_falla_sin_obs text;
  v_resultado  text;
  v_registro   uuid;
  v_aplicado   jsonb;
begin
  if p_operador_id is null then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_OPERADOR');
  end if;
  if not exists (select 1 from public.personal_cloud where id = p_operador_id and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'OPERADOR_DESCONOCIDO');
  end if;
  if p_frecuencia not in ('DIARIA', 'SEMANAL', 'MENSUAL') then
    return jsonb_build_object('ok', false, 'motivo', 'FRECUENCIA_INVALIDA');
  end if;
  if not exists (select 1 from public.inventario_cuartel where id = p_item_id and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'ITEM_INEXISTENTE_O_DADO_DE_BAJA');
  end if;
  if jsonb_typeof(p_respuestas) is distinct from 'array' then
    return jsonb_build_object('ok', false, 'motivo', 'RESPUESTAS_INVALIDAS');
  end if;

  select array_agg(tarea_id) into v_esperadas
    from public.vw_mantenimiento_tareas_item where item_id = p_item_id and frecuencia = p_frecuencia;
  if v_esperadas is null then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_TAREAS');
  end if;

  -- Validación de las respuestas
  if exists (select 1 from jsonb_array_elements(p_respuestas) x
              where coalesce(x->>'estado', '') not in ('OK', 'FALLA', 'NO_APLICA')
                 or coalesce(x->>'tarea_id', '') !~ '^[0-9a-f-]{36}$') then
    return jsonb_build_object('ok', false, 'motivo', 'RESPUESTAS_INVALIDAS');
  end if;
  select coalesce(array_agg((x->>'tarea_id')::uuid), '{}'::uuid[]) into v_respondidas
    from jsonb_array_elements(p_respuestas) x;
  if (select count(*) from unnest(v_respondidas)) <> (select count(distinct u) from unnest(v_respondidas) u)
     or exists (select 1 from unnest(v_respondidas) u where u <> all (v_esperadas)) then
    return jsonb_build_object('ok', false, 'motivo', 'TAREA_AJENA');
  end if;
  select count(*) into v_faltantes from unnest(v_esperadas) e where e <> all (v_respondidas);
  if v_faltantes > 0 then
    return jsonb_build_object('ok', false, 'motivo', 'RESPUESTAS_INCOMPLETAS', 'faltantes', v_faltantes);
  end if;
  select t.titulo into v_falla_sin_obs
    from jsonb_array_elements(p_respuestas) x
    join public.mantenimiento_tareas t on t.id = (x->>'tarea_id')::uuid
   where x->>'estado' = 'FALLA' and length(trim(coalesce(x->>'observacion', ''))) = 0
   limit 1;
  if v_falla_sin_obs is not null then
    return jsonb_build_object('ok', false, 'motivo', 'FALTA_OBSERVACION_FALLA', 'tarea', v_falla_sin_obs);
  end if;

  select case
           when bool_or(t.critica and x->>'estado' = 'FALLA') then 'FALLIDO'
           when bool_or(x->>'estado' = 'FALLA') then 'CON_OBSERVACIONES'
           else 'APROBADO' end
    into v_resultado
    from jsonb_array_elements(p_respuestas) x
    join public.mantenimiento_tareas t on t.id = (x->>'tarea_id')::uuid;

  insert into public.mantenimiento_registros (item_id, frecuencia, responsable_id, resultado, observaciones)
  values (p_item_id, p_frecuencia, p_operador_id, v_resultado, nullif(trim(coalesce(p_observaciones, '')), ''))
  returning id into v_registro;

  insert into public.mantenimiento_registro_tareas (registro_id, tarea_id, titulo, critica, estado, observacion)
  select v_registro, t.id, t.titulo, t.critica, x->>'estado', nullif(trim(coalesce(x->>'observacion', '')), '')
    from jsonb_array_elements(p_respuestas) x
    join public.mantenimiento_tareas t on t.id = (x->>'tarea_id')::uuid;

  v_aplicado := public.mantenimiento_aplicar_estado(p_item_id);
  return jsonb_build_object(
    'ok', true, 'registro_id', v_registro, 'resultado', v_resultado,
    'estado_general', v_aplicado->'evaluacion'->>'estado_general',
    'estado_item', v_aplicado->>'estado_nuevo', 'cambio_estado', (v_aplicado->>'cambio')::boolean,
    'motivos', v_aplicado->'evaluacion'->'motivos');
end
$$;

-- -----------------------------------------------------------------------------
-- RPC: plantillas (solo ADMIN)
-- -----------------------------------------------------------------------------
create or replace function public.rpc_mantenimiento_tarea_guardar(p_operador_id integer, p_id uuid, p_datos jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_motivo text := public.inventario_operador_motivo(p_operador_id);
  v_item   uuid := nullif(p_datos->>'item_id', '')::uuid;
  v_cat    text := nullif(p_datos->>'categoria', '');
  v_fila   public.mantenimiento_tareas;
begin
  if v_motivo is not null then
    return jsonb_build_object('ok', false, 'motivo', v_motivo);
  end if;
  if length(trim(coalesce(p_datos->>'titulo', ''))) = 0 or coalesce(p_datos->>'frecuencia', '') = '' then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INCOMPLETOS');
  end if;
  if (v_item is null) = (v_cat is null) then
    return jsonb_build_object('ok', false, 'motivo', 'ALCANCE_INVALIDO');
  end if;
  if v_item is not null and not exists (select 1 from public.inventario_cuartel where id = v_item and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'ITEM_INEXISTENTE_O_DADO_DE_BAJA');
  end if;

  if p_id is null then
    insert into public.mantenimiento_tareas
      (titulo, descripcion, frecuencia, critica, categoria, item_id, orden, creado_por_id)
    values
      (trim(p_datos->>'titulo'), nullif(trim(coalesce(p_datos->>'descripcion', '')), ''),
       p_datos->>'frecuencia', coalesce((p_datos->>'critica')::boolean, false), v_cat, v_item,
       coalesce(nullif(p_datos->>'orden', '')::integer, 100), p_operador_id)
    returning * into v_fila;
  else
    update public.mantenimiento_tareas set
      titulo = trim(p_datos->>'titulo'),
      descripcion = nullif(trim(coalesce(p_datos->>'descripcion', '')), ''),
      frecuencia = p_datos->>'frecuencia',
      critica = coalesce((p_datos->>'critica')::boolean, critica),
      categoria = v_cat, item_id = v_item,
      orden = coalesce(nullif(p_datos->>'orden', '')::integer, orden)
    where id = p_id and activo
    returning * into v_fila;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'TAREA_INEXISTENTE');
    end if;
  end if;
  perform public.mantenimiento_evaluar_todo();
  return jsonb_build_object('ok', true, 'tarea', to_jsonb(v_fila));
exception
  when check_violation or string_data_right_truncation or invalid_text_representation then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INVALIDOS', 'detalle', sqlerrm);
end
$$;

create or replace function public.rpc_mantenimiento_tarea_desactivar(p_operador_id integer, p_id uuid)
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
  update public.mantenimiento_tareas set activo = false where id = p_id and activo;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'TAREA_INEXISTENTE');
  end if;
  perform public.mantenimiento_evaluar_todo();   -- puede devolver a OPERATIVO un ítem
  return jsonb_build_object('ok', true, 'id', p_id);
end
$$;

-- -----------------------------------------------------------------------------
-- Plantillas iniciales (editables / desactivables desde la app por un ADMIN)
-- -----------------------------------------------------------------------------
insert into public.mantenimiento_tareas (titulo, descripcion, frecuencia, critica, categoria, orden) values
  ('Niveles de aceite, agua y líquido de frenos', 'Motor frío, en superficie plana. Completar si falta.', 'DIARIA', true, 'VEHICULO', 10),
  ('Combustible al menos 3/4', null, 'DIARIA', true, 'VEHICULO', 20),
  ('Luces, balizas y sirena', 'Probar luces altas/bajas, giros, balizas y sirena.', 'DIARIA', true, 'VEHICULO', 30),
  ('Presión y estado de neumáticos', null, 'DIARIA', false, 'VEHICULO', 40),
  ('Equipamiento completo según inventario del móvil', null, 'DIARIA', false, 'VEHICULO', 50),
  ('Prueba de bomba y cebado', 'Bomba en marcha 5 min, verificar presión y cebado.', 'SEMANAL', true, 'VEHICULO', 60),
  ('Frenos, dirección y batería', null, 'SEMANAL', true, 'VEHICULO', 70),
  ('Prueba de equipos de radio (base, móviles y portátiles)', 'Prueba de enlace con la central y carga de baterías.', 'SEMANAL', true, 'COMUNICACIONES', 10),
  ('Presión del cilindro y prueba de alarma', 'Cilindro >= 90 % de carga; probar silbato de baja presión.', 'DIARIA', true, 'PROTECCION_RESPIRATORIA', 10),
  ('Vencimiento de prueba hidráulica del cilindro', 'Revisar fecha estampada; vencido = fuera de servicio.', 'MENSUAL', true, 'PROTECCION_RESPIRATORIA', 20),
  ('Máscara, arnés y válvulas', 'Limpieza, estanqueidad y estado de correas.', 'MENSUAL', false, 'PROTECCION_RESPIRATORIA', 30),
  ('Estado y vencimiento de EPP', 'Roturas, costuras, cintas reflectivas y fecha de fabricación.', 'MENSUAL', false, 'EPP', 10),
  ('Tubos de oxígeno: carga y vencimiento', 'Manómetro en verde y prueba hidráulica vigente.', 'MENSUAL', true, 'MEDICO', 10),
  ('Botiquín completo y medicamentos vigentes', null, 'MENSUAL', false, 'MEDICO', 20),
  ('Herramientas hidráulicas: prueba de funcionamiento', 'Arranque de central, mangueras y acoples sin pérdidas.', 'SEMANAL', true, 'RESCATE', 10),
  ('Motosierras / motobombas: arranque y combustible', null, 'SEMANAL', false, 'HERRAMIENTA', 10);

-- -----------------------------------------------------------------------------
-- Permisos
-- -----------------------------------------------------------------------------
grant select on public.vw_mantenimiento_tareas_item, public.vw_mantenimiento_items,
                public.vw_mantenimiento_historial, public.vw_mantenimiento_tareas to service_role;
revoke all on public.vw_mantenimiento_tareas_item, public.vw_mantenimiento_items,
              public.vw_mantenimiento_historial, public.vw_mantenimiento_tareas from anon, authenticated;

revoke all on function public.mantenimiento_get_estado_general(uuid) from public, anon, authenticated;
revoke all on function public.mantenimiento_aplicar_estado(uuid) from public, anon, authenticated;
revoke all on function public.mantenimiento_evaluar_todo() from public, anon, authenticated;
revoke all on function public.rpc_mantenimiento_registrar(integer, uuid, text, jsonb, text) from public, anon, authenticated;
revoke all on function public.rpc_mantenimiento_tarea_guardar(integer, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.rpc_mantenimiento_tarea_desactivar(integer, uuid) from public, anon, authenticated;
grant execute on function public.mantenimiento_get_estado_general(uuid) to service_role;
grant execute on function public.rpc_mantenimiento_registrar(integer, uuid, text, jsonb, text) to service_role;
grant execute on function public.rpc_mantenimiento_tarea_guardar(integer, uuid, jsonb) to service_role;
grant execute on function public.rpc_mantenimiento_tarea_desactivar(integer, uuid) to service_role;

-- Vencimientos aunque nadie use la app.
select cron.schedule('mantenimiento-evaluar', '*/15 * * * *', 'select public.mantenimiento_evaluar_todo()');

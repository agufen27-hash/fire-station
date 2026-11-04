-- =============================================================================
-- Libro de Guardia y Bitácora (app de escritorio).
--
-- * Vista libro_guardia_bitacora: une en una sola grilla los viajes
--   (viajes_activos) y las asistencias al cuartel (asistencias_eventos: GPS,
--   QR/token). Columnas comunes: bombero, actividad/destino, vehículo,
--   inicio (ingreso / salida), fin (egreso / regreso), minutos y estado:
--     EN_CURSO   -> viaje abierto, o asistencia sin egreso de las últimas 16 h
--     FINALIZADO -> con egreso / regreso
--     SIN_EGRESO -> asistencia que nunca se cerró (más de 16 h; el bot ya no la
--                   considera abierta)
-- * rpc_resumen_libro_guardia(): KPIs (viajes activos, personal en cuartel,
--   guardias del día).
-- * rpc_cerrar_viaje_manual(): el operador de guardia cierra un viaje con
--   fecha y hora completas (viajes multi-día), entre la salida y now().
-- =============================================================================

create or replace view public.libro_guardia_bitacora
with (security_invoker = true) as
select
  v.id,
  'VIAJE'::text                                   as tipo_registro,
  v.bombero_id,
  pc.legajo,
  public.nombre_legible(pc.nombre_completo)       as bombero,
  pc.jerarquia,
  'Viaje a ' || v.destino                         as actividad,
  v.destino,
  v.vehiculo,
  v.fecha_salida                                  as inicio,
  v.fecha_llegada                                 as fin,
  round(extract(epoch from (v.fecha_llegada - v.fecha_salida)) / 60)::integer as minutos,
  case when v.estado = 'EN_VIAJE' then 'EN_CURSO' else 'FINALIZADO' end as estado,
  v.origen::text                                  as metodo_registro
from public.viajes_activos v
join public.personal_cloud pc on pc.id = v.bombero_id
union all
select
  a.id,
  'ASISTENCIA'::text,
  a.personal_id,
  pc.legajo,
  public.nombre_legible(pc.nombre_completo),
  pc.jerarquia,
  case a.tipo_actividad
    when 'CAPACITACION'  then 'Capacitación'
    when 'MANTENIMIENTO' then 'Mantenimiento'
    when 'REUNION'       then 'Reunión'
    when 'ORDEN_INTERNO' then 'Orden interno'
    when 'GUARDIA'       then 'Guardia'
    else coalesce(a.tipo_actividad, 'Asistencia')
  end || coalesce(' — ' || e.descripcion, ''),
  null::text,
  null::text,
  a.fecha_hora_ingreso,
  a.fecha_hora_egreso,
  round(extract(epoch from (a.fecha_hora_egreso - a.fecha_hora_ingreso)) / 60)::integer,
  case
    when a.fecha_hora_egreso is not null then 'FINALIZADO'
    when a.fecha_hora_ingreso > now() - interval '16 hours' then 'EN_CURSO'
    else 'SIN_EGRESO'
  end,
  coalesce(a.metodo_registro, 'UBICACION')::text
from public.asistencias_eventos a
join public.personal_cloud pc on pc.id = a.personal_id
left join public.eventos_asistencia e on e.id = a.evento_id;

comment on view public.libro_guardia_bitacora is
  'Bitácora unificada (viajes + asistencias) para el Libro de Guardia del escritorio y la planilla mensual ART.';

create or replace function public.rpc_resumen_libro_guardia()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'viajes_activos',
      (select count(*) from public.viajes_activos where estado = 'EN_VIAJE'),
    'personal_en_cuartel',
      (select count(distinct personal_id) from public.asistencias_eventos
        where fecha_hora_egreso is null and fecha_hora_ingreso > now() - interval '16 hours'),
    'guardias_del_dia',
      (select count(*) from public.asistencias_eventos
        where tipo_actividad = 'GUARDIA'
          and (fecha_hora_ingreso at time zone 'America/Argentina/Cordoba')::date
              = (now() at time zone 'America/Argentina/Cordoba')::date),
    'actualizado_at', now()
  )
$$;

create or replace function public.rpc_cerrar_viaje_manual(p_viaje_id uuid, p_fecha_llegada timestamptz)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v record;
begin
  select id, destino, fecha_salida, estado into v
    from public.viajes_activos where id = p_viaje_id
    for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'VIAJE_INEXISTENTE');
  end if;
  if v.estado <> 'EN_VIAJE' then
    return jsonb_build_object('ok', false, 'motivo', 'VIAJE_YA_FINALIZADO');
  end if;
  if p_fecha_llegada is null then
    return jsonb_build_object('ok', false, 'motivo', 'FECHA_INVALIDA');
  end if;
  if p_fecha_llegada < v.fecha_salida then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_ANTERIOR_A_SALIDA', 'fecha_salida', v.fecha_salida);
  end if;
  if p_fecha_llegada > now() then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_EN_EL_FUTURO', 'ahora', now());
  end if;

  update public.viajes_activos
     set fecha_llegada = p_fecha_llegada, estado = 'FINALIZADO'
   where id = v.id;

  return jsonb_build_object('ok', true, 'viaje_id', v.id, 'destino', v.destino,
                            'fecha_salida', v.fecha_salida, 'fecha_llegada', p_fecha_llegada,
                            'minutos', round(extract(epoch from (p_fecha_llegada - v.fecha_salida)) / 60));
end
$$;

grant select on public.libro_guardia_bitacora to service_role;
revoke all on public.libro_guardia_bitacora from anon, authenticated;

revoke all on function public.rpc_resumen_libro_guardia() from public, anon, authenticated;
revoke all on function public.rpc_cerrar_viaje_manual(uuid, timestamptz) from public, anon, authenticated;
grant execute on function public.rpc_resumen_libro_guardia() to service_role;
grant execute on function public.rpc_cerrar_viaje_manual(uuid, timestamptz) to service_role;

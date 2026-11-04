-- =============================================================================
-- Viajes multi-día: la llegada ya NO tiene que caer el mismo día que la salida.
--
-- Reglas nuevas para fecha_llegada (valen para el bot, el escritorio y SQL):
--   * fecha_llegada >= fecha_salida      (CHECK viajes_llegada_posterior, ya existía)
--   * fecha_llegada <= now()             (trigger: no se registran llegadas futuras)
--
-- rpc_cerrar_viaje(p_bombero_id, 'HH:MM'): combina la hora con el día ACTUAL
-- (hora del cuartel) en que el bombero responde, y la acepta solo si cae dentro
-- de [fecha_salida, now()].
-- =============================================================================

alter table public.viajes_activos drop constraint viajes_llegada_mismo_dia;

comment on table public.viajes_activos is
  'Libro de guardia: salidas del personal. Viajes multi-día permitidos; '
  'la llegada debe estar entre la salida y el momento actual.';

-- now() no se usa en un CHECK (se reevaluaría en dumps/restores): va en un trigger.
create or replace function public.tg_viajes_llegada_no_futura()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.fecha_llegada is not null
     and (tg_op = 'INSERT' or new.fecha_llegada is distinct from old.fecha_llegada)
     and new.fecha_llegada > now() then
    raise exception 'La fecha de llegada (%) no puede ser posterior al momento actual', new.fecha_llegada
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;

create trigger viajes_activos_llegada_no_futura
  before insert or update of fecha_llegada on public.viajes_activos
  for each row execute function public.tg_viajes_llegada_no_futura();

-- Cierra el viaje abierto: p_hora 'HH:MM' (hora del cuartel) + día actual.
create or replace function public.rpc_cerrar_viaje(p_bombero_id integer, p_hora text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  tz        constant text := 'America/Argentina/Cordoba';
  v         record;
  v_ahora   timestamptz := now();
  v_llegada timestamptz;
begin
  select id, destino, vehiculo, fecha_salida into v
    from public.viajes_activos
   where bombero_id = p_bombero_id and estado = 'EN_VIAJE'
   for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_VIAJE_ABIERTO');
  end if;

  if coalesce(p_hora, '') !~ '^([01]?[0-9]|2[0-3]):[0-5][0-9]$' then
    return jsonb_build_object('ok', false, 'motivo', 'HORA_INVALIDA');
  end if;

  -- Día en que se responde (hora local del cuartel) + hora indicada.
  v_llegada := (((v_ahora at time zone tz)::date + p_hora::time)::timestamp) at time zone tz;

  if v_llegada < v.fecha_salida then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_ANTERIOR_A_SALIDA',
                              'destino', v.destino, 'fecha_salida', v.fecha_salida,
                              'llegada_propuesta', v_llegada);
  end if;
  if v_llegada > v_ahora then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_EN_EL_FUTURO',
                              'destino', v.destino, 'fecha_salida', v.fecha_salida,
                              'llegada_propuesta', v_llegada, 'ahora', v_ahora);
  end if;

  update public.viajes_activos
     set fecha_llegada = v_llegada, estado = 'FINALIZADO'
   where id = v.id;

  return jsonb_build_object('ok', true, 'viaje_id', v.id, 'destino', v.destino,
                            'vehiculo', v.vehiculo, 'fecha_salida', v.fecha_salida,
                            'fecha_llegada', v_llegada,
                            'minutos', round(extract(epoch from (v_llegada - v.fecha_salida)) / 60));
end
$$;

revoke all on function public.rpc_cerrar_viaje(integer, text) from public, anon, authenticated;
grant execute on function public.rpc_cerrar_viaje(integer, text) to service_role;

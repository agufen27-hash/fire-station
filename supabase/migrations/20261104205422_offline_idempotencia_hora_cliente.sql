-- =============================================================================
-- Modo offline del escritorio: escrituras que se sincronizan más tarde.
--
-- Una operación hecha sin red se guarda en la cola local de la app y se envía
-- al volver la conexión. Para que eso sea correcto:
--   * HORA REAL: la app manda la hora en que el operador la hizo
--     (p_fecha_salida / p_fecha_hora). Se acepta entre 7 días atrás y ahora
--     (+5 min de tolerancia de reloj); si no viene, now() como antes (bot).
--   * IDEMPOTENCIA: cada operación lleva un p_cliente_id (uuid generado en la
--     PC). Si un reintento llega después de que la primera vez SÍ se aplicó
--     (respuesta perdida por un corte), se devuelve el resultado original en
--     vez de duplicar el viaje o el checklist.
-- Las firmas cambian (parámetros nuevos con default): se recrean las RPC para
-- que PostgREST no vea dos sobrecargas ambiguas. El bot no se ve afectado.
-- =============================================================================

alter table public.viajes_activos add column cliente_id uuid unique;
alter table public.mantenimiento_registros add column cliente_id uuid unique;

create or replace function public.fecha_cliente_valida(p_fecha timestamptz)
returns boolean
language sql
stable
as $$
  select p_fecha is null or (p_fecha <= now() + interval '5 minutes' and p_fecha >= now() - interval '7 days')
$$;

-- -----------------------------------------------------------------------------
-- rpc_registrar_viaje (+ p_fecha_salida, p_cliente_id)
-- -----------------------------------------------------------------------------
drop function public.rpc_registrar_viaje(integer, text, text, text);
create function public.rpc_registrar_viaje(
  p_bombero_id integer, p_destino text, p_vehiculo text, p_origen text default 'BOT',
  p_fecha_salida timestamptz default null, p_cliente_id uuid default null)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_abierto record;
  v_previo  record;
  v_id      uuid;
  v_salida  timestamptz;
begin
  if p_cliente_id is not null then
    select id, destino, vehiculo, fecha_salida into v_previo from public.viajes_activos where cliente_id = p_cliente_id;
    if found then
      return jsonb_build_object('ok', true, 'viaje_id', v_previo.id, 'destino', v_previo.destino,
                                'vehiculo', v_previo.vehiculo, 'fecha_salida', v_previo.fecha_salida,
                                'ya_aplicado', true);
    end if;
  end if;
  if not exists (select 1 from public.personal_cloud where id = p_bombero_id and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'BOMBERO_NO_REGISTRADO_O_INACTIVO');
  end if;
  if length(trim(coalesce(p_destino, ''))) = 0 or length(trim(coalesce(p_vehiculo, ''))) = 0 then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INCOMPLETOS');
  end if;
  if not public.fecha_cliente_valida(p_fecha_salida) then
    return jsonb_build_object('ok', false, 'motivo', 'FECHA_FUERA_DE_RANGO');
  end if;

  select destino, vehiculo, fecha_salida into v_abierto
    from public.viajes_activos where bombero_id = p_bombero_id and estado = 'EN_VIAJE';
  if found then
    return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_VIAJE_ABIERTO',
                              'destino', v_abierto.destino, 'fecha_salida', v_abierto.fecha_salida);
  end if;

  insert into public.viajes_activos (bombero_id, destino, vehiculo, origen, fecha_salida, cliente_id)
  values (p_bombero_id, trim(p_destino), trim(p_vehiculo), coalesce(p_origen, 'BOT'),
          least(coalesce(p_fecha_salida, now()), now()), p_cliente_id)
  returning id, fecha_salida into v_id, v_salida;

  return jsonb_build_object('ok', true, 'viaje_id', v_id, 'destino', trim(p_destino),
                            'vehiculo', trim(p_vehiculo), 'fecha_salida', v_salida);
exception when unique_violation then
  return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_VIAJE_ABIERTO');
end
$$;
revoke all on function public.rpc_registrar_viaje(integer, text, text, text, timestamptz, uuid) from public, anon, authenticated;
grant execute on function public.rpc_registrar_viaje(integer, text, text, text, timestamptz, uuid) to service_role;

-- -----------------------------------------------------------------------------
-- rpc_cerrar_viaje_manual: reintento de un cierre ya aplicado = éxito
-- -----------------------------------------------------------------------------
create or replace function public.rpc_cerrar_viaje_manual(p_viaje_id uuid, p_fecha_llegada timestamptz)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v record;
begin
  select id, destino, fecha_salida, fecha_llegada, estado into v
    from public.viajes_activos where id = p_viaje_id
    for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'VIAJE_INEXISTENTE');
  end if;
  if v.estado <> 'EN_VIAJE' then
    if v.fecha_llegada = p_fecha_llegada then   -- el mismo cierre, reenviado
      return jsonb_build_object('ok', true, 'viaje_id', v.id, 'destino', v.destino, 'fecha_salida', v.fecha_salida,
                                'fecha_llegada', v.fecha_llegada, 'ya_aplicado', true,
                                'minutos', round(extract(epoch from (v.fecha_llegada - v.fecha_salida)) / 60));
    end if;
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

-- -----------------------------------------------------------------------------
-- rpc_mantenimiento_registrar (+ p_fecha_hora, p_cliente_id)
-- -----------------------------------------------------------------------------
drop function public.rpc_mantenimiento_registrar(integer, uuid, text, jsonb, text);
create function public.rpc_mantenimiento_registrar(
  p_operador_id integer, p_item_id uuid, p_frecuencia text, p_respuestas jsonb, p_observaciones text,
  p_fecha_hora timestamptz default null, p_cliente_id uuid default null)
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
  v_previo     record;
begin
  if p_cliente_id is not null then
    select id, resultado into v_previo from public.mantenimiento_registros where cliente_id = p_cliente_id;
    if found then
      v_aplicado := public.mantenimiento_aplicar_estado(p_item_id);
      return jsonb_build_object(
        'ok', true, 'registro_id', v_previo.id, 'resultado', v_previo.resultado, 'ya_aplicado', true,
        'estado_general', v_aplicado->'evaluacion'->>'estado_general',
        'estado_item', v_aplicado->>'estado_nuevo', 'cambio_estado', false,
        'motivos', v_aplicado->'evaluacion'->'motivos');
    end if;
  end if;
  if p_operador_id is null then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_OPERADOR');
  end if;
  if not exists (select 1 from public.personal_cloud where id = p_operador_id and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'OPERADOR_DESCONOCIDO');
  end if;
  if p_frecuencia not in ('DIARIA', 'SEMANAL', 'MENSUAL') then
    return jsonb_build_object('ok', false, 'motivo', 'FRECUENCIA_INVALIDA');
  end if;
  if not public.fecha_cliente_valida(p_fecha_hora) then
    return jsonb_build_object('ok', false, 'motivo', 'FECHA_FUERA_DE_RANGO');
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

  insert into public.mantenimiento_registros
    (item_id, frecuencia, responsable_id, resultado, observaciones, fecha_hora, cliente_id)
  values (p_item_id, p_frecuencia, p_operador_id, v_resultado, nullif(trim(coalesce(p_observaciones, '')), ''),
          least(coalesce(p_fecha_hora, clock_timestamp()), clock_timestamp()), p_cliente_id)
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
revoke all on function public.rpc_mantenimiento_registrar(integer, uuid, text, jsonb, text, timestamptz, uuid)
  from public, anon, authenticated;
grant execute on function public.rpc_mantenimiento_registrar(integer, uuid, text, jsonb, text, timestamptz, uuid)
  to service_role;
revoke all on function public.fecha_cliente_valida(timestamptz) from public, anon, authenticated;

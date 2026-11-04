-- =============================================================================
-- Libro de Guardia: viajes activos, salidas de emergencia y cola de
-- notificaciones salientes (WhatsApp + pasarela externa Alerthor).
--
-- Flujo:
--   INSERT viajes_activos / salidas_emergencia (escritorio, bot o SQL)
--     -> trigger -> notificar_canales_externos(evento, id)
--          -> encola filas en notificaciones_salientes (1 por destino WhatsApp
--             + 1 para ALERTHOR)
--          -> invocar_despachador(): pg_net llama a la Edge Function
--             `despachador-notificaciones`, que envía todo EN PARALELO
--             (WhatsApp por Graph API y Alerthor con ALERTHOR_API_URL/ALERTHOR_TOKEN).
--   pg_cron:
--     * watchdog-viajes-24h (cada 10 min): viajes EN_VIAJE con más de 24 h y sin
--       recordatorio -> mensaje privado al bombero (RECORDATORIO_VIAJE).
--     * despachador-reintentos (cada 5 min): reintenta envíos en ERROR.
--
-- Regla del día de llegada: la llegada debe caer el MISMO día (hora del cuartel)
-- que la salida. Lo garantiza un CHECK, así que vale para cualquier origen
-- (bot, escritorio o SQL); además un trigger impide mover el día de salida.
-- =============================================================================

create extension if not exists pg_net with schema extensions;
create extension if not exists pg_cron;

-- -----------------------------------------------------------------------------
-- Utilidades
-- -----------------------------------------------------------------------------
-- "FENOGLIO, Agustín" -> "Agustín Fenoglio"
create or replace function public.nombre_legible(p_nombre text)
returns text
language sql
immutable
set search_path = public
as $$
  select case
    when position(',' in coalesce(p_nombre, '')) > 0 then
      trim(initcap(lower(trim(split_part(p_nombre, ',', 2)))) || ' ' ||
           initcap(lower(trim(split_part(p_nombre, ',', 1)))))
    else initcap(lower(trim(coalesce(p_nombre, ''))))
  end
$$;

create table public.config_sistema (
  clave       varchar(60) primary key,
  valor       text not null,
  descripcion text,
  updated_at  timestamptz not null default now()
);
insert into public.config_sistema (clave, valor, descripcion) values
  ('despachador_url',
   'https://uzwegoetekpdommmrnrv.supabase.co/functions/v1/despachador-notificaciones',
   'Edge Function que envía la cola notificaciones_salientes (WhatsApp + Alerthor)'),
  ('zona_horaria', 'America/Argentina/Cordoba', 'Zona horaria del cuartel');

-- -----------------------------------------------------------------------------
-- Viajes activos (libro de guardia)
-- -----------------------------------------------------------------------------
create table public.viajes_activos (
  id                      uuid primary key default gen_random_uuid(),
  bombero_id              integer not null references public.personal_cloud(id),
  destino                 text not null check (length(trim(destino)) between 1 and 120),
  vehiculo                text not null check (length(trim(vehiculo)) between 1 and 60),
  fecha_salida            timestamptz not null default now(),
  fecha_llegada           timestamptz,
  estado                  varchar(12) not null default 'EN_VIAJE'
                          check (estado in ('EN_VIAJE', 'FINALIZADO')),
  notificacion_enviada_at timestamptz,          -- watchdog 24 h: recordatorio ya enviado
  origen                  varchar(12) not null default 'ESCRITORIO'
                          check (origen in ('ESCRITORIO', 'BOT', 'SQL')),
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  constraint viajes_estado_coherente
    check ((estado = 'EN_VIAJE') = (fecha_llegada is null)),
  constraint viajes_llegada_posterior
    check (fecha_llegada is null or fecha_llegada >= fecha_salida),
  constraint viajes_llegada_mismo_dia
    check (fecha_llegada is null or
           (fecha_llegada at time zone 'America/Argentina/Cordoba')::date =
           (fecha_salida  at time zone 'America/Argentina/Cordoba')::date)
);
comment on table public.viajes_activos is
  'Libro de guardia: salidas del personal. La llegada queda anclada al día de salida.';

-- Un solo viaje abierto por bombero.
create unique index viajes_un_abierto_por_bombero
  on public.viajes_activos (bombero_id) where estado = 'EN_VIAJE';
create index viajes_watchdog_idx
  on public.viajes_activos (fecha_salida)
  where estado = 'EN_VIAJE' and notificacion_enviada_at is null;

create trigger viajes_activos_updated_at before update on public.viajes_activos
  for each row execute function public.tg_set_updated_at();

-- Bloqueo rígido del día: la hora de salida se puede corregir, el día no.
create or replace function public.tg_viajes_bloquear_dia()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.fecha_salida is distinct from old.fecha_salida
     and (new.fecha_salida at time zone 'America/Argentina/Cordoba')::date
         <> (old.fecha_salida at time zone 'America/Argentina/Cordoba')::date then
    raise exception 'No se puede modificar el día de salida de un viaje (solo la hora)'
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
create trigger viajes_activos_bloquear_dia before update on public.viajes_activos
  for each row execute function public.tg_viajes_bloquear_dia();

-- -----------------------------------------------------------------------------
-- Salidas de emergencia (siniestros)
-- -----------------------------------------------------------------------------
create table public.salidas_emergencia (
  id                 uuid primary key default gen_random_uuid(),
  tipo_incendio      text not null check (length(trim(tipo_incendio)) between 1 and 80),
  direccion          text not null check (length(trim(direccion)) between 1 and 200),
  comunicado_por_id  integer references public.personal_cloud(id),
  comunicado_por     text,                      -- nombre libre si no está en el padrón
  fecha_salida       timestamptz not null default now(),
  observaciones      text,
  origen             varchar(12) not null default 'ESCRITORIO'
                     check (origen in ('ESCRITORIO', 'BOT', 'SQL')),
  created_at         timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Destinos y cola de notificaciones
-- -----------------------------------------------------------------------------
create table public.destinos_notificacion (
  id          serial primary key,
  grupo       varchar(20) not null check (grupo in ('OPERATIVO', 'EMERGENCIAS')),
  tipo        varchar(8)  not null default 'NUMERO' check (tipo in ('NUMERO', 'GRUPO')),
  destino     varchar(64) not null,              -- 549XXXXXXXXXX o ID de grupo de WhatsApp
  descripcion text,
  activo      boolean not null default true,
  unique (grupo, destino)
);
comment on table public.destinos_notificacion is
  'OPERATIVO recibe "Saliendo hacia..."; EMERGENCIAS recibe "Salida, Incendio de...". '
  'tipo NUMERO = envío individual; GRUPO = ID de grupo (Groups API de WhatsApp Cloud).';

create table public.notificaciones_salientes (
  id            uuid primary key default gen_random_uuid(),
  evento        varchar(24) not null
                check (evento in ('VIAJE_SALIDA', 'SINIESTRO', 'RECORDATORIO_VIAJE')),
  referencia_id uuid not null,                  -- viajes_activos.id / salidas_emergencia.id
  canal         varchar(10) not null check (canal in ('WHATSAPP', 'ALERTHOR')),
  destino_tipo  varchar(8) check (destino_tipo in ('NUMERO', 'GRUPO')),
  destino       varchar(64),
  mensaje       text not null,
  payload       jsonb not null default '{}'::jsonb,
  estado        varchar(10) not null default 'PENDIENTE'
                check (estado in ('PENDIENTE', 'PROCESANDO', 'ENVIADO', 'ERROR', 'OMITIDO')),
  intentos      smallint not null default 0,
  ultimo_error  text,
  respuesta     jsonb,
  created_at    timestamptz not null default now(),
  tomado_at     timestamptz,
  enviado_at    timestamptz
);
create index notificaciones_pendientes_idx
  on public.notificaciones_salientes (created_at)
  where estado in ('PENDIENTE', 'PROCESANDO', 'ERROR');
create index notificaciones_referencia_idx on public.notificaciones_salientes (referencia_id);

-- -----------------------------------------------------------------------------
-- Disparo de la Edge Function despachadora (pg_net, asíncrono: sale al commit)
-- -----------------------------------------------------------------------------
create or replace function public.invocar_despachador()
returns bigint
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_url text;
begin
  if not exists (
    select 1 from public.notificaciones_salientes
     where estado = 'PENDIENTE'
        or (estado = 'ERROR' and intentos < 5 and created_at > now() - interval '6 hours')
        or (estado = 'PROCESANDO' and tomado_at < now() - interval '5 minutes')
  ) then
    return null;
  end if;
  select valor into v_url from public.config_sistema where clave = 'despachador_url';
  if v_url is null then
    return null;
  end if;
  return net.http_post(
    url                  := v_url,
    body                 := '{}'::jsonb,
    headers              := jsonb_build_object('Content-Type', 'application/json'),
    timeout_milliseconds := 15000
  );
end
$$;

-- -----------------------------------------------------------------------------
-- notificar_canales_externos: punto único de despacho de eventos.
-- Arma el texto exacto de WhatsApp, el JSON para Alerthor y los encola.
-- -----------------------------------------------------------------------------
create or replace function public.notificar_canales_externos(p_evento text, p_referencia_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_msg        text;
  v_grupo      text;
  v_privado    text;
  v_datos      jsonb;
  v_parametros jsonb;
  v_payload    jsonb;
  v_nombre     text;
  n_wa         integer := 0;
  r            record;
begin
  if p_evento = 'VIAJE_SALIDA' then
    select va.*, pc.nombre_completo into r
      from public.viajes_activos va
      join public.personal_cloud pc on pc.id = va.bombero_id
     where va.id = p_referencia_id;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'REFERENCIA_INEXISTENTE');
    end if;
    v_nombre := public.nombre_legible(r.nombre_completo);
    v_msg := format('Bombero %s Saliendo hacia %s en %s', v_nombre, trim(r.destino), trim(r.vehiculo));
    v_grupo := 'OPERATIVO';
    v_parametros := jsonb_build_array(v_nombre, trim(r.destino), trim(r.vehiculo));
    v_datos := jsonb_build_object('viaje_id', r.id, 'bombero_id', r.bombero_id, 'bombero', v_nombre,
                                  'destino', r.destino, 'vehiculo', r.vehiculo,
                                  'fecha_salida', r.fecha_salida, 'origen', r.origen);

  elsif p_evento = 'SINIESTRO' then
    select se.*, pc.nombre_completo into r
      from public.salidas_emergencia se
      left join public.personal_cloud pc on pc.id = se.comunicado_por_id
     where se.id = p_referencia_id;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'REFERENCIA_INEXISTENTE');
    end if;
    v_nombre := coalesce(nullif(public.nombre_legible(r.nombre_completo), ''),
                         nullif(trim(r.comunicado_por), ''), 'Guardia');
    v_msg := format('Salida, Incendio de %s, %s, comunico %s', trim(r.tipo_incendio), trim(r.direccion), v_nombre);
    v_grupo := 'EMERGENCIAS';
    v_parametros := jsonb_build_array(trim(r.tipo_incendio), trim(r.direccion), v_nombre);
    v_datos := jsonb_build_object('salida_id', r.id, 'tipo_incendio', r.tipo_incendio,
                                  'direccion', r.direccion, 'comunicado_por', v_nombre,
                                  'comunicado_por_id', r.comunicado_por_id,
                                  'fecha_salida', r.fecha_salida, 'origen', r.origen);

  elsif p_evento = 'RECORDATORIO_VIAJE' then
    select va.*, pc.nombre_completo, pc.telefono_whatsapp into r
      from public.viajes_activos va
      join public.personal_cloud pc on pc.id = va.bombero_id
     where va.id = p_referencia_id;
    if not found then
      return jsonb_build_object('ok', false, 'motivo', 'REFERENCIA_INEXISTENTE');
    end if;
    v_nombre := public.nombre_legible(r.nombre_completo);
    v_msg := format('Hola %s, figura un viaje abierto hacia %s desde hace 24 horas. ¿Quiere marcar su regreso?',
                    v_nombre, trim(r.destino));
    v_privado := coalesce(r.telefono_whatsapp, '');
    v_parametros := jsonb_build_array(v_nombre, trim(r.destino));
    v_datos := jsonb_build_object('viaje_id', r.id, 'bombero_id', r.bombero_id, 'destino', r.destino,
                                  'fecha_salida', r.fecha_salida);
  else
    return jsonb_build_object('ok', false, 'motivo', 'EVENTO_DESCONOCIDO');
  end if;

  v_payload := jsonb_build_object('evento', p_evento, 'referencia_id', p_referencia_id,
                                  'mensaje', v_msg, 'parametros', v_parametros,
                                  'datos', v_datos, 'emitido_at', now());

  -- WhatsApp
  if v_privado is not null then
    insert into public.notificaciones_salientes
      (evento, referencia_id, canal, destino_tipo, destino, mensaje, payload, estado, ultimo_error)
    values
      (p_evento, p_referencia_id, 'WHATSAPP', 'NUMERO', nullif(v_privado, ''), v_msg, v_payload,
       case when v_privado = '' then 'OMITIDO' else 'PENDIENTE' end,
       case when v_privado = '' then 'El bombero no tiene telefono_whatsapp cargado' end);
    n_wa := case when v_privado = '' then 0 else 1 end;
  else
    insert into public.notificaciones_salientes
      (evento, referencia_id, canal, destino_tipo, destino, mensaje, payload)
    select p_evento, p_referencia_id, 'WHATSAPP', d.tipo, d.destino, v_msg, v_payload
      from public.destinos_notificacion d
     where d.grupo = v_grupo and d.activo;
    get diagnostics n_wa = row_count;
    if n_wa = 0 then
      insert into public.notificaciones_salientes
        (evento, referencia_id, canal, mensaje, payload, estado, ultimo_error)
      values
        (p_evento, p_referencia_id, 'WHATSAPP', v_msg, v_payload, 'OMITIDO',
         format('Sin destinos activos para el grupo %s en destinos_notificacion', v_grupo));
    end if;
  end if;

  -- Alerthor (pasarela externa): se encola siempre para eventos operativos; si
  -- ALERTHOR_API_URL no está configurado, el despachador la marca OMITIDO.
  if p_evento in ('VIAJE_SALIDA', 'SINIESTRO') then
    insert into public.notificaciones_salientes
      (evento, referencia_id, canal, mensaje, payload)
    values
      (p_evento, p_referencia_id, 'ALERTHOR', v_msg, v_payload);
  end if;

  perform public.invocar_despachador();
  return jsonb_build_object('ok', true, 'mensaje', v_msg, 'whatsapp', n_wa);
end
$$;

-- Triggers de alta: un fallo de notificación nunca bloquea el registro.
create or replace function public.tg_notificar_alta()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    perform public.notificar_canales_externos(tg_argv[0], new.id);
  exception when others then
    raise warning 'notificar_canales_externos(%, %) falló: %', tg_argv[0], new.id, sqlerrm;
  end;
  return null;
end
$$;

create trigger viajes_activos_notificar after insert on public.viajes_activos
  for each row execute function public.tg_notificar_alta('VIAJE_SALIDA');
create trigger salidas_emergencia_notificar after insert on public.salidas_emergencia
  for each row execute function public.tg_notificar_alta('SINIESTRO');

-- -----------------------------------------------------------------------------
-- Cola: toma de lote para el despachador (concurrencia segura)
-- -----------------------------------------------------------------------------
create or replace function public.rpc_tomar_notificaciones(p_limite integer default 25)
returns setof public.notificaciones_salientes
language sql
security definer
set search_path = public
as $$
  update public.notificaciones_salientes n
     set estado = 'PROCESANDO', intentos = n.intentos + 1, tomado_at = now()
   where n.id in (
     select id from public.notificaciones_salientes
      where estado = 'PENDIENTE'
         or (estado = 'ERROR' and intentos < 5 and created_at > now() - interval '6 hours'
             and tomado_at < now() - interval '2 minutes')
         or (estado = 'PROCESANDO' and intentos < 5 and tomado_at < now() - interval '5 minutes')
      order by created_at
      limit greatest(1, least(p_limite, 100))
      for update skip locked)
  returning n.*;
$$;

-- -----------------------------------------------------------------------------
-- Watchdog 24 h
-- -----------------------------------------------------------------------------
create or replace function public.watchdog_viajes_24h()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  r record;
  n integer := 0;
begin
  for r in
    select id from public.viajes_activos
     where estado = 'EN_VIAJE'
       and notificacion_enviada_at is null
       and fecha_salida <= now() - interval '24 hours'
     for update skip locked
  loop
    update public.viajes_activos set notificacion_enviada_at = now() where id = r.id;
    perform public.notificar_canales_externos('RECORDATORIO_VIAJE', r.id);
    n := n + 1;
  end loop;
  return n;
end
$$;

-- -----------------------------------------------------------------------------
-- RPC del bot / escritorio
-- -----------------------------------------------------------------------------
create or replace function public.rpc_registrar_viaje(
  p_bombero_id integer, p_destino text, p_vehiculo text, p_origen text default 'BOT')
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_abierto record;
  v_id      uuid;
  v_salida  timestamptz;
begin
  if not exists (select 1 from public.personal_cloud where id = p_bombero_id and activo) then
    return jsonb_build_object('ok', false, 'motivo', 'BOMBERO_NO_REGISTRADO_O_INACTIVO');
  end if;
  if length(trim(coalesce(p_destino, ''))) = 0 or length(trim(coalesce(p_vehiculo, ''))) = 0 then
    return jsonb_build_object('ok', false, 'motivo', 'DATOS_INCOMPLETOS');
  end if;

  select destino, vehiculo, fecha_salida into v_abierto
    from public.viajes_activos where bombero_id = p_bombero_id and estado = 'EN_VIAJE';
  if found then
    return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_VIAJE_ABIERTO',
                              'destino', v_abierto.destino, 'fecha_salida', v_abierto.fecha_salida);
  end if;

  insert into public.viajes_activos (bombero_id, destino, vehiculo, origen)
  values (p_bombero_id, trim(p_destino), trim(p_vehiculo), coalesce(p_origen, 'BOT'))
  returning id, fecha_salida into v_id, v_salida;

  return jsonb_build_object('ok', true, 'viaje_id', v_id, 'destino', trim(p_destino),
                            'vehiculo', trim(p_vehiculo), 'fecha_salida', v_salida);
exception when unique_violation then
  return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_VIAJE_ABIERTO');
end
$$;

-- Cierra el viaje abierto: p_hora 'HH:MM' (hora del cuartel). El DÍA no se elige:
-- se toma siempre el de la salida.
create or replace function public.rpc_cerrar_viaje(p_bombero_id integer, p_hora text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  tz        constant text := 'America/Argentina/Cordoba';
  v         record;
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

  v_llegada := (((v.fecha_salida at time zone tz)::date + p_hora::time)::timestamp) at time zone tz;

  if v_llegada < v.fecha_salida then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_ANTERIOR_A_SALIDA',
                              'destino', v.destino, 'fecha_salida', v.fecha_salida);
  end if;
  if v_llegada > now() + interval '5 minutes' then
    return jsonb_build_object('ok', false, 'motivo', 'LLEGADA_EN_EL_FUTURO',
                              'destino', v.destino, 'fecha_salida', v.fecha_salida);
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

-- -----------------------------------------------------------------------------
-- Permisos: solo service_role (Edge Functions / sync del escritorio)
-- -----------------------------------------------------------------------------
grant select, insert, update, delete on
  public.config_sistema, public.viajes_activos, public.salidas_emergencia,
  public.destinos_notificacion, public.notificaciones_salientes
to service_role;
grant usage, select on sequence public.destinos_notificacion_id_seq to service_role;

alter table public.config_sistema          enable row level security;
alter table public.viajes_activos          enable row level security;
alter table public.salidas_emergencia      enable row level security;
alter table public.destinos_notificacion   enable row level security;
alter table public.notificaciones_salientes enable row level security;

revoke all on
  public.config_sistema, public.viajes_activos, public.salidas_emergencia,
  public.destinos_notificacion, public.notificaciones_salientes
from anon, authenticated;

revoke all on function public.invocar_despachador() from public, anon, authenticated;
revoke all on function public.notificar_canales_externos(text, uuid) from public, anon, authenticated;
revoke all on function public.rpc_tomar_notificaciones(integer) from public, anon, authenticated;
revoke all on function public.watchdog_viajes_24h() from public, anon, authenticated;
revoke all on function public.rpc_registrar_viaje(integer, text, text, text) from public, anon, authenticated;
revoke all on function public.rpc_cerrar_viaje(integer, text) from public, anon, authenticated;
revoke all on function public.tg_notificar_alta() from public, anon, authenticated;

grant execute on function public.notificar_canales_externos(text, uuid) to service_role;
grant execute on function public.rpc_tomar_notificaciones(integer) to service_role;
grant execute on function public.watchdog_viajes_24h() to service_role;
grant execute on function public.rpc_registrar_viaje(integer, text, text, text) to service_role;
grant execute on function public.rpc_cerrar_viaje(integer, text) to service_role;

-- -----------------------------------------------------------------------------
-- Tareas programadas (pg_cron)
-- -----------------------------------------------------------------------------
select cron.schedule('watchdog-viajes-24h', '*/10 * * * *', 'select public.watchdog_viajes_24h()');
select cron.schedule('despachador-reintentos', '*/5 * * * *', 'select public.invocar_despachador()');

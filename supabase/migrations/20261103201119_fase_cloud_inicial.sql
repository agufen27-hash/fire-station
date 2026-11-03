-- =============================================================================
-- Fire Station · Fase Cloud inicial (Supabase / PostgreSQL + PostGIS)
--
-- Bot de WhatsApp híbrido (ubicación en vivo + botones interactivos),
-- geofencing de asistencias, parque automotor / libro de guardia y vistas
-- para Power BI.
--
-- Espejo de la base local:
--   * personal_cloud.id espeja el id nativo de SQLite (personal.id). legajo es
--     texto opcional ('59-027'; 21 de 49 no lo tienen) e id_ruba es el entero
--     de RUBA. Todas las FKs apuntan a personal_cloud.id (columnas *_id).
--   * telefono_whatsapp sin CHECK de formato: la sync en Python lo normaliza
--     a E.164 ('+549358...') antes de subirlo.
--   * Minutos y KMs NO se guardan como columnas generadas: se calculan en las
--     vistas vw_powerbi_horas_personal y vw_estado_flota_resumen.
--
-- Seguridad: todas las tablas con RLS activado y SIN políticas (solo el
-- service_role del backend del bot las ve); RPCs SECURITY DEFINER ejecutables
-- solo por service_role; vistas security_invoker sin acceso para anon /
-- authenticated. Power BI debe conectarse con un rol dedicado (a crear).
-- =============================================================================

create extension if not exists postgis with schema extensions;

-- -----------------------------------------------------------------------------
-- Utilidad: updated_at automático
-- -----------------------------------------------------------------------------
create or replace function public.tg_set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- Geocercas (cuartel, predio de capacitación, etc.)
-- -----------------------------------------------------------------------------
create table public.zonas_geocerca (
  id                       uuid primary key default gen_random_uuid(),
  nombre                   varchar(120) not null,
  ubicacion                extensions.geography(point, 4326) not null,
  radio_tolerancia_metros  int not null default 120 check (radio_tolerancia_metros between 10 and 5000),
  activo                   boolean not null default true
);
create index zonas_geocerca_ubicacion_gix on public.zonas_geocerca using gist (ubicacion);

-- -----------------------------------------------------------------------------
-- Espejo del personal (fuente: app de escritorio, id = personal.id de SQLite)
-- -----------------------------------------------------------------------------
create table public.personal_cloud (
  id                 integer primary key,
  legajo             varchar(20),
  id_ruba            integer,
  nombre_completo    varchar(160) not null,
  jerarquia          varchar(60),
  telefono_whatsapp  varchar(16) unique,          -- normalizado a E.164 en Python
  rol                varchar(20) not null default 'BOMBERO'
                     check (rol in ('BOMBERO', 'JEFATURA', 'ADMIN')),
  activo             boolean not null default true,
  updated_at         timestamptz not null default now()
);
create trigger personal_cloud_updated_at before update on public.personal_cloud
  for each row execute function public.tg_set_updated_at();

-- -----------------------------------------------------------------------------
-- Estado conversacional del bot (una sesión pendiente por teléfono)
-- -----------------------------------------------------------------------------
create table public.bot_sesiones_pendientes (
  telefono_whatsapp  varchar(16) primary key,
  personal_id        integer not null references public.personal_cloud (id) on delete cascade,
  accion_pendiente   varchar(40) not null
                     check (accion_pendiente in ('SELECCIONAR_ACTIVIDAD_INGRESO',
                                                 'CONFIRMAR_TALLER',
                                                 'CONFIRMAR_SALIDA_MOVIL')),
  payload_temporal   jsonb not null default '{}'::jsonb,
  expires_at         timestamptz not null default (now() + interval '15 minutes'),
  updated_at         timestamptz not null default now()
);
create index bot_sesiones_expires_idx on public.bot_sesiones_pendientes (expires_at);
create index bot_sesiones_personal_idx on public.bot_sesiones_pendientes (personal_id);
create trigger bot_sesiones_updated_at before update on public.bot_sesiones_pendientes
  for each row execute function public.tg_set_updated_at();

-- -----------------------------------------------------------------------------
-- Asistencias por geofencing (minutos se calculan en la vista)
-- -----------------------------------------------------------------------------
create table public.asistencias_eventos (
  id                    uuid primary key default gen_random_uuid(),
  personal_id           integer not null references public.personal_cloud (id),
  zona_id               uuid references public.zonas_geocerca (id),
  tipo_actividad        varchar(20) not null
                        check (tipo_actividad in ('CAPACITACION', 'MANTENIMIENTO', 'REUNION',
                                                  'ORDEN_INTERNO', 'GUARDIA')),
  fecha_hora_ingreso    timestamptz not null default now(),
  fecha_hora_egreso     timestamptz,
  ingreso_ubicacion     extensions.geography(point, 4326),
  ingreso_distancia_m   double precision,
  egreso_ubicacion      extensions.geography(point, 4326),
  egreso_distancia_m    double precision,
  wa_message_id         varchar(128) unique,          -- mensaje de ubicación del ingreso
  wa_message_id_egreso  varchar(128) unique,          -- mensaje de ubicación del egreso
  metadata_wa           jsonb not null default '{}'::jsonb,
  sincronizado_local    boolean not null default false,
  constraint asistencia_egreso_posterior check (fecha_hora_egreso is null or fecha_hora_egreso >= fecha_hora_ingreso)
);
create index asistencias_abiertas_idx on public.asistencias_eventos (personal_id, fecha_hora_ingreso desc)
  where fecha_hora_egreso is null;
create index asistencias_personal_ingreso_idx on public.asistencias_eventos (personal_id, fecha_hora_ingreso);
create index asistencias_zona_idx on public.asistencias_eventos (zona_id);
create index asistencias_no_sincronizadas_idx on public.asistencias_eventos (id) where not sincronizado_local;

-- -----------------------------------------------------------------------------
-- Parque automotor y libro de guardia
-- -----------------------------------------------------------------------------
create table public.flota_moviles (
  movil_id               varchar(20) primary key,          -- p. ej. 'M-12'
  nombre_interno         varchar(80),
  tipo_unidad            varchar(80),
  estado_operativo       varchar(20) not null default 'EN_SERVICIO'
                         check (estado_operativo in ('EN_SERVICIO', 'CON_OBSERVACIONES',
                                                     'EN_TALLER_FS', 'EN_EMERGENCIA')),
  km_actual              int check (km_actual >= 0),
  taller_actual          text,
  motivo_fuera_servicio  text,
  ultimo_cambio_por_id   integer references public.personal_cloud (id),
  updated_at             timestamptz not null default now()
);
create index flota_ultimo_cambio_idx on public.flota_moviles (ultimo_cambio_por_id);
create trigger flota_moviles_updated_at before update on public.flota_moviles
  for each row execute function public.tg_set_updated_at();

create table public.novedades_taller_moviles (
  id                    uuid primary key default gen_random_uuid(),
  movil_id              varchar(20) not null references public.flota_moviles (movil_id),
  reportado_por_id      integer references public.personal_cloud (id),
  fecha_ingreso_taller  timestamptz not null default now(),
  fecha_alta_servicio   timestamptz,
  taller_mecanico       text,
  falla_reportada       text not null,
  trabajo_realizado     text,
  sincronizado_local    boolean not null default false,
  constraint taller_alta_posterior check (fecha_alta_servicio is null or fecha_alta_servicio >= fecha_ingreso_taller)
);
create index novedades_taller_movil_idx on public.novedades_taller_moviles (movil_id, fecha_ingreso_taller desc);
create index novedades_taller_reportado_idx on public.novedades_taller_moviles (reportado_por_id);

-- KMs recorridos se calculan en la vista (km_regreso - km_salida).
create table public.movimientos_moviles (
  id                       uuid primary key default gen_random_uuid(),
  movil_id                 varchar(20) not null references public.flota_moviles (movil_id),
  chofer_id                integer not null references public.personal_cloud (id),
  a_cargo_id               integer references public.personal_cloud (id),
  tipo_salida              varchar(40) not null,
  destino_detalle          text,
  km_salida                int not null check (km_salida >= 0),
  km_regreso               int,
  hora_salida              timestamptz not null default now(),
  hora_regreso             timestamptz,
  novedades_mecanicas      text,
  estado_movil_al_regreso  varchar(20)
                           check (estado_movil_al_regreso in ('EN_SERVICIO', 'CON_OBSERVACIONES',
                                                              'EN_TALLER_FS', 'EN_EMERGENCIA')),
  sincronizado_local       boolean not null default false,
  constraint movimiento_km_regreso check (km_regreso is null or km_regreso >= km_salida),
  constraint movimiento_hora_regreso check (hora_regreso is null or hora_regreso >= hora_salida)
);
create index movimientos_movil_salida_idx on public.movimientos_moviles (movil_id, hora_salida desc);
create index movimientos_chofer_idx on public.movimientos_moviles (chofer_id);
create index movimientos_a_cargo_idx on public.movimientos_moviles (a_cargo_id);

-- -----------------------------------------------------------------------------
-- RLS: sin políticas = solo service_role (backend del bot) accede vía API
-- -----------------------------------------------------------------------------
alter table public.zonas_geocerca            enable row level security;
alter table public.personal_cloud            enable row level security;
alter table public.bot_sesiones_pendientes   enable row level security;
alter table public.asistencias_eventos       enable row level security;
alter table public.flota_moviles             enable row level security;
alter table public.novedades_taller_moviles  enable row level security;
alter table public.movimientos_moviles       enable row level security;

-- =============================================================================
-- RPC 1: ubicación recibida por WhatsApp
--   - bombero activo por teléfono
--   - filtro antitrampa: una ubicación con 'name' / 'address' es un lugar
--     elegido de la lista, no la ubicación en vivo
--   - geocerca más cercana con ST_Distance (metros, geography)
--   - asistencia abierta en las últimas 16 h -> EGRESO directo
--   - si no -> sesión pendiente para que elija la actividad con un botón
-- =============================================================================
create or replace function public.rpc_procesar_ubicacion_wa(
  p_telefono   varchar,
  p_lat        double precision,
  p_lng        double precision,
  p_wa_msg_id  varchar,
  p_metadata   jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_personal_id  int;
  v_punto        geography;
  v_zona         record;
  v_abierta      record;
  v_minutos      int;
  v_botones      jsonb := '["CAPACITACION", "MANTENIMIENTO", "REUNION"]'::jsonb;
  v_meta         jsonb := coalesce(p_metadata, '{}'::jsonb);
begin
  if p_lat is null or p_lng is null or p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    return jsonb_build_object('ok', false, 'motivo', 'COORDENADAS_INVALIDAS');
  end if;

  -- FOR UPDATE serializa los mensajes simultáneos del mismo bombero.
  select pc.id into v_personal_id
    from personal_cloud pc
   where pc.telefono_whatsapp = p_telefono and pc.activo
   for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'BOMBERO_NO_REGISTRADO_O_INACTIVO');
  end if;

  if v_meta ? 'name' or v_meta ? 'address' then
    return jsonb_build_object('ok', false, 'motivo', 'UBICACION_NO_EN_VIVO',
                              'detalle', 'Enviá tu ubicación actual, no un lugar elegido de la lista.');
  end if;

  if p_wa_msg_id is not null and exists (
       select 1 from asistencias_eventos
        where wa_message_id = p_wa_msg_id or wa_message_id_egreso = p_wa_msg_id) then
    return jsonb_build_object('ok', false, 'motivo', 'MENSAJE_YA_PROCESADO');
  end if;

  v_punto := st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography;

  select z.id, z.nombre, z.radio_tolerancia_metros, st_distance(z.ubicacion, v_punto) as distancia
    into v_zona
    from zonas_geocerca z
   where z.activo
   order by st_distance(z.ubicacion, v_punto)
   limit 1;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_ZONAS_CONFIGURADAS');
  end if;
  if v_zona.distancia > v_zona.radio_tolerancia_metros then
    return jsonb_build_object('ok', false, 'motivo', 'FUERA_DE_ZONA',
                              'zona', v_zona.nombre,
                              'distancia_m', round(v_zona.distancia::numeric, 1),
                              'tolerancia_m', v_zona.radio_tolerancia_metros);
  end if;

  select a.id into v_abierta
    from asistencias_eventos a
   where a.personal_id = v_personal_id
     and a.fecha_hora_egreso is null
     and a.fecha_hora_ingreso >= now() - interval '16 hours'
   order by a.fecha_hora_ingreso desc
   limit 1
   for update;

  if found then
    update asistencias_eventos
       set fecha_hora_egreso    = now(),
           egreso_ubicacion     = v_punto,
           egreso_distancia_m   = v_zona.distancia,
           wa_message_id_egreso = p_wa_msg_id,
           metadata_wa          = metadata_wa || jsonb_build_object('egreso', v_meta)
     where id = v_abierta.id
     returning (floor(extract(epoch from (fecha_hora_egreso - fecha_hora_ingreso)) / 60))::int
          into v_minutos;
    delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
    return jsonb_build_object('ok', true, 'modo', 'EGRESO_DIRECTO',
                              'asistencia_id', v_abierta.id,
                              'zona', v_zona.nombre,
                              'minutos', v_minutos);
  end if;

  insert into bot_sesiones_pendientes as s
         (telefono_whatsapp, personal_id, accion_pendiente, payload_temporal, expires_at)
  values (p_telefono, v_personal_id, 'SELECCIONAR_ACTIVIDAD_INGRESO',
          jsonb_build_object('lat', p_lat, 'lng', p_lng,
                             'zona_id', v_zona.id, 'distancia_m', v_zona.distancia,
                             'wa_message_id', p_wa_msg_id, 'metadata', v_meta,
                             'validado_en', now(), 'botones', v_botones),
          now() + interval '15 minutes')
  on conflict (telefono_whatsapp) do update
     set personal_id      = excluded.personal_id,
         accion_pendiente = excluded.accion_pendiente,
         payload_temporal = excluded.payload_temporal,
         expires_at       = excluded.expires_at;

  return jsonb_build_object('ok', true, 'modo', 'PEDIR_BOTON_ACTIVIDAD',
                            'zona', v_zona.nombre,
                            'distancia_m', round(v_zona.distancia::numeric, 1),
                            'botones', v_botones);
end;
$$;

-- =============================================================================
-- RPC 2: botón interactivo presionado
--   SELECCIONAR_ACTIVIDAD_INGRESO -> INSERT de la asistencia con las
--   coordenadas validadas en la RPC 1 (hora de ingreso = la de la ubicación).
--   CONFIRMAR_TALLER / CONFIRMAR_SALIDA_MOVIL: reservadas, aún sin lógica.
-- =============================================================================
create or replace function public.rpc_confirmar_boton_wa(
  p_telefono  varchar,
  p_boton_id  varchar
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_ses     bot_sesiones_pendientes%rowtype;
  v_tipo    varchar := upper(btrim(coalesce(p_boton_id, '')));
  v_p       jsonb;
  v_id      uuid;
begin
  select * into v_ses
    from bot_sesiones_pendientes
   where telefono_whatsapp = p_telefono
   for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'SIN_SESION_PENDIENTE');
  end if;
  if v_ses.expires_at < now() then
    delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
    return jsonb_build_object('ok', false, 'motivo', 'SESION_EXPIRADA');
  end if;
  if not exists (select 1 from personal_cloud where id = v_ses.personal_id and activo) then
    delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
    return jsonb_build_object('ok', false, 'motivo', 'BOMBERO_NO_REGISTRADO_O_INACTIVO');
  end if;

  if v_ses.accion_pendiente <> 'SELECCIONAR_ACTIVIDAD_INGRESO' then
    return jsonb_build_object('ok', false, 'motivo', 'ACCION_NO_SOPORTADA',
                              'accion', v_ses.accion_pendiente);
  end if;

  v_p := v_ses.payload_temporal;
  -- Solo se aceptan los botones que se ofrecieron (la sesión sigue viva si no).
  if not coalesce(v_p -> 'botones', '[]'::jsonb) ? v_tipo then
    return jsonb_build_object('ok', false, 'motivo', 'BOTON_INVALIDO', 'boton', p_boton_id);
  end if;

  -- Doble toque / carrera: si ya hay un ingreso abierto no se duplica.
  if exists (select 1 from asistencias_eventos
              where personal_id = v_ses.personal_id and fecha_hora_egreso is null
                and fecha_hora_ingreso >= now() - interval '16 hours') then
    delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
    return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_INGRESO_ABIERTO');
  end if;

  insert into asistencias_eventos
         (personal_id, zona_id, tipo_actividad, fecha_hora_ingreso,
          ingreso_ubicacion, ingreso_distancia_m, wa_message_id, metadata_wa)
  values (v_ses.personal_id,
          (v_p ->> 'zona_id')::uuid,
          v_tipo,
          coalesce((v_p ->> 'validado_en')::timestamptz, now()),
          st_setsrid(st_makepoint((v_p ->> 'lng')::double precision,
                                  (v_p ->> 'lat')::double precision), 4326)::geography,
          (v_p ->> 'distancia_m')::double precision,
          v_p ->> 'wa_message_id',
          jsonb_build_object('ingreso', coalesce(v_p -> 'metadata', '{}'::jsonb), 'boton', p_boton_id))
  returning id into v_id;

  delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
  return jsonb_build_object('ok', true, 'modo', 'INGRESO_REGISTRADO',
                            'asistencia_id', v_id, 'tipo_actividad', v_tipo);
end;
$$;

-- Solo el backend (service_role) ejecuta las RPC.
revoke all on function public.rpc_procesar_ubicacion_wa(varchar, double precision, double precision, varchar, jsonb)
  from public, anon, authenticated;
revoke all on function public.rpc_confirmar_boton_wa(varchar, varchar) from public, anon, authenticated;
grant execute on function public.rpc_procesar_ubicacion_wa(varchar, double precision, double precision, varchar, jsonb)
  to service_role;
grant execute on function public.rpc_confirmar_boton_wa(varchar, varchar) to service_role;

-- =============================================================================
-- Vistas para Power BI / estadísticas
-- =============================================================================

-- Minutos enteros (truncados) = floor(epoch(egreso - ingreso) / 60).
create view public.vw_powerbi_horas_personal
with (security_invoker = true) as
select a.id                                                            as asistencia_id,
       a.personal_id,
       p.legajo,
       p.id_ruba,
       p.nombre_completo,
       p.jerarquia,
       p.rol,
       a.tipo_actividad,
       z.nombre                                                        as zona,
       a.fecha_hora_ingreso,
       a.fecha_hora_egreso,
       (a.fecha_hora_ingreso at time zone 'America/Argentina/Cordoba')::date as fecha_local,
       extract(year  from a.fecha_hora_ingreso at time zone 'America/Argentina/Cordoba')::int as anio,
       extract(month from a.fecha_hora_ingreso at time zone 'America/Argentina/Cordoba')::int as mes,
       m.minutos_computados,
       round(m.minutos_computados / 60.0, 2)                           as horas_computadas,
       a.sincronizado_local
  from public.asistencias_eventos a
  join public.personal_cloud p on p.id = a.personal_id
  left join public.zonas_geocerca z on z.id = a.zona_id
  cross join lateral (
    select (floor(extract(epoch from (a.fecha_hora_egreso - a.fecha_hora_ingreso)) / 60))::int
           as minutos_computados
  ) m
 where a.fecha_hora_egreso is not null;

-- Operativa = EN_SERVICIO o CON_OBSERVACIONES; en taller = EN_TALLER_FS.
-- KMs recorridos = km_regreso - km_salida (solo movimientos con regreso).
create view public.vw_estado_flota_resumen
with (security_invoker = true) as
select f.movil_id,
       f.nombre_interno,
       f.tipo_unidad,
       f.estado_operativo,
       f.estado_operativo in ('EN_SERVICIO', 'CON_OBSERVACIONES')                as operativa,
       f.km_actual,
       f.taller_actual,
       f.motivo_fuera_servicio,
       f.ultimo_cambio_por_id,
       p.nombre_completo                                                         as ultimo_cambio_por,
       f.updated_at,
       km.cantidad_salidas_cerradas,
       km.km_recorridos_total,
       ult.hora_regreso                                                          as ultimo_regreso,
       ult.km_recorridos                                                         as ultimo_km_recorridos,
       count(*) over ()                                                          as total_unidades,
       count(*) filter (where f.estado_operativo in ('EN_SERVICIO', 'CON_OBSERVACIONES')) over () as total_operativas,
       count(*) filter (where f.estado_operativo = 'EN_TALLER_FS') over ()       as total_en_taller,
       count(*) filter (where f.estado_operativo = 'EN_EMERGENCIA') over ()      as total_en_emergencia
  from public.flota_moviles f
  left join public.personal_cloud p on p.id = f.ultimo_cambio_por_id
  left join lateral (
    select count(*)                                   as cantidad_salidas_cerradas,
           coalesce(sum(mm.km_regreso - mm.km_salida), 0) as km_recorridos_total
      from public.movimientos_moviles mm
     where mm.movil_id = f.movil_id and mm.km_regreso is not null
  ) km on true
  left join lateral (
    select mm.hora_regreso, (mm.km_regreso - mm.km_salida) as km_recorridos
      from public.movimientos_moviles mm
     where mm.movil_id = f.movil_id and mm.km_regreso is not null
     order by mm.hora_salida desc
     limit 1
  ) ult on true;

revoke all on public.vw_powerbi_horas_personal from anon, authenticated;
revoke all on public.vw_estado_flota_resumen   from anon, authenticated;

-- =============================================================================
-- Fire Station · Asistencia por token / QR + zona inicial del cuartel
--
--   * eventos_asistencia: sesiones (mantenimiento, reunión, capacitación...)
--     con un token alfanumérico que el bombero envía por WhatsApp ("QR:ABC123"
--     o el código solo). Válidas solo entre valido_desde y valido_hasta.
--   * asistencias_eventos gana evento_id y metodo_registro (UBICACION / TOKEN).
--   * rpc_registrar_token_wa: 1er envío del token = INGRESO, 2do = EGRESO.
--   * Zona "Cuartel Central" con coordenadas ESTIMADAS (centro de Adelia María):
--     hay que corregirlas con las reales del cuartel.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Eventos con token
-- -----------------------------------------------------------------------------
create table public.eventos_asistencia (
  id              uuid primary key default gen_random_uuid(),
  token           varchar(32) not null unique check (token ~ '^[A-Z0-9]{4,32}$'),
  tipo_actividad  varchar(20) not null
                  check (tipo_actividad in ('CAPACITACION', 'MANTENIMIENTO', 'REUNION',
                                            'ORDEN_INTERNO', 'GUARDIA')),
  descripcion     text,
  zona_id         uuid references public.zonas_geocerca (id),
  valido_desde    timestamptz not null default now(),
  valido_hasta    timestamptz not null,
  activo          boolean not null default true,
  creado_por_id   integer references public.personal_cloud (id),
  created_at      timestamptz not null default now(),
  constraint evento_ventana_valida check (valido_hasta > valido_desde)
);
create index eventos_asistencia_zona_idx on public.eventos_asistencia (zona_id);
create index eventos_asistencia_creado_por_idx on public.eventos_asistencia (creado_por_id);

alter table public.eventos_asistencia enable row level security;
grant select, insert, update, delete on public.eventos_asistencia to service_role;
revoke all on public.eventos_asistencia from anon, authenticated;

-- -----------------------------------------------------------------------------
-- Asistencias: vínculo al evento y método de registro
-- -----------------------------------------------------------------------------
alter table public.asistencias_eventos
  add column evento_id        uuid references public.eventos_asistencia (id),
  add column metodo_registro  varchar(12) not null default 'UBICACION'
                              check (metodo_registro in ('UBICACION', 'TOKEN'));
create index asistencias_evento_idx on public.asistencias_eventos (evento_id);

-- =============================================================================
-- RPC 3: token / QR recibido por WhatsApp
-- =============================================================================
create or replace function public.rpc_registrar_token_wa(
  p_telefono   varchar,
  p_token      varchar,
  p_wa_msg_id  varchar
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_personal_id  int;
  v_token        varchar := regexp_replace(upper(btrim(coalesce(p_token, ''))), '^QR\s*[:\-]\s*', '');
  v_ev           eventos_asistencia%rowtype;
  v_abierta      record;
  v_minutos      int;
  v_id           uuid;
begin
  select pc.id into v_personal_id
    from personal_cloud pc
   where pc.telefono_whatsapp = p_telefono and pc.activo
   for update;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'BOMBERO_NO_REGISTRADO_O_INACTIVO');
  end if;

  if p_wa_msg_id is not null and exists (
       select 1 from asistencias_eventos
        where wa_message_id = p_wa_msg_id or wa_message_id_egreso = p_wa_msg_id) then
    return jsonb_build_object('ok', false, 'motivo', 'MENSAJE_YA_PROCESADO');
  end if;

  select * into v_ev from eventos_asistencia where token = v_token;
  if not found then
    return jsonb_build_object('ok', false, 'motivo', 'TOKEN_INVALIDO');
  end if;
  if not v_ev.activo or now() < v_ev.valido_desde or now() > v_ev.valido_hasta then
    return jsonb_build_object('ok', false, 'motivo', 'SESION_NO_ACTIVA',
                              'descripcion', v_ev.descripcion,
                              'valido_desde', v_ev.valido_desde,
                              'valido_hasta', v_ev.valido_hasta);
  end if;

  select a.id, a.evento_id into v_abierta
    from asistencias_eventos a
   where a.personal_id = v_personal_id
     and a.fecha_hora_egreso is null
     and a.fecha_hora_ingreso >= now() - interval '16 hours'
   order by a.fecha_hora_ingreso desc
   limit 1
   for update;

  if found then
    if v_abierta.evento_id is distinct from v_ev.id then
      return jsonb_build_object('ok', false, 'motivo', 'YA_TIENE_INGRESO_ABIERTO');
    end if;
    -- Segundo envío del mismo token: egreso.
    update asistencias_eventos
       set fecha_hora_egreso    = now(),
           wa_message_id_egreso = p_wa_msg_id,
           metadata_wa          = metadata_wa || jsonb_build_object('egreso', jsonb_build_object('token', v_token))
     where id = v_abierta.id
     returning (floor(extract(epoch from (fecha_hora_egreso - fecha_hora_ingreso)) / 60))::int
          into v_minutos;
    return jsonb_build_object('ok', true, 'modo', 'EGRESO_REGISTRADO',
                              'asistencia_id', v_abierta.id,
                              'tipo_actividad', v_ev.tipo_actividad,
                              'descripcion', v_ev.descripcion,
                              'minutos', v_minutos);
  end if;

  if exists (select 1 from asistencias_eventos
              where personal_id = v_personal_id and evento_id = v_ev.id) then
    return jsonb_build_object('ok', false, 'motivo', 'YA_REGISTRADO',
                              'descripcion', v_ev.descripcion);
  end if;

  insert into asistencias_eventos
         (personal_id, zona_id, tipo_actividad, fecha_hora_ingreso,
          wa_message_id, metadata_wa, evento_id, metodo_registro)
  values (v_personal_id, v_ev.zona_id, v_ev.tipo_actividad, now(),
          p_wa_msg_id, jsonb_build_object('ingreso', jsonb_build_object('token', v_token)),
          v_ev.id, 'TOKEN')
  returning id into v_id;

  delete from bot_sesiones_pendientes where telefono_whatsapp = p_telefono;
  return jsonb_build_object('ok', true, 'modo', 'INGRESO_REGISTRADO',
                            'asistencia_id', v_id,
                            'tipo_actividad', v_ev.tipo_actividad,
                            'descripcion', v_ev.descripcion);
end;
$$;

revoke all on function public.rpc_registrar_token_wa(varchar, varchar, varchar) from public, anon, authenticated;
grant execute on function public.rpc_registrar_token_wa(varchar, varchar, varchar) to service_role;

-- -----------------------------------------------------------------------------
-- Zona inicial (COORDENADAS ESTIMADAS: centro de Adelia María, Córdoba).
-- Corregir con: update zonas_geocerca set ubicacion = st_setsrid(st_makepoint(<lng>, <lat>), 4326)::geography
--               where nombre = 'Cuartel Central';
-- -----------------------------------------------------------------------------
insert into public.zonas_geocerca (nombre, ubicacion, radio_tolerancia_metros)
select 'Cuartel Central',
       extensions.st_setsrid(extensions.st_makepoint(-64.0213, -33.6317), 4326)::extensions.geography,
       150
 where not exists (select 1 from public.zonas_geocerca where nombre = 'Cuartel Central');

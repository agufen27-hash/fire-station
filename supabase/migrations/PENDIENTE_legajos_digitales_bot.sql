-- =============================================================================
-- Legajos digitales en la nube + estado de menús del bot de WhatsApp
--
--   * personal_cloud.dni: búsqueda de legajos por DNI desde el bot.
--   * legajo_documentos: índice de los documentos del legajo (espejo de
--     documentos_personal de SQLite, id = documentos_personal.id). El archivo
--     vive en el bucket PRIVADO `legajos` de Storage: {personal_id}/{id}_{archivo}.
--     `categoria` se deriva del tipo de documento (agrupa los menús del bot).
--   * bot_menu_estado: último menú numerado mostrado a cada teléfono, para
--     que una respuesta "2" se traduzca a la opción elegida (vence a los 15 min).
--   * legajo_accesos_bot: auditoría de cada consulta / descarga por el bot.
--
-- Acceso: RLS sin políticas y solo service_role (la Edge Function y la app de
-- escritorio), igual que el resto del esquema.
-- =============================================================================

alter table public.personal_cloud add column if not exists dni varchar(15);
create index if not exists personal_cloud_dni_idx on public.personal_cloud (dni);

-- -----------------------------------------------------------------------------
-- Documentos del legajo
-- -----------------------------------------------------------------------------
create table public.legajo_documentos (
  id              integer primary key,                      -- = documentos_personal.id local
  personal_id     integer not null references public.personal_cloud (id) on delete cascade,
  tipo_documento  varchar(60) not null,
  categoria       varchar(20) generated always as (
                    case
                      when upper(tipo_documento) = 'DNI' then 'DNI'
                      when upper(tipo_documento) like 'CERTIFICADO%' then 'CURSOS'
                      when upper(tipo_documento) like 'LIBRETA%' then 'LIBRETA'
                      when upper(tipo_documento) like 'SUMARIO%'
                        or upper(tipo_documento) like 'SOLICITUD%' then 'ADMINISTRATIVO'
                      else 'OTROS'
                    end
                  ) stored,
  nombre_archivo  text not null,
  storage_path    text not null unique,
  tamanio_bytes   bigint check (tamanio_bytes >= 0),
  mime_type       text,
  fecha_subida    timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index legajo_documentos_personal_idx on public.legajo_documentos (personal_id, categoria);
create trigger legajo_documentos_updated_at before update on public.legajo_documentos
  for each row execute function public.tg_set_updated_at();

comment on table public.legajo_documentos is
  'Índice de documentos del legajo (archivos en Storage, bucket privado legajos). Fuente: app de escritorio.';

-- -----------------------------------------------------------------------------
-- Estado de menús numerados del bot
-- -----------------------------------------------------------------------------
create table public.bot_menu_estado (
  telefono_whatsapp  varchar(16) primary key,
  personal_id        integer not null references public.personal_cloud (id) on delete cascade,
  menu               varchar(60) not null,
  opciones           jsonb not null default '[]'::jsonb,   -- acciones en el orden mostrado (1..n)
  expires_at         timestamptz not null default (now() + interval '15 minutes'),
  updated_at         timestamptz not null default now()
);
create index bot_menu_estado_personal_idx on public.bot_menu_estado (personal_id);
create trigger bot_menu_estado_updated_at before update on public.bot_menu_estado
  for each row execute function public.tg_set_updated_at();

-- -----------------------------------------------------------------------------
-- Auditoría de consultas de legajos
-- -----------------------------------------------------------------------------
create table public.legajo_accesos_bot (
  id                 uuid primary key default gen_random_uuid(),
  consultante_id     integer not null references public.personal_cloud (id),
  legajo_personal_id integer references public.personal_cloud (id) on delete set null,
  accion             varchar(20) not null,     -- BUSQUEDA / VER / FICHA / CATEGORIA / ZIP / DENEGADO
  detalle            text,
  created_at         timestamptz not null default now()
);
create index legajo_accesos_consultante_idx on public.legajo_accesos_bot (consultante_id, created_at desc);
create index legajo_accesos_legajo_idx on public.legajo_accesos_bot (legajo_personal_id);

-- -----------------------------------------------------------------------------
-- Seguridad
-- -----------------------------------------------------------------------------
alter table public.legajo_documentos   enable row level security;
alter table public.bot_menu_estado     enable row level security;
alter table public.legajo_accesos_bot  enable row level security;

revoke all on public.legajo_documentos, public.bot_menu_estado, public.legajo_accesos_bot
  from anon, authenticated;
grant select, insert, update, delete on public.legajo_documentos, public.bot_menu_estado
  to service_role;
grant select, insert on public.legajo_accesos_bot to service_role;

-- -----------------------------------------------------------------------------
-- Bucket privado de Storage (sin políticas: solo service_role, que las saltea)
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit)
values ('legajos', 'legajos', false, 26214400)   -- 25 MB por archivo
on conflict (id) do nothing;

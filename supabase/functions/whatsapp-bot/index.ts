// =============================================================================
// Fire Station · Edge Function `whatsapp-bot` (Fase 3)
//
// Webhook de WhatsApp Cloud API (Meta):
//   GET  -> verificación del webhook (hub.mode / hub.verify_token / hub.challenge)
//   POST -> recepción de mensajes; valida el remitente contra public.personal_cloud
//           y enruta según el tipo:
//             location            -> rpc_procesar_ubicacion_wa (geocerca)
//                                    · dentro y sin ingreso abierto -> botones de actividad
//                                    · dentro y con ingreso abierto -> egreso
//             interactive (botón) -> rpc_confirmar_boton_wa (registra el ingreso)
//             text "QR:XXXX" / token -> rpc_registrar_token_wa (evento con token)
//             text "VIAJE <destino> EN <vehículo>" -> rpc_registrar_viaje (libro de guardia;
//                                    el trigger avisa al grupo OPERATIVO y a Alerthor)
//             text "REGRESO HH:MM" / "HH:MM" -> rpc_cerrar_viaje (hora de hoy, entre salida y ahora)
//             text "SI" / "REGRESO" con viaje abierto -> pide la hora de llegada
//             text normal         -> saludo + menú principal
//
// Motor de comandos (comandos.ts): todo texto se normaliza (sin tildes,
// mayúsculas ni espacios de más) y la palabra de comando se compara de forma
// aproximada (difflib + Damerau-Levenshtein) contra el catálogo:
//   MENU · INVENTARIO · MOVILES · GUARDIA · LEGAJO · VIAJE · REGRESO · CANCELAR
//   similitud ≥ 0.80 -> se ejecuta ("invnetario", "regreos 18:30")
//   0.60 - 0.80      -> botones "¿Quisiste decir…?"
//
// Menús interactivos: mensajes de lista de WhatsApp + el mismo listado
// numerado en el texto; responder "2" elige la opción 2 (estado en
// public.bot_menu_estado, vence a los 15 min). "0" vuelve al menú principal.
// Cada opción es una "acción" (id de fila) que se puede volver a ejecutar:
//   INV…  inventario      MOV…  móviles      GUA…  estado de guardia
//   LEG…  legajos         CMD:<COMANDO>:<args>  (botón de sugerencia)
//
// Legajos digitales: LEGAJO <DNI | N° de legajo | apellido>. Un BOMBERO solo
// ve su propio legajo; ADMIN y JEFATURA ven cualquiera. Los archivos están en
// el bucket privado `legajos` (los sube la app de escritorio) y se envían como
// documento con un enlace firmado de 1 h; el paquete completo se arma como ZIP.
// Cada consulta queda en public.legajo_accesos_bot.
//
// Variables de entorno:
//   WHATSAPP_VERIFY_TOKEN      (secret propio)  token elegido al configurar el webhook en Meta
//   WHATSAPP_APP_SECRET        (secret propio, opcional) App Secret de Meta para validar
//                              la firma X-Hub-Signature-256. Si no está, se loguea un aviso.
//   WHATSAPP_ACCESS_TOKEN      (secret propio) token de la app de Meta para la Graph API
//   WHATSAPP_PHONE_ID          (secret propio) Phone Number ID del número del bot
//   SUPABASE_URL               (inyectada por Supabase)
//   SUPABASE_SERVICE_ROLE_KEY  (inyectada por Supabase)
//
// Geocerca: las coordenadas y el radio del cuartel NO están en el código; viven en
// public.zonas_geocerca (ubicacion, radio_tolerancia_metros) y se cambian por SQL.
//
// Por qué service_role y no anon: las tablas tienen RLS activado SIN políticas y
// los privilegios de anon/authenticated están revocados (ver migraciones).
//
// La función debe desplegarse con --no-verify-jwt: Meta no envía un JWT de Supabase.
// =============================================================================

import { createClient } from "npm:@supabase/supabase-js@2";
import JSZip from "npm:jszip@3.10.1";
import { COMANDOS, type IdComando, normalizar, puntajeBusqueda, resolverComando } from "./comandos.ts";

const VERIFY_TOKEN = Deno.env.get("WHATSAPP_VERIFY_TOKEN") ?? "";
const APP_SECRET = Deno.env.get("WHATSAPP_APP_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ACCESS_TOKEN = Deno.env.get("WHATSAPP_ACCESS_TOKEN") ?? "";
const PHONE_ID = Deno.env.get("WHATSAPP_PHONE_ID") ?? "";
const GRAPH_URL = "https://graph.facebook.com/v26.0";
const TZ = "America/Argentina/Cordoba";

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

// -----------------------------------------------------------------------------
// Tipos mínimos del payload de Meta
// -----------------------------------------------------------------------------
interface WaLocation {
  latitude: number;
  longitude: number;
  name?: string;
  address?: string;
  url?: string;
}

interface WaMessage {
  from: string;
  id: string;
  timestamp: string;
  type: string;
  text?: { body: string };
  button?: { text: string; payload?: string };
  interactive?: {
    type: string;
    button_reply?: { id: string; title: string };
    list_reply?: { id: string; title: string };
  };
  location?: WaLocation;
}

interface WaWebhookPayload {
  object?: string;
  entry?: Array<{
    changes?: Array<{
      field?: string;
      value?: {
        messages?: WaMessage[];
        statuses?: unknown[];
      };
    }>;
  }>;
}

interface PersonalRow {
  id: number;
  nombre_completo: string;
  jerarquia: string | null;
  rol: string;
  activo: boolean;
  telefono_whatsapp: string;
}

// deno-lint-ignore no-explicit-any
type RpcResult = Record<string, any>;

// deno-lint-ignore no-explicit-any
type Contenido = Record<string, any>; // parte "type + {text|interactive|document}" del mensaje saliente

/** Uno o varios mensajes salientes, enviados en orden. */
type Respuesta = Contenido | Contenido[];

// -----------------------------------------------------------------------------
// Utilidades
// -----------------------------------------------------------------------------
const ok = (body = "EVENT_RECEIVED") =>
  new Response(body, { status: 200, headers: { "Content-Type": "text/plain" } });

/** Enmascara el teléfono en los logs: +54935812****. */
const mask = (tel: string) => (tel.length > 4 ? tel.slice(0, -4) + "****" : "****");

/**
 * Meta envía `from` sin '+' (ej. '5493581234567'). En personal_cloud el número
 * puede estar cargado con o sin '+', con o sin 54 / 9 ('549358...' hoy).
 * Para Argentina (54...) se busca por los últimos 10 dígitos (área + número
 * local), que son iguales en todas esas variantes; si no, coincidencia exacta.
 */
function buscarPersonal(from: string) {
  const digits = from.replace(/\D/g, ""); // quita '+', espacios, guiones
  const q = supabase
    .from("personal_cloud")
    .select("id, nombre_completo, jerarquia, rol, activo, telefono_whatsapp");
  if (digits.startsWith("54") && digits.length >= 12) {
    const local10 = digits.slice(-10); // solo dígitos: seguro dentro de LIKE
    return q.like("telefono_whatsapp", `%${local10}`).limit(1).maybeSingle<PersonalRow>();
  }
  return q.in("telefono_whatsapp", [digits, `+${digits}`]).limit(1).maybeSingle<PersonalRow>();
}

/** Texto legible del mensaje según su tipo (para logs). */
function extraerTexto(msg: WaMessage): string {
  switch (msg.type) {
    case "text":
      return msg.text?.body ?? "";
    case "button":
      return msg.button?.text ?? "";
    case "interactive":
      return msg.interactive?.button_reply?.title ??
        msg.interactive?.list_reply?.title ?? "";
    case "location":
      return msg.location ? `[ubicación ${msg.location.latitude},${msg.location.longitude}]` : "";
    default:
      return `[${msg.type}]`;
  }
}

/** Hora local del cuartel, p.ej. "04/11 21:35". */
function horaLocal(d = new Date()): string {
  return new Intl.DateTimeFormat("es-AR", {
    timeZone: TZ,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** Solo el día local, p.ej. "04/11". */
function diaLocal(d: Date): string {
  return new Intl.DateTimeFormat("es-AR", { timeZone: TZ, day: "2-digit", month: "2-digit" }).format(d);
}

/** Solo la hora local, p.ej. "21:35". */
function soloHora(d: Date): string {
  return new Intl.DateTimeFormat("es-AR", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false })
    .format(d);
}

function duracion(min: number | null | undefined): string {
  const m = Math.max(0, Math.floor(Number(min ?? 0)));
  const h = Math.floor(m / 60);
  return h > 0 ? `${h} h ${m % 60} min` : `${m} min`;
}

const NOMBRE_ACTIVIDAD: Record<string, string> = {
  CAPACITACION: "Capacitación",
  MANTENIMIENTO: "Mantenimiento",
  REUNION: "Reunión",
  ORDEN_INTERNO: "Orden interno",
  GUARDIA: "Guardia",
};
const actividad = (t: string | null | undefined) => NOMBRE_ACTIVIDAD[t ?? ""] ?? (t ?? "");

/** Valida X-Hub-Signature-256 = sha256=HMAC(app_secret, raw_body). */
async function firmaValida(rawBody: string, header: string | null): Promise<boolean> {
  if (!APP_SECRET) return true; // sin secret configurado no se puede validar
  if (!header?.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(APP_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const esperado = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  const recibido = header.slice("sha256=".length);
  // Comparación en tiempo constante
  if (recibido.length !== esperado.length) return false;
  let diff = 0;
  for (let i = 0; i < esperado.length; i++) diff |= esperado.charCodeAt(i) ^ recibido.charCodeAt(i);
  return diff === 0;
}

// -----------------------------------------------------------------------------
// GET: verificación del webhook
// -----------------------------------------------------------------------------
function handleVerify(url: URL): Response {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode === "subscribe" && VERIFY_TOKEN && token === VERIFY_TOKEN && challenge) {
    console.log("[whatsapp-bot] Webhook verificado por Meta");
    return new Response(challenge, { status: 200, headers: { "Content-Type": "text/plain" } });
  }
  console.warn("[whatsapp-bot] Verificación rechazada (token o modo inválido)");
  return new Response("Forbidden", { status: 403 });
}

// -----------------------------------------------------------------------------
// Envío (Graph API de Meta)
// -----------------------------------------------------------------------------
const texto = (body: string): Contenido => ({ type: "text", text: { preview_url: false, body } });

function botones(body: string, ids: string[]): Contenido {
  return {
    type: "interactive",
    interactive: {
      type: "button",
      body: { text: body },
      action: {
        // WhatsApp admite hasta 3 botones con títulos de hasta 20 caracteres.
        buttons: ids.slice(0, 3).map((id) => ({
          type: "reply",
          reply: { id, title: actividad(id).slice(0, 20) },
        })),
      },
    },
  };
}

/** Botones con id y título libres (máx. 3, título ≤ 20). */
function botonesLibres(body: string, opciones: Array<{ id: string; titulo: string }>): Contenido {
  return {
    type: "interactive",
    interactive: {
      type: "button",
      body: { text: body },
      action: {
        buttons: opciones.slice(0, 3).map((o) => ({
          type: "reply",
          reply: { id: o.id.slice(0, 256), title: o.titulo.slice(0, 20) },
        })),
      },
    },
  };
}

interface Opcion {
  accion: string; // id de la fila = acción a ejecutar
  titulo: string; // ≤ 24 caracteres en la lista
  descripcion?: string; // ≤ 72
}

/** Mensaje de lista de WhatsApp (máx. 10 filas). */
function lista(body: string, opciones: Opcion[]): Contenido {
  return {
    type: "interactive",
    interactive: {
      type: "list",
      body: { text: body.slice(0, 1024) },
      action: {
        button: "Ver opciones",
        sections: [{
          title: "Opciones",
          rows: opciones.slice(0, 10).map((o) => ({
            id: o.accion.slice(0, 200),
            title: o.titulo.slice(0, 24),
            ...(o.descripcion ? { description: o.descripcion.slice(0, 72) } : {}),
          })),
        }],
      },
    },
  };
}

const documento = (link: string, filename: string, caption?: string): Contenido => ({
  type: "document",
  document: { link, filename, ...(caption ? { caption: caption.slice(0, 1024) } : {}) },
});

async function postGraph(
  to: string,
  contenido: Contenido,
  // deno-lint-ignore no-explicit-any
): Promise<{ ok: boolean; status: number; data: any }> {
  const res = await fetch(`${GRAPH_URL}/${PHONE_ID}/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${ACCESS_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messaging_product: "whatsapp", recipient_type: "individual", to, ...contenido }),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

/**
 * Formatos de destino a probar, en orden. Peculiaridad de Argentina: Meta
 * entrega el wa_id como '549' + área + número, pero según cómo se registró el
 * destinatario solo acepta alguno de estos (si no, error 131030):
 *   1. 549 + área + número          (original)
 *   2. 54  + área + número          (sin el 9)
 *   3. 54  + área + 15 + número     (formato con 15)
 * Para el 3 se asume característica de 3 dígitos (358, la del cuartel).
 */
const AREA_LEN_AR = 3;

function formatosDestino(to: string): string[] {
  const d = to.replace(/\D/g, "");
  if (!d.startsWith("549") || d.length !== 13) return [d];
  const nacional = d.slice(3); // área + número (10 dígitos)
  const area = nacional.slice(0, AREA_LEN_AR);
  const local = nacional.slice(AREA_LEN_AR);
  return [d, `54${nacional}`, `54${area}15${local}`];
}

/** Errores de Meta que pueden deberse al formato del destinatario. */
const ERRORES_FORMATO = new Set([131030, 100]);

async function enviar(to: string, contenido: Contenido): Promise<void> {
  if (!ACCESS_TOKEN || !PHONE_ID) {
    console.error("[whatsapp-bot] Faltan WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_ID: no se responde");
    return;
  }
  const formatos = formatosDestino(to);
  for (let i = 0; i < formatos.length; i++) {
    const destino = formatos[i];
    const r = await postGraph(destino, contenido);
    if (r.ok) {
      console.log(
        `[whatsapp-bot] Respuesta (${contenido.type}) enviada a ${mask(`+${destino}`)} ` +
          `(formato ${i + 1}/${formatos.length}, wamid ${r.data?.messages?.[0]?.id ?? "?"})`,
      );
      return;
    }
    const e = r.data?.error ?? {};
    console.error(
      `[whatsapp-bot] Graph API ${r.status} con formato ${i + 1}/${formatos.length} (${mask(`+${destino}`)}): ` +
        `code=${e.code} ${e.message ?? JSON.stringify(r.data)}`,
    );
    if (!ERRORES_FORMATO.has(e.code)) return; // token, permisos, etc.: otro formato no lo arregla
  }
}

// -----------------------------------------------------------------------------
// Textos de respuesta
// -----------------------------------------------------------------------------
const MSG_DENEGADO = "Acceso denegado. Este número no está registrado en el padrón del cuartel.";
const MSG_AYUDA = "📍 Para fichar, enviá tu *ubicación actual* desde el cuartel.\n" +
  "🔑 Para un evento con código, enviá *QR:CODIGO* (ej. QR:AB12CD).\n" +
  "🚒 Para registrar un viaje: *VIAJE Río Cuarto EN M-12*.\n" +
  "🏁 Para marcar el regreso: *REGRESO 18:30*.\n" +
  "📋 Consultas: *MENU*, *INVENTARIO*, *MOVILES*, *GUARDIA*, *LEGAJO <DNI o apellido>*.\n" +
  "No hace falta escribirlos perfecto: el bot entiende errores de tipeo.";
const MSG_ERROR = "⚠️ No pude procesar tu solicitud en este momento. Probá de nuevo en unos minutos.";

/** "Bombero" / "Cabo" ... ; capitaliza si viene todo en mayúsculas. */
function capitalizar(s: string): string {
  if (s !== s.toUpperCase()) return s; // ya tiene mayúsculas/minúsculas mixtas
  return s.toLowerCase().replace(/(^|[\s'-])(\p{L})/gu, (_m, sep, c) => sep + c.toUpperCase());
}

/** nombre_completo tiene formato "APELLIDO, Nombre" -> "Apellido". */
function apellidoDe(nombreCompleto: string): string {
  return capitalizar(nombreCompleto.split(",")[0].trim());
}

function trato(p: PersonalRow): string {
  const grado = p.jerarquia?.trim() ? capitalizar(p.jerarquia.trim()) + " " : "";
  return `${grado}${apellidoDe(p.nombre_completo)}`;
}

/** Motivos de rechazo comunes a las tres RPC. */
function textoMotivo(r: RpcResult): string {
  switch (r.motivo) {
    case "FUERA_DE_ZONA":
      return `❌ Estás fuera de la zona del ${r.zona}: a ${Math.round(r.distancia_m)} m ` +
        `(tolerancia ${r.tolerancia_m} m). Acercate al cuartel y volvé a enviar tu ubicación.`;
    case "UBICACION_NO_EN_VIVO":
      return "❌ Esa ubicación es un lugar elegido de la lista. Enviá tu *ubicación actual* " +
        "(📎 → Ubicación → Enviar mi ubicación actual).";
    case "COORDENADAS_INVALIDAS":
      return "❌ La ubicación recibida no es válida. Probá enviarla de nuevo.";
    case "SIN_ZONAS_CONFIGURADAS":
      return "⚠️ El cuartel todavía no tiene una zona de fichaje configurada. Avisá a la Jefatura.";
    case "MENSAJE_YA_PROCESADO":
      return "ℹ️ Ese mensaje ya fue procesado.";
    case "SIN_SESION_PENDIENTE":
    case "SESION_EXPIRADA":
      return "⌛ La selección expiró. Volvé a enviar tu ubicación para fichar.";
    case "BOTON_INVALIDO":
      return "❌ Opción no válida. Volvé a enviar tu ubicación para fichar.";
    case "YA_TIENE_INGRESO_ABIERTO":
      return "ℹ️ Ya tenés un ingreso abierto. Para cerrarlo enviá tu ubicación (o el mismo código del evento).";
    case "TOKEN_INVALIDO":
      return "❌ Código no válido. Revisá que esté bien escrito.";
    case "SESION_NO_ACTIVA":
      return `❌ El evento${r.descripcion ? ` "${r.descripcion}"` : ""} no está activo ahora ` +
        `(válido de ${horaLocal(new Date(r.valido_desde))} a ${horaLocal(new Date(r.valido_hasta))}).`;
    case "YA_REGISTRADO":
      return `ℹ️ Ya registraste tu asistencia completa a${r.descripcion ? ` "${r.descripcion}"` : "l evento"}.`;
    case "BOMBERO_NO_REGISTRADO_O_INACTIVO":
      return MSG_DENEGADO;
    case "YA_TIENE_VIAJE_ABIERTO":
      return `ℹ️ Ya tenés un viaje abierto${r.destino ? ` hacia ${r.destino}` : ""}` +
        `${r.fecha_salida ? ` (salida ${horaLocal(new Date(r.fecha_salida))})` : ""}. ` +
        "Marcá el regreso con *REGRESO HH:MM* antes de registrar otro.";
    case "DATOS_INCOMPLETOS":
      return "❌ Faltan datos. Formato: *VIAJE <destino> EN <vehículo>* (ej. VIAJE Río Cuarto EN M-12).";
    case "SIN_VIAJE_ABIERTO":
      return "ℹ️ No tenés ningún viaje abierto.";
    case "HORA_INVALIDA":
      return "❌ Hora no válida. Usá el formato *REGRESO HH:MM* (ej. REGRESO 18:30).";
    case "LLEGADA_ANTERIOR_A_SALIDA":
      return `❌ La llegada (${horaLocal(new Date(r.llegada_propuesta))}) no puede ser anterior a la salida ` +
        `(${horaLocal(new Date(r.fecha_salida))}). La hora se toma con el día de hoy.`;
    case "LLEGADA_EN_EL_FUTURO":
      return `❌ La llegada (${horaLocal(new Date(r.llegada_propuesta))}) es posterior al momento actual. ` +
        "La hora se toma con el día de hoy: indicá una hora que ya haya pasado.";
    default:
      return MSG_ERROR;
  }
}

// -----------------------------------------------------------------------------
// Handlers por tipo de mensaje (el remitente ya está autenticado y activo)
// -----------------------------------------------------------------------------
async function rpc(nombre: string, args: Record<string, unknown>): Promise<RpcResult | null> {
  const { data, error } = await supabase.rpc(nombre, args);
  if (error) {
    console.error(`[whatsapp-bot] RPC ${nombre} falló: ${error.code} ${error.message}`);
    return null;
  }
  console.log(`[whatsapp-bot] RPC ${nombre} -> ${JSON.stringify(data)}`);
  return data as RpcResult;
}

async function manejarUbicacion(p: PersonalRow, msg: WaMessage): Promise<Contenido> {
  const loc = msg.location!;
  const r = await rpc("rpc_procesar_ubicacion_wa", {
    p_telefono: p.telefono_whatsapp,
    p_lat: loc.latitude,
    p_lng: loc.longitude,
    p_wa_msg_id: msg.id,
    p_metadata: loc, // name/address presentes => lugar elegido, no ubicación en vivo
  });
  if (!r) return texto(MSG_ERROR);
  if (!r.ok) return texto(textoMotivo(r));

  if (r.modo === "PEDIR_BOTON_ACTIVIDAD") {
    return botones(
      `✅ ${trato(p)}, ubicación validada en ${r.zona} (a ${Math.round(r.distancia_m)} m).\n` +
        "¿Qué actividad vas a realizar?",
      r.botones ?? ["CAPACITACION", "MANTENIMIENTO", "REUNION"],
    );
  }
  if (r.modo === "EGRESO_DIRECTO") {
    return texto(
      `👋 Egreso registrado por geolocalización en ${r.zona} a las ${horaLocal()}.\n` +
        `Tiempo computado: ${duracion(r.minutos)}.`,
    );
  }
  return texto(MSG_ERROR);
}

async function manejarBoton(p: PersonalRow, botonId: string): Promise<Contenido> {
  const r = await rpc("rpc_confirmar_boton_wa", { p_telefono: p.telefono_whatsapp, p_boton_id: botonId });
  if (!r) return texto(MSG_ERROR);
  if (!r.ok) return texto(textoMotivo(r));
  return texto(
    `✅ Presente registrado por geolocalización: *${actividad(r.tipo_actividad)}*, ` +
      `ingreso ${horaLocal()}.\nAl retirarte, enviá de nuevo tu ubicación para registrar el egreso.`,
  );
}

async function manejarToken(p: PersonalRow, token: string, msg: WaMessage): Promise<RpcResult | null> {
  return await rpc("rpc_registrar_token_wa", {
    p_telefono: p.telefono_whatsapp,
    p_token: token,
    p_wa_msg_id: msg.id,
  });
}

function respuestaToken(r: RpcResult | null): Contenido {
  if (!r) return texto(MSG_ERROR);
  if (!r.ok) return texto(textoMotivo(r));
  const evento = `*${actividad(r.tipo_actividad)}*${r.descripcion ? ` — ${r.descripcion}` : ""}`;
  if (r.modo === "EGRESO_REGISTRADO") {
    return texto(`👋 Egreso registrado en ${evento} a las ${horaLocal()}.\nTiempo computado: ${duracion(r.minutos)}.`);
  }
  return texto(
    `✅ Presente registrado en ${evento}, ingreso ${horaLocal()}.\n` +
      "Al retirarte, enviá el mismo código para registrar el egreso.",
  );
}

/** "QR:AB12CD", "qr - ab12cd" -> token explícito. */
const RE_QR = /^\s*QR\s*[:\-]\s*([A-Za-z0-9]{4,32})\s*$/i;
/** Código suelto: 6-32 alfanuméricos con al menos una letra y un dígito (no confunde "Hola"). */
const RE_TOKEN_SUELTO = /^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]{6,32}$/;

// --- Libro de guardia: viajes -------------------------------------------------
/** "VIAJE Río Cuarto EN M-12" (corta en el ÚLTIMO " en ": el destino puede contenerlo). */
const RE_VIAJE = /^\s*viaje\s+(?:a\s+|hacia\s+)?(.+)\s+en\s+(.+?)\s*$/i;
/** "REGRESO 18:30", "llegué 18.30", "18h30", "18:30 hs". Grupo 1 = palabra clave (opcional). */
const RE_REGRESO_HORA =
  /^\s*(regreso|llegada|llegu[eé]|volv[ií])?\s*(?:a\s+las\s+)?([01]?\d|2[0-3])\s*[:.hH]\s*([0-5]\d)\s*(?:hs?\.?)?\s*$/i;
/** Respuesta al recordatorio sin hora: "Sí", "regreso", "volví". */
const RE_REGRESO_SIN_HORA = /^\s*(s[ií]|regreso|marcar regreso|llegu[eé]|volv[ií])\s*[.!]*\s*$/i;

async function viajeAbierto(p: PersonalRow) {
  const { data, error } = await supabase
    .from("viajes_activos")
    .select("id, destino, vehiculo, fecha_salida")
    .eq("bombero_id", p.id)
    .eq("estado", "EN_VIAJE")
    .maybeSingle<{ id: string; destino: string; vehiculo: string; fecha_salida: string }>();
  if (error) console.error(`[whatsapp-bot] Error consultando viajes_activos: ${error.code} ${error.message}`);
  return data;
}

async function registrarViaje(p: PersonalRow, destino: string, vehiculo: string): Promise<Contenido> {
  const r = await rpc("rpc_registrar_viaje", {
    p_bombero_id: p.id,
    p_destino: destino,
    p_vehiculo: vehiculo,
    p_origen: "BOT",
  });
  if (!r) return texto(MSG_ERROR);
  if (!r.ok) return texto(textoMotivo(r));
  return texto(
    `🚒 Viaje registrado: salida ${horaLocal(new Date(r.fecha_salida))} hacia *${r.destino}* en *${r.vehiculo}*.\n` +
      "Se notificará al grupo operativo. Al volver, enviá *REGRESO HH:MM*.",
  );
}

async function cerrarViaje(p: PersonalRow, hora: string): Promise<RpcResult | null> {
  return await rpc("rpc_cerrar_viaje", { p_bombero_id: p.id, p_hora: hora });
}

function respuestaCierre(r: RpcResult | null): Contenido {
  if (!r) return texto(MSG_ERROR);
  if (!r.ok) return texto(textoMotivo(r));
  return texto(
    `🏁 Regreso registrado: ${diaLocal(new Date(r.fecha_llegada))} ${soloHora(new Date(r.fecha_llegada))} ` +
      `desde *${r.destino}* (${r.vehiculo}). Duración: ${duracion(r.minutos)}. Viaje FINALIZADO.`,
  );
}

/**
 * Comandos de formato fijo (VIAJE / REGRESO / "SI"). Devuelve null si el texto
 * no es ninguno. Lo usa también el motor aproximado con el texto ya corregido
 * ("viajr Río Cuarto EN M-12" -> "viaje Río Cuarto EN M-12").
 */
async function comandosDeViaje(p: PersonalRow, body: string): Promise<Contenido | null> {
  const viaje = RE_VIAJE.exec(body);
  if (viaje) return await registrarViaje(p, viaje[1].trim(), viaje[2].trim());

  const regreso = RE_REGRESO_HORA.exec(body);
  if (regreso) {
    const hora = `${regreso[2].padStart(2, "0")}:${regreso[3]}`;
    const r = await cerrarViaje(p, hora);
    // Una hora suelta sin viaje abierto no es un comando: se responde el saludo.
    if (regreso[1] || !(r && !r.ok && r.motivo === "SIN_VIAJE_ABIERTO")) return respuestaCierre(r);
  }

  const sinHora = RE_REGRESO_SIN_HORA.exec(body);
  if (sinHora) {
    const v = await viajeAbierto(p);
    if (v) {
      const salida = new Date(v.fecha_salida);
      return texto(
        `Indicá la hora de llegada desde *${v.destino}*, por ejemplo *REGRESO 18:30*.\n` +
          `Se toma el día de hoy; tiene que ser posterior a la salida (${horaLocal(salida)}) y no futura.`,
      );
    }
    if (!/^s[ií]$/i.test(sinHora[1])) return texto(textoMotivo({ motivo: "SIN_VIAJE_ABIERTO" }));
  }
  return null;
}

/** "2", "2.", "2)" -> opción numerada del último menú. */
const RE_NUMERO_OPCION = /^\s*(\d{1,2})\s*[.)]?\s*$/;

async function manejarTexto(p: PersonalRow, msg: WaMessage): Promise<Respuesta> {
  const body = (msg.text?.body ?? "").trim();

  // 1. Respuesta numérica a un menú.
  const numero = RE_NUMERO_OPCION.exec(body);
  if (numero) {
    const n = Number(numero[1]);
    if (n === 0) return await menuPrincipal(p);
    const accion = await opcionDeMenu(p, n);
    if (accion) return await ejecutarAccion(p, accion, msg);
    return await menuPrincipal(p, `⌛ No hay un menú abierto con la opción ${n} (los menús vencen a los 15 min).`);
  }

  // 2. Código de evento explícito.
  const qr = RE_QR.exec(body);
  if (qr) return respuestaToken(await manejarToken(p, qr[1], msg));

  // 3. Formatos fijos escritos correctamente (VIAJE / REGRESO / hora suelta).
  const clasico = await comandosDeViaje(p, body);
  if (clasico) return clasico;

  // 4. Interceptor global con tolerancia a errores.
  const r = resolverComando(body);
  if (r.tipo === "EJECUTAR") {
    console.log(
      `[whatsapp-bot] Comando ${r.comando.id} (similitud ${r.comando.puntaje.toFixed(2)}) args="${r.comando.argumentos}"`,
    );
    return await ejecutarComando(p, r.comando.id, r.comando.argumentos, msg);
  }

  // 5. Código de evento suelto (antes de sugerir: "AB12CD" no es un comando mal escrito).
  if (RE_TOKEN_SUELTO.test(body)) {
    const t = await manejarToken(p, body, msg);
    // Si no es un token conocido, se trata como texto normal.
    if (!(t && !t.ok && t.motivo === "TOKEN_INVALIDO")) return respuestaToken(t);
  }

  if (r.tipo === "SUGERIR") {
    const opciones = r.candidatos.map((c) => ({
      id: `CMD:${c.id}:${c.argumentos}`,
      titulo: COMANDOS.find((d) => d.id === c.id)!.titulo,
    }));
    if (opciones.length < 3) opciones.push({ id: "MENU", titulo: "Menú principal" });
    return botonesLibres(`🤔 No reconocí «${r.escrito}». ¿Quisiste decir…?`, opciones);
  }

  return await menuPrincipal(p, `Hola, ${trato(p)}. Sistema FireStation en línea.`);
}

// -----------------------------------------------------------------------------
// Menús numerados y acciones
// -----------------------------------------------------------------------------
const FILAS_POR_PAGINA = 9; // + "Ver más…" = 10, el máximo de una lista de WhatsApp
const MAX_CUERPO_INTERACTIVO = 1024;
const MINUTOS_MENU = 15;

async function guardarMenu(p: PersonalRow, clave: string, acciones: string[]): Promise<void> {
  const { error } = await supabase.from("bot_menu_estado").upsert({
    telefono_whatsapp: p.telefono_whatsapp,
    personal_id: p.id,
    menu: clave.slice(0, 60),
    opciones: acciones,
    expires_at: new Date(Date.now() + MINUTOS_MENU * 60_000).toISOString(),
  });
  if (error) console.error(`[whatsapp-bot] No se pudo guardar el menú: ${error.code} ${error.message}`);
}

async function opcionDeMenu(p: PersonalRow, n: number): Promise<string | null> {
  const { data, error } = await supabase
    .from("bot_menu_estado")
    .select("opciones, expires_at")
    .eq("telefono_whatsapp", p.telefono_whatsapp)
    .maybeSingle<{ opciones: string[]; expires_at: string }>();
  if (error) console.error(`[whatsapp-bot] Error leyendo bot_menu_estado: ${error.code} ${error.message}`);
  if (!data || new Date(data.expires_at) < new Date()) return null;
  return data.opciones[n - 1] ?? null;
}

async function borrarMenu(p: PersonalRow): Promise<void> {
  await supabase.from("bot_menu_estado").delete().eq("telefono_whatsapp", p.telefono_whatsapp);
}

/**
 * Menú interactivo estándar: lista de WhatsApp + el mismo listado numerado en
 * el texto. `clave` es la acción que regenera este menú (para paginar).
 */
async function menu(
  p: PersonalRow,
  clave: string,
  encabezado: string,
  opciones: Opcion[],
  desde = 0,
): Promise<Respuesta> {
  if (!opciones.length) return texto(encabezado);
  let visibles = opciones;
  if (opciones.length > 10) {
    visibles = opciones.slice(desde, desde + FILAS_POR_PAGINA);
    if (desde + FILAS_POR_PAGINA < opciones.length) {
      visibles = [...visibles, {
        accion: `${clave}@${desde + FILAS_POR_PAGINA}`,
        titulo: "Ver más…",
        descripcion: `${opciones.length - desde - FILAS_POR_PAGINA} más`,
      }];
    }
  }
  await guardarMenu(p, clave, visibles.map((o) => o.accion));
  const cuerpo = (conDescripcion: boolean) =>
    `${encabezado}\n\n` +
    visibles
      .map((o, i) => `*${i + 1}.* ${o.titulo}${conDescripcion && o.descripcion ? ` — ${o.descripcion}` : ""}`)
      .join("\n") +
    "\n\nRespondé con el *número* o tocá *Ver opciones*. *0* = menú principal.";
  // El cuerpo de un mensaje interactivo admite hasta 1024 caracteres.
  const completo = cuerpo(true);
  if (completo.length <= MAX_CUERPO_INTERACTIVO) return lista(completo, visibles);
  const corto = cuerpo(false);
  if (corto.length <= MAX_CUERPO_INTERACTIVO) return lista(corto, visibles);
  return [texto(completo), lista("Elegí una opción:", visibles)];
}

const ES_ACCION = /^(MENU|AYU|INV|MOV|GUA|LEG|CMD)(:|@|$)/;

async function ejecutarAccion(p: PersonalRow, accion: string, msg: WaMessage): Promise<Respuesta> {
  console.log(`[whatsapp-bot] Acción ${accion}`);
  if (accion.startsWith("CMD:")) {
    const [, id, ...resto] = accion.split(":");
    return await ejecutarComando(p, id as IdComando, resto.join(":"), msg);
  }
  const [base, pagina] = accion.split("@");
  const desde = Math.max(0, Number(pagina ?? 0) || 0);
  const partes = base.split(":");
  switch (partes[0]) {
    case "MENU":
      return await menuPrincipal(p);
    case "AYU":
      return texto(MSG_AYUDA);
    case "INV":
      return await accionInventario(p, partes.slice(1), desde);
    case "MOV":
      return await accionMoviles(p, partes.slice(1), desde);
    case "GUA":
      return await accionGuardia(p, partes.slice(1));
    case "LEG":
      return await accionLegajo(p, partes.slice(1), desde);
  }
  return await menuPrincipal(p);
}

/** Saca del texto los separadores de acciones (':' '@') para poder guardarlo en un id. */
const limpiarArg = (s: string) => s.replace(/[:@]/g, " ").replace(/\s+/g, " ").trim().slice(0, 60);

async function ejecutarComando(p: PersonalRow, id: IdComando, args: string, _msg: WaMessage): Promise<Respuesta> {
  const a = args.trim();
  switch (id) {
    case "MENU":
      return await menuPrincipal(p);
    case "INVENTARIO":
      return a ? await accionInventario(p, ["Q", limpiarArg(a)], 0) : await accionInventario(p, [], 0);
    case "MOVILES":
      return a ? await accionMoviles(p, ["Q", limpiarArg(a)], 0) : await accionMoviles(p, [], 0);
    case "GUARDIA":
      return await accionGuardia(p, []);
    case "LEGAJO":
      return a ? await accionLegajo(p, ["Q", limpiarArg(a)], 0) : await accionLegajo(p, [], 0);
    case "VIAJE":
      return (await comandosDeViaje(p, `viaje ${a}`)) ?? texto(textoMotivo({ motivo: "DATOS_INCOMPLETOS" }));
    case "REGRESO":
      return (await comandosDeViaje(p, a ? `regreso ${a}` : "regreso")) ??
        texto(textoMotivo({ motivo: "HORA_INVALIDA" }));
    case "CANCELAR":
      await borrarMenu(p);
      return texto("👌 Listo, cancelado. Enviá *MENU* cuando necesites algo.");
  }
  return await menuPrincipal(p);
}

async function menuPrincipal(p: PersonalRow, encabezado?: string): Promise<Respuesta> {
  const leg = autorizadoLegajos(p)
    ? { accion: "LEG", titulo: "Legajos", descripcion: "Fichas, DNI y certificados del personal" }
    : { accion: "LEG", titulo: "Mi legajo", descripcion: "Tu ficha, DNI y certificados" };
  return await menu(p, "MENU", `${encabezado ? encabezado + "\n" : ""}📋 *Menú principal*`, [
    { accion: "INV", titulo: "Inventario", descripcion: "Estado de equipos y búsqueda" },
    { accion: "MOV", titulo: "Móviles", descripcion: "Estado de la flota" },
    { accion: "GUA", titulo: "Estado de guardia", descripcion: "Presentes y viajes en curso" },
    leg,
    { accion: "AYU", titulo: "Fichar y viajes", descripcion: "Ubicación, códigos QR, VIAJE y REGRESO" },
  ]);
}

// --- Inventario ----------------------------------------------------------------
interface ItemInventario {
  id: string;
  codigo_qr: string;
  nombre: string;
  categoria: string;
  ubicacion: string;
  estado: string;
  marca_modelo: string | null;
  numero_serie: string | null;
  observaciones: string | null;
  motivo_estado: string | null;
}

const CATEGORIA_INV: Record<string, string> = {
  VEHICULO: "Vehículos",
  HERRAMIENTA: "Herramientas",
  PROTECCION_RESPIRATORIA: "Protección respiratoria",
  EPP: "EPP",
  COMUNICACIONES: "Comunicaciones",
  RESCATE: "Rescate",
  MEDICO: "Médico",
  OTRO: "Otros",
};
const ESTADO_INV: Record<string, string> = {
  OPERATIVO: "✅ Operativo",
  EN_REPARACION: "🛠️ En reparación",
  FUERA_DE_SERVICIO: "⛔ Fuera de servicio",
};

async function inventario(): Promise<ItemInventario[] | null> {
  const { data, error } = await supabase
    .from("inventario_cuartel")
    .select("id, codigo_qr, nombre, categoria, ubicacion, estado, marca_modelo, numero_serie, observaciones, motivo_estado")
    .eq("activo", true)
    .order("nombre");
  if (error) {
    console.error(`[whatsapp-bot] Error leyendo inventario_cuartel: ${error.code} ${error.message}`);
    return null;
  }
  return data as ItemInventario[];
}

const filaItem = (i: ItemInventario): Opcion => ({
  accion: `INV:IT:${i.id}`,
  titulo: i.nombre,
  descripcion: `${ESTADO_INV[i.estado] ?? i.estado} · ${i.ubicacion}`,
});

function detalleItem(i: ItemInventario): Contenido {
  const lineas = [
    `🧰 *${i.nombre}*  (${i.codigo_qr})`,
    `Estado: ${ESTADO_INV[i.estado] ?? i.estado}${i.motivo_estado ? ` — ${i.motivo_estado}` : ""}`,
    `Categoría: ${CATEGORIA_INV[i.categoria] ?? i.categoria}`,
    `Ubicación: ${i.ubicacion}`,
  ];
  if (i.marca_modelo) lineas.push(`Marca / modelo: ${i.marca_modelo}`);
  if (i.numero_serie) lineas.push(`N° de serie: ${i.numero_serie}`);
  if (i.observaciones) lineas.push(`Obs.: ${i.observaciones}`);
  return texto(lineas.join("\n"));
}

async function accionInventario(p: PersonalRow, partes: string[], desde: number): Promise<Respuesta> {
  const [sub, arg] = partes;
  if (!sub) {
    return await menu(p, "INV", "🧰 *Inventario del cuartel*", [
      { accion: "INV:RES", titulo: "Resumen por estado" },
      { accion: "INV:NOOP", titulo: "Equipos no operativos", descripcion: "En reparación o fuera de servicio" },
      { accion: "INV:CATS", titulo: "Ver por categoría" },
      { accion: "INV:BUS", titulo: "Buscar un equipo", descripcion: "Por nombre o código QR" },
    ]);
  }
  if (sub === "BUS") {
    return texto("🔎 Enviá *INVENTARIO* seguido del nombre o código.\nEj.: *INVENTARIO motosierra* o *INVENTARIO FS-1A2B3C4D*.");
  }
  const items = await inventario();
  if (!items) return texto(MSG_ERROR);
  if (!items.length) return texto("🧰 El inventario todavía no tiene equipos cargados.");

  switch (sub) {
    case "RES": {
      const por = (e: string) => items.filter((i) => i.estado === e).length;
      return texto(
        `🧰 *Inventario* — ${items.length} equipos activos\n` +
          Object.entries(ESTADO_INV).map(([k, v]) => `${v}: ${por(k)}`).join("\n") +
          "\n\nEnviá *INVENTARIO* para ver más opciones.",
      );
    }
    case "NOOP": {
      const noop = items.filter((i) => i.estado !== "OPERATIVO");
      if (!noop.length) return texto("✅ Todos los equipos están operativos.");
      return await menu(p, "INV:NOOP", `⚠️ *Equipos no operativos* (${noop.length})`, noop.map(filaItem), desde);
    }
    case "CATS": {
      const cats = Object.keys(CATEGORIA_INV).filter((c) => items.some((i) => i.categoria === c));
      return await menu(
        p,
        "INV:CATS",
        "🗂️ *Inventario por categoría*",
        cats.map((c) => {
          const de = items.filter((i) => i.categoria === c);
          const mal = de.filter((i) => i.estado !== "OPERATIVO").length;
          return {
            accion: `INV:CAT:${c}`,
            titulo: CATEGORIA_INV[c],
            descripcion: `${de.length} equipos${mal ? ` · ${mal} no operativos` : ""}`,
          };
        }),
      );
    }
    case "CAT": {
      const de = items.filter((i) => i.categoria === arg);
      return await menu(p, `INV:CAT:${arg}`, `🗂️ *${CATEGORIA_INV[arg] ?? arg}* (${de.length})`, de.map(filaItem), desde);
    }
    case "IT": {
      const item = items.find((i) => i.id === arg);
      return item ? detalleItem(item) : texto("❌ Ese equipo ya no está en el inventario.");
    }
    case "Q": {
      const q = arg ?? "";
      const exacto = items.find((i) => normalizar(i.codigo_qr) === normalizar(q));
      if (exacto) return detalleItem(exacto);
      const hallados = items
        .map((i) => ({
          i,
          s: Math.max(
            puntajeBusqueda(q, i.nombre),
            puntajeBusqueda(q, i.marca_modelo ?? ""),
            puntajeBusqueda(q, i.ubicacion) * 0.95,
          ),
        }))
        .filter((x) => x.s >= 0.75)
        .sort((a, b) => b.s - a.s)
        .map((x) => x.i);
      if (!hallados.length) return texto(`🔎 No encontré equipos parecidos a «${q}». Enviá *INVENTARIO* para ver el menú.`);
      if (hallados.length === 1) return detalleItem(hallados[0]);
      return await menu(p, `INV:Q:${q}`, `🔎 Resultados para «${q}» (${hallados.length})`, hallados.map(filaItem), desde);
    }
  }
  return await accionInventario(p, [], 0);
}

// --- Móviles -------------------------------------------------------------------
interface Movil {
  movil_id: string;
  nombre_interno: string | null;
  tipo_unidad: string | null;
  estado_operativo: string;
  km_actual: number | null;
  taller_actual: string | null;
  motivo_fuera_servicio: string | null;
  updated_at: string;
}

const ESTADO_MOVIL: Record<string, string> = {
  EN_SERVICIO: "✅ En servicio",
  CON_OBSERVACIONES: "⚠️ Con observaciones",
  EN_TALLER_FS: "🛠️ En taller / fuera de servicio",
  EN_EMERGENCIA: "🚨 En emergencia",
};
const compacto = (s: string) => normalizar(s).replace(/[\s\-.]/g, "");

async function accionMoviles(p: PersonalRow, partes: string[], desde: number): Promise<Respuesta> {
  const [sub, arg] = partes;
  const { data, error } = await supabase.from("flota_moviles").select("*").order("movil_id");
  if (error) {
    console.error(`[whatsapp-bot] Error leyendo flota_moviles: ${error.code} ${error.message}`);
    return texto(MSG_ERROR);
  }
  const flota = data as Movil[];
  if (!flota.length) return texto("🚒 Todavía no hay móviles cargados en la flota.");

  if (sub === "U" || sub === "Q") {
    let m = flota.find((x) => compacto(x.movil_id) === compacto(arg ?? ""));
    if (!m && sub === "Q") {
      const hallados = flota
        .map((x) => ({
          x,
          s: Math.max(puntajeBusqueda(arg ?? "", x.nombre_interno ?? ""), puntajeBusqueda(arg ?? "", x.tipo_unidad ?? "")),
        }))
        .filter((h) => h.s >= 0.75)
        .sort((a, b) => b.s - a.s);
      if (hallados.length === 1) m = hallados[0].x;
      else if (hallados.length > 1) {
        return await menu(p, `MOV:Q:${arg}`, `🔎 Móviles parecidos a «${arg}»`, hallados.map((h) => filaMovil(h.x)), desde);
      }
    }
    if (!m) return texto(`❌ No encontré el móvil «${arg}». Enviá *MOVILES* para ver la flota.`);
    return await detalleMovil(m);
  }

  if (sub === "RES") {
    const por = (e: string) => flota.filter((x) => x.estado_operativo === e).length;
    return texto(
      `🚒 *Flota* — ${flota.length} móviles\n` +
        Object.entries(ESTADO_MOVIL).map(([k, v]) => `${v}: ${por(k)}`).join("\n"),
    );
  }

  return await menu(
    p,
    "MOV",
    "🚒 *Móviles* — elegí uno para ver el detalle",
    [{ accion: "MOV:RES", titulo: "Resumen de la flota" }, ...flota.map(filaMovil)],
    desde,
  );
}

function filaMovil(m: Movil): Opcion {
  return {
    accion: `MOV:U:${m.movil_id}`,
    titulo: `${m.movil_id}${m.nombre_interno ? ` ${m.nombre_interno}` : ""}`,
    descripcion: ESTADO_MOVIL[m.estado_operativo] ?? m.estado_operativo,
  };
}

async function detalleMovil(m: Movil): Promise<Contenido> {
  const lineas = [
    `🚒 *${m.movil_id}*${m.nombre_interno ? ` — ${m.nombre_interno}` : ""}`,
    `Estado: ${ESTADO_MOVIL[m.estado_operativo] ?? m.estado_operativo}`,
  ];
  if (m.tipo_unidad) lineas.push(`Tipo: ${m.tipo_unidad}`);
  if (m.km_actual != null) lineas.push(`Km: ${m.km_actual.toLocaleString("es-AR")}`);
  if (m.taller_actual) lineas.push(`Taller: ${m.taller_actual}`);
  if (m.motivo_fuera_servicio) lineas.push(`Motivo: ${m.motivo_fuera_servicio}`);
  const { data: viajes } = await supabase
    .from("viajes_activos")
    .select("destino, vehiculo, fecha_salida")
    .eq("estado", "EN_VIAJE");
  const enViaje = (viajes ?? []).find((v) => compacto(v.vehiculo ?? "") === compacto(m.movil_id));
  if (enViaje) lineas.push(`🛣️ En viaje hacia ${enViaje.destino} desde ${horaLocal(new Date(enViaje.fecha_salida))}`);
  lineas.push(`Actualizado: ${horaLocal(new Date(m.updated_at))}`);
  return texto(lineas.join("\n"));
}

// --- Estado de guardia ---------------------------------------------------------
interface RegistroBitacora {
  tipo_registro: string;
  bombero: string;
  jerarquia: string | null;
  actividad: string;
  destino: string | null;
  vehiculo: string | null;
  inicio: string;
}

async function accionGuardia(p: PersonalRow, partes: string[]): Promise<Respuesta> {
  const [sub] = partes;
  if (!sub) {
    return await menu(p, "GUA", `🛡️ *Estado de guardia* — ${horaLocal()}`, [
      { accion: "GUA:RES", titulo: "Resumen", descripcion: "Presentes, viajes y unidades fuera de servicio" },
      { accion: "GUA:PRES", titulo: "Presentes en el cuartel" },
      { accion: "GUA:VIA", titulo: "Viajes en curso" },
    ]);
  }
  const { data, error } = await supabase
    .from("libro_guardia_bitacora")
    .select("tipo_registro, bombero, jerarquia, actividad, destino, vehiculo, inicio")
    .eq("estado", "EN_CURSO")
    .order("inicio");
  if (error) {
    console.error(`[whatsapp-bot] Error leyendo libro_guardia_bitacora: ${error.code} ${error.message}`);
    return texto(MSG_ERROR);
  }
  const regs = data as RegistroBitacora[];
  const presentes = regs.filter((r) => r.tipo_registro === "ASISTENCIA");
  const viajes = regs.filter((r) => r.tipo_registro === "VIAJE");
  const nombre = (r: RegistroBitacora) => `${r.jerarquia ? capitalizar(r.jerarquia) + " " : ""}${r.bombero}`;
  const listaPresentes = presentes.length
    ? presentes.map((r) => `• ${nombre(r)} — ${r.actividad} (desde ${soloHora(new Date(r.inicio))})`).join("\n")
    : "Nadie fichado en este momento.";
  const listaViajes = viajes.length
    ? viajes.map((r) => `• ${nombre(r)} → ${r.destino} en ${r.vehiculo} (salida ${horaLocal(new Date(r.inicio))})`)
      .join("\n")
    : "No hay viajes en curso.";

  if (sub === "PRES") return texto(`👥 *Presentes en el cuartel* (${presentes.length})\n${listaPresentes}`);
  if (sub === "VIA") return texto(`🛣️ *Viajes en curso* (${viajes.length})\n${listaViajes}`);

  const [{ data: flota }, items] = await Promise.all([
    supabase.from("flota_moviles").select("movil_id, estado_operativo"),
    inventario(),
  ]);
  const fueraFlota = (flota ?? []).filter((m) => m.estado_operativo !== "EN_SERVICIO");
  const noop = (items ?? []).filter((i) => i.estado !== "OPERATIVO").length;
  return texto(
    `🛡️ *Estado de guardia* — ${horaLocal()}\n\n` +
      `👥 Presentes: ${presentes.length}\n${listaPresentes}\n\n` +
      `🛣️ Viajes en curso: ${viajes.length}\n${listaViajes}\n\n` +
      `🚒 Móviles no disponibles: ${fueraFlota.length}` +
      (fueraFlota.length
        ? "\n" + fueraFlota.map((m) => `• ${m.movil_id}: ${ESTADO_MOVIL[m.estado_operativo] ?? m.estado_operativo}`)
          .join("\n")
        : "") +
      `\n🧰 Equipos no operativos: ${noop}`,
  );
}

// --- Legajos digitales -----------------------------------------------------------
interface PersonaLegajo {
  id: number;
  nombre_completo: string;
  jerarquia: string | null;
  legajo: string | null;
  dni: string | null;
  rol: string;
  activo: boolean;
}

interface DocLegajo {
  id: number;
  tipo_documento: string;
  categoria: string;
  nombre_archivo: string;
  storage_path: string;
  tamanio_bytes: number | null;
  fecha_subida: string;
}

const BUCKET_LEGAJOS = "legajos";
const CATEGORIA_LEG: Record<string, string> = {
  DNI: "DNI",
  CURSOS: "Certificados de cursos",
  LIBRETA: "Libreta operativa",
  ADMINISTRATIVO: "Administrativos",
  OTROS: "Otros documentos",
};
const MAX_DOCS_POR_ENVIO = 10;
const MAX_BYTES_ZIP = 60 * 1024 * 1024;
const SEGUNDOS_ENLACE = 3600;

/** ADMIN y JEFATURA ven cualquier legajo (igual que en la app de escritorio). */
const autorizadoLegajos = (p: PersonalRow) => p.rol === "ADMIN" || p.rol === "JEFATURA";
const puedeVer = (p: PersonalRow, personaId: number) => autorizadoLegajos(p) || p.id === personaId;

async function auditar(p: PersonalRow, accion: string, personaId: number | null, detalle?: string): Promise<void> {
  const { error } = await supabase.from("legajo_accesos_bot").insert({
    consultante_id: p.id,
    legajo_personal_id: personaId,
    accion,
    detalle: detalle?.slice(0, 500) ?? null,
  });
  if (error) console.error(`[whatsapp-bot] No se pudo auditar el acceso: ${error.code} ${error.message}`);
}

const MSG_LEGAJO_DENEGADO = "🔒 Solo ADMIN o Jefatura pueden consultar legajos de otras personas.\n" +
  "Enviá *LEGAJO* para ver el tuyo.";

async function persona(id: number): Promise<PersonaLegajo | null> {
  const { data, error } = await supabase
    .from("personal_cloud")
    .select("id, nombre_completo, jerarquia, legajo, dni, rol, activo")
    .eq("id", id)
    .maybeSingle<PersonaLegajo>();
  if (error) console.error(`[whatsapp-bot] Error leyendo personal_cloud: ${error.code} ${error.message}`);
  return data;
}

async function documentosDe(personaId: number, categoria?: string): Promise<DocLegajo[] | null> {
  let q = supabase
    .from("legajo_documentos")
    .select("id, tipo_documento, categoria, nombre_archivo, storage_path, tamanio_bytes, fecha_subida")
    .eq("personal_id", personaId)
    .order("fecha_subida", { ascending: false });
  if (categoria) q = q.eq("categoria", categoria);
  const { data, error } = await q;
  if (error) {
    console.error(`[whatsapp-bot] Error leyendo legajo_documentos: ${error.code} ${error.message}`);
    return null;
  }
  return data as DocLegajo[];
}

const nombrePersona = (x: PersonaLegajo) =>
  `${x.jerarquia ? capitalizar(x.jerarquia) + " " : ""}${capitalizar(x.nombre_completo)}`;

async function accionLegajo(p: PersonalRow, partes: string[], desde: number): Promise<Respuesta> {
  const [sub, a1, a2] = partes;

  if (!sub) {
    if (!autorizadoLegajos(p)) return await menuLegajoPersona(p, p.id);
    return await menu(p, "LEG", "🗂️ *Legajos digitales*", [
      { accion: "LEG:BUS", titulo: "Buscar un legajo", descripcion: "Por DNI, N° de legajo o apellido" },
      { accion: `LEG:P:${p.id}`, titulo: "Mi legajo" },
    ]);
  }
  if (sub === "BUS") {
    return texto("🔎 Enviá *LEGAJO* seguido del DNI, el N° de legajo o el apellido.\n" +
      "Ej.: *LEGAJO 30123456*, *LEGAJO 59-027* o *LEGAJO Pérez*.");
  }
  if (sub === "Q") return await buscarLegajo(p, a1 ?? "", desde);

  const id = Number(a1);
  if (!Number.isInteger(id)) return await accionLegajo(p, [], 0);
  if (!puedeVer(p, id)) {
    await auditar(p, "DENEGADO", id, partes.join(":"));
    return texto(MSG_LEGAJO_DENEGADO);
  }
  switch (sub) {
    case "P":
      return await menuLegajoPersona(p, id);
    case "F":
      return await fichaLegajo(p, id);
    case "C":
      return await enviarCategoria(p, id, a2);
    case "Z":
      return await enviarZip(p, id);
  }
  return await menuLegajoPersona(p, id);
}

async function buscarLegajo(p: PersonalRow, q: string, desde: number): Promise<Respuesta> {
  const { data, error } = await supabase
    .from("personal_cloud")
    .select("id, nombre_completo, jerarquia, legajo, dni, rol, activo")
    .order("nombre_completo");
  if (error) {
    console.error(`[whatsapp-bot] Error buscando legajos: ${error.code} ${error.message}`);
    return texto(MSG_ERROR);
  }
  const padron = data as PersonaLegajo[];
  const digitos = q.replace(/\D/g, "");
  let hallados: PersonaLegajo[];
  if (digitos.length >= 6 && /^[\d.\s]+$/.test(q)) {
    hallados = padron.filter((x) => (x.dni ?? "").replace(/\D/g, "") === digitos);
  } else {
    const porLegajo = padron.filter((x) => x.legajo && compacto(x.legajo) === compacto(q));
    hallados = porLegajo.length ? porLegajo : padron
      .map((x) => ({
        x,
        s: Math.max(
          puntajeBusqueda(q, x.nombre_completo.split(",")[0]),
          puntajeBusqueda(q, x.nombre_completo) * 0.95,
        ),
      }))
      .filter((h) => h.s >= 0.8)
      .sort((a, b) => b.s - a.s)
      .map((h) => h.x);
  }

  // Un BOMBERO solo puede llegar a su propio legajo (no se revela quién más coincide).
  if (!autorizadoLegajos(p)) {
    await auditar(p, "BUSQUEDA", null, q);
    if (hallados.some((x) => x.id === p.id)) return await menuLegajoPersona(p, p.id);
    return texto(MSG_LEGAJO_DENEGADO);
  }
  await auditar(p, "BUSQUEDA", hallados.length === 1 ? hallados[0].id : null, `${q} -> ${hallados.length}`);
  if (!hallados.length) return texto(`🔎 No encontré legajos para «${q}». Probá con el DNI o el apellido.`);
  if (hallados.length === 1) return await menuLegajoPersona(p, hallados[0].id);
  return await menu(
    p,
    `LEG:Q:${q}`,
    `🔎 Legajos para «${q}» (${hallados.length})`,
    hallados.map((x) => ({
      accion: `LEG:P:${x.id}`,
      titulo: capitalizar(x.nombre_completo),
      descripcion: [x.jerarquia && capitalizar(x.jerarquia), x.legajo && `Leg. ${x.legajo}`, !x.activo && "INACTIVO"]
        .filter(Boolean).join(" · "),
    })),
    desde,
  );
}

async function menuLegajoPersona(p: PersonalRow, id: number): Promise<Respuesta> {
  const [x, docs] = await Promise.all([persona(id), documentosDe(id)]);
  if (!x) return texto("❌ No encontré ese legajo.");
  if (!docs) return texto(MSG_ERROR);
  await auditar(p, "VER", id);
  const opciones: Opcion[] = [{ accion: `LEG:F:${id}`, titulo: "Ficha personal", descripcion: "Datos del legajo" }];
  for (const [cat, etiqueta] of Object.entries(CATEGORIA_LEG)) {
    const n = docs.filter((d) => d.categoria === cat).length;
    if (n) opciones.push({ accion: `LEG:C:${id}:${cat}`, titulo: etiqueta, descripcion: `${n} documento(s)` });
  }
  if (docs.length) {
    opciones.push({ accion: `LEG:Z:${id}`, titulo: "Paquete completo (ZIP)", descripcion: `${docs.length} documentos` });
  }
  const encabezado = `🗂️ *Legajo de ${nombrePersona(x)}*${x.legajo ? ` — N° ${x.legajo}` : ""}` +
    (docs.length ? "\n¿Qué querés recibir?" : "\nTodavía no tiene documentos digitalizados en la nube.");
  return await menu(p, `LEG:P:${id}`, encabezado, opciones);
}

async function fichaLegajo(p: PersonalRow, id: number): Promise<Contenido> {
  const [x, docs] = await Promise.all([persona(id), documentosDe(id)]);
  if (!x) return texto("❌ No encontré ese legajo.");
  await auditar(p, "FICHA", id);
  const conteo = Object.entries(CATEGORIA_LEG)
    .map(([cat, et]) => [et, (docs ?? []).filter((d) => d.categoria === cat).length] as const)
    .filter(([, n]) => n > 0)
    .map(([et, n]) => `  • ${et}: ${n}`);
  return texto(
    [
      `🪪 *Ficha personal*`,
      `Nombre: ${capitalizar(x.nombre_completo)}`,
      `Jerarquía: ${x.jerarquia ? capitalizar(x.jerarquia) : "—"}`,
      `N° de legajo: ${x.legajo ?? "—"}`,
      `DNI: ${x.dni ?? "—"}`,
      `Rol en el sistema: ${capitalizar(x.rol)}`,
      `Estado: ${x.activo ? "Activo" : "Inactivo"}`,
      `Documentos digitalizados: ${(docs ?? []).length}`,
      ...conteo,
    ].join("\n"),
  );
}

async function enlaceFirmado(ruta: string): Promise<string | null> {
  const { data, error } = await supabase.storage.from(BUCKET_LEGAJOS).createSignedUrl(ruta, SEGUNDOS_ENLACE);
  if (error) console.error(`[whatsapp-bot] No se pudo firmar ${ruta}: ${error.message}`);
  return data?.signedUrl ?? null;
}

async function enviarCategoria(p: PersonalRow, id: number, categoria: string | undefined): Promise<Respuesta> {
  if (!categoria || !CATEGORIA_LEG[categoria]) return await menuLegajoPersona(p, id);
  const [x, docs] = await Promise.all([persona(id), documentosDe(id, categoria)]);
  if (!x || !docs) return texto(MSG_ERROR);
  if (!docs.length) return texto(`ℹ️ No hay documentos de ${CATEGORIA_LEG[categoria]} en este legajo.`);
  await auditar(p, "CATEGORIA", id, `${categoria} (${docs.length})`);

  const salida: Contenido[] = [];
  for (const d of docs.slice(0, MAX_DOCS_POR_ENVIO)) {
    const link = await enlaceFirmado(d.storage_path);
    if (link) salida.push(documento(link, d.nombre_archivo, `${d.tipo_documento} — ${capitalizar(x.nombre_completo)}`));
  }
  if (!salida.length) return texto(MSG_ERROR);
  const extra = docs.length > MAX_DOCS_POR_ENVIO
    ? ` Se envían los ${MAX_DOCS_POR_ENVIO} más recientes; pedí el *paquete completo (ZIP)* para el resto.`
    : "";
  return [
    texto(`📎 ${CATEGORIA_LEG[categoria]} de ${nombrePersona(x)}: ${docs.length} documento(s).${extra}`),
    ...salida,
  ];
}

async function enviarZip(p: PersonalRow, id: number): Promise<Respuesta> {
  const [x, docs] = await Promise.all([persona(id), documentosDe(id)]);
  if (!x || !docs) return texto(MSG_ERROR);
  if (!docs.length) return texto("ℹ️ Este legajo no tiene documentos digitalizados.");
  const total = docs.reduce((s, d) => s + (d.tamanio_bytes ?? 0), 0);
  if (total > MAX_BYTES_ZIP) {
    return texto("⚠️ El legajo completo supera los 60 MB. Pedí los documentos por categoría desde el menú del legajo.");
  }

  const zip = new JSZip();
  const usados = new Set<string>();
  const faltantes: string[] = [];
  for (const d of docs) {
    const { data, error } = await supabase.storage.from(BUCKET_LEGAJOS).download(d.storage_path);
    if (error || !data) {
      console.error(`[whatsapp-bot] No se pudo descargar ${d.storage_path}: ${error?.message}`);
      faltantes.push(d.nombre_archivo);
      continue;
    }
    let nombre = `${CATEGORIA_LEG[d.categoria] ?? "Otros documentos"}/${d.nombre_archivo}`;
    if (usados.has(nombre)) nombre = `${CATEGORIA_LEG[d.categoria] ?? "Otros documentos"}/${d.id}_${d.nombre_archivo}`;
    usados.add(nombre);
    zip.file(nombre, new Uint8Array(await data.arrayBuffer()));
  }
  if (!usados.size) return texto(MSG_ERROR);
  const bytes = await zip.generateAsync({ type: "uint8array", compression: "DEFLATE" });

  // Un único paquete vigente por persona: se borran los anteriores.
  const carpeta = `_paquetes/${id}`;
  const { data: viejos } = await supabase.storage.from(BUCKET_LEGAJOS).list(carpeta);
  if (viejos?.length) {
    await supabase.storage.from(BUCKET_LEGAJOS).remove(viejos.map((o) => `${carpeta}/${o.name}`));
  }
  const apellido = normalizar(x.nombre_completo.split(",")[0]).replace(/[^a-z0-9]+/g, "_");
  const nombreZip = `Legajo_${apellido}${x.legajo ? `_${x.legajo.replace(/[^A-Za-z0-9-]/g, "")}` : ""}.zip`;
  const ruta = `${carpeta}/${Date.now()}_${nombreZip}`;
  const { error: errSubida } = await supabase.storage.from(BUCKET_LEGAJOS)
    .upload(ruta, bytes, { contentType: "application/zip", upsert: true });
  if (errSubida) {
    console.error(`[whatsapp-bot] No se pudo subir el ZIP: ${errSubida.message}`);
    return texto(MSG_ERROR);
  }
  const link = await enlaceFirmado(ruta);
  if (!link) return texto(MSG_ERROR);
  await auditar(p, "ZIP", id, `${usados.size} archivos, ${bytes.length} bytes`);
  const aviso = faltantes.length ? `\n⚠️ No se pudieron incluir: ${faltantes.join(", ")}.` : "";
  return documento(
    link,
    nombreZip,
    `🗜️ Legajo completo de ${nombrePersona(x)}: ${usados.size} documento(s). Enlace válido por 1 h.${aviso}`,
  );
}

// -----------------------------------------------------------------------------
// POST: mensajes entrantes
// -----------------------------------------------------------------------------
async function procesarWebhook(raw: string, firma: string | null): Promise<void> {
  try {
    if (!APP_SECRET) {
      console.warn("[whatsapp-bot] WHATSAPP_APP_SECRET no configurado: firma sin validar");
    } else if (!(await firmaValida(raw, firma))) {
      console.warn("[whatsapp-bot] Firma X-Hub-Signature-256 inválida: payload descartado");
      return;
    }

    const payload = JSON.parse(raw) as WaWebhookPayload;
    const msg = payload.entry?.[0]?.changes?.[0]?.value?.messages?.[0];

    // Notificaciones de estado (sent/delivered/read) u otros eventos sin mensaje.
    if (!msg) {
      console.log("[whatsapp-bot] Evento sin mensaje (statuses u otro): ignorado");
      return;
    }

    const from = msg.from;
    const resumen = extraerTexto(msg);
    const { data, error } = await buscarPersonal(from);

    if (error) {
      // Error de base: no se responde "denegado" a alguien que quizá sí está en el padrón.
      console.error(`[whatsapp-bot] Error consultando personal_cloud: ${error.code} ${error.message}`);
      return;
    }

    if (!data) {
      console.log(`[whatsapp-bot] RECHAZADO ${mask(`+${from}`)}: no pertenece al cuartel | texto="${resumen}"`);
      await enviar(from, texto(MSG_DENEGADO));
      return;
    }
    if (!data.activo) {
      console.log(
        `[whatsapp-bot] RECHAZADO ${mask(`+${from}`)}: ${data.nombre_completo} está INACTIVO (rol ${data.rol})`,
      );
      await enviar(from, texto(MSG_DENEGADO));
      return;
    }

    console.log(
      `[whatsapp-bot] AUTORIZADO ${mask(`+${from}`)}: ${data.jerarquia ?? ""} ${data.nombre_completo} ` +
        `(id ${data.id}, rol ${data.rol}) | tipo=${msg.type} texto="${resumen}"`,
    );

    let respuesta: Respuesta;
    const idInteractivo = msg.type === "interactive"
      ? (msg.interactive?.button_reply?.id ?? msg.interactive?.list_reply?.id)
      : undefined;
    if (msg.type === "location" && msg.location) {
      respuesta = await manejarUbicacion(data, msg);
    } else if (idInteractivo && ES_ACCION.test(idInteractivo)) {
      respuesta = await ejecutarAccion(data, idInteractivo, msg); // menús y sugerencias
    } else if (msg.type === "interactive" && msg.interactive?.button_reply) {
      respuesta = await manejarBoton(data, msg.interactive.button_reply.id); // actividad del fichaje
    } else if (msg.type === "text") {
      respuesta = await manejarTexto(data, msg);
    } else {
      respuesta = await menuPrincipal(data, `Hola, ${trato(data)}. Sistema FireStation en línea.`);
    }
    for (const c of Array.isArray(respuesta) ? respuesta : [respuesta]) await enviar(from, c);
  } catch (err) {
    console.error("[whatsapp-bot] Error procesando el webhook:", err instanceof Error ? err.message : err);
  }
}

async function handleMessage(req: Request): Promise<Response> {
  let raw = "";
  try {
    raw = await req.text();
    console.log(`[whatsapp-bot] POST recibido (${raw.length} bytes, UA=${req.headers.get("user-agent") ?? "?"})`);
  } catch (err) {
    console.error("[whatsapp-bot] No se pudo leer el body:", err instanceof Error ? err.message : err);
    return ok();
  }

  // Se procesa todo (consulta + envío a Meta, ~1-3 s) ANTES de responder: así el
  // runtime no puede cortar trabajo pendiente. Meta tolera esa latencia.
  // procesarWebhook nunca lanza (try/catch interno), así que el 200 está garantizado.
  await procesarWebhook(raw, req.headers.get("x-hub-signature-256"));

  return ok();
}

// -----------------------------------------------------------------------------
// Entry point
// -----------------------------------------------------------------------------
Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  if (req.method === "GET") return handleVerify(url);
  if (req.method === "POST") return await handleMessage(req);
  return new Response("Method Not Allowed", { status: 405 });
});

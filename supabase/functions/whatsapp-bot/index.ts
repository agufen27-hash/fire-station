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
//             text "REGRESO HH:MM" / "HH:MM" -> rpc_cerrar_viaje (día anclado a la salida)
//             text "SI" / "REGRESO" con viaje abierto -> pide la hora de llegada
//             text normal         -> saludo + ayuda
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
type Contenido = Record<string, any>; // parte "type + {text|interactive}" del mensaje saliente

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
  "🏁 Para marcar el regreso: *REGRESO 18:30*.";
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

function saludo(p: PersonalRow): string {
  return `Hola, ${trato(p)}. Sistema FireStation en línea. ¿En qué te puedo ayudar?\n\n${MSG_AYUDA}`;
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
      return `❌ La llegada no puede ser anterior a la salida (${horaLocal(new Date(r.fecha_salida))}). ` +
        `El día queda fijo en el de salida (${diaLocal(new Date(r.fecha_salida))}); indicá una hora posterior.`;
    case "LLEGADA_EN_EL_FUTURO":
      return "❌ Esa hora todavía no pasó. Indicá la hora real de llegada.";
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

async function manejarTexto(p: PersonalRow, msg: WaMessage): Promise<Contenido> {
  const body = (msg.text?.body ?? "").trim();

  const qr = RE_QR.exec(body);
  if (qr) return respuestaToken(await manejarToken(p, qr[1], msg));

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
          `El día queda fijo en ${diaLocal(salida)} (salida ${soloHora(salida)}); solo se puede elegir la hora.`,
      );
    }
    if (!/^s[ií]$/i.test(sinHora[1])) return texto(textoMotivo({ motivo: "SIN_VIAJE_ABIERTO" }));
  }

  if (RE_TOKEN_SUELTO.test(body)) {
    const r = await manejarToken(p, body, msg);
    // Si no es un token conocido, se trata como texto normal.
    if (!(r && !r.ok && r.motivo === "TOKEN_INVALIDO")) return respuestaToken(r);
  }

  return texto(saludo(p));
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

    let respuesta: Contenido;
    if (msg.type === "location" && msg.location) {
      respuesta = await manejarUbicacion(data, msg);
    } else if (msg.type === "interactive" && msg.interactive?.button_reply) {
      respuesta = await manejarBoton(data, msg.interactive.button_reply.id);
    } else if (msg.type === "text") {
      respuesta = await manejarTexto(data, msg);
    } else {
      respuesta = texto(saludo(data));
    }
    await enviar(from, respuesta);
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

// =============================================================================
// Fire Station · Edge Function `whatsapp-bot` (Fase 3)
//
// Webhook de WhatsApp Cloud API (Meta):
//   GET  -> verificación del webhook (hub.mode / hub.verify_token / hub.challenge)
//   POST -> recepción de mensajes; valida el remitente contra public.personal_cloud
//
// Variables de entorno:
//   WHATSAPP_VERIFY_TOKEN      (secret propio)  token elegido al configurar el webhook en Meta
//   WHATSAPP_APP_SECRET        (secret propio, opcional) App Secret de Meta para validar
//                              la firma X-Hub-Signature-256. Si no está, se loguea un aviso.
//   SUPABASE_URL               (inyectada por Supabase)
//   SUPABASE_SERVICE_ROLE_KEY  (inyectada por Supabase)
//
// Por qué service_role y no anon: personal_cloud tiene RLS activado SIN políticas
// y los privilegios de anon/authenticated están revocados (ver migraciones), así
// que con la anon key la consulta devolvería siempre 0 filas / 42501.
//
// La función debe desplegarse con --no-verify-jwt: Meta no envía un JWT de Supabase.
// =============================================================================

import { createClient } from "npm:@supabase/supabase-js@2";

const VERIFY_TOKEN = Deno.env.get("WHATSAPP_VERIFY_TOKEN") ?? "";
const APP_SECRET = Deno.env.get("WHATSAPP_APP_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

// -----------------------------------------------------------------------------
// Tipos mínimos del payload de Meta
// -----------------------------------------------------------------------------
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
  location?: { latitude: number; longitude: number };
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
}

// -----------------------------------------------------------------------------
// Utilidades
// -----------------------------------------------------------------------------
const ok = (body = "EVENT_RECEIVED") =>
  new Response(body, { status: 200, headers: { "Content-Type": "text/plain" } });

/** Enmascara el teléfono en los logs: +54935812****. */
const mask = (tel: string) => (tel.length > 4 ? tel.slice(0, -4) + "****" : "****");

/**
 * Meta envía `from` sin '+' (ej. '5493581234567'). En la base está en E.164
 * ('+549358...'). Para Argentina se prueban ambas variantes (con y sin el 9
 * de celular) por si el padrón se cargó de una u otra forma.
 */
function candidatosTelefono(from: string): string[] {
  const digits = from.replace(/\D/g, "");
  const set = new Set<string>([`+${digits}`]);
  if (digits.startsWith("549")) set.add(`+54${digits.slice(3)}`);
  else if (digits.startsWith("54")) set.add(`+549${digits.slice(2)}`);
  return [...set];
}

/** Texto legible del mensaje según su tipo. */
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
// POST: mensajes entrantes
// -----------------------------------------------------------------------------
async function handleMessage(req: Request): Promise<Response> {
  try {
    const raw = await req.text();

    if (!APP_SECRET) {
      console.warn("[whatsapp-bot] WHATSAPP_APP_SECRET no configurado: firma sin validar");
    } else if (!(await firmaValida(raw, req.headers.get("x-hub-signature-256")))) {
      console.warn("[whatsapp-bot] Firma X-Hub-Signature-256 inválida: payload descartado");
      return ok();
    }

    const payload = JSON.parse(raw) as WaWebhookPayload;
    const msg = payload.entry?.[0]?.changes?.[0]?.value?.messages?.[0];

    if (!msg) {
      // Notificaciones de estado (sent/delivered/read) u otros eventos sin mensaje.
      return ok();
    }

    const from = msg.from;
    const texto = extraerTexto(msg);
    const candidatos = candidatosTelefono(from);

    const { data, error } = await supabase
      .from("personal_cloud")
      .select("id, nombre_completo, jerarquia, rol, activo")
      .in("telefono_whatsapp", candidatos)
      .limit(1)
      .maybeSingle<PersonalRow>();

    if (error) {
      console.error(`[whatsapp-bot] Error consultando personal_cloud: ${error.code} ${error.message}`);
      return ok();
    }

    if (!data) {
      console.log(`[whatsapp-bot] RECHAZADO ${mask(`+${from}`)}: no pertenece al cuartel | texto="${texto}"`);
    } else if (!data.activo) {
      console.log(
        `[whatsapp-bot] RECHAZADO ${mask(`+${from}`)}: ${data.nombre_completo} está INACTIVO (rol ${data.rol})`,
      );
    } else {
      console.log(
        `[whatsapp-bot] AUTORIZADO ${mask(`+${from}`)}: ${data.jerarquia ?? ""} ${data.nombre_completo} ` +
          `(id ${data.id}, rol ${data.rol}) | tipo=${msg.type} texto="${texto}"`,
      );
    }
  } catch (err) {
    console.error("[whatsapp-bot] Error procesando el webhook:", err instanceof Error ? err.message : err);
  }

  // SIEMPRE 200 para que Meta no reintente.
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

// =============================================================================
// Fire Station · Edge Function `despachador-notificaciones`
//
// Vacía la cola public.notificaciones_salientes, que llena la función SQL
// notificar_canales_externos() (triggers de viajes_activos / salidas_emergencia
// y el watchdog de 24 h). La invocan pg_net (al encolar) y pg_cron (reintentos).
//
// Cada fila se envía por su canal, todas EN PARALELO:
//   WHATSAPP -> Graph API (texto; o plantilla si WA_PLANTILLA_<EVENTO> está definida)
//   ALERTHOR -> POST JSON a ALERTHOR_API_URL con Bearer ALERTHOR_TOKEN.
//               Sin ALERTHOR_API_URL la fila queda OMITIDO (pasarela no activada).
//
// No recibe datos del request: solo procesa lo que ya está en la cola, así que
// una invocación externa no puede inyectar mensajes. Por eso verify_jwt=false
// (pg_net no tiene un JWT a mano).
//
// Variables de entorno:
//   WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_ID      (mismos secrets que whatsapp-bot)
//   ALERTHOR_API_URL, ALERTHOR_TOKEN              (opcionales: pasarela Alerthor)
//   WA_PLANTILLA_VIAJE_SALIDA, WA_PLANTILLA_SINIESTRO, WA_PLANTILLA_RECORDATORIO_VIAJE
//                                                 (opcionales: nombre de plantilla aprobada
//                                                  en Meta; parámetros {{1}}..{{n}} = payload.parametros)
//   WA_PLANTILLA_IDIOMA                           (opcional, default "es_AR")
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY       (inyectadas por Supabase)
// =============================================================================

import { createClient } from "npm:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const ACCESS_TOKEN = Deno.env.get("WHATSAPP_ACCESS_TOKEN") ?? "";
const PHONE_ID = Deno.env.get("WHATSAPP_PHONE_ID") ?? "";
const ALERTHOR_API_URL = Deno.env.get("ALERTHOR_API_URL") ?? "";
const ALERTHOR_TOKEN = Deno.env.get("ALERTHOR_TOKEN") ?? "";
const IDIOMA_PLANTILLA = Deno.env.get("WA_PLANTILLA_IDIOMA") ?? "es_AR";
const GRAPH_URL = "https://graph.facebook.com/v26.0";
const LOTE = 25;
const MAX_LOTES = 4;

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

interface Notificacion {
  id: string;
  evento: "VIAJE_SALIDA" | "SINIESTRO" | "RECORDATORIO_VIAJE";
  referencia_id: string;
  canal: "WHATSAPP" | "ALERTHOR";
  destino_tipo: "NUMERO" | "GRUPO" | null;
  destino: string | null;
  mensaje: string;
  // deno-lint-ignore no-explicit-any
  payload: Record<string, any>;
  intentos: number;
}

interface Resultado {
  estado: "ENVIADO" | "ERROR" | "OMITIDO";
  error?: string;
  respuesta?: unknown;
}

const mask = (tel: string) => (tel.length > 4 ? tel.slice(0, -4) + "****" : "****");

// -----------------------------------------------------------------------------
// WhatsApp
// -----------------------------------------------------------------------------
/** Mismos formatos que whatsapp-bot: 549..., 54..., 54+área+15+número. */
function formatosDestino(to: string): string[] {
  const d = to.replace(/\D/g, "");
  if (!d.startsWith("549") || d.length !== 13) return [d];
  const nacional = d.slice(3);
  return [d, `54${nacional}`, `54${nacional.slice(0, 3)}15${nacional.slice(3)}`];
}
const ERRORES_FORMATO = new Set([131030, 100]);

// deno-lint-ignore no-explicit-any
function contenidoWhatsApp(n: Notificacion): Record<string, any> {
  const plantilla = Deno.env.get(`WA_PLANTILLA_${n.evento}`);
  if (plantilla) {
    const params: string[] = Array.isArray(n.payload?.parametros) ? n.payload.parametros : [];
    return {
      type: "template",
      template: {
        name: plantilla,
        language: { code: IDIOMA_PLANTILLA },
        components: params.length
          ? [{ type: "body", parameters: params.map((t) => ({ type: "text", text: String(t) })) }]
          : [],
      },
    };
  }
  return { type: "text", text: { preview_url: false, body: n.mensaje } };
}

async function enviarWhatsApp(n: Notificacion): Promise<Resultado> {
  if (!ACCESS_TOKEN || !PHONE_ID) return { estado: "ERROR", error: "Faltan WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_ID" };
  if (!n.destino) return { estado: "OMITIDO", error: "Sin destino" };

  const esGrupo = n.destino_tipo === "GRUPO";
  const destinos = esGrupo ? [n.destino] : formatosDestino(n.destino);
  const contenido = contenidoWhatsApp(n);
  let ultimo = "";

  for (let i = 0; i < destinos.length; i++) {
    const res = await fetch(`${GRAPH_URL}/${PHONE_ID}/messages`, {
      method: "POST",
      headers: { Authorization: `Bearer ${ACCESS_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        messaging_product: "whatsapp",
        recipient_type: esGrupo ? "group" : "individual",
        to: destinos[i],
        ...contenido,
      }),
      signal: AbortSignal.timeout(10_000),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) return { estado: "ENVIADO", respuesta: { wamid: data?.messages?.[0]?.id, formato: i + 1 } };

    const e = data?.error ?? {};
    ultimo = `Graph ${res.status} code=${e.code} ${e.message ?? ""}`.trim();
    console.error(`[despachador] WhatsApp ${n.evento} -> ${esGrupo ? "grupo" : mask(destinos[i])} ` +
      `(formato ${i + 1}/${destinos.length}): ${ultimo}`);
    if (!ERRORES_FORMATO.has(e.code)) break;
  }
  return { estado: "ERROR", error: ultimo };
}

// -----------------------------------------------------------------------------
// Alerthor (pasarela externa). El JSON es el payload armado en SQL:
//   { evento, referencia_id, mensaje, parametros, datos, emitido_at }
// -----------------------------------------------------------------------------
async function enviarAlerthor(n: Notificacion): Promise<Resultado> {
  if (!ALERTHOR_API_URL) return { estado: "OMITIDO", error: "ALERTHOR_API_URL no configurado (pasarela inactiva)" };
  const res = await fetch(ALERTHOR_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(ALERTHOR_TOKEN ? { Authorization: `Bearer ${ALERTHOR_TOKEN}` } : {}),
      "Idempotency-Key": n.id,
    },
    body: JSON.stringify({ origen: "firestation", notificacion_id: n.id, ...n.payload }),
    signal: AbortSignal.timeout(10_000),
  });
  const cuerpo = (await res.text().catch(() => "")).slice(0, 500);
  if (res.ok) return { estado: "ENVIADO", respuesta: { status: res.status, cuerpo } };
  return { estado: "ERROR", error: `Alerthor ${res.status}: ${cuerpo}` };
}

// -----------------------------------------------------------------------------
// Cola
// -----------------------------------------------------------------------------
async function procesar(n: Notificacion): Promise<Resultado["estado"]> {
  let r: Resultado;
  try {
    r = n.canal === "ALERTHOR" ? await enviarAlerthor(n) : await enviarWhatsApp(n);
  } catch (err) {
    r = { estado: "ERROR", error: err instanceof Error ? err.message : String(err) };
  }
  const { error } = await supabase
    .from("notificaciones_salientes")
    .update({
      estado: r.estado,
      ultimo_error: r.error ?? null,
      respuesta: r.respuesta ?? null,
      enviado_at: r.estado === "ENVIADO" ? new Date().toISOString() : null,
    })
    .eq("id", n.id);
  if (error) console.error(`[despachador] No se pudo actualizar ${n.id}: ${error.message}`);
  console.log(`[despachador] ${n.evento}/${n.canal} ${n.id} -> ${r.estado}${r.error ? ` (${r.error})` : ""}`);
  return r.estado;
}

async function vaciarCola() {
  const totales = { ENVIADO: 0, ERROR: 0, OMITIDO: 0 };
  for (let lote = 0; lote < MAX_LOTES; lote++) {
    const { data, error } = await supabase.rpc("rpc_tomar_notificaciones", { p_limite: LOTE });
    if (error) {
      console.error(`[despachador] rpc_tomar_notificaciones falló: ${error.code} ${error.message}`);
      break;
    }
    const filas = (data ?? []) as Notificacion[];
    if (!filas.length) break;
    // WhatsApp y Alerthor en paralelo: un canal caído no demora al otro.
    const estados = await Promise.allSettled(filas.map(procesar));
    for (const e of estados) if (e.status === "fulfilled") totales[e.value]++;
    if (filas.length < LOTE) break;
  }
  return totales;
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return new Response("Method Not Allowed", { status: 405 });
  await req.body?.cancel(); // el body se ignora a propósito
  const totales = await vaciarCola();
  return new Response(JSON.stringify({ ok: true, ...totales }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

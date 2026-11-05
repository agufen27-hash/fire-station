// =============================================================================
// Motor de comandos del bot: normalización + coincidencia aproximada.
//
// Módulo PURO (sin Deno ni Supabase) para poder probarlo con Node:
//   node supabase/functions/whatsapp-bot/comandos.test.ts
//
// Similitud = máximo entre
//   * ratio de difflib.SequenceMatcher (Ratcliff/Obershelp, port de Python), y
//   * 1 - distancia de Damerau-Levenshtein / largo: cuenta una transposición
//     ("invnetario") como UN error, que difflib penaliza de más.
// =============================================================================

/** Minúsculas, sin tildes ni signos (¿?¡!*), espacios simples. Conserva ':' '-' '.' (horas, QR, M-12). */
export function normalizar(s: string): string {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9ñ:.\-\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// --- difflib.SequenceMatcher.ratio() -----------------------------------------
function bloquesCoincidentes(a: string, b: string): number {
  const b2j = new Map<string, number[]>();
  for (let j = 0; j < b.length; j++) {
    const l = b2j.get(b[j]);
    if (l) l.push(j);
    else b2j.set(b[j], [j]);
  }
  const masLarga = (alo: number, ahi: number, blo: number, bhi: number): [number, number, number] => {
    let bi = alo, bj = blo, size = 0;
    let j2len = new Map<number, number>();
    for (let i = alo; i < ahi; i++) {
      const nuevo = new Map<number, number>();
      for (const j of b2j.get(a[i]) ?? []) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) ?? 0) + 1;
        nuevo.set(j, k);
        if (k > size) {
          bi = i - k + 1;
          bj = j - k + 1;
          size = k;
        }
      }
      j2len = nuevo;
    }
    return [bi, bj, size];
  };
  let total = 0;
  const cola: Array<[number, number, number, number]> = [[0, a.length, 0, b.length]];
  while (cola.length) {
    const [alo, ahi, blo, bhi] = cola.pop()!;
    const [i, j, k] = masLarga(alo, ahi, blo, bhi);
    if (k === 0) continue;
    total += k;
    if (alo < i && blo < j) cola.push([alo, i, blo, j]);
    if (i + k < ahi && j + k < bhi) cola.push([i + k, ahi, j + k, bhi]);
  }
  return total;
}

export function ratioDifflib(a: string, b: string): number {
  const n = a.length + b.length;
  return n === 0 ? 1 : (2 * bloquesCoincidentes(a, b)) / n;
}

// --- Damerau-Levenshtein (alineación óptima de cadenas) ----------------------
export function distanciaDamerau(a: string, b: string): number {
  const d: number[][] = Array.from({ length: a.length + 1 }, (_, i) =>
    Array.from({ length: b.length + 1 }, (_, j) => (i === 0 ? j : j === 0 ? i : 0)));
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      const costo = a[i - 1] === b[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + costo);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
      }
    }
  }
  return d[a.length][b.length];
}

export function similitud(a: string, b: string): number {
  if (a === b) return 1;
  const largo = Math.max(a.length, b.length);
  if (largo === 0) return 1;
  return Math.max(ratioDifflib(a, b), 1 - distanciaDamerau(a, b) / largo);
}

// --- Catálogo de comandos ----------------------------------------------------
export type IdComando =
  | "MENU"
  | "INVENTARIO"
  | "MOVILES"
  | "GUARDIA"
  | "LEGAJO"
  | "VIAJE"
  | "REGRESO"
  | "CANCELAR";

export interface DefComando {
  id: IdComando;
  titulo: string; // título del botón de sugerencia (≤ 20 caracteres)
  palabras: string[]; // formas aceptadas, ya normalizadas (pueden tener varias palabras)
}

export const COMANDOS: DefComando[] = [
  { id: "MENU", titulo: "Menú principal", palabras: ["menu", "ayuda", "opciones", "inicio", "comandos", "help"] },
  { id: "INVENTARIO", titulo: "Inventario", palabras: ["inventario", "equipos", "materiales", "herramientas"] },
  { id: "MOVILES", titulo: "Móviles", palabras: ["moviles", "movil", "unidades", "unidad", "flota", "vehiculos"] },
  {
    id: "GUARDIA",
    titulo: "Estado de guardia",
    palabras: ["guardia", "estado de guardia", "estado guardia", "presentes", "quien esta"],
  },
  { id: "LEGAJO", titulo: "Legajos", palabras: ["legajo", "legajos", "ficha", "documentos"] },
  { id: "VIAJE", titulo: "Registrar viaje", palabras: ["viaje"] },
  { id: "REGRESO", titulo: "Marcar regreso", palabras: ["regreso", "llegada", "llegue", "volvi"] },
  { id: "CANCELAR", titulo: "Cancelar", palabras: ["cancelar", "cancela", "salir"] },
];

/** ≥ UMBRAL_EJECUTAR: se ejecuta directo. Entre UMBRAL_SUGERIR y UMBRAL_EJECUTAR: se sugiere. */
export const UMBRAL_EJECUTAR = 0.8; // = 1 error en una palabra de 5 letras
export const UMBRAL_SUGERIR = 0.6;
/** Palabras más cortas que esto solo valen escritas exactas (evita "si" -> "salir"). */
const LARGO_MINIMO_FUZZY = 4;

export interface Candidato {
  id: IdComando;
  puntaje: number;
  palabras: number; // cuántas palabras del mensaje forman el comando
  argumentos: string; // resto del mensaje, con el texto ORIGINAL (mayúsculas, tildes)
}

export type Resolucion =
  | { tipo: "EJECUTAR"; comando: Candidato }
  | { tipo: "SUGERIR"; candidatos: Candidato[]; escrito: string }
  | { tipo: "NINGUNO" };

/**
 * Interpreta las primeras 1-3 palabras del mensaje como comando y el resto
 * como argumentos ("invnetario motosierra" -> INVENTARIO + "motosierra").
 */
export function resolverComando(texto: string): Resolucion {
  // Tokens originales con su forma normalizada (se descartan los que quedan vacíos, ej. "¿").
  const tokens = texto.trim().split(/\s+/)
    .map((orig) => ({ orig, norm: normalizar(orig) }))
    .filter((t) => t.norm.length > 0);
  if (!tokens.length) return { tipo: "NINGUNO" };

  const mejores = new Map<IdComando, Candidato>();
  for (let n = 1; n <= Math.min(3, tokens.length); n++) {
    const frase = tokens.slice(0, n).map((t) => t.norm).join(" ");
    const argumentos = tokens.slice(n).map((t) => t.orig).join(" ");
    for (const def of COMANDOS) {
      for (const palabra of def.palabras) {
        const exacta = frase === palabra;
        if (!exacta && (frase.length < LARGO_MINIMO_FUZZY || palabra.length < LARGO_MINIMO_FUZZY)) continue;
        // Solo comparar frases de igual cantidad de palabras: "estado de" no debe parecerse a "estado".
        if (palabra.split(" ").length !== n) continue;
        const puntaje = exacta ? 1 : similitud(frase, palabra);
        const previo = mejores.get(def.id);
        if (!previo || puntaje > previo.puntaje || (puntaje === previo.puntaje && n > previo.palabras)) {
          mejores.set(def.id, { id: def.id, puntaje, palabras: n, argumentos });
        }
      }
    }
  }

  const orden = [...mejores.values()].sort((a, b) => b.puntaje - a.puntaje);
  const top = orden[0];
  if (!top || top.puntaje < UMBRAL_SUGERIR) return { tipo: "NINGUNO" };
  // Ejecuta si supera el umbral y no hay otro comando casi igual de parecido (ambigüedad).
  const segundo = orden[1];
  const ambiguo = segundo && top.puntaje < 1 && top.puntaje - segundo.puntaje < 0.05;
  if (top.puntaje >= UMBRAL_EJECUTAR && !ambiguo) return { tipo: "EJECUTAR", comando: top };
  return {
    tipo: "SUGERIR",
    candidatos: orden.filter((c) => c.puntaje >= UMBRAL_SUGERIR).slice(0, 3),
    escrito: tokens.slice(0, top.palabras).map((t) => t.orig).join(" "),
  };
}

/** Mejor puntaje de `consulta` contra cualquier palabra (o frase completa) de `objetivo`. */
export function puntajeBusqueda(consulta: string, objetivo: string): number {
  const q = normalizar(consulta);
  const o = normalizar(objetivo);
  if (!q || !o) return 0;
  if (o.includes(q)) return 1;
  let mejor = similitud(q, o);
  for (const palabra of o.split(/[\s,.\-]+/)) {
    if (palabra.length >= 3) mejor = Math.max(mejor, similitud(q, palabra));
  }
  return mejor;
}

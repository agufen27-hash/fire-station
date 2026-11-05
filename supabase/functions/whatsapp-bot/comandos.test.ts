// Pruebas del motor de comandos (sin dependencias):
//   node supabase/functions/whatsapp-bot/comandos.test.ts
// (Node ≥ 22.18 / 23.6 ejecuta TypeScript directamente; en Deno: deno run comandos.test.ts)
import assert from "node:assert/strict";
import { normalizar, puntajeBusqueda, ratioDifflib, resolverComando, similitud } from "./comandos.ts";

let fallas = 0;
function caso(nombre: string, fn: () => void) {
  try {
    fn();
    console.log(`ok   ${nombre}`);
  } catch (e) {
    fallas++;
    console.error(`FAIL ${nombre}\n     ${(e as Error).message}`);
  }
}

const ejecuta = (texto: string, id: string, args = "") => {
  const r = resolverComando(texto);
  assert.equal(r.tipo, "EJECUTAR", `"${texto}" -> ${JSON.stringify(r)}`);
  if (r.tipo === "EJECUTAR") {
    assert.equal(r.comando.id, id, `"${texto}"`);
    assert.equal(r.comando.argumentos, args, `"${texto}" argumentos`);
  }
};
const sugiere = (texto: string, id: string) => {
  const r = resolverComando(texto);
  assert.equal(r.tipo, "SUGERIR", `"${texto}" -> ${JSON.stringify(r)}`);
  if (r.tipo === "SUGERIR") assert.ok(r.candidatos.some((c) => c.id === id), `"${texto}" sugiere ${id}`);
};
const ninguno = (texto: string) => assert.equal(resolverComando(texto).tipo, "NINGUNO", `"${texto}"`);

caso("normalizar quita tildes, mayúsculas, signos y espacios", () => {
  assert.equal(normalizar("  ¿Móviles   DISPONIBLES?! "), "moviles disponibles");
  assert.equal(normalizar("REGRESO 18:30"), "regreso 18:30");
  assert.equal(normalizar("Peñaloza"), "penaloza");
});

caso("ratio igual a difflib.SequenceMatcher", () => {
  // Valores de referencia calculados con Python: SequenceMatcher(None, a, b).ratio()
  assert.equal(ratioDifflib("inventario", "invetario").toFixed(4), "0.9474");
  assert.equal(ratioDifflib("abcd", "bcde").toFixed(4), "0.7500");
  assert.equal(ratioDifflib("legajo", "legajo"), 1);
});

caso("comandos exactos (con tildes / mayúsculas)", () => {
  ejecuta("INVENTARIO", "INVENTARIO");
  ejecuta("Móviles", "MOVILES");
  ejecuta("menú", "MENU");
  ejecuta("Estado de guardia", "GUARDIA");
  ejecuta("legajo 30.123.456", "LEGAJO", "30.123.456");
});

caso("faltas de letras, letras de más y teclas invertidas", () => {
  ejecuta("invetario", "INVENTARIO");
  ejecuta("invnetario", "INVENTARIO");
  ejecuta("inventarioo", "INVENTARIO");
  ejecuta("moviels", "MOVILES");
  ejecuta("gaurdia", "GUARDIA");
  ejecuta("gurda", "GUARDIA");
  ejecuta("lgeajo Pérez", "LEGAJO", "Pérez");
  ejecuta("legaj Perez", "LEGAJO", "Perez");
  ejecuta("estado de guadia", "GUARDIA");
});

caso("comandos anteriores con errores conservan los argumentos originales", () => {
  ejecuta("viajr Río Cuarto EN M-12", "VIAJE", "Río Cuarto EN M-12");
  ejecuta("regreos 18:30", "REGRESO", "18:30");
  ejecuta("Regresso 7:05", "REGRESO", "7:05");
});

caso("coincidencia intermedia -> sugerencia", () => {
  sugiere("invntr", "INVENTARIO");
  sugiere("guadr", "GUARDIA");
  sugiere("lejago", "LEGAJO");
});

caso("texto común no se confunde con un comando", () => {
  ninguno("hola");
  ninguno("buen día");
  ninguno("si");
  ninguno("gracias");
  ninguno("AB12CD");
});

caso("búsqueda aproximada para inventario / apellidos", () => {
  assert.ok(puntajeBusqueda("motosiera", "Motosierra Stihl MS 382") >= 0.85);
  assert.ok(puntajeBusqueda("gonzales", "GONZÁLEZ, Juan Pablo") >= 0.8);
  assert.ok(puntajeBusqueda("perez", "RODRÍGUEZ, Ana") < 0.7);
  assert.ok(similitud("perez", "peres") >= 0.8);
});

if (fallas) {
  console.error(`\n${fallas} caso(s) fallaron`);
  // deno-lint-ignore no-explicit-any
  (globalThis as any).process?.exit?.(1);
} else {
  console.log("\nTodos los casos pasaron");
}

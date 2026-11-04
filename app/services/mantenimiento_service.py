"""
Checklists y mantenimiento preventivo contra Supabase.

Objetos de Supabase (supabase/migrations/*mantenimiento*):
  * vw_mantenimiento_items         ítems con tareas + evaluación del motor
  * vw_mantenimiento_tareas_item   cada tarea aplicable a cada ítem, con su estado
  * vw_mantenimiento_historial     checklists realizados
  * mantenimiento_registro_tareas  detalle de cada checklist
  * vw_mantenimiento_tareas        plantillas
  * rpc_mantenimiento_registrar           cualquier integrante activo
  * rpc_mantenimiento_tarea_guardar / _desactivar   solo ADMIN

Motor de estado: vive en la base (mantenimiento_get_estado_general /
mantenimiento_aplicar_estado + pg_cron cada 15 min), así un vencimiento saca
de servicio un ítem aunque nadie tenga la app abierta. `get_estado_general`
de este módulo es la MISMA regla en Python: la UI la usa para anticipar, antes
de enviar un checklist, si el ítem va a quedar fuera de servicio.

  tarea CRÍTICA con FALLA o VENCIDA  -> FUERA_DE_SERVICIO
  otra tarea con FALLA o VENCIDA     -> CON_OBSERVACIONES
  si no                              -> OPERATIVO

Llamadas bloqueantes: la UI las corre en un TareaSupabase (QThread). Sin
conexión: las lecturas fallan con SupabaseSyncError (la UI conserva lo último
mostrado) y las escrituras no se pierden (el formulario queda abierto con lo
cargado para reintentar).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.core.roles import ROL_ADMIN, normalizar_rol
from app.services.libro_guardia_service import TZ_CUARTEL
from app.services.supabase_rest import ClienteSupabaseRest, SupabaseRechazo

DIARIA, SEMANAL, MENSUAL = "DIARIA", "SEMANAL", "MENSUAL"
FRECUENCIAS = {DIARIA: "Diarias", SEMANAL: "Semanales", MENSUAL: "Mensuales"}
AYUDA_FRECUENCIA = {
    DIARIA: "Parte de guardia / antes de salir",
    SEMANAL: "Mecánica y comunicaciones",
    MENSUAL: "Vencimientos, EPP y tubos de oxígeno",
}

# Estado de una tarea para un ítem (vw_mantenimiento_tareas_item.estado_tarea)
AL_DIA, POR_VENCER, PENDIENTE, VENCIDA, FALLA = "AL_DIA", "POR_VENCER", "PENDIENTE", "VENCIDA", "FALLA"
ESTADOS_TAREA = {AL_DIA: "Al día", POR_VENCER: "Por vencer", PENDIENTE: "Pendiente",
                 VENCIDA: "Vencida", FALLA: "Con falla"}
TONO_ESTADO_TAREA = {AL_DIA: "ok", POR_VENCER: "info", PENDIENTE: "neutro", VENCIDA: "error", FALLA: "error"}
GRAVEDAD = {AL_DIA: 0, POR_VENCER: 1, PENDIENTE: 2, VENCIDA: 3, FALLA: 4}
ESTADOS_MALOS = (VENCIDA, FALLA)

# Respuesta de cada tarea en un checklist
OK, FALLA_RESPUESTA, NO_APLICA = "OK", "FALLA", "NO_APLICA"
RESPUESTAS = {OK: "OK", FALLA_RESPUESTA: "Falla", NO_APLICA: "N/A"}

# Estado general (motor)
OPERATIVO, CON_OBSERVACIONES, FUERA_DE_SERVICIO = "OPERATIVO", "CON_OBSERVACIONES", "FUERA_DE_SERVICIO"
ESTADOS_GENERALES = {OPERATIVO: "Operativo", CON_OBSERVACIONES: "Con observaciones",
                     FUERA_DE_SERVICIO: "Fuera de servicio"}
TONO_ESTADO_GENERAL = {OPERATIVO: "ok", CON_OBSERVACIONES: "alerta", FUERA_DE_SERVICIO: "error"}

RESULTADOS = {"APROBADO": "Aprobado", "CON_OBSERVACIONES": "Con observaciones", "FALLIDO": "Fallido"}
TONO_RESULTADO = {"APROBADO": "ok", "CON_OBSERVACIONES": "alerta", "FALLIDO": "error"}

MOTIVOS = {
    "SIN_OPERADOR": "Identificate como operador (chip 👤 de la barra superior) para firmar la revisión.",
    "OPERADOR_DESCONOCIDO":
        "Tu usuario no figura (o está inactivo) en la nube: sincronizá el padrón "
        "(Personal y Unidades → ☁️ Sincronizar a Cloud).",
    "OPERADOR_NO_ADMIN":
        "La nube no te reconoce como Administrador. Si te asignaron el rol hace poco, sincronizá el padrón.",
    "FRECUENCIA_INVALIDA": "Cadencia inválida.",
    "ITEM_INEXISTENTE_O_DADO_DE_BAJA": "El ítem ya no existe o fue dado de baja. Actualizá la lista.",
    "RESPUESTAS_INVALIDAS": "Respuestas inválidas.",
    "SIN_TAREAS": "Ese ítem no tiene tareas para esta cadencia.",
    "TAREA_AJENA": "Las tareas cambiaron mientras completabas la revisión: cerrala y volvé a abrirla.",
    "RESPUESTAS_INCOMPLETAS": "Falta responder tareas: todas son obligatorias (OK, Falla o N/A).",
    "FALTA_OBSERVACION_FALLA": "Toda falla necesita una observación (qué se encontró).",
    "DATOS_INCOMPLETOS": "Faltan el título o la frecuencia de la tarea.",
    "ALCANCE_INVALIDO": "La tarea va asociada a UNA categoría o a UN ítem puntual.",
    "DATOS_INVALIDOS": "Algún dato de la tarea no es válido.",
    "TAREA_INEXISTENTE": "La tarea ya no existe o fue desactivada. Actualizá la lista.",
}


class MantenimientoError(SupabaseRechazo):
    """Operación de mantenimiento rechazada (permisos o datos)."""


def _fecha(texto: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(texto).astimezone(TZ_CUARTEL) if texto else None


# =============================================================================
# Modelos
# =============================================================================

@dataclass(frozen=True)
class ResumenFrecuencia:
    total: int = 0
    al_dia: int = 0
    por_vencer: int = 0
    pendientes: int = 0
    vencidas: int = 0
    fallas: int = 0
    criticas_mal: int = 0
    ultima_revision: Optional[datetime] = None
    proximo_vencimiento: Optional[datetime] = None

    @property
    def estado(self) -> str:
        """Estado de la cadencia para el ítem (el peor de sus tareas)."""
        if self.fallas:
            return FALLA
        if self.vencidas:
            return VENCIDA
        if self.pendientes:
            return PENDIENTE
        if self.por_vencer:
            return POR_VENCER
        return AL_DIA

    @classmethod
    def desde(cls, datos: Mapping[str, Any]) -> "ResumenFrecuencia":
        enteros = {k: int(datos.get(k) or 0) for k in
                   ("total", "al_dia", "por_vencer", "pendientes", "vencidas", "fallas", "criticas_mal")}
        return cls(**enteros, ultima_revision=_fecha(datos.get("ultima_revision")),
                   proximo_vencimiento=_fecha(datos.get("proximo_vencimiento")))


@dataclass(frozen=True)
class EstadoItem:
    id: str
    codigo_qr: str
    nombre: str
    categoria: str
    ubicacion: str
    estado: str                         # estado del inventario
    fuera_servicio_automatico: bool
    motivo_estado: Optional[str]
    estado_general: str                 # evaluación del motor
    motivos: List[Dict[str, Any]] = field(default_factory=list)
    por_frecuencia: Dict[str, ResumenFrecuencia] = field(default_factory=dict)

    def resumen(self, frecuencia: str) -> Optional[ResumenFrecuencia]:
        return self.por_frecuencia.get(frecuencia)


def estado_item_desde_fila(fila: Mapping[str, Any]) -> EstadoItem:
    return EstadoItem(
        id=str(fila["id"]), codigo_qr=fila.get("codigo_qr") or "", nombre=fila.get("nombre") or "",
        categoria=fila.get("categoria") or "OTRO", ubicacion=fila.get("ubicacion") or "",
        estado=fila.get("estado") or "OPERATIVO",
        fuera_servicio_automatico=bool(fila.get("fuera_servicio_automatico")),
        motivo_estado=fila.get("motivo_estado"),
        estado_general=fila.get("estado_general") or OPERATIVO,
        motivos=list(fila.get("motivos") or []),
        por_frecuencia={f: ResumenFrecuencia.desde(d) for f, d in (fila.get("por_frecuencia") or {}).items()},
    )


@dataclass(frozen=True)
class TareaItem:
    tarea_id: str
    frecuencia: str
    titulo: str
    descripcion: Optional[str]
    critica: bool
    orden: int
    estado_tarea: str
    ultimo_estado: Optional[str]
    ultima_fecha: Optional[datetime]
    ultimo_responsable: Optional[str]
    ultima_observacion: Optional[str]
    vence_at: Optional[datetime]


def tarea_item_desde_fila(fila: Mapping[str, Any]) -> TareaItem:
    return TareaItem(
        tarea_id=str(fila["tarea_id"]), frecuencia=fila["frecuencia"], titulo=fila.get("titulo") or "",
        descripcion=fila.get("descripcion"), critica=bool(fila.get("critica")), orden=int(fila.get("orden") or 0),
        estado_tarea=fila.get("estado_tarea") or PENDIENTE, ultimo_estado=fila.get("ultimo_estado"),
        ultima_fecha=_fecha(fila.get("ultima_fecha")), ultimo_responsable=fila.get("ultimo_responsable"),
        ultima_observacion=fila.get("ultima_observacion"), vence_at=_fecha(fila.get("vence_at")),
    )


@dataclass(frozen=True)
class RegistroHistorial:
    id: str
    item_id: str
    item: str
    codigo_qr: str
    frecuencia: str
    fecha_hora: datetime
    resultado: str
    observaciones: Optional[str]
    responsable: Optional[str]
    tareas: int
    fallas: int


def registro_desde_fila(fila: Mapping[str, Any]) -> RegistroHistorial:
    return RegistroHistorial(
        id=str(fila["id"]), item_id=str(fila["item_id"]), item=fila.get("item") or "",
        codigo_qr=fila.get("codigo_qr") or "", frecuencia=fila["frecuencia"], fecha_hora=_fecha(fila["fecha_hora"]),
        resultado=fila.get("resultado") or "APROBADO", observaciones=fila.get("observaciones"),
        responsable=fila.get("responsable"), tareas=int(fila.get("tareas") or 0), fallas=int(fila.get("fallas") or 0),
    )


@dataclass(frozen=True)
class TareaPlantilla:
    id: str
    titulo: str
    descripcion: Optional[str]
    frecuencia: str
    critica: bool
    categoria: Optional[str]
    item_id: Optional[str]
    item_nombre: Optional[str]
    orden: int
    activo: bool

    @property
    def alcance(self) -> str:
        if self.item_id:
            return f"Ítem: {self.item_nombre or self.item_id}"
        from app.services.inventario_service import CATEGORIAS

        return f"Categoría: {CATEGORIAS.get(self.categoria or '', self.categoria or '—')}"


def plantilla_desde_fila(fila: Mapping[str, Any]) -> TareaPlantilla:
    return TareaPlantilla(
        id=str(fila["id"]), titulo=fila.get("titulo") or "", descripcion=fila.get("descripcion"),
        frecuencia=fila["frecuencia"], critica=bool(fila.get("critica")), categoria=fila.get("categoria"),
        item_id=fila.get("item_id"), item_nombre=fila.get("item_nombre"), orden=int(fila.get("orden") or 100),
        activo=bool(fila.get("activo", True)),
    )


@dataclass(frozen=True)
class FichaActivo:
    """Lo que se abre al escanear un QR: el activo, su evaluación y sus tareas."""
    item: Any                            # app.services.inventario_service.ItemInventario
    estado: Optional[EstadoItem]         # None si el ítem no tiene tareas de mantenimiento
    tareas: List[TareaItem]

    def resumen(self, frecuencia: str) -> Optional[ResumenFrecuencia]:
        return self.estado.resumen(frecuencia) if self.estado else None

    @property
    def frecuencia_mas_urgente(self) -> Optional[str]:
        """La cadencia a revisar primero (falla > vencida > pendiente > por vencer)."""
        candidatas = [(GRAVEDAD[r.estado], f) for f in FRECUENCIAS if (r := self.resumen(f)) and r.estado != AL_DIA]
        return max(candidatas)[1] if candidatas else None


@dataclass(frozen=True)
class ResultadoChecklist:
    registro_id: str
    resultado: str
    estado_general: str
    estado_item: str
    cambio_estado: bool
    motivos: List[Dict[str, Any]]
    encolado: bool = False          # sin red: quedó en la cola local, se sincroniza solo


# =============================================================================
# Motor (espejo en Python de mantenimiento_get_estado_general)
# =============================================================================

def get_estado_general(tareas: Iterable[TareaItem]) -> str:
    """Misma regla que la base, sobre las tareas de un ítem."""
    tareas = list(tareas)
    if any(t.critica and t.estado_tarea in ESTADOS_MALOS for t in tareas):
        return FUERA_DE_SERVICIO
    if any(t.estado_tarea in ESTADOS_MALOS for t in tareas):
        return CON_OBSERVACIONES
    return OPERATIVO


def resultado_checklist(tareas: Iterable[TareaItem], respuestas: Mapping[str, str]) -> str:
    """APROBADO / CON_OBSERVACIONES / FALLIDO, como lo calcula la RPC."""
    criticas = {t.tarea_id for t in tareas if t.critica}
    fallas = {tid for tid, r in respuestas.items() if r == FALLA_RESPUESTA}
    if fallas & criticas:
        return "FALLIDO"
    return "CON_OBSERVACIONES" if fallas else "APROBADO"


def prever_estado_general(tareas_item: Iterable[TareaItem], respuestas: Mapping[str, str]) -> str:
    """Estado general que quedaría si se envía `respuestas` (tarea_id -> OK/FALLA/NO_APLICA)
    sobre las tareas del ítem: las respondidas pasan a AL_DIA o FALLA; el resto
    (otras cadencias) queda como está."""
    simuladas = []
    for t in tareas_item:
        respuesta = respuestas.get(t.tarea_id)
        if respuesta is None:
            simuladas.append(t)
        else:
            nuevo = FALLA if respuesta == FALLA_RESPUESTA else AL_DIA
            simuladas.append(replace(t, estado_tarea=nuevo))
    return get_estado_general(simuladas)


def exigir_operador(operador_id: Optional[int]) -> None:
    if operador_id is None:
        raise MantenimientoError(MOTIVOS["SIN_OPERADOR"])


def exigir_admin(rol: Optional[str], operador_id: Optional[int]) -> None:
    exigir_operador(operador_id)
    if normalizar_rol(rol) != ROL_ADMIN:
        raise MantenimientoError("Editar las plantillas de mantenimiento requiere rol Administrador.")


def validar_respuestas(tareas: Iterable[TareaItem], respuestas: Mapping[str, str],
                       observaciones: Mapping[str, str]) -> Optional[str]:
    """Validación local (la base vuelve a validar): texto del primer problema o None."""
    tareas = list(tareas)
    faltan = [t.titulo for t in tareas if respuestas.get(t.tarea_id) not in RESPUESTAS]
    if faltan:
        return f"Falta responder {len(faltan)} tarea(s): {faltan[0]}" + ("…" if len(faltan) > 1 else "")
    for t in tareas:
        if respuestas[t.tarea_id] == FALLA_RESPUESTA and not (observaciones.get(t.tarea_id) or "").strip():
            return f"Describí la falla de «{t.titulo}»."
    return None


# =============================================================================
# Cliente
# =============================================================================

class MantenimientoCliente(ClienteSupabaseRest):
    def _exigir_ok(self, resultado: Any) -> Dict[str, Any]:  # type: ignore[override]
        return ClienteSupabaseRest._exigir_ok(resultado, MOTIVOS, MantenimientoError)

    # -- Lecturas ----------------------------------------------------------------

    def items(self) -> List[EstadoItem]:
        filas = self._pedir("GET", "vw_mantenimiento_items", params={"select": "*", "order": "nombre.asc"}) or []
        return [estado_item_desde_fila(f) for f in filas]

    def tareas_item(self, item_id: str, frecuencia: Optional[str] = None) -> List[TareaItem]:
        params = {"select": "*", "item_id": f"eq.{item_id}", "order": "frecuencia.asc,orden.asc,titulo.asc"}
        if frecuencia:
            params["frecuencia"] = f"eq.{frecuencia}"
        return [tarea_item_desde_fila(f) for f in self._pedir("GET", "vw_mantenimiento_tareas_item", params=params) or []]

    def historial(self, item_id: Optional[str] = None, limite: int = 200) -> List[RegistroHistorial]:
        params = {"select": "*", "order": "fecha_hora.desc", "limit": str(limite)}
        if item_id:
            params["item_id"] = f"eq.{item_id}"
        return [registro_desde_fila(f) for f in self._pedir("GET", "vw_mantenimiento_historial", params=params) or []]

    def ficha_por_codigo(self, codigo: str) -> FichaActivo:
        """Busca el activo por su código QR (exacto, ya normalizado)."""
        from app.services.inventario_service import item_desde_fila

        filas = self._pedir("GET", "vw_inventario_cuartel", params={
            "select": "*", "codigo_qr": f"eq.{codigo}", "limit": "1"}) or []
        if not filas:
            raise MantenimientoError(f"No hay ningún activo con el código {codigo}.")
        item = item_desde_fila(filas[0])
        if not item.activo:
            raise MantenimientoError(f"El activo {codigo} («{item.nombre}») está dado de baja"
                                     + (f": {item.motivo_baja}" if item.motivo_baja else "."))
        estados = self._pedir("GET", "vw_mantenimiento_items", params={
            "select": "*", "id": f"eq.{item.id}", "limit": "1"}) or []
        return FichaActivo(item=item, estado=estado_item_desde_fila(estados[0]) if estados else None,
                           tareas=self.tareas_item(item.id))

    def detalle_registro(self, registro_id: str) -> List[Dict[str, Any]]:
        return self._pedir("GET", "mantenimiento_registro_tareas", params={
            "select": "titulo,critica,estado,observacion", "registro_id": f"eq.{registro_id}",
            "order": "critica.desc,titulo.asc"}) or []

    def plantillas(self, incluir_inactivas: bool = False) -> List[TareaPlantilla]:
        params = {"select": "*", "order": "frecuencia.asc,orden.asc,titulo.asc"}
        if not incluir_inactivas:
            params["activo"] = "is.true"
        return [plantilla_desde_fila(f) for f in self._pedir("GET", "vw_mantenimiento_tareas", params=params) or []]

    # -- Escrituras ----------------------------------------------------------------

    def registrar(self, operador_id: Optional[int], item_id: str, frecuencia: str,
                  respuestas: Mapping[str, str], observaciones_tareas: Mapping[str, str],
                  observaciones: str = "") -> ResultadoChecklist:
        exigir_operador(operador_id)
        cuerpo = [{"tarea_id": tid, "estado": estado,
                   "observacion": (observaciones_tareas.get(tid) or "").strip()}
                  for tid, estado in respuestas.items()]
        r = self._rpc_escritura("rpc_mantenimiento_registrar", {
            "p_operador_id": operador_id, "p_item_id": item_id, "p_frecuencia": frecuencia,
            "p_respuestas": cuerpo, "p_observaciones": observaciones.strip(),
            "p_fecha_hora": datetime.now(TZ_CUARTEL).isoformat(),   # hora real aunque se envíe después
            "p_cliente_id": str(uuid.uuid4()),
        }, f"revisión {FRECUENCIAS.get(frecuencia, frecuencia).lower()}")
        if r is None:
            fallas = any(e == FALLA_RESPUESTA for e in respuestas.values())
            return ResultadoChecklist(registro_id="", resultado="CON_OBSERVACIONES" if fallas else "APROBADO",
                                      estado_general="", estado_item="", cambio_estado=False, motivos=[],
                                      encolado=True)
        r = self._exigir_ok(r)
        return ResultadoChecklist(
            registro_id=str(r["registro_id"]), resultado=r["resultado"], estado_general=r["estado_general"],
            estado_item=r["estado_item"], cambio_estado=bool(r.get("cambio_estado")),
            motivos=list(r.get("motivos") or []))

    def guardar_plantilla(self, operador_id: Optional[int], rol: Optional[str], datos: Mapping[str, Any],
                          tarea_id: Optional[str] = None) -> TareaPlantilla:
        exigir_admin(rol, operador_id)
        r = self._exigir_ok(self._rpc("rpc_mantenimiento_tarea_guardar", {
            "p_operador_id": operador_id, "p_id": tarea_id, "p_datos": dict(datos)}))
        return plantilla_desde_fila(r["tarea"])

    def desactivar_plantilla(self, operador_id: Optional[int], rol: Optional[str], tarea_id: str) -> None:
        exigir_admin(rol, operador_id)
        self._exigir_ok(self._rpc("rpc_mantenimiento_tarea_desactivar",
                                  {"p_operador_id": operador_id, "p_id": tarea_id}))

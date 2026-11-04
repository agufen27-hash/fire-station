"""
Inventario del cuartel (ABM de activos) contra Supabase.

Objetos de Supabase (supabase/migrations/*inventario*):
  * tabla  inventario_cuartel        SOLO LECTURA para la app (service_role)
  * vista  vw_inventario_cuartel     + nombre de quién creó / modificó
  * rpc    rpc_inventario_guardar    alta (p_id null) o edición
  * rpc    rpc_inventario_baja       baja lógica con motivo

Permisos: escribir requiere rol ADMIN. Se controla en TRES lugares:
  1. la UI oculta los controles de gestión si el operador no es ADMIN,
  2. este servicio se niega antes de salir a la red (`exigir_admin`),
  3. la base vuelve a verificar que `p_operador_id` sea ADMIN y activo en
     personal_cloud (la tabla no acepta INSERT/UPDATE directos).
El rol de la nube es el del padrón sincronizado: si se nombra un ADMIN en
Configuración → Usuarios y Roles, hay que sincronizar el padrón.

Llamadas bloqueantes: la UI las corre en un TareaSupabase (QThread).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.roles import ROL_ADMIN, normalizar_rol
from app.services.libro_guardia_service import TZ_CUARTEL
from app.services.supabase_rest import ClienteSupabaseRest, SupabaseRechazo

VISTA = "vw_inventario_cuartel"

CATEGORIAS = {
    "VEHICULO": "Vehículo",
    "HERRAMIENTA": "Herramienta",
    "PROTECCION_RESPIRATORIA": "Protección respiratoria (ERA)",
    "EPP": "EPP / Indumentaria",
    "COMUNICACIONES": "Comunicaciones",
    "RESCATE": "Rescate",
    "MEDICO": "Material médico",
    "OTRO": "Otro",
}

ESTADO_OPERATIVO = "OPERATIVO"
ESTADO_EN_REPARACION = "EN_REPARACION"
ESTADO_FUERA_DE_SERVICIO = "FUERA_DE_SERVICIO"
ESTADOS = {
    ESTADO_OPERATIVO: "Operativo",
    ESTADO_EN_REPARACION: "En reparación",
    ESTADO_FUERA_DE_SERVICIO: "Fuera de servicio",
}

MOTIVOS = {
    "SIN_OPERADOR": "Identificate como operador (chip 👤 de la barra superior) con un usuario Administrador.",
    "OPERADOR_DESCONOCIDO":
        "Tu usuario no figura en la nube: sincronizá el padrón (Personal y Unidades → ☁️ Sincronizar a Cloud).",
    "OPERADOR_NO_ADMIN":
        "La nube no te reconoce como Administrador. Si te asignaron el rol hace poco, sincronizá el padrón "
        "(Personal y Unidades → ☁️ Sincronizar a Cloud).",
    "DATOS_INCOMPLETOS": "Faltan el nombre, la categoría o la ubicación.",
    "DATOS_INVALIDOS": "Algún dato no es válido (código QR: letras, números y guiones; máx. 40).",
    "CODIGO_QR_DUPLICADO": "Ya existe otro ítem con ese código QR.",
    "ITEM_INEXISTENTE_O_DADO_DE_BAJA": "El ítem ya no existe o fue dado de baja. Actualizá la lista.",
    "FALTA_MOTIVO_BAJA": "Indicá el motivo de la baja.",
}

CAMPOS_EDITABLES = ("codigo_qr", "nombre", "categoria", "ubicacion", "estado",
                    "marca_modelo", "numero_serie", "observaciones")


class InventarioError(SupabaseRechazo):
    """Operación de inventario rechazada (permisos o datos)."""


def exigir_admin(rol: Optional[str], operador_id: Optional[int]) -> None:
    """Guarda del lado del cliente (la base vuelve a verificar)."""
    if operador_id is None:
        raise InventarioError(MOTIVOS["SIN_OPERADOR"])
    if normalizar_rol(rol) != ROL_ADMIN:
        raise InventarioError("Modificar el inventario requiere rol Administrador.")


def _fecha(texto: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(texto).astimezone(TZ_CUARTEL) if texto else None


@dataclass(frozen=True)
class ItemInventario:
    id: str
    codigo_qr: str
    nombre: str
    categoria: str
    ubicacion: str
    estado: str
    marca_modelo: Optional[str]
    numero_serie: Optional[str]
    observaciones: Optional[str]
    activo: bool
    motivo_baja: Optional[str]
    fecha_alta: Optional[datetime]
    updated_at: Optional[datetime]
    creado_por: Optional[str]
    modificado_por: Optional[str]
    fuera_servicio_automatico: bool = False   # puesto fuera de servicio por el motor de mantenimiento
    motivo_estado: Optional[str] = None

    @property
    def categoria_legible(self) -> str:
        return CATEGORIAS.get(self.categoria, self.categoria.title())

    @property
    def estado_legible(self) -> str:
        return ESTADOS.get(self.estado, self.estado.title())

    def datos(self) -> Dict[str, Any]:
        """Campos editables (para precargar el diálogo de edición)."""
        return {campo: getattr(self, campo) or "" for campo in CAMPOS_EDITABLES}


def item_desde_fila(fila: Dict[str, Any]) -> ItemInventario:
    return ItemInventario(
        id=str(fila["id"]),
        codigo_qr=fila.get("codigo_qr") or "",
        nombre=fila.get("nombre") or "",
        categoria=fila.get("categoria") or "OTRO",
        ubicacion=fila.get("ubicacion") or "",
        estado=fila.get("estado") or ESTADO_OPERATIVO,
        marca_modelo=fila.get("marca_modelo"),
        numero_serie=fila.get("numero_serie"),
        observaciones=fila.get("observaciones"),
        activo=bool(fila.get("activo", True)),
        motivo_baja=fila.get("motivo_baja"),
        fecha_alta=_fecha(fila.get("fecha_alta")),
        updated_at=_fecha(fila.get("updated_at")),
        creado_por=fila.get("creado_por"),
        modificado_por=fila.get("modificado_por"),
        fuera_servicio_automatico=bool(fila.get("fuera_servicio_automatico")),
        motivo_estado=fila.get("motivo_estado"),
    )


class InventarioCliente(ClienteSupabaseRest):
    def _exigir_ok(self, resultado: Any) -> Dict[str, Any]:  # type: ignore[override]
        return ClienteSupabaseRest._exigir_ok(resultado, MOTIVOS, InventarioError)

    # -- Lectura (cualquier rol) ---------------------------------------------------

    def listar(self, incluir_bajas: bool = False) -> List[ItemInventario]:
        params = {"select": "*", "order": "nombre.asc", "limit": "5000"}
        if not incluir_bajas:
            params["activo"] = "is.true"
        return [item_desde_fila(f) for f in self._pedir("GET", VISTA, params=params) or []]

    # -- Escritura (solo ADMIN) ------------------------------------------------------

    def guardar(self, operador_id: Optional[int], rol: Optional[str], datos: Dict[str, Any],
                item_id: Optional[str] = None) -> ItemInventario:
        """Alta (item_id None) o edición. Devuelve el ítem como quedó en la base."""
        exigir_admin(rol, operador_id)
        limpio = {k: (str(v).strip() if v is not None else "") for k, v in datos.items() if k in CAMPOS_EDITABLES}
        r = self._exigir_ok(self._rpc("rpc_inventario_guardar", {
            "p_operador_id": operador_id, "p_id": item_id, "p_datos": limpio,
        }))
        return item_desde_fila(r["item"])

    def dar_de_baja(self, operador_id: Optional[int], rol: Optional[str], item_id: str, motivo: str) -> None:
        exigir_admin(rol, operador_id)
        self._exigir_ok(self._rpc("rpc_inventario_baja", {
            "p_operador_id": operador_id, "p_id": item_id, "p_motivo": motivo.strip(),
        }))

"""
Control de acceso basado en roles (RBAC) local.

Cada `Personal` tiene un `rol`:
    BOMBERO   (default) cargar/cerrar planillas, firmar dotaciones, ver
              estadísticas y su propio legajo (con su PIN).
    JEFATURA  lo de Bombero + ver CUALQUIER legajo e historial en modo
              solo lectura (sin alta, baja, edición ni reseteo).
    ADMIN     control total: ABM de personal y unidades, configuración
              crítica, legajos editables.
    ESTADISTICA  lo de Bombero + carga / sincronización con RUBA y acceso
              total a Estadísticas (sin legajos ajenos ni configuración).

Permiso RUBA (`cargar_ruba`): carga y sincronización de planillas con el
sistema oficial RUBA (🚀 Cargar a RUBA, ↻ RUBA, marcado manual). Por defecto
solo Jefatura, Administrador y Estadística.

Estadísticas (`ver_estadisticas_completas`): un Bombero ve los KPIs
generales del cuartel y su estadística personal; Jefatura, Administrador y
Estadística ven toda la base (rankings por bombero, filtros avanzados y el
reporte gerencial).

La app no tiene login: quien no se identificó como operador (chip 👤 de la
barra superior, bombero + PIN) opera con los permisos de BOMBERO. Los roles
se asignan en Configuración → "Usuarios y Roles", autorizado con la cuenta
institucional de RUBA (o la clave maestra si todavía no hay credenciales
RUBA guardadas), así nunca queda la app sin forma de nombrar un ADMIN.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ROL_BOMBERO = "BOMBERO"
ROL_JEFATURA = "JEFATURA"
ROL_ADMIN = "ADMIN"
ROL_ESTADISTICA = "ESTADISTICA"
ROLES = (ROL_BOMBERO, ROL_JEFATURA, ROL_ADMIN, ROL_ESTADISTICA)
ROL_DEFAULT = ROL_BOMBERO

NOMBRES_ROL = {
    ROL_BOMBERO: "Bombero",
    ROL_JEFATURA: "Jefatura (solo lectura de legajos)",
    ROL_ADMIN: "Administrador",
    ROL_ESTADISTICA: "Estadística (RUBA y reportes)",
}


@dataclass(frozen=True)
class Permisos:
    """Qué habilita cada rol en la UI."""

    editar_padron: bool         # alta / baja / edición / eliminación de personal y unidades, importar
    ver_legajos_ajenos: bool    # abrir el legajo de otro sin su PIN
    resetear_pin_ajeno: bool    # reseteo con clave maestra desde el legajo de otro
    configurar: bool            # credenciales RUBA, cuartel, mapa, mantenimiento
    cargar_ruba: bool = False   # permiso RUBA: cargar / sincronizar planillas con el portal oficial
    ver_estadisticas_completas: bool = False  # toda la base estadística (no solo KPIs generales + la propia)


_PERMISOS = {
    ROL_BOMBERO: Permisos(editar_padron=False, ver_legajos_ajenos=False, resetear_pin_ajeno=False, configurar=False),
    ROL_JEFATURA: Permisos(editar_padron=False, ver_legajos_ajenos=True, resetear_pin_ajeno=False, configurar=False,
                           cargar_ruba=True, ver_estadisticas_completas=True),
    ROL_ADMIN: Permisos(editar_padron=True, ver_legajos_ajenos=True, resetear_pin_ajeno=True, configurar=True,
                        cargar_ruba=True, ver_estadisticas_completas=True),
    ROL_ESTADISTICA: Permisos(editar_padron=False, ver_legajos_ajenos=False, resetear_pin_ajeno=False,
                              configurar=False, cargar_ruba=True, ver_estadisticas_completas=True),
}

# Roles que reciben el permiso RUBA por defecto.
ROLES_CON_PERMISO_RUBA = tuple(r for r in ROLES if _PERMISOS[r].cargar_ruba)


def normalizar_rol(valor: Optional[str]) -> str:
    """Rol válido en mayúsculas; cualquier valor desconocido o vacío -> BOMBERO."""
    rol = (valor or "").strip().upper()
    return rol if rol in ROLES else ROL_DEFAULT


def permisos_de(rol: Optional[str]) -> Permisos:
    return _PERMISOS[normalizar_rol(rol)]


def puede_cargar_ruba(rol: Optional[str]) -> bool:
    return permisos_de(rol).cargar_ruba

"""
Importación de los reportes Excel que exporta RUBA a la base local
(data/fire_station.db). El archivo SIEMPRE lo elige el usuario (QFileDialog
en la UI): este módulo no conoce ninguna ruta fija y no depende de Qt.

- 'Reporte de vehiculos.xlsx'  -> tabla `moviles`  (clave: 'Nº Móvil').
- 'Reporte de bomberos.xlsx'   -> tabla `personal` (clave: 'Id' de RUBA, luego DNI).

Cada importación corre en UNA transacción (get_session): si una fila rompe
una restricción de la base, no queda nada a medio importar.

Particularidades del export real de RUBA que se contemplan:
  - 'Nº Móvil' viene a veces como número (26) y con espacios dobles
    ('Rojo  22'); en la base el móvil puede figurar como 'Móvil 26'.
    Todo se compara por una clave normalizada (ver `clave_movil`).
  - El mismo móvil puede aparecer dos veces (un alta vieja y la vigente):
    se queda la fila más completa y se avisa en el resumen.
  - El reporte de bomberos real NO trae 'Jerarquía': se usa esa columna si
    existe y, si no, 'Formación' (Bombero / Aspirante / Cadete).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.orm import Session

ESTADO_MOVIL_EN_SERVICIO = "En Servicio"
ESTADO_MOVIL_FUERA_DE_SERVICIO = "Fuera de Servicio"
ESTADO_MOVIL_BAJA = "Baja"

ESTADOS_PERSONAL = ("Activo", "Licencia", "Reserva", "Baja")


# ---------------------------------------------------------------------------
# Lectura genérica de la hoja
# ---------------------------------------------------------------------------

def _clave(texto: Any) -> str:
    """Sin acentos, mayúsculas, espacios colapsados (para encabezados y valores)."""
    descompuesto = unicodedata.normalize("NFKD", str(texto or ""))
    sin_acentos = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_acentos.upper().split())


def _texto(valor: Any) -> Optional[str]:
    """Celda -> texto limpio (espacios colapsados); 14000.0 -> '14000'; None si vacía."""
    if valor is None:
        return None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = " ".join(str(valor).split())
    return texto or None


def _es_si(valor: Any) -> bool:
    return _clave(valor) in {"SI", "S", "TRUE", "1", "VERDADERO"}


def _entero(valor: Any) -> Optional[int]:
    try:
        return int(float(str(valor).strip())) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fecha(valor: Any) -> Optional[date]:
    """RUBA exporta dd/mm/aa como texto; openpyxl puede dar datetime."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = _texto(valor)
    for formato in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto or "", formato).date()
        except ValueError:
            continue
    return None


def _leer_hoja(ruta: Path, requeridas: List[str]) -> Tuple[Dict[str, int], Iterator[tuple]]:
    """Primera hoja del Excel: (índice de columnas por clave normalizada, filas).
    FileNotFoundError si no existe; ValueError si no es .xlsx o faltan columnas."""
    from zipfile import BadZipFile

    from openpyxl import load_workbook  # import diferido: openpyxl es pesado
    from openpyxl.utils.exceptions import InvalidFileException

    ruta = Path(ruta)
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe el archivo: {ruta}")
    try:
        libro = load_workbook(ruta, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError) as e:
        raise ValueError(f"{ruta.name} no es un Excel (.xlsx) válido: {e}") from e
    try:
        filas = list(libro.worksheets[0].iter_rows(values_only=True))
    finally:
        libro.close()
    if not filas:
        raise ValueError(f"{ruta.name}: la hoja está vacía.")
    indices = {_clave(h): i for i, h in enumerate(filas[0]) if h is not None}
    faltantes = [c for c in requeridas if _clave(c) not in indices]
    if faltantes:
        raise ValueError(f"{ruta.name}: faltan las columnas {faltantes}. ¿Es el reporte correcto de RUBA?")
    return indices, iter(filas[1:])


def _celda(indices: Dict[str, int], fila: tuple, columna: str) -> Any:
    i = indices.get(_clave(columna))
    return fila[i] if i is not None and i < len(fila) else None


@dataclass
class ResumenImportacion:
    total: int = 0
    nuevas: int = 0
    actualizadas: int = 0
    avisos: List[str] = field(default_factory=list)

    def texto(self, sustantivo: str) -> str:
        return f"{self.total} {sustantivo} importadas ({self.nuevas} nuevas, {self.actualizadas} actualizadas)"


# ---------------------------------------------------------------------------
# Vehículos
# ---------------------------------------------------------------------------

COLUMNAS_VEHICULOS = ["Nº Móvil", "Marca", "Modelo", "Tipo", "Año", "Servicio", "Baja"]

_RE_PREFIJO_MOVIL = re.compile(r"^(MOVIL|MOV\.?|N[º°O]?\s*MOVIL:?)\s+(?=\d+$)")


def normalizar_dominio(valor: Any) -> Optional[str]:
    """'ab 123 cd' / 'AB-123-CD' -> 'AB123CD'. None si queda vacío."""
    texto = re.sub(r"[\s.\-]", "", _texto(valor) or "").upper()
    return texto[:15] or None


def clave_movil(numero: Any) -> str:
    """Clave de comparación de un móvil: 'Rojo  22' == 'ROJO 22';
    26 == '26' == 'Móvil 26' (así calza con los nombres ya cargados)."""
    return _RE_PREFIJO_MOVIL.sub("", _clave(_texto(numero)))


def nombre_para_movil(numero: str) -> str:
    """Nombre visible de una unidad nueva: mismo criterio que el alta desde
    ruba_mapping.json (app/db.py): '26' -> 'Móvil 26', 'Rojo 18' tal cual."""
    return f"Móvil {numero}" if numero.isdigit() else numero


@dataclass
class VehiculoRuba:
    numero: str                        # 'Nº Móvil' limpio
    tipo: Optional[str]
    marca: Optional[str]
    modelo: Optional[str]
    anio: Optional[int]
    estado: str                        # ESTADO_MOVIL_*
    fecha_adquisicion: Optional[date] = None
    dominio: Optional[str] = None      # solo si el reporte trae "Dominio" / "Patente"
    fila_excel: int = 0
    completitud: int = 0               # para elegir entre filas duplicadas

    @property
    def activo(self) -> bool:
        return self.estado == ESTADO_MOVIL_EN_SERVICIO

    @property
    def clave(self) -> str:
        return clave_movil(self.numero)


def _estado_vehiculo(servicio: Any, baja: Any) -> str:
    if _es_si(baja):
        return ESTADO_MOVIL_BAJA
    if _es_si(servicio):
        return ESTADO_MOVIL_EN_SERVICIO
    return ESTADO_MOVIL_FUERA_DE_SERVICIO


def _preferir(a: VehiculoRuba, b: VehiculoRuba) -> VehiculoRuba:
    """Entre dos filas del mismo móvil: la que no está de baja, luego la más
    completa, luego la adquirida más recientemente, luego la de más abajo."""
    def orden(v: VehiculoRuba):
        return (v.estado != ESTADO_MOVIL_BAJA, v.completitud, v.fecha_adquisicion or date.min, v.fila_excel)
    return max(a, b, key=orden)


def leer_reporte_vehiculos(ruta: Path) -> Tuple[List[VehiculoRuba], List[str]]:
    """Filas del 'Reporte de vehiculos' de RUBA, sin duplicados por Nº Móvil.
    Devuelve (vehículos, avisos)."""
    indices, filas = _leer_hoja(ruta, COLUMNAS_VEHICULOS)
    avisos: List[str] = []
    por_clave: Dict[str, VehiculoRuba] = {}
    for n, fila in enumerate(filas, start=2):
        numero = _texto(_celda(indices, fila, "Nº Móvil"))
        if not numero:
            continue  # fila vacía / totales
        marca = _texto(_celda(indices, fila, "Marca")) or _texto(_celda(indices, fila, "Otra Marca"))
        modelo = _texto(_celda(indices, fila, "Modelo"))
        tipo = _texto(_celda(indices, fila, "Tipo"))
        vto = _texto(_celda(indices, fila, "Fecha de Vto. Verificacion"))
        vehiculo = VehiculoRuba(
            numero=numero, tipo=tipo, marca=marca, modelo=modelo,
            anio=_entero(_celda(indices, fila, "Año")),
            estado=_estado_vehiculo(_celda(indices, fila, "Servicio"), _celda(indices, fila, "Baja")),
            fecha_adquisicion=_fecha(_celda(indices, fila, "Fecha de Adquisición")),
            dominio=normalizar_dominio(_celda(indices, fila, "Dominio") or _celda(indices, fila, "Patente")),
            fila_excel=n,
            # un modelo puramente numérico igual al año suele ser un alta mal cargada
            completitud=sum(1 for x in (marca, tipo, vto) if x) + (1 if modelo and not modelo.isdigit() else 0),
        )
        previo = por_clave.get(vehiculo.clave)
        if previo is None:
            por_clave[vehiculo.clave] = vehiculo
            continue
        elegido = _preferir(previo, vehiculo)
        descartado = vehiculo if elegido is previo else previo
        por_clave[vehiculo.clave] = elegido
        avisos.append(
            f"'{descartado.numero}' (fila {descartado.fila_excel}) repite el móvil '{elegido.numero}' "
            f"(fila {elegido.fila_excel}): se tomó la fila {elegido.fila_excel} "
            f"({elegido.marca or 's/marca'} {elegido.modelo or ''}, {elegido.estado})."
        )
    if not por_clave:
        raise ValueError(f"{Path(ruta).name}: no se encontró ningún móvil.")
    return list(por_clave.values()), avisos


def _ids_ruba_por_clave() -> Dict[str, int]:
    """Nº Móvil -> value del <option> de RUBA (ruba_mapping.json), si se conoce."""
    try:
        from app.core.catalogos import cargar_moviles

        return {clave_movil(m.numero): m.id_ruba for m in cargar_moviles()}
    except (OSError, ValueError):
        return {}


def upsert_vehiculos(session: Session, vehiculos: List[VehiculoRuba]) -> ResumenImportacion:
    """Alta o actualización de cada vehículo usando 'Nº Móvil' como clave.
    Se busca primero por `numero_movil` (importaciones previas) y después
    por el nombre visible ('Rojo 18', 'Móvil 26'), para enlazar las
    unidades ya cargadas en vez de duplicarlas. Nunca borra unidades."""
    from app.models import Movil

    resumen = ResumenImportacion(total=len(vehiculos))
    ids_ruba = _ids_ruba_por_clave()
    existentes = session.query(Movil).all()
    por_numero = {clave_movil(m.numero_movil): m for m in existentes if m.numero_movil}
    por_nombre = {clave_movil(m.nombre_identificador): m for m in existentes}
    nombres_usados = {m.nombre_identificador for m in existentes}

    for v in vehiculos:
        movil = por_numero.get(v.clave) or por_nombre.get(v.clave)
        if movil is None:
            nombre = nombre_para_movil(v.numero)
            if nombre in nombres_usados:  # choca con otra unidad cargada a mano
                nombre = f"{nombre} (RUBA)"
            movil = Movil(nombre_identificador=nombre)
            session.add(movil)
            nombres_usados.add(nombre)
            resumen.nuevas += 1
        else:
            resumen.actualizadas += 1
        movil.numero_movil = v.numero
        movil.tipo = v.tipo
        movil.marca = v.marca
        movil.modelo = v.modelo
        movil.anio = v.anio
        if v.dominio:  # el reporte habitual no trae dominio: no se borra el cargado a mano
            movil.dominio = v.dominio
        movil.estado = v.estado
        movil.activo = v.activo
        if movil.id_ruba is None and v.clave in ids_ruba:
            otro = session.query(Movil).filter(Movil.id_ruba == ids_ruba[v.clave]).first()
            if otro is None or otro is movil:
                movil.id_ruba = ids_ruba[v.clave]
        por_numero[v.clave] = movil
    session.flush()
    return resumen


def importar_vehiculos_desde_excel(ruta: Path) -> ResumenImportacion:
    """Lee y aplica el 'Reporte de vehiculos' en una sola transacción."""
    from app.db import get_session

    vehiculos, avisos = leer_reporte_vehiculos(ruta)
    with get_session() as session:
        resumen = upsert_vehiculos(session, vehiculos)
    resumen.avisos = avisos + resumen.avisos
    return resumen


# ---------------------------------------------------------------------------
# Bomberos
# ---------------------------------------------------------------------------

COLUMNAS_BOMBEROS = ["Id", "Apellido", "Nombre", "DNI"]


@dataclass
class BomberoRuba:
    id_ruba: int
    apellido: str
    nombre: str
    dni: str
    legajo: Optional[str]
    jerarquia: Optional[str]
    jerarquia_es_oficial: bool          # vino de 'Jerarquía' (no de 'Formación')
    estado: str                         # uno de ESTADOS_PERSONAL


def _estado_personal(estado: Any, activo: Any) -> str:
    clave = _clave(estado)
    for opcion in ESTADOS_PERSONAL:
        if clave == _clave(opcion):
            return opcion
    if clave:  # 'Suspendido', 'Inactivo'... cualquier otro estado de RUBA
        return "Baja" if clave not in {"ACTIVO", "ACTIVA"} else "Activo"
    return "Activo" if activo is None or _es_si(activo) else "Baja"


def leer_reporte_bomberos(ruta: Path) -> List[BomberoRuba]:
    """TODAS las filas del 'Reporte de bomberos' (Activo, Reserva, Baja...),
    a diferencia de catalogos.leer_padron_personal, que solo deja al
    personal activo para los selectores de RUBA."""
    indices, filas = _leer_hoja(ruta, COLUMNAS_BOMBEROS)
    tiene_jerarquia = _clave("Jerarquía") in indices
    bomberos: List[BomberoRuba] = []
    for fila in filas:
        id_ruba = _entero(_celda(indices, fila, "Id"))
        if id_ruba is None:
            continue  # fila vacía / totales
        # Puede venir sin DNI (pasa en RUBA): se actualiza igual si ya existe por Id.
        dni = re.sub(r"\D", "", _texto(_celda(indices, fila, "DNI")) or "")
        jerarquia = _texto(_celda(indices, fila, "Jerarquía" if tiene_jerarquia else "Formación"))
        legajo = _texto(_celda(indices, fila, "Legajo"))
        bomberos.append(BomberoRuba(
            id_ruba=id_ruba,
            apellido=(_texto(_celda(indices, fila, "Apellido")) or "").upper(),
            nombre=_texto(_celda(indices, fila, "Nombre")) or "",
            dni=dni,
            legajo=legajo.replace("/", "-") if legajo else None,
            jerarquia=jerarquia,
            jerarquia_es_oficial=tiene_jerarquia,
            estado=_estado_personal(_celda(indices, fila, "Estado"), _celda(indices, fila, "Activo")),
        ))
    if not bomberos:
        raise ValueError(f"{Path(ruta).name}: no se encontró ningún bombero con Id.")
    return bomberos


def upsert_bomberos(session: Session, bomberos: List[BomberoRuba]) -> ResumenImportacion:
    """Alta/actualización por Id de RUBA y, si no, por DNI (así el personal
    ya cargado conserva PIN, firma y legajo digital). Reglas:
      - estado/activo, apellido, nombre e id_ruba se toman de RUBA;
      - legajo y DNI se actualizan solo si no los usa otra persona;
      - la jerarquía se pisa solo si viene de la columna 'Jerarquía'; la
        'Formación' (Bombero/Aspirante) solo completa jerarquías vacías,
        para no borrar grados cargados a mano (Sargento, Cabo...);
      - a quien está de Baja y no existe en la base no se lo da de alta."""
    from app.models import Personal

    resumen = ResumenImportacion()
    existentes = session.query(Personal).all()
    por_id = {p.id_ruba: p for p in existentes if p.id_ruba is not None}
    por_dni = {p.dni: p for p in existentes}
    legajos = {p.legajo: p for p in existentes if p.legajo}

    for b in bomberos:
        persona = por_id.get(b.id_ruba) or (por_dni.get(b.dni) if b.dni else None)
        if persona is None:
            if b.estado == "Baja":
                continue
            if not b.dni:
                resumen.avisos.append(f"{b.apellido}, {b.nombre} (Id RUBA {b.id_ruba}) no tiene DNI en RUBA: "
                                      "no se dio de alta (cargalo a mano o completá el DNI en RUBA).")
                continue
            persona = Personal(dni=b.dni, nombre=b.nombre, apellido=b.apellido)
            session.add(persona)
            por_dni[b.dni] = persona
            resumen.nuevas += 1
        else:
            resumen.actualizadas += 1
        resumen.total += 1

        persona.id_ruba = b.id_ruba
        por_id[b.id_ruba] = persona
        persona.apellido = b.apellido or persona.apellido
        persona.nombre = b.nombre or persona.nombre
        if b.dni and persona.dni != b.dni:
            if por_dni.get(b.dni) not in (None, persona):
                resumen.avisos.append(f"DNI {b.dni} de {b.apellido}, {b.nombre} ya es de otra persona: no se cambió.")
            else:
                por_dni.pop(persona.dni, None)
                persona.dni = b.dni
                por_dni[b.dni] = persona
        if b.legajo and persona.legajo != b.legajo:
            if legajos.get(b.legajo) not in (None, persona):
                resumen.avisos.append(f"Legajo {b.legajo} de {b.apellido}, {b.nombre} ya está asignado: no se cambió.")
            else:
                legajos.pop(persona.legajo, None)
                persona.legajo = b.legajo
                legajos[b.legajo] = persona
        if b.jerarquia and (b.jerarquia_es_oficial or not persona.jerarquia):
            persona.jerarquia = b.jerarquia
        persona.estado = b.estado
        persona.activo = b.estado == "Activo"
    session.flush()
    return resumen


def importar_bomberos_desde_excel(ruta: Path) -> Tuple[ResumenImportacion, Optional[int]]:
    """Aplica el 'Reporte de bomberos' a la base (una transacción) y
    actualiza la copia local del padrón que usan los selectores de RUBA.
    Devuelve (resumen, cantidad de activos en el padrón | None si no se
    pudo actualizar el padrón -- el motivo va en los avisos)."""
    from app.core.catalogos import importar_padron
    from app.db import get_session

    bomberos = leer_reporte_bomberos(ruta)
    with get_session() as session:
        resumen = upsert_bomberos(session, bomberos)
    try:
        activos: Optional[int] = len(importar_padron(Path(ruta)))
    except (OSError, ValueError) as e:
        activos = None
        resumen.avisos.append(f"La base se actualizó, pero no el padrón de los selectores de RUBA: {e}")
    return resumen, activos

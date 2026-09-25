"""
Catálogos de referencia para cruzar datos locales con RUBA:

- Padrón de personal activo, leído del export de RUBA
  `data/Reporte de bomberos.xlsx` (Id interno de RUBA, legajo, DNI, etc.).
- Móviles del cuartel con su ID oficial de RUBA (`vehiculos_cuartel` en
  `config/ruba_mapping.json`).
- Tipos y subtipos (categorías) de incidente (`catalogo` en el mismo JSON).

Todo se expone como dataclasses inmutables con `to_dict()` (serializable a
JSON) y los cargadores por defecto se cachean: leer el Excel una vez por
sesión alcanza. Este módulo no depende de Qt ni de Playwright.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.paths import get_resource_path, get_writable_dir

NOMBRE_REPORTE_PERSONAL = "Reporte de bomberos.xlsx"

CLASIFICACION_BOMBERO = "Bombero"
CLASIFICACION_ASPIRANTE = "Aspirante"

# Valores de la columna "Cargo" que en RUBA significan "sin cargo".
_CARGOS_VACIOS = {"", "NO POSEE", "NINGUNO", "-"}


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _clave_busqueda(texto: str) -> str:
    """Forma canónica para comparar: sin acentos, mayúsculas, espacios colapsados."""
    return " ".join(_sin_acentos(texto).upper().split())


def _texto(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    texto = " ".join(str(valor).split())
    return texto or None


# ---------------------------------------------------------------------------
# Personal
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bombero:
    id_ruba: int
    apellido: str
    nombre: str
    dni: str
    legajo: Optional[str]
    cargo: Optional[str]
    clasificacion: str          # "Bombero" | "Aspirante"
    formacion: str              # valor original de RUBA (Bombero / Aspirante / Cadete)

    @property
    def nombre_completo(self) -> str:
        return f"{self.apellido}, {self.nombre}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clasificar(formacion: str) -> str:
    """RUBA distingue Bombero / Aspirante / Cadete; para la operativa todo lo
    que no es Bombero formado cuenta como Aspirante."""
    return CLASIFICACION_BOMBERO if _clave_busqueda(formacion) == "BOMBERO" else CLASIFICACION_ASPIRANTE


def _normalizar_legajo(valor: Any) -> Optional[str]:
    texto = _texto(valor)
    return texto.replace("/", "-") if texto else None


def _normalizar_cargo(valor: Any) -> Optional[str]:
    texto = _texto(valor)
    return None if texto is None or texto.upper() in _CARGOS_VACIOS else texto


def _normalizar_dni(valor: Any) -> str:
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return re.sub(r"\D", "", str(valor or ""))


def ruta_padron_por_defecto() -> Path:
    """Copia local del padrón (data/Reporte de bomberos.xlsx). Puede no
    existir: se crea al importar el Excel desde la vista de Personal."""
    return get_writable_dir("data") / NOMBRE_REPORTE_PERSONAL


def leer_padron_personal(ruta: Optional[Path] = None) -> List[Bombero]:
    """Lee el export 'Reporte de bomberos' de RUBA y devuelve solo el
    personal activo, ordenado por apellido y nombre. Las columnas se ubican
    por nombre de encabezado (sin importar orden ni acentos).

    Errores: FileNotFoundError si el archivo no existe; ValueError si no es
    un Excel válido o le faltan columnas (nunca otra excepción de openpyxl)."""
    from zipfile import BadZipFile

    from openpyxl import load_workbook  # import diferido: openpyxl es pesado
    from openpyxl.utils.exceptions import InvalidFileException

    ruta = Path(ruta) if ruta else ruta_padron_por_defecto()
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe el padrón de bomberos: {ruta}")
    try:
        libro = load_workbook(ruta, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError) as e:
        raise ValueError(f"{ruta.name} no es un Excel (.xlsx) válido: {e}") from e
    try:
        filas = libro.worksheets[0].iter_rows(values_only=True)
        encabezado = next(filas, None)
        if encabezado is None:
            return []
        indices = {_clave_busqueda(str(h)): i for i, h in enumerate(encabezado) if h is not None}

        # Legajo / Cargo son opcionales; la formación puede venir como
        # 'Formación' (export actual) o 'Jerarquía' (otros exports de RUBA).
        requeridas = ["ID", "APELLIDO", "NOMBRE", "DNI"]
        faltantes = [c for c in requeridas if c not in indices]
        if "FORMACION" not in indices and "JERARQUIA" not in indices:
            faltantes.append("FORMACION o JERARQUIA")
        if faltantes:
            raise ValueError(f"{ruta.name}: faltan las columnas {faltantes}.")
        columna_formacion = "FORMACION" if "FORMACION" in indices else "JERARQUIA"

        def celda(fila: tuple, columna: str) -> Any:
            i = indices.get(columna)
            return fila[i] if i is not None and i < len(fila) else None

        personal: List[Bombero] = []
        for fila in filas:
            if celda(fila, "ID") is None:
                continue  # fila vacía al final de la hoja
            activo = _clave_busqueda(str(celda(fila, "ACTIVO") or "SI")) in {"SI", "S", "TRUE", "1"}
            estado = _clave_busqueda(str(celda(fila, "ESTADO") or "ACTIVO"))
            if not activo or estado != "ACTIVO":
                continue

            formacion = _texto(celda(fila, columna_formacion)) or ""
            try:
                id_ruba = int(celda(fila, "ID"))
            except (TypeError, ValueError):
                continue  # fila de totales / texto suelto: no es un bombero
            personal.append(Bombero(
                id_ruba=id_ruba,
                apellido=(_texto(celda(fila, "APELLIDO")) or "").upper(),
                nombre=_texto(celda(fila, "NOMBRE")) or "",
                dni=_normalizar_dni(celda(fila, "DNI")),
                legajo=_normalizar_legajo(celda(fila, "LEGAJO")),
                cargo=_normalizar_cargo(celda(fila, "CARGO")),
                clasificacion=_clasificar(formacion),
                formacion=formacion,
            ))
    finally:
        libro.close()

    personal.sort(key=lambda b: (_clave_busqueda(b.apellido), _clave_busqueda(b.nombre)))
    return personal


class PadronPersonal:
    """Padrón indexado para búsquedas rápidas (O(1) por ID/DNI)."""

    def __init__(self, bomberos: Iterable[Bombero]) -> None:
        self._bomberos: List[Bombero] = list(bomberos)
        self._por_id: Dict[int, Bombero] = {b.id_ruba: b for b in self._bomberos}
        self._por_dni: Dict[str, Bombero] = {b.dni: b for b in self._bomberos if b.dni}
        if len(self._por_id) != len(self._bomberos):
            raise ValueError("El padrón tiene Ids de RUBA duplicados.")

    def __len__(self) -> int:
        return len(self._bomberos)

    def __iter__(self):
        return iter(self._bomberos)

    @property
    def bomberos(self) -> List[Bombero]:
        return list(self._bomberos)

    def buscar_por_id(self, id_ruba: int | str) -> Optional[Bombero]:
        try:
            return self._por_id.get(int(id_ruba))
        except (TypeError, ValueError):
            return None

    def buscar_por_dni(self, dni: int | str) -> Optional[Bombero]:
        return self._por_dni.get(_normalizar_dni(dni))

    def buscar_por_apellido(self, apellido: str, exacto: bool = False) -> List[Bombero]:
        """Coincidencia sin distinguir mayúsculas ni acentos. Por defecto
        busca apellidos que *empiecen* con el texto (útil para autocompletar);
        con `exacto=True` exige el apellido completo. Devuelve lista porque
        hay apellidos repetidos en el cuerpo."""
        clave = _clave_busqueda(apellido)
        if not clave:
            return []
        if exacto:
            return [b for b in self._bomberos if _clave_busqueda(b.apellido) == clave]
        return [b for b in self._bomberos if _clave_busqueda(b.apellido).startswith(clave)]

    def por_clasificacion(self, clasificacion: str) -> List[Bombero]:
        return [b for b in self._bomberos if b.clasificacion == clasificacion]

    def to_list(self) -> List[Dict[str, Any]]:
        return [b.to_dict() for b in self._bomberos]


@lru_cache(maxsize=1)
def obtener_padron() -> PadronPersonal:
    """Padrón por defecto (data/Reporte de bomberos.xlsx), cacheado.
    Llamar `obtener_padron.cache_clear()` tras reemplazar el Excel."""
    return PadronPersonal(leer_padron_personal())


def importar_padron(origen: Path) -> PadronPersonal:
    """Importa un 'Reporte de bomberos' elegido por el usuario (cualquier
    ubicación): lo valida ANTES de tocar nada y recién entonces lo copia a
    data/Reporte de bomberos.xlsx, reemplazando el anterior. Levanta
    FileNotFoundError / ValueError sin modificar la copia local si el
    archivo no sirve."""
    import shutil

    origen = Path(origen)
    padron = PadronPersonal(leer_padron_personal(origen))
    if not len(padron):
        raise ValueError(f"{origen.name} no tiene personal activo: no se importó.")
    destino = ruta_padron_por_defecto()
    if origen.resolve() != destino.resolve():
        temporal = destino.with_suffix(".importando.xlsx")
        shutil.copy2(origen, temporal)
        temporal.replace(destino)
    obtener_padron.cache_clear()
    return padron


# ---------------------------------------------------------------------------
# ruba_mapping.json: móviles y tipos de incidente
# ---------------------------------------------------------------------------

# -- Tipo/Subtipo -> bloque condicional de "Editar General" en RUBA --------------
# (claves de `selectores.editar_general.condicionales` en ruba_mapping.json)

TIPO_ACCIDENTES = 1
TIPO_INCENDIOS = 3
SUBTIPO_INCENDIO_FORESTAL = 19
SUBTIPOS_INCENDIO_ESTRUCTURAL = {15, 21, 23}  # Comercio, Industrias, Vivienda

FORM_FORESTAL = "incendio_forestal"
FORM_ESTRUCTURAL = "incendio_estructural"
FORM_INCENDIO = "incendio"  # resto de los Incendios (vehicular, aeronaves...): solo la causa
FORM_ACCIDENTE = "accidente"


def formulario_para(tipo_id: Optional[int], subtipo_codigo: Optional[str | int]) -> Optional[str]:
    """Qué bloque condicional de RUBA corresponde a un Tipo + Subtipo
    (códigos RUBA), o None si ese siniestro no tiene campos extra."""
    try:
        subtipo = int(subtipo_codigo) if subtipo_codigo not in (None, "") else None
    except (TypeError, ValueError):
        subtipo = None
    if tipo_id == TIPO_INCENDIOS:
        if subtipo == SUBTIPO_INCENDIO_FORESTAL:
            return FORM_FORESTAL
        if subtipo in SUBTIPOS_INCENDIO_ESTRUCTURAL:
            return FORM_ESTRUCTURAL
        return FORM_INCENDIO
    if tipo_id == TIPO_ACCIDENTES:
        return FORM_ACCIDENTE
    return None


def leer_mapping(ruta: Optional[Path] = None) -> Dict[str, Any]:
    """Lee config/ruba_mapping.json. Lanza OSError / json.JSONDecodeError si
    falta o está mal formado."""
    ruta = ruta or get_resource_path("config/ruba_mapping.json")
    with ruta.open("r", encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class Movil:
    id_ruba: int
    numero: str                 # "Rojo 18", "26", ...
    marca: Optional[str]
    modelo: Optional[str]
    descripcion: str            # texto original de RUBA

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_RE_MOVIL = re.compile(r"^N[º°o]\s*M[óo]vil:\s*(?P<numero>.+?)\s*\((?P<vehiculo>.+)\)\s*$", re.IGNORECASE)


def _parsear_movil(id_ruba: str, descripcion: str) -> Movil:
    coincidencia = _RE_MOVIL.match(descripcion.strip())
    if not coincidencia:
        return Movil(int(id_ruba), descripcion.strip(), None, None, descripcion)
    marca, _, modelo = coincidencia.group("vehiculo").partition(" - ")
    return Movil(
        id_ruba=int(id_ruba),
        numero=coincidencia.group("numero"),
        marca=marca.strip() or None,
        modelo=modelo.strip() or None,
        descripcion=descripcion,
    )


def cargar_moviles(mapping: Optional[Dict[str, Any]] = None) -> List[Movil]:
    """Móviles de `vehiculos_cuartel`, ordenados por ID de RUBA."""
    mapping = leer_mapping() if mapping is None else mapping
    vehiculos = mapping.get("vehiculos_cuartel", {}) or {}
    return sorted((_parsear_movil(k, v) for k, v in vehiculos.items()), key=lambda m: m.id_ruba)


@dataclass(frozen=True)
class TipoIncidente:
    id_ruba: int
    nombre: str
    subtipos: Dict[int, str] = field(default_factory=dict)  # id_ruba del subtipo -> nombre

    def to_dict(self) -> Dict[str, Any]:
        # JSON no admite claves int: se serializan como str.
        return {"id_ruba": self.id_ruba, "nombre": self.nombre,
                "subtipos": {str(k): v for k, v in self.subtipos.items()}}


@dataclass(frozen=True)
class SubtipoIncidente:
    id_ruba: int
    nombre: str
    tipo_id: int
    tipo_nombre: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def cargar_tipos_incidente(mapping: Optional[Dict[str, Any]] = None) -> List[TipoIncidente]:
    """Tipos de incidente con sus subtipos (categorías), ordenados por ID.
    El orden de los subtipos respeta el del JSON (= el del combo de RUBA,
    con "Otros" al final)."""
    mapping = leer_mapping() if mapping is None else mapping
    catalogo = mapping.get("catalogo", {}) or {}
    tipos = [
        TipoIncidente(
            id_ruba=int(tipo_id),
            nombre=str(datos["nombre"]),
            subtipos={int(k): str(v) for k, v in (datos.get("subtipos") or {}).items()},
        )
        for tipo_id, datos in catalogo.items()
    ]
    return sorted(tipos, key=lambda t: t.id_ruba)


def indexar_subtipos(tipos: Iterable[TipoIncidente]) -> Dict[int, SubtipoIncidente]:
    """ID de subtipo -> subtipo con su tipo padre. En RUBA los IDs de
    subtipo son únicos en todo el catálogo (no se repiten entre tipos)."""
    indice: Dict[int, SubtipoIncidente] = {}
    for tipo in tipos:
        for sub_id, sub_nombre in tipo.subtipos.items():
            if sub_id in indice:
                raise ValueError(f"Subtipo RUBA {sub_id} repetido en '{indice[sub_id].tipo_nombre}' y '{tipo.nombre}'.")
            indice[sub_id] = SubtipoIncidente(sub_id, sub_nombre, tipo.id_ruba, tipo.nombre)
    return indice


@dataclass(frozen=True)
class CatalogosRuba:
    """Móviles + tipos/subtipos ya indexados para búsqueda por ID."""
    moviles: List[Movil]
    tipos: List[TipoIncidente]

    def movil(self, id_ruba: int | str) -> Optional[Movil]:
        return next((m for m in self.moviles if str(m.id_ruba) == str(id_ruba)), None)

    def tipo(self, id_ruba: int | str) -> Optional[TipoIncidente]:
        return next((t for t in self.tipos if str(t.id_ruba) == str(id_ruba)), None)

    def subtipo(self, id_ruba: int | str) -> Optional[SubtipoIncidente]:
        try:
            return indexar_subtipos(self.tipos).get(int(id_ruba))
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> Dict[str, Any]:
        return {"moviles": [m.to_dict() for m in self.moviles], "tipos": [t.to_dict() for t in self.tipos]}


@lru_cache(maxsize=1)
def obtener_catalogos_ruba() -> CatalogosRuba:
    mapping = leer_mapping()
    return CatalogosRuba(moviles=cargar_moviles(mapping), tipos=cargar_tipos_incidente(mapping))

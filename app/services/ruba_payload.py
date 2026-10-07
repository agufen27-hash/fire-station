"""
Payload del servicio para la automatización de RUBA.

El payload es un dict JSON-serializable con una sección por pantalla de
RUBA, y cada clave de campo se llama igual que su selector en
`config/ruba_mapping.json` (`selectores.<sección>.<clave>`). La
automatización (app/services/ruba_automation.py) recorre esas mismas claves:
no hay un segundo mapeo de nombres en el medio.

    {
      "version": 1,
      "numero_parte_local": "004/2026",
      "inicializacion":         {numero_parte, tipo_incidente, categoria_incidente, hay_participaciones},
      "editar_general":         {localidad_autocomplete, calle, altura, tipo_zona, latitud, longitud, *_solicitante,
                                 descripcion, civiles_*, compania_seguro, numero_poliza,
                                 condicionales: {formulario, campos} | None,
                                 medios_aereos: {aviones, avionetas, helicopteros, otros} | None},
      "damnificados":           {heridos: [{nombre, apellido, dni, genero}], fallecidos: [...],
                                 bienes: [{tipo, descripcion, titular, seguro}],
                                 bomberos: [{bombero: PERSONA, detalle_atencion}]},
      "participacion":          {numero_parte, hora_llamado, hora_toque, fecha_/hora_salida,
                                 fecha_/hora_llegada, bomberos_*, intervencion_vehiculos,
                                 intervencion_comision, hay_intervinientes_bomberos},
      "intervencion_bomberos":  {cantidad_bomberos, bomberos: [{autocomplete_nombre: PERSONA,
                                 fecha_inicio, hora_inicio, fecha_fin, hora_fin, tipo_tarea, is_encargado}]},
      "intervencion_vehiculos": {vehiculos: [{select_vehiculo, numero_movil, autocomplete_chofer: PERSONA | None,
                                 fecha_salida, hora_salida, fecha_llegada, hora_llegada}]},
      "vehiculos_accidentes":   [{marca (ID RUBA), marca_nombre, dominio, modelo, anio,
                                 asegurado, aseguradora, poliza}]   (solo Accidentes)
    }

PERSONA = {texto_busqueda, nombre_completo, apellido, nombre, dni, id_ruba}.
Fechas "dd/mm/aaaa", horas "HH:MM" (formato de los datepicker/timepicker
de RUBA); los valores de <select> son los códigos oficiales de RUBA.

Hay dos formas de obtener los datos de entrada (`DatosServicio`): desde la
ventana abierta (`MainWindow.obtener_payload_servicio`) o desde un incidente
ya guardado (`datos_desde_incidente`, para sincronizar pendientes).

La app carga dotaciones en formato PCD2 (móvil + chofer + Jefe + embarcados)
y el Personal en Base; RUBA pide dos listas planas. `aplanar_unidades` hace
esa conversión igual para ambos orígenes: un vehículo por dotación; como
Intervinientes (tarea '1') el chofer, el Jefe y los embarcados de cada una
con los horarios de SU dotación, y el Jefe de la Dotación N° 1 como
Encargado; como Apresto (tarea '2') todo el personal en base.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Dict, List, Optional

from app.core.catalogos import FORM_ESTRUCTURAL, FORM_FORESTAL, FORM_INCENDIO, formulario_para, leer_mapping
from app.core.horarios import fecha_fin_ajustada
from app.models import CondicionDamnificado, FuncionBase, Incidente, Personal, RolDotacion

PAYLOAD_VERSION = 1


@dataclass
class PersonaRuba:
    apellido: str
    nombre: str
    dni: Optional[str] = None
    id_ruba: Optional[int] = None


@dataclass
class VehiculoServicio:
    movil_id_ruba: Optional[int]
    chofer: Optional[PersonaRuba]
    # 'Nº Móvil' local ("24", "Rojo 24", "B-24"): respaldo para elegir la
    # opción de RUBA por texto si el value (id_ruba) no está en el combo.
    numero_movil: Optional[str] = None
    fecha_salida: Optional[date] = None
    hora_salida: Optional[time] = None
    fecha_llegada: Optional[date] = None
    hora_llegada: Optional[time] = None


@dataclass
class BomberoServicio:
    persona: PersonaRuba
    tipo_tarea: Optional[str] = None
    encargado: bool = False
    # Horario de su unidad (si falta, el general del servicio).
    fecha_inicio: Optional[date] = None
    hora_inicio: Optional[time] = None
    fecha_fin: Optional[date] = None
    hora_fin: Optional[time] = None


@dataclass
class UnidadServicio:
    """Una dotación tal como se carga (formato PCD2)."""
    movil_id_ruba: Optional[int]
    chofer: Optional[PersonaRuba]
    jefe: Optional[PersonaRuba] = None
    numero_movil: Optional[str] = None
    fecha_salida: Optional[date] = None
    hora_salida: Optional[time] = None
    fecha_llegada: Optional[date] = None
    hora_llegada: Optional[time] = None
    embarcados: List[PersonaRuba] = field(default_factory=list)


TAREA_INTERVINIENTE = "1"
TAREA_APRESTO = "2"


def aplanar_unidades(unidades: List[UnidadServicio], base: Optional[List[PersonaRuba]] = None) -> tuple:
    """Dotaciones + personal en base -> (vehículos, bomberos) para RUBA.
    Nadie se repite; el Jefe de la Dotación N° 1 es el Encargado."""
    vehiculos: List[VehiculoServicio] = []
    bomberos: List[BomberoServicio] = []
    vistos: set = set()

    def clave(persona: PersonaRuba):
        return persona.id_ruba if persona.id_ruba is not None else (persona.apellido, persona.nombre)

    def agregar(persona: Optional[PersonaRuba], tarea: str, encargado: bool = False, **horario) -> None:
        if persona is None or clave(persona) in vistos:
            return
        vistos.add(clave(persona))
        bomberos.append(BomberoServicio(persona, tarea, encargado, **horario))

    for numero, u in enumerate(unidades, start=1):
        vehiculos.append(VehiculoServicio(
            movil_id_ruba=u.movil_id_ruba, chofer=u.chofer, numero_movil=u.numero_movil,
            fecha_salida=u.fecha_salida, hora_salida=u.hora_salida,
            fecha_llegada=u.fecha_llegada, hora_llegada=u.hora_llegada,
        ))
        horario = dict(fecha_inicio=u.fecha_salida, hora_inicio=u.hora_salida,
                       fecha_fin=u.fecha_llegada, hora_fin=u.hora_llegada)
        agregar(u.chofer, TAREA_INTERVINIENTE, **horario)
        agregar(u.jefe, TAREA_INTERVINIENTE, encargado=(numero == 1), **horario)
        for persona in u.embarcados:
            agregar(persona, TAREA_INTERVINIENTE, **horario)
    for persona in base or []:
        agregar(persona, TAREA_APRESTO)  # horario general del servicio
    return vehiculos, bomberos


@dataclass
class DatosServicio:
    numero_parte: str
    tipo_id: Optional[int]
    categoria_codigo: Optional[str]
    fecha: Optional[date] = None
    hora_llamado: Optional[time] = None
    hora_toque: Optional[time] = None
    calle_altura: Optional[str] = None
    localidad: Optional[str] = None
    zona: Optional[str] = None
    latitud: Optional[float] = None   # punto marcado en el mapa del parte
    longitud: Optional[float] = None
    denunciante_nombre: Optional[str] = None
    denunciante_apellido: Optional[str] = None
    denunciante_dni: Optional[str] = None
    denunciante_telefono: Optional[str] = None
    descripcion: Optional[str] = None
    civiles_heridos: int = 0
    civiles_fallecidos: int = 0
    civiles_desaparecidos: int = 0
    seguro_compania: Optional[str] = None
    seguro_poliza: Optional[str] = None
    datos_especificos: Optional[Dict[str, Any]] = None
    damnificados: List[Dict[str, Optional[str]]] = field(default_factory=list)  # {condicion, nombre, apellido, dni, genero}
    bienes: List[Dict[str, Optional[str]]] = field(default_factory=list)        # {tipo, descripcion, titular, seguro}
    bomberos_damnificados: List[Dict[str, Any]] = field(default_factory=list)   # {persona: PersonaRuba, detalle}
    fecha_salida: Optional[date] = None
    hora_salida: Optional[time] = None
    fecha_llegada: Optional[date] = None
    hora_llegada: Optional[time] = None
    bomberos_lesionados: int = 0
    vehiculos: List[VehiculoServicio] = field(default_factory=list)
    bomberos: List[BomberoServicio] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatos
# ---------------------------------------------------------------------------

def fmt_fecha(valor: Optional[date]) -> Optional[str]:
    return valor.strftime("%d/%m/%Y") if valor else None


def fmt_hora(valor: Optional[time]) -> Optional[str]:
    return valor.strftime("%H:%M") if valor else None


def opcional(valor: Any) -> Any:
    """Campo NO obligatorio de RUBA (póliza, aseguradora, DNI / teléfono del
    denunciante, dominio...): vacío, None o solo espacios -> None, así la
    automatización lo saltea en vez de tipear "" o frenar la carga."""
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor.strip() or None
    return valor


def horario_ruba(fecha_ini: Optional[date], hora_ini: Optional[time],
                 fecha_fin: Optional[date], hora_fin: Optional[time]) -> Dict[str, Optional[str]]:
    """Inicio y fin con fecha + hora completas: si cruza la medianoche
    (23:30 -> 01:15) la fecha de fin pasa al día siguiente."""
    return {"fecha_inicio": fmt_fecha(fecha_ini), "hora_inicio": fmt_hora(hora_ini),
            "fecha_fin": fmt_fecha(fecha_fin_ajustada(fecha_ini, hora_ini, fecha_fin, hora_fin)),
            "hora_fin": fmt_hora(hora_fin)}


def numero_parte_ruba(numero_local: str) -> str:
    """RUBA solo acepta dígitos: '004/2026' -> '0042026'."""
    return re.sub(r"\D", "", numero_local or "")


_RE_ALTURA = re.compile(r"^(?P<calle>.*?)[\s,]+(?:N[°º]\s*)?(?P<altura>\d+\s*[A-Za-z]?)\s*$")


def separar_calle_altura(texto: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'Belgrano 58' -> ('Belgrano', '58'); sin número final -> (texto, None)."""
    texto = " ".join((texto or "").split())
    if not texto:
        return None, None
    coincidencia = _RE_ALTURA.match(texto)
    if not coincidencia or not coincidencia.group("calle"):
        return texto, None
    return coincidencia.group("calle").rstrip(", "), coincidencia.group("altura").replace(" ", "")


def persona_desde_personal(persona: Personal) -> PersonaRuba:
    return PersonaRuba(apellido=persona.apellido, nombre=persona.nombre, dni=persona.dni, id_ruba=persona.id_ruba)


def _persona(p: Optional[PersonaRuba]) -> Optional[Dict[str, Any]]:
    if p is None:
        return None
    return {
        "texto_busqueda": p.apellido,
        "nombre_completo": f"{p.apellido}, {p.nombre}",
        "apellido": p.apellido,
        "nombre": p.nombre,
        "dni": p.dni,
        "id_ruba": p.id_ruba,
    }


# ---------------------------------------------------------------------------
# Construcción
# ---------------------------------------------------------------------------

# Select "genero" de cada fila de Heridos en RUBA (Heridos_N_genero, HTML real
# del 25/09/2026): 1 Masculino, 2 Femenino, 3 Se desconoce. Nunca se manda "".
GENEROS_RUBA: Dict[str, str] = {"1": "Masculino", "2": "Femenino", "3": "Se desconoce"}
GENERO_DEFAULT = "3"
_PALABRAS_MASCULINO = {"M", "MASCULINO", "MASC", "VARON", "HOMBRE"}
_PALABRAS_FEMENINO = {"F", "FEMENINO", "FEM", "MUJER"}


def genero_ruba(valor: Any) -> str:
    """Género del parte local -> value del <option> de RUBA.

    Se compara por PALABRAS completas (sin acentos), no por "contiene": con
    "contiene M", "FEMENINO" sería masculino. Acepta el código ya resuelto
    ("1"/"2"/"3", lo que guarda el combo del formulario). Vacío, "Se
    desconoce", "Sin datos" o cualquier otra cosa -> "3" (Se desconoce)."""
    crudo = str(valor or "").strip()
    if crudo in GENEROS_RUBA:
        return crudo
    normalizado = "".join(c for c in unicodedata.normalize("NFKD", crudo.upper()) if not unicodedata.combining(c))
    palabras = set(re.findall(r"[A-Z]+", normalizado))
    if palabras & _PALABRAS_FEMENINO:
        return "2"
    if palabras & _PALABRAS_MASCULINO:
        return "1"
    return GENERO_DEFAULT


# Select "tipo" de cada vehículo del Accidente en RUBA
# (bomberos_estructurabundle_incidenteAccidenteType_datosVehiculosAccidentes_N_tipo,
# HTML real del 25/09/2026). value -> texto del <option>.
TIPOS_VEHICULO_RUBA: Dict[str, str] = {
    "14": "Transito > Autos", "15": "Transito > Camionetas", "16": "Transito > Camiones",
    "12": "Transito > Motos", "11": "Transito > Bicicletas", "17": "Transito > Colectivos",
    "18": "Transito > Micros", "13": "Transito > Cuatriciclos", "20": "Transito > Otros",
}
# Nunca se manda la opción vacía "": sin tipo (o sin coincidencia) va como Autos.
TIPO_VEHICULO_DEFAULT = "14"
# Orden importa: "camioneta" antes que "camion", "autobus" antes que "auto".
_REGLAS_TIPO_VEHICULO = (
    (("camioneta", "pick-up", "pickup", "pick up", "utilitario"), "15"),
    (("camion",), "16"),
    (("colectivo", "omnibus", "autobus"), "17"),
    (("micro",), "18"),
    (("cuatriciclo",), "13"),
    (("moto",), "12"),                  # moto, motocicleta, ciclomotor
    (("bici",), "11"),                  # bici, bicicleta
    (("auto", "automovil"), "14"),
    (("otro",), "20"),                  # "Otro" elegido a propósito en el formulario
)


def tipo_vehiculo_ruba(texto: Any) -> str:
    """Tipo de vehículo del parte local ("Moto", "camión", "Pick-up"...) ->
    value del <option> de RUBA. Acepta también el value ya resuelto ("15").
    Vacío o sin coincidencia -> "14" (Transito > Autos)."""
    crudo = str(texto or "").strip()
    if crudo in TIPOS_VEHICULO_RUBA:
        return crudo
    normalizado = "".join(c for c in unicodedata.normalize("NFKD", crudo.lower()) if not unicodedata.combining(c))
    for claves, valor in _REGLAS_TIPO_VEHICULO:
        if any(clave in normalizado for clave in claves):
            return valor
    return TIPO_VEHICULO_DEFAULT


def _vehiculos_accidente(datos_especificos: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Vehículos damnificados de un Accidente (guardados en datos_especificos
    con la marca por nombre) con la marca ya traducida al ID de RUBA."""
    if not datos_especificos or not datos_especificos.get("vehiculos"):
        return []
    marcas = leer_mapping().get("selectores", {}).get("vehiculos_accidentes", {}).get("marcas") or {}
    return [
        {
            "marca": marcas.get(v.get("marca") or "", marcas.get("Otra")) if v.get("marca") else None,
            "marca_nombre": v.get("marca"),
            "tipo": tipo_vehiculo_ruba(v.get("tipo")),
            "tipo_nombre": v.get("tipo"),
            "dominio": opcional(v.get("dominio")),
            "modelo": opcional(v.get("modelo")),
            "anio": opcional(v.get("anio")),
            "asegurado": bool(v.get("asegurado")),
            # Aseguradora y póliza son opcionales aunque esté asegurado.
            "aseguradora": opcional(v.get("aseguradora")) if v.get("asegurado") else None,
            "poliza": opcional(v.get("poliza")) if v.get("asegurado") else None,
        }
        for v in datos_especificos["vehiculos"]
    ]


def _condicionales(datos_especificos: Optional[Dict[str, Any]], tipo_id: Optional[int] = None,
                   subtipo_codigo: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Bloque condicional de Editar General: solo {formulario, campos} (los
    vehículos van aparte, en `vehiculos_accidentes`). Sin bloque guardado
    se deduce el formulario del Tipo/Subtipo con campos vacíos: la
    automatización completa los obligatorios con su `<campo>_default`."""
    bloque = {k: v for k, v in (datos_especificos or {}).items() if k not in ("vehiculos", "medios_aereos")}
    if not bloque.get("formulario"):
        formulario = formulario_para(tipo_id, subtipo_codigo)
        if formulario is None:
            return None
        bloque = {"formulario": formulario, "campos": {}}
    bloque.setdefault("campos", {})
    return bloque


FORMULARIOS_CON_MEDIOS_AEREOS = (FORM_FORESTAL, FORM_ESTRUCTURAL, FORM_INCENDIO)
CLAVES_MEDIOS_AEREOS = ("aviones", "avionetas", "helicopteros", "otros")


def _cantidad(valor: Any) -> int:
    try:
        return max(int(valor or 0), 0)
    except (TypeError, ValueError):
        return 0


def _medios_aereos(datos_especificos: Optional[Dict[str, Any]], tipo_id: Optional[int] = None,
                   subtipo_codigo: Optional[str] = None) -> Optional[Dict[str, int]]:
    """Medios aéreos de un Incendio (datos_especificos["medios_aereos"],
    ver app/ui/siniestro_widgets.py) con la forma que espera
    RubaServiceAutomation._cargar_medios_aereos: {aviones, avionetas,
    helicopteros, otros}. None (-> combo en "No", contadores deshabilitados)
    si no es un Incendio, si no intervinieron o si todas las cantidades
    son 0: RUBA no acepta "Sí" sin ningún medio."""
    datos = datos_especificos or {}
    formulario = datos.get("formulario") or formulario_para(tipo_id, subtipo_codigo)
    medios = datos.get("medios_aereos")
    if formulario not in FORMULARIOS_CON_MEDIOS_AEREOS or not isinstance(medios, dict):
        return None
    if not medios.get("intervinieron"):
        return None
    cantidades = {clave: _cantidad(medios.get(clave)) for clave in CLAVES_MEDIOS_AEREOS}
    return cantidades if any(cantidades.values()) else None


def construir_payload(d: DatosServicio) -> Dict[str, Any]:
    calle, altura = separar_calle_altura(d.calle_altura)
    fecha_salida = d.fecha_salida or d.fecha
    general = horario_ruba(fecha_salida, d.hora_salida, d.fecha_llegada, d.hora_llegada)

    def horario_de(fecha_ini, hora_ini, fecha_fin, hora_fin) -> Dict[str, Optional[str]]:
        """Horario propio de una dotación; lo que falte, del general. La
        fecha de fin general solo se hereda si tampoco hay inicio propio (si
        no, se deduce del inicio propio con la regla de la medianoche)."""
        propio = fecha_ini is not None or hora_ini is not None
        return horario_ruba(
            fecha_ini or fecha_salida, hora_ini or d.hora_salida,
            fecha_fin or (None if propio else d.fecha_llegada), hora_fin or d.hora_llegada,
        )

    def damnificados_de(condicion: str) -> List[Dict[str, Optional[str]]]:
        return [
            {"nombre": opcional(x.get("nombre")), "apellido": opcional(x.get("apellido")),
             "dni": opcional(x.get("dni")), "genero": genero_ruba(x.get("genero"))}
            for x in d.damnificados if x.get("condicion") == condicion
        ]

    return {
        "version": PAYLOAD_VERSION,
        "numero_parte_local": d.numero_parte,
        "inicializacion": {
            "numero_parte": numero_parte_ruba(d.numero_parte),
            "tipo_incidente": str(d.tipo_id) if d.tipo_id is not None else None,
            "categoria_incidente": str(d.categoria_codigo) if d.categoria_codigo is not None else None,
            # "Hay participaciones" = de OTROS cuerpos (combo `cuerpos`): la
            # dotación propia se carga en Participación.
            "hay_participaciones": False,
        },
        "editar_general": {
            "localidad_autocomplete": d.localidad,
            "calle": calle,
            "altura": altura,
            "tipo_zona": d.zona,
            "latitud": d.latitud,
            "longitud": d.longitud,
            # Datos del denunciante: todos opcionales (None = no se tipean).
            "nombre_solicitante": opcional(d.denunciante_nombre),
            "apellido_solicitante": opcional(d.denunciante_apellido),
            "telefono_solicitante": opcional(d.denunciante_telefono),
            "dni_solicitante": opcional(d.denunciante_dni),
            "descripcion": d.descripcion,
            "civiles_heridos": d.civiles_heridos,
            "civiles_fallecidos": d.civiles_fallecidos,
            "civiles_desaparecidos": d.civiles_desaparecidos,
            "compania_seguro": opcional(d.seguro_compania),
            "numero_poliza": opcional(d.seguro_poliza),
            # True solo si el parte trae compañía o póliza reales: decide si la
            # automatización busca el bloque "Datos del Seguro" en RUBA.
            "tiene_seguro": bool(opcional(d.seguro_compania) or opcional(d.seguro_poliza)),
            "condicionales": _condicionales(d.datos_especificos, d.tipo_id, d.categoria_codigo),
            # Incendios: con apoyo aéreo real el combo va en "Sí" con las cantidades.
            "medios_aereos": _medios_aereos(d.datos_especificos, d.tipo_id, d.categoria_codigo),
        },
        "damnificados": {
            "heridos": damnificados_de(CondicionDamnificado.HERIDO.value),
            "fallecidos": damnificados_de(CondicionDamnificado.FALLECIDO.value),
            "bienes": [
                {"tipo": b.get("tipo"), "descripcion": opcional(b.get("descripcion")),
                 "titular": opcional(b.get("titular")), "seguro": opcional(b.get("seguro"))}
                for b in d.bienes
            ],
            "bomberos": [
                {"bombero": _persona(b["persona"]), "detalle_atencion": opcional(b.get("detalle"))}
                for b in d.bomberos_damnificados
            ],
        },
        "participacion": {
            "numero_parte": numero_parte_ruba(d.numero_parte),
            "hora_llamado": fmt_hora(d.hora_llamado),
            "hora_toque": fmt_hora(d.hora_toque),
            "fecha_salida": general["fecha_inicio"],
            "hora_salida": general["hora_inicio"],
            "fecha_llegada": general["fecha_fin"],
            "hora_llegada": general["hora_fin"],
            "bomberos_heridos": d.bomberos_lesionados or 0,
            "bomberos_fallecidos": 0,
            "bomberos_desaparecidos": 0,
            "intervencion_vehiculos": bool(d.vehiculos),
            "intervencion_comision": False,
            # "Bomberos Damnificados de éste cuerpo": Sí solo con bomberos
            # lesionados (con "Si" y contadores en 0 RUBA rechaza el guardado).
            "hay_intervinientes_bomberos": bool(d.bomberos_lesionados),
        },
        "intervencion_bomberos": {
            "cantidad_bomberos": len(d.bomberos),
            "bomberos": [
                {
                    "autocomplete_nombre": _persona(b.persona),
                    **horario_de(b.fecha_inicio, b.hora_inicio, b.fecha_fin, b.hora_fin),
                    "tipo_tarea": b.tipo_tarea,
                    "is_encargado": b.encargado,
                }
                for b in d.bomberos
            ],
        },
        "intervencion_vehiculos": {
            "vehiculos": [
                {
                    "select_vehiculo": str(v.movil_id_ruba) if v.movil_id_ruba is not None else None,
                    "numero_movil": v.numero_movil,
                    "autocomplete_chofer": _persona(v.chofer),
                    # Sin horario propio, el móvil hereda el general del servicio.
                    **_como_vehiculo(horario_de(v.fecha_salida, v.hora_salida, v.fecha_llegada, v.hora_llegada)),
                }
                for v in d.vehiculos
            ],
        },
        "vehiculos_accidentes": _vehiculos_accidente(d.datos_especificos),
    }


def _como_vehiculo(horario: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Mismo horario con las claves de 'Vehículos intervinientes' de RUBA."""
    return {"fecha_salida": horario["fecha_inicio"], "hora_salida": horario["hora_inicio"],
            "fecha_llegada": horario["fecha_fin"], "hora_llegada": horario["hora_fin"]}


def validar_payload(payload: Dict[str, Any]) -> List[str]:
    """Problemas que harían fallar la carga en RUBA (vacío = listo)."""
    errores: List[str] = []
    ini = payload.get("inicializacion", {})
    if not ini.get("numero_parte"):
        errores.append("El N° de parte no tiene dígitos.")
    if not ini.get("tipo_incidente") or not ini.get("categoria_incidente"):
        errores.append("Falta el Tipo o la Categoría del incidente.")
    if not (payload.get("editar_general", {}).get("descripcion") or "").strip():
        errores.append("Falta la Reseña operativa: RUBA exige la Descripción del incidente.")

    vehiculos = payload.get("intervencion_vehiculos", {}).get("vehiculos", [])
    for i, v in enumerate(vehiculos, start=1):
        if not v.get("select_vehiculo") and not v.get("numero_movil"):
            errores.append(f"Móvil {i}: no es un vehículo oficial de RUBA (sin id en vehiculos_cuartel).")
        if not v.get("autocomplete_chofer"):
            errores.append(f"Móvil {i}: falta el chofer.")

    bomberos = payload.get("intervencion_bomberos", {}).get("bomberos", [])
    if bomberos:
        encargados = sum(1 for b in bomberos if b.get("is_encargado"))
        if encargados != 1:
            errores.append(f"Tiene que haber exactamente un Encargado (hay {encargados}).")
    return errores


# ---------------------------------------------------------------------------
# Adaptador: incidente guardado -> DatosServicio
# ---------------------------------------------------------------------------

def datos_desde_incidente(incidente: Incidente) -> DatosServicio:
    """Lee un incidente (con su sesión todavía abierta) al formato neutral.
    Soporta los incidentes de antes de la Fase 11: chofer como fila de
    dotación con rol CHOFER y móviles sin id de RUBA (esos quedan con
    `movil_id_ruba=None` y `validar_payload` lo reporta)."""
    unidades: List[UnidadServicio] = []
    for su in incidente.salidas_unidad:
        filas = sorted(su.dotacion, key=lambda f: f.id)
        chofer = su.chofer
        if chofer is None:  # incidentes de antes de la Fase 11: chofer como fila de dotación
            fila_chofer = next((f for f in filas if f.rol == RolDotacion.CHOFER.value), None)
            chofer = fila_chofer.personal if fila_chofer else None
        jefe = next((f.personal for f in filas if f.rol == RolDotacion.A_CARGO.value), None)
        unidades.append(UnidadServicio(
            movil_id_ruba=su.movil.id_ruba if su.movil else None,
            numero_movil=(su.movil.numero_movil or su.movil.nombre_identificador) if su.movil else None,
            chofer=persona_desde_personal(chofer) if chofer else None,
            jefe=persona_desde_personal(jefe) if jefe else None,
            fecha_salida=su.fecha_salida, hora_salida=su.hora_salida,
            fecha_llegada=su.fecha_llegada, hora_llegada=su.hora_regreso,
            embarcados=[persona_desde_personal(f.personal) for f in filas if f.rol == RolDotacion.BOMBERO.value],
        ))
    base = [persona_desde_personal(b.personal) for b in _base_en_orden(incidente.personal_base)]
    vehiculos, bomberos = aplanar_unidades(unidades, base)

    return DatosServicio(
        numero_parte=incidente.numero_parte,
        tipo_id=incidente.tipo_incidente_id,
        categoria_codigo=incidente.categoria.codigo_ruba if incidente.categoria else None,
        fecha=incidente.fecha,
        hora_llamado=incidente.hora_llamado,
        hora_toque=incidente.hora_toque,
        calle_altura=incidente.calle_altura,
        localidad=incidente.localidad,
        zona=incidente.zona,
        latitud=incidente.latitud,
        longitud=incidente.longitud,
        denunciante_nombre=incidente.denunciante_nombre,
        denunciante_apellido=incidente.denunciante_apellido,
        denunciante_dni=incidente.denunciante_dni,
        denunciante_telefono=incidente.denunciante_telefono,
        descripcion=incidente.resena_operativa,
        civiles_heridos=incidente.damnificados_heridos,
        civiles_fallecidos=incidente.damnificados_muertos,
        civiles_desaparecidos=incidente.damnificados_desaparecidos,
        seguro_compania=incidente.seguro_compania,
        seguro_poliza=incidente.seguro_poliza,
        datos_especificos=incidente.datos_especificos,
        damnificados=[
            {"condicion": d.condicion, "nombre": d.nombre, "apellido": d.apellido, "dni": d.dni, "genero": d.genero}
            for d in incidente.damnificados_civiles
        ],
        bienes=[
            {"tipo": b.tipo, "descripcion": b.descripcion, "titular": b.titular, "seguro": b.seguro}
            for b in incidente.bienes_afectados
        ],
        bomberos_damnificados=[
            {"persona": persona_desde_personal(b.personal), "detalle": b.detalle_atencion}
            for b in incidente.bomberos_damnificados
        ],
        fecha_salida=incidente.fecha_salida,
        hora_salida=incidente.hora_salida,
        fecha_llegada=incidente.fecha_llegada,
        hora_llegada=incidente.hora_regreso,
        bomberos_lesionados=incidente.bomberos_lesionados,
        vehiculos=vehiculos,
        bomberos=bomberos,
    )


def _base_en_orden(filas):
    """Operador 1, Operador 2 y después el apresto en el orden cargado."""
    prioridad = {FuncionBase.OPERADOR_1.value: 0, FuncionBase.OPERADOR_2.value: 1}
    return sorted(filas, key=lambda b: (prioridad.get(b.funcion, 2), b.orden))


def payload_desde_incidente(incidente_id: int) -> Dict[str, Any]:
    from app.db import get_session

    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            raise ValueError(f"No existe un incidente con id={incidente_id}.")
        return construir_payload(datos_desde_incidente(incidente))

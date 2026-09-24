"""Pruebas del padrón de personal (Reporte de bomberos.xlsx) y de los
catálogos de RUBA (móviles y tipos/subtipos de config/ruba_mapping.json)."""

import json

import pytest
from openpyxl import Workbook

from app.core.catalogos import (
    CLASIFICACION_ASPIRANTE,
    CLASIFICACION_BOMBERO,
    Bombero,
    Movil,
    PadronPersonal,
    cargar_moviles,
    cargar_tipos_incidente,
    indexar_subtipos,
    leer_mapping,
    leer_padron_personal,
    obtener_catalogos_ruba,
    obtener_padron,
)

# -- Padrón real -------------------------------------------------------------

@pytest.fixture(scope="module")
def padron() -> PadronPersonal:
    return obtener_padron()


def test_padron_tiene_48_bomberos_activos(padron):
    assert len(padron) == 48


def test_padron_tipos_consistentes(padron):
    for b in padron:
        assert isinstance(b, Bombero)
        assert isinstance(b.id_ruba, int) and b.id_ruba > 0
        assert isinstance(b.apellido, str) and b.apellido == b.apellido.strip().upper() and b.apellido
        assert isinstance(b.nombre, str) and b.nombre
        assert isinstance(b.dni, str) and b.dni.isdigit() and 7 <= len(b.dni) <= 8
        assert b.legajo is None or (isinstance(b.legajo, str) and "/" not in b.legajo)
        assert b.cargo is None or (isinstance(b.cargo, str) and b.cargo.upper() != "NO POSEE")
        assert b.clasificacion in (CLASIFICACION_BOMBERO, CLASIFICACION_ASPIRANTE)


def test_padron_ids_y_dnis_unicos(padron):
    assert len({b.id_ruba for b in padron}) == 48
    assert len({b.dni for b in padron}) == 48


def test_padron_es_serializable(padron):
    datos = json.loads(json.dumps(padron.to_list(), ensure_ascii=False))
    assert len(datos) == 48
    assert set(datos[0]) == {"id_ruba", "apellido", "nombre", "dni", "legajo", "cargo", "clasificacion", "formacion"}


def test_clasificacion_cubre_a_todos(padron):
    bomberos = padron.por_clasificacion(CLASIFICACION_BOMBERO)
    aspirantes = padron.por_clasificacion(CLASIFICACION_ASPIRANTE)
    assert bomberos and aspirantes
    assert len(bomberos) + len(aspirantes) == 48


def test_busqueda_por_id_y_dni(padron):
    primero = padron.bomberos[0]
    assert padron.buscar_por_id(primero.id_ruba) is primero
    assert padron.buscar_por_id(str(primero.id_ruba)) is primero
    assert padron.buscar_por_dni(primero.dni) is primero
    assert padron.buscar_por_id(-1) is None
    assert padron.buscar_por_id("no-es-un-id") is None


def test_busqueda_por_apellido(padron):
    # Sin distinguir mayúsculas/acentos, por prefijo, y con apellidos repetidos.
    assert {b.apellido for b in padron.buscar_por_apellido("abri")} == {"ABRILE"}
    assert len(padron.buscar_por_apellido("Abrile", exacto=True)) == 2
    # Con acento y en minúsculas encuentra el apellido del padrón (MAYÚSCULAS sin acento).
    objetivo = next(b.apellido for b in padron.bomberos if "a" in b.apellido.title()[1:])
    con_acento = objetivo.title()[0] + objetivo.title()[1:].replace("a", "á", 1)
    assert padron.buscar_por_apellido(con_acento, exacto=True)[0].apellido == objetivo
    assert padron.buscar_por_apellido("") == []
    assert padron.buscar_por_apellido("zzzz") == []


# -- Normalización con un Excel sintético -----------------------------------

def test_normalizacion_y_filtro_de_inactivos(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Formación", "Id", "Apellido", "Nombre", "DNI", "Legajo", "Cargo", "Activo", "Estado"])
    ws.append(["Cadete", 1, " pérez  ", "Ana", 40111222, "59/010", "No Posee", "Si", "Activo"])
    ws.append(["Bombero", 2, "GÓMEZ", "Luis", 30111222.0, None, "Jefe de Cuerpo Activo", "Si", "Activo"])
    ws.append(["Bombero", 3, "BAJA", "Dado", 20111222, "59-001", None, "No", "Baja"])
    ws.append([None] * 9)
    ruta = tmp_path / "reporte.xlsx"
    wb.save(ruta)

    personal = leer_padron_personal(ruta)
    assert [b.id_ruba for b in personal] == [2, 1]  # ordenado por apellido sin acentos; inactivo afuera
    gomez, perez = personal
    assert perez.apellido == "PÉREZ" and perez.legajo == "59-010" and perez.cargo is None
    assert perez.clasificacion == CLASIFICACION_ASPIRANTE and perez.formacion == "Cadete"
    assert gomez.dni == "30111222" and gomez.legajo is None and gomez.cargo == "Jefe de Cuerpo Activo"
    assert PadronPersonal(personal).buscar_por_apellido("perez")[0] is perez


def test_columna_faltante_da_error_claro(tmp_path):
    wb = Workbook()
    wb.active.append(["Id", "Apellido"])
    ruta = tmp_path / "incompleto.xlsx"
    wb.save(ruta)
    with pytest.raises(ValueError, match="faltan las columnas"):
        leer_padron_personal(ruta)


# -- Móviles y tipos de incidente --------------------------------------------

def test_moviles_son_10_con_tipos_consistentes():
    moviles = cargar_moviles()
    assert len(moviles) == 10
    assert len({m.id_ruba for m in moviles}) == 10
    for m in moviles:
        assert isinstance(m, Movil)
        assert isinstance(m.id_ruba, int)
        assert isinstance(m.numero, str) and m.numero
        assert isinstance(m.marca, str) and isinstance(m.modelo, str)
        assert isinstance(m.descripcion, str)
    json.dumps([m.to_dict() for m in moviles])


def test_movil_parseado():
    catalogos = obtener_catalogos_ruba()
    rojo_18 = catalogos.movil(4326)
    assert (rojo_18.numero, rojo_18.marca, rojo_18.modelo) == ("Rojo 18", "Ford", "F-100 4x4")
    rojo_23 = catalogos.movil("9906")
    assert (rojo_23.marca, rojo_23.modelo) == ("Man", "076-TGM 13.250")  # guion interno del modelo preservado
    assert catalogos.movil(1) is None


def test_tipos_y_subtipos():
    tipos = cargar_tipos_incidente()
    assert [t.id_ruba for t in tipos] == [1, 2, 3, 4, 5, 6]
    for t in tipos:
        assert isinstance(t.nombre, str) and t.subtipos
        assert all(isinstance(k, int) and isinstance(v, str) for k, v in t.subtipos.items())

    subtipos = indexar_subtipos(tipos)
    assert len(subtipos) == sum(len(t.subtipos) for t in tipos)
    vivienda = subtipos[23]
    assert (vivienda.nombre, vivienda.tipo_id, vivienda.tipo_nombre) == ("Vivienda", 3, "Incendios")

    catalogos = obtener_catalogos_ruba()
    assert catalogos.tipo(3).nombre == "Incendios"
    assert catalogos.subtipo("43").nombre == "Sismo"
    json.dumps(catalogos.to_dict(), ensure_ascii=False)


def test_subtipo_repetido_se_detecta():
    mapping = {"catalogo": {"1": {"nombre": "A", "subtipos": {"5": "x"}}, "2": {"nombre": "B", "subtipos": {"5": "y"}}}}
    with pytest.raises(ValueError, match="repetido"):
        indexar_subtipos(cargar_tipos_incidente(mapping))


def test_mapping_real_es_json_valido():
    mapping = leer_mapping()
    assert {"catalogo", "vehiculos_cuartel", "selectores"} <= set(mapping)

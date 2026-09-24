"""RubaServiceAutomation contra el RUBA falso de tests/fake_ruba.py (HTTP
local que reproduce el DOM y los redirects del RUBA real)."""

from datetime import date, time

import pytest

from app.core.catalogos import obtener_padron
from app.services import ruba_automation as ra
from app.services.ruba_payload import (
    BomberoServicio,
    DatosServicio,
    PersonaRuba,
    VehiculoServicio,
    construir_payload,
)
from tests.fake_ruba import FakeRuba


def _persona(b) -> PersonaRuba:
    return PersonaRuba(apellido=b.apellido, nombre=b.nombre, dni=b.dni, id_ruba=b.id_ruba)


@pytest.fixture(scope="module")
def padron():
    return obtener_padron().bomberos


def _datos(padron, heridos=1) -> DatosServicio:
    return DatosServicio(
        numero_parte="004/2026", tipo_id=3, categoria_codigo="19", fecha=date(2026, 9, 23),
        hora_llamado=time(8, 15), calle_altura="Belgrano 58", localidad="Adelia María", zona="Urbana",
        denunciante_apellido="Pérez", denunciante_nombre="Juan", descripcion="Quema de pastizal",
        civiles_heridos=heridos,
        datos_especificos={"formulario": "incendio_forestal",
                           "campos": {"tipo_lugar": "7", "unidad_superficie": "3", "cantidad_superficie": 3.5}},
        damnificados=[{"condicion": "Herido", "nombre": "Ana", "apellido": "Gómez", "dni": "30111222", "genero": "1"}]
        if heridos else [],
        fecha_salida=date(2026, 9, 23), hora_salida=time(8, 20), fecha_llegada=date(2026, 9, 23),
        hora_llegada=time(9, 45),
        vehiculos=[
            VehiculoServicio(4326, _persona(padron[0])),
            VehiculoServicio(9906, _persona(padron[5]), hora_salida=time(8, 30)),
        ],
        bomberos=[
            BomberoServicio(_persona(padron[0]), "1", False),
            BomberoServicio(_persona(padron[5]), "1", True),
            BomberoServicio(_persona(padron[10]), "2", False),
        ],
    )


@pytest.fixture
def payload(padron):
    return construir_payload(_datos(padron))


@pytest.fixture
def fake(padron):
    sitio = FakeRuba([(b.id_ruba, f"{b.apellido}, {b.nombre} (DNI {b.dni})") for b in padron]).iniciar()
    yield sitio
    sitio.detener()


def _automatizacion(payload, fake, tmp_path, eventos=None, **kwargs):
    return ra.RubaServiceAutomation(
        payload,
        credenciales={"usuario": "guardia", "clave": "x", "url_login": fake.url_login,
                      "url_incidentes": fake.url_incidentes},
        on_progreso=(eventos.append if eventos is not None else None),
        dir_capturas=tmp_path / "capturas",
        ruta_sesion=tmp_path / "sesion.json",
        timeout_ms=6000,
        **kwargs,
    )


def _estados(eventos):
    return {e.paso: e.estado for e in eventos if e.estado != "inicio"}


def test_flujo_completo(payload, fake, tmp_path, padron):
    eventos, ids = [], []
    resultado = _automatizacion(payload, fake, tmp_path, eventos, on_id_remoto=ids.append).ejecutar()

    assert resultado.ruba_id_remoto == "555" and ids == ["555"]  # el ID se informa apenas se crea
    assert resultado.url_final.endswith("/estructura/incidente/555")
    assert fake.logins == 1 and fake.inicializaciones == 1

    ini = fake.envios["/estructura/incidente/agregar"]
    assert ini["numeroParte"] == ["0042026"] and ini["tipo"] == ["3"] and ini["categoria"] == ["19"]
    assert "hayParticipaciones" not in ini

    gen = fake.envios["/estructura/incidente/editar/555"]
    # Localidad: se escribió en el autocomplete VISIBLE y quedó vinculado el hidden de Symfony.
    assert gen["localidad"] == ["1001"]
    assert "otraLocalidad" not in gen  # vacío: se deshabilita y no viaja (Symfony lo marcaba required)
    assert gen["hayIntervinientesPersonas"] == ["si"]  # hay 1 herido
    assert (gen["latitud"], gen["longitud"]) == (["-33.6305911"], ["-64.0211957"])  # lo fijó Buscar Puntos
    assert (gen["calle"], gen["altura"], gen["tipoZona"], gen["punto"]) == (["Belgrano"], ["58"], ["1"], ["0"])
    assert gen["tipoLugarForestal"] == ["7"] and gen["evacuacion"] == ["3"] and gen["superficie"] == ["3.5"]
    assert gen["heridos"] == ["1"] and gen["descripcion"] == ["Quema de pastizal"]

    dam = fake.envios["/estructura/incidente/damnificados/555"]
    assert (dam["h0_nombre"], dam["h0_dni"], dam["h0_genero"]) == (["Ana"], ["30111222"], ["1"])

    # Participación: pickers readonly cargados por JS.
    part = fake.envios["/estructura/incidente/participacion/555"]
    assert part["horaLlamado"] == ["08:15"] and part["fechaHoraSalida_date"] == ["23/09/2026"]
    assert part["fechaHoraLlegada_time"] == ["09:45"]
    assert part["intervencionVehiculo"] == ["1"] and "intervencionComision" not in part

    bom = fake.envios["/estructura/incidente/participacion/intervenciones/bomberos/77"]
    assert bom["cantBomberos"] == ["3"]
    assert bom["b0_bombero_id"] == [str(padron[0].id_ruba)] and bom["b1_bombero_id"] == [str(padron[5].id_ruba)]
    assert [bom.get(f"b{i}_encargado") for i in range(3)] == [None, ["1"], None]
    assert bom["b2_tarea"] == ["2"] and bom["b0_hi"] == ["08:20"] and bom["b0_ff"] == ["23/09/2026"]

    veh = fake.envios["/estructura/incidente/participacion/intervenciones/vehiculos/77"]
    assert veh["v0_vehiculo"] == ["4326"] and veh["v1_vehiculo"] == ["9906"]
    assert veh["v1_chofer_id"] == [str(padron[5].id_ruba)]
    assert veh["v0_hs"] == ["08:20"] and veh["v1_hs"] == ["08:30"]

    assert _estados(eventos) == {p: "ok" for p in ra.PASOS}
    assert eventos[-1].porcentaje == 100


def test_sin_victimas_ruba_saltea_damnificados(padron, fake, tmp_path):
    payload = construir_payload(_datos(padron, heridos=0))
    eventos = []
    _automatizacion(payload, fake, tmp_path, eventos).ejecutar()
    estados = _estados(eventos)
    assert estados[ra.PasoRuba.GENERAL] == "ok"
    assert estados[ra.PasoRuba.DAMNIFICADOS] == "omitido"
    assert estados[ra.PasoRuba.PARTICIPACION] == "ok" and estados[ra.PasoRuba.VEHICULOS] == "ok"
    assert "/estructura/incidente/damnificados/555" not in fake.envios  # nunca se pasó por ahí


def test_reanuda_un_incidente_ya_creado_sin_duplicarlo(payload, fake, tmp_path):
    eventos = []
    _automatizacion(payload, fake, tmp_path, eventos, ruba_id_existente="555").ejecutar()
    assert fake.inicializaciones == 0
    assert _estados(eventos)[ra.PasoRuba.INICIALIZACION] == "omitido"
    assert _estados(eventos)[ra.PasoRuba.VEHICULOS] == "ok"


def test_reutiliza_la_sesion_guardada(payload, fake, tmp_path):
    _automatizacion(payload, fake, tmp_path).ejecutar()
    assert (tmp_path / "sesion.json").exists()
    _automatizacion(payload, fake, tmp_path).ejecutar()
    assert fake.logins == 1


def test_validacion_del_servidor_misma_url_da_error_claro(payload, fake, tmp_path):
    payload["editar_general"]["descripcion"] = None  # RUBA la exige: re-renderiza /editar con el error
    eventos = []
    with pytest.raises(ra.RubaAutomationError) as error:
        _automatizacion(payload, fake, tmp_path, eventos).ejecutar()
    assert error.value.paso == ra.PasoRuba.GENERAL
    assert "La descripción es obligatoria" in error.value.detalle
    assert error.value.captura.exists() and error.value.captura.with_suffix(".html").exists()
    assert eventos[-1].estado == "error"


def test_categoria_inexistente_falla_en_inicializacion(payload, fake, tmp_path):
    payload["inicializacion"]["categoria_incidente"] = "99"
    with pytest.raises(ra.RubaAutomationError) as error:
        _automatizacion(payload, fake, tmp_path).ejecutar()
    assert error.value.paso == ra.PasoRuba.INICIALIZACION


def test_login_rechazado(payload, fake, tmp_path):
    automatizacion = _automatizacion(payload, fake, tmp_path)
    automatizacion.credenciales = {"url_login": fake.url_login, "url_incidentes": fake.url_incidentes}
    with pytest.raises(ra.RubaAutomationError) as error:
        automatizacion.ejecutar()
    assert error.value.paso == ra.PasoRuba.SESION and "usuario/clave" in error.value.detalle

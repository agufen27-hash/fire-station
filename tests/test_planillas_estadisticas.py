"""Pruebas rápidas (sin MainWindow) de: apresto con operadores de radio
(fila 51 de la PCS), rol ESTADISTICA y permiso RUBA, estado del parte en
Planillas (Borrador / En curso / Finalizado), filtros y estadística
personal del servicio de estadísticas, y la página Estadísticas según el
nivel de acceso."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.core import roles
from app.models import (
    DotacionSalida, EstadoOperativo, FuncionBase, Incidente, Movil, Personal, PersonalBase, RolDotacion,
    SalidaUnidad, TipoIncidente,
)
from app.reports.excel_generator import personal_en_apresto
from app.services.statistics_service import calcular_estadisticas, estadistica_personal

pytestmark = pytest.mark.rapido


# -- Fila 51: operadores de radio suman al apresto -------------------------------

def test_apresto_cuenta_operadores_de_radio_y_reserva():
    base = [SimpleNamespace(personal_id=1, funcion=FuncionBase.OPERADOR_1.value),
            SimpleNamespace(personal_id=2, funcion=FuncionBase.OPERADOR_2.value),
            SimpleNamespace(personal_id=3, funcion=FuncionBase.APRESTO.value),
            SimpleNamespace(personal_id=4, funcion=FuncionBase.APRESTO.value)]
    assert personal_en_apresto(SimpleNamespace(personal_base=base)) == 4
    # Solo operadores (sin reserva): antes daba 0.
    assert personal_en_apresto(SimpleNamespace(personal_base=base[:2])) == 2
    # Nadie se cuenta dos veces.
    assert personal_en_apresto(SimpleNamespace(personal_base=base + base[:1])) == 4


# -- Roles y permiso RUBA ------------------------------------------------------------

def test_permiso_ruba_y_estadisticas_por_rol():
    assert roles.ROL_ESTADISTICA in roles.ROLES
    assert set(roles.ROLES_CON_PERMISO_RUBA) == {roles.ROL_JEFATURA, roles.ROL_ADMIN, roles.ROL_ESTADISTICA}
    bombero = roles.permisos_de(roles.ROL_BOMBERO)
    assert not bombero.cargar_ruba and not bombero.ver_estadisticas_completas
    for rol in (roles.ROL_JEFATURA, roles.ROL_ADMIN, roles.ROL_ESTADISTICA):
        assert roles.puede_cargar_ruba(rol) and roles.permisos_de(rol).ver_estadisticas_completas
    estadistica = roles.permisos_de("estadistica")
    assert not (estadistica.editar_padron or estadistica.configurar or estadistica.ver_legajos_ajenos)
    assert roles.normalizar_rol("Estadistica") == roles.ROL_ESTADISTICA
    assert len(roles.ROL_ESTADISTICA) <= 12  # entra en personal.rol String(12)


# -- Base con partes de prueba --------------------------------------------------------

@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    with db.get_session() as s:
        ana = Personal(nombre="Ana", apellido="Radio", dni="91000001")
        beto = Personal(nombre="Beto", apellido="Chofer", dni="91000002")
        ceci = Personal(nombre="Ceci", apellido="Reserva", dni="91000003")
        tipo_a = TipoIncidente(nombre="Zz Incendio prueba")
        tipo_b = TipoIncidente(nombre="Zz Rescate prueba")
        s.add_all([ana, beto, ceci, tipo_a, tipo_b])
        s.flush()
        moviles = s.query(Movil).order_by(Movil.id).limit(2).all()
        m1, m2 = moviles[0], moviles[1]

        def parte(numero, fecha, tipo, movil, estado=EstadoOperativo.CERRADO.value, salida=time(10, 0),
                  regreso=time(12, 0)):
            inc = Incidente(numero_parte=numero, fecha=fecha, fecha_salida=fecha, fecha_llegada=fecha,
                            hora_salida=salida, hora_regreso=regreso, tipo_incidente_id=tipo.id,
                            estado_operativo=estado)
            s.add(inc)
            s.flush()
            su = SalidaUnidad(incidente_id=inc.id, movil_id=movil.id, chofer_personal_id=beto.id,
                              fecha_salida=fecha, hora_salida=salida, fecha_llegada=fecha, hora_regreso=regreso)
            s.add(su)
            s.flush()
            s.add(DotacionSalida(incidente_id=inc.id, movil_id=movil.id, personal_id=beto.id,
                                 rol=RolDotacion.CHOFER.value, salida_unidad_id=su.id))
            s.add(PersonalBase(incidente_id=inc.id, personal_id=ana.id, funcion=FuncionBase.OPERADOR_1.value))
            return inc

        p1 = parte("001/2026", date(2026, 1, 10), tipo_a, m1)                        # 2 h
        parte("002/2026", date(2026, 3, 5), tipo_b, m2, regreso=time(11, 0))          # 1 h
        s.add(PersonalBase(incidente_id=p1.id, personal_id=ceci.id, funcion=FuncionBase.APRESTO.value))
        parte("003/2026", date(2026, 3, 6), tipo_a, m1, estado=EstadoOperativo.EN_CURSO.value, regreso=None)
        ids = SimpleNamespace(ana=ana.id, beto=beto.id, ceci=ceci.id, tipo_a=tipo_a.id, m1=m1.id, m2=m2.id)
    yield ids
    engine.dispose()


DESDE, HASTA = date(2026, 1, 1), date(2026, 3, 31)


def test_operador_de_radio_suma_horas_de_apresto(base):
    r = calcular_estadisticas(DESDE, HASTA)
    assert r.servicios == 2 and r.servicios_en_curso == 1
    ana = r.ficha_de(base.ana)
    assert ana.servicios_cuartel == 2 and ana.horas_cuartel == pytest.approx(3.0)
    assert [n for _m, n in r.por_mes] == [1, 0, 1]
    assert r.promedio_horas_servicio == pytest.approx(1.5)


def test_filtros_por_tipo_vehiculo_y_bombero(base):
    por_tipo = calcular_estadisticas(DESDE, HASTA, tipo_id=base.tipo_a)
    assert por_tipo.servicios == 1 and por_tipo.servicios_en_curso == 1
    por_movil = calcular_estadisticas(DESDE, HASTA, movil_id=base.m2)
    assert por_movil.servicios == 1 and por_movil.salidas_moviles == 1
    assert [m.movil_id for m in por_movil.moviles] == [base.m2]
    por_bombero = calcular_estadisticas(DESDE, HASTA, personal_id=base.ceci)
    assert por_bombero.servicios == 1


def test_estadistica_personal(base):
    beto = estadistica_personal(base.beto, DESDE, HASTA)
    assert beto.asistencias == 2 and beto.servicios_interviniente == 2
    assert beto.horas_interviniente == pytest.approx(3.0) and beto.horas_apresto == 0
    assert beto.porcentaje_asistencia == pytest.approx(100.0)
    ceci = estadistica_personal(base.ceci, DESDE, HASTA)
    assert (ceci.asistencias, ceci.horas_apresto, ceci.horas_totales) == (1, pytest.approx(2.0), pytest.approx(2.0))
    assert ceci.porcentaje_asistencia == pytest.approx(50.0)


# -- Planillas: estado del parte ------------------------------------------------------

def test_estado_del_parte_borrador_en_curso_finalizado():
    from app.ui.history_window import estado_parte

    cerrado = SimpleNamespace(estado_operativo=EstadoOperativo.CERRADO.value, hora_salida=None, salidas_unidad=[])
    en_curso = SimpleNamespace(estado_operativo=EstadoOperativo.EN_CURSO.value, hora_salida=None,
                               salidas_unidad=[SimpleNamespace(hora_salida=time(9, 0))])
    borrador = SimpleNamespace(estado_operativo=EstadoOperativo.EN_CURSO.value, hora_salida=None,
                               salidas_unidad=[SimpleNamespace(hora_salida=None)])
    assert (estado_parte(cerrado), estado_parte(en_curso), estado_parte(borrador)) == (
        "Finalizado", "En curso", "Borrador")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_planillas_oculta_ruba_sin_permiso(qapp, base):
    from app.ui.history_window import COL_ESTADO_PARTE, COL_SELECCION, HistoryWindow

    pagina = HistoryWindow()
    assert pagina.boton_cargar_lote.isHidden() and not pagina.label_sin_permiso_ruba.isHidden()
    assert pagina.tabla.isColumnHidden(COL_SELECCION)
    assert not pagina.boton_nueva_planilla.isHidden()
    estados = {pagina.tabla.item(f, COL_ESTADO_PARTE).text() for f in range(pagina.tabla.rowCount())}
    assert estados == {"✅ Finalizado", "⏱️ En curso"}
    emitidos = []
    pagina.carga_lote_solicitada.connect(emitidos.append)
    pagina._seleccionados = {1}
    from app.ui import history_window

    avisos = []
    original = history_window.QMessageBox.warning
    history_window.QMessageBox.warning = lambda *a, **k: avisos.append(a)
    try:
        pagina._cargar_seleccionados()
        pagina._reintentar_ruba(1, "001/2026")
    finally:
        history_window.QMessageBox.warning = original
    assert emitidos == [] and len(avisos) == 2

    pagina.set_permiso_ruba(True)
    assert not pagina.boton_cargar_lote.isHidden() and pagina.label_sin_permiso_ruba.isHidden()
    assert not pagina.tabla.isColumnHidden(COL_SELECCION)
    pagina.close()


def test_pagina_estadisticas_segun_nivel_de_acceso(qapp, base):
    from app.ui.estadisticas_view import PERIODO_PERSONALIZADO, PaginaEstadisticas, _a_qdate

    pagina = PaginaEstadisticas()
    pagina.combo_periodo.setCurrentText(PERIODO_PERSONALIZADO)
    pagina.fecha_desde.setDate(_a_qdate(DESDE))
    pagina.fecha_hasta.setDate(_a_qdate(HASTA))

    # Sin operador: KPIs generales, sin ficha personal ni datos de otros.
    assert not pagina.vista_completa and pagina.personal is None
    assert pagina.reporte.servicios == 2
    assert pagina.tabla_bomberos.isHidden() and pagina.combo_bombero.isHidden()
    assert pagina.grafico_bomberos.datos() == []

    # Bombero identificado: su propia estadística.
    pagina.set_operador(base.ceci, roles.ROL_BOMBERO)
    pagina.refrescar()
    assert pagina.personal.personal_id == base.ceci and not pagina.vista_completa
    assert pagina.tabla_bomberos.rowCount() == 0

    # Estadística: toda la base y la ficha de cualquier bombero.
    pagina.set_operador(base.ceci, roles.ROL_ESTADISTICA)
    pagina.refrescar()
    assert pagina.vista_completa and not pagina.tabla_bomberos.isHidden()
    assert pagina.tabla_bomberos.rowCount() == 3
    pagina.combo_bombero.setCurrentIndex(pagina.combo_bombero.findData(base.beto))
    assert pagina.personal.personal_id == base.beto and pagina.reporte.servicios == 2
    pagina.close()

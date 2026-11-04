"""Credenciales de Supabase en data/config.json (app de escritorio)."""

import json

import pytest

from app import paths
from app.services import supabase_client
from app.services.supabase_client import (
    SupabaseConfigError,
    SupabaseCredenciales,
    cargar_credenciales,
    guardar_credenciales,
)

INSTALACION_VIEJA = {
    "ruba": {"usuario": "guardia", "clave": "secreta", "url_login": "x", "url_incidentes": "y"},
    "cuartel": {"nombre": "BV", "localidad": "Adelia María", "codigo": "C 59 / R 3"},
    "actualizaciones": {"modo": "preguntar", "github_token": "tok"},
}


@pytest.fixture
def carpeta(monkeypatch, tmp_path):
    datos, recursos = tmp_path / "data", tmp_path / "bundle"
    (recursos / "data").mkdir(parents=True)
    datos.mkdir()
    (recursos / "data" / "config.example.json").write_text(json.dumps({
        "ruba": {"usuario": "", "clave": "", "url_login": "x", "url_incidentes": "y"},
        "supabase": {"url": "", "service_key": ""},
    }), encoding="utf-8")
    monkeypatch.setattr(paths, "get_writable_dir", lambda _sub: datos)
    monkeypatch.setattr(paths, "get_resource_path", lambda rel: recursos / rel)
    monkeypatch.setattr(supabase_client, "get_writable_dir", lambda _sub: datos)
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    return datos


def test_completar_config_no_pisa_valores():
    completa, cambio = paths.completar_config({"a": {"x": ""}, "b": 1}, {"a": {"x": "def", "y": 2}, "c": {}})
    assert cambio and completa == {"a": {"x": "", "y": 2}, "b": 1, "c": {}}
    assert paths.completar_config(completa, {"a": {"x": "otro"}}) == (completa, False)


def test_instalacion_vieja_recibe_la_seccion_supabase(carpeta):
    config = carpeta / "config.json"
    config.write_text("﻿" + json.dumps(INSTALACION_VIEJA), encoding="utf-8")   # con BOM (Bloc de notas)
    paths.asegurar_config_inicial()
    datos = json.loads(config.read_text(encoding="utf-8"))
    assert datos["supabase"] == {"url": paths.URL_SUPABASE_PREDETERMINADA, "service_key": ""}
    assert datos["ruba"]["clave"] == "secreta" and datos["actualizaciones"]["github_token"] == "tok"
    assert json.loads((carpeta / "config.json.bak").read_text(encoding="utf-8-sig")) == INSTALACION_VIEJA
    antes = config.read_text(encoding="utf-8")
    paths.asegurar_config_inicial()                      # idempotente
    assert config.read_text(encoding="utf-8") == antes


def test_primer_arranque_crea_config_desde_plantilla(carpeta):
    paths.asegurar_config_inicial()
    datos = json.loads((carpeta / "config.json").read_text(encoding="utf-8"))
    assert datos["supabase"]["url"] == paths.URL_SUPABASE_PREDETERMINADA and "ruba" in datos


def test_config_rota_no_se_reescribe(carpeta):
    (carpeta / "config.json").write_text("{ roto", encoding="utf-8")
    paths.asegurar_config_inicial()
    assert (carpeta / "config.json").read_text(encoding="utf-8") == "{ roto"


def test_sin_key_error_claro_y_url_por_defecto(carpeta):
    (carpeta / "config.json").write_text(json.dumps({"supabase": {"url": "", "service_key": ""}}), encoding="utf-8")
    with pytest.raises(SupabaseConfigError, match="Configurar conexión"):
        cargar_credenciales()
    (carpeta / "config.json").write_text("﻿" + json.dumps({"supabase": {"service_key": "k1"}}), encoding="utf-8")
    assert cargar_credenciales() == SupabaseCredenciales(paths.URL_SUPABASE_PREDETERMINADA, "k1")


@pytest.mark.parametrize("contenido", [
    {"Supabase": {"url": "https://a.co/", "service_role_key": "k2"}},
    {"SUPABASE_URL": "https://a.co/", "SUPABASE_SERVICE_KEY": "k2"},
])
def test_variantes_de_nombres(carpeta, contenido):
    (carpeta / "config.json").write_text(json.dumps(contenido), encoding="utf-8")
    assert cargar_credenciales() == SupabaseCredenciales("https://a.co", "k2")


def test_env_tiene_prioridad(carpeta, monkeypatch):
    (carpeta / "config.json").write_text(json.dumps({"supabase": {"service_key": "k-cfg"}}), encoding="utf-8")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k-env")
    assert cargar_credenciales().service_key == "k-env"


def test_guardar_credenciales_conserva_lo_demas(carpeta):
    (carpeta / "config.json").write_text(json.dumps(INSTALACION_VIEJA), encoding="utf-8")
    guardar_credenciales("https://p.supabase.co/", " k3 ")
    datos = json.loads((carpeta / "config.json").read_text(encoding="utf-8"))
    assert datos["supabase"] == {"url": "https://p.supabase.co", "service_key": "k3"}
    assert datos["ruba"] == INSTALACION_VIEJA["ruba"]
    assert cargar_credenciales() == SupabaseCredenciales("https://p.supabase.co", "k3")

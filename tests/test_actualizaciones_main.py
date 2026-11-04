"""Auto-actualización contra la rama main: commits sin publicar (API compare)
en el ejecutable, y `git pull --ff-only` seguro en desarrollo."""

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app.services import updater


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin (bare) + un checkout 'local' en main + otro 'dev' que publica commits."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    for repo in (dev,):
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
    (dev / "a.txt").write_text("1")
    _git(dev, "add", ".")
    _git(dev, "commit", "-qm", "inicial")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", "-b", "main", str(origin), str(local))
    (dev / "a.txt").write_text("2")
    _git(dev, "commit", "-qam", "feat: cambio nuevo")
    _git(dev, "push", "-q", "origin", "HEAD:main")
    return local


def test_git_detecta_commits_y_trae_con_ff_only(repos):
    info = updater.buscar_en_git(repos)
    assert info.origen == "git" and info.commits_nuevos == 1 and info.arbol_limpio
    assert "feat: cambio nuevo" in info.notas
    resumen = updater.traer_cambios_git(repos)
    assert "→" in resumen and (repos / "a.txt").read_text() == "2"
    assert updater.buscar_en_git(repos) is None


def test_git_no_pisa_cambios_locales(repos):
    (repos / "a.txt").write_text("cambio local")
    info = updater.buscar_en_git(repos)
    assert info.commits_nuevos == 1 and not info.arbol_limpio
    with pytest.raises(updater.ActualizacionError, match="cambios locales"):
        updater.traer_cambios_git(repos)
    assert (repos / "a.txt").read_text() == "cambio local"


def test_git_fuera_de_main_no_se_actualiza(repos):
    _git(repos, "checkout", "-qb", "otra")
    assert not updater.buscar_en_git(repos).arbol_limpio
    with pytest.raises(updater.ActualizacionError, match="rama 'otra'"):
        updater.traer_cambios_git(repos)


def test_commits_sin_publicar_lee_compare():
    respuesta = {"ahead_by": 3, "commits": [{"commit": {"message": "a"}}, {"commit": {"message": "fix: b\n\ncuerpo"}}]}
    with mock.patch.object(updater, "_consultar_github", return_value=respuesta) as consulta:
        assert updater.commits_sin_publicar("1.2.3") == (3, "fix: b")
    assert consulta.call_args.args[0].endswith("/compare/v1.2.3...main")


def test_commits_sin_publicar_sin_internet_no_falla():
    with mock.patch.object(updater, "_consultar_github", side_effect=OSError("sin red")):
        assert updater.commits_sin_publicar("1.2.3") is None


def test_ejecutable_sin_release_nuevo_avisa_commits_pendientes():
    with mock.patch.object(updater, "is_frozen", return_value=True), \
            mock.patch.object(updater, "buscar_en_releases", return_value=None), \
            mock.patch.object(updater, "commits_sin_publicar", return_value=(12, "feat: x")):
        info = updater.buscar_actualizacion()
    assert info.origen == "sin_publicar" and info.commits_nuevos == 12
    with mock.patch.object(updater, "is_frozen", return_value=True), \
            mock.patch.object(updater, "buscar_en_releases", return_value=None), \
            mock.patch.object(updater, "commits_sin_publicar", return_value=(0, "")):
        assert updater.buscar_actualizacion() is None

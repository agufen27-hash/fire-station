"""
Publica una versión nueva de Fire Station (máquina del desarrollador).

    python tools/release.py 1.0.1 --notas "Corrige el Paso 3 de RUBA"
    python tools/release.py 1.0.1 --sin-publicar     # solo versión + build + paquete

Pasos:
  1. Verifica: versión X.Y.Z mayor a la actual -- o IGUAL, si app/__init__.py
     ya se subió a mano y el tag vX.Y.Z todavía no existe --, rama main,
     árbol limpio y (salvo --sin-publicar) la variable GITHUB_TOKEN.
  2. Escribe la versión en app/__init__.py (si no estaba ya).
  3. Compila con tools/build_exe.py (si falla, se restaura la versión anterior).
  4. Arma dist/FireStation-update-X.Y.Z.zip: FireStation.exe + _internal/ SIN
     el Chromium de Playwright (_internal/ms-playwright). Nunca incluye data/,
     output/, logs/ ni resources/ de al lado del .exe, y aborta si dentro de
     _internal/ aparece un dato real (base SQLite, config.json, semilla...).
  5. Escribe dist/update.json (versión, SHA-256, Chromium requerido, notas).
  6. Commit "Release vX.Y.Z" (si hubo cambio de versión), tag vX.Y.Z y push
     de main y del tag.
  7. Crea el GitHub Release vX.Y.Z y sube el .zip y update.json (lo que lee
     app/services/updater.py en las PCs de guardia).

GITHUB_TOKEN: token personal de GitHub con permiso "Contents: Read and write"
sobre el repo (fine-grained) o scope "repo" (classic). No se guarda en ningún
archivo: se lee del entorno.

Primera instalación en una PC nueva: la carpeta completa dist/FireStation/
(con Chromium) se sigue copiando a mano; las siguientes versiones llegan solas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.services.updater import (  # noqa: E402
    CARPETA_CHROMIUM,
    CARPETA_INTERNA,
    NOMBRE_EXE,
    NOMBRE_MANIFIESTO,
    REPO_GITHUB,
    validar_entradas_paquete,
    version_tupla,
)

ARCHIVO_VERSION = BASE_DIR / "app" / "__init__.py"
DIST_APP = BASE_DIR / "dist" / "FireStation"
DIST = BASE_DIR / "dist"
RE_LINEA_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

# Datos reales que jamás pueden viajar en el paquete aunque PyInstaller los
# hubiera copiado dentro de _internal/ (el esqueleto config.example.json sí).
ARCHIVOS_PROHIBIDOS = {"config.json", "seed_bomberos.json", "ruba_sesion.json", "historial.json",
                       "cuerpo_activo.json", "Reporte de bomberos.xlsx"}
EXTENSIONES_PROHIBIDAS = {".db", ".db-journal", ".db-wal", ".log"}
CARPETAS_PROHIBIDAS_INTERNAS = {"logs", "output", "firmas", "legajos", "respaldos"}


def git(*args: str, capturar: bool = True) -> str:
    resultado = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=capturar, text=True)
    if resultado.returncode != 0:
        sys.exit(f"git {' '.join(args)} falló:\n{(resultado.stderr or '').strip()}")
    return (resultado.stdout or "").strip()


def version_actual() -> str:
    m = RE_LINEA_VERSION.search(ARCHIVO_VERSION.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"No se encontró __version__ en {ARCHIVO_VERSION}")
    return m.group(1)


def escribir_version(version: str) -> None:
    texto = ARCHIVO_VERSION.read_text(encoding="utf-8")
    ARCHIVO_VERSION.write_text(RE_LINEA_VERSION.sub(f'__version__ = "{version}"', texto, count=1), encoding="utf-8")


def verificar_precondiciones(version: str, publicar: bool) -> str:
    actual = version_actual()
    try:
        nueva, vigente = version_tupla(version), version_tupla(actual)
    except ValueError as e:
        sys.exit(str(e))
    # Igual a la actual solo si se subió a mano en app/__init__.py y todavía
    # no se publicó (el tag se controla abajo).
    if nueva < vigente:
        sys.exit(f"La versión {version} tiene que ser mayor o igual a la actual ({actual}).")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        sys.exit("Hay que publicar desde la rama main.")
    if git("status", "--porcelain"):
        sys.exit("Hay cambios sin commitear: commiteá o descartalos antes de publicar.")
    if git("tag", "--list", f"v{version}"):
        sys.exit(f"El tag v{version} ya existe.")
    if publicar and not os.environ.get("GITHUB_TOKEN"):
        sys.exit("Falta la variable de entorno GITHUB_TOKEN (o usá --sin-publicar).")
    return actual


def compilar(version_anterior: str) -> None:
    print("\n== Compilando con tools/build_exe.py ==")
    resultado = subprocess.run([sys.executable, str(BASE_DIR / "tools" / "build_exe.py")], cwd=BASE_DIR)
    if resultado.returncode != 0 or not (DIST_APP / NOMBRE_EXE).is_file():
        escribir_version(version_anterior)
        sys.exit("La compilación falló: se restauró la versión anterior en app/__init__.py.")


def armar_paquete(version: str, notas: str) -> tuple[Path, Path]:
    zip_ruta = DIST / f"FireStation-update-{version}.zip"
    print(f"\n== Armando {zip_ruta.name} (sin Chromium ni carpetas de datos) ==")
    entradas = [DIST_APP / NOMBRE_EXE] + sorted(
        p for p in (DIST_APP / CARPETA_INTERNA).rglob("*")
        if p.is_file() and p.relative_to(DIST_APP / CARPETA_INTERNA).parts[0] != CARPETA_CHROMIUM
    )
    nombres = [p.relative_to(DIST_APP).as_posix() for p in entradas]
    validar_entradas_paquete(nombres)  # el mismo control que hace el updater al instalar
    verificar_sin_datos_reales(nombres)
    with zipfile.ZipFile(zip_ruta, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for ruta, nombre in zip(entradas, nombres):
            z.write(ruta, nombre)

    h = hashlib.sha256()
    with zip_ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    chromium_dir = DIST_APP / CARPETA_INTERNA / CARPETA_CHROMIUM
    manifiesto = {
        "version": version,
        "paquete": zip_ruta.name,
        "sha256": h.hexdigest(),
        "tamano": zip_ruta.stat().st_size,
        "chromium": sorted(p.name for p in chromium_dir.iterdir() if p.is_dir()) if chromium_dir.is_dir() else [],
        "notas": notas,
    }
    if not manifiesto["chromium"]:
        print("   AVISO: no se encontró _internal/ms-playwright en el build: el manifiesto no exige Chromium.")
    with zipfile.ZipFile(zip_ruta) as z:  # el zip recién escrito se puede leer entero
        dañado = z.testzip()
    if dañado:
        sys.exit(f"El paquete quedó dañado ({dañado}): volvé a correr el release.")
    manifiesto_ruta = DIST / NOMBRE_MANIFIESTO
    manifiesto_ruta.write_text(json.dumps(manifiesto, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   {zip_ruta.stat().st_size / 1_048_576:.0f} MB · {len(nombres)} archivos · sha256 {manifiesto['sha256'][:16]}…")
    return zip_ruta, manifiesto_ruta


def verificar_sin_datos_reales(nombres: list[str]) -> None:
    """Corta el release si dentro de _internal/ hay datos del cuartel."""
    problemas = []
    for nombre in nombres:
        ruta = Path(nombre)
        if (ruta.name in ARCHIVOS_PROHIBIDOS or ruta.suffix.lower() in EXTENSIONES_PROHIBIDAS
                or CARPETAS_PROHIBIDAS_INTERNAS.intersection(ruta.parts[1:-1])):
            problemas.append(nombre)
    if problemas:
        sys.exit("El build trae datos reales que no pueden publicarse:\n  " + "\n  ".join(problemas[:20]))


def _api(url: str, token: str, datos: bytes | None = None, tipo: str = "application/json",
         metodo: str = "POST") -> dict:
    pedido = urllib.request.Request(url, data=datos, method=metodo, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": tipo, "User-Agent": "FireStation-release",
    })
    with urllib.request.urlopen(pedido, timeout=600) as resp:  # noqa: S310 - API de GitHub
        return json.loads(resp.read().decode("utf-8") or "{}")


def publicar_release(version: str, notas: str, archivos: list[Path]) -> str:
    token = os.environ["GITHUB_TOKEN"]
    print(f"\n== Creando el GitHub Release v{version} ==")
    release = _api(f"https://api.github.com/repos/{REPO_GITHUB}/releases", token, json.dumps({
        "tag_name": f"v{version}", "name": f"Fire Station {version}", "body": notas or f"Fire Station {version}",
        "draft": False, "prerelease": False,
    }).encode("utf-8"))
    base_subida = release["upload_url"].split("{", 1)[0]
    for archivo in archivos:
        print(f"   subiendo {archivo.name}…")
        tipo = "application/zip" if archivo.suffix == ".zip" else "application/json"
        _api(f"{base_subida}?name={archivo.name}", token, archivo.read_bytes(), tipo)
    return release.get("html_url", "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publica una versión nueva de Fire Station.")
    parser.add_argument("version", help="Versión nueva, formato X.Y.Z (ej: 1.0.1)")
    parser.add_argument("--notas", default="", help="Novedades (se muestran en el aviso de actualización)")
    parser.add_argument("--sin-publicar", action="store_true",
                        help="Solo versión + build + paquete: sin commit, tag, push ni Release")
    args = parser.parse_args()
    version = args.version.lstrip("v")
    publicar = not args.sin_publicar

    anterior = verificar_precondiciones(version, publicar)
    if anterior == version:
        print(f"Fire Station {version} (ya estaba en app/__init__.py; se publica sin cambiar la versión)")
    else:
        print(f"Fire Station {anterior} -> {version}")
        escribir_version(version)
    compilar(anterior)
    zip_ruta, manifiesto_ruta = armar_paquete(version, args.notas)

    if not publicar:
        print(f"\nListo (sin publicar). Paquete: {zip_ruta}\nManifiesto: {manifiesto_ruta}\n"
              f"app/__init__.py quedó en {version} sin commitear.")
        return

    print("\n== Commit, tag y push ==")
    git("add", str(ARCHIVO_VERSION.relative_to(BASE_DIR)))
    sin_cambios = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR).returncode == 0
    if sin_cambios:
        print(f"   app/__init__.py ya estaba en {version}: el tag va sobre el último commit.")
    else:
        git("commit", "-m", f"Release v{version}")
    git("tag", "-a", f"v{version}", "-m", f"Fire Station {version}")
    git("push", "origin", "main", capturar=False)
    git("push", "origin", f"v{version}", capturar=False)

    try:
        url = publicar_release(version, args.notas, [zip_ruta, manifiesto_ruta])
    except Exception as e:  # noqa: BLE001 - el tag ya está arriba: explicar cómo terminar a mano
        sys.exit(f"\nEl tag v{version} se subió, pero no se pudo crear el Release ({e}).\n"
                 f"Crealo a mano en https://github.com/{REPO_GITHUB}/releases/new con el tag v{version} "
                 f"y subí:\n  {zip_ruta}\n  {manifiesto_ruta}")
    print(f"\nPublicado: {url}\nLas PCs de guardia lo van a ofrecer al abrir Fire Station.")


if __name__ == "__main__":
    main()

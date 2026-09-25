"""
Genera el instalador de Windows dist/FireStation_Setup.exe con Inno Setup,
a partir de la carpeta compilada dist/FireStation/ (ver tools/installer.iss).

    python tools/build_installer.py              # usa dist/ (compila si falta)
    python tools/build_installer.py --compilar   # recompila el .exe antes
    python tools/build_installer.py --verificar  # solo valida installer.iss (no genera nada)

La versión sale de app/__init__.py (__version__) y se le pasa al .iss.
Requiere Inno Setup 6: https://jrsoftware.org/isdl.php
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
ISS = BASE_DIR / "tools" / "installer.iss"
DIST_APP = BASE_DIR / "dist" / "FireStation"
SALIDA = BASE_DIR / "dist" / "FireStation_Setup.exe"
URL_INNO = "https://jrsoftware.org/isdl.php"


def version_actual() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (BASE_DIR / "app" / "__init__.py").read_text(encoding="utf-8"),
                  re.MULTILINE)
    if not m:
        sys.exit("No se encontró __version__ en app/__init__.py")
    return m.group(1)


def _desde_registro() -> Optional[Path]:
    """InstallLocation de Inno Setup 6 (instalación por usuario o para todos)."""
    try:
        import winreg
    except ImportError:
        return None
    clave = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    for raiz, sub in ((winreg.HKEY_CURRENT_USER, clave), (winreg.HKEY_LOCAL_MACHINE, clave),
                      (winreg.HKEY_LOCAL_MACHINE, clave.replace("Software\\", "Software\\WOW6432Node\\"))):
        try:
            with winreg.OpenKey(raiz, sub) as k:
                ubicacion = Path(winreg.QueryValueEx(k, "InstallLocation")[0])
        except OSError:
            continue
        if (ubicacion / "ISCC.exe").is_file():
            return ubicacion / "ISCC.exe"
    return None


def buscar_iscc() -> Optional[Path]:
    en_path = shutil.which("ISCC") or shutil.which("iscc")
    if en_path:
        return Path(en_path)
    registro = _desde_registro()
    if registro:
        return registro
    candidatos = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
    ]
    return next((c for c in candidatos if c.is_file()), None)


def asegurar_dist(recompilar: bool) -> None:
    if recompilar or not (DIST_APP / "FireStation.exe").is_file():
        motivo = "se pidió --compilar" if recompilar else "no existe dist/FireStation/FireStation.exe"
        print(f"Compilando con tools/build_exe.py ({motivo})…")
        resultado = subprocess.run([sys.executable, str(BASE_DIR / "tools" / "build_exe.py")], cwd=BASE_DIR)
        if resultado.returncode != 0 or not (DIST_APP / "FireStation.exe").is_file():
            sys.exit("La compilación del .exe falló: no se puede armar el instalador.")
    datos = [c for c in ("data", "output", "logs", "resources") if (DIST_APP / c).exists()]
    if datos:
        # No viajan igual (installer.iss solo toma FireStation.exe y _internal\), pero conviene saberlo.
        print(f"Aviso: dist/FireStation/ tiene carpetas de datos ({', '.join(datos)}) de haber abierto el .exe "
              "ahí; el instalador NO las incluye.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera dist/FireStation_Setup.exe con Inno Setup.")
    parser.add_argument("--compilar", action="store_true", help="Recompilar el .exe con build_exe.py antes")
    parser.add_argument("--verificar", action="store_true", help="Solo validar installer.iss (sin generar)")
    args = parser.parse_args()

    iscc = buscar_iscc()
    if iscc is None:
        sys.exit(
            "No se encontró Inno Setup (ISCC.exe).\n"
            f"Descargalo e instalalo desde {URL_INNO} (Inno Setup 6) y volvé a correr este script."
        )

    version = version_actual()
    comando = [str(iscc), f"/DMyAppVersion={version}", str(ISS)]
    if args.verificar:
        comando.insert(1, "/O-")  # compila el script completo pero sin generar el instalador
    else:
        asegurar_dist(args.compilar)
    print(f"Inno Setup: {iscc}\nFire Station {version}\n")

    resultado = subprocess.run(comando, cwd=BASE_DIR / "tools")
    if resultado.returncode != 0:
        sys.exit(f"Inno Setup falló (código {resultado.returncode}).")
    if args.verificar:
        print("\ninstaller.iss: sintaxis OK.")
        return
    print(f"\nListo: {SALIDA} ({SALIDA.stat().st_size / 1_048_576:.0f} MB)\n"
          "Se instala por usuario, sin permisos de administrador, en %LOCALAPPDATA%\\Programs\\FireStation.")


if __name__ == "__main__":
    main()

"""
Punto de entrada de Fire Station.

Ejecutar con:  python main.py   (equivale a  python run.py)

Antes abría la versión vieja en customtkinter (ui/app.py), que no tiene el
auto-actualizador ni nada de lo posterior: ahora delega en run.py, el mismo
arranque que usa el ejecutable (FireStation.spec).
"""

from run import main

if __name__ == "__main__":
    main()

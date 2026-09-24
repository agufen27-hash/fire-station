"""
Ventana principal de Fire Station: arma el CTkTabview con las 3 pestañas
(Despacho, Historial, Configuración) y conecta los callbacks entre ellas.
"""

from __future__ import annotations

import customtkinter as ctk

from ui import theme
from ui.tab_configuracion import TabConfiguracion
from ui.tab_despacho import TabDespacho
from ui.tab_historial import TabHistorial

ANCHO_VENTANA = 1180
ALTO_VENTANA = 820


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        theme.aplicar_tema()

        self.title('Fire Station — Bomberos Voluntarios "Osvaldo R. Rossi" (C 59 / R 3)')
        self.geometry(f"{ANCHO_VENTANA}x{ALTO_VENTANA}")
        self.minsize(1000, 640)
        self.configure(fg_color="#1A1A1A")

        self._build_encabezado()
        self._build_tabs()

    def _build_encabezado(self) -> None:
        header = ctk.CTkFrame(self, fg_color=theme.ROJO, corner_radius=0, height=56)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="🚒  FIRE STATION", font=("Segoe UI Semibold", 20),
                     text_color="white").pack(side="left", padx=20)
        ctk.CTkLabel(header, text='Bomberos Voluntarios "Osvaldo R. Rossi" · Adelia María, Córdoba · C 59 / R 3',
                     font=theme.FUENTE_LABEL, text_color="#FFE0E0").pack(side="left", padx=6)

    def _build_tabs(self) -> None:
        self.tabview = ctk.CTkTabview(self, fg_color="#1A1A1A",
                                       segmented_button_fg_color=theme.GRIS_TARJETA_HEADER,
                                       segmented_button_selected_color=theme.ROJO,
                                       segmented_button_selected_hover_color=theme.ROJO_HOVER)
        self.tabview.pack(fill="both", expand=True)

        tab_despacho_frame = self.tabview.add("Despacho y Carga de Siniestro")
        tab_historial_frame = self.tabview.add("Historial de Salidas")
        tab_config_frame = self.tabview.add("Configuración y Catálogos")

        self.tab_historial = TabHistorial(tab_historial_frame)
        self.tab_despacho = TabDespacho(tab_despacho_frame, on_guardado=self._on_servicio_guardado)
        self.tab_configuracion = TabConfiguracion(tab_config_frame,
                                                    on_catalogos_actualizados=self._on_catalogos_actualizados)

    def _on_servicio_guardado(self, registro) -> None:
        self.tab_historial.refrescar()

    def _on_catalogos_actualizados(self) -> None:
        self.tab_despacho.refrescar_catalogos()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

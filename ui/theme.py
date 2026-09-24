"""
Tema visual institucional: modo oscuro con acentos rojo (Bomberos) y azul
(emergencias/RUBA). Controles grandes pensados para uso táctil en guardia.
"""

import customtkinter as ctk

# Paleta -----------------------------------------------------------------
ROJO = "#C62828"
ROJO_HOVER = "#8E0000"
AZUL = "#1565C0"
AZUL_HOVER = "#0D47A1"
VERDE = "#2E7D32"
VERDE_HOVER = "#1B5E20"
GRIS_TARJETA = "#232323"
GRIS_TARJETA_HEADER = "#2B2B2B"
GRIS_BORDE = "#3A3A3A"
TEXTO_MUTED = "#B0B0B0"

FUENTE_BASE = ("Segoe UI", 14)
FUENTE_LABEL = ("Segoe UI", 13)
FUENTE_TITULO_SECCION = ("Segoe UI Semibold", 17)
FUENTE_TITULO_APP = ("Segoe UI Semibold", 22)
FUENTE_TOTALES = ("Segoe UI Semibold", 20)
FUENTE_BOTON_GRANDE = ("Segoe UI Semibold", 15)

ALTO_CONTROL = 38
ALTO_BOTON_ACCION = 46


def aplicar_tema() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")


def boton_primario_kwargs() -> dict:
    return dict(fg_color=ROJO, hover_color=ROJO_HOVER, font=FUENTE_BOTON_GRANDE, height=ALTO_BOTON_ACCION)


def boton_secundario_kwargs() -> dict:
    return dict(fg_color="transparent", border_width=1, border_color=GRIS_BORDE,
                hover_color=GRIS_TARJETA_HEADER, font=FUENTE_BOTON_GRANDE, height=ALTO_BOTON_ACCION)


def boton_ahora_kwargs() -> dict:
    return dict(fg_color=AZUL, hover_color=AZUL_HOVER, width=64, height=ALTO_CONTROL, font=FUENTE_LABEL)

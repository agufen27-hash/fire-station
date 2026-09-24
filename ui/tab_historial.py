"""
Pestaña "Historial de Salidas": tabla interactiva con los servicios
guardados en `data/historial.json` (o la base que lo reemplace más adelante).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from core import historial_store
from core.models import RegistroServicio
from ui import theme

COLUMNAS = [
    ("n_siniestro", "N° Siniestro", 110),
    ("fecha", "Fecha", 90),
    ("tipo", "Tipo", 120),
    ("subtipo", "Subtipo", 120),
    ("lugar", "Calle / Lugar", 220),
    ("magnitud", "Magnitud", 70),
    ("moviles", "Móviles", 140),
    ("total_general", "Total Gral.", 80),
    ("ruba", "RUBA", 70),
]


class TabHistorial:
    def __init__(self, master):
        self.master = master
        self._registros: list[RegistroServicio] = []

        self._configurar_estilo_tabla()

        contenedor = ctk.CTkFrame(master, fg_color="transparent")
        contenedor.pack(fill="both", expand=True, padx=12, pady=12)
        contenedor.grid_columnconfigure(0, weight=1)
        contenedor.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(contenedor, text="Historial de Salidas", font=theme.FUENTE_TITULO_APP).grid(
            row=0, column=0, sticky="w", pady=(0, 10))

        barra = ctk.CTkFrame(contenedor, fg_color="transparent")
        barra.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        barra.grid_columnconfigure(0, weight=1)

        self.entry_filtro = ctk.CTkEntry(barra, placeholder_text="Buscar por N° de siniestro, calle o tipo…",
                                          height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_filtro.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.entry_filtro.bind("<KeyRelease>", lambda e: self._aplicar_filtro())

        ctk.CTkButton(barra, text="Actualizar", command=self.refrescar,
                      fg_color=theme.AZUL, hover_color=theme.AZUL_HOVER, width=120,
                      height=theme.ALTO_CONTROL).grid(row=0, column=1)

        tabla_frame = ctk.CTkFrame(contenedor, fg_color=theme.GRIS_TARJETA, corner_radius=8)
        tabla_frame.grid(row=2, column=0, sticky="nsew")
        tabla_frame.grid_columnconfigure(0, weight=1)
        tabla_frame.grid_rowconfigure(0, weight=1)

        columnas_ids = [c[0] for c in COLUMNAS]
        self.tabla = ttk.Treeview(tabla_frame, columns=columnas_ids, show="headings",
                                   style="FireStation.Treeview")
        for col_id, titulo, ancho in COLUMNAS:
            self.tabla.heading(col_id, text=titulo)
            self.tabla.column(col_id, width=ancho, anchor="center" if col_id != "lugar" else "w")
        self.tabla.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)

        scrollbar = ttk.Scrollbar(tabla_frame, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.tabla.bind("<Double-1>", self._mostrar_detalle)

        self.label_resumen = ctk.CTkLabel(contenedor, text="", font=theme.FUENTE_LABEL,
                                           text_color=theme.TEXTO_MUTED)
        self.label_resumen.grid(row=3, column=0, sticky="w", pady=(8, 0))

        self.refrescar()

    def _configurar_estilo_tabla(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("FireStation.Treeview", background=theme.GRIS_TARJETA,
                         fieldbackground=theme.GRIS_TARJETA, foreground="white",
                         rowheight=28, borderwidth=0, font=("Segoe UI", 11))
        style.configure("FireStation.Treeview.Heading", background=theme.GRIS_TARJETA_HEADER,
                         foreground="white", font=("Segoe UI Semibold", 11), borderwidth=0)
        style.map("FireStation.Treeview", background=[("selected", theme.ROJO)],
                  foreground=[("selected", "white")])

    def refrescar(self) -> None:
        self._registros = sorted(historial_store.listar_registros(),
                                  key=lambda r: r.pedido_socorro.fecha, reverse=True)
        self._aplicar_filtro()

    def _aplicar_filtro(self) -> None:
        texto = self.entry_filtro.get().strip().lower()
        self.tabla.delete(*self.tabla.get_children())

        visibles = 0
        for registro in self._registros:
            ps, ds = registro.pedido_socorro, registro.datos_siniestro
            campos_busqueda = f"{ps.n_siniestro} {ds.calle_lugar} {ds.tipo.value} {ds.subtipo}".lower()
            if texto and texto not in campos_busqueda:
                continue

            moviles = ", ".join(d.movil for d in registro.dotaciones if d.movil.strip()) or "—"
            self.tabla.insert("", "end", iid=str(registro.id), values=(
                ps.n_siniestro,
                ps.fecha.strftime("%d/%m/%Y"),
                ds.tipo.value,
                ds.subtipo or "—",
                ds.calle_lugar or "—",
                ds.magnitud.value,
                moviles,
                registro.total_general(),
                "Sí" if registro.cargado_ruba else "No",
            ))
            visibles += 1

        self.label_resumen.configure(
            text=f"{visibles} de {len(self._registros)} servicio(s) registrados. "
                 "Doble clic sobre una fila para ver el detalle."
        )

    def _mostrar_detalle(self, event) -> None:
        seleccion = self.tabla.focus()
        if not seleccion:
            return
        registro = next((r for r in self._registros if str(r.id) == seleccion), None)
        if registro is None:
            return

        ps, ds = registro.pedido_socorro, registro.datos_siniestro
        lineas = [
            f"N° Siniestro: {ps.n_siniestro}   ({ps.dia} {ps.fecha.strftime('%d/%m/%Y')}, "
            f"llamado {ps.hora_llamado.strftime('%H:%M')})",
            f"Comunicó: {ps.comunico or '—'}   |   Domicilio: {ps.domicilio_denunciante or '—'}",
            f"Tipo: {ds.tipo.value} / {ds.subtipo or '—'}   |   Magnitud: {ds.magnitud.value}",
            f"Lugar: {ds.calle_lugar or '—'}, {ds.zona_barrio or '—'}, {ds.localidad}",
            f"Relato: {ds.descripcion or '—'}",
            "",
            f"Total en Cuartel: {registro.total_cuartel()}   |   "
            f"Total en Servicio: {registro.total_servicio()}   |   "
            f"Total General: {registro.total_general()}",
            "",
        ]
        for d in registro.dotaciones:
            lineas.append(
                f"Dotación {d.numero} — Móvil: {d.movil or '—'}   |   "
                f"Jefe: {d.personal.jefe_dotacion.nombre_completo() or '—'}   |   "
                f"Dotación: {d.personal.cantidad()} bombero(s)"
            )

        ventana = ctk.CTkToplevel(self.master)
        ventana.title(f"Detalle — Siniestro {ps.n_siniestro}")
        ventana.geometry("620x420")
        ventana.configure(fg_color=theme.GRIS_TARJETA)

        texto = ctk.CTkTextbox(ventana, font=theme.FUENTE_BASE, wrap="word")
        texto.pack(fill="both", expand=True, padx=14, pady=14)
        texto.insert("1.0", "\n".join(lineas))
        texto.configure(state="disabled")

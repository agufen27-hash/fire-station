"""
Pestaña "Configuración y Catálogos": alta/baja de personal y móviles, y
credenciales del portal RUBA usadas más adelante por el bot de Playwright.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

import customtkinter as ctk

from core import catalogos
from ui import theme


class TabConfiguracion:
    def __init__(self, master, on_catalogos_actualizados: Optional[Callable[[], None]] = None):
        self.master = master
        self.on_catalogos_actualizados = on_catalogos_actualizados

        self._configurar_estilo_tabla()

        ctk.CTkLabel(master, text="Configuración y Catálogos", font=theme.FUENTE_TITULO_APP).pack(
            anchor="w", padx=16, pady=(16, 8))

        tabview = ctk.CTkTabview(master, fg_color=theme.GRIS_TARJETA_HEADER,
                                  segmented_button_fg_color=theme.GRIS_TARJETA)
        tabview.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        tab_personal = tabview.add("Personal")
        tab_moviles = tabview.add("Móviles")
        tab_ruba = tabview.add("RUBA")

        self._build_personal(tab_personal)
        self._build_moviles(tab_moviles)
        self._build_ruba(tab_ruba)

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

    def _avisar_actualizacion(self) -> None:
        if self.on_catalogos_actualizados:
            self.on_catalogos_actualizados()

    # -- Personal -----------------------------------------------------------

    def _build_personal(self, master) -> None:
        self._personal: list[dict] = catalogos.cargar_cuerpo_activo(solo_activos=False)
        self._personal_seleccion: Optional[int] = None

        master.grid_columnconfigure(0, weight=1)
        master.grid_rowconfigure(0, weight=1)

        tabla_frame = ctk.CTkFrame(master, fg_color=theme.GRIS_TARJETA, corner_radius=8)
        tabla_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=(8, 8))
        tabla_frame.grid_columnconfigure(0, weight=1)
        tabla_frame.grid_rowconfigure(0, weight=1)

        columnas = [("apellido", "Apellido", 140), ("nombre", "Nombre", 140),
                    ("grado", "Grado", 160), ("activo", "Activo", 70)]
        self.tabla_personal = ttk.Treeview(tabla_frame, columns=[c[0] for c in columnas],
                                            show="headings", style="FireStation.Treeview")
        for col_id, titulo, ancho in columnas:
            self.tabla_personal.heading(col_id, text=titulo)
            self.tabla_personal.column(col_id, width=ancho, anchor="center")
        self.tabla_personal.grid(row=0, column=0, sticky="nsew")
        self.tabla_personal.bind("<<TreeviewSelect>>", self._on_seleccion_personal)

        scrollbar = ttk.Scrollbar(tabla_frame, orient="vertical", command=self.tabla_personal.yview)
        self.tabla_personal.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        form = ctk.CTkFrame(master, fg_color=theme.GRIS_TARJETA_HEADER, corner_radius=8)
        form.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))
        form.grid_columnconfigure((0, 1, 2), weight=1)

        self.entry_p_nombre = self._campo_form(form, 0, 0, "Nombre")
        self.entry_p_apellido = self._campo_form(form, 0, 1, "Apellido")

        ctk.CTkLabel(form, text="Grado", font=theme.FUENTE_LABEL).grid(row=0, column=2, sticky="w", padx=8)
        self.combo_p_grado = ctk.CTkComboBox(form, values=catalogos.GRADOS, height=theme.ALTO_CONTROL,
                                              font=theme.FUENTE_BASE)
        self.combo_p_grado.grid(row=1, column=2, sticky="ew", padx=8, pady=(0, 10))

        self.check_p_activo = ctk.CTkCheckBox(form, text="Activo", font=theme.FUENTE_LABEL)
        self.check_p_activo.select()
        self.check_p_activo.grid(row=2, column=0, sticky="w", padx=8, pady=(0, 10))

        botones = ctk.CTkFrame(form, fg_color="transparent")
        botones.grid(row=2, column=1, columnspan=2, sticky="e", padx=8, pady=(0, 10))
        ctk.CTkButton(botones, text="Nuevo", command=self._limpiar_form_personal,
                      **theme.boton_secundario_kwargs()).pack(side="left", padx=4)
        ctk.CTkButton(botones, text="Eliminar", command=self._eliminar_personal,
                      fg_color=theme.ROJO, hover_color=theme.ROJO_HOVER,
                      height=theme.ALTO_BOTON_ACCION).pack(side="left", padx=4)
        ctk.CTkButton(botones, text="Guardar", command=self._guardar_personal,
                      fg_color=theme.VERDE, hover_color=theme.VERDE_HOVER,
                      height=theme.ALTO_BOTON_ACCION).pack(side="left", padx=4)

        self._refrescar_tabla_personal()

    def _campo_form(self, master, row: int, col: int, texto: str) -> ctk.CTkEntry:
        ctk.CTkLabel(master, text=texto, font=theme.FUENTE_LABEL).grid(row=row, column=col, sticky="w", padx=8)
        entry = ctk.CTkEntry(master, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        entry.grid(row=row + 1, column=col, sticky="ew", padx=8, pady=(0, 10))
        return entry

    def _refrescar_tabla_personal(self) -> None:
        self.tabla_personal.delete(*self.tabla_personal.get_children())
        for i, p in enumerate(self._personal):
            self.tabla_personal.insert("", "end", iid=str(i), values=(
                p.get("apellido", ""), p.get("nombre", ""), p.get("grado", ""),
                "Sí" if p.get("activo", True) else "No",
            ))

    def _on_seleccion_personal(self, event) -> None:
        seleccion = self.tabla_personal.focus()
        if not seleccion:
            return
        i = int(seleccion)
        p = self._personal[i]
        self._personal_seleccion = i
        self.entry_p_nombre.delete(0, "end")
        self.entry_p_nombre.insert(0, p.get("nombre", ""))
        self.entry_p_apellido.delete(0, "end")
        self.entry_p_apellido.insert(0, p.get("apellido", ""))
        self.combo_p_grado.set(p.get("grado", ""))
        if p.get("activo", True):
            self.check_p_activo.select()
        else:
            self.check_p_activo.deselect()

    def _limpiar_form_personal(self) -> None:
        self._personal_seleccion = None
        self.tabla_personal.selection_remove(self.tabla_personal.selection())
        self.entry_p_nombre.delete(0, "end")
        self.entry_p_apellido.delete(0, "end")
        self.combo_p_grado.set("")
        self.check_p_activo.select()

    def _guardar_personal(self) -> None:
        nombre = self.entry_p_nombre.get().strip()
        apellido = self.entry_p_apellido.get().strip()
        grado = self.combo_p_grado.get().strip()
        if not nombre or not apellido:
            messagebox.showerror("Dato inválido", "Nombre y apellido son obligatorios.")
            return

        registro = {"nombre": nombre, "apellido": apellido, "grado": grado,
                    "activo": bool(self.check_p_activo.get())}

        if self._personal_seleccion is not None:
            self._personal[self._personal_seleccion] = registro
        else:
            self._personal.append(registro)

        catalogos.guardar_cuerpo_activo(self._personal)
        self._personal = catalogos.cargar_cuerpo_activo(solo_activos=False)
        self._refrescar_tabla_personal()
        self._limpiar_form_personal()
        self._avisar_actualizacion()

    def _eliminar_personal(self) -> None:
        if self._personal_seleccion is None:
            messagebox.showinfo("Eliminar", "Seleccioná primero una fila de la tabla.")
            return
        del self._personal[self._personal_seleccion]
        catalogos.guardar_cuerpo_activo(self._personal)
        self._refrescar_tabla_personal()
        self._limpiar_form_personal()
        self._avisar_actualizacion()

    # -- Móviles --------------------------------------------------------------

    def _build_moviles(self, master) -> None:
        self._moviles: list[dict] = catalogos.cargar_parque_automotor(solo_activos=False)
        self._moviles_seleccion: Optional[int] = None

        master.grid_columnconfigure(0, weight=1)
        master.grid_rowconfigure(0, weight=1)

        tabla_frame = ctk.CTkFrame(master, fg_color=theme.GRIS_TARJETA, corner_radius=8)
        tabla_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=(8, 8))
        tabla_frame.grid_columnconfigure(0, weight=1)
        tabla_frame.grid_rowconfigure(0, weight=1)

        columnas = [("codigo", "Código", 90), ("nombre", "Nombre", 180),
                    ("tipo", "Tipo", 160), ("activo", "Activo", 70)]
        self.tabla_moviles = ttk.Treeview(tabla_frame, columns=[c[0] for c in columnas],
                                           show="headings", style="FireStation.Treeview")
        for col_id, titulo, ancho in columnas:
            self.tabla_moviles.heading(col_id, text=titulo)
            self.tabla_moviles.column(col_id, width=ancho, anchor="center")
        self.tabla_moviles.grid(row=0, column=0, sticky="nsew")
        self.tabla_moviles.bind("<<TreeviewSelect>>", self._on_seleccion_movil)

        scrollbar = ttk.Scrollbar(tabla_frame, orient="vertical", command=self.tabla_moviles.yview)
        self.tabla_moviles.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        form = ctk.CTkFrame(master, fg_color=theme.GRIS_TARJETA_HEADER, corner_radius=8)
        form.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))
        form.grid_columnconfigure((0, 1, 2), weight=1)

        self.entry_m_codigo = self._campo_form(form, 0, 0, "Código (ej. B-1)")
        self.entry_m_nombre = self._campo_form(form, 0, 1, "Nombre")
        self.entry_m_tipo = self._campo_form(form, 0, 2, "Tipo")

        self.check_m_activo = ctk.CTkCheckBox(form, text="Activo", font=theme.FUENTE_LABEL)
        self.check_m_activo.select()
        self.check_m_activo.grid(row=2, column=0, sticky="w", padx=8, pady=(0, 10))

        botones = ctk.CTkFrame(form, fg_color="transparent")
        botones.grid(row=2, column=1, columnspan=2, sticky="e", padx=8, pady=(0, 10))
        ctk.CTkButton(botones, text="Nuevo", command=self._limpiar_form_movil,
                      **theme.boton_secundario_kwargs()).pack(side="left", padx=4)
        ctk.CTkButton(botones, text="Eliminar", command=self._eliminar_movil,
                      fg_color=theme.ROJO, hover_color=theme.ROJO_HOVER,
                      height=theme.ALTO_BOTON_ACCION).pack(side="left", padx=4)
        ctk.CTkButton(botones, text="Guardar", command=self._guardar_movil,
                      fg_color=theme.VERDE, hover_color=theme.VERDE_HOVER,
                      height=theme.ALTO_BOTON_ACCION).pack(side="left", padx=4)

        self._refrescar_tabla_moviles()

    def _refrescar_tabla_moviles(self) -> None:
        self.tabla_moviles.delete(*self.tabla_moviles.get_children())
        for i, m in enumerate(self._moviles):
            self.tabla_moviles.insert("", "end", iid=str(i), values=(
                m.get("codigo", ""), m.get("nombre", ""), m.get("tipo", ""),
                "Sí" if m.get("activo", True) else "No",
            ))

    def _on_seleccion_movil(self, event) -> None:
        seleccion = self.tabla_moviles.focus()
        if not seleccion:
            return
        i = int(seleccion)
        m = self._moviles[i]
        self._moviles_seleccion = i
        self.entry_m_codigo.delete(0, "end")
        self.entry_m_codigo.insert(0, m.get("codigo", ""))
        self.entry_m_nombre.delete(0, "end")
        self.entry_m_nombre.insert(0, m.get("nombre", ""))
        self.entry_m_tipo.delete(0, "end")
        self.entry_m_tipo.insert(0, m.get("tipo", ""))
        if m.get("activo", True):
            self.check_m_activo.select()
        else:
            self.check_m_activo.deselect()

    def _limpiar_form_movil(self) -> None:
        self._moviles_seleccion = None
        self.tabla_moviles.selection_remove(self.tabla_moviles.selection())
        self.entry_m_codigo.delete(0, "end")
        self.entry_m_nombre.delete(0, "end")
        self.entry_m_tipo.delete(0, "end")
        self.check_m_activo.select()

    def _guardar_movil(self) -> None:
        codigo = self.entry_m_codigo.get().strip()
        nombre = self.entry_m_nombre.get().strip()
        tipo = self.entry_m_tipo.get().strip()
        if not codigo or not nombre:
            messagebox.showerror("Dato inválido", "Código y nombre son obligatorios.")
            return

        registro = {"codigo": codigo, "nombre": nombre, "tipo": tipo,
                    "activo": bool(self.check_m_activo.get())}

        if self._moviles_seleccion is not None:
            self._moviles[self._moviles_seleccion] = registro
        else:
            self._moviles.append(registro)

        catalogos.guardar_parque_automotor(self._moviles)
        self._moviles = catalogos.cargar_parque_automotor(solo_activos=False)
        self._refrescar_tabla_moviles()
        self._limpiar_form_movil()
        self._avisar_actualizacion()

    def _eliminar_movil(self) -> None:
        if self._moviles_seleccion is None:
            messagebox.showinfo("Eliminar", "Seleccioná primero una fila de la tabla.")
            return
        del self._moviles[self._moviles_seleccion]
        catalogos.guardar_parque_automotor(self._moviles)
        self._refrescar_tabla_moviles()
        self._limpiar_form_movil()
        self._avisar_actualizacion()

    # -- Credenciales RUBA ------------------------------------------------------

    def _build_ruba(self, master) -> None:
        config = catalogos.cargar_config()
        ruba = config.get("ruba", {})

        frame = ctk.CTkFrame(master, fg_color=theme.GRIS_TARJETA_HEADER, corner_radius=8)
        frame.pack(fill="x", padx=8, pady=16)
        frame.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(frame, text="Credenciales del portal RUBA", font=theme.FUENTE_TITULO_SECCION).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 4))
        ctk.CTkLabel(frame, text="Se guardan en texto plano en data/config.json (uso local del cuartel).",
                     font=theme.FUENTE_LABEL, text_color=theme.TEXTO_MUTED).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12))

        ctk.CTkLabel(frame, text="Usuario", font=theme.FUENTE_LABEL).grid(row=2, column=0, sticky="w", padx=12)
        self.entry_ruba_usuario = ctk.CTkEntry(frame, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_ruba_usuario.insert(0, ruba.get("usuario", ""))
        self.entry_ruba_usuario.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))

        ctk.CTkLabel(frame, text="Contraseña", font=theme.FUENTE_LABEL).grid(row=2, column=1, sticky="w", padx=12)
        self.entry_ruba_clave = ctk.CTkEntry(frame, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE, show="•")
        self.entry_ruba_clave.insert(0, ruba.get("clave", ""))
        self.entry_ruba_clave.grid(row=3, column=1, sticky="ew", padx=12, pady=(0, 10))

        ctk.CTkLabel(frame, text="URL de login", font=theme.FUENTE_LABEL).grid(
            row=4, column=0, sticky="w", padx=12)
        self.entry_ruba_url_login = ctk.CTkEntry(frame, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_ruba_url_login.insert(0, ruba.get("url_login", ""))
        self.entry_ruba_url_login.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 14))

        ctk.CTkLabel(frame, text="URL de incidentes", font=theme.FUENTE_LABEL).grid(
            row=4, column=1, sticky="w", padx=12)
        self.entry_ruba_url_incidentes = ctk.CTkEntry(frame, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_ruba_url_incidentes.insert(0, ruba.get("url_incidentes", ""))
        self.entry_ruba_url_incidentes.grid(row=5, column=1, sticky="ew", padx=12, pady=(0, 14))

        ctk.CTkButton(frame, text="Guardar credenciales", command=self._guardar_ruba,
                      fg_color=theme.VERDE, hover_color=theme.VERDE_HOVER,
                      height=theme.ALTO_BOTON_ACCION).grid(row=6, column=0, columnspan=2, sticky="w",
                                                            padx=12, pady=(0, 14))

    def _guardar_ruba(self) -> None:
        config = catalogos.cargar_config()
        config["ruba"] = {
            "usuario": self.entry_ruba_usuario.get().strip(),
            "clave": self.entry_ruba_clave.get(),
            "url_login": self.entry_ruba_url_login.get().strip(),
            "url_incidentes": self.entry_ruba_url_incidentes.get().strip(),
        }
        catalogos.guardar_config(config)
        messagebox.showinfo("Configuración", "Credenciales de RUBA guardadas.")

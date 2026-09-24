"""
Formulario de una Dotación (móvil) despachada: cronología, tripulación,
informe táctico y datos de cierre. Se instancia dos veces (Dotación 1 y 2)
dentro de la pestaña de Despacho.
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from core.catalogos import cargar_parque_automotor, etiqueta_movil
from core.models import CausaSiniestro, Dotacion, PersonalDotacion
from ui import theme
from ui.widgets import DateTimeField, PersonaGradoSelector, SegmentedBool, TimeField

SIN_CAUSA = "Sin determinar"


class DotacionForm(ctk.CTkFrame):
    def __init__(self, master, numero: int, on_change: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.numero = numero
        self._on_change = on_change
        self.grid_columnconfigure((0, 1, 2), weight=1)

        fila = 0

        # -- Móvil y cronología -------------------------------------------
        ctk.CTkLabel(self, text="Unidad / Móvil", font=theme.FUENTE_LABEL).grid(
            row=fila, column=0, sticky="w", pady=(0, 2))
        fila += 1
        self._moviles = {etiqueta_movil(m): m for m in cargar_parque_automotor()}
        self.combo_movil = ctk.CTkComboBox(self, values=sorted(self._moviles.keys()),
                                            height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.combo_movil.set("")
        self.combo_movil.grid(row=fila, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        fila += 1

        ctk.CTkLabel(self, text="Fecha/Hora Salida", font=theme.FUENTE_LABEL).grid(
            row=fila, column=0, sticky="w")
        ctk.CTkLabel(self, text="Hora Arribo a QTH", font=theme.FUENTE_LABEL).grid(
            row=fila, column=1, sticky="w")
        ctk.CTkLabel(self, text="Fecha/Hora Regreso", font=theme.FUENTE_LABEL).grid(
            row=fila, column=2, sticky="w")
        fila += 1

        self.campo_salida = DateTimeField(self, label="Salida")
        self.campo_salida.grid(row=fila, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.campo_arribo = TimeField(self, label="Arribo a QTH")
        self.campo_arribo.grid(row=fila, column=1, sticky="ew", padx=6, pady=(0, 10))
        self.campo_regreso = DateTimeField(self, label="Regreso")
        self.campo_regreso.grid(row=fila, column=2, sticky="ew", padx=(6, 0), pady=(0, 10))
        fila += 1

        boton_ahora_todo = ctk.CTkButton(self, text="Marcar salida AHORA",
                                          command=self.campo_salida.set_ahora,
                                          **theme.boton_ahora_kwargs())
        boton_ahora_todo.configure(width=170)
        boton_ahora_todo.grid(row=fila, column=0, sticky="w", pady=(0, 10))

        boton_ahora_regreso = ctk.CTkButton(self, text="Marcar regreso AHORA",
                                             command=self.campo_regreso.set_ahora,
                                             **theme.boton_ahora_kwargs())
        boton_ahora_regreso.configure(width=170)
        boton_ahora_regreso.grid(row=fila, column=2, sticky="w", pady=(0, 10))
        fila += 1

        # -- Tripulación -----------------------------------------------------
        ctk.CTkLabel(self, text="Tripulación", font=theme.FUENTE_TITULO_SECCION).grid(
            row=fila, column=0, columnspan=3, sticky="w", pady=(6, 4))
        fila += 1

        self.sel_jefe_dotacion = self._fila_persona(fila, "Jefe de Dotación")
        fila += 2
        self.sel_jefe_seguridad = self._fila_persona(fila, "Jefe de Seguridad")
        fila += 2
        self.sel_chofer = self._fila_persona(fila, "Chofer")
        fila += 2

        ctk.CTkLabel(self, text="Bomberos (roles 4 a 12)", font=theme.FUENTE_LABEL,
                     text_color=theme.TEXTO_MUTED).grid(row=fila, column=0, columnspan=3, sticky="w", pady=(4, 2))
        fila += 1

        self.sel_bomberos: list[PersonaGradoSelector] = []
        for i in range(9):  # roles 4 al 12
            rol = i + 4
            fila_frame = ctk.CTkFrame(self, fg_color="transparent")
            fila_frame.grid(row=fila, column=0, columnspan=3, sticky="ew", pady=1)
            fila_frame.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(fila_frame, text=f"Bombero {rol}", width=80, font=theme.FUENTE_LABEL).grid(
                row=0, column=0, sticky="w")
            selector = PersonaGradoSelector(fila_frame, label=f"Bombero {rol}", on_change=self._avisar_cambio)
            selector.grid(row=0, column=1, sticky="ew")
            self.sel_bomberos.append(selector)
            fila += 1

        # -- Informe táctico y cierre -----------------------------------------
        ctk.CTkLabel(self, text="Informe táctico de la dotación", font=theme.FUENTE_TITULO_SECCION).grid(
            row=fila, column=0, columnspan=3, sticky="w", pady=(10, 4))
        fila += 1
        self.texto_informe = ctk.CTkTextbox(self, height=80, font=theme.FUENTE_BASE)
        self.texto_informe.grid(row=fila, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        fila += 1

        ctk.CTkLabel(self, text="Damnificados (personas)", font=theme.FUENTE_LABEL).grid(
            row=fila, column=0, sticky="w")
        ctk.CTkLabel(self, text="Cant.", font=theme.FUENTE_LABEL).grid(row=fila, column=1, sticky="w")
        ctk.CTkLabel(self, text="Damnificados (bomberos)", font=theme.FUENTE_LABEL).grid(
            row=fila, column=2, sticky="w")
        fila += 1

        self.bool_damnificados_personas = SegmentedBool(self)
        self.bool_damnificados_personas.grid(row=fila, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.entry_cant_damnificados = ctk.CTkEntry(self, placeholder_text="0", height=theme.ALTO_CONTROL,
                                                      font=theme.FUENTE_BASE)
        self.entry_cant_damnificados.grid(row=fila, column=1, sticky="ew", padx=6, pady=(0, 10))
        self.bool_damnificados_bomberos = SegmentedBool(self)
        self.bool_damnificados_bomberos.grid(row=fila, column=2, sticky="ew", padx=(6, 0), pady=(0, 10))
        fila += 1

        ctk.CTkLabel(self, text="Superficie afectada", font=theme.FUENTE_LABEL).grid(
            row=fila, column=0, sticky="w")
        ctk.CTkLabel(self, text="Causa", font=theme.FUENTE_LABEL).grid(row=fila, column=1, sticky="w")
        ctk.CTkLabel(self, text="Coordenadas GPS", font=theme.FUENTE_LABEL).grid(row=fila, column=2, sticky="w")
        fila += 1

        self.entry_superficie = ctk.CTkEntry(self, placeholder_text="ej. 150 m2 / 2 ha",
                                              height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_superficie.grid(row=fila, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))

        self.combo_causa = ctk.CTkOptionMenu(self, values=[SIN_CAUSA, *[c.value for c in CausaSiniestro]],
                                              height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.combo_causa.set(SIN_CAUSA)
        self.combo_causa.grid(row=fila, column=1, sticky="ew", padx=6, pady=(0, 10))

        self.entry_gps = ctk.CTkEntry(self, placeholder_text="lat, lon (o vacío = usar el de la escena)",
                                       height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_gps.grid(row=fila, column=2, sticky="ew", padx=(6, 0), pady=(0, 10))
        fila += 1

        self.check_firma = ctk.CTkCheckBox(self, text="Firmado por el Jefe de Dotación",
                                            font=theme.FUENTE_LABEL)
        self.check_firma.grid(row=fila, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _fila_persona(self, fila: int, etiqueta: str) -> PersonaGradoSelector:
        ctk.CTkLabel(self, text=etiqueta, font=theme.FUENTE_LABEL).grid(row=fila, column=0, sticky="w")
        selector = PersonaGradoSelector(self, label=etiqueta, on_change=self._avisar_cambio)
        selector.grid(row=fila + 1, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        return selector

    def _avisar_cambio(self) -> None:
        if self._on_change:
            self._on_change()

    def refrescar_catalogos(self) -> None:
        self._moviles = {etiqueta_movil(m): m for m in cargar_parque_automotor()}
        self.combo_movil.configure(values=sorted(self._moviles.keys()))
        for selector in [self.sel_jefe_dotacion, self.sel_jefe_seguridad, self.sel_chofer, *self.sel_bomberos]:
            selector.refrescar_catalogo()

    # -- Lectura / escritura del modelo --------------------------------------

    def cantidad_personal(self) -> int:
        personal = PersonalDotacion(
            jefe_dotacion=self.sel_jefe_dotacion.get_persona(),
            jefe_seguridad=self.sel_jefe_seguridad.get_persona(),
            chofer=self.sel_chofer.get_persona(),
            bomberos=[s.get_persona() for s in self.sel_bomberos],
        )
        return personal.cantidad()

    def leer(self, gps_escena_fallback: str = "") -> Dotacion:
        etiqueta_movil_sel = self.combo_movil.get().strip()
        movil = self._moviles.get(etiqueta_movil_sel)
        nombre_movil = movil["nombre"] if movil else etiqueta_movil_sel

        try:
            cantidad_damnificados = int(self.entry_cant_damnificados.get().strip() or "0")
        except ValueError:
            raise ValueError(f"Dotación {self.numero}: la cantidad de damnificados debe ser un número.")

        causa_texto = self.combo_causa.get()
        causa = None if causa_texto == SIN_CAUSA else CausaSiniestro(causa_texto)

        gps = self.entry_gps.get().strip() or gps_escena_fallback

        personal = PersonalDotacion(
            jefe_dotacion=self.sel_jefe_dotacion.get_persona(),
            jefe_seguridad=self.sel_jefe_seguridad.get_persona(),
            chofer=self.sel_chofer.get_persona(),
            bomberos=[s.get_persona() for s in self.sel_bomberos],
        )

        return Dotacion(
            numero=self.numero,
            movil=nombre_movil,
            fecha_hora_salida=self.campo_salida.leer(),
            hora_arribo_qth=self.campo_arribo.leer(),
            fecha_hora_regreso=self.campo_regreso.leer(),
            personal=personal,
            informe_tactico=self.texto_informe.get("1.0", "end").strip(),
            damnificados_personas=self.bool_damnificados_personas.get(),
            cantidad_damnificados_personas=cantidad_damnificados,
            damnificados_bomberos=self.bool_damnificados_bomberos.get(),
            superficie_afectada=self.entry_superficie.get().strip(),
            causa=causa,
            coordenadas_gps=gps,
            firma_jefe_dotacion=bool(self.check_firma.get()),
        )

    def limpiar(self) -> None:
        self.combo_movil.set("")
        self.campo_salida.set_datetime(None)
        self.campo_arribo.set_time(None)
        self.campo_regreso.set_datetime(None)
        for selector in [self.sel_jefe_dotacion, self.sel_jefe_seguridad, self.sel_chofer, *self.sel_bomberos]:
            selector.limpiar()
        self.texto_informe.delete("1.0", "end")
        self.bool_damnificados_personas.set(False)
        self.entry_cant_damnificados.delete(0, "end")
        self.bool_damnificados_bomberos.set(False)
        self.entry_superficie.delete(0, "end")
        self.combo_causa.set(SIN_CAUSA)
        self.entry_gps.delete(0, "end")
        self.check_firma.deselect()

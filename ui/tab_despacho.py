"""
Pestaña "Despacho y Carga de Siniestro": formulario principal que arma un
`RegistroServicio` completo (PCS + PCD) y lo persiste, dejándolo listo para
el próximo paso (inyección en PCS.xlsx / PCD2.xlsx vía OpenPyXL).
"""

from __future__ import annotations

from datetime import date, time
from tkinter import messagebox
from typing import Callable, Optional

import customtkinter as ctk

from core import historial_store
from core.models import (
    DatosSiniestro,
    Dotacion,
    Magnitud,
    MedioContacto,
    PedidoSocorro,
    PersonalCuartel,
    RegistroServicio,
    SUBTIPOS_POR_TIPO,
    TipoSiniestro,
)
from ui import theme
from ui.dotacion_form import DotacionForm
from ui.widgets import CollapsibleCard, DateField, PersonaGradoSelector, SegmentedBool, \
    SeleccionMultiplePersonal, TimeField

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


class TabDespacho:
    def __init__(self, master, on_guardado: Optional[Callable[[RegistroServicio], None]] = None):
        self.master = master
        self.on_guardado = on_guardado

        self.scroll = ctk.CTkScrollableFrame(master, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=12, pady=12)
        self.scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.scroll, text="Despacho y Carga de Siniestro",
                     font=theme.FUENTE_TITULO_APP).grid(row=0, column=0, sticky="w", pady=(0, 12))

        self._build_seccion_a(1)
        self._build_seccion_b(2)
        self._build_seccion_c(3)
        self._build_seccion_d(4)
        self._build_acciones(5)

        self._on_tipo_change(TipoSiniestro.OTRO.value)
        self._recalcular_totales()

    # -- Helper de layout -----------------------------------------------------

    def _campo(self, parent, row: int, col: int, colspan: int, texto_label: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=col, columnspan=colspan, sticky="ew", padx=4, pady=(0, 10))
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text=texto_label, font=theme.FUENTE_LABEL).grid(row=0, column=0, sticky="w")
        return frame

    # -- Sección A: Carátula y Alarma ------------------------------------------

    def _build_seccion_a(self, row: int) -> None:
        card = CollapsibleCard(self.scroll, "A. Carátula y Alarma (PCS)")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14))
        body = card.body

        f = self._campo(body, 0, 0, 1, "N° Siniestro")
        self.entry_n_siniestro = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_n_siniestro.grid(row=1, column=0, sticky="ew")
        self.entry_n_siniestro.insert(0, historial_store.siguiente_n_siniestro())

        f = self._campo(body, 0, 1, 1, "Fecha")
        self.campo_fecha = DateField(f, label="Fecha", on_change=self._actualizar_dia)
        self.campo_fecha.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 0, 2, 1, "Día")
        self.entry_dia = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE, state="disabled")
        self.entry_dia.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 0, 3, 1, "Hora de Llamado")
        self.campo_hora_llamado = TimeField(f, label="Hora de Llamado")
        self.campo_hora_llamado.grid(row=1, column=0, sticky="ew")

        self.campo_fecha.set_hoy()
        self.campo_hora_llamado.set_ahora()

        f = self._campo(body, 2, 0, 2, "Comunicó (denunciante)")
        self.entry_comunico = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_comunico.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 2, 2, "Domicilio del denunciante")
        self.entry_domicilio = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_domicilio.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 4, 0, 2, "Medio de contacto")
        self.seg_medio_contacto = ctk.CTkSegmentedButton(f, values=[m.value for m in MedioContacto],
                                                           font=theme.FUENTE_BASE)
        self.seg_medio_contacto.set(MedioContacto.TELEFONO.value)
        self.seg_medio_contacto.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 4, 2, 2, "N° de teléfono / canal de radio")
        self.entry_medio_detalle = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_medio_detalle.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 6, 0, 4, "Recibió el aviso (bombero + grado)")
        self.sel_recibio_aviso = PersonaGradoSelector(f, label="Recibió aviso")
        self.sel_recibio_aviso.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 8, 0, 1, "¿Se activó la alarma?")
        self.bool_alarma = SegmentedBool(f)
        self.bool_alarma.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 8, 1, 1, "SAB")
        self.entry_sab = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_sab.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 8, 2, 2, "Hora de Alarma")
        self.campo_hora_alarma = TimeField(f, label="Hora de Alarma")
        self.campo_hora_alarma.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 10, 0, 4, "Autorizó la salida (bombero + grado)")
        self.sel_autorizo_salida = PersonaGradoSelector(f, label="Autorizó salida")
        self.sel_autorizo_salida.grid(row=1, column=0, sticky="ew")

    def _actualizar_dia(self) -> None:
        try:
            fecha = self.campo_fecha.leer()
        except ValueError:
            fecha = None
        texto = DIAS_SEMANA[fecha.weekday()] if fecha else ""
        self.entry_dia.configure(state="normal")
        self.entry_dia.delete(0, "end")
        self.entry_dia.insert(0, texto)
        self.entry_dia.configure(state="disabled")

    # -- Sección B: Tipología, Ubicación y Escena ------------------------------

    def _build_seccion_b(self, row: int) -> None:
        card = CollapsibleCard(self.scroll, "B. Tipología, Ubicación y Escena (PCS)")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14))
        body = card.body

        f = self._campo(body, 0, 0, 1, "Tipo de siniestro")
        self.combo_tipo = ctk.CTkOptionMenu(f, values=[t.value for t in TipoSiniestro],
                                             height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE,
                                             command=self._on_tipo_change)
        self.combo_tipo.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 0, 1, 1, "Subtipo")
        self.combo_subtipo = ctk.CTkComboBox(f, values=[], height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.combo_subtipo.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 0, 2, 2, "Magnitud")
        self.seg_magnitud = ctk.CTkSegmentedButton(f, values=[str(m.value) for m in Magnitud],
                                                     font=theme.FUENTE_BASE)
        self.seg_magnitud.set(str(Magnitud.BAJA.value))
        self.seg_magnitud.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 0, 1, "Localidad")
        self.entry_localidad = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_localidad.insert(0, "Adelia María")
        self.entry_localidad.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 1, 1, "Calle / Lugar")
        self.entry_calle = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_calle.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 2, 1, "Zona / Barrio")
        self.entry_zona = ctk.CTkEntry(f, height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_zona.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 3, 1, "Coordenadas GPS de la escena")
        self.entry_gps_escena = ctk.CTkEntry(f, placeholder_text="lat, lon (opcional)",
                                              height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE)
        self.entry_gps_escena.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 4, 0, 4, "Descripción / Relato del siniestro")
        self.texto_descripcion = ctk.CTkTextbox(f, height=90, font=theme.FUENTE_BASE)
        self.texto_descripcion.grid(row=1, column=0, sticky="ew")

    def _on_tipo_change(self, tipo_texto: str) -> None:
        tipo = TipoSiniestro(tipo_texto)
        subtipos = SUBTIPOS_POR_TIPO.get(tipo, ["Otro"])
        self.combo_subtipo.configure(values=subtipos)
        if self.combo_subtipo.get() not in subtipos:
            self.combo_subtipo.set(subtipos[0])

    # -- Sección C: Dotaciones Despachadas --------------------------------------

    def _build_seccion_c(self, row: int) -> None:
        card = CollapsibleCard(self.scroll, "C. Dotaciones Despachadas (PCD)")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14))
        body = card.body

        tabview = ctk.CTkTabview(body, height=680, fg_color=theme.GRIS_TARJETA_HEADER,
                                  segmented_button_fg_color=theme.GRIS_TARJETA)
        tabview.grid(row=0, column=0, columnspan=4, sticky="nsew")

        tab1 = tabview.add("Dotación 1")
        tab2 = tabview.add("Dotación 2 (opcional)")

        scroll1 = ctk.CTkScrollableFrame(tab1, fg_color="transparent")
        scroll1.pack(fill="both", expand=True)
        self.form_dotacion_1 = DotacionForm(scroll1, numero=1, on_change=self._recalcular_totales)
        self.form_dotacion_1.pack(fill="both", expand=True, padx=6, pady=6)

        scroll2 = ctk.CTkScrollableFrame(tab2, fg_color="transparent")
        scroll2.pack(fill="both", expand=True)
        self.form_dotacion_2 = DotacionForm(scroll2, numero=2, on_change=self._recalcular_totales)
        self.form_dotacion_2.pack(fill="both", expand=True, padx=6, pady=6)

    # -- Sección D: Base de Comunicaciones, Reserva y Totales -------------------

    def _build_seccion_d(self, row: int) -> None:
        card = CollapsibleCard(self.scroll, "D. Base de Comunicaciones, Reserva y Totales (PCS)")
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14))
        body = card.body

        f = self._campo(body, 0, 0, 2, "Operador de guardia 1")
        self.sel_operador_1 = PersonaGradoSelector(f, label="Operador 1", on_change=self._recalcular_totales)
        self.sel_operador_1.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 0, 2, 2, "Operador de guardia 2")
        self.sel_operador_2 = PersonaGradoSelector(f, label="Operador 2", on_change=self._recalcular_totales)
        self.sel_operador_2.grid(row=1, column=0, sticky="ew")

        f = self._campo(body, 2, 0, 4, "Personal de Reserva presente en cuartel")
        self.sel_reserva = SeleccionMultiplePersonal(f, on_change=self._recalcular_totales)
        self.sel_reserva.grid(row=1, column=0, sticky="ew")

        totales_frame = ctk.CTkFrame(body, fg_color=theme.GRIS_TARJETA_HEADER, corner_radius=8)
        totales_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(4, 10))
        totales_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.entry_total_cuartel = self._caja_total(totales_frame, 0, "Total en Cuartel")
        self.entry_total_servicio = self._caja_total(totales_frame, 1, "Total en Servicio")
        self.entry_total_general = self._caja_total(totales_frame, 2, "Total General")

        f = self._campo(body, 5, 0, 4, "Confeccionó la planilla (nombre + grado)")
        self.sel_confeccion = PersonaGradoSelector(f, label="Confeccionó planilla")
        self.sel_confeccion.grid(row=1, column=0, sticky="ew")

    def _caja_total(self, parent, col: int, texto: str) -> ctk.CTkEntry:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=0, column=col, sticky="ew", padx=10, pady=10)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text=texto, font=theme.FUENTE_LABEL, text_color=theme.TEXTO_MUTED).grid(
            row=0, column=0, sticky="w")
        entry = ctk.CTkEntry(frame, height=44, font=theme.FUENTE_TOTALES, justify="center", state="disabled")
        entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        return entry

    def _recalcular_totales(self) -> None:
        cuartel = PersonalCuartel(
            operador_guardia_1=self.sel_operador_1.get_persona(),
            operador_guardia_2=self.sel_operador_2.get_persona(),
            personal_reserva=self.sel_reserva.get_seleccionados(),
        )
        total_cuartel = cuartel.total_cuartel()
        total_servicio = self.form_dotacion_1.cantidad_personal() + self.form_dotacion_2.cantidad_personal()
        total_general = total_cuartel + total_servicio

        for entry, valor in [
            (self.entry_total_cuartel, total_cuartel),
            (self.entry_total_servicio, total_servicio),
            (self.entry_total_general, total_general),
        ]:
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, str(valor))
            entry.configure(state="disabled")

    # -- Acciones ---------------------------------------------------------------

    def _build_acciones(self, row: int) -> None:
        frame = ctk.CTkFrame(self.scroll, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=(4, 20))
        frame.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(frame, text="Guardar e Inyectar en Planillas", command=self._guardar,
                      **theme.boton_primario_kwargs()).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(frame, text="Limpiar Formulario", command=self._limpiar,
                      **theme.boton_secundario_kwargs()).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    @staticmethod
    def _dotacion_vacia(d: Dotacion) -> bool:
        return (not d.movil.strip() and d.personal.cantidad() == 0
                and not d.informe_tactico.strip() and not d.coordenadas_gps.strip())

    def _guardar(self) -> None:
        try:
            pedido = PedidoSocorro(
                n_siniestro=self.entry_n_siniestro.get().strip(),
                dia=self.entry_dia.get().strip(),
                fecha=self.campo_fecha.leer() or date.today(),
                hora_llamado=self.campo_hora_llamado.leer() or time(0, 0),
                comunico=self.entry_comunico.get().strip(),
                domicilio_denunciante=self.entry_domicilio.get().strip(),
                medio_contacto=MedioContacto(self.seg_medio_contacto.get()),
                medio_contacto_detalle=self.entry_medio_detalle.get().strip(),
                recibio_aviso=self.sel_recibio_aviso.get_persona(),
                alarma_activada=self.bool_alarma.get(),
                sab=self.entry_sab.get().strip(),
                hora_alarma=self.campo_hora_alarma.leer(),
                autorizo_salida=self.sel_autorizo_salida.get_persona(),
            )

            datos_siniestro = DatosSiniestro(
                tipo=TipoSiniestro(self.combo_tipo.get()),
                subtipo=self.combo_subtipo.get().strip(),
                magnitud=Magnitud(int(self.seg_magnitud.get())),
                localidad=self.entry_localidad.get().strip(),
                calle_lugar=self.entry_calle.get().strip(),
                zona_barrio=self.entry_zona.get().strip(),
                descripcion=self.texto_descripcion.get("1.0", "end").strip(),
            )

            gps_escena = self.entry_gps_escena.get().strip()
            dotacion_1 = self.form_dotacion_1.leer(gps_escena_fallback=gps_escena)
            dotacion_2 = self.form_dotacion_2.leer(gps_escena_fallback=gps_escena)

            dotaciones = [dotacion_1]
            if not self._dotacion_vacia(dotacion_2):
                dotaciones.append(dotacion_2)

            personal_cuartel = PersonalCuartel(
                operador_guardia_1=self.sel_operador_1.get_persona(),
                operador_guardia_2=self.sel_operador_2.get_persona(),
                personal_reserva=self.sel_reserva.get_seleccionados(),
                confecciono_planilla=self.sel_confeccion.get_persona(),
            )

            registro = RegistroServicio(
                pedido_socorro=pedido,
                datos_siniestro=datos_siniestro,
                personal_cuartel=personal_cuartel,
                dotaciones=dotaciones,
            )

        except ValueError as e:
            messagebox.showerror("Dato inválido", str(e))
            return

        errores = registro.validar()
        if errores:
            messagebox.showerror("Revisá el formulario", "\n".join(f"• {e}" for e in errores))
            return

        historial_store.guardar_registro(registro)

        if self.on_guardado:
            self.on_guardado(registro)

        messagebox.showinfo(
            "Servicio guardado",
            f"Siniestro N° {registro.pedido_socorro.n_siniestro} guardado en el histórico.\n"
            "Queda listo para inyectarse en PCS.xlsx / PCD2.xlsx.",
        )
        self._limpiar()

    def _limpiar(self) -> None:
        self.entry_n_siniestro.delete(0, "end")
        self.entry_n_siniestro.insert(0, historial_store.siguiente_n_siniestro())
        self.campo_fecha.set_hoy()
        self.campo_hora_llamado.set_ahora()
        self.entry_comunico.delete(0, "end")
        self.entry_domicilio.delete(0, "end")
        self.seg_medio_contacto.set(MedioContacto.TELEFONO.value)
        self.entry_medio_detalle.delete(0, "end")
        self.sel_recibio_aviso.limpiar()
        self.bool_alarma.set(False)
        self.entry_sab.delete(0, "end")
        self.campo_hora_alarma.set_time(None)
        self.sel_autorizo_salida.limpiar()

        self.combo_tipo.set(TipoSiniestro.OTRO.value)
        self._on_tipo_change(TipoSiniestro.OTRO.value)
        self.seg_magnitud.set(str(Magnitud.BAJA.value))
        self.entry_localidad.delete(0, "end")
        self.entry_localidad.insert(0, "Adelia María")
        self.entry_calle.delete(0, "end")
        self.entry_zona.delete(0, "end")
        self.entry_gps_escena.delete(0, "end")
        self.texto_descripcion.delete("1.0", "end")

        self.form_dotacion_1.limpiar()
        self.form_dotacion_2.limpiar()

        self.sel_operador_1.limpiar()
        self.sel_operador_2.limpiar()
        self.sel_reserva.limpiar()
        self.sel_confeccion.limpiar()

        self._recalcular_totales()

    def refrescar_catalogos(self) -> None:
        """Se llama desde la pestaña de Configuración cuando cambian los catálogos."""
        for selector in [self.sel_recibio_aviso, self.sel_autorizo_salida, self.sel_operador_1,
                          self.sel_operador_2, self.sel_confeccion]:
            selector.refrescar_catalogo()
        self.sel_reserva.cargar_personal()
        self.form_dotacion_1.refrescar_catalogos()
        self.form_dotacion_2.refrescar_catalogos()

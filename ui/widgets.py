"""
Controles reutilizables de la GUI: tarjetas colapsables, campos de
fecha/hora con botón "Ahora", selector combinado nombre+grado alimentado
por `data/cuerpo_activo.json`, y selección múltiple de personal de reserva.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from core.catalogos import GRADOS, cargar_cuerpo_activo, nombre_completo
from core.models import PersonaGrado
from ui import theme


# ---------------------------------------------------------------------------
# Tarjeta colapsable (secciones A-D de la pestaña de despacho)
# ---------------------------------------------------------------------------

class CollapsibleCard(ctk.CTkFrame):
    def __init__(self, master, titulo: str, subtitulo: str = "", expandido: bool = True, **kwargs):
        super().__init__(master, fg_color=theme.GRIS_TARJETA, corner_radius=10,
                          border_width=1, border_color=theme.GRIS_BORDE, **kwargs)
        self.grid_columnconfigure(0, weight=1)

        self._expandido = expandido

        header = ctk.CTkFrame(self, fg_color=theme.GRIS_TARJETA_HEADER, corner_radius=10)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        texto_titulo = titulo if not subtitulo else f"{titulo}   ·   {subtitulo}"
        self._boton_toggle = ctk.CTkButton(
            header, text=self._texto_boton(texto_titulo), anchor="w",
            fg_color="transparent", hover_color=theme.GRIS_BORDE,
            font=theme.FUENTE_TITULO_SECCION, height=42,
            command=self.alternar,
        )
        self._boton_toggle.grid(row=0, column=0, sticky="ew", padx=6, pady=4)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid_columnconfigure((0, 1, 2, 3), weight=1)
        if self._expandido:
            self.body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 14))

        self._titulo_base = texto_titulo

    def _texto_boton(self, titulo: str) -> str:
        icono = "▾" if self._expandido else "▸"
        return f"{icono}  {titulo}"

    def alternar(self) -> None:
        self._expandido = not self._expandido
        if self._expandido:
            self.body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 14))
        else:
            self.body.grid_forget()
        self._boton_toggle.configure(text=self._texto_boton(self._titulo_base))


# ---------------------------------------------------------------------------
# Campos de fecha / hora con captura de un toque
# ---------------------------------------------------------------------------

class TimeField(ctk.CTkFrame):
    """Entry HH:MM + botón "Ahora"."""

    def __init__(self, master, label: str, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.label = label
        self.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(self, placeholder_text="HH:MM", height=theme.ALTO_CONTROL,
                                   font=theme.FUENTE_BASE)
        self.entry.grid(row=0, column=0, sticky="ew")

        self.boton_ahora = ctk.CTkButton(self, text="Ahora", command=self.set_ahora,
                                          **theme.boton_ahora_kwargs())
        self.boton_ahora.grid(row=0, column=1, padx=(6, 0))

    def set_ahora(self) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, datetime.now().strftime("%H:%M"))

    def set_time(self, valor: Optional[time]) -> None:
        self.entry.delete(0, "end")
        if valor is not None:
            self.entry.insert(0, valor.strftime("%H:%M"))

    def leer(self) -> Optional[time]:
        texto = self.entry.get().strip()
        if not texto:
            return None
        try:
            return datetime.strptime(texto, "%H:%M").time()
        except ValueError:
            raise ValueError(f"{self.label}: formato de hora inválido, usá HH:MM.")


class DateField(ctk.CTkFrame):
    """Entry DD/MM/AAAA + botón "Hoy"."""

    def __init__(self, master, label: str, on_change: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.label = label
        self._on_change = on_change
        self.grid_columnconfigure(0, weight=1)

        self.entry = ctk.CTkEntry(self, placeholder_text="DD/MM/AAAA", height=theme.ALTO_CONTROL,
                                   font=theme.FUENTE_BASE)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<FocusOut>", lambda e: self._avisar_cambio())

        self.boton_hoy = ctk.CTkButton(self, text="Hoy", command=self.set_hoy,
                                        **theme.boton_ahora_kwargs())
        self.boton_hoy.grid(row=0, column=1, padx=(6, 0))

    def _avisar_cambio(self) -> None:
        if self._on_change:
            self._on_change()

    def set_hoy(self) -> None:
        self.set_date(date.today())
        self._avisar_cambio()

    def set_date(self, valor: Optional[date]) -> None:
        self.entry.delete(0, "end")
        if valor is not None:
            self.entry.insert(0, valor.strftime("%d/%m/%Y"))

    def leer(self) -> Optional[date]:
        texto = self.entry.get().strip()
        if not texto:
            return None
        try:
            return datetime.strptime(texto, "%d/%m/%Y").date()
        except ValueError:
            raise ValueError(f"{self.label}: formato de fecha inválido, usá DD/MM/AAAA.")


class DateTimeField(ctk.CTkFrame):
    """Combinación de DateField + TimeField para timestamps de dotación
    (salida / regreso), con un único botón "Ahora" que completa ambos."""

    def __init__(self, master, label: str, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.label = label
        self.grid_columnconfigure((0, 1), weight=1)

        self.campo_fecha = DateField(self, label=f"{label} (fecha)")
        self.campo_fecha.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.campo_hora = TimeField(self, label=f"{label} (hora)")
        self.campo_hora.grid(row=0, column=1, sticky="ew")

    def set_ahora(self) -> None:
        self.campo_fecha.set_hoy()
        self.campo_hora.set_ahora()

    def set_datetime(self, valor: Optional[datetime]) -> None:
        if valor is None:
            self.campo_fecha.set_date(None)
            self.campo_hora.set_time(None)
        else:
            self.campo_fecha.set_date(valor.date())
            self.campo_hora.set_time(valor.time())

    def leer(self) -> Optional[datetime]:
        fecha = self.campo_fecha.leer()
        hora = self.campo_hora.leer()
        if fecha is None and hora is None:
            return None
        if fecha is None or hora is None:
            raise ValueError(f"{self.label}: completá fecha y hora juntas, o dejá ambas vacías.")
        return datetime.combine(fecha, hora)


# ---------------------------------------------------------------------------
# Selector combinado Nombre + Grado, alimentado por el cuerpo activo
# ---------------------------------------------------------------------------

class PersonaGradoSelector(ctk.CTkFrame):
    """Dos combobox editables (nombre / grado). Al elegir un nombre del
    catálogo se autocompleta el grado; ambos campos aceptan texto libre para
    cargar personal que todavía no esté en `cuerpo_activo.json`."""

    def __init__(self, master, label: str = "", ancho_nombre: int = 220,
                 on_change: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.label = label
        self._on_change = on_change
        self.grid_columnconfigure(0, weight=2)
        self.grid_columnconfigure(1, weight=1)

        self._catalogo: Dict[str, str] = {
            nombre_completo(p): p.get("grado", "") for p in cargar_cuerpo_activo()
        }
        nombres = sorted(self._catalogo.keys())

        self.combo_nombre = ctk.CTkComboBox(self, values=nombres, width=ancho_nombre,
                                             height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE,
                                             command=self._on_nombre_seleccionado)
        self.combo_nombre.set("")
        self.combo_nombre.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.combo_nombre.bind("<FocusOut>", lambda e: self._avisar_cambio())

        self.combo_grado = ctk.CTkComboBox(self, values=GRADOS, width=150,
                                            height=theme.ALTO_CONTROL, font=theme.FUENTE_BASE,
                                            command=lambda _v: self._avisar_cambio())
        self.combo_grado.set("")
        self.combo_grado.grid(row=0, column=1, sticky="ew")
        self.combo_grado.bind("<FocusOut>", lambda e: self._avisar_cambio())

    def _on_nombre_seleccionado(self, nombre_seleccionado: str) -> None:
        grado = self._catalogo.get(nombre_seleccionado)
        if grado:
            self.combo_grado.set(grado)
        self._avisar_cambio()

    def _avisar_cambio(self) -> None:
        if self._on_change:
            self._on_change()

    def refrescar_catalogo(self) -> None:
        self._catalogo = {nombre_completo(p): p.get("grado", "") for p in cargar_cuerpo_activo()}
        self.combo_nombre.configure(values=sorted(self._catalogo.keys()))

    def get_persona(self) -> PersonaGrado:
        return PersonaGrado(nombre=self.combo_nombre.get().strip(), grado=self.combo_grado.get().strip())

    def set_persona(self, persona: PersonaGrado) -> None:
        self.combo_nombre.set(persona.nombre)
        self.combo_grado.set(persona.grado)

    def limpiar(self) -> None:
        self.combo_nombre.set("")
        self.combo_grado.set("")


# ---------------------------------------------------------------------------
# Booleano grande tipo SI/NO (alarma, damnificados, etc.)
# ---------------------------------------------------------------------------

class SegmentedBool(ctk.CTkFrame):
    def __init__(self, master, on_change: Optional[Callable[[bool], None]] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_change = on_change
        self.segmented = ctk.CTkSegmentedButton(self, values=["SI", "NO"], command=self._on_click,
                                                  font=theme.FUENTE_BASE)
        self.segmented.set("NO")
        self.segmented.pack(fill="x")

    def _on_click(self, valor: str) -> None:
        if self._on_change:
            self._on_change(self.get())

    def get(self) -> bool:
        return self.segmented.get() == "SI"

    def set(self, valor: bool) -> None:
        self.segmented.set("SI" if valor else "NO")


# ---------------------------------------------------------------------------
# Selección múltiple de personal de reserva (checkboxes touch-friendly)
# ---------------------------------------------------------------------------

class SeleccionMultiplePersonal(ctk.CTkScrollableFrame):
    def __init__(self, master, on_change: Optional[Callable[[], None]] = None, height: int = 180, **kwargs):
        super().__init__(master, height=height, fg_color=theme.GRIS_TARJETA_HEADER,
                          corner_radius=8, **kwargs)
        self._on_change = on_change
        self._vars: Dict[str, ctk.BooleanVar] = {}
        self._grados: Dict[str, str] = {}
        self.grid_columnconfigure(0, weight=1)
        self.cargar_personal()

    def cargar_personal(self) -> None:
        for widget in self.winfo_children():
            widget.destroy()
        self._vars.clear()
        self._grados.clear()

        for i, persona in enumerate(cargar_cuerpo_activo()):
            nombre = nombre_completo(persona)
            grado = persona.get("grado", "")
            self._grados[nombre] = grado
            var = ctk.BooleanVar(value=False)
            self._vars[nombre] = var
            chk = ctk.CTkCheckBox(self, text=f"{nombre}  ({grado})", variable=var,
                                   font=theme.FUENTE_LABEL,
                                   command=self._on_change if self._on_change else None)
            chk.grid(row=i, column=0, sticky="w", pady=2, padx=4)

    def get_seleccionados(self) -> List[PersonaGrado]:
        return [
            PersonaGrado(nombre=nombre, grado=self._grados.get(nombre, ""))
            for nombre, var in self._vars.items() if var.get()
        ]

    def limpiar(self) -> None:
        for var in self._vars.values():
            var.set(False)

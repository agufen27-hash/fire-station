"""Helpers compartidos por los tests de UI: cargar un servicio completo en la
ventana principal con la estructura "Dotaciones por Unidad"."""

from datetime import date, time


def elegir_categoria(ventana, mw, tipo_id: int, codigo: str) -> None:
    ventana.combo_tipo.setCurrentIndex(ventana.combo_tipo.findData(tipo_id))
    for i in range(ventana.combo_categoria.count()):
        if ventana.combo_categoria.itemData(i, mw.ROL_CODIGO_RUBA) == codigo:
            ventana.combo_categoria.setCurrentIndex(i)
            return
    raise AssertionError(f"No está la categoría {codigo}")


def despachar(ventana, movil_id_ruba: int, chofer, bomberos=(), encargado_en: int = -1, jefe=None):
    """Agrega una Dotación con su chofer, Jefe y embarcados. `encargado_en`
    = índice dentro de `bomberos` que pasa a ser el Jefe de Dotación (el de
    la Dotación N° 1 es el Encargado del servicio en RUBA)."""
    bomberos = list(bomberos)
    if encargado_en >= 0:
        jefe = bomberos.pop(encargado_en)
    tarjeta = ventana.panel_dotaciones.agregar_unidad()
    tarjeta.combo_movil.setCurrentIndex(tarjeta.combo_movil.findData(movil_id_ruba))
    tarjeta.selector_chofer.set_id_ruba(chofer.id_ruba)
    if jefe is not None:
        tarjeta.selector_jefe.set_id_ruba(jefe.id_ruba)
    for bombero in bomberos:
        tarjeta.agregar_bombero().selector.set_id_ruba(bombero.id_ruba)
    return tarjeta


def cargar_servicio_basico(ventana, mw, padron, calle="Belgrano 58", codigo_categoria="23"):
    """Incendio con una dotación (chofer padron[0], embarcado padron[1],
    Jefe/Encargado padron[2]) y el horario general 08:20 -> 09:45."""
    elegir_categoria(ventana, mw, 3, codigo_categoria)
    ventana.entry_calle.setText(calle)
    ventana.texto_resena.setPlainText("Incendio atendido, sin novedades.")
    ventana.campo_fecha_salida.set_value(date(2026, 9, 23))
    ventana.campo_fecha_llegada.set_value(date(2026, 9, 23))
    ventana.campo_hora_salida.set_value(time(8, 20))
    ventana.campo_hora_llegada.set_value(time(9, 45))
    ventana._on_horario_general_cambiado()
    return despachar(ventana, 4326, padron[0], (padron[1], padron[2]), encargado_en=1)

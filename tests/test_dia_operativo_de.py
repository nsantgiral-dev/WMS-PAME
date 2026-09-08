"""
`dia_operativo_de()` — la operación inversa de `inicio_del_dia_utc()`.

Usada para agrupar "por día" en el tablero BI (métricas 1 y 5) sin caer en
el error de fondo que la Regla 5 del proyecto prohíbe explícitamente: un
evento ocurrido entre las 7 p.m. y la medianoche de Colombia cae en la
fecha UTC del día SIGUIENTE. Agrupar por `func.date()` sobre la columna UTC
cruda movería ese evento al día equivocado en la tendencia del tablero.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.fecha import dia_operativo_de, inicio_del_dia_utc

_TZ = ZoneInfo('America/Bogota')


class TestDiaOperativoDe:

    def test_medianoche_bogota_es_ese_mismo_dia(self):
        # inicio_del_dia_utc(dia) es, por construcción, la medianoche Bogotá
        # de `dia` expresada en UTC — la inversa debe devolver `dia`.
        from datetime import date
        dia = date(2026, 8, 15)
        assert dia_operativo_de(inicio_del_dia_utc(dia)) == dia

    def test_las_23_59_bogota_no_caen_en_el_dia_utc_siguiente(self):
        """El caso que rompe `func.date()` sobre la columna cruda: 23:59
        Bogotá del 15 es 04:59 UTC del 16 — un `func.date()` ingenuo lo
        agruparía en el 16, un día que en Colombia todavía no empezaba."""
        momento_utc = datetime(2026, 8, 16, 4, 59)  # naive UTC, como guarda el WMS
        assert dia_operativo_de(momento_utc).isoformat() == '2026-08-15'

    def test_las_00_01_utc_del_mismo_dia_calendario_utc_ya_es_ayer_en_bogota(self):
        """Espejo del caso anterior: temprano en UTC todavía es la noche
        anterior en Colombia (UTC-5)."""
        momento_utc = datetime(2026, 8, 16, 2, 0)  # 21:00 del 15 en Bogotá
        assert dia_operativo_de(momento_utc).isoformat() == '2026-08-15'

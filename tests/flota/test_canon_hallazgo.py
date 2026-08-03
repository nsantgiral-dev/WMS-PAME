"""
`dias_hallazgo_abierto` — tests DERIVADOS DEL CANON, no del código.

Cada test cita la decisión de `docs/flota/canones/dias_hallazgo_abierto.md` que
lo obliga. Se escribieron antes del cálculo, contra un documento que definió
Santiago: **si el valor y su prueba los produce la misma mano, la prueba es
decoración.**

Esta es la diferencia con los 631 tests que dejaron pasar una fórmula
dimensionalmente imposible: aquellos verificaban que el código hiciera lo que el
código hacía. Estos verifican que el código haga lo que dice un documento
escrito por otro.
"""
from datetime import datetime, timedelta

import pytest

from flota.dominio.hallazgo import (
    EstadoHallazgo,
    Hallazgo,
    dias_hallazgo_abierto,
    dias_transcurridos,
    entra_al_indicador,
    promedio_del_indicador,
    vencido,
)
from flota.dominio.valores import SIN_DATO


def _h(reportado, cerrado=None, estado=None, linea_base=False,
       criticidad='mayor', limite=None, aplazado=0):
    return Hallazgo(
        reportado_ts=reportado,
        cerrado_ts=cerrado,
        estado=estado or (EstadoHallazgo.CERRADO if cerrado else EstadoHallazgo.ABIERTO),
        linea_base=linea_base,
        criticidad=criticidad,
        fecha_limite=limite,
        aplazado_veces=aplazado,
    )


# ══════════════════════════════════════════════════════════════════════════
# §5 — EL CASO CALCULADO A MANO: THP 696
# ══════════════════════════════════════════════════════════════════════════

class TestCasoTHP696:
    """La aritmética del canon, verificada contra el código.

    El canon deja el par exacto de fechas pendiente porque THP 696 es un caso
    de papel y no tiene timestamp. Lo que sí fija es la magnitud —entre 32 y 38
    días— y las cuatro combinaciones posibles. Eso es lo que se prueba.
    """

    @pytest.mark.parametrize('reporte,cierre,esperado', [
        (datetime(2025, 10, 6),  datetime(2025, 11, 12), 37),
        (datetime(2025, 10, 6),  datetime(2025, 11, 13), 38),
        (datetime(2025, 10, 11), datetime(2025, 11, 12), 32),
        (datetime(2025, 10, 11), datetime(2025, 11, 13), 33),
    ])
    def test_la_aritmetica_del_canon(self, reporte, cierre, esperado):
        assert dias_hallazgo_abierto(_h(reporte, cierre)) == esperado

    def test_la_magnitud_es_un_mes_no_una_semana(self):
        """Lo que el canon SÍ fija del caso, aunque falte el par exacto."""
        extremos = [
            dias_hallazgo_abierto(_h(datetime(2025, 10, 11), datetime(2025, 11, 12))),
            dias_hallazgo_abierto(_h(datetime(2025, 10, 6), datetime(2025, 11, 13))),
        ]
        assert min(extremos) == 32 and max(extremos) == 38


# ══════════════════════════════════════════════════════════════════════════
# §4 — LAS DECISIONES QUE FIJAN EL CÁLCULO
# ══════════════════════════════════════════════════════════════════════════

class TestCuandoParaElReloj:
    """"Cuando el vehículo vuelve reparado. NO al aprobar la OT ni al entrar
    al taller." Mide riesgo real, no gestión administrativa."""

    def test_para_en_el_cierre_fisico(self):
        h = _h(datetime(2026, 8, 1, 6, 0), datetime(2026, 8, 11, 18, 0))
        assert dias_hallazgo_abierto(h) == 10

    def test_un_vehiculo_tres_semanas_en_taller_NO_muestra_el_indicador_limpio(self):
        """El escenario exacto del motivo escrito en el canon.

        Si el reloj parara al aprobar la OT, este hallazgo diría 2 días. Como
        para cuando el vehículo vuelve reparado, dice 23.
        """
        reporte = datetime(2026, 8, 1)
        # OT aprobada a los 2 días; el vehículo vuelve reparado a los 23.
        assert dias_hallazgo_abierto(_h(reporte, reporte + timedelta(days=23))) == 23


class TestDiasCalendario:
    """"Un vehículo con una falla el domingo sigue con la falla el domingo.\""""

    def test_un_fin_de_semana_cuenta_completo(self):
        # Viernes 7 de agosto de 2026 → lunes 10. Tres días, no uno.
        viernes, lunes = datetime(2026, 8, 7), datetime(2026, 8, 10)
        assert viernes.weekday() == 4 and lunes.weekday() == 0
        assert dias_hallazgo_abierto(_h(viernes, lunes)) == 3

    def test_una_quincena_con_dos_fines_de_semana_cuenta_los_14_dias(self):
        h = _h(datetime(2026, 8, 3), datetime(2026, 8, 17))
        assert dias_hallazgo_abierto(h) == 14


class TestElAplazamientoNoCongelaElReloj:
    """"Aplazar cambia la fecha límite, no borra el tiempo transcurrido.
    Si congelara, aplazar sería la forma fácil de limpiar el tablero.\""""

    def test_dos_hallazgos_identicos_dan_lo_mismo_aplazado_o_no(self):
        reporte, cierre = datetime(2026, 8, 1), datetime(2026, 8, 21)
        sin_aplazar = _h(reporte, cierre)
        aplazado = _h(reporte, cierre, limite=datetime(2026, 8, 20), aplazado=3)
        assert dias_hallazgo_abierto(aplazado) == dias_hallazgo_abierto(sin_aplazar) == 20

    def test_pero_el_aplazamiento_SI_mueve_cuando_se_considera_vencido(self):
        """Lo que el aplazamiento sí hace, para que la prueba anterior no se
        lea como "aplazar no hace nada"."""
        reporte = datetime(2026, 8, 1)
        ahora = datetime(2026, 8, 10)
        assert vencido(_h(reporte, limite=datetime(2026, 8, 8)), ahora) is True
        assert vencido(_h(reporte, limite=datetime(2026, 8, 20), aplazado=1), ahora) is False


# ══════════════════════════════════════════════════════════════════════════
# §6 — CASOS DEGENERADOS. Ninguno vale cero por omisión.
# ══════════════════════════════════════════════════════════════════════════

class TestAbiertoSinCerrar:
    """"sin_dato con el reloj corriendo. NUNCA cero ni un número grande.\""""

    def test_devuelve_sin_dato(self):
        assert dias_hallazgo_abierto(_h(datetime(2026, 8, 1))) is SIN_DATO

    def test_no_es_cero_ni_falsy(self):
        """Cero diría "se resolvió al instante"."""
        v = dias_hallazgo_abierto(_h(datetime(2026, 8, 1)))
        assert v != 0
        assert bool(v) is True

    def test_pero_el_reloj_corre_y_se_puede_leer(self):
        """El dato que sí existe: cuántos días LLEVA, no cuántos duró."""
        h = _h(datetime(2026, 8, 1))
        assert dias_transcurridos(h, datetime(2026, 8, 12)) == 11

    def test_dias_transcurridos_no_es_dias_hallazgo_abierto(self):
        """Son dos números distintos. Mezclarlos es lo que el canon prohíbe."""
        h = _h(datetime(2026, 8, 1))
        assert dias_transcurridos(h, datetime(2026, 8, 12)) == 11
        assert dias_hallazgo_abierto(h) is SIN_DATO


class TestFueraDelIndicador:

    def test_linea_base_excluida(self):
        """El desorden viejo no entra. Nace preexistente, sin responsable y
        sin reloj — la cuenta empieza al día siguiente, con todo fechado."""
        h = _h(datetime(2026, 8, 1), datetime(2026, 8, 20), linea_base=True)
        assert entra_al_indicador(h) is False

    def test_descartado_excluido(self):
        """No se resolvió: se determinó que no era un hallazgo."""
        h = _h(datetime(2026, 8, 1), estado=EstadoHallazgo.DESCARTADO)
        assert entra_al_indicador(h) is False

    def test_no_aplica_excluido(self):
        """Vehículo dado de baja antes de reparar: no hay resolución posible."""
        h = _h(datetime(2026, 8, 1), estado=EstadoHallazgo.NO_APLICA)
        assert entra_al_indicador(h) is False

    def test_un_hallazgo_normal_si_entra(self):
        assert entra_al_indicador(_h(datetime(2026, 8, 1))) is True

    def test_pedirle_dias_a_uno_excluido_levanta(self):
        """Una pregunta mal hecha no se contesta con un número.

        Responder 19 para un hallazgo de línea base lo convertiría en un dato
        que alguien va a promediar — y el canon dice que ese tiempo no es
        atribuible a nadie.
        """
        h = _h(datetime(2026, 8, 1), datetime(2026, 8, 20), linea_base=True)
        with pytest.raises(ValueError):
            dias_hallazgo_abierto(h)


class TestPromedioDelIndicador:

    def test_promedia_solo_los_cerrados_que_entran(self):
        reporte = datetime(2026, 8, 1)
        hallazgos = [
            _h(reporte, reporte + timedelta(days=10)),                    # 10, entra
            _h(reporte, reporte + timedelta(days=20)),                    # 20, entra
            _h(reporte, reporte + timedelta(days=90), linea_base=True),   # excluido
            _h(reporte, estado=EstadoHallazgo.DESCARTADO),                # excluido
            _h(reporte),                                                  # abierto
        ]
        assert promedio_del_indicador(hallazgos) == 15.0

    def test_sin_nada_que_promediar_es_sin_dato_no_cero(self):
        """Un promedio de cero elementos no es cero: es que no hay nada que
        promediar, y las dos cosas se leen distinto en un tablero."""
        assert promedio_del_indicador([]) is SIN_DATO
        assert promedio_del_indicador([_h(datetime(2026, 8, 1))]) is SIN_DATO

    def test_la_linea_base_no_infla_el_promedio(self):
        """El caso que motiva la exclusión: un hallazgo preexistente de 90 días
        arrastraría el promedio y haría ver mal a quien no tuvo nada que ver."""
        reporte = datetime(2026, 8, 1)
        sin_base = [_h(reporte, reporte + timedelta(days=10))]
        con_base = sin_base + [_h(reporte, reporte + timedelta(days=90), linea_base=True)]
        assert promedio_del_indicador(con_base) == promedio_del_indicador(sin_base) == 10.0

"""`flota/dominio/senales.py`, sin base: cada juicio en sus tres estados.

Cada función tiene tres salidas —señal, normal, no evaluable— y un test que
distingue cada una. El caso que más importa es el tercero: **sin dato no hay
señal, y se dice por qué**. Un detector que contesta «normal» cuando no pudo
mirar se apaga sin que nadie lo note.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from flota.dominio import senales as dom
from flota.dominio.valores import SIN_DATO, Confianza

D = date(2026, 9, 10)


class TestMediana:
    def test_impar_par_y_vacia(self):
        assert dom.mediana([3, 1, 2]) == Decimal(2)
        assert dom.mediana([1, 2, 3, 10]) == Decimal('2.5')
        assert dom.mediana([]) is SIN_DATO


class TestKmSinRuta:
    def test_senal(self):
        v = dom.km_sin_ruta(km=120, dias=[D, D + timedelta(1)], dias_con_ruta=set(),
                            marca=Confianza.DECLARADA)
        assert v.estado == dom.SENAL and v.datos['km'] == 120

    def test_un_dia_con_ruta_en_el_tramo_basta(self):
        v = dom.km_sin_ruta(km=500, dias=[D, D + timedelta(1)],
                            dias_con_ruta={D + timedelta(1)},
                            marca=Confianza.DECLARADA)
        assert v.estado == dom.NORMAL

    def test_dentro_de_la_tolerancia(self):
        v = dom.km_sin_ruta(km=dom.KM_TOLERANCIA_SIN_RUTA, dias=[D],
                            dias_con_ruta=set(), marca=Confianza.VERIFICADA)
        assert v.estado == dom.NORMAL

    @pytest.mark.parametrize('marca', [Confianza.DUDOSA, SIN_DATO])
    def test_tramo_en_duda_no_se_juzga(self, marca):
        v = dom.km_sin_ruta(km=900, dias=[D], dias_con_ruta=set(), marca=marca)
        assert v.estado == dom.NO_EVALUABLE and 'duda' in v.motivo


class TestKmDeRuta:
    def test_senal_sobre_la_mediana(self):
        v = dom.km_de_ruta(km=160, historico=[100, 100, 90, 110, 100])
        assert v.estado == dom.SENAL and v.datos['mediana'] == Decimal(100)

    def test_justo_en_el_factor_no_marca(self):
        v = dom.km_de_ruta(km=150, historico=[100] * 5)
        assert v.estado == dom.NORMAL

    def test_sin_n_suficiente_declara_el_n(self):
        v = dom.km_de_ruta(km=900, historico=[100] * 4)
        assert v.estado == dom.NO_EVALUABLE
        assert v.datos['n'] == 4 and '4 recorrido(s)' in v.motivo


class TestGalones:
    def test_senal(self):
        v = dom.galones_de_ventana(km=300, galones=Decimal(16),
                                   km_galon=Decimal(30), publicable=True,
                                   motivo_rendimiento=None)
        assert v.estado == dom.SENAL
        assert v.datos['esperados'] == Decimal(10)

    def test_dentro_de_la_tolerancia(self):
        v = dom.galones_de_ventana(km=300, galones=Decimal('12.5'),
                                   km_galon=Decimal(30), publicable=True,
                                   motivo_rendimiento=None)
        assert v.estado == dom.NORMAL

    def test_rendimiento_no_publicable_no_juzga(self):
        """Hay número, pero no se sostiene: la misma vara que la pantalla del
        conductor. Con dos ventanas la mitad de los tanqueos sanos dispararían."""
        v = dom.galones_de_ventana(km=300, galones=Decimal(40),
                                   km_galon=Decimal(30), publicable=False,
                                   motivo_rendimiento='2 ventana(s) de 6')
        assert v.estado == dom.NO_EVALUABLE and '2 ventana(s)' in v.motivo

    def test_sin_rendimiento_no_juzga(self):
        v = dom.galones_de_ventana(km=300, galones=Decimal(40), km_galon=SIN_DATO,
                                   publicable=False, motivo_rendimiento='x')
        assert v.estado == dom.NO_EVALUABLE


class TestPrecio:
    def test_senal(self):
        v = dom.precio_de_galon(precio=Decimal(20000),
                                historico=[Decimal(15000)] * 5)
        assert v.estado == dom.SENAL

    def test_normal(self):
        v = dom.precio_de_galon(precio=Decimal(16000),
                                historico=[Decimal(15000)] * 5)
        assert v.estado == dom.NORMAL

    def test_pocos_tanqueos_no_juzga(self):
        v = dom.precio_de_galon(precio=Decimal(90000),
                                historico=[Decimal(15000)] * 4)
        assert v.estado == dom.NO_EVALUABLE

    def test_sin_galones_no_es_caro(self):
        v = dom.precio_de_galon(precio=SIN_DATO, historico=[Decimal(1)] * 9)
        assert v.estado == dom.NO_EVALUABLE


class TestTurnoDeLaRuta:
    def _v(self, **kw):
        base = dict(conductor_ruta_id=1, estado_ruta='EN_TRANSITO',
                    custodia_tipo='conductor', custodio_conductor_id=1,
                    otro_vehiculo_del_conductor=None)
        base.update(kw)
        return dom.turno_de_la_ruta(**base)

    def test_coincide(self):
        assert self._v().estado == dom.NORMAL

    def test_turno_de_otro(self):
        v = self._v(custodio_conductor_id=2)
        assert v.estado == dom.SENAL and v.datos['forma'] == 'turno_de_otro'

    def test_salio_con_la_custodia_en_la_sede(self):
        v = self._v(custodia_tipo='sede', custodio_conductor_id=None)
        assert v.datos['forma'] == 'salio_sin_turno'

    def test_salio_sin_turno_abierto(self):
        v = self._v(custodia_tipo=None, custodio_conductor_id=None)
        assert v.datos['forma'] == 'salio_sin_turno'

    def test_antes_de_salir_la_sede_es_normal(self):
        v = self._v(estado_ruta='EN_CARGUE', custodia_tipo='sede',
                    custodio_conductor_id=None)
        assert v.estado == dom.NORMAL

    def test_el_conductor_tiene_otro_vehiculo(self):
        v = self._v(estado_ruta='EN_CARGUE', custodia_tipo='sede',
                    custodio_conductor_id=None, otro_vehiculo_del_conductor=7)
        assert v.datos['forma'] == 'conductor_con_otro_vehiculo'

    def test_sin_conductor_no_se_juzga(self):
        assert self._v(conductor_ruta_id=None).estado == dom.NO_EVALUABLE


# El semáforo se mudó a `flota/dominio/salida.py` (2026-09-24): sus casos viven
# en `tests/flota/test_una_politica_de_salida.py`.


class TestNingunJuicioRecibeUnaPersona:
    """Regla 2 escrita en la firma: ninguna función del dominio de señales
    recibe un nombre, un usuario o un conductor como tal — reciben ids para
    comparar turnos, nunca para juzgar a quién."""

    def test_ninguna_firma_recibe_nombre_ni_usuario(self):
        import inspect
        for nombre in ('km_sin_ruta', 'km_de_ruta', 'galones_de_ventana',
                       'precio_de_galon', 'turno_de_la_ruta', 'km_sin_explicar'):
            params = inspect.signature(getattr(dom, nombre)).parameters
            for p in params:
                assert 'nombre' not in p and 'usuario' not in p, (nombre, p)

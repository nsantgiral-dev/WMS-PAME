"""
`km_por_dia` — el número que convierte «te faltan 500 km» en «unos 6 días».

Vive en `flota/dominio/odometro.py` y no en el preventivo porque es una
propiedad de la serie de odómetro: el preventivo es su primer consumidor y no va
a ser el último. Estos tests son puros —sin base, sin app— porque la función lo
es, y eso es justamente lo que permite ejercer los casos que en producción no se
pueden fabricar.

## Las dos direcciones, en todos los guards

Cada regla se prueba de los dos lados: que **devuelve `SIN_DATO`** cuando el
tramo no se puede medir, y que **NO lo devuelve** sobre una serie sana. Un
detector probado en un solo sentido está probado a la mitad, y el sentido que
falta es el que rompe producción.

## El caso que le da sentido al archivo entero

El THP696 tiene una lectura de 16.697.948 km del 2026-08-18. Sin la ventana de
`vigentes_tras_la_ultima_correccion`, su km/día sería de seis cifras **para
siempre**, y la corrección que arregló la monotonía no lo arreglaría: el tramo
seguiría arrancando en la lectura envenenada.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from flota.dominio.odometro import RitmoDeUso, km_por_dia
from flota.dominio.valores import SIN_DATO, Confianza, Lectura, OrigenLectura

_T0 = datetime(2026, 3, 1, 6, 0)


def _lec(km, dias=0, confianza=Confianza.DECLARADA,
         origen=OrigenLectura.ENTREGA, motivo=None):
    return Lectura(valor_km=km, ts=_T0 + timedelta(days=dias), origen=origen,
                   autor_usuario_id=1, motivo_correccion=motivo,
                   confianza=confianza)


class TestElRitmoSeMideOSeDiceQueNo:
    """Nunca un número inventado en el medio. **Nunca un promedio que tapa.**"""

    def test_diez_dias_y_mil_kilometros_dan_cien_por_dia(self):
        r = km_por_dia([_lec(50000), _lec(51000, dias=10)])
        assert r.km_dia == Decimal('100')
        assert r.n == 2
        assert r.dias == Decimal('10')
        assert r.motivo is None

    def test_una_sola_lectura_no_es_un_tramo(self):
        """No es cero km/día: es que no hay dos puntos entre los que restar."""
        r = km_por_dia([_lec(50000)])
        assert r.km_dia is SIN_DATO
        assert r.n == 1
        assert 'menos de dos lecturas' in r.motivo

    def test_sin_lecturas_tampoco(self):
        r = km_por_dia([])
        assert r.km_dia is SIN_DATO
        assert r.n == 0

    def test_un_vehiculo_quieto_da_CERO_MEDIDO_y_no_sin_dato(self):
        """**Los dos ceros.** Hubo dos lecturas, pasaron días y el odómetro no
        se movió: eso es un cero medido y se dice como tal.

        Confundirlo con `SIN_DATO` convertiría «no sabemos» en «está parado»,
        y de ahí en «nunca va a llegar al cambio de correa».
        """
        r = km_por_dia([_lec(50000), _lec(50000, dias=7)])
        assert r.km_dia == Decimal('0')
        assert r.km_dia is not SIN_DATO
        assert r.motivo is None

    def test_dos_lecturas_del_mismo_instante_no_dan_velocidad_infinita(self):
        """En producción hay diez lecturas con el mismo segundo. Dividir por
        cero días no da un número grande: no da un número."""
        r = km_por_dia([_lec(50000), _lec(50400, dias=0)])
        assert r.km_dia is SIN_DATO
        assert 'misma marca de tiempo' in r.motivo

    def test_un_tramo_que_decrece_no_produce_un_ritmo_negativo(self):
        """No lo produce la operación —`validar_lectura` no deja decrecer— pero
        sí un `INSERT` a mano.

        **Un km/día negativo es peor que ninguno**: se propaga a «faltan −4
        días» y eso se lee como vencido, sobre un vehículo que no lo está.
        """
        r = km_por_dia([_lec(51000), _lec(50000, dias=5)])
        assert r.km_dia is SIN_DATO
        assert 'decrece' in r.motivo


class TestLaConfianzaViajaConElNumero:
    """`confianza_del_tramo`, **no una copia escrita acá** (regla 0)."""

    def test_dos_declaradas_dan_declarada(self):
        r = km_por_dia([_lec(50000), _lec(51000, dias=10)])
        assert r.marca == Confianza.DECLARADA

    def test_una_dudosa_devuelve_el_numero_PERO_MARCADO(self):
        """El número sí sale: hay un extremo sólido y una resta real. Lo que
        cambia es que quien lo agregue puede excluirlo."""
        r = km_por_dia([_lec(50000),
                        _lec(51000, dias=10, confianza=Confianza.DUDOSA)])
        assert r.km_dia == Decimal('100')
        assert r.marca == Confianza.DUDOSA

    def test_los_dos_extremos_dudosos_no_son_un_ritmo_bajo_son_ningun_ritmo(self):
        """Publicarlo «con asterisco» garantiza que alguien lo promedie con los
        buenos. Es la misma decisión que ya tomó el CPK."""
        r = km_por_dia([_lec(50000, confianza=Confianza.DUDOSA),
                        _lec(51000, dias=10, confianza=Confianza.DUDOSA)])
        assert r.km_dia is SIN_DATO
        assert 'dudosas' in r.motivo

    def test_dos_verificadas_dan_verificada(self):
        r = km_por_dia([_lec(50000, confianza=Confianza.VERIFICADA),
                        _lec(51000, dias=10, confianza=Confianza.VERIFICADA)])
        assert r.marca == Confianza.VERIFICADA


class TestLaCorreccionCortaElTramo:
    """**El caso THP696.** Sin esto, el km/día de ese camión sería de seis
    cifras para siempre y la corrección no lo arreglaría."""

    def test_la_lectura_envenenada_queda_fuera_del_ritmo(self):
        lecturas = [
            _lec(55349),
            _lec(16697948, dias=13),
            _lec(55400, dias=14, origen=OrigenLectura.CORRECCION,
                 motivo='el número anterior es imposible'),
            _lec(55900, dias=19),
        ]
        r = km_por_dia(lecturas)
        # 500 km en 5 días: sólo las dos vigentes.
        assert r.km_dia == Decimal('100')
        assert r.n == 2

    def test_sin_correccion_el_tramo_es_toda_la_serie(self):
        """La otra dirección: la ventana **no se aplica** cuando no hubo
        corrección. Un guard que recortara siempre mediría otra cosa."""
        r = km_por_dia([_lec(50000), _lec(50500, dias=5), _lec(51000, dias=10)])
        assert r.n == 3
        assert r.km_dia == Decimal('100')


class TestNadaDeEstoEsUnUmbral:
    """Regla 13. `km_por_dia` publica un hecho y no juzga.

    Ni un km/día de 5 ni uno de 900 producen nada distinto de un número con su
    marca. Quien decida que algo es «pronto» es otra función, y esa lleva su
    umbral declarado con nombre propio (`DIAS_AVISO_PREVENTIVO`).
    """

    def test_un_ritmo_altisimo_sale_igual_que_uno_normal(self):
        r = km_por_dia([_lec(0), _lec(9000, dias=10)])
        assert r.km_dia == Decimal('900')
        assert r.marca == Confianza.DECLARADA
        assert r.motivo is None

    def test_no_hay_ventana_de_dias_que_recorte_la_historia(self):
        """Se mide sobre TODAS las vigentes. Cualquier ventana —30 días, 90—
        sería un umbral inventado, y con `n` y `dias` publicados quien lea el
        número sabe sobre cuánta historia está parado."""
        r = km_por_dia([_lec(0), _lec(36500, dias=365)])
        assert r.n == 2
        assert r.dias == Decimal('365')
        assert r.km_dia == Decimal('100')


class TestElRitmoSeSirveEnteroParaPoderDudar:
    """`n` y `dias` no son adorno: el mismo cociente sobre dos lecturas de un
    día y sobre cuarenta de tres meses son dos números distintos."""

    def test_es_un_RitmoDeUso_y_no_una_tupla_suelta(self):
        assert isinstance(km_por_dia([_lec(1), _lec(2, dias=1)]), RitmoDeUso)

    def test_el_sin_dato_siempre_trae_su_motivo(self):
        """Un `SIN_DATO` sin motivo obliga a adivinar si faltan lecturas, si el
        vehículo está quieto o si el tramo es dudoso — y las tres se corrigen
        distinto."""
        for lecturas in ([], [_lec(1)], [_lec(1), _lec(2, dias=0)],
                         [_lec(2), _lec(1, dias=1)]):
            r = km_por_dia(lecturas)
            assert r.km_dia is SIN_DATO
            assert r.motivo, f'sin motivo para {lecturas}'

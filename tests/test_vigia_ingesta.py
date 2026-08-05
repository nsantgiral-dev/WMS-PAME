"""
Las tres series de negocio del Vigía, alimentadas hacia adelante.

`despachos_{co}`, `facturacion_{co}` y `facturas_{co}` tenían línea base
histórica —26 semanas cargadas por TXT— y **ninguna ingesta viva**. El CUSUM
vigilaba dos series de cinco: la adopción del picking, y nada del negocio.

Lo que se prueba acá no es que escriba filas. Es lo que hace que un detector
sea confiable:

  1. **Comparabilidad.** La agregación viva replica EXACTAMENTE la del TXT. Si
     midiera otra cosa, el CUSUM leería la diferencia de método como un
     desplome — un detector que dispara por cambiar de fuente es peor que no
     tener detector.
  2. **Una semana a medias no se escribe.** Parece un colapso.
  3. **Un fallo de red NO escribe cero.** Cero es "no se vendió"; hueco es "no
     sabemos". Escribir cero ante un timeout dispara una alarma de colapso
     operativo que no ocurrió — el error más caro que puede cometer un detector.
  4. **La línea base no se sobrescribe.** Sin las 26 semanas de referencia el
     CUSUM queda ciego ~6 meses.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.services.vigia_service import SerieVigia, VigiaService


def _lunes_pasado():
    hoy = date.today()
    lunes_actual = hoy - timedelta(days=hoy.weekday())
    return lunes_actual - timedelta(days=7)


@pytest.fixture
def base_historica(db):
    """Un CO con línea base, como quedaría después del TXT."""
    semana = _lunes_pasado() - timedelta(days=14)
    for serie, valor in (('despachos_003', 500), ('facturacion_003', 9_000_000),
                         ('facturas_003', 40)):
        db.session.add(SerieVigia(serie=serie, semana=semana, valor=valor,
                                  registros=40, fuente='HISTORICO'))
    db.session.commit()
    return {'co': '003', 'semana_historica': semana}


def _gw_falso(filas, simulacion=False):
    """Un gateway que devuelve `filas` en la primera página y nada después."""
    class _GW:
        modo_simulacion = simulacion

        def __init__(self):
            self.llamadas = 0

        def _get(self, api, params):
            self.llamadas += 1
            if self.llamadas > 1:
                return {'detalle': {'Table': []}}
            return {'detalle': {'Table': filas}}
    return _GW()


def _fila(cant, valor, doc, estado='1'):
    return {'f470_cant_base': cant, 'f470_vlr_neto': valor,
            'f350_id_tipo_docto': 'FEW', 'f350_consec_docto': doc,
            'f350_ind_estado': estado}


class TestComparabilidadConLaLineaBase:
    """La agregación viva tiene que reproducir la del cargador TXT.

    El TXT hace:
        despachos   → SUMA de cantidad   (no cuenta líneas)
        facturacion → SUMA de valor neto
        facturas    → documentos ÚNICOS

    El docstring del cargador dice "líneas despachadas" y el código suma
    `cantidad`. **El contrato es el código**: con él se construyeron las 26
    semanas de referencia.
    """

    def _correr(self, db, filas, co='003'):
        gw = _gw_falso(filas)
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            return VigiaService.alimentar_series_facturacion(cos=[co])

    def test_despachos_suma_cantidad_no_cuenta_lineas(self, app, db, base_historica):
        """Tres líneas de 10 unidades son 30, no 3."""
        self._correr(db, [_fila(10, 100, 'A'), _fila(10, 100, 'A'),
                          _fila(10, 100, 'B')])
        fila = SerieVigia.query.filter_by(
            serie='despachos_003', semana=_lunes_pasado()).first()
        assert fila is not None
        assert float(fila.valor) == 30, (
            'si cuenta líneas en vez de sumar cantidad, la serie viva no es '
            'comparable con la línea base y el CUSUM dispara por el cambio')
        assert fila.registros == 3

    def test_facturacion_suma_valor_neto(self, app, db, base_historica):
        self._correr(db, [_fila(1, 1500.50, 'A'), _fila(1, 2499.50, 'B')])
        fila = SerieVigia.query.filter_by(
            serie='facturacion_003', semana=_lunes_pasado()).first()
        assert float(fila.valor) == 4000.0

    def test_facturas_cuenta_documentos_unicos(self, app, db, base_historica):
        """Cinco líneas de dos facturas son 2, no 5. Es la serie de FRECUENCIA
        de servicio: mide visitas, no volumen."""
        self._correr(db, [_fila(1, 10, 'A'), _fila(1, 10, 'A'), _fila(1, 10, 'A'),
                          _fila(1, 10, 'B'), _fila(1, 10, 'B')])
        fila = SerieVigia.query.filter_by(
            serie='facturas_003', semana=_lunes_pasado()).first()
        assert float(fila.valor) == 2

    def test_las_anuladas_no_cuentan(self, app, db, base_historica):
        """Estado 9 en Siesa es anulado. Sumarlas infla la semana."""
        self._correr(db, [_fila(10, 100, 'A'), _fila(99, 9999, 'Z', estado='9')])
        fila = SerieVigia.query.filter_by(
            serie='facturacion_003', semana=_lunes_pasado()).first()
        assert float(fila.valor) == 100


class TestLoQueNoSeEscribe:
    """Las tres negativas que hacen confiable al detector."""

    def test_la_semana_en_curso_no_se_escribe(self, app, db, base_historica):
        """Una semana a medias parece un desplome."""
        hoy = date.today()
        lunes_actual = hoy - timedelta(days=hoy.weekday())
        r = VigiaService.alimentar_series_facturacion(semana=lunes_actual)
        assert 'error' in r
        assert 'curso' in r['error'] or 'cerradas' in r['error']

    def test_un_fallo_de_red_NO_escribe_cero(self, app, db, base_historica):
        """EL caso. Un cero es 'no se vendió'; un hueco es 'no sabemos'.

        Escribir 0 ante un timeout dispara una alarma de colapso operativo que
        nunca ocurrió, y manda a alguien a investigar una caída inexistente.
        """
        class _Roto:
            modo_simulacion = False

            def _get(self, api, params):
                raise RuntimeError('Siesa no responde')

        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=_Roto()):
            r = VigiaService.alimentar_series_facturacion(cos=['003'])

        assert r['cos_sin_dato'] == ['003']
        assert r['series_escritas'] == 0
        assert SerieVigia.query.filter_by(
            serie='facturacion_003', semana=_lunes_pasado()).first() is None, (
            'se escribió una fila ante un fallo de red — eso es una alarma '
            'de colapso fabricada')

    def test_una_respuesta_None_tampoco(self, app, db, base_historica):
        """`_get` devuelve None con el breaker abierto."""
        class _Nada:
            modo_simulacion = False

            def _get(self, api, params):
                return None

        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=_Nada()):
            r = VigiaService.alimentar_series_facturacion(cos=['003'])
        assert r['cos_sin_dato'] == ['003']

    def test_en_simulacion_no_escribe_nada(self, app, db, base_historica):
        """Regla 8: un doble de prueba no deja rastro indistinguible del real.

        Una serie del Vigía escrita con datos simulados es peor que un dato
        falso cualquiera: alimenta el detector que decide si la empresa se está
        desplomando.
        """
        gw = _gw_falso([_fila(10, 100, 'A')], simulacion=True)
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            r = VigiaService.alimentar_series_facturacion(cos=['003'])
        assert 'error' in r
        assert SerieVigia.query.filter_by(serie='despachos_003',
                                          semana=_lunes_pasado()).first() is None

    def test_una_semana_sin_ventas_SI_escribe_cero(self, app, db, base_historica):
        """La contracara: una lista vacía es un hecho, y se registra.

        Si no se escribiera, una semana de verdad muerta sería indistinguible de
        una semana sin dato — y esa es justo la que el CUSUM tiene que ver.
        """
        gw = _gw_falso([])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            r = VigiaService.alimentar_series_facturacion(cos=['003'])
        assert r['cos_sin_dato'] == []
        fila = SerieVigia.query.filter_by(
            serie='facturacion_003', semana=_lunes_pasado()).first()
        assert fila is not None and float(fila.valor) == 0


class TestLaLineaBaseEsIntocable:

    def test_no_sobrescribe_una_fila_historica(self, app, db, base_historica):
        """Sin las 26 semanas de referencia el CUSUM queda ciego ~6 meses, y
        el TXT se cargó una sola vez."""
        semana = base_historica['semana_historica']
        gw = _gw_falso([_fila(1, 1, 'X')])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            VigiaService.alimentar_series_facturacion(semana=semana, cos=['003'])

        fila = SerieVigia.query.filter_by(serie='facturacion_003', semana=semana).first()
        assert fila.fuente == 'HISTORICO'
        assert float(fila.valor) == 9_000_000, 'se pisó la línea base'

    def test_reescribir_una_semana_de_produccion_es_idempotente(self, app, db, base_historica):
        gw1 = _gw_falso([_fila(10, 100, 'A')])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw1):
            VigiaService.alimentar_series_facturacion(cos=['003'])
        gw2 = _gw_falso([_fila(20, 200, 'A')])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw2):
            VigiaService.alimentar_series_facturacion(cos=['003'])

        filas = SerieVigia.query.filter_by(
            serie='facturacion_003', semana=_lunes_pasado()).all()
        assert len(filas) == 1, 'se duplicó la fila'
        assert float(filas[0].valor) == 200, 'no se actualizó al último valor'


class TestQueCOsSeVigilan:

    def test_solo_los_que_tienen_linea_base(self, app, db, base_historica):
        """Vigilar un C.O. sin μ_ref no sirve: el CUSUM no tiene contra qué
        comparar y la serie es un número suelto."""
        assert VigiaService._cos_a_vigilar() == ['003']


class TestElCronNaceApagado:
    """Regla 10 — todo cron que escribe nace apagado, por variable de entorno.

    Acá el motivo es concreto: el primer ciclo consulta Siesa por cada C.O. y
    escribe series que el CUSUM evalúa de inmediato. Si la agregación viva no
    reprodujera la histórica, el detector leería la diferencia de método como
    un desplome — y esa comprobación hay que hacerla a mano ANTES.
    """

    def test_el_interruptor_existe_y_esta_apagado_por_defecto(self):
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'vigia_service.py').read_text(encoding='utf-8')
        assert 'VIGIA_INGESTA_FACTURACION' in fuente
        assert "os.getenv('VIGIA_INGESTA_FACTURACION', '')" in fuente, (
            'el default tiene que ser apagado')

    def test_hay_forma_de_correrlo_a_mano_para_verificar(self):
        """Sin el endpoint, encender el cron sería confiar en que el cálculo
        coincide con la base sin haberlo comprobado nunca."""
        from pathlib import Path

        rutas = (Path(__file__).resolve().parents[1] / 'app' / 'routes'
                 / 'vigia.py').read_text(encoding='utf-8')
        assert 'ingesta/facturacion' in rutas


class TestLaComparacionContraLaLineaBase:
    """El paso que decide si el cron se puede encender.

    Sin esto, encender la ingesta es confiar en que dos cálculos coinciden sin
    haberlo mirado nunca. Y si no coinciden, el CUSUM lee la diferencia de
    método como un desplome del negocio.
    """

    def test_no_escribe_nada(self, app, db, base_historica):
        """Comparar es una pregunta, no una acción."""
        semana = base_historica['semana_historica']
        antes = SerieVigia.query.count()
        gw = _gw_falso([_fila(500, 9_000_000, 'A')])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            VigiaService.comparar_con_linea_base(semana, cos=['003'])
        assert SerieVigia.query.count() == antes

    def test_cuando_coincide_dice_que_es_apto(self, app, db, base_historica):
        semana = base_historica['semana_historica']
        # despachos=500, facturacion=9.000.000, facturas=40 → igual a la base
        filas = [_fila(500, 9_000_000, f'D{i}') for i in range(1)]
        filas += [_fila(0, 0, f'D{i}') for i in range(1, 40)]
        gw = _gw_falso(filas)
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            r = VigiaService.comparar_con_linea_base(semana, cos=['003'])
        assert r['difieren'] == 0
        assert r['apto_para_encender'] is True

    def test_cuando_difiere_lo_dice_y_NO_es_apto(self, app, db, base_historica):
        """El caso que importa: la diferencia de método se ve ANTES de encender."""
        semana = base_historica['semana_historica']
        gw = _gw_falso([_fila(1, 1, 'X')])   # nada que ver con la base
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            r = VigiaService.comparar_con_linea_base(semana, cos=['003'])
        assert r['difieren'] > 0
        assert r['apto_para_encender'] is False
        assert any(f['estado'] == 'DIFIERE' for f in r['filas'])

    def test_una_semana_sin_base_no_cuenta_como_apta(self, app, db, base_historica):
        """Comparar contra nada no es comparar."""
        semana = _lunes_pasado()
        gw = _gw_falso([_fila(1, 1, 'X')])
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            r = VigiaService.comparar_con_linea_base(semana, cos=['003'])
        assert r['sin_linea_base'] == 3
        assert r['apto_para_encender'] is False

    def test_el_panel_tiene_el_boton(self):
        """Una verificación que nadie puede disparar no verifica nada."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'
              / 'vigia.js').read_text(encoding='utf-8')
        assert js.count('vigiaCompararIngesta') >= 2, 'definida y sin caller'


class TestPrecioRealizadoHaciaAdelante:
    """`PrecioRealizado` solo se llenaba con el TXT bloqueado.

    Es el precio al que DE VERDAD se vende —valor/cantidad sobre ventas reales,
    neto de descuentos y de la escalera clandestina—. Sin alimentación viva, la
    jerarquía de costos usa un promedio de toda la historia que arrastra listas
    de hace años.

    La misma consulta que alimenta las series lo trae. Segunda salida de la
    misma pasada, igual que hace el cargador TXT.
    """

    def _correr(self, filas, co='003'):
        gw = _gw_falso(filas)
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=gw):
            return VigiaService.alimentar_series_facturacion(cos=[co])

    def _fila_ref(self, ref, cant, valor, doc='A'):
        f = _fila(cant, valor, doc)
        f['f120_referencia'] = ref
        return f

    def test_escribe_el_precio_de_la_semana(self, app, db, base_historica):
        from app.models.precio_realizado import PrecioRealizado

        self._correr([self._fila_ref('SKU1', 10, 1000)])
        semanal = PrecioRealizado.query.filter_by(
            referencia='SKU1', centro_operacion=None,
            periodo=f'S-{_lunes_pasado().isoformat()}').first()
        assert semanal is not None
        assert float(semanal.precio_realizado) == 100.0

    def test_es_el_cociente_de_los_totales_no_el_promedio_de_cocientes(
            self, app, db, base_historica):
        """Una venta de 1 unidad a $500 y otra de 99 a $100 dan $104, no $300.

        El promedio de cocientes pesa igual una venta chica que una grande — y
        la escalera de descuentos vive justo en las grandes.
        """
        from app.models.precio_realizado import PrecioRealizado

        self._correr([self._fila_ref('SKU1', 1, 500),
                      self._fila_ref('SKU1', 99, 9900)])
        f = PrecioRealizado.query.filter_by(
            referencia='SKU1', centro_operacion=None,
            periodo=f'S-{_lunes_pasado().isoformat()}').first()
        assert float(f.precio_realizado) == pytest.approx(104.0, abs=0.01)

    def test_correr_la_misma_semana_dos_veces_NO_duplica(self, app, db, base_historica):
        """La trampa de un acumulador: por eso cada semana es su propia fila y
        el promedio se RECALCULA, no se suma."""
        from app.models.precio_realizado import PrecioRealizado

        self._correr([self._fila_ref('SKU1', 10, 1000)])
        self._correr([self._fila_ref('SKU1', 10, 1000)])
        vivo = PrecioRealizado.query.filter_by(
            referencia='SKU1', centro_operacion=None, periodo='VIVO').first()
        assert float(vivo.cantidad_total) == 10, 'se contó dos veces'
        assert float(vivo.precio_realizado) == 100.0

    def test_no_toca_la_fila_TOTAL_del_TXT(self, app, db, base_historica):
        from app.models.precio_realizado import PrecioRealizado

        db.session.add(PrecioRealizado(
            referencia='SKU1', centro_operacion=None, periodo='TOTAL',
            valor_total=999999, cantidad_total=1, precio_realizado=999999))
        db.session.commit()

        self._correr([self._fila_ref('SKU1', 10, 1000)])
        total = PrecioRealizado.query.filter_by(
            referencia='SKU1', centro_operacion=None, periodo='TOTAL').first()
        assert float(total.precio_realizado) == 999999, 'se pisó la historia del TXT'


class TestLaLecturaDeCostoNoCambiaSinIngesta:
    """La garantía que hace seguro este cambio.

    `costo_service._precios_realizados` ahora prefiere VIVO sobre TOTAL. Si no
    hay ingesta viva —el estado de hoy— tiene que comportarse EXACTAMENTE como
    antes: gana TOTAL, que era la única fila que existía.
    """

    def test_sin_fila_VIVO_gana_TOTAL(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import _precios_realizados

        db.session.add(PrecioRealizado(
            referencia='SKU9', centro_operacion=None, periodo='TOTAL',
            valor_total=1000, cantidad_total=10, precio_realizado=100))
        db.session.commit()
        assert _precios_realizados(['SKU9']) == {'SKU9': 100.0}

    def test_con_fila_VIVO_gana_VIVO(self, app, db):
        """Un precio del último trimestre describe mejor lo que hoy se cobra."""
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import _precios_realizados

        db.session.add_all([
            PrecioRealizado(referencia='SKU9', centro_operacion=None, periodo='TOTAL',
                            valor_total=1000, cantidad_total=10, precio_realizado=100),
            PrecioRealizado(referencia='SKU9', centro_operacion=None, periodo='VIVO',
                            valor_total=1400, cantidad_total=10, precio_realizado=140),
        ])
        db.session.commit()
        assert _precios_realizados(['SKU9']) == {'SKU9': 140.0}

    def test_las_filas_SEMANALES_no_se_cuelan_en_la_lectura(self, app, db):
        """Antes la consulta traía TODAS las filas del SKU y cuál ganaba en el
        dict era arbitrario. Ese es el bug que el filtro por periodo evita."""
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import _precios_realizados

        db.session.add_all([
            PrecioRealizado(referencia='SKU9', centro_operacion=None, periodo='TOTAL',
                            valor_total=1000, cantidad_total=10, precio_realizado=100),
            PrecioRealizado(referencia='SKU9', centro_operacion=None, periodo='S-2026-01-05',
                            valor_total=90, cantidad_total=10, precio_realizado=9),
        ])
        db.session.commit()
        assert _precios_realizados(['SKU9']) == {'SKU9': 100.0}, (
            'una fila semanal se coló y desplazó al TOTAL')

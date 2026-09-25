"""
Fotos diarias de Siesa (m036fotos): ventas, stock y cartera.

La propiedad que manda: **lo completo se escribe; lo fallido se declara y no
escribe un cero.** Un rechazo (`alerta`), una excepción, una respuesta vacía
del breaker o una paginación truncada dejan la corrida `completa=False` con su
motivo, y `filas_vigentes` devuelve `None` — hueco declarado, no «cero ventas».

Siesa falso: `_Siesa` sirve páginas por API y cuenta las llamadas.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.utils.fecha import TZ_BOGOTA


def _venta(rowid, doc=1449, co='003', dia='2026-09-22', neto=74500.0, pedido=1502,
           estado=1):
    return {
        'f470_rowid': rowid, 'f350_rowid': 624000 + doc, 'f350_id_co': co,
        'f350_id_tipo_docto': 'FE', 'f350_consec_docto': doc,
        'f350_fecha': f'{dia}T00:00:00', 'f350_id_clase_docto': 520,
        'f350_ind_estado': estado, 'f200_id_fact': '1000124053',
        'f200_nit_fact': '1000124053', 'f200_razon_social_fact': 'BENITEZ',
        'f461_id_sucursal_fact': '001', 'f210_codigo_vendedor': '001 ',
        'f200_id_vendedor': 'Generico', 'f461_id_cond_pago': 'C01',
        'f150_id': 'NB1', 'f120_id': 17368, 'f120_referencia': 'PAPELSP9218',
        'f470_id_concepto': 501, 'f470_id_motivo': '01', 'f470_id_causal_devol': None,
        'f470_ind_naturaleza': 2, 'f470_id_un_movto': None, 'f281_id': '001',
        'f470_cant_base': 5.0, 'f470_precio_uni': 14900.0, 'f470_vlr_bruto': 62605.0,
        'f470_vlr_dscto_linea': 0.0, 'f470_vlr_dscto_global': 0.0,
        'f470_vlr_imp': 11895.0, 'f470_vlr_neto': neto, 'f470_costo_prom_tot': 59345.95,
        'f430_id_co': '003', 'f430_id_tipo_docto': 'PD', 'f430_consec_docto': pedido,
    }


def _cxc(rowid, db_=1000.0, cr=250.0, vcto='2026-09-20', consec=20):
    return {'f353_rowid': rowid, 'f353_id_co_cruce': '001',
            'f353_id_tipo_docto_cruce': 'FE', 'f353_consec_docto_cruce': consec,
            'f353_nro_cuota_cruce': 0, 'f353_fecha': '2026-09-01T00:00:00',
            'f353_fecha_cancelacion': None, 'f353_fecha_vcto': f'{vcto}T00:00:00',
            'f200_id': '1080017085', 'f201_id_sucursal': '001', 'f253_id': '13050501',
            'f353_total_db': db_, 'f353_total_cr': cr, 'f353_id_un_cruce': '99'}


def _inv(ref, ex, tot, lote=''):
    return {'f150_id': 'NB1  ', 'f120_referencia': f'{ref}      ', 'f400_id_lote': lote,
            'f400_id_ubicacion_aux': None, 'f400_cant_existencia_1': ex,
            'f400_costo_prom_tot': tot}


class _Siesa:
    """`paginas[api]` = lista de respuestas: una lista de filas, `None`
    (breaker), una `Exception` para levantar, o una lista de páginas por pasada
    (`pasadas`)."""

    def __init__(self, paginas=None, pasadas=None):
        self.paginas = paginas or {}
        self.pasadas = pasadas or {}
        self.llamadas = []
        self.modo_simulacion = False
        self._pasada = {}

    def _get(self, api, params=None, **k):
        self.llamadas.append((api, dict(params or {})))
        pag = int(params['paginacion'].split('numPag=')[1].split('|')[0])
        if api in self.pasadas:
            if pag == 1:
                self._pasada[api] = self._pasada.get(api, -1) + 1
            fuente = self.pasadas[api][self._pasada[api]]
        else:
            fuente = self.paginas.get(api, [[]])
        r = fuente[pag - 1] if pag - 1 < len(fuente) else []
        if isinstance(r, Exception):
            raise r
        if r is None:
            return None
        return {'codigo': 0, 'detalle': {'Table': r}}


_V = 'API_v2_Ventas_Facturas_DesdePedido'
_C = 'API_v2_CxC_General'
_I = 'API_v2_Inventarios_InvFecha'
_DIA = date(2026, 9, 22)


def _cien(base, **kw):
    return [_venta(base + i, **kw) for i in range(100)]


class TestFotoDeVentas:

    def test_lo_completo_se_escribe(self, db):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[_venta(1), _venta(2)]]}))
        assert c['completa'] and c['filas'] == 2 and c['paginas'] == 1
        fila = FotoVentaLinea.query.filter_by(f470_rowid=1).one()
        assert fila.completa and fila.dia_operativo == _DIA
        assert fila.pedido_clave == '003-PD-1502'
        assert fila.vendedor_codigo == '001', 'el código de vendedor viene con espacio'
        assert fila.vlr_neto == Decimal('74500.00')
        assert len(f.filas_vigentes('VENTAS', '003', _DIA)) == 2

    def test_el_filtro_lleva_la_fecha_entre_comillas(self, db):
        """Sin comillas Siesa contesta «sin registros» (verificado en vivo)."""
        from app.services import fotos_siesa_service as f
        s = _Siesa({_V: [[]]})
        f.fotografiar_ventas('003', _DIA, s)
        filtro = s.llamadas[0][1]['parametros']
        assert "f350_fecha >= ''20260922''" in filtro
        assert "f350_fecha <= ''20260922''" in filtro
        assert "f350_id_co = ''003''" in filtro
        assert 'tamPag=100' in s.llamadas[0][1]['paginacion']

    def test_paginas_llenas_hasta_la_corta(self, db):
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), _cien(2000),
                                                           [_venta(3000)]]}))
        assert c['completa'] and c['filas'] == 201 and c['paginas'] == 3

    def test_pagina_exacta_se_cierra_con_la_vacia(self, db):
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), []]}))
        assert c['completa'] and c['filas'] == 100 and c['paginas'] == 2

    def test_un_dia_sin_ventas_es_un_cero_verdadero(self, db):
        """La otra cara: una respuesta vacía de verdad SÍ es un total, en cero."""
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('009', _DIA, _Siesa({_V: [[]]}))
        assert c['completa'] and c['filas'] == 0
        assert f.filas_vigentes('VENTAS', '009', _DIA) == []

    @pytest.mark.parametrize('segunda,en_motivo', [
        ([{'alerta': 'Por favor verifique los parámetros'}], 'verifique'),
        (Exception('Connekta no respondió — reintenta'), 'no respondió'),
        (None, 'no respondió'),
    ])
    def test_lo_fallido_se_declara_y_no_es_un_total(self, db, segunda, en_motivo):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), segunda]}))
        assert c['completa'] is False and en_motivo in c['motivo']
        assert f.filas_vigentes('VENTAS', '003', _DIA) is None, \
            'sin corrida completa no hay total: None, no cero'
        assert not FotoVentaLinea.query.filter_by(completa=True).count()

    def test_rechazo_en_la_primera_pagina_no_escribe_nada(self, db):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[{'alerta': 'no'}]]}))
        assert c['completa'] is False and FotoVentaLinea.query.count() == 0

    def test_truncada_no_es_completa(self, db, monkeypatch):
        from app.services import fotos_siesa_service as f
        monkeypatch.setattr(f, 'MAX_PAGINAS', 2)
        c = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), _cien(2000),
                                                           [_venta(1)]]}))
        assert c['completa'] is False and 'truncada' in c['motivo']

    def test_el_upsert_es_idempotente(self, db):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services import fotos_siesa_service as f
        s = _Siesa({_V: [[_venta(1), _venta(2)]]})
        f.fotografiar_ventas('003', _DIA, s)
        c2 = f.fotografiar_ventas('003', _DIA, s)
        assert FotoVentaLinea.query.count() == 2
        assert {x.run_id for x in FotoVentaLinea.query.all()} == {c2['run_id']}

    def test_una_lectura_incompleta_no_pisa_lo_que_ya_estaba(self, db):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services import fotos_siesa_service as f
        c1 = f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), [_venta(1)]]}))
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000), [{'alerta': 'x'}]]}))
        assert {x.run_id for x in FotoVentaLinea.query.all()} == {c1['run_id']}
        assert len(f.filas_vigentes('VENTAS', '003', _DIA)) == 101

    def test_una_linea_que_ya_no_viene_sale_del_total(self, db):
        from app.services import fotos_siesa_service as f
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[_venta(1), _venta(2)]]}))
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[_venta(1)]]}))
        assert [x.f470_rowid for x in f.filas_vigentes('VENTAS', '003', _DIA)] == [1]

    def test_paginacion_inestable_pide_segunda_pasada(self, db):
        from app.services import fotos_siesa_service as f
        sucia = [_cien(1000)[:50] + _cien(1000)[:50], [_venta(5000)]]
        limpia = [_cien(1000), [_venta(5000)]]
        c = f.fotografiar_ventas('003', _DIA, _Siesa(pasadas={_V: [sucia, limpia]}))
        assert c['completa'] and c['filas'] == 101

    def test_inestable_dos_veces_no_es_completa(self, db):
        from app.services import fotos_siesa_service as f
        sucia = [_cien(1000)[:50] + _cien(1000)[:50], [_venta(5000)]]
        c = f.fotografiar_ventas('003', _DIA, _Siesa(pasadas={_V: [sucia, sucia]}))
        assert c['completa'] is False and 'inestable' in c['motivo']


class TestFotoDeCartera:

    def test_saldo_y_dias_vencido(self, db):
        from app.models.fotos_siesa import FotoCarteraDiaria
        from app.services import fotos_siesa_service as f
        dia = date(2026, 9, 24)
        s = _Siesa({_C: [[_cxc(8), _cxc(9, vcto='2026-09-30')]]})
        c = f.fotografiar_cartera(dia, s)
        assert c['completa'] and c['filas'] == 2
        a = FotoCarteraDiaria.query.filter_by(f353_rowid=8).one()
        assert a.saldo == Decimal('750.00') and a.dias_vencido == 4
        assert FotoCarteraDiaria.query.filter_by(f353_rowid=9).one().dias_vencido == -6
        filtro = s.llamadas[0][1]['parametros']
        assert 'f353_fecha_cancelacion IS NULL' in filtro and "LIKE ''1305%''" in filtro

    def test_truncada_no_se_usa_como_total(self, db, monkeypatch):
        from app.services import fotos_siesa_service as f
        monkeypatch.setattr(f, 'MAX_PAGINAS', 1)
        dia = date(2026, 9, 24)
        c = f.fotografiar_cartera(dia, _Siesa({_C: [[_cxc(i) for i in range(100)]]}))
        assert c['completa'] is False
        assert f.filas_vigentes('CARTERA', 'TODAS', dia) is None


class TestFotoDeStock:

    def _stock(self, db, bodega, cod, ex, updated_at, comp=0.0):
        from app.models.stock_siesa import StockSiesa
        db.session.add(StockSiesa(bodega=bodega, codigo_siesa=cod, existencia=ex,
                                  comprometido=comp, salida_sin_conf=0,
                                  updated_at=updated_at))
        db.session.commit()

    def test_copia_stock_con_costo_y_marca_lo_vendible(self, db):
        from app.models.fotos_siesa import FotoStockDiaria
        from app.services import fotos_siesa_service as f
        from app.utils.fecha import dia_operativo_de
        ahora = datetime.utcnow()
        dia = dia_operativo_de(ahora)
        self._stock(db, 'NB1', 'A', 10, ahora)
        self._stock(db, 'NB1', 'B', 5, ahora)
        self._stock(db, 'NB1', 'CERO', 0, ahora)
        s = _Siesa({_I: [[_inv('A', 4, 400, 'L1'), _inv('A', 6, 660, 'L2')]]})
        c = f.fotografiar_stock('NB1', dia, s, costo=True)
        assert c['completa'] and c['filas'] == 2, 'el SKU en cero no se escribe'
        a = FotoStockDiaria.query.filter_by(codigo_siesa='A').one()
        assert a.vendible and a.costo_prom_uni == Decimal('106.0000')
        assert a.costo_prom_tot == Decimal('1060.00')
        b = FotoStockDiaria.query.filter_by(codigo_siesa='B').one()
        assert b.costo_prom_uni is None, 'sin costo en InvFecha es NULL, no cero'

    def test_bodega_de_servicio_no_es_vendible_ni_se_costea(self, db):
        from app.models.fotos_siesa import FotoStockDiaria
        from app.services import fotos_siesa_service as f
        from app.utils.fecha import dia_operativo_de
        ahora = datetime.utcnow()
        self._stock(db, 'AV1', 'A', 3, ahora)
        s = _Siesa()
        f.fotografiar_stock('AV1', dia_operativo_de(ahora), s, costo=True)
        fila = FotoStockDiaria.query.one()
        assert fila.vendible is False and fila.costo_prom_uni is None
        assert not s.llamadas

    def test_costo_rechazado_queda_null_y_declarado(self, db):
        import json
        from app.models.fotos_siesa import FotoCorrida, FotoStockDiaria
        from app.services import fotos_siesa_service as f
        from app.utils.fecha import dia_operativo_de
        ahora = datetime.utcnow()
        self._stock(db, 'NB1', 'A', 10, ahora)
        c = f.fotografiar_stock('NB1', dia_operativo_de(ahora),
                                _Siesa({_I: [[{'alerta': 'no'}]]}), costo=True)
        assert c['completa'], 'el stock sí está; lo que falta es el costo'
        assert FotoStockDiaria.query.one().costo_prom_uni is None
        det = json.loads(FotoCorrida.query.one().detalle)
        assert det['costo']['completo'] is False

    def test_stock_viejo_no_es_la_foto_de_hoy(self, db):
        from app.services import fotos_siesa_service as f
        ahora = datetime.utcnow()
        self._stock(db, 'NB1', 'A', 10, ahora - timedelta(days=3))
        c = f.fotografiar_stock('NB1', f.dia_operativo(), _Siesa(), costo=False)
        assert c['completa'] is False and 'no se descarga desde' in c['motivo']

    def test_fila_rezagada_se_marca(self, db):
        from app.models.fotos_siesa import FotoStockDiaria
        from app.services import fotos_siesa_service as f
        from app.utils.fecha import dia_operativo_de
        ahora = datetime.utcnow()
        self._stock(db, 'NB1', 'A', 10, ahora)
        self._stock(db, 'NB1', 'VIEJO', 7, ahora - timedelta(hours=5))
        f.fotografiar_stock('NB1', dia_operativo_de(ahora), _Siesa(), costo=False)
        assert FotoStockDiaria.query.filter_by(codigo_siesa='VIEJO').one().rezagada
        assert not FotoStockDiaria.query.filter_by(codigo_siesa='A').one().rezagada

    def test_sin_stock_siesa_se_declara(self, db):
        from app.services import fotos_siesa_service as f
        c = f.fotografiar_stock('NB1', f.dia_operativo(), _Siesa(), costo=False)
        assert c['completa'] is False and 'no tiene filas' in c['motivo']


class TestLaCorridaDelDia:

    @pytest.fixture(autouse=True)
    def _encendido(self, monkeypatch):
        monkeypatch.setenv('FOTOS_SIESA', 'true')
        monkeypatch.setenv('FOTOS_SIESA_DIAS_VENTAS', '0')

    def _siesa(self):
        return _Siesa({_V: [[]], _C: [[_cxc(1)]], _I: [[]]})

    def test_nace_apagada(self, db, monkeypatch):
        from app.services import fotos_siesa_service as f
        monkeypatch.delenv('FOTOS_SIESA')
        s = self._siesa()
        r = f.correr_fotos(s, ahora=datetime(2026, 9, 24, 12, 0, tzinfo=TZ_BOGOTA))
        assert 'omitido' in r and not s.llamadas

    def test_en_simulacion_no_fotografia(self, db):
        from app.services import fotos_siesa_service as f
        s = self._siesa()
        s.modo_simulacion = True
        assert 'omitido' in f.correr_fotos(
            s, ahora=datetime(2026, 9, 24, 12, 0, tzinfo=TZ_BOGOTA))

    # La ventana de Siesa es UNA (06:00–19:30, `ventana_siesa`) desde el 2026-09-25.
    @pytest.mark.parametrize('hora,minuto', [(5, 59), (19, 31), (22, 0)])
    def test_fuera_de_la_ventana_no_toca_siesa(self, db, hora, minuto, ventana_qa):
        from app.services import fotos_siesa_service as f
        s = self._siesa()
        r = f.correr_fotos(s, ahora=datetime(2026, 9, 24, hora, minuto, tzinfo=TZ_BOGOTA))
        assert 'omitido' in r and not s.llamadas

    def test_el_dia_es_el_de_bogota_en_la_franja_de_la_noche(self, db):
        """19:15 en Bogotá = 00:15 del 25 en UTC. La foto es del 24."""
        from app.models.fotos_siesa import FotoCarteraDiaria, FotoCorrida
        from app.services import fotos_siesa_service as f
        ahora = datetime(2026, 9, 24, 19, 15, tzinfo=TZ_BOGOTA)
        r = f.correr_fotos(self._siesa(), ahora=ahora)
        assert r['cartera']['dia_operativo'] == '2026-09-24'
        assert {c.dia_operativo for c in FotoCorrida.query.all()} == {date(2026, 9, 24)}
        assert FotoCarteraDiaria.query.one().dias_vencido == 4

    def test_si_la_ventana_se_cierra_lo_que_falta_se_declara(self, db, ventana_qa):
        from app.services import fotos_siesa_service as f
        horas = iter([datetime(2026, 9, 24, 19, 29, tzinfo=TZ_BOGOTA)] * 3
                     + [datetime(2026, 9, 24, 19, 40, tzinfo=TZ_BOGOTA)] * 100)
        r = f.correr_fotos(self._siesa(), reloj=lambda: next(horas))
        assert r['no_corrieron'], r
        assert any(x.startswith('stock') for x in r['no_corrieron'])


class TestValorFacturaDesdeLaFoto:

    def _packing(self, db, almacen, codigo, clave=None, fe=None, valor=None,
                 triggered=True):
        from app.models.packing import TareaPacking
        t = TareaPacking(codigo=codigo, almacen_id=almacen.id, estado='DESPACHADO',
                         numero_pedido_siesa=codigo, pedido_clave=clave,
                         siesa_triggered=triggered, valor_factura=valor,
                         fe_tipo=fe[0] if fe else None, fe_consec=fe[1] if fe else None)
        db.session.add(t)
        db.session.commit()
        return t

    def test_por_factura_resuelta(self, db, almacen):
        from app.services import fotos_siesa_service as f
        t = self._packing(db, almacen, 'P1', fe=('FE', '1449'))
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[
            _venta(1, neto=100.0), _venta(2, neto=50.5), _venta(3, doc=9, neto=7.0)]]}))
        r = f.completar_valor_factura()
        db.session.refresh(t)
        assert t.valor_factura == Decimal('150.50') and r['por_factura'] == 1

    def test_por_pedido_solo_si_es_inequivoco(self, db, almacen):
        from app.services import fotos_siesa_service as f
        uno = self._packing(db, almacen, 'P1', clave='003-PD-1502')
        dos = self._packing(db, almacen, 'P2', clave='003-PD-1600')
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[
            _venta(1, doc=1, neto=10.0, pedido=1502),
            _venta(2, doc=2, neto=20.0, pedido=1600),
            _venta(3, doc=3, neto=30.0, pedido=1600)]]}))
        r = f.completar_valor_factura()
        db.session.refresh(uno)
        db.session.refresh(dos)
        assert uno.valor_factura == Decimal('10.00')
        assert dos.valor_factura is None, 'dos facturas para un pedido: no se adivina'
        assert r['ambiguos'] == 1

    def test_no_pisa_lo_que_anoto_el_conductor_ni_usa_anuladas(self, db, almacen):
        from app.services import fotos_siesa_service as f
        ya = self._packing(db, almacen, 'P1', fe=('FE', '1449'), valor=Decimal('99'))
        anulada = self._packing(db, almacen, 'P2', fe=('FE', '77'))
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[
            _venta(1, neto=100.0), _venta(2, doc=77, estado=9)]]}))
        f.completar_valor_factura()
        db.session.refresh(ya)
        db.session.refresh(anulada)
        assert ya.valor_factura == Decimal('99.00') and anulada.valor_factura is None

    def test_una_foto_incompleta_no_da_valor(self, db, almacen):
        from app.services import fotos_siesa_service as f
        t = self._packing(db, almacen, 'P1', fe=('FE', '1449'))
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [_cien(1000),
                                                       [{'alerta': 'x'}]]}))
        f.completar_valor_factura()
        db.session.refresh(t)
        assert t.valor_factura is None


class TestSaludPublicaLaFrescura:

    def test_health_siesa_trae_las_fotos(self, client, db, jwt_token_admin):
        from app.services import fotos_siesa_service as f
        f.fotografiar_ventas('003', _DIA, _Siesa({_V: [[{'alerta': 'x'}]]}))
        r = client.get('/api/health/siesa',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        fotos = r.get_json()['fotos_siesa']
        assert fotos['encendido'] is False
        assert fotos['ultima_corrida']['VENTAS']['completa'] is False

    def test_estado_declara_huecos(self, db):
        from app.services import fotos_siesa_service as f
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        f.fotografiar_ventas('003', hoy, _Siesa({_V: [[{'alerta': 'x'}]]}))
        f.fotografiar_ventas('001', hoy, _Siesa({_V: [[]]}))
        huecos = f.estado()['huecos_ultimos_dias']
        assert {'tipo': 'VENTAS', 'alcance': '003', 'dia': hoy.isoformat()} in huecos
        assert not any(h['alcance'] == '001' for h in huecos)

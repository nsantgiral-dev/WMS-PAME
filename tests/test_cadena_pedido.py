"""
La clave del pedido que une picking, packing, historia y fotos (m036fotos).

Hasta el 2026-09-24 picking y packing del mismo pedido se unían solo porque
`TareaPicking.referencia_documento` y `TareaPacking.numero_pedido_siesa`
coincidían **como texto** — sin CO, que es parte de la clave documental
(Regla 18). El packing nace en paralelo con `crear_manual`, no desde el
picking: no había nada que dijera «estas tareas son del mismo pedido».

Lo que se exige acá:
- la clave se arma en UNA función (`cadena_pedido.clave_pedido`);
- se puebla al crear, en toda puerta (picking y packing);
- el backfill de la migración usa la misma regla y cubre los strings viejos;
- sin CO no se inventa: la clave queda NULL.
"""
import importlib.util
import pathlib
from unittest.mock import patch

import pytest

_MIG = (pathlib.Path(__file__).resolve().parents[1] / 'migrations' / 'versions'
        / 'm036fotos_fase0_analitica.py')


def _migracion():
    spec = importlib.util.spec_from_file_location('_m034', _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLaClave:

    @pytest.mark.parametrize('co,tipo,consec,esperada', [
        ('003', 'PD', 1502, '003-PD-1502'),
        ('3', 'pd ', '1502', '003-PD-1502'),
        (' 003 ', 'PD ', '001502', '003-PD-1502'),
        ('003', 'PD', None, None),
        (None, 'PD', 1502, None),
        ('003', '', 1502, None),
        ('003', 'PD', 'X12', None),
    ])
    def test_normaliza_o_no_inventa(self, co, tipo, consec, esperada):
        from app.services.cadena_pedido import clave_pedido
        assert clave_pedido(co, tipo, consec) == esperada

    @pytest.mark.parametrize('numero,esperado', [
        ('PD1502', ('PD', 1502)), ('pd-001502', ('PD', 1502)),
        (' PD 7 ', ('PD', 7)), ('1502', None), ('', None), (None, None),
        ('ST-20260603-001', None),
    ])
    def test_parte_el_numero_del_pedido(self, numero, esperado):
        from app.services.cadena_pedido import partir_numero_pedido
        assert partir_numero_pedido(numero) == esperado


class TestDeDondeSaleElCO:

    def _pedido(self, db, co, consec=1502, item='X'):
        from app.models.pedido_siesa import PedidoSiesa
        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=consec, centro_op=co,
                                   bodega='NB1', numero_pedido=f'PD{consec}',
                                   item_codigo=item))
        db.session.commit()

    def test_manda_el_co_de_siesa(self, db, almacen):
        from app.services.cadena_pedido import co_del_pedido
        almacen.centro_op_siesa = '999'
        self._pedido(db, '003')
        assert co_del_pedido('PD', 1502, almacen.id) == '003'

    def test_sin_pedido_cae_al_almacen(self, db, almacen):
        from app.services.cadena_pedido import co_del_pedido
        almacen.centro_op_siesa = '003'
        db.session.commit()
        assert co_del_pedido('PD', 1502, almacen.id) == '003'

    def test_almacen_sin_co_cae_al_maestro_de_bodegas(self, db, almacen):
        """`co_de_bodega`, no un mapa propio."""
        from app.services.cadena_pedido import co_del_pedido
        almacen.centro_op_siesa = None
        almacen.bodega_siesa_id = 'NC1'
        db.session.commit()
        assert co_del_pedido('PD', 1502, almacen.id) == '002'

    def test_dos_co_para_el_mismo_consecutivo_no_se_adivina(self, db, almacen):
        from app.services.cadena_pedido import co_del_pedido
        self._pedido(db, '003', item='A')
        self._pedido(db, '001', item='B')
        assert co_del_pedido('PD', 1502, almacen.id) is None

    def test_sin_nada_es_none(self, db):
        from app.services.cadena_pedido import co_del_pedido
        assert co_del_pedido('PD', 1502, None) is None


class TestSePuebladaAlCrear:
    """En toda puerta, no solo en la ruta de Siesa."""

    def test_iniciar_despacho_da_la_misma_clave_a_picking_y_packing(
            self, app, db, client, jwt_token_admin, almacen, producto, inv_picking):
        from app.models.packing import TareaPacking
        from app.models.pedido_siesa import PedidoSiesa
        from app.models.picking import TareaPicking
        from app.services.connekta_gateway import CompromisosNoDisponibles

        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=1502, centro_op='003',
                                   bodega='NB1', numero_pedido='PD1502',
                                   item_codigo=producto.codigo_siesa, cliente='CLIENTE X'))
        db.session.commit()
        with patch('app.services.despacho_parcial_service.DespachoParialService.'
                   'obtener_compromisos', side_effect=CompromisosNoDisponibles('sin red')):
            r = client.post('/api/siesa/iniciar-despacho', json={
                'numero_pedido': 'PD1502', 'tipo_docto': 'PD', 'consec_docto': '1502',
                'almacen_id': almacen.id,
                'items': [{'producto_id': producto.id, 'item_codigo': producto.codigo_siesa,
                           'cantidad_pendiente': 5}]},
                headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 201, r.get_json()
        picks = TareaPicking.query.filter_by(referencia_documento='PD1502').all()
        pack = TareaPacking.query.filter_by(numero_pedido_siesa='PD1502').one()
        assert picks
        assert {t.pedido_clave for t in picks} == {'003-PD-1502'}
        assert pack.pedido_clave == '003-PD-1502'
        assert pack.to_dict()['pedido_clave'] == '003-PD-1502'

    def test_crear_desde_picking_tambien(self, db, almacen, producto, inv_picking):
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        almacen.centro_op_siesa = '003'
        db.session.commit()
        tareas = PickingService.crear_tareas(
            producto_id=producto.id, cantidad=3, almacen_id=almacen.id,
            referencia_documento='PD77', tipo_documento='PEDIDO')
        for t in tareas:
            t.estado, t.cantidad_recogida = 'COMPLETADO', t.cantidad_solicitada
        db.session.commit()
        pack = PackingService.crear_desde_picking([t.id for t in tareas], 'PD77', almacen.id)
        assert {t.pedido_clave for t in tareas} == {'003-PD-77'}
        assert pack.pedido_clave == '003-PD-77'

    @pytest.mark.parametrize('tipo', [None, 'TRASLADO', 'MANUAL'])
    def test_lo_que_no_es_pedido_no_lleva_clave(self, db, almacen, producto,
                                                inv_picking, tipo):
        """`'MANUAL-1'` tiene la forma de `'PD1502'`: sin tipo de pedido,
        armaría la clave de un pedido que no existe."""
        from app.services.picking_service import PickingService
        almacen.centro_op_siesa = '003'
        db.session.commit()
        tareas = PickingService.crear_tareas(
            producto_id=producto.id, cantidad=2, almacen_id=almacen.id,
            referencia_documento='MANUAL-1', tipo_documento=tipo)
        assert {t.pedido_clave for t in tareas} == {None}

    def test_sin_co_no_se_inventa(self, db, almacen):
        from app.services.packing_service import PackingService
        almacen.centro_op_siesa = None
        almacen.bodega_siesa_id = None
        db.session.commit()
        pack = PackingService.crear_manual('PD88', almacen.id, [], 'PD', '88')
        assert pack.pedido_clave is None


class TestElBackfillUsaLaMismaRegla:
    """Una migración no importa código de la app, así que la regla está dos
    veces. Este test es lo que impide que diverjan."""

    @pytest.mark.parametrize('co,tipo,consec', [
        ('003', 'PD', 1502), ('3', 'pd ', '1502'), (' 003 ', 'PD ', '001502'),
        ('003', 'PD', None), (None, 'PD', 1), ('003', '', 1), ('ABC', 'fv', '9'),
        ('003', 'PD', 'X1'),
    ])
    def test_clave_identica(self, co, tipo, consec):
        from app.services.cadena_pedido import clave_pedido
        assert _migracion()._clave(co, tipo, consec) == clave_pedido(co, tipo, consec)

    @pytest.mark.parametrize('numero', ['PD1502', 'pd-0001', ' PD 7 ', '1502', '',
                                        None, 'ST-20260603-001', 'MANUAL-1'])
    def test_particion_identica(self, numero):
        from app.services.cadena_pedido import partir_numero_pedido
        assert _migracion()._partir(numero) == partir_numero_pedido(numero)


class TestElBackfillCubreLosStringsViejos:

    def _datos(self, db, almacen, producto, ub_picking):
        from app.models.almacen import Almacen
        from app.models.packing import TareaPacking
        from app.models.pedido_siesa import PedidoSiesa
        from app.models.picking import TareaPicking
        almacen.centro_op_siesa = '003'
        otro = Almacen(codigo='SIN-CO', nombre='Sin CO', activo=True)
        db.session.add(otro)
        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=10, centro_op='001',
                                   bodega='NS1', numero_pedido='PD10', item_codigo='A'))
        db.session.flush()

        def pick(ref, tipo, alm=almacen):
            t = TareaPicking(codigo=f'PK-{ref}-{tipo}', producto_id=producto.id,
                             cantidad_solicitada=1, ubicacion_id=ub_picking.id,
                             almacen_id=alm.id, referencia_documento=ref,
                             tipo_documento=tipo)
            db.session.add(t)
            return t

        def pack(numero, tipo_doc='PEDIDO', tipo=None, consec=None, alm=almacen):
            t = TareaPacking(codigo=f'PA-{numero}-{tipo_doc}', numero_pedido_siesa=numero,
                             tipo_documento=tipo_doc, tipo_docto_pedido_siesa=tipo,
                             consec_docto_pedido_siesa=consec, almacen_id=alm.id)
            db.session.add(t)
            return t

        filas = {
            'pack_explicito': pack('PD1502', tipo='PD', consec='1502'),
            'pack_parseado': pack('PD1503'),
            'pack_co_de_siesa': pack('PD10'),
            'pack_traslado': pack('ST-1', tipo_doc='TRASLADO'),
            'pack_sin_co': pack('PD20', alm=otro),
            'pick_siesa': pick('PD1502', 'PEDIDO_SIESA'),
            'pick_pedido': pick('PD10', 'PEDIDO'),
            'pick_traslado': pick('ST-1', 'TRASLADO'),
            'pick_manual': pick('MANUAL-1', None),
            'pick_sin_co': pick('PD20', 'PEDIDO', alm=otro),
        }
        db.session.commit()
        for t in filas.values():       # simula filas anteriores a la columna
            t.pedido_clave = None
        db.session.commit()
        return filas

    def test_rellena_con_la_regla_y_deja_null_lo_que_no_sabe(
            self, db, almacen, producto, ub_picking):
        filas = self._datos(db, almacen, producto, ub_picking)
        res = _migracion().backfill_pedido_clave(db.session.connection())
        db.session.commit()
        for t in filas.values():
            db.session.refresh(t)
        assert filas['pack_explicito'].pedido_clave == '003-PD-1502'
        assert filas['pack_parseado'].pedido_clave == '003-PD-1503'
        assert filas['pack_co_de_siesa'].pedido_clave == '001-PD-10', \
            'el CO de pedidos_siesa manda sobre el del almacén'
        assert filas['pack_traslado'].pedido_clave is None
        assert filas['pack_sin_co'].pedido_clave is None, 'sin CO no se inventa'
        assert filas['pick_siesa'].pedido_clave == '003-PD-1502'
        assert filas['pick_pedido'].pedido_clave == '001-PD-10'
        assert filas['pick_traslado'].pedido_clave is None
        assert filas['pick_manual'].pedido_clave is None
        assert filas['pick_sin_co'].pedido_clave is None
        assert res['packing'] == 3 and res['picking'] == 2
        assert res['packing_sin_clave'] == 1 and res['picking_sin_clave'] == 1

    def test_es_idempotente(self, db, almacen, producto, ub_picking):
        self._datos(db, almacen, producto, ub_picking)
        mig = _migracion()
        mig.backfill_pedido_clave(db.session.connection())
        segunda = mig.backfill_pedido_clave(db.session.connection())
        assert segunda['packing'] == 0 and segunda['picking'] == 0

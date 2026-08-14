"""
Un pedido recorriendo todo el WMS, y los invariantes evaluados sobre el
resultado.

Ninguno de los 1897 tests existentes puede fallar por un defecto de frontera:
cada archivo arma su propia `TareaPacking(...)` desde cero, así que verifica
cada etapa con datos que él mismo fabricó coherentes. Éste no fabrica nada
intermedio — cada etapa sale de llamar al servicio que la operación llama.
"""
import pytest

from app.services import auditoria
from tests.flujo import conductor_de_flujo as cf


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_flujo@test.com'),
                       ('conductor', 'cond_flujo@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('test123')
            db.session.add(u)
            db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


@pytest.fixture
def flujo(db, almacen, actores):
    return cf.flujo_completo(db, almacen, actores['operario'].id,
                             actores['conductor'].id)


class TestElFlujoLlegaHastaElFinal:
    """Si una etapa deja de producir lo que la siguiente espera, se rompe acá."""

    def test_el_pedido_llega_a_recaudo(self, flujo):
        assert flujo.packing_id and flujo.bultos and flujo.ruta_id
        assert flujo.recaudo_id

    def test_el_picking_fue_parcial(self, flujo):
        """El caso que operaciones confirmó como real. Un arnés que solo ejerce
        el caso feliz verifica un flujo que nadie tiene."""
        from app.models.picking import TareaPicking
        tareas = TareaPicking.query.filter(TareaPicking.id.in_(flujo.pickings)).all()
        assert tareas
        assert all(t.cantidad_recogida < t.cantidad_solicitada for t in tareas)


class TestLosInvariantesSeCumplenSobreUnFlujoSano:
    """El arnés produce datos por el camino real: nada debería romperse.

    Cuando algo se rompe acá, no es el test el que está mal — es que una etapa
    dejó de honrar lo que la siguiente asume.
    """

    def test_ningun_invariante_bloqueante_se_rompe(self, flujo):
        r = auditoria.auditar('venta')
        rotos = [x for x in r['resultados']
                 if x['severidad'] == auditoria.BLOQUEA and x['total']]
        assert not rotos, '\n' + '\n'.join(
            f"{x['codigo']} ({x['frontera']}): {x['total']} — "
            f"{x['hallazgos'][0]['detalle']}" for x in rotos)

    def test_ningun_invariante_revienta(self, flujo):
        """Un invariante que lanza excepción no dice «pasa»: dice que no se
        pudo saber, y eso tiene que verse."""
        r = auditoria.auditar('venta')
        assert not r['errores'], r['errores']


class TestElDetectorNoEstaCiego:
    """Un auditor que no encuentra nada se lee igual que uno que no corrió.

    Estos tests rompen el flujo a propósito y exigen que el invariante
    correspondiente lo vea. Sin ellos, `0 hallazgos` no significaría nada.
    """

    def test_hay_invariantes_registrados(self):
        assert len(auditoria.registrados('venta')) >= 10
        assert 'venta' in auditoria.flujos()

    def test_ve_un_item_sin_producto_local(self, db, flujo):
        """El defecto real: `pedidos_sync` deja `producto_id=None` en silencio
        y el empacador termina viendo «Producto None»."""
        from app.models.pedido_siesa import PedidoSiesa
        p = PedidoSiesa.query.filter_by(numero_pedido=flujo.pedido).first()
        p.producto_id = None
        db.session.commit()

        r = auditoria.auditar('venta')
        vta01 = next(x for x in r['resultados'] if x['codigo'] == 'VTA-01')
        assert vta01['total'] == 1
        assert flujo.pedido in vta01['hallazgos'][0]['referencia']

    def test_ve_un_nombre_que_no_coincide(self, db, flujo):
        from app.models.pedido_siesa import PedidoSiesa
        p = PedidoSiesa.query.filter_by(numero_pedido=flujo.pedido).first()
        p.item_descripcion = 'ALGO COMPLETAMENTE DISTINTO'
        db.session.commit()

        r = auditoria.auditar('venta')
        vta02 = next(x for x in r['resultados'] if x['codigo'] == 'VTA-02')
        assert vta02['total'] == 1

    def test_ve_que_se_pickeo_de_mas(self, db, flujo):
        from app.models.picking import TareaPicking
        t = TareaPicking.query.get(flujo.pickings[0])
        t.cantidad_recogida = t.cantidad_solicitada + 5
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-10')['total'] == 1

    def test_ve_un_despacho_sin_bultos(self, db, flujo):
        from app.models.bulto import Bulto
        for b in Bulto.query.filter(Bulto.id.in_(flujo.bultos)).all():
            db.session.delete(b)
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-30')['total'] >= 1

    def test_ve_un_bulto_entregado_sin_ruta(self, db, flujo):
        from app.models.bulto import Bulto
        b = Bulto.query.get(flujo.bultos[0])
        b.ruta_despacho_id = None
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-40')['total'] == 1

    def test_ve_la_contradiccion_entre_parada_y_bultos(self, db, flujo):
        """La que costó el cuarto estado: la parada dice que los bultos
        volvieron y los bultos dicen que no."""
        from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
        r_ = RecaudoEntrega.query.get(flujo.recaudo_id)
        r_.estado_entrega = EstadoEntrega.RECHAZADO
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-50')['total'] == 1

    def test_ve_un_cobro_que_no_llego_a_siesa(self, db, flujo):
        from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
        ruta = RutaDespacho.query.get(flujo.ruta_id)
        ruta.estado_financiero = EstadoFinancieroRuta.LIQUIDADA
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-60')['total'] == 1

    def test_ve_una_parada_de_contado(self, db, flujo, monkeypatch):
        from app.models.packing import TareaPacking
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01')
        t = TareaPacking.query.get(flujo.packing_id)
        t.cond_pago = 'C01'
        db.session.commit()

        r = auditoria.auditar('venta')
        assert next(x for x in r['resultados'] if x['codigo'] == 'VTA-61')['total'] == 1


class TestElEndpointDeAuditoria:
    """El mismo código, la otra fuente de datos."""

    _URL = '/api/auditoria/flujo'

    def test_gestion_puede_auditar(self, client, jwt_token_admin, flujo):
        r = client.get(self._URL,
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        assert d['invariantes_corridos'] >= 10
        assert d['bloqueantes'] == 0

    def test_un_flujo_desconocido_no_devuelve_verde(self, client, jwt_token_admin):
        """Pedir un flujo que no existe tiene que fallar, no correr cero
        invariantes y reportar «todo bien» — que es lo que se leería."""
        r = client.get(self._URL + '?flujo=inexistente',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 400

    def test_sin_permiso_no_pasa(self, client, jwt_token_abastecedor):
        r = client.get(self._URL,
                       headers={'Authorization': f'Bearer {jwt_token_abastecedor}'})
        assert r.status_code == 403

    def test_el_reporte_trae_el_catalogo_completo(self, client, jwt_token_admin, flujo):
        """Todos los invariantes, rotos o no, con su consecuencia escrita.

        Si solo viniera lo roto, «0 hallazgos» no se distinguiría de «no corrió
        nada». Y un invariante sin consecuencia es un hallazgo que nadie sabe
        si atender.
        """
        r = client.get(self._URL,
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        res = r.get_json()['resultados']
        assert len(res) == r.get_json()['invariantes_corridos']
        assert all(x['consecuencia'] and x['frontera'] for x in res)


class TestElBordeEntrePickingYPacking:
    """Los dos invariantes que más hallazgos produjeron en producción, y los
    dos que no tenían detector ciego.

    El primer VTA-20 comparaba `TareaPicking.cantidad_recogida` **en vivo**
    contra lo empacado, y `reabrir_picking` pone esa cantidad en cero
    (`picking_service.py:519`). Un pedido pickeado, empacado y con su picking
    reabierto después salía como «empacado 7 > recogido 3» sin que nadie
    hubiera empacado de más.

    Comparar un valor mutable contra un consumo histórico mide dos momentos
    distintos. Ahora se compara contra el snapshot que el propio packing
    guardó.
    """

    @staticmethod
    def _res(codigo):
        r = auditoria.auditar('venta')
        return next(x for x in r['resultados'] if x['codigo'] == codigo)

    def test_ve_que_se_empaco_mas_de_lo_que_el_picking_entrego(self, db, flujo):
        from app.models.packing import ItemPacking
        it = ItemPacking.query.filter_by(tarea_id=flujo.packing_id).first()
        it.cantidad_real = (it.cantidad_esperada or 0) + 5
        db.session.commit()
        assert self._res('VTA-20')['total'] == 1

    def test_reabrir_el_picking_NO_dispara_el_bloqueante(self, db, flujo):
        """La regresión que este cambio corrige. Se reproduce el escenario
        real: el picking se reabre después de que el packing ya consumió su
        cantidad."""
        from app.models.picking import TareaPicking
        for t in TareaPicking.query.filter(TareaPicking.id.in_(flujo.pickings)).all():
            t.cantidad_recogida = 0        # lo que hace `reabrir_picking`
        db.session.commit()
        assert self._res('VTA-20')['total'] == 0, (
            'una reapertura legítima volvió a contarse como empaque de más')

    def test_pero_sí_lo_declara_como_aviso(self, db, flujo):
        """No se esconde: baja de severidad. Si reabrir pickings de pedidos ya
        empacados fuera frecuente, eso hay que verlo."""
        from app.models.picking import TareaPicking
        for t in TareaPicking.query.filter(TareaPicking.id.in_(flujo.pickings)).all():
            t.cantidad_recogida = 0
        db.session.commit()
        r = self._res('VTA-22')
        assert r['total'] >= 1
        assert r['severidad'] == auditoria.AVISA

    def test_un_packing_sin_picking_avisa_pero_no_bloquea(self, db, flujo):
        """`crear_manual` existe y no deja rastro: un packing manual legítimo
        es hoy indistinguible de un picking perdido. No se bloquea sobre una
        pregunta que el modelo no sabe responder."""
        from app.models.picking import TareaPicking
        for t in TareaPicking.query.filter(TareaPicking.id.in_(flujo.pickings)).all():
            t.referencia_documento = 'OTRO-PEDIDO'
        db.session.commit()
        r = self._res('VTA-21')
        assert r['total'] >= 1
        assert r['severidad'] == auditoria.AVISA

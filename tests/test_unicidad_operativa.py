"""
Los cuatro check-then-insert que no tenían índice detrás.

Cuatro servicios comprueban en Python que una fila no exista y después
insertan, sin bloqueo. Entre el `.first()` y el `add()` no hay nada:

    recaudos_entrega       «un recaudo por parada»
    tareas_packing         «Ya existe una tarea de packing para el pedido {n}»
    recepciones            «Ya existe una recepción para la OC {n}»
    devoluciones_cliente   «no se duplica»

## Y en tres de los cuatro no hace falta concurrencia exótica

`recaudos_entrega` tiene **dos escritores**: `confirmar_parada` y
`forzar_cierre_ruta`. `devoluciones_cliente` también —
`crear_devoluciones_pendientes_ruta` se llama desde `liquidar_ruta` y desde
`forzar_cierre_ruta`—. Y el endpoint de recepción se declara idempotente
apoyado en un `.first()`.

## El precedente que ya existía

`f4b84ad06843_add_sesion_conteo_unique_idx.py` hizo exactamente esto para
`sesiones_conteo`: *«Prevents race condition where API + scheduler create
duplicate CC1 sessions»*. Las cuatro tablas de acá mueven plata, inventario y
documentos fiscales, y se habían quedado sin él.

## Los índices son PARCIALES y copian el filtro de su servicio

Un índice total prohibiría el historial legítimo. Cada test de abajo tiene su
pareja: uno que exige que el duplicado choque, y otro que exige que lo
histórico siga cabiendo. El segundo es el que impide que este archivo rompa la
operación sin que nadie lo note hasta que alguien no pueda trabajar.
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def ruta(db):
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario
    u = Usuario.query.filter_by(email='uniq@test.com').first()
    if not u:
        u = Usuario(email='uniq@test.com', nombre='C', rol='conductor',
                    activo=True)
        u.set_password('t')
        db.session.add(u)
        db.session.flush()
    r = RutaDespacho(conductor_id=u.id, tipo_ruta='Urbana', estado='ENTREGADA')
    db.session.add(r)
    db.session.commit()
    return r


def _tarea(db, almacen, pedido=None, estado='DESPACHADO'):
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:8]}', estado=estado,
                     almacen_id=almacen.id,
                     numero_pedido_siesa=pedido or f'PD-{uuid.uuid4().hex[:6]}',
                     tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa='1')
    db.session.add(t)
    db.session.commit()
    return t


class TestUnRecaudoPorParada:
    def test_dos_para_la_misma_parada_chocan(self, db, almacen, ruta):
        """**El detector ciego.** Con dos filas, `total_recaudado()` suma las
        dos, la liquidación emite dos RC, y el congelamiento del monto se
        decide con un `.first()` sin `order_by`."""
        from app.models.recaudo_entrega import RecaudoEntrega
        t = _tarea(db, almacen)
        db.session.add(RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id,
                                      estado_entrega='ENTREGADO',
                                      forma_pago='EFECTIVO', monto_cobrado=100))
        db.session.commit()
        db.session.add(RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id,
                                      estado_entrega='RECHAZADO',
                                      monto_cobrado=0))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_paradas_distintas_de_la_misma_ruta_conviven(self, db, almacen, ruta):
        """Una ruta tiene muchas paradas. Es el caso normal."""
        from app.models.recaudo_entrega import RecaudoEntrega
        for _ in range(3):
            t = _tarea(db, almacen)
            db.session.add(RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id,
                                          estado_entrega='ENTREGADO',
                                          forma_pago='EFECTIVO',
                                          monto_cobrado=100))
        db.session.commit()


class TestUnaTareaActivaPorPedido:
    def test_dos_activas_del_mismo_pedido_chocan(self, db, almacen):
        _tarea(db, almacen, pedido='PD-DUP')
        with pytest.raises(IntegrityError):
            _tarea(db, almacen, pedido='PD-DUP')
        db.session.rollback()

    def test_una_cancelada_deja_rehacer_el_pedido(self, db, almacen):
        """El caso legítimo que un índice total habría prohibido: se cancela
        la tarea y se rehace el mismo pedido."""
        _tarea(db, almacen, pedido='PD-REH', estado='CANCELADO')
        _tarea(db, almacen, pedido='PD-REH')   # no debe levantar

    def test_dos_traslados_sin_pedido_conviven(self, db, almacen):
        """`numero_pedido_siesa` es NULL en las tareas de TRASLADO. Si el
        índice no las excluyera, **una sola tarea de traslado podría existir
        en todo el sistema**."""
        from app.models.packing import TareaPacking
        for _ in range(3):
            db.session.add(TareaPacking(
                codigo=f'PK-{uuid.uuid4().hex[:8]}', estado='PENDIENTE',
                tipo_documento='TRASLADO', almacen_id=almacen.id,
                numero_pedido_siesa=None))
        db.session.commit()


class TestUnaRecepcionActivaPorOC:
    def _rec(self, db, almacen, oc, estado='ABIERTA', co='003'):
        from app.models.recepcion import RecepcionMercancia
        r = RecepcionMercancia(codigo=f'REC-{uuid.uuid4().hex[:8]}',
                               numero_oc_siesa=oc, co_oc_siesa=co,
                               estado=estado, almacen_id=almacen.id)
        db.session.add(r)
        db.session.commit()
        return r

    def test_dos_activas_de_la_misma_oc_chocan(self, db, almacen):
        self._rec(db, almacen, 'OC-991')
        with pytest.raises(IntegrityError):
            self._rec(db, almacen, 'OC-991')
        db.session.rollback()

    def test_una_cancelada_deja_reabrir(self, db, almacen):
        self._rec(db, almacen, 'OC-992', estado='CANCELADA')
        self._rec(db, almacen, 'OC-992')   # no debe levantar

    def test_la_misma_oc_en_otro_CO_convive(self, db, almacen):
        """El filtro del servicio incluye `co_oc_siesa` cuando viene: dos CO
        distintos son dos recepciones distintas."""
        self._rec(db, almacen, 'OC-993', co='003')
        self._rec(db, almacen, 'OC-993', co='001')   # no debe levantar


class TestUnaDevolucionActivaPorRecaudo:
    def _dev(self, db, almacen, recaudo_id, estado='ABIERTA'):
        from app.models.devolucion_cliente import DevolucionCliente
        t = _tarea(db, almacen)
        d = DevolucionCliente(codigo=f'DEVC-{uuid.uuid4().hex[:8]}',
                              tarea_packing_id=t.id,
                              numero_pedido_siesa=t.numero_pedido_siesa,
                              tipo_docto_fe='FEW', consec_fe='1',
                              almacen_id=almacen.id, estado=estado,
                              recaudo_entrega_id=recaudo_id)
        db.session.add(d)
        db.session.commit()
        return d

    def _recaudo(self, db, almacen, ruta):
        from app.models.recaudo_entrega import RecaudoEntrega
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=_tarea(db, almacen).id,
                           estado_entrega='PARCIAL', forma_pago='EFECTIVO',
                           monto_cobrado=10)
        db.session.add(r)
        db.session.commit()
        return r

    def test_dos_activas_del_mismo_recaudo_chocan(self, db, almacen, ruta):
        """Dos filas → la recepcionista confirma las dos → dos notas crédito
        251126 sobre la misma factura, con cruce de cartera automático."""
        rec = self._recaudo(db, almacen, ruta)
        self._dev(db, almacen, rec.id)
        with pytest.raises(IntegrityError):
            self._dev(db, almacen, rec.id)
        db.session.rollback()

    def test_una_cancelada_deja_rehacer(self, db, almacen, ruta):
        rec = self._recaudo(db, almacen, ruta)
        self._dev(db, almacen, rec.id, estado='CANCELADA')
        self._dev(db, almacen, rec.id)   # no debe levantar

    def test_varias_devoluciones_SIN_recaudo_conviven(self, db, almacen):
        """Las devoluciones que no vienen de una ruta llevan
        `recaudo_entrega_id` NULL. Si los NULL colisionaran, **solo podría
        existir una devolución directa en todo el sistema**."""
        for _ in range(3):
            self._dev(db, almacen, None)


class TestLosIndicesEstanDeclarados:
    @pytest.mark.parametrize('modulo,clase,indice', [
        ('app.models.recaudo_entrega', 'RecaudoEntrega', 'uq_recaudo_por_parada'),
        ('app.models.packing', 'TareaPacking', 'uq_packing_pedido_activo'),
        ('app.models.recepcion', 'RecepcionMercancia', 'uq_recepcion_oc_activa'),
        ('app.models.devolucion_cliente', 'DevolucionCliente',
         'uq_devolucion_por_recaudo'),
    ])
    def test_existe_y_es_unico(self, modulo, clase, indice):
        import importlib
        M = getattr(importlib.import_module(modulo), clase)
        idx = {i.name: i for i in M.__table__.indexes}
        assert indice in idx, (
            f'{M.__tablename__} volvió a quedarse sin respaldo: el '
            f'check-then-insert de su servicio no lo sostiene nadie')
        assert idx[indice].unique

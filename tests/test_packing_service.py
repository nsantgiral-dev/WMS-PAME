"""
Tests de PackingService + PedidoPackingCloser — el flujo de empaque y despacho.
Si packing falla, el pedido no se factura en Siesa.
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def packing_setup(db, almacen, producto, ub_picking, usuario):
    """Setup completo para tests de packing: almacen + producto + ubicacion + usuario."""
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'usuario': usuario,
    }


def _crear_tarea_packing(db, almacen, producto, estado='PENDIENTE',
                         numero_pedido='PED-TEST-001',
                         tipo_docto='PD', consec_docto='1307',
                         verificado_items=False, cantidad=10,
                         siesa_triggered=False,
                         pedido_anulado=False):
    """Helper: crea TareaPacking + ItemPacking con datos realistas."""
    from app.models.packing import TareaPacking, ItemPacking
    tarea = TareaPacking(
        codigo=f'PACK-TEST-{numero_pedido}',
        almacen_id=almacen.id,
        estado=estado,
        tipo_docto_pedido_siesa=tipo_docto,
        consec_docto_pedido_siesa=consec_docto,
        numero_pedido_siesa=numero_pedido,
        tipo_documento='PEDIDO',
        siesa_triggered=siesa_triggered,
        pedido_anulado_siesa=pedido_anulado,
    )
    db.session.add(tarea)
    db.session.flush()

    item = ItemPacking(
        tarea_id=tarea.id,
        producto_id=producto.id,
        cantidad_esperada=cantidad,
        cantidad_real=cantidad if verificado_items else 0,
        verificado=verificado_items,
    )
    db.session.add(item)
    db.session.commit()
    return tarea


# ═══════════════════════════════════════════════════════════════════
# 1. Crear tarea de packing desde picking
# ═══════════════════════════════════════════════════════════════════

class TestCrearTareaPacking:

    def test_crear_tarea_packing(self, app, db, packing_setup):
        """crear_desde_picking genera TareaPacking + ItemPacking agrupados."""
        s = packing_setup
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService

        # Simular tarea de picking completada
        tp = TareaPicking(
            codigo='PIK-001',
            producto_id=s['producto'].id,
            cantidad_solicitada=10,
            cantidad_recogida=10,
            ubicacion_id=s['ubicacion'].id,
            almacen_id=s['almacen'].id,
            estado='COMPLETADO',
        )
        db.session.add(tp)
        db.session.commit()

        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=[tp.id],
            numero_pedido_siesa='PED-001',
            almacen_id=s['almacen'].id,
            tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='1307',
        )

        assert tarea is not None
        assert tarea.estado == 'PENDIENTE'
        assert tarea.numero_pedido_siesa == 'PED-001'
        assert tarea.codigo.startswith('PACK-')
        assert len(tarea.items) == 1
        assert tarea.items[0].producto_id == s['producto'].id
        assert tarea.items[0].cantidad_esperada == 10
        assert tarea.items[0].verificado is False

    def test_crear_duplicado_rechaza(self, app, db, packing_setup):
        """No se permite crear dos packings para el mismo pedido."""
        s = packing_setup
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService

        tp = TareaPicking(
            codigo='PIK-DUP-001',
            producto_id=s['producto'].id,
            cantidad_solicitada=5,
            cantidad_recogida=5,
            ubicacion_id=s['ubicacion'].id,
            almacen_id=s['almacen'].id,
            estado='COMPLETADO',
        )
        db.session.add(tp)
        db.session.commit()

        PackingService.crear_desde_picking(
            tareas_picking_ids=[tp.id],
            numero_pedido_siesa='PED-DUP',
            almacen_id=s['almacen'].id,
        )

        tp2 = TareaPicking(
            codigo='PIK-DUP-002',
            producto_id=s['producto'].id,
            cantidad_solicitada=5,
            cantidad_recogida=5,
            ubicacion_id=s['ubicacion'].id,
            almacen_id=s['almacen'].id,
            estado='COMPLETADO',
        )
        db.session.add(tp2)
        db.session.commit()

        with pytest.raises(ValueError, match='Ya existe una tarea de packing'):
            PackingService.crear_desde_picking(
                tareas_picking_ids=[tp2.id],
                numero_pedido_siesa='PED-DUP',
                almacen_id=s['almacen'].id,
            )


# ═══════════════════════════════════════════════════════════════════
# 2. Cerrar packing verificado crea SiesaJob DESPACHO_F470
# ═══════════════════════════════════════════════════════════════════

class TestCerrarPackingCreaJobSiesa:

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    @patch('app.services.closing.pedido_closer.connekta')
    def test_cerrar_packing_crea_job_siesa(self, mock_connekta, mock_dlq,
                                           app, db, packing_setup):
        """Cerrar un packing VERIFICADO encola SiesaJob tipo DESPACHO_F470."""
        s = packing_setup
        from app.models.siesa_job import SiesaJob
        from app.models.pedido_siesa import PedidoSiesa

        tarea = _crear_tarea_packing(
            db, s['almacen'], s['producto'],
            estado='VERIFICADO',
            verificado_items=True,
            numero_pedido='PED-CIERRE-001',
        )

        # PedidoSiesa para el lookup de item_id_siesa
        ps = PedidoSiesa(
            tipo_docto='PD', consec_docto=1307,
            centro_op='003', bodega='NB1',
            numero_pedido='PED-CIERRE-001',
            item_codigo=s['producto'].codigo_siesa,
            item_id_siesa='ITEM-001',
        )
        db.session.add(ps)
        db.session.commit()

        # Mock connekta precheck — no bloquear
        mock_connekta.get_estado_pedido.return_value = 3  # Comprometido
        mock_connekta.get_factura_desde_pedido.return_value = []  # Sin factura previa

        from app.services.closing.pedido_closer import PedidoPackingCloser
        closer = PedidoPackingCloser()
        resultado = closer.ejecutar_cierre(
            tarea_id=tarea.id,
            bultos_data=[{'tipo': 'Caja', 'cantidad': 1}],
            usuario_id=s['usuario'].id,
        )

        assert resultado.exitoso is True

        # Verificar que se creó el SiesaJob
        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_F470',
            referencia_tipo='TareaPacking',
            referencia_id=tarea.id,
        ).first()
        assert job is not None
        assert job.estado == 'PENDIENTE'

        # Verificar que la tarea cambió a DESPACHADO
        db.session.refresh(tarea)
        assert tarea.estado == 'DESPACHADO'

        # DLQ fue disparado
        mock_dlq.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# 3. Idempotencia — siesa_triggered=True bloquea segundo cierre
# ═══════════════════════════════════════════════════════════════════

class TestCerrarPackingIdempotente:

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    @patch('app.services.closing.pedido_closer.connekta')
    def test_cerrar_packing_idempotente(self, mock_connekta, mock_dlq,
                                        app, db, packing_setup):
        """Si siesa_triggered=True, un segundo cierre es rechazado."""
        s = packing_setup

        tarea = _crear_tarea_packing(
            db, s['almacen'], s['producto'],
            estado='DESPACHADO',
            verificado_items=True,
            numero_pedido='PED-IDEMP-001',
            siesa_triggered=True,
        )

        mock_connekta.get_estado_pedido.return_value = 3
        mock_connekta.get_factura_desde_pedido.return_value = []

        from app.services.closing.pedido_closer import PedidoPackingCloser
        closer = PedidoPackingCloser()
        resultado = closer.ejecutar_cierre(
            tarea_id=tarea.id,
            bultos_data=[{'tipo': 'Caja', 'cantidad': 1}],
            usuario_id=s['usuario'].id,
        )

        assert resultado.exitoso is False
        assert 'ya procesó' in resultado.error.lower()

        # No se creó un job nuevo
        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(
            referencia_tipo='TareaPacking',
            referencia_id=tarea.id,
        ).all()
        assert len(jobs) == 0


# ═══════════════════════════════════════════════════════════════════
# 4. Cerrar sin verificar rechaza
# ═══════════════════════════════════════════════════════════════════

class TestCerrarSinVerificarRechaza:

    @patch('app.services.closing.pedido_closer.connekta')
    def test_cerrar_sin_verificar_rechaza(self, mock_connekta,
                                          app, db, packing_setup):
        """Un packing en estado PENDIENTE/EN_PROCESO no puede cerrarse."""
        s = packing_setup

        for estado in ['PENDIENTE', 'EN_PROCESO']:
            tarea = _crear_tarea_packing(
                db, s['almacen'], s['producto'],
                estado=estado,
                verificado_items=False,
                numero_pedido=f'PED-NOVERIF-{estado}',
            )

            mock_connekta.get_estado_pedido.return_value = 3
            mock_connekta.get_factura_desde_pedido.return_value = []

            from app.services.closing.pedido_closer import PedidoPackingCloser
            closer = PedidoPackingCloser()
            resultado = closer.ejecutar_cierre(
                tarea_id=tarea.id,
                bultos_data=[{'tipo': 'Caja', 'cantidad': 1}],
                usuario_id=s['usuario'].id,
            )

            assert resultado.exitoso is False
            assert 'VERIFICADO' in resultado.error


# ═══════════════════════════════════════════════════════════════════
# 5. Pedido anulado en Siesa bloquea cierre
# ═══════════════════════════════════════════════════════════════════

class TestPackingPedidoAnulado:

    def test_packing_pedido_anulado_bloquea_iniciar(self, app, db, packing_setup):
        """pedido_anulado_siesa=True impide iniciar el packing."""
        s = packing_setup
        from app.services.packing_service import PackingService

        tarea = _crear_tarea_packing(
            db, s['almacen'], s['producto'],
            estado='PENDIENTE',
            numero_pedido='PED-ANULADO-001',
            pedido_anulado=True,
        )

        with pytest.raises(ValueError, match='[Aa]nulado'):
            PackingService.iniciar(tarea.id, empacador_id=s['usuario'].id)

    def test_packing_pedido_anulado_bloquea_confirmar(self, app, db, packing_setup):
        """pedido_anulado_siesa=True impide confirmar el packing."""
        s = packing_setup
        from app.services.packing_service import PackingService

        tarea = _crear_tarea_packing(
            db, s['almacen'], s['producto'],
            estado='EN_PROCESO',
            verificado_items=True,
            numero_pedido='PED-ANULADO-002',
            pedido_anulado=True,
        )

        with pytest.raises(ValueError, match='[Aa]nulado'):
            PackingService.confirmar_packing(tarea.id)

    @patch('app.services.closing.pedido_closer.connekta')
    def test_packing_pedido_anulado_precheck_bloquea_cierre(
            self, mock_connekta, app, db, packing_setup):
        """Si Siesa reporta estado anulado (5/9/-1), precheck bloquea el cierre."""
        s = packing_setup

        tarea = _crear_tarea_packing(
            db, s['almacen'], s['producto'],
            estado='VERIFICADO',
            verificado_items=True,
            numero_pedido='PED-ANULADO-003',
        )

        # Siesa reporta pedido anulado (estado 5)
        mock_connekta.get_estado_pedido.return_value = 5
        mock_connekta.get_factura_desde_pedido.return_value = []

        from app.services.closing.pedido_closer import PedidoPackingCloser
        closer = PedidoPackingCloser()
        resultado = closer.ejecutar_cierre(
            tarea_id=tarea.id,
            bultos_data=[{'tipo': 'Caja', 'cantidad': 1}],
            usuario_id=s['usuario'].id,
        )

        assert resultado.exitoso is False
        assert 'anulado' in resultado.error.lower()

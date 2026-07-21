"""
Tests de RecepcionService — flujo completo de recepción de mercancía.
PENDIENTE (ABIERTA) → EN_PROCESO → CONFIRMADA con stock increment + SiesaJob ENTRADA_OC.
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def recepcion_setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    """Setup completo: almacen + producto + ubicacion + inventario + recepcionista."""
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'inventario': inv_picking,
        'usuario': usuario,
    }


def _crear_recepcion(db, almacen_id, producto_id):
    """Helper: crea una recepcion ABIERTA con un item de 10 unidades."""
    from app.services.recepcion_service import RecepcionService
    return RecepcionService.crear_recepcion(
        numero_oc_siesa='OC-TEST-001',
        almacen_id=almacen_id,
        proveedor_codigo='PROV-001',
        proveedor_nombre='Proveedor Test',
        co_oc_siesa='003',
        tipo_docto_oc_siesa='OCN',
        consec_docto_oc_siesa='12345',
        items=[{
            'producto_id': producto_id,
            'cantidad_ordenada': 10,
            'tolerancia_exceso_pct': 10.0,
        }],
    )


# ═══════════════════════════════════════════════════════════════════
# 1. Crear recepcion
# ═══════════════════════════════════════════════════════════════════

class TestCrearRecepcion:

    def test_crear_recepcion_genera_registro(self, app, db, recepcion_setup):
        s = recepcion_setup
        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)

        assert rec.id is not None
        assert rec.estado == 'ABIERTA'
        assert rec.numero_oc_siesa == 'OC-TEST-001'
        assert rec.codigo.startswith('REC-')
        assert len(rec.items) == 1
        assert rec.items[0].cantidad_ordenada == 10
        assert rec.items[0].cantidad_recibida == 0

    def test_crear_recepcion_duplicada_rechaza(self, app, db, recepcion_setup):
        s = recepcion_setup
        _crear_recepcion(db, s['almacen'].id, s['producto'].id)

        with pytest.raises(ValueError, match='Ya existe una recepción'):
            _crear_recepcion(db, s['almacen'].id, s['producto'].id)


# ═══════════════════════════════════════════════════════════════════
# 2. Escaneo de productos (recepcion ciega)
# ═══════════════════════════════════════════════════════════════════

class TestEscanearProducto:

    def test_escanear_incrementa_cantidad_recibida(self, app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService
        from app.models.recepcion import ItemRecepcion

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)

        resultado = RecepcionService.escanear_producto(
            recepcion_id=rec.id,
            producto_id=s['producto'].id,
            cantidad=3,
        )

        item = ItemRecepcion.query.filter_by(
            recepcion_id=rec.id, producto_id=s['producto'].id
        ).first()
        assert item.cantidad_recibida == 3
        assert resultado['item']['cantidad_recibida'] == 3

    def test_escanear_multiples_acumula(self, app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService
        from app.models.recepcion import ItemRecepcion

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)

        RecepcionService.escanear_producto(rec.id, s['producto'].id, 3)
        RecepcionService.escanear_producto(rec.id, s['producto'].id, 4)

        item = ItemRecepcion.query.filter_by(
            recepcion_id=rec.id, producto_id=s['producto'].id
        ).first()
        assert item.cantidad_recibida == 7

    def test_escanear_exceso_bloqueado(self, app, db, recepcion_setup):
        """Tolerancia 10% sobre 10 unidades = max 11. Intentar 12 debe fallar."""
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)

        with pytest.raises(ValueError):
            RecepcionService.escanear_producto(rec.id, s['producto'].id, 12)


# ═══════════════════════════════════════════════════════════════════
# 3. Confirmar recepcion — stock increment + SiesaJob
# ═══════════════════════════════════════════════════════════════════

class TestConfirmarRecepcion:

    @patch('app.services.recepcion_service.connekta')
    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    def test_confirmar_incrementa_stock(self, mock_dlq, mock_connekta,
                                         app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService
        from app.models.inventario import UbicacionProducto

        mock_connekta.get_ordenes_compra_aprobadas.return_value = {
            'detalle': {'Table': [{
                'f200_nit_prov': 'PROV-001',
                'f202_id_sucursal_prov': '001',
                'f120_referencia': 'PROD-001',
                'f150_id': 'NB1',
                'f421_id_unidad_medida': 'UND',
                'f421_fecha_entrega': '20260717',
                'f421_id_motivo': '01',
                'f120_id': '',
                'f420_id_co': '003',
                'f420_id_moneda_docto': 'COP',
                'f420_id_moneda_conv': 'COP',
                'f420_id_moneda_local': 'COP',
                'f420_tasa_conv': '1',
                'f420_tasa_local': '1',
                'f200_nit_comprador': '',
            }]}
        }
        mock_connekta.centro_op = '003'

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)
        RecepcionService.escanear_producto(rec.id, s['producto'].id, 10)

        stock_antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad

        RecepcionService.confirmar_recepcion(rec.id)

        stock_despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad
        assert stock_despues == stock_antes + 10

        db.session.refresh(rec)
        assert rec.estado == 'CONFIRMADA'
        assert rec.fecha_confirmacion is not None

    @patch('app.services.recepcion_service.connekta')
    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    def test_confirmar_crea_siesa_job_entrada_oc(self, mock_dlq, mock_connekta,
                                                   app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService
        from app.models.siesa_job import SiesaJob

        mock_connekta.get_ordenes_compra_aprobadas.return_value = {
            'detalle': {'Table': [{
                'f200_nit_prov': 'PROV-001',
                'f202_id_sucursal_prov': '001',
                'f120_referencia': 'PROD-001',
                'f150_id': 'NB1',
                'f421_id_unidad_medida': 'UND',
                'f421_fecha_entrega': '20260717',
                'f421_id_motivo': '01',
                'f120_id': '',
                'f420_id_co': '003',
                'f420_id_moneda_docto': 'COP',
                'f420_id_moneda_conv': 'COP',
                'f420_id_moneda_local': 'COP',
                'f420_tasa_conv': '1',
                'f420_tasa_local': '1',
                'f200_nit_comprador': '',
            }]}
        }
        mock_connekta.centro_op = '003'

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)
        RecepcionService.escanear_producto(rec.id, s['producto'].id, 10)
        RecepcionService.confirmar_recepcion(rec.id)

        job = SiesaJob.query.filter_by(
            referencia_tipo='RecepcionMercancia',
            referencia_id=rec.id,
        ).first()
        assert job is not None
        assert job.tipo == 'ENTRADA_OC'
        assert job.estado == 'PENDIENTE'

        import json
        payload = json.loads(job.payload)
        assert payload['numero_oc_siesa'] == 'OC-TEST-001'
        assert len(payload['items']) == 1
        assert payload['items'][0]['cantidad_recibida'] == 10

        mock_dlq.assert_called_once()

    @patch('app.services.recepcion_service.connekta')
    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    def test_confirmar_doble_no_duplica_stock(self, mock_dlq, mock_connekta,
                                               app, db, recepcion_setup):
        """Idempotencia: confirmar dos veces no incrementa stock dos veces."""
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService
        from app.models.inventario import UbicacionProducto

        mock_connekta.get_ordenes_compra_aprobadas.return_value = {
            'detalle': {'Table': [{
                'f200_nit_prov': 'PROV-001',
                'f202_id_sucursal_prov': '001',
                'f120_referencia': 'PROD-001',
                'f150_id': 'NB1',
                'f421_id_unidad_medida': 'UND',
                'f421_fecha_entrega': '20260717',
                'f421_id_motivo': '01',
                'f120_id': '',
                'f420_id_co': '003',
                'f420_id_moneda_docto': 'COP',
                'f420_id_moneda_conv': 'COP',
                'f420_id_moneda_local': 'COP',
                'f420_tasa_conv': '1',
                'f420_tasa_local': '1',
                'f200_nit_comprador': '',
            }]}
        }
        mock_connekta.centro_op = '003'

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)
        RecepcionService.escanear_producto(rec.id, s['producto'].id, 10)

        RecepcionService.confirmar_recepcion(rec.id)

        stock_after_first = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad

        # Marcar siesa_triggered para simular normal completion path
        rec.siesa_triggered = True
        db.session.commit()

        # Segunda confirmacion — debe retornar sin efecto
        result = RecepcionService.confirmar_recepcion(rec.id)
        assert result.estado == 'CONFIRMADA'

        stock_after_second = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad
        assert stock_after_second == stock_after_first


# ═══════════════════════════════════════════════════════════════════
# 4. Flujo de estados
# ═══════════════════════════════════════════════════════════════════

class TestFlujoEstados:

    def test_iniciar_transiciona_a_en_proceso(self, app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        assert rec.estado == 'ABIERTA'

        RecepcionService.iniciar(rec.id, s['usuario'].id)
        db.session.refresh(rec)

        assert rec.estado == 'EN_PROCESO'
        assert rec.recepcionista_id == s['usuario'].id
        assert rec.fecha_inicio is not None

    def test_iniciar_desde_en_proceso_rechaza(self, app, db, recepcion_setup):
        s = recepcion_setup
        from app.services.recepcion_service import RecepcionService

        rec = _crear_recepcion(db, s['almacen'].id, s['producto'].id)
        RecepcionService.iniciar(rec.id, s['usuario'].id)

        with pytest.raises(ValueError, match='No se puede iniciar'):
            RecepcionService.iniciar(rec.id, s['usuario'].id)

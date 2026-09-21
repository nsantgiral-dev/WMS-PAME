"""Auditoría de picking: el resultado ENCONTRADO no ajusta nada, dispara un conteo
cíclico forzado del SKU — el conteo es el único que ajusta Siesa. Y las tareas de
backorder de Siesa no tocan stock ni packing al auditarse."""
from unittest.mock import patch

import pytest


@pytest.fixture
def tarea_bloqueada(db, almacen, producto, ub_picking, inv_picking):
    from app.models.picking import TareaPicking, EstadoPicking

    inv_picking.cantidad = 5
    inv_picking.reservado = 0
    inv_picking.bloqueado = 2
    t = TareaPicking(
        codigo='PICK-AUD-1', producto_id=producto.id, cantidad_solicitada=2,
        cantidad_recogida=0, ubicacion_id=ub_picking.id, almacen_id=almacen.id,
        estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='FALTANTE',
        referencia_documento='PD-AUD',
    )
    db.session.add(t)
    db.session.commit()
    return t


def _auditar(client, jwt, tarea_id, **body):
    return client.post(
        f'/api/picking/{tarea_id}/auditar', json=body,
        headers={'Authorization': f'Bearer {jwt}'})


class TestEncontradoNoAjustaYForzaConteo:

    def test_no_toca_inventario_ni_siesa_ni_packing(
        self, app, db, usuario_admin, almacen, producto, tarea_bloqueada, inv_picking,
    ):
        from app.models.packing import ItemPacking
        from app.models.picking import EstadoPicking
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService

        packing = PackingService.crear_manual(
            numero_pedido_siesa='PD-AUD', almacen_id=almacen.id,
            items=[{'producto_id': producto.id, 'cantidad': 2}])

        with patch('app.services.conteo_service.ConteoService.'
                   'ajustar_desde_auditoria_picking') as ajuste_directo:
            t = PickingService.auditar_tarea(
                tarea_bloqueada.id, admin_id=usuario_admin.id,
                resultado='ENCONTRADO', cantidad_hallada=10)

        ajuste_directo.assert_not_called()
        assert t.estado == EstadoPicking.CANCELADO
        assert t.auditoria_resultado == 'ENCONTRADO'
        assert t.auditoria_cantidad_hallada == 10
        db.session.refresh(inv_picking)
        assert inv_picking.cantidad == 5            # el WMS no se ajusta
        assert inv_picking.bloqueado == 0           # pero sí se descongela
        item = ItemPacking.query.filter_by(tarea_id=packing.id).one()
        assert item.cantidad_esperada == 2          # el packing no cambia

    def test_la_ruta_genera_el_conteo_forzado(
        self, app, db, client, jwt_token_admin, tarea_bloqueada, producto,
    ):
        from app.models.conteo import SesionConteo

        r = _auditar(client, jwt_token_admin, tarea_bloqueada.id,
                     resultado='ENCONTRADO', cantidad_hallada=10)

        assert r.status_code == 200, r.get_json()
        conteo = r.get_json()['conteo_forzado']
        assert conteo['ok'] is True
        assert conteo['tareas_creadas'] == 1
        sesion = SesionConteo.query.filter_by(producto_id=producto.id).one()
        assert sesion.tipo == 'MANUAL' and sesion.codigo == conteo['codigos'][0]

    def test_se_puede_desmarcar_el_conteo(
        self, app, db, client, jwt_token_admin, tarea_bloqueada, producto,
    ):
        from app.models.conteo import SesionConteo

        r = _auditar(client, jwt_token_admin, tarea_bloqueada.id,
                     resultado='ENCONTRADO', forzar_conteo=False)

        assert r.status_code == 200
        assert r.get_json()['conteo_forzado'] is None
        assert SesionConteo.query.filter_by(producto_id=producto.id).count() == 0

    def test_otros_resultados_no_generan_conteo(
        self, app, db, client, jwt_token_admin, tarea_bloqueada,
    ):
        r = _auditar(client, jwt_token_admin, tarea_bloqueada.id,
                     resultado='NO_ENCONTRADO')
        assert r.status_code == 200
        assert r.get_json()['conteo_forzado'] is None

    def test_si_el_conteo_falla_la_auditoria_igual_queda_cerrada(
        self, app, db, client, jwt_token_admin, tarea_bloqueada,
    ):
        from app.models.picking import TareaPicking, EstadoPicking

        with patch('app.services.conteo_service.ConteoService.crear_conteo_manual',
                   side_effect=ValueError('El producto no tiene stock registrado en este almacén')):
            r = _auditar(client, jwt_token_admin, tarea_bloqueada.id,
                         resultado='ENCONTRADO')

        assert r.status_code == 200
        conteo = r.get_json()['conteo_forzado']
        assert conteo['ok'] is False and 'stock' in conteo['error']
        assert TareaPicking.query.get(tarea_bloqueada.id).estado == EstadoPicking.CANCELADO


class TestBackorderDeSiesaNoTocaStockNiPacking:

    def _backorder(self, db, almacen, producto, ub_picking, inv_picking):
        from app.models.picking import TareaPicking, EstadoPicking
        from app.services.packing_service import PackingService

        inv_picking.cantidad = 4
        inv_picking.reservado = 2      # la pickeable, todavía sin recoger
        inv_picking.bloqueado = 2      # la del backorder (con stock reservado)
        db.session.add(TareaPicking(
            codigo='PICK-BO-A', producto_id=producto.id, cantidad_solicitada=2,
            cantidad_recogida=0, ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            estado=EstadoPicking.PENDIENTE, referencia_documento='PD-BO'))
        bo = TareaPicking(
            codigo='PICK-BO-B', producto_id=producto.id, cantidad_solicitada=2,
            cantidad_recogida=0, ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='BACKORDER_SIESA',
            referencia_documento='PD-BO')
        db.session.add(bo)
        db.session.commit()
        packing = PackingService.crear_manual(
            numero_pedido_siesa='PD-BO', almacen_id=almacen.id,
            items=[{'producto_id': producto.id, 'cantidad': 2}])
        return bo, packing

    def test_no_encontrado_no_pone_la_ubicacion_en_cero_ni_borra_el_item(
        self, app, db, almacen, producto, ub_picking, inv_picking, usuario_admin,
    ):
        """Auditar el backorder ANTES de que la hermana se recoja no puede borrar
        el ítem del packing (total 0) ni vaciar la ubicación."""
        from app.models.packing import ItemPacking
        from app.services.picking_service import PickingService

        bo, packing = self._backorder(db, almacen, producto, ub_picking, inv_picking)

        PickingService.auditar_tarea(
            bo.id, admin_id=usuario_admin.id, resultado='NO_ENCONTRADO')

        db.session.refresh(inv_picking)
        assert inv_picking.cantidad == 4
        assert inv_picking.bloqueado == 0
        assert ItemPacking.query.filter_by(tarea_id=packing.id).one().cantidad_esperada == 2

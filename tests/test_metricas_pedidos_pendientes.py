"""
Métrica 3 — Pedidos dejados de despachar (fill rate + motivo reclasificado).

Casos borde pedidos explícitamente en Fase 5: día sin operación, pedidos
parcialmente despachados, motivo sin tarea de picking asociada.
"""
import pytest

from app.extensions import db
from app.models.pedido_siesa import PedidoSiesa
from app.models.picking import TareaPicking
from app.services.metricas.pedidos_pendientes import (
    calcular_pedidos_pendientes, listar_pedidos_pendientes_detalle, mapear_motivo,
)

_consec = iter(range(1, 10_000))


def _pedido(numero_pedido, bodega='NB1', fecha_entrega='2026-08-10T00:00:00',
            cantidad_pedida=10, cantidad_pendiente=10, estado_siesa=3,
            producto_id=None, item_codigo=None, cliente='Cliente X'):
    p = PedidoSiesa(
        tipo_docto='PD', consec_docto=next(_consec), centro_op='003', bodega=bodega,
        numero_pedido=numero_pedido, item_codigo=item_codigo or f'SKU-{next(_consec)}',
        cliente=cliente, fecha_entrega=fecha_entrega, estado_siesa=estado_siesa,
        cantidad_pedida=cantidad_pedida, cantidad_pendiente=cantidad_pendiente,
        producto_id=producto_id,
    )
    db.session.add(p)
    db.session.commit()
    return p


class TestMapearMotivo:
    """Pura, sin DB."""

    @pytest.mark.parametrize('motivo_bloqueo,esperado', [
        ('FALTANTE', 'Falta de inventario'),
        ('BACKORDER_SIESA', 'Falta de inventario'),
        ('UBICACION_VACIA', 'Falta de inventario'),
        ('MERCANCIA_AVERIADA', 'Proceso/calidad'),
        ('PRODUCTO_INCORRECTO', 'Proceso/calidad'),
        (None, 'Sin clasificar'),
        ('ALGO_DESCONOCIDO', 'Sin clasificar'),
    ])
    def test_mapeo(self, motivo_bloqueo, esperado):
        assert mapear_motivo(motivo_bloqueo) == esperado


class TestCalcularPedidosPendientes:

    def test_dia_sin_operacion_retorna_ceros(self, app, db, almacen):
        from datetime import date
        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 1, 1), date(2026, 1, 1))
        assert resultado['lineas_pendientes'] == 0
        assert resultado['fill_rate'] is None
        assert resultado['por_motivo'] == {}

    def test_fill_rate_con_despacho_parcial(self, app, db, almacen):
        from datetime import date
        _pedido('PD-A', cantidad_pedida=10, cantidad_pendiente=4, fecha_entrega='2026-08-10T00:00:00')
        _pedido('PD-B', cantidad_pedida=10, cantidad_pendiente=0, fecha_entrega='2026-08-10T00:00:00')
        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 8, 1), date(2026, 8, 15))
        # remisionado = (10-4) + (10-0) = 16; pedido = 20 → fill_rate = 0.8
        assert resultado['fill_rate'] == pytest.approx(0.8)
        assert resultado['lineas_pendientes'] == 1  # solo PD-A tiene pendiente > 0

    def test_excluye_estado_anulado(self, app, db, almacen):
        from datetime import date
        _pedido('PD-ANULADO', estado_siesa=5, cantidad_pendiente=10, fecha_entrega='2026-08-10T00:00:00')
        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 8, 1), date(2026, 8, 15))
        assert resultado['lineas_pendientes'] == 0
        assert resultado['fill_rate'] is None

    def test_excluye_fuera_de_rango_y_fecha_no_parseable(self, app, db, almacen):
        from datetime import date
        _pedido('PD-FUERA', cantidad_pendiente=5, fecha_entrega='2026-01-01T00:00:00')
        _pedido('PD-MAL', cantidad_pendiente=5, fecha_entrega='no-es-una-fecha')
        _pedido('PD-SIN-FECHA', cantidad_pendiente=5, fecha_entrega=None)
        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 8, 1), date(2026, 8, 15))
        assert resultado['lineas_pendientes'] == 0

    def test_motivo_reclasificado_desde_tarea_bloqueada(self, app, db, almacen, producto, ub_picking):
        from datetime import date
        _pedido('PD-MOTIVO', cantidad_pendiente=3, fecha_entrega='2026-08-10T00:00:00',
                producto_id=producto.id)
        db.session.add(TareaPicking(
            codigo='TP-MOTIVO', producto_id=producto.id, cantidad_solicitada=3,
            ubicacion_id=ub_picking.id, almacen_id=almacen.id, estado='BLOQUEADO',
            motivo_bloqueo='FALTANTE', referencia_documento='PD-MOTIVO',
        ))
        db.session.commit()

        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 8, 1), date(2026, 8, 15))
        assert resultado['lineas_pendientes'] == 1
        assert resultado['por_motivo'] == {'Falta de inventario': 1}

    def test_sin_tarea_bloqueada_cae_en_sin_clasificar(self, app, db, almacen):
        from datetime import date
        _pedido('PD-SOLO', cantidad_pendiente=2, fecha_entrega='2026-08-10T00:00:00')
        resultado = calcular_pedidos_pendientes(almacen.id, date(2026, 8, 1), date(2026, 8, 15))
        assert resultado['por_motivo'] == {'Sin clasificar': 1}


class TestListarPedidosPendientesDetalle:

    def test_pagina_y_calcula_dias_atraso(self, app, db, almacen):
        from datetime import date
        _pedido('PD-DET-1', cantidad_pendiente=1, fecha_entrega='2026-08-05T00:00:00')
        _pedido('PD-DET-2', cantidad_pendiente=1, fecha_entrega='2026-08-01T00:00:00')

        resultado = listar_pedidos_pendientes_detalle(almacen.id, date(2026, 8, 1), date(2026, 8, 10),
                                                        page=1, per_page=1)
        assert resultado['total'] == 2
        assert len(resultado['items']) == 1
        # el más atrasado primero (fecha_entrega más vieja = más días de atraso)
        assert resultado['items'][0]['numero_pedido'] == 'PD-DET-2'
        assert resultado['items'][0]['dias_atraso'] == 9

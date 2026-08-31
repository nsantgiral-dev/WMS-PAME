"""
Simulación end-to-end del conductor (Víctor) → Liquidación → Devolución.

Usa el arnés de `conductor_de_flujo.py` (servicios reales, no filas
fabricadas) para llevar UN pedido hasta la ruta, y desde ahí ejerce
`RutaService.confirmar_parada` con los cuatro datos que el conductor puede
declarar en la pantalla de confirmación de parada:

    1. Entregado + Pago total
    2. Entregado + Pago parcial (retención en la puerta)
    3. Parcial (el cliente devuelve parte del pedido)
    4. Rechazado (la mercancía vuelve al camión)
    5. Rechazado, pero el cliente se queda con la mercancía (bonus — el
       cuarto estado, `ENTREGADO_SIN_PAGO`)

Cada escenario sigue el camino real hasta el final:
`LiquidacionService.liquidar_ruta_siesa` (lo que la pantalla de Liquidación
dispara) y, cuando hay mercancía que vuelve, `DevolucionClienteService.
confirmar_entrada_fisica` (lo que la recepcionista confirma en Devoluciones)
— verificando que el inventario reingresa a la bodega y que la NC queda
encolada.

No toca Siesa real: `connekta` se reemplaza por un doble en cada test (no hay
`CONNEKTA_IKEY` en este entorno, así que tampoco lo tocaría en modo normal —
el doble está para controlar los valores que Liquidación necesita, no por
seguridad).
"""
import pytest
from unittest.mock import MagicMock, patch

from tests.flujo.conductor_de_flujo import (
    Flujo, hacer_packing, hacer_picking, hacer_ruta, sembrar_catalogo,
    sembrar_pedido,
)


@pytest.fixture
def victor(db, almacen):
    """El conductor de esta simulación — cuenta de login + ficha de conductor."""
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash

    u = Usuario(nombre='Victor Ramirez', email='victor@test.com',
                password_hash=generate_password_hash('test123'),
                rol='conductor', almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.flush()
    c = Conductor(nombre='Victor Ramirez', cedula='V-9999999',
                  usuario_id=u.id, activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    return {'usuario_id': u.id, 'conductor_id': c.id}


def _armar_parada(db, almacen, victor, cantidad_pedida=10):
    """Pedido → picking completo → packing → ruta EN_TRANSITO, listo para
    que Víctor confirme la parada. Recoge el 100% a propósito (no el picking
    parcial que `conductor_de_flujo` usa por defecto) — lo que se ejerce acá
    es la devolución DECLARADA POR EL CLIENTE en la puerta, no un faltante de
    bodega; mezclar los dos habría hecho el cálculo de cada escenario
    ambiguo."""
    productos, _ = sembrar_catalogo(db, almacen, n=1, con_stock=100)
    pedido = sembrar_pedido(db, productos, cantidad=cantidad_pedida)
    flujo = Flujo(pedido=pedido, almacen_id=almacen.id,
                  usuario_id=victor['usuario_id'],
                  producto_ids=[p.id for p in productos])
    hacer_picking(db, flujo, cantidad_pedida, cantidad_pedida)
    hacer_packing(db, flujo)
    hacer_ruta(db, flujo, victor['conductor_id'])
    return flujo, productos[0]


def _mock_connekta(producto, cantidad_facturada, bruto_unit=10_000, iva_pct=0.19):
    """Doble de Connekta — mismo patrón que `tests/test_liquidacion.py`.

    `get_detalle_factura` vacío + `modo_simulacion=True` hace que
    `fe_resolver` resuelva la FE como `SIMFE-<pedido>` sin tocar la red —
    la resolución de FE no es lo que estos escenarios verifican.
    """
    bruto = round(bruto_unit * cantidad_facturada, 2)
    iva = round(bruto * iva_pct, 2)
    neto = round(bruto + iva, 2)
    mock = MagicMock()
    mock.modo_simulacion = True
    mock.get_detalle_factura.return_value = []
    mock.get_pedido_cabecera.return_value = {
        'f430_id_co': '003', 'f200_id_pedido_fact': 'NIT-CLIENTE-VICTOR',
        'f461_id_sucursal_pedido_rem': '001',
    }
    mock.get_rowids_factura.return_value = [{
        'f470_vlr_bruto': bruto, 'f470_vlr_imp': iva, 'f470_vlr_neto': neto,
        'f120_referencia': producto.codigo_siesa, 'f470_rowid': 'R1',
        'f470_cant_base': float(cantidad_facturada),
        'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
    }]
    return mock, bruto, iva, neto


def _stock_total(producto_id):
    from app.models.inventario import UbicacionProducto
    filas = UbicacionProducto.query.filter_by(producto_id=producto_id).all()
    return sum(f.cantidad for f in filas)


def _jobs(tipo, referencia_tipo, referencia_id):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(
        tipo=tipo, referencia_tipo=referencia_tipo, referencia_id=referencia_id
    ).all()


class TestEntregaTotalPagoTotal:

    def test_pago_total_encola_solo_el_rc(self, db, almacen, victor):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': neto,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)

        assert resumen['rc_encolados'] == 1
        assert resumen['nc_encolados'] == 0
        assert resumen['dc_encolados'] == 0
        assert not resumen['errores']
        rc_jobs = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)
        assert len(rc_jobs) == 1
        payload = rc_jobs[0].get_payload()
        assert payload['monto'] == neto
        assert payload['forma_pago'] == 'EFECTIVO'

    def test_transferencia_bancaria_especifica_no_rebota(self, db, almacen, victor):
        """Regresión — capturada en vivo en QA (2026-08-31): el `<select>`
        del conductor (`_FORMAS_PAGO_COBRO`, rutas.js) se desglosó por banco,
        pero `FormaPago.VALIDOS` se quedó con la lista vieja de 5. El
        conductor elegía «Transferencia Bancolombia Corriente» y el propio
        servidor lo rechazaba con «forma_pago inválido» — la pantalla ofrecía
        una opción que el backend no aceptaba de vuelta."""
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'ENTREGADO',
                    'forma_pago': 'TRANSFERENCIA_BANCOLOMBIA_CTE',
                    'monto_cobrado': neto,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)

        assert resumen['rc_encolados'] == 1
        assert not resumen['errores']
        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        assert rc_payload['forma_pago'] == 'TRANSFERENCIA_BANCOLOMBIA_CTE'


class TestEntregaTotalPagoParcial:

    def test_retencion_en_la_puerta_encola_rc_y_dc(self, db, almacen, victor):
        """«Pago Parcial» sobre Entregado — el conductor declara una
        retención tributaria (RETEFUENTE, ICA...) que el cliente aplicó al
        pagar, no un faltante sin explicar. `monto_descuento` viaja ya
        calculado, tal como lo arma la pantalla del conductor."""
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        retencion = round(bruto * 0.025, 2)  # RETEFUENTE_2.5
        monto_cobrado = round(neto - retencion, 2)

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': monto_cobrado,
                    'motivo_descuento': 'RETEFUENTE_2.5',
                    'monto_descuento': retencion,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)

        assert resumen['rc_encolados'] == 1
        assert resumen['dc_encolados'] == 1
        assert resumen['nc_encolados'] == 0
        assert not resumen['errores']

        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        assert rc_payload['monto'] == monto_cobrado

        dc_payload = _jobs('DOCUMENTO_CONTABLE_RET', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        assert dc_payload['cuenta_puc'] == '13551501'
        assert dc_payload['monto'] == retencion


class TestPedidoParcialDevolucion:

    def test_cliente_devuelve_parte_del_pedido(self, db, almacen, victor):
        """El cliente recibe 6 de 10 — los 4 que sobran quedan como
        devolución. Liquidación arma la DevolucionCliente ABIERTA; el
        módulo de Devoluciones la confirma, y ESO reingresa el stock a la
        bodega (Liquidación misma nunca toca inventario — ver
        `_crear_devolucion_pendiente`)."""
        from app.models.devolucion_cliente import DevolucionCliente, EstadoDevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        precio_unit_neto = round(neto / 10, 2)
        entregado, devuelto = 6, 4
        monto_cobrado = round(precio_unit_neto * entregado, 2)

        stock_antes = _stock_total(producto.id)

        with patch('app.services.connekta_gateway.connekta', mock), \
             patch('app.services.devolucion_cliente_service.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': monto_cobrado,
                    'observaciones': 'Cliente devolvio 4 unidades por sobrepedido',
                    'items_entregados': [{
                        'codigo': producto.codigo, 'nombre': producto.nombre,
                        'unidad': 'und', 'cantidad_pedida': 10,
                        'cantidad_entregada': entregado,
                    }],
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)

            assert resumen['nc_encolados'] == 1
            assert resumen['rc_encolados'] == 1
            assert not resumen['errores']

            devolucion = DevolucionCliente.query.filter_by(
                recaudo_entrega_id=recaudo_id).one()
            assert devolucion.estado == EstadoDevolucionCliente.ABIERTA
            assert devolucion.es_total is False
            linea = devolucion.lineas[0]
            assert linea.producto_id == producto.id
            assert linea.cantidad_facturada == 10
            assert linea.cantidad_devuelta == devuelto

            # ── Módulo de Devolución: recepción confirma físicamente ──────
            DevolucionClienteService.confirmar_entrada_fisica(
                devolucion.id, recepcionista_id=victor['usuario_id'])

        devolucion = DevolucionCliente.query.get(devolucion.id)
        assert devolucion.estado == EstadoDevolucionCliente.CONFIRMADA
        assert _stock_total(producto.id) == stock_antes + devuelto

        nc_jobs = _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE', 'DevolucionCliente', devolucion.id)
        assert len(nc_jobs) == 1
        assert nc_jobs[0].get_payload()['items_devueltos'][0]['cantidad_devuelta'] == devuelto


class TestPedidoRechazado:

    def test_rechazado_con_mercancia_que_vuelve_genera_devolucion_total(
            self, db, almacen, victor):
        """RECHAZADO con un motivo que SÍ retorna mercancía (`CLIENTE_CERRADO`):
        toda la factura vuelve. No hay RC — nadie cobró nada."""
        from app.models.bulto import Bulto, EstadoBulto
        from app.models.devolucion_cliente import DevolucionCliente, EstadoDevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        stock_antes = _stock_total(producto.id)

        with patch('app.services.connekta_gateway.connekta', mock), \
             patch('app.services.devolucion_cliente_service.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'RECHAZADO',
                    'motivo_rechazo': 'CLIENTE_CERRADO',
                    'observaciones': 'Local cerrado, nadie recibio',
                })

            bultos = Bulto.query.filter_by(tarea_id=flujo.packing_id).all()
            assert all(b.estado == EstadoBulto.RECHAZADO for b in bultos)

            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)
            assert resumen['nc_encolados'] == 1
            assert resumen['rc_encolados'] == 0
            assert not resumen['errores']

            devolucion = DevolucionCliente.query.filter_by(
                recaudo_entrega_id=recaudo_id).one()
            assert devolucion.es_total is True
            assert devolucion.lineas[0].cantidad_devuelta == 10

            DevolucionClienteService.confirmar_entrada_fisica(
                devolucion.id, recepcionista_id=victor['usuario_id'])

        devolucion = DevolucionCliente.query.get(devolucion.id)
        assert devolucion.estado == EstadoDevolucionCliente.CONFIRMADA
        assert _stock_total(producto.id) == stock_antes + 10

    def test_rechazado_sin_retorno_no_genera_ni_rc_ni_nc(self, db, almacen, victor):
        """`NO_PAGO_SE_QUEDO`: el servidor traduce a `ENTREGADO_SIN_PAGO` —
        los bultos NO vuelven (quedan ENTREGADO) y Liquidación no encola
        nada: la factura queda abierta en cartera para que Gestor la vea,
        no se automatiza como si fuera un crédito autorizado."""
        from app.models.bulto import Bulto, EstadoBulto
        from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, victor, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, victor['usuario_id'], {
                    'estado_entrega': 'RECHAZADO',
                    'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
                    'observaciones': 'Dice que paga la otra semana, se quedo con todo',
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(flujo.ruta_id)

        recaudo = RecaudoEntrega.query.get(recaudo_id)
        assert recaudo.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO
        bultos = Bulto.query.filter_by(tarea_id=flujo.packing_id).all()
        assert all(b.estado == EstadoBulto.ENTREGADO for b in bultos)

        assert resumen['rc_encolados'] == 0
        assert resumen['nc_encolados'] == 0
        assert resumen['dc_encolados'] == 0
        assert not _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)
        assert not _jobs('NOTA_CREDITO_FACTURA', 'RecaudoEntrega', recaudo_id)

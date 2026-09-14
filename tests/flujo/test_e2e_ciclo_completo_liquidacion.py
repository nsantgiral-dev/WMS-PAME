"""
E2E simulado de punta a punta — pedido → picking → packing → muelle → ruta
→ conductor (entrega) → liquidación — para la matriz de escenarios que el
usuario pidió cubrir en Liquidación (2026-09-04): pago completo, picking
parcial, pago parcial, un pedido por cada tipo de pago, un pedido por cada
motivo de descuento (retención) uno por uno, y un pedido rechazado.

## Qué recorre cada grupo de escenarios

**Escenarios 1 y 2** (`TestEscenario01...`, `TestEscenario02...`) recorren
el camino real completo, INCLUIDO el muelle: `RutaService.crear_ruta`
(que ya asigna el conductor) → `MuelleService.asignar_a_ruta` → `MuelleService.
cargar_bulto` (scan-to-truck, uno por bulto) → `RutaService.cerrar_ruta`
(EN_CARGUE → EN_TRANSITO, «enviando esos pedidos»). Es la parte que
`conductor_de_flujo.hacer_ruta()` se salta a propósito (crea la fila
directo en EN_TRANSITO) porque para esos tests el muelle no era lo que se
probaba — acá sí, porque el usuario lo pidió explícitamente como parte del
recorrido.

**El resto** (13 motivos de descuento + pago parcial + 5 tipos de pago + 1
rechazado = 20 pedidos más) reutiliza el atajo de `hacer_ruta` — el muelle
ya quedó probado de punta a punta en los dos primeros; repetir esa misma
mecánica 20 veces más no prueba nada adicional (Regla 0 corolario del
CLAUDE.md: no duplicar la misma verificación en dos sitios). Lo que sí
varía en cada uno de esos 20 es lo único que el conductor puede declarar en
la pantalla de confirmación de parada, que es lo que este archivo existe
para cubrir.

## No toca Siesa real

`connekta` se reemplaza por un doble en todos los escenarios (no hay
`CONNEKTA_IKEY` en este entorno de pruebas, así que tampoco lo tocaría en
modo normal — ver `tests/conftest.py`). El cierre de packing (244328 →
142945) sí corre por el código real, pero en modo simulación (sin
credenciales Connekta) — es lo mismo que ya usa `conductor_de_flujo.py`
en el resto de la suite.

## El reporte

Cada escenario se registra con `_reportar()`: cómo entró (pedido,
cantidad, lo que el conductor declaró en la puerta) y cómo terminó (qué
quedó encolado hacia Siesa — RC/NC/DC — y con qué monto). Por defecto esto
NO escribe nada a disco — es puramente en memoria, para no ensuciar una
corrida normal de CI con archivos nuevos. Si la variable de entorno
`E2E_REPORTE_DIR` está definida, al terminar la sesión de pytest se vuelca
un `.md` y un `.json` con el reporte completo a esa carpeta.
"""
import json
import os
from datetime import datetime, timezone

import pytest
from unittest.mock import patch

from app.services.liquidacion_service import CATALOGO_RETENCIONES, monto_de_retencion
from tests.flujo.conductor_de_flujo import (
    Flujo, _sufijo, hacer_packing, hacer_picking, hacer_ruta,
    sembrar_catalogo, sembrar_pedido,
)
from tests.flujo.test_flujo_conductor_pagos import _jobs, _mock_connekta, _stock_total

_REPORTE = []


def _reportar(escenario, entrada, salida):
    _REPORTE.append({
        'escenario': escenario,
        'entrada': entrada,
        'salida': salida,
    })


def _render_markdown(reporte):
    hoy = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    out = [
        '# Reporte E2E simulado — Pedidos → Picking → Packing → Muelle → '
        'Ruta → Conductor → Liquidación',
        '',
        f'Generado: {hoy}. {len(reporte)} escenarios, cada uno con un '
        'pedido propio, corridos de punta a punta contra los servicios '
        'reales del WMS (sin tocar Siesa real — `connekta` va simulado).',
        '',
    ]
    for i, r in enumerate(reporte, 1):
        out.append(f'## {i}. {r["escenario"]}')
        out.append('')
        out.append('**Cómo inició:**')
        out.append('')
        for k, v in r['entrada'].items():
            out.append(f'- `{k}`: {v}')
        out.append('')
        out.append('**Cómo terminó:**')
        out.append('')
        for k, v in r['salida'].items():
            out.append(f'- `{k}`: {v}')
        out.append('')
    return '\n'.join(out)


@pytest.fixture(scope='session', autouse=True)
def _volcar_reporte():
    yield
    destino = os.environ.get('E2E_REPORTE_DIR')
    if not destino or not _REPORTE:
        return
    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, 'e2e_liquidacion.json'), 'w', encoding='utf-8') as f:
        json.dump(_REPORTE, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(destino, 'e2e_liquidacion.md'), 'w', encoding='utf-8') as f:
        f.write(_render_markdown(_REPORTE))


# ── Fixtures de actores ─────────────────────────────────────────────────────

@pytest.fixture
def conductor_full(db, almacen):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash

    s = _sufijo()
    u = Usuario(nombre='Conductor E2E', email=f'conductor_e2e_{s}@test.com',
                password_hash=generate_password_hash('test123'),
                rol='conductor', almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.flush()
    c = Conductor(nombre='Conductor E2E', cedula=f'E2E-{s}',
                  usuario_id=u.id, activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    return {'usuario_id': u.id, 'conductor_id': c.id}


@pytest.fixture
def vehiculo_full(db):
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa=f'E2E{_sufijo()[:3].upper()}', tipo='NHR', activo=True)
    db.session.add(v)
    db.session.commit()
    return v


@pytest.fixture
def admin_full(db):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(nombre='Admin E2E', email=f'admin_e2e_{_sufijo()}@test.com',
                password_hash=generate_password_hash('test123'),
                rol='admin', activo=True)
    db.session.add(u)
    db.session.commit()
    return u.id


# ── Armado de la parada, dos caminos ────────────────────────────────────────

def _armar_parada(db, almacen, conductor, cantidad_pedida=10, recoger=None):
    """Camino corto (`hacer_ruta`) — ruta directo en EN_TRANSITO. Usado para
    los 20 escenarios donde lo que varía es la confirmación de parada, no el
    muelle (ya probado en los escenarios 1 y 2)."""
    if recoger is None:
        recoger = cantidad_pedida
    productos, _ = sembrar_catalogo(db, almacen, n=1, con_stock=max(cantidad_pedida, 100))
    pedido = sembrar_pedido(db, productos, cantidad=cantidad_pedida)
    flujo = Flujo(pedido=pedido, almacen_id=almacen.id,
                  usuario_id=conductor['usuario_id'],
                  producto_ids=[p.id for p in productos])
    hacer_picking(db, flujo, cantidad_pedida, recoger)
    hacer_packing(db, flujo)
    hacer_ruta(db, flujo, conductor['conductor_id'])
    return flujo, productos[0]


def _armar_parada_muelle_real(db, almacen, conductor, vehiculo, cantidad_pedida=10, recoger=None):
    """Camino real completo: pedido → picking → packing → `RutaService.
    crear_ruta` (asigna conductor+vehículo) → `MuelleService.asignar_a_ruta`
    → `MuelleService.cargar_bulto` (scan-to-truck) → `RutaService.
    cerrar_ruta` (envía la ruta, EN_TRANSITO)."""
    from app.models.bulto import Bulto
    from app.services.muelle_service import MuelleService
    from app.services.ruta_service import RutaService

    if recoger is None:
        recoger = cantidad_pedida
    productos, _ = sembrar_catalogo(db, almacen, n=1, con_stock=max(cantidad_pedida, 100))
    pedido = sembrar_pedido(db, productos, cantidad=cantidad_pedida)
    flujo = Flujo(pedido=pedido, almacen_id=almacen.id,
                  usuario_id=conductor['usuario_id'],
                  producto_ids=[p.id for p in productos])
    hacer_picking(db, flujo, cantidad_pedida, recoger)
    hacer_packing(db, flujo)

    # `cerrar_packing` ENCOLA el despacho (244328→142945, job `DESPACHO_F470`)
    # en vez de dispararlo inline — lo procesa el DLQ, que este entorno de
    # pruebas apaga (`_sin_hilos_dlq_reales`, `tests/conftest.py`) para no
    # dejar hilos vivos entre tests. Ejecutarlo de verdad (`_ejecutar_job`)
    # exige `connekta.get_pedido_cabecera` real — un GET que "modo
    # simulación" no fabrica (solo protege los POST) y que no es lo que
    # este escenario existe para probar (eso ya lo cubre
    # `test_liquidacion_de_punta_a_punta.py`, con Siesa estubado a
    # propósito). Lo que el muelle necesita es solo la bandera que ese job
    # deja al terminar (`MuelleService.listar_bultos_listos`/`cargar_bulto`
    # filtran por `siesa_triggered=True`) — se sienta acá, en la frontera,
    # igual que `ruta_entregada` (`test_liquidacion_de_punta_a_punta.py`)
    # siembra el consecutivo de FE que solo Siesa puede asignar.
    from app.models.packing import TareaPacking

    tarea = TareaPacking.query.get(flujo.packing_id)
    tarea.siesa_triggered = True
    db.session.commit()

    ruta = RutaService.crear_ruta({
        'conductor_id': conductor['conductor_id'],
        'vehiculo_id': vehiculo.id,
        'tipo_ruta': 'Urbana',
    })
    MuelleService.asignar_a_ruta(ruta.id, bultos_ids=flujo.bultos)
    # `MuelleService.cargar_bulto` normaliza el código escaneado a
    # mayúsculas antes de buscarlo (escáneres con layout español) — en
    # producción el número de pedido Siesa siempre llega en mayúsculas, así
    # que `codigo_barras` ya nace uppercase. El arnés lo genera con
    # `_sufijo()` (hex minúsculas) solo para unicidad de prueba; se
    # normaliza acá antes de escanear para no simular un desalineamiento
    # que no ocurre en la operación real.
    for bulto_id in flujo.bultos:
        b = Bulto.query.get(bulto_id)
        b.codigo_barras = b.codigo_barras.upper()
    db.session.commit()
    for bulto_id in flujo.bultos:
        b = Bulto.query.get(bulto_id)
        resultado_scan = MuelleService.cargar_bulto(b.codigo_barras, ruta.id)
        assert resultado_scan.get('ok'), f'el bulto {b.codigo_barras} no cargó en el muelle'
    RutaService.cerrar_ruta(ruta.id)

    flujo.ruta_id = ruta.id
    return flujo, productos[0]


# ── Escenario 1 — pedido completo, muelle real, pago completo ──────────────

class TestEscenario01PedidoCompletoPagoCompleto:
    """Recogido = pedido completo. Entregado = todo. Pagado = todo, en
    efectivo. El camino de muelle real de punta a punta."""

    def test_pedido_completo_pago_completo_muelle_real(
            self, db, almacen, conductor_full, vehiculo_full, admin_full):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService
        from app.models.ruta_despacho import EstadoRutaDespacho, RutaDespacho

        flujo, producto = _armar_parada_muelle_real(
            db, almacen, conductor_full, vehiculo_full, cantidad_pedida=10)

        ruta = RutaDespacho.query.get(flujo.ruta_id)
        assert ruta.estado == EstadoRutaDespacho.EN_TRANSITO, (
            'el muelle no dejó la ruta en tránsito — la ruta no se "envió"')

        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': neto,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)

        assert not resumen['errores']
        assert resumen['rc_encolados'] == 1
        assert resumen['nc_encolados'] == 0
        assert resumen['dc_encolados'] == 0

        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        assert rc_payload['monto'] == neto

        _reportar(
            'Escenario 1 — Pedido completo · muelle real (crear ruta → '
            'asignar en muelle → escanear bultos → enviar ruta) · pago '
            'completo en efectivo',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'cantidad_recogida_picking': 10, 'ruta_id': flujo.ruta_id,
                'estado_entrega_declarado': 'ENTREGADO',
                'forma_pago_declarada': 'EFECTIVO',
                'monto_declarado': neto, 'motivo_descuento': None,
            },
            salida={
                'estado_final_ruta': ruta.estado,
                'jobs_encolados': resumen,
                'recibo_de_caja_monto': rc_payload['monto'],
                'recibo_de_caja_forma_pago': rc_payload['forma_pago'],
            },
        )


# ── Escenario 2 — picking PARCIAL (backorder), muelle real, pago completo ──

class TestEscenario02PickingParcialMuelleReal:
    """Se piden 10, bodega solo recoge 6 (faltante de picking, no del
    cliente en la puerta) — el pedido "parcial" en el sentido de bodega, no
    de entrega. Lo que se empacó (6) se entrega y se cobra completo."""

    def test_picking_parcial_pago_completo_de_lo_recogido(
            self, db, almacen, conductor_full, vehiculo_full, admin_full):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada_muelle_real(
            db, almacen, conductor_full, vehiculo_full,
            cantidad_pedida=10, recoger=6)

        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=6)
        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': neto,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)

        assert not resumen['errores']
        assert resumen['rc_encolados'] == 1
        assert resumen['nc_encolados'] == 0

        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()

        _reportar(
            'Escenario 2 — Pedido con picking PARCIAL en bodega (6 de 10 '
            'unidades pedidas) · muelle real · lo empacado se entrega y se '
            'cobra completo',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'cantidad_recogida_picking': 6, 'ruta_id': flujo.ruta_id,
                'estado_entrega_declarado': 'ENTREGADO',
                'forma_pago_declarada': 'EFECTIVO', 'monto_declarado': neto,
            },
            salida={
                'jobs_encolados': resumen,
                'recibo_de_caja_monto': rc_payload['monto'],
            },
        )


# ── Un pedido por cada uno de los 13 motivos de descuento (retención) ──────

MOTIVOS_DESCUENTO = list(CATALOGO_RETENCIONES.keys())


class TestUnPedidoPorMotivoDeDescuento:
    """Entrega completa, pago en efectivo NETO de la retención declarada —
    un pedido nuevo por cada uno de los 13 motivos reales del catálogo
    (`CATALOGO_RETENCIONES`, `liquidacion_service.py`). El motivo de
    descuento es distinto del "pago parcial": acá el cliente SÍ recibe todo
    y SÍ paga todo, solo que retiene una parte del valor a título tributario
    — es lo que genera el Documento Contable (142882), no una Nota Crédito."""

    @pytest.mark.parametrize('motivo', MOTIVOS_DESCUENTO)
    def test_motivo_de_descuento_uno_por_uno(
            self, db, almacen, conductor_full, admin_full, motivo):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, conductor_full, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        retencion = monto_de_retencion(motivo, bruto, iva)
        assert retencion > 0, f'{motivo}: la tasa del catálogo dio retención $0, revisar el caso de prueba'
        monto_cobrado = round(neto - retencion, 2)

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': monto_cobrado,
                    'motivo_descuento': motivo,
                    'monto_descuento': retencion,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)

        assert not resumen['errores'], f'{motivo}: {resumen["errores"]}'
        assert resumen['rc_encolados'] == 1, f'{motivo}: no encoló RC'
        assert resumen['dc_encolados'] == 1, f'{motivo}: no encoló DC'
        assert resumen['nc_encolados'] == 0

        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        dc_payload = _jobs('DOCUMENTO_CONTABLE_RET', 'RecaudoEntrega', recaudo_id)[0].get_payload()

        cuenta_puc_esperada = CATALOGO_RETENCIONES[motivo]['puc']
        assert dc_payload['cuenta_puc'] == cuenta_puc_esperada, (
            f'{motivo}: DC salió con cuenta PUC {dc_payload["cuenta_puc"]}, '
            f'se esperaba {cuenta_puc_esperada}')
        assert abs(dc_payload['monto'] - retencion) < 0.01
        assert abs(rc_payload['monto'] - monto_cobrado) < 0.01

        _reportar(
            f'Motivo de descuento — {motivo} ({CATALOGO_RETENCIONES[motivo]["nombre"]})',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'estado_entrega_declarado': 'ENTREGADO',
                'forma_pago_declarada': 'EFECTIVO',
                'factura_bruta': bruto, 'factura_iva': iva, 'factura_neta': neto,
                'motivo_descuento': motivo,
                'monto_descuento_declarado': retencion,
                'monto_cobrado_declarado': monto_cobrado,
            },
            salida={
                'jobs_encolados': resumen,
                'documento_contable_cuenta_puc': dc_payload['cuenta_puc'],
                'documento_contable_monto': dc_payload['monto'],
                'recibo_de_caja_monto_neto_de_retencion': rc_payload['monto'],
            },
        )


# ── Pago parcial — el cliente recibe menos y paga solo lo entregado ───────

class TestPagoParcial:
    """`estado_entrega=PARCIAL`: el cliente devuelve parte del pedido en la
    puerta y paga solo lo que se queda. Genera Nota Crédito (por lo
    devuelto) + Recibo de Caja (por lo que sí pagó) — distinto de un motivo
    de descuento, donde todo se entrega y solo cambia cuánto retiene."""

    def test_cliente_recibe_menos_y_paga_lo_entregado(
            self, db, almacen, conductor_full, admin_full):
        from app.models.devolucion_cliente import DevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, conductor_full, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        precio_unit_neto = round(neto / 10, 2)
        entregado, devuelto = 7, 3
        monto_cobrado = round(precio_unit_neto * entregado, 2)
        stock_antes = _stock_total(producto.id)

        with patch('app.services.connekta_gateway.connekta', mock), \
             patch('app.services.devolucion_cliente_service.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': monto_cobrado,
                    'observaciones': 'Cliente recibio 7 de 10, devolvio 3 por sobrepedido',
                    'items_entregados': [{
                        'codigo': producto.codigo, 'nombre': producto.nombre,
                        'unidad': 'und', 'cantidad_pedida': 10,
                        'cantidad_entregada': entregado,
                    }],
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)

            assert not resumen['errores']
            assert resumen['nc_encolados'] == 1
            assert resumen['rc_encolados'] == 1

            devolucion = DevolucionCliente.query.filter_by(
                recaudo_entrega_id=recaudo_id).one()
            DevolucionClienteService.confirmar_entrada_fisica(
                devolucion.id, recepcionista_id=conductor_full['usuario_id'])

        devolucion = DevolucionCliente.query.get(devolucion.id)
        rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
        nc_jobs = _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE', 'DevolucionCliente', devolucion.id)

        _reportar(
            'Pago parcial — el cliente recibe 7 de 10, devuelve 3, paga '
            'solo lo entregado',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'estado_entrega_declarado': 'PARCIAL',
                'cantidad_entregada': entregado, 'cantidad_devuelta': devuelto,
                'forma_pago_declarada': 'EFECTIVO',
                'monto_cobrado_declarado': monto_cobrado,
            },
            salida={
                'jobs_encolados': resumen,
                'devolucion_estado_final': devolucion.estado,
                'stock_reingresado': _stock_total(producto.id) - stock_antes,
                'recibo_de_caja_monto': rc_payload['monto'],
                'nota_credito_encolada': bool(nc_jobs),
            },
        )


# ── Un pedido por cada tipo de pago restante ────────────────────────────────
# EFECTIVO ya quedó cubierto en el Escenario 1 — acá va el resto de
# `FormaPago` (`ruta_service.py`): TRANSFERENCIA, CHEQUE, TARJETA, CREDITO,
# EXENTO. (Los 7 medios bancarios específicos de TRANSFERENCIA —
# TRANSFERENCIA_BANCOLOMBIA_CTE, etc. — ya están cubiertos exhaustivamente
# en `test_flujo_conductor_pagos.py::TestTodasLasFormasDePagoHastaLiquidacion`,
# no se repiten acá.)

TIPOS_DE_PAGO_RESTANTES = ('TRANSFERENCIA', 'CHEQUE', 'TARJETA', 'CREDITO', 'EXENTO')


class TestUnPedidoPorTipoDePago:

    @pytest.mark.parametrize('forma_pago', TIPOS_DE_PAGO_RESTANTES)
    def test_tipo_de_pago_hasta_liquidacion(
            self, db, almacen, conductor_full, admin_full, forma_pago):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, conductor_full, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        monto_cobrado = 0 if forma_pago in ('CREDITO', 'EXENTO') else neto

        with patch('app.services.connekta_gateway.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'ENTREGADO', 'forma_pago': forma_pago,
                    'monto_cobrado': monto_cobrado,
                })
            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)

        assert not resumen['errores'], f'{forma_pago}: {resumen["errores"]}'

        salida = {'jobs_encolados': resumen}
        if forma_pago == 'CREDITO':
            assert resumen['credito_omitidos'] == 1
            assert resumen['rc_encolados'] == 0
            salida['nota'] = ('CREDITO no se cobra en la puerta — la '
                               'factura queda abierta en cartera, la '
                               'gestiona el Gestor de Cartera, no un RC de '
                               'ruta.')
        elif forma_pago == 'EXENTO':
            assert resumen['rc_encolados'] == 0
            assert resumen['nc_encolados'] == 0
            assert resumen['dc_encolados'] == 0
            salida['nota'] = ('EXENTO no dispara ningún documento hacia '
                               'Siesa desde la liquidación de ruta.')
        else:
            assert resumen['rc_encolados'] == 1, f'{forma_pago} no encoló RC'
            rc_payload = _jobs('RECIBO_CAJA', 'RecaudoEntrega', recaudo_id)[0].get_payload()
            assert rc_payload['forma_pago'] == forma_pago
            assert rc_payload['monto'] == neto
            salida['recibo_de_caja_monto'] = rc_payload['monto']
            salida['recibo_de_caja_forma_pago'] = rc_payload['forma_pago']

        _reportar(
            f'Tipo de pago — {forma_pago}',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'estado_entrega_declarado': 'ENTREGADO',
                'forma_pago_declarada': forma_pago,
                'monto_declarado': monto_cobrado,
            },
            salida=salida,
        )


# ── Un pedido rechazado ─────────────────────────────────────────────────────

class TestUnPedidoRechazado:
    """`estado_entrega=RECHAZADO`, motivo `CLIENTE_CERRADO` (retorna
    mercancía): toda la factura se devuelve — no hay RC, sí hay NC total."""

    def test_rechazado_devuelve_toda_la_mercancia(
            self, db, almacen, conductor_full, admin_full):
        from app.models.bulto import Bulto, EstadoBulto
        from app.models.devolucion_cliente import DevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        flujo, producto = _armar_parada(db, almacen, conductor_full, cantidad_pedida=10)
        mock, bruto, iva, neto = _mock_connekta(producto, cantidad_facturada=10)
        stock_antes = _stock_total(producto.id)

        with patch('app.services.connekta_gateway.connekta', mock), \
             patch('app.services.devolucion_cliente_service.connekta', mock):
            recaudo_id, _ = RutaService.confirmar_parada(
                flujo.ruta_id, flujo.packing_id, conductor_full['usuario_id'], {
                    'estado_entrega': 'RECHAZADO',
                    'motivo_rechazo': 'CLIENTE_CERRADO',
                    'observaciones': 'Local cerrado, nadie recibio el pedido',
                })

            bultos = Bulto.query.filter_by(tarea_id=flujo.packing_id).all()
            assert all(b.estado == EstadoBulto.RECHAZADO for b in bultos)

            resumen = LiquidacionService.liquidar_ruta_siesa(
                flujo.ruta_id, admin_id=admin_full)
            assert not resumen['errores']
            assert resumen['nc_encolados'] == 1
            assert resumen['rc_encolados'] == 0

            devolucion = DevolucionCliente.query.filter_by(
                recaudo_entrega_id=recaudo_id).one()
            assert devolucion.es_total is True

            DevolucionClienteService.confirmar_entrada_fisica(
                devolucion.id, recepcionista_id=conductor_full['usuario_id'])

        devolucion = DevolucionCliente.query.get(devolucion.id)
        nc_jobs = _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE', 'DevolucionCliente', devolucion.id)

        _reportar(
            'Pedido rechazado — cliente cerrado, toda la mercancía vuelve al camión',
            entrada={
                'pedido_siesa': flujo.pedido, 'cantidad_pedida': 10,
                'estado_entrega_declarado': 'RECHAZADO',
                'motivo_rechazo': 'CLIENTE_CERRADO',
            },
            salida={
                'jobs_encolados': resumen,
                'devolucion_es_total': devolucion.es_total,
                'devolucion_estado_final': devolucion.estado,
                'stock_reingresado': _stock_total(producto.id) - stock_antes,
                'nota_credito_encolada': bool(nc_jobs),
                'recibo_de_caja_encolado': False,
            },
        )

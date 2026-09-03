"""
El fallback de descuento de un traslado tomaba unidades de cualquier bodega.

`_descontar_inventario_wms` buscaba las ubicaciones del producto por
`producto_id` a secas y las ordenaba por cantidad ascendente. El almacén de
origen solo aparecía después, al firmar el `MovimientoInventario` con
`almacen.id` — de modo que un traslado que sale del CD (NB1) podía vaciar el
bin de Neiva Centro y anotar la salida a nombre de Bodega CD.

No era un caso de borde: el `order_by` ascendente empieza justamente por las
ubicaciones con menos unidades, que son las de las tiendas. Era el primer bin
que tocaba.

Y ningún cuadre por sumas lo ve — el total de la red no cambia, solo se mueve
de un almacén a otro. Lo que se rompe es el saldo por bodega: la tienda queda
con menos de lo que tiene en el piso (y su picker reporta un faltante que no
existe) y el CD con más de lo que tiene (y promete stock que ya salió).

Detector en las dos direcciones (regla dura del repo): además de exigir que el
cruce de almacén no vuelva a ocurrir, este archivo exige que el traslado sano
—stock suficiente en el almacén correcto— siga descontando exactamente igual
que antes. Un detector que solo prueba que dispara, prueba la mitad.
"""
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — dos almacenes, el mismo producto en los dos
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def almacen_tienda(db):
    """Neiva Centro (NC1) — el almacén que NO debe tocarse."""
    from app.models.almacen import Almacen
    a = Almacen(codigo='ALM-NC1', nombre='Neiva Centro',
                bodega_siesa_id='NC1', activo=True)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def ub_tienda(db, almacen_tienda):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='NC1-PIK-01', almacen_id=almacen_tienda.id,
                  tipo_zona='PICKING', activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def ub_cd_2(db, almacen):
    """Segundo bin dentro del MISMO almacén origen (NB1)."""
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='NB1-PIK-02', almacen_id=almacen.id,
                  tipo_zona='PICKING', activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def solicitud_cd(db, usuario_admin, producto):
    """Traslado que SALE del CD (NB1) hacia la tienda, listo para descontar."""
    from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
    s = SolicitudTraslado(
        codigo='ST-ALM-0001',
        bodega_origen_siesa='NB1',
        bodega_destino_siesa='NC1',
        nombre_punto_venta='Neiva Centro',
        estado='EN_PICKING',
        modo_transferencia='EN_TRANSITO',
        bodega_transito_siesa='TR',
        solicitante_id=usuario_admin.id,
        aprobador_id=usuario_admin.id,
    )
    db.session.add(s)
    db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id,
        producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        cantidad_solicitada=10,
        cantidad_aprobada=10,
        cantidad_enviada=10,
    ))
    db.session.commit()
    return s


def _inv(db, ubicacion, producto, cantidad):
    from app.models.inventario import UbicacionProducto
    reg = UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                            cantidad=cantidad, reservado=0)
    db.session.add(reg)
    db.session.commit()
    return reg.id


# ─────────────────────────────────────────────────────────────────────────────
# Dirección 1 — el defecto: el descuento cruzaba de almacén
# ─────────────────────────────────────────────────────────────────────────────

class TestElDescuentoNoCruzaDeAlmacen:

    def test_no_vacia_el_bin_de_la_tienda(
            self, app, db, almacen, ub_picking, ub_tienda, solicitud_cd, producto):
        """
        El caso exacto que ocurría en producción: la tienda tiene MENOS que el
        CD, así que el `order_by(cantidad.asc())` la elegía primero.
        """
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto

            id_tienda = _inv(db, ub_tienda, producto, 4)    # menos → iba primero
            id_cd = _inv(db, ub_picking, producto, 100)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_tienda).cantidad == 4, \
                'el traslado salió del CD y vació el bin de la tienda'
            assert UbicacionProducto.query.get(id_cd).cantidad == 90

    def test_el_saldo_del_movimiento_es_el_del_almacen_que_firma(
            self, app, db, almacen, ub_picking, ub_tienda, solicitud_cd, producto):
        """
        `saldo_antes`/`saldo_despues` van en un movimiento firmado con
        `almacen_id` del origen. Si suman el stock de toda la red, el kardex de
        esa bodega declara un saldo que esa bodega nunca tuvo.
        """
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import MovimientoInventario

            _inv(db, ub_tienda, producto, 4)
            _inv(db, ub_picking, producto, 100)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            mov = MovimientoInventario.query.filter_by(
                numero_documento=solicitud_cd.codigo).one()
            assert mov.almacen_id == almacen.id
            assert mov.saldo_antes == 100, 'saldo_antes suma stock de otras bodegas'
            assert mov.saldo_despues == 90
            assert mov.cantidad == -10


# ─────────────────────────────────────────────────────────────────────────────
# Dirección 2 — la operación sana sigue funcionando igual
# ─────────────────────────────────────────────────────────────────────────────

class TestLaOperacionSanaNoCambia:

    def test_descuenta_del_almacen_correcto_con_stock_suficiente(
            self, app, db, almacen, ub_picking, solicitud_cd, producto):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto, MovimientoInventario

            id_cd = _inv(db, ub_picking, producto, 30)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_cd).cantidad == 20
            mov = MovimientoInventario.query.filter_by(
                numero_documento=solicitud_cd.codigo).one()
            assert mov.tipo == 'SALIDA_TRASLADO'
            assert mov.cantidad == -10
            assert mov.saldo_antes == 30 and mov.saldo_despues == 20
            assert mov.siesa_sync == 'OMITIDO'
            assert solicitud_cd.codigo in mov.motivo
            assert 'FALTANTE' not in (mov.motivo or '')

    def test_reparte_entre_varios_bins_del_mismo_almacen_de_menor_a_mayor(
            self, app, db, almacen, ub_picking, ub_cd_2, solicitud_cd, producto):
        """El FIFO de bins pequeños primero es intencional — dentro del almacén."""
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto

            id_chico = _inv(db, ub_cd_2, producto, 4)
            id_grande = _inv(db, ub_picking, producto, 100)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_chico).cantidad == 0
            assert UbicacionProducto.query.get(id_grande).cantidad == 94

    def test_ignora_los_bins_en_cero_del_almacen_correcto(
            self, app, db, almacen, ub_picking, ub_cd_2, solicitud_cd, producto):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto

            id_vacio = _inv(db, ub_cd_2, producto, 0)
            id_cd = _inv(db, ub_picking, producto, 30)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_vacio).cantidad == 0
            assert UbicacionProducto.query.get(id_cd).cantidad == 20


# ─────────────────────────────────────────────────────────────────────────────
# Dirección 3 — lo que antes tapaba el cruce: el faltante ahora se declara
# ─────────────────────────────────────────────────────────────────────────────

class TestStockInsuficienteSeDeclara:

    def test_no_completa_robando_de_otra_bodega(
            self, app, db, almacen, ub_picking, ub_tienda, solicitud_cd, producto):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto

            id_cd = _inv(db, ub_picking, producto, 6)
            id_tienda = _inv(db, ub_tienda, producto, 500)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_cd).cantidad == 0
            assert UbicacionProducto.query.get(id_tienda).cantidad == 500

    def test_el_movimiento_declara_el_faltante(
            self, app, db, almacen, ub_picking, solicitud_cd, producto):
        """
        Un `logger.warning` no es una declaración: nadie lee los logs de un
        despacho que salió en verde. El faltante queda en el movimiento, que es
        la fila que alguien consulta cuando el saldo no cuadra.
        """
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import MovimientoInventario

            _inv(db, ub_picking, producto, 6)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            mov = MovimientoInventario.query.filter_by(
                numero_documento=solicitud_cd.codigo).one()
            assert mov.cantidad == -6, 'el movimiento debe registrar lo realmente descontado'
            assert mov.saldo_antes == 6 and mov.saldo_despues == 0
            assert 'FALTANTE' in (mov.motivo or ''), \
                'el faltante no queda declarado en ningún lado consultable'
            assert '4' in mov.motivo
            assert len(mov.motivo) <= 200

    def test_sin_stock_en_el_almacen_correcto_no_descuenta_nada(
            self, app, db, almacen, ub_tienda, solicitud_cd, producto):
        """El almacén origen no tiene ni una fila — antes se llevaba las 10 de la tienda."""
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto, MovimientoInventario

            id_tienda = _inv(db, ub_tienda, producto, 500)

            TrasladoService._descontar_inventario_wms(solicitud_cd)

            assert UbicacionProducto.query.get(id_tienda).cantidad == 500
            mov = MovimientoInventario.query.filter_by(
                numero_documento=solicitud_cd.codigo).one()
            assert mov.cantidad == 0
            assert mov.saldo_antes == 0 and mov.saldo_despues == 0
            assert 'FALTANTE' in (mov.motivo or '')

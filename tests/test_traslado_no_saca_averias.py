"""Un traslado **no puede llevarse mercancía averiada** como si fuera buena.

## El defecto que este archivo cierra

`_descontar_inventario_wms` arma la lista de bins candidatos así:

    UbicacionProducto.query
      .filter_by(producto_id=...)
      .filter(UbicacionProducto.ubicacion_id.in_(_ubis_del_almacen))
      .filter(UbicacionProducto.cantidad > 0)
      .order_by(UbicacionProducto.cantidad.asc())

El único recorte es por almacén. **No pregunta por la zona.** Y el `order_by`
ascendente empieza por los bins más pequeños — que es exactamente la forma de
un bin de averías, donde se acumulan pocas unidades sueltas.

El comentario que está sobre esa consulta cuenta que el filtro de almacén se
agregó porque el mismo `order_by` vaciaba primero los bins de las tiendas. Se
cubrió la dimensión *almacén* y quedó sin cubrir la dimensión *zona*: mismo
defecto, siguiente eje. Y el propio comentario advierte que «ningún cuadre por
sumas lo detecta» — el total de la red no cambia.

Hay tres puertas, no una:

  · el descuento al despachar (`_descontar_inventario_wms`),
  · el catálogo de «Pedir desde» (`_get_stock_wms`), que ofrece lo averiado
    como disponible,
  · y el retorno de la reversa, que elige bin con `order_by(id).first()` bajo
    el nombre `ub_general` — si el bin de averías es el más antiguo del
    almacén, una reversa mete mercancía BUENA dentro de él.

## Por qué ahora y no después

Medido contra producción el 2026-09-14: 24 ubicaciones, todas `GENERAL`. No
existe un solo bin de averías todavía. Estos filtros no cambian ningún
comportamiento actual — son la condición para que crear el primero sea seguro.
Al revés, crear el bin antes que los filtros activa las tres puertas a la vez.
"""
import pytest

from app.models.almacen import Almacen
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado, EstadoTraslado
from app.models.ubicacion import Ubicacion
from app.services.traslado_service import TrasladoService


ZONAS_VENDIBLES = ('PICKING', 'RESERVA', 'GENERAL', 'IMPORTADOS')


def _bin(db, almacen, codigo, tipo_zona, activo=True):
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=activo)
    db.session.add(u)
    db.session.flush()
    return u


def _stock(db, ubicacion, producto, cantidad):
    reg = UbicacionProducto(ubicacion_id=ubicacion.id,
                            producto_id=producto.id, cantidad=cantidad)
    db.session.add(reg)
    db.session.flush()
    return reg


def _solicitud(db, almacen, producto, cantidad, usuario, codigo='ST-AVE-01'):
    s = SolicitudTraslado(
        codigo=codigo,
        bodega_origen_siesa=almacen.bodega_siesa_id,
        bodega_destino_siesa='NC1',
        estado=EstadoTraslado.EN_TRANSITO,
        solicitante_id=usuario.id,
    )
    db.session.add(s)
    db.session.flush()
    it = ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        cantidad_solicitada=cantidad, cantidad_enviada=cantidad,
    )
    db.session.add(it)
    db.session.flush()
    return s


# ── 0.1 · el descuento al despachar ────────────────────────────────────────

def test_descuento_no_toca_el_bin_de_averias(db, almacen, producto, usuario_admin):
    """El bin de averías tiene MENOS unidades, así que el `order_by` ascendente
    lo pondría primero. Debe quedar intacto."""
    ave = _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    pik = _bin(db, almacen, 'PIK-A1-C01-E01-H01', 'PICKING')
    r_ave = _stock(db, ave, producto, 3)
    r_pik = _stock(db, pik, producto, 20)

    s = _solicitud(db, almacen, producto, 5, usuario_admin)
    TrasladoService._descontar_inventario_wms(s)
    db.session.flush()

    assert r_ave.cantidad == 3, 'el traslado se llevó mercancía averiada'
    assert r_pik.cantidad == 15


def test_sin_stock_vendible_el_faltante_se_declara(db, almacen, producto, usuario_admin):
    """Si lo único que hay está averiado, no se descuenta y el faltante queda
    escrito en el movimiento. Regla 0: fallar conservador Y declararlo."""
    ave = _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    r_ave = _stock(db, ave, producto, 10)

    s = _solicitud(db, almacen, producto, 4, usuario_admin)
    TrasladoService._descontar_inventario_wms(s)
    db.session.flush()

    assert r_ave.cantidad == 10
    mov = MovimientoInventario.query.filter_by(
        tipo='SALIDA_TRASLADO', numero_documento=s.codigo).one()
    assert mov.cantidad == 0
    assert 'FALTANTE 4' in (mov.motivo or ''), mov.motivo


@pytest.mark.parametrize('zona', ZONAS_VENDIBLES)
def test_el_filtro_no_excluye_zonas_vendibles(db, almacen, producto, usuario_admin, zona):
    """Dirección contraria: el guard no puede volverse celoso y dejar de
    descontar de un bin normal."""
    b = _bin(db, almacen, f'BIN-{zona}', zona)
    reg = _stock(db, b, producto, 12)

    s = _solicitud(db, almacen, producto, 5, usuario_admin, codigo=f'ST-{zona}')
    TrasladoService._descontar_inventario_wms(s)
    db.session.flush()

    assert reg.cantidad == 7, f'zona {zona} quedó excluida y no debía'


# ── 0.2 · el catálogo de «Pedir desde» ─────────────────────────────────────

def test_stock_disponible_no_ofrece_lo_averiado(db, almacen, producto):
    ave = _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    pik = _bin(db, almacen, 'PIK-A1-C01-E01-H01', 'PICKING')
    _stock(db, ave, producto, 40)
    _stock(db, pik, producto, 60)
    db.session.commit()

    res = TrasladoService._get_stock_wms(almacen.bodega_siesa_id)
    items = {i['codigo_siesa']: i for i in res['items']}
    assert items[producto.codigo_siesa]['existencia'] == 60, \
        'las 40 averiadas se ofrecieron como disponibles'


def test_producto_solo_averiado_no_aparece_en_el_catalogo(db, almacen, producto):
    ave = _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    _stock(db, ave, producto, 40)
    db.session.commit()

    res = TrasladoService._get_stock_wms(almacen.bodega_siesa_id)
    assert producto.codigo_siesa not in {i['codigo_siesa'] for i in res['items']}


# ── 0.3 · el retorno de la reversa ─────────────────────────────────────────

def test_reversa_no_devuelve_mercancia_buena_al_bin_de_averias(db, almacen, producto, usuario_admin):
    """El bin de averías se crea PRIMERO, así que tiene el `id` más bajo del
    almacén — que es como `ub_general` elegía."""
    ave = _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    gen = _bin(db, almacen, 'PIK-B1-C01-E01-H01', 'PICKING')
    assert ave.id < gen.id, 'la premisa del test: averías es el bin más antiguo'

    s = _solicitud(db, almacen, producto, 7, usuario_admin, codigo='ST-REV-01')
    db.session.commit()

    TrasladoService.reversar_traslado(s.id, usuario_id=usuario_admin.id, motivo='prueba')
    db.session.flush()

    en_ave = UbicacionProducto.query.filter_by(
        ubicacion_id=ave.id, producto_id=producto.id).first()
    en_gen = UbicacionProducto.query.filter_by(
        ubicacion_id=gen.id, producto_id=producto.id).first()

    assert (en_ave is None or en_ave.cantidad == 0), \
        'la reversa metió mercancía buena en el bin de averías'
    assert en_gen is not None and en_gen.cantidad == 7


def test_reversa_sin_bin_vendible_dice_la_verdad(db, almacen, producto, usuario_admin):
    """Un almacén cuya única ubicación activa es la de averías no puede recibir
    la reversa — y el error tiene que nombrar esa condición, no mandar a crear
    una ubicación que ya existe."""
    _bin(db, almacen, 'AVE-A1-C01-E01-H01', 'AVERIAS')
    s = _solicitud(db, almacen, producto, 7, usuario_admin, codigo='ST-REV-02')
    db.session.commit()

    with pytest.raises(ValueError) as exc:
        TrasladoService.reversar_traslado(s.id, usuario_id=usuario_admin.id,
                                          motivo='prueba')
    assert 'vendible' in str(exc.value).lower()

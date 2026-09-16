"""El sync de Siesa **no manda sobre la zona de averías**.

## El defecto que este archivo cierra, y que yo mismo encendí

Hasta el 2026-09-16 la avería se quedaba dentro del WMS: Siesa seguía contando
esas unidades en la bodega. Ese día se conectó el aviso al ERP
(`encolar_traslado_averias`, NB1→AV1) — y eso volvió activos dos defectos que
hasta entonces eran inofensivos:

  · **La resta.** El sync descuenta de `SIESA-GENERAL` lo que ya vive en bins
    reales, para no duplicarlo. Con las unidades ya en AV1, Siesa deja de
    contarlas en la bodega, así que seguir restándolas deja el bucket vendible
    **subvaluado** por esa cantidad.

  · **El cereo.** El bulk zero pone `cantidad = 0` **sin escribir ningún
    MovimientoInventario**. Su protección por producto solo cubre lo que Siesa
    reportó en esa bodega; un SKU cuyas unidades pasaron enteras a AV1 ya no se
    reporta, queda fuera, y el cron del día siguiente **borra el bin de averías
    en silencio**. La mercancía rota desaparece sin una línea de kardex.

Es el mismo patrón que ya está anotado en el repo: un arreglo conservador que
enciende un defecto latente. Acá el «arreglo» fue avisarle a Siesa.

## La contrapartida, declarada

Entre que se registra la avería y que el job la mueve a AV1, Siesa todavía la
cuenta en la bodega y el sync ya no la resta: `SIESA-GENERAL` queda
momentáneamente alto. Es transitorio y se corrige en la siguiente corrida. Lo
otro era permanente y mudo.
"""
import pytest

from app.models.inventario import UbicacionProducto
from app.models.ubicacion import Ubicacion
from app.services.inventario_siesa_service import bins_administrados_por_el_sync


def _bin(db, almacen, codigo, tipo_zona):
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=True)
    db.session.add(u); db.session.commit()
    return u


def test_la_zona_de_averias_queda_fuera(db, almacen):
    ave = _bin(db, almacen, 'AVE-A1-EST01', 'AVERIAS')
    pik = _bin(db, almacen, 'PIK-01', 'PICKING')
    ids = {u.id for u in bins_administrados_por_el_sync(almacen.id)}
    assert pik.id in ids
    assert ave.id not in ids, 'el sync puede borrar o restar el bin de averías'


@pytest.mark.parametrize('zona', ['PICKING', 'RESERVA', 'GENERAL', 'IMPORTADOS'])
def test_las_zonas_vendibles_siguen_adentro(db, almacen, zona):
    """Dirección contraria: excluir de más dejaría al sync sin poder actualizar
    los bins que sí le corresponden — y el stock quedaría congelado."""
    u = _bin(db, almacen, f'B-{zona}', zona)
    assert u.id in {x.id for x in bins_administrados_por_el_sync(almacen.id)}


def test_se_puede_excluir_el_bucket_general(db, almacen):
    """La resta contra `SIESA-GENERAL` no puede incluirse a sí misma."""
    gen = _bin(db, almacen, Ubicacion.CODIGO_GENERAL, 'GENERAL')
    pik = _bin(db, almacen, 'PIK-01', 'PICKING')
    ids = {u.id for u in bins_administrados_por_el_sync(almacen.id, gen.id)}
    assert pik.id in ids and gen.id not in ids


def test_no_se_mezclan_almacenes(db, almacen, almacen2=None):
    from app.models.almacen import Almacen
    otro = Almacen(codigo='ALM-2', nombre='Otro', bodega_siesa_id='NC1', activo=True)
    db.session.add(otro); db.session.commit()
    mio = _bin(db, almacen, 'PIK-01', 'PICKING')
    ajeno = Ubicacion(codigo='PIK-02', almacen_id=otro.id,
                      tipo_zona='PICKING', activo=True)
    db.session.add(ajeno); db.session.commit()
    ids = {u.id for u in bins_administrados_por_el_sync(almacen.id)}
    assert mio.id in ids and ajeno.id not in ids


def test_un_bin_de_averias_con_stock_no_entra_ni_con_unidades(db, almacen, producto):
    """El caso real: el bin tiene mercancía y justamente por eso no se puede
    ni restar ni cerear."""
    ave = _bin(db, almacen, 'AVE-A1-EST01', 'AVERIAS')
    db.session.add(UbicacionProducto(ubicacion_id=ave.id,
                                     producto_id=producto.id, cantidad=25))
    db.session.commit()
    assert ave.id not in {u.id for u in bins_administrados_por_el_sync(almacen.id)}


# ── El bulk zero, ejercitado de verdad ────────────────────────────────────
#
# Los tests de arriba prueban el helper. Este prueba que el bulk zero LO USE:
# una mutación que revierta el sitio de uso tiene que ponerlo rojo, y probando
# solo el helper no lo hacía.
#
# Se siembra la descarga (`_descargar_una_pasada_custom`) en vez de mockear
# Siesa entero: la propiedad bajo prueba está aguas abajo de la descarga.

from unittest.mock import patch

from app.services import inventario_siesa_service as iss


def _sembrar_sync(db, almacen, producto):
    """Deja el almacén listo: bucket general, bin de averías con 20 y picking
    con 80."""
    gen = _bin(db, almacen, Ubicacion.CODIGO_GENERAL, 'GENERAL')
    ave = _bin(db, almacen, 'AVE-A1-EST01', 'AVERIAS')
    pik = _bin(db, almacen, 'PIK-01', 'PICKING')
    db.session.add_all([
        UbicacionProducto(ubicacion_id=ave.id, producto_id=producto.id, cantidad=20),
        UbicacionProducto(ubicacion_id=pik.id, producto_id=producto.id, cantidad=80),
    ])
    db.session.commit()
    return gen, ave, pik


def _respuesta_siesa_sin(bodega, producto, db):
    """Una respuesta REALISTA que no incluye el SKU averiado.

    Tiene que traer al menos 50 productos: `_verificar_respuesta_no_parcial`
    aborta la carga entera si Siesa devuelve menos —protección contra el zeroing
    masivo por una respuesta parcial—. Una respuesta vacía nunca llega al bulk
    zero, así que probar con ella pasaría por la razón equivocada.
    """
    from app.models.producto import Producto
    filas = {}
    for i in range(60):
        cod = f'RELLENO-{i:03d}'
        p = Producto(codigo=cod, nombre=f'Relleno {i}', codigo_siesa=cod, activo=True)
        db.session.add(p)
        filas[cod] = {'existencia': 5.0, 'comprometido': 0.0,
                      'salida_sin_conf': 0.0, 'descripcion': '', 'unidad': 'UND'}
    db.session.commit()
    return {bodega: filas}


def _correr_carga(app, db, almacen, datos):
    with patch.object(iss, '_descargar_una_pasada_custom', return_value=datos), \
         patch.object(iss, 'connekta') as ck:
        ck.bodega = almacen.bodega_siesa_id
        iss._run_carga_inicial(app, almacen.bodega_siesa_id)
    db.session.expire_all()


def test_el_bulk_zero_no_borra_el_bin_de_averias(app, db, almacen, producto):
    """El caso que el aviso a Siesa volvió real: las 20 averiadas pasaron a AV1,
    así que Siesa ya no reporta ese SKU en la bodega. Sin el filtro, el cron del
    día siguiente pone el bin en cero SIN escribir un solo movimiento."""
    _gen, ave, _pik = _sembrar_sync(db, almacen, producto)
    datos = _respuesta_siesa_sin(almacen.bodega_siesa_id, producto, db)
    _correr_carga(app, db, almacen, datos)

    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=ave.id, producto_id=producto.id).first()
    assert reg is not None and reg.cantidad == 20, (
        'el sync borró la mercancía averiada sin dejar rastro en el kardex')


def test_el_bulk_zero_si_toca_los_bins_vendibles(app, db, almacen, producto):
    """Dirección contraria: excluir de más dejaría el stock vendible congelado
    en un número que Siesa ya no respalda."""
    _gen, _ave, pik = _sembrar_sync(db, almacen, producto)
    datos = _respuesta_siesa_sin(almacen.bodega_siesa_id, producto, db)
    _correr_carga(app, db, almacen, datos)

    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=pik.id, producto_id=producto.id).first()
    assert reg is not None and reg.cantidad == 0, (
        'el bin vendible quedó congelado: el sync dejó de administrarlo')

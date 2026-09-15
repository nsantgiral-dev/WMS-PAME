"""El catálogo **no puede afirmar que lo roto es vendible**.

## El defecto que este archivo cierra

`Producto.stock_total` dice de sí mismo, en su docstring, *«Fuente única de
verdad: suma de todas las ubicaciones»* — y suma también las de zona `AVERIAS`.
Ese número sale por `to_dict()` al catálogo de la pestaña Stock.

En **esa misma pantalla**, el bloque de alertas de stock sí excluye averías
(`dashboard_service.consulta_productos_bajo_minimo`). Dos números uno al lado
del otro, alimentados por políticas opuestas, sin que nada lo diga.

El jefe lee «hay 40» y gestiona sobre 40, sin saber que 12 están rotas. No es
que el averiado sea invisible: está **mezclado**, que es peor.

## Por qué no se arregla filtrando `stock_total`

Porque entonces mentiría en la otra dirección: un «total» que no es el total.
El arreglo es **declarar la composición** — el total sigue siendo el total, y
al lado se expone cuánto de eso está averiado. Quien decide, decide viendo las
dos cifras.

## El trinquete que no lo atrapó

`tests/test_politica_vendible_unica.py` prohíbe el literal `'AVERIAS'` fuera de
`picking_service`. Atrapa a quien **copia mal** la política. No atrapa a quien
**nunca la consulta** — y `stock_total` no usaba el literal: simplemente no
hacía la pregunta. Es el mismo hueco por el que pasaron traslados y conteo.
"""
from app.models.inventario import UbicacionProducto
from app.models.ubicacion import Ubicacion


def _bin(db, almacen, codigo, tipo_zona):
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=True)
    db.session.add(u)
    db.session.flush()
    return u


def _stock(db, ubicacion, producto, cantidad, reservado=0):
    db.session.add(UbicacionProducto(
        ubicacion_id=ubicacion.id, producto_id=producto.id,
        cantidad=cantidad, reservado=reservado))
    db.session.flush()


def test_el_total_sigue_siendo_el_total(db, almacen, producto):
    _stock(db, _bin(db, almacen, 'PIK-01', 'PICKING'), producto, 28)
    _stock(db, _bin(db, almacen, 'AVE-01', 'AVERIAS'), producto, 12)
    db.session.commit()
    assert producto.stock_total == 40


def test_lo_averiado_se_declara_aparte(db, almacen, producto):
    _stock(db, _bin(db, almacen, 'PIK-01', 'PICKING'), producto, 28)
    _stock(db, _bin(db, almacen, 'AVE-01', 'AVERIAS'), producto, 12)
    db.session.commit()
    assert producto.stock_averiado == 12
    assert producto.stock_vendible == 28


def test_sin_averias_el_declarado_es_cero(db, almacen, producto):
    """Dirección contraria: no puede inventar averías donde no las hay."""
    for zona in ('PICKING', 'RESERVA', 'GENERAL', 'IMPORTADOS'):
        _stock(db, _bin(db, almacen, f'B-{zona}', zona), producto, 5)
    db.session.commit()
    assert producto.stock_averiado == 0
    assert producto.stock_vendible == producto.stock_total == 20


def test_la_suma_cierra_siempre(db, almacen, producto):
    _stock(db, _bin(db, almacen, 'PIK-01', 'PICKING'), producto, 7)
    _stock(db, _bin(db, almacen, 'AVE-01', 'AVERIAS'), producto, 3)
    _stock(db, _bin(db, almacen, 'RES-01', 'RESERVA'), producto, 11)
    db.session.commit()
    assert producto.stock_vendible + producto.stock_averiado == producto.stock_total


def test_el_catalogo_expone_las_tres_cifras(db, almacen, producto):
    _stock(db, _bin(db, almacen, 'PIK-01', 'PICKING'), producto, 28)
    _stock(db, _bin(db, almacen, 'AVE-01', 'AVERIAS'), producto, 12)
    db.session.commit()
    d = producto.to_dict()
    assert d['stock_total'] == 40
    assert d['stock_averiado'] == 12
    assert d['stock_vendible'] == 28


def test_producto_sin_stock_no_rompe(db, producto):
    assert producto.stock_total == 0
    assert producto.stock_averiado == 0
    assert producto.stock_vendible == 0


def test_el_umbral_abc_se_ancla_en_la_misma_base_que_la_alerta(db, almacen, producto):
    """`poblar_stock_minimo_desde_abc` promete en su docstring usar la misma
    suma que la alerta de bajo mínimo. La alerta excluye averías; la base del
    umbral tiene que excluirlas también, o el corte significa cosas distintas
    en los dos lados."""
    from app.services.abc_service import ABCService

    _stock(db, _bin(db, almacen, 'PIK-01', 'PICKING'), producto, 100)
    _stock(db, _bin(db, almacen, 'AVE-01', 'AVERIAS'), producto, 900)
    producto.clasificacion_abc = 'A'
    producto.stock_minimo = 0
    db.session.commit()

    ABCService.poblar_stock_minimo_desde_abc(dry_run=False)
    db.session.refresh(producto)

    # 20 % de las 100 vendibles = 20. Si contara las 900 averiadas daría 200.
    assert producto.stock_minimo == 20, (
        f'el umbral se ancló en una base que incluye averías: {producto.stock_minimo}')

"""El catálogo de Stock separa el stock por almacén.

`Producto.stock_total` suma todos los almacenes. El 2026-09-23 la pantalla
mostró 446 de PAPELSP9218 cuando NB1 tenía 110: el resto eran tiendas con datos
del ensayo. Filtrando por un almacén, lo de los otros no puede colarse; sin
filtrar, la respuesta de siempre más el desglose que explica el total.
"""
import pytest


@pytest.fixture
def tienda(db):
    from app.models.almacen import Almacen
    a = Almacen(codigo='ALM-TIENDA', nombre='Tienda Test',
                bodega_siesa_id='NC1', activo=True)
    db.session.add(a)
    db.session.commit()
    return a


def _ubicacion(db, almacen, codigo, **campos):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id, activo=True,
                  **({'tipo_zona': 'GENERAL'} | campos))
    db.session.add(u)
    db.session.commit()
    return u


def _stock(db, ubicacion, producto, cantidad, reservado=0, bloqueado=0):
    from app.models.inventario import UbicacionProducto
    db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                     cantidad=cantidad, reservado=reservado,
                                     bloqueado=bloqueado))
    db.session.commit()


@pytest.fixture
def escenario(db, almacen, tienda, producto):
    """110 en el CD, 186 en la tienda — la forma exacta del 2026-09-23."""
    _stock(db, _ubicacion(db, almacen, 'CD-GEN'), producto, 110)
    _stock(db, _ubicacion(db, tienda, 'TI-GEN'), producto, 186)
    return {'cd': almacen, 'tienda': tienda, 'producto': producto}


def _listar(client, token, **params):
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    r = client.get(f'/api/productos/?{q}', headers={'Authorization': f'Bearer {token}'})
    return r


class TestFiltrarPorAlmacen:

    def test_el_filtro_solo_cuenta_ese_almacen(self, client, jwt_token_admin, escenario):
        r = _listar(client, jwt_token_admin, almacen_id=escenario['cd'].id)
        assert r.status_code == 200, r.get_json()
        p = r.get_json()['productos'][0]
        assert p['stock_total'] == 110

    def test_detector_ciego_lo_de_la_tienda_no_se_cuela(self, client, jwt_token_admin, escenario):
        """Filtrando por la tienda, el CD no aparece — y al revés, arriba."""
        r = _listar(client, jwt_token_admin, almacen_id=escenario['tienda'].id)
        p = r.get_json()['productos'][0]
        assert p['stock_total'] == 186
        assert p['stock_vendible'] == 186

    def test_almacen_sin_stock_da_cero_no_el_total(self, client, jwt_token_admin, escenario, db):
        from app.models.almacen import Almacen
        vacio = Almacen(codigo='ALM-VACIO', nombre='Vacío', activo=True)
        db.session.add(vacio)
        db.session.commit()
        p = _listar(client, jwt_token_admin, almacen_id=vacio.id).get_json()['productos'][0]
        assert p['stock_total'] == 0 and p['stock_disponible'] == 0

    def test_un_almacen_que_no_existe_es_400_no_ceros(self, client, jwt_token_admin, escenario):
        r = _listar(client, jwt_token_admin, almacen_id=99999)
        assert r.status_code == 400


class TestSinFiltroNadaCambia:

    def test_el_total_sigue_siendo_de_todas_las_bodegas(self, client, jwt_token_admin, escenario):
        p = _listar(client, jwt_token_admin).get_json()['productos'][0]
        assert p['stock_total'] == 296

    def test_trae_el_desglose_que_explica_el_total(self, client, jwt_token_admin, escenario):
        p = _listar(client, jwt_token_admin).get_json()['productos'][0]
        por = {a['almacen']: a['stock_total'] for a in p['stock_por_almacen']}
        assert por == {'ALM-TEST': 110, 'ALM-TIENDA': 186}
        assert sum(por.values()) == p['stock_total']


class TestLaSumaPorAlmacenCuadraConElModelo:
    """Una política, una función: la agregación SQL y las propiedades de
    `Producto` tienen que dar lo mismo, campo por campo."""

    def test_averias_y_disponible_por_almacen(self, db, almacen, tienda, producto):
        from app.services.picking_service import campos_ubicacion_averias
        from app.services.stock_por_almacen import CAMPOS_STOCK, stock_por_almacen
        _stock(db, _ubicacion(db, almacen, 'CD-GEN'), producto, 50, reservado=8, bloqueado=2)
        _stock(db, _ubicacion(db, almacen, 'CD-AVE', **campos_ubicacion_averias()), producto, 7)
        _stock(db, _ubicacion(db, tienda, 'TI-GEN'), producto, 20, reservado=25)

        por = stock_por_almacen([producto.id])[producto.id]
        cd, ti = por[almacen.id], por[tienda.id]
        assert (cd['stock_total'], cd['stock_vendible'], cd['stock_averiado']) == (57, 50, 7)
        assert cd['stock_disponible'] == 40 + 7       # 50-8-2 y los 7 averiados libres
        assert ti['stock_disponible'] == 0            # reservado > cantidad: max(0, …) por fila

        db.session.refresh(producto)
        for campo in CAMPOS_STOCK:
            assert cd[campo] + ti[campo] == getattr(producto, campo), campo

    def test_sin_ids_no_consulta(self, db):
        from app.services.stock_por_almacen import stock_por_almacen
        assert stock_por_almacen([]) == {}

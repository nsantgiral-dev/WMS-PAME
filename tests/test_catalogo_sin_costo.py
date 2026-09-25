"""La tienda ve el catálogo, sin costos (decisión del 2026-09-25).

**Qué pasaba.** La pantalla de la tienda busca un producto para recibir su OC
en `/api/productos/`, que le contestaba 403 (el catálogo era solo del personal
de almacén). Y la búsqueda —la de la tienda y la de las bonificaciones de
recepción— mandaba `?search=`, un parámetro que el servidor no lee: recibía el
catálogo entero y registraba **el primer producto**, fuera cual fuera.

**La clase**: *el costo de compra en una respuesta a quien no decide compras*.
Una política (`ve_costo_de_compra`: gestión y compras) y una forma del
producto en el catálogo (`productos._producto_para`); quien entra lo dice
`Roles.CATALOGO` (lista blanca: almacén + tienda).

Tres capas: por HTTP y por rol, quién entra y quién recibe `precio_compra`;
la búsqueda exacta; y por AST, ningún `to_dict()` de producto en el catálogo
fuera de la política.

**Lo que NO cubre:** otros endpoints que devuelven un producto entero a
personal de almacén (layout, stock, conteo) — no son el catálogo y siguen
como estaban.
"""
import ast
import uuid
from pathlib import Path

import pytest

from tests.test_cartera_retencion import _jwt, _usuario

RAIZ = Path(__file__).resolve().parents[1]

#: Rol → (entra al catálogo, ve el costo). **A mano.**
ESPERADO = {
    'admin': (True, True), 'supervisor': (True, True), 'jefe_almacen': (True, True),
    'gerente': (True, True), 'compras': (True, True),
    'operario': (True, False), 'empacador': (True, False), 'recepcionista': (True, False),
    'picker_traslado': (True, False), 'packer_traslado': (True, False),
    'tienda': (True, False),
    'conductor': (False, False), 'control_flota': (False, False),
    'liquidador': (False, False), 'lider_cartera': (False, False),
}


def _producto(db, codigo=None, **kw):
    from app.models.producto import Producto
    p = Producto(codigo=codigo or f'P{uuid.uuid4().hex[:6]}', nombre='Cuaderno', activo=True,
                 precio_compra=1234, precio_venta=2000, **kw)
    db.session.add(p)
    db.session.commit()
    return p


class TestQuienVeQue:

    @pytest.mark.parametrize('rol', sorted(ESPERADO))
    def test_por_rol(self, app, client, db, almacen, rol):
        p = _producto(db)
        h = _jwt(app, _usuario(db, rol=rol))
        entra, costo = ESPERADO[rol]
        for url in ('/api/productos/', f'/api/productos/{p.id}'):
            r = client.get(url, headers=h)
            assert (r.status_code == 200) is entra, (rol, url, r.status_code)
            if entra:
                d = r.get_json()
                fila = d['productos'][0] if 'productos' in d else d
                assert ('precio_compra' in fila) is costo, (rol, url)
                assert fila['precio_venta'] == 2000

    def test_la_lista_blanca(self):
        from app.routes._auth_helpers import Roles
        assert Roles.TIENDA in Roles.CATALOGO
        for rol in (Roles.CONDUCTOR, Roles.CONTROL_FLOTA, Roles.LIQUIDADOR, Roles.LIDER_CARTERA):
            assert rol not in Roles.CATALOGO
        assert Roles.TIENDA not in Roles.VEN_COSTO_DE_COMPRA
        assert Roles.OPERARIO not in Roles.VEN_COSTO_DE_COMPRA


class TestLaBusquedaExacta:

    def test_trae_solo_el_del_codigo(self, app, client, db, almacen):
        _producto(db, codigo='AAA1')
        buscado = _producto(db, codigo='BBB2', codigo_barras='7701234567890')
        h = _jwt(app, _usuario(db, rol='tienda'))
        for codigo in ('BBB2', '7701234567890'):
            d = client.get(f'/api/productos/?codigo={codigo}', headers=h).get_json()
            assert [x['id'] for x in d['productos']] == [buscado.id], codigo
        d = client.get('/api/productos/?codigo=NOEXISTE', headers=h).get_json()
        assert d['productos'] == []

    def test_un_parecido_no_es_el_codigo(self, app, client, db, almacen):
        _producto(db, codigo='BBB22')
        h = _jwt(app, _usuario(db, rol='tienda'))
        assert client.get('/api/productos/?codigo=BBB2', headers=h).get_json()['productos'] == []


class TestLaPantallaNoTomaElPrimeroQueSeParezca:
    """`productoPorCodigoExacto` (app.js, Node real): uno se usa; ninguno o
    varios no se registran — se dice."""

    @pytest.mark.parametrize('productos,esperado,aviso', [
        ([{'id': 7}], 7, None),
        ([], None, 'No hay ningún producto'),
        ([{'id': 7}, {'id': 8}], None, 'corresponde a 2 productos'),
    ])
    def test_uno_ninguno_varios(self, tmp_path, productos, esperado, aviso):
        from tests.test_sin_codigos_en_pantalla import _node
        # app.js trae su propio get(): se reemplaza después de cargarlo, y la
        # URL pedida queda anotada (tiene que ser la del código exacto).
        out = _node(tmp_path, ['util.js', 'app.js'], {},
                    """const avisos = [], urls = [];
                       alerta = (m) => avisos.push(m);
                       get = async (u) => { urls.push(u); return { productos: PRODUCTOS }; };
                       const p = await productoPorCodigoExacto('X 1');
                       return { id: p ? p.id : null, avisos, urls };""",
                    globales={'PRODUCTOS': productos})
        assert out['urls'] == ['/api/productos/?codigo=X%201']
        assert out['id'] == esperado
        assert (aviso is None and out['avisos'] == []) or any(aviso in a for a in out['avisos'])


# ═════════════════════════════════════════════════════════════════════════════
# AST: el catálogo no arma un producto por fuera de la política
# ═════════════════════════════════════════════════════════════════════════════

def to_dict_fuera_de_la_politica(fuente: str):
    """Funciones que llaman `<algo>.to_dict()` y no son `_producto_para`."""
    out = []
    for fn in ast.walk(ast.parse(fuente)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != '_producto_para':
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'to_dict'):
                    out.append(fn.name)
                    break
    return out


class TestElCatalogoPasaPorLaPolitica:

    def test_productos_py(self):
        fuente = (RAIZ / 'app' / 'routes' / 'productos.py').read_text(encoding='utf-8')
        assert to_dict_fuera_de_la_politica(fuente) == []

    def test_meta_ve_y_no_marca_lo_sano(self):
        src = ("def a(p):\n    return p.to_dict()\n"
               "def _producto_para(u, p):\n    return p.to_dict()\n"
               "def b(p):\n    '''p.to_dict()'''\n    return _producto_para(None, p)\n")
        assert to_dict_fuera_de_la_politica(src) == ['a']

    def test_piso_la_politica_existe_y_se_usa(self):
        fuente = (RAIZ / 'app' / 'routes' / 'productos.py').read_text(encoding='utf-8')
        llamadas = [n for n in ast.walk(ast.parse(fuente))
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == '_producto_para']
        assert len(llamadas) >= 4, 'listar, obtener, crear y actualizar pasan por la política'

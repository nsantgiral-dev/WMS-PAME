"""
Un código de barras contesta dos preguntas, y el sistema adivinaba la segunda.

    es_empaque = (codigo == codigo_barras_empaque) OR factor > 1

*Qué producto* y *qué unidad de empaque* son preguntas distintas. Cuando el
código solo puede contestar la primera, el `or` **afirmaba** la segunda.

## El caso caro

`tienda_oc_service` escribe el `f421_factor` de **una OC** sobre la fila global
del `Producto`. Después el recepcionista del CD escanea la EAN de **unidad
suelta** de ese SKU y el sistema registra `factor` unidades, con un toast que
dice «Empaque escaneado → +12 UND». Sobre-recepción silenciosa en el CD por un
dato que escribió una tienda.

## Por qué el `or` NO se puede cambiar por `and`

Cuando un SKU tiene `factor > 1` y **no** tiene `codigo_barras_empaque`
poblado —el proveedor pegó la EAN de unidad en la caja— el `or` es lo único que
hace que la caja sume 12. Con `and`, ese operario escanea **doce veces por
caja**: se arreglaría la sobre-recepción y se rompería la recepción normal el
mismo día.

Es *medir antes de imponer*: el diagnóstico era correcto y la solución obvia
era incorrecta.

## Lo que sí se puede

Dejar de decidir por el operario cuando el dato es ambiguo. `_buscar_producto`
**ya sabía** por qué campo entró el código y lo tiraba. Ahora lo devuelve, y
`None` significa «no sé» — la pantalla pregunta.

`mobile_service._es_escaneo_empaque` ya exigía las dos condiciones: era la
política correcta, en otro archivo. Queda como el modelo, no como la excepción.
"""
import pytest

from app.models.producto import Producto


@pytest.fixture
def sku(db):
    def _crear(**kw):
        base = dict(codigo='SKU-EMP', nombre='Producto de prueba',
                    unidad_medida='UND', factor_conversion=1)
        base.update(kw)
        p = Producto(**base)
        db.session.add(p)
        db.session.commit()
        return p
    return _crear


def _consultar(client, token, codigo):
    r = client.get(f'/api/siesa/producto/{codigo}',
                   headers={'Authorization': f'Bearer {token}'})
    return r.get_json()


class TestLaProcedenciaDelEscaneo:

    def test_el_ean_de_caja_afirma_empaque(self, client, jwt_token, sku):
        sku(codigo='S1', codigo_barras='111', codigo_barras_empaque='999',
            factor_conversion=12)
        d = _consultar(client, jwt_token, '999')
        assert d['es_empaque'] is True
        assert d['empaque_procedencia'] == 'ean_empaque'

    def test_el_ean_de_unidad_con_ean_de_caja_poblado_afirma_unidad(
            self, client, jwt_token, sku):
        """**El agujero que se cierra.** Antes el `factor > 1` decía «caja»
        aunque se hubiera escaneado la unidad suelta."""
        sku(codigo='S2', codigo_barras='222', codigo_barras_empaque='888',
            factor_conversion=12)
        d = _consultar(client, jwt_token, '222')
        assert d['es_empaque'] is False, (
            'escanear la EAN de unidad registró una caja de 12 — es la '
            'sobre-recepción del CD por un factor que escribió una tienda')

    def test_sin_ean_de_caja_NO_se_afirma_nada(self, client, jwt_token, sku):
        """El caso donde el `and` habría roto el CD: el proveedor pegó la EAN de
        unidad en la caja. El sistema no puede saberlo — y quien sí puede es el
        que tiene la caja en la mano."""
        sku(codigo='S3', codigo_barras='333', codigo_barras_empaque=None,
            factor_conversion=12)
        d = _consultar(client, jwt_token, '333')
        assert d['es_empaque'] is None, (
            'volvió a decidir por el operario sobre un dato ambiguo')
        assert d['empaque_procedencia'] == 'ambiguo_sin_ean_empaque'
        assert d['factor_conversion'] == 12

    def test_un_sku_sin_factor_es_unidad(self, client, jwt_token, sku):
        sku(codigo='S4', codigo_barras='444', factor_conversion=1)
        d = _consultar(client, jwt_token, '444')
        assert d['es_empaque'] is False
        assert d['empaque_procedencia'] == 'unidad'

    def test_el_codigo_wms_tampoco_afirma(self, client, jwt_token, sku):
        """Buscar por código interno no dice nada sobre qué hay en la mano."""
        sku(codigo='S5', codigo_barras='555', codigo_barras_empaque=None,
            factor_conversion=6)
        d = _consultar(client, jwt_token, 'S5')
        assert d['es_empaque'] is None


class TestElOrNoVuelveAAdivinar:

    def test_ninguna_rama_afirma_empaque_solo_por_el_factor(self):
        """Por AST: que no exista un `or factor > 1` decidiendo `es_empaque`.

        Un detector de texto se atraparía en este propio docstring — pasó seis
        veces en este repo.
        """
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/routes/siesa.py').read_text())
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == 'buscar_producto')
        ors = [n for n in ast.walk(fn)
               if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)]
        for o in ors:
            texto = ast.dump(o)
            assert not ('factor' in texto and 'codigo_barras_empaque' in texto), (
                'volvió el `or factor > 1`: un solo código vuelve a contestar '
                'dos preguntas, y la segunda se adivina')

    def test_la_busqueda_devuelve_por_que_campo_entro(self):
        from app.routes.siesa import _buscar_producto_con_origen
        assert callable(_buscar_producto_con_origen)

    def test_la_politica_de_mobile_sigue_exigiendo_las_dos(self):
        """Era la correcta desde antes, en otro archivo. Si alguien la afloja
        para «unificar», el CD vuelve a sobre-recibir."""
        from app.models.producto import Producto as _P
        from app.services.mobile_service import MobileService
        p = _P(codigo='X', nombre='x', unidad_medida='UND',
               factor_conversion=12, codigo_barras_empaque=None)
        assert MobileService._es_escaneo_empaque(p, '123') is False


class TestElBacklogDeEAN:
    """Cada pregunta al operario es una fila acá. Cerrarla la elimina."""

    def test_lista_los_sku_ambiguos(self, client, jwt_token_admin, sku):
        sku(codigo='B1', codigo_barras='611', factor_conversion=12,
            codigo_barras_empaque=None)
        sku(codigo='B2', codigo_barras='622', factor_conversion=12,
            codigo_barras_empaque='623')
        r = client.get('/api/siesa/skus-sin-ean-empaque',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        codigos = {s['codigo'] for s in d['skus']}
        assert 'B1' in codigos and 'B2' not in codigos

    def test_declara_si_trunco(self, client, jwt_token_admin):
        r = client.get('/api/siesa/skus-sin-ean-empaque',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert 'truncado' in r.get_json()

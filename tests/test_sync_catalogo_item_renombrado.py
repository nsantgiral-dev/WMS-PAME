"""
El sync de catálogo sigue un ítem por su NÚMERO (`f120_id`), no solo por su
referencia.

Producción, 2026-10-01: Siesa renombró el ítem 0017173 de `PAPELSP8985` a
`P197_006`. El sync buscaba por referencia, no encontró `P197_006` y **creó otro
producto**; el viejo —con las ubicaciones, los empaques y los conteos— quedó
apuntando a una referencia que ya no existe en Siesa, y todo conteo sobre él
quedó bloqueado («no tiene foto de Siesa») para siempre.

Se mockea solo la frontera HTTP (`connekta.get_items_catalogo`), con filas de
la forma del contrato (`API_v2_Items.docx`, que declara `f120_id`).
"""
import pytest

from tests.test_sync_catalogo_completitud import _correr, _fila


@pytest.fixture(autouse=True)
def _entorno_del_sync(monkeypatch):
    from app.services import siesa_sync_service as svc
    monkeypatch.setenv('SYNC_PAGE_DELAY_S', '0')
    limpio = {'en_curso': False, 'ultimo_inicio': None,
              'ultimo_resultado': None, 'ultimo_error': None}
    svc._sync_estado.update(limpio)
    yield
    svc._sync_estado.update(limpio)


def _producto(db, codigo, codigo_siesa=None, id_item=None, nombre='Repuesto'):
    from app.models.producto import Producto
    p = Producto(codigo=codigo, nombre=nombre,
                 codigo_siesa=codigo_siesa if codigo_siesa is not None else codigo,
                 id_item_siesa=id_item, activo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _todos(db):
    from app.models.producto import Producto
    return {p.codigo: p for p in Producto.query.order_by(Producto.id).all()}


class TestElNumeroDelItemMandaSobreLaReferencia:

    def test_un_renombre_en_siesa_sigue_sobre_el_mismo_producto(self, app, db):
        """El caso de producción: el producto viejo ya conoce su número."""
        viejo = _producto(db, 'PAPELSP8985', id_item=17173)
        r = _correr(app, {1: [_fila('P197_006', f120_id=17173)]})

        prods = _todos(db)
        assert list(prods) == ['PAPELSP8985'], 'no se crea un segundo producto'
        p = prods['PAPELSP8985']
        assert p.id == viejo.id
        assert p.codigo_siesa == 'P197_006'
        # `codigo` no se renombra: lo usan pedidos, picking y packing.
        assert p.codigo == 'PAPELSP8985'
        assert r['renombradas'] == 1
        assert r['renombradas_detalle'] == [{'item': 17173, 'producto': viejo.id,
                                             'antes': 'PAPELSP8985', 'ahora': 'P197_006'}]
        assert r['creados'] == 0

    def test_la_primera_vuelta_anota_el_numero_y_la_segunda_sigue_el_renombre(self, app, db):
        """El producto de antes de m052itemsiesa no tiene número: el sync lo
        aprende de su referencia, y con eso el renombre siguiente ya no rompe."""
        _producto(db, 'PAPELSP8985')
        _correr(app, {1: [_fila('PAPELSP8985', f120_id=17173)]})
        assert _todos(db)['PAPELSP8985'].id_item_siesa == 17173

        r = _correr(app, {1: [_fila('P197_006', f120_id=17173)]})
        prods = _todos(db)
        assert list(prods) == ['PAPELSP8985']
        assert prods['PAPELSP8985'].codigo_siesa == 'P197_006'
        assert r['renombradas'] == 1

    def test_el_numero_viaja_como_texto_con_ceros(self, app, db):
        """Siesa muestra «0017173»; el número es el mismo."""
        _producto(db, 'PAPELSP8985', id_item=17173)
        _correr(app, {1: [_fila('P197_006', f120_id='0017173')]})
        assert list(_todos(db)) == ['PAPELSP8985']

    def test_un_producto_nuevo_nace_con_su_numero(self, app, db):
        _correr(app, {1: [_fila('NUEVO-1', f120_id=555)]})
        assert _todos(db)['NUEVO-1'].id_item_siesa == 555

    def test_sin_f120_id_se_sigue_por_la_referencia_como_antes(self, app, db):
        p = _producto(db, 'REF-1')
        r = _correr(app, {1: [_fila('REF-1')]})
        prods = _todos(db)
        assert list(prods) == ['REF-1'] and prods['REF-1'].id == p.id
        assert prods['REF-1'].id_item_siesa is None
        assert r['renombradas'] == 0 and r['conflictos_referencia'] == 0


class TestLoQueElSyncNoUnificaSolo:

    def test_dos_productos_para_un_item_se_reportan_y_no_se_mezclan(self, app, db):
        """El duplicado ya existe (el sync viejo creó el segundo). El sync no
        mueve stock, empaques ni conteos: lo reporta y una persona unifica."""
        viejo = _producto(db, 'PAPELSP8985', id_item=17173)
        nuevo = _producto(db, 'P197_006')
        r = _correr(app, {1: [_fila('P197_006', f120_id=17173)]})

        prods = _todos(db)
        assert prods['PAPELSP8985'].codigo_siesa == 'PAPELSP8985', \
            'el viejo no toma la referencia de otro producto'
        assert prods['P197_006'].id_item_siesa is None, \
            'el número no se duplica en el segundo producto'
        assert r['conflictos_referencia'] == 1
        assert r['conflictos_referencia_detalle'] == [{
            'item': 17173, 'referencia': 'P197_006',
            'producto_con_item': viejo.id, 'producto_con_referencia': nuevo.id}]
        assert r['renombradas'] == 0

    def test_una_referencia_que_ahora_es_de_otro_item_no_pisa_al_producto(self, app, db):
        """Siesa le quitó «A» al ítem 5 y se la dio al ítem 9: el producto del
        ítem 5 no se convierte en el del 9."""
        a = _producto(db, 'A', id_item=5, nombre='El de siempre')
        _correr(app, {1: [_fila('A', f120_id=9)]})

        prods = _todos(db)
        assert prods['A'].id == a.id and prods['A'].nombre == 'El de siempre'
        assert prods['A'].id_item_siesa == 5
        # El ítem 9 nace aparte; `codigo` es UNIQUE, así que lleva el número.
        assert prods['A-9'].id_item_siesa == 9
        assert prods['A-9'].codigo_siesa == 'A'


class TestElContratoDeclaraElNumero:

    def test_f120_id_esta_en_el_docx_de_api_v2_items(self):
        """El sync lee `f120_id`: el trinquete del contrato lo exige declarado."""
        import pathlib
        import re
        import zipfile
        docx = (pathlib.Path(__file__).resolve().parents[1]
                / 'docs' / 'siesa-specs' / 'API_v2_Items.docx')
        texto = re.sub('<[^>]+>', ' ',
                       zipfile.ZipFile(docx).read('word/document.xml').decode('utf-8'))
        assert 'f120_id' in set(re.findall(r'f120_[a-z_0-9]+', texto))

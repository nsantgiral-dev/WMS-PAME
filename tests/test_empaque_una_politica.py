"""Paquetes al pedir (2026-10-02): una política para «¿en qué paquete viene?».

Decisiones del dueño: si el producto viene en paquete la casilla arranca en
paquetes (salvo que no alcance uno completo), se ve cuántas unidades trae y
cuántas suman los elegidos, y se pueden pedir unidades sueltas junto con los
paquetes. El inventario y Siesa siguen en unidades.

La clase que se cierra: *dos funciones contestaban «en qué paquete viene este
producto» con dos políticas* — `traslado_service._resolver_empaque` caía a
`producto_empaques`; el HUD de picking leía solo `Producto.factor_conversion`,
que está en 1 para casi todo el catálogo (medido en producción el 2026-10-02:
6 productos contra 234 con su paquete en `producto_empaques`). Ahora contesta
`empaque_producto.empaque_de` y el trinquete de abajo impide que una función
nueva lea el factor o la unidad de empaque por su cuenta.
"""
import ast
import pathlib

import pytest

from app.extensions import db
from app.models.producto import Producto
from app.models.producto_empaque import ProductoEmpaque
from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
from app.services import empaque_producto as ep

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _producto(codigo, **kw):
    p = Producto(codigo=codigo, nombre=kw.pop('nombre', f'Producto {codigo}'),
                 codigo_siesa=kw.pop('codigo_siesa', codigo),
                 unidad_negocio_id=kw.pop('unidad_negocio_id', '001'), **kw)
    db.session.add(p)
    db.session.flush()
    return p


def _paquete(producto, unidad, factor, codigo='7700000000017', origen='SIESA_GS1', activo=True):
    e = ProductoEmpaque(producto_id=producto.id, referencia_item=producto.codigo_siesa,
                        codigo_barras=codigo, unidad_medida=unidad,
                        factor_conversion=factor, origen=origen, activo=activo)
    db.session.add(e)
    db.session.flush()
    return e


# ── La política ──────────────────────────────────────────────────────────────

class TestLaPolitica:
    def test_el_paquete_de_siesa_manda(self, app, db):
        p = _producto('DORI07')
        _paquete(p, 'PQ', 12)
        e = ep.empaque_de(p)
        assert (e.unidad, e.factor, e.fuente) == ('PQ', 12, ep.FUENTE_EMPAQUES)

    def test_con_varios_gana_el_que_siesa_declara_para_el_item(self, app, db):
        p = _producto('VARIOS', unidad_empaque='CAJ')
        _paquete(p, 'PQ', 12, codigo='7700000000101')
        _paquete(p, 'CAJ', 144, codigo='7700000000102')
        assert ep.empaque_de(p).unidad == 'CAJ'

    def test_sin_declarado_gana_el_menor(self, app, db):
        p = _producto('MENOR')
        _paquete(p, 'CAJ', 144, codigo='7700000000201')
        _paquete(p, 'PQ', 12, codigo='7700000000202')
        assert (ep.empaque_de(p).unidad, ep.empaque_de(p).factor) == ('PQ', 12)

    def test_una_paca_del_wms_no_es_la_unidad_comercial(self, app, db):
        p = _producto('PACA')
        _paquete(p, 'PACA', 5, codigo='LPN-0001', origen='WMS_LPN')
        assert ep.empaque_de(p) is None

    def test_inactivo_factor_uno_y_und_no_cuentan(self, app, db):
        p = _producto('NADA')
        _paquete(p, 'PQ', 12, codigo='7700000000301', activo=False)
        _paquete(p, 'PQ', 1, codigo='7700000000302')
        _paquete(p, 'UND', 6, codigo='7700000000303')
        assert ep.empaque_de(p) is None

    def test_sin_filas_cae_al_dato_viejo_del_producto(self, app, db):
        p = _producto('VIEJO', unidad_empaque='PQ', factor_conversion=6)
        e = ep.empaque_de(p)
        assert (e.unidad, e.factor, e.fuente) == ('PQ', 6, ep.FUENTE_PRODUCTO)

    def test_el_producto_que_declara_pq_sin_factor_no_viene_en_paquete(self, app, db):
        # 260 en producción: unidad_empaque PQ y factor 1. Sin factor no se
        # inventa uno (Regla 0).
        assert ep.empaque_de(_producto('SINFACTOR', unidad_empaque='PQ', factor_conversion=1)) is None

    def test_el_lote_da_lo_mismo_que_uno_por_uno(self, app, db):
        a, b, c = _producto('A1'), _producto('B1', unidad_empaque='PQ', factor_conversion=10), _producto('C1')
        _paquete(a, 'PQ', 12, codigo='7700000000401')
        lote = ep.empaques_de([a, b, c])
        assert lote == {pid: e for pid, e in ((x.id, ep.empaque_de(x)) for x in (a, b, c)) if e}
        assert ep.empaques_por_id([a.id, b.id, c.id]) == lote

    def test_el_traslado_a_siesa_usa_su_propia_regla(self, app, db):
        """Lo que va a Siesa NO es `empaque_de` (2026-10-02, revisión): una
        fila sin código de barras (Paso E del sync) no cambia el payload."""
        from app.services.traslado_service import _resolver_empaque
        p = _producto('TR1')
        _paquete(p, 'PQ', 12, codigo='7700000000501')
        assert _resolver_empaque(p) == ('PQ', 12)
        q = _producto('TR2')
        _paquete(q, 'PACA', 5, codigo='LPN-0002', origen='WMS_LPN')
        assert _resolver_empaque(q) == ('', 1)
        r = _producto('TR3')
        _paquete(r, 'PQ', 12, codigo=None)
        assert ep.empaque_de(r).factor == 12, 'para pedir sí viene en paquete'
        assert _resolver_empaque(r) == ('', 1), 'a Siesa sigue en unidades'

    def test_a_siesa_el_dato_del_producto_manda_primero(self, app, db):
        """La regla de antes, idéntica: Producto con factor > 1 antes que
        producto_empaques."""
        p = _producto('TR4', unidad_empaque='CAJ', factor_conversion=24)
        _paquete(p, 'PQ', 12, codigo='7700000000502')
        assert ep.unidad_para_siesa(p) == ('CAJ', 24)
        assert ep.empaque_de(p).unidad == 'PQ'

    def test_el_hud_de_picking_le_pregunta_a_la_misma(self, app, db):
        from app.services.mobile_service import MobileService
        p = _producto('HUD1')  # factor_conversion del producto en 1 (lo normal)
        _paquete(p, 'PQ', 12, codigo='7700000000601')
        assert MobileService._empaque_hud(p) == {'factor_conversion': 12, 'unidad_empaque': 'PQ'}
        assert MobileService._empaque_hud(_producto('HUD2')) == {'factor_conversion': 1, 'unidad_empaque': ''}


# ── Paquetes + sueltas → unidades ─────────────────────────────────────────────

class TestUnidadesDeLinea:
    PQ12 = ep.Empaque('PQ', 12, ep.FUENTE_EMPAQUES)

    def test_paquetes_y_sueltas_dan_el_total(self):
        assert ep.unidades_de_linea({'paquetes': 2, 'sueltas': 5}, self.PQ12, 'cantidad_solicitada') == (29, 2, 5)

    def test_el_total_de_la_pantalla_tiene_que_cuadrar(self):
        assert ep.unidades_de_linea({'paquetes': 2, 'sueltas': 5, 'cantidad_solicitada': 29},
                                    self.PQ12, 'cantidad_solicitada')[0] == 29
        with pytest.raises(ValueError, match='no cuadra'):
            ep.unidades_de_linea({'paquetes': 2, 'sueltas': 5, 'cantidad_solicitada': 25},
                                 self.PQ12, 'cantidad_solicitada')

    def test_paquetes_de_un_producto_que_no_viene_en_paquete(self):
        with pytest.raises(ValueError, match='no viene en paquete'):
            ep.unidades_de_linea({'paquetes': 1, 'sueltas': 0}, None, 'cantidad_solicitada')
        assert ep.unidades_de_linea({'paquetes': 0, 'sueltas': 4}, None, 'cantidad_solicitada') == (4, 0, 4)

    def test_solo_unidades_como_siempre(self):
        assert ep.unidades_de_linea({'cantidad_solicitada': 7}, self.PQ12, 'cantidad_solicitada') == (7, None, None)

    @pytest.mark.parametrize('malo', [-1, 'x', True, 1.5])
    def test_basura_se_rechaza(self, malo):
        with pytest.raises(ValueError):
            ep.unidades_de_linea({'paquetes': malo, 'sueltas': 0}, self.PQ12, 'c')


# ── Pedir: el servidor recalcula ──────────────────────────────────────────────

def _crear(items, **kw):
    from app.services.traslado_service import TrasladoService
    from app.models.usuario import Usuario
    u = Usuario.query.first()
    return TrasladoService.crear_solicitud(
        solicitante_id=u.id, bodega_destino='PC1', nombre_punto_venta='Pitalito Centro',
        items=items, bodega_origen='NB1', **kw)


class TestPedir:
    def test_se_guarda_en_unidades_y_como_se_pidio(self, app, db, usuario_admin):
        p = _producto('PEDIR1')
        _paquete(p, 'PQ', 12, codigo='7700000000701')
        s = _crear([{'producto_id': p.id, 'paquetes': 2, 'sueltas': 5, 'cantidad_solicitada': 29}])
        it = s.items[0]
        assert it.cantidad_solicitada == 29
        assert (it.paquetes_pedidos, it.sueltas_pedidas, it.factor_al_pedir,
                it.unidad_empaque_al_pedir) == (2, 5, 12, 'PQ')
        d = s.to_dict()['items'][0]
        assert d['empaque'] == {'unidad': 'PQ', 'factor': 12, 'fuente': ep.FUENTE_EMPAQUES}
        assert d['pedido_como'] == {'paquetes': 2, 'sueltas': 5, 'factor': 12, 'unidad': 'PQ'}

    def test_el_total_que_no_cuadra_no_crea_nada(self, app, db, usuario_admin):
        p = _producto('PEDIR2')
        _paquete(p, 'PQ', 12, codigo='7700000000801')
        with pytest.raises(ValueError, match='no cuadra'):
            _crear([{'producto_id': p.id, 'paquetes': 2, 'sueltas': 0, 'cantidad_solicitada': 2}])
        db.session.rollback()
        assert ItemSolicitudTraslado.query.count() == 0

    def test_solo_unidades_sigue_igual_y_sin_como_se_pidio(self, app, db, usuario_admin):
        p = _producto('PEDIR3')
        _paquete(p, 'PQ', 12, codigo='7700000000901')
        s = _crear([{'producto_id': p.id, 'cantidad_solicitada': 7}])
        it = s.items[0]
        assert it.cantidad_solicitada == 7 and it.factor_al_pedir is None
        assert s.to_dict()['items'][0]['pedido_como'] is None

    def test_cero_unidades_no_es_un_pedido(self, app, db, usuario_admin):
        p = _producto('PEDIR4')
        _paquete(p, 'PQ', 12, codigo='7700000001001')
        with pytest.raises(ValueError, match='al menos una unidad'):
            _crear([{'producto_id': p.id, 'paquetes': 0, 'sueltas': 0}])

    def test_aprobar_en_paquetes_y_no_mas_de_lo_pedido(self, app, db, usuario_admin):
        from app.services.traslado_service import TrasladoService
        p = _producto('APR1')
        _paquete(p, 'PQ', 12, codigo='7700000001101')
        s = _crear([{'producto_id': p.id, 'paquetes': 2, 'sueltas': 5}])
        s.estado = 'ENVIADA'
        db.session.commit()
        it = s.items[0]
        with pytest.raises(ValueError, match='más de lo pedido'):
            TrasladoService.aprobar_solicitud(
                s.id, usuario_admin.id,
                items_aprobados=[{'id': it.id, 'paquetes': 3, 'sueltas': 0}])
        db.session.rollback()
        TrasladoService.aprobar_solicitud(
            s.id, usuario_admin.id,
            items_aprobados=[{'id': it.id, 'paquetes': 2, 'sueltas': 0, 'cantidad_aprobada': 24}])
        assert db.session.get(ItemSolicitudTraslado, it.id).cantidad_aprobada == 24


# ── Verlo: la lista de Pedir trae el paquete ──────────────────────────────────

class TestStockDisponibleTraeElPaquete:
    def test_cada_fila_trae_su_paquete_sin_tocar_el_cache(self, client, jwt_token_admin, app, db):
        from unittest.mock import patch
        p = _producto('STK1')
        _paquete(p, 'PQ', 12, codigo='7700000001201')
        q = _producto('STK2')
        db.session.commit()
        cache = {'items': [
            {'codigo_siesa': 'STK1', 'nombre': 'uno', 'producto_id': p.id, 'disponible': 1368},
            {'codigo_siesa': 'STK2', 'nombre': 'dos', 'producto_id': q.id, 'disponible': 5},
        ], 'bodega': 'NB1', 'total': 2}
        with patch('app.services.traslado_service.TrasladoService.get_stock_disponible',
                   return_value=cache):
            r = client.get('/api/traslados/stock-disponible?bodega=NB1&completo=true',
                           headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        por = {i['codigo_siesa']: i for i in r.get_json()['items']}
        assert por['STK1']['empaque'] == {'unidad': 'PQ', 'factor': 12, 'fuente': ep.FUENTE_EMPAQUES}
        assert por['STK2']['empaque'] is None
        assert 'empaque' not in cache['items'][0], 'la respuesta no puede mutar el cache compartido'


# ── Datos: el sync y lo que hay que revisar ──────────────────────────────────

class TestPaquetesSinCodigo:
    def _prods(self, *ps):
        from collections import namedtuple
        PD = namedtuple('PD', ['id', 'codigo_siesa', 'codigo'])
        return {x.codigo_siesa: PD(x.id, x.codigo_siesa, x.codigo) for x in ps}, {}

    def test_q35_sin_codigo_se_guarda_sin_codigo(self, app, db):
        from app.services.empaques_sync_service import _sincronizar_paquetes_sin_codigo
        p = _producto('Q35A')
        por_siesa, por_codigo = self._prods(p)
        r = _sincronizar_paquetes_sin_codigo({('Q35A', 'PQ'): 12, ('Q35A', 'UND'): 1},
                                             por_siesa, por_codigo, set())
        assert (r['insertados'], r['desactivados']) == (1, 0)
        e = ProductoEmpaque.query.filter_by(producto_id=p.id).one()
        assert e.codigo_barras is None and (e.unidad_medida, e.factor_conversion) == ('PQ', 12)
        assert ep.empaque_de(p).factor == 12

    def test_con_codigo_no_se_duplica_y_el_vacio_se_apaga(self, app, db):
        from app.services.empaques_sync_service import _sincronizar_paquetes_sin_codigo
        p = _producto('Q35B')
        _paquete(p, 'PQ', 12, codigo=None)
        _paquete(p, 'PQ', 12, codigo='7700000001301')
        por_siesa, por_codigo = self._prods(p)
        r = _sincronizar_paquetes_sin_codigo({('Q35B', 'PQ'): 12}, por_siesa, por_codigo, set())
        assert (r['insertados'], r['desactivados']) == (0, 1)
        assert ProductoEmpaque.query.filter_by(producto_id=p.id, activo=True).count() == 1

    def test_un_codigo_vacio_nunca_se_escanea(self, app, db):
        from app.services.empaques_service import scan_barcode
        p = _producto('Q35C')
        _paquete(p, 'PQ', 12, codigo=None)
        assert ProductoEmpaque.buscar_por_barcode('') is None
        assert ProductoEmpaque.buscar_todos_por_barcode('   ') == []
        assert scan_barcode('  ')['tipo'] == 'NO_ENCONTRADO'

    def test_revisar_lista_lo_que_falta_en_siesa(self, client, jwt_token_admin, app, db):
        sin = _producto('REV1', unidad_empaque='PQ')
        con = _producto('REV2', unidad_empaque='PQ')
        _paquete(con, 'PQ', 12, codigo=None)
        _producto('REV3', unidad_empaque='UND')
        db.session.commit()
        r = client.get('/api/empaques/revisar', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        assert [x['codigo'] for x in d['declarado_sin_factor']['productos']] == ['REV1']
        assert [x['codigo'] for x in d['paquete_sin_codigo']['productos']] == ['REV2']


class TestElSyncNoInventaElFactor:
    """Un PQ cuyo factor no está en q35 caía a factor 1 «fallback seguro»:
    escanear el paquete contaba 1 unidad. Ahora no se registra y se apaga."""

    def test_sin_factor_no_entra_y_el_viejo_se_apaga(self, app, db, monkeypatch):
        from app.services import empaques_sync_service as es
        p = _producto('SYNC1')
        viejo = _paquete(p, 'PQ', 1, codigo='7700000001401')
        db.session.commit()

        class _Lock:
            def liberar(self):
                pass
        monkeypatch.setattr('app.utils.lock.tomar_lock_de_sesion', lambda *a, **k: _Lock())
        monkeypatch.setattr(es, '_cargar_factores_q35', lambda: {('OTRO', 'PQ'): 12})
        filas = [{'f131_id': '7700000001401', 'f120_referencia': 'SYNC1', 'f131_id_unidad_medida': 'PQ'},
                 {'f131_id': '7700000001402', 'f120_referencia': 'SYNC1', 'f131_id_unidad_medida': 'CAJ'}]
        paginas = iter([{'detalle': {'Table': filas}}, {'detalle': {'Table': []}}])
        monkeypatch.setattr(es.connekta, '_get', lambda *a, **k: next(paginas))
        es._run_sync(app)
        res = es.get_estado()['ultimo_resultado']
        assert res['sin_factor_q35'] == 2 and res['desactivados_sin_factor'] == 1
        assert sorted(res['sin_factor_q35_refs']) == ['SYNC1 CAJ', 'SYNC1 PQ']
        db.session.expire_all()
        assert db.session.get(ProductoEmpaque, viejo.id).activo is False
        assert ProductoEmpaque.query.filter_by(codigo_barras='7700000001402').count() == 0


# ── El trinquete: nadie más decide el paquete ────────────────────────────────

#: Atributos que contestan «en qué paquete viene». Leerlos es decidir.
ATRIBUTOS = ('factor_conversion', 'unidad_empaque')

#: Archivos que los DEFINEN (columnas) o los deciden.
DUENOS = {'app/models/producto.py', 'app/models/producto_empaque.py',
          'app/services/empaque_producto.py'}

#: Funciones que hoy leen el factor o la unidad de empaque por su cuenta, con
#: su porqué. **Solo encoge**: una función nueva que lo lea va por
#: `empaque_producto`; una que deja de leerlo sale de acá.
LECTORES_DECLARADOS = {
    ('app/models/lpn.py', 'to_dict'): 'el factor propio del LPN (copia inmutable al nacer)',
    ('app/models/packing.py', 'to_dict'): 'línea de empaque: contrato viejo del empacador; migrarlo es una consulta por fila',
    ('app/models/picking.py', 'to_dict'): 'línea de picking: ídem (el HUD vivo ya usa la política)',
    ('app/models/recepcion.py', 'to_dict'): 'línea de recepción: ídem',
    ('app/routes/siesa.py', '_iniciar_despacho_tras_cartera'): 'cantidad de la línea del pedido de Siesa (doble unidad), no cómo se pide',
    ('app/routes/siesa.py', 'buscar_producto'): 'diagnóstico de admin: muestra el dato crudo del producto',
    ('app/routes/siesa.py', 'iniciar_recepcion'): 'recepción por OC: la unidad de la línea de la OC',
    ('app/routes/siesa.py', 'skus_sin_ean_empaque'): 'diagnóstico de admin del dato crudo',
    ('app/services/closing/pedido_closer.py', '_construir_items_payload'): 'payload a Siesa del pedido (doble unidad PQ/UND); no se toca en esta tanda',
    ('app/services/compras_fuentes.py', 'empaque_de_compra'): 'unidad de COMPRA al proveedor: otra pregunta (compras)',
    ('app/services/conteo_service.py', 'vista_hud'): 'HUD de conteo: fuera del alcance (no se toca el conteo)',
    ('app/services/despacho_parcial_service.py', '_build_items'): 'línea del pedido de Siesa en PQ o UND (doble unidad)',
    ('app/services/despacho_parcial_service.py', 'despachar_parcial'): 'línea del pedido de Siesa (doble unidad) al despachar',
    ('app/services/empaques_service.py', 'descomponer_en_empaques'): 'el factor de una fila de producto_empaques (pacas/LPN)',
    ('app/services/empaques_service.py', 'generar_lpn'): 'el factor de la fila del empaque del LPN',
    ('app/services/empaques_service.py', 'scan_barcode'): 'el código escaneado define su propio factor',
    ('app/services/empaques_sync_service.py', '_run_sync'): 'escribe/lee las filas que sincroniza',
    ('app/services/empaques_sync_service.py', '_sincronizar_paquetes_sin_codigo'): 'las filas sin código que sincroniza',
    ('app/services/mobile_service.py', '_es_escaneo_empaque'): 'escaneo: la pareja EAN + factor del producto (cambiarla cambia lo que cuenta un escaneo)',
    ('app/services/mobile_service.py', '_unidades_del_escaneo'): 'escaneo: cuántas unidades vale el EAN del paquete',
    ('app/services/mobile_service.py', 'procesar_escaneo'): 'ídem (y la heurística de packing en PQ)',
    ('app/services/packing_picking_sync_service.py', 'sincronizar'): 'línea de empaque en PQ o UND (contrato viejo)',
    ('app/services/packing_service.py', '_cerrar_packing_pedido_legacy'): 'cierre viejo, payload a Siesa',
    ('app/services/parada_tardia.py', 'sin_gestionar'): 'rótulo de la unidad en la pantalla de la oficina',
    ('app/services/recepcion_service.py', 'escanear_producto'): 'recepción: factor del escaneo',
    ('app/services/ruta_service.py', 'listar_paradas'): 'rótulo de la unidad en la pantalla del conductor',
    ('app/services/ruta_service.py', '_uom_declarada'): 'unidad de la línea de la factura (doble unidad), no cómo se pide',
    ('app/services/siesa_sync_service.py', '_run_sync'): 'escribe el dato que viene de Siesa',
    ('app/services/tienda_oc_service.py', 'iniciar_recepcion'): 'recepción de tienda: factor de la línea',
}


def _lee_atributo(nodo):
    if isinstance(nodo, ast.Attribute) and nodo.attr in ATRIBUTOS and isinstance(nodo.ctx, ast.Load):
        return True
    if (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == 'getattr'
            and len(nodo.args) >= 2 and isinstance(nodo.args[1], ast.Constant)
            and nodo.args[1].value in ATRIBUTOS):
        return True
    return False


def lectores(fuente: str, archivo: str) -> set:
    """`{(archivo, función)}` de toda función que lee un atributo de empaque.
    Una función anidada cuenta como la de afuera (es la que decide)."""
    encontrados = set()

    def andar(nodo, funcion):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                andar(hijo, funcion or hijo.name)
                continue
            if _lee_atributo(hijo):
                encontrados.add((archivo, funcion or '<modulo>'))
            andar(hijo, funcion)

    andar(ast.parse(fuente), None)
    return encontrados


def _todos():
    res = set()
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        rel = p.relative_to(RAIZ).as_posix()
        if rel in DUENOS:
            continue
        res |= lectores(p.read_text(encoding='utf-8'), rel)
    return res


class TestNadieMasDecideElPaquete:
    def test_no_aparece_un_lector_nuevo(self):
        nuevos = _todos() - set(LECTORES_DECLARADOS)
        assert not nuevos, (
            f'Estas funciones leen factor_conversion/unidad_empaque por su cuenta: {sorted(nuevos)}. '
            'El paquete de un producto lo decide empaque_producto.empaque_de.')

    def test_el_inventario_solo_encoge(self):
        viejos = set(LECTORES_DECLARADOS) - _todos()
        assert not viejos, f'Ya no leen el atributo: sáquelas de LECTORES_DECLARADOS: {sorted(viejos)}'

    def test_cada_una_dice_por_que(self):
        assert all(len(m.strip()) > 10 for m in LECTORES_DECLARADOS.values())

    def test_los_que_se_migraron_ya_no_leen(self):
        encontrados = _todos()
        for sitio in [('app/services/traslado_service.py', '_resolver_empaque'),
                      ('app/services/mobile_service.py', 'get_tarea_actual')]:
            assert sitio not in encontrados

    def test_piso(self):
        assert len(_todos()) >= 25


class TestElDetectorMuerde:
    def test_ve_las_formas(self):
        src = (
            'def a(p):\n    return p.factor_conversion\n'
            'def b(p):\n    return getattr(p, "unidad_empaque", "")\n'
            'def c(p):\n    def d():\n        return p.unidad_empaque\n    return d\n'
        )
        assert lectores(src, 'x.py') == {('x.py', 'a'), ('x.py', 'b'), ('x.py', 'c')}

    def test_no_ve_lo_sano(self):
        src = (
            'def a(p):\n    """p.factor_conversion en la documentación"""\n'
            '    # p.unidad_empaque en un comentario\n'
            '    x = "factor_conversion"\n'
            '    p.factor_conversion = 3\n'
            '    return p.factor\n'
        )
        assert lectores(src, 'x.py') == set()

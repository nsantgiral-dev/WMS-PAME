"""
El inventario no se escribe con un dato de Siesa que no se tiene (P0-8).

## El caso

`_descargar_inventario_siesa_raw` arma el inventario igual cuando Siesa no
responde: lo saca de `stock_siesa` —la foto de ayer, o de la semana pasada— y lo
marca `degradado`. La carga física de las 7:00 (`CARGA_FISICA_AUTOMATICA`, nace
encendida) lo tomaba por `_descargar_inventario_siesa`, que **no miraba** esa
marca, y reescribía `UbicacionProducto` con él. Y una pasada con una página que
no se pudo leer (tres 429 seguidos) seguía de largo y volvía «completa».

## La decisión (dueño, 2026-09-25)

La carga sigue automática, pero **solo escribe con dato fresco y completo**: no
degradado, sello de hoy (Bogotá), con filas de esa bodega en la descarga de hoy,
sin páginas perdidas. Si no, no escribe nada y lo declara en `registros_sync`
(lo leen 🩺 Salud y el resumen diario). Se reintenta a mano con
`POST /api/siesa/cargar-inventario` dentro de la ventana de Siesa.

## El trinquete

1. Con la fuente degradada, **cero escrituras** en `UbicacionProducto`.
2. Por AST: toda función de `app/` que escribe `UbicacionProducto` y lee una
   fuente de stock de Siesa pasa por la política (`_descargar_inventario_siesa`
   o `_exigir_fuente_para_escribir`), y `_descargar_inventario_siesa` la llama.
"""
import ast
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Nombres que delatan que una función lee stock de Siesa.
FUENTES_SIESA = {
    '_descargar_inventario_siesa_raw', '_descargar_todas_bodegas_custom',
    '_descargar_una_pasada_custom', 'get_stock_bodega', 'get_inventario_fecha',
    'StockSiesa', '_leer_stock_de_bd', 'obtener_stock_bodega',
    '_descargar_inventario_multibodega_para_reconciliar',
    '_descargar_inventario_siesa', 'existencia_siesa',
}
#: Pasar por uno de estos es pasar por la política.
GUARDAS = {'_descargar_inventario_siesa', '_exigir_fuente_para_escribir'}

#: Escritores de inventario que leen Siesa SIN pasar por la política, con su
#: motivo. Vacío, y así se queda: solo encoge.
EXCEPCIONES_DECLARADAS = {}


def _nombres(nodo):
    out = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _escribe_inventario(fn) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == 'UbicacionProducto':
            return True
        if isinstance(n, (ast.Assign, ast.AugAssign)):
            objetivos = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Attribute) and t.attr == 'cantidad' for t in objetivos):
                return True
    return False


def _escritores_desde_siesa(base=None):
    """{`archivo::funcion`: guardada?} para toda función que escribe
    inventario y lee una fuente de stock de Siesa."""
    base = base or RAIZ
    out = {}
    for f in sorted((base / 'app').rglob('*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _escribe_inventario(fn):
                continue
            ns = _nombres(fn)
            if ns & FUENTES_SIESA:
                out[f'{f.relative_to(base)}::{fn.name}'] = bool(ns & GUARDAS)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# La política
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def cache_limpio():
    orig = dict(iss._cache_inventario_multibodega)
    yield iss._cache_inventario_multibodega
    iss._cache_inventario_multibodega.clear()
    iss._cache_inventario_multibodega.update(orig)


class TestLaPolitica:

    def test_degradado_no_sirve(self, cache_limpio):
        cache_limpio.update(data={'NB1': {'X': {}}}, ts=datetime.utcnow(),
                            degradado=True, bodegas_frescas=frozenset({'NB1'}))
        assert 'falló' in iss.fuente_para_escribir('NB1')

    def test_sello_de_ayer_no_sirve(self, cache_limpio):
        cache_limpio.update(data={'NB1': {'X': {}}}, ts=datetime.utcnow() - timedelta(days=1, hours=1),
                            degradado=False, bodegas_frescas=frozenset({'NB1'}))
        assert 'no es de hoy' in iss.fuente_para_escribir('NB1')

    def test_bodega_que_la_descarga_no_trajo_no_sirve(self, cache_limpio):
        """El merge rellena esa bodega con `stock_siesa`: no es dato de hoy."""
        cache_limpio.update(data={'NB1': {'X': {}}, 'NS1': {'Y': {}}}, ts=datetime.utcnow(),
                            degradado=False, bodegas_frescas=frozenset({'NB1'}))
        assert iss.fuente_para_escribir('NB1') == ''
        assert 'NS1' in iss.fuente_para_escribir('NS1')

    def test_fresco_y_completo_sirve(self, cache_limpio):
        cache_limpio.update(data={'NB1': {'X': {}}}, ts=datetime.utcnow(),
                            degradado=False, bodegas_frescas=frozenset({'NB1'}))
        assert iss.fuente_para_escribir('NB1') == ''


class TestUnaPaginaPerdidaInvalidaLaPasada:

    def test_tres_429_en_una_pagina_no_se_saltan(self):
        pagina_llena = {'detalle': {'Datos': [
            {'f150_id': 'NB1', 'f120_referencia': f'R{i}', 'f400_cant_existencia_1': 1}
            for i in range(1000)]}}

        ultima = {'detalle': {'Datos': [
            {'f150_id': 'NB1', 'f120_referencia': 'FIN', 'f400_cant_existencia_1': 1}]}}

        def _get(api, params, url=None):
            # Página 1 llena, página 2 perdida por 429, página 3 la última:
            # saltarse la 2 devolvería una pasada que PARECE completa.
            if params['paginacion'].startswith('numPag=1|'):
                return pagina_llena
            if params['paginacion'].startswith('numPag=2|'):
                raise RuntimeError('Connekta rate-limit (429)')
            return ultima

        with patch.object(iss.connekta, '_get', side_effect=_get), \
                patch('time.sleep'):
            assert iss._descargar_una_pasada_custom() is None, (
                'la pasada volvió «completa» con la página 2 sin leer')

    def test_una_pasada_sana_sigue_devolviendo_datos(self):
        """La otra dirección."""
        def _get(api, params, url=None):
            return {'detalle': {'Datos': [
                {'f150_id': 'NB1', 'f120_referencia': 'R1', 'f400_cant_existencia_1': 3}]}}
        with patch.object(iss.connekta, '_get', side_effect=_get):
            assert iss._descargar_una_pasada_custom()['NB1']['R1']['existencia'] == 3.0


# ─────────────────────────────────────────────────────────────────────────────
# El caso completo: fuente degradada → cero escrituras
# ─────────────────────────────────────────────────────────────────────────────

def _foto_inventario():
    from app.models.inventario import MovimientoInventario, UbicacionProducto
    filas = sorted((r.ubicacion_id, r.producto_id, r.cantidad)
                   for r in UbicacionProducto.query.all())
    return filas, MovimientoInventario.query.count()


def _sembrar(db, almacen, producto):
    from app.models.inventario import UbicacionProducto
    from app.models.stock_siesa import StockSiesa
    from app.models.ubicacion import Ubicacion
    ub = Ubicacion(codigo='PIK-P08', almacen_id=almacen.id, tipo_zona='PICKING', activo=True)
    db.session.add(ub)
    db.session.flush()
    db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id, cantidad=40))
    # La foto vieja que el fallback sirve cuando Siesa no responde: 60 SKU
    # (pasa el guard de «respuesta sospechosamente pequeña»), otra cantidad.
    viejo = datetime.utcnow() - timedelta(days=3)
    for i in range(60):
        db.session.add(StockSiesa(bodega=almacen.bodega_siesa_id, codigo_siesa=f'V{i:03d}',
                                  existencia=999, updated_at=viejo))
    db.session.add(StockSiesa(bodega=almacen.bodega_siesa_id, codigo_siesa=producto.codigo_siesa,
                              existencia=999, updated_at=viejo))
    db.session.commit()


class TestConFuenteDegradadaNoSeEscribeNada:

    def test_cero_escrituras_y_queda_declarado(self, app, db, almacen, producto, cache_limpio):
        from app.services import registro_sync_service as reg
        _sembrar(db, almacen, producto)
        antes = _foto_inventario()
        cache_limpio.update(data=None, ts=None, degradado=False, bodegas_frescas=frozenset())
        with patch.object(iss, '_descargar_todas_bodegas_custom', return_value=None), \
                patch.object(iss.connekta, 'bodega', almacen.bodega_siesa_id), \
                patch.object(iss, '_guardar_stock_en_bd'):
            iss._run_carga_inicial(app, almacen.bodega_siesa_id)
        db.session.expire_all()
        assert _foto_inventario() == antes, (
            'la carga escribió UbicacionProducto con la foto vieja de stock_siesa')
        ultimo = reg.ultimo(iss._tipo_registro_stock(almacen.bodega_siesa_id))
        assert ultimo and ultimo['ok'] is False
        assert 'No se escribe inventario' in (ultimo['error'] or '')

    def test_con_fuente_fresca_si_escribe(self, app, db, almacen, producto, cache_limpio):
        """La otra dirección: un guard que bloquea todo prueba la mitad."""
        _sembrar(db, almacen, producto)
        antes = _foto_inventario()
        fresco = {almacen.bodega_siesa_id: {
            **{f'V{i:03d}': {'existencia': 1.0, 'comprometido': 0.0, 'salida_sin_conf': 0.0}
               for i in range(60)},
            producto.codigo_siesa: {'existencia': 7.0, 'comprometido': 0.0, 'salida_sin_conf': 0.0},
        }}
        cache_limpio.update(data=None, ts=None, degradado=False, bodegas_frescas=frozenset())
        with patch.object(iss, '_descargar_todas_bodegas_custom', return_value=fresco), \
                patch.object(iss.connekta, 'bodega', almacen.bodega_siesa_id), \
                patch.object(iss, '_guardar_stock_en_bd'):
            iss._run_carga_inicial(app, almacen.bodega_siesa_id)
        db.session.expire_all()
        assert _foto_inventario() != antes


# ─────────────────────────────────────────────────────────────────────────────
# La clase: todo escritor desde Siesa pasa por la política
# ─────────────────────────────────────────────────────────────────────────────

class TestTodoEscritorDesdeSiesaPasaPorLaPolitica:

    def test_ninguno_sin_guarda(self):
        sin = sorted(k for k, g in _escritores_desde_siesa().items()
                     if not g and k not in EXCEPCIONES_DECLARADAS)
        assert not sin, (
            '\nFunciones que escriben UbicacionProducto con dato de Siesa sin '
            'pasar por la política de frescura:\n  · ' + '\n  · '.join(sin)
            + '\nLlamá _exigir_fuente_para_escribir(bodega) antes de escribir.')

    def test_las_excepciones_solo_encogen(self):
        hallados = _escritores_desde_siesa()
        sobran = [k for k in EXCEPCIONES_DECLARADAS if hallados.get(k) is not False]
        assert not sobran, f'Ya no son excepción: {sobran}'

    def test_la_descarga_para_escribir_exige_la_politica(self):
        """Pasar por `_descargar_inventario_siesa` cuenta como guarda SOLO
        porque ella llama a la política. Si deja de llamarla, el guard de
        arriba se apaga en silencio: se mide acá."""
        arbol = ast.parse((RAIZ / 'app/services/inventario_siesa_service.py')
                          .read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)
                  and n.name == '_descargar_inventario_siesa')
        llamadas = {n.func.id for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert '_exigir_fuente_para_escribir' in llamadas


class TestElDetectorMuerde:

    def _en(self, tmp_path, fuente):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _escritores_desde_siesa(base=tmp_path)

    def test_ve_al_escritor_sin_guarda(self, tmp_path):
        src = ('def cargar():\n    inv = _descargar_inventario_siesa_raw()\n'
               '    reg.cantidad = inv["X"]\n')
        assert self._en(tmp_path, src) == {'app/x.py::cargar': False}

    def test_ve_la_creacion_de_fila(self, tmp_path):
        src = ('def cargar():\n    s = StockSiesa.query.all()\n'
               '    db.session.add(UbicacionProducto(cantidad=s[0].existencia))\n')
        assert self._en(tmp_path, src) == {'app/x.py::cargar': False}

    def test_reconoce_la_guarda(self, tmp_path):
        src = ('def cargar(b):\n    _exigir_fuente_para_escribir(b)\n'
               '    inv = _descargar_inventario_siesa_raw()\n    reg.cantidad = 1\n')
        assert self._en(tmp_path, src) == {'app/x.py::cargar': True}

    def test_NO_marca_quien_solo_lee(self, tmp_path):
        src = 'def ver():\n    return _descargar_inventario_siesa_raw()\n'
        assert self._en(tmp_path, src) == {}

    def test_un_docstring_no_cuenta(self, tmp_path):
        src = ('def cargar():\n    """_descargar_inventario_siesa_raw"""\n'
               '    reg.cantidad = 1\n')
        assert self._en(tmp_path, src) == {}

    def test_piso_minimo(self):
        """La carga física tiene que aparecer: si no, el escáner se rompió."""
        hallados = _escritores_desde_siesa()
        assert 'app/services/inventario_siesa_service.py::_run_carga_inicial' in hallados


class TestSeVeYSeReintentaEnLaVentana:
    """Decisión del dueño (2026-09-25): la carga que no escribió se ve en la
    🩺 Salud y en el resumen diario, y se reintenta a mano dentro de la
    ventana de Siesa."""

    def test_la_salud_lo_dice(self, db):
        from app.services import registro_sync_service as reg
        from app.services.analitica_salud import carga_fisica
        rid = reg.abrir('stock')
        reg.cerrar_error(rid, iss.FuenteInventarioNoConfiable(
            'No se escribe inventario de NB1: la última descarga de Siesa falló.'))
        c = carga_fisica()
        assert c['nivel'] == 'advertencia' and 'NO escrita' in c['texto']

    def test_sin_fallas_no_dice_nada(self, db):
        from app.services.analitica_salud import carga_fisica
        assert carga_fisica()['nivel'] == 'ok'

    def test_el_reintento_fuera_de_ventana_es_409(self, client, db, jwt_token_admin):
        from app.services import ventana_siesa
        ventana_siesa._RELOJ_FIJO['abierta'] = False
        r = client.post('/api/siesa/cargar-inventario',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 409 and 'ventana' in r.get_json()['error']

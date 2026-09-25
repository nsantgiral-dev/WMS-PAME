"""`GET /api/rutas/<id>/paradas` es una lectura: no escribe, y en frío no tarda
la suma de todas las consultas a Siesa.

**El caso** (QA real con victor@, 2026-09-25): la lista de paradas del
conductor hacía `commit` —snapshot de cobro, FE resuelta, valor de la
factura— dentro del GET, y tardaba ~7 s en frío: una consulta a Siesa por
parada, en serie, más una consulta a la base por parada para los ítems.

**Ahora**: la lista no escribe nada (`anotar=False` hasta el fondo:
`_valor_y_cond_pago`, `resolver_fe`, `clasificar_tarea`); la escritura es
`POST /api/rutas/<id>/paradas/anotar` (`RutaService.anotar_paradas`), que la
pantalla dispara aparte. Las lecturas a Siesa salen en paralelo sobre una foto
de cada tarea, y los ítems se precargan.

**La clase**: *un GET que escribe*. El test dinámico escucha el SQL del GET y
exige cero INSERT/UPDATE/DELETE; el AST mira los GET de `app/routes` y
`flota/api` que hacen `commit` directo (inventario que solo encoge). Lo que el
AST NO ve: un servicio que escribe llamado desde un GET — por eso el
dinámico, para las lecturas del conductor.
"""
import ast
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event

from tests.test_contado_contraentrega import _ruta  # noqa: F401 (helper)

RAIZ = Path(__file__).resolve().parents[1]


@contextmanager
def _sql(db):
    """Las sentencias SQL ejecutadas dentro del bloque."""
    vistas = []

    def _oir(conn, cursor, stmt, params, ctx, many):
        vistas.append(stmt.strip().split()[0].upper())
    motor = db.engine
    event.listen(motor, 'before_cursor_execute', _oir)
    try:
        yield vistas
    finally:
        event.remove(motor, 'before_cursor_execute', _oir)


def _lineas(ref='X', cond='C04'):
    return [{'f470_vlr_neto': 1000, 'f470_vlr_bruto': 840, 'f470_vlr_imp': 160,
             'f461_id_cond_pago': cond, 'f120_referencia': ref, 'f470_cant_base': 1,
             'f210_codigo_vendedor': '002'}]


def _jwt_conductor(app, c):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(c.usuario_id))}'}


class TestLaListaNoEscribe:

    def test_el_get_no_ejecuta_ninguna_escritura(self, app, client, db, almacen):
        from app.services.connekta_gateway import connekta
        ruta, t, c = _ruta(db, almacen)
        _ruta(db, almacen, conductor=c, ruta=ruta)
        with patch('app.services.fe_resolver.resolver_fe_o_none', lambda _t, **_k: ('FEW', '7')), \
             patch.object(type(connekta), 'get_rowids_factura', lambda self, *a, **k: _lineas()), \
             patch.object(type(connekta), 'get_pedido_cabecera',
                          lambda self, *a, **k: {'f430_id_cond_pago': 'C04'}):
            with _sql(db) as vistas:
                r = client.get(f'/api/rutas/{ruta.id}/paradas', headers=_jwt_conductor(app, c))
        assert r.status_code == 200, r.get_json()
        escrituras = [v for v in vistas if v in ('INSERT', 'UPDATE', 'DELETE')]
        assert escrituras == [], f'el GET escribió: {escrituras}'
        # Y la pantalla recibe lo mismo que si hubiera anotado: la FE manda.
        p = r.get_json()['paradas'][0]
        assert p['cond_pago_fe'] == 'C04' and p['valor_factura'] == 1000

    def test_la_escritura_es_el_post_y_deja_el_snapshot(self, app, client, db, almacen):
        from app.models.packing import TareaPacking
        from app.services.connekta_gateway import connekta
        ruta, t, c = _ruta(db, almacen)
        with patch('app.services.fe_resolver.resolver_fe_o_none', lambda _t, **_k: ('FEW', '8')), \
             patch.object(type(connekta), 'get_rowids_factura', lambda self, *a, **k: _lineas(cond='C02')):
            r = client.post(f'/api/rutas/{ruta.id}/paradas/anotar', headers=_jwt_conductor(app, c))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['anotadas'] == 1
        db.session.expire_all()
        tt = db.session.get(TareaPacking, t.id)
        assert (tt.cond_pago_fe, float(tt.valor_factura), tt.cobro_contraentrega) == ('C02', 1000.0, True)

    def test_el_post_pide_el_mismo_acceso(self, app, client, db, almacen):
        from tests.test_contado_contraentrega import _conductor
        ruta, t, c = _ruta(db, almacen)
        otro = _conductor(db)
        r = client.post(f'/api/rutas/{ruta.id}/paradas/anotar', headers=_jwt_conductor(app, otro))
        assert r.status_code == 403


class TestEnFrioNoSeSumaTodo:

    def test_las_consultas_a_siesa_van_en_paralelo(self, db, almacen):
        from app.services.connekta_gateway import connekta
        from app.services.ruta_service import RutaService
        ruta, t, c = _ruta(db, almacen)
        for _ in range(5):
            _ruta(db, almacen, conductor=c, ruta=ruta)
        estado = {'en_vuelo': 0, 'maximo': 0}
        candado = threading.Lock()

        def _lento(self, tipo, consec):
            with candado:
                estado['en_vuelo'] += 1
                estado['maximo'] = max(estado['maximo'], estado['en_vuelo'])
            time.sleep(0.2)
            with candado:
                estado['en_vuelo'] -= 1
            return _lineas()
        consec = iter(range(100, 200))
        with patch('app.services.fe_resolver.resolver_fe_o_none',
                   lambda _t, **_k: ('FEW', str(next(consec)))), \
             patch.object(type(connekta), 'get_rowids_factura', _lento):
            t0 = time.monotonic()
            d = RutaService.listar_paradas(ruta.id)
            dur = time.monotonic() - t0
        assert len(d['paradas']) == 6
        assert estado['maximo'] >= 2, 'las seis facturas se leyeron una detrás de otra'
        assert dur < 6 * 0.2, f'en frío tardó {dur:.2f} s: la suma de las seis'

    def test_los_items_no_cuestan_una_consulta_por_parada(self, db, almacen):
        from types import SimpleNamespace
        from app.services.ruta_service import RutaService
        alm = SimpleNamespace(id=almacen.id)

        def _consultas(n):
            ruta, t, c = _ruta(db, alm)
            for _ in range(n - 1):
                _ruta(db, alm, conductor=c, ruta=ruta)
            rid = ruta.id
            db.session.expunge_all()      # como un request nuevo: nada en memoria
            with _sql(db) as vistas:
                RutaService.listar_paradas(rid)
            return sum(1 for v in vistas if v == 'SELECT')
        dos, ocho = _consultas(2), _consultas(8)
        # Queda una consulta por parada: el punto del cliente (`maestro_de`).
        # Antes eran dos más: los ítems de cada tarea y su producto.
        assert ocho - dos <= 6 * 1, f'{dos} consultas con 2 paradas y {ocho} con 8'
        assert dos <= 8, f'{dos} consultas con dos paradas'


# ═════════════════════════════════════════════════════════════════════════════
# AST: ningún GET hace commit directo
# ═════════════════════════════════════════════════════════════════════════════

#: (archivo, función) → por qué. **Solo encoge.**
GET_QUE_ESCRIBEN = {
    ('picking.py', 'siguiente_tarea'):
        'asigna la siguiente tarea al operario que la pide: una lectura que escribe; '
        'pendiente de pasar a POST',
}


def _metodos(fn):
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == 'route':
            ms = [k for k in d.keywords if k.arg == 'methods']
            if not ms:
                return {'GET'}
            if isinstance(ms[0].value, (ast.List, ast.Tuple)):
                return {e.value for e in ms[0].value.elts if isinstance(e, ast.Constant)}
    return None


def gets_que_hacen_commit(fuente: str):
    out = []
    for fn in ast.walk(ast.parse(fuente)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _metodos(fn) == {'GET'}:
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in ('commit', 'flush') for n in ast.walk(fn)):
                out.append(fn.name)
    return out


def _encontrados():
    out = set()
    for base in ('app/routes', 'flota/api'):
        for f in sorted((RAIZ / base).rglob('*.py')):
            for n in gets_que_hacen_commit(f.read_text(encoding='utf-8')):
                out.add((f.name, n))
    return out


class TestNingunGetHaceCommit:

    def test_no_crece(self):
        nuevos = _encontrados() - set(GET_QUE_ESCRIBEN)
        assert not nuevos, f'un GET que escribe: {sorted(nuevos)}. La escritura va en un POST.'

    def test_solo_encoge(self):
        assert not set(GET_QUE_ESCRIBEN) - _encontrados()

    def test_meta_ve_y_no_marca_lo_sano(self):
        src = ("@bp.route('/a')\ndef a():\n    db.session.commit()\n"
               "@bp.route('/b', methods=['GET'])\ndef b():\n    db.session.flush()\n"
               "@bp.route('/c', methods=['POST'])\ndef c():\n    db.session.commit()\n"
               "@bp.route('/d')\ndef d():\n    '''db.session.commit()'''\n    return 1\n")
        assert gets_que_hacen_commit(src) == ['a', 'b']

    def test_piso(self):
        n = sum(1 for base in ('app/routes', 'flota/api') for _ in (RAIZ / base).rglob('*.py'))
        assert n >= 40

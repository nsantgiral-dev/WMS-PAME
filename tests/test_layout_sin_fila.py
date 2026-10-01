"""
Layout: estanterías «sin fila».

Por defecto un pasillo tiene 2 filas (un lado y el otro). En Picking, Reserva,
Importados y Averías hay estanterías pegadas a la pared que tienen un solo
lado: se crean con fila = None («Sin fila» en el wizard).

- El código lleva solo el pasillo: PIK-A-C03-E01-H01 (con fila: PIK-A1-...).
- Un pasillo es entero con filas o entero sin fila: no se mezclan.
- Editar, reclasificar y eliminar el cuerpo funcionan con fila = None.
- La pantalla: el checkbox deshabilita el selector y manda fila: null.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.extensions import db as _db
from app.models.ubicacion import Ubicacion
from app.services import layout_service as svc

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


# ── Servicio ─────────────────────────────────────────────────────────────────

def test_cuerpo_sin_fila_genera_codigo_sin_fila(db, almacen):
    creadas = svc.crear_cuerpo(almacen.id, 'A', None, 3, 2, 'PICKING')
    assert [u.codigo for u in creadas] == ['PIK-A-C03-E01-H01', 'PIK-A-C03-E02-H01']
    assert all(u.fila is None and u.cuerpo == 3 for u in creadas)


@pytest.mark.parametrize('zona,prefijo', [
    ('PICKING', 'PIK'), ('RESERVA', 'RES'), ('IMPORTADOS', 'IMP'), ('AVERIAS', 'AVE'),
])
def test_sin_fila_en_las_cuatro_zonas(db, almacen, zona, prefijo):
    ub = svc.crear_cuerpo(almacen.id, 'B', None, 1, 1, zona)[0]
    assert ub.codigo == f'{prefijo}-B-C01-E01-H01'
    assert ub.tipo_zona == zona


@pytest.mark.parametrize('tipo,sufijo', [('vitrina', 'VIT'), ('estiba', 'EST')])
def test_mueble_suelto_sin_fila(db, almacen, tipo, sufijo):
    ub = svc.crear_cuerpo(almacen.id, 'C', None, 4, 1, 'PICKING', tipo_mueble=tipo)[0]
    assert ub.codigo == f'PIK-C-{sufijo}04'


@pytest.mark.parametrize('tipo,sufijo', [('vitrina', 'VIT'), ('estiba', 'EST')])
def test_vitrina_y_estiba_nunca_llevan_fila_aunque_llegue_una(db, almacen, tipo, sufijo):
    ub = svc.crear_cuerpo(almacen.id, 'C', 2, 4, 1, 'PICKING', tipo_mueble=tipo)[0]
    assert ub.fila is None
    assert ub.codigo == f'PIK-C-{sufijo}04'


def test_vitrina_y_estiba_conviven_con_un_pasillo_de_filas(db, almacen):
    """La regla de no mezclar es solo entre estanterías."""
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING')
    assert svc.crear_cuerpo(almacen.id, 'A', None, 2, 1, 'PICKING', tipo_mueble='vitrina')
    assert svc.crear_cuerpo(almacen.id, 'A', None, 3, 1, 'PICKING', tipo_mueble='estiba')
    # y la vitrina tampoco vuelve «sin fila» al pasillo: sigue admitiendo fila 2
    assert svc.crear_cuerpo(almacen.id, 'A', 2, 1, 1, 'PICKING')


def test_una_vitrina_vieja_con_fila_conserva_su_codigo_al_remodular(db, almacen):
    """Las creadas antes de la regla (PIK-A1-VIT01) no cambian de código: hay etiquetas impresas."""
    _db.session.add(Ubicacion(codigo='PIK-A1-VIT01', almacen_id=almacen.id, pasillo='A', fila=1,
                              cuerpo=1, nivel=1, hueco=1, tipo_zona='PICKING', tipo='vitrina',
                              origen='MANUAL', activo=True))
    _db.session.commit()
    nuevas = svc.editar_cuerpo(almacen.id, 'A', 1, 1, 1)
    assert [u.codigo for u in nuevas] == ['PIK-A1-VIT01']


def test_un_pasillo_con_filas_no_admite_un_cuerpo_sin_fila(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')
    with pytest.raises(ValueError, match='ya tiene ubicaciones con fila'):
        svc.crear_cuerpo(almacen.id, 'A', None, 2, 1, 'PICKING')


def test_un_pasillo_sin_fila_no_admite_un_cuerpo_con_fila(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')
    with pytest.raises(ValueError, match='es sin fila'):
        svc.crear_cuerpo(almacen.id, 'A', 2, 2, 1, 'RESERVA')


def test_la_regla_es_por_almacen(db, almacen):
    """Otro almacén con el mismo pasillo no cuenta."""
    from app.models.almacen import Almacen
    otro = Almacen(codigo='OTRO', nombre='Otro', activo=True)
    _db.session.add(otro)
    _db.session.commit()
    svc.crear_cuerpo(otro.id, 'A', 1, 1, 1, 'PICKING')
    assert svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')


def test_las_filas_legadas_no_cuentan_para_la_regla(db, almacen):
    """Una fila plana del mecanismo viejo (estante, sin cuerpo) no es «con fila»."""
    _db.session.add(Ubicacion(codigo='PIK-A01-01', almacen_id=almacen.id, pasillo='A',
                              estante='1', tipo_zona='PICKING', tipo='estanteria',
                              origen='MANUAL', activo=True))
    _db.session.commit()
    assert svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')


def test_varios_cuerpos_sin_fila_en_el_mismo_pasillo(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')
    svc.crear_cuerpo(almacen.id, 'A', None, 2, 1, 'RESERVA')
    with pytest.raises(ValueError, match='ya existe'):
        svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')


def test_editar_cuerpo_sin_fila_conserva_el_sin_fila(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')
    nuevas = svc.editar_cuerpo(almacen.id, 'A', None, 1, 2, huecos_por_nivel=[1, 2])
    assert sorted(u.codigo for u in nuevas) == [
        'PIK-A-C01-E01-H01', 'PIK-A-C01-E02-H01', 'PIK-A-C01-E02-H02']
    assert all(u.fila is None for u in nuevas)


def test_reclasificar_y_eliminar_cuerpo_sin_fila(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', None, 1, 2, 'PICKING')
    svc.reclasificar_cuerpo(almacen.id, 'A', None, 1, tipo_zona='RESERVA')
    assert {u.tipo_zona for u in Ubicacion.query.filter_by(pasillo='A').all()} == {'RESERVA'}
    svc.eliminar_cuerpo(almacen.id, 'A', None, 1)
    assert Ubicacion.query.filter_by(pasillo='A').count() == 0


def test_un_cuerpo_sin_fila_no_se_confunde_con_el_de_fila(db, almacen):
    """_ubicaciones_de_cuerpo con fila=None busca fila IS NULL, no «cualquier fila»."""
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')
    with pytest.raises(ValueError, match='No existe el cuerpo A-C01'):
        svc.eliminar_cuerpo(almacen.id, 'A', None, 1)


def test_ruta_de_picking_pone_el_pasillo_sin_fila_en_su_lugar(db, almacen):
    """orden_ruta_fisica ordena por pasillo: el sin fila no cae al final de todo."""
    from app.services.picking_service import PickingService
    b = svc.crear_cuerpo(almacen.id, 'B', 1, 1, 1, 'PICKING')[0]
    a = svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')[0]
    c = svc.crear_cuerpo(almacen.id, 'C', None, 1, 1, 'PICKING')[0]
    ids = [u.id for u in Ubicacion.query.filter(Ubicacion.id.in_([a.id, b.id, c.id]))
           .order_by(*PickingService.orden_ruta_fisica()).all()]
    assert ids == [a.id, b.id, c.id]


# ── Endpoints ────────────────────────────────────────────────────────────────

def _h(token):
    return {'Authorization': f'Bearer {token}'}


def test_endpoint_crea_sin_fila_con_null(client, jwt_token_admin, almacen):
    r = client.post(f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
                    json={'pasillo': 'A', 'fila': None, 'cuerpo': 1,
                          'cantidad_entrepanos': 2, 'tipo_zona': 'RESERVA'},
                    headers=_h(jwt_token_admin))
    assert r.status_code == 201, r.get_json()
    assert [u['codigo'] for u in r.get_json()['ubicaciones']] == [
        'RES-A-C01-E01-H01', 'RES-A-C01-E02-H01']
    assert all(u['fila'] is None for u in r.get_json()['ubicaciones'])


def test_endpoint_exige_la_clave_fila(client, jwt_token_admin, almacen):
    r = client.post(f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
                    json={'pasillo': 'A', 'cuerpo': 1, 'cantidad_entrepanos': 1,
                          'tipo_zona': 'PICKING'},
                    headers=_h(jwt_token_admin))
    assert r.status_code == 400
    assert 'fila' in r.get_json()['error']


def test_endpoint_rechaza_la_mezcla_con_400(client, jwt_token_admin, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')
    r = client.post(f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
                    json={'pasillo': 'A', 'fila': None, 'cuerpo': 2,
                          'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
                    headers=_h(jwt_token_admin))
    assert r.status_code == 400
    assert 'ya tiene ubicaciones con fila' in r.get_json()['error']


def test_endpoint_fila_ilegible_es_400(client, jwt_token_admin, almacen):
    r = client.post(f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
                    json={'pasillo': 'A', 'fila': 'x', 'cuerpo': 1,
                          'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
                    headers=_h(jwt_token_admin))
    assert r.status_code == 400


def test_endpoints_editar_reclasificar_eliminar_con_fila_null(client, jwt_token_admin, almacen):
    url = f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo'
    svc.crear_cuerpo(almacen.id, 'A', None, 1, 1, 'PICKING')
    r = client.put(url, json={'pasillo': 'A', 'fila': None, 'cuerpo': 1,
                              'cantidad_entrepanos': 2}, headers=_h(jwt_token_admin))
    assert r.status_code == 200, r.get_json()
    r = client.patch(url, json={'pasillo': 'A', 'fila': None, 'cuerpo': 1,
                                'tipo_zona': 'RESERVA'}, headers=_h(jwt_token_admin))
    assert r.status_code == 200, r.get_json()
    r = client.delete(url, json={'pasillo': 'A', 'fila': None, 'cuerpo': 1},
                      headers=_h(jwt_token_admin))
    assert r.status_code == 200, r.get_json()
    assert Ubicacion.query.filter_by(pasillo='A').count() == 0


# ── Pantalla (layout.js real en Node) ───────────────────────────────────────

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const ESC = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const els = {};
const el = (id) => (els[id] ||= { id, value: '', checked: false, disabled: false,
                                   style: {}, innerHTML: '', textContent: '' });
for (const [id, v] of Object.entries(ESC.dom || {})) Object.assign(el(id), v);
const posts = [];
const ctx = {
  console,
  document: { getElementById: el, querySelector: () => null,
              querySelectorAll: () => [], addEventListener() {},
              createElement: () => ({ style: {} }) },
  window: { location: { origin: 'http://test' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  alerta() {}, get: async () => [], post: async (url, body) => { posts.push(body); return {}; },
  ALMACEN_ID: 1, API: '',
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/layout.js', 'utf-8'), ctx);
vm.runInContext('_layoutUbicacionesCache = ' + JSON.stringify(ESC.cache || []) +
                '; layoutCargarUbicaciones = async () => {};', ctx);
const out = await vm.runInContext(ESC.js, ctx);
process.stdout.write(JSON.stringify({ out, posts,
  filaDisabled: el('layout-cuerpo-fila').disabled,
  sinFila: el('layout-cuerpo-sin-fila').checked }));
"""


def _correr(tmp_path, js, dom=None, cache=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    e = tmp_path / 'e.json'
    e.write_text(json.dumps({'js': js, 'dom': dom or {}, 'cache': cache or []}), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(PWA), str(e)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_DOM_PASO = {
    'layout-cuerpo-pasillo': {'value': 'A'},
    'layout-cuerpo-fila': {'value': '2'},
    'layout-cuerpo-numero': {'value': '3'},
    'layout-cuerpo-entrepanos': {'value': '1'},
    'layout-cuerpo-hueco-nivel-1': {'value': '1'},
}


def test_marcar_sin_fila_deshabilita_las_filas_y_desmarcar_las_habilita(tmp_path):
    r = _correr(tmp_path, '_layoutSetSinFila(true); _layoutFilaElegida()')
    assert r['filaDisabled'] is True and r['out'] is None
    r = _correr(tmp_path, '_layoutSetSinFila(true); _layoutSetSinFila(false); _layoutFilaElegida()',
                dom={'layout-cuerpo-fila': {'value': '2'}})
    assert r['filaDisabled'] is False and r['out'] == 2


def test_guardar_sin_fila_manda_fila_null(tmp_path):
    dom = dict(_DOM_PASO, **{'layout-cuerpo-sin-fila': {'checked': True}})
    r = _correr(tmp_path, 'layoutGuardarCuerpo()', dom=dom)
    assert r['posts'] and r['posts'][0]['fila'] is None


def test_guardar_por_defecto_manda_la_fila_elegida(tmp_path):
    r = _correr(tmp_path, 'layoutGuardarCuerpo()', dom=_DOM_PASO)
    assert r['posts'][0]['fila'] == 2


def test_un_pasillo_existente_sin_fila_marca_el_check_solo(tmp_path):
    cache = [{'pasillo': 'A', 'fila': None, 'cuerpo': 1}]
    r = _correr(tmp_path, 'layoutCuerpoPasilloCambio()',
                dom={'layout-cuerpo-pasillo': {'value': 'A'}}, cache=cache)
    assert r['sinFila'] is True and r['filaDisabled'] is True
    cache = [{'pasillo': 'A', 'fila': 1, 'cuerpo': 1}]
    r = _correr(tmp_path, '_layoutSetSinFila(true); layoutCuerpoPasilloCambio()',
                dom={'layout-cuerpo-pasillo': {'value': 'A'}}, cache=cache)
    assert r['sinFila'] is False and r['filaDisabled'] is False


@pytest.mark.parametrize('tipo', ['vitrina', 'estiba'])
def test_vitrina_y_estiba_esconden_la_fila_y_mandan_null(tmp_path, tipo):
    dom = dict(_DOM_PASO, **{'layout-cuerpo-fila-wrap': {}})
    r = _correr(tmp_path, f"""
      layoutCuerpoSetTipoMueble('{tipo}');
      const oculta = document.getElementById('layout-cuerpo-fila-wrap').style.display;
      layoutCuerpoCrearMuebleSuelto().then(() => oculta);""", dom=dom)
    assert r['out'] == 'none'
    assert r['posts'] and r['posts'][0]['fila'] is None
    r = _correr(tmp_path, """
      layoutCuerpoSetTipoMueble('vitrina'); layoutCuerpoSetTipoMueble('estanteria');
      document.getElementById('layout-cuerpo-fila-wrap').style.display""", dom=dom)
    assert r['out'] == 'block'


def test_el_pasillo_no_se_juzga_por_sus_vitrinas(tmp_path):
    """Un pasillo que solo tiene vitrinas/estibas (fila null) no marca «Sin fila»."""
    cache = [{'pasillo': 'A', 'fila': None, 'cuerpo': 2, 'tipo': 'vitrina'},
             {'pasillo': 'A', 'fila': None, 'cuerpo': 3, 'tipo': 'estiba'}]
    r = _correr(tmp_path, 'layoutCuerpoPasilloCambio()',
                dom={'layout-cuerpo-pasillo': {'value': 'A'}}, cache=cache)
    assert r['sinFila'] is False


def test_el_codigo_mostrado_no_dice_null(tmp_path):
    r = _correr(tmp_path, '[_layoutEjePasilloFila("A", null), _layoutEjePasilloFila("A", 1)]')
    assert r['out'] == ['A', 'A1']

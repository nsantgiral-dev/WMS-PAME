"""Los roles «liquidador» y «líder de cartera»: cada uno puede lo suyo, y nada
más (decisión del dueño, 2026-09-25).

**Qué pasaba.** La plata de la ruta tenía dos dueños posibles: admin (todo) o
admin-y-jefe de almacén. Liquidar, registrar un cobro o autorizar un crédito
obligaba a darle a alguien de oficina el rol **admin** —usuarios, maestros,
inventario, Siesa entero— para una sola pantalla. Y el jefe de almacén, que
opera bodega, confirmaba retenciones y corregía cobros.

**La clase**: *un permiso que solo se puede dar entero*. Ahora cada operación
es una función (`permisos_liquidacion`, `cartera_service.puede_autorizar`) y
cada rol nuevo es una línea en la de su operación.

Cuatro capas, todas con la matriz del dueño **escrita acá a mano** (derivarla
de las funciones sería medir el código contra sí mismo):

1. **La matriz por función**: para cada operación, exactamente estos roles.
2. **Por HTTP, con ids reales** (el inventario de `test_permisos_lista_blanca`
   usa ids inexistentes y no ve una ruta que contesta 404 después de mirar el
   rol): por rol, qué escrituras de la liquidación y de cartera atraviesan la
   puerta. Y las lecturas de la pantalla.
3. **El aterrizaje** (Node, `util.js` + `app.js` reales): qué pestañas ve cada
   rol y dónde cae al entrar.
4. **La tarjeta de Liquidación** (Node): cada botón de plata, solo a quien lo
   puede usar, con los `permisos` reales del detalle.

**Lo que NO cubre:** el formulario de parada tardía (otro frente); los botones
de pantallas que estos roles no abren (Rutas, Operación hoy).
"""
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.test_cartera_retencion import (  # noqa: F401 — `fake` es fixture
    _jwt, _recaudo, _retener, _tarea, _usuario, fake)

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

ROLES = ('admin', 'jefe_almacen', 'supervisor', 'gerente', 'liquidador', 'lider_cartera',
         'operario', 'conductor', 'tienda', 'control_flota')

#: La matriz del dueño (2026-09-25). **A mano, a propósito.**
MATRIZ = {
    'puede_liquidar':                {'admin', 'liquidador'},
    'puede_resolver_documento':      {'admin', 'liquidador'},
    'puede_registrar_parada_tardia': {'admin', 'liquidador'},
    'puede_confirmar_retencion':     {'admin', 'lider_cartera'},
    'puede_corregir_cobro':          {'admin', 'lider_cartera'},
    'puede_autorizar_credito':       {'admin', 'lider_cartera'},
    'puede_forzar_cierre_ruta':      {'admin'},
    'puede_ver_liquidacion':         {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'},
}


def _u(rol, **kw):
    from types import SimpleNamespace
    return SimpleNamespace(rol=rol, activo=True, puede_autorizar_cartera=False, **kw)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La matriz por función
# ═════════════════════════════════════════════════════════════════════════════

class TestLaMatrizDelDueno:

    @pytest.mark.parametrize('funcion', sorted(MATRIZ))
    def test_cada_operacion_deja_pasar_exactamente_a_sus_roles(self, funcion):
        from app.services import permisos_liquidacion as pl
        f = getattr(pl, funcion)
        pasan = {r for r in ROLES if f(_u(r))}
        assert pasan == MATRIZ[funcion], (funcion, sorted(pasan))

    def test_inactivo_o_nadie_nunca(self):
        from types import SimpleNamespace
        from app.services import permisos_liquidacion as pl
        for funcion in MATRIZ:
            f = getattr(pl, funcion)
            assert not f(None) and not f(SimpleNamespace(rol='admin', activo=False)), funcion

    def test_toda_funcion_de_permiso_esta_en_la_matriz(self):
        """Una operación nueva entra a la matriz con sus roles o esto se pone
        rojo (las que reciben más que el usuario se prueban aparte)."""
        import inspect
        from app.services import permisos_liquidacion as pl
        de_un_usuario = {n for n, f in vars(pl).items()
                         if n.startswith('puede_') and callable(f)
                         and len(inspect.signature(f).parameters) == 1}
        assert de_un_usuario == set(MATRIZ)

    def test_reintentar_un_documento_de_la_liquidacion_es_liquidar(self):
        from app.services.permisos_liquidacion import puede_reintentar_job
        pasan = {r for r in ROLES if puede_reintentar_job(_u(r), 'RECIBO_CAJA')}
        assert pasan == {'admin', 'liquidador'}
        otros = {r for r in ROLES if puede_reintentar_job(_u(r), 'DESPACHO_F470')}
        assert otros == {'admin', 'supervisor', 'jefe_almacen'}

    def test_los_envios_de_la_liquidacion_los_ve_quien_ve_la_liquidacion(self):
        from app.services.permisos_liquidacion import puede_ver_jobs
        liq = ['RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA']
        assert {r for r in ROLES if puede_ver_jobs(_u(r), liq)} == \
            {'admin', 'supervisor', 'jefe_almacen', 'liquidador', 'lider_cartera'}
        # Sin filtro (todos) o con uno ajeno: solo supervisión.
        for tipos in ([], ['RECIBO_CAJA', 'DESPACHO_F470']):
            assert {r for r in ROLES if puede_ver_jobs(_u(r), tipos)} == \
                {'admin', 'supervisor', 'jefe_almacen'}, tipos

    def test_cartera_la_autoriza_el_lider_por_su_rol(self):
        from app.services.cartera_service import puede_autorizar
        assert {r for r in ROLES if puede_autorizar(_u(r))} == {'lider_cartera'}

    def test_la_casilla_vale_solo_en_gestion(self):
        """Lista blanca: la casilla por persona en un rol de gestión (el admin,
        como antes); en cualquier otro rol —el que se cree mañana— no decide."""
        from types import SimpleNamespace
        from app.services.cartera_service import puede_autorizar
        con = {r for r in ROLES if puede_autorizar(
            SimpleNamespace(rol=r, activo=True, puede_autorizar_cartera=True))}
        assert con == {'admin', 'supervisor', 'jefe_almacen', 'gerente', 'lider_cartera'}
        assert not puede_autorizar(SimpleNamespace(rol='rol_de_manana', activo=True,
                                                   puede_autorizar_cartera=True))

    def test_no_son_personal_de_almacen_ni_gestion(self):
        from app.routes._auth_helpers import Roles
        for rol in (Roles.LIQUIDADOR, Roles.LIDER_CARTERA):
            assert rol not in Roles.PERSONAL_ALMACEN
            assert rol not in Roles.GESTION
            assert rol not in Roles.SUPERVISION

    def test_se_pueden_crear(self):
        from app.routes.auth import _ROLES_VALIDOS
        assert {'liquidador', 'lider_cartera'} <= set(_ROLES_VALIDOS)


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Por HTTP, con ids reales
# ═════════════════════════════════════════════════════════════════════════════

def _mundo(db, almacen, estado_ruta='ENTREGADA'):
    """Una ruta con una parada confirmada (recaudo) y un RC fallido."""
    from app.models.siesa_job import SiesaJob
    t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 90000 + 10000}', cond='C02',
               fe=('FEW', str(uuid.uuid4().int % 90000)), valor=100_000)
    r = _recaudo(db, t, estado='ENTREGADO', forma='EFECTIVO', monto=100_000)
    r.ruta.estado = estado_ruta
    job = SiesaJob(tipo='RECIBO_CAJA', payload='{}', referencia_tipo='RecaudoEntrega',
                   referencia_id=r.id, estado='FALLIDO', error_ultimo='x')
    db.session.add(job)
    db.session.commit()
    return {'ruta': r.ruta_id, 'rec': r.id, 'tarea': t.id, 'job': job.id}


#: (nombre, método, url con {ruta}/{rec}/{tarea}/{job}, cuerpo, estado de la
#: ruta, quién pasa). Los roles que pasan, **a mano**.
ESCRITURAS = [
    ('liquidar', 'POST', '/api/rutas/{ruta}/liquidar', {}, 'ENTREGADA',
     {'admin', 'liquidador'}),
    ('enviar a Siesa', 'POST', '/api/rutas/{ruta}/liquidar-siesa', {}, 'ENTREGADA',
     {'admin', 'liquidador'}),
    ('registrar cobro', 'POST', '/api/rutas/{ruta}/recaudos/{rec}/registrar-cobro', {}, 'ENTREGADA',
     {'admin', 'liquidador'}),
    ('resolver RC', 'POST', '/api/rutas/{ruta}/recaudos/{rec}/resolver-rc', {}, 'ENTREGADA',
     {'admin', 'liquidador'}),
    ('reintentar RC', 'POST', '/api/reposicion/siesa-jobs/{job}/reintentar', {}, 'ENTREGADA',
     {'admin', 'liquidador'}),
    ('parada tardía', 'POST', '/api/rutas/{ruta}/paradas/{tarea}/confirmar',
     {'estado_entrega': 'ENTREGADO'}, 'ENTREGADA', {'admin', 'liquidador'}),
    ('confirmar retención', 'POST', '/api/rutas/{ruta}/recaudos/{rec}/confirmar-retencion', {},
     'ENTREGADA', {'admin', 'lider_cartera'}),
    ('corregir cobro', 'POST', '/api/rutas/{ruta}/recaudos/{rec}/corregir-monto', {}, 'ENTREGADA',
     {'admin', 'lider_cartera'}),
    ('parada en tránsito desde la oficina', 'POST', '/api/rutas/{ruta}/paradas/{tarea}/confirmar',
     {'estado_entrega': 'ENTREGADO'}, 'EN_TRANSITO', {'admin', 'lider_cartera'}),
    ('autorizar crédito', 'POST', '/api/rutas/{ruta}/recaudos/{rec}/autorizar-credito', {},
     'ENTREGADA', {'admin', 'lider_cartera'}),
    ('crédito en lote', 'POST', '/api/cartera/panel/credito-lote', {}, 'ENTREGADA',
     {'admin', 'lider_cartera'}),
    ('forzar cierre', 'POST', '/api/rutas/{ruta}/forzar-cierre', {}, 'ENTREGADA', {'admin'}),
]

LECTURAS = [
    ('dashboard', '/api/rutas/liquidacion/dashboard', {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('desglose', '/api/rutas/liquidacion/desglose', {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('detalle', '/api/rutas/{ruta}/liquidacion-detalle', {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('reconciliación', '/api/rutas/{ruta}/reconciliacion', {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('planilla', '/api/rutas/{ruta}/planilla', {'admin', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('envíos de la liquidación',
     '/api/reposicion/siesa-jobs?estado=FALLIDO&tipos=NOTA_CREDITO_FACTURA,RECIBO_CAJA,DOCUMENTO_CONTABLE_RET',
     {'admin', 'supervisor', 'jefe_almacen', 'liquidador', 'lider_cartera'}),
    ('todos los envíos', '/api/reposicion/siesa-jobs?estado=FALLIDO', {'admin', 'supervisor', 'jefe_almacen'}),
    ('retenidos por cartera', '/api/cartera/panel/retenciones?estado=RETENIDO',
     {'admin', 'supervisor', 'jefe_almacen', 'gerente', 'lider_cartera'}),
]

_HTTP_ROLES = ('admin', 'jefe_almacen', 'supervisor', 'gerente', 'liquidador', 'lider_cartera',
               'operario')


class TestPorHttp:

    @pytest.mark.parametrize('nombre,metodo,url,cuerpo,estado,pasan', ESCRITURAS,
                             ids=[e[0] for e in ESCRITURAS])
    def test_la_escritura_la_atraviesa_solo_quien_debe(self, app, client, db, almacen,
                                                       nombre, metodo, url, cuerpo, estado, pasan):
        vistos = {}
        for rol in _HTTP_ROLES:
            ids = _mundo(db, almacen, estado)          # un mundo por rol: ninguno pisa al otro
            r = client.open(url.format(**ids), method=metodo, json=cuerpo,
                            headers=_jwt(app, _usuario(db, rol=rol)))
            vistos[rol] = r.status_code
        atraviesan = {rol for rol, c in vistos.items() if c != 403}
        assert atraviesan == pasan, (nombre, vistos)
        # Quien pasa no se encuentra con un 401/404: la puerta se midió de verdad.
        assert all(vistos[r] not in (401, 404, 405) for r in pasan), (nombre, vistos)

    @pytest.mark.parametrize('nombre,url,pasan', LECTURAS, ids=[e[0] for e in LECTURAS])
    def test_la_lectura_de_la_pantalla(self, app, client, db, almacen, nombre, url, pasan):
        ids = _mundo(db, almacen)
        vistos = {rol: client.get(url.format(**ids), headers=_jwt(app, _usuario(db, rol=rol))).status_code
                  for rol in _HTTP_ROLES}
        assert {r for r, c in vistos.items() if c == 200} == pasan, (nombre, vistos)
        assert {r for r, c in vistos.items() if c == 403} == set(_HTTP_ROLES) - pasan, (nombre, vistos)

    def test_el_detalle_le_dice_a_cada_rol_que_puede(self, app, client, db, almacen):
        ids = _mundo(db, almacen)
        url = f"/api/rutas/{ids['ruta']}/liquidacion-detalle"
        for rol in ('liquidador', 'lider_cartera'):
            p = client.get(url, headers=_jwt(app, _usuario(db, rol=rol))).get_json()['permisos']
            si = {k for k, v in p.items() if v}
            assert si == {
                'liquidador': {'liquidar', 'resolver_documento', 'parada_tardia'},
                'lider_cartera': {'confirmar_retencion', 'autorizar_credito', 'corregir_cobro'},
            }[rol], (rol, p)

    def test_cada_envio_dice_si_quien_mira_lo_reintenta(self, app, client, db, almacen):
        _mundo(db, almacen)
        url = '/api/reposicion/siesa-jobs?estado=FALLIDO&tipos=RECIBO_CAJA'
        for rol, puede in (('liquidador', True), ('lider_cartera', False), ('jefe_almacen', False)):
            jobs = client.get(url, headers=_jwt(app, _usuario(db, rol=rol))).get_json()['jobs']
            assert jobs and all(j['puede_reintentar'] is puede for j in jobs), rol

    def test_el_lider_de_cartera_decide_una_retencion(self, app, client, db, almacen, fake):  # noqa: F811
        ret = _retener(db, fake, almacen)
        for rol, pasa in (('lider_cartera', True), ('liquidador', False), ('admin', False)):
            r = client.post(f'/api/cartera/panel/retenciones/{ret.id}/convertir-contado',
                            json={'motivo': 'cliente pidió pagar contra entrega'},
                            headers=_jwt(app, _usuario(db, rol=rol)))
            assert (r.status_code != 403) is pasa, (rol, r.status_code, r.get_json())
            if pasa:
                assert r.status_code == 200, r.get_json()
                break
        # Y el panel se lo dice a la pantalla (los botones de decidir).
        d = client.get('/api/cartera/panel/retenciones?estado=RETENIDO',
                       headers=_jwt(app, _usuario(db, rol='lider_cartera'))).get_json()
        assert d['puede_autorizar'] is True and d['puede_autorizar_lote'] is True


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El aterrizaje (Node, app.js real)
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'), vm = require('vm');
const [base, rol] = process.argv.slice(1).filter(a => a !== '--');
const html = fs.readFileSync(base + '/index.html', 'utf8');
const navs = [...html.matchAll(/<div[^>]*class="nav-tab[^"]*"[^>]*onclick="([^"]*)"[^>]*>/g)]
  .map(m => ({ onclick: m[1], style: {}, classList: { toggle() {} },
               getAttribute(k) { return k === 'onclick' ? this.onclick : null; } }));
const contenidos = {};
const ctx = { console, window: { location: { origin: 'http://t' }, addEventListener() {} },
  document: { body: { appendChild() {} }, createElement: () => ({ style: {} }),
              getElementById: (id) => /^tab-/.test(id) ? (contenidos[id] = contenidos[id] || { style: {} }) : null,
              querySelector: () => null,
              querySelectorAll: (sel) => sel === '.nav-tab' ? navs
                : (sel.startsWith('.nav-tab[onclick*="') ? navs.filter(n => n.onclick.includes(sel.split('"')[1])) : []),
              addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, navigator: { onLine: true },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 1, clearInterval() {} };
ctx.globalThis = ctx; ctx.location = ctx.window.location; ctx.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['util.js', 'app.js']) vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
const pantallas = [], cargas = [];
vm.runInContext(`pantalla = (id) => __p.push(id); actualizarUI = () => {};
  cargarAdmin = async () => __c.push(TAB);`, Object.assign(ctx, { __p: pantallas, __c: cargas }));
ctx.OPERARIO = { rol, nombre: 'x' };
vm.runInContext(`OPERARIO = { rol: ${JSON.stringify(rol)}, nombre: 'x' }; mostrarSegunRol(${JSON.stringify(rol)});`, ctx);
const visibles = navs.filter(n => n.style.display !== 'none').map(n => (n.onclick.match(/tab\('([^']+)'\)/) || [])[1]).filter(Boolean);
console.log(JSON.stringify({ pantallas, cargas, visibles, activa: vm.runInContext('TAB', ctx) }));
"""


def _aterrizar(rol):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    p = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), rol],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


class TestElAterrizaje:

    @pytest.mark.parametrize('rol,pestanas', [
        ('liquidador', ['tab-liquidacion']),
        ('lider_cartera', ['tab-liquidacion', 'tab-cartera']),
        ('control_flota', ['tab-flota']),
    ])
    def test_ve_solo_sus_pestanas_y_aterriza_en_la_primera(self, rol, pestanas):
        d = _aterrizar(rol)
        assert d['pantallas'][-1] == 'pantalla-admin', d
        assert sorted(d['visibles']) == sorted(pestanas), d
        primera = {'liquidador': 'tab-liquidacion', 'lider_cartera': 'tab-cartera',
                   'control_flota': 'tab-flota'}[rol]
        assert d['activa'] == primera and d['cargas'][-1] == primera, d

    def test_el_admin_no_ve_la_pestana_propia_del_lider(self):
        d = _aterrizar('admin')
        assert 'tab-cartera' not in d['visibles'] and 'tab-liquidacion' in d['visibles'], d

    def test_el_orden_de_las_pestanas_es_el_de_tab(self):
        """`tab()` marca la activa por índice: la pestaña nueva tiene que estar
        en la misma posición en el HTML y en el arreglo `TABS`."""
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        en_html = re.findall(r'class="nav-tab[^"]*"[^>]*onclick="tab\(\'([^\']+)\'\)"', html)
        js = (PWA / 'app.js').read_text(encoding='utf-8')
        tabs = re.findall(r"'(tab-[a-z]+)'", re.search(r'const TABS = \[([^\]]+)\]', js).group(1))
        assert en_html == tabs[:len(en_html)] and len(en_html) == len(tabs), (en_html, tabs)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · La tarjeta de Liquidación ofrece a cada rol solo lo suyo (Node)
# ═════════════════════════════════════════════════════════════════════════════

class TestLaTarjetaOfreceSoloLoSuyo:
    """Los botones salen de `permisos` del detalle (`_liqPermiso`), que el
    servidor calcula con las mismas funciones que cortan con 403."""

    def _pintar(self, app, client, db, almacen, tmp_path, rol):
        import copy
        from tests.test_liquidacion_pantalla_dinero import _ruta_entregada
        from tests.test_sin_codigos_en_pantalla import _node
        ruta = _ruta_entregada(db, almacen)
        det = client.get(f'/api/rutas/{ruta.id}/liquidacion-detalle',
                         headers=_jwt(app, _usuario(db, rol=rol))).get_json()
        base = det['recaudos'][0]
        con_ret = copy.deepcopy(base)
        con_ret.update(id=base['id'] + 1, motivo_descuento='RETEFUENTE_2.5', monto_descuento=1000,
                       retencion_confirmada=None)
        sin_plata = copy.deepcopy(base)
        sin_plata.update(id=base['id'] + 2, credito_no_autorizado=True, credito_autorizado_en=None)
        det['recaudos'] = [base, con_ret, sin_plata]
        # Dos momentos de la misma ruta: por liquidar (el botón de liquidar,
        # el crédito sin plata) y liquidada (cobro, retención, corrección).
        html = ''
        for estado in ('PENDIENTE', 'LIQUIDADA'):
            d = copy.deepcopy(det)
            d['ruta']['estado_financiero'] = estado
            html += _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
                _liqDetalleRuta = DET;
                _liqRenderDetalle();
                return { html: document.getElementById('liq-modal-body').innerHTML };
            """, globales={'DET': d})['html']
        return html

    #: función del onclick → quién la ve. A mano.
    BOTONES = {
        'liqToggleCobro(': {'admin', 'liquidador'},
        'liqLiquidarWMS(': {'admin', 'liquidador'},
        'liqConfirmarRetencion(': {'admin', 'lider_cartera'},
        'liqAutorizarCredito(': {'admin', 'lider_cartera'},
        'liqCorregirMontoParada(': {'lider_cartera'},   # el admin corrige dentro del panel de cobro
    }

    @pytest.mark.parametrize('rol', ['admin', 'liquidador', 'lider_cartera', 'jefe_almacen'])
    def test_cada_boton_a_quien_lo_puede_usar(self, app, client, db, almacen, tmp_path, rol):
        html = self._pintar(app, client, db, almacen, tmp_path, rol)
        ve = {b for b in self.BOTONES if b in html}
        assert ve == {b for b, roles in self.BOTONES.items() if rol in roles}, (rol, ve)

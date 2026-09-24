"""Los filtros de Inventario Cíclico filtran lo que dicen (2026-09-24).

«En Conteos, al filtrar por marca no filtra en realidad por marca» y «hay
botones de las pestañas que no filtran bien». Lo que había, de punta a punta:

| Filtro | Defecto | Ahora |
|---|---|---|
| Marca | Buscaba en `Producto.categoria` (no es la marca, y ningún sync de Siesa la llena) | `Producto.marca_siesa`, sin distinguir mayúsculas; si ningún producto tiene marca **se dice** (`marca.aviso`) |
| ⚠ Acción | El JS quitaba los CC2/CC3 **después** de paginar: total, contador y páginas contaban hijos que quedan en DESCUADRE para siempre | Solo raíces, en la consulta (`conteo_listado.VISTAS`) |
| ✓ Resueltos | La raíz y su CC2 como dos resueltos del mismo hueco | Solo raíces |
| Almacén | La barra, «Asignar» y «Exportar» leían el selector escondido de ABC; la lista no filtraba | Un filtro `inv-filtro-almacen` para los cuatro |
| Texto / pestañas | Una petición por tecla; la última en LLEGAR pintaba, y al cambiar de pestaña quedaban las tarjetas viejas | Debounce + la respuesta vieja no pinta + «Cargando» al cambiar de filtro |
| «Hoy» de la barra | Medianoche UTC (Regla 5) | Día operativo Bogotá |
| Exportar | Día UTC, y una fecha ilegible exportaba TODO | Día Bogotá; ilegible → 400 |
| Parámetros | `almacen_id=abc` o `clasificacion=X` se ignoraban: la lista de otro filtro con cara de ser la pedida | 400 |

Backend con el test client y datos que distinguen; frontend con `conteo.js` en
Node sobre `util.js` REAL.
"""
import json
import pathlib
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


# ─────────────────────────────────────────────────────────────────────────────
# Datos que distinguen: dos almacenes, dos marcas, tres clases, cadenas
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mundo(db, almacen):
    from app.models.almacen import Almacen
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    from app.models.usuario import Usuario

    otro = Almacen(codigo='ALM-2', nombre='Almacén Dos', bodega_siesa_id='NS1', activo=True)
    db.session.add(otro)
    db.session.flush()
    prods = {}
    # La categoría del de M003 dice «M175» a propósito: si el filtro volviera
    # a buscar en categoría, `marca=m175` lo traería.
    for cod, marca, categoria in (('P-NORMA', 'M003', 'M175'),
                                  ('P-SCRIBE', 'M175', None),
                                  ('P-SIN', None, None)):
        p = Producto(codigo=cod, nombre=cod, codigo_siesa=cod, marca_siesa=marca,
                     categoria=categoria)
        db.session.add(p)
        prods[cod] = p
    ubs = {}
    for alm in (almacen, otro):
        u = Ubicacion(codigo=f'U-{alm.codigo}', almacen_id=alm.id, tipo_zona='PICKING',
                      stock_minimo=0, stock_maximo=999, secuencia_ruteo=1, activo=True)
        db.session.add(u)
        ubs[alm.id] = u
    sup = Usuario(nombre='sup', email='sup-filtros@test.com',
                  password_hash=generate_password_hash('x'), rol='supervisor',
                  almacen_id=almacen.id, activo=True)
    db.session.add(sup)
    db.session.commit()
    return {'a1': almacen, 'a2': otro, 'prods': prods, 'ubs': ubs, 'sup': sup}


_N = [0]


def _s(db, m, *, estado, alm='a1', prod='P-SIN', clase='C', origen=None,
       operario_id=None, **kw):
    from app.models.conteo import SesionConteo
    from app.models.ubicacion import Ubicacion
    _N[0] += 1
    a = m[alm]
    # Un hueco por sesión (el índice de «una cadena viva por hueco»); el hijo
    # va en el hueco de su raíz, como en la vida real.
    if origen is not None:
        ub_id = origen.ubicacion_id
    else:
        ub = Ubicacion(codigo=f'U-{_N[0]}', almacen_id=a.id, tipo_zona='PICKING',
                       stock_minimo=0, stock_maximo=999, secuencia_ruteo=1, activo=True)
        db.session.add(ub)
        db.session.flush()
        ub_id = ub.id
    s = SesionConteo(codigo=f'T-{_N[0]}', tipo=kw.pop('tipo', 'DIARIO_ABC'),
                     clasificacion_abc=clase, ubicacion_id=ub_id,
                     almacen_id=a.id, producto_id=m['prods'][prod].id, estado=estado,
                     es_segundo_conteo=origen is not None,
                     sesion_origen_id=origen.id if origen is not None else None,
                     operario_id=operario_id, **kw)
    db.session.add(s)
    db.session.flush()
    return s


def _tok(app, u):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


def _lista(client, app, m, **q):
    qs = '&'.join(f'{k}={v}' for k, v in q.items())
    r = client.get(f'/api/conteo/?{qs}', headers=_tok(app, m['sup']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _ids(d):
    return {s['id'] for s in d['sesiones']}


# ─────────────────────────────────────────────────────────────────────────────
# Pestañas: Acción / En progreso / Resueltos
# ─────────────────────────────────────────────────────────────────────────────

class TestLasPestanasMuestranLoQueDicen:

    @pytest.fixture
    def cadenas(self, db, mundo):
        m = mundo
        # Cadena resuelta por CC1 == CC2: la raíz AJUSTADO, el CC2 en DESCUADRE
        # para siempre (así lo deja el servicio).
        r_aj = _s(db, m, estado='AJUSTADO')
        h_aj = _s(db, m, estado='DESCUADRE', origen=r_aj)
        # Raíz esperando CC2 pendiente.
        r_esp = _s(db, m, estado='SEGUNDO_CONTEO')
        h_esp = _s(db, m, estado='PENDIENTE', origen=r_esp)
        # Raíz en DESCUADRE esperando al supervisor, con su CC2 en DESCUADRE.
        r_des = _s(db, m, estado='DESCUADRE')
        h_des = _s(db, m, estado='DESCUADRE', origen=r_des)
        # Cadena que cuadró en el CC2: raíz y CC2 MATCH.
        r_ok = _s(db, m, estado='MATCH')
        h_ok = _s(db, m, estado='MATCH', origen=r_ok)
        suelto = _s(db, m, estado='PENDIENTE')
        db.session.commit()
        return dict(r_aj=r_aj, h_aj=h_aj, r_esp=r_esp, h_esp=h_esp, r_des=r_des,
                    h_des=h_des, r_ok=r_ok, h_ok=h_ok, suelto=suelto)

    def test_accion_solo_raices_y_el_total_es_la_lista(self, app, client, mundo, cadenas):
        c = cadenas
        d = _lista(client, app, mundo, vista='accion')
        assert _ids(d) == {c['r_esp'].id, c['r_des'].id}, (
            'Acción trajo CC2/CC3: el hijo de una cadena resuelta queda en '
            'DESCUADRE para siempre y el contador crecía sin techo')
        assert d['total'] == 2

    def test_resueltos_una_tarjeta_por_cadena(self, app, client, mundo, cadenas):
        c = cadenas
        d = _lista(client, app, mundo, vista='resueltos')
        assert _ids(d) == {c['r_aj'].id, c['r_ok'].id}
        assert d['total'] == 2

    def test_en_progreso_incluye_el_cc2_pendiente(self, app, client, mundo, cadenas):
        c = cadenas
        d = _lista(client, app, mundo, vista='progreso')
        assert _ids(d) == {c['h_esp'].id, c['suelto'].id}, (
            'un CC2 pendiente es una tarea que alguien tiene que contar')

    def test_el_contador_de_accion_es_el_total_de_accion(self, app, client, mundo, cadenas):
        r = client.get('/api/conteo/stats', headers=_tok(app, mundo['sup']))
        assert r.status_code == 200
        assert r.get_json()['accion_requerida'] == _lista(client, app, mundo, vista='accion')['total'] == 2
        assert r.get_json()['resueltos'] == 2

    def test_estados_explicitos_siguen_sirviendo(self, app, client, mundo, cadenas):
        """Un PWA viejo (en caché del service worker) manda `estados`."""
        d = _lista(client, app, mundo, estados='DESCUADRE')
        assert d['total'] == 3  # raíz + dos hijos: la lista cruda, como antes

    def test_la_paginacion_cuenta_lo_que_pinta(self, db, app, client, mundo):
        """31 raíces en Acción con un CC2 viejo cada una: 2 páginas (30 + 1),
        no 3 páginas de las que una llega casi vacía."""
        for _ in range(31):
            r = _s(db, mundo, estado='DESCUADRE')
            _s(db, mundo, estado='DESCUADRE', origen=r)
        db.session.commit()
        p1 = _lista(client, app, mundo, vista='accion', page=1)
        p2 = _lista(client, app, mundo, vista='accion', page=2)
        assert (p1['total'], p1['total_paginas']) == (31, 2)
        assert (len(p1['sesiones']), len(p2['sesiones'])) == (30, 1)
        assert not (_ids(p1) & _ids(p2))


# ─────────────────────────────────────────────────────────────────────────────
# Almacén, clase, marca
# ─────────────────────────────────────────────────────────────────────────────

class TestFiltrosSecundarios:

    @pytest.fixture
    def filas(self, db, mundo):
        m = mundo
        f = {
            'a1_A_norma': _s(db, m, estado='PENDIENTE', alm='a1', clase='A', prod='P-NORMA'),
            'a1_B_scribe': _s(db, m, estado='PENDIENTE', alm='a1', clase='B', prod='P-SCRIBE'),
            'a2_A_scribe': _s(db, m, estado='PENDIENTE', alm='a2', clase='A', prod='P-SCRIBE'),
            'a2_C_sin': _s(db, m, estado='PENDIENTE', alm='a2', clase='C', prod='P-SIN'),
        }
        db.session.commit()
        return f

    def test_almacen(self, app, client, mundo, filas):
        d = _lista(client, app, mundo, vista='progreso', almacen_id=mundo['a2'].id)
        assert _ids(d) == {filas['a2_A_scribe'].id, filas['a2_C_sin'].id}
        assert d['total'] == 2

    def test_clase(self, app, client, mundo, filas):
        d = _lista(client, app, mundo, vista='progreso', clasificacion='A')
        assert _ids(d) == {filas['a1_A_norma'].id, filas['a2_A_scribe'].id}
        d = _lista(client, app, mundo, vista='progreso', clasificacion='b')
        assert _ids(d) == {filas['a1_B_scribe'].id}

    def test_marca_es_la_marca_no_la_categoria(self, app, client, mundo, filas):
        d = _lista(client, app, mundo, vista='progreso', marca='m175')
        assert _ids(d) == {filas['a1_B_scribe'].id, filas['a2_A_scribe'].id}, (
            'el filtro de marca volvió a buscar en otra columna')
        assert d['total'] == 2
        assert d['marca'] == {'productos_con_marca': 2, 'aviso': None}

    def test_se_combinan(self, app, client, mundo, filas):
        d = _lista(client, app, mundo, vista='progreso', marca='M175',
                   almacen_id=mundo['a2'].id, clasificacion='A')
        assert _ids(d) == {filas['a2_A_scribe'].id}

    def test_sin_marcas_cargadas_se_dice(self, db, app, client, mundo, filas):
        from app.models.producto import Producto
        Producto.query.update({Producto.marca_siesa: None})
        db.session.commit()
        d = _lista(client, app, mundo, vista='progreso', marca='norma')
        assert d['total'] == 0
        assert d['marca']['productos_con_marca'] == 0
        assert 'marca' in d['marca']['aviso'].lower()

    def test_la_barra_se_filtra_por_el_almacen(self, app, client, mundo, filas):
        r = client.get(f'/api/conteo/stats?almacen_id={mundo["a2"].id}',
                       headers=_tok(app, mundo['sup'])).get_json()
        assert r['pendientes'] == 2
        r = client.get('/api/conteo/stats', headers=_tok(app, mundo['sup'])).get_json()
        assert r['pendientes'] == 4


class TestUnFiltroIlegibleEs400:

    @pytest.mark.parametrize('qs', [
        'vista=otra', 'clasificacion=X', 'almacen_id=abc', 'estados=NOPE',
        'vista=accion&estados=DESCUADRE', 'page=dos', 'operario_id=x'])
    def test_listado(self, app, client, mundo, qs):
        r = client.get(f'/api/conteo/?{qs}', headers=_tok(app, mundo['sup']))
        assert r.status_code == 400, (qs, r.get_json())

    def test_barra(self, app, client, mundo):
        r = client.get('/api/conteo/stats?almacen_id=abc', headers=_tok(app, mundo['sup']))
        assert r.status_code == 400

    @pytest.mark.parametrize('qs', ['desde=10/09/2026', 'hasta=ayer',
                                    'desde=2026-09-11&hasta=2026-09-10'])
    def test_exportar(self, app, client, mundo, qs):
        r = client.get(f'/api/conteo/exportar?{qs}', headers=_tok(app, mundo['sup']))
        assert r.status_code == 400, qs


# ─────────────────────────────────────────────────────────────────────────────
# Días: «Hoy» de la barra y el rango de Exportar son días de Bogotá
# ─────────────────────────────────────────────────────────────────────────────

class TestLosDiasSonDeBogota:

    def test_hoy_arranca_a_la_medianoche_de_bogota(self, db, app, client, mundo):
        from app.utils.fecha import inicio_del_dia_utc
        ini = inicio_del_dia_utc()
        dentro = _s(db, mundo, estado='MATCH', fecha_cierre=ini + timedelta(minutes=1))
        _s(db, mundo, estado='MATCH', fecha_cierre=ini - timedelta(minutes=1))
        db.session.commit()
        r = client.get('/api/conteo/stats', headers=_tok(app, mundo['sup'])).get_json()
        assert dentro.id and r['hoy_completados'] == 1, (
            'el «Hoy» de la barra no es el día operativo: se reinicia a las 7 p. m.')

    def test_exportar_corta_por_dia_de_bogota(self, db, app, client, mundo):
        from app.utils.fecha import inicio_del_dia_utc
        d = date(2026, 9, 10)
        ini, fin = inicio_del_dia_utc(d), inicio_del_dia_utc(d + timedelta(days=1))
        adentro = [_s(db, mundo, estado='MATCH', fecha_creacion=ini + timedelta(minutes=1)),
                   _s(db, mundo, estado='MATCH', fecha_creacion=fin - timedelta(minutes=1))]
        afuera = [_s(db, mundo, estado='MATCH', fecha_creacion=ini - timedelta(minutes=1)),
                  _s(db, mundo, estado='MATCH', fecha_creacion=fin + timedelta(minutes=1))]
        db.session.commit()
        texto = client.get('/api/conteo/exportar?desde=2026-09-10&hasta=2026-09-10',
                           headers=_tok(app, mundo['sup'])).get_data(as_text=True)
        codigos = {l.split(',')[0] for l in texto.splitlines() if l.startswith('T-')}
        assert codigos == {s.codigo for s in adentro}, (codigos, [s.codigo for s in afuera])


class TestSinAsignarEsLoQueElBotonReparte:

    def test_el_cc3_no_cuenta(self, db, app, client, mundo):
        r = _s(db, mundo, estado='TERCER_CONTEO')
        cc2 = _s(db, mundo, estado='DESCUADRE', origen=r)
        _s(db, mundo, estado='PENDIENTE', origen=cc2)          # CC3 sin dueño
        _s(db, mundo, estado='PENDIENTE')                      # CC1 sin dueño
        db.session.commit()
        d = client.get('/api/conteo/stats', headers=_tok(app, mundo['sup'])).get_json()
        assert d['sin_asignar'] == 1, (
            '«Asignar N pendientes» prometía el CC3, que es de supervisión y '
            'asignar-lote nunca reparte')


class TestPestanaAbc:

    def test_el_resumen_es_del_almacen_elegido(self, db, app, client, mundo):
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC as P
        pr = mundo['prods']
        for prod, alm, clase in (('P-NORMA', 'a1', 'A'), ('P-SCRIBE', 'a1', 'A'),
                                 ('P-SIN', 'a1', 'C'), ('P-NORMA', 'a2', 'B')):
            db.session.add(P(producto_id=pr[prod].id, almacen_id=mundo[alm].id, clasificacion=clase))
        db.session.commit()
        h = _tok(app, mundo['sup'])
        d1 = client.get(f'/api/conteo/abc/resumen?almacen_id={mundo["a1"].id}', headers=h).get_json()
        d2 = client.get(f'/api/conteo/abc/resumen?almacen_id={mundo["a2"].id}', headers=h).get_json()
        assert {k: v['total_productos'] for k, v in d1['distribucion_abc'].items()} == {'A': 2, 'B': 0, 'C': 1}
        assert {k: v['total_productos'] for k, v in d2['distribucion_abc'].items()} == {'A': 0, 'B': 1, 'C': 0}


# ─────────────────────────────────────────────────────────────────────────────
# La política vive en el servicio
# ─────────────────────────────────────────────────────────────────────────────

class TestLaRutaSoloParsea:

    def test_la_ruta_no_arma_filtros(self):
        import ast
        arbol = ast.parse((RAIZ / 'app' / 'routes' / 'conteo.py').read_text(encoding='utf-8'))
        fns = {n.name: n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)}
        for nombre, delega in (('listar_sesiones', 'listar'), ('stats_conteo', 'barra')):
            fn = fns[nombre]
            atributos = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            assert not atributos & {'filter', 'filter_by', 'join', 'in_', 'ilike', 'count'}, (
                f'{nombre} volvió a armar la consulta en la ruta')
            assert delega in atributos, f'{nombre} no delega en conteo_listado.{delega}'


# ─────────────────────────────────────────────────────────────────────────────
# Frontend: el parámetro viaja, el botón activo se marca y se conserva
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, value: '', innerHTML: '', textContent: '', options: [] });
const urls = []; const posts = []; const pendientes = [];
const ctx = { console, window: {}, document: { getElementById: el }, URLSearchParams,
  setTimeout: (fn) => { pendientes.push(fn); return pendientes.length; }, clearTimeout: () => {},
  alerta: () => {}, API: '',
  _modalTexto: async () => '',
  _fetchConTimeout: async (u) => { urls.push(u); return { ok: false, json: async () => ({ error: 'x' }) }; },
  post: async (u, b) => { posts.push([u, b]); return { asignadas: 0 }; } };
// get: cada llamada queda en espera; el arnés decide cuándo y con qué contesta.
const esperas = [];
ctx.get = (u) => { urls.push(u); return new Promise(res => esperas.push({ u, res })); };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
const R = (s) => vm.runInContext(s, ctx);
const tick = () => new Promise(r => setImmediate(r));
const resp = (id, extra) => ({ sesiones: [{ id, codigo: 'C' + id, estado: 'PENDIENTE', producto_codigo: 'P' + id,
  producto_nombre: 'N' + id }], total: 1, total_paginas: 1, ...(extra || {}) });
const contestar = (pred, body) => { const i = esperas.findIndex(e => pred(e.u)); const e = esperas.splice(i, 1)[0]; e.res(body); };
const lista = (u) => u.startsWith('/api/conteo/?');
const stats = (u) => u.startsWith('/api/conteo/stats');
const out = {};
(async () => {
  el('inv-filtro-almacen').value = '7';
  el('inv-filtro-marca').value = ' norma ';
  el('inv-filtro-clase').value = 'B';
  el('inv-abc-almacen').value = '99';          // el de ABC ya no manda en Conteos
  el('inv-conteos-lista').innerHTML = 'x';

  // 1. Cambiar a Acción: viaja la vista y los filtros; el botón se marca.
  R("conteoVista('accion')");
  await tick();
  out.urlAccion = urls.filter(lista).pop();
  out.urlStats = urls.filter(stats).pop();
  out.botones = ['accion', 'progreso', 'resueltos'].map(k => [k, els['cv-tab-' + k].style.background]);
  out.cargandoAlCambiar = els['inv-conteos-lista'].innerHTML.includes('Cargando');
  contestar(stats, { accion_requerida: 5 });
  await tick();
  out.badge = els['cv-badge-accion'].textContent;

  // 2. Carrera: se pide Resueltos antes de que Acción conteste. Acción
  //    contesta DESPUÉS y no debe pisar a Resueltos.
  R("conteoVista('resueltos')");
  await tick();
  contestar(u => lista(u) && u.includes('vista=resueltos'), resp(2));
  await tick();
  contestar(u => lista(u) && u.includes('vista=accion'), resp(1));
  await tick();
  out.pintadoTrasCarrera = els['inv-conteos-lista'].innerHTML;

  // 3. Recargar (conteosFiltrar tras una acción) conserva la pestaña y su botón.
  R('conteosFiltrar()');
  await tick();
  out.urlRecarga = urls.filter(lista).pop();
  out.botonesRecarga = ['accion', 'progreso', 'resueltos'].map(k => [k, els['cv-tab-' + k].style.background]);
  contestar(u => lista(u) && u.includes('vista=resueltos'), resp(3, { marca: { productos_con_marca: 0, aviso: 'Ningún producto <b>tiene</b> marca' } }));
  await tick();
  out.aviso = els['inv-filtro-aviso'].innerHTML;

  // 4. Tipear no dispara una petición por tecla: se agenda.
  const antes = urls.filter(lista).length;
  R('conteosFiltrarTexto()'); R('conteosFiltrarTexto()'); R('conteosFiltrarTexto()');
  await tick();
  out.peticionesAlTipear = urls.filter(lista).length - antes;
  const agendada = pendientes.pop(); if (agendada) agendada();
  await tick();
  out.peticionesTrasPausa = urls.filter(lista).length - antes;

  // 5. Exportar y Asignar usan el almacén de Conteos.
  await R('conteoExportar()');
  out.urlExportar = urls.filter(u => u.startsWith('/api/conteo/exportar')).pop();
  el('conteo-asignar-operario').value = '4';
  R('conteoAsignarLote()');  // después espera la barra: no se espera
  await tick();
  out.asignar = posts.filter(p => p[0] === '/api/conteo/asignar-lote').pop();

  // 6. Estadísticas: los filtros elegidos viajan.
  R('conteoEstIniciar'); // existe
  el('ce-desde').value = '2026-09-01'; el('ce-hasta').value = '2026-09-10';
  el('ce-almacen').value = '7'; el('ce-clase').value = 'A'; el('ce-tipo').value = 'MANUAL';
  el('inv-estadisticas-contenido');
  R('conteoEstCargar()');
  await tick();
  out.urlEst = urls.filter(u => u.startsWith('/api/conteo/estadisticas')).pop();

  // 7. ABC: el resumen y los lotes van con el almacén de SU selector.
  el('inv-abc-almacen').value = '5';
  el('inv-abc-resumen');
  R('cargarResumenAbc()');
  await tick();
  out.urlAbc = urls.filter(u => u.startsWith('/api/conteo/abc/resumen')).pop();
  R("generarAbc('B')");
  await tick();
  out.lote = posts.filter(p => p[0] === '/api/conteo/abc/generar-tareas').pop();

  // 8. Líder: el almacén de su selector.
  el('lider-almacen').options = [1]; el('lider-almacen').value = '3';
  el('inv-lider-contenido');
  R('liderCargar()');
  await tick();
  out.urlLider = urls.filter(u => u.startsWith('/api/conteo/lider/tablero')).pop();
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.fixture(scope='module')
def js():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _qs(url):
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


class TestElFiltroViajaDesdeLaPantalla:

    def test_la_pestana_viaja_como_vista(self, js):
        q = _qs(js['urlAccion'])
        assert q.get('vista') == 'accion' and 'estados' not in q, js['urlAccion']

    def test_viajan_almacen_marca_y_clase(self, js):
        q = _qs(js['urlAccion'])
        assert (q.get('almacen_id'), q.get('marca'), q.get('clasificacion')) == ('7', 'norma', 'B')

    def test_la_barra_usa_el_almacen_de_conteos(self, js):
        assert _qs(js['urlStats']).get('almacen_id') == '7', js['urlStats']

    def test_el_contador_de_accion_sale_de_la_barra(self, js):
        assert str(js['badge']) == '5'

    def test_el_boton_activo_se_marca(self, js):
        activos = [k for k, bg in js['botones'] if bg == 'var(--pm)']
        assert activos == ['accion'], js['botones']

    def test_al_cambiar_de_pestana_no_quedan_las_tarjetas_viejas(self, js):
        assert js['cargandoAlCambiar']

    def test_una_respuesta_vieja_no_pisa_la_vigente(self, js):
        assert 'N2' in js['pintadoTrasCarrera'] and 'N1' not in js['pintadoTrasCarrera'], (
            'la respuesta de Acción, que llegó tarde, pintó bajo el botón de Resueltos')

    def test_recargar_conserva_la_pestana(self, js):
        assert _qs(js['urlRecarga']).get('vista') == 'resueltos'
        assert [k for k, bg in js['botonesRecarga'] if bg == 'var(--pm)'] == ['resueltos']

    def test_el_aviso_de_marca_se_pinta_escapado(self, js):
        assert 'Ningún producto &lt;b&gt;tiene&lt;/b&gt; marca' in js['aviso']

    def test_tipear_no_manda_una_peticion_por_tecla(self, js):
        assert (js['peticionesAlTipear'], js['peticionesTrasPausa']) == (0, 1)

    def test_exportar_y_asignar_usan_el_almacen_de_conteos(self, js):
        assert _qs(js['urlExportar']).get('almacen_id') == '7', js['urlExportar']
        assert js['asignar'][1]['almacen_id'] == 7

    def test_estadisticas_mandan_sus_filtros(self, js):
        assert _qs(js['urlEst']) == {'desde': '2026-09-01', 'hasta': '2026-09-10',
                                     'almacen_id': '7', 'clase': 'A', 'tipo': 'MANUAL'}

    def test_abc_usa_su_almacen_y_la_clase_del_boton(self, js):
        assert _qs(js['urlAbc']) == {'almacen_id': '5'}
        assert js['lote'][1] == {'almacen_id': 5, 'clasificacion': 'B'}

    def test_lider_usa_su_selector(self, js):
        assert _qs(js['urlLider']) == {'almacen_id': '3'}


class TestElHtmlTieneLosFiltros:

    def test_el_almacen_de_conteos_existe_y_arranca_en_todos(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        i = html.index('id="inv-filtro-almacen"')
        assert 'onchange="conteosFiltrar()"' in html[i:i + 300]
        assert '<option value="">Todos los almacenes</option>' in html[i:i + 500]
        assert 'oninput="conteosFiltrarTexto()"' in html
        assert 'id="inv-filtro-aviso"' in html

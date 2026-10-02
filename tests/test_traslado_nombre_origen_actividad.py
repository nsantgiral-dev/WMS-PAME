"""Un mensaje solo afirma lo que un dato prueba (2026-10-02).

Pedidos del dueño de ese día, con la revisión de los paquetes:

1. Todo pedido de traslado dice el NOMBRE del producto (la referencia queda
   pequeña al lado, para bodega) y DE DÓNDE se pidió.
2. «🔍 Operario pickeando...» y «📦 Empacando en …» salían del estado del
   documento, no de las tareas: un traslado que nadie tomó decía que alguien
   lo pickeaba. Ahora lo dice `traslado_actividad`, con datos reales.
3. El empacador leía «El operario aún está pickeando» con un picking que
   nadie tomó.
4. El semáforo de Siesa decía «Conectado» porque el circuito estaba cerrado,
   que es su estado por defecto aunque nadie le haya hablado a Siesa en horas.

Y los hallazgos de la revisión de 5cbc13c3..fd01da38: lo que va a Siesa no
cambia por las filas sin código, el escaneo solo usa paquetes escaneables, la
unidad base del ítem es factor 1, un paquete que Siesa quitó se apaga, la
lista de compras usa la misma política, sin N+1, sin el catálogo entero por
request, `esc()` en recepción, una sola validación de enteros.
"""
import ast
import json
import pathlib
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from app.extensions import db as _db
from app.models.producto import Producto
from app.models.producto_empaque import ProductoEmpaque
from app.services import empaque_producto as ep
from app.services import traslado_actividad as ta

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
MALO = '<img src=x onerror=alert(1)>'

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const guion = args[1];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', value: '', textContent: '' });
const alertas = [];
const ctx = { console, els, alertas,
  document: { getElementById: el, createElement: () => ({ style: {} }) },
  alerta: (m, t) => alertas.push([String(m), t]),
  get: () => Promise.resolve({}), post: () => Promise.resolve({}),
  OPERARIO: { rol: 'admin' },
};
vm.createContext(ctx);
for (const f of args.slice(2)) vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
(async () => {
  const salida = await vm.runInContext(guion, ctx);
  console.log(JSON.stringify(salida === undefined ? null : salida));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""


def correr(guion, archivos=('util.js',)):
    if not shutil.which('node'):
        pytest.skip('sin node')
    envuelto = f'(async () => {{ {guion} }})()'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), envuelto, *archivos],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


SOL = {'id': 7, 'codigo': 'ST-20261002-1562', 'estado': 'ENVIADA', 'total_items': 1,
       'bodega_origen_siesa': 'NB1', 'bodega_destino_siesa': 'PC1',
       'nombre_punto_venta': 'Pitalito Centro', 'fecha_creacion': '2026-10-02T10:00:00',
       'items': [{'id': 1, 'producto_id': 1, 'producto_codigo': 'PAPELSP249',
                  'producto_codigo_siesa': 'PAPELSP249',
                  'producto_nombre': 'BOLIGRAFO DORICOLOR 07 AZUL TAPA',
                  'cantidad_solicitada': 15,
                  'empaque': {'unidad': 'PQ', 'factor': 12},
                  'pedido_como': {'paquetes': 1, 'sueltas': 3, 'factor': 12, 'unidad': 'PQ'}}]}


# ── 1 · Nombre del producto y de dónde se pidió ───────────────────────────────

class TestNombreYOrigenEnPantalla:
    def test_mis_pedidos_de_la_tienda(self):
        h = correr(f"""get = () => Promise.resolve({{ solicitudes: [{json.dumps(SOL)}] }});
            await tiendaCargarSolicitudes(); return els['tienda-lista-solicitudes'].innerHTML;""",
                   ('util.js', 'traslados.js', 'tienda.js'))
        assert 'BOLIGRAFO DORICOLOR 07 AZUL TAPA' in h
        assert 'Pedido a Bodega Principal (NB1)' in h
        assert '1 PQ + 3 und = 15 und' in h
        assert 'PAPELSP249 —' not in h, 'la referencia ya no encabeza la línea'

    def test_tarjeta_del_admin(self):
        h = correr(f"return _renderTrasladoCard({json.dumps(SOL)});", ('util.js', 'traslados.js'))
        assert 'Bodega Principal (NB1) → Pitalito Centro (PC1)' in h
        assert 'BOLIGRAFO DORICOLOR 07 AZUL TAPA' in h

    def test_tarjeta_del_operario(self):
        h = correr(f"return _renderTrasladoOperario({json.dumps(SOL)});", ('util.js', 'traslados.js'))
        assert 'Bodega Principal (NB1) → Pitalito Centro (PC1)' in h
        assert 'BOLIGRAFO DORICOLOR 07 AZUL TAPA' in h

    def test_un_nombre_malicioso_se_escapa(self):
        s = dict(SOL, items=[dict(SOL['items'][0], producto_nombre=MALO)])
        for fn in ('_renderTrasladoCard', '_renderTrasladoOperario', '_renderRequisicionCard'):
            h = correr(f"return {fn}({json.dumps(s)});", ('util.js', 'traslados.js'))
            assert '<img' not in h, fn

    def test_recepcion_escapa_el_nombre(self):
        """Hallazgo 8: `${i.producto_nombre || i.producto_codigo}` sin `esc()`."""
        h = correr(f"""return _recepRenderItemsTraslado([{{producto_id: 1, producto_nombre: {json.dumps(MALO)},
            producto_codigo: 'X', cantidad_enviada: 2}}]);""", ('util.js', 'recepcion.js'))
        assert '<img' not in h and '&lt;img' in h

    def test_un_solo_mapa_de_nombres_de_bodega(self):
        """El mapa vivía en traslados.js (`_REQ_BODEGA_NOMBRES`): ahora es
        `BODEGA_NOMBRES` de util.js y nadie más define uno."""
        for f in PWA.glob('*.js'):
            t = f.read_text(encoding='utf-8')
            if f.name == 'util.js':
                assert 'const BODEGA_NOMBRES' in t
                continue
            assert '_REQ_BODEGA_NOMBRES' not in t and '_reqNombreBodega' not in t, f.name


# ── 2 · ¿Alguien está pickeando o empacando de verdad? ────────────────────────

def _tp(estado='PENDIENTE', operario=None, inicio=None, fin=None, creada=None, ultimo=None):
    from types import SimpleNamespace
    return SimpleNamespace(estado=estado, operario_id=operario, ultimo_operario_id=ultimo,
                           fecha_creacion=creada or datetime(2026, 10, 2, 8, 0),
                           fecha_inicio=inicio, fecha_completado=fin)


AHORA = datetime(2026, 10, 2, 12, 0)
NOMBRES = {1: 'Ana', 2: 'Beto'}


class TestActividadDelPicking:
    def test_sin_tareas(self):
        assert ta.actividad_picking([], NOMBRES, AHORA)['actividad'] == ta.SIN_TAREAS

    def test_nadie_la_tomo(self):
        a = ta.actividad_picking([_tp(), _tp()], NOMBRES, AHORA)
        assert a['actividad'] == ta.SIN_TOMAR and a['operarios'] == [] and a['minutos'] == 240

    def test_asignada_sin_actividad(self):
        a = ta.actividad_picking([_tp(operario=1, inicio=AHORA - timedelta(hours=3))], NOMBRES, AHORA)
        assert a['actividad'] == ta.SIN_ACTIVIDAD and a['operarios'] == ['Ana'] and a['minutos'] == 180

    def test_asignada_que_nunca_empezo_no_es_actividad(self):
        a = ta.actividad_picking([_tp(operario=1)], NOMBRES, AHORA)
        assert a['actividad'] == ta.SIN_ACTIVIDAD
        # Recién asignada (hace 5 min) y sin tocar: tampoco es «pickeando».
        a = ta.actividad_picking([_tp(operario=1, creada=AHORA - timedelta(minutes=5))], NOMBRES, AHORA)
        assert (a['actividad'], a['minutos']) == (ta.SIN_ACTIVIDAD, 5)

    def test_pickeando_dentro_del_umbral(self):
        ts = [_tp('COMPLETADO', operario=1, inicio=AHORA - timedelta(minutes=20),
                  fin=AHORA - timedelta(minutes=5)), _tp('EN_PROCESO', operario=1)]
        a = ta.actividad_picking(ts, NOMBRES, AHORA)
        assert (a['actividad'], a['minutos'], a['ultima_actividad'], a['completadas'], a['total']) == \
               (ta.ACTIVO, 5, 'TERMINADA', 1, 2)

    def test_el_umbral_es_uno(self):
        ts = [_tp('EN_PROCESO', operario=1, inicio=AHORA - timedelta(minutes=ta.UMBRAL_SIN_ACTIVIDAD_MIN + 1))]
        assert ta.actividad_picking(ts, NOMBRES, AHORA)['actividad'] == ta.SIN_ACTIVIDAD
        ts = [_tp('EN_PROCESO', operario=1, inicio=AHORA - timedelta(minutes=ta.UMBRAL_SIN_ACTIVIDAD_MIN))]
        assert ta.actividad_picking(ts, NOMBRES, AHORA)['actividad'] == ta.ACTIVO

    def test_completo(self):
        a = ta.actividad_picking([_tp('COMPLETADO', operario=1, fin=AHORA)], NOMBRES, AHORA)
        assert a['actividad'] == ta.COMPLETO

    def test_nadie_tiene_lo_que_falta_dice_quien_lo_trabajo(self):
        ts = [_tp('COMPLETADO', fin=AHORA - timedelta(minutes=50), ultimo=2), _tp(ultimo=2)]
        a = ta.actividad_picking(ts, NOMBRES, AHORA)
        assert a['actividad'] == ta.SIN_TOMAR and a['operarios_anteriores'] == ['Beto']


class TestTextosDeLaRequisicion:
    def _texto(self, pp):
        return correr(f"return _reqActividadPicking({json.dumps(pp)}).texto;", ('util.js', 'traslados.js'))

    def test_nunca_pickeando_sin_operario(self):
        t = self._texto({'actividad': 'SIN_TOMAR', 'completadas': 0, 'total': 2, 'minutos': 240, 'operarios': []})
        assert t.startswith('Nadie la ha tomado') and 'pickeando' not in t

    def test_asignada_sin_actividad(self):
        t = self._texto({'actividad': 'SIN_ACTIVIDAD', 'completadas': 0, 'total': 2, 'minutos': 180,
                         'operarios': ['Ana']})
        assert t == 'Asignada a Ana · sin actividad hace 3 h · 0 de 2 líneas'

    def test_pickeando(self):
        t = self._texto({'actividad': 'ACTIVO', 'completadas': 1, 'total': 2, 'minutos': 5,
                         'operarios': ['Ana'], 'ultima_actividad': 'TERMINADA'})
        assert t == 'Ana pickeando · 1 de 2 líneas · terminó una línea hace 5 min'

    def test_sin_tareas(self):
        assert self._texto({'actividad': 'SIN_TAREAS'}) == '⚠ Sin tareas de picking: revisar'

    def test_la_tarjeta_usa_el_dato_y_no_el_estado(self):
        r = dict(SOL, estado='EN_PICKING', picking_progreso={'actividad': 'SIN_TOMAR', 'completadas': 0,
                                                             'total': 1, 'minutos': 30, 'operarios': []})
        h = correr(f"return _renderRequisicionCard({json.dumps(r)});", ('util.js', 'traslados.js'))
        assert 'Nadie la ha tomado' in h and 'Operario pickeando' not in h
        assert '1 PQ + 3 und = 15 und' in h, 'las líneas muestran el paquete'

    def test_empaque_sin_asignar_no_dice_empacando(self):
        r = dict(SOL, estado='EN_PACKING', packing_info={'codigo': 'PK-1', 'actividad': 'SIN_ASIGNAR',
                                                         'minutos': 50, 'empacador': None})
        h = correr(f"return _renderRequisicionCard({json.dumps(r)});", ('util.js', 'traslados.js'))
        assert 'Empaque sin asignar' in h and 'empacando' not in h


class TestActividadEnElServidor:
    def _sol(self, usuario, estado='EN_PICKING'):
        from app.models.traslado import SolicitudTraslado
        s = SolicitudTraslado(codigo='ST-ACT-1', bodega_origen_siesa='NB1', bodega_destino_siesa='PC1',
                              solicitante_id=usuario.id, estado=estado)
        _db.session.add(s)
        _db.session.flush()
        return s

    def test_picking_sin_tomar(self, app, db, usuario, producto, ub_picking, almacen):
        from app.models.picking import TareaPicking
        s = self._sol(usuario)
        db.session.add(TareaPicking(codigo='PK-ACT-1', producto_id=producto.id, cantidad_solicitada=5,
                                    ubicacion_id=ub_picking.id, almacen_id=almacen.id,
                                    referencia_documento=s.codigo, tipo_documento='TRASLADO'))
        db.session.commit()
        pp = s.to_dict()['picking_progreso']
        assert pp['actividad'] == ta.SIN_TOMAR and pp['total'] == 1

    def test_empaque_sin_empacador(self, app, db, usuario, almacen):
        from app.models.packing import TareaPacking
        s = self._sol(usuario, estado='EN_PACKING')
        db.session.add(TareaPacking(codigo='PKG-ACT-1', tipo_documento='TRASLADO', solicitud_id=s.id,
                                    almacen_id=almacen.id))
        db.session.commit()
        pk = s.to_dict()['packing_info']
        assert pk['actividad'] == ta.SIN_ASIGNAR and pk['empacador'] is None

    def test_el_empacador_sabe_si_alguien_tomo_el_picking(self, client, jwt_token_admin, db,
                                                         producto, ub_picking, almacen):
        """La espera del empacador: «El operario aún está pickeando» salía con
        un picking que nadie tomó. El detalle trae `picking_actividad`."""
        from app.models.packing import TareaPacking
        from app.models.picking import TareaPicking
        t = TareaPacking(codigo='PKG-PD1', numero_pedido_siesa='PD1', almacen_id=almacen.id)
        db.session.add(t)
        db.session.add(TareaPicking(codigo='PK-PD1', producto_id=producto.id, cantidad_solicitada=5,
                                    ubicacion_id=ub_picking.id, almacen_id=almacen.id,
                                    referencia_documento='PD1', tipo_documento='PEDIDO'))
        db.session.commit()
        r = client.get(f'/api/packing/{t.id}', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        assert d['picking_listo'] is False and d['picking_actividad']['actividad'] == ta.SIN_TOMAR

    def test_el_texto_del_empacador(self):
        t = correr("return textoEsperaPicking({actividad: 'SIN_TOMAR', completadas: 0, total: 1});")
        assert t == 'Nadie ha tomado el picking de este pedido todavía'
        t = correr("return textoEsperaPicking({actividad: 'SIN_ACTIVIDAD', operarios: ['Ana'], minutos: 125});")
        assert t == 'Asignado a Ana, sin actividad hace 2 h'
        t = correr("return textoEsperaPicking({actividad: 'ACTIVO', operarios: ['Ana'], minutos: 4, ultima_actividad: 'TOMADA'});")
        assert t == 'Ana está pickeando (tomó una línea hace 4 min)'

    def test_la_pantalla_del_empacador_lo_usa(self):
        t = (PWA / 'packing.js').read_text(encoding='utf-8')
        assert 'textoEsperaPicking(t.picking_actividad)' in t
        assert 'aún está pickeando' not in t

    def test_el_umbral_es_una_constante(self):
        """Una constante: nadie más escribe el 30 de «sin actividad»."""
        src = (RAIZ / 'app/services/traslado_actividad.py').read_text(encoding='utf-8')
        assert 'UMBRAL_SIN_ACTIVIDAD_MIN = 30' in src
        for f in ('app/routes/packing.py', 'app/models/traslado.py'):
            assert 'UMBRAL_SIN_ACTIVIDAD' not in (RAIZ / f).read_text(encoding='utf-8')
        import re
        for f in ('traslados.js', 'packing.js', 'util.js'):
            t = (PWA / f).read_text(encoding='utf-8')
            assert not re.search(r'\.minutos\s*[<>]=?\s*\d', t), f'{f} juzga la actividad por su cuenta'


# ── 3 · El semáforo de Siesa ──────────────────────────────────────────────────

class TestContactoConSiesa:
    def test_cerrado_no_es_contacto(self):
        from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker
        cb = ConnektaCircuitBreaker()
        s = cb.snapshot()
        assert s['state'] == 'CLOSED' and s['ultimo_exito'] is None and s['contacto_reciente'] is False

    def test_un_exito_reciente_si(self):
        from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker
        cb = ConnektaCircuitBreaker()
        cb.record_success()
        s = cb.snapshot()
        assert s['contacto_reciente'] is True and s['minutos_desde_exito'] == 0
        cb.ultimo_exito = datetime.utcnow() - timedelta(minutes=cb.snapshot()['contacto_reciente_min'] + 5)
        assert cb.snapshot()['contacto_reciente'] is False

    def test_el_fallo_queda_con_su_hora(self):
        from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker
        cb = ConnektaCircuitBreaker()
        cb.record_failure()
        assert cb.snapshot()['ultimo_fallo'] is not None

    def test_la_pantalla(self):
        r = correr("return [textoContactoSiesa({state: 'CLOSED'}), "
                   "textoContactoSiesa({state: 'CLOSED', contacto_reciente: true, minutos_desde_exito: 3}), "
                   "textoContactoSiesa({state: 'CLOSED', contacto_reciente: false, minutos_desde_exito: 300})];")
        assert r[0]['color'] == 'gris' and 'Sin contacto reciente' in r[0]['texto']
        assert r[1] == {'color': 'verde', 'texto': 'Respondió hace 3 min (este servicio)'}
        assert r[2]['color'] == 'gris' and 'hace 5 h' in r[2]['texto']

    def test_el_tablero_ya_no_dice_conectado(self):
        src = (PWA / 'app.js').read_text(encoding='utf-8')
        assert "'Conectado'" not in src and 'textoContactoSiesa(cb)' in src


# ── 4 · Lo que va a Siesa, el escaneo y el sync ───────────────────────────────

def _producto(codigo, **kw):
    p = Producto(codigo=codigo, nombre=kw.pop('nombre', f'P {codigo}'), codigo_siesa=codigo,
                 unidad_negocio_id='001', **kw)
    _db.session.add(p)
    _db.session.flush()
    return p


def _emp(p, unidad, factor, codigo='7700000009001', origen='SIESA_GS1', activo=True):
    e = ProductoEmpaque(producto_id=p.id, referencia_item=p.codigo_siesa, codigo_barras=codigo,
                        unidad_medida=unidad, factor_conversion=factor, origen=origen, activo=activo)
    _db.session.add(e)
    _db.session.flush()
    return e


class TestLaEntradaReusaLaUnidadDeLaSalida:
    def test_lee_el_payload_del_sts(self, app, db, usuario, almacen):
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.models.traslado import SolicitudTraslado
        from app.services.traslado_service import _unidades_del_sts
        s = SolicitudTraslado(codigo='ST-STS-1', bodega_origen_siesa='NB1', bodega_destino_siesa='PC1',
                              solicitante_id=usuario.id)
        db.session.add(s)
        db.session.flush()
        t = TareaPacking(codigo='PKG-STS-1', tipo_documento='TRASLADO', solicitud_id=s.id,
                         almacen_id=almacen.id)
        db.session.add(t)
        db.session.flush()
        assert _unidades_del_sts(s) == {}, 'sin job: la entrada resuelve con la misma regla'
        job = SiesaJob.encolar(tipo='DESPACHO_TRASLADO', payload={'solicitud_id': s.id, 'items': [
            {'codigo_siesa': 'A', 'unidad_empaque': 'PQ', 'factor_empaque': 12},
            {'codigo_siesa': 'B', 'unidad_empaque': '', 'factor_empaque': 1}]},
            referencia_tipo='TareaPacking', referencia_id=t.id)
        job.estado = 'COMPLETADO'
        db.session.commit()
        assert _unidades_del_sts(s) == {'A': ('PQ', 12), 'B': ('', 1)}

    def test_la_recepcion_la_usa(self):
        arbol = ast.parse((RAIZ / 'app/services/traslado_service.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)
                  and n.name == 'confirmar_recepcion')
        llamadas = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Name)}
        assert '_unidades_del_sts' in llamadas


class TestElEscaneoSoloConPaquetesEscaneables:
    def test_con_lpn_usa_el_de_doce(self, app, db, almacen):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.models.lpn import LPN
        from app.services.empaques_service import descomponer_en_empaques
        p = _producto('ESC2')
        _emp(p, 'PQ', 12, codigo='7700000009201')
        _emp(p, 'CAJ', 144, codigo=None)
        db.session.commit()
        lpn = SimpleNamespace(cantidad_actual=12, to_dict=lambda: {'codigo': 'LPN-1'})
        with patch.object(LPN, 'buscar_activos_por_producto', return_value=[lpn, lpn, lpn]):
            r = descomponer_en_empaques(p.id, 30, almacen.id)
        assert (r['factor_empaque'], r['unidad_empaque']) == (12, 'PQ')


class TestElSyncDeEmpaques:
    def _correr(self, app, monkeypatch, factores, filas):
        from app.services import empaques_sync_service as es

        class _Lock:
            def liberar(self):
                pass
        monkeypatch.setattr('app.utils.lock.tomar_lock_de_sesion', lambda *a, **k: _Lock())
        monkeypatch.setattr(es, '_cargar_factores_q35', lambda: factores)
        paginas = iter([{'detalle': {'Table': filas}}, {'detalle': {'Table': []}}])
        monkeypatch.setattr(es.connekta, '_get', lambda *a, **k: next(paginas))
        es._run_sync(app)
        return es.get_estado()['ultimo_resultado']

    def test_el_codigo_de_la_unidad_base_es_factor_uno(self, app, db, monkeypatch):
        p = _producto('BASE1', unidad_medida='RES')
        viejo = _emp(p, 'RES', 1, codigo='7700000009301')
        db.session.commit()
        res = self._correr(app, monkeypatch, {('OTRO', 'PQ'): 12}, [
            {'f131_id': '7700000009301', 'f120_referencia': 'BASE1', 'f131_id_unidad_medida': 'RES'},
            {'f131_id': '7700000009302', 'f120_referencia': 'BASE1', 'f131_id_unidad_medida': 'RES'}])
        assert res['sin_factor_q35'] == 0
        db.session.expire_all()
        assert db.session.get(ProductoEmpaque, viejo.id).activo is True
        nuevo = ProductoEmpaque.query.filter_by(codigo_barras='7700000009302').one()
        assert nuevo.factor_conversion == 1

    def test_un_paquete_de_otra_unidad_sin_factor_sigue_fuera(self, app, db, monkeypatch):
        _producto('BASE2', unidad_medida='RES')
        db.session.commit()
        res = self._correr(app, monkeypatch, {('OTRO', 'PQ'): 12}, [
            {'f131_id': '7700000009401', 'f120_referencia': 'BASE2', 'f131_id_unidad_medida': 'CAJ'}])
        assert res['sin_factor_q35'] == 1


class TestPaquetesSinCodigoQueSiesaQuito:
    def _prods(self, p):
        from collections import namedtuple
        PD = namedtuple('PD', ['id', 'codigo_siesa', 'codigo'])
        return {p.codigo_siesa: PD(p.id, p.codigo_siesa, p.codigo)}, {}

    def test_con_lectura_completa_se_apaga(self, app, db):
        from app.services.empaques_sync_service import _sincronizar_paquetes_sin_codigo
        p = _producto('QUITO1')
        e = _emp(p, 'PQ', 12, codigo=None)
        por_siesa, por_codigo = self._prods(p)
        r = _sincronizar_paquetes_sin_codigo({('OTRO', 'PQ'): 6}, por_siesa, por_codigo, set(),
                                             q35_completa=True)
        assert r['retirados_de_siesa'] == 1 and e.activo is False
        assert ep.empaque_de(p) is None

    def test_con_lectura_parcial_no_se_toca(self, app, db):
        from app.services.empaques_sync_service import _sincronizar_paquetes_sin_codigo
        p = _producto('QUITO2')
        e = _emp(p, 'PQ', 12, codigo=None)
        por_siesa, por_codigo = self._prods(p)
        r = _sincronizar_paquetes_sin_codigo({('OTRO', 'PQ'): 6}, por_siesa, por_codigo, set(),
                                             q35_completa=False)
        assert r['retirados_de_siesa'] == 0 and r['q35_completa'] is False and e.activo is True

    def test_la_lectura_de_q35_sabe_si_fue_completa(self, monkeypatch):
        from app.services import empaques_sync_service as es
        fila = {'f120_referencia': 'A', 'f122_id_unidad': 'PQ', 'f122_factor': '12'}
        paginas = iter([{'detalle': {'Table': [fila]}}, {'detalle': {'Table': []}}])
        monkeypatch.setattr(es.connekta, 'get_items_unidades_medida', lambda pag: next(paginas))
        f = es._cargar_factores_q35()
        assert f[('A', 'PQ')] == 12 and f.completa is True

        def _cae(pag):
            if pag == 1:
                return {'detalle': {'Table': [fila]}}
            raise RuntimeError('red')
        monkeypatch.setattr(es.connekta, 'get_items_unidades_medida', _cae)
        assert es._cargar_factores_q35().completa is False


class TestLaListaDeComprasUsaLaMismaPolitica:
    def test_lo_que_ya_se_pide_por_paquete_no_se_reporta(self, app, db):
        resuelto = _producto('LISTA1', unidad_empaque='CAJ', factor_conversion=24)
        falta = _producto('LISTA2', unidad_empaque='PQ', factor_conversion=1)
        db.session.commit()
        assert ep.empaque_de(resuelto).factor == 24
        d = ep.paquetes_por_revisar()
        codigos = [x['codigo'] for x in d['declarado_sin_factor']['productos']]
        assert 'LISTA2' in codigos and 'LISTA1' not in codigos


class TestSinNMasUno:
    def _mundo(self, db, usuario, n):
        from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
        for i in range(n):
            s = SolicitudTraslado(codigo=f'ST-N1-{n}-{i}', bodega_origen_siesa='NB1',
                                  bodega_destino_siesa='PC1', solicitante_id=usuario.id,
                                  estado='EN_PICKING')
            db.session.add(s)
            db.session.flush()
            for j in range(3):
                p = _producto(f'N1-{n}-{i}-{j}')
                _emp(p, 'PQ', 12, codigo=f'77{n:02d}{i:03d}{j:03d}0000')
                db.session.add(ItemSolicitudTraslado(solicitud_id=s.id, producto_id=p.id,
                                                     producto_codigo_siesa=p.codigo_siesa,
                                                     cantidad_solicitada=5))
        db.session.commit()

    def _consultas(self, app, client, token):
        from sqlalchemy import event
        n = [0]

        def contar(*a, **k):
            n[0] += 1
        motor = _db.engine
        event.listen(motor, 'before_cursor_execute', contar)
        try:
            r = client.get('/api/traslados/', headers={'Authorization': f'Bearer {token}'})
        finally:
            event.remove(motor, 'before_cursor_execute', contar)
        assert r.status_code == 200
        return n[0], len(r.get_json()['solicitudes'])

    def test_la_lista_no_crece_por_solicitud(self, app, db, client, usuario, jwt_token_admin):
        self._mundo(db, usuario, 2)
        pocas, n1 = self._consultas(app, client, jwt_token_admin)
        self._mundo(db, usuario, 6)
        muchas, n2 = self._consultas(app, client, jwt_token_admin)
        assert (n1, n2) == (2, 8)
        assert muchas == pocas, f'{pocas} consultas con 2 solicitudes, {muchas} con 8'


class TestElCatalogoNoSeCargaEntero:
    def test_empaques_por_id_no_trae_productos_al_mapa(self, app, db):
        p = _producto('CAT1')
        _emp(p, 'PQ', 12, codigo='7700000009501')
        db.session.commit()
        pid = p.id
        db.session.expunge_all()
        emps = ep.empaques_por_id([pid])
        assert emps[pid].factor == 12
        assert not [o for o in db.session.identity_map.values() if isinstance(o, Producto)]


class TestUnaSolaValidacionDeEnteros:
    def test_no_hay_copia(self):
        from app.services.mobile_service import MobileService
        from app.utils.entero_json import entero_no_negativo
        assert ep._entero is entero_no_negativo
        assert not hasattr(ep, 'descomponer'), 'sin caller: se borró'
        with pytest.raises(ValueError):
            MobileService._entero_no_negativo(True, 'x')
        src = (RAIZ / 'app/services/mobile_service.py').read_text(encoding='utf-8')
        assert 'isinstance(valor, bool)' not in src


class TestAprobarSinDatoDeDisponible:
    def test_desconocido_no_es_insuficiente(self):
        h = correr("return empaqueCasillasHtml('ap-1', {unidad: 'PQ', factor: 12}, null, null, 0);")
        assert 'no alcanza' not in h and 'sin dato de disponible' in h
        h = correr("return empaqueCasillasHtml('ap-1', {unidad: 'PQ', factor: 12}, 5, null, 0);")
        assert 'no alcanza un paquete completo' in h

    def test_los_jsdoc_vuelven_a_su_funcion(self):
        t = (PWA / 'traslados.js').read_text(encoding='utf-8')
        i = t.index('async function trasAprobar(id) {')
        assert 'Open the approval modal' in t[i - 200:i]
        p = (PWA / 'picking.js').read_text(encoding='utf-8')
        i = p.index('function renderTarea(t) {')
        assert 'Renderiza la tarea activa' in p[i - 400:i]

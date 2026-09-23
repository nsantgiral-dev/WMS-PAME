"""
Tablero del líder de bodega — `app/services/tablero_lider_conteo.py`,
`GET /api/conteo/lider/tablero` y la pestaña 🧭 Líder (`conteo.js`).

## Qué protege

1. **Cada bloque sale de la política que ya existía**, no de una copia: los
   bloqueados de `listar_bloqueados`, lo aprobable de `motivo_bloqueo_ajuste`,
   lo que sale del plan de lo que filtra el generador, «hoy» de la regla de día
   del reporte. El mundo se arma con los SERVICIOS reales (contar, bloquear,
   reportar sin código, auditoría por excepción, picking y empaque), con un
   caso por fila, y cada número se afirma.
2. **«Auditoría urgente» tiene una definición**: el KPI del dashboard es `len`
   de la lista del tablero. Antes contaba filas: el CC2 hereda el tipo de su
   raíz y queda en DESCUADRE para siempre, y el KPI solo crecía.
3. **Ningún botón promete lo que el endpoint niega**: `permisos_de` se cruza
   contra la respuesta real de cada endpoint, rol por rol.
4. **Todo dato se escapa**: el render real corre en Node con `util.js` real y
   `<img onerror>` en cada texto.
"""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from tests.test_conteo_teorico_pos import siesa, tienda  # noqa: F401 (fixtures)
from tests.test_estadisticas_conteo import _abrir_y_contar, _hueco, _nuevo_cc1, _poner

RAIZ = pathlib.Path(__file__).resolve().parents[1]
X = '<img src=x onerror=alert(1)>'


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _tok(app, usuario):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


def _ids_hueco(sku):
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    p = Producto.query.filter_by(codigo=sku).one()
    n = sku.replace('EST', '')
    u = Ubicacion.query.filter_by(codigo=f'EST-UB-{n}').one()
    return p.id, u.id


def _auditoria(db, tienda, sku):
    from app.models.conteo import SesionConteo
    pid, uid = _ids_hueco(sku)
    s = _svc().generar_auditoria_por_excepcion(None, uid, pid, tienda['almacen'].id)
    db.session.commit()
    assert s.tipo == 'EXCEPCION_PICKING'
    return SesionConteo.query.get(s.id).id


@pytest.fixture(autouse=True)
def _config_limpia(monkeypatch):
    for nombre in ('CONTEO_CUPO_DIARIO', 'CONTEO_CUPO_POR_BODEGA',
                   'CONTEO_INTERVALOS_DIAS', 'CONTEO_WATCHDOG_DIAS_SIN_REABRIR'):
        monkeypatch.delenv(nombre, raising=False)


# ─────────────────────────────────────────────────────────────────────────────
# El mundo: un caso de cada fila
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mundo(app, db, siesa, tienda, monkeypatch):
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob

    # Cupo 2: con las pendientes del mundo el generador queda detenido por
    # rezago, que es lo que el aviso tiene que decir.
    monkeypatch.setenv('CONTEO_CUPO_DIARIO', '2')
    a, b, sup = tienda['a'], tienda['b'], tienda['supervisor']
    huecos = iter(_hueco(db, tienda, n) for n in range(101, 130))
    ids = {}

    # Conteos bloqueados: «no lo encontré» y otro problema.
    _poner(siesa, 10)
    ids['bloq_noenc'] = s = _nuevo_cc1(tienda, next(huecos))
    _svc().obtener_tarea_operario(s, a.id)
    _svc().bloquear_conteo(s, a.id, 'NO_ENCONTRADO')
    ids['bloq_otro'] = s = _nuevo_cc1(tienda, next(huecos))
    _svc().obtener_tarea_operario(s, a.id)
    _svc().bloquear_conteo(s, a.id, 'OTRO', observaciones='la estantería está caída')

    # Mercancía sin código (el conteo sigue en proceso).
    ids['con_novedad'] = s = _nuevo_cc1(tienda, next(huecos))
    _svc().obtener_tarea_operario(s, a.id)
    ids['novedad'] = _svc().registrar_novedad_sin_codigo(s, a.id, 'caja sin etiqueta al fondo').id

    # Ajuste aprobable con costo: CC1 −1, CC2 −2, el definitivo +2 a $2.000.
    _poner(siesa, 10, costo=1000)
    ids['aprobable'] = s = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    r = _abrir_y_contar(r['segundo_conteo_id'], b, 8)
    _poner(siesa, 10, costo=2000)
    r3 = _abrir_y_contar(r['tercer_conteo_id'], sup, 12)
    assert r3['resultado'] == 'DESCUADRE' and not r3['ajuste_bloqueado'], r3

    # Ajuste aprobable SIN costo en la foto.
    _poner(siesa, 10)
    ids['aprobable_sin_costo'] = s = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    r = _abrir_y_contar(r['segundo_conteo_id'], b, 8)
    r3 = _abrir_y_contar(r['tercer_conteo_id'], sup, 7)
    assert r3['resultado'] == 'DESCUADRE' and not r3['ajuste_bloqueado'], r3

    # Ajuste que no se puede aprobar: salidas sin confirmar que no son POS.
    _poner(siesa, 10, pos=2, salida_sin_conf=5)
    ids['bloq_ajuste'] = s = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    r2 = _abrir_y_contar(r['segundo_conteo_id'], b, 9)
    assert r2['ajuste_bloqueado'], r2

    # Ajuste que Siesa rechazó: CC1 == CC2 encola solo; la cola lo agota.
    _poner(siesa, 10, costo=1500)
    ids['fallido'] = s = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    r2 = _abrir_y_contar(r['segundo_conteo_id'], b, 9)
    assert r2['auto_encolado'] is True, r2
    job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=s).one()
    job.estado = EstadoSiesaJob.FALLIDO
    job.error_ultimo = 'Siesa: el tipo de documento no está autorizado'
    # Un job viejo sin sesión en el payload: cuenta en el sistema, no acá.
    db.session.add(SiesaJob(tipo='AJUSTE_CONTEO', payload=json.dumps({'motivo_codigo': 'AJ-SAL'}),
                            estado=EstadoSiesaJob.FALLIDO))
    db.session.commit()

    # Un conteo que cuadró hoy.
    _poner(siesa, 10)
    ids['match'] = s = _nuevo_cc1(tienda, next(huecos))
    assert _abrir_y_contar(s, a, 10)['resultado'] == 'MATCH'

    # Auditorías por faltante: en cola, en segundo conteo, esperando el
    # definitivo, y una ya resuelta (CC1 == CC2 → ajuste en camino) que NO es
    # urgente aunque su CC2 quede en DESCUADRE para siempre.
    ids['aud_cola'] = _auditoria(db, tienda, next(huecos))
    ids['aud_cc2'] = s = _auditoria(db, tienda, next(huecos))
    _abrir_y_contar(s, a, 9)
    ids['aud_cc3'] = s = _auditoria(db, tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    assert _abrir_y_contar(r['segundo_conteo_id'], b, 8)['resultado'] == 'TERCER_CONTEO'
    ids['aud_resuelta'] = s = _auditoria(db, tienda, next(huecos))
    r = _abrir_y_contar(s, a, 9)
    assert _abrir_y_contar(r['segundo_conteo_id'], b, 9)['auto_encolado'] is True
    assert db.session.get(SesionConteo, s).estado == EstadoConteo.AJUSTANDO

    # El rezago de abril: tres conteos del plan que nadie tomó.
    from app.models.producto import Producto
    for n in range(3):
        sku = next(huecos)
        pid, uid = _ids_hueco(sku)
        db.session.add(SesionConteo(
            codigo=f'ABRIL-{n}', tipo='DIARIO_ABC', clasificacion_abc='C', ubicacion_id=uid,
            almacen_id=tienda['almacen'].id, producto_id=pid, producto_codigo_siesa=sku,
            estado='PENDIENTE', es_segundo_conteo=False,
            fecha_creacion=datetime.utcnow() - timedelta(days=150)))
    db.session.commit()

    # Fuera del plan. Sin fecha: un empaque cancelado sin remisión (nadie sabe
    # si la mercancía volvió al estante). Con fecha: un picking recogido que
    # sale solo cuando se remisione. Después de contar: no toca los ajustes.
    from app.services.packing_service import PackingService
    from app.services.picking_service import PickingService

    def _recoger(producto_id, ref):
        tareas = PickingService.crear_tareas(
            producto_id=producto_id, cantidad=2, almacen_id=tienda['almacen'].id,
            referencia_documento=ref, tipo_documento='PEDIDO')
        for t in tareas:
            PickingService.iniciar_picking(t.id, a.id)
            PickingService.confirmar_picking(t.id, 2, a.id)
        return tareas

    packing = PackingService.crear_desde_picking(
        tareas_picking_ids=[t.id for t in _recoger(tienda['producto'].id, 'PD-LIDER-1')],
        numero_pedido_siesa='PD-LIDER-1', almacen_id=tienda['almacen'].id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='1')
    PackingService.cancelar(packing.id, motivo='pedido anulado')
    con_fecha = Producto.query.filter_by(codigo=next(huecos)).one()
    _recoger(con_fecha.id, 'PD-LIDER-2')
    db.session.commit()
    db.session.expire_all()
    ids['sku_sin_fecha'] = tienda['producto'].codigo
    ids['sku_con_fecha'] = con_fecha.codigo
    return ids


def _tablero(tienda, rol='supervisor'):
    from app.services.tablero_lider_conteo import tablero
    return tablero(tienda['almacen'].id, rol=rol)


# ─────────────────────────────────────────────────────────────────────────────
# El servicio, bloque por bloque
# ─────────────────────────────────────────────────────────────────────────────

class TestLaColaDeDecisiones:

    def test_los_bloques_van_en_el_orden_del_usuario(self, mundo, tienda):
        from app.services.tablero_lider_conteo import ORDEN_DE_URGENCIA
        d = _tablero(tienda)
        assert ORDEN_DE_URGENCIA == ('bloqueados', 'novedades', 'ajustes', 'auditorias',
                                     'rechazados_siesa')
        assert list(d['decisiones']) == list(ORDEN_DE_URGENCIA) == d['orden']

    def test_bloqueados_con_su_motivo(self, mundo, tienda):
        b = _tablero(tienda)['decisiones']['bloqueados']
        assert b['total'] == 2
        assert b['por_motivo'] == {'NO_ENCONTRADO': 1, 'OTRO': 1}
        por_id = {f['id']: f for f in b['filas']}
        assert set(por_id) == {mundo['bloq_noenc'], mundo['bloq_otro']}
        assert por_id[mundo['bloq_noenc']]['motivo_texto'] == 'No lo encontró'
        assert 'estantería' in por_id[mundo['bloq_otro']]['nota']

    def test_novedades_sin_resolver(self, mundo, tienda):
        n = _tablero(tienda)['decisiones']['novedades']
        assert n['total'] == 1
        assert n['filas'][0]['id'] == mundo['novedad']
        assert n['filas'][0]['descripcion'] == 'caja sin etiqueta al fondo'

    def test_ajustes_aprobables_con_su_plata_y_sin_costo_adelante(self, mundo, tienda):
        a = _tablero(tienda)['decisiones']['ajustes']
        ap = a['aprobables']
        assert a['total_descuadres'] == 3
        assert ap['total'] == 2 and ap['valorizados'] == 1 and ap['sin_costo'] == 1
        # Sin costo primero: que no se sepa cuánto vale no lo vuelve chico.
        assert [f['id'] for f in ap['filas']] == [mundo['aprobable_sin_costo'], mundo['aprobable']]
        sin, con = ap['filas']
        assert sin['valor'] is None and sin['direccion'] == 'SALIDA' and sin['unidades'] == 3
        assert con['valor'] == 4000.0 and con['direccion'] == 'ENTRADA' and con['unidades'] == 2
        assert con['costo_unitario'] == 2000.0
        assert ap['valor_total'] == 4000.0

    def test_ajustes_bloqueados_con_motivo_resumido_y_accion(self, mundo, tienda):
        bl = _tablero(tienda)['decisiones']['ajustes']['bloqueados']
        assert bl['total'] == 1 and bl['por_motivo'] == {'SALIDAS_NO_POS': 1}
        f = bl['filas'][0]
        assert f['id'] == mundo['bloq_ajuste']
        assert f['accion']['tipo'] == 'RECONTAR'
        assert 'salidas sin confirmar' in f['motivo']

    def test_cada_motivo_de_bloqueo_tiene_accion(self):
        """Un motivo nuevo en `resumir_motivo_bloqueo` sin acción caería en
        «OTRO» sin que nadie lo decidiera."""
        from app.services.metricas.conteo import _CLAVES_BLOQUEO
        from app.services.tablero_lider_conteo import ACCION_POR_MOTIVO_AJUSTE
        claves = {c for _, c in _CLAVES_BLOQUEO} | {'OTRO'}
        assert claves == set(ACCION_POR_MOTIVO_AJUSTE)

    def test_auditorias_vivas_una_por_cadena_con_su_accion(self, mundo, tienda):
        au = _tablero(tienda)['decisiones']['auditorias']
        por_id = {f['id']: f['accion']['tipo'] for f in au['filas']}
        assert por_id == {mundo['aud_cola']: 'EN_COLA', mundo['aud_cc2']: 'EN_CURSO',
                          mundo['aud_cc3']: 'CONTAR_DEFINITIVO'}
        assert au['total'] == 3 and au['esperan_al_lider'] == 1

    def test_rechazados_por_siesa_del_almacen_contra_el_total(self, mundo, tienda):
        r = _tablero(tienda)['decisiones']['rechazados_siesa']
        assert r['total'] == 1 and r['total_sistema'] == 2
        assert r['sin_sesion_identificable'] == 1
        assert r['filas'][0]['id'] == mundo['fallido']
        assert 'no está autorizado' in r['filas'][0]['error']

    def test_el_resumen_no_cuenta_dos_veces(self, mundo, tienda):
        d = _tablero(tienda)
        assert d['resumen']['por_bloque'] == {'bloqueados': 2, 'novedades': 1, 'ajustes': 3,
                                              'auditorias': 1, 'rechazados_siesa': 1}
        assert d['resumen']['decisiones_pendientes'] == 8


class TestFueraDelPlanSinFecha:

    def test_lista_el_documento_a_cerrar(self, mundo, tienda):
        fp = _tablero(tienda)['fuera_del_plan']
        assert fp['skus_sin_fecha'] == 1 and fp['documentos'] == 1
        assert fp['filas'][0]['skus'] == [mundo['sku_sin_fecha']]
        assert fp['filas'][0]['clase'] == 'VENTA'
        assert 'PD-LIDER-1' in fp['filas'][0]['documento']
        # El de fecha no se lista: sale solo cuando su documento entra.
        assert fp['skus_con_fecha_salen_solos'] == 1

    def test_es_lo_mismo_que_excluye_el_generador(self, mundo, tienda):
        """Una política: los SKUs sin fecha del tablero están entre los que el
        generador deja fuera (`filtrar_elegibles`)."""
        from app.models.producto import Producto
        from app.services.conteo_service import ConteoService
        fuera = ConteoService.productos_con_mercancia_en_proceso(tienda['almacen'].id)
        pid = Producto.query.filter_by(codigo=mundo['sku_sin_fecha']).one().id
        assert pid in fuera


class TestHoy:

    def test_cerrados_hoy_contra_el_cupo(self, mundo, tienda):
        h = _tablero(tienda)['hoy']
        # Cerradas con veredicto hoy: match, aprobable, aprobable sin costo,
        # bloq_ajuste, fallido y la auditoría resuelta. No: bloqueadas,
        # pendientes, ni las que esperan 2º o definitivo.
        assert h['cerrados'] == 6
        assert h['cupo_diario'] == 2
        assert h['pendientes_vivas'] >= 4 and h['dias_de_cupo_pendientes'] >= 2

    def test_por_persona_solo_volumen_ordenado_por_nombre(self, mundo, tienda):
        pp = _tablero(tienda)['hoy']['por_persona']
        filas = [(f['nombre'], f['cadenas'], f['conteos']) for f in pp['filas']]
        assert filas == [('pos-a@test.com', 6, 6), ('pos-b@test.com', 5, 5),
                         ('pos-sup@test.com', 2, 2)]
        assert set(pp['filas'][0]) == {'operario_id', 'nombre', 'cadenas', 'conteos'}

    def test_el_dia_es_el_de_bogota(self, mundo, tienda):
        """A las 8 p. m. de Bogotá ya es mañana en UTC: el tablero sigue en hoy."""
        from app.services.tablero_lider_conteo import tablero
        from app.utils.fecha import dia_operativo_de
        ahora = datetime.utcnow()
        d = tablero(tienda['almacen'].id, rol='supervisor', ahora=ahora)
        assert d['al_dia_operativo'] == dia_operativo_de(ahora).isoformat()


class TestRezago:

    def test_avisa_si_el_generador_esta_detenido_y_hay_que_cancelar(self, mundo, tienda):
        r = _tablero(tienda)['rezago']
        assert r['generador_detenido'] is True and r['hay_aviso'] is True
        assert r['a_cancelar'] == 3
        assert r['por_antiguedad_dias']['>30'] == 3

    def test_sin_rezago_no_hay_aviso(self, mundo, tienda, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '500')
        r = _tablero(tienda)['rezago']
        assert r['generador_detenido'] is False and r['hay_aviso'] is False


# ─────────────────────────────────────────────────────────────────────────────
# El KPI del dashboard: la misma lista
# ─────────────────────────────────────────────────────────────────────────────

class TestAuditoriasUrgentesUnaSolaFuente:

    def test_el_kpi_es_la_lista_del_tablero(self, app, client, mundo, tienda):
        resp = client.get(f'/api/dashboard/resumen-completo?almacen_id={tienda["almacen"].id}',
                          headers=_tok(app, tienda['supervisor']))
        assert resp.status_code == 200, resp.get_json()
        filas = _tablero(tienda)['decisiones']['auditorias']['filas']
        assert resp.get_json()['auditorias_urgentes'] == len(filas) == 3

    def test_la_cuenta_vieja_por_filas_daba_otra_cosa(self, mundo, tienda):
        """Documenta el defecto: la consulta anterior contaba el CC2 de la
        misma auditoría y el CC2 resuelto que queda en DESCUADRE para siempre."""
        from app.models.conteo import SesionConteo
        vieja = (SesionConteo.query
                 .filter_by(tipo='EXCEPCION_PICKING', almacen_id=tienda['almacen'].id)
                 .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO',
                                                  'SEGUNDO_CONTEO', 'DESCUADRE']))
                 .count())
        assert vieja == 6

    def test_el_dashboard_no_escribe_su_propia_consulta(self):
        """Por AST: la ruta del dashboard llama a `contar_auditorias_urgentes` y
        no filtra `EXCEPCION_PICKING` por su cuenta."""
        import ast
        arbol = ast.parse((RAIZ / 'app' / 'routes' / 'dashboard.py').read_text(encoding='utf-8'))
        llamadas = {n.func.id for n in ast.walk(arbol)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        literales = {n.value for n in ast.walk(arbol) if isinstance(n, ast.Constant)}
        assert 'contar_auditorias_urgentes' in llamadas
        assert 'EXCEPCION_PICKING' not in literales


# ─────────────────────────────────────────────────────────────────────────────
# El endpoint y los permisos de cada botón
# ─────────────────────────────────────────────────────────────────────────────

def _usuario(db, tienda, rol):
    from app.models.usuario import Usuario
    u = Usuario(nombre=f'{rol}@lider', email=f'{rol}@lider.test',
                password_hash=generate_password_hash('x'), rol=rol,
                almacen_id=tienda['almacen'].id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


class TestElEndpoint:

    def test_operario_recibe_403(self, app, client, db, tienda):
        resp = client.get(f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}',
                          headers=_tok(app, tienda['a']))
        assert resp.status_code == 403

    @pytest.mark.parametrize('rol', ['admin', 'supervisor', 'jefe_almacen'])
    def test_supervision_lo_ve(self, app, client, db, siesa, tienda, rol):
        u = _usuario(db, tienda, rol)
        resp = client.get(f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}',
                          headers=_tok(app, u))
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()['fuente'].startswith('solo base del WMS')

    def test_sin_almacen_o_almacen_inexistente(self, app, client, db, tienda):
        h = _tok(app, tienda['supervisor'])
        assert client.get('/api/conteo/lider/tablero', headers=h).status_code == 400
        assert client.get('/api/conteo/lider/tablero?almacen_id=x', headers=h).status_code == 400
        assert client.get('/api/conteo/lider/tablero?almacen_id=99999', headers=h).status_code == 404

    def test_cero_siesa(self, app, client, mundo, tienda, siesa):
        antes = siesa.lecturas
        resp = client.get(f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}',
                          headers=_tok(app, tienda['supervisor']))
        assert resp.status_code == 200
        assert siesa.lecturas == antes


#: Cada permiso del tablero y una llamada inocua a SU endpoint: con permiso
#: responde cualquier cosa menos 403 (404/400 por el id inexistente); sin
#: permiso, 403. Ninguna escribe: los ids no existen y no hay jobs.
LLAMADA_POR_PERMISO = {
    'reabrir_cancelar_bloqueado': ('post', '/api/conteo/999999/reabrir', {}),
    'resolver_novedad': ('post', '/api/conteo/novedades/999999/resolver', {'nota': 'x'}),
    'aprobar_ajuste': ('put', '/api/conteo/999999/ajustar', {}),
    'recontar': ('post', '/api/conteo/manual', {}),
    'cancelar_conteo': ('put', '/api/conteo/999999/cancelar', {'motivo': 'x'}),
    'reintentar_descartar_fallos': ('get', '/api/conteo/descartar-fallos/preview', None),
    'cancelar_rezago': ('get', '/api/conteo/abc/limpiar-pendientes/preview', None),
}


class TestNingunBotonPrometeUn403:

    def test_cada_permiso_tiene_su_llamada(self):
        from app.services.tablero_lider_conteo import permisos_de
        assert set(permisos_de('admin')) == set(LLAMADA_POR_PERMISO)

    @pytest.mark.parametrize('rol', ['admin', 'supervisor', 'jefe_almacen', 'operario'])
    def test_el_permiso_coincide_con_el_endpoint(self, app, client, db, tienda, rol):
        from app.services.tablero_lider_conteo import permisos_de
        u = _usuario(db, tienda, rol)
        h = _tok(app, u)
        for clave, permitido in permisos_de(rol).items():
            metodo, url, cuerpo = LLAMADA_POR_PERMISO[clave]
            kw = {'headers': h} if cuerpo is None else {'headers': h, 'json': cuerpo}
            status = getattr(client, metodo)(url, **kw).status_code
            db.session.rollback()
            assert (status != 403) == permitido, (rol, clave, status)


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla — render real en Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '';
const ctx = { console, document: { getElementById: () => null }, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
const X = '<img src=x onerror=alert(1)>';
const fila = { id: 7, codigo: X, producto_codigo: X, producto_nombre: X, estado_texto: X, es_auditoria: true };
const permisos = modo === 'sin-permisos' ? {} : {
  reabrir_cancelar_bloqueado: true, resolver_novedad: true, aprobar_ajuste: true, recontar: true,
  cancelar_conteo: true, reintentar_descartar_fallos: true, cancelar_rezago: true };
const d = {
  almacen_id: 1, almacen: X, bodega_siesa: X, al_dia_operativo: X, fuente: X, permisos,
  resumen: { decisiones_pendientes: 8, por_bloque: { bloqueados: 1, novedades: 1, ajustes: 2, auditorias: 1, rechazados_siesa: 1 } },
  decisiones: {
    bloqueados: { total: 1, por_motivo: { [X]: 1 }, filas: [{ ...fila, motivo_bloqueo: X, motivo_texto: X, nivel: X, nota: X, reportado_por_nombre: X }] },
    novedades: { total: 1, filas: [{ id: 3, descripcion: X, reportado_por_nombre: X, producto_en_conteo: X }] },
    ajustes: { total_descuadres: 2,
      aprobables: { total: 1, valor_total: 4000, valorizados: 1, sin_costo: 0, etiqueta_valor: X,
        filas: [{ ...fila, direccion: 'SALIDA', unidades: 2, valor: 4000, costo_unitario: 2000, dia_conteo: X }] },
      bloqueados: { total: 1, por_motivo: { [X]: 1 }, filas: [{ ...fila, motivo_clave: X, motivo: X, accion: { tipo: 'RECONTAR', texto: X } }] } },
    auditorias: { total: 1, esperan_al_lider: 1, filas: [{ ...fila, pedido: X, dias_abierta: 3, accion: { tipo: 'CONTAR_DEFINITIVO', texto: X } }] },
    rechazados_siesa: { total: 1, total_sistema: 2, sin_sesion_identificable: 1,
      filas: [{ ...fila, motivo_codigo: X, unidades: 1, intentos: 3, error: X }] },
  },
  fuera_del_plan: { skus_sin_fecha: 1, documentos: 1, skus_con_fecha_salen_solos: 1,
    filas: [{ clase: X, documento: X, detalle: X, accion: X, skus: [X], n_skus: 1 }] },
  hoy: { dia: X, cerrados: 5, cupo_diario: 60, pendientes_vivas: 9, dias_de_cupo_pendientes: 0.2,
    mensaje_generador: X, unidad: X, por_persona: { nota: X, filas: [{ operario_id: 1, nombre: X, cadenas: 2, conteos: 3 }] } },
  rezago: { hay_aviso: true, a_cancelar: 3, pendientes_vivas: 9, cupo_diario: 2, dias_de_cupo_pendientes: 4.5,
    por_antiguedad_dias: { [X]: 3 } },
};
const html = vm.runInContext('liderTableroHtml', ctx)(d);
const handlers = [...new Set([...html.matchAll(/onclick="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
const sinDefinir = handlers.filter(h => typeof ctx[h] !== 'function');
const anchos = [...html.matchAll(/(?:^|[;"\s])(?:min-)?width:\s*(\d+)px/g)].map(m => +m[1]);
console.log(JSON.stringify({
  crudos: (html.match(/<img/g) || []).length,
  escapados: (html.match(/&lt;img/g) || []).length,
  titulos: ['Conteos bloqueados', 'Mercancía sin código', 'Ajustes esperando decisión',
    'Auditorías por faltante', 'Ajustes rechazados por Siesa', 'Fuera del plan', '📅 Hoy',
    'generador está detenido'].map(t => html.indexOf(t)),
  handlers, sinDefinir, anchoMax: Math.max(0, ...anchos),
  botonAprobar: html.includes('liderAprobarAjuste('), botonRezago: html.includes('liderCancelarRezago('),
}));
"""


def _render(modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPestana:

    def test_ningun_texto_llega_crudo(self):
        r = _render()
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 30, f'piso: el arnés dejó de pintar el tablero ({r})'

    def test_el_arnes_muerde_con_esc_roto(self):
        assert _render('esc-roto')['crudos'] >= 30

    def test_todos_los_bloques_se_pintan_en_orden(self):
        """El aviso de rezago arriba de todo; después la cola en el orden del
        usuario; fuera del plan y hoy al final."""
        pos = _render()['titulos']
        rezago = pos[-1]
        bloques = pos[:-1]
        assert all(p >= 0 for p in pos), pos
        assert rezago < bloques[0]
        assert bloques == sorted(bloques), pos

    def test_cada_boton_llama_una_funcion_que_existe(self):
        r = _render()
        assert r['sinDefinir'] == [], r
        assert len(r['handlers']) >= 10, r

    def test_sin_permiso_no_hay_boton(self):
        con, sin = _render(), _render('sin-permisos')
        assert con['botonAprobar'] and con['botonRezago']
        assert not sin['botonAprobar'] and not sin['botonRezago']

    def test_cabe_en_un_celular(self):
        """Nada con ancho fijo mayor que una pantalla de 360 px."""
        assert _render()['anchoMax'] <= 360

    def test_la_pestana_existe_y_llega_desde_el_kpi(self):
        html = (RAIZ / 'app' / 'static' / 'pwa' / 'index.html').read_text(encoding='utf-8')
        assert 'onclick="invSubtab(\'lider\')"' in html
        assert 'id="inv-panel-lider"' in html and 'id="inv-lider-contenido"' in html
        tarjeta = re.search(r'<div class="kpi-card" id="kpi-card-auditorias"[^>]*>', html).group(0)
        assert "invSubtab('lider')" in tarjeta

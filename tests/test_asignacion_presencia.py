"""
Asignación de trabajo: presencia y pull (m051asignacion, 2026-09-27).

## Lo que pasaba

El dueño, en Inventario Cíclico → Conteos: el botón decía «Asignar 6
pendientes», el formulario proponía «Cantidad 10» y UN solo operario, y el
desplegable ofrecía a «María Jefe Almacén». Tres defectos en una pantalla, y
debajo una clase: **nada en el sistema sabía quién había venido.**

- El 6 del botón lo contaba el servidor; el 10 estaba escrito en el HTML
  (`value="10"`) y el POST caía a `limite=20` si no llegaba nada.
- El desplegable eran «todos los usuarios activos con rol operario o jefe»:
  el jefe no hace conteos rutinarios (decisión del dueño), y «activo» es «tiene
  cuenta», no «vino hoy».
- El lote entero le caía a una persona.
- El 2º conteo (CC2) se le ponía al operario de id más bajo, viniera o no; y
  como el barrido de zombis solo mira EN_PROCESO, un CC2 PENDIENTE pegado a un
  incapacitado no lo soltaba nadie. Si el 1º lo había hecho un jefe, el 2º iba
  a un jefe o a un admin.
- Un traslado aprobado «para Pedro» quedaba con sus tareas pegadas a Pedro —y
  el dispensador se las ofrecía a Pedro AL FINAL, detrás de toda la cola de
  conteos—; con Pedro de vacaciones, no las hacía nadie.
- Un pedido chico «pegado» a quien lo empezó esperaba a que esa persona
  volviera, aunque se hubiera ido a casa.

## La clase y el trinquete

*«Se le pone dueño a una tarea sin preguntar si esa persona hace ese trabajo y
está.»* Toda escritura de `operario_id`/`abastecedor_id` distinta de `None`
(asignación directa, `.update({...})`, constructor del modelo, `setattr`) vive
en una función que llama a la política (`asignacion.exigir_asignable`,
`asignable_o_none` o `motivo_no_asignable`), o en un inventario declarado de
**autoasignaciones** —el que pide el trabajo es el asignado: su petición
prueba que está— que solo encoge. Por AST, con meta-tests y piso.

Lo encontró en su primera corrida: `TrasladoService._crear_picking_tienda`
copiaba el dueño de la solicitud a las tareas sin preguntar nada.
"""
import ast
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


# ═════════════════════════════════════════════════════════════════════════════
# El mundo: NB1 con su gente
# ═════════════════════════════════════════════════════════════════════════════

_N = [0]


def _persona(db, almacen, nombre, rol='operario', *, senal=True, **kw):
    from app.models.usuario import Usuario
    _N[0] += 1
    kw.setdefault('puede_picar', True)
    u = Usuario(nombre=nombre, email=f'{nombre.lower().replace(" ", "")}{_N[0]}@asig.test',
                password_hash=generate_password_hash('x'), rol=rol,
                almacen_id=almacen.id if almacen else None, activo=True,
                ultima_senal_at=datetime.utcnow() if senal else None, **kw)
    db.session.add(u)
    db.session.commit()
    return u


def _tok(app, u):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


def _conteo(db, almacen, *, operario=None, estado='PENDIENTE', origen=None, clase='C',
            tipo='DIARIO_ABC'):
    """Una sesión de conteo en su propio hueco (el índice de «una cadena viva
    por hueco»); un hijo, en el hueco de su raíz."""
    from app.models.conteo import SesionConteo
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    _N[0] += 1
    if origen is not None:
        ub_id, prod_id = origen.ubicacion_id, origen.producto_id
    else:
        p = Producto(codigo=f'ASG{_N[0]}', nombre=f'Prod {_N[0]}', codigo_siesa=f'ASG{_N[0]}')
        ub = Ubicacion(codigo=f'ASG-U{_N[0]}', almacen_id=almacen.id, tipo_zona='GENERAL',
                       stock_minimo=0, stock_maximo=999, secuencia_ruteo=_N[0], activo=True)
        db.session.add_all([p, ub])
        db.session.flush()
        ub_id, prod_id = ub.id, p.id
    s = SesionConteo(codigo=f'ASG-{_N[0]}', tipo=tipo, clasificacion_abc=clase,
                     ubicacion_id=ub_id, almacen_id=almacen.id, producto_id=prod_id,
                     estado=estado, es_segundo_conteo=origen is not None,
                     sesion_origen_id=origen.id if origen is not None else None,
                     operario_id=operario.id if operario else None,
                     fecha_inicio=datetime.utcnow() if estado == 'EN_PROCESO' else None)
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def nb1(db, almacen):
    """NB1 con tres contadores en turno (cupo 20 c/u), una jefa en turno y un
    supervisor en turno."""
    gente = {
        'ana': _persona(db, almacen, 'Ana', capacidad_diaria_conteo=20),
        'luis': _persona(db, almacen, 'Luis', capacidad_diaria_conteo=20),
        'caro': _persona(db, almacen, 'Caro', capacidad_diaria_conteo=20),
        'maria': _persona(db, almacen, 'Maria Jefe', rol='jefe_almacen'),
        'sup': _persona(db, almacen, 'Sup', rol='supervisor'),
    }
    return {'almacen': almacen, **gente}


def _sesion(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Presencia: una política
# ═════════════════════════════════════════════════════════════════════════════

class TestLaPresencia:

    def test_en_turno_sin_senal_ausente_inactivo(self, db, almacen):
        from app.services import presencia
        from app.models.ausencia import AusenciaUsuario
        hoy = presencia._dia()
        activo = _persona(db, almacen, 'Activo')
        nunca = _persona(db, almacen, 'Nunca', senal=False)
        viejo = _persona(db, almacen, 'Viejo')
        viejo.ultima_senal_at = datetime.utcnow() - timedelta(hours=3)
        incap = _persona(db, almacen, 'Incap')
        db.session.add(AusenciaUsuario(usuario_id=incap.id, motivo='INCAPACIDAD', desde=hoy,
                                       regreso=hoy + timedelta(days=3)))
        baja = _persona(db, almacen, 'Baja')
        baja.activo = False
        db.session.commit()
        cods = {u.nombre: presencia.estado(u)['codigo'] for u in (activo, nunca, viejo, incap, baja)}
        assert cods == {'Activo': 'EN_TURNO', 'Nunca': 'SIN_SENAL', 'Viejo': 'SIN_SENAL',
                        'Incap': 'AUSENTE', 'Baja': 'INACTIVO'}
        e = presencia.estado(incap)
        assert not e['disponible'] and 'incapacidad' in e['texto'] and 'regresa el' in e['texto']

    def test_la_ausencia_con_fecha_de_regreso_vence_sola(self, db, almacen):
        """Cubre de `desde` al día ANTERIOR a `regreso`; el día de regreso, con
        señal, vuelve a estar en turno sin que nadie toque nada."""
        from app.services import presencia
        from app.models.ausencia import AusenciaUsuario
        hoy = presencia._dia()
        u = _persona(db, almacen, 'Vuelve')
        a = AusenciaUsuario(usuario_id=u.id, motivo='VACACIONES', desde=hoy - timedelta(days=5),
                            regreso=hoy + timedelta(days=1))
        db.session.add(a)
        db.session.commit()
        assert a.cubre(hoy) and not a.cubre(hoy + timedelta(days=1))
        assert presencia.estado(u)['codigo'] == 'AUSENTE'
        manana = datetime.utcnow() + timedelta(days=1)
        u.ultima_senal_at = manana
        assert presencia.estado(u, ahora=manana)['codigo'] == 'EN_TURNO'

    def test_sin_fecha_de_regreso_no_vence(self, db, almacen):
        from app.services import presencia
        from app.models.ausencia import AusenciaUsuario
        hoy = presencia._dia()
        u = _persona(db, almacen, 'SinFecha')
        db.session.add(AusenciaUsuario(usuario_id=u.id, motivo='INCAPACIDAD',
                                       desde=hoy - timedelta(days=40)))
        db.session.commit()
        e = presencia.estado(u)
        assert e['codigo'] == 'AUSENTE' and 'sin fecha de regreso' in e['texto']

    def test_sql_y_python_contestan_igual(self, db, almacen):
        """La misma política escrita dos veces (una consulta no llama a Python):
        sobre la matriz completa, las dos dicen lo mismo."""
        from app.extensions import db as _db
        from app.models.ausencia import AusenciaUsuario
        from app.models.usuario import Usuario
        from app.services import presencia
        hoy = presencia._dia()
        ahora = datetime.utcnow()
        senales = {'nunca': None, 'vieja': ahora - timedelta(hours=5), 'reciente': ahora}
        ausencias = {
            'ninguna': None,
            'vigente': dict(desde=hoy, regreso=hoy + timedelta(days=2)),
            'vencida': dict(desde=hoy - timedelta(days=5), regreso=hoy),
            'futura': dict(desde=hoy + timedelta(days=1), regreso=hoy + timedelta(days=3)),
            'sin_regreso': dict(desde=hoy - timedelta(days=1), regreso=None),
            'anulada': dict(desde=hoy, regreso=hoy + timedelta(days=2), anulada_en=ahora),
        }
        usuarios = []
        for activo in (True, False):
            for sn, s in senales.items():
                for an, a in ausencias.items():
                    u = _persona(db, almacen, f'M-{activo}-{sn}-{an}', senal=False)
                    u.activo, u.ultima_senal_at = activo, s
                    if a:
                        db.session.add(AusenciaUsuario(usuario_id=u.id, motivo='OTRO', **a))
                    usuarios.append(u)
        db.session.commit()
        en_sql = {r[0] for r in _db.session.query(Usuario.id).filter(
            presencia.condicion_sql_disponible(Usuario.id, ahora=ahora)).all()}
        en_py = {u.id for u in usuarios if presencia.estado(u, ahora=ahora)['disponible']}
        en_sql &= {u.id for u in usuarios}
        assert en_sql == en_py
        assert len(en_py) == 4, 'reciente × {ninguna, vencida, futura, anulada}, activo'

    def test_toda_peticion_autenticada_que_escribe_es_senal(self, app, db, client, almacen):
        """Un POST con JWT deja señal; sin JWT no; y un GET tampoco — un GET
        no escribe (la regla de `test_lista_paradas_no_escribe`)."""
        u = _persona(db, almacen, 'Llega', senal=False)
        client.post('/api/mobile/confirmar', json={})              # sin JWT: nada
        client.get('/api/conteo/mis-tareas', headers=_tok(app, u))  # GET: nada
        db.session.expire_all()
        assert db.session.get(type(u), u.id).ultima_senal_at is None
        client.post('/api/mobile/confirmar', json={}, headers=_tok(app, u))
        db.session.expire_all()
        assert db.session.get(type(u), u.id).ultima_senal_at is not None

    def test_pedir_trabajo_es_senal(self, db, almacen):
        from app.services.mobile_service import MobileService
        u = _persona(db, almacen, 'Pide', senal=False)
        MobileService.get_tarea_actual(u.id)
        db.session.expire_all()
        assert db.session.get(type(u), u.id).ultima_senal_at is not None


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Quién puede: el desplegable
# ═════════════════════════════════════════════════════════════════════════════

class TestQuienPuede:

    def test_el_jefe_no_aparece_para_rutinarios_y_si_para_el_definitivo(self, db, nb1):
        from app.services import asignacion
        c = asignacion.candidatos(asignacion.CONTEO, almacen_id=nb1['almacen'].id)
        nombres = {x['nombre'] for x in c['disponibles'] + c['no_disponibles']}
        assert nombres == {'Ana', 'Luis', 'Caro'}, nombres
        d = asignacion.candidatos(asignacion.CONTEO_DEFINITIVO)
        assert {'Maria Jefe', 'Sup'} <= {x['nombre'] for x in d['disponibles']}

    def test_el_ausente_aparece_aparte_con_su_motivo(self, app, db, client, nb1):
        from app.services import presencia
        presencia.declarar_ausencia(nb1['luis'].id, 'INCAPACIDAD', por_id=nb1['sup'].id,
                                    regreso=(presencia._dia() + timedelta(days=4)).isoformat())
        r = client.get(f'/api/asignacion/candidatos?tipo=CONTEO&almacen_id={nb1["almacen"].id}',
                       headers=_tok(app, nb1['sup']))
        d = r.get_json()
        assert r.status_code == 200, d
        assert {x['nombre'] for x in d['disponibles']} == {'Ana', 'Caro'}
        (luis,) = d['no_disponibles']
        assert luis['nombre'] == 'Luis' and 'incapacidad' in luis['presencia_texto']

    def test_un_operario_no_ve_la_lista(self, app, client, nb1):
        r = client.get('/api/asignacion/candidatos?tipo=CONTEO', headers=_tok(app, nb1['ana']))
        assert r.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Repartir N pendientes
# ═════════════════════════════════════════════════════════════════════════════

class TestRepartir:

    def test_reparte_entre_los_presentes_segun_su_cupo(self, app, db, client, nb1):
        """72 sin dueño, tres presentes con cupo 20: 20/20/20 y 12 en la cola."""
        for _ in range(72):
            _conteo(db, nb1['almacen'])
        r = client.post('/api/conteo/asignar-lote', headers=_tok(app, nb1['sup']),
                        json={'almacen_id': nb1['almacen'].id})
        d = r.get_json()
        assert r.status_code == 200, d
        assert d['asignadas'] == 60 and d['quedan_en_pool'] == 12, d
        assert {p['nombre']: p['recibe'] for p in d['por_persona']} == \
            {'Ana': 20, 'Luis': 20, 'Caro': 20}

    def test_iguala_la_fraccion_de_cupo_no_el_numero(self, db, nb1):
        """Luis ya hizo 10 hoy: con 10 conteos para repartir, van a Ana y Caro
        (5 y 5) antes que a él."""
        from app.services import asignacion
        for _ in range(10):
            _conteo(db, nb1['almacen'], operario=nb1['luis'], estado='EN_PROCESO')
        for _ in range(10):
            _conteo(db, nb1['almacen'])
        p = asignacion.plan_reparto_conteos(nb1['almacen'].id)
        assert {x['nombre']: x['recibe'] for x in p['por_persona']} == \
            {'Ana': 5, 'Luis': 0, 'Caro': 5}, p

    def test_boton_vista_previa_y_ejecucion_dan_lo_mismo(self, app, db, client, nb1):
        for _ in range(7):
            _conteo(db, nb1['almacen'])
        h = _tok(app, nb1['sup'])
        alm = nb1['almacen'].id
        barra = client.get(f'/api/conteo/stats?almacen_id={alm}', headers=h).get_json()
        vp = client.get(f'/api/conteo/asignar-lote/vista-previa?almacen_id={alm}', headers=h).get_json()
        assert barra['sin_asignar'] == vp['pendientes'] == 7
        r = client.post('/api/conteo/asignar-lote', headers=h, json={'almacen_id': alm}).get_json()
        assert r['asignadas'] == vp['a_repartir'] == 7
        assert {p['id']: p['recibe'] for p in r['por_persona']} == \
            {p['id']: p['recibe'] for p in vp['por_persona']}

    def test_la_pantalla_no_tiene_un_numero_propio(self):
        """El 10 del formulario vivía en el HTML. La cantidad ahora es opcional
        y el número que se muestra viene del servidor."""
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        assert 'id="conteo-asignar-limite"' not in html
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        assert "|| 10" not in js.split('async function conteoAsignarLote')[1].split('\n}\n')[0]

    def test_el_cc2_no_va_a_quien_hizo_el_cc1(self, db, nb1):
        """Solo Ana presente, y el único pendiente es el CC2 de algo que ella
        contó: no se le da."""
        from app.services import asignacion
        for u in ('luis', 'caro'):
            nb1[u].ultima_senal_at = None
        db.session.commit()
        cc1 = _conteo(db, nb1['almacen'], operario=nb1['ana'], estado='SEGUNDO_CONTEO')
        cc2 = _conteo(db, nb1['almacen'], origen=cc1)
        p = asignacion.repartir_conteos(nb1['almacen'].id, por_id=nb1['sup'].id)
        assert p['asignadas'] == 0 and p['saltadas_doble_ciego'] == 1, p
        assert 'doble ciego' in p['motivo_sin_reparto']
        assert _sesion(db, cc2.id).operario_id is None

    def test_nadie_presente_no_se_asigna_y_se_dice(self, app, db, client, nb1):
        for u in ('ana', 'luis', 'caro'):
            nb1[u].ultima_senal_at = datetime.utcnow() - timedelta(hours=6)
        db.session.commit()
        s = _conteo(db, nb1['almacen'])
        d = client.post('/api/conteo/asignar-lote', headers=_tok(app, nb1['sup']),
                        json={'almacen_id': nb1['almacen'].id}).get_json()
        assert d['asignadas'] == 0 and d['quedan_en_pool'] == 1
        assert 'Nadie que haga conteos está en turno' in d['motivo_sin_reparto']
        assert 'Ana' in d['motivo_sin_reparto'], 'dice quiénes y por qué'
        assert _sesion(db, s.id).operario_id is None

    def test_al_jefe_no_se_le_reparte_aunque_lo_pidan(self, app, db, client, nb1):
        _conteo(db, nb1['almacen'])
        d = client.post('/api/conteo/asignar-lote', headers=_tok(app, nb1['sup']),
                        json={'almacen_id': nb1['almacen'].id,
                              'operario_ids': [nb1['maria'].id]}).get_json()
        assert d['asignadas'] == 0 and d['rechazados'] == [nb1['maria'].id], d

    def test_el_cc3_nunca_se_reparte(self, db, nb1):
        from app.services import asignacion
        cc1 = _conteo(db, nb1['almacen'], operario=nb1['ana'], estado='TERCER_CONTEO')
        cc2 = _conteo(db, nb1['almacen'], origen=cc1, estado='DESCUADRE', operario=nb1['luis'])
        _conteo(db, nb1['almacen'], origen=cc2)
        assert asignacion.plan_reparto_conteos(nb1['almacen'].id)['pendientes'] == 0

    def test_deja_la_asignacion_en_la_bitacora(self, db, nb1):
        from app.models.bitacora import BitacoraAccion
        from app.services import asignacion
        s = _conteo(db, nb1['almacen'])
        asignacion.repartir_conteos(nb1['almacen'].id, por_id=nb1['sup'].id)
        fila = BitacoraAccion.query.filter_by(entidad='SesionConteo', entidad_id=s.id).one()
        assert fila.accion == 'REASIGNAR' and fila.usuario_id == nb1['sup'].id


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Quien no está suelta lo que tenía
# ═════════════════════════════════════════════════════════════════════════════

def _picking(db, almacen, *, operario=None, estado='PENDIENTE', referencia='ST-ASG',
             tipo='TRASLADO', bodega='NB1'):
    from app.models.picking import TareaPicking
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    _N[0] += 1
    p = Producto(codigo=f'PK{_N[0]}', nombre=f'P {_N[0]}', codigo_siesa=f'PK{_N[0]}')
    ub = Ubicacion(codigo=f'PK-U{_N[0]}', almacen_id=almacen.id, tipo_zona='PICKING',
                   stock_minimo=0, stock_maximo=999, secuencia_ruteo=_N[0], activo=True)
    db.session.add_all([p, ub])
    db.session.flush()
    t = TareaPicking(codigo=f'PK-{_N[0]}', producto_id=p.id, cantidad_solicitada=5,
                     cantidad_recogida=0, ubicacion_id=ub.id, almacen_id=almacen.id,
                     estado=estado, prioridad=10, referencia_documento=referencia,
                     tipo_documento=tipo, bodega_origen_siesa=bodega,
                     operario_id=operario.id if operario else None,
                     fecha_inicio=datetime.utcnow() if estado == 'EN_PROCESO' else None)
    db.session.add(t)
    db.session.commit()
    return t


class TestQuienNoEstaSuelta:

    def test_ausencia_declarada_devuelve_todo_lo_que_se_puede_soltar(self, app, db, client, nb1):
        from app.models.bitacora import BitacoraAccion
        from app.models.picking import TareaPicking
        luis = nb1['luis']
        pend = _conteo(db, nb1['almacen'], operario=luis)
        curso = _conteo(db, nb1['almacen'], operario=luis, estado='EN_PROCESO')
        curso.cantidad_fisica = 4
        db.session.commit()
        pk = _picking(db, nb1['almacen'], operario=luis)
        pk_curso = _picking(db, nb1['almacen'], operario=luis, estado='EN_PROCESO', referencia='ST-2')
        r = client.post('/api/asignacion/ausencias', headers=_tok(app, nb1['maria']),
                        json={'usuario_id': luis.id, 'motivo': 'INCAPACIDAD',
                              'regreso': (date.today() + timedelta(days=5)).isoformat()})
        d = r.get_json()
        assert r.status_code == 201, d
        assert (d['devuelto']['conteos'], d['devuelto']['picking']) == (2, 1), d
        assert [x['id'] for x in d['devuelto']['requieren_decision']] == [pk_curso.id], (
            'un picking a medio recoger no se suelta solo: hay mercancía en un carro')
        assert _sesion(db, pend.id).operario_id is None
        c = _sesion(db, curso.id)
        assert (c.operario_id, c.estado) == (None, 'PENDIENTE')
        assert c.lista_conteos_descartados()[-1]['motivo'] == 'AUSENCIA', 'lo contado queda'
        assert db.session.get(TareaPicking, pk.id).operario_id is None
        assert db.session.get(TareaPicking, pk.id).ultimo_operario_id == luis.id
        assert BitacoraAccion.query.filter_by(accion='DESASIGNAR').count() == 3

    def test_el_barrido_suelta_lo_no_empezado_de_quien_no_da_senal(self, db, nb1):
        from app.services import asignacion
        caro = nb1['caro']
        pend = _conteo(db, nb1['almacen'], operario=caro)
        curso = _conteo(db, nb1['almacen'], operario=caro, estado='EN_PROCESO')
        otra = _conteo(db, nb1['almacen'], operario=nb1['ana'])
        caro.ultima_senal_at = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        r = asignacion.barrer()
        assert r['conteos'] == 1, r
        assert _sesion(db, pend.id).operario_id is None
        assert _sesion(db, curso.id).operario_id == caro.id, (
            'lo EMPEZADO lo cuida el barrido de inactividad, que mide la tarea')
        assert _sesion(db, otra.id).operario_id == nb1['ana'].id

    def test_el_barrido_suelta_la_reposicion_de_un_ausente(self, db, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.models.tarea_reposicion import TareaReposicion
        from app.services import asignacion, presencia
        luis = nb1['luis']
        luis.puede_abastecer = True
        ref = _conteo(db, nb1['almacen'])
        rep = TareaReposicion(codigo='REP-ASG-1', producto_id=ref.producto_id,
                              ubicacion_reserva_id=ref.ubicacion_id,
                              ubicacion_picking_id=ref.ubicacion_id,
                              almacen_id=nb1['almacen'].id, estado='EN_PROCESO', cantidad_unidades=5,
                              abastecedor_id=luis.id, fecha_inicio=datetime.utcnow())
        db.session.add(rep)
        db.session.add(AusenciaUsuario(usuario_id=luis.id, motivo='PERMISO', desde=presencia._dia()))
        db.session.commit()
        asignacion.barrer()
        db.session.expire_all()
        r = db.session.get(TareaReposicion, rep.id)
        assert (r.abastecedor_id, r.estado) == (None, 'PENDIENTE')

    def test_anular_la_ausencia_la_vuelve_a_poner_en_turno(self, app, db, client, nb1):
        from app.services import presencia
        d = presencia.declarar_ausencia(nb1['luis'].id, 'PERMISO', por_id=nb1['sup'].id)
        assert presencia.estado(nb1['luis'])['codigo'] == 'AUSENTE'
        r = client.post(f'/api/asignacion/ausencias/{d["ausencia"]["id"]}/anular',
                        headers=_tok(app, nb1['sup']), json={'motivo': 'volvió'})
        assert r.status_code == 200, r.get_json()
        assert presencia.estado(nb1['luis'])['codigo'] == 'EN_TURNO'

    def test_la_fecha_de_regreso_tiene_que_ser_despues(self, app, client, nb1):
        r = client.post('/api/asignacion/ausencias', headers=_tok(app, nb1['sup']),
                        json={'usuario_id': nb1['luis'].id, 'motivo': 'PERMISO',
                              'desde': '2026-10-05', 'regreso': '2026-10-05'})
        assert r.status_code == 400

    def test_el_equipo_dice_quien_esta_y_que_quedo_por_decidir(self, app, db, client, nb1):
        from app.services import presencia
        _picking(db, nb1['almacen'], operario=nb1['caro'], estado='EN_PROCESO')
        presencia.declarar_ausencia(nb1['caro'].id, 'INCAPACIDAD', por_id=nb1['sup'].id)
        d = client.get(f'/api/asignacion/equipo?almacen_id={nb1["almacen"].id}',
                       headers=_tok(app, nb1['sup'])).get_json()
        por = {p['nombre']: p for p in d['personas']}
        assert por['Caro']['presencia'] == 'AUSENTE' and por['Ana']['presencia'] == 'EN_TURNO'
        assert [x['nombre'] for x in d['requieren_decision']] == ['Caro']
        assert 'CONTEO' in por['Ana']['hace'] and 'CONTEO' not in por['Maria Jefe']['hace']


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Las puertas del líder y del sistema pasan por la política
# ═════════════════════════════════════════════════════════════════════════════

class TestLasPuertas:

    def test_el_cc2_nace_para_un_presente_y_nunca_para_el_del_cc1(self, db, nb1):
        from app.services.conteo_service import ConteoService
        # Como en la vida real: el CC1 ya está contado (SEGUNDO_CONTEO), así que
        # no es el «conflicto de hueco» lo que aparta a Ana, sino el doble ciego.
        cc1 = _conteo(db, nb1['almacen'], operario=nb1['ana'], estado='SEGUNDO_CONTEO')
        nb1['luis'].ultima_senal_at = None                    # Luis no vino
        db.session.commit()
        cc2 = ConteoService._crear_conteo_verificacion(cc1, None)
        db.session.commit()
        assert cc2.operario_id == nb1['caro'].id, 'Luis (id menor) no vino; Ana hizo el CC1'

    def test_sin_par_presente_el_cc2_queda_en_la_cola(self, db, nb1):
        from app.services.conteo_service import ConteoService
        cc1 = _conteo(db, nb1['almacen'], operario=nb1['ana'], estado='EN_PROCESO')
        for u in ('luis', 'caro'):
            nb1[u].ultima_senal_at = None
        db.session.commit()
        cc2 = ConteoService._crear_conteo_verificacion(cc1, nb1['ana'].id)
        db.session.commit()
        assert cc2.operario_id is None, 'ni a un ausente, ni a la jefa, ni al supervisor'

    def test_el_cc1_de_un_jefe_no_manda_el_cc2_a_otro_jefe(self, db, nb1):
        from app.services.conteo_service import ConteoService
        cc1 = _conteo(db, nb1['almacen'], operario=nb1['maria'], estado='EN_PROCESO')
        cc2 = ConteoService._crear_conteo_verificacion(cc1, nb1['maria'].id)
        db.session.commit()
        assert cc2.operario_id == nb1['ana'].id

    def test_reasignar_a_un_ausente_se_rechaza_con_el_motivo(self, app, db, client, nb1):
        from app.services import presencia
        s = _conteo(db, nb1['almacen'], operario=nb1['ana'])
        presencia.declarar_ausencia(nb1['luis'].id, 'VACACIONES', por_id=nb1['sup'].id)
        admin = _persona(db, nb1['almacen'], 'Admin', rol='admin')
        r = client.put(f'/api/conteo/{s.id}/editar', headers=_tok(app, admin),
                       json={'operario_id': nb1['luis'].id, 'motivo_edicion': 'x'})
        assert r.status_code == 400 and 'vacaciones' in r.get_json()['error'], r.get_json()

    def test_forzar_un_conteo_a_la_jefa_se_rechaza(self, db, nb1):
        from app.services.conteo_service import ConteoService
        s = _conteo(db, nb1['almacen'])
        with pytest.raises(ValueError, match='no hace conteos rutinarios'):
            ConteoService.reasignar_operario(s, nb1['maria'].id)

    def test_traslado_quien_recoge_tiene_que_ser_de_la_bodega_y_estar(self, app, db, client, nb1):
        from app.models.traslado import SolicitudTraslado
        otra = _persona(db, None, 'De NC1', bodega_siesa_id='NC1')
        s = SolicitudTraslado(codigo='ST-ASG-1', estado='EN_PICKING', bodega_origen_siesa='NB1',
                              bodega_destino_siesa='PC1', solicitante_id=nb1['sup'].id)
        db.session.add(s)
        db.session.commit()
        t = _picking(db, nb1['almacen'], referencia='ST-ASG-1')
        h = _tok(app, nb1['sup'])
        r = client.post(f'/api/traslados/{s.id}/reasignar-operario', headers=h,
                        json={'operario_id': otra.id})
        assert r.status_code == 409 and 'NB1' in r.get_json()['error']
        nb1['luis'].ultima_senal_at = None
        db.session.commit()
        r = client.post(f'/api/traslados/{s.id}/reasignar-operario', headers=h,
                        json={'operario_id': nb1['luis'].id})
        assert r.status_code == 409 and 'señal' in r.get_json()['error']
        r = client.post(f'/api/traslados/{s.id}/reasignar-operario', headers=h,
                        json={'operario_id': nb1['ana'].id})
        assert r.status_code == 200, r.get_json()
        from app.models.picking import TareaPicking
        db.session.expire_all()
        assert db.session.get(TareaPicking, t.id).operario_id == nb1['ana'].id
        d = client.get(f'/api/traslados/operarios-disponibles?solicitud_id={s.id}', headers=h).get_json()
        assert {o['nombre'] for o in d['operarios']} == {'Ana', 'Caro'}
        assert [o['nombre'] for o in d['no_disponibles']] == ['Luis']


# ═════════════════════════════════════════════════════════════════════════════
# 6 · El dispensador
# ═════════════════════════════════════════════════════════════════════════════

class TestElDispensador:

    def test_lo_que_el_lider_le_asigno_va_primero(self, db, nb1):
        """Antes quedaba detrás de toda la cola de conteos, y como PENDIENTE."""
        from app.services.mobile_service import MobileService
        _picking(db, nb1['almacen'], referencia='PED-POOL', tipo='PEDIDO', bodega=None)
        mio = _picking(db, nb1['almacen'], operario=nb1['ana'], referencia='ST-MIO')
        t = MobileService.get_tarea_actual(nb1['ana'].id)
        assert (t['id'], t['estado']) == (mio.id, 'EN_PROCESO'), t

    def test_la_jefa_no_recibe_conteos_de_la_cola(self, db, nb1):
        from app.services.mobile_service import MobileService
        _conteo(db, nb1['almacen'])
        assert MobileService.get_tarea_actual(nb1['maria'].id) is None
        assert MobileService.get_tarea_actual(nb1['ana'].id)['tipo'] == 'CONTEO'

    def test_el_pedido_chico_de_alguien_que_se_fue_lo_termina_otro(self, db, nb1):
        from app.services.mobile_service import MobileService
        _picking(db, nb1['almacen'], operario=nb1['luis'], estado='COMPLETADO',
                 referencia='PD-CHICO', tipo='PEDIDO_SIESA', bodega=None)
        resto = _picking(db, nb1['almacen'], referencia='PD-CHICO', tipo='PEDIDO_SIESA', bodega=None)
        assert MobileService.get_tarea_actual(nb1['ana'].id) is None, 'Luis está: sigue pegado'
        nb1['luis'].ultima_senal_at = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        assert MobileService.get_tarea_actual(nb1['ana'].id)['id'] == resto.id


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Trinquete: toda asignación pasa por la política
# ═════════════════════════════════════════════════════════════════════════════

CAMPOS = {'operario_id', 'abastecedor_id'}
MODELOS_TAREA = {'SesionConteo', 'TareaPicking', 'TareaReposicion', 'SolicitudTraslado'}
POLITICA = {'exigir_asignable', 'asignable_o_none', 'motivo_no_asignable'}
_ANIDADAS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: Autoasignaciones: **el que pide el trabajo es el asignado**, así que su
#: propia petición prueba que está (y la deja como señal, `presencia`). Solo
#: encoge. Una asignación a OTRA persona no entra acá: pasa por la política.
AUTOASIGNACIONES = {
    ('app/routes/picking.py', 'siguiente_tarea'):
        'El dispensador de /api/picking/siguiente-tarea: la tarea se le da al dueño del JWT '
        'que la pidió.',
    ('app/services/conteo_service.py', 'ConteoService.obtener_tarea_operario'):
        'El operario abre un conteo sin dueño (o el suyo): queda a su nombre quien lo abrió.',
    ('app/services/conteo_service.py', 'ConteoService._descartar_conteo'):
        'Recuento pedido a quien acaba de contar: la sesión sigue a nombre de quien la cerró.',
    ('app/services/conteo_service.py', 'ConteoService.registrar_conteo'):
        'Cierre del conteo: operario_id pasa a ser QUIÉN CONTÓ (lo lee el doble ciego).',
    ('app/services/mobile_service.py', 'MobileService.get_tarea_actual'):
        'Cola unificada: el conteo de la cola y el intercalado se le dan a quien pidió trabajo.',
    ('app/services/mobile_service.py', 'MobileService.get_tarea_actual._finalizar_asignacion'):
        'Cola unificada: el picking elegido se le da a quien pidió trabajo, bajo lock.',
    ('app/services/mobile_service.py', 'MobileService._next_conteo_tienda'):
        'Cola de la tienda: el siguiente conteo se le da al picker de tienda que lo pidió.',
    ('app/services/mobile_service.py', 'MobileService.procesar_escaneo'):
        'El primer escaneo de un picking PENDIENTE lo arranca a nombre de quien escaneó.',
    ('app/services/picking_service.py', 'PickingService.iniciar_picking'):
        'Iniciar una tarea: queda a nombre de quien la inicia (supervisión puede tomarla).',
    ('app/services/reposicion_service.py', 'get_tarea_abastecedor'):
        'Cola del abastecedor: la siguiente reposición se le da al abastecedor que la pidió.',
}


def _es_none(v) -> bool:
    return isinstance(v, ast.Constant) and v.value is None


def _nombre(f):
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


def escribe_dueno(n) -> bool:
    """Las cuatro escrituras de «ponerle dueño a una tarea»: `t.operario_id = x`,
    `q.update({'operario_id': x})`, `TareaPicking(operario_id=x)` y
    `setattr(t, 'operario_id', x)`. `= None` es soltar, no asignar."""
    if isinstance(n, (ast.Assign, ast.AnnAssign)):
        destinos = n.targets if isinstance(n, ast.Assign) else [n.target]
        if n.value is not None and not _es_none(n.value):
            return any(isinstance(t, ast.Attribute) and t.attr in CAMPOS for t in destinos)
    if isinstance(n, ast.Call):
        nom = _nombre(n.func)
        if nom == 'update':
            for a in n.args:
                if isinstance(a, ast.Dict) and any(
                        isinstance(k, ast.Constant) and k.value in CAMPOS and not _es_none(v)
                        for k, v in zip(a.keys, a.values)):
                    return True
        if nom in MODELOS_TAREA:
            return any(kw.arg in CAMPOS and not _es_none(kw.value) for kw in n.keywords)
        if nom == 'setattr' and len(n.args) >= 3 and isinstance(n.args[1], ast.Constant) \
                and n.args[1].value in CAMPOS and not _es_none(n.args[2]):
            return True
    return False


def _funciones(arbol):
    out = [('<modulo>', arbol)]

    def visitar(nodo, pre):
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f'{pre}{h.name}'
                out.append((q, h))
                visitar(h, q + '.')
            elif isinstance(h, ast.ClassDef):
                visitar(h, f'{pre}{h.name}.')
            else:
                visitar(h, pre)
    visitar(arbol, '')
    return out


def _propios(fn):
    pila = [n for n in getattr(fn, 'body', []) if not isinstance(n, _ANIDADAS)]
    while pila:
        n = pila.pop()
        yield n
        for h in ast.iter_child_nodes(n):
            if not isinstance(h, _ANIDADAS):
                pila.append(h)


def llama_a_la_politica(fn) -> bool:
    return any(isinstance(n, ast.Call) and _nombre(n.func) in POLITICA for n in _propios(fn))


def sitios(fuente: str) -> dict:
    """`{qualname: (lineas, llama_a_la_politica)}` de las funciones que ponen dueño."""
    out = {}
    for q, fn in _funciones(ast.parse(fuente)):
        lineas = sorted(n.lineno for n in _propios(fn) if escribe_dueno(n))
        if lineas:
            out[q] = (lineas, llama_a_la_politica(fn))
    return out


def _repo() -> dict:
    out = {}
    for base in ('app', 'flota'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            rel = p.relative_to(RAIZ).as_posix()
            for q, v in sitios(p.read_text(encoding='utf-8')).items():
                out[(rel, q)] = v
    return out


class TestTodaAsignacionPasaPorLaPolitica:

    def test_nadie_pone_dueno_sin_la_politica(self):
        rotos = {k: v[0] for k, v in _repo().items()
                 if not v[1] and k not in AUTOASIGNACIONES}
        assert not rotos, (
            f'Le ponen dueño a una tarea sin preguntar si la persona hace ese trabajo '
            f'y está: {rotos}. Pase por asignacion.exigir_asignable (acción de un líder) '
            f'o asignable_o_none (flujo del sistema). Si el asignado es el que pide, '
            f'declárelo en AUTOASIGNACIONES con su porqué.')

    def test_el_inventario_solo_encoge(self):
        hoy = _repo()
        viejos = [k for k in AUTOASIGNACIONES if k not in hoy or hoy[k][1]]
        assert not viejos, f'Ya no se autoasignan (o ya usan la política): sáquelos {viejos}'

    def test_cada_autoasignacion_dice_por_que(self):
        for k, razon in AUTOASIGNACIONES.items():
            assert len(razon.strip()) >= 40, f'{k}: la razón no explica nada'

    def test_piso(self):
        """Un escáner que se desincroniza devuelve cero, y cero se lee igual que
        «todo en orden»."""
        hoy = _repo()
        con_politica = [k for k, v in hoy.items() if v[1]]
        assert len(hoy) >= 19, f'solo {len(hoy)} sitios que ponen dueño: ¿se rompió el lector?'
        assert len(con_politica) >= 9, con_politica

    # ── meta ────────────────────────────────────────────────────────────────

    def test_meta_ve_las_cuatro_escrituras(self):
        fuente = (
            "def a(t, u):\n    t.operario_id = u\n"
            "def b(q, u):\n    q.update({'operario_id': u}, synchronize_session=False)\n"
            "def c(u):\n    return TareaPicking(codigo='x', operario_id=u)\n"
            "def d(t, u):\n    setattr(t, 'abastecedor_id', u)\n"
            "def e(t, u):\n    t.abastecedor_id: int = u\n"
            "def f(s, f2):\n    s.operario_id = f2.id if f2 else None\n")
        assert set(sitios(fuente)) == {'a', 'b', 'c', 'd', 'e', 'f'}

    def test_meta_no_marca_lo_sano(self):
        fuente = (
            'def a(t):\n    """t.operario_id = otro"""\n    # t.operario_id = otro\n    t.operario_id = None\n'
            "def b(q, u):\n    return q.filter_by(operario_id=u).filter(X.operario_id == u)\n"
            "def c(u):\n    return Bitacora(operario_id=u)\n"
            "def d(q):\n    q.update({'operario_id': None})\n"
            "def e(fila, u):\n    fila['operario_id'] = u\n")
        assert sitios(fuente) == {}

    def test_meta_ve_la_politica_y_no_la_de_una_funcion_hija(self):
        fuente = (
            "def a(t, u):\n    asignacion.exigir_asignable(u, 'CONTEO')\n    t.operario_id = u\n"
            "def b(t, u):\n    def hija():\n        asignable_o_none(u, 'PICKING')\n    t.operario_id = u\n")
        s = sitios(fuente)
        assert s['a'][1] is True and s['b'][1] is False


# ═════════════════════════════════════════════════════════════════════════════
# 8 · Las pantallas (Node, util.js + modal.js + app.js + conteo.js reales)
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'), vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, value: '', innerHTML: '', textContent: '' });
const cuerpo = [];
function nodo() {
  const botones = {};
  return { style: {}, innerHTML: '', remove() { const i = cuerpo.indexOf(this); if (i >= 0) cuerpo.splice(i, 1); },
           querySelector(sel) { return (botones[sel] = botones[sel] || { onclick: null, style: {} }); } };
}
const urls = [], posts = [], alertas = [];
const ctx = { console, URLSearchParams, encodeURIComponent,
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  document: { getElementById: el, createElement: () => nodo(),
              body: { appendChild(n) { cuerpo.push(n); } },
              querySelector: () => null, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, navigator: { onLine: true },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 1, clearInterval() {} };
ctx.globalThis = ctx; ctx.location = ctx.window.location; ctx.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['util.js', 'modal.js', 'app.js', 'conteo.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
const PLAN = (ids) => ({
  almacen_id: 7, pendientes: 72, a_repartir: ids ? 40 : 60, quedan_en_pool: ids ? 32 : 12,
  presentes: [{ id: 1, nombre: 'Ana' }, { id: 2, nombre: 'Luis <b>' }, { id: 3, nombre: 'Caro' }],
  por_persona: [{ id: 1, nombre: 'Ana', recibe: 20, cupo: 20, usados_hoy: 0, en_cola: 0 },
                { id: 2, nombre: 'Luis <b>', recibe: ids ? 0 : 20, cupo: 20, usados_hoy: 0, en_cola: 0 },
                { id: 3, nombre: 'Caro', recibe: 20, cupo: 20, usados_hoy: 0, en_cola: 0 }]
               .filter(p => !ids || ids.includes(String(p.id))),
  no_disponibles: [{ id: 9, nombre: 'Pedro', presencia_texto: 'está ausente: incapacidad — regresa el 30/09' }],
  saltadas_doble_ciego: 0, motivo_sin_reparto: null });
const EQUIPO = { en_turno: 1, ventana_senal_horas: 2, requieren_decision: [
    { id: 9, nombre: 'Pedro', presencia_texto: 'ausente', picking_en_curso: 1, empaque_en_curso: 0 }],
  personas: [
    { id: 1, nombre: 'Ana', rol: 'operario', presencia: 'EN_TURNO', presencia_texto: 'en turno',
      disponible: true, ausencia: null, hace: ['CONTEO', 'PICKING'], carga: { conteos_en_cola: 3 },
      cupo_conteo: { cupo: 20, usados_hoy: 2, en_cola: 3 } },
    { id: 9, nombre: 'Pedro', rol: 'operario', presencia: 'AUSENTE',
      presencia_texto: 'está ausente: incapacidad — regresa el 30/09', disponible: false,
      ausencia: { id: 55 }, hace: ['CONTEO'], carga: { picking_en_curso: 1 }, cupo_conteo: null }] };
vm.runInContext(`
  get = async (u) => { __urls.push(u);
    if (u.startsWith('/api/conteo/asignar-lote/vista-previa')) {
      const m = /operario_ids=([^&]*)/.exec(u); return __plan(m ? decodeURIComponent(m[1]).split(',') : null); }
    if (u.startsWith('/api/conteo/stats')) return { sin_asignar: 72, accion_requerida: 0 };
    if (u.startsWith('/api/asignacion/equipo')) return __equipo;
    if (u.startsWith('/api/dashboard/productividad')) return { operarios: [] };
    if (u.startsWith('/api/asignacion/candidatos')) return { disponibles: [{ id: 1, nombre: 'Ana' }],
      no_disponibles: [{ id: 9, nombre: 'Pedro', presencia_texto: 'ausente' }] };
    return {}; };
  post = async (u, b) => { __posts.push([u, b]);
    if (u === '/api/conteo/asignar-lote') return { asignadas: 40, quedan_en_pool: 32,
      por_persona: [{ nombre: 'Ana', recibe: 20 }, { nombre: 'Caro', recibe: 20 }] };
    return { devuelto: { conteos: 3, picking: 0, reposicion: 0, requieren_decision: [] } }; };
  alerta = (m, t) => __alertas.push([m, t]);
  cargarConteos = async () => {};
  ALMACEN_ID = 7;
`, Object.assign(ctx, { __urls: urls, __posts: posts, __alertas: alertas, __plan: PLAN, __equipo: EQUIPO }));
const R = (s) => vm.runInContext(s, ctx);
const out = {};
(async () => {
  el('inv-filtro-almacen').value = '7';
  await R('cargarConteoStats()');
  out.boton = els['cs-sin-asignar'].textContent;
  await R('conteoMostrarAsignar()');
  out.urlPrevia = urls.filter(u => u.includes('vista-previa')).pop();
  out.panel = els['conteo-asignar-cuerpo'].innerHTML;
  await R('conteoRepartoAlternar(2)');
  out.urlSinLuis = urls.filter(u => u.includes('vista-previa')).pop();
  out.panelSinLuis = els['conteo-asignar-cuerpo'].innerHTML;
  await R('conteoAsignarLote()');
  out.post = posts.filter(p => p[0] === '/api/conteo/asignar-lote').pop();
  out.alertaReparto = alertas.pop();
  // Selector del conteo manual: solo quienes cuentan y están.
  el('conteo-manual-almacen').value = '7';
  await R('conteoManualCargarOperarios()');
  out.urlCand = urls.filter(u => u.startsWith('/api/asignacion/candidatos')).pop();
  out.selector = els['conteo-manual-operario'].innerHTML;
  // Operarios: presencia, carga, ausencia.
  await R('cargarOperarios()');
  out.equipo = els['lista-operarios'].innerHTML;
  el('aus-motivo-1').value = 'INCAPACIDAD'; el('aus-regreso-1').value = '2026-10-01'; el('aus-nota-1').value = '';
  await R('operarioGuardarAusencia(1)');
  out.postAusencia = posts.filter(p => p[0] === '/api/asignacion/ausencias').pop();
  out.alertaAusencia = alertas.pop();
  const anular = R('operarioAnularAusencia(55)');
  cuerpo[cuerpo.length - 1].querySelector('#_mconf-si').onclick();
  await anular;
  out.postAnular = posts.filter(p => p[0].includes('/anular')).pop();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope='module')
def js():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLasPantallas:

    def test_el_boton_y_la_vista_previa_hablan_del_mismo_numero(self, js):
        assert str(js['boton']) == '72'
        assert 'almacen_id=7' in js['urlPrevia'] and 'operario_ids' not in js['urlPrevia']
        assert '<b>72</b> sin dueño' in js['panel'] and 'quedan <b>12</b>' in js['panel']

    def test_muestra_quien_recibe_y_quien_no_esta(self, js):
        assert 'recibe 20' in js['panel'] and 'Repartir 60' in js['panel']
        assert 'Pedro' in js['panel'] and 'incapacidad' in js['panel']
        assert 'Luis &lt;b&gt;' in js['panel'] and 'Luis <b>' not in js['panel'], 'el nombre va con esc()'

    def test_desmarcar_a_alguien_recalcula_sin_el(self, js):
        assert 'operario_ids=1%2C3' in js['urlSinLuis'] or 'operario_ids=1,3' in js['urlSinLuis']
        assert 'no participa' in js['panelSinLuis']

    def test_repartir_manda_lo_que_se_vio(self, js):
        url, body = js['post']
        assert body == {'almacen_id': 7, 'operario_ids': [1, 3]}, body
        assert 'Ana 20' in js['alertaReparto'][0] and js['alertaReparto'][1] == 'exito'

    def test_el_selector_solo_ofrece_a_quien_esta(self, js):
        assert 'tipo=CONTEO' in js['urlCand'] and 'almacen_id=7' in js['urlCand']
        assert '<option value="1">Ana</option>' in js['selector']
        assert 'value="no-9" disabled' in js['selector']

    def test_operarios_muestra_presencia_carga_y_lo_por_decidir(self, js):
        eq = js['equipo']
        assert 'En turno: 1 de 2' in eq
        assert 'Ausente' in eq and 'regresa el 30/09' in eq
        assert 'Tiene: 3 conteo(s) en su cola' in eq
        assert 'Por decidir' in eq and 'picking a medio recoger' in eq
        assert 'Ya volvió' in eq and 'Marcar ausencia' in eq

    def test_declarar_y_quitar_la_ausencia(self, js):
        url, body = js['postAusencia']
        assert body == {'usuario_id': 1, 'motivo': 'INCAPACIDAD', 'regreso': '2026-10-01', 'nota': None}
        assert 'Volvieron a la cola: 3 conteo(s)' in js['alertaAusencia'][0]
        assert js['postAnular'][0] == '/api/asignacion/ausencias/55/anular'

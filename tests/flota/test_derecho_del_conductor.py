"""
El rol no alcanza: sobre QUÉ vehículo opera el conductor.

## La clase

**Un endpoint `LECTURA_FLOTA` que recibe una placa, un vehículo o una entidad
—del cuerpo o de la URL— opera sin verificar que el conductor tiene derecho
sobre ella.** La puerta (`@exige`) pregunta por el ROL; la entidad la elige
quien manda el request. Es la forma de «un guard cuya precondición la manda
quien pide», que ya había costado la escalada del traspaso del 2026-09-02.

Los casos que la destaparon, ejecutados el 2026-09-24:

  · un conductor nombraba custodio a un compañero sobre un camión en la sede;
  · un conductor mandaba `POST /flota/odometro {placa: <ajena>, origen:
    correccion}` y le reescribía el odómetro —el CPK, el preventivo— a otro
    vehículo; y sin corrección igual registraba inspección, daño y tanqueo
    sobre cualquier placa;
  · `GET /flota/foto/<id>` le entregaba el escaneo del SOAT o de la tarjeta de
    propiedad de cualquier vehículo, y `/custodia/<id>/fotos` los turnos ajenos.

## El trinquete

Inventario **declarado** por AST de toda ruta de `flota/api/` cuya tupla de
`@exige(...)` admite al conductor. Cada una está en exactamente una lista:

  · `VERIFICAN` — la función llama DIRECTAMENTE a su política
    (`sin_derecho_sobre_*` de `flota/api/_permisos.py`);
  · `VERIFICAN_EN_EL_ADAPTADOR` — la regla vive en el servicio porque depende
    de estado que el servicio ya resuelve (el custodio actual);
  · `SIN_VERIFICAR` — con su porqué escrito. Solo puede encoger.

Y la otra mitad, que es la que muerde: **cada puerta que verifica se ejerce por
HTTP con el JWT de un conductor A sobre la entidad de un conductor B → 403**, y
sobre la suya → no 403. Una lista que dice «verifica» sin un request que lo
pruebe es la proxy que este repo ya aprendió a no creer.

Meta-tests: la forma que se detecta, la que no se marca (docstring, llamada en
una función anidada, tupla sin conductor), el piso de puertas y el ruido ante
una tupla que el detector no sabe resolver.
"""
import ast
import json
import shutil
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.routes._auth_helpers import Roles

RAIZ = Path(__file__).resolve().parents[2]
_API = RAIZ / 'flota' / 'api'
_H = 'Authorization'
_HASH = generate_password_hash('x')


# ═══════════════════════════════════════════════════════════════════════════
# El inventario declarado
# ═══════════════════════════════════════════════════════════════════════════

#: Puertas que admiten al conductor y le preguntan por la entidad. Valor: la
#: política que la función tiene que llamar ella misma.
VERIFICAN = {
    ('custodia.py', 'registrar_odometro'): 'sin_derecho_sobre_vehiculo',
    ('hallazgos.py', 'reportar'): 'sin_derecho_sobre_vehiculo',
    ('inspecciones.py', 'registrar'): 'sin_derecho_sobre_vehiculo',
    ('gastos.py', 'registrar_tanqueo'): 'sin_derecho_sobre_vehiculo',
    ('custodia.py', 'ver_foto'): 'sin_derecho_sobre_foto',
    ('custodia.py', 'fotos_de_custodia'): 'sin_derecho_sobre_custodia',
}

#: Puertas cuya regla vive en el servicio. Valor: (archivo del adaptador,
#: función del adaptador que la puerta llama, política que esa función llama).
VERIFICAN_EN_EL_ADAPTADOR = {
    ('custodia.py', 'custodia_traspaso'): (
        'flota/adaptadores/traspaso.py', 'traspasar',
        'custodio_que_puede_nombrar'),
}

#: Admiten al conductor y NO preguntan por la entidad. Cada una dice por qué.
SIN_VERIFICAR = {
    ('custodia.py', 'custodia_activa'):
        'Lectura. El conductor que va a RECIBIR un camión tiene que ver quién '
        'lo tiene y desde cuándo antes de tenerlo: exigirle la custodia para '
        'mirarla volvería imposible el recibo.',
    ('hallazgos.py', 'listar'):
        'Lectura. Recibir es reconocer lo heredado: los daños abiertos del '
        'camión se leen antes de firmar el recibo, y `mi-turno` ya expone el '
        'estado del vehículo sugerido.',
    ('inspecciones.py', 'items_del_dia'):
        'Lectura del catálogo de preguntas del día para ese tipo de vehículo. '
        'No escribe nada; la inspección (el POST) sí exige la custodia.',
    ('gastos.py', 'vocabulario'):
        'No nombra ninguna entidad: devuelve catálogos cerrados del dominio, '
        'iguales para todos los vehículos.',
    ('conductor.py', 'mi_turno'):
        'No recibe entidad: la identidad y el vehículo salen del token '
        '(`_conductor_del_token`), nunca del request.',
    ('conductor.py', 'mis_turnos'):
        'No recibe entidad: lista las custodias del conductor del token, '
        'filtradas por su propia ficha.',
}

#: Solo encoge. Subirlo es una decisión que se escribe en el PR, no un número
#: que se ajusta para que pase.
TOPE_SIN_VERIFICAR = 6


# ═══════════════════════════════════════════════════════════════════════════
# El detector (AST, nunca texto)
# ═══════════════════════════════════════════════════════════════════════════

class TuplaDesconocida(Exception):
    """El detector no sabe resolver la tupla de roles de un `@exige`.

    Se levanta en vez de devolver «no admite al conductor»: un detector que no
    entiende una forma y la da por limpia es el cero que se lee como «acá no
    hay nada».
    """


def _resolver_roles(expr):
    from flota.api import _permisos

    if (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
            and expr.value.id == 'Roles' and hasattr(Roles, expr.attr)):
        return tuple(getattr(Roles, expr.attr))
    if isinstance(expr, ast.Name) and hasattr(_permisos, expr.id):
        return tuple(getattr(_permisos, expr.id))
    raise TuplaDesconocida(ast.dump(expr))


def _es_ruta(fn):
    return any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and d.func.attr == 'route' for d in fn.decorator_list)


def _roles_de(fn):
    for d in fn.decorator_list:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                and d.func.id == 'exige' and d.args):
            return _resolver_roles(d.args[0])
    return None


def puertas_del_conductor(fuentes):
    """{(archivo, función)} de toda ruta cuya tupla de `exige` trae al conductor."""
    salida = set()
    for archivo, fuente in fuentes.items():
        for nodo in ast.walk(ast.parse(fuente)):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _es_ruta(nodo):
                continue
            roles = _roles_de(nodo)
            if roles is not None and Roles.CONDUCTOR in roles:
                salida.add((archivo, nodo.name))
    return salida


def _llamadas_propias(fn):
    """Las llamadas del cuerpo de `fn`, sin entrar a funciones anidadas.

    Una llamada dentro de un `def` interno o de una lambda no es la función
    preguntando: es una función que quizá nadie invoca.
    """
    pendientes = list(fn.body)
    while pendientes:
        n = pendientes.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                          ast.ClassDef)):
            continue
        if isinstance(n, ast.Call):
            yield n
        pendientes.extend(ast.iter_child_nodes(n))


def llama(fn, nombre):
    for c in _llamadas_propias(fn):
        f = c.func
        if (isinstance(f, ast.Name) and f.id == nombre) or (
                isinstance(f, ast.Attribute) and f.attr == nombre):
            return True
    return False


def _funcion(fuente, nombre):
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == nombre:
            return n
    raise LookupError(nombre)


def _fuentes_reales():
    return {p.name: p.read_text(encoding='utf-8') for p in sorted(_API.glob('*.py'))}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · El inventario, contra el código
# ═══════════════════════════════════════════════════════════════════════════

class TestElInventarioEsElCodigo:

    def test_toda_puerta_del_conductor_esta_declarada(self):
        reales = puertas_del_conductor(_fuentes_reales())
        declaradas = (set(VERIFICAN) | set(VERIFICAN_EN_EL_ADAPTADOR)
                      | set(SIN_VERIFICAR))
        nuevas = reales - declaradas
        assert not nuevas, (
            'rutas que admiten al conductor sin decir si verifican su derecho '
            'sobre la entidad:\n' + '\n'.join(f'  · {a}::{f}' for a, f in sorted(nuevas))
            + '\nO llaman a `sin_derecho_sobre_*` y van a VERIFICAN (con su caso '
              'en CASOS), o van a SIN_VERIFICAR con su porqué.')
        viejas = declaradas - reales
        assert not viejas, (
            'el inventario nombra puertas que ya no admiten al conductor o no '
            'existen:\n' + '\n'.join(f'  · {a}::{f}' for a, f in sorted(viejas)))

    def test_ninguna_puerta_esta_en_dos_listas(self):
        a, b, c = set(VERIFICAN), set(VERIFICAN_EN_EL_ADAPTADOR), set(SIN_VERIFICAR)
        assert not (a & b) and not (a & c) and not (b & c)

    def test_sin_verificar_solo_encoge(self):
        assert len(SIN_VERIFICAR) <= TOPE_SIN_VERIFICAR, (
            f'SIN_VERIFICAR creció a {len(SIN_VERIFICAR)}: una puerta nueva '
            f'del conductor que no pregunta por la entidad es la clase entera '
            f'de este archivo. Si de verdad no hace falta, se decide y se sube '
            f'el tope en el mismo cambio, con el motivo escrito.')

    @pytest.mark.parametrize('clave', sorted(SIN_VERIFICAR), ids=lambda k: '::'.join(k))
    def test_cada_excepcion_dice_por_que(self, clave):
        motivo = SIN_VERIFICAR[clave]
        assert len(motivo.strip()) >= 60, f'{clave}: «{motivo}» no es un motivo'

    @pytest.mark.parametrize('clave', sorted(VERIFICAN), ids=lambda k: '::'.join(k))
    def test_cada_puerta_que_verifica_llama_a_su_politica(self, clave):
        archivo, funcion = clave
        fn = _funcion((_API / archivo).read_text(encoding='utf-8'), funcion)
        assert llama(fn, VERIFICAN[clave]), (
            f'{archivo}::{funcion} figura como que verifica y no llama a '
            f'{VERIFICAN[clave]}() en su propio cuerpo')

    @pytest.mark.parametrize('clave', sorted(VERIFICAN_EN_EL_ADAPTADOR),
                             ids=lambda k: '::'.join(k))
    def test_la_regla_del_adaptador_se_llama_de_verdad(self, clave):
        archivo, funcion = clave
        ruta_ad, fn_ad, politica = VERIFICAN_EN_EL_ADAPTADOR[clave]
        puerta = _funcion((_API / archivo).read_text(encoding='utf-8'), funcion)
        assert llama(puerta, fn_ad), f'{funcion} ya no llama a {fn_ad}'
        adaptador = _funcion((RAIZ / ruta_ad).read_text(encoding='utf-8'), fn_ad)
        assert llama(adaptador, politica), f'{fn_ad} ya no llama a {politica}'

    def test_toda_puerta_que_verifica_tiene_su_caso_por_http(self):
        faltan = (set(VERIFICAN) | set(VERIFICAN_EN_EL_ADAPTADOR)) - set(CASOS)
        assert not faltan, (
            'puertas declaradas como verificadas sin un request que lo pruebe: '
            + ', '.join('::'.join(k) for k in sorted(faltan)))


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Meta: el detector muerde, y no muerde de más
# ═══════════════════════════════════════════════════════════════════════════

_SINTETICO = textwrap.dedent('''
    @bp.route('/x', methods=['POST'])
    @jwt_required()
    @exige(Roles.LECTURA_FLOTA, 'x')
    def abierta():
        """Menciona sin_derecho_sobre_vehiculo en el docstring y no la llama."""
        # sin_derecho_sobre_vehiculo(v, 'x')   <- comentario, tampoco
        def interna():
            return sin_derecho_sobre_vehiculo(v, 'x')
        return 1

    @bp.route('/y', methods=['POST'])
    @jwt_required()
    @exige(Roles.LECTURA_FLOTA, 'y')
    def cerrada():
        denegado = sin_derecho_sobre_vehiculo(v, 'y')
        return denegado

    @bp.route('/z', methods=['POST'])
    @jwt_required()
    @exige(MAESTROS_FLOTA, 'z')
    def de_maestros():
        return 1

    @exige(Roles.LECTURA_FLOTA, 'sin ruta')
    def no_es_ruta():
        return 1
''')


class TestElDetectorSeMide:

    def test_ve_las_puertas_del_conductor_y_solo_esas(self):
        p = puertas_del_conductor({'s.py': _SINTETICO})
        assert p == {('s.py', 'abierta'), ('s.py', 'cerrada')}

    def test_la_llamada_directa_cuenta(self):
        assert llama(_funcion(_SINTETICO, 'cerrada'), 'sin_derecho_sobre_vehiculo')

    def test_ni_el_docstring_ni_el_comentario_ni_una_funcion_anidada_cuentan(self):
        assert not llama(_funcion(_SINTETICO, 'abierta'), 'sin_derecho_sobre_vehiculo')

    def test_una_tupla_que_no_sabe_resolver_es_ruido_no_un_cero(self):
        raro = textwrap.dedent('''
            @bp.route('/w')
            @exige(ROLES_QUE_NO_EXISTEN, 'w')
            def w():
                return 1
        ''')
        with pytest.raises(TuplaDesconocida):
            puertas_del_conductor({'r.py': raro})

    def test_piso_de_puertas_reales(self):
        """Un escáner que se desincroniza devuelve cero. Hoy son 13."""
        assert len(puertas_del_conductor(_fuentes_reales())) >= 13


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Por rol, ejecutando la app
# ═══════════════════════════════════════════════════════════════════════════

def _auth(t):
    return {_H: f'Bearer {t}'}


def _usuario(db, rol, email):
    from app.models.usuario import Usuario

    u = Usuario(email=email, nombre=email.split('@')[0], rol=rol, activo=True,
                password_hash=_HASH)
    db.session.add(u)
    db.session.commit()
    return u


def _foto(db, entidad_tipo, entidad_id, autor_id):
    from flota.adaptadores.modelos import Foto

    f = Foto(clase='evidencia_estado', entidad_tipo=entidad_tipo,
             entidad_id=entidad_id, storage_ref=f'test/{entidad_tipo}/{entidad_id}',
             hash_sha256='0' * 64, bytes=10, ancho=800, alto=600,
             mime='image/jpeg', ts_captura=datetime(2026, 9, 24, 10),
             autor_usuario_id=autor_id)
    db.session.add(f)
    db.session.commit()
    return f.id


@pytest.fixture
def mundo(app, db):
    """A y B con su camión cada uno, C sin camión, uno en la sede, uno libre."""
    from app.models.almacen import Almacen
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import traspaso
    from flota.dominio.valores import CustodioTipo
    from tests.flota._turno import dar_turno

    for p in ('DRA100', 'DRB100', 'DRS100', 'DRD100'):
        db.session.add(Vehiculo(placa=p, tipo='Turbo', activo=True))
    sede = Almacen(codigo='DR-SEDE', nombre='Patio DR')
    db.session.add(sede)
    db.session.commit()

    ua = _usuario(db, 'conductor', 'dr-a@x.com')
    ub = _usuario(db, 'conductor', 'dr-b@x.com')
    uc = _usuario(db, 'conductor', 'dr-c@x.com')
    usin = _usuario(db, 'conductor', 'dr-sin-ficha@x.com')
    ucf = _usuario(db, 'control_flota', 'dr-cf@x.com')
    uad = _usuario(db, 'admin', 'dr-admin@x.com')

    ca = dar_turno(db, ua.id, 'DRA100', km=1000, nombre='Conductor A')
    cb = dar_turno(db, ub.id, 'DRB100', km=2000, nombre='Conductor B')
    # C tiene ficha y ningún camión.
    from app.models.conductor import Conductor
    cc = Conductor(nombre='Conductor C', cedula='DR-C', activo=True,
                   usuario_id=uc.id)
    db.session.add(cc)
    db.session.commit()
    # El de la sede lo dejó bien entregado un admin.
    vs = Vehiculo.query.filter_by(placa='DRS100').one()
    traspaso.traspasar(vehiculo_id=vs.id, km=500,
                       registrado_por_usuario_id=uad.id,
                       custodio_tipo=CustodioTipo.SEDE,
                       custodio_sede_id=sede.id)

    vehiculo_a = Vehiculo.query.filter_by(placa='DRA100').one()
    return {
        't': {k: create_access_token(identity=str(u.id)) for k, u in {
            'a': ua, 'b': ub, 'c': uc, 'sin': usin, 'cf': ucf, 'admin': uad,
        }.items()},
        'u': {'a': ua.id, 'b': ub.id, 'cf': ucf.id, 'admin': uad.id},
        'cond': {'a': ca.custodio_conductor_id, 'b': cb.custodio_conductor_id,
                 'c': cc.id},
        'custodia': {'a': ca.id, 'b': cb.id},
        'sede': sede.id,
        'foto': {
            'custodia_a': _foto(db, 'custodia_inicio', ca.id, uad.id),
            'custodia_b': _foto(db, 'custodia_inicio', cb.id, ub.id),
            'documento_a': _foto(db, 'documento', vehiculo_a.id, uad.id),
        },
    }


def _odometro(client, mundo, quien, placa, **extra):
    cuerpo = {'placa': placa, 'valor_km': 5000, 'origen': 'cierre_dia'}
    cuerpo.update(extra)
    return client.post('/flota/odometro', json=cuerpo,
                       headers=_auth(mundo['t'][quien]))


def _hallazgo(client, mundo, quien, placa):
    return client.post('/flota/hallazgos', json={
        'placa': placa, 'criticidad': 'menor', 'descripcion': 'raspón',
        'km': 5000}, headers=_auth(mundo['t'][quien]))


def _inspeccion(client, mundo, quien, placa):
    return client.post('/flota/inspeccion', json={
        'placa': placa, 'km': 5000, 'respuestas': [], 'segundos_llenado': 90,
    }, headers=_auth(mundo['t'][quien]))


def _tanqueo(client, mundo, quien, placa):
    return client.post('/flota/tanqueos', json={
        'placa': placa, 'fecha': '2026-09-24', 'valor': '100000',
        'galones': '8', 'tanque': 'parcial', 'estacion': 'Terpel', 'km': 5000,
        'proveedor': 'Terpel', 'origen_costo': 'tarjeta_convenio',
    }, headers=_auth(mundo['t'][quien]))


def _traspaso(client, mundo, quien, placa, **cuerpo):
    base = {'placa': placa, 'km': 6000}
    base.update(cuerpo)
    return client.post('/flota/custodia/traspaso', json=base,
                       headers=_auth(mundo['t'][quien]))


#: Por cada puerta que verifica: (lo de B pedido por A, lo propio pedido por A
#: — o por C, que no tiene camión, en el traspaso). La tabla es la prueba de
#: que la lista `VERIFICAN` no es una afirmación.
CASOS = {
    ('custodia.py', 'registrar_odometro'): (
        lambda c, m: _odometro(c, m, 'a', 'DRB100'),
        lambda c, m: _odometro(c, m, 'a', 'DRA100')),
    ('hallazgos.py', 'reportar'): (
        lambda c, m: _hallazgo(c, m, 'a', 'DRB100'),
        lambda c, m: _hallazgo(c, m, 'a', 'DRA100')),
    ('inspecciones.py', 'registrar'): (
        lambda c, m: _inspeccion(c, m, 'a', 'DRB100'),
        lambda c, m: _inspeccion(c, m, 'a', 'DRA100')),
    ('gastos.py', 'registrar_tanqueo'): (
        lambda c, m: _tanqueo(c, m, 'a', 'DRB100'),
        lambda c, m: _tanqueo(c, m, 'a', 'DRA100')),
    ('custodia.py', 'ver_foto'): (
        lambda c, m: c.get(f"/flota/foto/{m['foto']['custodia_b']}",
                           headers=_auth(m['t']['a'])),
        lambda c, m: c.get(f"/flota/foto/{m['foto']['custodia_a']}",
                           headers=_auth(m['t']['a']))),
    ('custodia.py', 'fotos_de_custodia'): (
        lambda c, m: c.get(f"/flota/custodia/{m['custodia']['b']}/fotos",
                           headers=_auth(m['t']['a'])),
        lambda c, m: c.get(f"/flota/custodia/{m['custodia']['a']}/fotos",
                           headers=_auth(m['t']['a']))),
    ('custodia.py', 'custodia_traspaso'): (
        # C dejando el camión de la sede a nombre de B.
        lambda c, m: _traspaso(c, m, 'c', 'DRS100', custodio_tipo='conductor',
                               custodio_conductor_id=m['cond']['b']),
        # C recibiéndolo a su nombre.
        lambda c, m: _traspaso(c, m, 'c', 'DRS100', custodio_tipo='conductor',
                               custodio_conductor_id=m['cond']['c'])),
}


class TestElConductorASobreLoDeB:

    @pytest.mark.parametrize('clave', sorted(CASOS), ids=lambda k: '::'.join(k))
    def test_lo_ajeno_es_403_y_lo_propio_no(self, client, mundo, clave):
        ajeno, propio = CASOS[clave]
        r = ajeno(client, mundo)
        assert r.status_code == 403, (clave, r.status_code, r.get_json())
        assert r.get_json()['motivo'] == 'sin_derecho', (
            'un 403 de derecho no manda a pedir otro rol: '
            f'{r.get_json()}')
        r2 = propio(client, mundo)
        assert r2.status_code != 403, (clave, r2.status_code, r2.get_json())

    def test_el_rechazo_no_escribe_nada(self, client, mundo):
        from flota.adaptadores.modelos import Gasto, Hallazgo, LecturaOdometro

        antes = (LecturaOdometro.query.count(), Hallazgo.query.count(),
                 Gasto.query.count())
        for clave in (('custodia.py', 'registrar_odometro'),
                      ('hallazgos.py', 'reportar'),
                      ('gastos.py', 'registrar_tanqueo')):
            assert CASOS[clave][0](client, mundo).status_code == 403
        assert (LecturaOdometro.query.count(), Hallazgo.query.count(),
                Gasto.query.count()) == antes


class TestLaCustodiaEsLaActiva:

    def test_despues_de_entregar_ya_no_registra_sobre_ese_camion(
            self, client, mundo):
        r = _traspaso(client, mundo, 'a', 'DRA100', custodio_tipo='sede',
                      custodio_sede_id=mundo['sede'], ubicacion='sede')
        assert r.status_code == 201, r.get_json()
        r = _odometro(client, mundo, 'a', 'DRA100', valor_km=6100)
        assert r.status_code == 403 and r.get_json()['motivo'] == 'sin_derecho'

    def test_un_conductor_sin_ficha_no_opera_ningun_camion(self, client, mundo):
        r = _odometro(client, mundo, 'sin', 'DRD100')
        assert r.status_code == 403
        assert 'vinculado' in r.get_json()['error']

    @pytest.mark.parametrize('quien', ['cf', 'admin'])
    def test_gestion_y_control_de_flota_operan_cualquier_camion(
            self, client, mundo, quien):
        for placa in ('DRA100', 'DRB100', 'DRD100'):
            r = _odometro(client, mundo, quien, placa, valor_km=7000)
            assert r.status_code == 201, (quien, placa, r.get_json())


class TestElCustodioLoNombraElQueRecibe:

    def test_A_no_deja_su_camion_a_nombre_de_C(self, client, mundo):
        """El relevo directo de conductor a conductor ya no existe: C respondería
        por un camión que no recibió ni fotografió."""
        r = _traspaso(client, mundo, 'a', 'DRA100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'])
        assert r.status_code == 403, r.get_json()
        from flota.adaptadores import traspaso
        from app.models.vehiculo import Vehiculo
        v = Vehiculo.query.filter_by(placa='DRA100').one()
        assert traspaso.custodia_activa(v.id).custodio_conductor_id == mundo['cond']['a']

    def test_A_no_manda_a_la_sede_el_camion_de_B(self, client, mundo):
        r = _traspaso(client, mundo, 'a', 'DRB100', custodio_tipo='sede',
                      custodio_sede_id=mundo['sede'], ubicacion='sede')
        assert r.status_code == 403, r.get_json()

    def test_A_si_entrega_el_suyo_a_la_sede(self, client, mundo):
        r = _traspaso(client, mundo, 'a', 'DRA100', custodio_tipo='sede',
                      custodio_sede_id=mundo['sede'], ubicacion='sede')
        assert r.status_code == 201, r.get_json()

    @pytest.mark.parametrize('quien', ['cf', 'admin'])
    def test_el_recibo_de_escritorio_nombra_a_cualquiera(self, client, mundo, quien):
        r = _traspaso(client, mundo, quien, 'DRS100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'])
        assert r.status_code == 201, r.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Corregir un odómetro no es registrarlo
# ═══════════════════════════════════════════════════════════════════════════

class TestLaCorreccionEsDeMaestros:

    def test_el_conductor_no_corrige_ni_su_propio_camion(self, client, mundo):
        r = _odometro(client, mundo, 'a', 'DRA100', origen='correccion',
                      motivo_correccion='me equivoqué', valor_km=900)
        assert r.status_code == 403, r.get_json()
        cuerpo = r.get_json()
        # Este 403 sí es de rol: dice a quién pedírselo.
        assert Roles.CONTROL_FLOTA in cuerpo['roles_permitidos']
        assert Roles.CONDUCTOR not in cuerpo['roles_permitidos']

    def test_ni_el_de_otro(self, client, mundo):
        r = _odometro(client, mundo, 'a', 'DRB100', origen='correccion',
                      motivo_correccion='x', valor_km=900)
        assert r.status_code == 403

    def test_control_de_flota_si_corrige(self, client, mundo):
        r = _odometro(client, mundo, 'cf', 'DRA100', origen='correccion',
                      motivo_correccion='el tablero decía 1.050', valor_km=1050)
        assert r.status_code == 201, r.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Las fotos: por padre, y nunca documentos
# ═══════════════════════════════════════════════════════════════════════════

class TestLasFotosDelConductor:

    def test_el_mapa_es_total_sobre_los_padres_de_foto(self):
        from flota.adaptadores.modelos import ENTIDAD_FOTO
        from flota.api._permisos import FOTO_DEL_CONDUCTOR
        from flota.dominio.valores import EntidadFoto

        assert set(FOTO_DEL_CONDUCTOR) == {e.value for e in EntidadFoto}
        assert set(FOTO_DEL_CONDUCTOR) == set(ENTIDAD_FOTO)

    def test_el_documento_no_lo_ve_ni_del_camion_que_maneja(self, client, mundo):
        r = client.get(f"/flota/foto/{mundo['foto']['documento_a']}",
                       headers=_auth(mundo['t']['a']))
        assert r.status_code == 403, r.get_json()

    def test_control_de_flota_si_ve_el_documento(self, client, mundo):
        r = client.get(f"/flota/foto/{mundo['foto']['documento_a']}",
                       headers=_auth(mundo['t']['cf']))
        assert r.status_code != 403

    def test_la_foto_de_su_turno_tomada_por_otro_tambien_es_suya(self, client, mundo):
        """Por padre y no por autor: la tomó el admin en el recibo y es la
        evidencia de cómo estaba el camión cuando se lo dieron."""
        r = client.get(f"/flota/foto/{mundo['foto']['custodia_a']}",
                       headers=_auth(mundo['t']['a']))
        assert r.status_code != 403

    def test_la_foto_de_su_propio_danio(self, client, mundo, db):
        r = _hallazgo(client, mundo, 'b', 'DRB100')
        assert r.status_code == 201, r.get_json()
        fid = _foto(db, 'hallazgo', r.get_json()['id'], mundo['u']['b'])
        assert client.get(f'/flota/foto/{fid}',
                          headers=_auth(mundo['t']['b'])).status_code != 403
        assert client.get(f'/flota/foto/{fid}',
                          headers=_auth(mundo['t']['a'])).status_code == 403


    def test_la_foto_del_recibo_de_su_propio_tanqueo(self, client, mundo, db):
        """El recibo del tanqueo (`entidad_tipo='gasto'`, de la app del
        conductor) es suyo por la lectura que él registró; el de otro, no."""
        r = _tanqueo(client, mundo, 'b', 'DRB100')
        assert r.status_code == 201, r.get_json()
        fid = _foto(db, 'gasto', r.get_json()['id'], mundo['u']['b'])
        assert client.get(f'/flota/foto/{fid}',
                          headers=_auth(mundo['t']['b'])).status_code != 403
        assert client.get(f'/flota/foto/{fid}',
                          headers=_auth(mundo['t']['a'])).status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 6 · El cierre forzado de control de flota
# ═══════════════════════════════════════════════════════════════════════════

class TestControlDeFlotaFuerzaConMotivo:

    def test_sin_motivo_no(self, client, mundo):
        r = _traspaso(client, mundo, 'cf', 'DRA100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'])
        assert r.status_code == 409, r.get_json()
        assert 'motivo' in r.get_json()['error']

    def test_ni_con_las_fotos_de_cierre(self, client, mundo):
        """El atajo del admin —con fotos es un cierre normal— a él no le sirve:
        es el rol medido por los cierres forzados (regla 11)."""
        from tests.flota.test_traspaso_t1 import _foto as foto_payload

        r = _traspaso(client, mundo, 'cf', 'DRA100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'],
                      fotos_fin=[foto_payload(i) for i in range(8)])
        assert r.status_code == 409, r.get_json()
        assert 'motivo' in r.get_json()['error']

    def test_con_motivo_si_y_queda_con_su_nombre(self, client, mundo):
        r = _traspaso(client, mundo, 'cf', 'DRA100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'],
                      motivo_forzado='A se fue sin cerrar; el camión sale con C')
        assert r.status_code == 201, r.get_json()
        from flota.adaptadores.modelos import Custodia

        anterior = Custodia.query.get(mundo['custodia']['a'])
        assert anterior.cierre_forzado is True
        assert anterior.cierre_forzado_por_usuario_id == mundo['u']['cf']
        cierres = client.get('/flota/custodia/cierres-forzados',
                             headers=_auth(mundo['t']['cf'])).get_json()['cierres']
        assert [c['placa'] for c in cierres] == ['DRA100']
        assert cierres[0]['forzado_por'] == 'dr-cf'

    def test_de_la_sede_no_hay_nada_que_forzar(self, client, mundo):
        r = _traspaso(client, mundo, 'cf', 'DRS100', custodio_tipo='conductor',
                      custodio_conductor_id=mundo['cond']['c'])
        assert r.status_code == 201, r.get_json()

    def test_quien_pide_sale_del_rol_para_todos_los_roles(self, app, db):
        """Total sobre `Roles`: un rol nuevo cae en CONDUCTOR, el mínimo."""
        from flota.api._permisos import quien_pide_de
        from flota.dominio.valores import QuienPide

        class _U:
            def __init__(self, rol):
                self.rol = rol

        roles = {v for k, v in vars(Roles).items()
                 if not k.startswith('_') and isinstance(v, str)}
        for rol in roles:
            esperado = (QuienPide.ADMIN_ZONA if rol in Roles.GESTION else
                        QuienPide.CONTROL_FLOTA if rol == Roles.CONTROL_FLOTA else
                        QuienPide.CONDUCTOR)
            assert quien_pide_de(_U(rol)) == esperado, rol
        assert quien_pide_de(None) == QuienPide.CONDUCTOR


# ═══════════════════════════════════════════════════════════════════════════
# 7 · La pantalla dice lo mismo que el backend
# ═══════════════════════════════════════════════════════════════════════════

_FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'

_HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const [FLOTAJS, GUION_P] = process.argv.slice(2);
const G = JSON.parse(fs.readFileSync(GUION_P, 'utf-8'));
function el(id) { return { id, innerHTML: '', value: 'sede', style: {}, textContent: '',
  classList: { add() {}, remove() {} }, addEventListener() {}, dataset: {} }; }
const els = {};
const ctx = {
  console, document: { getElementById(id) { if (!(id in els)) els[id] = el(id); return els[id]; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, body: el('body'), createElement: el },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, setTimeout, clearTimeout,
  setInterval: () => 0, clearInterval() {},
  get: async () => ({ conductores: [], almacenes: [] }),
  horaColombia: (x) => String(x), alerta() {}, API: '', TOKEN: '',
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(FLOTAJS.replace(/[^/]+$/, 'util.js'), 'utf-8'), ctx);
vm.runInContext('var OPERARIO = ' + JSON.stringify({ rol: G.rol }) + ';', ctx);
vm.runInContext(fs.readFileSync(FLOTAJS, 'utf-8'), ctx);
vm.runInContext('FLOTA_ESTADO = ' + JSON.stringify(G.estado) + '; FLOTA_PLACA = "DRA100";'
                + 'FLOTA_ANGULOS = FLOTA_ANGULOS_FIJOS.slice();', ctx);
ctx.flotaRenderRecibo();
const alta = vm.runInContext('flotaDondeSeDaDeAlta()', ctx);
await new Promise(r => setTimeout(r, 0));
process.stdout.write(JSON.stringify({ recibo: els['flota-recibo'].innerHTML, alta }));
"""


def _correr(tmp_path, rol, custodio_tipo):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(_HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    estado = {'odometro_actual': 1000, 'posiciones_llanta': 4,
              'posiciones_llanta_fuente': 'ficha', 'tipo': 'Turbo',
              'custodia': None if custodio_tipo is None else {
                  'id': 7, 'custodio_tipo': custodio_tipo,
                  'inicio_ts': '2026-09-24T10:00:00Z'}}
    g.write_text(json.dumps({'rol': rol, 'estado': estado}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(_FLOTA_JS), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestLaPantallaDiceLoMismo:

    def test_la_lista_del_js_es_FUERZA_CIERRE(self):
        import re

        from flota.api._permisos import FUERZA_CIERRE

        m = re.search(r'const FLOTA_ROLES_FUERZAN_CIERRE = \[([^\]]*)\]',
                      _FLOTA_JS.read_text(encoding='utf-8'))
        assert m, 'no existe FLOTA_ROLES_FUERZAN_CIERRE en flota.js'
        assert tuple(sorted(re.findall(r"'([a-z_]+)'", m.group(1)))) == \
            tuple(sorted(FUERZA_CIERRE))

    def test_control_de_flota_ve_el_motivo_si_el_turno_es_de_un_conductor(
            self, tmp_path):
        out = _correr(tmp_path, 'control_flota', 'conductor')
        assert 'flota-motivo-forzado' in out['recibo']
        assert 'obligatorio' in out['recibo']

    def test_si_viene_de_la_sede_no_hay_motivo_que_pedir(self, tmp_path):
        out = _correr(tmp_path, 'control_flota', 'sede')
        assert 'flota-motivo-forzado' not in out['recibo']

    def test_a_control_de_flota_no_lo_manda_a_una_pestania_que_no_ve(
            self, tmp_path):
        out = _correr(tmp_path, 'control_flota', None)
        assert 'Rutas' not in out['alta']
        assert 'administración' in out['alta']

    def test_al_admin_si_le_dice_donde(self, tmp_path):
        assert 'Rutas → Alta de vehículos' in _correr(tmp_path, 'admin', None)['alta']

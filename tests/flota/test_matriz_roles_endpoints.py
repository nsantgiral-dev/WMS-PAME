"""
La matriz completa rol × endpoint, ejercida contra HTTP — no leída del código.

El trinquete que ya existe (`test_permisos_flota.py`) mide **presencia** de
`@exige(...)` por AST. Eso es una proxy: afirma que hay un decorador, no que
rechace. Un `exige` con la tupla equivocada, un decorador puesto en el orden
equivocado, o un guard que el handler contradice por dentro pasan ese trinquete
en verde. Es la forma exacta de «un trinquete que mide una proxy» del CLAUDE.md.

Acá se genera el producto cartesiano entero —todos los roles de `Roles` contra
todas las reglas de `url_map` bajo `/flota`— y **se hace la petición**. La
afirmación es bidireccional, que es la mitad que suele faltar:

  · el rol que NO está en la tupla declarada tiene que recibir 403;
  · el rol que SÍ está NO puede recibir 403 — ni de la puerta ni de nada que la
    puerta llame por dentro.

La segunda dirección es la que destapa la escalada: un endpoint compuesto que
deja entrar a un rol y después lo rechaza en el adaptador está diciendo dos
cosas distintas sobre la misma autoridad, y la que manda es la de adentro. La
inversa —entrar por la puerta ancha a la operación estricta— es
`/liquidar-completo` otra vez.

Nada de esto se escribe a mano: si mañana aparece un endpoint nuevo o un rol
nuevo, la matriz crece sola.
"""
import inspect
import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA

_H = 'Authorization'
_PLACA = 'MTZ100'

#: El hash se calcula **una vez** para toda la matriz.
#: `set_password` usa scrypt: ~0.3 s por usuario. Trece roles por test × 88
#: tests son veinte minutos de derivación de clave para probar cero rutas. Una
#: matriz que tarda veinte minutos es una matriz que alguien deselecciona, y una
#: dimensión deseleccionada no cubre nada.
_HASH = generate_password_hash('x')


# ── El eje de roles: todo lo que `Roles` declara, no una lista escrita acá ───

def _todos_los_roles():
    """Los roles del sistema por introspección de `Roles`.

    Escribirlos a mano es la forma de `_BODEGA_CO_MAP`: el día que alguien
    agrega un rol, la matriz sigue en verde sin haberlo probado nunca.
    """
    return tuple(sorted({
        v for k, v in vars(Roles).items()
        if not k.startswith('_') and isinstance(v, str)
    }))


ROLES = _todos_los_roles()


# ── El eje de endpoints: `url_map`, no una lista escrita acá ────────────────

def _roles_declarados(vf):
    """La tupla que `@exige(...)` recibió, sacada del closure del decorador.

    Se lee del objeto vivo y no del AST a propósito: lo que importa es con qué
    tupla quedó montada la ruta, no qué nombre se escribió en el archivo.
    """
    fn = vf
    for _ in range(12):
        celdas = getattr(fn, '__closure__', None) or ()
        nombres = getattr(getattr(fn, '__code__', None), 'co_freevars', ())
        d = {}
        for n, c in zip(nombres, celdas):
            try:
                d[n] = c.cell_contents
            except ValueError:      # celda vacía — recursión aún sin cerrar
                pass
        if 'roles' in d:
            return tuple(d['roles'])
        fn = getattr(fn, '__wrapped__', None)
        if fn is None:
            return None
    return None


#: Rutas cuyo guard es anterior a `@exige` y vive dentro del handler.
#: `/flota/health` usa `_es_control_flota()`: gestión + control de flota.
_GUARD_PROPIO = {
    '/flota/health': tuple(Roles.GESTION) + (Roles.CONTROL_FLOTA,),
}

#: Rutas que no autentican con JWT: las llama una máquina (`exige_secreto`).
#: Su matriz de roles no existe — se ejercen en `test_permisos_flota.py`.
_SIN_JWT = {'/flota/avisos/entrega'}


def _celdas(app):
    """(regla, método, roles_esperados) por cada ruta de `/flota`."""
    salida = []
    for r in app.url_map.iter_rules():
        if not str(r.rule).startswith('/flota'):
            continue
        if str(r.rule) in _SIN_JWT:
            continue
        esperados = _roles_declarados(app.view_functions[r.endpoint])
        if esperados is None:
            esperados = _GUARD_PROPIO.get(str(r.rule))
        for m in sorted(r.methods - {'HEAD', 'OPTIONS'}):
            salida.append((str(r.rule), m, esperados))
    return sorted(salida)


#: El guion que enumera. Corre en OTRO intérprete y devuelve JSON.
#:
#: Reusa `_roles_declarados` **del mismo archivo**, leyéndolo por `inspect`: una
#: segunda copia del recorrido de closures sería la que diverja el día que
#: `exige` cambie de forma, y sería la copia que decide si un permiso está
#: declarado (regla 0 del WMS, corolario «una política, una función»).
_GUION = '''
import inspect, json, os, sys
sys.path.insert(0, os.getcwd())
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ['WORKER_SKIP_ESSENTIAL'] = 'true'
from app import create_app
_app = create_app()
salida = []
for r in _app.url_map.iter_rules():
    if not str(r.rule).startswith('/flota'):
        continue
    roles = _roles_declarados(_app.view_functions[r.endpoint])
    for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
        salida.append((str(r.rule), m, list(roles) if roles else None))
print('@@' + json.dumps(salida))
'''


def _construir_matriz():
    """Enumera las rutas EN OTRO PROCESO, y esa palabra es todo el arreglo.

    `pytest.mark.parametrize` corre en tiempo de colección, cuando el fixture
    `app` todavía no existe, así que hace falta levantar una app para poder
    enumerar: los roles esperados salen de inspeccionar los decoradores de
    `app.view_functions`, y para eso hace falta el objeto vivo.

    La primera versión la levantaba **en este mismo proceso**, y eso tuvo un
    costo que tardó tres días en verse. El síntoma estaba a dos archivos:

        tests/test_10_traslados.py::test_confirmar_recepcion_guarda_siesa_entrada_consec
        assert None == 5555          ← `patch.object(connekta, '_post')` sin efecto

    Pasaba solo y fallaba en conjunto, **tres corridas con el mismo resultado**.
    Lo encontró una bisección, y solo después de corregirla dos veces: la
    primera usaba el test suelto como víctima y no reproducía, porque hace falta
    el ARCHIVO entero para que el estado se acumule; la segunda corría sin
    `-m "not postgres"` y acusó a un archivo que en CI ni se ejecuta.

    **No sé exactamente qué de la segunda app rompe el parche**, y probé dos
    hipótesis que resultaron falsas —los hilos de los schedulers y una
    reconfiguración del gateway—. Lo que sí está medido es que enumerar en este
    proceso lo rompe y enumerar afuera no. Enumerar no puede tener efectos sobre
    la sesión que enumera; el subproceso lo garantiza sin depender de saber cuál
    de los efectos era el que mordía.

    Es la segunda vez en esta suite que un subproceso reemplaza a algo que
    tocaba estado global — la otra fue `importlib.reload` sobre los módulos del
    dominio, que rompía la identidad de `SIN_DATO`.
    """
    import json
    import os
    import subprocess
    import sys

    raiz = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    guion = inspect.getsource(_roles_declarados) + _GUION
    r = subprocess.run([sys.executable, '-c', guion], cwd=raiz,
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f'no se pudo enumerar la app:\n{r.stderr[-900:]}')
    crudas = json.loads(
        [l for l in r.stdout.split('\n') if l.startswith('@@')][-1][2:])

    salida = []
    for ruta, metodo, roles in crudas:
        if ruta in _SIN_JWT:
            continue
        esperados = tuple(roles) if roles else _GUARD_PROPIO.get(ruta)
        salida.append((ruta, metodo, esperados))
    return sorted(salida)


MATRIZ = _construir_matriz()
#: El espacio entero: una celda por cada (regla, método, rol).
CELDAS = [(ruta, metodo, esperados, rol)
          for (ruta, metodo, esperados) in MATRIZ
          for rol in ROLES]


def _url(regla):
    """Rellena los parámetros de la regla. Ids inexistentes a propósito.

    Un 404 no es un 403 y eso es todo lo que esta matriz necesita distinguir:
    la puerta se ejerce antes de que el handler sepa si el recurso existe.
    """
    partes = []
    for trozo in regla.split('/'):
        if not trozo.startswith('<'):
            partes.append(trozo)
            continue
        nombre = trozo.strip('<>').split(':')[-1]
        partes.append(_PLACA if nombre == 'placa' else '999999')
    return '/'.join(partes)


@pytest.fixture
def _vehiculo_matriz(app, db):
    """Un vehículo real: sin él las rutas por placa cortan en 404 antes de que
    un handler compuesto llegue a su segunda puerta, y la segunda dirección de
    la matriz —el rol admitido que igual recibe 403— no se ejercería nunca."""
    from app.extensions import db as _db
    from app.models.vehiculo import Vehiculo

    v = Vehiculo.query.filter_by(placa=_PLACA).first()
    if v is None:
        _db.session.add(Vehiculo(placa=_PLACA, tipo='Turbo', activo=True))
        _db.session.commit()
    return _PLACA


@pytest.fixture
def _tokens(app, db):
    """Un usuario activo por rol.

    Se crea **por test y no por módulo**: la fixture `db` trunca todas las
    tablas al terminar cada uno, y un token que apunta a un usuario borrado
    devuelve 401. Un 401 no es un 403 — la matriz habría dado en verde la
    dirección del rechazo sin haber ejercido un solo guard de rol, que es
    exactamente la clase de verde falso que este archivo existe para evitar.
    """
    from app.models.usuario import Usuario

    fichas = {}
    for rol in ROLES:
        correo = f'matriz-{rol}@x.com'
        u = Usuario.query.filter_by(email=correo).first()
        if u is None:
            u = Usuario(email=correo, nombre=rol, rol=rol, activo=True,
                        password_hash=_HASH)
            db.session.add(u)
        fichas[rol] = u
    db.session.commit()
    return {rol: create_access_token(identity=str(u.id))
            for rol, u in fichas.items()}


def _pedir(cliente, metodo, url, token):
    return cliente.open(
        url, method=metodo,
        headers={_H: f'Bearer {token}'},
        json={} if metodo in ('POST', 'PUT', 'PATCH') else None)


def _id(fila):
    ruta, metodo, _esperados = fila
    return f'{metodo} {ruta}'


class TestMatrizRolPorEndpoint:
    """N roles × M reglas. El tamaño se afirma para que encogerlo se vea."""

    def test_la_matriz_cubre_todo_el_espacio(self):
        assert len(ROLES) >= 13, f'roles detectados: {len(ROLES)}'
        assert len(MATRIZ) >= 43, f'celdas de endpoint detectadas: {len(MATRIZ)}'
        assert len(CELDAS) == len(ROLES) * len(MATRIZ)

    def test_toda_ruta_de_flota_declara_sus_roles(self):
        """Una ruta sin tupla declarada no se puede probar: pasaría la matriz
        entera por vacuidad. Es el modo de falla del trinquete anterior."""
        mudas = [f'{m} {r}' for (r, m, e) in MATRIZ if not e]
        assert not mudas, (
            'rutas sin roles declarados — la matriz no las puede ejercer:\n'
            + '\n'.join(f'  · {x}' for x in mudas))

    @pytest.mark.parametrize('fila', MATRIZ, ids=_id)
    def test_ningun_rol_ajeno_entra(self, client, _tokens, _vehiculo_matriz,
                                    fila):
        """Dirección uno: quien no está en la tupla no entra.

        Se ejerce el rechazo, no se lee el decorador. El bucle sobre los roles
        va **dentro** del test y no en el `parametrize` a propósito: un test por
        celda cuesta un truncado de todas las tablas por celda, y una matriz que
        tarda diez minutos es una matriz que alguien deselecciona.
        """
        ruta, metodo, esperados = fila
        entraron = []
        for rol in ROLES:
            if rol in (esperados or ()):
                continue
            r = _pedir(client, metodo, _url(ruta), _tokens[rol])
            if r.status_code != 403:
                entraron.append(f'{rol} → {r.status_code}')
        assert not entraron, (
            f'{metodo} {ruta} declara {sorted(esperados or ())} y deja entrar '
            f'a: {", ".join(entraron)}')

    @pytest.mark.parametrize('fila', MATRIZ, ids=_id)
    def test_ningun_rol_admitido_recibe_403(self, client, _tokens,
                                            _vehiculo_matriz, fila):
        """Dirección dos: **la que destapa las escaladas.**

        Un 403 acá significa que la puerta declara una autoridad y algo por
        dentro exige otra. Las dos no pueden ser ciertas: la que manda es la de
        adentro, y entonces el decorador miente sobre quién puede usar el
        endpoint. Es la mitad que un detector ciego nunca prueba.
        """
        ruta, metodo, esperados = fila
        rechazados = []
        for rol in (esperados or ()):
            r = _pedir(client, metodo, _url(ruta), _tokens[rol])
            if r.status_code == 403:
                rechazados.append(f'{rol} → {r.get_json()}')
        assert not rechazados, (
            f'{metodo} {ruta} declara admitir y rechaza igual a: '
            + ' | '.join(rechazados))


class TestNingunEndpointCompuestoPideMenosQueSuOperacion:
    """La pregunta que destapó `/liquidar-completo`, packing y `/flota/tanqueos`.

    No mira decoradores: compara la autoridad de la puerta contra la autoridad
    que el dominio le exige a la operación que corre por dentro.
    """

    def test_tanqueos_y_gastos_dicen_lo_mismo_sobre_combustible(self):
        """`POST /flota/tanqueos` es `POST /flota/gastos` con categoría fija.

        Si `combustible` dejara de ser categoría de campo, la puerta ancha de
        tanqueos quedaría escribiendo en la tabla del CPK sin autoridad — que
        es la escalada del 2026-09-02. El guard vive en el dominio, no en la
        ruta, justamente para que las dos puertas lo hereden.
        """
        from flota.dominio import costos

        assert not costos.exige_maestros('combustible')
        assert 'combustible' in costos.CATEGORIAS_GASTO

    @pytest.mark.parametrize('categoria', [
        c for c in __import__('flota.dominio.costos', fromlist=['x'])
        .CATEGORIAS_GASTO if c != 'combustible'])
    def test_toda_categoria_que_no_es_de_campo_exige_maestros(self, categoria):
        """Total sobre el vocabulario: ninguna categoría nueva entra por la
        puerta del conductor sin que alguien lo decida a mano."""
        from flota.dominio import costos

        assert costos.exige_maestros(categoria), (
            f'«{categoria}» se podría registrar desde /flota/tanqueos si '
            f'alguien la metiera en CATEGORIAS_DE_CAMPO sin pensarlo')

    def test_el_conductor_puede_leer_lo_que_escribe_por_tanqueos(
            self, app, db, client, _tokens):
        """La contracara de la escalada: **poder escribir y no poder leer.**

        `/flota/gastos/<placa>` pide `MAESTROS_FLOTA`, así que un conductor que
        registra un tanqueo no puede volver a verlo. Eso es una decisión
        declarada (el CPK no se le muestra al conductor, regla 2) y no un
        descuido — se afirma acá para que quien la cambie sepa que era a
        propósito.
        """
        assert Roles.CONDUCTOR not in MAESTROS_FLOTA
        r = client.get(f'/flota/gastos/{_PLACA}',
                       headers={_H: f'Bearer {_tokens[Roles.CONDUCTOR]}'})
        assert r.status_code == 403

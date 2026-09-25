"""
Un endpoint que encola un documento de plata exige el permiso más estricto de
lo que ejecuta.

La primera versión de este archivo (2026-08-13) nació con `/liquidar-completo`:
hacía lo mismo que `/liquidar` y `/liquidar-siesa` —las dos admin— y pedía
admin-o-jefe. **Y se escribió como una lista a mano de tres rutas.** El
2026-09-25 la misma forma volvió por una ruta que la lista no nombraba:
«Registrar cobro» encola un recibo de caja y sus retenciones —lo mismo que
«Enviar a Siesa», admin— y pedía admin-o-jefe. El guard medía las rutas que
alguien se acordó de escribir, no la propiedad.

## Ahora se descubre (AST, sobre todo `app/`)

1. **Encoladores de plata**: toda función que llama `.encolar(...)` con un tipo
   de `TIPOS_DE_PLATA` (primer argumento o `tipo=`). Cada uno tiene que estar
   en `PERMISO_DE_ENCOLADOR` con el permiso que lo protege — uno nuevo sin
   declarar pone esto rojo.
2. **Quién llega**: el grafo de llamadas por nombre, hacia arriba, hasta las
   funciones decoradas con `.route(...)`.
3. **Qué exige cada ruta**: los guards que llama (`_con_permiso(puede_x)`,
   `_solo_admin()`, …), traducidos a su conjunto de roles. La ruta tiene que
   dejar pasar **un subconjunto** de los roles del permiso de cada encolador
   que alcanza.

## Lo que NO cubre, dicho

- El grafo es **por nombre**: dos funciones homónimas se confunden (hacia el
  lado de ver de más, que es el seguro).
- Una llamada por string o por diccionario de funciones no se ve.
- Un guard **condicional** (reintentar un job de plata pide liquidar; uno de
  otro tipo, supervisión: `puede_reintentar_job`) no se ve: el reintento
  (`reencolar_job_fallido`) no es un encolador y su guard se prueba por
  comportamiento abajo.
- Los guards de la parada (conductor dueño O oficina) son disyunciones: esas
  rutas no llegan a ningún encolador de plata; si llegaran, esto las marcaría
  «sin guard reconocido».
"""
import ast
import pathlib
from types import SimpleNamespace

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'

#: Los documentos de plata del conductor y de la devolución.
TIPOS_DE_PLATA = {'RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA',
                  'NOTA_CREDITO_DEVOLUCION_CLIENTE'}

#: Encolador → el guard que lo protege. **Declarado**: un encolador nuevo entra
#: acá con el permiso de su operación, o esto se pone rojo.
PERMISO_DE_ENCOLADOR = {
    # Liquidación: recibo de caja y retenciones. Solo quien puede liquidar.
    '_encolar_recibo_caja': 'puede_liquidar',
    '_encolar_retencion': 'puede_liquidar',
    # La NC de una devolución la encola recepción al CONTAR lo que volvió: es
    # la operación de recepción, con su permiso.
    'confirmar_entrada_fisica': '_es_recepcion',
}

#: Excepciones aceptadas (ruta → por qué). **Solo puede encoger.**
EXCEPCIONES = {}


def _roles():
    from app.routes._auth_helpers import Roles
    return {v for k, v in vars(Roles).items() if k.isupper() and isinstance(v, str)}


def _roles_de_permiso(nombre: str) -> frozenset:
    """Qué roles deja pasar un guard, evaluándolo (las funciones de
    `permisos_liquidacion`) o leyéndolo de `Roles` (los guards viejos)."""
    from app.routes._auth_helpers import Roles
    fijos = {
        '_solo_admin': (Roles.ADMIN,),
        '_es_admin_o_jefe': Roles.ALMACEN,
        '_es_gestion': Roles.GESTION,
        '_es_recepcion': Roles.RECEPCION_ROLES,
    }
    if nombre in fijos:
        return frozenset(fijos[nombre])
    from app.services import permisos_liquidacion as pl
    fn = getattr(pl, nombre, None)
    if fn is None:
        return None
    return frozenset(r for r in _roles()
                     if fn(SimpleNamespace(rol=r, activo=True)))


# ═════════════════════════════════════════════════════════════════════════════
# El escáner
# ═════════════════════════════════════════════════════════════════════════════

def _tipo_encolado(call: ast.Call):
    f = call.func
    nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
    if nombre != 'encolar':
        return None
    arg = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == 'tipo'), None)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _nombre_llamado(call: ast.Call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)


def _es_ruta(fn) -> bool:
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                and d.func.attr == 'route':
            return True
    return False


def _funciones(arbol):
    """(función, llamadas propias) — las de una función anidada son de ella."""
    out = []

    def visitar(nodo, dueño):
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(h)
                visitar(h, h)
            else:
                visitar(h, dueño)
    visitar(arbol, None)
    return out


def _llamadas_propias(fn):
    """Las Call de `fn` sin entrar a funciones anidadas."""
    pila = list(ast.iter_child_nodes(fn))
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            yield n
        pila.extend(ast.iter_child_nodes(n))


def _guards(fn) -> list:
    """Los guards que una ruta llama: `_con_permiso(x)` → `x`; el resto por
    nombre si es un guard conocido."""
    out = []
    for c in _llamadas_propias(fn):
        n = _nombre_llamado(c)
        if n == '_con_permiso' and c.args and isinstance(c.args[0], ast.Name):
            out.append(c.args[0].id)
        elif n in ('_solo_admin', '_es_admin_o_jefe', '_es_gestion', '_es_recepcion'):
            out.append(n)
        elif n and n.startswith('puede_'):
            out.append(n)
    return out


def analizar(fuentes: dict) -> dict:
    """`{ruta_archivo: fuente}` → `{encoladores, rutas: {clave: {llega, guards}}}`."""
    defs, llamadas, rutas = {}, {}, {}
    encoladores = set()
    for archivo, src in fuentes.items():
        for fn in _funciones(ast.parse(src)):
            clave = f'{archivo}::{fn.name}:{fn.lineno}'
            defs.setdefault(fn.name, []).append(clave)
            nombres = set()
            for c in _llamadas_propias(fn):
                if _tipo_encolado(c) in TIPOS_DE_PLATA:
                    encoladores.add(fn.name)
                nombres.add(_nombre_llamado(c))
            llamadas[clave] = nombres
            if _es_ruta(fn):
                rutas[clave] = fn
    # Hacia arriba, por nombre: qué encoladores alcanza cada función.
    nombre_de = {c: c.split('::')[1].split(':')[0] for c in llamadas}
    alcanza = {e: {e} for e in encoladores}
    cambio = True
    while cambio:
        cambio = False
        for clave, nombres in llamadas.items():
            propio = nombre_de[clave]
            nuevo = set(alcanza.get(propio, set()))
            for n in nombres:
                nuevo |= alcanza.get(n, set())
            if nuevo - alcanza.get(propio, set()):
                alcanza[propio] = nuevo
                cambio = True
    alc_por_clave = {c: set().union(*(alcanza.get(n, set()) for n in ns))
                     if ns else set() for c, ns in llamadas.items()}
    salida = {}
    for clave, fn in rutas.items():
        llega = alc_por_clave.get(clave, set())
        if llega:
            salida[clave] = {'llega': llega, 'guards': _guards(fn)}
    return {'encoladores': encoladores, 'rutas': salida}


def violaciones(resultado: dict) -> list:
    out = []
    for clave, info in sorted(resultado['rutas'].items()):
        ruta = clave.rsplit(':', 1)[0]
        if ruta in EXCEPCIONES:
            continue
        conjuntos = [_roles_de_permiso(g) for g in info['guards']]
        conjuntos = [c for c in conjuntos if c is not None]
        if not conjuntos:
            out.append(f'{ruta}: llega a {sorted(info["llega"])} sin guard reconocido')
            continue
        deja_pasar = frozenset.intersection(*conjuntos)
        for e in sorted(info['llega']):
            permiso = PERMISO_DE_ENCOLADOR.get(e)
            exigido = _roles_de_permiso(permiso) if permiso else frozenset()
            if not deja_pasar <= exigido:
                out.append(f'{ruta}: llega a {e} (exige {permiso}) y deja pasar '
                           f'{sorted(deja_pasar - exigido)} de más')
    return out


def _fuentes_app():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8')
            for p in APP.rglob('*.py')}


@pytest.fixture(scope='module')
def resultado():
    return analizar(_fuentes_app())


# ═════════════════════════════════════════════════════════════════════════════
# El trinquete
# ═════════════════════════════════════════════════════════════════════════════

class TestNingunaRutaEncolaPlataConMenosPermiso:

    def test_todo_encolador_de_plata_esta_declarado(self, resultado):
        nuevos = resultado['encoladores'] - set(PERMISO_DE_ENCOLADOR)
        assert not nuevos, (
            f'\nEncoladores de plata sin permiso declarado: {sorted(nuevos)}. '
            'Declará en PERMISO_DE_ENCOLADOR qué permiso protege esa operación.')
        viejos = set(PERMISO_DE_ENCOLADOR) - resultado['encoladores']
        assert not viejos, f'ya no encolan plata (sacarlos de la lista): {sorted(viejos)}'

    def test_ninguna_ruta_exige_menos_que_lo_que_encola(self, resultado):
        v = violaciones(resultado)
        assert not v, '\n' + '\n'.join(v)

    def test_las_excepciones_solo_encogen(self):
        assert len(EXCEPCIONES) <= 0
        for ruta, porque in EXCEPCIONES.items():
            assert porque and len(porque) > 20, f'{ruta}: excepción sin su porqué'

    def test_piso(self, resultado):
        """Un escáner roto devuelve cero, y cero se lee como «acá no hay nada»."""
        assert len(resultado['encoladores']) >= 3
        nombres = {c.split('::')[1].split(':')[0] for c in resultado['rutas']}
        for esperada in ('liquidar_ruta_siesa', 'registrar_cobro_recaudo',
                         'confirmar_devolucion'):
            assert esperada in nombres, f'{esperada} ya no llega a un encolador: ¿escáner roto?'


class TestElEscanerMuerde:
    """Meta-tests: la forma que debe ver, la sana que no, y lo que no cuenta."""

    _SERVICIO = (
        "def servicio():\n"
        "    SiesaJob.encolar('RECIBO_CAJA', {})\n"
        "def servicio_kw():\n"
        "    SiesaJob.encolar(tipo='DOCUMENTO_CONTABLE_RET', payload={})\n"
        "def correo():\n"
        "    SiesaJob.encolar('ALERTA_EMAIL', {})\n"
        "def doc():\n"
        "    '''SiesaJob.encolar('RECIBO_CAJA') en un docstring no cuenta'''\n"
    )

    def _ruta(self, guard):
        return ("@bp.route('/x', methods=['POST'])\n"
                "def x():\n"
                f"    {guard}\n"
                "    capa()\n"
                "def capa():\n"
                "    servicio()\n")

    def test_ve_las_dos_escrituras_y_no_el_correo_ni_el_docstring(self):
        r = analizar({'s.py': self._SERVICIO})
        assert r['encoladores'] == {'servicio', 'servicio_kw'}

    def test_una_ruta_sin_guard_se_marca(self, monkeypatch):
        monkeypatch.setitem(PERMISO_DE_ENCOLADOR, 'servicio', 'puede_liquidar')
        r = analizar({'s.py': self._SERVICIO, 'r.py': self._ruta('pass')})
        assert any('sin guard' in v for v in violaciones(r))

    def test_una_ruta_mas_floja_se_marca(self, monkeypatch):
        monkeypatch.setitem(PERMISO_DE_ENCOLADOR, 'servicio', 'puede_liquidar')
        r = analizar({'s.py': self._SERVICIO,
                      'r.py': self._ruta('if not _es_admin_o_jefe(): return 1')})
        assert any('de más' in v for v in violaciones(r)), violaciones(r)

    def test_la_ruta_con_el_permiso_pasa(self, monkeypatch):
        monkeypatch.setitem(PERMISO_DE_ENCOLADOR, 'servicio', 'puede_liquidar')
        r = analizar({'s.py': self._SERVICIO,
                      'r.py': self._ruta('if not _con_permiso(puede_liquidar): return 1')})
        assert violaciones(r) == []

    def test_una_ruta_que_no_llega_no_se_mira(self):
        r = analizar({'s.py': self._SERVICIO,
                      'r.py': "@bp.route('/y')\ndef y():\n    correo()\n"})
        assert r['rutas'] == {}

    def test_el_guard_de_una_funcion_anidada_no_cuenta(self, monkeypatch):
        monkeypatch.setitem(PERMISO_DE_ENCOLADOR, 'servicio', 'puede_liquidar')
        src = ("@bp.route('/x')\n"
               "def x():\n"
               "    def _no_se_llama():\n"
               "        _con_permiso(puede_liquidar)\n"
               "    servicio()\n")
        r = analizar({'s.py': self._SERVICIO, 'r.py': src})
        assert any('sin guard' in v for v in violaciones(r))


# ═════════════════════════════════════════════════════════════════════════════
# Por comportamiento: lo que el escáner no ve
# ═════════════════════════════════════════════════════════════════════════════

class TestLasPuertasDeLaLiquidacion:

    def test_liquidar_completo_ya_no_existe(self, client, jwt_token_admin):
        """Se borró el 2026-09-25: sin pantalla (DEUDA_SIN_UI), escribía la
        retención sin pasar por la decisión de la oficina y mandaba el DC sin
        la cuenta ni la unidad de negocio reales (el Bug 3 del 2026-09-04)."""
        r = client.post('/api/rutas/1/liquidar-completo',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'}, json={})
        assert r.status_code in (404, 405)

    def test_un_jefe_no_registra_cobro(self, app, client, db, almacen):
        from app.models.usuario import Usuario
        from flask_jwt_extended import create_access_token
        u = Usuario(email='jefe_cobro@test.com', nombre='Jefe', rol='jefe_almacen', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            tok = create_access_token(identity=str(u.id))
        r = client.post('/api/rutas/1/recaudos/1/registrar-cobro',
                        headers={'Authorization': f'Bearer {tok}'}, json={})
        assert r.status_code == 403

    def test_reintentar_un_documento_de_plata_pide_liquidar(self, app, client, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.models.usuario import Usuario
        from flask_jwt_extended import create_access_token
        u = Usuario(email='sup_rc@test.com', nombre='Sup', rol='supervisor', activo=True)
        u.set_password('x')
        db.session.add(u)
        rc = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': 1})
        rc.estado = 'FALLIDO'
        otro = SiesaJob.encolar('AJUSTE_CONTEO', {'sesion_id': 1})
        otro.estado = 'FALLIDO'
        db.session.commit()
        with app.app_context():
            tok = create_access_token(identity=str(u.id))
        h = {'Authorization': f'Bearer {tok}'}
        assert client.post(f'/api/reposicion/siesa-jobs/{rc.id}/reintentar',
                           headers=h, json={}).status_code == 403
        assert client.post(f'/api/reposicion/siesa-jobs/{otro.id}/reintentar',
                           headers=h, json={}).status_code == 200

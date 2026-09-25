"""Cada rol solo lo suyo: el personal de almacén es una LISTA BLANCA, y por rol
se sabe qué escrituras atraviesan la puerta.

**El caso** (QA real con flota@/victor@, 2026-09-25): `control_flota` recibía
200 en `/api/productos/` —con `precio_compra`— y pasaba la puerta de
`/api/mobile/conteo/*`. `_es_personal_almacen` y el `_ROLES_SIN_ALMACEN` de
mobile eran listas NEGRAS («todos menos conductor y tienda»): todo rol que no
estuviera en la lista —el de flota ese día, el que se cree mañana— entraba.

**La clase**: *un permiso escrito como lista negra de roles*. Ahora hay una
sola lista blanca (`Roles.PERSONAL_ALMACEN`); agregar un rol es una línea.

Tres capas:

1. Casos por HTTP: control_flota no ve el catálogo con costos ni opera
   conteos; un operario sí.
2. **Inventario por rol** de las escrituras que NO responden 401/403 (con ids
   inexistentes y un cuerpo mínimo), para control_flota, conductor y tienda.
   Solo encoge; cada entrada dice por qué. Lo que responde 404 por el id antes
   de mirar el rol NO se puede medir así (declarado).
3. AST: ninguna comparación de `.rol` contra un conjunto que sea exactamente
   {conductor, tienda} (la firma de la lista negra), salvo el inventario.
"""
import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


def _token(app, db, almacen, rol, email):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    u = Usuario(nombre=rol, email=email, rol=rol, password_hash='x',
                almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Los casos
# ═════════════════════════════════════════════════════════════════════════════

class TestControlFlotaNoOperaAlmacen:

    def test_no_ve_el_catalogo_con_costos(self, app, client, db, almacen, producto):
        h = _token(app, db, almacen, 'control_flota', 'cf-lb@t.co')
        assert client.get('/api/productos/', headers=h).status_code == 403
        assert client.get(f'/api/productos/{producto.id}', headers=h).status_code == 403

    def test_no_pasa_la_puerta_de_conteo(self, app, client, db, almacen):
        h = _token(app, db, almacen, 'control_flota', 'cf-lb2@t.co')
        r = client.post('/api/mobile/conteo/total', json={'tarea_id': 1, 'total_acumulado': 1}, headers=h)
        assert r.status_code == 403
        assert client.get('/api/mobile/tarea-actual', headers=h).status_code == 403

    def test_un_operario_si(self, app, client, db, almacen, producto):
        h = _token(app, db, almacen, 'operario', 'op-lb@t.co')
        r = client.get('/api/productos/', headers=h)
        assert r.status_code == 200

    def test_la_lista_blanca_no_tiene_a_quien_no_opera_almacen(self):
        from app.routes._auth_helpers import Roles
        for rol in (Roles.CONDUCTOR, Roles.TIENDA, Roles.CONTROL_FLOTA,
                    Roles.LIQUIDADOR, Roles.LIDER_CARTERA):
            assert rol not in Roles.PERSONAL_ALMACEN


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Inventario por rol: escrituras que atraviesan la puerta
# ═════════════════════════════════════════════════════════════════════════════

_SERVICIO = 'la llama el Gestor de Cartera con su token de servicio (sin token: 503); el JWT del rol no cuenta'
_LOGIN = 'el login es público: es la puerta'
_SYNC = ('la cola offline: cada ítem se juzga por su tipo con la misma lista blanca; '
         'vacía responde 200 sin escribir')

#: rol → {«MÉTODO regla»: por qué}. **Solo encoge.** Se mide con ids
#: inexistentes y un cuerpo mínimo: un 404 por el id antes del rol no se ve.
ABIERTAS_POR_ROL = {
    'control_flota': {
        'POST /api/auth/login': _LOGIN,
        'POST /api/cartera/habilitaciones': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/autorizar': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/convertir-contado': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/reevaluar': _SERVICIO,
        'POST /api/mobile/sync': _SYNC,
        'POST /flota/avisos/barrer': 'flota: el barrido de avisos es del encargado de flota',
        'POST /flota/avisos/entrega': 'flota: webhook del canal de mensajería (503 sin configurar)',
        'POST /flota/custodia/traspaso': 'flota: su registro',
        'POST /flota/gastos': 'flota: su registro',
        'POST /flota/hallazgos': 'flota: su registro',
        'POST /flota/inspeccion': 'flota: su registro',
        'POST /flota/llantas': 'flota: su registro',
        'POST /flota/montajes': 'flota: su registro',
        'POST /flota/montajes/<int:montaje_id>/desmontar': 'flota: su registro',
        'POST /flota/odometro': 'flota: su registro',
        'POST /flota/odometro/<int:lectura_id>/verificar': 'flota: verifica kilometrajes',
        'POST /flota/ordenes/<int:orden_id>/factura': 'flota: su registro',
        'POST /flota/ordenes/<int:orden_id>/intervenciones': 'flota: su registro',
        'POST /flota/preventivo/tarea/<int:plan_id>/ejecucion': 'flota: su registro',
        'POST /flota/tanqueos': 'flota: su registro',
        'PUT /flota/preventivo/tarea/<int:plan_id>': 'flota: su registro',
    },
    'conductor': {
        'POST /api/auth/login': _LOGIN,
        'POST /api/cartera/habilitaciones': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/autorizar': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/convertir-contado': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/reevaluar': _SERVICIO,
        'POST /api/mobile/sync': _SYNC,
        'POST /flota/avisos/entrega': 'flota: webhook del canal de mensajería (503 sin configurar)',
        'POST /flota/custodia/traspaso': 'su turno: recibe y entrega el camión',
        'POST /flota/hallazgos': 'su turno: reporta un daño de SU camión (derecho verificado adentro)',
        'POST /flota/inspeccion': 'su turno: el preoperacional',
        'POST /flota/odometro': 'su turno: el kilometraje de SU camión',
        'POST /flota/tanqueos': 'su turno: tanquea SU camión',
    },
    'tienda': {
        'POST /api/auth/login': _LOGIN,
        'POST /api/cartera/habilitaciones': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/autorizar': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/convertir-contado': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/reevaluar': _SERVICIO,
        'POST /api/mobile/sync': _SYNC,
        'POST /api/traslados/': 'la tienda pide traslados y declara averías',
        'POST /api/traslados/invalidar-cache-stock': 'solo vacía el caché de stock del proceso: no escribe datos',
        'POST /flota/avisos/entrega': 'flota: webhook del canal de mensajería (503 sin configurar)',
    },
    # Los roles de la plata (2026-09-25). Sus escrituras propias (liquidar,
    # registrar cobro, confirmar retención…) contestan 404 con ids que no
    # existen **después** de mirar el rol: este inventario no las ve. Las mide
    # con ids reales `test_roles_plata.py::TestPorHttp`.
    'liquidador': {
        'POST /api/auth/login': _LOGIN,
        'POST /api/cartera/habilitaciones': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/autorizar': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/convertir-contado': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/reevaluar': _SERVICIO,
        'POST /api/mobile/sync': _SYNC,
        'POST /flota/avisos/entrega': 'flota: webhook del canal de mensajería (503 sin configurar)',
    },
    'lider_cartera': {
        'POST /api/auth/login': _LOGIN,
        'POST /api/cartera/habilitaciones': _SERVICIO,
        'POST /api/cartera/panel/credito-lote': 'autoriza en lote paradas de crédito no autorizado: es suyo (400 sin motivo)',
        'POST /api/cartera/retenciones/<int:rid>/autorizar': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/convertir-contado': _SERVICIO,
        'POST /api/cartera/retenciones/<int:rid>/reevaluar': _SERVICIO,
        'POST /api/mobile/sync': _SYNC,
        'POST /flota/avisos/entrega': 'flota: webhook del canal de mensajería (503 sin configurar)',
    },
}


def _url(regla: str) -> str:
    return re.sub(r'<(?:(\w+):)?(\w+)>', lambda m: '999999' if m.group(1) == 'int' else 'X', regla)


def abiertas(app, client, headers) -> dict:
    out = {}
    for rule in app.url_map.iter_rules():
        for m in sorted((rule.methods or set()) & {'POST', 'PUT', 'PATCH', 'DELETE'}):
            r = client.open(_url(rule.rule), method=m, headers=headers,
                            json={'tipo': 'PICKING', 'tarea_id': 999999, 'codigo': 'X'})
            if r.status_code not in (401, 403, 404, 405):
                out[f'{m} {rule.rule}'] = r.status_code
    return out


@pytest.mark.parametrize('rol', sorted(ABIERTAS_POR_ROL))
class TestInventarioDeEscriturasPorRol:

    def test_no_crece_y_solo_encoge(self, app, client, db, almacen, rol):
        hoy = abiertas(app, client, _token(app, db, almacen, rol, f'{rol}-inv@t.co'))
        declaradas = ABIERTAS_POR_ROL[rol]
        nuevas = sorted(set(hoy) - set(declaradas))
        assert not nuevas, (
            f'«{rol}» atraviesa la puerta de escrituras nuevas: {nuevas} ({[hoy[k] for k in nuevas]}). '
            f'Si es de su oficio, declárelo con su porqué; si no, falta el control de rol.')
        viejas = sorted(set(declaradas) - set(hoy))
        assert not viejas, f'ya no responden a «{rol}»: sáquelas del inventario: {viejas}'

    def test_cada_entrada_dice_por_que(self, rol):
        for k, v in ABIERTAS_POR_ROL[rol].items():
            assert isinstance(v, str) and len(v) > 10, (rol, k)


def test_control_flota_no_escribe_fuera_de_flota_salvo_lo_declarado():
    """La propiedad del caso, sobre el inventario: fuera de /flota/ solo las
    puertas que no son suyas por el JWT (login, servicio, la cola vacía)."""
    fuera = {k for k in ABIERTAS_POR_ROL['control_flota'] if ' /flota/' not in k}
    assert fuera == {'POST /api/auth/login', 'POST /api/mobile/sync',
                     'POST /api/cartera/habilitaciones',
                     'POST /api/cartera/retenciones/<int:rid>/autorizar',
                     'POST /api/cartera/retenciones/<int:rid>/convertir-contado',
                     'POST /api/cartera/retenciones/<int:rid>/reevaluar'}


def test_piso_de_escrituras_medidas(app):
    n = sum(len((r.methods or set()) & {'POST', 'PUT', 'PATCH', 'DELETE'}) for r in app.url_map.iter_rules())
    assert n >= 150, f'solo {n} escrituras en el url_map: ¿se rompió el recorrido?'


# ═════════════════════════════════════════════════════════════════════════════
# 3 · AST: ninguna lista negra {conductor, tienda}
# ═════════════════════════════════════════════════════════════════════════════

_NEGRA = {'CONDUCTOR', 'TIENDA'}

#: (archivo, función) → por qué. **Solo encoge.** Vacío desde el 2026-09-25:
#: `cartera_service.puede_autorizar` y `_ve_cartera` eran las dos últimas
#: («todos menos conductor y tienda» con la casilla); con el rol «líder de
#: cartera» pasaron a lista blanca (`Roles.CARTERA_POR_ROL`,
#: `Roles.CARTERA_CON_CASILLA`).
LISTAS_NEGRAS_DECLARADAS = {}


def _conjunto_de_roles(nodo):
    """Los nombres `Roles.X` / 'x' de una tupla o conjunto literal, o None."""
    if not isinstance(nodo, (ast.Tuple, ast.Set, ast.List)):
        return None
    nombres = set()
    for e in nodo.elts:
        if isinstance(e, ast.Attribute):
            nombres.add(e.attr.upper())
        elif isinstance(e, ast.Constant) and isinstance(e.value, str):
            nombres.add(e.value.upper())
        else:
            return None
    return nombres


def listas_negras(fuente: str):
    """`[función]` donde `.rol` se compara (in / not in) contra exactamente
    {conductor, tienda}, literal o por una constante del módulo."""
    arbol = ast.parse(fuente)
    constantes = {}
    for n in arbol.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            c = _conjunto_de_roles(n.value)
            if c is not None:
                constantes[n.targets[0].id] = c
    salida = []
    for fn in [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(fn):
            if (isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) and n.left.attr == 'rol'
                    and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops)):
                d = n.comparators[0]
                conj = constantes.get(d.id) if isinstance(d, ast.Name) else _conjunto_de_roles(d)
                if conj == _NEGRA:
                    salida.append(fn.name)
    return salida


def _encontradas():
    out = set()
    for base in ('app', 'flota'):
        for f in sorted((RAIZ / base).rglob('*.py')):
            for nombre in listas_negras(f.read_text(encoding='utf-8')):
                out.add((f.name, nombre))
    return out


class TestNingunaListaNegraDeRoles:

    def test_no_crece(self):
        nuevas = _encontradas() - set(LISTAS_NEGRAS_DECLARADAS)
        assert not nuevas, (f'Permiso por lista negra de roles: {sorted(nuevas)}. Use '
                            f'Roles.PERSONAL_ALMACEN (lista blanca) o declárelo.')

    def test_solo_encoge(self):
        assert not set(LISTAS_NEGRAS_DECLARADAS) - _encontradas()

    def test_piso_el_detector_recorre_el_codigo(self):
        """Con el inventario en cero, un escáner roto también da cero: que
        recorra de verdad `app/` (comparaciones de `.rol` contra un conjunto)."""
        n = 0
        for f in sorted((RAIZ / 'app').rglob('*.py')):
            for nodo in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if (isinstance(nodo, ast.Compare) and isinstance(nodo.left, ast.Attribute)
                        and nodo.left.attr == 'rol'
                        and any(isinstance(o, (ast.In, ast.NotIn)) for o in nodo.ops)):
                    n += 1
        assert n >= 100, f'solo {n} comparaciones de .rol en app/: ¿se rompió el recorrido?'

    def test_meta_ve_las_formas(self):
        src = ("_X = {Roles.CONDUCTOR, Roles.TIENDA}\n"
               "def a(u):\n    return u.rol not in (Roles.CONDUCTOR, Roles.TIENDA)\n"
               "def b(u):\n    return u.rol in _X\n"
               "def c(u):\n    return u.rol not in ('conductor', 'tienda')\n")
        assert listas_negras(src) == ['a', 'b', 'c']

    def test_meta_no_marca_lo_sano(self):
        src = ('"""u.rol not in (Roles.CONDUCTOR, Roles.TIENDA)"""\n'
               "def a(u):\n    return u.rol in Roles.PERSONAL_ALMACEN\n"
               "def b(u):\n    return u.rol in (Roles.ADMIN, Roles.CONDUCTOR, Roles.TIENDA)\n")
        assert listas_negras(src) == []

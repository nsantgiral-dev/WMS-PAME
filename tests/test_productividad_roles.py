"""La productividad del tablero lista solo a quien ejecuta tareas del piso.

**Qué pasaba** (validación e2e 2026-09-26): `productividad_operarios` tomaba a
todo usuario activo del almacén. El liquidador, el líder de cartera, la
tienda, compras, el gerente y control de flota salían como operarios con
«0 tareas» y un cupo de conteo de 15 que nadie les iba a asignar.

**La clase:** una lista de personas armada por «todo el que existe» en vez de
por «el rol que hace esto». **Ahora:** lista blanca `Roles.OPERAN_TAREAS` para
la productividad, y el cupo de conteo lo lleva quien la política de
asignación (`asignacion.motivo_no_elegible(u, CONTEO)`) deja contar.

**Integración n1 (2026-09-27):** aquí vivía una tupla `Roles.CUENTAN` cruzada
por AST contra los roles que `conteo_service` comparaba a mano. La asignación
por presencia sacó esas comparaciones de `conteo_service` (ahora pregunta a la
política) y decidió que supervisión no hace conteos rutinarios, solo el
definitivo: la tupla y la política decían dos cosas. Quedó una función; el
guard ahora exige que el tablero conteste lo mismo que la política, rol por
rol, y que `conteo_service` no vuelva a escribir roles a mano.
"""
import ast
from pathlib import Path

import pytest

from app.routes._auth_helpers import Roles

RAIZ = Path(__file__).resolve().parents[1]

_FUERA = ['liquidador', 'lider_cartera', 'tienda', 'compras', 'gerente', 'control_flota',
          'conductor', 'recepcionista']


def _crear(db, almacen, rol, n):
    from app.models.usuario import Usuario
    u = Usuario(nombre=f'U {rol} {n}', email=f'{rol}{n}@prod.test', password_hash='x',
                rol=rol, almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


class TestSoloQuienOperaElPiso:

    def test_la_productividad_no_lista_roles_de_oficina(self, app, db, almacen):
        from app.services.dashboard_service import DashboardService
        ids = {rol: _crear(db, almacen, rol, k).id
               for k, rol in enumerate(_FUERA + list(Roles.OPERAN_TAREAS))}
        filas = {o['operario_id']: o for o in
                 DashboardService.productividad_operarios(almacen.id, dias=7)['operarios']}
        for rol in _FUERA:
            assert ids[rol] not in filas, f'{rol} aparece en la productividad'
        for rol in Roles.OPERAN_TAREAS:
            assert ids[rol] in filas, f'{rol} opera el piso y no aparece'

    def test_el_cupo_de_conteo_solo_para_quien_cuenta(self, app, db, almacen):
        from app.services.dashboard_service import DashboardService
        ids = {rol: _crear(db, almacen, rol, k).id for k, rol in enumerate(Roles.OPERAN_TAREAS)}
        filas = {o['operario_id']: o for o in
                 DashboardService.productividad_operarios(almacen.id, dias=7)['operarios']}
        from app.models.usuario import Usuario
        from app.services import asignacion
        cuentan = 0
        for rol, uid in ids.items():
            cupo = filas[uid]['capacidad_diaria_conteo']
            if asignacion.motivo_no_elegible(db.session.get(Usuario, uid), asignacion.CONTEO) is None:
                cuentan += 1
                assert cupo == 15, (rol, cupo)
            else:
                assert cupo is None, (rol, cupo)
        assert cuentan >= 2, 'piso: la política deja contar al operario y al picker de traslado'
        # Decisión del dueño que la asignación aplica: supervisión solo hace el CC3.
        for rol in (Roles.SUPERVISOR, Roles.JEFE_ALMACEN, Roles.ADMIN, Roles.EMPACADOR):
            assert filas[ids[rol]]['capacidad_diaria_conteo'] is None, rol

    def test_las_tuplas_no_tienen_roles_de_plata_ni_de_oficina(self):
        for rol in _FUERA:
            assert rol not in Roles.OPERAN_TAREAS, rol
        assert not hasattr(Roles, 'CUENTAN'), ('quién cuenta lo contesta la política de '
                                               'asignación; una tupla aparte vuelve a divergir')


def roles_asignados_por_conteo(src):
    """Roles literales que `conteo_service` compara contra `Usuario.rol`
    (`Usuario.rol == 'x'` y `Usuario.rol.in_([...])`), por AST."""
    out = set()

    def es_rol(n):
        return (isinstance(n, ast.Attribute) and n.attr == 'rol'
                and isinstance(n.value, ast.Name) and n.value.id == 'Usuario')

    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Compare) and es_rol(n.left):
            out.update(c.value for c in n.comparators
                       if isinstance(c, ast.Constant) and isinstance(c.value, str))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'in_' and es_rol(n.func.value) and n.args
                and isinstance(n.args[0], (ast.List, ast.Tuple))):
            out.update(e.value for e in n.args[0].elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return out


class TestQuienCuentaLoDiceUnaSolaFuncion:

    def test_conteo_service_no_escribe_roles_a_mano(self):
        # A quién se le asigna un conteo lo decide `asignacion`; una
        # comparación `Usuario.rol == '...'` en conteo_service es una segunda
        # política que el tablero no ve.
        src = (RAIZ / 'app' / 'services' / 'conteo_service.py').read_text(encoding='utf-8')
        asignados = roles_asignados_por_conteo(src)
        assert not asignados, (f'conteo_service vuelve a elegir por rol a mano: {sorted(asignados)}; '
                               'que pregunte a app/services/asignacion.py')

    @pytest.mark.parametrize('src,esperado', [
        ("q.filter(Usuario.rol == 'operario')", {'operario'}),
        ("q.filter(Usuario.rol.in_(['jefe_almacen', 'admin']))", {'jefe_almacen', 'admin'}),
        ("q.filter(Otro.rol == 'tienda', Usuario.nombre == 'x')", set()),
        ("'''Usuario.rol == \"gerente\"'''", set()),
    ])
    def test_el_escaner_ve_las_dos_escrituras(self, src, esperado):
        assert roles_asignados_por_conteo(src) == esperado

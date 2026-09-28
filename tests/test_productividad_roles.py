"""La productividad del tablero lista solo a quien ejecuta tareas del piso.

**Qué pasaba** (validación e2e 2026-09-26): `productividad_operarios` tomaba a
todo usuario activo del almacén. El liquidador, el líder de cartera, la
tienda, compras, el gerente y control de flota salían como operarios con
«0 tareas» y un cupo de conteo de 15 que nadie les iba a asignar.

**La clase:** una lista de personas armada por «todo el que existe» en vez de
por «el rol que hace esto». **Ahora:** lista blanca en `Roles` —
`OPERAN_TAREAS` para la productividad y `CUENTAN` para el cupo de conteo—, y
`CUENTAN` se cruza por AST contra los roles que `conteo_service` escribe a mano
al asignar un conteo: si ese servicio asigna a un rol que no está en la tupla,
el tablero lo escondería.
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
        for rol, uid in ids.items():
            cupo = filas[uid]['capacidad_diaria_conteo']
            if rol in Roles.CUENTAN:
                assert cupo == 15, (rol, cupo)
            else:
                assert cupo is None, (rol, cupo)

    def test_las_tuplas_no_tienen_roles_de_plata_ni_de_oficina(self):
        for rol in _FUERA:
            assert rol not in Roles.OPERAN_TAREAS, rol
            assert rol not in Roles.CUENTAN, rol
        assert set(Roles.CUENTAN) <= set(Roles.OPERAN_TAREAS)


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


class TestCuentanCoincideConQuienRecibeConteos:

    def test_conteo_service_no_asigna_a_un_rol_fuera_de_cuentan(self):
        src = (RAIZ / 'app' / 'services' / 'conteo_service.py').read_text(encoding='utf-8')
        asignados = roles_asignados_por_conteo(src)
        assert len(asignados) >= 3, asignados          # piso: el escáner lee algo
        fuera = asignados - set(Roles.CUENTAN)
        assert not fuera, (f'conteo_service asigna conteos a {sorted(fuera)} y no están en '
                           'Roles.CUENTAN: el tablero les escondería el cupo')

    @pytest.mark.parametrize('src,esperado', [
        ("q.filter(Usuario.rol == 'operario')", {'operario'}),
        ("q.filter(Usuario.rol.in_(['jefe_almacen', 'admin']))", {'jefe_almacen', 'admin'}),
        ("q.filter(Otro.rol == 'tienda', Usuario.nombre == 'x')", set()),
        ("'''Usuario.rol == \"gerente\"'''", set()),
    ])
    def test_el_escaner_ve_las_dos_escrituras(self, src, esperado):
        assert roles_asignados_por_conteo(src) == esperado

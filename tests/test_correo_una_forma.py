"""Un correo, una forma: toda puerta que crea o busca un usuario por correo
pasa por `normalizar_email` / `Usuario.por_email`.

**El caso** (e2e del día completo, 2026-09-25): «Crear cuenta» del conductor
guardaba el correo en minúsculas y el login comparaba exacto. La cuenta creada
como `condA@e2e.co` no entraba con `condA@e2e.co` (401): el teclado del
teléfono pone la mayúscula inicial solo.

**La clase**: *dos puertas que tratan el mismo dato con dos políticas*. El
trinquete (AST sobre `app/` y `flota/`) exige que ninguna consulta compare
`email` a mano y que todo `Usuario(email=…)` lleve el valor normalizado.
Fuera de alcance, declarado: `scripts/` (arman usuarios de prueba con correos
literales en minúsculas).
"""
import ast
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
DUENO = RAIZ / 'app' / 'models' / 'usuario.py'


def _correo(client, correo, clave):
    return client.post('/api/auth/login', json={'email': correo, 'password': clave})


class TestEntraComoSeLoDieron:

    def test_cuenta_de_conductor_con_mayusculas(self, client, db, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post('/api/rutas/conductores', headers=h, json={'nombre': 'Ana', 'cedula': 'CC-mayus-1'})
        cid = (r.get_json().get('id') or r.get_json()['conductor']['id'])
        r = client.post(f'/api/rutas/conductores/{cid}/cuenta', headers=h,
                        json={'email': ' Ana.Ruiz@Papeleria.co ', 'password': 'Clave-1'})
        assert r.status_code == 201, r.get_json()
        for tecleado in ('Ana.Ruiz@Papeleria.co', 'ana.ruiz@papeleria.co', ' ANA.RUIZ@PAPELERIA.CO'):
            assert _correo(client, tecleado, 'Clave-1').status_code == 200, tecleado

    def test_registro_guarda_normalizado_y_no_duplica_por_mayusculas(self, client, db, jwt_token_admin):
        from app.models.usuario import Usuario
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post('/api/auth/register', headers=h,
                        json={'nombre': 'Bea', 'email': 'Bea@Pame.co', 'password': 'x1', 'rol': 'operario'})
        assert r.status_code in (200, 201), r.get_json()
        assert Usuario.query.filter(Usuario.nombre == 'Bea').one().email == 'bea@pame.co'
        r = client.post('/api/auth/register', headers=h,
                        json={'nombre': 'Bea2', 'email': 'BEA@pame.co', 'password': 'x1', 'rol': 'operario'})
        assert r.status_code == 409

    def test_una_fila_vieja_con_mayusculas_tambien_entra(self, client, db):
        from app.models.usuario import Usuario
        u = Usuario(nombre='Vieja', email='Vieja@Pame.co', rol='operario', activo=True)
        u.set_password('k')
        db.session.add(u)
        db.session.commit()
        assert _correo(client, 'vieja@pame.co', 'k').status_code == 200

    def test_clave_equivocada_sigue_siendo_401(self, client, db):
        from app.models.usuario import Usuario
        u = Usuario(nombre='C', email='c@pame.co', rol='operario', activo=True)
        u.set_password('k')
        db.session.add(u)
        db.session.commit()
        assert _correo(client, 'C@pame.co', 'otra').status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete
# ═════════════════════════════════════════════════════════════════════════════

def _archivos():
    for base in ('app', 'flota'):
        for f in sorted((RAIZ / base).rglob('*.py')):
            if '/tests/' not in str(f):
                yield f


def _es_normalizar(nodo):
    return (isinstance(nodo, ast.Call) and
            ((isinstance(nodo.func, ast.Name) and nodo.func.id == 'normalizar_email') or
             (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == 'normalizar_email')))


def violaciones(fuente: str):
    """`[(linea, forma)]`: consultas que comparan `email` a mano y
    `Usuario(email=…)` sin normalizar. Los comentarios no son código."""
    arbol = ast.parse(fuente)
    salida = []
    for fn in [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]:
        normalizados = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and _es_normalizar(n.value)
                        for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == 'filter_by' and any(k.arg == 'email' for k in n.keywords):
                salida.append((n.lineno, 'filter_by(email=…)'))
            if isinstance(f, ast.Name) and f.id == 'Usuario':
                for k in n.keywords:
                    if k.arg == 'email' and not (_es_normalizar(k.value) or
                                                 (isinstance(k.value, ast.Name) and k.value.id in normalizados)):
                        salida.append((n.lineno, 'Usuario(email=<sin normalizar>)'))
        for n in ast.walk(fn):
            if (isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute)
                    and n.left.attr == 'email' and any(isinstance(o, ast.Eq) for o in n.ops)):
                salida.append((n.lineno, '.email == …'))
    return sorted(set(salida))


class TestTrinqueteUnCorreoUnaForma:

    def test_nadie_compara_ni_crea_a_mano(self):
        malos = {}
        for f in _archivos():
            if f == DUENO:
                continue
            v = violaciones(f.read_text(encoding='utf-8'))
            if v:
                malos[str(f.relative_to(RAIZ))] = v
        assert not malos, (
            f'Un correo tratado por su cuenta: {malos}. Buscá con Usuario.por_email '
            f'y guardá con normalizar_email (app/models/usuario.py).')

    def test_meta_ve_las_tres_formas(self):
        src = ("def f(data):\n"
               "    Usuario.query.filter_by(email=data['email']).first()\n"
               "    Usuario.query.filter(Usuario.email == data['email'])\n"
               "    Usuario(nombre='x', email=data['email'])\n")
        assert [x[1] for x in violaciones(src)] == [
            'filter_by(email=…)', '.email == …', 'Usuario(email=<sin normalizar>)']

    def test_meta_no_marca_lo_sano(self):
        src = ('"""Usuario.query.filter_by(email=x)"""\n'
               "# Usuario(email=x)\n"
               "def f(e):\n"
               "    e = normalizar_email(e)\n"
               "    Usuario(nombre='x', email=e)\n"
               "    Usuario(nombre='y', email=normalizar_email(e))\n"
               "    Usuario.por_email(e)\n")
        assert violaciones(src) == []

    def test_piso(self):
        assert sum(1 for _ in _archivos()) >= 200

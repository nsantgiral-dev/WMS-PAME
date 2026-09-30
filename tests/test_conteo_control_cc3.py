"""
Quien cuenta no firma solo, nadie ve las cifras antes del definitivo, y saltar
un recuento deja rastro (P1-1 / P1-2, 2026-09-27).

## El defecto

- **El supervisor contaba el CC3 y aprobaba su propio ajuste, de cualquier
  monto.** Con un solo supervisor en producción, una persona decidía la cifra y
  la firmaba: un faltante que se quería tapar se tapaba con un CC3. Igual la
  auditoría de picking: un conteo de quien audita + su firma.
- **El «ciego» del CC3 era de una sola pantalla.** `GET /api/conteo/` (la
  pestaña Conteos, de supervisión) devolvía teórico, CC1 y CC2 de toda cadena
  viva a quien iba a contar el definitivo.
- **«Saltar conteo definitivo» aprobaba el CC1 que el doble ciego ya refutó**,
  sin motivo y sin rastro (solo el log), y lo firmaba el mismo líder.

## La clase

*Una decisión de ajuste que una sola persona toma entera*, y *una vista de
cifras que no pregunta si el conteo sigue ciego*. Una política cada una:
`ConteoService._motivo_ajuste_propio` (dentro de `motivo_no_puede_aprobar`) y
`ConteoService.cifras_ocultas`. Trinquete AST: toda función de las vistas de
conteo que lee una cifra pregunta a la política o está declarada con su porqué.
"""
import ast
import csv
import io
import pathlib

import pytest
from werkzeug.security import generate_password_hash

from tests.test_conteo_teorico_pos import siesa, tienda  # noqa: F401
from tests.test_conteo_pool_sin_dueno import _cadena_con_cc3, _sesion, _token

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


@pytest.fixture(autouse=True)
def _sin_env(monkeypatch):
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    for n in VARIABLES_DE_ENTORNO:
        monkeypatch.delenv(n, raising=False)
    # El camino automático (tolerancia, CC1 == CC2) sigue detrás del interruptor;
    # el defecto —todo espera aprobación— lo prueba test_conteo_ajuste_siempre_aprobado.
    monkeypatch.setenv('CONTEO_AJUSTE_AUTOMATICO', 'true')


def _usuario(db, tienda, email, rol):
    from app.models.usuario import Usuario
    u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                rol=rol, almacen_id=tienda['almacen'].id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _jobs(sid):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=sid).all()


def _cc3_contado(db, siesa, tienda, quien, fisico=9, costo=1000.0):
    """CC1 7, CC2 8 (discordantes) y el definitivo lo cuenta `quien`."""
    cc1, cc2, cc3 = _cadena_con_cc3(tienda, siesa)
    siesa.poner(existencia=10, pos=0, costo=costo)
    _svc().obtener_tarea_operario(cc3, quien.id)
    r = _svc().registrar_conteo(cc3, quien.id, fisico)
    assert r['resultado'] == 'DESCUADRE', r
    assert _sesion(db, cc1).estado == 'DESCUADRE'
    return cc1, cc2, cc3


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Quién firma el ajuste que definió un CC3
# ─────────────────────────────────────────────────────────────────────────────

class TestQuienFirma:

    def test_el_supervisor_que_conto_firma_hasta_el_tope(self, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'])   # 1 und × $1.000
        _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        assert len(_jobs(cc1)) == 1

    def test_por_encima_del_tope_no_lo_firma_quien_conto(self, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'], costo=200000.0)
        with pytest.raises(PermissionError, match='Usted contó'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        assert _jobs(cc1) == []

    def test_por_encima_del_tope_otro_supervisor_tampoco_lo_firma_el_admin(
            self, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'], costo=200000.0)
        otro = _usuario(db, tienda, 'sup2-cc3@test.com', 'supervisor')
        with pytest.raises(PermissionError, match='lo aprueba el admin'):
            _svc().confirmar_ajuste(cc1, otro.id)
        admin = _usuario(db, tienda, 'adm-cc3@test.com', 'admin')
        _svc().confirmar_ajuste(cc1, admin.id)
        assert len(_jobs(cc1)) == 1

    def test_el_admin_que_conto_tampoco_se_firma_por_encima(self, db, siesa, tienda):
        admin = _usuario(db, tienda, 'adm-cuenta@test.com', 'admin')
        cc1, _, _ = _cc3_contado(db, siesa, tienda, admin, costo=200000.0)
        with pytest.raises(PermissionError, match='Usted contó'):
            _svc().confirmar_ajuste(cc1, admin.id)
        otro = _usuario(db, tienda, 'adm-otro@test.com', 'admin')
        _svc().confirmar_ajuste(cc1, otro.id)

    def test_sin_costo_quien_conto_no_firma(self, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'], costo=None)
        with pytest.raises(PermissionError, match='no se sabe cuánto vale'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)

    def test_el_tope_se_configura(self, db, siesa, tienda, monkeypatch):
        monkeypatch.setenv('CONTEO_TOPE_AUTOAPROBACION', '500000')
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'], costo=200000.0)
        _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)

    def test_el_tablero_dice_por_que_no_aprueba(self, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'], costo=200000.0)
        from app.services.tablero_lider_conteo import tablero
        # Quien lo contó ve por qué no firma él; otro supervisor, que lo firma el admin.
        t = tablero(tienda['almacen'].id, rol='supervisor', usuario_id=tienda['supervisor'].id)
        (fila,) = t['decisiones']['ajustes']['aprobables']['filas']
        assert 'Usted contó' in (fila['no_puede_aprobar'] or '')
        t = tablero(tienda['almacen'].id, rol='supervisor')
        (fila,) = t['decisiones']['ajustes']['aprobables']['filas']
        assert 'lo aprueba el admin' in (fila['no_puede_aprobar'] or '')


class TestLaAuditoriaDePicking:

    def _tarea(self, db, tienda):
        from app.models.picking import TareaPicking
        t = TareaPicking(codigo='PK-AUD-1', producto_id=tienda['producto'].id,
                         cantidad_solicitada=5, cantidad_recogida=0,
                         ubicacion_id=tienda['ubicacion'].id,
                         almacen_id=tienda['almacen'].id, estado='BLOQUEADO')
        db.session.add(t)
        db.session.commit()
        return t

    def test_quien_audita_firma_solo_hasta_el_tope(self, db, siesa, tienda):
        t = self._tarea(db, tienda)
        siesa.poner(existencia=10, costo=200000.0)
        s = _svc().ajustar_desde_auditoria_picking(t, cantidad_fisica=9,
                                                   aprobador_id=tienda['supervisor'].id)
        assert s.estado == 'DESCUADRE' and _jobs(s.id) == [], (
            'quien auditó firmó su propio ajuste de $200.000')

    def test_barato_sale_con_su_firma(self, db, siesa, tienda):
        t = self._tarea(db, tienda)
        siesa.poner(existencia=10, costo=1000.0)
        s = _svc().ajustar_desde_auditoria_picking(t, cantidad_fisica=9,
                                                   aprobador_id=tienda['supervisor'].id)
        assert s.estado == 'AJUSTANDO' and len(_jobs(s.id)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Nadie ve las cifras antes del definitivo
# ─────────────────────────────────────────────────────────────────────────────

class TestLasCifrasOcultas:

    def test_la_lista_no_muestra_cifras_con_el_cc3_por_hacer(self, app, client, db, siesa, tienda):
        cc1, cc2, cc3 = _cadena_con_cc3(tienda, siesa)
        tok = _token(app, tienda['supervisor'])
        d = client.get('/api/conteo/?vista=accion', headers=tok).get_json()
        (raiz,) = [x for x in d['sesiones'] if x['id'] == cc1]
        assert raiz['cifras_ocultas']
        for campo in ('existencia_siesa', 'teorico_siesa', 'cantidad_fisica', 'diferencia'):
            assert raiz[campo] is None, campo
        assert raiz['segundo_conteo']['cantidad_fisica'] is None
        assert raiz['conteos_descartados'] == []
        # El CC3 mismo (pestaña En progreso) tampoco dice nada.
        d = client.get('/api/conteo/?vista=progreso', headers=tok).get_json()
        (fila3,) = [x for x in d['sesiones'] if x['id'] == cc3]
        assert fila3['cifras_ocultas'] and fila3['existencia_siesa'] is None

    def test_contado_el_definitivo_se_ven(self, app, client, db, siesa, tienda):
        cc1, _, _ = _cc3_contado(db, siesa, tienda, tienda['supervisor'])
        d = client.get('/api/conteo/?vista=accion', headers=_token(app, tienda['supervisor'])).get_json()
        (raiz,) = [x for x in d['sesiones'] if x['id'] == cc1]
        assert raiz['cifras_ocultas'] is None
        assert raiz['teorico_siesa'] == 10 and raiz['segundo_conteo']['cantidad_fisica'] == 8

    def test_el_csv_tampoco(self, app, client, db, siesa, tienda):
        """El CSV exporta lo cerrado: el CC2 ya contado (DESCUADRE) con su cifra
        salía mientras el definitivo seguía por hacerse."""
        _, cc2, _ = _cadena_con_cc3(tienda, siesa)
        r = client.get('/api/conteo/exportar', headers=_token(app, tienda['supervisor']))
        filas = {f['Codigo']: f for f in csv.DictReader(io.StringIO(r.get_data(as_text=True)))}
        fila = filas[_sesion(db, cc2).codigo]
        assert (fila['Existencia Siesa'], fila['Teorico Siesa'], fila['Conteo Fisico'],
                fila['Diferencia']) == ('', '', '', '')


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Saltar un recuento
# ─────────────────────────────────────────────────────────────────────────────

def _cadena_en_segundo(db, siesa, tienda):
    from tests.test_conteo_pool_sin_dueno import _cc1
    siesa.poner(existencia=10, pos=0)
    cc1, r1 = _cc1(tienda, tienda['a'], 7)
    assert r1['resultado'] == 'SEGUNDO_CONTEO'
    return cc1, r1['segundo_conteo_id']


class TestSaltarUnRecuento:

    def test_sin_motivo_no(self, app, client, db, siesa, tienda):
        cc1, _ = _cadena_en_segundo(db, siesa, tienda)
        r = client.post(f'/api/conteo/{cc1}/omitir-segundo', json={},
                        headers=_token(app, tienda['supervisor']))
        assert r.status_code == 400 and _sesion(db, cc1).estado == 'SEGUNDO_CONTEO'

    def test_saltar_el_segundo_deja_rastro_y_firma_otro(self, app, client, db, siesa, tienda):
        from app.models.bitacora import BitacoraAccion
        cc1, cc2 = _cadena_en_segundo(db, siesa, tienda)
        r = client.post(f'/api/conteo/{cc1}/omitir-segundo', json={'motivo': 'no hay quién'},
                        headers=_token(app, tienda['supervisor']))
        assert r.status_code == 200, r.get_json()
        raiz = _sesion(db, cc1)
        assert (raiz.estado, raiz.verificacion_omitida_por_id, raiz.verificacion_omitida_motivo) \
            == ('DESCUADRE', tienda['supervisor'].id, 'no hay quién')
        assert _sesion(db, cc2).estado == 'CANCELADO'
        acciones = {(b.accion, b.entidad_id) for b in BitacoraAccion.query.all()}
        assert ('CANCELAR', cc2) in acciones and ('EDITAR', cc1) in acciones
        # Quien omitió no lo aprueba, ni barato ($3.000).
        with pytest.raises(PermissionError, match='saltó la verificación'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        admin = _usuario(db, tienda, 'adm-omit@test.com', 'admin')
        _svc().confirmar_ajuste(cc1, admin.id)

    def test_saltar_el_definitivo_cancela_la_cadena(self, app, client, db, siesa, tienda):
        from app.models.bitacora import BitacoraAccion
        cc1, cc2, cc3 = _cadena_con_cc3(tienda, siesa)
        r = client.post(f'/api/conteo/{cc1}/omitir-segundo', json={'motivo': 'no hay supervisor'},
                        headers=_token(app, tienda['supervisor']))
        assert r.status_code == 200, r.get_json()
        assert [_sesion(db, x).estado for x in (cc1, cc3)] == ['CANCELADO', 'CANCELADO'], (
            'el CC1 que el 2º conteo refutó quedó aprobable')
        assert BitacoraAccion.query.filter_by(accion='CANCELAR', entidad_id=cc1).count() == 1
        assert _jobs(cc1) == []


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Trinquete: toda vista que pinta cifras pregunta si el conteo sigue ciego
# ─────────────────────────────────────────────────────────────────────────────

VISTAS = ('app/models/conteo.py', 'app/routes/conteo.py', 'app/services/conteo_listado.py',
          'app/services/tablero_lider_conteo.py')
CIFRAS = {'existencia_siesa', 'teorico_siesa', 'cantidad_fisica', 'diferencia',
          'cant_pos_siesa', 'salida_sin_conf_siesa', 'existencia_inicio_siesa',
          'cant_pos_inicio_siesa', 'salida_sin_conf_inicio_siesa', 'conteos_descartados'}
POLITICA = {'cifras_ocultas', '_cifras_ocultas'}

#: Funciones de las vistas que leen cifras sin preguntar, con su porqué. Solo encoge.
SIN_PREGUNTAR = {
    ('app/models/conteo.py', 'SesionConteo._to_dict_completo'):
        'Solo la llama `to_dict`, que aplica `cifras_ocultas` sobre su resultado '
        '(lo exige test_el_dict_completo_solo_sale_por_to_dict).',
    ('app/models/conteo.py', 'SesionConteo.lista_conteos_descartados'):
        'Lee el JSON de la columna: es un accesor, no una vista. `to_dict` lo vacía '
        'cuando las cifras están ocultas.',
    ('app/routes/conteo.py', 'confirmar_ajuste'):
        'Responde después de aprobar: la raíz estaba en DESCUADRE, y una raíz en '
        'DESCUADRE no tiene un CC3 vivo (llega ahí cuando el CC3 resuelve).',
    ('app/services/tablero_lider_conteo.py', '_ajustes'):
        'Solo raíces en DESCUADRE (`filtro_descuadres_por_decidir`): sin CC3 vivo '
        'por construcción.',
    ('app/services/tablero_lider_conteo.py', '_quien_decidio'):
        'La llama `_ajustes` sobre raíces en DESCUADRE: sin CC3 vivo.',
}


def _funciones(arbol):
    salida = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef):
            for hijo in nodo.body:
                if isinstance(hijo, ast.FunctionDef):
                    salida.append((f'{nodo.name}.{hijo.name}', hijo))
    en_clase = {id(n) for _, n in salida}
    salida += [(n.name, n) for n in ast.walk(arbol)
               if isinstance(n, ast.FunctionDef) and id(n) not in en_clase]
    return salida


def _leen_sin_preguntar(fuentes):
    malos = set()
    for archivo, src in fuentes:
        for nombre, fn in _funciones(ast.parse(src)):
            lee = any(isinstance(a, ast.Attribute) and a.attr in CIFRAS
                      and isinstance(a.ctx, ast.Load) for a in ast.walk(fn))
            pregunta = any(isinstance(c, ast.Call) and (
                getattr(c.func, 'attr', None) in POLITICA or getattr(c.func, 'id', None) in POLITICA)
                for c in ast.walk(fn))
            if lee and not pregunta:
                malos.add((archivo, nombre))
    return malos


def _fuentes():
    return [(f, (RAIZ / f).read_text(encoding='utf-8')) for f in VISTAS]


class TestTodaVistaPregunta:

    def test_ninguna_vista_lee_cifras_sin_preguntar(self):
        malos = _leen_sin_preguntar(_fuentes())
        assert malos <= set(SIN_PREGUNTAR), sorted(malos - set(SIN_PREGUNTAR))

    def test_el_inventario_solo_encoge(self):
        malos = _leen_sin_preguntar(_fuentes())
        assert set(SIN_PREGUNTAR) <= malos, sorted(set(SIN_PREGUNTAR) - malos)
        assert len(SIN_PREGUNTAR) <= 5 and all(len(m) >= 40 for m in SIN_PREGUNTAR.values())

    def test_el_dict_completo_solo_sale_por_to_dict(self):
        llamadores = set()
        for ruta in (RAIZ / 'app').rglob('*.py'):
            for nombre, fn in _funciones(ast.parse(ruta.read_text(encoding='utf-8'))):
                if any(isinstance(c, ast.Call) and getattr(c.func, 'attr', None) == '_to_dict_completo'
                       for c in ast.walk(fn)):
                    llamadores.add((ruta.relative_to(RAIZ).as_posix(), nombre))
        assert llamadores == {('app/models/conteo.py', 'SesionConteo.to_dict')}

    def test_piso(self):
        """Las dos vistas que SÍ preguntan tienen que verse como lectoras."""
        todos = set()
        for archivo, src in _fuentes():
            for nombre, fn in _funciones(ast.parse(src)):
                if any(isinstance(a, ast.Attribute) and a.attr in CIFRAS for a in ast.walk(fn)):
                    todos.add((archivo, nombre))
        assert ('app/routes/conteo.py', 'exportar_conteos') in todos
        assert len(todos) >= 6


class TestElDetectorMuerde:

    def test_ve_una_vista_que_no_pregunta(self):
        src = 'def ver(s):\n    return {"x": s.teorico_siesa}\n'
        assert _leen_sin_preguntar([('app/v.py', src)]) == {('app/v.py', 'ver')}

    def test_no_marca_la_que_pregunta(self):
        src = ('def ver(s):\n    if ConteoService.cifras_ocultas(s):\n        return {}\n'
               '    return {"x": s.teorico_siesa}\n')
        assert _leen_sin_preguntar([('app/v.py', src)]) == set()

    def test_una_escritura_o_un_docstring_no_es_leer(self):
        src = ('def f(s):\n    """s.teorico_siesa"""\n    s.cantidad_fisica = 3\n')
        assert _leen_sin_preguntar([('app/v.py', src)]) == set()

"""
Los cuatro defectos que encontró el e2e de conteo (2026-09-29), por su CLASE:

1. **El WMS se movía con el delta de Siesa** en vez de quedar en lo contado
   (`llevar_wms_a_lo_contado`, la única que mueve el WMS por un conteo).
2. **Una respuesta de ensayo se daba por hecha**: toda rama de `_ejecutar_job`
   que usa el resultado de `_ejecutar_con_preflag` pregunta por `modo_ensayo`.
3. **CNT-04 bloqueaba la omisión autorizada** (`omision_autorizada`).
4. **El sobrante con cajas empacadas por salir salía solo**
   (`unidades_empacadas_por_salir`, una definición, dentro de `motivo_no_sale_solo`).

Los escenarios de punta a punta viven en `tests/flujo/test_e2e_conteo_escenarios.py`;
acá, lo que ese archivo no ejerce y los trinquetes (AST, con meta-tests y pisos).
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from tests.flujo.test_e2e_conteo_escenarios import (  # noqa: F401
    _pedido, _politica_por_defecto, m, siesa)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'


# ─────────────────────────────────────────────────────────────────────────────
# 1 · El WMS queda en lo contado
# ─────────────────────────────────────────────────────────────────────────────

class TestElWmsQuedaEnLoContado:

    def test_lo_movido_despues_del_conteo_se_respeta(self, m):
        """Contado 46 (Siesa 50) y, antes de que el líder apruebe, un picking
        saca 10: el WMS termina en 36 (lo contado menos lo que salió después),
        no en 46 ni en 50 − 4 − 10 dos veces."""
        from tests.flujo.conductor_de_flujo import hacer_picking
        p = m.producto(lugares={'PIK-1': 50}, costo=90000)       # sobre el tope: espera firma
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 46)
        assert m.s(sid).estado == 'DESCUADRE'
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)                       # después del conteo
        assert m.wms(p) == 40
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert m.wms(p) == 36
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_llamarla_dos_veces_no_mueve_dos_veces(self, m):
        """La recuperación de una sesión atascada vuelve a llamarla: sus
        propios movimientos no cuentan como «movido después»."""
        from app.services.conteo_service import ConteoService
        p = m.producto(lugares={'SIESA-GENERAL': 10, 'CROSS-DOCK': 12}, existencia=10)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 15)
        s = m.s(sid)
        ConteoService.llevar_wms_a_lo_contado(s)
        m.db.session.commit()
        assert m.wms(p) == 15
        r = ConteoService.llevar_wms_a_lo_contado(m.s(sid))
        m.db.session.commit()
        assert (m.wms(p), r['movimientos']) == (15, [])

    def test_sin_foto_no_mueve_nada_y_lo_dice(self, m):
        from app.services.conteo_service import ConteoService
        p = m.producto(lugares={'SIESA-GENERAL': 10})
        sid = m.manual(m.sofi, p)
        r = ConteoService.llevar_wms_a_lo_contado(m.s(sid))     # sin contar: sin foto
        assert r['sin_foto'] is True and m.wms(p) == 10


# ─────────────────────────────────────────────────────────────────────────────
# 3 · CNT-04: la omisión autorizada no bloquea; la no autorizada sí
# ─────────────────────────────────────────────────────────────────────────────

def _omitida_y_aprobada(m):
    p = m.producto(lugares={'SIESA-GENERAL': 50})
    sid = m.manual(m.sofi, p)
    m.contar(m.ana, sid, 47)
    m.confirmar(m.ana, sid, 47)
    st, r = m.post(m.sofi, f'/api/conteo/{sid}/omitir-segundo', {'motivo': 'sin segundo operario'})
    assert st == 200, r
    st, r = m.put(m.saul, f'/api/conteo/{sid}/ajustar')
    assert st == 202, r
    m.ejecutar_ajustes()
    return sid


def _cnt04():
    from app.services import auditoria
    return next(x for x in auditoria.auditar('conteo')['resultados'] if x['codigo'] == 'CNT-04')


class TestLaOmisionAutorizada:

    def test_con_motivo_y_otra_firma_no_es_hallazgo(self, m):
        _omitida_y_aprobada(m)
        assert _cnt04()['total'] == 0

    @pytest.mark.parametrize('rompe', ['sin_motivo', 'firma_quien_omitio', 'firma_quien_conto',
                                       'sin_firma'])
    def test_la_no_autorizada_sigue_bloqueando(self, m, rompe):
        sid = _omitida_y_aprobada(m)
        s = m.s(sid)
        if rompe == 'sin_motivo':
            s.verificacion_omitida_motivo = '   '
        elif rompe == 'firma_quien_omitio':
            s.aprobador_id = s.verificacion_omitida_por_id
        elif rompe == 'firma_quien_conto':
            s.aprobador_id = m.ana.id
        else:
            s.aprobador_id = None
        m.db.session.commit()
        assert _cnt04()['total'] == 1, rompe


# ─────────────────────────────────────────────────────────────────────────────
# Trinquetes
# ─────────────────────────────────────────────────────────────────────────────

def _funciones(arbol):
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _llamadas(nodo, nombre):
    return [c for c in ast.walk(nodo) if isinstance(c, ast.Call)
            and (getattr(c.func, 'attr', None) == nombre or getattr(c.func, 'id', None) == nombre)]


def quienes_llaman(fuentes: dict, nombre: str) -> set:
    """`{'archivo::funcion'}` que llaman a `nombre` (la función más interna).
    Docstrings y comentarios no son llamadas."""
    out = set()
    for ruta, texto in fuentes.items():
        arbol = ast.parse(texto)
        for fn in _funciones(arbol):
            propias = [c for c in _llamadas(fn, nombre)
                       if not any(c in list(ast.walk(h)) for h in fn.body
                                  if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)))]
            if propias:
                out.add(f'{ruta}::{fn.name}')
    return out


def _fuentes_app():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8') for p in APP.rglob('*.py')}


#: Quién puede repartir un movimiento de conteo en el WMS. Solo encoge.
LLAMAN_A_APLICAR = {'app/services/conteo_service.py::llevar_wms_a_lo_contado'}


class TestUnaFuncionMueveElWmsPorUnConteo:

    def test_solo_llevar_a_lo_contado_reparte(self):
        hallados = quienes_llaman(_fuentes_app(), 'aplicar_ajuste_al_wms')
        assert hallados == LLAMAN_A_APLICAR, hallados

    def test_el_job_y_el_match_pasan_por_ella(self):
        hallados = quienes_llaman(_fuentes_app(), 'llevar_wms_a_lo_contado')
        assert {'app/services/siesa_job_service.py::_ejecutar_job',
                'app/services/conteo_service.py::cuadrar_wms_con_lo_contado'} <= hallados

    def test_el_escaner_ve_una_llamada_nueva_y_no_un_docstring(self):
        falso = {'x.py': (
            'def a():\n    """aplicar_ajuste_al_wms(s)"""\n    # aplicar_ajuste_al_wms(s)\n    pass\n'
            'def b(s):\n    ConteoService.aplicar_ajuste_al_wms(s, "AJ-SAL", 3)\n'
            'def c(s):\n    def d():\n        aplicar_ajuste_al_wms(s)\n    return d\n')}
        assert quienes_llaman(falso, 'aplicar_ajuste_al_wms') == {'x.py::b', 'x.py::d'}

    def test_piso(self):
        assert len(_fuentes_app()) >= 200


def ramas_sin_pregunta_de_ensayo(texto: str) -> list:
    """En `_ejecutar_job`: toda rama `if job.tipo == X` que ASIGNA el resultado
    de `_ejecutar_con_preflag` (y sigue haciendo cosas con él) tiene que
    preguntar por `'modo_ensayo'`. Devolverlo directo (`return
    _ejecutar_con_preflag(...)`) no cierra nada: está bien."""
    arbol = ast.parse(texto)
    malas = []
    for fn in _funciones(arbol):
        if fn.name != '_ejecutar_job':
            continue
        for rama in ast.walk(fn):
            if not isinstance(rama, ast.If):
                continue
            asigna = [a for a in ast.walk(rama) if isinstance(a, ast.Assign)
                      and isinstance(a.value, ast.Call)
                      and getattr(a.value.func, 'id', None) == '_ejecutar_con_preflag']
            if not asigna:
                continue
            anidadas = [i for i in rama.body if isinstance(i, ast.If)
                        and any(a in list(ast.walk(i)) for a in asigna)]
            if anidadas:
                continue            # se juzga en la rama más interna que asigna
            pregunta = any(isinstance(c, ast.Constant) and c.value == 'modo_ensayo'
                           for c in ast.walk(rama))
            if not pregunta:
                malas.append(rama.lineno)
    return malas


class TestNingunaRespuestaDeEnsayoSeDaPorHecha:

    def test_el_ejecutor(self):
        texto = (APP / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        assert ramas_sin_pregunta_de_ensayo(texto) == []
        n = sum(1 for c in ast.walk(ast.parse(texto)) if isinstance(c, ast.Call)
                and getattr(c.func, 'id', None) == '_ejecutar_con_preflag')
        assert n >= 3, 'el escáner no ve las llamadas al helper'

    def test_el_escaner_ve_la_forma_rota_y_no_la_sana(self):
        roto = ('def _ejecutar_job(job):\n    if job.tipo == "X":\n'
                '        r = _ejecutar_con_preflag(o, f)\n        o.estado = "HECHO"\n')
        sano = ('def _ejecutar_job(job):\n    if job.tipo == "X":\n'
                '        r = _ejecutar_con_preflag(o, f)\n        if r.get("modo_ensayo"):\n'
                '            raise E()\n        o.estado = "HECHO"\n'
                '    if job.tipo == "Y":\n        return _ejecutar_con_preflag(o, f)\n')
        doc = ('def _ejecutar_job(job):\n    if job.tipo == "X":\n'
               '        """modo_ensayo"""\n        r = _ejecutar_con_preflag(o, f)\n')
        assert ramas_sin_pregunta_de_ensayo(roto) == [2]
        assert ramas_sin_pregunta_de_ensayo(sano) == []
        # Un docstring con la palabra sí cuenta como Constant: el trinquete lo
        # acepta a sabiendas (la guarda real es el test de conducta del e2e).
        assert ramas_sin_pregunta_de_ensayo(doc) == []


class TestUnaDefinicionDeEmpacadoPorSalir:

    def test_la_guarda_del_sobrante_la_usa_y_el_hud_tambien(self):
        texto = (APP / 'services' / 'conteo_service.py').read_text(encoding='utf-8')
        fns = {f.name: f for f in _funciones(ast.parse(texto))}
        assert _llamadas(fns['motivo_no_sale_solo'], 'unidades_empacadas_por_salir')
        assert _llamadas(fns['empacado_por_salir'], 'unidades_empacadas_por_salir')
        # Nadie más arma la consulta de «empacado»: ItemPacking.cantidad_real
        # se suma solo en su definición dentro de conteo_service.
        dentro = [f.name for f in fns.values()
                  if any(isinstance(a, ast.Attribute) and a.attr == 'cantidad_real'
                         for a in ast.walk(f))]
        assert dentro == ['unidades_empacadas_por_salir'], dentro

    def test_la_omision_autorizada_la_decide_una_funcion(self):
        texto = (APP / 'services' / 'auditoria' / 'conteo.py').read_text(encoding='utf-8')
        fns = {f.name: f for f in _funciones(ast.parse(texto))}
        assert _llamadas(fns['un_descuadre_se_cuenta_dos_veces'], 'omision_autorizada')
        assert not any(isinstance(a, ast.Attribute) and a.attr.startswith('verificacion_omitida')
                       for a in ast.walk(fns['un_descuadre_se_cuenta_dos_veces']))

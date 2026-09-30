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


# ─────────────────────────────────────────────────────────────────────────────
# VAL-E1/E2 · el libro dice cuánto cambió el WMS, y de dónde vino el cambio
# ─────────────────────────────────────────────────────────────────────────────

def _constructores_de_movimiento(fuentes: dict):
    """`[(clave 'archivo::funcion', ast.Call)]` de cada `MovimientoInventario(...)`."""
    def propios(fn):
        """Los nodos de `fn` sin entrar a sus funciones anidadas: cada
        constructor pertenece a la función más interna."""
        pila = list(ast.iter_child_nodes(fn))
        while pila:
            n = pila.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yield n
            pila.extend(ast.iter_child_nodes(n))
    out = []
    for ruta, texto in fuentes.items():
        for fn in _funciones(ast.parse(texto)):
            for c in propios(fn):
                if isinstance(c, ast.Call) and (getattr(c.func, 'id', None) == 'MovimientoInventario'
                                                or getattr(c.func, 'attr', None) == 'MovimientoInventario'):
                    out.append((f'{ruta}::{fn.name}', c))
    return out


def sin_saldo(fuentes: dict) -> set:
    """Constructores que cambian cantidad sin declarar `saldo_antes` y
    `saldo_despues` (aunque sea `None` cuando no hay fila)."""
    return {k for k, c in _constructores_de_movimiento(fuentes)
            if not {'saldo_antes', 'saldo_despues'} <= {kw.arg for kw in c.keywords}}


def tipos_literales(fuentes: dict) -> tuple:
    """(`{tipo literal}`, `{clave con tipo no literal}`) de los constructores.
    Un `a if c else b` con dos literales cuenta como literales."""
    literales, dinamicos = set(), set()
    for k, c in _constructores_de_movimiento(fuentes):
        tipo = next((kw.value for kw in c.keywords if kw.arg == 'tipo'), None)
        valores = ([tipo] if not isinstance(tipo, ast.IfExp) else [tipo.body, tipo.orelse])
        if tipo is not None and all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                                    for v in valores):
            literales |= {v.value for v in valores}
        else:
            dinamicos.add(k)
    return literales, dinamicos


def _fuentes_app_y_flota():
    fs = _fuentes_app()
    fs.update({str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8')
               for p in (RAIZ / 'flota').rglob('*.py')})
    return fs


#: Escritores sin saldo. Solo encoge. Vacío desde el 2026-09-29.
SIN_SALDO_DECLARADOS: dict = {}

#: Escritores cuyo `tipo` no es literal, con los valores que puede tomar
#: (verificados a mano en el código). Solo encoge.
TIPO_DINAMICO = {
    'app/routes/inventario.py::ajuste_inventario': {'ENTRADA', 'SALIDA', 'AJUSTE'},
    'app/services/conteo_service.py::_mov': {'AJUSTE_CONTEO', 'CUADRE_CONTEO'},
    'app/services/devolucion_cliente_service.py::_entrar': {
        'DEVOLUCION_CLIENTE', 'DEVOLUCION_CLIENTE_AVERIADO'},
}


class TestElLibroDiceCuantoCambio:

    def test_todo_escritor_declara_su_saldo(self):
        hallados = sin_saldo(_fuentes_app_y_flota())
        assert hallados == set(SIN_SALDO_DECLARADOS), hallados

    def test_piso(self):
        assert len(_constructores_de_movimiento(_fuentes_app())) >= 20

    def test_el_escaner_ve_el_que_no_declara_y_no_el_sano(self):
        falso = {'x.py': (
            'def a():\n    """MovimientoInventario(tipo=1)"""\n'
            '    db.session.add(MovimientoInventario(tipo="SALIDA", cantidad=-3))\n'
            'def b():\n    db.session.add(M.MovimientoInventario(tipo="ENTRADA", cantidad=3,\n'
            '        saldo_antes=1, saldo_despues=4))\n'
            'def c():\n    def d():\n        MovimientoInventario(tipo="SALIDA")\n    return d\n')}
        assert sin_saldo(falso) == {'x.py::a', 'x.py::d'}


class TestTodoTipoTieneOrigenDeclarado:

    def _tabla(self):
        from app.services.conteo_service import ConteoService
        return ConteoService.ORIGEN_DE_MOVIMIENTO

    def test_los_literales_estan_en_la_tabla(self, app):
        literales, dinamicos = tipos_literales(_fuentes_app_y_flota())
        assert literales - set(self._tabla()) == set(), literales - set(self._tabla())
        assert dinamicos == set(TIPO_DINAMICO), dinamicos
        for clave, valores in TIPO_DINAMICO.items():
            assert valores <= set(self._tabla()), clave

    def test_los_tipos_que_viajan_por_parametro_estan_en_la_tabla(self, app):
        """`_entrar(..., tipo='X')`, `aplicar_ajuste_al_wms(..., tipo='X')`,
        `llevar_wms_a_lo_contado(..., tipo='X')`: el literal de la llamada."""
        vistos = set()
        for texto in _fuentes_app().values():
            for c in ast.walk(ast.parse(texto)):
                if isinstance(c, ast.Call) and getattr(c.func, 'attr', None) in (
                        '_entrar', 'aplicar_ajuste_al_wms', 'llevar_wms_a_lo_contado'):
                    for kw in c.keywords:
                        if kw.arg == 'tipo' and isinstance(kw.value, ast.Constant):
                            vistos.add(kw.value.value)
        assert vistos and vistos <= set(self._tabla()), vistos

    def test_la_sincronizacion_no_es_fisica(self, app):
        t = self._tabla()
        assert t['CARGA_INICIAL_SIESA'] == 'SINCRONIZACION'
        assert {t['AJUSTE_CONTEO'], t['CUADRE_CONTEO']} == {'CONTEO'}
        assert set(t.values()) == {'FISICO', 'SINCRONIZACION', 'CONTEO', 'MANUAL'}

    def test_el_escaner_ve_un_tipo_nuevo(self):
        falso = {'x.py': ('def a():\n    MovimientoInventario(tipo="NUEVO_TIPO", saldo_antes=0, saldo_despues=1)\n'
                          'def b(t):\n    MovimientoInventario(tipo=t, saldo_antes=0, saldo_despues=1)\n'
                          'def c(x):\n    MovimientoInventario(tipo="A" if x else "B", saldo_antes=0, saldo_despues=1)\n')}
        literales, dinamicos = tipos_literales(falso)
        assert literales == {'NUEVO_TIPO', 'A', 'B'} and dinamicos == {'x.py::b'}

    def test_movido_despues_suma_solo_fisicos_con_saldo(self):
        texto = (APP / 'services' / 'conteo_service.py').read_text(encoding='utf-8')
        fn = next(f for f in _funciones(ast.parse(texto)) if f.name == 'movido_despues_del_conteo')
        attrs = {a.attr for a in ast.walk(fn) if isinstance(a, ast.Attribute)}
        assert {'saldo_antes', 'saldo_despues', 'ORIGEN_DE_MOVIMIENTO'} <= attrs
        assert 'cantidad' not in attrs, 'una cantidad sin saldo no dice cuánto cambió el WMS'


class TestLaSincronizacionNoEsMovimientoFisico:

    def test_la_carga_de_siesa_despues_del_conteo_no_cuenta(self, m):
        """Entre el conteo y la aprobación corre la carga de las 7:00 y deja el
        WMS en otro número: no es mercancía que se movió en la bodega."""
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        p = m.producto(lugares={'PIK-1': 50}, costo=90000)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 46)
        fila = UbicacionProducto.query.filter_by(producto_id=p.id).one()
        m.db.session.add(MovimientoInventario(
            producto_id=p.id, ubicacion_id=fila.ubicacion_id, almacen_id=m.nb1.id,
            tipo='CARGA_INICIAL_SIESA', cantidad=0, saldo_antes=50, saldo_despues=58,
            motivo='carga de Siesa', numero_documento='CARGA-SIESA'))
        fila.cantidad = 58
        m.db.session.commit()
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert m.wms(p) == 46


class TestLasOperacionesRealesDespuesDelConteo:
    """Las mismas formas que la validación armó a mano, pero con el servicio
    que las escribe: si vuelve a dejar una pata sin saldo, esto lo ve."""

    def test_reposicion_real_no_suma_unidades(self, m):
        from app.models.inventario import MovimientoInventario
        from app.models.lpn import LPN
        from app.models.tarea_reposicion import TareaReposicion
        from app.services.reposicion_service import confirmar_reposicion
        p = m.producto(lugares={'PIK-1': 20, 'RES-1': 30}, costo=90000)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 45)                        # espera firma
        pik, res = m.ub(m.nb1, 'PIK-1'), m.ub(m.nb1, 'RES-1')
        lpn = LPN(codigo='LPN-E2E-1', producto_id=p.id, factor_conversion=10,
                  cantidad_actual=10, estado='ACTIVO', almacen_id=m.nb1.id, ubicacion_id=res.id)
        m.db.session.add(lpn)
        m.db.session.flush()
        t = TareaReposicion(codigo='REP-E2E-1', producto_id=p.id, almacen_id=m.nb1.id,
                            cantidad_unidades=10, ubicacion_reserva_id=res.id,
                            ubicacion_picking_id=pik.id, lpn_id=lpn.id, estado='EN_PROCESO',
                            abastecedor_id=m.caro.id)
        m.db.session.add(t)
        m.db.session.commit()
        confirmar_reposicion(t.id, m.caro.id)
        movs = MovimientoInventario.query.filter_by(producto_id=p.id, tipo='REPOSICION').all()
        assert sorted(x.saldo_despues - x.saldo_antes for x in movs) == [-10, 10]
        assert m.wms(p) == 50
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert m.wms(p) == 45, m.por_lugar(p)

    def test_el_faltante_no_sale_de_la_zona_de_averias(self, m):
        """PIK-1 (el lugar de la cadena) 10 + RES-1 5 vendibles y 50 averiadas:
        contadas 3, se quitan 12 del vendible, nunca de averías."""
        m.ub(m.nb1, 'AVE-1', 'AVERIAS')
        p = m.producto(lugares={'PIK-1': 10, 'RES-1': 5, 'AVE-1': 50}, existencia=15, costo=100)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 3)
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st in (200, 202), r
        m.ejecutar_ajustes()
        assert m.por_lugar(p) == {'PIK-1': 0, 'RES-1': 3, 'AVE-1': 50}, m.por_lugar(p)


class TestElHudNoNombraLoQueElTotalNoCuenta:

    def test_ni_averias_ni_devoluciones_y_el_perimetro_lo_dice(self, m):
        m.ub(m.nb1, 'AVE-1', 'AVERIAS')
        m.ub(m.nb1, 'DEV-1', 'DEVOLUCION')
        p = m.producto(lugares={'PIK-1': 10, 'AVE-1': 4, 'DEV-1': 2}, existencia=10)
        sid = m.manual(m.sofi, p)
        t = m.abrir(m.ana, sid)
        assert [l['codigo'] for l in t['lugares']] == ['PIK-1'], t['lugares']
        assert 'NO cuente lo averiado' in t['perimetro']['no_cuente']
        r = m.contar(m.beto, m.manual(m.sofi, m.producto(lugares={'PIK-2': 1})), 1)
        assert r['resultado'] == 'MATCH'

    def test_todo_perimetro_dice_lo_averiado(self):
        from app.services.conteo_politica import PERIMETRO_DE_CONTEO
        assert all('averiado' in v['no_cuente'] for v in PERIMETRO_DE_CONTEO.values())

    def test_los_lugares_y_el_total_usan_la_misma_politica(self):
        texto = (APP / 'services' / 'conteo_service.py').read_text(encoding='utf-8')
        fns = {f.name: f for f in _funciones(ast.parse(texto))}
        for nombre in ('lugares_del_sku', 'existencia_wms_del_sku', 'aplicar_ajuste_al_wms'):
            assert _llamadas(fns[nombre], 'filtro_ubicacion_vendible'), nombre


# ─────────────────────────────────────────────────────────────────────────────
# VAL-E4 · ningún cambio de stock sin su movimiento (la clase)
# ─────────────────────────────────────────────────────────────────────────────

def _escribe_cantidad(n) -> bool:
    """Las tres escrituras de `.cantidad`: asignación (`=`, `+=`, `-=`),
    `.update({'cantidad': …})` y `setattr(x, 'cantidad', …)`."""
    if isinstance(n, (ast.Assign, ast.AugAssign)):
        ts = n.targets if isinstance(n, ast.Assign) else [n.target]
        return any(isinstance(x, ast.Attribute) and x.attr == 'cantidad' for x in ts)
    if isinstance(n, ast.Call) and getattr(n.func, 'attr', None) == 'update' and n.args \
            and isinstance(n.args[0], ast.Dict):
        return any(isinstance(k, ast.Constant) and k.value == 'cantidad' for k in n.args[0].keys)
    if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'setattr' and len(n.args) > 1:
        return isinstance(n.args[1], ast.Constant) and n.args[1].value == 'cantidad'
    return False


def cambios_de_stock_sin_movimiento(fuentes: dict) -> set:
    """`{'archivo::funcion'}` que cambian `.cantidad` en un módulo que maneja
    `UbicacionProducto` sin construir un `MovimientoInventario` en la misma
    función (una función anidada suya cuenta: es el mismo cambio)."""
    out = set()
    for ruta, texto in fuentes.items():
        if 'UbicacionProducto' not in texto:
            continue
        for fn in _funciones(ast.parse(texto)):
            pila, propios = list(ast.iter_child_nodes(fn)), []
            while pila:
                n = pila.pop()
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                propios.append(n)
                pila.extend(ast.iter_child_nodes(n))
            if not any(_escribe_cantidad(n) for n in propios):
                continue
            mov = any(isinstance(c, ast.Call) and (getattr(c.func, 'id', None) == 'MovimientoInventario'
                                                   or getattr(c.func, 'attr', None) == 'MovimientoInventario')
                      for c in ast.walk(fn))
            if not mov:
                out.add(f'{ruta}::{fn.name}')
    return out


#: Funciones que cambian stock sin su movimiento, con su porqué. Solo encoge.
#: Vacío desde el 2026-09-29 (VAL-E4 cerró los traspasos de Layout y la pata
#: del vendible del dictamen de averías).
CAMBIOS_SIN_MOVIMIENTO: dict = {}


class TestNingunCambioDeStockSinSuMovimiento:

    def test_la_clase(self):
        hallados = cambios_de_stock_sin_movimiento(_fuentes_app_y_flota())
        assert hallados == set(CAMBIOS_SIN_MOVIMIENTO), hallados

    def test_el_escaner_ve_las_tres_escrituras_y_no_lo_sano(self):
        falso = {'x.py': (
            'from m import UbicacionProducto\n'
            'def a(reg):\n    """reg.cantidad -= 1"""\n    # reg.cantidad = 0\n    reg.cantidad -= 1\n'
            'def b(q):\n    q.update({"cantidad": 0})\n'
            'def c(r):\n    setattr(r, "cantidad", 3)\n'
            'def d(reg):\n    reg.cantidad = 5\n    db.session.add(MovimientoInventario(tipo="X"))\n'
            'def e(reg):\n    def _m():\n        MovimientoInventario(tipo="X")\n'
            '    reg.cantidad = 1\n    _m()\n'
            'def f(reg):\n    x = reg.cantidad\n'),
            'otro.py': 'def g(item):\n    item.cantidad = 1\n'}
        assert cambios_de_stock_sin_movimiento(falso) == {'x.py::a', 'x.py::b', 'x.py::c'}

    def test_piso(self):
        fs = _fuentes_app_y_flota()
        n = 0
        for ruta, texto in fs.items():
            if 'UbicacionProducto' in texto:
                n += sum(1 for fn in _funciones(ast.parse(texto))
                         if any(_escribe_cantidad(x) for x in ast.walk(fn)))
        assert n >= 18, n

    def test_el_traspaso_de_layout_deja_sus_dos_patas(self, m):
        """Asignar a un hueco real saca de SIESA-GENERAL: dos patas con saldo,
        y el total del SKU no cambia en el libro."""
        from app.models.inventario import MovimientoInventario
        from app.services import layout_service
        assert m.nb1.tiene_fusion_layout_activa          # NB1 / 003
        pik = m.ub(m.nb1, 'PIK-9', 'PICKING')
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        layout_service.asignar_producto(pik.id, p.id, 20, usuario_id=m.adri.id, capacidad_maxima=100)
        movs = MovimientoInventario.query.filter_by(producto_id=p.id, tipo='ASIGNACION_LAYOUT').all()
        assert sorted(x.saldo_despues - x.saldo_antes for x in movs) == [-20, 20]
        assert m.wms(p) == 50

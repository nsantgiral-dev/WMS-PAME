"""
Analítica: solo lo actual (2026-09-24).

La Analítica, la salud del dato, la auditoría de invariantes y el tablero
mostraban como «errores» cosas que no son de hoy: jobs FALLIDO del ensayo ya
superados, pasos de un flujo anteriores a la corrección de su defecto, paradas
juzgadas con una regla que no existía cuando se confirmaron, limpieza técnica
del layout contada como cancelación de negocio.

La clase: **un lector que no distingue lo vigente de lo histórico**. Cinco
piezas, cada una una política y una función:

· `corte.py` — `FECHA_INICIO_AUDITORIA`; lo anterior se cuenta aparte.
· `siesa_job_service.fallidos_vigentes` — la única que cuenta FALLIDO.
· `@invariante(defecto_corregido=...)` — la huella de un defecto arreglado.
· `cond_pago.anterior_a_la_regla` — la regla de contado no es retroactiva.
· `ENTIDADES_DE_NEGOCIO` — una limpieza técnica no es una cancelación.

Los trinquetes son por AST, con inventario que solo encoge, meta-tests y pisos.
"""
import ast
import json
import pathlib
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
AUDITORIA = RAIZ / 'app' / 'services' / 'auditoria'
PWA = RAIZ / 'app' / 'static' / 'pwa'


@pytest.fixture
def con_corte(monkeypatch):
    """Pone `FECHA_INICIO_AUDITORIA`. Devuelve la función que la fija."""
    def _poner(valor):
        monkeypatch.setenv('FECHA_INICIO_AUDITORIA', valor)
    monkeypatch.delenv('FECHA_INICIO_AUDITORIA', raising=False)
    return _poner


@pytest.fixture
def sin_corte(monkeypatch):
    monkeypatch.delenv('FECHA_INICIO_AUDITORIA', raising=False)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La variable — una función
# ═════════════════════════════════════════════════════════════════════════════

class TestLaVariable:

    def test_sin_variable_no_hay_corte_y_se_declara(self, sin_corte):
        from app.services import corte
        assert corte.inicio_auditoria() is None
        e = corte.estado()
        assert e['configurado'] is False and e['valido'] is False
        assert 'Sin FECHA_INICIO_AUDITORIA' in e['texto']

    def test_la_fecha_sola_es_medianoche_de_bogota(self, con_corte):
        from app.services import corte
        con_corte('2026-10-01')
        assert corte.inicio_auditoria() == datetime(2026, 10, 1, 5, 0)
        assert corte.dia_de_corte() == date(2026, 10, 1)

    def test_con_hora_es_hora_de_bogota(self, con_corte):
        from app.services import corte
        con_corte('2026-10-01T06:30')
        assert corte.inicio_auditoria() == datetime(2026, 10, 1, 11, 30)

    def test_con_zona_explicita_se_respeta(self, con_corte):
        from app.services import corte
        con_corte('2026-10-01T02:00Z')
        assert corte.inicio_auditoria() == datetime(2026, 10, 1, 2, 0)
        # 02:00 UTC es el 30 de septiembre en Bogotá.
        assert corte.dia_de_corte() == date(2026, 9, 30)

    @pytest.mark.parametrize('basura', ['ayer', '2026-13-40', '01/10/2026', '2026-10-01T99:00'])
    def test_invalida_no_corta_nada_y_se_declara(self, con_corte, basura):
        from app.services import corte
        con_corte(basura)
        assert corte.inicio_auditoria() is None
        e = corte.estado()
        assert e['configurado'] is True and e['valido'] is False
        assert 'inválida' in e['texto']

    def test_es_anterior(self, con_corte):
        from app.services import corte
        con_corte('2026-10-01')
        assert corte.es_anterior(datetime(2026, 10, 1, 4, 59)) is True
        assert corte.es_anterior(datetime(2026, 10, 1, 5, 0)) is False
        assert corte.es_anterior(date(2026, 9, 30)) is True
        assert corte.es_anterior(date(2026, 10, 1)) is False
        # Sin fecha no se sabe que sea vieja: cuenta como vigente (Regla 0).
        assert corte.es_anterior(None) is False

    def test_sin_corte_nada_es_anterior(self, sin_corte):
        from app.services import corte
        assert corte.es_anterior(datetime(2001, 1, 1)) is False

    def test_recortar_rango(self, con_corte):
        from app.services import corte
        con_corte('2026-10-10')
        d, h, info = corte.recortar_rango(date(2026, 10, 1), date(2026, 10, 20))
        assert (d, h) == (date(2026, 10, 10), date(2026, 10, 20))
        assert info['aplicado'] and info['antes'] == (date(2026, 10, 1), date(2026, 10, 9))
        assert info['dias_antes_del_corte'] == 9
        d, h, info = corte.recortar_rango(date(2026, 10, 11), date(2026, 10, 20))
        assert d == date(2026, 10, 11) and not info['aplicado']
        d, h, info = corte.recortar_rango(date(2026, 9, 1), date(2026, 9, 5))
        assert d > h, 'todo el rango es anterior: el lector devuelve vacío'
        assert info['antes'] == (date(2026, 9, 1), date(2026, 9, 5))


# ═════════════════════════════════════════════════════════════════════════════
# 1b · La auditoría cuenta aparte lo anterior al corte
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_actual@test.com'),
                       ('conductor', 'cond_actual@test.com')):
        u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
        u.set_password('test123')
        db.session.add(u)
        db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


@pytest.fixture
def flujo(db, almacen, actores):
    from tests.flujo import conductor_de_flujo as cf
    return cf.flujo_completo(db, almacen, actores['operario'].id, actores['conductor'].id)


def _res(rep, codigo):
    return next(x for x in rep['resultados'] if x['codigo'] == codigo)


class TestLaAuditoriaCuentaAparteLoViejo:

    def _pickeo_de_mas(self, db, flujo, nacio):
        from app.models.picking import TareaPicking
        t = db.session.get(TareaPicking, flujo.pickings[0])
        t.cantidad_recogida = t.cantidad_solicitada + 5
        t.fecha_creacion = nacio
        db.session.commit()

    def test_sin_corte_el_hallazgo_viejo_bloquea(self, db, flujo, sin_corte):
        from app.services import auditoria
        self._pickeo_de_mas(db, flujo, datetime(2026, 4, 2, 15, 0))
        rep = auditoria.auditar('venta')
        assert _res(rep, 'VTA-10')['total'] == 1
        assert rep['bloqueantes'] >= 1
        assert rep['corte']['configurado'] is False

    def test_con_corte_se_cuenta_aparte_y_no_bloquea(self, db, flujo, con_corte):
        from app.services import auditoria
        self._pickeo_de_mas(db, flujo, datetime(2026, 4, 2, 15, 0))
        con_corte('2026-06-01')
        rep = auditoria.auditar('venta')
        r = _res(rep, 'VTA-10')
        assert r['total'] == 0 and r['antes_del_corte'] == 1
        assert rep['antes_del_corte'] >= 1
        assert not any(x['codigo'] == 'VTA-10' for x in rep['resultados']
                       if x['total'] and x['severidad'] == 'BLOQUEA')

    def test_lo_posterior_al_corte_sigue_bloqueando(self, db, flujo, con_corte):
        from app.services import auditoria
        self._pickeo_de_mas(db, flujo, datetime.utcnow())
        con_corte('2026-06-01')
        assert _res(auditoria.auditar('venta'), 'VTA-10')['total'] == 1

    def test_la_salud_no_sube_el_nivel_por_lo_viejo(self, db, flujo, con_corte):
        from app.services import analitica_salud as sa
        self._pickeo_de_mas(db, flujo, datetime(2026, 4, 2, 15, 0))
        con_corte('2026-06-01')
        res = sa.resumen_auditoria(forzar=True)
        venta = next(x for x in res['por_flujo'] if x['flujo'] == 'venta')
        assert venta['antes_del_corte'] >= 1
        assert not any(p['codigo'] == 'VTA-10' for p in venta['peores'])


class TestLosDefectosCorregidosSonArtefacto:
    """`defecto_corregido=(commit, fecha)`: lo anterior a la corrección se
    reporta como «artefacto de <commit>: N casos» y no sube el nivel."""

    #: Los invariantes que vigilan un defecto ya corregido. **Exacto**: uno
    #: nuevo con `defecto_corregido` pide entrar acá con su porqué.
    DEFECTOS = {
        'VTA-50': '359eac0f',   # entregar_ruta marcaba ENTREGADO lo ausente
        'VTA-61': '75c168c8',   # el fallback de condición emitía contado
        'VTA-62': 'd5010187',   # la regla ≤ 15 días nació con m043contado
        'CNT-07': '02d4a80f',   # la aprobación sobre base del WMS se niega
        'TRA-01': 'd5e78970',   # el CHECK de m013 impide la cadena que crece
    }

    def test_el_inventario_es_exacto(self):
        from app.services.auditoria.base import registrados
        declarados = {i.codigo: i.defecto_corregido[0] for i in registrados()
                      if i.defecto_corregido}
        assert declarados == self.DEFECTOS

    def test_toda_fecha_se_lee(self):
        from app.services.auditoria.base import registrados
        for i in registrados():
            if i.defecto_corregido:
                assert isinstance(i.vigente_desde_utc, datetime), i.codigo

    def test_un_defecto_corregido_no_puede_quedar_sin_fecha_en_sus_hallazgos(self):
        """Si el invariante dice ser de un defecto corregido, TODOS sus
        hallazgos llevan fecha: sin ella no se puede decir si es huella."""
        from app.services.auditoria.base import registrados
        sin = _hallazgos_sin_fecha()
        for i in registrados():
            if i.defecto_corregido:
                assert i.fn.__name__ not in {f for (_, f) in sin}, i.codigo

    def test_clasificar(self):
        from app.services.auditoria.base import BLOQUEA, Hallazgo, Invariante, _clasificar
        inv = Invariante('XX-01', 'venta', 'f', 'c', BLOQUEA, fn=lambda ctx: [],
                         defecto_corregido=('abc1234', '2026-08-01'))
        hs = [Hallazgo('a', 'd', fecha=datetime(2026, 7, 1)),
              Hallazgo('b', 'd', fecha=datetime(2026, 9, 1)),
              Hallazgo('c', 'd', fecha=None),
              Hallazgo('d', 'd', fecha=date(2026, 7, 31))]
        vig, antes, art = _clasificar(inv, hs, None)
        assert [h.referencia for h in vig] == ['b', 'c'] and antes == 0 and art == 2
        vig, antes, art = _clasificar(inv, hs, datetime(2026, 7, 15))
        assert antes == 1 and art == 1 and [h.referencia for h in vig] == ['b', 'c']

    def test_vta50_antes_de_la_correccion_es_artefacto(self, db, flujo, sin_corte):
        from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
        from app.services import auditoria
        r_ = db.session.get(RecaudoEntrega, flujo.recaudo_id)
        r_.estado_entrega = EstadoEntrega.RECHAZADO
        r_.fecha_confirmacion = datetime(2026, 9, 20, 15, 0)
        db.session.commit()
        r = _res(auditoria.auditar('venta'), 'VTA-50')
        assert r['total'] == 0
        assert r['artefacto']['casos'] == 1 and '359eac0f' in r['artefacto']['texto']

    def test_vta50_despues_de_la_correccion_bloquea(self, db, flujo, sin_corte):
        from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
        from app.services import auditoria
        r_ = db.session.get(RecaudoEntrega, flujo.recaudo_id)
        r_.estado_entrega = EstadoEntrega.RECHAZADO
        r_.fecha_confirmacion = datetime.utcnow()
        db.session.commit()
        r = _res(auditoria.auditar('venta'), 'VTA-50')
        assert r['total'] == 1 and r['artefacto']['casos'] == 0

    def test_la_salud_publica_los_artefactos(self, db, flujo, sin_corte):
        from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
        from app.services import analitica_salud as sa
        r_ = db.session.get(RecaudoEntrega, flujo.recaudo_id)
        r_.estado_entrega = EstadoEntrega.RECHAZADO
        r_.fecha_confirmacion = datetime(2026, 9, 20, 15, 0)
        db.session.commit()
        res = sa.resumen_auditoria(forzar=True)
        assert any(a['codigo'] == 'VTA-50' for a in res['artefactos'])
        venta = next(x for x in res['por_flujo'] if x['flujo'] == 'venta')
        assert not any(p['codigo'] == 'VTA-50' for p in venta['peores'])


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete — todo Hallazgo declara la fecha de su entidad
# ═════════════════════════════════════════════════════════════════════════════

#: Invariantes de ESTADO ACTUAL: su fila es de hoy por construcción, no tiene
#: una fecha de nacimiento que decida si es vieja. Solo encoge.
SIN_FECHA = {
    'toda_linea_del_pedido_tiene_producto_local':
        'VTA-01: `pedidos_siesa` es el espejo de lo pendiente HOY en Siesa',
    'el_nombre_del_pedido_coincide_con_el_del_catalogo':
        'VTA-02: compara el pendiente de hoy contra el catálogo de hoy',
    'la_ventana_mirada_se_declara':
        'INV-09: declara la cobertura de la corrida actual, no un hecho fechado',
    # DEV-04/05 salieron en la integración del 2026-09-24: el agente de
    # devoluciones los rehízo y ahí recibieron su fecha, igual que DEV-06..13.
}
TOPE_SIN_FECHA = 3


def _funcion_de(tree):
    """Nodo → nombre de la función de nivel superior que lo contiene."""
    dueno = {}
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(top):
                dueno[n] = top.name
    return dueno


def hallazgos_en(src: str):
    """`[(funcion, lineno, tiene_fecha)]` de cada `Hallazgo(...)` del fuente."""
    tree = ast.parse(src)
    dueno = _funcion_de(tree)
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'Hallazgo'):
            out.append((dueno.get(n, '<modulo>'), n.lineno,
                        any(k.arg == 'fecha' for k in n.keywords)))
    return out


def _todos_los_hallazgos():
    out = []
    for f in sorted(AUDITORIA.glob('*.py')):
        if f.name in ('base.py', '__init__.py'):
            continue
        for fn, ln, tiene in hallazgos_en(f.read_text(encoding='utf-8')):
            out.append((f.name, fn, ln, tiene))
    return out


def _hallazgos_sin_fecha():
    return {(f, fn) for f, fn, _, tiene in _todos_los_hallazgos() if not tiene}


class TestTodoHallazgoDeclaraSuFecha:

    def test_ningun_hallazgo_sin_fecha_fuera_del_inventario(self):
        fuera = sorted({(f, fn) for f, fn in _hallazgos_sin_fecha() if fn not in SIN_FECHA})
        assert not fuera, (
            f'Hallazgo(...) sin `fecha=`: {fuera}. Sin la fecha de la entidad, el '
            'corte (FECHA_INICIO_AUDITORIA) no la puede separar y lo del ensayo '
            'sigue bloqueando. Pasá la columna de nacimiento de la entidad.')

    def test_el_inventario_solo_encoge(self):
        assert len(SIN_FECHA) <= TOPE_SIN_FECHA

    def test_cada_excepcion_dice_por_que(self):
        for fn, motivo in SIN_FECHA.items():
            assert len(motivo) > 20, fn

    def test_una_excepcion_que_ya_tiene_fecha_se_borra(self):
        vivos = {fn for _, fn in _hallazgos_sin_fecha()}
        viejas = sorted(set(SIN_FECHA) - vivos)
        assert not viejas, f'ya pasan la fecha, sacarlas del inventario: {viejas}'

    def test_piso(self):
        assert len(_todos_los_hallazgos()) >= 45

    # ── meta-tests: el detector muerde y no marca lo sano ─────────────────

    def test_meta_ve_un_hallazgo_sin_fecha(self):
        src = 'def inv():\n    return [Hallazgo(referencia="x", detalle="y")]\n'
        assert hallazgos_en(src) == [('inv', 2, False)]

    def test_meta_no_marca_uno_con_fecha(self):
        src = 'def inv():\n    return [Hallazgo(referencia="x", detalle="y", fecha=t.f)]\n'
        assert hallazgos_en(src) == [('inv', 2, True)]

    def test_meta_la_funcion_anidada_cuenta_para_la_de_arriba(self):
        src = ('def inv():\n    def _h(x):\n        return Hallazgo("a", "b")\n'
               '    return [_h(1)]\n')
        assert hallazgos_en(src) == [('inv', 3, False)]

    def test_meta_un_docstring_no_es_un_hallazgo(self):
        src = 'def inv():\n    """Hallazgo(referencia=1) sin fecha"""\n    # Hallazgo(x)\n    return []\n'
        assert hallazgos_en(src) == []


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete — la variable la lee un solo módulo, y nadie corta con un literal
# ═════════════════════════════════════════════════════════════════════════════

def _archivos_py():
    for base in ('app', 'flota'):
        yield from sorted((RAIZ / base).rglob('*.py'))


def lee_la_variable(src: str) -> bool:
    return any(isinstance(n, ast.Constant) and n.value == 'FECHA_INICIO_AUDITORIA'
               for n in ast.walk(ast.parse(src)))


def fechas_literales(src: str):
    """`datetime(2026, …)` / `date(2026, …)` con argumentos constantes: una
    fecha de corte escrita a mano."""
    out = []
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
                and (getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)) in ('datetime', 'date')
                and n.args and all(isinstance(a, ast.Constant) for a in n.args)):
            out.append(n.lineno)
    return out


class TestUnaSolaFuenteDelCorte:

    def test_solo_corte_py_nombra_la_variable(self):
        """La variable se nombra en `corte.py` y en nada más de `app/`/`flota/`
        (el docstring de un lector puede citarla: el detector mira constantes
        de CÓDIGO que no son docstrings)."""
        culpables = []
        for f in _archivos_py():
            if f.name == 'corte.py':
                continue
            tree = ast.parse(f.read_text(encoding='utf-8'))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(getattr(n.body[0], 'value', None), ast.Constant)}
            for n in ast.walk(tree):
                if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and n.value == 'FECHA_INICIO_AUDITORIA' and id(n) not in docs):
                    culpables.append(str(f.relative_to(RAIZ)))
        assert not culpables, culpables

    def test_ningun_invariante_corta_con_una_fecha_literal(self):
        culpables = []
        for f in sorted(AUDITORIA.glob('*.py')):
            for ln in fechas_literales(f.read_text(encoding='utf-8')):
                culpables.append(f'{f.name}:{ln}')
        assert not culpables, (
            f'{culpables}: una fecha escrita en un invariante es un corte que no '
            'pasa por `corte.py`. Si es la corrección de un defecto, va en '
            '`defecto_corregido` del decorador.')

    def test_meta_el_detector_ve_la_variable_y_la_fecha(self):
        assert lee_la_variable("import os\nos.environ.get('FECHA_INICIO_AUDITORIA')\n")
        assert not lee_la_variable("x = 'OTRA_VARIABLE'\n")
        assert fechas_literales('x = datetime(2026, 1, 1)\n') == [1]
        assert fechas_literales('x = date(2026, 1, 1)\n') == [1]
        assert fechas_literales('x = datetime(a, 1, 1)\n') == []
        assert fechas_literales('x = datetime.utcnow()\n') == []


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete — cada lector declarado pasa por el corte
# ═════════════════════════════════════════════════════════════════════════════

#: `archivo::función` → lo que tiene que llamar para aplicar el corte (alguna
#: de las del conjunto). Declarado a mano A PROPÓSITO: es la lista de los
#: lectores que muestran «errores»; uno nuevo entra con su llamada.
LECTORES_CON_CORTE = {
    'app/services/auditoria/base.py::auditar': {'inicio_auditoria'},
    'app/services/auditoria/base.py::_clasificar': {'es_anterior'},
    'app/services/rezago_liquidacion.py::separar_por_corte': {'separar'},
    'app/services/rezago_liquidacion.py::rutas_entregadas_sin_liquidar': {'separar_por_corte'},
    'app/services/rezago_liquidacion.py::diagnostico': {'separar_por_corte'},
    'app/services/rezago_liquidacion.py::rutas_sin_liquidar_al_cierre': {'es_anterior'},
    'app/services/dashboard_service.py::_cola': {'inicio_auditoria'},
    'app/services/dashboard_service.py::DashboardService.kpis_operativos': {'_cola'},
    'app/services/dashboard_service.py::DashboardService.kpis_traslados_rutas': {'_cola'},
    'app/services/analitica_fugas.py::_Rangos.__init__': {'recortar_rango'},
    'app/services/analitica_fugas.py::calcular_fugas': {'_Rangos'},
    'app/services/analitica_fugas.py::detalle_fuga': {'_Rangos'},
    'app/services/analitica_fugas.py::_limbo_de_traslados': {'separar'},
    'app/services/analitica_fugas.py::_documentos_trabados': {'inicio_auditoria'},
    'app/services/analitica_recorrido.py::cohorte': {'es_anterior'},
    'app/services/analitica_salud.py::cola_siesa': {'inicio_auditoria'},
    'app/services/analitica_salud.py::cobertura_claves': {'inicio_auditoria'},
    'app/services/analitica_salud.py::patrones_bitacora': {'recortar_rango'},
    'app/services/analitica_kpi.py::dia_de_corte_de': {'dia_de_corte'},
    'app/services/analitica_kpi.py::serie': {'dia_de_corte_de'},
    'app/services/alertas_service.py::enviar_resumen_diario': {'inicio_auditoria'},
}


def llamadas_de(src: str, calificado: str) -> set:
    """Nombres llamados (por nombre o atributo) dentro de `Clase.metodo` o
    `funcion` del fuente."""
    tree = ast.parse(src)
    partes = calificado.split('.')
    nodo = tree
    for p in partes:
        nodo = next((n for n in nodo.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == p), None)
        if nodo is None:
            return None
    out = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, 'attr', None))
    return out


class TestCadaLectorPasaPorElCorte:

    @pytest.mark.parametrize('lector', sorted(LECTORES_CON_CORTE))
    def test_llama_al_corte(self, lector):
        archivo, fn = lector.split('::')
        llamadas = llamadas_de((RAIZ / archivo).read_text(encoding='utf-8'), fn)
        assert llamadas is not None, f'{lector} ya no existe: actualizar el inventario'
        assert llamadas & LECTORES_CON_CORTE[lector], (
            f'{lector} no llama a {sorted(LECTORES_CON_CORTE[lector])}: muestra lo '
            'del ensayo como si fuera de hoy.')

    def test_piso(self):
        assert len(LECTORES_CON_CORTE) >= 20

    def test_meta_detecta_la_llamada_y_su_ausencia(self):
        src = ('class A:\n    def m(self):\n        return corte.inicio_auditoria()\n'
               'def f():\n    return 1\n')
        assert 'inicio_auditoria' in llamadas_de(src, 'A.m')
        assert 'inicio_auditoria' not in llamadas_de(src, 'f')
        assert llamadas_de(src, 'no_existe') is None


# ═════════════════════════════════════════════════════════════════════════════
# 2 · fallidos_vigentes — la única que cuenta FALLIDO
# ═════════════════════════════════════════════════════════════════════════════

def _job(db, tipo, estado, dias=0, ref_tipo='TareaPacking', ref_id=None, payload=None):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar(tipo, payload or {}, referencia_tipo=ref_tipo, referencia_id=ref_id)
    db.session.flush()
    if estado == 'FALLIDO':
        while j.estado != 'FALLIDO':
            j.marcar_fallo('Siesa rechazó')
    else:
        j.estado = estado
        if estado == 'COMPLETADO':
            j.fecha_completado = datetime.utcnow()
    if dias:
        cuando = datetime.utcnow() - timedelta(days=dias)
        j.fecha_creacion = cuando
        j.fecha_procesando = cuando if estado == 'FALLIDO' else None
    db.session.commit()
    return j


class TestFallidosVigentes:

    def test_el_superado_por_un_completado_posterior_no_cuenta(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        viejo = _job(db, 'DESPACHO_F470', 'FALLIDO', dias=10, ref_id=5)
        _job(db, 'DESPACHO_F470', 'COMPLETADO', dias=9, ref_id=5)
        r = fallidos_vigentes()
        assert viejo.id not in [j.id for j in r['jobs']] and r['superados'] == 1

    def test_un_completado_anterior_no_lo_supera(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        _job(db, 'DESPACHO_F470', 'COMPLETADO', dias=12, ref_id=6)
        viejo = _job(db, 'DESPACHO_F470', 'FALLIDO', dias=10, ref_id=6)
        assert [j.id for j in fallidos_vigentes()['jobs']] == [viejo.id]

    def test_otro_tipo_u_otra_referencia_no_lo_supera(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        f = _job(db, 'RECIBO_CAJA', 'FALLIDO', ref_tipo='RecaudoEntrega', ref_id=7)
        _job(db, 'DESPACHO_F470', 'COMPLETADO', ref_tipo='RecaudoEntrega', ref_id=7)
        _job(db, 'RECIBO_CAJA', 'COMPLETADO', ref_tipo='RecaudoEntrega', ref_id=8)
        assert [j.id for j in fallidos_vigentes()['jobs']] == [f.id]

    def test_sin_referencia_sigue_contando(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        _job(db, 'AJUSTE_CONTEO', 'FALLIDO', ref_tipo=None, ref_id=None)
        r = fallidos_vigentes()
        assert len(r['jobs']) == 1 and r['sin_referencia'] == 1

    def test_recientes_y_viejos_con_su_edad(self, db):
        from app.services.siesa_job_service import DIAS_FALLIDO_RECIENTE, fallidos_vigentes
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=1, ref_tipo='RecaudoEntrega', ref_id=1)
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=DIAS_FALLIDO_RECIENTE + 30,
             ref_tipo='RecaudoEntrega', ref_id=2)
        r = fallidos_vigentes()
        assert len(r['recientes']) == 1 and len(r['viejos']) == 1
        t = r['por_tipo'][0]
        assert t['tipo'] == 'RECIBO_CAJA' and t['edad_dias_max'] >= DIAS_FALLIDO_RECIENTE + 29
        assert t['desde_utc'] is not None

    def test_desde_y_hasta_acotan_la_cohorte(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=40, ref_tipo='RecaudoEntrega', ref_id=1)
        nuevo = _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=1, ref_tipo='RecaudoEntrega', ref_id=2)
        r = fallidos_vigentes(desde=datetime.utcnow() - timedelta(days=5))
        assert [j.id for j in r['jobs']] == [nuevo.id]

    def test_alerta_email_no_es_documento(self, db):
        from app.services.siesa_job_service import fallidos_vigentes
        _job(db, 'ALERTA_EMAIL', 'FALLIDO', ref_tipo=None)
        assert fallidos_vigentes()['jobs'] == []
        assert len(fallidos_vigentes(excluir_tipos=())['jobs']) == 1

    def test_la_reconciliacion_cierra_el_job_de_su_tarea(self, db, flujo, monkeypatch):
        from app.models.packing import TareaPacking
        from app.models.siesa_job import EstadoSiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.reconciliacion_service import ReconciliacionService
        from app.services.siesa_job_service import fallidos_vigentes
        tarea = db.session.get(TareaPacking, flujo.packing_id)
        tarea.siesa_triggered = False
        db.session.commit()
        job = _job(db, 'DESPACHO_F470', 'FALLIDO', ref_id=tarea.id)
        assert job.id in [j.id for j in fallidos_vigentes()['jobs']]
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido',
                            lambda self, t, c: [{'f350_consec_docto': 99}])
        r = ReconciliacionService.reconciliar_despacho(tarea, 'PD', '123')
        assert r['reconciliado'] is True
        db.session.refresh(job)
        assert job.estado == EstadoSiesaJob.COMPLETADO
        assert json.loads(job.resultado)['reconciliado'] is True
        assert job.id not in [j.id for j in fallidos_vigentes()['jobs']]


class TestLosLectoresUsanFallidosVigentes:

    def test_la_salud_pone_critico_solo_por_los_recientes(self, db, sin_corte):
        from app.services import analitica_salud as sa
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=60, ref_tipo='RecaudoEntrega', ref_id=1)
        c = sa.cola_siesa(datetime.utcnow())
        assert c['nivel'] == sa.ADVERTENCIA and c['fallidos_viejos'] == 1
        assert 'más viejo' in c['texto'] and c['fallidos_por_tipo'][0]['edad_dias'] >= 59
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=1, ref_tipo='RecaudoEntrega', ref_id=2)
        assert sa.cola_siesa(datetime.utcnow())['nivel'] == sa.CRITICO

    def test_la_salud_no_cuenta_el_superado(self, db, sin_corte):
        from app.services import analitica_salud as sa
        _job(db, 'DESPACHO_F470', 'FALLIDO', dias=2, ref_id=5)
        _job(db, 'DESPACHO_F470', 'COMPLETADO', dias=1, ref_id=5)
        c = sa.cola_siesa(datetime.utcnow())
        assert c['nivel'] == sa.OK and c['fallidos'] == 0 and c['fallidos_superados'] == 1

    def test_la_salud_cuenta_aparte_lo_anterior_al_corte(self, db, con_corte):
        from app.services import analitica_salud as sa
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=150, ref_tipo='RecaudoEntrega', ref_id=1)
        con_corte((datetime.utcnow() - timedelta(days=30)).date().isoformat())
        c = sa.cola_siesa(datetime.utcnow())
        assert c['nivel'] == sa.OK and c['fallidos_antes_del_corte'] == 1

    def test_la_fuga_no_cuenta_el_superado(self, db, sin_corte):
        from app.services import analitica_fugas as fg
        from app.utils.fecha import dia_operativo
        _job(db, 'DESPACHO_F470', 'FALLIDO', dias=3, ref_id=5)
        _job(db, 'DESPACHO_F470', 'COMPLETADO', dias=2, ref_id=5)
        vivo = _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=1, ref_tipo='RecaudoEntrega',
                    ref_id=902, payload={'monto': 5000})
        hoy = dia_operativo()
        det = fg.detalle_fuga('documentos_trabados', hoy - timedelta(days=29), hoy)
        assert [c['referencia'] for c in det['casos']] == [f'Job {vivo.id}']
        assert det['fuga']['extra']['superados'] == 1

    def test_el_kpi_no_cuenta_el_superado(self, db, sin_corte):
        from app.services import analitica_kpi as kpi
        from app.utils.fecha import dia_operativo
        _job(db, 'DESPACHO_F470', 'FALLIDO', dias=10, ref_id=5)
        _job(db, 'DESPACHO_F470', 'COMPLETADO', dias=9, ref_id=5)
        r = kpi.calcular('jobs_siesa_fallidos', dia_operativo() - timedelta(days=10))
        assert r.valor == 0 and r.detalle['superados'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete — nadie más consulta `estado == FALLIDO` para contar
# ═════════════════════════════════════════════════════════════════════════════

#: `archivo::función` que LEE el estado FALLIDO para OPERAR (reintentar,
#: descartar, reutilizar el job, decidir si una mercancía sigue en proceso),
#: no para contar lo trabado. Solo encoge. Un lector de analítica no entra.
CONSULTAS_FALLIDO_OPERATIVAS = {
    'app/routes/conteo.py::reintentar_fallos_dlq': 'reintenta los AJUSTE_CONTEO fallidos',
    'app/routes/conteo.py::_plan_descarte_ajustes': 'arma el plan de descarte de ajustes fallidos',
    'app/routes/siesa.py::listar_jobs': 'lista la cola para actuar (filtro por estado)',
    'app/routes/siesa.py::listar_jobs_fallidos': 'lista para reintentar/descartar: tiene que ver TODOS',
    'app/routes/siesa.py::resetear_jobs_fallidos': 'reencola los FALLIDO de un tipo',
    'app/services/alertas_service.py::_enviar_email_con_dlq': 'anti-duplicado de la alerta del día',
    'app/services/avisos_conteo_service.py::obtener_avisos_pendientes':
        'busca el job fallido de una raíz atascada para mostrar su error',
    'app/services/closing/pedido_closer.py::PedidoPackingCloser._encolar_job':
        'reutiliza el job FALLIDO en vez de duplicar el despacho',
    'app/services/closing/traslado_closer.py::TrasladoPackingCloser._encolar_job_traslado':
        'reutiliza el job FALLIDO en vez de duplicar el despacho del traslado',
    'app/services/conteo_listado.py::barra':
        'cuenta lo que tocan «reintentar/descartar fallos»: tiene que coincidir con el botón',
    'app/services/conteo_service.py::ConteoService._en_proceso_por_recepcion':
        'un FALLIDO no llegó a Siesa: la mercancía sigue en proceso',
    'app/services/conteo_service.py::ConteoService._en_proceso_por_averia':
        'un FALLIDO no llegó a Siesa: la avería sigue en proceso',
    'app/services/liquidacion_service.py::_hay_rc_en_cola': 'anti-doble-encolado (notin_ FALLIDO)',
    'app/services/packing_service.py::PackingService._cerrar_packing_pedido_legacy':
        'reutiliza el job FALLIDO tras resetear-siesa',
    'app/services/recepcion_service.py::RecepcionService.confirmar_recepcion':
        'reutiliza el job FALLIDO de la entrada',
    'app/services/reconciliacion_service.py::ReconciliacionService._cerrar_jobs_fallidos':
        'cierra los FALLIDO que la reconciliación resolvió',
    'app/services/siesa_job_service.py::_run_dlq_jobs': 'el procesador de la cola',
    'app/services/siesa_job_service.py::get_jobs_fallidos': 'lista para actuar sobre todos',
    'app/services/siesa_job_service.py::fallidos_vigentes': 'LA función que cuenta',
    'app/services/siesa_job_service.py::descartar_job': 'solo se descarta un FALLIDO',
    'app/services/siesa_job_service.py::reintentar_job': 'solo se reintenta un FALLIDO',
    'app/services/tablero_lider_conteo.py::_rechazados_siesa':
        'lista para reintentar/descartar desde el tablero del líder',
}
TOPE_CONSULTAS_FALLIDO = 22

#: Módulos que MUESTRAN números: acá no puede haber ninguna consulta propia.
MODULOS_QUE_SOLO_CUENTAN = ('app/services/analitica_', 'app/services/dashboard_service.py',
                            'app/routes/health.py', 'app/routes/analitica')


def _alias(tree):
    estado, job = {'EstadoSiesaJob'}, {'SiesaJob'}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.endswith('siesa_job'):
            for a in n.names:
                if a.name == 'EstadoSiesaJob':
                    estado.add(a.asname or a.name)
                if a.name == 'SiesaJob':
                    job.add(a.asname or a.name)
    return estado, job


def consultas_de_fallido(src: str):
    """`[(Clase.funcion, lineno)]` que LEEN FALLIDO de un SiesaJob:
    `EstadoSiesaJob.FALLIDO` fuera de una asignación, o `SiesaJob.estado`
    comparado (o `in_`/`notin_`) contra el literal 'FALLIDO'."""
    tree = ast.parse(src)
    estado, job = _alias(tree)
    out = []

    def es_fallido(n):
        return (isinstance(n, ast.Attribute) and n.attr == 'FALLIDO'
                and isinstance(n.value, ast.Name) and n.value.id in estado)

    def es_estado_job(n):
        return (isinstance(n, ast.Attribute) and n.attr == 'estado'
                and isinstance(n.value, ast.Name) and n.value.id in job)

    def con_literal(n):
        return any(isinstance(x, ast.Constant) and x.value == 'FALLIDO' for x in ast.walk(n))

    def visitar(nodo, pila):
        for hijo in ast.iter_child_nodes(nodo):
            p = pila + [hijo.name] if isinstance(
                hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else pila
            if (isinstance(hijo, (ast.Assign, ast.AnnAssign)) and hijo.value is not None
                    and es_fallido(hijo.value)):
                continue                    # escribir FALLIDO no es consultarlo
            nombre = '.'.join(p) or '<modulo>'
            if es_fallido(hijo):
                out.append((nombre, hijo.lineno))
            elif (isinstance(hijo, ast.Compare)
                  and (es_estado_job(hijo.left) or any(es_estado_job(c) for c in hijo.comparators))
                  and con_literal(hijo)):
                out.append((nombre, hijo.lineno))
            elif (isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
                  and hijo.func.attr in ('in_', 'notin_') and es_estado_job(hijo.func.value)
                  and any(con_literal(a) for a in hijo.args)):
                out.append((nombre, hijo.lineno))
            visitar(hijo, p)

    visitar(tree, [])
    return out


def _todas_las_consultas_de_fallido():
    out = {}
    for f in _archivos_py():
        rel = str(f.relative_to(RAIZ))
        for fn, ln in consultas_de_fallido(f.read_text(encoding='utf-8')):
            out.setdefault(f'{rel}::{fn}', []).append(ln)
    return out


class TestNadieMasCuentaFallidos:

    def test_ninguna_consulta_fuera_del_inventario(self):
        fuera = {k: v for k, v in _todas_las_consultas_de_fallido().items()
                 if k not in CONSULTAS_FALLIDO_OPERATIVAS}
        assert not fuera, (
            f'Consultas de FALLIDO sin declarar: {fuera}. Para CONTAR lo trabado '
            'usá `siesa_job_service.fallidos_vigentes` (excluye lo superado y lo '
            'que la reconciliación cerró); para operar, declaralo con su porqué.')

    def test_los_modulos_que_muestran_numeros_no_consultan_por_su_cuenta(self):
        for k in CONSULTAS_FALLIDO_OPERATIVAS:
            assert not k.startswith(MODULOS_QUE_SOLO_CUENTAN), k

    def test_el_inventario_solo_encoge(self):
        assert len(CONSULTAS_FALLIDO_OPERATIVAS) <= TOPE_CONSULTAS_FALLIDO

    def test_una_entrada_que_ya_no_consulta_se_borra(self):
        viejas = sorted(set(CONSULTAS_FALLIDO_OPERATIVAS) - set(_todas_las_consultas_de_fallido()))
        assert not viejas, viejas

    def test_cada_entrada_dice_por_que(self):
        for k, motivo in CONSULTAS_FALLIDO_OPERATIVAS.items():
            assert len(motivo) > 10, k

    def test_piso(self):
        assert len(_todas_las_consultas_de_fallido()) >= 20

    def test_meta_las_escrituras_que_ve(self):
        casos = [
            'from app.models.siesa_job import EstadoSiesaJob as E\n'
            'def f():\n    return q.filter(SiesaJob.estado == E.FALLIDO).count()\n',
            'def f():\n    return q.filter_by(estado=EstadoSiesaJob.FALLIDO).count()\n',
            "def f():\n    return q.filter(SiesaJob.estado == 'FALLIDO').count()\n",
            "def f():\n    return q.filter(SiesaJob.estado.in_(['PENDIENTE', 'FALLIDO']))\n",
            'def f(filas):\n    return [t for t, e in filas if e == EstadoSiesaJob.FALLIDO]\n',
        ]
        for src in casos:
            r = consultas_de_fallido(src)
            assert len(r) == 1 and r[0][0] == 'f', (src, r)

    def test_meta_lo_sano_no_se_marca(self):
        sanos = [
            'def f(job):\n    job.estado = EstadoSiesaJob.FALLIDO\n',
            "def f(r):\n    anotar(r, 'FALLIDO')\n",
            "def f(r):\n    return r.siesa_rc_resultado == 'FALLIDO'\n",
            'def f():\n    """SiesaJob.estado == EstadoSiesaJob.FALLIDO"""\n    return 1\n',
        ]
        for src in sanos:
            assert consultas_de_fallido(src) == [], src


# ═════════════════════════════════════════════════════════════════════════════
# 2b · Descartar: DESCARTADO + bitácora, y NO reenvía nada (Regla 3)
# ═════════════════════════════════════════════════════════════════════════════

def _auth(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


class TestDescartarUnJob:

    def test_solo_admin(self, app, client, db, usuario):
        j = _job(db, 'RECIBO_CAJA', 'FALLIDO', ref_tipo='RecaudoEntrega', ref_id=1)
        r = client.post(f'/api/siesa/jobs/{j.id}/descartar', json={'motivo': 'x'},
                        headers=_auth(app, usuario))
        assert r.status_code == 403

    def test_motivo_obligatorio(self, app, client, db, usuario_admin):
        j = _job(db, 'RECIBO_CAJA', 'FALLIDO', ref_tipo='RecaudoEntrega', ref_id=1)
        for cuerpo in ({}, {'motivo': '   '}):
            r = client.post(f'/api/siesa/jobs/{j.id}/descartar', json=cuerpo,
                            headers=_auth(app, usuario_admin))
            assert r.status_code == 400

    def test_no_existe_y_no_fallido(self, app, client, db, usuario_admin):
        r = client.post('/api/siesa/jobs/999999/descartar', json={'motivo': 'x'},
                        headers=_auth(app, usuario_admin))
        assert r.status_code == 404
        j = _job(db, 'RECIBO_CAJA', 'COMPLETADO', ref_tipo='RecaudoEntrega', ref_id=1)
        r = client.post(f'/api/siesa/jobs/{j.id}/descartar', json={'motivo': 'x'},
                        headers=_auth(app, usuario_admin))
        assert r.status_code == 409

    def test_descarta_con_bitacora_y_no_reenvia(self, app, client, db, usuario_admin, monkeypatch):
        from app.models.bitacora import BitacoraAccion
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        llamados = []
        monkeypatch.setattr(sjs, 'disparar_dlq_inmediato', lambda *a, **k: llamados.append(1))
        j = _job(db, 'RECIBO_CAJA', 'FALLIDO', ref_tipo='RecaudoEntrega', ref_id=1,
                 payload={'monto': 5000})
        antes = SiesaJob.query.count()
        r = client.post(f'/api/siesa/jobs/{j.id}/descartar',
                        json={'motivo': 'ensayo de abril, no es operación'},
                        headers=_auth(app, usuario_admin))
        assert r.status_code == 200, r.get_json()
        db.session.expire_all()
        j = db.session.get(SiesaJob, j.id)
        assert j.estado == 'DESCARTADO'
        assert j.error_ultimo == 'Siesa rechazó', 'no se borra el error'
        assert SiesaJob.query.count() == antes, 'descartar no encola nada'
        assert not llamados, 'descartar no dispara la cola'
        b = BitacoraAccion.query.filter_by(accion='DESCARTAR', entidad='SiesaJob',
                                           entidad_id=j.id).one()
        assert b.motivo == 'ensayo de abril, no es operación'
        assert b.usuario_id == usuario_admin.id
        assert b.antes['error_ultimo'] == 'Siesa rechazó'
        assert sjs.fallidos_vigentes()['jobs'] == []


# ═════════════════════════════════════════════════════════════════════════════
# 4 · La regla de contado no es retroactiva
# ═════════════════════════════════════════════════════════════════════════════

class TestLaReglaDeContadoNoEsRetroactiva:

    def _parada_credito(self, db, almacen, actores, cuando):
        from app.models.recaudo_entrega import RecaudoEntrega
        from tests.flujo import conductor_de_flujo as cf
        fl = cf.flujo_completo(db, almacen, actores['operario'].id, actores['conductor'].id,
                               estado_entrega='ENTREGADO', forma_pago='CREDITO',
                               monto_cobrado=0)
        r = db.session.get(RecaudoEntrega, fl.recaudo_id)
        r.cobro_contraentrega = None
        r.fecha_confirmacion = cuando
        db.session.commit()
        return r

    def test_parada_vieja_credito_cero_no_es_no_autorizada(self, db, almacen, actores):
        from app.services import cond_pago as cp
        r = self._parada_credito(db, almacen, actores, datetime(2026, 9, 12, 15, 0))
        assert cp.anterior_a_la_regla(r)
        assert cp.trato_de_cobro(r) == cp.TRATO_CREDITO
        assert not cp.credito_no_autorizado(r)

    def test_parada_nueva_igual_si_es_no_autorizada(self, db, almacen, actores):
        from app.services import cond_pago as cp
        r = self._parada_credito(db, almacen, actores, datetime.utcnow())
        assert not cp.anterior_a_la_regla(r)
        assert cp.credito_no_autorizado(r)

    def test_con_snapshot_rige_la_regla_nueva_aunque_sea_vieja(self, db, almacen, actores):
        from app.services import cond_pago as cp
        r = self._parada_credito(db, almacen, actores, datetime(2026, 9, 12, 15, 0))
        r.cobro_contraentrega = True
        db.session.commit()
        assert not cp.anterior_a_la_regla(r)
        assert cp.credito_no_autorizado(r)

    def test_lo_que_la_regla_vieja_ya_rechazaba_sigue_siendo_no_autorizado(
            self, db, almacen, actores, monkeypatch):
        from app.models.packing import TareaPacking
        from app.services import cond_pago as cp
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        r = self._parada_credito(db, almacen, actores, datetime(2026, 9, 12, 15, 0))
        t = db.session.get(TareaPacking, r.tarea_id)
        t.cond_pago, t.cond_pago_fe = 'C01', None
        db.session.commit()
        assert cp.trato_de_cobro(r) == cp.TRATO_NO_AUTORIZADO

    def test_vta62_no_la_ve_y_la_liquidacion_no_se_traba(self, db, almacen, actores):
        from app.services import auditoria
        from app.services import cond_pago as cp
        r = self._parada_credito(db, almacen, actores, datetime(2026, 9, 12, 15, 0))
        assert _res(auditoria.auditar('venta'), 'VTA-62')['total'] == 0
        from app.models.recaudo_entrega import RecaudoEntrega
        pendientes = [x for x in RecaudoEntrega.query.filter_by(ruta_id=r.ruta_id).all()
                      if cp.credito_no_autorizado(x)]
        assert pendientes == []

    def test_la_fecha_de_la_regla_es_la_del_commit(self):
        from app.services import cond_pago as cp
        assert cp.regla_contado_desde() == datetime(2026, 9, 24, 22, 35, 31)


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Cancelaciones: solo el flujo de negocio
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelacionesSoloDelNegocio:

    def test_la_limpieza_del_layout_va_a_tecnicas(self, db, sin_corte):
        from app.services import analitica_kpi as kpi
        from app.services.bitacora import registrar_accion
        from app.utils.fecha import dia_operativo
        ayer = dia_operativo() - timedelta(days=1)
        for i in range(3):
            registrar_accion('ELIMINAR', 'Ubicacion', 1000 + i).dia_operativo = ayer
        registrar_accion('CANCELAR', 'TareaPicking', 555, motivo='cliente anuló').dia_operativo = ayer
        registrar_accion('ELIMINAR', 'SiesaMapeoUnidades', 7).dia_operativo = ayer
        db.session.commit()
        r = kpi.calcular('cancelaciones', ayer)
        assert r.valor == 1
        assert r.detalle['por_entidad'] == {'TareaPicking': 1}
        assert r.detalle['tecnicas'] == {'Ubicacion': 3, 'SiesaMapeoUnidades': 1}

    def test_las_entidades_de_negocio_son_modelos_que_existen(self):
        import app.models  # noqa: F401
        from app.extensions import db
        from app.services.analitica_kpi import ENTIDADES_DE_NEGOCIO
        nombres = {m.class_.__name__ for m in db.Model.registry.mappers}
        assert ENTIDADES_DE_NEGOCIO <= nombres, ENTIDADES_DE_NEGOCIO - nombres


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Dashboard: «con diferencia» es DESCUADRE, edad, IRA por cadenas, cupo
# ═════════════════════════════════════════════════════════════════════════════

def _sesion(db, almacen, producto, estado, es_hijo=False, origen=None, nacio=None):
    from app.models.conteo import SesionConteo
    from app.models.ubicacion import Ubicacion
    import uuid
    ub = Ubicacion(codigo=f'GEN-{uuid.uuid4().hex[:6]}', almacen_id=almacen.id,
                   tipo_zona='GENERAL', activo=True)
    db.session.add(ub)
    db.session.flush()
    s = SesionConteo(codigo=f'CC-{uuid.uuid4().hex[:8]}', almacen_id=almacen.id,
                     producto_id=producto.id, estado=estado, es_segundo_conteo=es_hijo,
                     sesion_origen_id=origen, ubicacion_id=ub.id)
    db.session.add(s)
    db.session.flush()
    if nacio:
        s.fecha_creacion = nacio
    db.session.commit()
    return s


class TestDashboard:

    def test_con_diferencia_cuenta_descuadres_y_no_segundo_conteo(self, db, almacen, producto, sin_corte):
        from app.services.dashboard_service import DashboardService
        _sesion(db, almacen, producto, 'SEGUNDO_CONTEO')
        _sesion(db, almacen, producto, 'SEGUNDO_CONTEO')
        raiz = _sesion(db, almacen, producto, 'DESCUADRE', nacio=datetime.utcnow() - timedelta(hours=30))
        # El CC2 que resolvió queda en DESCUADRE para siempre: no es una decisión.
        _sesion(db, almacen, producto, 'DESCUADRE', es_hijo=True, origen=raiz.id)
        k = DashboardService.kpis_operativos(almacen.id)
        assert k['conteo']['en_descuadre'] == 1
        assert k['conteo']['en_segundo_conteo'] == 2
        assert k['alertas']['conteos_descuadre'] == 1
        assert k['conteo']['en_descuadre_edad']['edad_horas'] >= 29

    def test_la_cola_desde_el_corte_y_con_edad(self, db, almacen, producto, con_corte):
        from app.services.dashboard_service import DashboardService
        _sesion(db, almacen, producto, 'PENDIENTE', nacio=datetime(2026, 4, 2))
        _sesion(db, almacen, producto, 'PENDIENTE', nacio=datetime.utcnow() - timedelta(hours=5))
        con_corte('2026-06-01')
        k = DashboardService.kpis_operativos(almacen.id)
        assert k['conteo']['pendientes'] == 1 and k['conteo']['antes_del_corte'] == 1
        assert 4.5 <= k['conteo']['pendientes_edad']['edad_horas'] <= 6
        assert k['corte']['valido'] is True

    def test_ira_por_cadenas(self, db, almacen, sin_corte):
        from app.services.dashboard_service import DashboardService
        k = DashboardService.kpis_operativos(almacen.id)
        ira = k['conteo']['ira_hoy']
        assert set(ira) >= {'numerador', 'denominador', 'tasa', 'min_n', 'definicion'}
        assert ira['denominador'] == 0 and ira['tasa'] is None
        assert 'por cadena' in ira['definicion']

    @pytest.mark.parametrize('desc,defi,pend,tope,color', [
        (0, 1, 0, {'cupo_diario': 60, 'pendientes_vivas': 0}, 'rojo'),
        (2, 0, 5, {'cupo_diario': 60, 'pendientes_vivas': 5}, 'rojo'),
        (0, 0, 200, {'cupo_diario': 60, 'pendientes_vivas': 200}, 'amarillo'),
        (0, 0, 10, {'cupo_diario': 60, 'pendientes_vivas': 10}, 'verde'),
        (0, 0, 0, {'cupo_diario': 60, 'pendientes_vivas': 0}, 'gris'),
        (0, 0, 10, {}, 'amarillo'),
    ])
    def test_semaforo_de_conteo(self, desc, defi, pend, tope, color):
        from app.services.dashboard_service import semaforo_de_conteo
        assert semaforo_de_conteo(desc, defi, pend, tope)['color'] == color

    def test_traslados_y_rutas_traen_edad(self, db, sin_corte):
        from app.services.dashboard_service import DashboardService
        d = DashboardService.kpis_traslados_rutas()
        assert 'edad_horas' in d['traslados'] and 'edad_horas' in d['rutas']
        assert d['traslados']['antes_del_corte'] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Rezago de liquidación, fugas, recorrido, KPI: el corte en cada lector
# ═════════════════════════════════════════════════════════════════════════════

def _ruta_entregada(db, cuando):
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    from app.models.usuario import Usuario
    cond = Usuario.query.filter_by(email='cond_corte@test.com').first()
    if not cond:
        cond = Usuario(email='cond_corte@test.com', nombre='Conductor', rol='conductor',
                       activo=True)
        cond.set_password('test123')
        db.session.add(cond)
        db.session.flush()
    r = RutaDespacho(conductor_id=cond.id, tipo_ruta='Urbana', estado='ENTREGADA',
                     estado_financiero=EstadoFinancieroRuta.PENDIENTE)
    r.fecha_entregada = cuando
    db.session.add(r)
    db.session.commit()
    return r


class TestRezagoDesdeElCorte:

    def test_lo_entregado_antes_del_corte_no_alerta(self, db, con_corte):
        from app.services import rezago_liquidacion as rl
        vieja = _ruta_entregada(db, datetime(2026, 4, 2, 15, 0))
        nueva = _ruta_entregada(db, datetime.utcnow() - timedelta(days=3))
        sin_fecha = _ruta_entregada(db, None)
        con_corte('2026-06-01')
        d = rl.diagnostico()
        ids = {f['ruta_id'] for f in d['rutas']}
        assert vieja.id not in ids and {nueva.id, sin_fecha.id} <= ids
        assert d['antes_del_corte'] == 1
        assert vieja.id not in {r.id for r in rl.rutas_entregadas_sin_liquidar()}

    def test_sin_corte_todo_cuenta(self, db, sin_corte):
        from app.services import rezago_liquidacion as rl
        vieja = _ruta_entregada(db, datetime(2026, 4, 2, 15, 0))
        assert vieja.id in {f['ruta_id'] for f in rl.diagnostico()['rutas']}

    def test_al_cierre_tambien(self, db, con_corte):
        from app.services import rezago_liquidacion as rl
        _ruta_entregada(db, datetime(2026, 4, 2, 15, 0))
        con_corte('2026-06-01')
        r = rl.rutas_sin_liquidar_al_cierre(date(2026, 9, 1))
        assert r['antes_del_corte'] == 1 and r['rutas'] == []


class TestFugasYRecorridoDesdeElCorte:

    def test_la_fuga_cuenta_aparte_lo_anterior(self, db, con_corte):
        from app.services import analitica_fugas as fg
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=20, ref_tipo='RecaudoEntrega', ref_id=1)
        vivo = _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=2, ref_tipo='RecaudoEntrega', ref_id=2)
        con_corte((hoy - timedelta(days=10)).isoformat())
        res = fg.calcular_fugas(hoy - timedelta(days=29), hoy)
        doc = next(f for f in res['fugas'] if f['clave'] == 'documentos_trabados')
        assert doc['casos'] == 1 and doc['antes_del_corte']['casos'] == 1
        assert res['meta']['corte']['desde_efectivo'] == (hoy - timedelta(days=10)).isoformat()
        det = fg.detalle_fuga('documentos_trabados', hoy - timedelta(days=29), hoy)
        assert [c['referencia'] for c in det['casos']] == [f'Job {vivo.id}']

    def test_si_el_anterior_cae_antes_del_corte_no_hay_base(self, db, con_corte):
        from app.services import analitica_fugas as fg
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        _job(db, 'RECIBO_CAJA', 'FALLIDO', dias=2, ref_tipo='RecaudoEntrega', ref_id=2)
        # Rango de 6 días desde el corte: el anterior (hoy−11 … hoy−6) es todo ensayo.
        con_corte((hoy - timedelta(days=5)).isoformat())
        res = fg.calcular_fugas(hoy - timedelta(days=5), hoy)
        doc = next(f for f in res['fugas'] if f['clave'] == 'documentos_trabados')
        assert doc['tendencia']['direccion'] == 'sin_base'

    def test_el_recorrido_saca_lo_que_entro_antes(self, db, flujo, con_corte):
        from app.models.picking import TareaPicking
        from app.models.packing import TareaPacking
        from app.services import analitica_recorrido as ar
        from app.utils.fecha import dia_operativo
        viejo = datetime(2026, 4, 2, 15, 0)
        for t in TareaPicking.query.all():
            t.fecha_creacion = viejo
        for t in TareaPacking.query.all():
            t.fecha_creacion = viejo
        db.session.commit()
        hoy = dia_operativo()
        filtros = {'desde': date(2026, 4, 1), 'hasta': hoy, 'almacen_id': None}
        antes = ar.recorrido(filtros)
        con_corte('2026-06-01')
        despues = ar.recorrido(filtros)
        assert despues['meta']['antes_del_corte'] >= 1
        assert despues['meta']['corte_auditoria']['valido'] is True
        assert despues['pedidos'] < antes['pedidos']


class TestElKpiMarcaLoAnteriorAlCorte:

    def test_serie_marca_y_agregar_no_suma(self, db, con_corte):
        from app.models.analitica_kpi import AnaliticaKpiDiario, EstadoKpi
        from app.services import analitica_kpi as kpi
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        d1, d2 = hoy - timedelta(days=5), hoy - timedelta(days=2)
        for d, v in ((d1, 40), (d2, 3)):
            db.session.add(AnaliticaKpiDiario(dia_operativo=d, metrica='cancelaciones',
                                              almacen_id=None, valor=v, n=int(v),
                                              estado=EstadoKpi.OK, fuente='test'))
        db.session.commit()
        con_corte((hoy - timedelta(days=3)).isoformat())
        puntos = kpi.serie('cancelaciones', d1, d2)
        marcados = {p['dia']: p['antes_del_corte'] for p in puntos}
        assert marcados[d1.isoformat()] is True and marcados[d2.isoformat()] is False
        agg = kpi.agregar('cancelaciones', puntos)
        assert agg['valor'] == 3 and agg['dias_antes_del_corte'] == 2

    def test_las_metricas_de_siesa_no_se_cortan(self, con_corte):
        from app.services import analitica_kpi as kpi
        con_corte('2026-06-01')
        assert kpi.dia_de_corte_de('cartera_abierta') is None
        assert kpi.dia_de_corte_de('cancelaciones') == date(2026, 6, 1)


# ═════════════════════════════════════════════════════════════════════════════
# 7 · 8 · Pantallas: día Bogotá, hora sin Z, «a revisar en Siesa», descartar
# ═════════════════════════════════════════════════════════════════════════════

def _node(script, tz=None):
    """`tz`: la zona del proceso Node. Las pruebas de fecha corren con la de
    Bogotá: con `TZ=UTC` (la del CI) leer una hora sin zona como local o como
    UTC da lo mismo y el defecto no se vería."""
    import os
    if not shutil.which('node'):
        pytest.skip('sin node')
    env = dict(os.environ, **({'TZ': tz} if tz else {}))
    r = subprocess.run(['node', '-e', script, '--', str(PWA)],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


_CARGA = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const doc = { getElementById: id => els[id] || null, createElement: () => ({}) };
const ctx = { console, window: {}, document: doc, Intl, Date, Number, JSON, Math,
  localStorage: { getItem: () => null, setItem: () => {} },
  navigator: {}, setInterval: () => 0, setTimeout: () => 0, addEventListener: () => {} };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/%s', 'utf8'), ctx);
"""


class TestLasPantallas:

    def test_liquidacion_hoy_es_bogota(self):
        d = _node(_CARGA % 'liquidacion.js' + r"""
console.log(JSON.stringify({
  noche: vm.runInContext("liqHoyBogota(new Date('2026-09-25T02:30:00Z'))", ctx),
  dia: vm.runInContext("liqHoyBogota(new Date('2026-09-24T15:00:00Z'))", ctx)}));
""", tz='UTC')
        assert d == {'noche': '2026-09-24', 'dia': '2026-09-24'}

    def test_liquidacion_ya_no_usa_el_dia_utc(self):
        src = (PWA / 'liquidacion.js').read_text(encoding='utf-8')
        assert "toISOString().split('T')[0]" not in src

    def test_liquidacion_pinta_a_revisar_en_siesa(self):
        d = _node(_CARGA % 'liquidacion.js' + r"""
const el = { innerHTML: '' }; els['liq-desglose'] = el;
ctx.get = async () => ({ recaudos: { total: 3, matriz: {}, parcial_o_rechazado: 0 },
  rezago_liquidacion: { antes_del_corte: 4 },
  condicion_pago_ausente: { alertas: 9, a_revisar_en_siesa: '<b>2</b>', nota: '' } });
vm.runInContext('liqCargarDesglose()', ctx).then(() => console.log(JSON.stringify({ h: el.innerHTML })));
""")
        h = d['h']
        assert 'A revisar en Siesa' in h and '&lt;b&gt;2&lt;/b&gt;' in h
        assert 'badge-red' in h, 'lo que hay que mirar en Siesa manda el color'
        assert 'Anteriores al corte' in h

    def test_tablero_bi_dia_bogota_y_hora_sin_z(self):
        d = _node(_CARGA % 'tablero_bi.js' + r"""
console.log(JSON.stringify({
  hoy: vm.runInContext("biHoyBogota(new Date('2026-09-25T02:30:00Z'))", ctx),
  hora: vm.runInContext("biFechaHora('2026-09-25T01:10:00')", ctx),
  conZ: vm.runInContext("biFechaHora('2026-09-25T01:10:00Z')", ctx)}));
""", tz='America/Bogota')
        assert d['hoy'] == '2026-09-24'
        assert '24/09' in d['hora'] or '24/9' in d['hora']
        assert d['hora'] == d['conZ'], 'sin Z es UTC, igual que con Z'

    def test_tablero_bi_ya_no_usa_el_dia_utc(self):
        src = (PWA / 'tablero_bi.js').read_text(encoding='utf-8')
        assert "toISOString().split('T')[0]" not in src

    def test_el_panel_de_la_dlq_descarta_solo_con_el_id(self):
        src = (PWA / 'app.js').read_text(encoding='utf-8')
        assert 'onclick="siesaDescartarJob(${Number(j.id)})"' in src
        assert '/api/siesa/jobs/${Number(id)}/descartar' in src
        assert 'j.ultimo_error' not in src, 'el campo es error_ultimo'

    def test_la_edad_del_dashboard(self):
        import re
        src = (PWA / 'app.js').read_text(encoding='utf-8')
        fn = re.search(r'function _dashEdadTexto\(b\) \{.*?\n\}\n', src, re.S).group(0)
        d = _node(r"""
const vm = require('vm'); const ctx = { Number, Math }; vm.createContext(ctx);
vm.runInContext(""" + json.dumps(fn) + r""", ctx);
const f = vm.runInContext('_dashEdadTexto', ctx);
console.log(JSON.stringify({ h: f({edad_horas: 5.4}), d: f({edad_horas: 100}),
  n: f({edad_horas: null}), u: f(undefined) }));
""")
        assert d == {'h': 'lo más viejo: 5 h', 'd': 'lo más viejo: 4 días', 'n': '', 'u': ''}

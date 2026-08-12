"""
El pedido no es la factura, y `get_rowids_factura` necesita la factura.

En las consultas de Siesa los dos documentos viven en prefijos distintos:

    f350_*  → el documento consultado (la FACTURA)
    f430_*  → el pedido que la originó

`get_rowids_factura` filtra por `f350_id_tipo_docto` / `f350_consec_docto`.
Todo el flujo de liquidación le pasaba `tarea.tipo_docto_pedido_siesa` — **en
seis sitios** — con la variable llamada `tipo_docto_fe`.

Medido en producción el 2026-08-11, ruta 15, job 440:

    GET API_v2_Ventas_Facturas_DesdePedido: 400 Client Error
    parametros=f350_id_tipo_docto = ''PD'' AND f350_consec_docto = 1308

`PD` es el tipo del pedido; la factura es `FEW-xxxx`. **Ninguna nota crédito de
liquidación llegó nunca a Siesa**, igual que ningún recibo de caja (142888) ni
documento contable (142882): los tres conectores financieros de la liquidación
estaban rotos, y los tres por la misma razón de fondo — el flujo se ejercitó
por primera vez esa noche.

## Por qué el nombre importa

`tipo_docto_fe` —FE, factura electrónica— contenía el pedido. Quien lee
`get_rowids_factura(tipo_docto_fe, consec_fe)` no tiene motivo para dudar. El
defecto no está donde se usa: está donde se asigna, y el nombre lo esconde.

Por eso este archivo **no comprueba nombres de variable**: comprueba que ningún
llamador reciba el pedido, y que la resolución exista y no caiga al pedido
cuando falla.

## Y la lección ya estaba escrita

`devolucion_cliente_service` lo hacía bien desde antes, con el comentario
«Resolver tipo/consec REALES de la FE — nunca los _pedido_siesa». Liquidación
hacía exactamente lo que ese comentario prohíbe: un comentario protege al
archivo donde está y a ninguno más. Ahora es una función.
"""
import ast
import re
from pathlib import Path

import pytest

from app.services.fe_resolver import FENoEncontrada, resolver_fe, resolver_fe_o_none

_RAIZ = Path(__file__).resolve().parents[1]
_ARCHIVOS = ['app/services/liquidacion_service.py', 'app/routes/rutas.py',
             'app/services/siesa_job_service.py',
             'app/services/devolucion_cliente_service.py']


class _TareaFalsa:
    id = 7
    codigo = 'PK-7'
    rm_tipo = 'RS'
    rm_consec = 44
    tipo_docto_pedido_siesa = 'PD'
    consec_docto_pedido_siesa = '1308'


@pytest.fixture(autouse=True)
def _no_simulacion(monkeypatch):
    """El doble `SIMFE` solo aplica en simulación. Acá se prueba el camino
    real, así que se apaga — si no, todo resolvería al doble y los tests de
    fallo pasarían por el motivo equivocado."""
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)


@pytest.fixture
def con_factura(monkeypatch):
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'get_detalle_factura',
                        lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                       'f350_consec_docto': '1466'}],
                        raising=False)


@pytest.fixture
def sin_factura(monkeypatch):
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'get_detalle_factura',
                        lambda **kw: [], raising=False)


class TestResuelveLaFacturaYNoElPedido:

    def test_devuelve_la_FE_no_el_pedido(self, con_factura):
        assert resolver_fe(_TareaFalsa()) == ('FEW', '1466')

    def test_nunca_devuelve_el_pedido_cuando_no_encuentra(self, sin_factura):
        """El caso que produjo el 400. Caer al pedido no es un fallback: es
        pedirle a Siesa una factura que no existe con ese número — o peor,
        encontrar otro documento que sí existe con esa numeración."""
        with pytest.raises(FENoEncontrada):
            resolver_fe(_TareaFalsa())

    def test_la_version_tolerante_tampoco_cae_al_pedido(self, sin_factura):
        assert resolver_fe_o_none(_TareaFalsa()) == (None, None)

    def test_sin_tarea_levanta(self):
        with pytest.raises(FENoEncontrada):
            resolver_fe(None)

    def test_falta_el_consecutivo_en_la_respuesta(self, monkeypatch):
        """Siesa respondió, pero sin los campos. Media respuesta no es una FE."""
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW'}],
                            raising=False)
        with pytest.raises(FENoEncontrada):
            resolver_fe(_TareaFalsa())


class TestNingunLlamadorRecibeElPedido:
    """Se mide el ARGUMENTO, no el nombre de la variable. El defecto original
    tenía el nombre correcto (`tipo_docto_fe`) y el valor equivocado."""

    def _llamadas(self, ruta: Path):
        arbol = ast.parse(ruta.read_text(encoding='utf-8'))
        salida = []
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == 'get_rowids_factura'):
                salida.append((nodo.lineno, [ast.unparse(a) for a in nodo.args]))
        return salida

    def test_ninguna_llamada_pasa_un_campo_del_pedido(self):
        culpables = []
        for rel in _ARCHIVOS:
            for linea, args in self._llamadas(_RAIZ / rel):
                if any('pedido_siesa' in a for a in args):
                    culpables.append(f'{rel}:{linea}  {args}')
        assert not culpables, (
            '\n`get_rowids_factura` filtra por f350_* (la FACTURA) y le están '
            'pasando el PEDIDO:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar `fe_resolver.resolver_fe(tarea)`.')

    def test_el_detector_ve_las_llamadas(self):
        """Un detector que deja de encontrar llamadas pasa vacío para siempre."""
        total = sum(len(self._llamadas(_RAIZ / r)) for r in _ARCHIVOS)
        assert total >= 6, f'solo encontró {total} llamadas — ¿cambió el nombre?'

    def _contaminadas_por_funcion(self, ruta: Path):
        """Variables asignadas desde un `*_pedido_siesa`, por función.

        La versión anterior de este guard solo miraba los ARGUMENTOS de la
        llamada, y una mutación lo esquivó con una asignación intermedia:

            tipo_docto_fe, consec_fe = tarea.tipo_docto_pedido_siesa, ...
            connekta.get_rowids_factura(tipo_docto_fe, consec_fe)

        Que es, literalmente, la forma que tenía el bug original. Un detector
        que mira el sitio del síntoma y no el de la causa deja pasar
        exactamente el caso que motivó escribirlo.
        """
        arbol = ast.parse(ruta.read_text(encoding='utf-8'))
        salida = {}
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sucias = set()
            for nodo in ast.walk(fn):
                if not isinstance(nodo, ast.Assign):
                    continue
                origen = ast.unparse(nodo.value)
                if '_pedido_siesa' not in origen:
                    continue
                for destino in nodo.targets:
                    for sub in ast.walk(destino):
                        if isinstance(sub, ast.Name):
                            sucias.add(sub.id)
            if sucias:
                salida[fn.name] = (fn, sucias)
        return salida

    def test_ninguna_variable_del_pedido_llega_a_la_llamada(self):
        """Sigue la variable, no el argumento literal."""
        culpables = []
        for rel in _ARCHIVOS:
            for fn_nombre, (fn, sucias) in self._contaminadas_por_funcion(_RAIZ / rel).items():
                for nodo in ast.walk(fn):
                    if (isinstance(nodo, ast.Call)
                            and isinstance(nodo.func, ast.Attribute)
                            and nodo.func.attr == 'get_rowids_factura'):
                        usados = {n.id for a in nodo.args for n in ast.walk(a)
                                  if isinstance(n, ast.Name)}
                        if usados & sucias:
                            culpables.append(
                                f'{rel}:{nodo.lineno} en {fn_nombre}() — '
                                f'{sorted(usados & sucias)} viene(n) del pedido')
        assert not culpables, (
            '\nUna variable cargada desde `*_pedido_siesa` termina en '
            '`get_rowids_factura`:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nEse fue el bug: el nombre decía `_fe` y el valor era el pedido.')

    def test_ninguna_variable_llamada_fe_se_carga_con_el_pedido(self):
        """El nombre que miente, detectado donde nace.

        Los dos guards anteriores miran la llamada y la función. Ninguno ve el
        caso real, porque **el defecto cruza un módulo**: `liquidacion_service`
        metía el pedido en el payload del job bajo la clave `tipo_docto_fe`, y
        `siesa_job_service` lo leía de ahí y llamaba a `get_rowids_factura`.
        Entre el origen y el uso hay una cola de trabajos.

        Lo que sí es local y es el defecto entero: una variable llamada `_fe`
        asignada desde `*_pedido_siesa`. Eso no tiene ningún caso legítimo — si
        el valor es el pedido, el nombre tiene que decir pedido.
        """
        culpables = []
        for rel in _ARCHIVOS:
            arbol = ast.parse((_RAIZ / rel).read_text(encoding='utf-8'))
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.Assign):
                    continue
                origen = ast.unparse(nodo.value)
                if '_pedido_siesa' not in origen:
                    continue
                nombres = [n.id for d in nodo.targets for n in ast.walk(d)
                           if isinstance(n, ast.Name)]
                mentirosos = [n for n in nombres if re.search(r'_fe$|_fe_', n)]
                if mentirosos:
                    culpables.append(f'{rel}:{nodo.lineno}  {mentirosos} = {origen[:60]}')
        assert not culpables, (
            '\nUna variable con nombre de FACTURA cargada con el PEDIDO:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nEs el bug del job 440 tal cual. Usar `fe_resolver.resolver_fe`, '
              'o renombrar la variable a lo que de verdad contiene.')

    def test_el_seguimiento_de_variables_no_esta_ciego(self):
        """Comprueba sobre código sintético que el rastreo detecta la forma que
        se le escapó a la primera versión."""
        import textwrap
        codigo = textwrap.dedent('''
            def f(tarea, connekta):
                tipo_docto_fe = tarea.tipo_docto_pedido_siesa
                consec_fe = tarea.consec_docto_pedido_siesa
                return connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        ''')
        tmp = _RAIZ / 'tests' / '_tmp_deteccion.py'
        tmp.write_text(codigo, encoding='utf-8')
        try:
            encontrado = self._contaminadas_por_funcion(tmp)
            assert 'f' in encontrado
            assert {'tipo_docto_fe', 'consec_fe'} <= encontrado['f'][1]
        finally:
            tmp.unlink()


class TestUnaSolaResolucion:
    """Anti-divergencia. La lógica correcta existía en
    `devolucion_cliente_service` y no se aplicaba en liquidación."""

    def test_nadie_arma_su_propia_resolucion_de_FE(self):
        """Buscar `f350_id_tipo_docto` leído de una respuesta de factura fuera
        del resolver: es la firma de una segunda implementación."""
        culpables = []
        for rel in _ARCHIVOS:
            texto = (_RAIZ / rel).read_text(encoding='utf-8')
            for n, l in enumerate(texto.split('\n'), 1):
                if re.search(r"get\(\s*'f350_id_tipo_docto'", l):
                    culpables.append(f'{rel}:{n}')
        assert not culpables, (
            '\nVuelven a resolver la FE por su cuenta:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar `fe_resolver.resolver_fe`.')

    def test_el_resolver_existe_y_se_usa(self):
        usos = sum(
            'fe_resolver' in (_RAIZ / r).read_text(encoding='utf-8')
            for r in _ARCHIVOS)
        assert usos >= 2, 'el resolver no lo llama casi nadie — ¿quedó sin caller?'

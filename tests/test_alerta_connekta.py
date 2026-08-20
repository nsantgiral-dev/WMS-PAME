"""
Connekta no falla con un código HTTP. Falla con una fila.

Cuando el filtro no le gusta, la API responde **HTTP 200** con una tabla de un
elemento::

    {"detalle": {"Table": [{"alerta": "Por favor verifique los parámetros
                            o filtros enviados en la petición."}]}}

Para el código, tan válida como una consulta que devolvió una factura.

El WMS lo trataba de dos maneras y **las dos degradaban hacia la respuesta
buena** —lo que la auditoría de agosto llama, con nombre propio, el adaptador
que miente:

    rows = [r for r in rows if 'alerta' not in r]     → sigue con []
    if len(rows) == 1 and 'alerta' in rows[0]: break  → corta y no lo dice

El primero convierte «tu consulta fue rechazada» en «no hay nada». Uno de esos
dos sitios era `get_compromisos_pedido`, cuyo `[]` significa **«no queda nada
por remisionar»** — la misma forma que ya costó mercancía saliendo del CD sin
respaldo fiscal y que `CompromisosNoDisponibles` vino a cerrar.

## El reconocimiento tiene que ser estrecho

Una fila, una clave, que se llame `alerta`. Una consulta que devuelve un único
registro de un solo campo es rara pero legítima, y tratarla como error
escondería datos buenos: se cambiaría un modo de fallo ruidoso por uno
silencioso, que es peor.
"""
import pytest

from app.services.connekta_gateway import (
    ConnektaConsultaRechazada,
    _alerta_de,
    _exigir_datos,
)

_SOBRE = [{'alerta': 'Por favor verifique los parámetros o filtros enviados '
                     'en la petición.'}]


class TestElReconocimiento:
    def test_ve_el_sobre_real_de_connekta(self):
        assert _alerta_de(_SOBRE).startswith('Por favor verifique')

    @pytest.mark.parametrize('rows,por_que', [
        ([], 'una tabla vacía es «no hay filas», no un rechazo'),
        ([{'f120_referencia': 'ARTESA898'}], 'una fila de datos normal'),
        ([{'alerta': 'x'}, {'alerta': 'y'}],
         'dos filas no son el sobre — el sobre trae exactamente una'),
        ([{'total': 42}],
         'UNA fila de UN campo, legítima: un COUNT. Marcarla como error '
         'escondería datos buenos'),
        ([{'alerta': 'x', 'f120_referencia': 'A'}],
         'trae `alerta` pero también datos: no es el sobre de rechazo'),
        (None, 'no es una lista'),
    ])
    def test_no_confunde_datos_con_rechazo(self, rows, por_que):
        """Ensanchar esto cambia un fallo ruidoso por uno silencioso."""
        assert _alerta_de(rows) == '', por_que


class TestExigirDatos:
    def test_levanta_con_el_filtro_en_el_mensaje(self):
        """Sin el filtro hay que adivinar cuál de las veinte consultas del
        sistema fue la que rebotó."""
        with pytest.raises(ConnektaConsultaRechazada) as e:
            _exigir_datos(_SOBRE, 'get_compromisos_pedido',
                          "f430_id_co = ''003''")
        assert 'get_compromisos_pedido' in str(e.value)
        assert "f430_id_co = ''003''" in str(e.value)

    def test_deja_pasar_los_datos_tal_cual(self):
        filas = [{'f405_cant_por_remisionar_base': 5}]
        assert _exigir_datos(filas, 'x') is filas

    def test_una_tabla_vacia_no_es_un_rechazo(self):
        """`[]` legítimo tiene que seguir pasando: hay consultas cuyo
        resultado normal es ninguna fila."""
        assert _exigir_datos([], 'x') == []


class TestNingunSitioLoArmaAMano:
    """Por AST. La política ya existía escrita en 5 sitios con 2 formas
    distintas — es cómo se llega a que dos de ellos fallen hacia el lado
    equivocado."""

    def test_ninguna_comparacion_manual_contra_alerta(self):
        import ast
        import pathlib

        src = pathlib.Path('app/services/connekta_gateway.py').read_text()
        arbol = ast.parse(src)
        # `'alerta' in <algo>` fuera de las dos funciones autorizadas
        autorizadas = {'_alerta_de', '_exigir_datos'}
        culpables = []
        for fn in ast.walk(arbol):
            if not isinstance(fn, ast.FunctionDef) or fn.name in autorizadas:
                continue
            for n in ast.walk(fn):
                if not isinstance(n, ast.Compare):
                    continue
                izq = n.left
                if (isinstance(izq, ast.Constant) and izq.value == 'alerta'
                        and any(isinstance(o, (ast.In, ast.NotIn))
                                for o in n.ops)):
                    culpables.append(f'{fn.name}:{n.lineno}')
        assert not culpables, (
            f'{culpables} vuelve a preguntar por `alerta` a mano. Usar '
            f'`_exigir_datos(rows, nombre, filtro)`: cinco sitios con dos '
            f'formas distintas es cómo dos de ellos terminaron degradando '
            f'hacia la respuesta buena.')

    def test_el_detector_ve_una_reintroduccion(self):
        """Detector ciego: sin esto, «0 hallazgos» no distingue un repo sano
        de un detector roto."""
        import ast
        fuente = "def f(rows):\n    if 'alerta' in rows[0]:\n        return []\n"
        arbol = ast.parse(fuente)
        encontrado = []
        for fn in ast.walk(arbol):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for n in ast.walk(fn):
                if (isinstance(n, ast.Compare)
                        and isinstance(n.left, ast.Constant)
                        and n.left.value == 'alerta'
                        and any(isinstance(o, (ast.In, ast.NotIn))
                                for o in n.ops)):
                    encontrado.append(n.lineno)
        assert encontrado == [2]


class TestLosSitiosVivosLevantan:
    """Que la función exista no sirve si los llamadores no la usan — es la
    lección de `dias_expuestos`, centralizada y con cinco llamadores que sí la
    llamaban, y de `get_factura_desde_remision`, probada y sin un solo caller.
    """

    @pytest.mark.parametrize('metodo,args', [
        ('get_compromisos_pedido', ('PD', 1352)),
        ('get_detalle_factura', ('FEW', 1466)),
    ])
    def test_un_rechazo_de_siesa_no_se_lee_como_lista_vacia(self, monkeypatch,
                                                            metodo, args):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, '_get',
                            lambda *a, **k: {'detalle': {'Table': _SOBRE}})

        with pytest.raises(Exception) as e:
            getattr(connekta, metodo)(*args)
        assert 'rechazó la consulta' in str(e.value) or \
               'No se pudo' in str(e.value), (
            f'{metodo} se comió el rechazo y devolvió algo que parece datos')

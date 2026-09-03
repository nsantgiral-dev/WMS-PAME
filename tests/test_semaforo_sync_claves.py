"""El semáforo de `/api/siesa/monitor` leía claves que nadie escribe.

```
semaforo()                 leía   resultado · ultima_sync · error
estado_reconciliacion()    devuelve      ultimo_resultado · ultimo_inicio · ultimo_error
```

Ninguna de las tres coincidía, y **las cuatro** funciones de estado del monitor
(productos, pedidos, inventario, reconciliación) tienen la misma forma: la
desalineación no era de un módulo, era de las cuatro. Resultado: el semáforo
sólo podía dar `AMARILLO` (mientras corre) o `GRIS`. Nunca verde, nunca rojo —
o sea, **una reconciliación que reventó se veía igual que una que nunca se
corrió.** Preexistente.

Lo que lo mantuvo invisible es lo de siempre: `modulos` no se pintaba en
ninguna pantalla. Arreglar las claves sin pintarlo habría sido arreglar el
gemelo muerto — por eso `siesaRecuperacionCargar()` ahora lo muestra, y
`tests/test_render_reconciliacion_js.py::TestElPanelDeRecuperacionPintaLosSemaforos`
lo ejerce.

Este trinquete mide la propiedad que faltaba, no una copia de ella: toma las
claves que el semáforo lee **del propio semáforo, por AST** y exige que estén
en lo que cada función de estado devuelve de verdad. Escribir a mano la lista
de claves acá sería reintroducir el mismo defecto en el test.
"""
import ast
import inspect

import pytest

from app.routes import siesa as rutas_siesa


# Las cuatro funciones que alimentan el semáforo. Si aparece una quinta y no
# se agrega acá, `test_el_monitor_no_gana_un_modulo_sin_trinquete` lo dice.
def _funciones_de_estado():
    from app.services.inventario_siesa_service import (
        estado_carga_inventario, estado_reconciliacion)
    from app.services.pedidos_sync_service import estado_sync as estado_pedidos
    from app.services.siesa_sync_service import estado_sync as estado_productos
    return {
        'productos': estado_productos,
        'pedidos': estado_pedidos,
        'inventario': estado_carga_inventario,
        'reconciliacion': estado_reconciliacion,
    }


def _claves_que_lee_el_semaforo():
    """Las cadenas que `_semaforo_de_estado` pasa a `.get(...)`, por AST.

    Por AST y no por texto: un `grep` de `'ultimo_error'` sobre el archivo lo
    encuentra en el docstring de al lado y se declara satisfecho — la forma que
    en este repo ya falló siete veces.
    """
    arbol = ast.parse(inspect.getsource(rutas_siesa._semaforo_de_estado))
    claves = set()
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == 'get'
                and nodo.args
                and isinstance(nodo.args[0], ast.Constant)
                and isinstance(nodo.args[0].value, str)):
            claves.add(nodo.args[0].value)
    return claves


class TestElSemaforoLeeLoQueLosServiciosEscriben:

    def test_toda_clave_leida_existe_en_las_cuatro(self):
        claves = _claves_que_lee_el_semaforo()
        assert claves, 'el AST no encontró ninguna clave — el detector se apagó'
        for nombre, fn in _funciones_de_estado().items():
            faltan = claves - set(fn())
            assert not faltan, (
                f'el semáforo de «{nombre}» lee {sorted(faltan)} y '
                f'{fn.__module__}.{fn.__name__}() nunca las escribe: ese '
                'módulo se queda en gris para siempre')

    def test_el_monitor_no_gana_un_modulo_sin_trinquete(self, client,
                                                        jwt_token_admin):
        r = client.get('/api/siesa/monitor',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        assert set(r.get_json()['modulos']) == set(_funciones_de_estado()), (
            'el monitor tiene un módulo que este archivo no vigila')


class TestElSemaforoDaLosCuatroColores:
    """Un detector que solo prueba que dispara, prueba la mitad. Acá se exige
    cada color, y sobre todo que el rojo siga siendo rojo."""

    def _estado(self, **kw):
        base = {'en_curso': False, 'ultimo_inicio': None,
                'ultimo_resultado': None, 'ultimo_error': None}
        base.update(kw)
        return base

    def test_en_curso_es_amarillo(self):
        assert rutas_siesa._semaforo_de_estado(
            self._estado(en_curso=True)) == 'AMARILLO'

    def test_una_reconciliacion_fallida_es_roja(self):
        assert rutas_siesa._semaforo_de_estado(
            self._estado(ultimo_error='Connekta 500')) == 'ROJO'

    def test_el_error_gana_sobre_un_resultado_viejo(self):
        """Si la última corrida reventó, el resultado de la anterior sigue en
        memoria. Verde ahí sería pintar de verde una corrida que falló."""
        assert rutas_siesa._semaforo_de_estado(self._estado(
            ultimo_error='timeout',
            ultimo_resultado={'sin_diferencias': True},
            ultimo_inicio='2026-08-20T10:00:00')) == 'ROJO'

    def test_una_corrida_terminada_es_verde(self):
        assert rutas_siesa._semaforo_de_estado(self._estado(
            ultimo_resultado={'sin_diferencias': False},
            ultimo_inicio='2026-08-20T10:00:00')) == 'VERDE'

    def test_un_resultado_con_diferencias_igual_es_verde(self):
        """El semáforo mide si el SINCRONIZADOR corrió, no si el inventario
        cuadra. Mezclar las dos preguntas deja un rojo que nadie sabe leer —
        el veredicto de la reconciliación tiene su propia pantalla."""
        assert rutas_siesa._semaforo_de_estado(self._estado(
            ultimo_resultado={'sin_diferencias': False,
                              'total_discrepancias': 12})) == 'VERDE'

    def test_nunca_corrida_es_gris(self):
        assert rutas_siesa._semaforo_de_estado(self._estado()) == 'GRIS'

    def test_gris_y_verde_no_se_confunden_con_un_resultado_vacio(self):
        """`ultimo_resultado = {}` es un resultado, y `{}` es falsy. Con `or`
        se leería como «nunca corrió»."""
        assert rutas_siesa._semaforo_de_estado(
            self._estado(ultimo_resultado={})) == 'VERDE'


class TestElMonitorPublicaLaColaDLQ:
    """`siesaRecuperacionCargar()` pinta «Jobs pendientes / Jobs fallidos» con
    `monitor.pendientes` y `monitor.fallidos`, y el endpoint nunca los
    devolvió: las dos casillas mostraban `—` desde siempre. La misma clase que
    el semáforo, en el mismo endpoint."""

    def test_el_endpoint_declara_pendientes_y_fallidos(self, client,
                                                       jwt_token_admin):
        r = client.get('/api/siesa/monitor',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        assert isinstance(d.get('pendientes'), int)
        assert isinstance(d.get('fallidos'), int)

    def test_las_claves_viejas_siguen_ahi(self, client, jwt_token_admin):
        """Contrato del JSON: se agrega, no se borra."""
        r = client.get('/api/siesa/monitor',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert 'modulos' in d and 'connekta' in d
        for m in d['modulos'].values():
            assert set(m) == {'estado', 'detalle'}

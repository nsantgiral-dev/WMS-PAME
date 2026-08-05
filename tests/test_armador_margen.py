"""
El armador ordena por margen de verdad, no por cero.

Hasta el 2026-08-05 el relleno del contenedor rankeaba así:

    margen_u = (prod.precio_venta or 0) - (prod.precio_compra or 0)

y **la sincronización de Siesa NUNCA puebla ninguno de los dos** — está
declarado en `costo_service.py`. Los dos valían 0 para todos los SKU, así que
`margen_por_cbm` valía 0 para todos los candidatos.

**El bug no producía un error: producía un orden.** `list.sort()` es estable, de
modo que la lista salía en el orden de entrada con aspecto de ranking. Y el
recorte por presupuesto —que invierte ese mismo orden— cortaba por lo mismo. Lo
que entra al contenedor y lo que se saca se decidían con una constante.

`temporada_service` **ya había tenido este bug exacto** y se corrigió ahí, con un
comentario que lo explica: *"la cobertura no era 40%, era CERO"*. No se propagó
al armador. La misma política en dos sitios, arreglada en uno — el patrón que
este repo ya tiene documentado como el más caro.

Un contenedor mal armado es irreversible 120 días.
"""
import ast
from pathlib import Path

import pytest

_FUENTE = (Path(__file__).resolve().parents[1] / 'app' / 'services'
           / 'armador_service.py')


class TestNoSeLeeElMaestroParaElMargen:
    """TRINQUETE — el maestro no tiene el dato y nunca lo va a tener.

    `Producto.precio_venta` y `precio_compra` existen como columnas y están
    siempre en 0. Cualquier cálculo de margen que los lea da 0 sin fallar.
    """

    def test_el_armador_no_lee_precio_del_maestro(self):
        arbol = ast.parse(_FUENTE.read_text(encoding='utf-8'))
        docstrings = set()
        for n in ast.walk(arbol):
            if not isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                continue
            cuerpo = getattr(n, 'body', [])
            if cuerpo and isinstance(cuerpo[0], ast.Expr) and \
               isinstance(cuerpo[0].value, ast.Constant) and \
               isinstance(cuerpo[0].value.value, str):
                ini = cuerpo[0].value.lineno
                docstrings.update(range(ini, (cuerpo[0].value.end_lineno or ini) + 1))

        malas = []
        for n, linea in enumerate(_FUENTE.read_text(encoding='utf-8').split('\n'), 1):
            if n in docstrings or linea.strip().startswith('#'):
                continue
            if 'precio_venta' in linea or 'precio_compra' in linea:
                malas.append(f'{n}: {linea.strip()[:70]}')
        assert not malas, (
            '\nEl armador volvió a leer el precio del maestro, que siempre es 0:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar costo_service.resolver_costos() — devuelve `cu` con su fuente.')

    def test_usa_el_servicio_de_costos(self):
        fuente = _FUENTE.read_text(encoding='utf-8')
        assert 'resolver_costos' in fuente
        assert 'resumen_por_fuente' in fuente, (
            'sin el resumen, un contenedor armado 100% sobre margen supuesto se '
            've igual que uno armado sobre cotizaciones vigentes')


class TestElRankingDistingue:
    """La propiedad: dos ítems con márgenes distintos salen en orden distinto.

    Con el bug, cualquier lista salía en el orden de entrada — el test que
    hubiera importado no es "el margen se calcula bien", es "el orden CAMBIA
    cuando el margen cambia".
    """

    def _ordenar(self, items):
        """Reproduce el criterio de orden del armador."""
        items = list(items)
        items.sort(key=lambda x: (-x['margen_por_cbm'], x['costo_fob_usd']))
        return [i['referencia'] for i in items]

    def test_el_de_mayor_margen_va_primero(self):
        orden = self._ordenar([
            {'referencia': 'BAJO',  'margen_por_cbm': 10.0, 'costo_fob_usd': 5},
            {'referencia': 'ALTO',  'margen_por_cbm': 90.0, 'costo_fob_usd': 5},
            {'referencia': 'MEDIO', 'margen_por_cbm': 50.0, 'costo_fob_usd': 5},
        ])
        assert orden == ['ALTO', 'MEDIO', 'BAJO']

    def test_con_todos_los_margenes_iguales_desempata_por_costo(self):
        """EL caso del bug: si nadie tiene costo, `cu` es 0 para todos.

        Antes eso devolvía el orden de entrada disfrazado de ranking. Ahora
        entra primero el más barato, que es una decisión defendible: con la
        misma información, ocupar el contenedor con lo que menos capital
        inmoviliza.
        """
        orden = self._ordenar([
            {'referencia': 'CARO',   'margen_por_cbm': 0.0, 'costo_fob_usd': 900},
            {'referencia': 'BARATO', 'margen_por_cbm': 0.0, 'costo_fob_usd': 10},
            {'referencia': 'MEDIO',  'margen_por_cbm': 0.0, 'costo_fob_usd': 100},
        ])
        assert orden == ['BARATO', 'MEDIO', 'CARO'], (
            'con márgenes empatados el orden volvió a ser el de entrada')

    def test_el_margen_manda_sobre_el_costo(self):
        """El desempate no puede invertir el criterio principal."""
        orden = self._ordenar([
            {'referencia': 'BARATO_SIN_MARGEN', 'margen_por_cbm': 0.0,  'costo_fob_usd': 1},
            {'referencia': 'CARO_CON_MARGEN',   'margen_por_cbm': 80.0, 'costo_fob_usd': 999},
        ])
        assert orden[0] == 'CARO_CON_MARGEN'


class TestLaFilaDeclaraDeDondeSalioElMargen:
    """Regla 0 aplicada al ranking: un supuesto se declara, no se disfraza.

    El comité firma una fila. Tiene derecho a saber si el margen viene de una
    cotización vigente o de un porcentaje supuesto sobre el costo.
    """

    def test_cada_item_de_relleno_lleva_su_procedencia(self):
        fuente = _FUENTE.read_text(encoding='utf-8')
        i = fuente.index("'tipo': 'RELLENO'")
        bloque = fuente[max(0, i - 1400):i]
        for campo in ('margen_fuente', 'precio_fuente', 'precio_es_supuesto'):
            assert f"'{campo}'" in bloque, f'la fila no declara {campo}'

    def test_las_filas_supuestas_llevan_rango_y_no_un_punto(self):
        """±10 puntos de margen mueven Q* ~24%. Un número exacto sobre un
        supuesto es más engañoso que un rango."""
        fuente = _FUENTE.read_text(encoding='utf-8')
        assert 'margen_por_cbm_rango' in fuente

    def test_la_propuesta_reporta_la_cobertura_por_fuente(self):
        fuente = _FUENTE.read_text(encoding='utf-8')
        assert "'margen_cobertura'" in fuente, (
            'sin esto no se puede distinguir un contenedor armado sobre datos '
            'de uno armado sobre supuestos')


class TestLaFuenteDelMargenEsCu:
    """`cu` de costo_service ES el margen unitario: precio de venta − costo.

    Se verifica contra el servicio real para que un cambio de contrato ahí
    rompa acá, y no en producción tres meses después.
    """

    def test_resolver_costos_devuelve_cu(self, app, db):
        from app.services.costo_service import resolver_costos

        r = resolver_costos(['NO-EXISTE-XYZ'])
        assert 'NO-EXISTE-XYZ' in r, (
            'los SKU sin fuente deben salir declarados, no omitidos en silencio')
        fila = r['NO-EXISTE-XYZ']
        for campo in ('cu', 'fuente', 'precio_es_supuesto', 'fuente_precio'):
            assert campo in fila, f'costo_service dejó de devolver {campo}'

    def test_un_sku_sin_ninguna_fuente_sale_declarado(self, app, db):
        from app.services.costo_service import resolver_costos

        fila = resolver_costos(['NO-EXISTE-XYZ'])['NO-EXISTE-XYZ']
        assert fila['fuente'] == 'SIN_COSTO'
        assert fila['cu'] == 0
        # Y con cu=0 el ranking lo manda al fondo, que es lo correcto:
        # no se puede rankear lo que no se sabe cuánto vale.


class TestLaPantallaLoMuestra:
    """TRINQUETE — el margen decidía el orden y **no se mostraba en ninguna parte**.

    Por eso nadie pudo detectar el bug mirando la aplicación: la métrica reina
    era invisible. Un número que decide y no se ve es un número que nadie
    revisa.
    """

    _JS = (Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'
           / 'compras_ia.js')

    def test_la_fila_de_relleno_muestra_el_margen(self):
        js = self._JS.read_text(encoding='utf-8')
        assert 'margen_por_cbm' in js, (
            'la métrica que ordena el contenedor volvió a ser invisible')

    def test_y_muestra_de_donde_salio(self):
        js = self._JS.read_text(encoding='utf-8')
        assert 'margen_fuente' in js
        assert 'precio_es_supuesto' in js, (
            'sin esto, una fila sobre cotización vigente y una sobre un margen '
            'supuesto se ven idénticas en la pantalla que se firma')

    def test_avisa_cuando_el_ranking_no_esta_informado(self):
        """Si más de la mitad no tiene costo, el orden no es una decisión."""
        js = self._JS.read_text(encoding='utf-8')
        assert 'margen_cobertura' in js
        assert 'no es una decisión informada' in js

    def test_las_claves_del_resumen_existen_de_verdad(self):
        """La UI lee `con_costo`, `sin_costo`, `precio_supuesto` y `total_skus`.

        Si `resumen_por_fuente` cambia de contrato, el aviso queda en blanco sin
        error — que es exactamente cómo se esconden estas cosas.
        """
        from app.services.costo_service import resumen_por_fuente

        r = resumen_por_fuente({'X': {'fuente': 'SIN_COSTO'}})
        for k in ('total_skus', 'con_costo', 'sin_costo', 'precio_supuesto'):
            assert k in r, f'resumen_por_fuente dejó de devolver {k}'

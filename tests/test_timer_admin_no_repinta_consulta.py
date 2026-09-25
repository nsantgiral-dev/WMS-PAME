"""El refresco de 30 s del admin solo recarga las pestañas de datos vivos.

Clase del defecto (2026-09-25): una pestaña de consulta que el timer
`cargarAdmin(true)` recarga entera parpadea y pierde lo que el usuario tenía
abierto — sub-pestaña, detalles desplegados, filtros, texto a medio escribir.
Ya había pasado con Inventario → Estadísticas y con Layout; volvió con Compras.

Cada rama de `cargarAdmin` tiene que:
  · ignorar el timer (`if (!desdeTimer) …`), o
  · pasarle `desdeTimer` a su cargador (que decide qué es vivo), o
  · estar declarada en VIVAS con su motivo (la pantalla ES de datos en vivo).
Una pestaña nueva que no cumpla ninguna de las tres pone rojo este test.
"""
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa' / 'app.js'

#: Pestañas que el timer recarga a propósito, con su motivo. Solo encoge.
VIVAS = {
    'tab-dashboard': 'colas y KPIs del turno en vivo',
    'tab-pedidos': 'pedidos que entran de Siesa',
    'tab-requisiciones': 'solicitudes que llegan',
    'tab-bodega': 'tareas de bodega en curso',
    'tab-operarios': 'quién está trabajando ahora',
    'tab-usuarios': 'lista administrativa (histórico: se recargaba así)',
    'tab-stock': 'existencias que cambian',
    'tab-connekta': 'cola de Siesa y recuperación en vivo',
    'tab-muelle': 'bultos que se cargan',
    'tab-rutas': 'paradas que confirman los conductores',
    'tab-traslados': 'traslados en curso',
    'tab-reposicion': 'tareas de reposición en curso',
    'tab-liquidacion': 'rutas que llegan a liquidar',
}


def _ramas():
    s = APP.read_text(encoding='utf-8')
    i = s.index('async function cargarAdmin(')
    cuerpo = s[i:s.index('\n}\n', i)]
    return re.findall(r"TAB === '(tab-[a-z]+)'\)\s*(\{[^\n]*\}|[^\n]*;)", cuerpo)


def test_el_escaner_ve_las_ramas():
    ramas = dict(_ramas())
    assert len(ramas) >= 18, f'el escáner no ve las ramas de cargarAdmin: {sorted(ramas)}'
    assert 'tab-compras' in ramas and 'tab-analitica' in ramas


def test_ninguna_pestana_de_consulta_se_repinta_por_el_timer():
    malas = []
    for tab, codigo in _ramas():
        ignora_timer = '!desdeTimer' in codigo
        delega = re.search(r'\(\s*desdeTimer\s*\)', codigo) is not None
        if not (ignora_timer or delega or tab in VIVAS):
            malas.append(f'{tab}: {codigo.strip()}')
    assert not malas, ('Estas pestañas se recargan enteras cada 30 s sin ser de datos '
                       'vivos (parpadean y pierden lo abierto):\n  ' + '\n  '.join(malas))


def test_compras_carga_solo_al_entrar():
    assert "TAB === 'tab-compras') { if (!desdeTimer) await cargarCompras(); }" in APP.read_text(encoding='utf-8')


def test_vivas_no_tiene_entradas_muertas():
    ramas = dict(_ramas())
    muertas = [t for t in VIVAS if t not in ramas]
    assert not muertas, f'VIVAS declara pestañas que ya no existen: {muertas}'

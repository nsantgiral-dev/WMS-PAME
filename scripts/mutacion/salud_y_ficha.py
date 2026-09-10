"""¿Mueren las mutaciones sobre los doce campos pintados y la capacidad del tanque?"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

MUT = [
    ('M1 el PUT vuelve a ignorar la capacidad (defecto original)',
     'flota/api/ficha.py',
     "    'capacidad_tanque_galones', 'capacidad_tanque_fuente',\n)",
     ')',
     'tests/flota/test_vocabularios_completos.py'),
    # M2 REESCRITA el 2026-09-04. La versión anterior **no mutaba nada**:
    # anteponía un `def excede_capacidad_APAGADO(...)` y dejaba la función real
    # definida después, que es la que gana. Llevaba reportando `SOBREVIVIÓ`
    # sobre un cambio que nunca ocurrió — y eso se lee como «la suite no
    # detecta que el sobre-tanqueo se apague», una alarma falsa sobre un hueco
    # que no existía. Es el espejo de la podredumbre de ancla: una mutación que
    # no muta miente igual que un test que no corre.
    #
    # Y apuntaba a `test_vocabularios_completos.py`, que no ejerce esta
    # función. Ahora son dos mutaciones, cada una sobre una forma real de
    # apagar el detector, contra los tests que sí lo ejercen.
    ('M2a el sobre-tanqueo sin capacidad declarada sale LIMPIO en vez de sin_dato',
     'flota/dominio/costos.py',
     '    if capacidad_galones is None:\n        return SIN_DATO',
     '    if capacidad_galones is None:\n        return False',
     'tests/flota/test_costos.py tests/flota/test_gastos.py'),

    ('M2b el sobre-tanqueo deja pasar el que iguala la capacidad',
     'flota/dominio/costos.py',
     '    return Decimal(galones) > Decimal(capacidad_galones)',
     '    return Decimal(galones) > Decimal(capacidad_galones) * 2',
     'tests/flota/test_costos.py tests/flota/test_gastos.py'),
    ('M3 el tablero deja de decir que los datos son de QA',
     'app/static/pwa/flota.js',
     '  if (h.datos_reales === false) {',
     '  if (false) {',
     'tests/flota/test_render_salud_js.py'),
    ('M4 el cierre forzado vuelve a ser invisible',
     'app/static/pwa/flota.js',
     '  if (h.custodias_cerradas_forzadas > 0) {',
     '  if (false) {',
     'tests/flota/test_render_salud_js.py'),
    ('M5 la cobertura de fichas se pinta siempre, aunque esté completa',
     'app/static/pwa/flota.js',
     '  if (h.vehiculos_activos > 0 && h.fichas_completas < h.vehiculos_activos) {',
     '  if (h.vehiculos_activos > 0) {',
     'tests/flota/test_render_salud_js.py'),

    # ── El trinquete de campos mudos, ensanchado el 2026-09-04 ───────────
    #
    # Al partir la analítica en su propio archivo hizo falta que el detector
    # viera más de un `.js`. La opción cómoda —leer todo el PWA— habría dejado
    # `ambiente` satisfecho por `app.js`, sin ninguna pantalla de flota
    # leyéndolo: el trinquete verde, y un campo menos vigilado.
    #
    # Esta mutación es la que impide volver a esa opción por comodidad.
    ('M6 el glob de consumidores se ensancha a todo el PWA',
     'tests/flota/test_render_salud_js.py',
     "    _GLOB_CONSUMIDORES = 'flota*.js'",
     "    _GLOB_CONSUMIDORES = '*.js'",
     'tests/flota/test_render_salud_js.py'),

    # M7 SOBREVIVIÓ la primera vez que se corrió, y por eso existe
    # `test_la_exencion_es_por_campo_y_NO_por_archivo`: contra el repo real las
    # dos formulaciones dan el mismo resultado —la única colisión que existe es
    # justo la declarada—, así que la propiedad solo se puede ejercer con un
    # mundo sintético donde el mismo archivo traiga un campo declarado y otro
    # que no.
    ('M7 la exención de app.js se vuelve un pase libre al archivo entero',
     'tests/flota/test_render_salud_js.py',
     "            if c in texto and (a, c) not in self._COLISIONES_CONOCIDAS)",
     "            if c in texto and a != 'app.js')",
     'tests/flota/test_render_salud_js.py'),
]


def correr(objetivo):
    # `.split()` y no `[objetivo]`: una mutación cuya propiedad se prueba en dos
    # archivos necesita pasarle los dos a pytest. Con el argumento entero como
    # una sola ruta, `'a.py b.py'` es un archivo que no existe — pytest sale con
    # código 4 y el arnés lo reporta `SE SALTÓ`. Se descubrió así el 2026-09-04,
    # con el tercer estado haciendo exactamente su trabajo.
    r = subprocess.run(['venv/bin/python', '-m', 'pytest', *objetivo.split(),
                        '-q', '-p', 'no:randomly', '--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
                            'HOME': str(Path.home())})
    s = r.stdout or ''
    cola = s.strip().split('\n')[-1] if s.strip() else ''
    # Los tres estados, y el tercero se decide por lo que REALMENTE corrió.
    #
    # La primera versión daba «no se juzgó» ante cualquier `skipped` en la
    # salida, y eso es demasiado grueso: un archivo con dos tests saltados por
    # una razón legítima —los hay— no se podría juzgar nunca, y la mutación
    # figuraría como no evaluada para siempre. El arnés que persigue verdes
    # falsos produciría un «no sé» falso, que es el mismo error con otro signo.
    #
    # La regla correcta mira si hubo veredicto: si algo falló, la mutación
    # murió; si algo pasó y nada falló, sobrevivió; si no corrió nada, no se
    # juzgó.
    if 'failed' in cola:
        return 1, cola
    if 'no tests ran' in cola or 'error' in cola.lower() or 'passed' not in cola:
        return 0, 'NO SE JUZGÓ: ' + cola
    return 0, cola


fallos = []
for etiqueta, archivo, viejo, nuevo, objetivo in MUT:
    ruta = RAIZ / archivo
    respaldo = ruta.read_text()
    if viejo not in respaldo:
        print(f'  ⚠ {etiqueta}: el texto a mutar NO existe'); fallos.append(etiqueta); continue
    ruta.write_text(respaldo.replace(viejo, nuevo, 1))
    try:
        cod, cola = correr(objetivo)
    finally:
        ruta.write_text(respaldo)
    if cod == 0:
        print(f'  ✗ {"SE SALTÓ  " if cola.startswith("NO SE") else "SOBREVIVIÓ"}  {etiqueta}')
        fallos.append(etiqueta)
    else:
        print(f'  ✓ muerta      {etiqueta}')

print()
print(f'MUTACIONES MUERTAS: {len(MUT) - len(fallos)}/{len(MUT)}')
sys.exit(1 if fallos else 0)

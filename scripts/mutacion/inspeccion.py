#!/usr/bin/env python3
"""Arnés de mutación de la inspección diaria — rompe una propiedad por vez y
exige que la suite se ponga ROJA.

## Por qué no alcanza con que los tests pasen

Un test verde no dice que el invariante esté protegido: dice que hoy nadie lo
violó. La única forma de saber si el guard mira es romper lo que promete cuidar
y ver si grita. Este repo tiene seis casos fechados de guards en verde sobre
propiedades que la vía sana satisfacía por construcción.

## Y un test SALTADO sale con exit 0 igual que uno que pasó

`pytest` devuelve 0 con `skipped`, con `no tests ran` y con `deselected`. Un
arnés que mire solo el `returncode` declararía «mutación muerta» sobre una
corrida que no juzgó nada — que es exactamente el verde falso que este arnés
existe para cazar. Por eso se parsea la línea de resumen y se exige que haya
`failed`, con al menos un test efectivamente ejecutado.

También se pasa el entorno REAL al subproceso: sin `PATH`, `shutil.which('node')`
devuelve `None`, los tests de pantalla se saltan solos y la mutación del
formulario «sobreviviría» sin que nadie la haya juzgado.

    python3 mutar_inspeccion.py            # todas
    python3 mutar_inspeccion.py 3 7        # solo esas
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PYTEST = [str(RAIZ / 'venv' / 'bin' / 'python'), '-m', 'pytest', '-q',
          '-p', 'no:randomly', '-m', 'not postgres', '--no-header']

ADAPTADOR = 'flota/adaptadores/inspecciones.py'
DOMINIO = 'flota/dominio/inspeccion.py'
API = 'flota/api/inspecciones.py'
MEDICION = 'flota/adaptadores/medicion.py'
HEALTH = 'flota/api/health.py'
JS = 'app/static/pwa/flota.js'

T_ADAPTADOR = ['tests/flota/test_inspeccion_del_dia.py']
T_API = ['tests/flota/test_endpoints_inspeccion.py']
T_JS = ['tests/flota/test_pantalla_inspeccion_js.py']
T_SALUD = ['tests/flota/test_render_salud_js.py']

MUTACIONES = [
    # ── Regla 1: el hueco no puede volverse un dato ──────────────────────
    dict(
        nombre='el ítem no respondido entra como `optimo` en vez de `sin_dato`',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""            respuesta = dicho['respuesta'] if dicho else str(dom.SIN_DATO)""",
                  """            respuesta = dicho['respuesta'] if dicho else dom.OPTIMO""")],
    ),
    dict(
        nombre='el veredicto ignora los huecos y devuelve `apto`',
        archivo=DOMINIO, tests=T_ADAPTADOR,
        cambios=[("""    if c.items_sin_dato > 0:
        return INCOMPLETA""",
                  """    if False:
        return INCOMPLETA""")],
    ),
    dict(
        nombre='`incompleta` colapsa en `no_apto` («total, ninguna autoriza»)',
        archivo=DOMINIO, tests=T_ADAPTADOR,
        cambios=[("""    if c.items_sin_dato > 0:
        return INCOMPLETA""",
                  """    if c.items_sin_dato > 0:
        return NO_APTO""")],
    ),
    dict(
        nombre='el veredicto se calcula solo sobre los ítems que vinieron',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""        for i in esperados
    ]""",
                  """        for i in esperados if i.id in por_item
    ]""")],
    ),
    dict(
        nombre='`incompleta` habilita despacho',
        archivo=DOMINIO, tests=T_ADAPTADOR,
        cambios=[("""    return veredicto_ == APTO""",
                  """    return veredicto_ != NO_APTO""")],
    ),
    # ── Regla 11: el orden y el reloj ────────────────────────────────────
    dict(
        nombre='el barajado deja de depender del día (orden fijo para siempre)',
        archivo=DOMINIO, tests=T_ADAPTADOR,
        cambios=[("""        semilla = f'{dia.isoformat()}|{item.orden}|{item.nombre}'.encode()""",
                  """        semilla = f'FIJO|{item.orden}|{item.nombre}'.encode()""")],
    ),
    dict(
        nombre='el barajado deja de ser reproducible (semilla al azar)',
        archivo=DOMINIO, tests=T_ADAPTADOR,
        cambios=[("""        semilla = f'{dia.isoformat()}|{item.orden}|{item.nombre}'.encode()""",
                  """        import random
        semilla = f'{random.random()}|{item.orden}|{item.nombre}'.encode()""")],
    ),
    dict(
        nombre='`segundos_llenado` se guarda siempre en 0',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""            segundos_llenado=segundos,""",
                  """            segundos_llenado=0,""")],
    ),
    dict(
        nombre='la frontera rellena `segundos_llenado` con 0 si no viene',
        archivo=API, tests=T_API,
        cambios=[("""    faltantes = [c for c in ('placa', 'km', 'respuestas', 'segundos_llenado')
                 if c not in datos]""",
                  """    faltantes = [c for c in ('placa', 'km', 'respuestas')
                 if c not in datos]
    if 'segundos_llenado' not in datos:
        datos['segundos_llenado'] = 0"""),
                 ],
    ),
    dict(
        # Son DOS cambios porque la propiedad la sostienen dos líneas: el
        # `orden` que pone el servidor y la lista blanca de campos de
        # `_validar_respuestas`. Mutar solo la primera es un no-op —el campo
        # nunca llega— y un no-op que «sobrevive» se lee como un hueco de los
        # tests cuando en realidad es una mutación mal escrita.
        nombre='`orden_mostrado` lo elige el cliente',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""                orden_mostrado=orden,""",
                  """                orden_mostrado=(dicho['orden_mostrado']
                                if dicho and 'orden_mostrado' in dicho
                                else orden),"""),
                 ("""        por_item[item_id] = {
            'respuesta': str(respuesta),
            'nota': (nota or '').strip() or None,
        }""",
                  """        por_item[item_id] = dict(
            cruda, respuesta=str(respuesta),
            nota=(nota or '').strip() or None)""")],
    ),
    # ── El puente al hallazgo ────────────────────────────────────────────
    dict(
        nombre='un `no_apto` deja de hacer nacer su hallazgo',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""            if respuesta == dom.NO_APTO:""",
                  """            if False:""")],
    ),
    dict(
        nombre='también nace hallazgo sobre `sin_dato` («no sé» = «está mal»)',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""            if respuesta == dom.NO_APTO:""",
                  """            if respuesta != dom.OPTIMO:""")],
    ),
    dict(
        nombre='el hallazgo se guarda solo (`commit=True`): la transacción deja '
               'de ser del llamador',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""                    commit=False,""", """                    commit=True,""")],
    ),
    dict(
        nombre='la respuesta deja de anotar de qué hallazgo salió',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""                hallazgo_id=hallazgo_id,""", """                hallazgo_id=None,"""), ],
    ),
    # ── Regla 3 y regla 5 del WMS ────────────────────────────────────────
    dict(
        nombre='el origen de la lectura miente (dice `hallazgo`)',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""            origen=OrigenLectura.PREOPERACIONAL)""",
                  """            origen=OrigenLectura.HALLAZGO)""")],
    ),
    dict(
        nombre='el día se calcula en UTC y no en Bogotá',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()""",
                  """    return ts.date()""")],
    ),
    dict(
        nombre='se escribe la inspección aunque nada valide las respuestas',
        archivo=ADAPTADOR, tests=T_ADAPTADOR,
        cambios=[("""        if item_id in por_item:
            raise InspeccionInvalida(""",
                  """        if False:
            raise InspeccionInvalida(""")],
    ),
    # ── Permisos ─────────────────────────────────────────────────────────
    dict(
        nombre='el conductor deja de poder registrar su propia inspección',
        archivo=API, tests=T_API,
        cambios=[("""@exige(Roles.LECTURA_FLOTA, 'registrar la inspección preoperacional')""",
                  """@exige(MAESTROS_FLOTA, 'registrar la inspección preoperacional')"""),
                 ("""from flota.api._permisos import exige""",
                  """from flota.api._permisos import MAESTROS_FLOTA, exige""")],
    ),
    dict(
        nombre='cualquiera con sesión registra una inspección',
        archivo=API, tests=T_API,
        cambios=[("""@exige(Roles.LECTURA_FLOTA, 'registrar la inspección preoperacional')\n""",
                  """""")],
    ),
    # ── El health ────────────────────────────────────────────────────────
    dict(
        nombre='`vehiculos_sin_inspeccion_hoy` devuelve una constante plausible',
        archivo=MEDICION, tests=T_API,
        cambios=[("""        inspeccionados = {""",
                  """        return 0
        inspeccionados = {""")],
    ),
    dict(
        nombre='las incompletas se cuentan junto con todas las inspecciones',
        archivo=MEDICION, tests=T_API,
        cambios=[("""            Inspeccion.veredicto == INCOMPLETA,\n""", """""")],
    ),
    dict(
        nombre='`segundos_llenado_30d` deja de publicar el mínimo',
        archivo=MEDICION, tests=T_API,
        cambios=[("""        segundos = sorted(f.segundos_llenado for f in filas)""",
                  """        return {'n': 0, 'minimo': None, 'mediana': None, 'nota': 'x'}
        segundos = sorted(f.segundos_llenado for f in filas)""")],
    ),
    dict(
        nombre='el campo desaparece de la respuesta del health',
        archivo=HEALTH, tests=T_API,
        cambios=[("""    'vehiculos_sin_inspeccion_hoy',\n""", """""")],
    ),
    # ── La pantalla ──────────────────────────────────────────────────────
    dict(
        nombre='el botón «Bien» nace marcado (el default optimista)',
        archivo=JS, tests=T_JS,
        cambios=[("""      <button class="btn-flota ${r === 'optimo' ? 'ok' : ''}" style="flex:1\"""",
                  """      <button class="btn-flota ok" style="flex:1\"""")],
    ),
    dict(
        nombre='lo no contestado viaja como `optimo` en vez de no viajar',
        archivo=JS, tests=T_JS,
        cambios=[("""    respuestas: items
      .filter(i => FLOTA_INSP_RESP[i.item_id])
      .map(i => ({
        item_id: i.item_id,
        respuesta: FLOTA_INSP_RESP[i.item_id],""",
                  """    respuestas: items
      .map(i => ({
        item_id: i.item_id,
        respuesta: FLOTA_INSP_RESP[i.item_id] || 'optimo',""")],
    ),
    dict(
        nombre='el gesto deja de pintarse (queda solo el nombre del ítem)',
        archivo=JS, tests=T_JS,
        cambios=[("""      <div style="font-size:13px;color:var(--tx2);margin:2px 0 6px">${i.gesto}</div>""",
                  """      <div style="font-size:13px;color:var(--tx2);margin:2px 0 6px"></div>""")],
    ),
    dict(
        nombre='el formulario deja de sellar su placa',
        archivo=JS, tests=T_JS,
        cambios=[("""            id="insp-guardar" data-placa="${FLOTA_PLACA}">Terminar inspección</button>""",
                  """            id="insp-guardar">Terminar inspección</button>""")],
    ),
    dict(
        nombre='el tablero deja de mostrar los camiones sin inspección de hoy',
        archivo=JS, tests=T_SALUD,
        cambios=[("""  if (h.vehiculos_sin_inspeccion_hoy > 0) {""", """  if (false) {""")],
    ),
    dict(
        nombre='el tablero pinta el tiempo de llenado como si fuera una alarma '
               'con umbral',
        archivo=JS, tests=T_SALUD,
        cambios=[("""      'Es un dato, no una alarma: todavía no hay un piso medido con qué ' +
      'compararlo. Se publica para poder fijarlo con dato en vez de a ojo.'""",
                  """      'Por debajo de 60 segundos la inspección no es confiable.'""")],
    ),
]


_RESUMEN = re.compile(
    r'(?:(?P<failed>\d+) failed)?[, ]*(?:(?P<passed>\d+) passed)?'
    r'[, ]*(?:(?P<skipped>\d+) skipped)?[, ]*(?:(?P<errors>\d+) errors?)?')


def _juzgar(salida: str) -> dict:
    """Qué pasó DE VERDAD, leyendo el resumen — no el `returncode`.

    `pytest` sale con 0 cuando todo se saltó, cuando no se recolectó nada y
    cuando todo quedó deseleccionado. Las tres cosas se ven igual desde afuera y
    ninguna juzgó la mutación.
    """
    ultima = ''
    for linea in salida.strip().splitlines():
        if re.search(r'\d+ (passed|failed|skipped|error|deselected)', linea):
            ultima = linea
    if not ultima or 'no tests ran' in salida:
        return {'juzgo': False, 'failed': 0, 'passed': 0, 'linea': ultima or '(sin resumen)'}
    m = _RESUMEN.search(ultima)
    failed = int(m.group('failed') or 0)
    passed = int(m.group('passed') or 0)
    skipped = int(m.group('skipped') or 0)
    errores = int(m.group('errors') or 0)
    # «Juzgó» = corrió al menos un test de verdad. Un archivo entero saltado
    # (node ausente, import roto) no juzga nada aunque el returncode sea 0.
    return {'juzgo': (failed + passed) > 0, 'failed': failed, 'passed': passed,
            'skipped': skipped, 'errores': errores, 'linea': ultima}


def _correr(tests):
    proc = subprocess.run(
        PYTEST + tests, cwd=str(RAIZ), capture_output=True, text=True,
        # El entorno REAL: sin PATH no hay `node` y los tests de pantalla se
        # saltan solos. Un skip sale con exit 0 igual que un pass.
        env={**os.environ, 'TZ': 'UTC'}, timeout=900)
    return _juzgar(proc.stdout + proc.stderr)


def main():
    pedidas = {int(a) for a in sys.argv[1:]} or None

    print('── CONTROL: sin mutar, todo tiene que estar VERDE ' + '─' * 25)
    todos = sorted({t for m in MUTACIONES for t in m['tests']})
    control = _correr(todos)
    if not control['juzgo'] or control['failed']:
        print(f'  ✗ el control no está limpio: {control["linea"]}')
        print('    Sin control verde, «rojo tras mutar» no significa nada.')
        return 1
    print(f'  ✓ {control["linea"]}\n')

    muertas, vivas, sin_juzgar = [], [], []
    for i, mut in enumerate(MUTACIONES, 1):
        if pedidas and i not in pedidas:
            continue
        ruta = RAIZ / mut['archivo']
        original = ruta.read_text(encoding='utf-8')
        mutado = original
        for viejo, nuevo in mut['cambios']:
            if mutado.count(viejo) != 1:
                print(f'{i:2}. ⚠ NO APLICABLE — el texto a mutar aparece '
                      f'{mutado.count(viejo)} veces en {mut["archivo"]}')
                sin_juzgar.append((i, mut['nombre'], 'texto no encontrado'))
                mutado = None
                break
            mutado = mutado.replace(viejo, nuevo)
        if mutado is None:
            continue

        ruta.write_text(mutado, encoding='utf-8')
        try:
            r = _correr(mut['tests'])
        finally:
            ruta.write_text(original, encoding='utf-8')

        if not r['juzgo']:
            print(f'{i:2}. ⚠ NO SE JUZGÓ — {mut["nombre"]}\n      {r["linea"]}')
            sin_juzgar.append((i, mut['nombre'], r['linea']))
        elif r['failed'] > 0:
            print(f'{i:2}. ✓ MUERTA ({r["failed"]} rojo) — {mut["nombre"]}')
            muertas.append(i)
        else:
            print(f'{i:2}. ✗ SOBREVIVIÓ — {mut["nombre"]}\n      {r["linea"]}')
            vivas.append((i, mut['nombre']))

    total = len(muertas) + len(vivas) + len(sin_juzgar)
    print('\n' + '─' * 72)
    print(f'MUTACIONES MUERTAS: {len(muertas)}/{total}')
    for i, nombre in vivas:
        print(f'  ✗ sobrevivió {i}: {nombre}')
    for i, nombre, detalle in sin_juzgar:
        print(f'  ⚠ no se juzgó {i}: {nombre} — {detalle}')
    return 0 if (not vivas and not sin_juzgar) else 1


if __name__ == '__main__':
    sys.exit(main())

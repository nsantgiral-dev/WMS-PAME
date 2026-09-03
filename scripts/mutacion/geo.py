#!/usr/bin/env python3
"""Arnés de mutación del geocodificador — rompe una propiedad por vez y exige
que la suite se ponga ROJA.

## Por qué no alcanza con que los tests pasen

Un test verde no dice que el invariante esté protegido: dice que hoy nadie lo
violó. Este repo tiene seis casos fechados de guards en verde sobre propiedades
que la vía sana satisfacía por construcción. La única forma de saber si el
guard mira es romper lo que promete cuidar y ver si grita.

## Y un test SALTADO sale con exit 0 igual que uno que pasó

`pytest` devuelve 0 con `skipped`, con `no tests ran` y con `deselected`. Un
arnés que mire solo el `returncode` declararía «mutación muerta» sobre una
corrida que no juzgó nada — que es exactamente el verde falso que este arnés
existe para cazar. Por eso se parsea la línea de resumen y se exige que haya
`failed` con al menos un test efectivamente ejecutado.

También se pasa el entorno REAL al subproceso: sin `PATH`, `shutil.which('node')`
devuelve `None`, los 7 tests de pantalla se saltan solos y las mutaciones del
botón de navegación «sobrevivirían» sin que nadie las haya juzgado.

    python3 mutar_geo.py            # todas
    python3 mutar_geo.py 3 7        # solo esas
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PYTEST = [str(RAIZ / 'venv' / 'bin' / 'python'), '-m', 'pytest', '-q',
          '-p', 'no:randomly', '-m', 'not postgres', '--no-header']

POLITICA = 'app/services/geo_cliente.py'
MODELO = 'app/models/geo_entrega.py'
SERVICIO = 'app/services/ruta_service.py'
JS = 'app/static/pwa/rutas.js'

T_GEO = ['tests/test_geo_cliente.py']
T_JS = ['tests/test_navegacion_geo_js.py']

MUTACIONES = [
    # ══ Regla 4 — la ausencia NO es 0,0 ═════════════════════════════════════
    dict(
        nombre='0,0 y los ejes cambiados pasan como coordenada válida',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX""",
                  """    return -90 <= lat <= 90 and -180 <= lon <= 180""")],
    ),
    dict(
        nombre='un GPS negado se guarda como la coordenada 0,0',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        return Captura(None, None, None, SIN_DATO, _motivo_declarado(geo))""",
                  """        return Captura(0.0, 0.0, None, GPS_CONDUCTOR, None)""")],
    ),
    dict(
        nombre='«no vino nada» se guarda igual que «se preguntó y no se pudo»',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if not isinstance(geo, dict):
        return None""",
                  """    if not isinstance(geo, dict):
        return Captura(None, None, None, SIN_DATO, NO_DECLARADO)""")],
    ),
    dict(
        nombre='un «sin dato» sin motivo queda en NULL (ausencia muda)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return m if m in MOTIVOS_SIN_DATO else NO_DECLARADO""",
                  """    return m if m in MOTIVOS_SIN_DATO else None""")],
    ),
    dict(
        nombre='`lat: true` entra como latitud 1.0 (isinstance(True, int))',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if v is None or isinstance(v, bool):
        return None""",
                  """    if v is None:
        return None""")],
    ),
    dict(
        nombre='NaN e infinito pasan como coordenada',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return None if math.isnan(f) or math.isinf(f) else f""",
                  """    return f""")],
    ),
    dict(
        nombre='el cliente elige su propia procedencia (`corregida_a_mano` a dedo)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    fuente = fuente if fuente in (GPS_CONDUCTOR, CORREGIDA_A_MANO) else GPS_CONDUCTOR""",
                  """    fuente = fuente or GPS_CONDUCTOR""")],
    ),
    dict(
        nombre='precisión 0 se lee como precisión perfecta',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if precision is not None and precision <= 0:""",
                  """    if False:""")],
    ),

    # ══ El CHECK de la base — el detector que no pasa por el código ═════════
    dict(
        nombre='la base deja de exigir coordenada-con-procedencia / motivo-con-ausencia',
        archivo=MODELO, tests=T_GEO,
        cambios=[("""            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato' "
            "     AND motivo_sin_dato IS NULL) "
            "OR (lat IS NULL AND lon IS NULL AND fuente = 'sin_dato' "
            "     AND motivo_sin_dato IS NOT NULL)",
            name='ck_entrega_geo_coordenada_o_motivo'),""",
                  """            "1 = 1",
            name='ck_entrega_geo_coordenada_o_motivo'),""")],
    ),
    dict(
        nombre='la caja de Colombia se abre al mundo entero (0,0 entra)',
        archivo=MODELO, tests=T_GEO,
        cambios=[("""LAT_MIN, LAT_MAX = -4.5, 13.7
LON_MIN, LON_MAX = -82.2, -66.5""",
                  """LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0""")],
    ),
    dict(
        # El gemelo del anterior: un CHECK que rechaza TODO también «pasa» el
        # test de que 0,0 no entra. Sin esta mutación, ese test no distingue
        # un rango correcto de uno imposible.
        nombre='la caja de Colombia rechaza toda coordenada (CHECK imposible)',
        archivo=MODELO, tests=T_GEO,
        cambios=[("""LAT_MIN, LAT_MAX = -4.5, 13.7""",
                  """LAT_MIN, LAT_MAX = 40.0, 41.0""")],
    ),
    dict(
        nombre='se puede guardar un maestro sin decir por qué no tiene punto',
        archivo=MODELO, tests=T_GEO,
        cambios=[("""            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato' "
            "     AND motivo_sin_maestro IS NULL) "
            "OR (lat IS NULL AND lon IS NULL AND fuente = 'sin_dato' "
            "     AND motivo_sin_maestro IS NOT NULL)",
            name='ck_cliente_geo_coordenada_o_motivo'),""",
                  """            "1 = 1",
            name='ck_cliente_geo_coordenada_o_motivo'),""")],
    ),

    # ══ La política del maestro ═════════════════════════════════════════════
    dict(
        nombre='la mediana se vuelve el máximo (un outlier mueve el maestro)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return orden[medio] if n % 2 else (orden[medio - 1] + orden[medio]) / 2.0""",
                  """    return orden[-1]""")],
    ),
    dict(
        nombre='la mediana se vuelve «la más reciente» (la última captura manda)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    lat_m = mediana([float(c.lat) for c in votantes])
    lon_m = mediana([float(c.lon) for c in votantes])""",
                  """    lat_m = float(votantes[-1].lat)
    lon_m = float(votantes[-1].lon)""")],
    ),
    dict(
        nombre='vota cualquier precisión, por mala que sea',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""                and float(c.precision_m) <= PRECISION_MAXIMA_M]""",
                  """                and float(c.precision_m) <= 1e9]""")],
    ),
    dict(
        nombre='una captura sin precisión conocida vota igual',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""                and c.precision_m is not None
                and float(c.precision_m) <= PRECISION_MAXIMA_M]""",
                  """                and (c.precision_m is None
                     or float(c.precision_m) <= PRECISION_MAXIMA_M)]""")],
    ),
    dict(
        nombre='«nunca se capturó» y «se capturó y no sirve» colapsan en un motivo',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        motivo = PRECISION_INSUFICIENTE if con_punto else SIN_CAPTURAS""",
                  """        motivo = SIN_CAPTURAS""")],
    ),
    dict(
        nombre='dos clusters distintos eligen el punto del medio',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if len(lejanas) * 2 >= len(votantes):""", """    if False:""")],
    ),
    dict(
        nombre='el guard de dispersión dispara sobre un cluster sano',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""RADIO_COHERENCIA_M = 500.0""", """RADIO_COHERENCIA_M = 0.001""")],
    ),
    dict(
        nombre='la corrección a mano deja de ganarle al GPS',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if manuales:""", """    if False:""")],
    ),
    dict(
        nombre='entre dos correcciones a mano gana la más vieja',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        elegida = max(manuales,""", """        elegida = min(manuales,""")],
    ),
    dict(
        nombre='las capturas descartadas desaparecen del conteo',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        descartadas=len(con_punto) - len(votantes) + len(lejanas))""",
                  """        descartadas=0)""")],
    ),
    dict(
        nombre='la distancia devuelve siempre 0 (nada queda nunca lejos)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return 2 * RADIO_TIERRA_M * math.asin(min(1.0, math.sqrt(a)))""",
                  """    return 0.0""")],
    ),
    dict(
        nombre='la distancia sale en radianes (el umbral pierde escala)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return 2 * RADIO_TIERRA_M * math.asin(min(1.0, math.sqrt(a)))""",
                  """    return 2 * math.asin(min(1.0, math.sqrt(a)))""")],
    ),

    # ══ La clave del cliente ════════════════════════════════════════════════
    dict(
        nombre='el municipio sale de la clave (dos tiendas homónimas colisionan)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    return f'{nombre}|{_normalizar(municipio)}'[:220]""",
                  """    return nombre[:220]""")],
    ),
    dict(
        nombre='la clave deja de normalizar tildes y caja',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    s = unicodedata.normalize('NFKD', s)""", """    return s""")],
    ),
    dict(
        nombre='un cliente sin razón social igual arma una clave',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if not nombre:
        return None""",
                  """    if not nombre:
        nombre = 'SIN_NOMBRE'""")],
    ),

    # ══ La escritura y la entrega ═══════════════════════════════════════════
    dict(
        nombre='re-confirmar pisa la coordenada (el maestro camina hacia el CD)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if fila is not None and fila.lat is not None:
        return 'ya_capturada'""",
                  """    if False:
        return 'ya_capturada'""")],
    ),
    dict(
        nombre='una fila `sin_dato` ya no se puede completar nunca',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    if fila is not None and fila.lat is not None:""",
                  """    if fila is not None:""")],
    ),
    dict(
        nombre='la captura entra en la MISMA transacción que la entrega',
        archivo=SERVICIO, tests=T_GEO,
        cambios=[("""        db.session.commit()

        # ── La coordenada, DESPUÉS del commit y en su propia transacción ────""",
                  """        # ── La coordenada, ANTES del commit — mutación ────"""),
                 ("""        except Exception as _e_geo:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning('[GEO] no se pudo registrar la captura de la parada '
                           '%s/%s: %s', ruta_id, tarea_id, _e_geo)

        return recaudo.id, es_edicion""",
                  """        except Exception:
            raise

        db.session.commit()
        return recaudo.id, es_edicion""")],
    ),
    dict(
        nombre='el maestro no se recalcula al llegar una captura nueva',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    recalcular_maestro(clave, cliente=cliente, municipio=municipio, ahora=ahora)""",
                  """    pass""")],
    ),
    dict(
        nombre='la pantalla del conductor deja de recibir el punto',
        archivo=SERVICIO, tests=T_GEO,
        cambios=[("""            p['geo'] = _m.to_dict() if _m is not None else None""",
                  """            p['geo'] = None""")],
    ),
    dict(
        nombre='«nadie capturó» y «se capturó y no se pudo elegir» llegan iguales',
        archivo=SERVICIO, tests=T_GEO,
        cambios=[("""            p['geo'] = _m.to_dict() if _m is not None else None""",
                  """            p['geo'] = (_m.to_dict()
                        if _m is not None and _m.lat is not None else None)""")],
    ),

    # ══ La medida ═══════════════════════════════════════════════════════════
    dict(
        nombre='la cobertura cuenta como «con coordenada» a todos los maestros',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    con_coordenada = [m for m in maestros
                      if m.lat is not None and m.cliente_clave in visitados]""",
                  """    con_coordenada = [m for m in maestros
                      if m.cliente_clave in visitados]""")],
    ),
    dict(
        nombre='la cobertura deja de contar a los que NO tienen',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""    sin_coordenada = [m for m in maestros
                      if m.lat is None and m.cliente_clave in visitados]""",
                  """    sin_coordenada = []""")],
    ),
    dict(
        nombre='los motivos de las capturas sin punto no se cuentan',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        if c.motivo_sin_dato:""", """        if False:""")],
    ),
    dict(
        nombre='«con una sola captura» se esconde (180 puntos parecen verificados)',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        'con_una_sola_captura': sum(1 for m in con_coordenada
                                    if (m.capturas_consideradas or 0) <= 1),""",
                  """        'con_una_sola_captura': 0,""")],
    ),
    dict(
        nombre='el umbral de ruteo deja de viajar con el número',
        archivo=POLITICA, tests=T_GEO,
        cambios=[("""        'umbral_para_rutear': UMBRAL_PARA_RUTEAR,""",
                  """        'umbral_para_rutear': None,""")],
    ),
    dict(
        nombre='la cobertura la puede leer cualquiera con sesión',
        archivo='app/routes/rutas.py', tests=T_GEO,
        cambios=[("""    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver la cobertura'}), 403""",
                  """    if False:
        return jsonify({'error': 'Solo admin o jefe puede ver la cobertura'}), 403""")],
    ),

    # ══ La pantalla — el botón que NO se pinta ══════════════════════════════
    dict(
        nombre='se pinta Waze aunque no haya coordenada (centro del municipio)',
        archivo=JS, tests=T_JS,
        cambios=[("""  if (!g) {
    return aviso('📍 <b style="color:#d1d5db;">No sabemos dónde queda.</b><br>Tocá «Estoy aquí» al confirmar y la próxima ruta ya lo va a tener.');
  }""",
                  """  if (!g) {
    return `<div style="margin-top:12px;"><a href="https://waze.com/ul?q=${encodeURIComponent(p.municipio)}&navigate=yes">🧭 Waze</a></div>`;
  }""")],
    ),
    dict(
        nombre='un maestro sin punto pinta el botón igual',
        archivo=JS, tests=T_JS,
        cambios=[("""  if (g.lat == null || g.lon == null) {""", """  if (false) {""")],
    ),
    dict(
        nombre='todos los motivos de «no se sabe» dicen lo mismo',
        archivo=JS, tests=T_JS,
        cambios=[("""    const razon = motivos[g.motivo_sin_maestro] || 'no se pudo determinar';""",
                  """    const razon = 'no se pudo determinar';""")],
    ),
    dict(
        nombre='el punto se pinta sin decir cuántas visitas lo sostienen',
        archivo=JS, tests=T_JS,
        cambios=[("""      Punto de ${n} visita${n !== 1 ? 's' : ''}${prec} · lo pusieron los conductores""",
                  """      Ubicación del cliente""")],
    ),
    dict(
        nombre='el deep link pierde la longitud (Waze abre en el ecuador)',
        archivo=JS, tests=T_JS,
        cambios=[("""  const ll = `${g.lat},${g.lon}`;""", """  const ll = `${g.lat},0`;""")],
    ),
]


_RESUMEN = re.compile(
    r'(?:(?P<failed>\d+) failed)?[, ]*(?:(?P<passed>\d+) passed)?'
    r'[, ]*(?:(?P<skipped>\d+) skipped)?[, ]*(?:(?P<errors>\d+) errors?)?')


def _juzgar(salida: str) -> dict:
    """Qué pasó DE VERDAD, leyendo el resumen — no el `returncode`.

    `pytest` sale con 0 cuando todo se saltó, cuando no se recolectó nada y
    cuando todo quedó deseleccionado. Las tres cosas se ven igual desde afuera
    y ninguna juzgó la mutación.
    """
    ultima = ''
    for linea in salida.strip().splitlines():
        if re.search(r'\d+ (passed|failed|skipped|error|deselected)', linea):
            ultima = linea
    if not ultima or 'no tests ran' in salida:
        return {'juzgo': False, 'failed': 0, 'passed': 0,
                'linea': ultima or '(sin resumen)'}
    m = _RESUMEN.search(ultima)
    failed = int(m.group('failed') or 0)
    passed = int(m.group('passed') or 0)
    skipped = int(m.group('skipped') or 0)
    errores = int(m.group('errors') or 0)
    # «Juzgó» = corrió al menos un test de verdad. Un archivo entero saltado
    # (node ausente, import roto) no juzga nada aunque el returncode sea 0.
    return {'juzgo': (failed + passed + errores) > 0, 'failed': failed + errores,
            'passed': passed, 'skipped': skipped, 'linea': ultima}


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
    if control['skipped']:
        print(f'  ⚠ {control["skipped"]} test(s) SALTADOS en el control — las '
              f'mutaciones que dependan de ellos no se van a juzgar.')
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

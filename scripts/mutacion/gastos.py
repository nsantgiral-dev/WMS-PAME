#!/usr/bin/env python3
"""
Arnés de mutación de la fase «la plata que sale» (2026-09-02).

Rompe UNA propiedad a la vez y exige que la suite se ponga en rojo. Un test que
nunca puede fallar no es un test: es una línea que se ejecuta.

## Lo que este arnés hace y que el returncode no alcanza a decir

**Un test saltado sale con exit 0 igual que uno que pasó.** El arnés del hallazgo
(2026-09-01) reportó «sobrevivió» sobre tests que se estaban SALTANDO: `node` no
estaba en el PATH recortado del subproceso y los render tests hacían
`pytest.skip`. Un skip en verde, producido por el propio arnés que persigue
skips en verde.

Por eso acá:

1. se le pasa el **PATH real** al subproceso (`os.environ` completo);
2. se **parsea la línea de resumen**, no solo el returncode: una mutación cuyo
   test se saltó o cuya recolección falló se reporta `NO SE JUZGÓ`, que no es
   lo mismo que `SOBREVIVIÓ` ni que `MUERTA`;
3. se exige que la corrida **base** esté verde antes de mutar nada. Sobre una
   suite ya roja, toda mutación se ve muerta.

Uso:  venv/bin/python <este archivo>
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

DOM = 'flota/dominio/costos.py'
ADA = 'flota/adaptadores/gastos.py'
MED = 'flota/adaptadores/medicion.py'
API = 'flota/api/gastos.py'
JS = 'app/static/pwa/flota.js'

T_COSTOS = 'tests/flota/test_costos.py'
T_GASTOS = 'tests/flota/test_gastos.py'
T_RENDER = 'tests/flota/test_render_gastos_js.py'
T_TRINQ = 'tests/flota/test_trinquetes_flota.py'
T_USO = 'tests/flota/test_uso_real_20260805.py'

#: (nombre, archivo, viejo, nuevo, tests)
#:
#: Cada una rompe UNA propiedad. El nombre dice qué propiedad, para que el
#: informe se pueda leer sin abrir el archivo.
MUTACIONES = [
    # ── El dominio ───────────────────────────────────────────────────────
    ('exige_periodo degrada una categoría desconocida en vez de reventar',
     DOM,
     "    if categoria not in CATEGORIAS_GASTO:\n        raise ValueError(",
     "    if False:\n        raise ValueError(",
     [T_COSTOS]),

    ('dias_del_periodo cuenta con `.days` pelado (pierde un extremo)',
     DOM, "    return (hasta - desde).days + 1", "    return (hasta - desde).days",
     [T_COSTOS, T_GASTOS]),

    ('imputar_a_ventana devuelve SIN_DATO donde el cero es legítimo',
     DOM, "        return Decimal('0')\n    solapados", "        return SIN_DATO\n    solapados",
     [T_COSTOS]),

    ('el CPK devuelve cero cuando no hay ningún gasto registrado',
     DOM,
     "    if not hubo_gastos:\n        return SIN_DATO, marca_tramo",
     "    if not hubo_gastos:\n        return Decimal('0'), marca_tramo",
     [T_COSTOS, T_GASTOS]),

    ('el CPK acepta cero kilómetros y divide igual',
     DOM, "    if km_recorridos <= 0:", "    if km_recorridos < 0:",
     [T_COSTOS, T_GASTOS]),

    ('el CPK acepta una marca de tramo inventada',
     DOM,
     "    if marca_tramo not in MARCAS_TRAMO:\n        raise ValueError(",
     "    if False:\n        raise ValueError(",
     [T_COSTOS]),

    ('excede_capacidad devuelve False cuando la ficha no tiene capacidad',
     DOM,
     "    if capacidad_galones is None:\n        return SIN_DATO",
     "    if capacidad_galones is None:\n        return False",
     [T_COSTOS, T_GASTOS]),

    ('excede_capacidad marca como excedido el tanque lleno exacto',
     DOM,
     "    return Decimal(galones) > Decimal(capacidad_galones)",
     "    return Decimal(galones) >= Decimal(capacidad_galones)",
     [T_COSTOS]),

    ('las ventanas aceptan un tanqueo parcial como extremo',
     DOM,
     "    llenos = [i for i, t in enumerate(tanqueos) if t['tanque'] == 'lleno']",
     "    llenos = [i for i, t in enumerate(tanqueos) if t['tanque'] != 'sin_dato']",
     [T_COSTOS]),

    ('el rendimiento promedia razones en vez de sumar km y galones',
     DOM,
     "    km = sum(v['km'] for v in ventanas)\n    galones = sum((v['galones'] for v in ventanas), Decimal('0'))",
     "    km = sum(v['km_por_galon'] for v in ventanas)\n    galones = Decimal(len(ventanas))",
     [T_COSTOS, T_GASTOS]),

    ('precio_por_galon devuelve cero pesos sobre cero galones',
     DOM,
     "    if Decimal(galones) <= 0:\n        return SIN_DATO",
     "    if Decimal(galones) <= 0:\n        return Decimal('0')",
     [T_COSTOS]),

    # ── El adaptador ─────────────────────────────────────────────────────
    ('todas las categorías se tratan como de campo (el SOAT pide odómetro)',
     ADA, "    return categoria in ORIGEN_DE_LECTURA", "    return True",
     [T_GASTOS]),

    ('un período mandado sobre una categoría que no lo cubre se ignora',
     ADA,
     "    if desde is not None or hasta is not None:\n        raise GastoInvalido(",
     "    if False:\n        raise GastoInvalido(",
     [T_GASTOS]),

    ('el documento vacío se guarda como cadena en vez de NULL',
     ADA,
     "    limpio = (valor or '').strip()\n    return limpio or None",
     "    limpio = (valor or '').strip()\n    return limpio",
     [T_GASTOS]),

    ('el CPK afirma que hubo gastos aunque no haya ninguno',
     ADA, "        hubo_gastos=bool(gastos), marca_tramo=", "        hubo_gastos=True, marca_tramo=",
     [T_GASTOS]),

    # NOTA — mutación retirada por EQUIVALENTE, no por incómoda.
    # `len(lecturas) >= 2` → `>= 1` no cambia ningún resultado: con una sola
    # lectura, `max(...) - min(...)` da 0 y el CPK devuelve SIN_DATO igual que
    # por la otra rama. Se deja escrito en vez de borrarse: una mutación que
    # sobrevive porque no cambia nada NO es un hueco de tests, y confundir las
    # dos cosas lleva a escribir un test que afirma una diferencia inexistente.
    # En su lugar va la de abajo, que sí cambia el número.
    ('los kilómetros del CPK salen de toda la historia, no de la ventana',
     ADA, "                if desde <= l.ts.date() <= hasta]",
     "                if True]",
     [T_GASTOS]),

    ('el tanqueo escribe el gasto en su propia transacción',
     ADA, "            descripcion=descripcion, ts=ahora, commit=False)",
     "            descripcion=descripcion, ts=ahora, commit=True)",
     [T_GASTOS]),

    ('el número se publica en notación científica',
     ADA,
     "    paso = Decimal(1).scaleb(-decimales)\n    return str(Decimal(valor).quantize(paso, rounding=ROUND_HALF_UP))",
     "    return str(Decimal(valor))",
     [T_GASTOS]),

    # ── El medidor ───────────────────────────────────────────────────────
    ('los tanqueos que no se pudieron revisar dejan de contarse',
     MED, "            if veredicto is SIN_DATO:\n                sin_capacidad += 1",
     "            if veredicto is SIN_DATO:\n                sin_capacidad += 0",
     [T_GASTOS]),

    ('el health cuenta todos los gastos, con documento y sin él',
     MED,
     "        return _contar(Gasto.query.filter(Gasto.documento_numero.is_(None)))",
     "        return _contar(Gasto.query)",
     [T_GASTOS]),

    # ── La frontera ──────────────────────────────────────────────────────
    ('el conductor puede ver el CPK del vehículo',
     API, "@exige(MAESTROS_FLOTA, 'ver los gastos de un vehículo')",
     "@exige(Roles.LECTURA_FLOTA, 'ver los gastos de un vehículo')",
     [T_GASTOS]),

    ('el conductor NO puede registrar su tanqueo',
     API, "@exige(Roles.LECTURA_FLOTA, 'registrar un tanqueo')",
     "@exige(MAESTROS_FLOTA, 'registrar un tanqueo')",
     [T_GASTOS]),

    ('un tanqueo entra por la puerta genérica, sin galones',
     API, "    if datos['categoria'] == 'combustible':", "    if False:",
     [T_GASTOS]),

    # ── La pantalla ──────────────────────────────────────────────────────
    ('el tablero deja de mostrar los tanqueos que no se pudieron revisar',
     JS, "  if (h.tanqueos_sin_capacidad_declarada > 0) {",
     "  if (false && h.tanqueos_sin_capacidad_declarada > 0) {",
     [T_RENDER]),

    ('el tablero no muestra el exceso de capacidad',
     JS, "  if (h.tanqueos_sobre_capacidad > 0) {",
     "  if (false && h.tanqueos_sobre_capacidad > 0) {",
     [T_RENDER]),

    ('el CPK del tablero se promedia entre vehículos',
     JS, "  if (cpk && cpk.length) {", "  if (false && cpk && cpk.length) {",
     [T_RENDER]),

    ('el estado del tanque viene con «lleno» premarcado',
     JS, '<option value="" selected>— elegí una —</option>',
     '<option value="">— elegí una —</option>',
     [T_RENDER]),

    ('la fila del tanqueo excedido deja de explicar y solo señala',
     JS, "la capacidad de la ficha y este registro no pueden ser los dos",
     "revisar este registro",
     [T_RENDER]),

    ('el formulario de gastos deja de sellar su placa',
     JS, '            id="gs-guardar" data-placa="${FLOTA_PLACA}">Registrar</button>',
     '            id="gs-guardar">Registrar</button>',
     [T_USO, T_RENDER]),

    ('la URL del tanqueo se arma concatenando en vez de escribirse entera',
     JS, "const FLOTA_TANQUEO_URL = '/flota/tanqueos';",
     "const FLOTA_TANQUEO_URL = '/flota/' + 'tanq' + 'ueos';",
     [T_TRINQ]),
]

_RESUMEN = re.compile(
    r'(?:(\d+) failed)|(?:(\d+) passed)|(?:(\d+) error)|(?:(\d+) skipped)')


def _correr(tests):
    """Corre pytest y devuelve `(veredicto, resumen)`.

    `veredicto` ∈ {`rojo`, `verde`, `no_juzgado`}. **El returncode no alcanza**:
    un archivo cuyos tests se saltan sale 0, igual que uno que pasó.
    """
    proc = subprocess.run(
        [str(RAIZ / 'venv/bin/python'), '-m', 'pytest', *tests, '-q',
         '-m', 'not postgres', '-p', 'no:randomly', '--tb=no'],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=900,
        # PATH real: sin él `shutil.which('node')` falla y los render tests se
        # SALTAN. Un skip sale con exit 0 y se leería como «sobrevivió».
        env={**os.environ, 'TZ': 'UTC'},
    )
    salida = proc.stdout + proc.stderr
    ultima = [l for l in salida.strip().splitlines() if l.strip()]
    resumen = ultima[-1] if ultima else '(sin salida)'

    n = {'failed': 0, 'passed': 0, 'error': 0, 'skipped': 0}
    for clave in n:
        m = re.search(rf'(\d+) {clave}', resumen)
        if m:
            n[clave] = int(m.group(1))
    if n['error'] and not n['failed']:
        return 'no_juzgado', f'ERROR DE RECOLECCIÓN · {resumen}'
    if n['skipped'] and not n['passed'] and not n['failed']:
        return 'no_juzgado', f'TODO SALTADO · {resumen}'
    if n['failed']:
        return 'rojo', resumen
    if n['passed']:
        return 'verde', resumen
    return 'no_juzgado', f'NADA CORRIÓ · {resumen}'


def main():
    # **El snapshot se toma justo antes de cada mutación, no una vez al
    # arranque.** Hay otro agente editando `flota.js`, `medicion.py` y
    # `puertos.py` ahora mismo: restaurar desde una foto vieja le borraría el
    # trabajo en silencio, que es peor que cualquier mutación que se pierda.
    universo = sorted({t for _, _, _, _, ts in MUTACIONES for t in ts})
    print('BASE — sin mutar, sobre', len(universo), 'archivos de test')
    veredicto, resumen = _correr(universo)
    print(f'  {veredicto}: {resumen}')
    if veredicto != 'verde':
        print('\nLa base NO está verde. Sobre una suite roja toda mutación se '
              've muerta, así que no se mide nada. Abortando.')
        return 1

    muertas, vivas, sin_juzgar = [], [], []
    for i, (nombre, archivo, viejo, nuevo, tests) in enumerate(MUTACIONES, 1):
        ruta = RAIZ / archivo
        texto = ruta.read_text(encoding='utf-8')     # foto fresca, ver arriba
        if texto.count(viejo) != 1:
            sin_juzgar.append((nombre, f'el ancla aparece {texto.count(viejo)} '
                                       f'veces en {archivo}, no 1'))
            print(f'{i:2}/{len(MUTACIONES)} ⚠ ANCLA AMBIGUA · {nombre}')
            continue
        ruta.write_text(texto.replace(viejo, nuevo), encoding='utf-8')
        try:
            veredicto, resumen = _correr(tests)
        finally:
            ruta.write_text(texto, encoding='utf-8')

        if veredicto == 'rojo':
            muertas.append(nombre)
            marca = '✓ MUERTA'
        elif veredicto == 'verde':
            vivas.append((nombre, resumen))
            marca = '✗ SOBREVIVIÓ'
        else:
            sin_juzgar.append((nombre, resumen))
            marca = '⚠ NO SE JUZGÓ'
        print(f'{i:2}/{len(MUTACIONES)} {marca} · {nombre}\n      {resumen}')

    print(f'\n{len(muertas)}/{len(MUTACIONES)} muertas · '
          f'{len(vivas)} sobrevivieron · {len(sin_juzgar)} no se juzgaron')
    for nombre, r in vivas:
        print(f'  SOBREVIVIÓ: {nombre} — {r}')
    for nombre, r in sin_juzgar:
        print(f'  NO SE JUZGÓ: {nombre} — {r}')
    return 0 if (not vivas and not sin_juzgar) else 1


if __name__ == '__main__':
    sys.exit(main())

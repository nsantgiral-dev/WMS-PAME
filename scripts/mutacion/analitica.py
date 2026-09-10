#!/usr/bin/env python3
"""
Arnés de mutación del tab de analítica de flota (2026-09-04).

Rompe UNA propiedad a la vez y exige que la suite se ponga en rojo. Un test que
nunca puede fallar no es un test: es una línea que se ejecuta.

Las mutaciones de acá persiguen una sola forma de defecto, que es la del módulo
entero: **algo que se ve bien y no midió nada**. Un promedio sin su `n`, un
`sin_dato` sin motivo, un panel que desaparece por estar vacío, un umbral que
deja pasar un número que nadie puede sostener.

Tres estados, no dos: `MUERTA`, `SOBREVIVIÓ` y `NO SE JUZGÓ` — un test saltado
sale con exit 0 igual que uno que pasó, y el arnés que persigue verdes falsos ya
produjo uno por no distinguirlos.

Uso:  venv/bin/python scripts/mutacion/analitica.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

PROC = 'flota/dominio/procedencia.py'
HALL = 'flota/dominio/hallazgo.py'
COST = 'flota/dominio/costos.py'
MED = 'flota/adaptadores/medicion.py'
COND = 'flota/api/conductor.py'
JS = 'app/static/pwa/flota_analitica.js'

T_PROC = 'tests/flota/test_procedencia.py'
T_HALL = 'tests/flota/test_endpoints_hallazgo.py'
T_COND = 'tests/flota/test_el_conductor_ve_su_camion.py'
T_REND = 'tests/flota/test_render_analitica_js.py'
T_HEALTH = 'tests/flota/test_health_flota.py'
T_GASTOS = 'tests/flota/test_gastos.py'

#: (nombre, archivo, viejo, nuevo, tests)
MUTACIONES = [
    # ── `Cifra` — la regla 13 hecha tipo ─────────────────────────────────
    ('una cifra se puede publicar SIN BASE',
     PROC, "        if not (self.base or '').strip():", "        if False:",
     [T_PROC, T_GASTOS]),

    ('un sin_dato se puede publicar MUDO, sin decir por qué',
     PROC, "        if _es_sin_dato(self.valor) and not (self.motivo or '').strip():",
     "        if False:",
     [T_PROC]),

    ('un motivo se cuela al lado de una cifra que SÍ existe',
     PROC, "        if not _es_sin_dato(self.valor) and self.motivo is not None:",
     "        if False:",
     [T_PROC]),

    ('el sin_dato se compara por IDENTIDAD y deja de reconocer el formateado',
     PROC, "    return valor == SIN_DATO", "    return valor is SIN_DATO",
     [T_PROC, T_GASTOS]),

    ('un Decimal llega hasta jsonify',
     PROC, "        if not isinstance(self.valor, _JSON_SEGURO):", "        if False:",
     [T_PROC]),

    ('el valor pisa su propia base al serializar',
     PROC, "        if clave_valor in ('base', 'desde', 'hasta', 'etiqueta', 'n', 'motivo'):",
     "        if False:",
     [T_PROC]),

    # ── El indicador de hallazgos ────────────────────────────────────────
    ('el indicador deja de publicar el DENOMINADOR (n_fuera)',
     HALL, "        'n_fuera': len(hallazgos) - len(entran),", "        'n_fuera': 0,",
     [T_HALL, T_REND]),

    ('un hallazgo fuera del indicador deja de decir POR QUÉ está fuera',
     HALL, "            'motivo_fuera': ('es de línea base", "            'motivo_fuera_APAGADO': ('es de línea base",
     [T_HALL, T_REND]),

    ('el promedio se calcula acá en vez de con la política ya escrita',
     HALL, "        'promedio_dias': promedio_del_indicador(entran),",
     "        'promedio_dias': (sum(dias_hallazgo_abierto(h) for h in entran "
     "if h.cerrado_ts) / max(1, len([h for h in entran if h.cerrado_ts]))),",
     [T_HALL]),

    # ── Las cuatro condiciones del km/galón ──────────────────────────────
    ('el umbral de ventanas baja a una: el conductor ve ruido con su nombre',
     COST, "MIN_VENTANAS_PUBLICABLE = 6", "MIN_VENTANAS_PUBLICABLE = 1",
     [T_COND, T_REND]),

    ('el umbral de días desaparece: seis tanqueos de la misma semana bastan',
     COST, "    elif dias_historia < min_dias:", "    elif False:",
     [T_COND]),

    # Esta mutación SOBREVIVIÓ la primera vez: el test que existía sembraba
    # siete llenos y nunca llegaba a la rama `len(llenos) < 2`. Un caso límite
    # probado solo por su lado ancho. Ahora la cubre
    # `test_costos.py::TestSinDosLlenosEstanTODOSAfuera`.
    ('sin dos llenos, los tanqueos perdidos se cuentan como CERO',
     COST, "        return len(tanqueos)", "        return 0",
     ['tests/flota/test_costos.py', T_COND]),

    ('los tanqueos de los extremos dejan de contarse como perdidos',
     COST, "    return len(tanqueos) - (llenos[-1] - llenos[0] + 1)",
     "    return 0",
     ['tests/flota/test_costos.py', T_COND, T_REND]),

    ('al conductor le llega el número aunque no se pueda sostener',
     COND, "        'km_galon': (numero_legible(r['km_galon']) if r['publicable']\n"
           "                     else str(SIN_DATO)),",
     "        'km_galon': numero_legible(r['km_galon']),",
     [T_COND]),

    ('el rendimiento vuelve a tener DOS cuentas que pueden divergir',
     COST, "    return rendimiento_publicable(tanqueos, dias_historia=0)['km_galon']",
     "    v = ventanas_lleno_a_lleno(tanqueos)\n"
     "    if not v:\n        return SIN_DATO\n"
     "    return sum(x['km_por_galon'] for x in v) / len(v)",
     [T_COND, 'tests/flota/test_costos.py']),

    # ── El tablero ───────────────────────────────────────────────────────
    ('la procedencia del tablero pierde el huso de Bogotá',
     MED, "            'calculado_ts': ahora_bogota().isoformat(),",
     "            'calculado_ts': __import__('datetime').datetime.utcnow().isoformat(),",
     [T_HEALTH]),

    ('un panel vacío vuelve a desaparecer en vez de decir qué lo enciende',
     JS, "function flotaAnPanelVacio(titulo, base, gesto) {\n  return `<div class=\"tabla-card\">",
     "function flotaAnPanelVacio(titulo, base, gesto) {\n  if (true) return '';\n  return `<div class=\"tabla-card\">",
     [T_REND]),

    ('el tab pone la plata antes que la calidad del kilómetro',
     JS, "    flotaAnLecturas(h),\n    flotaAnCobertura(h),\n    flotaAnCPK(h),",
     "    flotaAnCPK(h),\n    flotaAnLecturas(h),\n    flotaAnCobertura(h),",
     [T_REND]),

    ('las llantas se agrupan por POSICIÓN y no por camión',
     JS, "  filas.forEach(f => { (porPlaca[f.placa] = porPlaca[f.placa] || []).push(f); });",
     "  filas.forEach(f => { (porPlaca[f.posicion] = porPlaca[f.posicion] || []).push(f); });",
     [T_REND]),

    ('el panel de rendimiento se ordena por número y se vuelve un ranking',
     JS, "  const cuerpo = filas.map(f => `<div style=\"margin-bottom:10px\">\n"
         "      <div style=\"display:flex;justify-content:space-between;font-size:13px\">\n"
         "        <b>${f.placa}</b>\n"
         "        <span style=\"color:var(--${f.publicable ? 'tx2' : 'yellow'})\">",
     "  const cuerpo = [...filas].sort((a, b) => Number(b.km_galon) - Number(a.km_galon))"
     ".map(f => `<div style=\"margin-bottom:10px\">\n"
         "      <div style=\"display:flex;justify-content:space-between;font-size:13px\">\n"
         "        <b>${f.placa}</b>\n"
         "        <span style=\"color:var(--${f.publicable ? 'tx2' : 'yellow'})\">",
     [T_REND]),

    ('el CPK del tab deja de decir a quién llamar',
     JS, "      ${f.motivo ? `<div style=\"font-size:12px;color:var(--tx2)\">${f.motivo}</div>` : ''}\n"
         "      <div style=\"font-size:11px;color:var(--tx3,var(--tx2))\">${f.base} · ${f.desde}",
     "      ${false ? `<div>${f.motivo}</div>` : ''}\n"
         "      <div style=\"font-size:11px;color:var(--tx3,var(--tx2))\">${f.base} · ${f.desde}",
     [T_REND]),
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

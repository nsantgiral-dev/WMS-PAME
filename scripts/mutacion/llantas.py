#!/usr/bin/env python
"""
Arnés de mutación de la fase 4 (llantas).

Un test en verde no prueba que el guard funcione: prueba que hoy no falla. Este
arnés rompe el código a propósito, corre los tests y exige que se pongan rojos.

## Lo que este arnés hace y el del hallazgo no hacía

**Un test saltado sale con exit 0 igual que uno que pasó.** El 2026-09-01 el
arnés del hallazgo recortó el `PATH` del subproceso, `node` desapareció, los
tests de render se saltaron y el arnés los reportó como «sobrevivió» — un skip en
verde producido por el propio arnés que persigue skips en verde.

Acá:

  · se le pasa el **entorno completo** al subproceso (`os.environ`), así que
    `node` está donde estaba;
  · el veredicto sale de **parsear el resumen de pytest**, no del `returncode`:
    se distingue `MUERTA` (algo falló) de `NO SE JUZGÓ` (0 tests corrieron, hubo
    error de colección, o los tests objetivo se saltaron);
  · **se exige la base en verde antes de mutar nada.** Si la línea base no está
    limpia, todo lo demás no significa nada.

Uso:
    venv/bin/python <este archivo>
    venv/bin/python <este archivo> 12          # solo la mutación 12
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PY = str(RAIZ / 'venv' / 'bin' / 'python')

MODELOS = 'flota/adaptadores/modelos.py'
DOMINIO = 'flota/dominio/llantas.py'
ADAPTA = 'flota/adaptadores/llantas.py'
MEDICION = 'flota/adaptadores/medicion.py'
API = 'flota/api/llantas.py'
HEALTH = 'flota/api/health.py'
RESET = 'scripts/reset_transaccional.py'
JS = 'app/static/pwa/flota.js'

TESTS = 'tests/flota/test_llantas.py'
RENDER = 'tests/flota/test_render_llantas_js.py'
TODO = f'{TESTS} {RENDER}'

VERDE, ROJO, AMAR, GRIS, FIN = (
    '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m')


#: `(nombre, archivo, viejo, nuevo, tests)`.
#:
#: Cada mutación rompe UNA propiedad y nombra el test que debería matarla. Si
#: sobrevive, el test estaba en verde por otra razón.
MUTACIONES = [
    # ── Los dos índices únicos parciales ────────────────────────────────────
    ('1. el índice de posición deja de ser PARCIAL', MODELOS,
     """db.Index('uq_flota_montaje_posicion_vigente', 'vehiculo_id', 'posicion',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),""",
     """db.Index('uq_flota_montaje_posicion_vigente', 'vehiculo_id', 'posicion',
                 unique=True),""", TESTS),

    ('2. el índice de posición deja de ser ÚNICO', MODELOS,
     """db.Index('uq_flota_montaje_posicion_vigente', 'vehiculo_id', 'posicion',
                 unique=True,""",
     """db.Index('uq_flota_montaje_posicion_vigente', 'vehiculo_id', 'posicion',
                 unique=False,""", TESTS),

    ('3. el índice de llanta deja de ser PARCIAL', MODELOS,
     """db.Index('uq_flota_montaje_llanta_vigente', 'llanta_id',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),""",
     """db.Index('uq_flota_montaje_llanta_vigente', 'llanta_id',
                 unique=True),""", TESTS),

    ('4. el índice de llanta deja de ser ÚNICO', MODELOS,
     """db.Index('uq_flota_montaje_llanta_vigente', 'llanta_id',
                 unique=True,""",
     """db.Index('uq_flota_montaje_llanta_vigente', 'llanta_id',
                 unique=False,""", TESTS),

    # ── El CHECK del tercer estado ──────────────────────────────────────────
    ('5. el cierre puede quedar sin lectura de fin', MODELOS,
     '"  ELSE lectura_fin_id IS NOT NULL"',
     '"  ELSE 1 = 1 OR lectura_fin_id IS NOT NULL"', TESTS),

    ('6. un montaje abierto puede traer lectura de cierre', MODELOS,
     '"  WHEN fin_ts IS NULL THEN lectura_fin_id IS NULL"',
     '"  WHEN fin_ts IS NULL THEN 1 = 1 OR lectura_fin_id IS NULL"', TESTS),

    ('7. el CHECK de rango de posición se afloja', MODELOS,
     "f'posicion >= 1 AND posicion <= {MAX_POSICIONES_LLANTA}',",
     "f'posicion >= 1 OR posicion <= {MAX_POSICIONES_LLANTA}',", TESTS),

    # ── El trigger que compara contra la ficha ──────────────────────────────
    ('8. el trigger de SQLite nunca dispara', MODELOS,
     """FOR EACH ROW WHEN NEW.posicion > COALESCE(
    (SELECT posiciones_llanta FROM flota_ficha_tecnica
      WHERE vehiculo_id = NEW.vehiculo_id), 0)""",
     """FOR EACH ROW WHEN NEW.posicion > 99 + COALESCE(
    (SELECT posiciones_llanta FROM flota_ficha_tecnica
      WHERE vehiculo_id = NEW.vehiculo_id), 0)""", TESTS),

    ('9. sin ficha, el trigger acepta cualquier posición', MODELOS,
     """      WHERE vehiculo_id = NEW.vehiculo_id), 0)
BEGIN SELECT RAISE(ABORT""",
     """      WHERE vehiculo_id = NEW.vehiculo_id), 99)
BEGIN SELECT RAISE(ABORT""", TESTS),

    # ── El dominio ──────────────────────────────────────────────────────────
    ('10. sin ficha, `posicion_valida` acepta', DOMINIO,
     """    if posiciones_del_vehiculo is None:
        return False""",
     """    if posiciones_del_vehiculo is None:
        return True""", TESTS),

    ('11. una llanta montada vale 0 km', DOMINIO,
     """    if lectura_fin is None:
        return VIGENTE, VIGENTE""",
     """    if lectura_fin is None:
        return 0, VIGENTE""", TODO),

    ('12. dos lecturas dudosas publican el número igual', DOMINIO,
     """    if marca == SIN_DATO:
        return SIN_DATO, SIN_DATO""",
     """    if marca == SIN_DATO:
        return lectura_fin.valor_km - lectura_inicio.valor_km, 'declarada'""",
     TESTS),

    ('13. el acumulado suma también los tramos vigentes', DOMINIO,
     "    cerrados = [(v, m) for v, m in tramos if v != VIGENTE]",
     "    cerrados = [(v, m) for v, m in tramos]", TESTS),

    ('14. un sumando sin dato no contamina la suma', DOMINIO,
     """    if any(v == SIN_DATO for v, _ in cerrados):
        return SIN_DATO, SIN_DATO""",
     """    if False:
        return SIN_DATO, SIN_DATO
    cerrados = [(v, m) for v, m in cerrados if v != SIN_DATO]""", TESTS),

    ('15. el total hereda la MEJOR marca en vez de la peor', DOMINIO,
     "    peor = orden[max(orden.index(str(m)) for _, m in cerrados)]",
     "    peor = orden[min(orden.index(str(m)) for _, m in cerrados)]", TESTS),

    ('16. la mediana se publica sin importar cuántas vidas hay', DOMINIO,
     "        if n >= MONTAJES_CERRADOS_PARA_VIDA_UTIL:",
     "        if n >= 1:", TODO),

    ('17. la mediana no se publica nunca', DOMINIO,
     "        if n >= MONTAJES_CERRADOS_PARA_VIDA_UTIL:",
     "        if n >= 9999:", TESTS),

    ('18. sin ficha, `posiciones_sin_llanta` devuelve lista vacía', DOMINIO,
     """    if posiciones_del_vehiculo is None:
        return SIN_DATO""",
     """    if posiciones_del_vehiculo is None:
        return []""", TODO),

    # ── El adaptador ────────────────────────────────────────────────────────
    ('19. montar no mira si la posición está ocupada', ADAPTA,
     "    ocupada = _vigente_en(vehiculo_id, posicion)",
     "    ocupada = None", TESTS),

    ('20. montar no mira si la llanta ya está puesta', ADAPTA,
     "    puesta = montaje_vigente_de(llanta_id)",
     "    puesta = None", TESTS),

    ('21. montar no valida la posición contra la ficha', ADAPTA,
     "    if not dom.posicion_valida(posicion, declaradas):",
     "    if False and not dom.posicion_valida(posicion, declaradas):", TESTS),

    ('22. la lectura del montaje miente sobre su origen', ADAPTA,
     "                                  origen=OrigenLectura.OT)\n"
     "        fila = MontajeLlanta(",
     "                                  origen=OrigenLectura.HALLAZGO)\n"
     "        fila = MontajeLlanta(", TESTS),

    ('23. desmontar acepta un motivo inventado', ADAPTA,
     "    if motivo not in dom.MOTIVOS_DESMONTAJE:",
     "    if False and motivo not in dom.MOTIVOS_DESMONTAJE:", TESTS),

    ('24. desmontar dos veces no levanta', ADAPTA,
     "    if fila.fin_ts is not None:",
     "    if False and fila.fin_ts is not None:", TESTS),

    ('25. el gasto de otro vehículo se acepta', ADAPTA,
     "    if gasto.vehiculo_id != vehiculo_id:",
     "    if False and gasto.vehiculo_id != vehiculo_id:", TESTS),

    ('26. el código repetido no se ataja en el adaptador', ADAPTA,
     "    if Llanta.query.filter_by(codigo=limpio).first() is not None:",
     "    if False:", TESTS),

    ('27. `km_por_posicion` cuenta también los tramos sin dato', ADAPTA,
     "        if km == VIGENTE or km == SIN_DATO:\n            continue",
     "        if km == VIGENTE:\n            continue", TESTS),

    ('28. `posiciones_declaradas` cae al fallback en vez de decir None', ADAPTA,
     "    return None if ficha is None else int(ficha.posiciones_llanta)",
     "    return 4 if ficha is None else int(ficha.posiciones_llanta)", TODO),

    # ── La medida ───────────────────────────────────────────────────────────
    ('29. `posiciones_sin_llanta` devuelve un cero plausible', MEDICION,
     "            if isinstance(libres, list):\n                total += len(libres)",
     "            if isinstance(libres, list):\n                total += 0", TESTS),

    ('30. el contador de los que no se pudieron mirar vale siempre 0', MEDICION,
     "        return len(self._flota_con_llantas()[1])",
     "        return 0", TESTS),

    ('31. `llantas_montadas` cuenta las cerradas', MEDICION,
     """        return _contar(MontajeLlanta.query.filter(
            MontajeLlanta.fin_ts.is_(None)))""",
     """        return _contar(MontajeLlanta.query.filter(
            MontajeLlanta.fin_ts.isnot(None)))""", TESTS),

    ('32. `km_por_posicion` del health devuelve vacío siempre', MEDICION,
     """        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):""",
     """        salida = []
        for v in []:""", TESTS),

    # ── La frontera ─────────────────────────────────────────────────────────
    ('33. montar se le abre al conductor', API,
     "@exige(MAESTROS_FLOTA, 'montar una llanta')",
     "@exige(MAESTROS_FLOTA + ('conductor',), 'montar una llanta')", TESTS),

    ('34. ver las llantas se le abre al conductor', API,
     "@exige(MAESTROS_FLOTA, 'ver las llantas de un vehículo')",
     "@exige(MAESTROS_FLOTA + ('conductor',), 'ver las llantas de un vehículo')",
     TESTS),

    ('35. el vocabulario de motivos no viaja al cliente', API,
     "'motivos_desmontaje': list(dom.MOTIVOS_DESMONTAJE),",
     "'motivos_desmontaje': [],", TESTS),

    ('36. un campo del health desaparece de `_CAMPOS`', HEALTH,
     "    'posiciones_sin_llanta',\n    'vehiculos_sin_posiciones_llanta',",
     "    'vehiculos_sin_posiciones_llanta',", TESTS),

    # ── El acta de corte ────────────────────────────────────────────────────
    ('37. la llanta se clasifica como operativa', RESET,
     "    'flota_montaje_llanta',\n    'flota_tanqueo',",
     "    'flota_montaje_llanta',\n    'flota_llanta',\n    'flota_tanqueo',",
     TESTS),

    ('38. el montaje se borra DESPUÉS del gasto', RESET,
     "    'flota_montaje_llanta',\n    'flota_tanqueo',\n    'flota_gasto',",
     "    'flota_tanqueo',\n    'flota_gasto',\n    'flota_montaje_llanta',",
     TESTS),

    # ── La pantalla ─────────────────────────────────────────────────────────
    ('39. una llanta montada se pinta como 0 km', JS,
     "  if (km === 'vigente') return '<b>montada</b> — su vida todavía no terminó';",
     "  if (km === 'vigente') return '<b>0 km</b>';", RENDER),

    ('40. `sin_dato` se pinta sin decir dónde se arregla', JS,
     """  if (km === 'sin_dato') {
    return '<span style="color:var(--yellow)">sin dato</span> — los dos ' +
      'kilometrajes están en duda. Se resuelve en «Verificar kilometrajes».';
  }""",
     """  if (km === 'sin_dato') {
    return '<span style="color:var(--yellow)">sin dato</span>';
  }""", RENDER),

    ('41. el aviso de desgaste irregular desaparece', JS,
     "  const aviso = m.motivo_desmontaje === 'desgaste_irregular'",
     "  const aviso = false && m.motivo_desmontaje === 'desgaste_irregular'",
     RENDER),

    ('42. el aviso de desgaste irregular sale para TODOS los motivos', JS,
     "  const aviso = m.motivo_desmontaje === 'desgaste_irregular'",
     "  const aviso = m.motivo_desmontaje !== null", RENDER),

    ('43. el renglón de posiciones sin llanta es incondicional', JS,
     "  if (h.posiciones_sin_llanta > 0) {",
     "  if (h.posiciones_sin_llanta >= 0) {", RENDER),

    ('44. el renglón de los que no se pudieron mirar desaparece', JS,
     "  if (h.vehiculos_sin_posiciones_llanta > 0) {",
     "  if (false && h.vehiculos_sin_posiciones_llanta > 0) {", RENDER),

    ('45. sin ficha, la pantalla dice que están todas cubiertas', JS,
     "  const bloqueLibres = libres === 'sin_dato'",
     "  const bloqueLibres = false", RENDER),

    ('46. el desplegable de montaje esconde el último motivo', JS,
     "${ll.ultimo_motivo ? ' · salió por ' + ll.ultimo_motivo : ''}",
     "", RENDER),
]

_RESUMEN = re.compile(
    r'(?:(\d+) failed)?[,\s]*(?:(\d+) passed)?[,\s]*(?:(\d+) skipped)?'
    r'[,\s]*(?:(\d+) error)?', re.I)


def _correr(tests: str):
    """`(veredicto, resumen)` — y **el veredicto NO sale del returncode**.

    Un test saltado sale con exit 0 igual que uno que pasó. Se parsea la línea
    de resumen de pytest y se distinguen tres casos, no dos.
    """
    proc = subprocess.run(
        [PY, '-m', 'pytest', *tests.split(), '-q', '-p', 'no:randomly',
         '--no-header', '-x'],
        cwd=RAIZ, capture_output=True, text=True,
        # **El entorno COMPLETO.** Recortarlo fue lo que dejó a `node` fuera del
        # PATH el 2026-09-01 y produjo skips que el arnés leyó como verdes.
        env={**os.environ, 'TZ': 'UTC'})
    salida = (proc.stdout or '') + (proc.stderr or '')
    linea = ''
    for l in reversed(salida.strip().splitlines()):
        if re.search(r'\d+ (passed|failed|skipped|error)', l):
            linea = l.strip()
            break
    if not linea:
        return 'NO SE JUZGÓ', f'sin resumen de pytest (rc={proc.returncode})'

    def _n(patron):
        m = re.search(rf'(\d+) {patron}', linea)
        return int(m.group(1)) if m else 0

    fallaron, pasaron = _n('failed'), _n('passed')
    saltados, errores = _n('skipped'), _n('errors?')

    if errores or 'no tests ran' in linea.lower():
        return 'NO SE JUZGÓ', linea
    if pasaron == 0 and fallaron == 0:
        return 'NO SE JUZGÓ', linea
    if saltados and pasaron == 0 and fallaron == 0:
        return 'NO SE JUZGÓ', linea
    if fallaron:
        return 'MUERTA', linea
    return 'SOBREVIVIÓ', linea


def _base_verde():
    print(f'{GRIS}Verificando la línea base…{FIN}')
    veredicto, linea = _correr(TODO)
    if veredicto != 'SOBREVIVIÓ':
        print(f'{ROJO}La base NO está en verde: {linea}{FIN}')
        print('Todo lo demás no significaría nada. Se aborta.')
        return False
    print(f'{VERDE}Base en verde:{FIN} {linea}\n')
    return True


def main():
    if not _base_verde():
        return 2

    solo = None
    if len(sys.argv) > 1:
        solo = sys.argv[1]

    muertas, sobrevivientes, sin_juzgar = 0, [], []
    for nombre, archivo, viejo, nuevo, tests in MUTACIONES:
        numero = nombre.split('.')[0]
        if solo and numero != solo:
            continue
        ruta = RAIZ / archivo
        original = ruta.read_text(encoding='utf-8')
        if viejo not in original:
            print(f'{AMAR}  {nombre}: el texto a mutar no existe — '
                  f'el arnés quedó desalineado del código{FIN}')
            sin_juzgar.append(nombre)
            continue
        if original.count(viejo) != 1:
            print(f'{AMAR}  {nombre}: el texto aparece '
                  f'{original.count(viejo)} veces, no 1{FIN}')
            sin_juzgar.append(nombre)
            continue
        try:
            ruta.write_text(original.replace(viejo, nuevo), encoding='utf-8')
            veredicto, linea = _correr(tests)
        finally:
            ruta.write_text(original, encoding='utf-8')

        if veredicto == 'MUERTA':
            muertas += 1
            print(f'{VERDE}  MUERTA{FIN}       {nombre}  {GRIS}{linea}{FIN}')
        elif veredicto == 'SOBREVIVIÓ':
            sobrevivientes.append(nombre)
            print(f'{ROJO}  SOBREVIVIÓ{FIN}   {nombre}  {GRIS}{linea}{FIN}')
        else:
            sin_juzgar.append(nombre)
            print(f'{AMAR}  NO SE JUZGÓ{FIN}  {nombre}  {GRIS}{linea}{FIN}')

    total = muertas + len(sobrevivientes) + len(sin_juzgar)
    print(f'\n  {muertas}/{total} muertas')
    if sobrevivientes:
        print(f'  {ROJO}Sobrevivieron:{FIN}')
        for s in sobrevivientes:
            print(f'    · {s}')
    if sin_juzgar:
        print(f'  {AMAR}No se juzgaron (NO son verdes):{FIN}')
        for s in sin_juzgar:
            print(f'    · {s}')
    return 0 if muertas == total else 1


if __name__ == '__main__':
    sys.exit(main())

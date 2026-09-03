#!/usr/bin/env python3
"""
Arnés de mutación de la fase 0 — «el kilómetro confiable» (2026-09-02).

Rompe UNA propiedad a la vez y exige que la suite se ponga en rojo. Un test que
nunca puede fallar no es un test: es una línea que se ejecuta.

## Lo que el returncode no alcanza a decir

**Un test saltado sale con exit 0 igual que uno que pasó.** El arnés del
hallazgo (2026-09-01) reportó «sobrevivió» sobre tests que se estaban SALTANDO:
`node` no estaba en el PATH recortado del subproceso y los render tests hacían
`pytest.skip`. Un skip en verde, producido por el propio arnés que persigue
skips en verde.

Por eso acá:

1. se le pasa el **entorno completo** al subproceso (`os.environ`), o sea el
   PATH real: sin él `shutil.which('node')` falla y los tres archivos de render
   se saltan enteros;
2. se **parsea la línea de resumen**, no el returncode: `NO SE JUZGÓ` no es lo
   mismo que `SOBREVIVIÓ` ni que `MUERTA`;
3. se exige que la corrida **base** esté verde antes de mutar nada — sobre una
   suite ya roja, toda mutación se ve muerta;
4. la foto del archivo se toma **justo antes de cada mutación** y se restaura en
   `finally`. Hay otro agente editando `flota.js`, `medicion.py` y `puertos.py`
   ahora mismo: restaurar desde una foto vieja le borraría el trabajo en
   silencio.

Uso:  venv/bin/python <este archivo>
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

MOD = 'flota/adaptadores/modelos.py'
VER = 'flota/adaptadores/verificacion.py'
GAS = 'flota/adaptadores/gastos.py'
MED = 'flota/adaptadores/medicion.py'
API = 'flota/api/custodia.py'
DOM = 'flota/dominio/odometro.py'
JS = 'app/static/pwa/flota.js'

T_CONF = 'tests/flota/test_confianza_odometro.py'
T_GASTOS = 'tests/flota/test_gastos.py'
T_RENDER = 'tests/flota/test_render_verificacion_js.py'
T_TRINQ = 'tests/flota/test_trinquetes_flota.py'
T_CT1 = 'tests/flota/test_constraints_t1.py'
T_FOTO = 'tests/flota/test_foto_atada_a_la_lectura.py'

#: (nombre, archivo, viejo, nuevo, tests). Cada una rompe UNA propiedad.
MUTACIONES = [
    # ── El gancho que marca al nacer ─────────────────────────────────────
    ('el gancho marca todo `declarada` y no aplica la política',
     MOD,
     "    target.confianza = confianza.value\n    target.motivo_dudosa = motivo",
     "    target.confianza = 'declarada'\n    target.motivo_dudosa = None",
     [T_CONF, T_GASTOS]),

    ('el gancho asume que toda lectura tiene foto',
     MOD, "        tiene_foto=target.foto_id is not None,", "        tiene_foto=True,",
     [T_CONF, T_GASTOS]),

    ('el gancho no mira la lectura previa (se pierden Δt=0 y el salto ×10)',
     MOD,
     "        previa_valor_km=previa.valor_km if previa is not None else None,\n"
     "        previa_ts=previa.ts if previa is not None else None,",
     "        previa_valor_km=None,\n        previa_ts=None,",
     [T_CONF]),

    ('el gancho deja nacer una lectura ya verificada',
     MOD,
     "    if target.confianza == Confianza.VERIFICADA.value:",
     "    if False:",
     [T_CONF]),

    ('el gancho no resuelve el `ts` y la regla del Δt=0 queda ciega',
     MOD, "    if target.ts is None:\n        target.ts = datetime.utcnow()",
     "    if False:\n        target.ts = datetime.utcnow()",
     [T_CONF]),

    # ── Los CHECK ────────────────────────────────────────────────────────
    ('una `dudosa` puede entrar sin motivo escrito',
     MOD,
     "            \"confianza <> 'dudosa' OR \"\n"
     "            \"(motivo_dudosa IS NOT NULL AND length(trim(motivo_dudosa)) > 0)\",",
     "            \"1 = 1\",",
     [T_CONF]),

    ('una `declarada` puede traer motivo de duda',
     MOD,
     "            \"confianza <> 'declarada' OR motivo_dudosa IS NULL\",",
     "            \"1 = 1\",",
     [T_CONF]),

    ('un verificador colgado de una fila NO verificada entra igual',
     MOD,
     '            "(confianza <> \'verificada\' AND verificada_por_usuario_id IS NULL "\n'
     '            " AND verificada_ts IS NULL)",',
     '            "(1 = 1 "\n            " AND 1 = 1)",',
     [T_CONF]),

    ('el vocabulario de confianza deja de ser cerrado',
     MOD, "        _en('confianza', CONFIANZA),", "",
     [T_CONF]),

    # ── Los triggers ─────────────────────────────────────────────────────
    ('el trigger deja nacer una fila verificada por SQL crudo',
     MOD,
     "FOR EACH ROW WHEN NEW.confianza = 'verificada'\nBEGIN SELECT RAISE(ABORT, '{_MSG_NACE_NO_VERIFICADA}'); END;",
     "FOR EACH ROW WHEN 0\nBEGIN SELECT RAISE(ABORT, '{_MSG_NACE_NO_VERIFICADA}'); END;",
     [T_CONF]),

    ('el append-only se abre del todo: cualquier UPDATE pasa',
     MOD,
     "FOR EACH ROW WHEN NOT (\n    {_identicas('IS')}",
     "FOR EACH ROW WHEN 0 AND NOT (\n    {_identicas('IS')}",
     [T_CONF, T_CT1, T_FOTO]),

    ('el append-only se cierra del todo: la verificación tampoco pasa',
     MOD,
     "    AND OLD.confianza <> 'verificada'\n    AND NEW.confianza = 'verificada')",
     "    AND OLD.confianza <> 'verificada'\n    AND NEW.confianza = 'jamas')",
     [T_CONF]),

    ('`valor_km` sale de las columnas inmutables',
     MOD,
     "    'id', 'vehiculo_id', 'valor_km', 'ts', 'origen', 'foto_id',",
     "    'id', 'vehiculo_id', 'ts', 'origen', 'foto_id',",
     [T_CONF, T_CT1]),

    ('se puede desverificar una lectura',
     MOD, "    AND OLD.confianza <> 'verificada'\n", "    \n",
     [T_CONF]),

    # ── El trigger hermano del ancla ─────────────────────────────────────
    ('el ancla puede bajar por debajo de la serie',
     MOD,
     "FOR EACH ROW WHEN NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (",
     "FOR EACH ROW WHEN 0 AND NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (",
     [T_CONF]),

    ('el ancla mide el estado y no el gesto: congela las 4 fichas incoherentes',
     MOD,
     "FOR EACH ROW WHEN NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (",
     "FOR EACH ROW WHEN NEW.km_inicial < (",
     [T_CONF]),

    # ── La cola ──────────────────────────────────────────────────────────
    ('la cola ignora la ventana de corrección y no drena nunca',
     VER,
     "         if desde_por_vehiculo[l.vehiculo_id] is None\n"
     "         or l.ts >= desde_por_vehiculo[l.vehiculo_id]],",
     "         if True],",
     [T_CONF]),

    ('la ventana excluye la corrección misma (empate estricto)',
     VER, "         or l.ts >= desde_por_vehiculo[l.vehiculo_id]],",
     "         or l.ts > desde_por_vehiculo[l.vehiculo_id]],",
     [T_CONF]),

    ('la cola sale en cualquier orden',
     VER, "        key=lambda par: (par[0].ts, par[0].id))",
     "        key=lambda par: (-par[0].id,))",
     [T_CONF]),

    ('verificar dos veces mueve la fecha y tapa el primer nombre',
     VER, "    if fila.confianza == Confianza.VERIFICADA.value:", "    if False:",
     [T_CONF]),

    ('se puede «verificar» una lectura que nadie puso en duda',
     VER, "    if fila.confianza != Confianza.DUDOSA.value:", "    if False:",
     [T_CONF]),

    # ── El dominio compartido ────────────────────────────────────────────
    ('la ventana de corrección se calcula sobre la lectura equivocada',
     DOM, "    desde = max(correcciones)", "    desde = min(correcciones)",
     [T_CONF]),

    # ── El CPK ───────────────────────────────────────────────────────────
    ('el CPK vuelve a publicar `declarada` sin mirar la confianza',
     GAS,
     "    marca = dom_odo.confianza_del_tramo(bajo.a_dominio(), alto.a_dominio())",
     "    marca = 'declarada'",
     [T_CONF, T_GASTOS]),

    ('un tramo sin dos lecturas se publica como declarado',
     GAS, "    if len(lecturas) < 2:\n        return 0, SIN_DATO",
     "    if len(lecturas) < 2:\n        return 0, 'declarada'",
     [T_CONF, T_GASTOS]),

    ('la marca sale como `Confianza.DUDOSA` en vez de `dudosa`',
     GAS, "        marca.value if isinstance(marca, Confianza) else marca)",
     "        str(marca))",
     [T_CONF, T_GASTOS]),

    ('el traductor de fila a dominio miente sobre la confianza',
     MOD, "            confianza=Confianza(self.confianza),",
     "            confianza=Confianza.DECLARADA,",
     [T_CONF, T_GASTOS]),

    # ── El health ────────────────────────────────────────────────────────
    ('el health cuenta las dudosas por su cuenta y diverge de la pantalla',
     MED, "        return len(pendientes())",
     "        from flota.adaptadores.modelos import LecturaOdometro as _L\n"
     "        return _contar(_L.query.filter(_L.confianza == 'dudosa'))",
     [T_CONF]),

    ('las verificadas se cuentan sin ventana de 30 días',
     MED,
     "            LecturaOdometro.verificada_ts >= _datetime.utcnow() - _timedelta(days=30),",
     "            LecturaOdometro.verificada_ts.isnot(None),",
     [T_CONF]),

    # ── La frontera ──────────────────────────────────────────────────────
    ('verificar un kilometraje se vuelve operación de turno (entra el conductor)',
     API,
     "@exige(MAESTROS_FLOTA, 'verificar un kilometraje')",
     "@exige(Roles.LECTURA_FLOTA, 'verificar un kilometraje')",
     [T_CONF]),

    ('quién verificó se toma del cuerpo del request y no del token',
     API,
     "        fila = verificacion.verificar(lectura_id=lectura_id,\n"
     "                                      usuario_id=_usuario_id())",
     "        _d = request.get_json(silent=True) or {}\n"
     "        fila = verificacion.verificar(\n"
     "            lectura_id=lectura_id,\n"
     "            usuario_id=int(_d['usuario_id']) if 'usuario_id' in _d else _usuario_id())",
     [T_CONF]),

    ('la lectura suelta deja de decir con qué confianza nació',
     API,
     "                    'origen': fila.origen, 'confianza': fila.confianza,\n"
     "                    'motivo_dudosa': fila.motivo_dudosa}), 201",
     "                    'origen': fila.origen}), 201",
     [T_CONF]),

    # ── La pantalla ──────────────────────────────────────────────────────
    ('el tablero deja de avisar que hay kilometrajes sin verificar',
     JS, "  if (!n) return '';", "  if (true) return '';",
     [T_RENDER]),

    ('la cola oculta que la lectura no tiene foto',
     JS,
     "    : `<span style=\"color:var(--yellow);font-size:12px\">sin foto del tablero —\n"
     "         confirmarla es tu palabra, no la de una foto</span>`;",
     "    : '';",
     [T_RENDER]),

    ('la fila deja de mostrar el motivo de la duda',
     JS,
     "    <div style=\"font-size:12px;color:var(--yellow);margin:2px 0 6px\">${p.motivo || ''}</div>",
     "    <div></div>",
     [T_RENDER]),

    ('el health deja de publicar la deuda del kilómetro en el tablero',
     JS, "  if (h.lecturas_dudosas_pendientes > 0) {",
     "  if (false && h.lecturas_dudosas_pendientes > 0) {",
     [T_RENDER]),

    ('la URL de la cola se arma concatenando en vez de escribirse entera',
     JS, "const FLOTA_DUDOSAS_URL = '/flota/odometro/dudosas';",
     "const FLOTA_DUDOSAS_URL = '/flota/odometro/' + 'dud' + 'osas';",
     [T_TRINQ]),

    # El trinquete global NO la puede matar, y está medido: trocea la ruta en
    # `/flota/odometro/` y `/verificar`, y `/verificar` ya existe en el PWA por
    # `reposicion.js` (`/api/reposicion/verificar-stock`). Por eso la mata el
    # guard propio de `test_confianza_odometro.py`, que exige la URL entera.
    ('la URL de verificar se arma concatenando',
     JS, "const FLOTA_VERIFICAR_URL = (id) => `/flota/odometro/${id}/verificar`;",
     "const FLOTA_VERIFICAR_URL = (id) => `/flota/odometro/${id}/` + 'verif' + 'icar';",
     [T_TRINQ, T_CONF]),
]


def _correr(tests):
    """Corre pytest y devuelve `(veredicto, resumen)`.

    `veredicto` ∈ {`rojo`, `verde`, `no_juzgado`}. **El returncode no alcanza**:
    un archivo cuyos tests se saltan sale 0, igual que uno que pasó.
    """
    proc = subprocess.run(
        [str(RAIZ / 'venv/bin/python'), '-m', 'pytest', *tests, '-q',
         '-m', 'not postgres', '-p', 'no:randomly', '--tb=no'],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=1800,
        env={**os.environ, 'TZ': 'UTC'},
    )
    salida = proc.stdout + proc.stderr
    lineas = [l for l in salida.strip().splitlines() if l.strip()]
    resumen = lineas[-1] if lineas else '(sin salida)'

    n = {}
    for clave in ('failed', 'passed', 'error', 'skipped'):
        m = re.search(rf'(\d+) {clave}', resumen)
        n[clave] = int(m.group(1)) if m else 0
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
        texto = ruta.read_text(encoding='utf-8')     # foto fresca, ver encabezado
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

#!/usr/bin/env python
"""
Arnés de mutación de la fase 2 — taller y garantía.

Cada mutación rompe UNA propiedad a propósito y exige que la suite se ponga
roja. Un test que pasa con la propiedad rota no verifica la propiedad: verifica
que el código corre.

## Las tres trampas que este arnés evita, todas cobradas antes en este repo

1. **Un test SALTADO sale con exit 0 igual que uno que pasó.** El arnés del
   hallazgo (2026-09-01) reportó «sobrevivió» sobre tests que se saltaban,
   porque le recortó el `PATH` al subproceso y `node` desapareció. Acá se
   distingue MUERTA de NO SE JUZGÓ **parseando el resumen de pytest**, no el
   returncode, y se le pasa el entorno completo (`os.environ`) al subproceso.
2. **La base tiene que estar en VERDE antes de mutar nada.** Con la base roja,
   toda mutación sale «muerta» y el arnés reporta N/N sobre nada.
3. **El detector en las dos direcciones.** Varias mutaciones no rompen el camino
   feliz sino el guard: `_INVERSA` marca las que exigen que la vía SANA siga
   funcionando, y se verifica que el fallo no sea el mismo test en todas.

Uso:
    venv/bin/python scripts/mutacion/taller.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if not (RAIZ / 'flota').exists():          # ejecutado desde otro cwd
    RAIZ = Path(__file__).resolve().parents[2]

DOM = RAIZ / 'flota' / 'dominio' / 'taller.py'
ADA = RAIZ / 'flota' / 'adaptadores' / 'taller.py'
GAS = RAIZ / 'flota' / 'adaptadores' / 'gastos.py'
MED = RAIZ / 'flota' / 'adaptadores' / 'medicion.py'
API = RAIZ / 'flota' / 'api' / 'taller.py'
MOD = RAIZ / 'flota' / 'adaptadores' / 'modelos.py'
JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'

#: `test_trinquetes_flota.py` NO está acá, y el motivo se declara: al momento de
#: correr esto, el trinquete de rutas huérfanas está rojo por endpoints de OTRO
#: agente (preventivo), y con la base roja toda mutación sale «muerta» y el N/N
#: no vale nada. La propiedad que ese trinquete cubre para esta fase —que las
#: URL vayan escritas enteras— la cubre
#: `test_render_taller_js.py::TestLasURLVanEnterasYNoConcatenadas`, que mira el
#: archivo directamente y no depende de las rutas de nadie más.
SUITE = ['tests/flota/test_garantia.py', 'tests/flota/test_taller.py',
         'tests/flota/test_render_taller_js.py',
         'tests/flota/test_gastos.py']

#: (archivo, texto original, texto mutado, qué propiedad rompe)
#:
#: `#` al principio de la descripción = la mutación ataca el GUARD y no el
#: camino feliz: lo que tiene que fallar es un test que exige que la vía sana
#: funcione.
MUTACIONES = [
    # ── El cálculo de la garantía ────────────────────────────────────────
    (DOM, 'return date(anio, mes, min(dia.day, monthrange(anio, mes)[1]))',
     'return date(anio, mes, dia.day) if dia.day <= 28 else date(anio, mes, 28)',
     'el recorte de fin de mes se vuelve un tope fijo en 28'),
    (DOM, "    total = (dia.year * 12 + (dia.month - 1)) + meses",
     "    total = (dia.year * 12 + (dia.month - 1)) + meses + 1",
     'la garantía dura un mes más de lo pactado'),
    (DOM, '    return sumar_meses(dia_trabajo, meses)',
     '    return sumar_meses(dia_trabajo, meses - 1)',
     'un mes menos de garantía — el error caro: se deja de reclamar'),
    (DOM, '    return km_trabajo + km_garantia',
     '    return km_garantia',
     'el km de vencimiento se calcula sin el odómetro del trabajo'),
    (DOM, "    if meses <= 0:\n        raise ValueError(\n            'una garantía de cero meses",
     "    if meses < 0:\n        raise ValueError(\n            'una garantía de cero meses",
     'cero meses deja de levantar y produce una garantía que vence hoy'),

    # ── Vigencia: las dos dimensiones y el tercer estado ─────────────────
    (DOM, "    por_fecha = SIN_DATO if hasta_fecha is None else (dia <= hasta_fecha)",
     "    por_fecha = SIN_DATO if hasta_fecha is None else (dia < hasta_fecha)",
     'el día del límite deja de estar cubierto'),
    (DOM, "              else (km_actual <= hasta_km))",
     "              else (km_actual < hasta_km))",
     'el kilómetro del límite deja de estar cubierto'),
    (DOM, "    por_km = (SIN_DATO if (hasta_km is None or km_actual is None)",
     "    por_km = (SIN_DATO if (hasta_km is None)",
     'sin odómetro conocido se compara contra None en vez de decir SIN_DATO'),
    (DOM, "    if por_fecha is True or por_km is True:",
     "    if por_fecha is True and por_km is True:",
     'la combinación pasa de «o» a «y» y deja de proponer el caso'),
    (DOM, "    elif por_fecha is SIN_DATO and por_km is SIN_DATO:",
     "    elif por_fecha is SIN_DATO or por_km is SIN_DATO:",
     'una dimensión sin juzgar contamina el veredicto entero'),
    (DOM, "    return v['vigente'] is True",
     "    return bool(v['vigente'])",
     'SIN_DATO se cuela como vigente — es verdadero en contexto booleano'),

    # ── Vocabularios ─────────────────────────────────────────────────────
    (DOM, "    if sistema not in SISTEMAS:\n        raise ValueError(",
     "    if False:\n        raise ValueError(",
     '# un sistema inventado deja de reventar y la búsqueda no lo encuentra'),
    (DOM, "    return sistema == 'otro'",
     "    return False",
     '`otro` deja de exigir descripción'),

    # ── El adaptador: regla 3 y el ancla ─────────────────────────────────
    (ADA, "                                  origen=OrigenLectura.OT)",
     "                                  origen=OrigenLectura.HALLAZGO)",
     'la lectura de la OT dice que nació de un hallazgo'),
    (ADA, "    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()",
     "    return ts.date()",
     'el día de la garantía se calcula en UTC en vez de en Bogotá'),
    (ADA, "        hasta_km = (dom.km_fin_garantia(orden.lectura.valor_km, kms)",
     "        hasta_km = (dom.km_fin_garantia(km_actual_de(orden.vehiculo_id), kms)",
     'la garantía por km se calcula contra el odómetro de hoy'),

    # ── El adaptador: coherencia de la garantía ──────────────────────────
    (ADA, "    elif meses is not None or kms is not None:\n        raise OrdenInvalida(",
     "    elif False:\n        raise OrdenInvalida(",
     'un `no` con plazos deja de levantar y se guarda en silencio'),
    (ADA, "        if meses is None and kms is None:\n            raise OrdenInvalida(",
     "        if False:\n            raise OrdenInvalida(",
     'un `si` sin ningún plazo pasa: garantía que se ve verde y no cubre'),
    (ADA, "    if n <= 0:\n        raise OrdenInvalida(",
     "    if n < 0:\n        raise OrdenInvalida(",
     'cero meses de garantía deja de levantar en la frontera del adaptador'),
    (ADA, "        raise OrdenInvalida(f'{campo} no es un número entero: {valor!r}')",
     "        return None",
     'un plazo ilegible se guarda como ausente en vez de reventar'),

    # ── El adaptador: desenlace ──────────────────────────────────────────
    (ADA, "    if not fila.intervenciones:\n        raise OrdenInvalida(\n            f'la orden {orden_trabajo_id} no tiene ningún trabajo",
     "    if False:\n        raise OrdenInvalida(\n            f'la orden {orden_trabajo_id} no tiene ningún trabajo",
     'se puede cerrar una visita sin registro de qué se hizo'),
    (ADA, "    if not (motivo or '').strip():\n        raise OrdenInvalida(\n            'anular exige motivo",
     "    if False:\n        raise OrdenInvalida(\n            'anular exige motivo",
     'anular deja de exigir motivo escrito'),
    (ADA, "            adaptador_hallazgos.cerrar(",
     "            fila.hallazgo.estado = 'cerrado' or adaptador_hallazgos.cerrar(",
     'el hallazgo se cierra por una segunda vía en vez de por su puerta'),
    (ADA, "    if cerrar_hallazgo and fila.hallazgo_id is None:",
     "    if False and fila.hallazgo_id is None:",
     'pedir cerrar un hallazgo inexistente revienta con 500 en vez de 409'),
    (ADA, "        if hallazgo.vehiculo_id != vehiculo_id:",
     "        if False:",
     'una OT puede colgar el hallazgo de otro camión'),
    (ADA, "        if hallazgo.estado != EstadoHallazgo.ABIERTO:",
     "        if False:",
     'una OT puede nacer de un hallazgo ya cerrado'),

    # ── El adaptador: la factura ─────────────────────────────────────────
    (ADA, "    ya_facturadas = [f.id for f in filas if f.gasto_id is not None]",
     "    ya_facturadas = []",
     'el mismo trabajo se puede facturar dos veces e inflar el CPK'),
    (ADA, "    ajenas = [f.id for f in filas if f.orden_trabajo_id != orden.id]",
     "    ajenas = []",
     'una factura puede cubrir trabajos de otra visita'),
    (ADA, "    if categoria not in CATEGORIAS_DE_TALLER:",
     "    if False:",
     'una factura de taller entra como SOAT y cuelga de una lectura que miente'),
    (ADA, "            lectura_id=orden.lectura_id,",
     "            km=orden.lectura.valor_km,",
     'la factura ancla con km en vez de con la lectura de la orden'),

    # ── La búsqueda que vale la plata ────────────────────────────────────
    (ADA, "    vigentes = [s for s in salida if s['cubre']]",
     "    vigentes = list(salida)",
     'se proponen garantías vencidas y sin juzgar como si cubrieran'),
    (ADA, "        consulta = consulta.filter(Intervencion.sistema == sistema)",
     "        consulta = consulta",
     '# el filtro por sistema desaparece: se propone el embrague por unos frenos'),
    (ADA, "                        OrdenTrabajo.estado != 'anulada',",
     "                        OrdenTrabajo.estado != '',",
     'una visita anulada sigue dejando garantía'),
    (ADA, "                        Intervencion.garantia_declarada == 'si')",
     "                        Intervencion.garantia_declarada != 'zzz')",
     'las declaradas `no` y `sin_dato` entran a la lista de vigentes'),
    (ADA, "    return None if ultima is None else ultima.valor_km",
     "    return 0 if ultima is None else ultima.valor_km",
     'un vehículo sin lecturas se lee como 0 km y toda garantía sale vigente'),

    # ── La puerta del gasto ──────────────────────────────────────────────
    (GAS, "        if lectura.vehiculo_id != vehiculo_id:",
     "        if False:",
     'un gasto se puede colgar del odómetro de otro camión'),
    (GAS, "    if lectura_id is not None:\n        if km is not None:",
     "    if lectura_id is not None:\n        if False:",
     'km y lectura_id conviven y una de las dos se ignora en silencio'),
    (GAS, "        lectura = LecturaOdometro.query.get(lectura_id)\n        if lectura is None:",
     "        lectura = LecturaOdometro.query.get(lectura_id)\n        if False:",
     'una lectura inexistente revienta con 500 en vez de con un 409 legible'),

    # ── El health ────────────────────────────────────────────────────────
    (MED, "            .filter(OrdenTrabajo.estado == 'cerrada',",
     "            .filter(OrdenTrabajo.estado != 'zzz',",
     '# los trabajos de órdenes abiertas cuentan como factura perdida'),
    (MED, "        return _contar(OrdenTrabajo.query.filter_by(estado='abierta'))",
     "        return _contar(OrdenTrabajo.query)",
     'las órdenes cerradas cuentan como abiertas'),
    (MED, "        return sum(len(garantias_vigentes_de(v.id, hoy))",
     "        return 0 * sum(len(garantias_vigentes_de(v.id, hoy))",
     'el contador de garantías vivas se apaga y siempre da 0'),

    # ── La frontera ──────────────────────────────────────────────────────
    (API, "    intrusos = [c for c in CAMPOS_CALCULADOS if c in datos]",
     "    intrusos = []",
     'el cuerpo puede traer la fecha de vencimiento y se ignora en silencio'),
    (API, "        'km_actual': km,",
     "        'km_actual': km or 0,",
     'un vehículo sin odómetro reporta 0 km en vez de «no se sabe»'),

    # ── El esquema ───────────────────────────────────────────────────────
    (MOD, '''            "CASE WHEN garantia_meses IS NULL"
            "     THEN garantia_hasta_fecha IS NULL"
            "     ELSE garantia_hasta_fecha IS NOT NULL END",''',
     '            "1 = 1",',
     'el CHECK deja pasar un vencimiento sin plazo que lo justifique'),

    # ── La pantalla ──────────────────────────────────────────────────────
    (JS, "  const vivas = (FLOTA_TALLER.garantias || []).filter(g => g.sistema === sel.value);",
     "  const vivas = (FLOTA_TALLER.garantias || []);",
     '# la propuesta ignora el sistema elegido'),
    (JS, "  if (!vivas.length) { cont.innerHTML = ''; return; }",
     "  { cont.innerHTML = ''; return; }",
     'la propuesta de garantía no se pinta nunca'),
    (JS, "  if (h.trabajos_sin_factura > 0) {",
     "  if (false) {",
     'el tablero deja de contar los trabajos sin factura'),
    (JS, "  intervenciones: (id) => `/flota/ordenes/${id}/intervenciones`,",
     "  intervenciones: (id) => `/flota/ordenes/${id}/` + 'intervenciones',",
     'la URL se arma por concatenación y el endpoint queda sin consumidor'),
]


def _resumen(salida: str):
    """`(passed, failed, errors, skipped)` del resumen de pytest.

    **Se parsea el resumen, no el returncode.** Un test saltado sale con exit 0
    igual que uno que pasó, y esa confusión es la que produjo un falso 26/26 el
    2026-09-01.
    """
    ultima = ''
    for linea in salida.strip().splitlines():
        if re.search(r'\d+ (passed|failed|error)', linea):
            ultima = linea
    def _n(que):
        m = re.search(rf'(\d+) {que}', ultima)
        return int(m.group(1)) if m else 0
    return _n('passed'), _n('failed'), _n('error'), _n('skipped'), ultima


def _correr():
    proc = subprocess.run(
        [str(RAIZ / 'venv' / 'bin' / 'python'), '-m', 'pytest', *SUITE,
         '-q', '-x', '-m', 'not postgres', '-p', 'no:randomly'],
        cwd=str(RAIZ), capture_output=True, text=True,
        # **El entorno completo.** Sin el PATH real, `node` no existe, los
        # tests de render se SALTAN, y un skip en verde se lee como «la
        # mutación sobrevivió». Es el defecto que este arnés persigue,
        # producido por el arnés mismo.
        env={**os.environ, 'TZ': 'UTC'})
    return _resumen(proc.stdout + proc.stderr)


def main():
    print('Verificando la base en verde antes de mutar nada…')
    passed, failed, errors, skipped, linea = _correr()
    if failed or errors:
        print(f'  BASE ROJA: {linea}')
        print('  Con la base roja toda mutación sale «muerta» y el N/N no vale.')
        return 1
    if skipped:
        print(f'  AVISO: {skipped} saltado(s) en la base — {linea}')
    print(f'  base en verde: {linea}\n')

    muertas, vivas, no_juzgadas = [], [], []
    fallos_por_mutacion = {}
    for i, (archivo, viejo, nuevo, que) in enumerate(MUTACIONES, 1):
        original = archivo.read_text(encoding='utf-8')
        if viejo not in original:
            print(f'{i:2}. ⚠️  NO APLICA — el texto cambió: {que}')
            no_juzgadas.append((i, que, 'texto no encontrado'))
            continue
        if original.count(viejo) != 1:
            print(f'{i:2}. ⚠️  NO APLICA — {original.count(viejo)} coincidencias: {que}')
            no_juzgadas.append((i, que, 'texto ambiguo'))
            continue
        archivo.write_text(original.replace(viejo, nuevo, 1), encoding='utf-8')
        try:
            p, f, e, s, linea = _correr()
        finally:
            archivo.write_text(original, encoding='utf-8')

        if f or e:
            print(f'{i:2}. ✅ MUERTA — {que}')
            muertas.append((i, que))
            fallos_por_mutacion[i] = linea
        elif s and not f and not e:
            # Un skip NO es una muerte. Distinguirlo es el punto entero.
            print(f'{i:2}. ⚠️  NO SE JUZGÓ ({s} saltado[s]) — {que}')
            no_juzgadas.append((i, que, linea))
        else:
            print(f'{i:2}. ❌ SOBREVIVIÓ — {que}')
            vivas.append((i, que))

    n = len(MUTACIONES)
    print(f'\n{len(muertas)}/{n} muertas · {len(vivas)} sobrevivieron · '
          f'{len(no_juzgadas)} no se juzgaron')
    for i, que in vivas:
        print(f'  SOBREVIVIÓ  #{i}: {que}')
    for i, que, motivo in no_juzgadas:
        print(f'  SIN JUZGAR  #{i}: {que} ({motivo})')
    return 0 if len(muertas) == n else 2


if __name__ == '__main__':
    sys.exit(main())

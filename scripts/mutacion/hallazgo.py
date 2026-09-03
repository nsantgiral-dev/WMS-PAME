"""Mutación: rompe una propiedad a la vez y exige que la suite se ponga roja.

Un test que pasa no dice nada por sí solo — puede estar midiendo algo que la vía
sana satisface por construcción. Lo que lo vuelve un trinquete es que MUERA
cuando la propiedad se rompe.
"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

MUTACIONES = [
    # (etiqueta, archivo, viejo, nuevo, tests que deben morir)
    ('M1  plazo ignorado (todo vence hoy)',
     'flota/dominio/inspeccion.py',
     'return dia_reporte + timedelta(days=dias_de_plazo(criticidad))',
     'return dia_reporte',
     'TestNaceConSuReloj'),

    ('M2  limite = el instante del reporte (bloqueante nace vencido)',
     'flota/adaptadores/hallazgos.py',
     '    return inicio_del_dia_utc(ultimo_dia + timedelta(days=1))',
     '    return reportado_ts',
     'TestNaceConSuReloj'),

    ('M3  el dia se calcula en UTC, no en Bogota',
     'flota/adaptadores/hallazgos.py',
     """    dia_reporte = (reportado_ts.replace(tzinfo=timezone.utc)
                   .astimezone(TZ_BOGOTA).date())""",
     '    dia_reporte = reportado_ts.date()',
     'TestNaceConSuReloj'),

    # El guard real es el `if ... raise`, no el indexado: mutar el `return` a
    # `.get(x, 30)` deja codigo INALCANZABLE y la mutacion sobrevive sin decir
    # nada del trinquete. Se muta la guarda.
    ('M4  criticidad desconocida degrada a menor',
     'flota/dominio/inspeccion.py',
     """    if criticidad not in DIAS_DE_PLAZO:
        raise ValueError(
            f'criticidad desconocida: {criticidad!r}. '
            f'Las válidas son {sorted(DIAS_DE_PLAZO)}.'
        )
    return DIAS_DE_PLAZO[criticidad]""",
     "    return DIAS_DE_PLAZO.get(criticidad, 30)",
     'TestNaceConSuReloj'),

    ('M5  siempre nace una lectura nueva (ruido de ts duplicado)',
     'flota/adaptadores/hallazgos.py',
     '    if ultima is not None and ultima.valor_km == km:\n        return ultima',
     '    if False:\n        return ultima',
     'TestElOdometroEntraPorLaMismaPuerta'),

    ('M6  siempre se reutiliza la ultima (se descarta el km tecleado)',
     'flota/adaptadores/hallazgos.py',
     '    if ultima is not None and ultima.valor_km == km:\n        return ultima',
     '    if ultima is not None:\n        return ultima',
     'TestElOdometroEntraPorLaMismaPuerta'),

    ('M7  el hallazgo se salta la monotonia del odometro',
     'flota/adaptadores/hallazgos.py',
     '    dom_odo.validar_lectura(',
     '    _ = lambda *a, **k: None; _(',
     'TestElOdometroEntraPorLaMismaPuerta'),

    ('M8  nada es linea base (el desorden viejo se le cuenta a alguien)',
     'flota/adaptadores/hallazgos.py',
     '            linea_base=(custodia is None or bool(custodia.linea_base)),',
     '            linea_base=False,',
     'TestLineaBaseYCustodia'),

    ('M9  todo es linea base (el indicador dice cero para siempre)',
     'flota/adaptadores/hallazgos.py',
     '            linea_base=(custodia is None or bool(custodia.linea_base)),',
     '            linea_base=True,',
     'TestLineaBaseYCustodia'),

    ('M10 la custodia no se deriva (el dano no dice bajo quien aparecio)',
     'flota/adaptadores/hallazgos.py',
     '            custodia_id=custodia.id if custodia is not None else None,',
     '            custodia_id=None,',
     'TestLineaBaseYCustodia'),

    ('M11 descartar sin motivo pasa por el adaptador',
     'flota/adaptadores/hallazgos.py',
     """    if not (motivo or '').strip():
        raise HallazgoInvalido(
            'descartar exige motivo escrito: sin él, un hallazgo incómodo y uno '
            'que de verdad no era nada se ven exactamente igual.'
        )""",
     '    pass',
     'TestLasTresSalidas'),

    ('M12 el aplazamiento se guarda en motivo_cierre (nombre que miente)',
     'flota/adaptadores/hallazgos.py',
     "    fila.bitacora = f'{fila.bitacora}\\n{sello}' if fila.bitacora else sello",
     "    fila.motivo_cierre = f'{fila.motivo_cierre}\\n{sello}' if fila.motivo_cierre else sello",
     'TestLasTresSalidas'),

    ('M13 la bitacora pisa en vez de acumular',
     'flota/adaptadores/hallazgos.py',
     "    fila.bitacora = f'{fila.bitacora}\\n{sello}' if fila.bitacora else sello",
     '    fila.bitacora = sello',
     'TestLasTresSalidas'),

    ('M14 aplazar mueve tambien el origen del reloj',
     'flota/adaptadores/hallazgos.py',
     '    fila.aplazado_veces = (fila.aplazado_veces or 0) + 1',
     '    fila.aplazado_veces = (fila.aplazado_veces or 0) + 1\n    fila.reportado_ts = fila.reportado_ts + timedelta(days=7)',
     'TestLasTresSalidas'),

    ('M15 cerrar dos veces es idempotente en silencio',
     'flota/adaptadores/hallazgos.py',
     "    if fila.estado != EstadoHallazgo.ABIERTO:",
     "    if False:",
     'TestLasTresSalidas'),

    ('M16 el orden ignora la severidad',
     'flota/adaptadores/hallazgos.py',
     "    return sorted(filas, key=lambda h: (_PESO[h.criticidad], h.reportado_ts))",
     '    return sorted(filas, key=lambda h: h.reportado_ts)',
     'TestElOrdenDeLaLista'),

    ('M17 sin el CHECK de desenlace la base acepta lo incoherente',
     'flota/adaptadores/modelos.py',
     "            name='ck_flota_hallazgo_desenlace',",
     "            name='ck_flota_hallazgo_desenlace_APAGADO') if False else db.CheckConstraint('1=1', name='ck_x',",
     'TestLaBaseImponeLoQueElAdaptadorPromete'),

    ('M18 lectura_id nullable: un dano sin kilometraje entra (regla 3)',
     'flota/adaptadores/modelos.py',
     "    lectura_id = db.Column(db.Integer, db.ForeignKey('flota_lectura_odometro.id'),\n                           nullable=False)",
     "    lectura_id = db.Column(db.Integer, db.ForeignKey('flota_lectura_odometro.id'),\n                           nullable=True)",
     'TestLaBaseImponeLoQueElAdaptadorPromete'),

    ('M19 sin el CHECK de descripcion entra una en blanco',
     'flota/adaptadores/modelos.py',
     "        db.CheckConstraint('length(trim(descripcion)) > 0',\n                           name='ck_flota_hallazgo_descripcion'),",
     "        db.CheckConstraint('1 = 1', name='ck_flota_hallazgo_descripcion'),",
     'TestLaBaseImponeLoQueElAdaptadorPromete'),

    ('M20 se valida la descripcion DESPUES de escribir la lectura',
     'flota/adaptadores/hallazgos.py',
     """    if not (descripcion or '').strip():
        raise HallazgoInvalido(
            'un hallazgo sin descripción no lo puede atender nadie: quien lo '
            'lea mañana no va a saber qué buscar en el vehículo.'
        )""",
     '    pass',
     'TestNaceConSuReloj'),
    # ── El lector: sin estos, la tabla es captura sin nadie que la mire ──
    ('M21 el health no publica los danos',
     'flota/api/health.py',
     "    'hallazgos_abiertos',\n    'hallazgos_vencidos',",
     '',
     'HEALTH'),

    ('M22 vencidos se cuenta como abiertos (el rojo deja de distinguir)',
     'flota/adaptadores/medicion.py',
     '        return sum(1 for h in abiertos if vencido(h.a_dominio(), ahora))',
     '        return len(abiertos)',
     'HEALTH'),

    ('M23 vencidos siempre cero (el vencido se vuelve invisible)',
     'flota/adaptadores/medicion.py',
     '        return sum(1 for h in abiertos if vencido(h.a_dominio(), ahora))',
     '        return 0',
     'HEALTH'),

    ('M24 lo cerrado sigue contando como abierto',
     'flota/adaptadores/medicion.py',
     '        return _contar(Hallazgo.query.filter_by(estado=EstadoHallazgo.ABIERTO))',
     '        return _contar(Hallazgo.query)',
     'HEALTH'),

    ('M25 el tablero no pinta los vencidos',
     'app/static/pwa/flota.js',
     "  if (h.hallazgos_vencidos > 0) {",
     '  if (false) {',
     'RENDER'),

    ('M26 abiertos y vencidos colapsan en una sola linea',
     'app/static/pwa/flota.js',
     "  if (h.hallazgos_abiertos > 0) {",
     '  if (false) {',
     'RENDER'),
]

#: Cada mutacion declara la CLASE de test que debe matarla. Estas dos etiquetas
#: apuntan a otro archivo — el guard vive donde vive su consumidor.
ARCHIVOS_POR_CLASE = {
    'HEALTH': 'tests/flota/test_endpoints_hallazgo.py::TestElHealthLosCuenta',
    'RENDER': ('tests/flota/test_render_salud_js.py::'
               'TestLosDanosSeVenSinAbrirSeisExpedientes'),
}

ARCHIVO_TESTS = 'tests/flota/test_hallazgo_nace.py'


def correr(clase):
    objetivo = ARCHIVOS_POR_CLASE.get(clase, f'{ARCHIVO_TESTS}::{clase}')
    r = subprocess.run(
        ['venv/bin/python', '-m', 'pytest', objetivo,
         '-q', '-m', 'not postgres', '--no-header', '-x'],
        cwd=RAIZ, capture_output=True, text=True,
        env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
             'HOME': str(Path.home())})
    salida = (r.stdout or '')
    # Un PATH recortado dejaba a `node` fuera y los tests de render se SALTABAN:
    # returncode 0, y la mutacion figuraba como sobreviviente sin haberse
    # juzgado. Un skip en verde es el falso negativo que este repo persigue —
    # aca lo produjo el propio arnes de mutacion.
    if 'skipped' in salida:
        return 0, 'SKIP — el test no corrio: ' + salida[-200:]
    return r.returncode, salida[-300:]


def main():
    fallos = []
    for etiqueta, archivo, viejo, nuevo, clase in MUTACIONES:
        ruta = RAIZ / archivo
        respaldo = ruta.read_text()
        if viejo not in respaldo:
            print(f'  ⚠ {etiqueta}: el texto a mutar NO existe — mutación inválida')
            fallos.append(etiqueta)
            continue
        ruta.write_text(respaldo.replace(viejo, nuevo, 1))
        try:
            codigo, cola = correr(clase)
        finally:
            ruta.write_text(respaldo)
        if codigo == 0:
            marca = 'SE SALTÓ  ' if cola.startswith('SKIP') else 'SOBREVIVIÓ'
            print(f'  ✗ {marca}  {etiqueta}')
            fallos.append(etiqueta)
        else:
            print(f'  ✓ muerta      {etiqueta}')

    print()
    if fallos:
        print(f'{len(fallos)} de {len(MUTACIONES)} sin trinquete:')
        for f in fallos:
            print('   ·', f)
        sys.exit(1)
    print(f'{len(MUTACIONES)}/{len(MUTACIONES)} mutaciones muertas.')


if __name__ == '__main__':
    main()

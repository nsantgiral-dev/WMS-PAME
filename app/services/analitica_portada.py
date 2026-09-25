"""
📈 Analítica → 🎯 ¿Cómo vamos? — la portada (2026-09-24).

La empresa gana convirtiendo pedido en caja **rápido y sin fugas**. La portada
contesta eso en seis indicadores, cada uno **contra su meta**, con su tendencia
de 8 semanas, la variación contra el período anterior, su dueño y —al tocarlo—
el porqué y la lista de qué hacer.

## Una cifra, una fuente

El problema que esto arregla no era de diseño: **el mismo hecho daba cifras
distintas según la pantalla.** Por eso ningún indicador se calcula acá. Cada uno
lee la función que ya lo decide en su pantalla de detalle:

| Indicador | Lo decide | La misma cifra que |
|---|---|---|
| Llega a caja completo | `analitica_recorrido._guia` (valor sin fuga, cerrados) | 🧭 Recorrido |
| Ciclo de caja | `analitica_recorrido._guia` (mediana aprobado → liquidado) | 🧭 Recorrido |
| Plata en riesgo | `analitica_fugas.FUGAS` (calle + sin pago + crédito no autorizado + documentos de plata) | 💸 Fugas |
| Venta perdida | `analitica_fugas.FUGAS['venta_perdida']` | 💸 Fugas |
| Nivel de servicio | KPI diario `fill_rate` (`metricas/fill_rate.py`) | `/api/analitica/serie` |
| Exactitud de inventario | KPI diario `exactitud_inventario` | `/api/analitica/serie` |

Las cuatro primeras se miden **en vivo** sobre el período (así la portada nace
útil aunque el cron del KPI diario esté apagado); las dos últimas leen el KPI
diario guardado, que es donde viven. Las metas, los umbrales y el dueño viven en
el catálogo (`analitica_kpi.METRICAS`), una sola vez; el semáforo contra la
meta también (`analitica_kpi.semaforo`).

## Cohortes, no días

Las cuatro en vivo son **cohortes**: los pedidos que entraron en la ventana (las
dos del recorrido) o los casos de la ventana con su estado de hoy (las de
fugas). Una ventana reciente está **censurada**: sus pedidos lentos todavía no
llegaron a liquidado. Por eso la tendencia de las últimas semanas del ciclo de
caja tiende a verse mejor de lo que es; se dice en «cómo se mide».

Cero Siesa: todo sale de la base del WMS.
"""
from datetime import date, datetime, timedelta

from app.utils.fecha import dia_operativo, rango_dia_operativo_utc

#: Los seis, en el orden de la historia: del pedido a la caja, y lo que la frena.
INDICADORES = ('llega_a_caja', 'ciclo_caja', 'plata_en_riesgo', 'fill_rate',
               'venta_perdida', 'exactitud_inventario')

#: Se miden en vivo sobre el período; los demás leen el KPI diario guardado.
EN_VIVO = ('llega_a_caja', 'ciclo_caja', 'plata_en_riesgo', 'venta_perdida')

SEMANAS_TENDENCIA = 8

#: Las fugas que son **plata** que salió del cliente o de la bodega y todavía
#: no llegó a caja ni a Siesa. Las demás fugas son mercancía o trabajo.
FUGAS_PLATA_EN_RIESGO = ('plata_en_la_calle', 'entregado_sin_pago',
                         'credito_no_autorizado', 'documentos_trabados')

#: De los documentos trabados, solo los que llevan plata: el recibo de caja, la
#: retención y la factura del despacho. Una nota crédito o un ajuste de conteo
#: trabados son un descuadre, no plata en riesgo.
TIPOS_JOB_DE_PLATA = ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'DESPACHO_F470')

#: Jobs que se reintentan desde 💰 Liquidación (su lista de jobs los filtra así);
#: el resto, desde la pestaña de Siesa.
TIPOS_JOB_LIQUIDACION = ('NOTA_CREDITO_FACTURA', 'RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET')

MAX_ACCIONES = 10
MAX_PEDIDOS = 6

OK, INCOMPLETO, AUSENTE = 'OK', 'INCOMPLETO', 'AUSENTE'


def _dias(d1, d2):
    return (d2 - d1).days + 1


def ventanas_semanales(hasta: date, semanas: int = SEMANAS_TENDENCIA) -> list:
    """`[(desde, hasta)]` de 7 días, la última terminando en `hasta`. Ventanas
    móviles y no semanas de calendario: la última siempre tiene 7 días."""
    salida = []
    for i in range(semanas - 1, -1, -1):
        fin = hasta - timedelta(days=7 * i)
        salida.append((fin - timedelta(days=6), fin))
    return salida


def _medida(valor, n=None, estado=OK, motivo=None, es_piso=False, **extra):
    d = {'valor': None if valor is None else float(valor), 'n': n, 'estado': estado,
         'motivo': motivo, 'es_piso': bool(es_piso)}
    d.update(extra)
    return d


def _ausente(motivo, n=None, **extra):
    return _medida(None, n, AUSENTE, motivo, **extra)


class Medicion:
    """Lo que la portada lee, calculado **una vez** por petición.

    Carga la cohorte del recorrido sobre la ventana más ancha que hace falta
    (período, período anterior y las 8 semanas) y la reparte por día de entrada
    con la misma regla que `analitica_recorrido.cohorte` —por eso un período
    da exactamente la cifra de la pantalla del Recorrido—. Las fugas se piden
    por ventana con un solo contexto (`_Ctx`), que ya memoriza lo compartido.
    """

    def __init__(self, desde: date, hasta: date, almacen_id=None, hoy: date = None):
        from app.services import analitica_fugas as fg
        self.desde, self.hasta, self.almacen_id = desde, hasta, almacen_id
        self.hoy = hoy or dia_operativo()
        self.ant_desde, self.ant_hasta = fg.periodo_anterior(desde, hasta)
        self.semanas = ventanas_semanales(hasta)
        self._inicio = min(self.ant_desde, self.semanas[0][0])
        self._pedidos = None
        self._guias = {}
        self._fugas = {}
        self._ctx = fg._Ctx(almacen_id)
        # Mismo contrato que `calcular_fugas`: lo que depende de «hoy» (rutas
        # sin fecha, fallidos de hoy) se cuenta en el período elegido.
        self._ctx.una_vez('periodo_actual', lambda: (desde, hasta))

    # ── La cohorte del recorrido ────────────────────────────────────────────

    @property
    def pedidos(self):
        if self._pedidos is None:
            from app.services.analitica_recorrido import cohorte
            todos, _ = cohorte(self._inicio, self.hasta, self.almacen_id)
            self._pedidos = [p for p in todos if p.enlazado]
        return self._pedidos

    def cohorte_de(self, d1, d2):
        ini, fin = rango_dia_operativo_utc(d1, d2)
        return [p for p in self.pedidos if p.entrada_at is not None and ini <= p.entrada_at < fin]

    def guia(self, d1, d2):
        if (d1, d2) not in self._guias:
            from app.services.analitica_recorrido import _guia
            self._guias[(d1, d2)] = _guia(self.cohorte_de(d1, d2))
        return self._guias[(d1, d2)]

    # ── Las fugas ───────────────────────────────────────────────────────────

    def fuga(self, clave, d1, d2):
        if (clave, d1, d2) not in self._fugas:
            from app.services import analitica_fugas as fg
            res = fg.FUGAS[clave].fn(d1, d2, self._ctx)
            if clave == 'documentos_trabados':
                res = fg.Resultado([c for c in res.casos
                                    if (c.detalle or {}).get('tipo') in TIPOS_JOB_DE_PLATA],
                                   res.sin_dato, res.faltan, res.extra)
            self._fugas[(clave, d1, d2)] = res
        return self._fugas[(clave, d1, d2)]

    # ── Cada indicador en una ventana ───────────────────────────────────────

    def medir(self, clave, d1, d2) -> dict:
        return getattr(self, '_m_' + clave)(d1, d2)

    def _m_llega_a_caja(self, d1, d2):
        g = self.guia(d1, d2)
        c = g['valor_sin_fuga_cerrados']
        sf = g['valor_sin_fuga']
        extra = {'valor_base': c['valor_base'], 'pedidos_base': c['pedidos_base'],
                 'valor_sin_fuga': sf['valor_sin_fuga'],
                 'pedidos_sin_valor': sf['pedidos_sin_valor']}
        if c['tasa'] is None:
            return _ausente('ningún pedido de la cohorte con valor terminó todavía '
                            '(completo o caído): no hay sobre qué medir', n=0, **extra)
        motivo = (f"{sf['pedidos_sin_valor']} pedido(s) sin valor en Siesa no entran"
                  if sf['pedidos_sin_valor'] else None)
        return _medida(c['tasa'], c['pedidos_base'], INCOMPLETO if motivo else OK,
                       motivo, **extra)

    def _m_ciclo_caja(self, d1, d2):
        c = self.guia(d1, d2)['ciclo_caja']
        extra = {'p90': c['p90_dias'], 'liquidados': c['liquidados'],
                 'sin_marca': c['sin_marca'], 'negativos': c['negativos']}
        if not c['n']:
            motivo = ('ningún pedido de la cohorte llegó a liquidado sin caerse'
                      if not c['liquidados'] else
                      f"{c['liquidados']} liquidado(s), ninguno con marca de aprobación "
                      'y de liquidación')
            return _ausente(motivo, n=0, **extra)
        motivo = (f"{c['sin_marca']} liquidado(s) sin marca de aprobación no entran"
                  if c['sin_marca'] else None)
        return _medida(c['mediana_dias'], c['n'], INCOMPLETO if motivo else OK, motivo, **extra)

    def _totales_de(self, claves, d1, d2):
        from app.services import analitica_fugas as fg
        pesos, casos, sin_valor, sin_dato, componentes = 0.0, 0, 0, [], []
        for k in claves:
            res = self.fuga(k, d1, d2)
            t = fg.totales(res)
            componentes.append({'clave': k, 'titulo': fg.FUGAS[k].titulo,
                                'pesos': t['pesos'], 'casos': t['casos'],
                                'sin_valor': (t['sin_valor'] or {}).get('casos'),
                                'sin_dato': t['sin_dato']})
            if t['sin_dato']:
                sin_dato.append(f"{fg.FUGAS[k].titulo}: {t['sin_dato']}")
                continue
            casos += t['casos'] or 0
            sin_valor += (t['sin_valor'] or {}).get('casos') or 0
            pesos += t['pesos'] or 0.0
        return pesos, casos, sin_valor, sin_dato, componentes

    def _m_plata_en_riesgo(self, d1, d2):
        pesos, casos, sin_valor, sin_dato, comp = self._totales_de(FUGAS_PLATA_EN_RIESGO, d1, d2)
        extra = {'casos': casos, 'sin_valor': sin_valor, 'componentes': comp}
        if sin_dato and not casos:
            return _ausente('; '.join(sin_dato), n=0, **extra)
        if casos and sin_valor == casos:
            return _ausente(f'{casos} caso(s), ninguno con valor conocido', n=casos, **extra)
        motivo = None
        if sin_valor:
            motivo = f'{sin_valor} caso(s) sin valor no suman: la cifra es un piso'
        if sin_dato:
            motivo = '; '.join(filter(None, [motivo] + sin_dato))
        return _medida(round(pesos, 2), casos, INCOMPLETO if motivo else OK, motivo,
                       es_piso=bool(sin_valor or sin_dato), **extra)

    def _m_venta_perdida(self, d1, d2):
        pesos, casos, sin_valor, sin_dato, comp = self._totales_de(('venta_perdida',), d1, d2)
        extra = {'casos': casos, 'sin_valor': sin_valor}
        if sin_dato:
            return _ausente('; '.join(sin_dato), n=0, **extra)
        if casos and sin_valor == casos:
            return _ausente(f'{casos} agotado(s), ninguno con precio de venta', n=casos, **extra)
        motivo = (f'{sin_valor} agotado(s) sin precio no suman: la cifra es un piso'
                  if sin_valor else None)
        return _medida(round(pesos, 2), casos, INCOMPLETO if motivo else OK, motivo,
                       es_piso=bool(sin_valor), **extra)


# ─────────────────────────────────────────────────────────────────────────────
# Los que viven en el KPI diario
# ─────────────────────────────────────────────────────────────────────────────

def _medir_kpi(clave, puntos):
    """Un período de la serie del KPI diario, en la forma de la portada."""
    from app.services import analitica_kpi as kpi
    a = kpi.agregar(clave, puntos)
    faltan = a.get('dias_sin_medir') or 0
    extra = {'numerador': a.get('numerador'), 'denominador': a.get('denominador'),
             'dias': a.get('dias'), 'dias_sin_medir': faltan}
    if a['valor'] is None:
        todos_sin_calcular = puntos and all(
            p['estado'] == 'AUSENTE' and (p.get('motivo') or '').startswith('sin calcular')
            for p in puntos if not p.get('en_curso'))
        motivo = ('el KPI diario no se calculó para estos días' if todos_sin_calcular
                  else a.get('motivo') or 'sin dato')
        return _ausente(motivo, n=a.get('n'), sin_calcular=bool(todos_sin_calcular), **extra)
    return _medida(a['valor'], a.get('n'), a['estado'], a.get('motivo'), **extra)


def _series_kpi(m: Medicion):
    """Una lectura de la tabla por métrica, sobre la ventana más ancha."""
    from app.services import analitica_kpi as kpi
    out = {}
    for clave in INDICADORES:
        if clave in EN_VIVO:
            continue
        puntos = kpi.serie(clave, m._inicio, m.hasta, m.almacen_id, hoy=m.hoy)
        out[clave] = {p['dia']: p for p in puntos}
    return out


def _tramo(serie, d1, d2):
    return [serie[(d1 + timedelta(days=i)).isoformat()] for i in range(_dias(d1, d2))
            if (d1 + timedelta(days=i)).isoformat() in serie]


# ─────────────────────────────────────────────────────────────────────────────
# El porqué y el qué hacer
# ─────────────────────────────────────────────────────────────────────────────

def _fila_pedido(p, extra=None):
    from app.services.analitica_recorrido import TITULOS, ETAPAS, TITULOS_ESTADO
    d = {'tipo': 'ver_pedido', 'pedido_clave': p.clave, 'numero': p.numero,
         'cliente': p.cliente, 'valor': float(p.valor) if p.valor is not None else None,
         'etapa': 'llegó hasta ' + TITULOS[ETAPAS[p.ultimo]].lower(),
         'estado': TITULOS_ESTADO[p.estado_final],
         'motivo': (p.fuga or p.detenido or {}).get('motivo')}
    d.update(extra or {})
    return d


def _por_que_llega(m: Medicion):
    from app.services.analitica_recorrido import EstadoFinal, TITULOS
    ps = m.cohorte_de(m.desde, m.hasta)
    grupos = {}
    for p in ps:
        perdidas = ([('cayo', p.fuga)] if p.fuga else []) + [('parcial', f) for f in p.parciales]
        for tipo, f in perdidas:
            g = grupos.setdefault((tipo, f.get('motivo')), {
                'tipo': tipo, 'motivo': f.get('motivo'), 'etapa': TITULOS.get(f.get('etapa')),
                'pedidos': 0, 'valor': 0.0, 'sin_valor': 0})
            g['pedidos'] += 1
            if p.valor is None:
                g['sin_valor'] += 1
            else:
                g['valor'] += float(p.valor)
    perdidas = sorted(grupos.values(), key=lambda g: (-g['valor'], -g['pedidos']))
    con_perdida = [p for p in ps if p.estado_final in (EstadoFinal.FUGA,
                                                      EstadoFinal.COMPLETO_CON_FUGA)]
    con_perdida.sort(key=lambda p: (p.valor is None, -(float(p.valor) if p.valor else 0)))
    return {'perdidas': perdidas[:8],
            'que_hacer': [_fila_pedido(p) for p in con_perdida[:MAX_PEDIDOS]]}


def _por_que_ciclo(m: Medicion):
    from app.services.analitica_recorrido import (ETAPAS, TITULOS, EstadoFinal, _embudo,
                                                  _horas)
    ps = m.cohorte_de(m.desde, m.hasta)
    tramos = []
    for e in _embudo(ps)[1:]:
        t = e['tiempo_desde_anterior'] or {}
        i = ETAPAS.index(e['etapa'])
        tramos.append({'desde': TITULOS[ETAPAS[i - 1]], 'hasta': e['titulo'],
                       'mediana_horas': t.get('mediana_horas'), 'n': t.get('n') or 0})
    # «El más lento» solo si de verdad se lleva la mayor parte: con todo en 0
    # (o empatado arriba) no hay uno que señalar.
    medidos = sorted((t for t in tramos if t['mediana_horas']), key=lambda t: -t['mediana_horas'])
    lento = medidos[0] if medidos and (len(medidos) == 1
                                       or medidos[0]['mediana_horas'] > medidos[1]['mediana_horas']) else None
    liquidados = []
    for p in ps:
        if p.ultimo == len(ETAPAS) - 1 and not p.fuga:
            h = _horas(p.marcas.get('aprobado'), p.marcas.get('liquidado'))
            if h is not None and h >= 0:
                liquidados.append((h / 24.0, p))
    liquidados.sort(key=lambda x: -x[0])
    ahora = datetime.utcnow()
    esperando = [(p, (ahora - p.entrada_at).total_seconds() / 86400.0) for p in ps
                 if p.estado_final in (EstadoFinal.EN_CURSO, EstadoFinal.DETENIDO)
                 and p.entrada_at is not None]
    esperando.sort(key=lambda x: -x[1])
    return {
        'tramos': tramos, 'tramo_mas_lento': lento,
        'que_hacer': ([_fila_pedido(p, {'dias': round(d, 1), 'grupo': 'esperando'})
                       for p, d in esperando[:MAX_PEDIDOS]]
                      + [_fila_pedido(p, {'dias': round(d, 1), 'grupo': 'lentos'})
                         for d, p in liquidados[:MAX_PEDIDOS]]),
    }


def _por_que_plata(m: Medicion):
    from app.services import analitica_fugas as fg
    acciones = []
    for c in m.fuga('plata_en_la_calle', m.desde, m.hasta).casos:
        det = c.detalle or {}
        acciones.append({'tipo': 'liquidar_ruta', 'ruta_id': det.get('ruta_id'),
                         'titulo': c.referencia, 'pesos': c.pesos, 'dias': det.get('dias'),
                         'detalle': f"{c.motivo} · {det.get('paradas_confirmadas', 0)} de "
                                    f"{det.get('paradas', 0)} paradas confirmadas",
                         'fuente': 'Plata en la calle'})
    for clave in ('entregado_sin_pago', 'credito_no_autorizado'):
        for c in m.fuga(clave, m.desde, m.hasta).casos:
            det = c.detalle or {}
            acciones.append({'tipo': 'ver_pedido' if c.pedido_clave else 'sin_accion',
                             'pedido_clave': c.pedido_clave, 'titulo': c.referencia,
                             'cliente': det.get('cliente') or None, 'pesos': c.pesos,
                             'dias': det.get('dias'),
                             'detalle': (None if clave == 'entregado_sin_pago' else c.motivo),
                             'fuente': fg.FUGAS[clave].titulo})
    for c in m.fuga('documentos_trabados', m.desde, m.hasta).casos:
        det = c.detalle or {}
        tipo = det.get('tipo')
        acciones.append({'tipo': 'reintentar', 'job_id': int(c.referencia.split()[-1])
                         if c.referencia.split()[-1].isdigit() else None,
                         'destino': 'liquidacion' if tipo in TIPOS_JOB_LIQUIDACION else 'siesa',
                         'titulo': c.motivo, 'pesos': c.pesos, 'dias': None,
                         'detalle': f"{det.get('intentos') or 0} intento(s)",
                         'pedido_clave': c.pedido_clave,
                         'fuente': fg.FUGAS['documentos_trabados'].titulo})
    # Lo sin valor primero (no saber cuánto vale no lo vuelve chico), después
    # de mayor a menor — el mismo orden que el detalle de 💸 Fugas.
    acciones.sort(key=lambda a: (a['pesos'] is not None, -(a['pesos'] or 0)))
    return {'que_hacer': acciones[:MAX_ACCIONES], 'total_acciones': len(acciones)}


def _por_que_venta_perdida(m: Medicion):
    from app.services import analitica_fugas as fg
    casos = m.fuga('venta_perdida', m.desde, m.hasta).casos
    return {'categorias': fg._agrupar(casos, lambda c: c.motivo)[:6],
            'que_hacer': [{'tipo': 'ver_fuga', 'fuga': 'venta_perdida',
                           'titulo': 'Ver cada agotado en 💸 Fugas'}] if casos else []}


_POR_QUE = {
    'llega_a_caja': _por_que_llega,
    'ciclo_caja': _por_que_ciclo,
    'plata_en_riesgo': _por_que_plata,
    'venta_perdida': _por_que_venta_perdida,
    'fill_rate': lambda m: {'que_hacer': [{'tipo': 'ir_tab', 'tab': 'tab-pedidos',
                                           'titulo': 'Ver pedidos pendientes'}]},
    'exactitud_inventario': lambda m: {'que_hacer': [{'tipo': 'ir_tab', 'tab': 'tab-inventario',
                                                      'titulo': 'Ir a conteo cíclico'}]},
}


# ─────────────────────────────────────────────────────────────────────────────
# La portada
# ─────────────────────────────────────────────────────────────────────────────

def _variacion(metrica, actual, anterior):
    from app.services import analitica_kpi as kpi
    if actual['valor'] is None or anterior['valor'] is None:
        return {'anterior': anterior['valor'], 'absoluta': None, 'favorable': None,
                'comparable': False,
                'motivo': 'uno de los dos períodos no tiene dato'}
    absoluta = actual['valor'] - anterior['valor']
    favorable = None if not absoluta else (absoluta > 0) == (metrica.direccion == kpi.SUBE_ES_BUENO)
    comparable = actual['estado'] == OK and anterior['estado'] == OK
    return {'anterior': anterior['valor'], 'absoluta': absoluta, 'favorable': favorable,
            'comparable': comparable,
            'motivo': None if comparable else
            'algún período está incompleto: la diferencia puede ser del dato, no del negocio'}


def medir_en_vivo(clave, desde, hasta, almacen_id=None, hoy=None, medicion=None) -> dict:
    """El período de un indicador en vivo (lo usa `analitica_kpi.resumen`)."""
    m = medicion or Medicion(desde, hasta, almacen_id, hoy)
    return m.medir(clave, desde, hasta)


def portada(desde: date, hasta: date, almacen_id=None, hoy: date = None,
            medicion: Medicion = None) -> dict:
    """Los seis indicadores, contra su meta. Ver el encabezado del módulo."""
    from app.services import analitica_kpi as kpi
    m = medicion or Medicion(desde, hasta, almacen_id, hoy)
    dias = _dias(desde, hasta)
    series = _series_kpi(m)
    tarjetas = []
    for clave in INDICADORES:
        met = kpi.METRICAS[clave]
        if clave in EN_VIVO:
            actual = m.medir(clave, desde, hasta)
            anterior = m.medir(clave, m.ant_desde, m.ant_hasta)
            semanas = [dict(m.medir(clave, a, b), desde=a.isoformat(), hasta=b.isoformat())
                       for a, b in m.semanas]
            origen = 'en_vivo'
        else:
            s = series[clave]
            actual = _medir_kpi(clave, _tramo(s, desde, hasta))
            anterior = _medir_kpi(clave, _tramo(s, m.ant_desde, m.ant_hasta))
            semanas = [dict(_medir_kpi(clave, _tramo(s, a, b)), desde=a.isoformat(),
                            hasta=b.isoformat()) for a, b in m.semanas]
            origen = 'kpi_diario'
        for sem in semanas:        # la tendencia no lleva el porqué de cada semana
            sem.pop('componentes', None)
        try:
            por_que = _POR_QUE[clave](m)
        except Exception as e:     # el porqué nunca tumba la cifra
            por_que = {'error': f'{type(e).__name__}: {e}'[:200], 'que_hacer': []}
        tarjetas.append({
            **met.to_dict(),
            'origen': origen,
            'periodo': actual,
            'semaforo': kpi.semaforo(clave, actual['valor'], dias=dias,
                                     es_piso=actual.get('es_piso', False)),
            'variacion': _variacion(met, actual, anterior),
            'tendencia': [dict(s, semaforo=kpi.semaforo(clave, s['valor'], dias=7,
                                                        es_piso=s.get('es_piso', False))['nivel'])
                          for s in semanas],
            'por_que': por_que,
        })
    niveles = [t['semaforo']['nivel'] for t in tarjetas]
    estado_kpi = kpi.estado(dias=dias, hoy=m.hoy)
    return {
        'tarjetas': tarjetas,
        'conteo': {n: niveles.count(n) for n in ('verde', 'amarillo', 'rojo', 'sin_dato')},
        'periodo': {'desde': desde.isoformat(), 'hasta': hasta.isoformat(), 'dias': dias},
        'anterior': {'desde': m.ant_desde.isoformat(), 'hasta': m.ant_hasta.isoformat()},
        'semanas': [{'desde': a.isoformat(), 'hasta': b.isoformat()} for a, b in m.semanas],
        'kpi_diario': {
            'encendido': estado_kpi.get('encendido'),
            'ultimo_calculo': estado_kpi.get('ultimo_calculo'),
            'dependen': [c for c in INDICADORES if c not in EN_VIVO],
            'como_encender': ('Variable ANALITICA_KPI=true en el servicio worker (con '
                              'HEAVY_SCHEDULERS=true): calcula cada día a las 04:30 Bogotá. '
                              'Mientras tanto, un administrador puede calcular a mano hasta '
                              f'{kpi.TOPE_DIAS_RECALCULO} días hacia atrás.'),
            'tope_dias_recalculo': kpi.TOPE_DIAS_RECALCULO,
        },
        'como_se_mide': {
            'en_vivo': ('Llega a caja, ciclo de caja, plata en riesgo y venta perdida se '
                        'calculan al abrir la portada, con la misma función que 🧭 Recorrido '
                        'y 💸 Fugas: la cifra es la misma en las tres pantallas.'),
            'cohorte': ('Llega a caja y ciclo de caja miran los pedidos que ENTRARON en el '
                        'período. Los más recientes todavía no terminan: en las últimas '
                        'semanas el ciclo sale más corto de lo que va a ser (los lentos aún '
                        'no llegan a caja).'),
            'plata': ('Plata en riesgo = lo cobrado en rutas sin liquidar + lo entregado sin '
                      'pago + el crédito que nadie autorizó + recibos, retenciones y facturas '
                      'trabados en Siesa, de lo que pasó en el período, con su estado de hoy.'),
            'tendencia': ('Cada punto de la tendencia son 7 días seguidos; el último termina '
                          'el día «hasta» del filtro.'),
            'metas': ('Las metas son provisionales hasta que la gerencia las confirme; el '
                      'semáforo compara contra ellas. «Sin dato» no es rojo: es que no hay '
                      'con qué medir.'),
            'piso': ('«al menos»: hay casos sin precio o sin valor que no suman. La cifra real '
                     'es mayor, y por eso un piso nunca se da por «en meta».'),
        },
    }


__all__ = ['INDICADORES', 'EN_VIVO', 'Medicion', 'portada', 'medir_en_vivo',
           'ventanas_semanales', 'FUGAS_PLATA_EN_RIESGO', 'TIPOS_JOB_DE_PLATA']

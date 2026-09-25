"""
Capa semántica y KPI diario del módulo 📈 Analítica (Fase 1, 2026-09-24).

Bajo la Ley 1116 la caja vale más: la gerencia necesita ver, día a día, si la
operación convierte pedidos en caja sin fugas. Este módulo es **el catálogo**
de las métricas con que se mide eso y **la tabla** donde queda lo que cada una
dio cada día (`analitica_kpi_diario`, m037kpi). Las tendencias y las alertas
(Fase 2) leen de acá; la pantalla de cada vista también.

## Las reglas

1. **Una métrica, una definición.** Cada entrada de `METRICAS` dice qué mide
   en palabras de gerencia, su unidad, hacia dónde es bueno, quién la cuida,
   de dónde sale y qué función la calcula para un día y un almacén. Las
   funciones **reutilizan** las políticas que ya existen (`metricas/*`,
   `rezago_liquidacion`, `fotos_siesa_service`, `EstadoEntrega`): acá no se
   reescribe qué es un despacho, un ajuste o un rechazo.
2. **«No sabemos» ≠ 0.** Un día sin dato se guarda `AUSENTE` con motivo —
   nunca un cero—. Si hay número pero la fuente estaba incompleta, es
   `INCOMPLETO` (cota, no total), también con motivo. Antes del primer
   registro de una fuente no hay «cero despachos»: hay «el WMS no registraba».
3. **Denominador visible.** Toda fila trae `n`; toda tasa, `numerador` y
   `denominador`, para agregarse bien (Σnum/Σden, no promedio de tasas).
4. **El día es el de Bogotá** (`app/utils/fecha.py`), también entre las 7 p. m.
   y la medianoche, cuando UTC ya es mañana.
5. **Cero Siesa.** Todo sale de la base del WMS y de las fotos ya guardadas.
6. **Lo que se sabía no se olvida.** Recalcular un día nunca pisa un valor
   guardado (OK o INCOMPLETO) con un AUSENTE: el acta de corte vacía las
   tablas operativas, y recalcular después «descubriría» que no hay datos.
   Esta tabla existe justamente para sobrevivir a eso.

## El cron

`[ANALITICA_KPI]`, en `_scheduler_pesados`, 04:30 Bogotá. **Nace apagado**
(`ANALITICA_KPI=true` para encender); el interruptor vive en `correr_kpi`, la
función que escribe. Calcula ayer y recalcula los `ANALITICA_KPI_DIAS` días
anteriores (defecto 7) para absorber lo que llega tarde: una foto de ventas
re-tomada, un job que terminó de fallar, un recaudo confirmado al otro día.
Lock `LOCK_ANALITICA_KPI` (2021), el mismo que toma el recálculo manual.

## El CUSUM

La alerta de cambio de `resumen` es el CUSUM de Vigía — **la misma función**
(`vigia_service.cusum_bilateral`), no una copia— sobre semanas cerradas y
completas de la métrica. Vigía está certificado sobre series semanales, así
que el KPI se agrega por semana antes de pasárselo (una serie diaria tiene el
domingo en cero y el CUSUM lo leería como desplome). Con menos de
`MIN_SEMANAS_CUSUM` semanas completas no hay alerta, y se dice.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import func

from app.extensions import db
from app.models.analitica_kpi import AnaliticaKpiDiario, EstadoKpi
from app.utils.fecha import dia_operativo, dia_operativo_de, rango_dia_operativo_utc

logger = logging.getLogger(__name__)

SUBE_ES_BUENO = 'SUBE_ES_BUENO'
BAJA_ES_BUENO = 'BAJA_ES_BUENO'
_FLECHA = {SUBE_ES_BUENO: '↑', BAJA_ES_BUENO: '↓'}

#: Cómo se agrega una métrica a una semana o a un período.
SUMA = 'SUMA'      # flujo: se suman los días
TASA = 'TASA'      # proporción: Σ numerador / Σ denominador
NIVEL = 'NIVEL'    # saldo al cierre: el último día con dato
#: Se mide EN VIVO sobre la cohorte del período (pedidos que entraron, casos
#: de la ventana con su estado de hoy), con la función de su pantalla de
#: detalle (`analitica_portada`). No se guarda por día: una mediana no se
#: agrega sumando días, y un «hoy» recalculado para un día viejo mentiría.
COHORTE = 'COHORTE'
AGREGACIONES = (SUMA, TASA, NIVEL, COHORTE)

#: Máximo de días por recálculo manual. Cada día son ~15 métricas × almacenes;
#: un rango más largo se pide en tandas (el recálculo es idempotente).
TOPE_DIAS_RECALCULO = 31
#: Máximo de días de una consulta de serie o resumen.
TOPE_DIAS_CONSULTA = 366
#: Semanas hacia atrás que se leen para el CUSUM: las 26 de referencia de
#: Vigía más otras tantas para vigilar.
SEMANAS_HISTORIA_CUSUM = 52

#: Tipos de la cola que no son documentos para Siesa.
TIPOS_JOB_SIN_SIESA = ('ALERTA_EMAIL',)

NO_POR_ALMACEN = 'esta métrica no se abre por almacén: se mide para toda la empresa'
EN_VIVO = ('se mide en vivo sobre la cohorte del período (portada 🎯 ¿Cómo vamos?), '
           'no se guarda por día')
DIA_EN_CURSO = 'día en curso: parcial, calculado en vivo y no guardado'
SIN_CALCULAR = ('sin calcular: el KPI diario no se calculó para ese día (el cron '
                'ANALITICA_KPI está apagado o no corrió; se puede recalcular a mano)')


# ──────────────────────────────────────────────────────────────────────────────
# Interruptores
# ──────────────────────────────────────────────────────────────────────────────

def encendido() -> bool:
    """`ANALITICA_KPI=true`. **Nace apagado**: un cron que escribe no se enciende solo."""
    return os.getenv('ANALITICA_KPI', 'false').strip().lower() == 'true'


def dias_recalculo() -> int:
    """`ANALITICA_KPI_DIAS` (defecto 7, tope 60): cuántos días antes de ayer
    recalcula el cron para absorber eventos tardíos."""
    try:
        return min(60, max(0, int(os.getenv('ANALITICA_KPI_DIAS', '7'))))
    except ValueError:
        return 7


# ──────────────────────────────────────────────────────────────────────────────
# Resultado de una métrica en un día
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Resultado:
    estado: str
    valor: Optional[float] = None
    n: Optional[int] = None
    numerador: Optional[float] = None
    denominador: Optional[float] = None
    motivo: Optional[str] = None
    detalle: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'estado': self.estado, 'valor': self.valor, 'n': self.n,
                'numerador': self.numerador, 'denominador': self.denominador,
                'motivo': self.motivo, 'detalle': self.detalle}


def _f(v):
    return float(v) if v is not None else None


def _ok(valor, n=None, **kw) -> Resultado:
    return Resultado(EstadoKpi.OK, _f(valor), n, **kw)


def _incompleto(valor, motivo, n=None, **kw) -> Resultado:
    return Resultado(EstadoKpi.INCOMPLETO, _f(valor), n, motivo=motivo, **kw)


def _ausente(motivo, **kw) -> Resultado:
    return Resultado(EstadoKpi.AUSENTE, None, motivo=motivo, **kw)


def _valor(valor, n, motivo_incompleto=None, **kw) -> Resultado:
    """OK, o INCOMPLETO si hay un motivo para desconfiar del total."""
    if motivo_incompleto:
        return _incompleto(valor, motivo_incompleto, n, **kw)
    return _ok(valor, n, **kw)


def _tasa(numerador, denominador, n, *, sin_denominador, min_n=None,
          motivo_incompleto=None, detalle=None) -> Resultado:
    """Una proporción 0–1. Sin denominador, o con muestra chica, **no hay
    tasa** (AUSENTE) — pero numerador y denominador se guardan igual, para que
    la semana o el período, que sí pueden tener n suficiente, los sumen."""
    kw = {'numerador': _f(numerador), 'denominador': _f(denominador),
          'detalle': detalle or {}}
    if not denominador:
        return _ausente(sin_denominador, n=n, **kw)
    if min_n is not None and denominador < min_n:
        return _ausente(f'n={int(denominador)} < {min_n}: muestra chica, no se publica '
                        f'la tasa del día (sí suma a la del período)', n=n, **kw)
    return _valor(Decimal(str(numerador)) / Decimal(str(denominador)), n,
                  motivo_incompleto, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Contexto de un cálculo: memoria de consultas repetidas
# ──────────────────────────────────────────────────────────────────────────────

class _Contexto:
    """Memoriza lo que varias métricas del mismo día comparten (despachos,
    cadenas de conteo, paradas) y el primer registro de cada fuente."""

    def __init__(self):
        self._memo = {}

    def memo(self, clave, fn):
        if clave not in self._memo:
            self._memo[clave] = fn()
        return self._memo[clave]

    def almacenes(self, alm):
        """El almacén pedido, o TODOS (activos o no) para el total: un total
        que dejara fuera un almacén desactivado perdería su historia."""
        if alm is not None:
            return [alm]
        from app.models.almacen import Almacen
        return self.memo('almacenes', lambda: Almacen.query.order_by(Almacen.id).all())

    def inicio(self, nombre, columna, *filtros):
        """Primer día operativo con datos en una fuente, o `None`."""
        def _min():
            v = db.session.query(func.min(columna)).filter(*filtros).scalar()
            if v is None:
                return None
            return dia_operativo_de(v) if isinstance(v, datetime) else v
        return self.memo(('inicio', nombre), _min)


def _antes_de(ctx, dia, nombre, columna, *filtros, que):
    """Motivo de AUSENTE si `dia` es anterior al primer registro de la fuente."""
    ini = ctx.inicio(nombre, columna, *filtros)
    if ini is None:
        return f'sin {que} registrados en el WMS: la fuente todavía no tiene datos'
    if dia < ini:
        return f'antes del primer registro de {que} en el WMS ({ini.isoformat()})'
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Las métricas
# ──────────────────────────────────────────────────────────────────────────────

def _despachos(ctx, dia, alm) -> dict:
    from app.services.metricas.pedidos_despachados import calcular_pedidos_despachados
    tot = {'pedidos': 0, 'valor_total': 0.0, 'sin_valor_factura': 0}
    for a in ctx.almacenes(alm):
        r = ctx.memo(('despachos', dia, a.id),
                     lambda a=a: calcular_pedidos_despachados(a.id, dia, dia))
        for k in tot:
            tot[k] += r[k]
    return tot


def _inicio_despachos(ctx, dia):
    from app.models.packing import TareaPacking
    return _antes_de(ctx, dia, 'despachos', TareaPacking.fecha_despachado,
                     TareaPacking.estado == 'DESPACHADO', que='despachos')


def m_pedidos_despachados(dia, alm, ctx) -> Resultado:
    motivo = _inicio_despachos(ctx, dia)
    if motivo:
        return _ausente(motivo)
    r = _despachos(ctx, dia, alm)
    return _ok(r['pedidos'], r['pedidos'])


def m_valor_despachado(dia, alm, ctx) -> Resultado:
    motivo = _inicio_despachos(ctx, dia)
    if motivo:
        return _ausente(motivo)
    r = _despachos(ctx, dia, alm)
    sin = r['sin_valor_factura']
    return _valor(r['valor_total'], r['pedidos'],
                  (f'{sin} despacho(s) sin valor de factura: el total es cota inferior'
                   if sin else None),
                  detalle={'sin_valor_factura': sin})


def m_fill_rate(dia, alm, ctx) -> Resultado:
    from app.services.metricas.fill_rate import calcular_fill_rate_historia
    if alm is not None and not alm.bodega_siesa_id:
        return _ausente('el almacén no tiene bodega Siesa: la historia de pedidos es por bodega')
    r = calcular_fill_rate_historia(dia, alm.bodega_siesa_id if alm is not None else None)
    detalle = {'excluidas': r['excluidas'], 'inicio_historia': r['inicio_historia']}
    if r['inicio_historia'] is None or dia.isoformat() < r['inicio_historia']:
        return _ausente(r['motivo'], detalle=detalle)
    return _tasa(r['unidades_servidas'], r['unidades_pedidas'], r['lineas'],
                 sin_denominador=r['motivo'] or 'ninguna línea con entrega ese día',
                 motivo_incompleto=None if r['completo'] else r['motivo'],
                 detalle=detalle)


def m_venta_perdida(dia, alm, ctx) -> Resultado:
    from app.models.evento_stock_agotado import EventoStockAgotado
    from app.services.metricas.venta_perdida import calcular_venta_perdida
    motivo = _antes_de(ctx, dia, 'agotados', EventoStockAgotado.creado_en,
                       que='eventos de agotado')
    if motivo:
        return _ausente(motivo)
    total, eventos, sin_eventos, sin_unidades = 0.0, 0, 0, 0
    for a in ctx.almacenes(alm):
        r = calcular_venta_perdida(a.id, dia, dia)
        total += r['venta_perdida_total']
        eventos += r['eventos']
        sin_eventos += r['sin_precio']['eventos']
        sin_unidades += r['sin_precio']['unidades']
    return _valor(total, eventos,
                  (f'{sin_eventos} evento(s) sin precio ({sin_unidades} und): el total es '
                   f'cota inferior' if sin_eventos else None),
                  detalle={'sin_precio': {'eventos': sin_eventos, 'unidades': sin_unidades}})


def _cadenas(ctx, dia, alm):
    from app.services.metricas.conteo import cadenas_del_dia
    alm_id = alm.id if alm is not None else None
    return ctx.memo(('cadenas', dia, alm_id), lambda: cadenas_del_dia(dia, alm_id))


def _inicio_conteo(ctx, dia):
    from app.models.conteo import SesionConteo
    return _antes_de(ctx, dia, 'conteo', SesionConteo.fecha_creacion, que='conteos')


def m_conteos_cerrados(dia, alm, ctx) -> Resultado:
    motivo = _inicio_conteo(ctx, dia)
    if motivo:
        return _ausente(motivo)
    cerradas = [c for c in _cadenas(ctx, dia, alm) if c.cerrada]
    return _ok(len(cerradas), len(cerradas))


def m_exactitud_inventario(dia, alm, ctx) -> Resultado:
    from app.services.metricas.conteo import MIN_N_EXACTITUD, exactitud_total
    motivo = _inicio_conteo(ctx, dia)
    if motivo:
        return _ausente(motivo)
    ex = exactitud_total([c for c in _cadenas(ctx, dia, alm) if c.cerrada])
    return _tasa(ex['numerador'], ex['denominador'], ex['denominador'],
                 sin_denominador='ningún conteo cerrado contra foto de Siesa ese día',
                 min_n=MIN_N_EXACTITUD, detalle={'excluidos': ex['excluidos']})


def m_ajustes_valor(dia, alm, ctx) -> Resultado:
    from app.services.metricas.conteo import ajustes_del_dia
    motivo = _inicio_conteo(ctx, dia)
    if motivo:
        return _ausente(motivo)
    alm_id = alm.id if alm is not None else None
    b = ajustes_del_dia(dia, alm_id, cadenas=_cadenas(ctx, dia, alm))
    v = b['valor']
    sin = v['ajustes_sin_costo']
    return _valor(v['ent'] + v['sal'], b['cantidad'],
                  (f'{sin} ajuste(s) sin costo en la foto del conteo: el valor es cota inferior'
                   if sin else None),
                  detalle={'entradas': v['ent'], 'salidas': v['sal'], 'neto': v['neto'],
                           'unidades_ent': b['unidades_ent'], 'unidades_sal': b['unidades_sal'],
                           'ajustes_sin_costo': sin, 'excluidos': b['excluidos']})


def m_rutas_sin_liquidar(dia, alm, ctx) -> Resultado:
    from app.models.ruta_despacho import RutaDespacho
    from app.services.rezago_liquidacion import rutas_sin_liquidar_al_cierre
    motivo = _antes_de(ctx, dia, 'rutas', RutaDespacho.fecha_creacion, que='rutas')
    if motivo:
        return _ausente(motivo)
    r = rutas_sin_liquidar_al_cierre(dia)
    detalle = {'atrasadas': r['atrasadas'], 'cruzan_mes': r['cruzan_mes'],
               'sin_liquidar_total': len(r['rutas']), 'desconocidas': r['desconocidas']}
    return _valor(r['con_rezago'], len(r['rutas']),
                  (f"{r['desconocidas']} ruta(s) liquidada(s) sin fecha de liquidación: no se "
                   f"sabe si al cierre de ese día ya lo estaban (cota inferior)"
                   if r['desconocidas'] else None),
                  detalle=detalle)


def _paradas(ctx, dia, alm):
    """`(estado_entrega, valor_factura)` de las paradas confirmadas ese día."""
    def _q():
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        ini, fin = rango_dia_operativo_utc(dia, dia)
        q = (db.session.query(RecaudoEntrega.estado_entrega, TareaPacking.valor_factura)
             .join(TareaPacking, RecaudoEntrega.tarea_id == TareaPacking.id)
             .filter(RecaudoEntrega.fecha_confirmacion >= ini,
                     RecaudoEntrega.fecha_confirmacion < fin))
        if alm is not None:
            q = q.filter(TareaPacking.almacen_id == alm.id)
        return q.all()
    return ctx.memo(('paradas', dia, alm.id if alm is not None else None), _q)


def _inicio_paradas(ctx, dia):
    from app.models.recaudo_entrega import RecaudoEntrega
    return _antes_de(ctx, dia, 'paradas', RecaudoEntrega.fecha_confirmacion,
                     que='paradas confirmadas')


def m_entregado_sin_pago(dia, alm, ctx) -> Resultado:
    from app.models.recaudo_entrega import EstadoEntrega
    motivo = _inicio_paradas(ctx, dia)
    if motivo:
        return _ausente(motivo)
    filas = [v for est, v in _paradas(ctx, dia, alm) if est == EstadoEntrega.ENTREGADO_SIN_PAGO]
    sin = sum(1 for v in filas if v is None)
    total = sum((v for v in filas if v is not None), Decimal(0))
    return _valor(total, len(filas),
                  (f'{sin} parada(s) sin valor de factura: el total es cota inferior'
                   if sin else None),
                  detalle={'sin_valor_factura': sin})


def m_rechazos_ruta(dia, alm, ctx) -> Resultado:
    from app.models.recaudo_entrega import EstadoEntrega
    motivo = _inicio_paradas(ctx, dia)
    if motivo:
        return _ausente(motivo)
    filas = _paradas(ctx, dia, alm)
    rechazadas = sum(1 for est, _ in filas if est == EstadoEntrega.RECHAZADO)
    return _tasa(rechazadas, len(filas), len(filas),
                 sin_denominador='ninguna parada confirmada ese día')


def m_jobs_siesa_fallidos(dia, alm, ctx) -> Resultado:
    """Cohorte de jobs encolados ese día que **siguen** trabados. Lo trabado lo
    decide `siesa_job_service.fallidos_vigentes` (la única que cuenta
    FALLIDO): un FALLIDO superado por un COMPLETADO posterior del mismo tipo y
    referencia, o cerrado por la reconciliación, no cuenta."""
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    from app.services.siesa_job_service import fallidos_vigentes
    motivo = _antes_de(ctx, dia, 'jobs', SiesaJob.fecha_creacion, que='documentos a Siesa')
    if motivo:
        return _ausente(motivo)
    ini, fin = rango_dia_operativo_utc(dia, dia)
    filas = (db.session.query(SiesaJob.tipo, SiesaJob.estado)
             .filter(SiesaJob.fecha_creacion >= ini, SiesaJob.fecha_creacion < fin,
                     SiesaJob.tipo.notin_(TIPOS_JOB_SIN_SIESA)).all())
    trabados = fallidos_vigentes(desde=ini, hasta=fin, excluir_tipos=TIPOS_JOB_SIN_SIESA)
    fallidos = [j.tipo for j in trabados['jobs']]
    activos = sum(1 for _, e in filas if e in EstadoSiesaJob.ACTIVOS)
    por_tipo = {}
    for t in fallidos:
        por_tipo[t] = por_tipo.get(t, 0) + 1
    return _valor(len(fallidos), len(filas),
                  (f'{activos} documento(s) de ese día siguen en la cola: el desenlace no es final'
                   if activos else None),
                  numerador=len(fallidos), denominador=len(filas),
                  detalle={'fallidos_por_tipo': por_tipo, 'en_cola': activos,
                           'superados': trabados['superados'],
                           'descartados': sum(1 for _, e in filas
                                              if e == EstadoSiesaJob.DESCARTADO)})


#: Acciones de la bitácora que dan de baja algo: se canceló, se borró o se anuló.
ACCIONES_DE_BAJA = ('CANCELAR', 'ELIMINAR', 'ANULAR')

#: Entidades del FLUJO DE NEGOCIO: su baja es trabajo o mercancía que se cayó
#: entre el pedido y la caja. Lista blanca, no negra: una entidad nueva que
#: empiece a escribir en la bitácora queda en `detalle.tecnicas` hasta que
#: alguien decida que es del flujo — contarla por descuido es lo que convertía
#: la limpieza del layout (ELIMINAR Ubicacion × 3) en «4 cancelaciones».
ENTIDADES_DE_NEGOCIO = frozenset({
    'TareaPicking', 'TareaPacking', 'Bulto', 'RecepcionMercancia',
    'SolicitudTraslado', 'TareaReposicion', 'DevolucionCliente',
    'TareaDevolucion', 'RutaDespacho', 'RecaudoEntrega', 'SesionConteo',
})


def m_cancelaciones(dia, alm, ctx) -> Resultado:
    """Bajas (CANCELAR/ELIMINAR/ANULAR) de entidades del flujo de negocio
    (`ENTIDADES_DE_NEGOCIO`). Las técnicas —ubicaciones del layout, mapeo de
    unidades, remodular, maestros, juicios— van a `detalle.tecnicas`: se
    cuentan, no suben el número."""
    from app.models.bitacora import BitacoraAccion
    motivo = _antes_de(ctx, dia, 'bitacora', BitacoraAccion.dia_operativo,
                       que='acciones en la bitácora')
    if motivo:
        return _ausente(motivo)
    q = (db.session.query(BitacoraAccion.accion, BitacoraAccion.entidad,
                          BitacoraAccion.almacen_id)
         .filter(BitacoraAccion.dia_operativo == dia,
                 BitacoraAccion.accion.in_(ACCIONES_DE_BAJA)))
    if alm is not None:
        q = q.filter(BitacoraAccion.almacen_id == alm.id)
    filas = q.all()
    negocio = [f for f in filas if f[1] in ENTIDADES_DE_NEGOCIO]
    por_accion, por_entidad, tecnicas = {}, {}, {}
    for accion, entidad, _ in negocio:
        por_accion[accion] = por_accion.get(accion, 0) + 1
        por_entidad[entidad] = por_entidad.get(entidad, 0) + 1
    for _, entidad, _ in filas:
        if entidad not in ENTIDADES_DE_NEGOCIO:
            tecnicas[entidad] = tecnicas.get(entidad, 0) + 1
    detalle = {'por_accion': por_accion, 'por_entidad': por_entidad,
               'tecnicas': tecnicas}
    if alm is None:
        detalle['sin_almacen'] = sum(1 for *_, a in negocio if a is None)
    return _ok(len(negocio), len(negocio), detalle=detalle)


def m_ventas_facturadas(dia, alm, ctx) -> Resultado:
    from app.services.bodegas import co_de_bodega
    from app.services.fotos_siesa_service import cos_operados, ventas_del_dia
    if alm is not None:
        co = co_de_bodega(alm.bodega_siesa_id)
        if not co:
            return _ausente('el almacén no tiene CO de Siesa: no hay foto de ventas que leer')
        pares = [(co, alm.bodega_siesa_id)]
    else:
        pares = [(co, None) for co in cos_operados()]
    neto, lineas, sin_valor, faltan, corridas = Decimal(0), 0, 0, [], {}
    for co, bodega in pares:
        r = ventas_del_dia(co, dia, bodega)
        if r is None:
            faltan.append(co)
            continue
        neto += r['neto']
        lineas += r['lineas']
        sin_valor += r['sin_valor']
        corridas[co] = r['terminada_at']
    detalle = {'faltan_cos': faltan, 'corridas': corridas, 'sin_valor': sin_valor}
    if len(faltan) == len(pares):
        return _ausente('sin foto completa de ventas de ese día'
                        + (f' (CO {", ".join(faltan)})' if faltan else ''), detalle=detalle)
    motivos = []
    if faltan:
        motivos.append(f'falta la foto completa de CO {", ".join(faltan)}')
    if sin_valor:
        motivos.append(f'{sin_valor} línea(s) sin valor neto')
    return _valor(neto, lineas,
                  ('; '.join(motivos) + ': el total es cota inferior') if motivos else None,
                  detalle=detalle)


def _cartera(ctx, dia):
    from app.services.fotos_siesa_service import cartera_del_dia
    return ctx.memo(('cartera', dia), lambda: cartera_del_dia(dia))


def m_cartera_abierta(dia, alm, ctx) -> Resultado:
    r = _cartera(ctx, dia)
    if r is None:
        return _ausente('sin foto completa de cartera de ese día')
    return _valor(r['abierta'], r['documentos'],
                  (f"{r['sin_saldo']} documento(s) sin saldo legible: cota inferior"
                   if r['sin_saldo'] else None),
                  detalle={'corrida': r['terminada_at'], 'sin_saldo': r['sin_saldo']})


def m_cartera_vencida(dia, alm, ctx) -> Resultado:
    r = _cartera(ctx, dia)
    if r is None:
        return _ausente('sin foto completa de cartera de ese día')
    faltan = r['sin_vencimiento'] + r['sin_saldo']
    return _valor(r['vencida'], r['vencidos'],
                  (f'{faltan} documento(s) sin vencimiento o sin saldo: la vencida es cota inferior'
                   if faltan else None),
                  detalle={'corrida': r['terminada_at'], 'sin_vencimiento': r['sin_vencimiento'],
                           'sin_saldo': r['sin_saldo'], 'abierta': _f(r['abierta'])})


# ──────────────────────────────────────────────────────────────────────────────
# El catálogo
# ──────────────────────────────────────────────────────────────────────────────

def m_en_vivo(dia, alm, ctx) -> Resultado:
    """Las métricas de COHORTE no tienen valor por día: se miden por período."""
    return _ausente(EN_VIVO)


@dataclass(frozen=True)
class Metrica:
    clave: str
    nombre: str
    mide: str
    unidad: str
    direccion: str
    dueno: str
    fuente: str
    agregacion: str
    por_almacen: bool
    calcular: Callable
    min_n: Optional[int] = None
    #: La meta del negocio y el borde del amarillo, en la unidad de la métrica
    #: (proporción 0–1, días, pesos). `None` = la métrica no tiene meta y no
    #: lleva semáforo. **Provisionales** hasta que la gerencia las confirme:
    #: sin meta ningún número dice si vamos bien, y una meta inventada que se
    #: presenta como decidida es peor que ninguna — por eso se declaran.
    meta: Optional[float] = None
    umbral_amarillo: Optional[float] = None
    #: La meta es por día del período (flujos en pesos: 30 días toleran 30
    #: veces lo de un día). Si no, es la misma para cualquier período.
    meta_por_dia: bool = False
    meta_provisional: bool = True

    def to_dict(self) -> dict:
        return {'clave': self.clave, 'nombre': self.nombre, 'mide': self.mide,
                'unidad': self.unidad, 'direccion': self.direccion,
                'direccion_buena': _FLECHA[self.direccion], 'dueno': self.dueno,
                'fuente': self.fuente, 'agregacion': self.agregacion,
                'por_almacen': self.por_almacen, 'min_n': self.min_n,
                'meta': self.meta, 'umbral_amarillo': self.umbral_amarillo,
                'meta_por_dia': self.meta_por_dia,
                'meta_provisional': self.meta_provisional if self.meta is not None else None}


#: Los niveles del semáforo. `sin_dato` es GRIS, nunca rojo: no saber no es una
#: mala noticia del negocio, es un hueco del dato (y se dice por qué).
NIVELES_SEMAFORO = ('verde', 'amarillo', 'rojo', 'sin_dato', 'sin_meta')
#: Los que cuenta la portada (una métrica de la portada siempre tiene meta).
NIVELES_CONTADOS = NIVELES_SEMAFORO[:4]
TEXTO_SEMAFORO = {'verde': 'En meta', 'amarillo': 'Cerca de la meta', 'rojo': 'Fuera de meta',
                  'sin_dato': 'Sin dato', 'sin_meta': 'Sin meta'}


def semaforo(clave: str, valor, dias: int = 1, es_piso: bool = False,
             sin_medir: bool = False) -> dict:
    """El semáforo de una métrica **contra su meta**. Una política, una función:
    la portada, el recorrido y cualquier pantalla futura la leen de acá.

    · `sin_meta` si la métrica no tiene meta; `sin_dato` si no hay valor.
    · Si la dirección buena es ↑: verde ≥ meta, amarillo ≥ umbral, rojo abajo.
      Si es ↓: verde ≤ meta, amarillo ≤ umbral, rojo arriba.
    · Metas por día (`meta_por_dia`) se multiplican por los días del período.
    · **Un piso nunca es verde** (Regla 0): con casos sin valor, «$0 de venta
      perdida» puede ser cualquier cosa; lo que sería verde queda amarillo y
      lo dice (`por_piso`).
    """
    m = METRICAS[clave]
    escala = max(1, int(dias or 1)) if m.meta_por_dia else 1
    meta = None if m.meta is None else m.meta * escala
    amarillo = None if m.umbral_amarillo is None else m.umbral_amarillo * escala
    base = {'meta': meta, 'umbral_amarillo': amarillo, 'provisional': m.meta_provisional,
            'por_piso': False, 'direccion': m.direccion}
    if meta is None:
        return {**base, 'nivel': 'sin_meta', 'texto': TEXTO_SEMAFORO['sin_meta']}
    if valor is None:
        return {**base, 'nivel': 'sin_dato', 'texto': TEXTO_SEMAFORO['sin_dato']}
    if sin_medir:
        # Un período que no se terminó de medir (todos sus días en curso o
        # incompletos) no se juzga contra la meta: gris y dicho (e2e
        # 2026-09-25: «Fuera de meta 0 %» un día que ni se había medido).
        return {**base, 'nivel': 'sin_dato', 'sin_medir': True,
                'texto': 'Sin juicio: el período todavía no se termina de medir'}
    v = float(valor)
    if m.direccion == SUBE_ES_BUENO:
        nivel = 'verde' if v >= meta else ('amarillo' if amarillo is not None and v >= amarillo
                                           else 'rojo')
    else:
        nivel = 'verde' if v <= meta else ('amarillo' if amarillo is not None and v <= amarillo
                                           else 'rojo')
    por_piso = False
    if es_piso and nivel == 'verde':
        nivel, por_piso = 'amarillo', True
    texto = ('Sin confirmar: hay casos sin valor' if por_piso else TEXTO_SEMAFORO[nivel])
    return {**base, 'nivel': nivel, 'texto': texto, 'por_piso': por_piso}


def _min_n_exactitud():
    from app.services.metricas.conteo import MIN_N_EXACTITUD
    return MIN_N_EXACTITUD


_CATALOGO = (
    Metrica('pedidos_despachados', 'Pedidos despachados',
            'Despachos cerrados ese día: tareas de empaque que pasaron a DESPACHADO (un pedido '
            'despachado en dos tandas cuenta dos).',
            'despachos', SUBE_ES_BUENO, 'Jefe de bodega',
            'tareas_packing · metricas.pedidos_despachados', SUMA, True, m_pedidos_despachados),
    Metrica('valor_despachado', 'Valor despachado',
            'Suma del valor neto de factura de los despachos del día. Un despacho sin valor '
            'anotado no suma y vuelve el total cota inferior.',
            'pesos', SUBE_ES_BUENO, 'Gerencia comercial',
            'tareas_packing.valor_factura · metricas.pedidos_despachados', SUMA, True,
            m_valor_despachado),
    Metrica('fill_rate', 'Nivel de servicio',
            'De las unidades pedidas con entrega ese día, qué proporción se remisionó '
            '(Σ min(remisionado, pedido) / Σ pedido). Sobre la historia del pedido, no sobre lo '
            'pendiente: lo cumplido también cuenta.',
            'proporcion', SUBE_ES_BUENO, 'Jefe de bodega',
            'pedidos_historia · metricas.fill_rate', TASA, True, m_fill_rate,
            meta=0.92, umbral_amarillo=0.85),
    Metrica('venta_perdida', 'Venta perdida por agotados',
            'Unidades faltantes × precio de los pickings de venta bloqueados por falta de '
            'inventario ese día. Un agotado sin precio no suma y vuelve el total cota inferior.',
            'pesos', BAJA_ES_BUENO, 'Compras',
            'eventos_stock_agotado · metricas.venta_perdida', SUMA, True, m_venta_perdida,
            meta=0.0, umbral_amarillo=100000.0, meta_por_dia=True),
    Metrica('conteos_cerrados', 'Conteos cerrados',
            'Conteos cíclicos (cadenas CC1→CC2→CC3) que llegaron a veredicto ese día.',
            'conteos', SUBE_ES_BUENO, 'Líder de inventario',
            'sesiones_conteo · metricas.conteo', SUMA, True, m_conteos_cerrados),
    Metrica('exactitud_inventario', 'Exactitud de inventario',
            'De los conteos cerrados contra la foto de Siesa, qué proporción cuadró exacto '
            '(IRA). Bajo 30 conteos no se publica la tasa.',
            'proporcion', SUBE_ES_BUENO, 'Líder de inventario',
            'sesiones_conteo · metricas.conteo', TASA, True, m_exactitud_inventario,
            min_n=_min_n_exactitud(), meta=0.95, umbral_amarillo=0.90),
    Metrica('ajustes_valor', 'Ajustes de inventario',
            'Valor absoluto de los ajustes de conteo confirmados ese día (sobrantes + '
            'faltantes), a costo de la foto del conteo.',
            'pesos', BAJA_ES_BUENO, 'Líder de inventario',
            'sesiones_conteo · metricas.conteo', SUMA, True, m_ajustes_valor),
    Metrica('rutas_sin_liquidar', 'Rutas entregadas sin liquidar a tiempo',
            'Al cierre del día: rutas entregadas cuya liquidación ya está atrasada o cruza de '
            'mes. Cada una deja la factura como cartera vencida hasta que se liquide.',
            'rutas', BAJA_ES_BUENO, 'Tesorería',
            'rutas_despacho · rezago_liquidacion', NIVEL, False, m_rutas_sin_liquidar),
    Metrica('entregado_sin_pago', 'Entregado sin pago',
            'Valor de factura de las paradas que el conductor dejó con el cliente sin cobrar '
            'ese día. La factura queda abierta en cartera.',
            'pesos', BAJA_ES_BUENO, 'Tesorería',
            'recaudos_entrega (ENTREGADO_SIN_PAGO)', SUMA, True, m_entregado_sin_pago),
    Metrica('rechazos_ruta', 'Rechazos en ruta',
            'De las paradas confirmadas ese día, qué proporción el cliente rechazó y volvió '
            'al camión.',
            'proporcion', BAJA_ES_BUENO, 'Coordinador de rutas',
            'recaudos_entrega (RECHAZADO)', TASA, True, m_rechazos_ruta),
    Metrica('jobs_siesa_fallidos', 'Documentos a Siesa que fallaron',
            'De los documentos encolados para Siesa ese día (sin contar correos), cuántos '
            'terminaron FALLIDO. Mientras quede alguno en cola, el número no es final.',
            'documentos', BAJA_ES_BUENO, 'Sistemas',
            'siesa_jobs', SUMA, False, m_jobs_siesa_fallidos),
    Metrica('cancelaciones', 'Cancelaciones y eliminaciones',
            'Acciones de la bitácora que cancelaron, borraron o anularon algo ese día.',
            'acciones', BAJA_ES_BUENO, 'Gerencia general',
            'bitacora_acciones', SUMA, True, m_cancelaciones),
    Metrica('ventas_facturadas', 'Ventas facturadas desde pedido',
            'Valor neto de las facturas desde pedido de ese día, según la foto diaria de Siesa '
            '(sin anuladas). La venta de caja en tienda no está.',
            'pesos', SUBE_ES_BUENO, 'Gerencia comercial',
            'foto_ventas_lineas · fotos_siesa_service', SUMA, True, m_ventas_facturadas),
    Metrica('cartera_abierta', 'Cartera abierta',
            'Saldo de clientes (cuentas 1305) sin cancelar según la foto de cartera del día.',
            'pesos', BAJA_ES_BUENO, 'Tesorería',
            'foto_cartera_diaria · fotos_siesa_service', NIVEL, False, m_cartera_abierta),
    Metrica('cartera_vencida', 'Cartera vencida',
            'De la cartera abierta del día, el saldo de los documentos con vencimiento pasado.',
            'pesos', BAJA_ES_BUENO, 'Tesorería',
            'foto_cartera_diaria · fotos_siesa_service', NIVEL, False, m_cartera_vencida),
    # ── Las de la portada que se miden en vivo por cohorte (2026-09-24) ──────
    Metrica('llega_a_caja', 'Llega a caja completo',
            'De cada $100 de pedidos que ya terminaron (completos o caídos), cuántos llegaron '
            'a caja liquidada sin ninguna pérdida: todo recogido, entregado completo, sin nota '
            'crédito ni devolución. Lo que va en camino no la baja.',
            'proporcion', SUBE_ES_BUENO, 'Gerencia de operaciones',
            'analitica_recorrido (valor sin fuga, cerrados)', COHORTE, True, m_en_vivo,
            meta=0.95, umbral_amarillo=0.90),
    Metrica('ciclo_caja', 'Ciclo de caja',
            'Días (mediana) entre que Siesa aprueba el pedido y su ruta se liquida, en los '
            'pedidos del período que llegaron a caja sin caerse.',
            'dias', BAJA_ES_BUENO, 'Tesorería',
            'analitica_recorrido (aprobado → liquidado)', COHORTE, True, m_en_vivo,
            meta=2.0, umbral_amarillo=3.0),
    Metrica('plata_en_riesgo', 'Plata en riesgo',
            'Plata que salió del cliente o de la bodega y todavía no llega a caja ni a Siesa: '
            'cobros en rutas sin liquidar, entregas sin pago, crédito que nadie autorizó y '
            'recibos, retenciones o facturas trabados en Siesa.',
            'pesos', BAJA_ES_BUENO, 'Tesorería',
            'analitica_fugas (calle, sin pago, crédito no autorizado, documentos de plata)',
            COHORTE, True, m_en_vivo, meta=0.0, umbral_amarillo=1000000.0),
)

METRICAS = {m.clave: m for m in _CATALOGO}


def catalogo() -> list:
    return [m.to_dict() for m in _CATALOGO]


# ──────────────────────────────────────────────────────────────────────────────
# Calcular y guardar
# ──────────────────────────────────────────────────────────────────────────────

def _almacen(almacen_id):
    if almacen_id is None:
        return None
    from app.models.almacen import Almacen
    alm = db.session.get(Almacen, int(almacen_id))
    if alm is None:
        raise ValueError(f'almacén {almacen_id} no existe')
    return alm


def calcular(clave: str, dia: date, almacen_id: int = None, ctx: _Contexto = None) -> Resultado:
    """El valor de una métrica un día. **Nunca levanta por un dato**: un error
    al calcular se devuelve AUSENTE con el motivo, no un cero."""
    m = METRICAS[clave]
    if almacen_id is not None and not m.por_almacen:
        return _ausente(NO_POR_ALMACEN)
    alm = _almacen(almacen_id)
    ctx = ctx or _Contexto()
    try:
        with db.session.begin_nested():
            return m.calcular(dia, alm, ctx)
    except Exception as e:
        logger.exception('[ANALITICA_KPI] %s %s alm=%s falló', clave, dia, almacen_id)
        return _ausente(f'error al calcular: {type(e).__name__}: {e}'[:300])


def guardar(dia: date, almacen_id, clave: str, r: Resultado, ahora: datetime = None) -> str:
    """Upsert por (día, almacén, métrica). Devuelve `nueva` | `actualizada` |
    `conservada`. **Un AUSENTE no pisa un valor**: ver la regla 6 del módulo."""
    fila = AnaliticaKpiDiario.query.filter_by(
        dia_operativo=dia, almacen_id=almacen_id, metrica=clave).first()
    if fila is not None and fila.estado != EstadoKpi.AUSENTE and r.estado == EstadoKpi.AUSENTE:
        return 'conservada'
    resultado = 'actualizada'
    if fila is None:
        fila = AnaliticaKpiDiario(dia_operativo=dia, almacen_id=almacen_id, metrica=clave)
        db.session.add(fila)
        resultado = 'nueva'
    fila.valor = r.valor
    fila.n = r.n
    fila.numerador = r.numerador
    fila.denominador = r.denominador
    fila.estado = r.estado
    fila.motivo = r.motivo
    fila.fuente = METRICAS[clave].fuente
    fila.detalle = json.dumps(r.detalle, ensure_ascii=False, default=str) if r.detalle else None
    fila.calculado_en = ahora or datetime.utcnow()
    db.session.flush()
    return resultado


def calcular_y_guardar_dia(dia: date, almacen_id: int = None) -> dict:
    """Todas las métricas de un día: el total y, si se abre por almacén, cada
    almacén activo (o solo `almacen_id`). Un commit por día."""
    from app.models.almacen import Almacen
    ctx = _Contexto()
    ahora = datetime.utcnow()
    if almacen_id is not None:
        almacenes = [_almacen(almacen_id).id]
        totales = False
    else:
        almacenes = [a.id for a in Almacen.query.filter(Almacen.activo.is_(True))
                     .order_by(Almacen.id).all()]
        totales = True
    cuenta = {'nueva': 0, 'actualizada': 0, 'conservada': 0}
    estados = {e: 0 for e in EstadoKpi.TODOS}
    for m in _CATALOGO:
        if m.agregacion == COHORTE:
            continue            # se miden en vivo por período: no hay valor del día
        destinos = ([None] if totales else []) + (almacenes if m.por_almacen else [])
        for alm_id in destinos:
            r = calcular(m.clave, dia, alm_id, ctx)
            cuenta[guardar(dia, alm_id, m.clave, r, ahora)] += 1
            estados[r.estado] += 1
    db.session.commit()
    return {'dia': dia.isoformat(), 'filas': cuenta, 'estados': estados}


class RecalculoEnCurso(RuntimeError):
    """Otro proceso tiene el lock del KPI diario (el cron o un recálculo manual)."""


def recalcular_rango(desde: date, hasta: date, almacen_id: int = None, hoy: date = None) -> dict:
    """Calcula y guarda cada día de `desde` a `hasta`. Idempotente. **El día en
    curso no se guarda** (está a medias): `hasta` tiene que ser anterior a hoy.

    Toma `LOCK_ANALITICA_KPI`: el cron y el recálculo manual no se pisan (dos
    inserciones del mismo día chocarían en el índice único)."""
    from app.utils.lock import LOCK_ANALITICA_KPI, advisory_lock
    hoy = hoy or dia_operativo()
    if desde > hasta:
        raise ValueError(f'desde ({desde}) es posterior a hasta ({hasta})')
    if hasta >= hoy:
        raise ValueError(f'hasta ({hasta}) tiene que ser anterior a hoy ({hoy}): '
                         'el día en curso no se guarda')
    with advisory_lock(LOCK_ANALITICA_KPI, 'analitica_kpi') as tomado:
        if not tomado:
            raise RecalculoEnCurso('otro proceso está calculando el KPI diario')
        dias = []
        dia = desde
        while dia <= hasta:
            try:
                dias.append(calcular_y_guardar_dia(dia, almacen_id))
            except Exception as e:
                db.session.rollback()
                logger.exception('[ANALITICA_KPI] el día %s falló', dia)
                dias.append({'dia': dia.isoformat(), 'error': str(e)[:300]})
            dia += timedelta(days=1)
    total = {'nueva': 0, 'actualizada': 0, 'conservada': 0}
    for d in dias:
        for k, v in (d.get('filas') or {}).items():
            total[k] += v
    return {'desde': desde.isoformat(), 'hasta': hasta.isoformat(), 'almacen_id': almacen_id,
            'filas': total, 'dias': dias}


def correr_kpi(hoy: date = None) -> dict:
    """El cron. **El interruptor vive acá**: sin `ANALITICA_KPI=true` no calcula
    ni escribe nada. Calcula ayer y recalcula los `dias_recalculo()` anteriores."""
    if not encendido():
        return {'omitido': 'ANALITICA_KPI no está en true — nace apagado'}
    hoy = hoy or dia_operativo()
    ayer = hoy - timedelta(days=1)
    return recalcular_rango(ayer - timedelta(days=dias_recalculo()), ayer, hoy=hoy)


# ──────────────────────────────────────────────────────────────────────────────
# Lectura: serie, agregación, resumen
# ──────────────────────────────────────────────────────────────────────────────

#: Métricas que leen la verdad de Siesa (fotos de ventas y cartera), no la
#: operación del WMS: el ensayo del WMS no las ensucia, así que el corte
#: (`FECHA_INICIO_AUDITORIA`) no se les aplica.
METRICAS_SIN_CORTE = frozenset({'ventas_facturadas', 'cartera_abierta', 'cartera_vencida'})


def dia_de_corte_de(clave: str):
    """El día Bogotá desde el que cuenta la métrica, o `None` (sin corte, o
    métrica de Siesa). Los días anteriores **se guardan y se muestran** (con
    `antes_del_corte`), pero no entran al período, a la comparación ni a la
    alerta: son del ensayo."""
    if clave in METRICAS_SIN_CORTE:
        return None
    from app.services import corte
    return corte.dia_de_corte()


def _punto(dia, r: Resultado = None, fila: AnaliticaKpiDiario = None, en_curso=False,
           motivo=None) -> dict:
    if fila is not None:
        p = fila.to_dict()
        p.pop('metrica', None)
        p.pop('almacen_id', None)
    elif r is not None:
        p = {'dia': dia.isoformat(), **r.to_dict(), 'fuente': None, 'calculado_en': None}
    else:
        p = {'dia': dia.isoformat(), 'estado': EstadoKpi.AUSENTE, 'valor': None, 'n': None,
             'numerador': None, 'denominador': None, 'motivo': motivo, 'detalle': {},
             'fuente': None, 'calculado_en': None}
    p['en_curso'] = en_curso
    return p


def serie(clave: str, desde: date, hasta: date, almacen_id: int = None,
          hoy: date = None) -> list:
    """Un punto por día de `desde` a `hasta`, con su estado. Lo guardado se lee
    de la tabla; **el día en curso se calcula en vivo** y se marca
    (`en_curso`, INCOMPLETO si daba OK: el día no terminó). Un día pasado sin
    fila es AUSENTE «sin calcular» — no se calcula al vuelo: el valor del día
    es el que quedó guardado, que sobrevive al acta de corte."""
    m = METRICAS[clave]
    hoy = hoy or dia_operativo()
    if almacen_id is not None:
        _almacen(almacen_id)
    if m.agregacion == COHORTE:
        dias = (hasta - desde).days + 1
        return [_punto(desde + timedelta(days=i), motivo=EN_VIVO) for i in range(dias)]
    if almacen_id is not None and not m.por_almacen:
        dias = (hasta - desde).days + 1
        return [_punto(desde + timedelta(days=i), motivo=NO_POR_ALMACEN) for i in range(dias)]
    q = AnaliticaKpiDiario.query.filter(
        AnaliticaKpiDiario.metrica == clave,
        AnaliticaKpiDiario.dia_operativo >= desde,
        AnaliticaKpiDiario.dia_operativo <= hasta)
    q = (q.filter(AnaliticaKpiDiario.almacen_id == almacen_id) if almacen_id is not None
         else q.filter(AnaliticaKpiDiario.almacen_id.is_(None)))
    filas = {f.dia_operativo: f for f in q.all()}
    dia_corte = dia_de_corte_de(clave)
    puntos = []
    dia = desde
    while dia <= hasta:
        if dia == hoy:
            r = calcular(clave, dia, almacen_id)
            if r.estado == EstadoKpi.OK:
                r.estado, r.motivo = EstadoKpi.INCOMPLETO, DIA_EN_CURSO
            elif r.motivo:
                r.motivo = f'{DIA_EN_CURSO}; {r.motivo}'
            puntos.append(_punto(dia, r, en_curso=True))
        elif dia in filas:
            puntos.append(_punto(dia, fila=filas[dia]))
        else:
            puntos.append(_punto(dia, motivo=SIN_CALCULAR))
        puntos[-1]['antes_del_corte'] = bool(dia_corte and dia < dia_corte)
        dia += timedelta(days=1)
    return puntos


def _medido(p) -> bool:
    """El día se midió de punta a punta: OK, o una tasa sin denominador o con
    muestra chica (se midió, y no había tasa que dar)."""
    if p.get('en_curso'):
        return False
    return p['estado'] == EstadoKpi.OK or (
        p['estado'] == EstadoKpi.AUSENTE and p.get('denominador') is not None)


def agregar(clave: str, puntos: list) -> dict:
    """El valor de un período a partir de sus días, según la agregación de la
    métrica. Declara cuántos días faltan o están incompletos."""
    m = METRICAS[clave]
    # Lo anterior al corte se cuenta, no se suma (`dia_de_corte_de`).
    antes = sum(1 for p in puntos if p.get('antes_del_corte'))
    puntos = [p for p in puntos if not p.get('antes_del_corte')]
    dias = len(puntos)
    faltan = [p['dia'] for p in puntos if not _medido(p)]
    res = {'valor': None, 'n': None, 'estado': EstadoKpi.AUSENTE, 'motivo': None,
           'dias': dias, 'dias_medidos': dias - len(faltan), 'dias_sin_medir': len(faltan),
           'dias_antes_del_corte': antes}
    if not puntos and antes:
        res['motivo'] = 'todo el período es anterior al corte (FECHA_INICIO_AUDITORIA)'
        return res
    if m.agregacion == TASA:
        con = [p for p in puntos if p.get('denominador') is not None]
        num = sum(p['numerador'] or 0 for p in con)
        den = sum(p['denominador'] or 0 for p in con)
        res.update(numerador=num if con else None, denominador=den if con else None,
                   n=sum(p['n'] or 0 for p in con) if con else None)
        if not con:
            res['motivo'] = 'ningún día del período tiene dato'
            return res
        if not den:
            res['motivo'] = 'sin denominador en el período'
            return res
        if m.min_n is not None and den < m.min_n:
            res['motivo'] = f'n={int(den)} < {m.min_n}: muestra chica, no se publica la tasa'
            return res
        res['valor'] = num / den
    elif m.agregacion == SUMA:
        con = [p for p in puntos if p['valor'] is not None]
        if not con:
            res['motivo'] = 'ningún día del período tiene dato'
            return res
        res['valor'] = sum(p['valor'] for p in con)
        res['n'] = sum(p['n'] or 0 for p in con)
    else:  # NIVEL
        con = [p for p in puntos if p['valor'] is not None]
        if not con:
            res['motivo'] = 'ningún día del período tiene dato'
            return res
        ultimo = max(con, key=lambda p: p['dia'])
        res.update(valor=ultimo['valor'], n=ultimo['n'], dia_del_valor=ultimo['dia'])
        if puntos and ultimo['dia'] != max(p['dia'] for p in puntos):
            faltan = faltan or [ultimo['dia']]
            res['motivo'] = f"saldo del {ultimo['dia']}, el último día con dato"
    if faltan:
        res['estado'] = EstadoKpi.INCOMPLETO
        extra = f'{len(faltan)} de {dias} día(s) sin medir o incompletos'
        res['motivo'] = f"{res['motivo']}; {extra}" if res['motivo'] else extra
    else:
        res['estado'] = EstadoKpi.OK
    return res


def _variacion(m: Metrica, actual: dict, anterior: dict) -> dict:
    if actual['valor'] is None or anterior['valor'] is None:
        return {'absoluta': None, 'relativa': None, 'favorable': None, 'comparable': False,
                'motivo': 'uno de los dos períodos no tiene valor'}
    absoluta = actual['valor'] - anterior['valor']
    relativa = absoluta / abs(anterior['valor']) if anterior['valor'] else None
    favorable = None
    if absoluta:
        favorable = (absoluta > 0) == (m.direccion == SUBE_ES_BUENO)
    comparable = actual['estado'] == EstadoKpi.OK and anterior['estado'] == EstadoKpi.OK
    return {'absoluta': absoluta, 'relativa': relativa, 'favorable': favorable,
            'comparable': comparable,
            'motivo': None if comparable else
            'algún período tiene días sin medir: la variación puede ser del dato, no del negocio'}


def _lunes(d: date) -> date:
    return d - timedelta(days=d.weekday())


def alerta_de_cambio(clave: str, desde: date, hasta: date, almacen_id: int = None,
                     hoy: date = None) -> dict:
    """El CUSUM de Vigía (`cusum_bilateral`, la misma función) sobre las
    semanas cerradas y completas de la métrica que terminan a más tardar en
    `hasta`. Una semana con un día sin medir se excluye y se cuenta: el CUSUM
    leería el hueco como un desplome. Sin `MIN_SEMANAS_CUSUM` semanas
    completas, no hay alerta y se dice por qué."""
    from app.services.vigia_service import MIN_SEMANAS_CUSUM, VENTANA_REF, cusum_bilateral
    m = METRICAS[clave]
    hoy = hoy or dia_operativo()
    if m.agregacion == COHORTE:
        return {'disponible': False, 'motivo': EN_VIVO + ': sin historia semanal guardada'}
    if almacen_id is not None and not m.por_almacen:
        return {'disponible': False, 'motivo': NO_POR_ALMACEN}
    tope = min(hasta, hoy - timedelta(days=1))
    ultimo_domingo = tope - timedelta(days=(tope.weekday() + 1) % 7)
    primer_lunes = _lunes(ultimo_domingo) - timedelta(weeks=SEMANAS_HISTORIA_CUSUM - 1)
    puntos = serie(clave, primer_lunes, ultimo_domingo, almacen_id, hoy=hoy) \
        if primer_lunes <= ultimo_domingo else []
    semanas, excluidas, antes_del_corte = [], 0, 0
    for i in range(0, len(puntos), 7):
        semana = puntos[i:i + 7]
        if any(p.get('antes_del_corte') for p in semana):
            antes_del_corte += 1          # ensayo: fuera de la referencia del CUSUM
            continue
        if len(semana) < 7 or not all(_medido(p) for p in semana):
            excluidas += 1
            continue
        valor = agregar(clave, semana)['valor']
        if valor is None:
            excluidas += 1
            continue
        semanas.append((semana[0]['dia'], valor))
    base = {'metodo': 'CUSUM de Vigía sobre semanas cerradas (vigia_service.cusum_bilateral)',
            'semanas_usadas': len(semanas), 'semanas_excluidas': excluidas,
            'semanas_antes_del_corte': antes_del_corte,
            'minimo_semanas': MIN_SEMANAS_CUSUM}
    if len(semanas) < MIN_SEMANAS_CUSUM:
        return {**base, 'disponible': False,
                'motivo': (f'historia insuficiente: {len(semanas)} semana(s) cerrada(s) y '
                           f'completa(s); el CUSUM de Vigía pide {MIN_SEMANAS_CUSUM}')}
    calc = cusum_bilateral([v for _, v in semanas])
    ultimo = calc['puntos'][-1]

    def _lectura(alarma):
        if not alarma:
            return None
        return 'favorable' if (alarma == 'SUBE') == (m.direccion == SUBE_ES_BUENO) else 'desfavorable'

    en_periodo = [
        {'semana': s, 'alarma': p['alarma'], 'severidad': p['severidad'],
         'lectura': _lectura(p['alarma'])}
        for (s, _), p in zip(semanas, calc['puntos'])
        if p['alarma'] and date.fromisoformat(s) + timedelta(days=6) >= desde]
    return {**base, 'disponible': True, 'motivo': None,
            'ventana_referencia': min(VENTANA_REF, len(semanas)),
            'mu_ref': calc['mu_ref'], 'sigma_ref': calc['sigma_ref'],
            'ultima_semana': semanas[-1][0], 'alarma': ultimo['alarma'],
            'severidad': ultimo['severidad'], 'lectura': _lectura(ultimo['alarma']),
            's_plus': ultimo['s_plus'], 's_minus': ultimo['s_minus'],
            'alarmas_en_periodo': en_periodo}


def _es_piso(m: Metrica, periodo: dict) -> bool:
    """Un total de pesos INCOMPLETO de algo malo (↓) es una cota inferior: lo
    que falta solo puede empeorarlo."""
    if periodo.get('es_piso'):
        return True
    return (periodo.get('estado') == EstadoKpi.INCOMPLETO and m.agregacion == SUMA
            and m.direccion == BAJA_ES_BUENO)


def _cohorte_como_periodo(medida: dict, desde, hasta) -> dict:
    return {'valor': medida['valor'], 'n': medida['n'], 'estado': medida['estado'],
            'motivo': medida['motivo'], 'es_piso': medida.get('es_piso', False),
            'dias': (hasta - desde).days + 1}


def resumen(desde: date, hasta: date, almacen_id: int = None, hoy: date = None,
            medicion=None) -> list:
    """Cada métrica: el período, el anterior de igual duración, la variación y
    la alerta de cambio. Las de COHORTE se miden en vivo (`analitica_portada`);
    `medicion` permite compartir la misma lectura con la portada."""
    hoy = hoy or dia_operativo()
    largo = (hasta - desde).days + 1
    ant_hasta = desde - timedelta(days=1)
    ant_desde = ant_hasta - timedelta(days=largo - 1)
    salida = []
    for m in _CATALOGO:
        if m.agregacion == COHORTE:
            from app.services import analitica_portada as port
            if medicion is None:
                medicion = port.Medicion(desde, hasta, almacen_id, hoy)
            actual = _cohorte_como_periodo(medicion.medir(m.clave, desde, hasta), desde, hasta)
            anterior = _cohorte_como_periodo(medicion.medir(m.clave, ant_desde, ant_hasta),
                                             ant_desde, ant_hasta)
        else:
            actual = agregar(m.clave, serie(m.clave, desde, hasta, almacen_id, hoy=hoy))
            anterior = agregar(m.clave, serie(m.clave, ant_desde, ant_hasta, almacen_id, hoy=hoy))
        salida.append({
            **m.to_dict(),
            'periodo': {'desde': desde.isoformat(), 'hasta': hasta.isoformat(), **actual},
            'anterior': {'desde': ant_desde.isoformat(), 'hasta': ant_hasta.isoformat(),
                         **anterior},
            'variacion': _variacion(m, actual, anterior),
            'semaforo': semaforo(m.clave, actual['valor'], dias=largo,
                                 es_piso=_es_piso(m, actual)),
            'alerta': alerta_de_cambio(m.clave, desde, hasta, almacen_id, hoy=hoy),
        })
    return salida


# ──────────────────────────────────────────────────────────────────────────────
# Frescura (para /api/health/siesa y el meta de los endpoints)
# ──────────────────────────────────────────────────────────────────────────────

def estado(dias: int = 7, hoy: date = None) -> dict:
    """Frescura del KPI diario, leída **de la base** (el cron corre en el
    worker). Un día sin filas del total es un hueco: no hay KPI de ese día,
    no un KPI en cero. Nunca levanta."""
    try:
        hoy = hoy or dia_operativo()
        desde = hoy - timedelta(days=dias)
        ultimo = db.session.query(func.max(AnaliticaKpiDiario.calculado_en)).scalar()
        filas = (db.session.query(AnaliticaKpiDiario.dia_operativo, AnaliticaKpiDiario.estado,
                                  func.count(AnaliticaKpiDiario.id))
                 .filter(AnaliticaKpiDiario.dia_operativo >= desde,
                         AnaliticaKpiDiario.almacen_id.is_(None))
                 .group_by(AnaliticaKpiDiario.dia_operativo, AnaliticaKpiDiario.estado).all())
        por_dia = {}
        for d, est, n in filas:
            por_dia.setdefault(d.isoformat(), {})[est] = n
        huecos = [(desde + timedelta(days=i)).isoformat() for i in range(dias)
                  if (desde + timedelta(days=i)).isoformat() not in por_dia]
        return {
            'encendido': encendido(),
            'dias_recalculo': dias_recalculo(),
            'ultimo_calculo': ultimo.isoformat() if ultimo else None,
            'totales_por_dia': por_dia,
            'dias_sin_calcular': huecos,
            'nota': ('Un día sin calcular no tiene KPI, no un KPI en cero. AUSENTE es un '
                     'dato que no existe; INCOMPLETO, un total que es cota.'),
        }
    except Exception as e:
        return {'encendido': encendido(), 'error': str(e)[:200]}


# ──────────────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────────────

def init_scheduler(app):
    """Cron diario 04:30 Bogotá: después de la medianoche (ayer ya cerró) y
    antes de los crons de las 5:30–6:30. No toca Siesa, así que la ventana de
    la Regla 14 no aplica. Nace apagado: sin `ANALITICA_KPI=true`,
    `correr_kpi` devuelve su motivo sin escribir nada."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[ANALITICA_KPI] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            try:
                r = correr_kpi()
                logger.info('[ANALITICA_KPI] %s', r.get('omitido') or r.get('filas'))
            except RecalculoEnCurso:
                logger.info('[ANALITICA_KPI] otro proceso ya calcula el KPI diario')
            except Exception as e:
                db.session.rollback()
                logger.error('[ANALITICA_KPI] falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(func=con_latido('analitica_kpi_diario', _job), trigger=CronTrigger(hour=4, minute=30,
                                                     timezone='America/Bogota'),
                      id='analitica_kpi_diario', replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
    scheduler.start()
    logger.info('[ANALITICA_KPI] Scheduler 04:30 Bogotá (encendido=%s)', encendido())
    return scheduler

"""
Una ruta entregada que nadie liquidó. **Una política, una función.**

El diagnóstico BK-OPS-01 v2.1 lo pide con estas palabras: *«Alerta de ruta
entregada sin liquidar, con días transcurridos. Hoy no existe ninguna: una ruta
puede quedar sin liquidar indefinidamente y nadie se entera.»*

Y era literal. El número existía —`rezago_liquidacion` en el desglose— pero es
un número en una pantalla que alguien tiene que abrir. El mismo documento
advierte por qué eso no alcanza: *«alguien que compare tres números una vez al
mes deja de hacerlo al tercero»*. Un dato que hay que ir a buscar no es una
alerta; es un dato que se va a mirar el día que ya sea tarde.

## Por qué importa que no se liquide

La factura de ruta nace a un día de plazo — es la única forma que Siesa
permite, porque una factura de contado exige el recaudo en el acto de
aprobarse. El saldo que esa factura deja abierto lo cierra el recibo de caja de
la liquidación.

Mientras nadie liquide, ese saldo **es cartera vencida a los ojos de todos los
sistemas**: computa mora, consume cupo y puede frenar el próximo pedido del
mismo cliente. No es una demora administrativa, es el mecanismo exacto que
produjo la cartera fantasma.

## Las dos urgencias no son la misma

`ATRASADA` es una ruta que debió liquidarse el mismo día y no se liquidó.

`CRUZA_MES` es peor y por eso tiene nombre propio: la entrega ocurrió en un mes
y el recaudo va a registrarse en otro. Eso ya no se arregla liquidando rápido
—el período contable no se puede mover— y es lo que la regla de cierre de mes
del diagnóstico viene a evitar.

Se distingue por mes calendario y no por «los últimos N días del mes» a
propósito: cualquier N sería un número inventado, y este proyecto ya tiene
suficientes umbrales razonados sin medir. El cruce de mes es un hecho, no un
umbral.
"""
from datetime import datetime

from app.utils.fecha import ahora_bogota, dia_operativo_de

#: Se liquida el mismo día. Un día de rezago ya es un incumplimiento del
#: proceso LOG-03, no una tolerancia.
OK = 'ok'
ATRASADA = 'atrasada'
CRUZA_MES = 'cruza_mes'


def fecha_de_referencia(ruta):
    """El día contra el que se mide el rezago.

    `fecha_entregada` es DateTime **UTC naive** y `fecha_programada` es Date:
    restarlas sin normalizar revienta. Y se usa el día de Bogotá, no el UTC —
    una ruta entregada a las 8 p.m. tiene 0 días de rezago, no 1 (Regla 5).

    Hasta el 2026-09-24 esto hacía `entregada.date()`, que sobre UTC es el día
    UTC: la docstring decía Bogotá y el código calculaba UTC. Una ruta
    entregada el 31 a las 9 p. m. caía el 1.º y no «cruzaba mes».
    """
    entregada = getattr(ruta, 'fecha_entregada', None)
    if isinstance(entregada, datetime):
        return dia_operativo_de(entregada)
    if entregada is not None:
        return entregada
    return getattr(ruta, 'fecha_programada', None)


def dias_de_rezago(ruta, hoy=None):
    """Días desde la entrega. `None` si la ruta no tiene fecha con qué medir."""
    ref = fecha_de_referencia(ruta)
    if ref is None:
        return None
    return ((hoy or ahora_bogota().date()) - ref).days


def urgencia(ruta, hoy=None):
    """`ok` | `atrasada` | `cruza_mes`.

    Ante una ruta sin fecha devuelve `ATRASADA` y no `OK`: no saber cuándo se
    entregó no es evidencia de que se entregó hoy. Regla 0 — el lado
    conservador es que aparezca en la lista y alguien la mire.
    """
    hoy = hoy or ahora_bogota().date()
    ref = fecha_de_referencia(ruta)
    if ref is None:
        return ATRASADA
    if (ref.year, ref.month) != (hoy.year, hoy.month):
        return CRUZA_MES
    return ATRASADA if (hoy - ref).days >= 1 else OK


def _todas_entregadas_sin_liquidar():
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    return (RutaDespacho.query
            .filter(RutaDespacho.estado == 'ENTREGADA')
            .filter(RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA)
            .all())


def separar_por_corte(rutas):
    """`(vigentes, anteriores)`: entregadas antes del corte
    (`FECHA_INICIO_AUDITORIA`, por `fecha_de_referencia`) van aparte. Una ruta
    sin fecha es vigente (Regla 0: no saber cuándo no la vuelve vieja)."""
    from app.services import corte
    return corte.separar(rutas, fecha_de_referencia)


def rutas_entregadas_sin_liquidar():
    """Las rutas entregadas cuyo cierre financiero no ocurrió, **desde el
    corte**. Las anteriores (el ensayo) no se esconden: `diagnostico` las
    cuenta en `antes_del_corte`, pero no entran a la alerta ni a la fuga.

    Vive acá y no repetida en el endpoint y en el cron: si las dos consultas
    divergieran, la alerta avisaría de un universo y el tablero mostraría otro
    — y el que manda el correo es el que nadie está mirando.
    """
    return separar_por_corte(_todas_entregadas_sin_liquidar())[0]


def diagnostico(hoy=None):
    """`{rutas, atrasadas, cruzan_mes, dias:[...]}` — lo que leen el desglose y
    el cron, calculado una sola vez y de una sola forma."""
    hoy = hoy or ahora_bogota().date()
    filas, dias = [], []
    vigentes, anteriores = separar_por_corte(_todas_entregadas_sin_liquidar())
    for ruta in vigentes:
        d = dias_de_rezago(ruta, hoy)
        if d is not None:
            dias.append(d)
        filas.append({
            'ruta_id': ruta.id,
            'dias': d,
            'urgencia': urgencia(ruta, hoy),
            'estado_financiero': ruta.estado_financiero,
        })
    return {
        'rutas': filas,
        'atrasadas': [f for f in filas if f['urgencia'] == ATRASADA],
        'cruzan_mes': [f for f in filas if f['urgencia'] == CRUZA_MES],
        'dias': dias,
        # Contadas, fuera de la alerta: entregadas antes de FECHA_INICIO_AUDITORIA.
        'antes_del_corte': len(anteriores),
        # Los recibos que ya se fecharon en el mes del envío (tanda 2).
        'recibos_de_otro_mes': recibos_de_otro_mes(hoy),
    }


def recibos_de_otro_mes(hoy=None):
    """Los recibos de caja cuyo cobro fue en un mes y se registraron en otro
    (`RecaudoEntrega.rc_cobro_otro_mes`, lo marca el ejecutor del RC con
    `politica_cobro.fechas_del_recibo`). Es la otra cara de `CRUZA_MES`: la
    ruta ya se liquidó, pero el recaudo quedó en el período siguiente y
    contabilidad tiene que saberlo al cerrar. Los del mes anterior y el actual
    (lo más viejo ya se cerró). `[{recaudo_id, ruta_id, pedido, cobrado_el}]`."""
    from app.models.recaudo_entrega import RecaudoEntrega
    hoy = hoy or ahora_bogota().date()
    primero = hoy.replace(day=1)
    desde = (primero.replace(year=primero.year - 1, month=12) if primero.month == 1
             else primero.replace(month=primero.month - 1))
    filas = (RecaudoEntrega.query
             .filter(RecaudoEntrega.rc_cobro_otro_mes.isnot(None))
             .filter(RecaudoEntrega.rc_cobro_otro_mes >= desde)
             .order_by(RecaudoEntrega.rc_cobro_otro_mes).all())
    return [{'recaudo_id': r.id, 'ruta_id': r.ruta_id,
             'pedido': getattr(r.tarea, 'numero_pedido_siesa', None),
             'cobrado_el': r.rc_cobro_otro_mes.isoformat()} for r in filas]


def rutas_sin_liquidar_al_cierre(dia):
    """Las rutas que al CIERRE del día `dia` (Bogotá) estaban entregadas y sin
    liquidar, con su urgencia medida contra `dia`. Es `rutas_entregadas_sin_liquidar`
    + `urgencia` llevados a un día pasado: para `dia` = hoy da el mismo universo.

    El pasado se reconstruye con `liquidada_en` (m035bitacora). Una ruta
    LIQUIDADA **sin** `liquidada_en` se liquidó antes de que esa columna
    existiera: si `dia` es posterior a la primera `liquidada_en` registrada, se
    sabe que ya estaba liquidada; si no, no se sabe y va a `desconocidas` — no
    se adivina hacia ningún lado (Regla 0). Una ruta sin fecha cuenta si ya
    existía ese día, igual que `urgencia` la cuenta como atrasada.

    Devuelve `{'rutas': [...], 'con_rezago': n, 'atrasadas': n, 'cruzan_mes': n,
    'desconocidas': n}`; `con_rezago` = atrasadas + cruzan_mes.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from app.extensions import db
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    from app.utils.fecha import inicio_del_dia_utc

    fin = inicio_del_dia_utc(dia + timedelta(days=1))
    primera = db.session.query(func.min(RutaDespacho.liquidada_en)).scalar()
    registro_desde = dia_operativo_de(primera) if primera else None

    from app.services import corte
    corte_utc = corte.inicio_auditoria()
    filas, desconocidas, antes_del_corte = [], 0, 0
    for ruta in RutaDespacho.query.filter(RutaDespacho.estado == 'ENTREGADA').all():
        ref = fecha_de_referencia(ruta)
        if corte.es_anterior(ref, corte_utc):
            antes_del_corte += 1          # del ensayo: se cuenta, no sube el nivel
            continue
        if ref is not None and ref > dia:
            continue                      # se entregó después de ese día
        if ref is None and (ruta.fecha_creacion is None or ruta.fecha_creacion >= fin):
            continue                      # sin fecha y todavía no existía
        if ruta.estado_financiero == EstadoFinancieroRuta.LIQUIDADA:
            if ruta.liquidada_en is not None:
                if ruta.liquidada_en < fin:
                    continue              # ya estaba liquidada al cierre
            elif registro_desde is not None and dia >= registro_desde:
                continue                  # liquidada antes de que existiera la columna
            else:
                desconocidas += 1
                continue
        filas.append({'ruta_id': ruta.id, 'dias': dias_de_rezago(ruta, dia),
                      'urgencia': urgencia(ruta, dia)})
    atrasadas = sum(1 for f in filas if f['urgencia'] == ATRASADA)
    cruzan = sum(1 for f in filas if f['urgencia'] == CRUZA_MES)
    return {'rutas': filas, 'con_rezago': atrasadas + cruzan, 'atrasadas': atrasadas,
            'cruzan_mes': cruzan, 'desconocidas': desconocidas,
            'antes_del_corte': antes_del_corte}

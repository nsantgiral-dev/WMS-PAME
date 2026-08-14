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
from app.utils.fecha import ahora_bogota

#: Se liquida el mismo día. Un día de rezago ya es un incumplimiento del
#: proceso LOG-03, no una tolerancia.
OK = 'ok'
ATRASADA = 'atrasada'
CRUZA_MES = 'cruza_mes'


def fecha_de_referencia(ruta):
    """El día contra el que se mide el rezago.

    `fecha_entregada` es DateTime y `fecha_programada` es Date: restarlas sin
    normalizar revienta. Y se usa la fecha de Bogotá, no UTC — una ruta
    entregada a las 8 p.m. tiene 0 días de rezago, no 1 (Regla 5).
    """
    entregada = getattr(ruta, 'fecha_entregada', None)
    if entregada is not None:
        return entregada.date() if hasattr(entregada, 'date') else entregada
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


def rutas_entregadas_sin_liquidar():
    """Las rutas entregadas cuyo cierre financiero no ocurrió.

    Vive acá y no repetida en el endpoint y en el cron: si las dos consultas
    divergieran, la alerta avisaría de un universo y el tablero mostraría otro
    — y el que manda el correo es el que nadie está mirando.
    """
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    return (RutaDespacho.query
            .filter(RutaDespacho.estado == 'ENTREGADA')
            .filter(RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA)
            .all())


def diagnostico(hoy=None):
    """`{rutas, atrasadas, cruzan_mes, dias:[...]}` — lo que leen el desglose y
    el cron, calculado una sola vez y de una sola forma."""
    hoy = hoy or ahora_bogota().date()
    filas, dias = [], []
    for ruta in rutas_entregadas_sin_liquidar():
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
    }

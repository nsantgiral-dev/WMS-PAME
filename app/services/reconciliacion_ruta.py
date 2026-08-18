"""
Cuatro números por día de ruta que tienen que ser iguales.

    entregas que debían cobrarse
        = cobros registrados
        = recibos de caja cruzados en Siesa
        = recaudo verificado al cierre

Cada diferencia es una fuga, y **cada tramo nombra su causa**. Es lo que
BK-OPS-01 §4.4 pide como reconciliación de tres sistemas, con una columna más
que la propuesta original.

## Por qué la cuarta columna y por qué no es «efectivo contado»

Las tres primeras salen todas de la base del WMS: se verifican **entre sí**, no
contra la plata. Un conductor que cobra $100, registra $100 y entrega $90
produce tres números perfectos.

La cuarta es la única que no la produce ningún sistema — la cuenta una persona
al cerrar la ruta. Y no puede ser solo el efectivo: `forma_pago` admite
transferencia y cheque, y si el control cubre una parte, el modo de fallo se
muda al medio de pago que nadie verifica. Efectivo contado **más** comprobantes
de transferencia **más** cheques físicos.

## Hoy esa columna no tiene fuente, y eso se declara

No hay campo donde se registre el conteo del cierre. Este módulo **no lo
inventa**: lo reporta como `capturado: False`. Un cero fabricado ahí sería una
alarma de faltante total; un número derivado de las otras columnas sería la
propia cifra que se quiere contrastar, verificándose contra sí misma.

Mientras no exista, la reconciliación cubre tres de cuatro tramos **y lo dice**.
Es la misma regla del denominador visible: «0 hallazgos» y «no se midió» no
pueden verse igual.

## La vara

El proceso que el WMS reemplaza dejó 72 facturas C02 sin cerrar sobre 15.554
emitidas en doce meses: **0,46% de ciclos rotos.**

Es un **piso, no la tasa real** — se mide sobre las que siguen abiertas hoy, así
que un ciclo que se rompió y alguien arregló después no aparece. La tasa
verdadera del proceso viejo es mayor, lo que hace la vara más exigente, que es
el lado correcto del error.

Sirve para contestar el viernes de la primera semana la única pregunta que
importa: **¿el reemplazo salió mejor o peor que lo reemplazado?**
"""
import json
import logging

logger = logging.getLogger(__name__)

#: 72 de 15.554 en doce meses. Ver el encabezado: es un piso.
VARA_CICLOS_ROTOS = 0.0046

#: Los tramos, en orden. El `id` es estable — lo consume la pantalla.
TRAMOS = (
    ('entrega_sin_cobro', 'debian_cobrarse', 'cobros_registrados',
     'Se entregó y no se registró cobro'),
    ('cobro_sin_recibo', 'cobros_registrados', 'rc_en_siesa',
     'Se cobró y el recibo no llegó a Siesa'),
    ('recibo_sin_verificar', 'rc_en_siesa', 'recaudo_verificado',
     'El recibo entró y la plata no se verificó al cierre'),
)


def _monto(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _debia_cobrarse(recaudo, tarea, connekta) -> bool:
    """¿Esta parada tenía que producir plata en la puerta?

    Se pregunta por `cobra_en_la_puerta` y **no** por la forma de pago
    elegida: usar lo que el conductor marcó haría que una parada donde no se
    pidió cobrar saliera del denominador — el defecto se borraría a sí mismo
    de su propia medición.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import cond_pago as _cp

    if recaudo.estado_entrega not in (EstadoEntrega.ENTREGADO,
                                      EstadoEntrega.PARCIAL):
        return False
    return _cp.cobra_en_la_puerta(
        getattr(tarea, 'cond_pago', None),
        connekta.cond_pago_ventas, connekta.cond_pago_ruta) is True


def _rc_llego_a_siesa(recaudo_id: int) -> bool:
    """`COMPLETADO`, y **solo** `COMPLETADO`.

    `siesa_rc_triggered` no sirve acá: se enciende ANTES del POST (Regla 6),
    así que un job que falló deja la bandera puesta. Y `DESCARTADO` tampoco
    cuenta — un humano decidió no intentarlo más, no que haya llegado.
    """
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    return SiesaJob.query.filter_by(
        tipo='RECIBO_CAJA', referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo_id, estado=EstadoSiesaJob.COMPLETADO,
    ).first() is not None


def _monto_del_recibo(recaudo_id: int):
    """El monto que el RC llevó de verdad, del payload del job. `None` si no
    hay job completado — que no es lo mismo que cero."""
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    job = SiesaJob.query.filter_by(
        tipo='RECIBO_CAJA', referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo_id, estado=EstadoSiesaJob.COMPLETADO,
    ).first()
    if not job:
        return None
    try:
        return _monto(json.loads(job.payload).get('monto'))
    except Exception:
        return None


def reconciliar(ruta_id: int) -> dict:
    """Las cuatro columnas de una ruta, con las fugas por tramo."""
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    from app.services.connekta_gateway import connekta

    ruta = RutaDespacho.query.get(ruta_id)
    if not ruta:
        raise ValueError(f'La ruta {ruta_id} no existe')

    recaudos = RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all()
    tareas = {t.id: t for t in TareaPacking.query.filter(
        TareaPacking.id.in_([r.tarea_id for r in recaudos]))} if recaudos else {}

    cols = {k: {'n': 0, 'valor': 0.0} for k in
            ('debian_cobrarse', 'cobros_registrados', 'rc_en_siesa')}
    detalle, sin_condicion = [], 0

    for r in recaudos:
        tarea = tareas.get(r.tarea_id)
        if not _debia_cobrarse(r, tarea, connekta):
            # Se cuenta aparte: una parada sin condición conocida no está
            # exenta, está **sin clasificar**, y con la cola mezclada nadie
            # nota que el universo se encogió.
            from app.services import cond_pago as _cp
            if _cp.cobra_en_la_puerta(
                    getattr(tarea, 'cond_pago', None), connekta.cond_pago_ventas,
                    connekta.cond_pago_ruta) is None:
                sin_condicion += 1
            continue

        esperado = _monto(getattr(tarea, 'valor_factura', None))
        cobrado = _monto(r.monto_cobrado)
        en_siesa = _rc_llego_a_siesa(r.id)
        del_recibo = _monto_del_recibo(r.id)

        cols['debian_cobrarse']['n'] += 1
        cols['debian_cobrarse']['valor'] += esperado
        if cobrado > 0:
            cols['cobros_registrados']['n'] += 1
            cols['cobros_registrados']['valor'] += cobrado
        if en_siesa:
            cols['rc_en_siesa']['n'] += 1
            cols['rc_en_siesa']['valor'] += (
                del_recibo if del_recibo is not None else cobrado)

        if cobrado <= 0 or not en_siesa:
            detalle.append({
                'recaudo_id': r.id,
                'tarea_id': r.tarea_id,
                'estado_entrega': r.estado_entrega,
                # El diagnóstico del primer tramo, sin adivinar: si la pantalla
                # estaba en CREDITO sobre una parada que debía cobrarse, es el
                # defecto del clasificador — no un conductor que se olvidó.
                'modo_pantalla': r.modo_pantalla,
                'cond_pago': getattr(tarea, 'cond_pago', None),
                'esperado': esperado,
                'cobrado': cobrado,
                'rc_en_siesa': en_siesa,
                'tramo': 'entrega_sin_cobro' if cobrado <= 0 else 'cobro_sin_recibo',
            })

    #: Sin fuente. **No se deriva de las otras**: una columna calculada desde
    #: lo que quiere contrastar no contrasta nada.
    cols['recaudo_verificado'] = {
        'n': None, 'valor': None, 'capturado': False,
        'nota': 'No hay captura del conteo de cierre. Efectivo contado + '
                'comprobantes de transferencia + cheques físicos. Mientras no '
                'exista, el tramo recibo→verificado no se puede medir.',
    }

    fugas = []
    for tramo_id, desde, hasta, causa in TRAMOS:
        a, b = cols[desde], cols[hasta]
        if b.get('n') is None:
            fugas.append({'tramo': tramo_id, 'causa': causa,
                          'medible': False,
                          'nota': 'la columna de destino no se captura'})
            continue
        fugas.append({
            'tramo': tramo_id, 'causa': causa, 'medible': True,
            'documentos': a['n'] - b['n'],
            'valor': round(a['valor'] - b['valor'], 2),
        })

    rotos = sum(f.get('documentos', 0) for f in fugas if f.get('medible'))
    base = cols['debian_cobrarse']['n']
    tasa = (rotos / base) if base else None

    return {
        'ruta_id': ruta_id,
        'fecha_ruta': (ruta.fecha_programada.isoformat()
                       if ruta.fecha_programada else None),
        'columnas': cols,
        'fugas': fugas,
        'ciclos_rotos': rotos,
        'tasa_ciclos_rotos': tasa,
        'vara': VARA_CICLOS_ROTOS,
        # `None` cuando no hay denominador: sin paradas cobrables no se puede
        # decir que la ruta salió bien, solo que no se midió.
        'peor_que_la_vara': (tasa > VARA_CICLOS_ROTOS) if tasa is not None else None,
        'paradas_sin_condicion': sin_condicion,
        'detalle': detalle,
        'cobertura': {
            'tramos_medibles': sum(1 for f in fugas if f['medible']),
            'tramos_totales': len(TRAMOS),
        },
    }

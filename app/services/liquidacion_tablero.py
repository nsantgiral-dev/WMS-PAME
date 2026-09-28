"""
La cabecera de Liquidación: **un universo, y números que cuadran entre sí**
(2026-09-27, P2-1 de la auditoría de liquidación).

Lo que pasaba: «Por liquidar» no contaba las rutas atrasadas que la lista sí
mostraba; «Efectivo» miraba las rutas del rango y «Efectivo en poder» las
abiertas de cualquier fecha, en la misma pantalla; CHEQUE no caía en ningún
bucket, así que los medios no sumaban el total; el detalle llamaba «Total
facturas» a lo cobrado. Un gerente no podía reconciliar lo que veía.

Ahora:
- **El universo es la lista**: las rutas del rango más las atrasadas. Todo
  número de la cabecera se calcula sobre esas mismas rutas (`universo` lo
  dice en palabras).
- **Los medios parten el total**: efectivo + bancario + tarjeta + cheque +
  otros = cobrado, por construcción (`medio_de_recaudo`, la misma del acta),
  y la respuesta lo comprueba (`cuadra`).
- **Por ruta**: la caja (acta recibida, falta, no aplica), lo que falta en
  Siesa (solo las liquidadas: una sin liquidar tiene todo por delante) y el
  recibo de caja en tres estados — encolado, enviado, confirmado —, con señal
  positiva (`politica_cobro.rc_llegaron`), no con la bandera de pre-envío.
"""
from __future__ import annotations


MEDIOS = ('EFECTIVO', 'BANCARIO', 'TARJETA', 'CHEQUE', 'OTRO')


def resumen(rutas_rango, rutas_atrasadas) -> dict:
    """Los números de la cabecera, sobre la lista que la pantalla muestra."""
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import caja_conductor as _cc
    from app.services import cond_pago as _cp

    universo = list(rutas_rango) + [r for r in rutas_atrasadas
                                    if r.id not in {x.id for x in rutas_rango}]
    por_medio = {m: 0.0 for m in MEDIOS}
    credito, credito_sin_valor = 0.0, 0
    pendientes = liquidadas = 0
    for ruta in universo:
        if (ruta.estado_financiero or 'PENDIENTE') == 'LIQUIDADA':
            liquidadas += 1
        elif ruta.estado == 'ENTREGADA':
            pendientes += 1
        for r in ruta.recaudos:
            medio = _cc.medio_de_recaudo(r)
            if medio is not None:
                por_medio[medio] += float(r.monto_cobrado or 0)
            if r.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL) and \
                    _cp.trato_de_cobro(r, r.tarea) == _cp.TRATO_CREDITO:
                vf = getattr(r.tarea, 'valor_factura', None) if r.tarea else None
                if vf is None:
                    credito_sin_valor += 1
                else:
                    credito += float(vf) - float(r.monto_cobrado or 0)
    total = round(sum(por_medio.values()), 2)
    atrasadas = sum(1 for r in universo if r.id in {x.id for x in rutas_atrasadas})
    return {
        'universo': (f'{len(universo)} ruta{"s" if len(universo) != 1 else ""} de la lista '
                     f'(las del rango{f" y {atrasadas} atrasada" + ("s" if atrasadas != 1 else "") if atrasadas else ""})'),
        'total_rutas': len(universo),
        'pendientes': pendientes,
        'liquidadas': liquidadas,
        'total_cobrado': total,
        # Alias de transición: la pantalla vieja lee `total_recaudado`.
        'total_recaudado': total,
        'total_efectivo': round(por_medio['EFECTIVO'], 2),
        'total_bancario': round(por_medio['BANCARIO'], 2),
        # Alias de transición del nombre viejo.
        'total_transferencia': round(por_medio['BANCARIO'], 2),
        'total_tarjeta': round(por_medio['TARJETA'], 2),
        'total_cheque': round(por_medio['CHEQUE'], 2),
        'total_otros': round(por_medio['OTRO'], 2),
        'cuadra': abs(total - sum(round(v, 2) for v in por_medio.values())) < 0.01,
        'total_credito': round(credito, 2),
        'credito_sin_valor': credito_sin_valor,
    }


def caja_de_ruta(ruta) -> dict:
    """La caja de la ruta: `RECIBIDA` (con su acta y la diferencia que le toca),
    `FALTA` (hay efectivo o gastos que nadie contó) o `NO_APLICA`."""
    from app.services import caja_conductor as _cc
    v = _cc.verificado_de_ruta(ruta)
    if v is not None:
        return {'estado': 'RECIBIDA', 'acta_id': v['acta_id'], 'acta_estado': v['estado'],
                'diferencia': v['diferencia'], 'prorrateado': v['prorrateado']}
    if (ruta.estado_financiero or 'PENDIENTE') == 'LIQUIDADA':
        return {'estado': 'NO_APLICA'}
    if _cc.exige_acta(ruta):
        return {'estado': 'FALTA', 'efectivo': _cc.efectivo_de_ruta(ruta)}
    return {'estado': 'NO_APLICA'}


def recibos_de_ruta(recaudos) -> dict:
    """El recibo de caja de las paradas, en tres estados que la pantalla
    distingue: en la cola, enviado (el POST salió, sin confirmar) y
    confirmado en Siesa (`rc_llegaron`, señal positiva)."""
    from app.models.siesa_job import SiesaJob
    from app.services.politica_cobro import rc_llegaron
    recaudos = list(recaudos)
    ids = [r.id for r in recaudos]
    confirmados = rc_llegaron(recaudos)
    en_cola_ids = set()
    if ids:
        en_cola_ids = {j.referencia_id for j in SiesaJob.query.filter(
            SiesaJob.tipo == 'RECIBO_CAJA', SiesaJob.referencia_tipo == 'RecaudoEntrega',
            SiesaJob.referencia_id.in_(ids),
            SiesaJob.estado.in_(('PENDIENTE', 'PROCESANDO'))).all()}
    enviados = {r.id for r in recaudos if r.siesa_rc_triggered and r.id not in confirmados}
    return {'en_cola': len(en_cola_ids - enviados - confirmados),
            'enviados': len(enviados), 'confirmados': len(confirmados)}

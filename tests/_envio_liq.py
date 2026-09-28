"""Lo que el ejecutor de la DLQ resuelve justo antes del POST de un recibo de
caja o una retención (`envio_liquidacion.resolver_envio`), para los tests.

Desde el 2026-09-27 encolar no le pregunta nada a Siesa (P1-1): el job nace con
la parada y lo que el WMS ya sabe; el monto de «Enviar todo a Siesa», el
tercero, la cuenta, la UN y el CO los resuelve el ejecutor. Los tests que
miraban el payload recién encolado corren acá **la misma función que corre el
ejecutor** (no una copia) y miran el payload resuelto.
"""
import json


def fila_cartera(tipo='FE', consec='1416', co='003', nit='900123456', cuenta='13050501',
                 un='99', sucursal='001', total_db=1000000, total_cr=0):
    """Una fila de `API_v2_CxC_General` con la forma real (campos medidos en
    producción el 2026-09-27)."""
    return {'f353_id_co_cruce': co, 'f353_id_tipo_docto_cruce': tipo,
            'f353_consec_docto_cruce': int(consec) if str(consec).isdigit() else consec,
            'f353_nro_cuota_cruce': 0, 'f200_id': nit, 'f201_id_sucursal': sucursal,
            'f253_id': cuenta, 'f353_id_un_cruce': un,
            'f353_total_db': total_db, 'f353_total_cr': total_cr}


def resolver_cola(recaudo_id, gateway=None, tipos=('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET')):
    """Resuelve (sin POST) los envíos PENDIENTE de la parada con
    `resolver_envio` y deja lo resuelto en su payload."""
    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.envio_liquidacion import resolver_envio
    rec = db.session.get(RecaudoEntrega, recaudo_id)
    jobs = (SiesaJob.query.filter_by(referencia_tipo='RecaudoEntrega', referencia_id=recaudo_id,
                                     estado='PENDIENTE')
            .filter(SiesaJob.tipo.in_(tipos)).all())
    for j in jobs:
        p, _ = resolver_envio(j, rec, gateway)
        j.payload = json.dumps(p, ensure_ascii=False)
    db.session.commit()
    return jobs


def cartera_en(mc, *lecturas, co='003'):
    """Configura un doble del gateway (`MagicMock`) para la lectura exacta de
    cartera (`get_cxc_de_factura(co, tipo, consec)`): cada argumento es una
    «lectura» de la cartera —una lista de filas, o una excepción— y se pasa a
    la siguiente cada vez que una consulta encuentra su documento (o falla).
    La última se repite. Devuelve `mc`."""
    mc.centro_op = co
    mc.modo_simulacion = False
    pos = [0]

    def _leer(co_, tipo, consec):
        lect = lecturas[min(pos[0], len(lecturas) - 1)]
        if isinstance(lect, BaseException):
            pos[0] += 1
            raise lect
        filas = [f for f in (lect or [])
                 if str(f.get('f353_id_co_cruce')) == str(co_)
                 and f.get('f353_id_tipo_docto_cruce') == tipo
                 and str(f.get('f353_consec_docto_cruce')) == str(consec)]
        if filas:
            pos[0] += 1
        return filas
    mc.get_cxc_de_factura.side_effect = _leer
    return mc


def liquidar_y_resolver(ruta_id, *args, **kwargs):
    """«Enviar todo a Siesa» (`liquidar_ruta_siesa`) y, con los mismos dobles
    activos, lo que el ejecutor resuelve antes del POST de cada parada. Los
    escenarios de punta a punta miran el monto del recibo: desde el
    2026-09-27 lo calcula el ejecutor (P1-1)."""
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services.liquidacion_service import LiquidacionService
    res = LiquidacionService.liquidar_ruta_siesa(ruta_id, *args, **kwargs)
    for r in RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all():
        resolver_cola(r.id)
    return res


def cartera_cualquiera(mc, co='003', **fila):
    """Un doble del gateway cuya cartera tiene una fila para cualquier
    documento que se le pida (con la forma real)."""
    mc.centro_op = co
    mc.modo_simulacion = False
    mc.get_cxc_de_factura.side_effect = lambda co_, t, c: [
        fila_cartera(tipo=t, consec=c, co=co_, **fila)]
    return mc


def rc_sin_verificar(recaudo, *, saldo_antes=10000.0, monto=10000.0, minutos=30,
                     co='003', tipo_fe='FEW', consec_fe='1'):
    """El último envío del recibo como lo deja la verificación diferida al
    rendirse: FALLIDO, con el saldo de antes del POST y el momento del intento
    (hace `minutos`). La bandera del recaudo queda puesta."""
    from datetime import datetime, timedelta
    from app.extensions import db
    from app.models.siesa_job import SiesaJob
    job = SiesaJob.encolar('RECIBO_CAJA', {
        'recaudo_id': recaudo.id, 'monto': monto, 'saldo_antes_rc': saldo_antes,
        'intento_rc_en': (datetime.utcnow() - timedelta(minutes=minutos)).isoformat(),
        'co_factura': co, 'tipo_docto_fe': tipo_fe, 'consec_fe': consec_fe,
        'tercero_nit': '900123456', 'fecha_documento_rc': '20260927'},
        referencia_tipo='RecaudoEntrega', referencia_id=recaudo.id)
    job.estado = 'FALLIDO'
    recaudo.siesa_rc_triggered = True
    db.session.commit()
    return job

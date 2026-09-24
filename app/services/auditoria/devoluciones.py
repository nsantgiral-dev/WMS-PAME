"""
Invariantes de devolución de cliente: apertura → confirmación física → nota
crédito (251126) → motivo DIAN (251546) → aprobación manual.

## Lo que este flujo tiene de propio

Es el único donde **mercancía y dinero vuelven juntos**, y por caminos
distintos: el inventario lo reingresa el WMS al confirmar la recepción física;
la cartera la cierra la nota crédito en Siesa. Si uno de los dos ocurre y el
otro no, el desacuerdo no se ve desde ninguno de los dos lados.

Y hay un paso que **termina en manos de una persona**: aprobar la NC en el
escritorio de Siesa (Regla 21). Eso no es un pendiente que el sistema vaya a
resolver — es el estado final del flujo automatizado. Lo que sí puede hacer el
sistema es no perder de vista las que llevan mucho esperando.

## El tope que no se puede pasar

    devuelta ≤ facturada

Se puede devolver menos de lo facturado —es el caso que el prorrateo del 251126
existe para cubrir— pero nunca más. Una devolución por encima de lo facturado
genera una nota crédito mayor que la factura: cartera en negativo y un saldo a
favor que nadie otorgó.
"""
from app.services.auditoria.base import _AUDITORIA_TRUNCADA,  AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _devoluciones(estados=None, limite=1000):
    """Las filas a auditar, **las más recientes primero**.

    `order_by(id.desc())` no es cosmética. El `.limit()` corta ANTES de que los
    invariantes filtren —el predicado corre en Python sobre lo que ya volvió—,
    así que sin orden el tope se llena con las filas más viejas y **en cuanto un
    flujo pasa el límite la auditoría deja de ver las violaciones nuevas**. El
    panel diría «0 hallazgos» porque dejó de mirar, no porque esté limpio.

    Y cuando el tope se alcanza se **declara**: `truncado` cuenta HALLAZGOS y no
    tiene nada que ver con esto. Son dos truncamientos distintos y uno estaba
    invisible.
    """
    from app.models.devolucion_cliente import DevolucionCliente
    q = DevolucionCliente.query
    if estados:
        q = q.filter(DevolucionCliente.estado.in_(estados))
    filas = q.order_by(DevolucionCliente.id.desc()).limit(limite).all()
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:{limite}')
    return filas


@invariante(
    codigo='DEV-01',
    flujo='devoluciones',
    frontera='factura → devolución',
    consecuencia='La nota crédito sale por más de lo facturado: cartera en '
                 'negativo y un saldo a favor que nadie otorgó.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_ve_que_se_devuelve_mas_de_lo_facturado',
)
def no_se_devuelve_mas_de_lo_facturado(ctx=None):
    """Devolver menos es el caso normal —para eso existe el prorrateo del
    251126—; devolver más no es un caso, es un error de captura."""
    from app.models.devolucion_cliente import LineaDevolucionCliente
    return [
        Hallazgo(
            referencia=f'devolucion#{ln.devolucion_id} / {ln.codigo_siesa}',
            fecha=ln.fecha_creacion,
            detalle=f'devuelta {ln.cantidad_devuelta} > facturada '
                    f'{ln.cantidad_facturada}',
            datos={'producto_id': ln.producto_id},
        )
        for ln in LineaDevolucionCliente.query.order_by(LineaDevolucionCliente.id.desc()).limit(5000).all()
        if float(ln.cantidad_devuelta or 0) > float(ln.cantidad_facturada or 0)
    ]


@invariante(
    codigo='DEV-02',
    flujo='devoluciones',
    frontera='devolución → Siesa',
    consecuencia='El inventario volvió a la bodega y la cartera del cliente '
                 'sigue con el cargo. Tiene la plata cobrada y la mercancía de '
                 'vuelta.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_ve_una_confirmada_sin_nota_credito',
)
def una_confirmada_tiene_su_nota_credito(ctx=None):
    """Confirmar la devolución **reingresa el inventario** en el WMS. La nota
    crédito es lo que cierra el otro lado. Sin ella, el desacuerdo entre las
    dos bases no se ve desde ninguna de las dos."""
    return [
        Hallazgo(
            referencia=d.codigo or f'devolucion#{d.id}',
            fecha=d.fecha_creacion,
            detalle=f'CONFIRMADA sin nota crédito disparada · cliente '
                    f'{d.cliente or "?"} · FE {d.tipo_docto_fe}-{d.consec_fe}',
            datos={'total': d.es_total},
        )
        for d in _devoluciones(('CONFIRMADA',))
        if not d.siesa_nc_triggered
    ]


@invariante(
    codigo='DEV-03',
    flujo='devoluciones',
    frontera='devolución → Siesa',
    consecuencia='Se creó una nota crédito por una devolución que después se '
                 'canceló: la cartera se abonó y la mercancía no volvió.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_ve_una_cancelada_que_dejo_nota_credito',
)
def una_cancelada_no_dejo_nota_credito(ctx=None):
    return [
        Hallazgo(
            referencia=d.codigo or f'devolucion#{d.id}',
            fecha=d.fecha_creacion,
            detalle=f'CANCELADA pero la NC ya salió'
                    + (f' (NCE-{d.siesa_nc_consec})' if d.siesa_nc_consec else ''),
            datos={'cliente': d.cliente},
        )
        for d in _devoluciones(('CANCELADA',))
        if d.siesa_nc_triggered
    ]


@invariante(
    codigo='DEV-04',
    flujo='devoluciones',
    frontera='NC → aprobación en Siesa',
    consecuencia='Notas crédito esperando la aprobación en Siesa. Hasta que '
                 'alguien las apruebe, la cartera del cliente sigue con el '
                 'cargo y lo devuelto sigue fuera de la venta.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_cuenta_las_nc_esperando_aprobacion',
)
def se_pueden_contar_las_nc_sin_aprobar(ctx=None):
    """Aprobar es manual en Siesa (Regla 21); desde m045devol el cron
    `devolucion_nc_verificador` lo LEE y marca. Esto cuenta lo que falta.

    Hasta el 2026-09-24 listaba toda CONFIRMADA con NC enviada **aunque ya
    estuviera aprobada**: el conteo solo crecía, y una de ayer se veía igual
    que una de hace un mes. Ahora solo las sin aprobar, con su antigüedad, las
    más viejas primero (el aviso de 3 días es DEV-12).
    """
    from datetime import datetime
    ahora = datetime.utcnow()
    filas = [d for d in _devoluciones(('CONFIRMADA',))
             if d.siesa_nc_triggered and not d.nc_aprobada_siesa]
    filas.sort(key=lambda d: d.siesa_nc_triggered_at or datetime.min)
    return [
        Hallazgo(
            referencia=d.codigo or f'devolucion#{d.id}',
            detalle=(f'NCE-{d.siesa_nc_consec} en Elaboración'
                     if d.siesa_nc_consec
                     else 'NC creada pero SIN consecutivo conocido — hay que '
                          'buscarla a mano en Auditoría')
                    + (f' hace {(ahora - d.siesa_nc_triggered_at).days} día(s)'
                       if d.siesa_nc_triggered_at else ''),
            datos={'cliente': d.cliente, 'motivo_dian': d.siesa_motivo_dian,
                   'dias': ((ahora - d.siesa_nc_triggered_at).total_seconds() / 86400
                            if d.siesa_nc_triggered_at else None)},
        )
        for d in filas
    ]


@invariante(
    codigo='DEV-05',
    flujo='devoluciones',
    frontera='NC → motivo DIAN',
    consecuencia='La NC no tiene concepto DIAN, y sin él el botón Aprobar de '
                 'Siesa ni siquiera se habilita: contabilidad lo pone a mano.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_cuenta_las_nc_esperando_aprobacion',
)
def la_nc_lleva_su_motivo_dian(ctx=None):
    """El motivo se automatizó (251546) pero está apagado hasta registrar la
    consulta dinámica. Contar las que salieron sin él dice cuánto trabajo
    manual está generando ese pendiente."""
    return [
        Hallazgo(
            referencia=d.codigo or f'devolucion#{d.id}',
            detalle=f'NCE-{d.siesa_nc_consec or "?"} sin concepto DIAN',
            datos={'cliente': d.cliente},
        )
        for d in _devoluciones(('CONFIRMADA',))
        if d.siesa_nc_triggered and d.siesa_nc_consec and not d.siesa_motivo_dian
    ]


# ── m045devol: la devolución de ruta nace con el rechazo ────────────────────
#
# «Si el cliente no paga, la mercancía vuelve y entra al inventario, sin cabos
# sueltos». Cada invariante de abajo es un cabo suelto con nombre. Sus
# detectores ciegos viven en `tests/test_devolucion_vuelve.py`.

_DET = 'tests/test_devolucion_vuelve.py::TestDetectoresDeDevolucionDeRuta::'


def _recaudos_que_devuelven(limite=2000):
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    filas = (RecaudoEntrega.query
             .filter(RecaudoEntrega.estado_entrega.in_(
                 (EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL)))
             .order_by(RecaudoEntrega.id.desc()).limit(limite).all())
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:recaudos:{limite}')
    return filas


@invariante(
    codigo='DEV-06',
    flujo='devoluciones',
    frontera='parada rechazada → devolución',
    consecuencia='La ruta se liquidó y la mercancía que el conductor dijo que '
                 'volvía no está en ninguna cola: ni en recepción ni en el '
                 'inventario. Nadie la va a buscar.',
    severidad=BLOQUEA,
    detector_ciego=_DET + 'test_ve_una_ruta_liquidada_con_un_rechazo_sin_devolucion',
)
def todo_rechazo_de_ruta_liquidada_tiene_devolucion(ctx=None):
    """Una parada RECHAZADA o PARCIAL de una ruta LIQUIDADA sin ninguna
    devolución (ni siquiera cancelada). La vía sana la crea al confirmar la
    parada, o la liquidación la asegura; una que no pase por
    `devolucion_ruta` deja exactamente esto."""
    from app.models.devolucion_cliente import DevolucionCliente
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    out = []
    for r in _recaudos_que_devuelven():
        ruta = RutaDespacho.query.get(r.ruta_id)
        if ruta is None or ruta.estado_financiero != EstadoFinancieroRuta.LIQUIDADA:
            continue
        if DevolucionCliente.query.filter_by(recaudo_entrega_id=r.id).first() is None:
            out.append(Hallazgo(
                referencia=f'ruta#{r.ruta_id}/tarea#{r.tarea_id}',
                detalle=f'parada {r.estado_entrega} de una ruta LIQUIDADA sin devolución',
                datos={'recaudo_id': r.id, 'motivo': r.motivo_rechazo}))
    return out


@invariante(
    codigo='DEV-07',
    flujo='devoluciones',
    frontera='camión → conteo en bodega',
    consecuencia='Mercancía declarada de vuelta hace más de un día y nadie la '
                 'contó: ni vendible ni con su nota crédito.',
    severidad=AVISA,
    detector_ciego=_DET + 'test_ve_una_devolucion_sin_contar_hace_mas_de_un_dia',
)
def ninguna_devolucion_de_ruta_espera_mas_de_un_dia_sin_contar(ctx=None):
    from datetime import datetime
    from app.services import devolucion_ruta as _dr
    ahora = datetime.utcnow()
    out = []
    for d in _devoluciones(('EN_CAMION', 'ABIERTA')):
        if not d.recaudo_entrega_id:
            continue
        t0 = _dr._momento_del_rechazo(d)
        if t0 is None:
            continue
        horas = (ahora - t0).total_seconds() / 3600
        if horas >= _dr.HORAS_SIN_CONTAR_AVISO:
            out.append(Hallazgo(
                referencia=d.codigo, detalle=f'{d.estado} hace {horas:.0f} h sin contar',
                datos={'pedido': d.numero_pedido_siesa, 'horas': round(horas, 1)}))
    return out


@invariante(
    codigo='DEV-08',
    flujo='devoluciones',
    frontera='parada rechazada → devolución',
    consecuencia='El conductor dijo que la mercancía volvía y su devolución se '
                 'canceló: lo que haya pasado con esas unidades no está medido '
                 '(ni contado, ni faltante).',
    severidad=AVISA,
    detector_ciego=_DET + 'test_ve_un_rechazo_con_la_devolucion_cancelada',
)
def un_rechazo_no_queda_con_la_devolucion_cancelada(ctx=None):
    """Cancelar es de supervisión y con motivo (bitácora); la regla nueva dice
    que si no volvió nada se cuenta en cero (FALTANTE_TOTAL). Una parada que
    sigue RECHAZADA/PARCIAL con su única devolución cancelada es un faltante
    que se sacó de la medición."""
    from app.services import devolucion_ruta as _dr
    out = []
    for r in _recaudos_que_devuelven():
        if _dr.devolucion_vigente(r.id) is None and _dr.tuvo_devolucion_cancelada(r.id):
            out.append(Hallazgo(
                referencia=f'ruta#{r.ruta_id}/tarea#{r.tarea_id}',
                detalle=f'parada {r.estado_entrega} con su devolución CANCELADA',
                datos={'recaudo_id': r.id}))
    return out


@invariante(
    codigo='DEV-09',
    flujo='devoluciones',
    frontera='devolución → recibo de caja',
    consecuencia='El recibo de caja espera una nota crédito que ya no va a '
                 'salir: la plata que cobró el conductor no entra a Siesa.',
    severidad=BLOQUEA,
    detector_ciego=_DET + 'test_ve_un_rc_esperando_una_nc_que_no_llegara',
)
def ningun_rc_espera_una_nc_que_no_llegara(ctx=None):
    """La devolución terminó sin NC (contada en cero o cancelada) hace más de
    una hora y el RC con `depende_de_nc` sigue en la cola. Desde m045devol el
    ejecutor lo deja salir (`nc_no_llegara`); si esto aparece, ese camino se
    rompió."""
    import json
    from datetime import datetime, timedelta
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    from app.services import devolucion_ruta as _dr
    from app.models.devolucion_cliente import DevolucionCliente
    hace_una_hora = datetime.utcnow() - timedelta(hours=1)
    out = []
    for job in SiesaJob.query.filter(SiesaJob.tipo == 'RECIBO_CAJA',
                                     SiesaJob.estado.in_(EstadoSiesaJob.ACTIVOS)).all():
        try:
            payload = json.loads(job.payload or '{}')
        except ValueError:
            continue
        if not payload.get('depende_de_nc'):
            continue
        rec = RecaudoEntrega.query.get(payload.get('recaudo_id') or job.referencia_id)
        if rec is None or not _dr.nc_no_llegara(rec):
            continue
        ultima = (DevolucionCliente.query.filter_by(recaudo_entrega_id=rec.id)
                  .order_by(DevolucionCliente.id.desc()).first())
        cuando = ((ultima.cancelada_at or ultima.fecha_confirmacion) if ultima else None)
        if cuando is not None and cuando > hace_una_hora:
            continue
        out.append(Hallazgo(
            referencia=f'job#{job.id}/recaudo#{rec.id}',
            detalle='RC esperando una NC que no va a salir (devolución '
                    f'{ultima.estado if ultima else "?"})',
            datos={'devolucion': ultima.codigo if ultima else None}))
    return out


@invariante(
    codigo='DEV-10',
    flujo='devoluciones',
    frontera='factura → devolución',
    consecuencia='Dos devoluciones sobre la misma línea de factura: se '
                 'reingresa dos veces y la nota crédito cruza de más contra '
                 'la cartera.',
    severidad=BLOQUEA,
    detector_ciego=_DET + 'test_ve_dos_devoluciones_sobre_la_misma_linea_de_factura',
)
def una_linea_de_factura_no_se_devuelve_de_mas(ctx=None):
    """Por línea de factura (`f470_rowid`): dos devoluciones ACTIVAS a la vez,
    o la suma de lo devuelto (contadas + activas) por encima de lo facturado.
    La de mostrador y la de ruta sobre la misma factura se suman."""
    from app.models.devolucion_cliente import DevolucionCliente, LineaDevolucionCliente
    from app.extensions import db
    filas = (db.session.query(LineaDevolucionCliente, DevolucionCliente)
             .join(DevolucionCliente, DevolucionCliente.id == LineaDevolucionCliente.devolucion_id)
             .filter(DevolucionCliente.estado.in_(('EN_CAMION', 'ABIERTA', 'CONFIRMADA')),
                     LineaDevolucionCliente.f470_rowid.isnot(None))
             .order_by(LineaDevolucionCliente.id.desc()).limit(20000).all())
    grupos = {}
    for ln, d in filas:
        g = grupos.setdefault(str(ln.f470_rowid), {'activas': set(), 'suma': 0.0,
                                                   'facturada': 0.0, 'codigos': set()})
        if d.estado in ('EN_CAMION', 'ABIERTA'):
            g['activas'].add(d.codigo)
        g['suma'] += float(ln.cantidad_devuelta or 0)
        g['facturada'] = max(g['facturada'], float(ln.cantidad_facturada or 0))
        g['codigos'].add(d.codigo)
    out = []
    for rowid, g in grupos.items():
        if len(g['activas']) > 1 or g['suma'] > g['facturada'] + 1e-9:
            out.append(Hallazgo(
                referencia=f'rowid {rowid}',
                detalle=(f'{len(g["activas"])} devoluciones activas' if len(g['activas']) > 1
                         else f'devuelto {g["suma"]:g} > facturado {g["facturada"]:g}'),
                datos={'devoluciones': sorted(g['codigos'])}))
    return out


@invariante(
    codigo='DEV-11',
    flujo='devoluciones',
    frontera='devolución → inventario vendible',
    consecuencia='Mercancía devuelta quedó a la venta antes de que la nota '
                 'crédito se aprobara en Siesa: se vende lo que el ERP sigue '
                 'atribuyendo al cliente.',
    severidad=BLOQUEA,
    detector_ciego=_DET + 'test_ve_un_reingreso_vendible_sin_nc_aprobada',
)
def ningun_reingreso_es_vendible_sin_nc_aprobada(ctx=None):
    """Solo las líneas contadas con la política de m045devol
    (`cantidad_averiada` escrita — la marca estructural): lo de antes entraba
    directo a picking por diseño y reportarlo sería ruido histórico que quema
    el canal. Viola: lo sano entró a una ubicación vendible, o se liberó a
    picking con la NC sin aprobar."""
    from app.models.devolucion_cliente import DevolucionCliente, LineaDevolucionCliente
    from app.models.ubicacion import Ubicacion
    from app.services.picking_service import es_ubicacion_vendible
    from app.extensions import db
    filas = (db.session.query(LineaDevolucionCliente, DevolucionCliente)
             .join(DevolucionCliente, DevolucionCliente.id == LineaDevolucionCliente.devolucion_id)
             .filter(DevolucionCliente.estado == 'CONFIRMADA',
                     LineaDevolucionCliente.cantidad_averiada.isnot(None))
             .order_by(LineaDevolucionCliente.id.desc()).limit(5000).all())
    out = []
    for ln, d in filas:
        if ln.sanas() <= 0:
            continue
        ub = db.session.get(Ubicacion, ln.ubicacion_id) if ln.ubicacion_id else None
        if ub is not None and es_ubicacion_vendible(ub):
            out.append(Hallazgo(referencia=d.codigo,
                                detalle=f'{ln.codigo_siesa}: lo devuelto entró a {ub.codigo} '
                                        f'(vendible) sin pasar por la zona de devoluciones',
                                datos={'linea_id': ln.id}))
        elif ln.ubicacion_liberada_id and not d.nc_aprobada_siesa:
            out.append(Hallazgo(referencia=d.codigo,
                                detalle=f'{ln.codigo_siesa}: liberado a picking con la NC sin '
                                        f'aprobar',
                                datos={'linea_id': ln.id}))
    return out


@invariante(
    codigo='DEV-12',
    flujo='devoluciones',
    frontera='NC → aprobación en Siesa',
    consecuencia='La nota crédito lleva más de tres días sin aprobar (o Siesa '
                 'la anuló): la cartera no se cruzó y lo devuelto sigue sin '
                 'poder venderse.',
    severidad=AVISA,
    detector_ciego=_DET + 'test_ve_una_nc_sin_aprobar_hace_mas_de_tres_dias',
)
def ninguna_nc_espera_aprobacion_mas_de_tres_dias(ctx=None):
    from app.services import devolucion_ruta as _dr
    a = _dr.avisos()
    return ([Hallazgo(referencia=f['codigo'],
                      detalle=f'NC {f["nc"] or "sin consecutivo"} sin aprobar hace '
                              f'{f["dias"]} días', datos={'cliente': f['cliente']})
             for f in a['nc_sin_aprobar_3d']]
            + [Hallazgo(referencia=f['codigo'],
                        detalle=f'NC {f["nc"]} ANULADA en Siesa (f350_ind_estado = 2)',
                        datos={'cliente': f['cliente']})
               for f in a['nc_anuladas']])


@invariante(
    codigo='DEV-13',
    flujo='devoluciones',
    frontera='camión → recepción',
    consecuencia='Recepción cerró la llegada del camión y hay bultos que el '
                 'conductor dijo que volvían sin recibir ni dar por faltantes: '
                 'el cuadre de la ruta no puede ser exacto.',
    severidad=AVISA,
    detector_ciego=_DET + 'test_ve_bultos_declarados_de_vuelta_tras_cerrar_la_llegada',
)
def la_llegada_cerrada_no_deja_bultos_en_el_camion(ctx=None):
    """Pasa si una parada se confirma RECHAZADA después de que recepción cerró
    la llegada (la cola offline del conductor sincroniza tarde)."""
    from app.models.bulto import Bulto, EstadoBulto
    from app.models.ruta_despacho import RutaDespacho
    out = []
    for ruta in (RutaDespacho.query.filter(RutaDespacho.llegada_cerrada_at.isnot(None))
                 .order_by(RutaDespacho.id.desc()).limit(500).all()):
        n = Bulto.query.filter_by(ruta_despacho_id=ruta.id,
                                  estado=EstadoBulto.RECHAZADO).count()
        if n:
            out.append(Hallazgo(referencia=f'ruta#{ruta.id}',
                                detalle=f'{n} bulto(s) declarados de vuelta sin recibir '
                                        f'tras cerrar la llegada',
                                datos={'bultos': n}))
    return out

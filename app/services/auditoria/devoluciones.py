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
    frontera='NC → aprobación manual',
    consecuencia='Notas crédito esperando la aprobación manual en Siesa. Hasta '
                 'que alguien las apruebe, la cartera del cliente sigue con el '
                 'cargo.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorDevoluciones::test_cuenta_las_nc_esperando_aprobacion',
)
def se_pueden_contar_las_nc_sin_aprobar(ctx=None):
    """Aprobar es manual y **es el estado final del flujo automatizado**, no un
    fallo (Regla 21). Pero una NC que lleva semanas esperando se ve igual que
    una de ayer si nadie las cuenta.

    Se listan las que tienen consecutivo conocido: sin él, contabilidad ni
    siquiera sabe qué documento buscar en Auditoría.
    """
    return [
        Hallazgo(
            referencia=d.codigo or f'devolucion#{d.id}',
            detalle=(f'NCE-{d.siesa_nc_consec} en Elaboración'
                     if d.siesa_nc_consec
                     else 'NC creada pero SIN consecutivo conocido — hay que '
                          'buscarla a mano en Auditoría'),
            datos={'cliente': d.cliente, 'motivo_dian': d.siesa_motivo_dian},
        )
        for d in _devoluciones(('CONFIRMADA',))
        if d.siesa_nc_triggered
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

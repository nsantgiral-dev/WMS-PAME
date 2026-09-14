"""
Invariantes de reposición: RESERVA → PICKING.

## Por qué este flujo se equivoca distinto

No entra ni sale mercancía del almacén: **se mueve de una ubicación a otra**.
El total no cambia, así que ningún cuadre global lo detecta — el inventario
sigue sumando lo mismo mientras las dos ubicaciones dicen mentiras opuestas.

Y el daño no se ve el día que ocurre: se ve cuando un picker va a la ubicación
de PICKING, no encuentra lo que el sistema dice que hay, y **reporta un
faltante que en realidad está en RESERVA**. Ahí ya nadie relaciona las dos
cosas.

## Lo que YA NO aplica (2026-09-07)

Hasta acá el módulo tenía dos invariantes más (REP-01/REP-02) sobre el envío
a Siesa del conector 173066 — retirado: RESERVA y PICKING son la misma
bodega Siesa (NB1 no tiene sub-bodegas internas, es organización 100% del
WMS), así que no hay ningún documento real que declarar, y 173066 además
resultó no idempotente y, probado en vivo, ni pasaba la validación de tamaño
de registro de Siesa. `confirmar_reposicion()` ya no encola nada — los
invariantes que vigilaban ese envío se volvieron sobre un riesgo que dejó de
existir, así que se borraron con él en vez de quedar en verde permanente
sobre código que ya no corre.
"""
from app.services.auditoria.base import _AUDITORIA_TRUNCADA, AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _tareas(estados=None, limite=2000):
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
    from app.models.tarea_reposicion import TareaReposicion
    q = TareaReposicion.query
    if estados:
        q = q.filter(TareaReposicion.estado.in_(estados))
    filas = q.order_by(TareaReposicion.id.desc()).limit(limite).all()
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:{limite}')
    return filas


@invariante(
    codigo='REP-03',
    flujo='reposicion',
    frontera='tarea → movimiento',
    consecuencia='Se movieron más unidades de las que la tarea pedía: se vació '
                 'la ubicación de RESERVA más allá de lo planeado.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestDetectorReposicion::test_ve_que_se_movio_mas_de_lo_pedido',
)
def no_se_mueve_mas_de_lo_pedido(ctx=None):
    """Mover menos es normal —la ubicación de origen puede no tener todo—;
    mover más no es un caso, es un error de captura."""
    return [
        Hallazgo(
            referencia=t.codigo or f'reposicion#{t.id}',
            detalle=f'movidas {t.unidades_movidas} sobre {t.cantidad_unidades} '
                    f'solicitadas',
            datos={'producto_id': t.producto_id},
        )
        for t in _tareas()
        if t.unidades_movidas is not None
        and t.unidades_movidas > (t.cantidad_unidades or 0)
    ]


@invariante(
    codigo='REP-04',
    flujo='reposicion',
    frontera='tarea → ubicaciones',
    consecuencia='Origen y destino son la misma ubicación: el movimiento no '
                 'repone nada, solo genera ruido en el historial.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestDetectorReposicion::test_ve_origen_igual_a_destino',
)
def el_origen_y_el_destino_son_distintos(ctx=None):
    return [
        Hallazgo(
            referencia=t.codigo or f'reposicion#{t.id}',
            detalle=f'reserva y picking apuntan a la ubicación '
                    f'{t.ubicacion_reserva_id}',
            datos={'estado': t.estado},
        )
        for t in _tareas()
        if t.ubicacion_reserva_id == t.ubicacion_picking_id
    ]


@invariante(
    codigo='REP-05',
    flujo='reposicion',
    frontera='asignación → confirmación',
    consecuencia='Reposiciones tomadas y nunca cerradas: la ubicación de '
                 'PICKING sigue esperando stock que alguien dio por movido.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestDetectorReposicion::test_cuenta_las_que_estan_en_curso',
)
def se_pueden_contar_las_reposiciones_en_curso(ctx=None):
    from app.utils.fecha import ahora_bogota
    hoy = ahora_bogota().date()
    out = []
    for t in _tareas(('EN_PROCESO',)):
        ref = t.fecha_inicio.date() if t.fecha_inicio else None
        dias = (hoy - ref).days if ref else None
        out.append(Hallazgo(
            referencia=t.codigo or f'reposicion#{t.id}',
            detalle=(f'EN_PROCESO hace {dias} día(s)' if dias is not None
                     else 'EN_PROCESO sin fecha de inicio'),
            datos={'abastecedor_id': t.abastecedor_id},
        ))
    return out

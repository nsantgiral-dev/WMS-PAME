"""
Invariantes de reposición: RESERVA → PICKING (conector 173066).

## Por qué este flujo se equivoca distinto

No entra ni sale mercancía del almacén: **se mueve de una ubicación a otra**.
El total no cambia, así que ningún cuadre global lo detecta — el inventario
sigue sumando lo mismo mientras las dos ubicaciones dicen mentiras opuestas.

Y el daño no se ve el día que ocurre: se ve cuando un picker va a la ubicación
de PICKING, no encuentra lo que el sistema dice que hay, y **reporta un
faltante que en realidad está en RESERVA**. Ahí ya nadie relaciona las dos
cosas.

## Lo que hace que sea especialmente fácil de romper

`transferir_entre_ubicaciones` (173066) **no es idempotente en Siesa**: si el
primer intento llegó y se reintenta, se crea un doble movimiento. El DLQ lo
sabe y aborta el reintento (`TRANSFERENCIA_UBICACIONES` es el único job que
hace eso). Ese diseño solo funciona si nadie más dispara la transferencia por
otra vía — y `/api/reposicion/<id>/confirmar` fue una de las rutas huérfanas
que apareció al cambiar el trinquete a adyacencia.
"""
from app.services.auditoria.base import AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _tareas(estados=None, limite=2000):
    from app.models.tarea_reposicion import TareaReposicion
    q = TareaReposicion.query
    if estados:
        q = q.filter(TareaReposicion.estado.in_(estados))
    return q.limit(limite).all()


@invariante(
    codigo='REP-01',
    flujo='reposicion',
    frontera='confirmación → Siesa',
    consecuencia='El WMS movió el stock entre ubicaciones y Siesa no. Las dos '
                 'bases suman igual y las dos ubicaciones dicen cosas '
                 'distintas — ningún cuadre global lo ve.',
    severidad=BLOQUEA,
)
def una_reposicion_completada_llego_a_siesa(ctx=None):
    """El total no cambia, así que este descuadre es invisible para cualquier
    verificación por sumas. Solo aparece cuando un picker va a PICKING y no
    encuentra lo que el sistema dice."""
    return [
        Hallazgo(
            referencia=t.codigo or f'reposicion#{t.id}',
            detalle=f'COMPLETADA sin envío a Siesa · {t.unidades_movidas} und '
                    f'de RESERVA a PICKING',
            datos={'producto_id': t.producto_id, 'almacen_id': t.almacen_id},
        )
        for t in _tareas(('COMPLETADA',))
        if not t.siesa_enviado
    ]


@invariante(
    codigo='REP-02',
    flujo='reposicion',
    frontera='confirmación → Siesa',
    consecuencia='Se mandó a Siesa un movimiento que el WMS no dio por hecho: '
                 'el ERP movió stock que acá sigue en RESERVA.',
    severidad=BLOQUEA,
)
def nada_se_envia_sin_completarse(ctx=None):
    """El espejo del anterior, y el que importa por la no-idempotencia: 173066
    **duplica el movimiento si se reintenta**, así que un envío sobre una tarea
    que no llegó a completarse es candidato a haberse disparado dos veces."""
    return [
        Hallazgo(
            referencia=t.codigo or f'reposicion#{t.id}',
            detalle=f'{t.estado} pero ya se envió a Siesa',
            datos={'unidades': t.unidades_movidas, 'job': t.siesa_job_id},
        )
        for t in _tareas()
        if t.siesa_enviado and t.estado not in ('COMPLETADA',)
    ]


@invariante(
    codigo='REP-03',
    flujo='reposicion',
    frontera='tarea → movimiento',
    consecuencia='Se movieron más unidades de las que la tarea pedía: se vació '
                 'la ubicación de RESERVA más allá de lo planeado.',
    severidad=BLOQUEA,
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
                 'repone nada y en Siesa queda un traslado a sí mismo.',
    severidad=AVISA,
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

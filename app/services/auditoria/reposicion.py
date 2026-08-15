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
from app.services.auditoria.base import _AUDITORIA_TRUNCADA,  AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


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
    codigo='REP-01',
    flujo='reposicion',
    frontera='confirmación → Siesa',
    consecuencia='El WMS movió el stock entre ubicaciones y Siesa no. Las dos '
                 'bases suman igual y las dos ubicaciones dicen cosas '
                 'distintas — ningún cuadre global lo ve.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestDetectorReposicion::test_ve_una_completada_que_no_llego_a_siesa',
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
    consecuencia='Una reposición con más de un envío a Siesa. **173066 NO es '
                 'idempotente**: el segundo movimiento vacía la ubicación de '
                 'RESERVA otra vez y el inventario del WMS no lo refleja.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestDetectorReposicion::test_ve_el_doble_envio',
)
def ninguna_reposicion_se_envia_dos_veces(ctx=None):
    """El riesgo que este flujo declara, y que nadie vigilaba.

    ## Qué medía antes, y por qué no podía fallar

    La versión anterior pedía `siesa_enviado AND estado != 'COMPLETADA'`. Pero
    `siesa_enviado = True` se escribe en **un solo sitio** —dentro del
    post-COMPLETADO del job (`siesa_job_service.py`)— y ese post solo existe si
    `confirmar_reposicion` ya puso `estado = COMPLETADA` en la misma
    transacción. Y `routes/reposicion.py` rechaza cualquier cambio de estado
    posterior.

    **No había estado alcanzable que lo disparara.** Un BLOQUEA en verde
    permanente sobre una propiedad que la vía feliz satisface por construcción.

    ## Qué mide ahora

    Lo que el docstring del módulo declara como el peligro real: el 173066
    **duplica el movimiento si se reintenta**, y el DLQ aborta el reintento por
    eso —es el único job que lo hace—. Ese diseño solo funciona si nadie más
    dispara la transferencia por otra vía, y
    `/api/reposicion/<id>/confirmar` fue una de las rutas huérfanas que apareció
    al cambiar el trinquete a adyacencia.

    Dos señales, las dos por la cola y no por la bandera:

      · más de un job terminado por tarea — el movimiento salió dos veces;
      · un job con reintentos Y error — el aborto del DLQ pudo llegar tarde.
    """
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob

    out = []
    for t in _tareas():
        jobs = SiesaJob.query.filter_by(
            tipo='TRANSFERENCIA_UBICACIONES',
            referencia_tipo='TareaReposicion', referencia_id=t.id,
        ).all()
        completados = [j for j in jobs if j.estado == EstadoSiesaJob.COMPLETADO]
        if len(completados) > 1:
            out.append(Hallazgo(
                referencia=t.codigo or f'reposicion#{t.id}',
                detalle=f'{len(completados)} envíos completados a Siesa para una '
                        f'sola reposición de {t.unidades_movidas} und — 173066 '
                        f'no es idempotente',
                datos={'jobs': [j.id for j in completados],
                       'producto_id': t.producto_id},
            ))
            continue
        # Un reintento sobre un POST que pudo haber llegado: la Regla 3 y el
        # aborto del DLQ existen para esto, y si dejó rastro hay que mirarlo.
        sospechosos = [j for j in jobs if (j.intentos or 0) > 1 and j.error_ultimo]
        if sospechosos:
            out.append(Hallazgo(
                referencia=t.codigo or f'reposicion#{t.id}',
                detalle=f'reintentado {sospechosos[0].intentos} vez(ces) tras un '
                        f'error — el 173066 pudo haber entrado igual',
                datos={'job': sospechosos[0].id,
                       'error': (sospechosos[0].error_ultimo or '')[:120]},
            ))
    return out


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
                 'repone nada y en Siesa queda un traslado a sí mismo.',
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

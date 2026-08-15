"""
Invariantes de recepción de compras: OC → recepción física → entrada en Siesa
(142948).

## Lo que se vigila acá

Es el flujo por donde **entra** el inventario, y su modo de fallo es el espejo
del de venta: en venta lo caro es que salga mercancía sin documento; acá lo
caro es que **entre mercancía que el ERP no registró** —el stock del WMS sube y
la cuenta 1435 no se mueve— o que se confirme una entrada sin que nadie haya
contado nada.

El exceso sobre lo ordenado no es un error por sí solo: los proveedores mandan
de más y hay una tolerancia declarada por línea. Lo que sí es un error es un
exceso **por encima de esa tolerancia**, porque significa que se recibió y se
va a pagar mercancía que nadie autorizó.
"""
from app.models.recepcion import EstadoRecepcion
from app.services.auditoria.base import _AUDITORIA_TRUNCADA,  AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _recepciones(estados=None, limite=1000):
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
    from app.models.recepcion import RecepcionMercancia as Recepcion
    q = Recepcion.query
    if estados:
        q = q.filter(Recepcion.estado.in_(estados))
    filas = q.order_by(Recepcion.id.desc()).limit(limite).all()
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:{limite}')
    return filas


@invariante(
    codigo='REC-01',
    flujo='recepcion',
    frontera='recepción → Siesa',
    consecuencia='El stock del WMS subió y el ERP no registró la entrada: la '
                 'mercancía existe en bodega y no en la cuenta 1435.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorRecepcion::test_ve_una_confirmada_sin_entrada_en_siesa',
)
def una_recepcion_confirmada_entro_a_siesa(ctx=None):
    """Dos formas de no haber entrado, y la segunda estaba invisible.

    ## `siesa_triggered` no significa «entró»

    El job de ENTRADA_OC tiene un bloque de emergencia que fuerza
    `siesa_triggered = True` **cuando la respuesta NO se pudo guardar**
    (`siesa_job_service.py`). Es correcto —sin eso el reintento del DLQ crea una
    entrada contable duplicada en la cuenta 1435— pero convierte la bandera en
    «se intentó y quizá entró», no en «entró».

    Un guard que la lee sola está en verde exactamente sobre el caso donde nadie
    sabe qué pasó. Es la misma forma que `siesa_rc_triggered`, que se enciende
    ANTES del POST por la Regla 6.

    La señal honesta es `siesa_response`: existe **solo** si hubo respuesta y se
    guardó, que es justo lo que el bloque de emergencia no logró. Comparar con
    traslados, que sí exige `siesa_salida_consec`.

    ## Y el estado fantasma

    El filtro incluía `'CERRADA'`, que **no existe** en `EstadoRecepcion`
    (ABIERTA · EN_PROCESO · CONFIRMADA · CANCELADA). Un filtro por un valor
    imposible no acota: decora.
    """
    out = []
    for r in _recepciones((EstadoRecepcion.CONFIRMADA,)):
        if not r.siesa_triggered:
            out.append(Hallazgo(
                referencia=r.codigo or f'recepcion#{r.id}',
                detalle=f'{r.estado} sin entrada disparada a Siesa · OC '
                        f'{r.numero_oc_siesa} · {r.proveedor_nombre or ""}',
                datos={'parcial': r.es_parcial, 'causa': 'nunca se disparó'},
            ))
        elif not (r.siesa_response or '').strip():
            out.append(Hallazgo(
                referencia=r.codigo or f'recepcion#{r.id}',
                detalle=f'{r.estado} marcada como enviada pero SIN respuesta de '
                        f'Siesa guardada · OC {r.numero_oc_siesa} — la bandera la '
                        f'forzó el bloque de emergencia y nadie sabe si entró',
                datos={'parcial': r.es_parcial, 'causa': 'bandera de emergencia'},
            ))
    return out


@invariante(
    codigo='REC-02',
    flujo='recepcion',
    frontera='OC → recepción',
    consecuencia='Se recibió por encima de la tolerancia pactada: mercancía '
                 'que nadie autorizó y que el proveedor va a cobrar.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorRecepcion::test_ve_el_exceso_por_encima_de_la_tolerancia',
)
def el_exceso_respeta_la_tolerancia(ctx=None):
    """Recibir de más no es un error por sí solo —los proveedores mandan de
    más y hay una tolerancia por línea—. Lo es pasarse de esa tolerancia."""
    from app.models.recepcion import ItemRecepcion
    out = []
    for it in ItemRecepcion.query.order_by(ItemRecepcion.id.desc()).limit(5000).all():
        ordenada = float(it.cantidad_ordenada or 0)
        recibida = float(it.cantidad_recibida or 0)
        if ordenada <= 0 or recibida <= ordenada:
            continue
        tope = ordenada * (1 + float(it.tolerancia_exceso_pct or 0) / 100.0)
        if recibida > tope:
            out.append(Hallazgo(
                referencia=f'recepcion#{it.recepcion_id} / producto#{it.producto_id}',
                detalle=f'recibida {recibida:g} sobre {ordenada:g} ordenadas '
                        f'(tope con tolerancia: {tope:g})',
                datos={'tolerancia_pct': it.tolerancia_exceso_pct},
            ))
    return out


@invariante(
    codigo='REC-03',
    flujo='recepcion',
    frontera='recepción → Siesa',
    consecuencia='Se mandó una entrada al ERP sin que nadie recibiera nada: se '
                 'sube inventario que no llegó.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorRecepcion::test_ve_una_entrada_por_cero',
)
def ninguna_entrada_sin_mercancia(ctx=None):
    """Una recepción disparada a Siesa con todas sus líneas en cero no es una
    recepción parcial: es una entrada por cero, que en el ERP queda como un
    documento sin movimiento y en el WMS como trabajo hecho."""
    from app.models.recepcion import ItemRecepcion
    out = []
    for r in _recepciones():
        if not r.siesa_triggered:
            continue
        total = sum(float(i.cantidad_recibida or 0)
                    for i in ItemRecepcion.query.filter_by(recepcion_id=r.id).all())
        if total <= 0:
            out.append(Hallazgo(
                referencia=r.codigo or f'recepcion#{r.id}',
                detalle='entrada disparada con 0 unidades recibidas',
                datos={'oc': r.numero_oc_siesa},
            ))
    return out


@invariante(
    codigo='REC-04',
    flujo='recepcion',
    frontera='recepción → Siesa',
    consecuencia='Recepciones abiertas hace días: mercancía en el muelle que '
                 'ni entró al inventario ni volvió al proveedor.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_devoluciones_recepcion.py::TestDetectorRecepcion::test_cuenta_las_abiertas',
)
def se_pueden_contar_las_recepciones_abiertas(ctx=None):
    from app.utils.fecha import ahora_bogota
    hoy = ahora_bogota().date()
    out = []
    for r in _recepciones(('ABIERTA', 'EN_PROCESO')):
        ref = r.fecha_creacion.date() if r.fecha_creacion else None
        dias = (hoy - ref).days if ref else None
        out.append(Hallazgo(
            referencia=r.codigo or f'recepcion#{r.id}',
            detalle=(f'{r.estado} hace {dias} día(s)' if dias is not None
                     else f'{r.estado} sin fecha de creación'),
            datos={'oc': r.numero_oc_siesa, 'proveedor': r.proveedor_nombre},
        ))
    return out

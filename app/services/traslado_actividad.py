"""¿Alguien está de verdad pickeando o empacando este traslado? (2026-10-02)

La tarjeta de Requisiciones decía «🔍 Operario pickeando...» solo porque la
solicitud estaba en `EN_PICKING`, y «📦 Empacando en …» solo porque estaba en
`EN_PACKING`: el estado del documento, no lo que pasa en la bodega. El dueño
vio la frase en un traslado que nadie había tomado. Un estado que afirma una
actividad que nadie verificó es peor que no decir nada: tranquiliza.

Ahora la frase sale de las tareas reales (`TareaPicking` de la solicitud,
`TareaPacking` del traslado) y distingue:

=================  =================================================
Picking            Qué significa
=================  =================================================
``SIN_TAREAS``     La solicitud está en picking y no tiene tareas: revisar
``SIN_TOMAR``      Hay líneas pendientes y ninguna tiene operario
``SIN_ACTIVIDAD``  Tiene operario, pero nada se movió en `UMBRAL_SIN_ACTIVIDAD_MIN`
``ACTIVO``         Algo se movió dentro del umbral
``COMPLETO``       Todas las líneas recogidas
=================  =================================================

**Lo que NO hay:** un sello por escaneo. `TareaPicking` guarda cuándo se tomó
la línea (`fecha_inicio`) y cuándo se terminó (`fecha_completado`); un escaneo
a mitad de línea no deja hora. Por eso la «última actividad» es la más reciente
de esas dos, y la pantalla dice cuál fue («tomó una línea» / «terminó una
línea»), no «último escaneo». Un picker escaneando una línea larga sin
terminarla puede aparecer «sin actividad»: es el lado conservador (Regla 0) —
prefiere que alguien vaya a mirar a que se crea que hay alguien trabajando.

Todo en lote: una consulta de tareas y una de nombres para toda la lista, no
una por solicitud.
"""
from datetime import datetime

#: Minutos sin que se mueva nada para dejar de decir «pickeando»/«empacando».
#: Declarado en CLAUDE.md («Paquetes al pedir» → actividad de Requisiciones).
UMBRAL_SIN_ACTIVIDAD_MIN = 30

SIN_TAREAS = 'SIN_TAREAS'
SIN_TOMAR = 'SIN_TOMAR'
SIN_ASIGNAR = 'SIN_ASIGNAR'
SIN_ACTIVIDAD = 'SIN_ACTIVIDAD'
ACTIVO = 'ACTIVO'
COMPLETO = 'COMPLETO'
TERMINADO = 'TERMINADO'

_ESTADOS_PICKING = ('EN_PICKING', 'PREPARADO')
_ESTADOS_PACKING = ('EN_PACKING', 'PREPARADO')
_PICKING_CERRADOS = ('COMPLETADO', 'CANCELADO')
_PACKING_TERMINADOS = ('VERIFICADO', 'DESPACHADO')


def _minutos(desde, ahora):
    if desde is None:
        return None
    return max(0, int((ahora - desde).total_seconds() // 60))


def nombres_de(ids):
    from app.models.usuario import Usuario
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.nombre for u in
            Usuario.query.filter(Usuario.id.in_(ids)).with_entities(Usuario.id, Usuario.nombre)}


def picking_de(solicitudes, ahora=None) -> dict:
    """`{solicitud_id: dict | None}`. `None` fuera de EN_PICKING/PREPARADO.

    El dict conserva las llaves de antes (`total`, `completadas`, `sin_tareas`,
    `porcentaje`) y agrega `actividad`, `operarios`, `minutos`,
    `ultima_actividad` (`TOMADA` | `TERMINADA` | None).
    """
    from app.models.picking import TareaPicking
    ahora = ahora or datetime.utcnow()
    aplica = {s.codigo: s for s in solicitudes if s.estado in _ESTADOS_PICKING}
    resultado = {s.id: None for s in solicitudes}
    if not aplica:
        return resultado
    tareas = (TareaPicking.query
              .filter(TareaPicking.referencia_documento.in_(list(aplica)),
                      TareaPicking.tipo_documento == 'TRASLADO')
              .with_entities(TareaPicking.referencia_documento, TareaPicking.estado,
                             TareaPicking.operario_id, TareaPicking.ultimo_operario_id,
                             TareaPicking.fecha_creacion, TareaPicking.fecha_inicio,
                             TareaPicking.fecha_completado)
              .all())
    por_codigo = {}
    for t in tareas:
        por_codigo.setdefault(t.referencia_documento, []).append(t)
    nombres = nombres_de([t.operario_id for t in tareas] + [t.ultimo_operario_id for t in tareas])

    for codigo, s in aplica.items():
        resultado[s.id] = actividad_picking(por_codigo.get(codigo, []), nombres, ahora)
    return resultado


def actividad_picking(ts, nombres, ahora) -> dict:
    """La actividad real de un grupo de `TareaPicking` (las de un traslado o
    las de un pedido). Una sola regla para Requisiciones y para la espera del
    empacador. `ts` necesita `estado`, `operario_id`, `ultimo_operario_id`,
    `fecha_creacion`, `fecha_inicio`, `fecha_completado`; `nombres`
    `{usuario_id: nombre}`."""
    total = len(ts)
    completadas = sum(1 for t in ts if t.estado == 'COMPLETADO')
    if total == 0:
        return {'total': 0, 'completadas': 0, 'sin_tareas': True,
                'actividad': SIN_TAREAS, 'operarios': [], 'operarios_anteriores': [],
                'minutos': None, 'ultima_actividad': None}
    abiertas = [t for t in ts if t.estado not in _PICKING_CERRADOS]
    con_operario = [t for t in abiertas if t.operario_id]
    operarios = sorted({nombres.get(t.operario_id, f'#{t.operario_id}') for t in con_operario})
    sellos = ([(t.fecha_completado, 'TERMINADA') for t in ts if t.fecha_completado]
              + [(t.fecha_inicio, 'TOMADA') for t in ts if t.fecha_inicio])
    ultima, tipo = max(sellos, key=lambda x: x[0]) if sellos else (None, None)

    if not abiertas:
        actividad, minutos = COMPLETO, _minutos(ultima, ahora)
    elif not con_operario:
        actividad = SIN_TOMAR
        minutos = _minutos(ultima or min((t.fecha_creacion for t in ts if t.fecha_creacion),
                                         default=None), ahora)
    else:
        # Sin sello propio, la línea asignada se mide desde que nació.
        desde = ultima or min((t.fecha_creacion for t in con_operario if t.fecha_creacion),
                              default=None)
        minutos = _minutos(desde, ahora)
        actividad = (ACTIVO if (ultima is not None and minutos is not None
                                and minutos <= UMBRAL_SIN_ACTIVIDAD_MIN)
                     else SIN_ACTIVIDAD)
    # Nadie las tiene ahora: decir quién las trabajó por última vez.
    previos = [] if operarios else sorted(
        {nombres[t.ultimo_operario_id] for t in ts
         if t.ultimo_operario_id and nombres.get(t.ultimo_operario_id)})
    return {
        'total': total,
        'completadas': completadas,
        'sin_tareas': False,
        'porcentaje': round(completadas / total * 100),
        'actividad': actividad,
        'operarios': operarios,
        'operarios_anteriores': previos,
        'minutos': minutos,
        'ultima_actividad': tipo,
    }


def packing_de(solicitudes, ahora=None) -> dict:
    """`{solicitud_id: dict | None}`. `None` fuera de EN_PACKING/PREPARADO o sin
    tarea. Conserva `id`, `codigo`, `estado`, `empacador` y agrega
    `actividad` y `minutos`."""
    from app.models.packing import TareaPacking
    ahora = ahora or datetime.utcnow()
    aplica = {s.id for s in solicitudes if s.estado in _ESTADOS_PACKING}
    resultado = {s.id: None for s in solicitudes}
    if not aplica:
        return resultado
    tareas = (TareaPacking.query
              .filter(TareaPacking.solicitud_id.in_(list(aplica)))
              .order_by(TareaPacking.id)
              .with_entities(TareaPacking.id, TareaPacking.solicitud_id, TareaPacking.codigo,
                             TareaPacking.estado, TareaPacking.empacador_id,
                             TareaPacking.fecha_creacion, TareaPacking.fecha_inicio,
                             TareaPacking.fecha_verificado)
              .all())
    primera = {}
    for t in tareas:
        primera.setdefault(t.solicitud_id, t)  # la misma que `tareas_packing[0]`
    nombres = nombres_de([t.empacador_id for t in primera.values()])
    for sid, t in primera.items():
        sellos = [x for x in (t.fecha_inicio, t.fecha_verificado) if x]
        ultima = max(sellos) if sellos else None
        if t.estado in _PACKING_TERMINADOS:
            actividad, minutos = TERMINADO, _minutos(ultima, ahora)
        elif not t.empacador_id:
            actividad, minutos = SIN_ASIGNAR, _minutos(t.fecha_creacion, ahora)
        else:
            minutos = _minutos(ultima or t.fecha_creacion, ahora)
            actividad = (ACTIVO if (ultima is not None and minutos is not None
                                    and minutos <= UMBRAL_SIN_ACTIVIDAD_MIN)
                         else SIN_ACTIVIDAD)
        resultado[sid] = {
            'id': t.id,
            'codigo': t.codigo,
            'estado': t.estado,
            'empacador': nombres.get(t.empacador_id) if t.empacador_id else None,
            'actividad': actividad,
            'minutos': minutos,
        }
    return resultado

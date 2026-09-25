"""
La bitácora de acciones — **una** función escribe en ella.

`registrar_accion(...)` agrega la fila a la sesión y **no hace commit**: va en
la misma transacción que la acción que registra. Si la acción se revierte, la
fila también; si la acción se confirma, la fila también. Una bitácora que se
escribiera aparte podría decir que se canceló algo que sigue vivo, o callar
algo que sí se canceló.

## Qué se registra

Lo que deja el recorrido pedido → caja sin una parte, o lo cambia después de
ocurrido: ELIMINAR, ANULAR, CANCELAR, REABRIR, EDITAR, REASIGNAR,
DESASIGNAR, REINTENTAR, DESCARTAR, FORZAR, LIQUIDAR, DESACTIVAR, BLOQUEAR.
El vocabulario es cerrado: un verbo libre haría que `'cancelar'` y
`'CANCELACION'` fueran dos series que nadie suma.

El flujo normal (confirmar un picking, cerrar un packing, cargar un bulto) NO
pasa por acá: su autor queda en columnas de la propia fila. La bitácora es
para lo que interrumpe o reescribe el flujo.

## `antes` / `despues`

Solo los campos que la acción cambia. Cuando la acción borra la fila, `antes`
lleva la fila completa (`foto(obj)`): es lo único que queda de ella.

## Motivo

`motivo_obligatorio()` es la única política de «el motivo vacío se rechaza».
Se aplica donde la acción es de negocio y la pantalla ya manda el motivo (o
no hay pantalla). Donde la pantalla todavía no lo pide, la acción se registra
igual con `motivo=None` — y CLAUDE.md lo declara, sitio por sitio.
"""
from datetime import date, datetime
from decimal import Decimal

from app.extensions import db
from app.models.bitacora import BitacoraAccion
from app.utils.fecha import dia_operativo_de

#: El vocabulario. Agregar un verbo es una decisión: los consumidores
#: (analítica, Fase 1) agrupan por acá.
ACCIONES = (
    'ELIMINAR',     # la fila deja de existir (borrado físico)
    'ANULAR',       # la fila queda, marcada como sin efecto
    'CANCELAR',     # una tarea/documento deja de ejecutarse
    'REABRIR',      # algo cerrado o bloqueado vuelve a estar vivo
    'EDITAR',       # un valor ya registrado cambia
    'REASIGNAR',    # cambia quién o dónde
    'DESASIGNAR',   # se quita la asignación (bulto ↔ ruta)
    'REINTENTAR',   # un job FALLIDO vuelve a la cola — su error queda acá
    'DESCARTAR',    # se decide no hacer algo pendiente
    'FORZAR',       # se salta una guarda — cuál, en despues['forzado'] (TIPOS_FORZADO)
    'LIQUIDAR',     # la ruta pasa a LIQUIDADA
    'DESACTIVAR',   # baja lógica de un maestro (producto, conductor, vehículo)
    'BLOQUEAR',     # una tarea sale del flujo esperando a un humano
)


#: Qué guarda se saltó un FORZAR. El verbo es uno (vocabulario cerrado) y lo
#: escriben dos sitios sobre la MISMA entidad (`RutaDespacho`): el cierre
#: forzado de una ruta y el despacho con advertencias de flota. Hasta el
#: 2026-09-24 nadie los distinguía: la jornada leía «despachó con el SOAT
#: vencido» como «ruta cerrada a la fuerza por la oficina» y la bitácora
#: legible decía «forzó el cierre» de una ruta que cerró el conductor.
#: Todo FORZAR declara cuál es en `despues['forzado']` (lo exige
#: `registrar_accion`); `tipo_de_forzado` lee eso y, para las filas de antes,
#: la forma de su `despues`.
FORZADO_CIERRE_RUTA = 'cierre_de_ruta'
FORZADO_ADVERTENCIAS_FLOTA = 'despacho_con_advertencias_de_flota'
# Los tres de abajo llegaron en la integración del 2026-09-24: cartera y
# devoluciones escribían FORZAR en paralelo al frente que exigió declararlo.
# Uno cae sobre la MISMA entidad (`RutaDespacho`): sin su tipo, la jornada lo
# habría leído como un cierre forzado de ruta.
FORZADO_AUTORIZACION_CARTERA = 'despacho_autorizado_por_cartera'
FORZADO_LIQUIDACION_SIN_CONTAR = 'liquidacion_con_devoluciones_sin_contar'
FORZADO_NC_APROBADA_A_MANO = 'nc_aprobada_sin_verificar_en_siesa'
# Frente dinero (2026-09-25).
FORZADO_PARADA_TARDIA = 'parada_confirmada_despues_del_cierre'
FORZADO_RC_RESUELTO_A_MANO = 'recibo_de_caja_resuelto_a_mano'
FORZADO_FE_SOBRE_RM_DIGITADA = 'factura_sobre_remision_digitada'
# Tanda 2 (2026-09-25).
FORZADO_PREFLAG_RESUELTO_A_MANO = 'envio_sin_verificar_resuelto_a_mano'

#: tipo → (verbo en pasado para la bitácora legible, qué se saltó).
TIPOS_FORZADO = {
    FORZADO_CIERRE_RUTA: ('forzó el cierre de',
                          'cerró la ruta dando por rechazado lo no gestionado'),
    FORZADO_ADVERTENCIAS_FLOTA: ('despachó pese a las advertencias de flota',
                                 'la ruta salió con advertencias de flota reconocidas'),
    FORZADO_AUTORIZACION_CARTERA: ('levantó con tope',
                                   'el pedido retenido por cartera sale con un tope'),
    FORZADO_LIQUIDACION_SIN_CONTAR: ('liquidó con devoluciones sin contar',
                                     'la ruta se liquidó sin que bodega contara lo devuelto'),
    FORZADO_NC_APROBADA_A_MANO: ('marcó aprobada a mano la nota crédito de',
                                 'la NC se dio por aprobada sin verificarla en Siesa'),
    FORZADO_PARADA_TARDIA: ('confirmó después del cierre de la ruta',
                            'la parada se registró con la ruta ya entregada'),
    FORZADO_RC_RESUELTO_A_MANO: ('resolvió a mano el recibo de caja de',
                                 'el desenlace de un recibo sin verificar lo decidió una persona'),
    FORZADO_FE_SOBRE_RM_DIGITADA: ('facturó sobre una remisión digitada a mano',
                                   'la factura salió sobre una remisión que el WMS no tenía'),
    FORZADO_PREFLAG_RESUELTO_A_MANO: ('resolvió a mano el envío a Siesa de',
                                      'el desenlace de un envío sin verificar lo decidió una persona'),
}

#: Un FORZAR que no dice qué forzó (fila vieja de forma desconocida).
VERBO_FORZADO_GENERICO = 'se saltó una guarda en'


def tipo_de_forzado(entidad, despues):
    """Qué guarda se saltó un FORZAR: una clave de `TIPOS_FORZADO` o `None` si
    la fila no lo dice ni su forma lo delata. **La única función que contesta
    esto**: jornada, bitácora legible y quien venga después la llaman."""
    d = despues if isinstance(despues, dict) else {}
    t = d.get('forzado')
    if t in TIPOS_FORZADO:
        return t
    # Filas escritas antes de `forzado` (2026-09-24): la forma las delata.
    if 'advertencias_flota' in d:
        return FORZADO_ADVERTENCIAS_FLOTA
    if entidad == 'RutaDespacho' and 'paradas_auto_rechazadas' in d:
        return FORZADO_CIERRE_RUTA
    return None


def verbo_de_forzado(entidad, despues) -> str:
    t = tipo_de_forzado(entidad, despues)
    return TIPOS_FORZADO[t][0] if t else VERBO_FORZADO_GENERICO


class MotivoRequerido(ValueError):
    """El motivo es obligatorio y llegó vacío. Las rutas lo traducen a 400."""


def motivo_obligatorio(motivo, que: str = 'esta acción') -> str:
    """El motivo, sin espacios sobrantes — o `MotivoRequerido` si está vacío.

    Una sola definición de «vacío»: `None`, `''` y `'   '` son lo mismo. Si
    cada sitio lo decidiera por su cuenta, uno aceptaría espacios y el
    motivo registrado sería un blanco que nadie puede leer.
    """
    texto = str(motivo).strip() if motivo is not None else ''
    if not texto:
        raise MotivoRequerido(f'El motivo es obligatorio para {que}')
    return texto


def _jsonable(valor):
    """Lo que la columna JSON puede guardar sin perder el dato."""
    if valor is None or isinstance(valor, (bool, int, float, str)):
        return valor
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        # str y no float: un monto se guarda exacto.
        return str(valor)
    if isinstance(valor, dict):
        return {str(k): _jsonable(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set)):
        return [_jsonable(v) for v in valor]
    return str(valor)


def foto(obj, campos=None) -> dict:
    """Los campos de una fila, listos para `antes`/`despues`.

    Sin `campos`, todas las columnas: es lo que se guarda cuando la fila se
    borra, porque después no queda otra cosa.
    """
    if obj is None:
        return None
    if campos is None:
        campos = [c.key for c in obj.__table__.columns]
    return {c: _jsonable(getattr(obj, c, None)) for c in campos}


def _codigo_legible(obj):
    for attr in ('codigo', 'codigo_barras', 'numero_pedido_siesa', 'placa',
                 'referencia', 'nombre'):
        v = getattr(obj, attr, None)
        if v:
            return str(v)[:80]
    return None


def _origen_de_la_peticion():
    try:
        from flask import has_request_context, request
        if has_request_context():
            return f'{request.method} {request.path}'[:120]
    except Exception:
        pass
    return None


def registrar_accion(accion: str, entidad, entidad_id: int = None, *,
                     entidad_codigo: str = None, usuario_id: int = None,
                     motivo: str = None, antes=None, despues=None,
                     almacen_id: int = None, origen: str = None) -> BitacoraAccion:
    """Agrega una fila a la bitácora. **No hace commit.**

    `entidad` puede ser el nombre (`'TareaPicking'`) o la fila misma: en ese
    caso se toman de ella el nombre de la clase, el id, el código legible y
    el almacén, salvo que se pasen explícitos.

    `origen`, si no se pasa, es la petición HTTP en curso (`PUT /api/...`).
    Fuera de una petición (cron, hilo) hay que nombrar el proceso.
    """
    if accion not in ACCIONES:
        raise ValueError(f'Acción de bitácora desconocida: {accion!r}. '
                         f'Vocabulario: {", ".join(ACCIONES)}')

    if accion == 'FORZAR' and (despues if isinstance(despues, dict) else {}).get(
            'forzado') not in TIPOS_FORZADO:
        raise ValueError(
            "Un FORZAR declara qué forzó: despues['forzado'] ∈ "
            f"{sorted(TIPOS_FORZADO)}. Sin eso, cualquier lector lo confunde "
            'con un cierre forzado de ruta.')

    if not isinstance(entidad, str):
        fila = entidad
        entidad = type(fila).__name__
        if entidad_id is None:
            entidad_id = getattr(fila, 'id', None)
        if entidad_codigo is None:
            entidad_codigo = _codigo_legible(fila)
        if almacen_id is None:
            almacen_id = getattr(fila, 'almacen_id', None)
    if not entidad:
        raise ValueError('La bitácora exige nombrar la entidad')

    ahora = datetime.utcnow()
    motivo = (str(motivo).strip() or None) if motivo is not None else None
    fila_bitacora = BitacoraAccion(
        ocurrido_en=ahora,
        dia_operativo=dia_operativo_de(ahora),
        accion=accion,
        entidad=entidad[:40],
        entidad_id=entidad_id,
        entidad_codigo=(str(entidad_codigo)[:80] if entidad_codigo else None),
        usuario_id=usuario_id,
        motivo=motivo,
        antes=_jsonable(antes),
        despues=_jsonable(despues),
        almacen_id=almacen_id,
        origen=(origen or _origen_de_la_peticion()),
    )
    db.session.add(fila_bitacora)
    return fila_bitacora


__all__ = ['ACCIONES', 'MotivoRequerido', 'motivo_obligatorio', 'foto',
           'registrar_accion']

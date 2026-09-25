"""
La plata del conductor hasta Siesa: **una función por pregunta**.

Cada pregunta de este módulo estaba contestada dos o tres veces, y las copias
divergieron (auditoría 2026-09-25):

· **¿Se puede emitir la retención?** `registrar_cobro_recaudo` bloqueaba una
  retención rechazada; `_procesar_recaudo` (el botón masivo) encolaba el DC
  con solo mirar `motivo_descuento` y nunca leía `retencion_confirmada`: una
  NI emitida por plata que el cliente no tenía derecho a descontar.
  → `decision_retencion` / `exigir_retencion_aplicable`.

· **¿Por cuánto sale el recibo de caja?** En una PARCIAL con retención,
  `monto_cobrado` ya es lo que el cliente pagó —neto de la retención que
  aplicó en la puerta— y «Registrar cobro» le restaba la retención OTRA VEZ.
  El botón masivo no la restaba. Dos puertas, dos montos. → `monto_rc`.

· **¿Sobre qué base se retiene?** Sobre la factura completa, incluida la
  mercancía que volvió al camión. → `base_retencion_entregada`.

· **¿Se puede cambiar el cobro de una parada?** Se congelaba con
  `siesa_rc_triggered`, que se enciende recién cuando el DLQ hace el POST.
  Entre encolar y enviar, el monto y el estado se reescribían y el RC salía
  con la cifra vieja. → `puede_editar_cobro`.

· **¿El recibo llegó a Siesa?** VTA-60 y la reconciliación leían «hay un job
  COMPLETADO», y un RC cuyo POST falló y no se pudo verificar terminaba
  COMPLETADO con `verificacion_imposible`. → `rc_llego_a_siesa`.

Sin red salvo donde se dice. Trinquete: `tests/test_politica_cobro.py`.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La retención que declaró el conductor
# ═════════════════════════════════════════════════════════════════════════════

#: El conductor no declaró retención: si quien liquida elige una, la decide él.
SIN_DECLARAR = 'SIN_DECLARAR'
#: El conductor la declaró y nadie decidió: **ni RC ni DC** (el monto del RC
#: depende de si la retención procede).
PENDIENTE = 'PENDIENTE'
CONFIRMADA = 'CONFIRMADA'
#: El cliente no tenía derecho: esa retención no se emite nunca, y el cobro
#: tiene que cubrir el valor completo.
RECHAZADA = 'RECHAZADA'


class RetencionNoAplicable(ValueError):
    """La retención no se puede emitir (o el cobro no se puede registrar)
    todavía o nunca. `ValueError` para que los llamadores de siempre la vean."""


def decision_retencion(recaudo) -> str:
    """`SIN_DECLARAR` | `PENDIENTE` | `CONFIRMADA` | `RECHAZADA`. La única."""
    if not getattr(recaudo, 'motivo_descuento', None):
        return SIN_DECLARAR
    confirmada = getattr(recaudo, 'retencion_confirmada', None)
    if confirmada is None:
        return PENDIENTE
    return CONFIRMADA if confirmada else RECHAZADA


def exigir_cobro_decidido(recaudo) -> None:
    """Con una retención declarada y sin decidir no sale ningún documento de
    plata: el monto del RC depende de si procede. Levanta `RetencionNoAplicable`."""
    if decision_retencion(recaudo) == PENDIENTE:
        raise RetencionNoAplicable(
            f'El conductor declaró un motivo de retención '
            f'({recaudo.motivo_descuento}) — confírmela o rechácela antes de '
            f'registrar el cobro')


def exigir_retencion_aplicable(recaudo, tipo_ret: str) -> None:
    """**La puerta de todo documento de retención (DC).** La llama el único
    encolador (`liquidacion_service._encolar_retencion`) y la revalida el
    ejecutor del DLQ antes del POST. Levanta `RetencionNoAplicable`.

    · PENDIENTE → nada sale hasta que alguien decida.
    · RECHAZADA → la retención rechazada no se emite nunca (otra que el que
      liquida elija a mano, sí: esa la decide él).
    · SIN_DECLARAR / CONFIRMADA → procede.
    """
    exigir_cobro_decidido(recaudo)
    if decision_retencion(recaudo) == RECHAZADA and tipo_ret == recaudo.motivo_descuento:
        raise RetencionNoAplicable(
            f'La retención {tipo_ret} fue rechazada — no se puede emitir el '
            f'documento contable')


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El monto del recibo de caja
# ═════════════════════════════════════════════════════════════════════════════

def monto_rc(recaudo, *, total_neto_siesa: float = None, retencion: float = 0.0,
             monto_override: float = None) -> float:
    """El monto del recibo de caja. **Una función para las dos puertas**
    («Registrar cobro» por parada y «Liquidar ruta» masivo).

    · **PARCIAL** — lo que entró: `monto_cobrado` (o el override de quien
      liquida). La pantalla del conductor arma ese número como «valor de lo
      entregado − descuento», así que **ya viene neto de la retención**. No se
      le resta otra vez: la retención la documenta el DC, y la devolución la
      NC. RC + DC + NC = factura.
    · **ENTREGADO** — la factura: el neto real de Siesa (o el override, o lo
      declarado si Siesa no respondió), **menos** la retención que procede.

    `retencion` es la suma de retenciones que SÍ se van a emitir (el llamador
    la calcula con `base_retencion_entregada` y la filtra con
    `exigir_retencion_aplicable`). Redondeado a centavos.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    if recaudo.estado_entrega == EstadoEntrega.PARCIAL:
        base = monto_override if monto_override is not None else (recaudo.monto_cobrado or 0)
        return round(float(base), 2)
    if monto_override is not None:
        bruto = float(monto_override)
    elif total_neto_siesa is not None and float(total_neto_siesa) > 0:
        bruto = float(total_neto_siesa)
    else:
        bruto = float(recaudo.monto_cobrado or 0)
    return round(bruto - float(retencion or 0), 2)


def rc_resta_retencion(recaudo) -> bool:
    """¿El RC de esta parada sale neto de la retención? Lo que dice `monto_rc`,
    para la pantalla (la vista previa no puede calcular por su cuenta)."""
    from app.models.recaudo_entrega import EstadoEntrega
    return recaudo.estado_entrega != EstadoEntrega.PARCIAL


# ═════════════════════════════════════════════════════════════════════════════
# 3 · La base de la retención: lo que el cliente se quedó
# ═════════════════════════════════════════════════════════════════════════════

def base_retencion_entregada(recaudo, lineas_factura: list):
    """`(base_gravable, iva)` de lo que el cliente **se quedó**, o `None` si no
    se puede saber.

    ENTREGADO: la factura entera (`f470_vlr_bruto`, `f470_vlr_imp` de API 45).

    PARCIAL: cada línea de la factura menos lo que el conductor declaró que
    volvió, prorrateado por cantidad (`f470_cant_base`). Lo declarado por
    línea sale de la devolución de la parada **amarrada a la factura**
    (`f470_rowid`). Es lo declarado y no lo contado: el cliente retiene sobre
    lo que recibió, no sobre lo que bodega encontró en el camión. Un producto
    de doble unidad declarado por referencia no se puede repartir entre sus
    dos líneas: ahí vale lo contado si ya se contó.

    `None` (Regla 0) cuando la devolución no está amarrada todavía, o una
    línea no se puede atribuir: **no se inventa una base**. El DC no sale y se
    declara; el RC de una PARCIAL no depende de esto.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    lineas = lineas_factura or []
    if recaudo.estado_entrega != EstadoEntrega.PARCIAL:
        return (round(sum(float(ln.get('f470_vlr_bruto', 0) or 0) for ln in lineas), 2),
                round(sum(float(ln.get('f470_vlr_imp', 0) or 0) for ln in lineas), 2))

    from app.models.devolucion_cliente import EstadoDevolucionCliente as E
    from app.services.devolucion_ruta import devolucion_vigente
    dev = devolucion_vigente(recaudo.id)
    if dev is None or (dev.vinculada_factura_at is None and dev.estado not in E.CONTADAS):
        return None
    contada = dev.estado in E.CONTADAS
    por_producto = ((dev.declaracion_conductor or {}).get('declarado_por_producto') or {})
    if por_producto and not contada:
        return None
    devuelto = {}
    for ln in dev.lineas:
        rid = str(ln.f470_rowid or '').strip()
        if not rid:
            return None
        if contada and por_producto:
            q = ln.cantidad_devuelta
        elif ln.cantidad_declarada is not None:
            q = ln.cantidad_declarada
        elif contada:
            q = ln.cantidad_devuelta
        else:
            return None
        devuelto[rid] = devuelto.get(rid, 0.0) + float(q or 0)

    base = iva = 0.0
    for fila in lineas:
        rid = str(fila.get('f470_rowid') or '').strip()
        cant = float(fila.get('f470_cant_base') or 0)
        dev_q = devuelto.pop(rid, 0.0)
        if dev_q <= 0:
            factor = 1.0
        elif cant <= 0:
            return None
        else:
            factor = max(0.0, (cant - dev_q) / cant)
        base += float(fila.get('f470_vlr_bruto', 0) or 0) * factor
        iva += float(fila.get('f470_vlr_imp', 0) or 0) * factor
    if any(q > 0 for q in devuelto.values()):
        # Lo declarado apunta a una línea que la factura no trae.
        return None
    return round(base, 2), round(iva, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · ¿Se puede cambiar el cobro de esta parada?
# ═════════════════════════════════════════════════════════════════════════════

#: Estados de la cola en los que un job **todavía cuenta** (vivo o ya hecho).
_JOB_NO_CUENTA = ('FALLIDO', 'DESCARTADO')


def motivo_cobro_congelado(recaudo) -> str | None:
    """Por qué el cobro de esta parada ya no se puede cambiar, o `None`.

    Se congela **al encolar** el recibo de caja (o una retención), no al
    enviarlo: el job lleva el monto armado desde estos campos, y reescribirlos
    mientras espera en la cola hace que Siesa reciba una cifra y el WMS diga
    otra. Un job FALLIDO/DESCARTADO con la guarda abajo ya no congela: no hay
    documento en Siesa que proteger.
    """
    if recaudo is None or getattr(recaudo, 'id', None) is None:
        return None
    if recaudo.siesa_rc_triggered:
        return 'el recibo de caja de esta parada ya se envió a Siesa'
    from app.models.siesa_job import SiesaJob
    vivo = SiesaJob.query.filter(
        SiesaJob.tipo.in_(('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET')),
        SiesaJob.referencia_tipo == 'RecaudoEntrega',
        SiesaJob.referencia_id == recaudo.id,
        SiesaJob.estado.notin_(_JOB_NO_CUENTA),
    ).first()
    if vivo is not None:
        doc = 'el recibo de caja' if vivo.tipo == 'RECIBO_CAJA' else 'una retención'
        return f'{doc} de esta parada ya está en cola para Siesa (job {vivo.id})'
    return None


def puede_editar_cobro(recaudo) -> bool:
    """¿Se pueden reescribir `monto_cobrado`/`estado_entrega`/forma de pago?
    Toda escritura de esos campos pasa por acá (trinquete AST)."""
    return motivo_cobro_congelado(recaudo) is None


def instantanea_cobro(recaudo) -> dict:
    """Lo que el RC encolado da por cierto de la parada. El ejecutor lo compara
    antes del POST (`difiere_de_instantanea`): si la parada cambió por un
    camino que no pasó por `puede_editar_cobro`, el RC no sale."""
    return {
        'monto_cobrado': round(float(recaudo.monto_cobrado or 0), 2),
        'estado_entrega': recaudo.estado_entrega,
        'forma_pago': (recaudo.forma_pago or '').upper() or None,
    }


def difiere_de_instantanea(recaudo, instantanea) -> list:
    """Los campos que cambiaron desde que se encoló el RC. `[]` si ninguno o si
    el job es anterior a esto (sin instantánea no hay contra qué comparar)."""
    if not isinstance(instantanea, dict) or not instantanea:
        return []
    ahora = instantanea_cobro(recaudo)
    cambios = []
    for k, antes in instantanea.items():
        if k not in ahora:
            continue
        v = ahora[k]
        if k == 'monto_cobrado':
            if abs(float(antes or 0) - float(v or 0)) > 0.01:
                cambios.append(f'{k} ({antes} → {v})')
        elif (antes or None) != (v or None):
            cambios.append(f'{k} ({antes} → {v})')
    return cambios


# ═════════════════════════════════════════════════════════════════════════════
# 5 · ¿El recibo de caja llegó a Siesa?
# ═════════════════════════════════════════════════════════════════════════════

#: Desenlaces que dicen «el documento existe» (o la factura ya estaba saldada).
LLEGO = ('ENVIADO', 'YA_SALDADA')
#: Marcas de un COMPLETADO que NO confirma nada (filas de antes de m036fotos).
_COMPLETADO_SIN_CONFIRMAR = ('verificacion_imposible', 'modo_ensayo', 'idempotente')


def _completado_confirma(job) -> bool:
    try:
        res = json.loads(job.resultado or '{}')
    except (ValueError, TypeError):
        return False
    if not isinstance(res, dict):
        return True
    return not any(res.get(k) for k in _COMPLETADO_SIN_CONFIRMAR)


def rc_llegaron(recaudos) -> set:
    """Ids de los recaudos cuyo RC **se sabe** que llegó a Siesa. La única.

    Señal positiva: `siesa_rc_resultado` ∈ `LLEGO`. Un recaudo anterior a esa
    columna (resultado `NULL`) cuenta solo con un job COMPLETADO que no diga
    `verificacion_imposible`/`idempotente`/`modo_ensayo` — esos COMPLETADO no
    confirmaban nada, y contarlos es exactamente el número que dice «llegó»
    sin haberlo confirmado.
    """
    recaudos = list(recaudos or [])
    ok = {r.id for r in recaudos if r.siesa_rc_resultado in LLEGO}
    legado = [r.id for r in recaudos if r.siesa_rc_resultado is None]
    if legado:
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        for j in SiesaJob.query.filter(
                SiesaJob.tipo == 'RECIBO_CAJA',
                SiesaJob.referencia_tipo == 'RecaudoEntrega',
                SiesaJob.referencia_id.in_(legado),
                SiesaJob.estado == EstadoSiesaJob.COMPLETADO).all():
            if _completado_confirma(j):
                ok.add(j.referencia_id)
    return ok


def rc_llego_a_siesa(recaudo) -> bool:
    return recaudo is not None and recaudo.id in rc_llegaron([recaudo])


# ═════════════════════════════════════════════════════════════════════════════
# 6 · ¿Qué documento de esta parada falta mandar?
# ═════════════════════════════════════════════════════════════════════════════

def documentos_pendientes(recaudo, tarea=None) -> list:
    """Los documentos que «Enviar a Siesa» produciría hoy para esta parada:
    `['NC']`, `['RC']`, ambos o `[]`. La pantalla no lo recalcula: una
    devolución contada en cero (FALTANTE_TOTAL) no va a tener NC, y el botón
    quedaba visible para siempre invitando a mandar algo que no existe."""
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import cond_pago as _cp
    from app.services import devolucion_ruta as _dr
    faltan = []
    if recaudo is None:
        return faltan
    if (recaudo.estado_entrega in (EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL)
            and not recaudo.siesa_nc_triggered and not _dr.nc_no_llegara(recaudo)):
        faltan.append('NC')
    if (recaudo.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)
            and float(recaudo.monto_cobrado or 0) > 0
            and not recaudo.siesa_rc_triggered
            and _cp.trato_de_cobro(recaudo, tarea if tarea is not None else recaudo.tarea)
            == _cp.TRATO_CONTADO):
        from app.services.liquidacion_service import _hay_rc_en_cola
        if not _hay_rc_en_cola(recaudo.id):
            faltan.append('RC')
    return faltan

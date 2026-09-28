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

class MontoNoPermitido(ValueError):
    """El monto del recibo (o su corrección) se sale de lo que la política
    permite. `ValueError` para los llamadores de siempre."""


#: Tolerancia de «es el mismo monto» (redondeo de centavos del teléfono).
TOLERANCIA_MONTO = 0.5


def exigir_override_permitido(recaudo, monto_override) -> None:
    """**Quien liquida no cambia el monto que declaró el conductor**
    (validación de la plata, 2026-09-26). En una PARCIAL el recibo sale por lo
    declarado: `monto_override` distinto de `monto_cobrado` era «corregir el
    cobro» por la puerta de registrar el cobro — sin `puede_corregir_cobro`, sin
    motivo y sin tope (el liquidador mandaba el RC por menos y el faltante
    quedaba de saldo del cliente en cartera). Si el número está mal, se corrige
    con la política de corrección (`exigir_correccion_de_monto`: admin + líder
    de cartera, razón, tope) y después se registra.

    En un ENTREGADO el override es el neto de Siesa, y su distancia a lo
    declarado la acota `_validar_diferencia_declarada` (o el ajuste al peso,
    con razón y tope). Sin nada declarado (≤ 0) no hay contra qué acotarlo:
    Regla 0, no sale. Levanta `MontoNoPermitido`."""
    from app.models.recaudo_entrega import EstadoEntrega
    if monto_override is None:
        return
    declarado = float(recaudo.monto_cobrado or 0)
    propuesto = float(monto_override)
    if recaudo.estado_entrega == EstadoEntrega.PARCIAL:
        if abs(propuesto - declarado) > TOLERANCIA_MONTO:
            raise MontoNoPermitido(
                f'El recibo de una entrega parcial sale por lo que declaró el conductor '
                f'(${declarado:,.0f}), no por ${propuesto:,.0f}. Si ese monto está mal, '
                f'corríjalo primero con «Corregir monto» (administrador o líder de '
                f'cartera, con una razón) y registre el cobro después.')
        return
    if declarado <= 0:
        raise MontoNoPermitido(
            'El conductor no declaró monto cobrado en esta parada: no hay contra qué '
            'respaldar un recibo de caja. Corrija el monto primero (administrador o líder '
            'de cartera, con una razón).')


def tope_de_correccion(recaudo, tarea=None):
    """El monto máximo al que se puede corregir lo cobrado en una parada: la
    factura (anotada en la tarea) más el residuo de redondeo — lo más que el
    cliente puede deber (con la retención rechazada, la factura entera). `None`
    si la factura no está anotada."""
    from app.services.liquidacion_service import tope_diferencia_recaudo
    t = tarea if tarea is not None else getattr(recaudo, 'tarea', None)
    valor = getattr(t, 'valor_factura', None) if t is not None else None
    if valor is None:
        return None
    return round(float(valor) + tope_diferencia_recaudo(), 2)


def exigir_correccion_de_monto(recaudo, nuevo_monto, razon) -> float:
    """**La política de corrección del monto cobrado.** Toda corrección pasa
    por acá (`LiquidacionService.corregir_monto_declarado`; la ruta exige
    `puede_corregir_cobro`). Devuelve el monto redondeado o levanta
    `MontoNoPermitido`.

    - Razón obligatoria (una corrección hacia abajo es «menos de lo declarado»:
      nunca sin motivo).
    - Tope: no más que lo que el cliente debía (`tope_de_correccion`). Sin la
      factura anotada: una PARCIAL no sube de lo declarado; un ENTREGADO queda
      acotado al registrar el cobro contra el neto de Siesa.
    - Mayor que cero; distinto de lo declarado.
    """
    nuevo = round(float(nuevo_monto or 0), 2)
    if nuevo <= 0:
        raise MontoNoPermitido('El monto corregido debe ser mayor a 0')
    if not (razon or '').strip():
        raise MontoNoPermitido(
            'La corrección necesita una razón. No es un ajuste contable —no toca Siesa—, '
            'pero sigue siendo dinero: sin razón, en tres meses nadie sabe por qué cambió '
            'el número.')
    declarado = float(recaudo.monto_cobrado or 0)
    if abs(nuevo - declarado) < 0.01:
        raise MontoNoPermitido(
            f'El monto corregido (${nuevo:,.2f}) es igual al ya declarado — no hay nada '
            f'que corregir')
    from app.models.recaudo_entrega import EstadoEntrega
    tope = tope_de_correccion(recaudo)
    # Sin la factura anotada: en una PARCIAL el recibo sale por lo declarado,
    # así que no se sube (Regla 0). En un ENTREGADO el recibo sale por el neto
    # de Siesa y `_validar_diferencia_declarada` lo acota al registrar.
    techo = tope if tope is not None else (
        declarado if recaudo.estado_entrega == EstadoEntrega.PARCIAL else None)
    if techo is not None and nuevo > techo + 0.01:
        raise MontoNoPermitido(
            f'El monto corregido (${nuevo:,.2f}) supera lo que el cliente debía por esta '
            f'parada (${techo:,.2f}'
            + (', el valor de la factura' if tope is not None
               else '; sin la factura anotada una entrega parcial no sube de lo declarado')
            + '). Una diferencia mayor no se corrige acá.')
    return nuevo


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
    exigir_override_permitido(recaudo, monto_override)
    if recaudo.estado_entrega == EstadoEntrega.PARCIAL:
        return round(float(recaudo.monto_cobrado or 0), 2)
    if monto_override is not None:
        bruto = float(monto_override)
    elif total_neto_siesa is not None and float(total_neto_siesa) > 0:
        bruto = float(total_neto_siesa)
    else:
        bruto = float(recaudo.monto_cobrado or 0)
    return round(bruto - float(retencion or 0), 2)


def esperado_en_caja(recaudo, tarea=None):
    """**Cuánta plata tenía que traer el conductor** de una parada que se cobra:
    `(valor, fuente)`. La usa la reconciliación (validación e2e 2026-09-26: la
    columna «debían cobrarse» sumaba la factura completa y una PARCIAL con su
    nota crédito, o una retención confirmada, salían como plata no cobrada).

    - La factura (`valor_factura`), menos la retención que procede (confirmada
      o sin decidir: `monto_descuento`; rechazada: nada), menos, en una
      PARCIAL, lo que volvió valorizado a precio de factura (lo DECLARADO por
      el conductor × `valor_unitario` de su línea amarrada).
    - Sin factura, o una PARCIAL cuyo devuelto no se puede valorizar: lo que el
      conductor tenía para cobrar (`monto_cobrado + monto_descuento`), con
      fuente `DECLARADO` — no se inventa un faltante (Regla 0).
    """
    from app.models.recaudo_entrega import EstadoEntrega
    t = tarea if tarea is not None else getattr(recaudo, 'tarea', None)
    valor = getattr(t, 'valor_factura', None) if t is not None else None
    descuento = float(recaudo.monto_descuento or 0)
    retencion = 0.0 if decision_retencion(recaudo) == RECHAZADA else descuento
    declarado = round(float(recaudo.monto_cobrado or 0) + descuento, 2)
    if valor is None:
        return declarado, 'DECLARADO'
    if recaudo.estado_entrega != EstadoEntrega.PARCIAL:
        return round(float(valor) - retencion, 2), 'FACTURA'
    devuelto = _devuelto_valorizado(recaudo)
    if devuelto is None:
        return declarado, 'DECLARADO'
    return round(float(valor) - devuelto - retencion, 2), 'FACTURA_MENOS_DEVUELTO'


def _devuelto_valorizado(recaudo):
    """Lo que el conductor declaró de vuelta en una PARCIAL, a precio de
    factura; `None` si alguna línea no se puede valorizar."""
    rid = getattr(recaudo, 'id', None)
    if rid is None:
        return None
    from app.services.devolucion_ruta import devolucion_vigente
    dev = devolucion_vigente(rid)
    if dev is None or ((dev.declaracion_conductor or {}).get('declarado_por_producto')):
        return None
    total = 0.0
    for ln in dev.lineas:
        q = ln.cantidad_declarada if ln.cantidad_declarada is not None else None
        if q is None or float(q) <= 0:
            continue
        if ln.valor_unitario is None:
            return None
        total += float(q) * float(ln.valor_unitario)
    return round(total, 2)


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
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    # Vivo o ya hecho. FALLIDO y DESCARTADO no congelan: con la guarda abajo no
    # hay documento que proteger.
    vivo = SiesaJob.query.filter(
        SiesaJob.tipo.in_(('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET')),
        SiesaJob.referencia_tipo == 'RecaudoEntrega',
        SiesaJob.referencia_id == recaudo.id,
        SiesaJob.estado.in_(list(EstadoSiesaJob.ACTIVOS) + [EstadoSiesaJob.COMPLETADO]),
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


def completado_confirma(job) -> bool:
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
            if completado_confirma(j):
                ok.add(j.referencia_id)
    return ok


def rc_llego_a_siesa(recaudo) -> bool:
    return recaudo is not None and recaudo.id in rc_llegaron([recaudo])


def resultado_dc(recaudo, cuenta_puc):
    """El desenlace anotado de la retención de `cuenta_puc` (o `None`). Lo
    guarda el modelo (`RecaudoEntrega._dc_resultado`)."""
    return recaudo._dc_resultado(cuenta_puc)


def dc_llego(recaudo, cuenta_puc) -> bool:
    """¿La retención de esa cuenta **se sabe** que entró? Señal positiva, como
    `rc_llego_a_siesa`: la marca de pre-envío sola no lo dice."""
    return resultado_dc(recaudo, cuenta_puc) in LLEGO


def nc_llego(recaudo) -> bool:
    """¿La nota crédito de la parada se sabe que entró?"""
    return getattr(recaudo, 'siesa_nc_resultado', None) in LLEGO


# ═════════════════════════════════════════════════════════════════════════════
# 6 · ¿Qué documento de esta parada falta mandar?
# ═════════════════════════════════════════════════════════════════════════════

#: Desenlaces de `dc_pendiente`.
DC_LISTO = 'LISTO'
DC_ESPERA_DEVOLUCION = 'ESPERA_DEVOLUCION'


def _pucs_marcadas(recaudo) -> set:
    try:
        return set(json.loads(getattr(recaudo, 'siesa_dc_pucs', None) or '[]'))
    except (ValueError, TypeError):
        return {'__ILEGIBLE__'}


def dc_pendiente(recaudo, tarea=None):
    """¿Falta el documento de la retención de esta parada? **La única** que lo
    contesta (validación de la plata, 2026-09-26): antes nadie lo sabía — la
    retención CONFIRMADA cuyo DC no se encoló (Siesa no respondió al leer la
    factura, o una PARCIAL sin la devolución amarrada) no tenía botón ni aviso.

    `None` = no falta (sin retención confirmada, ya enviada o en cola, o la
    parada no va a caja: crédito). `DC_LISTO` = «Enviar a Siesa» la produce
    hoy. `DC_ESPERA_DEVOLUCION` = una PARCIAL cuya devolución no está amarrada
    ni contada: sin base todavía (tanda 2 · D, se queda así)."""
    from app.models.recaudo_entrega import EstadoEntrega
    if recaudo is None or decision_retencion(recaudo) != CONFIRMADA:
        return None
    if recaudo.estado_entrega not in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
        return None
    from app.services.liquidacion_service import RETENCION_PUC, _hay_rc_en_cola, _pucs_en_cola
    puc = RETENCION_PUC.get(recaudo.motivo_descuento)
    if not puc:
        return None
    rid = getattr(recaudo, 'id', None)
    if puc in _pucs_marcadas(recaudo) or '__ILEGIBLE__' in _pucs_marcadas(recaudo):
        return None
    if rid is not None and puc in _pucs_en_cola(rid):
        return None
    # La retención va detrás del recibo: solo en las paradas que van a caja.
    rc_vivo = bool(recaudo.siesa_rc_triggered) or (rid is not None and _hay_rc_en_cola(rid))
    if not rc_vivo:
        from app.services import cond_pago as _cp
        if _cp.trato_de_cobro(recaudo, tarea if tarea is not None else recaudo.tarea) \
                != _cp.TRATO_CONTADO:
            return None
    if recaudo.estado_entrega == EstadoEntrega.PARCIAL and rid is not None \
            and retencion_espera_devolucion(recaudo):
        return DC_ESPERA_DEVOLUCION
    return DC_LISTO


def rc_ya_salio_o_en_cola(recaudo) -> bool:
    """El recibo de la parada ya se envió o está en la cola."""
    from app.services.liquidacion_service import _hay_rc_en_cola
    rid = getattr(recaudo, 'id', None)
    return bool(recaudo.siesa_rc_triggered) or (rid is not None and _hay_rc_en_cola(rid))


def retencion_espera_devolucion(recaudo) -> bool:
    """¿La base de la retención de esta PARCIAL depende de una devolución que
    todavía no está amarrada a la factura ni contada? La misma condición con
    que `base_retencion_entregada` devuelve `None` antes de mirar líneas."""
    from app.models.devolucion_cliente import EstadoDevolucionCliente as E
    from app.services.devolucion_ruta import devolucion_vigente
    dev = devolucion_vigente(recaudo.id)
    return dev is None or (dev.vinculada_factura_at is None and dev.estado not in E.CONTADAS)


def documentos_pendientes(recaudo, tarea=None) -> list:
    """Los documentos que «Enviar a Siesa» produciría hoy para esta parada:
    `['NC']`, `['RC']`, `['DC']` o varios, o `[]`. La pantalla no lo
    recalcula: una devolución contada en cero (FALTANTE_TOTAL) no va a tener
    NC, y el botón quedaba visible para siempre invitando a mandar algo que no
    existe. La retención que falta (`dc_pendiente`) entra desde el 2026-09-26."""
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
    if dc_pendiente(recaudo, tarea) == DC_LISTO:
        faltan.append('DC')
    return faltan


def retenciones_sin_documento(ahora=None) -> list:
    """Paradas con la retención CONFIRMADA y sin su documento, en rutas desde
    el corte, con `{ruta_id, pedido, cliente, conductor, retencion, estado,
    espera_devolucion}`. Para el resumen diario (`lineas_de_aviso_dc`)."""
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import corte
    q = RecaudoEntrega.query.filter(RecaudoEntrega.retencion_confirmada.is_(True))
    ini = corte.inicio_auditoria()
    if ini is not None:
        q = q.filter(RecaudoEntrega.fecha_confirmacion >= ini)
    out = []
    for r in q.all():
        # Antes del recibo, la retención que espera es lo normal: el aviso es
        # para la que se quedó atrás de un RC que ya salió.
        if not rc_ya_salio_o_en_cola(r):
            continue
        d = dc_pendiente(r)
        if d is None:
            continue
        ruta = r.ruta
        out.append({'ruta_id': r.ruta_id, 'recaudo_id': r.id,
                    'pedido': getattr(r.tarea, 'numero_pedido_siesa', None),
                    'cliente': getattr(r.tarea, 'cliente', None),
                    'conductor': getattr(getattr(ruta, 'conductor', None), 'nombre', None),
                    'retencion': r.motivo_descuento, 'estado': d,
                    'espera_devolucion': d == DC_ESPERA_DEVOLUCION})
    return out


def lineas_de_aviso_dc() -> list:
    """El resumen diario: retenciones confirmadas cuyo documento no salió."""
    listas = [x for x in retenciones_sin_documento() if not x['espera_devolucion']]
    if not listas:
        return []
    det = '; '.join(f"ruta {x['ruta_id']} · {x['pedido'] or '—'} · {x['conductor'] or 'sin conductor'}"
                    for x in listas[:10])
    return [f'⚠ {len(listas)} retención(es) confirmada(s) sin su documento en Siesa ({det}): '
            f'desde Liquidación, «Enviar todo a Siesa» de la ruta.']


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Las fechas del recibo de caja (142888) — regla del dueño, 2026-09-25
# ═════════════════════════════════════════════════════════════════════════════

def momento_del_cobro(recaudo):
    """**El momento del cobro** (UTC naive) de una parada, para el recibo de
    caja: `fecha_cobro` (m050plata); una parada anterior, la primera
    confirmación. `None` sin ninguna (el recibo va con el día del envío,
    declarado por `fechas_del_recibo`)."""
    if recaudo is None:
        return None
    return getattr(recaudo, 'fecha_cobro', None) or getattr(recaudo, 'fecha_confirmacion', None)


def fecha_cobro_de_la_confirmacion(ts_dispositivo, desfase_s, ahora):
    """La hora real del cobro que dice el teléfono: `ts_dispositivo` corregido
    por el desfase medido al enviar (teléfono − servidor). Sin hora del
    teléfono, `ahora` (el servidor). Nunca en el futuro."""
    if ts_dispositivo is None:
        return ahora
    from datetime import timedelta
    real = ts_dispositivo - timedelta(seconds=int(desfase_s or 0))
    return min(real, ahora)


def fecha_cobro_declarada(valor, ahora):
    """La fecha del cobro que declara la oficina (`AAAA-MM-DD`, Bogotá), como
    el mediodía de ese día en UTC. `None` si no vino. Levanta `ValueError` si
    es ilegible o futura."""
    if valor in (None, ''):
        return None
    from datetime import date, datetime as _dt, time
    from app.utils.fecha import TZ_BOGOTA, dia_operativo_de
    try:
        d = date.fromisoformat(str(valor)[:10])
    except ValueError as e:
        raise ValueError(f'La fecha del cobro es ilegible: {valor!r} (se espera AAAA-MM-DD)') from e
    if d > dia_operativo_de(ahora):
        raise ValueError('La fecha del cobro no puede ser futura')
    from zoneinfo import ZoneInfo
    local = _dt.combine(d, time(12, 0), tzinfo=TZ_BOGOTA)
    return local.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)


def _yyyymmdd(valor) -> str | None:
    v = str(valor or '').strip()
    if len(v) != 8 or not v.isdigit():
        return None
    try:
        from datetime import date
        date(int(v[:4]), int(v[4:6]), int(v[6:]))
    except ValueError:
        return None
    return v


def fechas_del_recibo(fecha_cobro, fecha_documento: str) -> dict:
    """**La única** que decide las fechas de un recibo de caja.

    - `F350_FECHA` = `fecha_documento`: el día (Bogotá) en que se envía.
    - `F357_FECHA_RECAUDO` = el día real del cobro **si cae en el mismo mes**
      que el documento. Si el mes cambió (ruta del 30 liquidada el 2), el
      recaudo no puede registrarse en un período que ya cerró: va con la fecha
      del documento y **el día real queda escrito en las notas** del recibo.
    - `F358_FECHA_CONSIGNACION` = **siempre** el día real del cobro (es la
      fecha del banco: el extracto se cruza con ella).

    `fecha_cobro` ilegible o ausente (parada sin confirmación registrada):
    todo va con el día del documento y se declara (`sin_fecha_cobro`). Un
    cobro «posterior» al documento (reloj corrido) tampoco se registra en el
    futuro: F357 va con el documento.

    Devuelve `{f350, f357, f358, fecha_cobro, cruza_mes, sin_fecha_cobro,
    nota}`; `nota` es `None` salvo que el mes haya cambiado.

    **No probado contra Siesa real** (CLAUDE.md, «La fecha del recibo de
    caja»): la prueba de fin de mes en QA está escrita allá.
    """
    doc = _yyyymmdd(fecha_documento)
    if doc is None:
        raise ValueError(f'fecha_documento ilegible: {fecha_documento!r} (se espera YYYYMMDD)')
    cobro = _yyyymmdd(fecha_cobro)
    real = cobro or doc
    cruza_mes = real[:6] != doc[:6]
    f357 = real if (not cruza_mes and real <= doc) else doc
    nota = None
    if cruza_mes:
        nota = (f'Cobrado por el conductor el {real[6:]}/{real[4:6]}/{real[:4]}; el recaudo '
                f'se registra el {doc[6:]}/{doc[4:6]}/{doc[:4]} porque el mes del cobro '
                f'ya cambió.')
    return {'f350': doc, 'f357': f357, 'f358': real, 'fecha_cobro': real,
            'cruza_mes': cruza_mes, 'sin_fecha_cobro': cobro is None, 'nota': nota}

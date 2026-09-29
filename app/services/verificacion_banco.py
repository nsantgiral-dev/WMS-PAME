"""
Los pagos bancarios de la ruta, vistos en el banco (m051liqcaja, 2026-09-27).

**El defecto:** una transferencia o consignación se daba por pagada con la
palabra del conductor. El teléfono capturaba referencia y foto del comprobante,
pero ninguna pantalla mostraba la foto, y el recibo de caja salía con medio
bancario apenas se encolaba. Un cliente de contado contraentrega que muestra un
pantallazo falso cierra su factura en Siesa, la compuerta de cartera deja de
frenarlo, y la pérdida aparece en la conciliación bancaria semanas después.

**La clase:** *se da por recibida plata que nadie vio llegar.* El efectivo lo
ve quien recibe la caja (`caja_conductor`); lo bancario, quien mira el extracto
(acá).

| Pregunta | Función |
|---|---|
| ¿Este medio se verifica contra el banco? | `es_bancaria` |
| ¿Está encendida la exigencia? | `exige_verificacion` |
| ¿En qué quedó esta parada? | `estado` |
| ¿El recibo de caja puede salir? | `exigir_para_rc` (lo llama el ejecutor del RC) |
| Declarar lo que se vio en el banco | `verificar` |
| La cola de «Transferencias por verificar» | `por_verificar` |
| La foto del comprobante, por parada | `comprobante` |
| El aviso del resumen diario | `lineas_de_aviso` |

El switch `TRANSFERENCIA_EXIGE_VERIFICACION` (default encendido) apaga la
espera del recibo sin apagar la pantalla: con la variable en `false`, lo que
cambia es que el RC ya no espera.

**La tarjeta no entra acá:** el voucher del datáfono lo liquida la adquirente,
no un extracto que alguien concilie a mano. Si el dueño decide lo contrario,
es una línea en `es_bancaria`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

VERIFICADA = 'VERIFICADA'
NO_ENCONTRADA = 'NO_ENCONTRADA'
#: Estado de una parada bancaria que nadie miró todavía (no se guarda: es NULL).
POR_VERIFICAR = 'POR_VERIFICAR'

#: Cuánto espera el recibo antes de volver a preguntar (la DLQ reprograma sin
#: gastar reintento). Al verificar, `verificar` lo despierta de una vez.
ESPERA_MINUTOS = 60

#: El resumen diario avisa las que llevan más de esto sin mirar.
HORAS_AVISO = 24

_ENV = 'TRANSFERENCIA_EXIGE_VERIFICACION'


def exige_verificacion() -> bool:
    """¿El recibo de caja de un pago bancario espera la verificación? Default
    **sí** (decisión por defecto del dueño, 2026-09-27). Solo un `false`
    explícito la apaga: una variable mal escrita no suelta recibos sin mirar
    (Regla 0)."""
    valor = (os.environ.get(_ENV) or '').strip().lower()
    return valor not in ('false', '0', 'no', 'off')


def es_bancaria(forma_pago) -> bool:
    """Transferencia (cualquier banco) o consignación: la plata que llega a una
    cuenta y que solo el extracto confirma."""
    fp = (forma_pago or '').strip().upper()
    return fp.startswith('TRANSFERENCIA') or fp == 'CONSIGNACION'


def estado(recaudo):
    """`None` (no es un pago bancario, o no hubo cobro) · `POR_VERIFICAR` ·
    `VERIFICADA` · `NO_ENCONTRADA`."""
    from app.models.recaudo_entrega import forma_pago_de
    if recaudo is None:
        return None
    fp = forma_pago_de(recaudo.estado_entrega, recaudo.forma_pago)
    if not es_bancaria(fp) or float(recaudo.monto_cobrado or 0) <= 0:
        return None
    return recaudo.verificado_banco_resultado or POR_VERIFICAR


def _clases_de_la_dlq():
    """Las excepciones con que la DLQ distingue esperar de fallar. Se importan
    tarde: este módulo no puede depender de la DLQ al cargar."""
    from app.services.siesa_job_service import DatoQueFalta, DependenciaPendiente
    return DependenciaPendiente, DatoQueFalta


def exigir_para_rc(recaudo) -> None:
    """**La compuerta del recibo de caja bancario.** La llama el ejecutor del
    RC antes del POST:

    - por verificar → `DependenciaPendiente` (esperar no gasta reintento);
    - no encontrada en el banco → `DatoQueFalta` (un `ErrorDeterminista`): el
      recibo no sale, la factura queda con su saldo y cartera la gestiona.
      Reintentar no la arregla — lo que la arregla es que la plata aparezca,
      y entonces `verificar(..., encontrada=True)` despierta el recibo;
    - verificada, no bancaria o con la exigencia apagada → nada.
    """
    e = estado(recaudo)
    if e is None or e == VERIFICADA:
        return
    DependenciaPendiente, DatoQueFalta = _clases_de_la_dlq()
    pedido = getattr(getattr(recaudo, 'tarea', None), 'numero_pedido_siesa', None) or f'parada {recaudo.id}'
    if e == NO_ENCONTRADA:
        raise DatoQueFalta(
            f'La transferencia de {pedido} no apareció en el banco '
            f'({recaudo.verificado_banco_nota or "sin nota"}): el recibo de caja no se '
            f'envía y la factura queda con su saldo en cartera. Si la plata aparece, '
            f'márquela vista en Liquidación → Transferencias y el recibo sale solo.')
    if exige_verificacion():
        raise DependenciaPendiente(
            f'El recibo de caja de {pedido} no sale hasta que alguien vea la transferencia '
            f'en el banco (Liquidación → Transferencias).', espera_minutos=ESPERA_MINUTOS)


def verificar(recaudo_id: int, usuario_id: int, encontrada: bool, nota: str = None) -> dict:
    """Declara lo que se vio en el banco, con autor y hora. **La única que
    escribe `verificado_banco_*`.**

    - «No apareció» exige nota (qué se buscó): es la que frena el recibo.
    - Cambiar lo declarado se puede mientras el recibo no haya salido; queda
      en la bitácora con el antes y el después.
    - Al verificar, el recibo que esperaba se despierta de una vez.
    """
    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services.bitacora import foto as foto_fila, motivo_obligatorio, registrar_accion

    r = db.session.get(RecaudoEntrega, recaudo_id)
    if r is None:
        raise LookupError('Parada no encontrada')
    if estado(r) is None:
        raise ValueError('Esta parada no se pagó por transferencia ni consignación: no hay '
                         'nada que verificar en el banco.')
    if r.siesa_rc_triggered and r.verificado_banco_resultado:
        raise ValueError('El recibo de caja de esta parada ya salió a Siesa: lo declarado '
                         'sobre el banco no se cambia.')
    nota = (nota or '').strip() or None
    if not encontrada:
        nota = motivo_obligatorio(nota, 'declarar que la transferencia no apareció en el banco')
    campos = ['verificado_banco_resultado', 'verificado_banco_por', 'verificado_banco_en',
              'verificado_banco_nota']
    antes = foto_fila(r, campos)
    r.verificado_banco_resultado = VERIFICADA if encontrada else NO_ENCONTRADA
    r.verificado_banco_por = usuario_id
    r.verificado_banco_en = datetime.utcnow()
    r.verificado_banco_nota = nota
    registrar_accion('EDITAR', r, usuario_id=usuario_id,
                     motivo=nota or 'Transferencia vista en el banco',
                     entidad_codigo=f'RECAUDO-{r.id}', antes=antes,
                     despues=foto_fila(r, campos))
    if encontrada:
        _despertar_recibo(r.id, usuario_id)
    db.session.commit()
    return r.to_dict()


def _despertar_recibo(recaudo_id: int, usuario_id: int = None) -> None:
    """El RC que esperaba la verificación sale en el próximo ciclo, no dentro de
    una hora. Y el que quedó FALLIDO porque la transferencia «no apareció»
    (`exigir_para_rc`) vuelve a la cola: lo único que lo frenaba era eso, y la
    bandera de pre-envío sigue abajo (nunca salió un POST).

    **Con él, la retención que cayó detrás** (validación 2026-09-29, P2): con
    el recibo fuera de la cola, el DC se declara «espera el recibo de caja… y
    ese recibo no está en la cola»; si el recibo vuelve, el DC también —si no,
    quedaba esperando que alguien lo reintentara a mano."""
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    from app.extensions import db
    from app.services.siesa_job_service import reencolar_job_fallido
    r = db.session.get(RecaudoEntrega, recaudo_id)
    ahora = datetime.utcnow()
    rc_de_vuelta = False
    for j in SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_tipo='RecaudoEntrega',
                                      referencia_id=recaudo_id).all():
        if j.estado == EstadoSiesaJob.PENDIENTE:
            j.proximo_intento = ahora
        elif (j.estado == EstadoSiesaJob.FALLIDO and r is not None and not r.siesa_rc_triggered
              and 'no apareció en el banco' in (j.error_ultimo or '')):
            reencolar_job_fallido(j, usuario_id=usuario_id,
                                  motivo='La transferencia apareció en el banco',
                                  origen='verificacion_banco')
            rc_de_vuelta = True
    for j in SiesaJob.query.filter_by(tipo='DOCUMENTO_CONTABLE_RET',
                                      referencia_tipo='RecaudoEntrega',
                                      referencia_id=recaudo_id).all():
        if j.estado == EstadoSiesaJob.PENDIENTE:
            j.proximo_intento = ahora            # esperaba 30 min por el banco
        elif (rc_de_vuelta and j.estado == EstadoSiesaJob.FALLIDO
                and 'ese recibo no está en la cola' in (j.error_ultimo or '')):
            reencolar_job_fallido(j, usuario_id=usuario_id,
                                  motivo='El recibo de caja que esperaba volvió a la cola '
                                         '(la transferencia apareció en el banco)',
                                  origen='verificacion_banco')


def _fila(r, ahora) -> dict:
    t = r.tarea
    ruta = r.ruta
    desde = r.fecha_cobro or r.fecha_confirmacion
    return {
        'recaudo_id': r.id,
        'ruta_id': r.ruta_id,
        'conductor': getattr(getattr(ruta, 'conductor', None), 'nombre', None),
        'pedido': getattr(t, 'numero_pedido_siesa', None),
        'factura': (f'{t.fe_tipo}-{t.fe_consec}' if t is not None and t.fe_consec else None),
        'cliente': getattr(t, 'cliente', None),
        'forma_pago': r.forma_pago,
        'monto': float(r.monto_cobrado or 0),
        'referencia': r.referencia_pago,
        'fecha_cobro': desde.isoformat() if desde else None,
        'horas': (round((ahora - desde).total_seconds() / 3600, 1) if desde else None),
        'tiene_foto': bool(r.foto_comprobante),
        'estado': estado(r),
        'verificado_banco_nota': r.verificado_banco_nota,
        'verificado_banco_en': r.verificado_banco_en.isoformat() if r.verificado_banco_en else None,
        'rc_salio': bool(r.siesa_rc_triggered),
    }


def por_verificar(incluir_no_encontradas: bool = True, limite: int = 300) -> list:
    """La cola de quien concilia el banco: pagos bancarios sin mirar (y los que
    no aparecieron, que siguen siendo trabajo), los más viejos primero. **Sin
    la foto**: la trae `comprobante`, una por una."""
    from sqlalchemy import or_
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import corte
    ahora = datetime.utcnow()
    q = RecaudoEntrega.query.filter(
        or_(RecaudoEntrega.forma_pago.like('TRANSFERENCIA%'),
            RecaudoEntrega.forma_pago == 'CONSIGNACION'),
        RecaudoEntrega.monto_cobrado > 0)
    if incluir_no_encontradas:
        q = q.filter(or_(RecaudoEntrega.verificado_banco_resultado.is_(None),
                         RecaudoEntrega.verificado_banco_resultado == NO_ENCONTRADA))
    else:
        q = q.filter(RecaudoEntrega.verificado_banco_resultado.is_(None))
    ini = corte.inicio_auditoria()
    if ini is not None:
        q = q.filter(RecaudoEntrega.fecha_confirmacion >= ini)
    filas = [_fila(r, ahora) for r in q.order_by(RecaudoEntrega.fecha_confirmacion).limit(limite)
             if estado(r) in (POR_VERIFICAR, NO_ENCONTRADA)]
    return filas


def comprobante(recaudo_id: int) -> dict:
    """La foto del comprobante de una parada, para mostrarla. `foto` es `None`
    si el conductor no la tomó (se dice, no se inventa)."""
    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    r = db.session.get(RecaudoEntrega, recaudo_id)
    if r is None:
        raise LookupError('Parada no encontrada')
    foto = r.foto_comprobante or None
    if foto and not str(foto).startswith('data:'):
        foto = 'data:image/jpeg;base64,' + str(foto)
    return {**_fila(r, datetime.utcnow()), 'foto': foto}


def lineas_de_aviso(ahora=None) -> list:
    """El resumen diario: pagos bancarios sin verificar hace más de un día, y
    los que no aparecieron en el banco (la factura sigue abierta)."""
    ahora = ahora or datetime.utcnow()
    lista = por_verificar()
    viejas = [f for f in lista if f['estado'] == POR_VERIFICAR
              and f['horas'] is not None and f['horas'] > HORAS_AVISO]
    no_estan = [f for f in lista if f['estado'] == NO_ENCONTRADA]
    lineas = []
    if viejas:
        total = sum(f['monto'] for f in viejas)
        lineas.append(
            f'⚠ {len(viejas)} transferencia(s) sin verificar en el banco hace más de '
            f'{HORAS_AVISO} h (${total:,.0f}): su recibo de caja espera. Liquidación → '
            'Transferencias.')
    if no_estan:
        det = ', '.join(f"{f['pedido'] or '—'} (${f['monto']:,.0f})" for f in no_estan[:10])
        lineas.append(f'🚨 {len(no_estan)} transferencia(s) que NO aparecieron en el banco: '
                      f'{det}. La factura sigue abierta; cartera la gestiona.')
    return lineas

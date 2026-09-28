"""
Lo que el ejecutor de la DLQ resuelve **justo antes** de mandar a Siesa un
documento de la liquidación: un recibo de caja (142888) o una retención
(142882). **Una política, una función.**

## Por qué vive acá y no en quien liquida (auditoría de liquidación, 2026-09-27)

«Enviar todo a Siesa» consultaba Siesa en serie **dentro del request**: por cada
parada la factura, la cabecera del pedido (dos veces) y la cartera del cliente.
Medido en producción: 2–3 s por parada de contado; una ruta de diez paradas se
comía los 25 s del PWA, que cortaba con «intente de nuevo» mientras el servidor
seguía encolando — y el segundo clic corría otra pasada encima (P1-1).

Y si Siesa titubeaba en ese momento, el job salía **envenenado**: la cabecera
fallida dejaba `tercero_nit=''`, y la cartera fallida dejaba la UN vacía, que el
gateway rellenaba con `SIESA_UNIDAD_NEGOCIO` (`001` en producción, cuando la
cartera real es `99` en el 100 % de las filas medidas). Rechazo determinista, y
«Reintentar» reenviaba el mismo payload cinco veces (P1-2; en QA, dos
retenciones FALLIDO 38 días con «la U.N. del auxiliar no es el mismo del
movimiento»).

Ahora encolar no toca Siesa: el job lleva el recaudo y lo que el WMS ya sabe.
Esto resuelve —con Siesa en la mano y sin apuro— **de una sola fila de
cartera**, la del documento exacto (CO + tipo + consecutivo, Regla 18):

    tercero (f200_id) · sucursal (f201_id_sucursal) · cuenta (f253_id)
    unidad de negocio (f353_id_un_cruce) · CO (f353_id_co_cruce) · saldo

Si no puede, **el envío espera** (`DependenciaPendiente`: no gasta intento) y lo
dice; pasado el tope, se declara con lo que falta y quién lo pone. Nunca sale un
POST con un dato de respaldo. Es también la decisión del dueño «Siesa caído =
se para todo»: nada se emite a ciegas.

Trinquete: `tests/test_envio_liquidacion.py`.
"""
import logging
import os
from collections import namedtuple
from datetime import datetime

from app.extensions import db

logger = logging.getLogger(__name__)

#: Minutos entre lecturas mientras la cartera no muestra la factura o Siesa no
#: responde. La cartera tarda minutos en indexar una FE recién creada
#: (CLAUDE.md, «RESUELTO 2026-09-04»).
ESPERA_ENTRE_LECTURAS_MIN = 10

#: Tope de espera por defecto (horas desde que se encoló el envío).
TOPE_ESPERA_HORAS_DEFECTO = 24.0

#: Las lecturas de verificación de un recibo que quedó sin respuesta clara,
#: en minutos después del intento (P1-3): la Regla 20 son 10–12 s, pero la
#: cartera tarda minutos en indexar, y una lectura en el mismo `except` del POST
#: casi siempre contesta «no sé».
PLAN_VERIFICACION_MIN = (15, 120)

#: Desde cuántos minutos después del intento una lectura de saldo intacto sirve
#: como evidencia de que el recibo NO entró (la indexación ya pasó).
MINUTOS_PARA_EVIDENCIA_NO_ENTRO = 15


def tope_espera_horas() -> float:
    """Cuánto puede esperar un envío de la liquidación a que Siesa conteste o
    a que la cartera muestre la factura antes de declararlo (`LIQUIDACION_
    ESPERA_SIESA_HORAS`, default 24: cubre una noche). Un valor ilegible usa
    el default y lo dice en el log."""
    crudo = os.environ.get('LIQUIDACION_ESPERA_SIESA_HORAS', '')
    if not str(crudo).strip():
        return TOPE_ESPERA_HORAS_DEFECTO
    try:
        valor = float(crudo)
        if valor > 0:
            return valor
    except (TypeError, ValueError):
        pass
    logger.warning('[ENVIO_LIQ] LIQUIDACION_ESPERA_SIESA_HORAS=%r ilegible — se usan %s h',
                   crudo, TOPE_ESPERA_HORAS_DEFECTO)
    return TOPE_ESPERA_HORAS_DEFECTO


#: La fila de cartera resuelta. `por` dice por qué documento apareció.
Cruce = namedtuple('Cruce', 'nit sucursal cuenta un co tipo consec saldo total_cr por')


class CarteraSinFila(Exception):
    """La cartera de Siesa no tiene (todavía) la fila del documento."""


class CarteraNoDisponible(Exception):
    """No se pudo leer la cartera (red, circuito, rechazo de la consulta)."""


class CruceInvalido(Exception):
    """La fila existe pero no sirve para cruzar (ambigua o sin sus datos).
    No se arregla esperando."""


def _gateway(gateway=None):
    if gateway is not None:
        return gateway
    from app.services.connekta_gateway import connekta
    return connekta


def _txt(v) -> str:
    return str(v if v is not None else '').strip()


def _documentos_candidatos(tarea, tipo_fe, consec_fe):
    """Los documentos por los que puede estar indexada la cartera: el PEDIDO
    primero y la FACTURA después — el orden de `cxc_cruce.fila_de_la_factura`
    (la política), con el CO como parte de la llave."""
    out = []
    tp, cp = _txt(getattr(tarea, 'tipo_docto_pedido_siesa', None)), \
        _txt(getattr(tarea, 'consec_docto_pedido_siesa', None))
    if tp and cp.isdigit():
        out.append((tp, cp, 'PEDIDO'))
    if _txt(tipo_fe) and _txt(consec_fe).isdigit():
        out.append((_txt(tipo_fe), _txt(consec_fe), 'FACTURA'))
    return out


def leer_cruce(tarea, tipo_fe, consec_fe, co, gateway=None) -> Cruce:
    """La fila de cartera del documento, leída **exacta** (CO + tipo +
    consecutivo). Levanta `CarteraNoDisponible` (no se pudo leer),
    `CarteraSinFila` (no está) o `CruceInvalido` (ambigua o incompleta).

    En simulación no hay cartera: devuelve un doble **distinguible** del dato
    real (`SIM…`, regla 8 de flota), después de intentar de verdad."""
    from app.services import cxc_cruce
    gw = _gateway(gateway)
    co = _txt(co)
    if not co:
        raise CruceInvalido('no se sabe en qué centro de operación está la factura')
    docs = _documentos_candidatos(tarea, tipo_fe, consec_fe)
    if not docs:
        raise CruceInvalido('la parada no tiene pedido ni factura con los que buscar la cartera')
    fila = por = None
    for tipo, consec, cual in docs:
        try:
            filas = gw.get_cxc_de_factura(co, tipo, consec)
        except Exception as e:                   # noqa: BLE001 — «no sé» se declara
            raise CarteraNoDisponible(f'no se pudo leer la cartera de {co}-{tipo}-{consec}: {e}') from e
        if not isinstance(filas, list):
            raise CarteraNoDisponible(f'la lectura de la cartera de {co}-{tipo}-{consec} no devolvió filas')
        try:
            fila = cxc_cruce.fila_unica(filas, co, tipo, consec)
        except cxc_cruce.FilaAmbigua as e:
            raise CruceInvalido(str(e)) from e
        if fila is not None:
            por = cual
            break
    if fila is None:
        if _en_simulacion(gw):
            tipo, consec, _ = docs[-1]
            return Cruce('SIMNIT', '001', 'SIMCXC', 'SIM', co, tipo, consec, None, 0.0, 'SIMULACION')
        raise CarteraSinFila(
            ' ni '.join(f'{co}-{t}-{c}' for t, c, _ in docs) + ' no aparece en la cartera')
    faltan = [nombre for nombre, campo in (('el tercero (f200_id)', 'f200_id'),
                                           ('la cuenta (f253_id)', 'f253_id'),
                                           ('la unidad de negocio (f353_id_un_cruce)',
                                            'f353_id_un_cruce'))
              if not _txt(fila.get(campo))]
    if faltan:
        raise CruceInvalido(f'la fila de cartera de {co}-{fila.get("f353_id_tipo_docto_cruce")}-'
                            f'{fila.get("f353_consec_docto_cruce")} no trae ' + ', '.join(faltan))
    return Cruce(
        nit=_txt(fila.get('f200_id')),
        sucursal=_txt(fila.get('f201_id_sucursal')) or None,
        cuenta=_txt(fila.get('f253_id')),
        un=_txt(fila.get('f353_id_un_cruce')),
        co=_txt(fila.get('f353_id_co_cruce')),
        tipo=_txt(fila.get('f353_id_tipo_docto_cruce')),
        consec=_txt(fila.get('f353_consec_docto_cruce')),
        saldo=cxc_cruce.saldo_de_la_fila(fila),
        total_cr=float(fila.get('f353_total_cr', 0) or 0),
        por=por,
    )


def co_de_la_factura(tarea, gateway=None):
    """El CO de la factura de la tarea (`cxc_cruce.co_de_la_factura`)."""
    from app.services import cxc_cruce
    gw = _gateway(gateway)
    return cxc_cruce.co_de_la_factura(tarea, getattr(gw, 'centro_op', None))


# ═════════════════════════════════════════════════════════════════════════════
# Cómo se nombra el envío en un mensaje: el pedido, la factura, el cliente
# ═════════════════════════════════════════════════════════════════════════════

def _pedido(tarea) -> str:
    if tarea is None:
        return 'la parada'
    num = _txt(getattr(tarea, 'numero_pedido_siesa', None))
    if num:
        return f'el pedido {num}'
    tp, cp = _txt(tarea.tipo_docto_pedido_siesa), _txt(tarea.consec_docto_pedido_siesa)
    return f'el pedido {tp}{cp}' if tp and cp else f'la tarea {tarea.id}'


def que_es(tarea, tipo_fe=None, consec_fe=None, co=None) -> str:
    """«el pedido PD1502 (factura FE-17062 del CO 003, cliente X)»."""
    partes = []
    if _txt(tipo_fe) and _txt(consec_fe):
        partes.append(f'factura {_txt(tipo_fe)}-{_txt(consec_fe)}' + (f' del CO {co}' if co else ''))
    cliente = _txt(getattr(tarea, 'cliente', None)) if tarea is not None else ''
    if cliente:
        partes.append(f'cliente {cliente}')
    return _pedido(tarea) + (f' ({", ".join(partes)})' if partes else '')


# ═════════════════════════════════════════════════════════════════════════════
# La resolución del envío
# ═════════════════════════════════════════════════════════════════════════════

def _esperar_o_declarar(job, que: str, motivo: str, quien: str):
    """Espera sin gastar intento; pasado el tope, lo declara (FALLIDO sin
    reintento, con qué falta y quién lo pone)."""
    from app.services.siesa_job_service import DatoQueFalta, DependenciaPendiente
    tope = tope_espera_horas()
    creado = getattr(job, 'fecha_creacion', None)
    horas = ((datetime.utcnow() - creado).total_seconds() / 3600) if creado else 0.0
    if horas >= tope:
        raise DatoQueFalta(
            f'{que}: {motivo}, y lleva {horas:.0f} h así (el tope es {tope:.0f} h). '
            f'{quien}')
    raise DependenciaPendiente(
        f'{que}: {motivo}. Se vuelve a intentar solo en {ESPERA_ENTRE_LECTURAS_MIN} min, '
        f'sin gastar intentos (tope {tope:.0f} h).',
        espera_minutos=ESPERA_ENTRE_LECTURAS_MIN)


def _factura(job, recaudo, payload, gateway):
    """`(tipo_fe, consec_fe)` del payload o, si al encolar no se sabía, de
    Siesa (`fe_resolver.resolver_fe`, que la deja anotada en la tarea)."""
    tipo_fe, consec_fe = _txt(payload.get('tipo_docto_fe')), _txt(payload.get('consec_fe'))
    if tipo_fe and consec_fe:
        return tipo_fe, consec_fe
    from app.services.fe_resolver import FENoEncontrada, resolver_fe
    tarea = recaudo.tarea
    try:
        return resolver_fe(tarea, gateway=gateway)
    except FENoEncontrada as e:
        _esperar_o_declarar(job, que_es(tarea), f'la factura todavía no aparece en Siesa ({e})',
                            'Revise en Siesa que el pedido se haya facturado.')
    except Exception as e:                       # noqa: BLE001
        _esperar_o_declarar(job, que_es(tarea), f'Siesa no respondió al buscar la factura ({e})',
                            'Si Siesa sigue sin responder, avísele a sistemas.')


def resolver_cruce(job, recaudo, payload, gateway=None):
    """`(campos, cruce)`: lo que el POST necesita de la cartera, resuelto de la
    fila del documento. Espera (`DependenciaPendiente`) o declara
    (`DatoQueFalta`) — nunca devuelve un campo vacío."""
    from app.services.siesa_job_service import DatoQueFalta
    gw = _gateway(gateway)
    tarea = recaudo.tarea
    tipo_fe, consec_fe = _factura(job, recaudo, payload, gw)
    co = co_de_la_factura(tarea, gw)
    que = que_es(tarea, tipo_fe, consec_fe, co)
    try:
        cruce = leer_cruce(tarea, tipo_fe, consec_fe, co, gw)
    except CarteraNoDisponible as e:
        _esperar_o_declarar(job, que, f'Siesa no respondió al leer la cartera ({e})',
                            'Si Siesa sigue sin responder, avísele a sistemas.')
    except CarteraSinFila:
        _esperar_o_declarar(
            job, que, 'la cartera de Siesa todavía no muestra la factura (la indexa en minutos)',
            'Revise en Siesa (Cuentas por cobrar) que la factura esté aprobada y a nombre del '
            'cliente; cuando aparezca, quien liquida pulsa «Enviar todo a Siesa».')
    except CruceInvalido as e:
        raise DatoQueFalta(
            f'{que}: {e}. No se envía con datos de respaldo. Lo revisa contabilidad en '
            f'Siesa (Cuentas por cobrar); cuando la fila esté bien, quien liquida pulsa '
            f'«Enviar todo a Siesa».') from e
    campos = {
        'tipo_docto_fe': tipo_fe,
        'consec_fe': str(consec_fe),
        'tercero_nit': cruce.nit,
        'sucursal': cruce.sucursal or _txt(payload.get('sucursal')) or '001',
        'cuenta_cxc': cruce.cuenta,
        'unidad_negocio': cruce.un,
        'co_factura': cruce.co or co,
        'cruce_por': cruce.por,
        'cruce_resuelto_en': datetime.utcnow().isoformat(timespec='seconds'),
    }
    return campos, cruce


def _lineas_de_la_factura(job, recaudo, tipo_fe, consec_fe, gateway):
    tarea = recaudo.tarea
    que = que_es(tarea, tipo_fe, consec_fe)
    try:
        lineas = gateway.get_rowids_factura(tipo_fe, consec_fe)
    except Exception as e:                       # noqa: BLE001
        _esperar_o_declarar(job, que, f'Siesa no respondió al leer la factura ({e})',
                            'Si Siesa sigue sin responder, avísele a sistemas.')
    if not lineas:
        if _en_simulacion(gateway):
            return []
        _esperar_o_declarar(job, que, 'Siesa todavía no devuelve las líneas de la factura',
                            'Revise en Siesa que la factura exista y esté aprobada.')
    return lineas


def _en_simulacion(gateway) -> bool:
    """Sin Siesa (sin credenciales). `is True` y no truthy: un `MagicMock` de
    los tests tiene todos los atributos, y el doble no puede disparar ahí."""
    return getattr(gateway, 'modo_simulacion', False) is True


def monto_del_recibo(job, recaudo, tipo_fe, consec_fe, gateway=None) -> float:
    """El monto del recibo de un job encolado sin monto (el botón masivo). La
    misma política que «Registrar cobro» (`politica_cobro.monto_rc`): en una
    PARCIAL lo declarado; en un ENTREGADO el neto de Siesa menos la retención
    que procede, y acotado contra lo que declaró el conductor
    (`_validar_diferencia_declarada`). Si no cuadra, se declara: no se manda
    un recibo por una cifra que nadie firmó."""
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import politica_cobro as pc
    from app.services.liquidacion_service import (_validar_diferencia_declarada,
                                                  monto_de_retencion)
    from app.services.siesa_job_service import DatoQueFalta
    if recaudo.estado_entrega == EstadoEntrega.PARCIAL:
        return pc.monto_rc(recaudo)
    gw = _gateway(gateway)
    lineas = _lineas_de_la_factura(job, recaudo, tipo_fe, consec_fe, gw)
    if not lineas:
        # Solo en simulación (sin Siesa no hay factura que leer ni recibo que
        # emitir de verdad): lo declarado.
        return pc.monto_rc(recaudo)
    neto = round(sum(float(ln.get('f470_vlr_neto', 0) or 0) for ln in lineas), 2)
    que = que_es(recaudo.tarea, tipo_fe, consec_fe)
    if neto <= 0:
        raise DatoQueFalta(f'{que}: la factura en Siesa suma ${neto:,.0f} — no hay contra qué '
                           f'emitir el recibo. Lo revisa contabilidad en Siesa.')
    retencion = 0.0
    if pc.decision_retencion(recaudo) == pc.CONFIRMADA:
        base = pc.base_retencion_entregada(recaudo, lineas)
        if base is not None:
            retencion = monto_de_retencion(recaudo.motivo_descuento, *base)
    monto = pc.monto_rc(recaudo, total_neto_siesa=neto, retencion=retencion)
    try:
        _validar_diferencia_declarada(
            float(recaudo.monto_cobrado or 0), monto,
            contexto=' (neto, después de descontar la retención)' if retencion else '')
    except ValueError as e:
        raise DatoQueFalta(
            f'{que}: {e} Lo corrige el líder de cartera («Corregir monto») o quien liquida '
            f'registra el cobro de la parada con el ajuste al peso; después, «Enviar todo '
            f'a Siesa».') from e
    return monto


def monto_de_la_retencion(job, recaudo, tipo_ret, tipo_fe, consec_fe, gateway=None):
    """`(monto, base_del_payload)` de una retención encolada sin monto: sobre
    lo que el cliente se quedó (`politica_cobro.base_retencion_entregada`),
    nunca sobre `monto_descuento` (un estimado del teléfono)."""
    from app.services import politica_cobro as pc
    from app.services.liquidacion_service import base_de_retencion, monto_de_retencion
    from app.services.siesa_job_service import DatoQueFalta
    gw = _gateway(gateway)
    lineas = _lineas_de_la_factura(job, recaudo, tipo_fe, consec_fe, gw)
    if not lineas:
        # Solo en simulación: sin factura, el estimado declarado (no sale a Siesa).
        return round(float(recaudo.monto_descuento or 0), 2), 0.0
    que = que_es(recaudo.tarea, tipo_fe, consec_fe)
    base = pc.base_retencion_entregada(recaudo, lineas)
    if base is None:
        _esperar_o_declarar(
            job, que, 'la entrega fue parcial y la devolución todavía no está amarrada a la '
                      'factura: sin saber qué se quedó el cliente no hay base para la retención',
            'Bodega cuenta la devolución en «Llegó el camión»; la retención sale sola después.')
    monto = monto_de_retencion(tipo_ret, *base)
    if monto <= 0:
        raise DatoQueFalta(f'{que}: la retención {tipo_ret} calculada sobre la factura da cero — '
                           f'no hay documento que emitir. Lo decide quien confirma retenciones.')
    return monto, base_de_retencion(tipo_ret, *base)


def resolver_envio(job, recaudo, gateway=None):
    """`(payload_resuelto, cruce)` de un RECIBO_CAJA o DOCUMENTO_CONTABLE_RET:
    **lo único que el ejecutor hace antes de armar el POST**. De la fila de
    cartera (`resolver_cruce`) el tercero, la sucursal, la cuenta, la UN y el
    CO; y el monto (y la base, en la retención) si el envío se encoló sin él.
    Espera o declara; nunca devuelve un campo vacío. No escribe el job: el
    ejecutor lo escribe junto con el pre-flag, en la misma transacción."""
    gw = _gateway(gateway)
    payload = dict(job.get_payload() or {})
    campos, cruce = resolver_cruce(job, recaudo, payload, gw)
    payload.update(campos)
    if payload.get('monto') is None:
        if job.tipo == 'RECIBO_CAJA':
            payload['monto'] = monto_del_recibo(
                job, recaudo, payload['tipo_docto_fe'], payload['consec_fe'], gw)
        elif job.tipo == 'DOCUMENTO_CONTABLE_RET':
            from app.services.liquidacion_service import RETENCION_PUC
            tipo_ret = payload.get('tipo_retencion') or next(
                (t for t, puc in RETENCION_PUC.items() if puc == payload.get('cuenta_puc')), None)
            payload['monto'], payload['base_gravable'] = monto_de_la_retencion(
                job, recaudo, tipo_ret, payload['tipo_docto_fe'], payload['consec_fe'], gw)
    return payload, cruce


# ═════════════════════════════════════════════════════════════════════════════
# El recibo que quedó sin respuesta clara: verificación diferida (P1-3)
# ═════════════════════════════════════════════════════════════════════════════

def momento_del_intento(payload):
    """Cuándo salió el POST del recibo (`intento_rc_en`, escrito con el
    pre-flag). No es `fecha_procesando`: esa la reescribe cada pasada de la
    cola, y la verificación diferida mediría siempre «0 minutos»."""
    crudo = (payload or {}).get('intento_rc_en')
    try:
        return datetime.fromisoformat(str(crudo)) if crudo else None
    except ValueError:
        return None


def lectura_de_verificacion(job, recaudo, gateway=None) -> dict:
    """Una lectura automática del saldo de la factura de un recibo sin
    verificar. **La única** que decide si hay evidencia (la usan la
    verificación diferida del ejecutor y «No está en Siesa»).

    `{'entro': bool, 'intacto': bool, 'saldo': float|None, 'saldo_antes':…,
      'leido_en': iso, 'minutos_desde_intento': float|None, 'motivo': str}`

    - **entró**: el saldo bajó, entre antes y después del intento, por al
      menos el monto del recibo (la misma vara que `_rc_entro_por_saldo`).
    - **intacto** (evidencia de que NO entró): el saldo no bajó —o, sin saldo
      de antes, la factura no tiene ningún crédito aplicado— **y** la lectura
      es de por lo menos `MINUTOS_PARA_EVIDENCIA_NO_ENTRO` después del intento
      (la cartera ya indexó). Cualquier otra cosa: ninguna de las dos.
    """
    from app.services.cxc_cruce import TOLERANCIA
    payload = job.get_payload() or {}
    gw = _gateway(gateway)
    ahora = datetime.utcnow()
    intento = momento_del_intento(payload)
    minutos = ((ahora - intento).total_seconds() / 60) if intento else None
    out = {'entro': False, 'intacto': False, 'saldo': None,
           'saldo_antes': payload.get('saldo_antes_rc'),
           'leido_en': ahora.isoformat(timespec='seconds'),
           'minutos_desde_intento': round(minutos, 1) if minutos is not None else None}
    tarea = recaudo.tarea
    tipo_fe, consec_fe = _txt(payload.get('tipo_docto_fe')), _txt(payload.get('consec_fe'))
    co = _txt(payload.get('co_factura')) or co_de_la_factura(tarea, gw)
    try:
        cruce = leer_cruce(tarea, tipo_fe, consec_fe, co, gw)
    except Exception as e:                       # noqa: BLE001 — «no sé»
        out['motivo'] = f'no se pudo leer la cartera: {e}'
        return out
    if cruce.saldo is None:
        out['motivo'] = 'sin saldo legible (simulación)'
        return out
    out['saldo'] = round(float(cruce.saldo), 2)
    antes = payload.get('saldo_antes_rc')
    monto = float(payload.get('monto') or 0)
    if antes is not None and monto > 0 and float(antes) - out['saldo'] >= monto - TOLERANCIA:
        out['entro'] = True
        out['motivo'] = (f'el saldo bajó de ${float(antes):,.0f} a ${out["saldo"]:,.0f}: '
                         f'el recibo entró')
        return out
    consec = rc_en_la_consulta(payload, gw)
    if consec:
        out['entro'] = True
        out['consec'] = consec
        out['motivo'] = f'la consulta de recibos de Siesa muestra el recibo {consec}'
        return out
    sin_mover = (out['saldo'] >= float(antes) - TOLERANCIA) if antes is not None \
        else (cruce.total_cr <= TOLERANCIA)
    tarde = minutos is not None and minutos >= MINUTOS_PARA_EVIDENCIA_NO_ENTRO
    out['intacto'] = bool(sin_mover and tarde)
    if sin_mover and not tarde:
        out['motivo'] = ('el saldo está igual, pero la lectura es de menos de '
                         f'{MINUTOS_PARA_EVIDENCIA_NO_ENTRO} min después del intento '
                         '(la cartera puede no haber indexado todavía)')
    elif sin_mover:
        out['motivo'] = (f'el saldo sigue en ${out["saldo"]:,.0f}' +
                         (f' (igual que antes del intento)' if antes is not None
                          else ' y la factura no tiene ningún crédito aplicado'))
    else:
        out['motivo'] = (f'el saldo cambió (${float(antes):,.0f} → ${out["saldo"]:,.0f}) pero no '
                         f'por el monto del recibo (${monto:,.0f}): no prueba nada'
                         if antes is not None else
                         f'la factura ya tiene créditos aplicados y no hay saldo de antes con '
                         f'qué comparar')
    return out


def rc_en_la_consulta(payload, gateway=None):
    """El consecutivo del recibo en `t350` si una consulta dinámica lo muestra
    **sin ambigüedad**, o `None`. La consulta es opcional y **sin default**
    (`CONNEKTA_CONSULTA_RC_RECIENTES`): la registra el consultor en Siesa como
    `papeleriamedellin_WMS_NC_Consecutivo` pero para el tipo de recibo, y debe
    devolver sin parámetros las columnas crudas `f350_rowid, f350_id_co,
    f350_id_tipo_docto, f350_consec_docto, f350_fecha, f350_total_db` **y el
    NIT del tercero** (`f200_id`), ordenadas por rowid descendente, `TOP 100`.

    Solo cuenta como prueba una fila del CO y el tipo del recibo, con la fecha
    del documento, el valor exacto y el NIT de la parada. Sin columna de NIT,
    o con dos candidatas, no prueba nada (dos clientes pagan lo mismo el mismo
    día). Nunca levanta."""
    from app.services.cxc_cruce import TOLERANCIA
    nombre = os.environ.get('CONNEKTA_CONSULTA_RC_RECIENTES', '').strip()
    gw = _gateway(gateway)
    if not nombre or getattr(gw, 'modo_simulacion', False) is True:
        return None
    p = payload or {}
    fecha = _txt(p.get('fecha_documento_rc'))
    nit = _txt(p.get('tercero_nit'))
    if not (fecha and nit and p.get('monto') is not None):
        return None
    ajuste = float(p.get('ajuste_valor') or 0)
    valor = float(p['monto']) + (ajuste if p.get('ajuste_es_sobrante') else -ajuste)
    try:
        res = gw._get(nombre, params_extra={'paginacion': 'numPag=1|tamPag=100'},
                      url=gw.url_get_dinamico)
        det = res.get('detalle', {}) if isinstance(res, dict) else {}
        filas = [f for f in (det.get('Datos') or det.get('Table') or []) if f.get('f350_rowid')]
    except Exception as e:                       # noqa: BLE001 — opcional
        logger.warning('[ENVIO_LIQ] consulta de recibos %s: %s', nombre, e)
        return None
    cands = [f for f in filas
             if _txt(f.get('f350_id_co')) == _txt(getattr(gw, 'centro_op', ''))
             and _txt(f.get('f350_id_tipo_docto')) == _txt(getattr(gw, 'tipo_docto_recibo_caja', ''))
             and _txt(f.get('f350_fecha'))[:10].replace('-', '') == fecha
             and abs(float(f.get('f350_total_db') or 0) - valor) <= TOLERANCIA
             and _txt(f.get('f200_id') or f.get('f350_id_tercero')) == nit]
    return _txt(cands[0].get('f350_consec_docto')) if len(cands) == 1 else None


def que_buscar_en_siesa(job, recaudo) -> dict:
    """Lo que una persona tiene que buscar en Siesa para un recibo sin
    verificar: el documento, el CO, la fecha, el tercero, el valor y la
    factura que cruza. Sale del payload resuelto (no se recalcula)."""
    from app.services.connekta_gateway import connekta
    p = job.get_payload() or {}
    fecha = p.get('fecha_documento_rc')
    return {
        'documento': f'Recibo de caja ({getattr(connekta, "tipo_docto_recibo_caja", "RC")})',
        'co': getattr(connekta, 'centro_op', None),
        'fecha': (f'{fecha[6:8]}/{fecha[4:6]}/{fecha[:4]}' if fecha and len(str(fecha)) == 8
                  else None),
        'tercero_nit': p.get('tercero_nit'),
        'valor': p.get('monto'),
        'factura': (f'{p.get("tipo_docto_fe")}-{p.get("consec_fe")}'
                    if p.get('tipo_docto_fe') and p.get('consec_fe') else None),
        'co_factura': p.get('co_factura'),
        'pedido': _txt(getattr(recaudo.tarea, 'numero_pedido_siesa', None)) if recaudo.tarea else None,
        'cliente': _txt(getattr(recaudo.tarea, 'cliente', None)) if recaudo.tarea else None,
        'verificacion': (p.get('verificacion') or {}).get('lecturas', []),
    }


# ═════════════════════════════════════════════════════════════════════════════
# La tarjeta del envío en Liquidación
# ═════════════════════════════════════════════════════════════════════════════

#: Marcas técnicas al frente de `error_ultimo` que la pantalla no muestra.
_MARCAS = ('SIN_VERIFICAR: ', 'DATO_QUE_FALTA: ')

TIPOS_DE_LA_LIQUIDACION = ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA')


def describir_envio(job) -> dict:
    """Lo que la tarjeta de un envío de la liquidación dice: de qué pedido y
    cliente es, qué pasó en palabras, y si «Reintentar» sirve.

    `reintentable` es falso cuando reintentar da lo mismo o es peligroso: un
    dato que falta (`DatoQueFalta`: lo pone una persona, y la tarjeta dice
    quién) o un envío sin verificar (se resuelve diciendo si está en Siesa).
    """
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import siesa_job_service as sjs
    p = job.get_payload() or {}
    rec = db.session.get(RecaudoEntrega, p.get('recaudo_id')) if p.get('recaudo_id') else None
    tarea = rec.tarea if rec is not None else None
    error = _txt(job.error_ultimo)
    determinista = error.startswith('DATO_QUE_FALTA: ')
    sin_verificar = (error.startswith('SIN_VERIFICAR: ') or sjs.preflag_sin_verificar(job)
                     or (job.tipo == 'RECIBO_CAJA' and job.estado == 'FALLIDO' and rec is not None
                         and rec.siesa_rc_triggered and not _rc_llego(rec)))
    mensaje = error
    for m in _MARCAS:
        if mensaje.startswith(m):
            mensaje = mensaje[len(m):]
    return {
        'pedido': _txt(getattr(tarea, 'numero_pedido_siesa', None)) or None,
        'cliente': _txt(getattr(tarea, 'cliente', None)) or None,
        'ruta_id': getattr(rec, 'ruta_id', None),
        'factura': (f'{p.get("tipo_docto_fe")}-{p.get("consec_fe")}'
                    if p.get('tipo_docto_fe') and p.get('consec_fe') else None),
        'valor': p.get('monto'),
        'mensaje': mensaje or None,
        'reintentable': job.estado == 'FALLIDO' and not determinista and not sin_verificar,
        'que_falta': determinista,
        'sin_verificar': bool(sin_verificar),
    }


def _rc_llego(recaudo) -> bool:
    from app.services.politica_cobro import rc_llego_a_siesa
    return rc_llego_a_siesa(recaudo)

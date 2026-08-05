"""
Quién recibe qué, y el registro de lo que salió.

Separado del canal a propósito: acá está la decisión (a quién avisar, cuándo,
una sola vez), allá está el transporte. El servicio no sabe que existe Gupshup
— recibe un `CanalDeAviso` y le pide que mande.

**Nace apagado** (regla 10): sin `FLOTA_AVISOS=true` el barrido no corre. Y aun
encendido, el canal por defecto es el simulado — encenderlo de verdad es una
segunda decisión explícita, `FLOTA_AVISOS_REALES=true`.

Los teléfonos van en configuración, a mano. Son cuatro números de empleados; no
salen de `TercerosContacto`, así que ni la paginación sin `ORDER BY` ni el caché
que reemplaza tocan este caso.
"""
import json
import logging
import os
from datetime import datetime

from app.extensions import db
from app.utils.fecha import dia_operativo
from flota.adaptadores.gupshup import AvisoNoEnviado, canal as canal_por_defecto
from flota.adaptadores.modelos import Aviso, DocumentoVehiculo
from flota.dominio.aviso import (
    AvisoInvalido,
    DIAS_AVISO_DOCUMENTO,
    clave_aviso,
    parametros_documento_vence,
    toca_avisar_vencimiento,
)

logger = logging.getLogger(__name__)


def avisos_encendidos() -> bool:
    return (os.getenv('FLOTA_AVISOS') or '').lower() == 'true'


def destinatarios(rol: str) -> list:
    """Teléfonos por rol, desde configuración.

    `FLOTA_AVISO_TELEFONOS` es JSON: `{"mantenimiento": ["573001112233"], ...}`.
    Sin default: un rol sin teléfonos configurados **no manda a nadie y lo
    dice**, en vez de caer a una lista global que avisaría al que no es.
    """
    crudo = (os.getenv('FLOTA_AVISO_TELEFONOS') or '').strip()
    if not crudo:
        return []
    try:
        mapa = json.loads(crudo)
    except ValueError as e:
        logger.error('[FLOTA/AVISO] FLOTA_AVISO_TELEFONOS no es JSON válido: %s', e)
        return []
    valor = mapa.get(rol) or []
    return [str(v).strip() for v in valor if str(v).strip()]


def _registrar(clave, plantilla, telefono, parametros, canal_usado):
    """Manda y deja la fila. La fila se escribe SIEMPRE, salga o no.

    Primero se inserta `encolado` y se hace commit: si el proceso se muere entre
    el POST y el registro, la clave única impide que el próximo ciclo lo mande
    de nuevo. Un aviso duplicado no es grave; tres seguidos silencian el chat, y
    entonces el que importa llega a un silencio.
    """
    fila = Aviso(
        clave=clave, plantilla=plantilla, telefono=str(telefono),
        parametros=json.dumps(parametros, ensure_ascii=False),
        estado='encolado', simulado=bool(canal_usado.simulado),
    )
    db.session.add(fila)
    db.session.commit()

    try:
        msg_id = canal_usado.enviar(telefono, plantilla, parametros)
    except (AvisoNoEnviado, AvisoInvalido) as e:
        fila.estado = 'fallido'
        fila.detalle = str(e)[:500]
        db.session.commit()
        logger.error('[FLOTA/AVISO] %s → %s falló: %s', plantilla, telefono, e)
        return fila

    fila.proveedor_msg_id = msg_id
    # `entregado_al_proveedor`, no `entregado`. Gupshup dijo "lo recibí".
    # Que haya llegado lo dice el evento de entrega, y hasta entonces este
    # aviso NO cuenta como avisado.
    fila.estado = 'entregado_al_proveedor'
    db.session.commit()
    return fila


def barrer_documentos_por_vencer(canal_usado=None, hoy=None) -> dict:
    """Avisa por cada documento que entró en la ventana de vencimiento.

    Devuelve un resumen, no `None`: un barrido que no dice qué hizo es
    indistinguible de uno que no corrió.

    Idempotente por `clave_aviso`, que lleva la fecha de vencimiento: correr dos
    veces el mismo día no repite, y un documento RENOVADO sí vuelve a avisar
    porque su hito cambió.
    """
    resumen = {'revisados': 0, 'en_ventana': 0, 'enviados': 0,
               'ya_avisados': 0, 'fallidos': 0, 'sin_destinatario': 0,
               'simulado': None}

    if not avisos_encendidos():
        resumen['motivo'] = 'FLOTA_AVISOS no está en true'
        return resumen

    hoy = hoy or dia_operativo()
    canal_usado = canal_usado or canal_por_defecto()
    resumen['simulado'] = bool(canal_usado.simulado)

    telefonos = destinatarios('mantenimiento')
    docs = (DocumentoVehiculo.query
            .filter(DocumentoVehiculo.estado == 'vigente')
            .filter(DocumentoVehiculo.fecha_vencimiento.isnot(None))
            .all())

    for doc in docs:
        resumen['revisados'] += 1
        if not toca_avisar_vencimiento(doc.fecha_vencimiento, hoy, DIAS_AVISO_DOCUMENTO):
            continue
        resumen['en_ventana'] += 1

        if not telefonos:
            # No se inventa un destinatario. Queda contado para que el health
            # pueda decir "hay avisos que nadie recibió por falta de números".
            resumen['sin_destinatario'] += 1
            continue

        # La clave se consulta EXACTAMENTE como se escribe, con el sufijo del
        # destinatario. Consultar la base y escribir la sufijada hacía que la
        # deduplicación nunca encontrara nada: el segundo barrido reventaba
        # contra el índice único en vez de saltarse el aviso.
        base = clave_aviso('flota_documento_vence', 'documento', doc.id,
                           doc.fecha_vencimiento.isoformat())
        pendientes = [(i, t) for i, t in enumerate(telefonos)
                      if Aviso.query.filter_by(clave=f'{base}:{i}').first() is None]
        if not pendientes:
            resumen['ya_avisados'] += 1
            continue

        placa = doc.vehiculo.placa if doc.vehiculo is not None else ''
        try:
            parametros = parametros_documento_vence(placa, doc.tipo, doc.fecha_vencimiento)
        except AvisoInvalido as e:
            logger.error('[FLOTA/AVISO] documento %s no se puede describir: %s', doc.id, e)
            resumen['fallidos'] += 1
            continue

        # Por destinatario y no por documento: si mañana se agrega un cuarto
        # número, esa persona recibe el aviso que todavía está vigente en vez de
        # quedar afuera para siempre porque "ya se avisó".
        for i, telefono in pendientes:
            fila = _registrar(f'{base}:{i}', 'flota_documento_vence',
                              telefono, parametros, canal_usado)
            if fila.estado == 'fallido':
                resumen['fallidos'] += 1
            else:
                resumen['enviados'] += 1

    logger.info('[FLOTA/AVISO] barrido de documentos: %s', resumen)
    return resumen


#: Eventos de Gupshup que SÍ mueven el estado. Los demás se ignoran.
_EVENTOS_PROVEEDOR = {'delivered': 'entregado', 'read': 'leido',
                      'failed': 'fallido', 'undelivered': 'fallido'}

#: Orden de avance de los estados. Total sobre `ESTADO_AVISO` a propósito —
#: `test_el_orden_cubre_todo_el_vocabulario` lo verifica contra la tabla.
#: `fallido` comparte nivel con `entregado_al_proveedor`: es un desenlace, no
#: un retroceso, y por eso se aplica siempre.
_ORDEN = {'encolado': 0, 'entregado_al_proveedor': 1, 'fallido': 1,
          'entregado': 2, 'leido': 3}


def registrar_entrega(proveedor_msg_id: str, estado_proveedor: str) -> bool:
    """Consume un evento de entrega. Devuelve si encontró la fila.

    Existe desde el día uno y no "cuando haga falta": sin esto, todo lo que el
    sistema puede afirmar es que Gupshup recibió el mensaje. El propósito del
    módulo es que un hallazgo vencido no se quede quieto — un aviso que no llega
    y nadie nota es el fallo exacto que hay que poder ver.
    """
    # Acá el `.get` SÍ corresponde y no es degradación: Gupshup manda decenas
    # de tipos de evento (`sent`, `enqueued`, `deleted`…) y los que no cambian
    # el estado se ignoran a propósito. La diferencia con el caso de arriba es
    # que el universo de entrada es ajeno y abierto, no un vocabulario nuestro.
    nuevo = _EVENTOS_PROVEEDOR.get((estado_proveedor or '').lower())
    if nuevo is None:
        logger.info('[FLOTA/AVISO] evento ignorado: %s', estado_proveedor)
        return False

    fila = Aviso.query.filter_by(proveedor_msg_id=(proveedor_msg_id or '').strip()).first()
    if fila is None:
        logger.warning('[FLOTA/AVISO] evento sin fila: %s', proveedor_msg_id)
        return False

    # `leido` no retrocede a `entregado`: los eventos pueden llegar desordenados
    # y un tablero que baja de estado se lee como un problema que no existe.
    #
    # El mapa es TOTAL sobre `ESTADO_AVISO` y se indexa directo, sin default: un
    # estado nuevo que alguien agregue al vocabulario y olvide acá tiene que
    # reventar, no ordenarse como 0 y hacer que todo lo demás lo pise en
    # silencio. Un `.get(x, 0)` acá es la regla 5.
    if _ORDEN[nuevo] < _ORDEN[fila.estado] and nuevo != 'fallido':
        return True

    fila.estado = nuevo
    if nuevo in ('entregado', 'leido') and fila.entregado_ts is None:
        fila.entregado_ts = datetime.utcnow()
    db.session.commit()
    return True


def avisos_sin_confirmar(horas: int = 6) -> int:
    """Cuántos salieron y nunca confirmaron entrega.

    Es el número que hace honesto al resto: si esto crece, el canal está
    aceptando mensajes que no llegan — el modo de fallo que ya pasó en cartera y
    que un contador de "enviados" no puede ver.
    """
    from datetime import timedelta

    corte = datetime.utcnow() - timedelta(hours=horas)
    return (Aviso.query
            .filter(Aviso.estado == 'entregado_al_proveedor')
            .filter(Aviso.simulado.is_(False))
            .filter(Aviso.creado_ts < corte)
            .count())


__all__ = ['avisos_encendidos', 'destinatarios', 'barrer_documentos_por_vencer',
           'registrar_entrega', 'avisos_sin_confirmar']

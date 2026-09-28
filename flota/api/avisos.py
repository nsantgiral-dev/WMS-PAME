"""
Avisos de flota: disparar el barrido a mano, mirar qué salió, recibir entregas.

**El callback existe desde el día uno.** Sin él lo único que el sistema puede
afirmar es que Gupshup recibió el mensaje, y el propósito del módulo es que un
vencimiento no se quede quieto: un aviso que no llega y nadie nota es el fallo
exacto que hay que poder ver. En cartera ese hueco costó semanas de creer que
se había avisado.

El callback **no lleva JWT** —lo llama Gupshup, no una persona— y por eso se
autentica con un secreto compartido en la URL. Sin `FLOTA_AVISO_WEBHOOK_TOKEN`
configurado, no acepta nada: un webhook abierto es un endpoint por el que
cualquiera marca avisos como entregados.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA, exige, exige_secreto

logger = logging.getLogger(__name__)

avisos_bp = Blueprint('flota_avisos', __name__)


@avisos_bp.route('/avisos', methods=['GET'])
@jwt_required()
@exige(Roles.VISTA_FLOTA, 'ver los avisos de toda la flota')
def listar_avisos():
    """Qué se mandó, a quién, y si llegó.

    `simulado` viaja en la respuesta, no solo en la fila: una pantalla que
    muestra avisos de prueba con el mismo aspecto que los reales es cómo se
    termina creyendo que 1.485 personas recibieron algo que nunca salió.
    """
    from flota.adaptadores.avisos import (CANAL_CORREO, avisos_sin_confirmar,
                                          configuracion_de_este_servicio,
                                          estado_del_barrido, que_falta)
    from flota.adaptadores.modelos import Aviso

    filas = Aviso.query.order_by(Aviso.id.desc()).limit(100).all()
    # Lo que corrió el barrido diario, leído de la BASE (2026-09-27). El cron
    # corre en el worker y este endpoint en la web: leer `os.getenv` acá
    # contestaba con las variables de la web, que no son las del cron.
    barridos = estado_del_barrido()
    esta_web = configuracion_de_este_servicio()
    con_inicio = [b for b in barridos if b.get('ultimo_inicio')]
    ultimo = max(con_inicio, key=lambda b: b['ultimo_inicio']) if con_inicio else None
    return jsonify({
        'avisos': [{
            'id': a.id, 'plantilla': a.plantilla, 'telefono': a.telefono,
            'canal': 'correo' if a.telefono == CANAL_CORREO else 'whatsapp',
            'parametros': a.parametros, 'estado': a.estado,
            'simulado': a.simulado, 'detalle': a.detalle,
            'creado_ts': a.creado_ts.isoformat() if a.creado_ts else None,
            'entregado_ts': a.entregado_ts.isoformat() if a.entregado_ts else None,
        } for a in filas],
        # El número que hace honesto al resto: si crece, el canal acepta
        # mensajes que no llegan.
        'sin_confirmar_6h': avisos_sin_confirmar(6),
        # Lo que corrió el cron (con su configuración en `ultimo_resumen`) y lo
        # que le falta a cada servicio, dicho con el nombre del servicio.
        'barrido': ultimo,
        'que_falta': que_falta(barridos, esta_web),
    }), 200


@avisos_bp.route('/avisos/barrer', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'disparar el barrido de avisos')
def barrer():
    """Corre el barrido de documentos por vencer ahora mismo.

    Existe para poder ejercerlo **antes** de encender el cron, y porque un
    barrido que solo corre de noche es un barrido que nadie vio nunca correr.
    Devuelve el resumen completo: un barrido que no dice qué hizo es
    indistinguible de uno que no corrió.
    """
    from app.utils.lock import LOCK_FLOTA_AVISOS, advisory_lock
    from flota.adaptadores.avisos import barrido_diario

    # El mismo candado del cron: el botón y el cron de las 06:00 no mandan dos
    # veces el mismo WhatsApp (2026-09-27; antes el botón no lo tomaba).
    with advisory_lock(LOCK_FLOTA_AVISOS, 'flota_avisos_barrido_manual') as tomado:
        if not tomado:
            return jsonify({'error': 'El barrido de avisos ya está corriendo. '
                                     'Espere un momento y vuelva a mirar.'}), 409
        r = barrido_diario()
    # Corre con las variables de ESTE servicio (la web), no las del cron.
    r['nota'] = ('Se corrió en este servicio, con sus variables. El barrido '
                 'diario de las 06:00 corre en el servicio de los crons.')
    return jsonify(r), 200


@avisos_bp.route('/avisos/entrega', methods=['POST'])
@exige_secreto('FLOTA_AVISO_WEBHOOK_TOKEN', 'el callback de entrega')
def entrega():
    """Callback de Gupshup. Sin JWT, con secreto en la URL.

    Devuelve 200 incluso cuando el evento no corresponde a ninguna fila: un
    webhook que responde error hace que el proveedor reintente en bucle. Lo que
    no se pudo cruzar queda en el log, no en un 500.
    """
    from flota.adaptadores.avisos import registrar_entrega

    datos = request.get_json(silent=True) or {}
    # Gupshup manda el evento anidado y con nombres distintos según el tipo.
    # Se leen los dos sitios conocidos y, si no aparece, se registra el cuerpo
    # crudo en el log: adivinar la forma es cómo se pierden eventos en silencio.
    payload = datos.get('payload') or {}
    msg_id = (payload.get('gsId') or payload.get('id')
              or datos.get('messageId') or '')
    estado = (payload.get('type') or datos.get('type') or '')

    if not msg_id:
        logger.warning('[FLOTA/AVISO] callback sin id reconocible: %s', str(datos)[:300])
        return jsonify({'ok': True, 'cruzado': False}), 200

    cruzado = registrar_entrega(msg_id, estado)
    return jsonify({'ok': True, 'cruzado': cruzado}), 200


__all__ = ['avisos_bp']

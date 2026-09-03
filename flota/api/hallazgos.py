"""
Endpoints de hallazgo — «llevar control sobre los daños que pasan».

Nacen con su pantalla (`app/static/pwa/flota.js`), no antes. Un endpoint sin
forma de llamarse es la regla 12 rota y el patrón que ya apareció cinco veces en
este repo: capacidad construida, probada, desplegada, y el gesto que la enciende
nunca escrito. El trinquete de huérfanas lo impide y no se exime nada «hasta que
llegue la pantalla».

## Quién puede qué, y por qué no es lo mismo

| | Rol | Motivo |
|---|---|---|
| Reportar | `LECTURA_FLOTA` (incluye conductor) | El que ve el golpe es el que maneja. Un daño que solo puede reportar un jefe es un daño que se reporta el lunes |
| Cerrar / descartar / aplazar | `MAESTROS_FLOTA` (sin conductor) | Regla 11: si quien reporta también cierra, el camino barato es reportar y descartar en el mismo minuto. La severidad la decide quien reporta; el desenlace, no |

`reportado_por_usuario_id` sale del token, nunca del cuerpo. Quién dice que vio
el daño no lo elige quien manda el JSON.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from app.routes._auth_helpers import Roles
from flota.adaptadores import hallazgos as adaptador
from flota.adaptadores.modelos import Hallazgo
from flota.api._permisos import MAESTROS_FLOTA, exige
from flota.api._tiempo import iso_utc
from flota.dominio.errores import ErrorFlota
from flota.dominio.hallazgo import (EstadoHallazgo, dias_transcurridos,
                                    entra_al_indicador, vencido)

hallazgos_bp = Blueprint('flota_hallazgos', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _json(h: Hallazgo, ahora) -> dict:
    """Un hallazgo como lo lee la pantalla. Los juicios los emite el DOMINIO.

    `vencido` y `entra_al_indicador` no se recalculan acá con un `if`: una regla
    escrita dos veces diverge, y la copia que diverge es siempre la de la
    pantalla — la que la gente mira.
    """
    dom = h.a_dominio()
    return {
        'id': h.id,
        'vehiculo_id': h.vehiculo_id,
        'criticidad': h.criticidad,
        'descripcion': h.descripcion,
        'estado': h.estado,
        'reportado_ts': iso_utc(h.reportado_ts),
        'reportado_por_usuario_id': h.reportado_por_usuario_id,
        'fecha_limite': iso_utc(h.fecha_limite),
        'cerrado_ts': iso_utc(h.cerrado_ts),
        'motivo_cierre': h.motivo_cierre,
        'bitacora': h.bitacora,
        'aplazado_veces': h.aplazado_veces,
        'linea_base': h.linea_base,
        'lectura_id': h.lectura_id,
        'custodia_id': h.custodia_id,
        # De qué ítem de la inspección nació, o `null` si lo escribió alguien a
        # mano. La columna existe con su FK desde que la inspección diaria hace
        # nacer hallazgos (2026-09-02) y **este endpoint no la publicaba**: el
        # POST de la inspección devuelve `hallazgos: [{item_id, nombre, …}]` y
        # esta lista —que es la que alguien abre mañana— no tenía con qué decir
        # que «Sudado de aceite» salió del preoperacional y no del teclado de
        # alguien. Los otros dos vínculos del hallazgo (`lectura_id`,
        # `custodia_id`) ya viajaban; faltaba el tercero.
        'item_id': h.item_id,
        'vencido': vencido(dom, ahora),
        'dias_abierto': dias_transcurridos(dom, ahora),
        # Se publica para que la pantalla no tenga que deducirlo de
        # `linea_base` + `estado`. Deducirlo sería la cuarta copia de la regla.
        'entra_al_indicador': entra_al_indicador(dom),
    }


@hallazgos_bp.route('/hallazgos/<placa>', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver los daños de un vehículo')
def listar(placa):
    """Los daños abiertos del vehículo, el que peor está primero.

    Por defecto solo los abiertos: es lo que hay que hacer hoy. `?todos=1` trae
    el histórico completo, que es otra pregunta —«qué le ha pasado a este
    camión»— y se contesta cuando alguien la hace, no en la pantalla del turno.
    """

    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    ahora = datetime.utcnow()
    if request.args.get('todos') == '1':
        filas = (Hallazgo.query.filter_by(vehiculo_id=vehiculo.id)
                 .order_by(Hallazgo.reportado_ts.desc()).all())
    else:
        filas = adaptador.abiertos_de(vehiculo.id)

    return jsonify({
        'placa': vehiculo.placa,
        'hallazgos': [_json(h, ahora) for h in filas],
        # Contadores separados y no un solo total: «tres abiertos» y «uno
        # vencido» se atienden distinto, y sumarlos esconde el vencido.
        'abiertos': sum(1 for h in filas if h.estado == EstadoHallazgo.ABIERTO),
        'vencidos': sum(1 for h in filas if vencido(h.a_dominio(), ahora)),
    })


@hallazgos_bp.route('/hallazgos', methods=['POST'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'reportar un daño')
def reportar():
    """Registra un daño con su reloj corriendo.

    `criticidad` la declara quien reporta; la **fecha límite no se recibe**, se
    calcula (regla 6). Un cuerpo que la mandara la vería ignorada, que es peor
    que un 400 — por eso ni se mira.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'criticidad', 'descripcion', 'km')
                 if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        km = int(datos['km'])
    except (ValueError, TypeError):
        return jsonify({'error': f'km inválido: {datos["km"]!r}'}), 400

    try:
        fila = adaptador.reportar(
            vehiculo_id=vehiculo.id,
            criticidad=datos['criticidad'],
            descripcion=datos['descripcion'],
            km=km,
            reportado_por_usuario_id=_usuario_id(),
            item_id=datos['item_id'] if 'item_id' in datos else None,
            fotos=datos['fotos'] if 'fotos' in datos else None,
        )
    except ErrorFlota as e:
        # 409 y no 400: el cuerpo puede estar perfecto y el estado del mundo no
        # admitirlo — un odómetro que retrocede es lo segundo.
        return jsonify({'error': str(e)}), 409

    return jsonify(_json(fila, datetime.utcnow())), 201


@hallazgos_bp.route('/hallazgos/<int:hallazgo_id>/cerrar', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'cerrar un daño')
def cerrar(hallazgo_id):
    """El vehículo volvió reparado. Para el reloj del indicador."""
    datos = request.get_json(silent=True) or {}
    try:
        fila = adaptador.cerrar(hallazgo_id=hallazgo_id, usuario_id=_usuario_id(),
                                nota=datos.get('nota'))
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify(_json(fila, datetime.utcnow()))


@hallazgos_bp.route('/hallazgos/<int:hallazgo_id>/descartar', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'descartar un daño')
def descartar(hallazgo_id):
    """No era un daño. Exige motivo escrito."""
    datos = request.get_json(silent=True) or {}
    try:
        fila = adaptador.descartar(hallazgo_id=hallazgo_id,
                                   usuario_id=_usuario_id(),
                                   motivo=datos.get('motivo') or '')
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify(_json(fila, datetime.utcnow()))


@hallazgos_bp.route('/hallazgos/<int:hallazgo_id>/aplazar', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'aplazar un daño')
def aplazar(hallazgo_id):
    """Corre el plazo y deja el contador subiendo. No borra el tiempo abierto."""
    datos = request.get_json(silent=True) or {}
    try:
        fila = adaptador.aplazar(hallazgo_id=hallazgo_id,
                                 usuario_id=_usuario_id(),
                                 motivo=datos.get('motivo') or '')
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify(_json(fila, datetime.utcnow()))


__all__ = ['hallazgos_bp']

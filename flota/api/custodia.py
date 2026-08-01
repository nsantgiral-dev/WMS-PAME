"""
Endpoints de custodia y odómetro — §4, construidos con su consumidor (§3).

Los cinco endpoints de la tanda 1 nacen en la misma sesión que la pantalla de
recibo de turno. No es orden de conveniencia: un endpoint sin forma de llamarse
es la regla 12 rota y el patrón que ya apareció cuatro veces en este repo —
capacidad construida, probada y desplegada, y el gesto que la enciende nunca
escrito. El trinquete de huérfanos lo impide, y eximirlos "hasta que llegue la
pantalla" convertiría un trinquete en cero en arqueología.

Todos exigen sesión: el conductor autentica, y `registrado_por_usuario_id` sale
del token, nunca del cuerpo del request. Quién dice que entregó el turno no lo
elige quien manda el JSON.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.vehiculo import Vehiculo
from flota.adaptadores import traspaso
from flota.adaptadores.modelos import LecturaOdometro
from flota.dominio import odometro as dom_odo
from flota.dominio.errores import ErrorFlota
from flota.dominio.valores import (
    SIN_DATO,
    CustodioEstado,
    CustodioTipo,
    Lectura,
    OrigenLectura,
)

custodia_bp = Blueprint('flota_custodia', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    """La placa es la clave natural de consulta; `vehiculo_id` es la FK.

    Levanta si no existe. No devuelve `None` para que el llamador improvise: un
    vehículo que no está en el maestro es un vehículo que nadie dio de alta, y
    eso se dice, no se rodea.
    """
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _lecturas_dominio(vehiculo_id):
    return [
        Lectura(valor_km=l.valor_km, ts=l.ts, origen=OrigenLectura(l.origen),
                autor_usuario_id=l.autor_usuario_id,
                motivo_correccion=l.motivo_correccion)
        for l in LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id)
                                      .order_by(LecturaOdometro.ts).all()
    ]


@custodia_bp.route('/custodia/activa/<placa>', methods=['GET'])
@jwt_required()
def custodia_activa(placa):
    """Quién responde por este vehículo ahora mismo, y con cuántos kilómetros.

    Es lo primero que carga la pantalla de recibo de turno: sin saber de quién
    viene, el conductor no puede reconocer ni disputar las novedades heredadas.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    vigente = traspaso.custodia_activa(vehiculo.id)
    km = dom_odo.odometro_actual(_lecturas_dominio(vehiculo.id))

    return jsonify({
        'placa': vehiculo.placa,
        'vehiculo_id': vehiculo.id,
        # `sin_dato` viaja como la palabra, no como 0. Un vehículo sin lecturas
        # no tiene 0 km: no sabemos cuántos tiene.
        'odometro_actual': km if km is not SIN_DATO else str(SIN_DATO),
        'custodia': None if vigente is None else {
            'id': vigente.id,
            'custodio_tipo': vigente.custodio_tipo,
            'custodio_estado': vigente.custodio_estado,
            'custodio_conductor_id': vigente.custodio_conductor_id,
            'custodio_sede_id': vigente.custodio_sede_id,
            'inicio_ts': vigente.inicio_ts.isoformat(),
            'km_inicio': vigente.km_inicio,
            'linea_base': vigente.linea_base,
        },
    }), 200


@custodia_bp.route('/custodia/traspaso', methods=['POST'])
@jwt_required()
def custodia_traspaso():
    """Cierra el turno anterior y abre el nuevo, atómicamente.

    Un solo endpoint y no dos (cerrar + abrir) a propósito: dos llamadas son dos
    transacciones, y entre ellas hay un vehículo sin responsable durante lo que
    tarde la red del conductor a las 5 a.m.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'km', 'custodio_tipo') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        tipo = CustodioTipo(datos['custodio_tipo'])
        estado = CustodioEstado(
            datos['custodio_estado'] if 'custodio_estado' in datos else 'resuelto'
        )
        km = int(datos['km'])
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Valor inválido: {e}'}), 400

    try:
        nueva = traspaso.traspasar(
            vehiculo_id=vehiculo.id,
            km=km,
            registrado_por_usuario_id=_usuario_id(),
            custodio_tipo=tipo,
            custodio_conductor_id=datos['custodio_conductor_id'] if 'custodio_conductor_id' in datos else None,
            custodio_sede_id=datos['custodio_sede_id'] if 'custodio_sede_id' in datos else None,
            custodio_estado=estado,
            fotos_fin=datos['fotos_fin'] if 'fotos_fin' in datos else None,
            fotos_inicio=datos['fotos_inicio'] if 'fotos_inicio' in datos else None,
        )
    except ErrorFlota as e:
        # 409: el estado del mundo no admite este traspaso. No es un error de
        # sintaxis del cliente ni una falla del servidor — es un "no se puede".
        return jsonify({'error': str(e)}), 409

    return jsonify({
        'custodia_id': nueva.id,
        'placa': vehiculo.placa,
        'inicio_ts': nueva.inicio_ts.isoformat(),
        'km_inicio': nueva.km_inicio,
        'linea_base': nueva.linea_base,
        'custodio_estado': nueva.custodio_estado,
    }), 201


@custodia_bp.route('/odometro', methods=['POST'])
@jwt_required()
def registrar_odometro():
    """Una lectura suelta: tanqueo, cierre de día, OT, o corrección.

    Append-only. Una lectura no se edita: se corrige con un registro nuevo de
    `origen = correccion`, que exige motivo escrito — sin él una corrección es
    indistinguible de un error de digitación.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'valor_km', 'origen') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        origen = OrigenLectura(datos['origen'])
        valor = int(datos['valor_km'])
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Valor inválido: {e}'}), 400

    from datetime import datetime
    nueva = Lectura(
        valor_km=valor, ts=datetime.utcnow(), origen=origen,
        autor_usuario_id=_usuario_id(),
        motivo_correccion=datos['motivo_correccion'] if 'motivo_correccion' in datos else None,
    )
    try:
        dom_odo.validar_lectura(_lecturas_dominio(vehiculo.id), nueva)
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    fila = LecturaOdometro(
        vehiculo_id=vehiculo.id, valor_km=nueva.valor_km, ts=nueva.ts,
        origen=origen.value, autor_usuario_id=nueva.autor_usuario_id,
        motivo_correccion=nueva.motivo_correccion,
    )
    db.session.add(fila)
    db.session.commit()
    return jsonify({'lectura_id': fila.id, 'valor_km': fila.valor_km,
                    'origen': fila.origen}), 201


__all__ = ['custodia_bp']

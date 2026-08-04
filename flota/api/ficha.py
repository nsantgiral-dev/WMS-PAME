"""
Ficha técnica — `GET` y `PUT /flota/vehiculo/{placa}/ficha`.

Es la pantalla donde aterriza el levantamiento de campo. Dos cosas que el
endpoint impone y no son de forma:

**Ningún atributo tiene valor por defecto optimista.** Una ficha nueva nace con
todo en `sin_dato` y el health la cuenta como incompleta hasta que alguien la
llene. Un default alegre convertiría 5 fichas vacías en 5 fichas completas el
día que se crearan.

**Los dos atributos que disparan tareas de seguridad llevan procedencia.** Si se
declara `distribucion = correa` sin decir de dónde salió, la base rechaza: un
dato con autoridad sin procedencia es tradición oral con formato de columna, y
alguien va a programar un cambio de correa contra una suposición.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA, exige
from app.models.vehiculo import Vehiculo
from flota.adaptadores.modelos import FichaTecnica

ficha_bp = Blueprint('flota_ficha', __name__)

# Lo que el levantamiento de campo puede escribir. `vehiculo_id` no está: la
# ficha no cambia de vehículo — sería otra ficha.
_EDITABLES = (
    'combustible', 'sistema_frenos', 'tiene_freno_escape', 'distribucion',
    'transmision_final', 'distribucion_km_cambio', 'norma_emisiones',
    'aceite_motor_spec', 'aceite_motor_litros', 'aceite_caja_spec',
    'aceite_diferencial_spec', 'refrigerante_spec', 'posiciones_llanta',
    'medida_llanta', 'tiene_furgon', 'km_inicial', 'km_inicial_ts',
    'distribucion_fuente', 'distribucion_verificado_ts',
    'frenos_fuente', 'frenos_verificado_ts',
)


def _vehiculo(placa):
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _serializar(placa, ficha):
    if ficha is None:
        # `None` no es "ficha vacía": es "no hay ficha". La pantalla tiene que
        # poder distinguir un vehículo sin levantar de uno levantado a medias.
        return {'placa': placa, 'existe': False, 'ficha': None,
                'atributos_sin_dato': None, 'completa': None}
    datos = {}
    for campo in _EDITABLES:
        valor = getattr(ficha, campo)
        datos[campo] = valor.isoformat() if hasattr(valor, 'isoformat') else (
            float(valor) if isinstance(valor, __import__('decimal').Decimal) else valor
        )
    return {'placa': placa, 'existe': True, 'ficha': datos,
            'atributos_sin_dato': ficha.atributos_sin_dato(),
            'completa': ficha.completa()}


@ficha_bp.route('/vehiculo/<placa>/ficha', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver la ficha tecnica')
def obtener_ficha(placa):
    try:
        vehiculo = _vehiculo(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    ficha = db.session.get(FichaTecnica, vehiculo.id)
    return jsonify(_serializar(vehiculo.placa, ficha)), 200


@ficha_bp.route('/vehiculo/<placa>/ficha', methods=['PUT'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'modificar la ficha tecnica')
def guardar_ficha(placa):
    """Crea o actualiza. Los CHECK de la base son la última palabra.

    No se validan los vocabularios acá además de en la base: una sola política
    en un solo lugar. Si el CHECK rechaza, se devuelve 409 con el nombre del
    constraint — que dice exactamente qué regla se violó.
    """
    try:
        vehiculo = _vehiculo(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    datos = request.get_json(silent=True) or {}
    ficha = db.session.get(FichaTecnica, vehiculo.id)
    creada = ficha is None
    if creada:
        # Mínimos estructurales que la tabla exige y no tienen "no sé" posible:
        # se cuentan a la vista o se leen del tablero.
        for campo in ('posiciones_llanta', 'km_inicial'):
            if campo not in datos:
                return jsonify({'error': f'Campo requerido al crear la ficha: {campo}'}), 400
        from datetime import datetime
        ficha = FichaTecnica(
            vehiculo_id=vehiculo.id,
            posiciones_llanta=datos['posiciones_llanta'],
            km_inicial=datos['km_inicial'],
            km_inicial_ts=datetime.utcnow(),
        )
        db.session.add(ficha)

    for campo in _EDITABLES:
        if campo not in datos or campo == 'km_inicial_ts':
            continue
        valor = datos[campo]
        if campo.endswith('_ts') and isinstance(valor, str):
            # ISO → datetime. Si el formato no sirve, se devuelve 400: una fecha
            # de verificación mal escrita que se guarda como NULL diría que el
            # dato nunca se verificó, y eso es peor que rechazar el PUT.
            from datetime import datetime as _dt
            try:
                valor = _dt.fromisoformat(valor)
            except ValueError:
                return jsonify({
                    'error': f'{campo} no es una fecha ISO válida: {datos[campo]!r}'
                }), 400
        setattr(ficha, campo, valor)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        # `str(e.orig)` directo: si el driver cambiara de forma, esto tiene que
        # reventar en un test, no devolver una cadena vacía que se lea como
        # "la base no dijo por qué".
        return jsonify({
            'error': 'La ficha viola una regla de la base',
            'detalle': str(e.orig)[:300],
        }), 409

    return jsonify(_serializar(vehiculo.placa, ficha)), 201 if creada else 200


__all__ = ['ficha_bp']

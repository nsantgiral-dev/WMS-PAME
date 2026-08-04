"""
Documentos del vehículo — SOAT, tecnomecánica, póliza, tarjeta de propiedad.

`GET` y `POST /flota/vehiculo/{placa}/documentos`.

**Estos endpoints faltaban en §4 de la especificación.** El health ya contaba
documentos vencidos y por vencer, la tabla existía desde la tanda 1 — y no había
por dónde cargar una fecha. La primera tarea del control de flota es verificar
los cinco SOAT: sin esto, esa verificación queda en la cabeza de quien la hizo y
en tres meses nadie sabe si se hizo.

**`no_encontrado` es un estado, no un campo vacío.** Un vehículo sin SOAT vigente
localizable es un hallazgo bloqueante. Registrarlo como ausencia de dato lo
vuelve indistinguible de "todavía no lo hemos mirado", y esas dos cosas exigen
acciones opuestas: una es buscar el papel, la otra es sacar el camión de ruta.
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA, exige
from app.models.vehiculo import Vehiculo
from flota.adaptadores.modelos import DocumentoVehiculo, Foto

documentos_bp = Blueprint('flota_documentos', __name__)


def _vehiculo(placa):
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _serializar(d):
    hoy = date.today()
    vencido = (d.fecha_vencimiento is not None and d.fecha_vencimiento < hoy)
    return {
        'id': d.id, 'tipo': d.tipo, 'estado': d.estado,
        'numero': d.numero, 'entidad': d.entidad,
        'fecha_expedicion': d.fecha_expedicion.isoformat() if d.fecha_expedicion else None,
        'fecha_vencimiento': d.fecha_vencimiento.isoformat() if d.fecha_vencimiento else None,
        'foto_id': d.foto_id,
        # Se calcula acá y no en el cliente: la misma pregunta contestada en dos
        # lugares termina con dos respuestas.
        'vencido': vencido,
        'dias_para_vencer': (d.fecha_vencimiento - hoy).days if d.fecha_vencimiento else None,
    }


@documentos_bp.route('/vehiculo/<placa>/documentos', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver los documentos')
def listar_documentos(placa):
    try:
        vehiculo = _vehiculo(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    docs = DocumentoVehiculo.query.filter_by(vehiculo_id=vehiculo.id).all()
    registrados = {d.tipo for d in docs}
    return jsonify({
        'placa': vehiculo.placa,
        'documentos': [_serializar(d) for d in docs],
        # Lo que NO se ha mirado, explícito. Un tipo ausente de la lista es un
        # documento sin verificar — distinto de uno verificado y no encontrado.
        'sin_verificar': sorted(
            {'soat', 'rtm', 'poliza_rc', 'tarjeta_propiedad'} - registrados),
    }), 200


@documentos_bp.route('/vehiculo/<placa>/documentos', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'registrar un documento')
def guardar_documento(placa):
    """Crea o reemplaza el documento de un tipo para ese vehículo.

    Reemplaza y no acumula: un SOAT nuevo sustituye al anterior del mismo tipo.
    El histórico de pólizas vencidas no es lo que este módulo persigue — lo que
    persigue es que hoy haya uno vigente.
    """
    try:
        vehiculo = _vehiculo(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    datos = request.get_json(silent=True) or {}
    if 'tipo' not in datos:
        return jsonify({'error': 'Campo requerido: tipo'}), 400

    estado = datos['estado'] if 'estado' in datos else 'vigente'
    if estado not in ('vigente', 'no_encontrado'):
        return jsonify({'error': f'estado inválido: {estado}'}), 400

    def _fecha(clave):
        if clave not in datos or not datos[clave]:
            return None
        try:
            return date.fromisoformat(datos[clave])
        except ValueError:
            raise ValueError(f'{clave} no es una fecha ISO válida: {datos[clave]!r}')

    try:
        expedicion, vencimiento = _fecha('fecha_expedicion'), _fecha('fecha_vencimiento')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    if estado == 'no_encontrado':
        # Las fechas se descartan a propósito: si el papel no apareció, no hay
        # de dónde salieron esas fechas. Aceptarlas sería inventar procedencia.
        expedicion = vencimiento = None
        numero = entidad = ''
    else:
        numero = (datos['numero'] if 'numero' in datos else '').strip()
        entidad = (datos['entidad'] if 'entidad' in datos else '').strip()

    doc = DocumentoVehiculo.query.filter_by(
        vehiculo_id=vehiculo.id, tipo=datos['tipo']).first()
    creado = doc is None
    if creado:
        doc = DocumentoVehiculo(vehiculo_id=vehiculo.id, tipo=datos['tipo'])
        db.session.add(doc)

    doc.estado, doc.numero, doc.entidad = estado, numero, entidad
    doc.fecha_expedicion, doc.fecha_vencimiento = expedicion, vencimiento

    try:
        db.session.flush()
        if 'foto' in datos and datos['foto']:
            from datetime import datetime

            from flota.adaptadores.almacen_fotos import guardar_foto

            campos = guardar_foto(datos['foto'])
            foto = Foto(
                entidad_tipo='documento', entidad_id=doc.id,
                ts_captura=datetime.utcnow(),
                autor_usuario_id=int(get_jwt_identity()),
                **campos,
            )
            db.session.add(foto)
            db.session.flush()
            doc.foto_id = foto.id
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({
            'error': 'El documento viola una regla de la base',
            'detalle': str(e.orig)[:300],
        }), 409

    return jsonify(_serializar(doc)), 201 if creado else 200


__all__ = ['documentos_bp']

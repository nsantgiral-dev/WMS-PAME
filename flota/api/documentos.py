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
from app.utils.fecha import dia_operativo
from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA, exige
from app.models.vehiculo import Vehiculo
from flota.adaptadores.almacen_fotos import ErrorAlmacen
from flota.adaptadores.modelos import DocumentoVehiculo, Foto
from flota.dominio.errores import FotoInvalida
from flota.dominio.valores import exige_vencimiento

documentos_bp = Blueprint('flota_documentos', __name__)


def _vehiculo(placa):
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _adjunto(d):
    """Qué archivo respalda este documento, o `None` si no hay ninguno.

    Devuelve el estado también: una fila que dice `pendiente_evidencia` afirma
    que hubo un archivo y que no se guardó. Ocultarlo detrás de un id lo vuelve
    indistinguible de un adjunto sano hasta que alguien lo abre.
    """
    if not d.foto_id:
        return None
    f = db.session.get(Foto, d.foto_id)
    if f is None:
        return None
    return {
        'id': f.id, 'mime': f.mime, 'clase': f.clase, 'estado': f.estado,
        'es_pdf': f.mime == 'application/pdf',
        'bytes': f.bytes, 'ancho': f.ancho, 'alto': f.alto,
    }


def _serializar(d):
    # Día operativo de Bogotá, no UTC: `vencido` y `dias_para_vencer` son la
    # respuesta a "¿sale este camión?", y en UTC cambiaban a las 7 p.m.
    hoy = dia_operativo()
    # `vence` distingue "no vence nunca" de "no sabemos cuándo vence". Sin ese
    # campo la pantalla tiene que adivinar por la ausencia de fecha, y adivinar
    # es como la tarjeta de propiedad terminó con '2045-08-20'.
    vence = exige_vencimiento(d.tipo)
    vencido = (d.fecha_vencimiento is not None and d.fecha_vencimiento < hoy)
    return {
        'id': d.id, 'tipo': d.tipo, 'estado': d.estado,
        'numero': d.numero, 'entidad': d.entidad,
        'fecha_expedicion': d.fecha_expedicion.isoformat() if d.fecha_expedicion else None,
        'fecha_vencimiento': d.fecha_vencimiento.isoformat() if d.fecha_vencimiento else None,
        'foto_id': d.foto_id,
        # El adjunto puede ser un PDF: la pantalla necesita saberlo para
        # decidir si lo pinta o lo abre. `foto_id` se conserva porque hay
        # clientes que ya lo leen, pero el nombre miente desde que se aceptan
        # archivos — el que describe la cosa es este.
        'adjunto': _adjunto(d),
        # Se calcula acá y no en el cliente: la misma pregunta contestada en dos
        # lugares termina con dos respuestas.
        'vence': vence,
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

    # Un tipo que no vence NO puede traer vencimiento, ni siquiera si el cliente
    # lo manda. Aceptarlo dejaría entrar la fecha inventada por otra puerta y el
    # aviso de renovación la perseguiría como si fuera real.
    try:
        if not exige_vencimiento(datos['tipo']):
            vencimiento = None
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

    # `archivo` es el nombre correcto desde que se aceptan PDF; `foto` se sigue
    # leyendo porque hay clientes desplegados que lo mandan. Uno solo de los
    # dos: si llegaran los dos, gana el nombre nuevo y no se adivina cuál quiso
    # mandar quien mandó ambos.
    adjunto = datos['archivo'] if datos.get('archivo') else datos.get('foto')

    try:
        db.session.flush()
        if adjunto:
            from datetime import datetime

            from flota.adaptadores.almacen_fotos import guardar_foto

            campos = guardar_foto(adjunto)
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
    except (FotoInvalida, ErrorAlmacen) as e:
        # 400 y no 500: el archivo que llegó no sirve, y eso es información
        # accionable para quien lo está subiendo — no un fallo del servidor.
        db.session.rollback()
        return jsonify({'error': 'El archivo adjunto no se puede aceptar',
                        'detalle': str(e)[:300]}), 400
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({
            'error': 'El documento viola una regla de la base',
            'detalle': str(e.orig)[:300],
        }), 409

    return jsonify(_serializar(doc)), 201 if creado else 200


__all__ = ['documentos_bp']

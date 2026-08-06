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
from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA, exige
from app.routes._auth_helpers import _es_gestion
from app.models.vehiculo import Vehiculo
from flota.api._tiempo import iso_utc
from flota.adaptadores import traspaso
from flota.adaptadores.modelos import FichaTecnica, LecturaOdometro
from flota.dominio import odometro as dom_odo
from flota.dominio.errores import ErrorFlota
from flota.dominio.valores import (
    MOTIVO_ORIGEN_NO_SUELTO,
    angulos_de_custodia,
    custodio_de_ubicacion,
    posiciones_llanta,
    ORIGENES_LECTURA_SUELTA,
    SIN_DATO,
    CustodioEstado,
    CustodioTipo,
    Lectura,
    OrigenLectura,
    QuienPide,
    Ubicacion,
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
@exige(Roles.LECTURA_FLOTA, 'ver la custodia activa')
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

    # Cuántas fotos de llanta pedir. Una sola foto llamada `llantas` no ubica
    # nada: una tuerca floja está en una rueda concreta. El número sale de la
    # ficha, y si no hay ficha se infiere del tipo — pero la pantalla dice
    # cuál de las dos cosas pasó, porque no valen lo mismo.
    ficha = db.session.get(FichaTecnica, vehiculo.id)
    n_llantas, fuente = posiciones_llanta(
        ficha.posiciones_llanta if ficha else None, vehiculo.tipo)

    return jsonify({
        'placa': vehiculo.placa,
        'vehiculo_id': vehiculo.id,
        'tipo': vehiculo.tipo,
        'posiciones_llanta': n_llantas,
        'posiciones_llanta_fuente': fuente,
        'angulos': list(angulos_de_custodia(n_llantas)),
        # `sin_dato` viaja como la palabra, no como 0. Un vehículo sin lecturas
        # no tiene 0 km: no sabemos cuántos tiene.
        'odometro_actual': km if km is not SIN_DATO else str(SIN_DATO),
        'custodia': None if vigente is None else {
            'id': vigente.id,
            'custodio_tipo': vigente.custodio_tipo,
            'custodio_estado': vigente.custodio_estado,
            'custodio_conductor_id': vigente.custodio_conductor_id,
            'custodio_sede_id': vigente.custodio_sede_id,
            'inicio_ts': iso_utc(vigente.inicio_ts),
            'km_inicio': vigente.km_inicio,
            'linea_base': vigente.linea_base,
        },
    }), 200


@custodia_bp.route('/foto/<int:foto_id>', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver fotos de flota')
def ver_foto(foto_id):
    """Devuelve el archivo. Un almacén de solo escritura no es un almacén.

    Si la foto no se puede recuperar, la evidencia no existe — da igual que la
    fila diga que sí. Por eso el test de guardado incluye recuperarla.
    """
    from flask import Response

    from flota.adaptadores.almacen_fotos import AlmacenLocal, ErrorAlmacen
    from flota.adaptadores.modelos import Foto

    foto = db.session.get(Foto, foto_id)
    if foto is None:
        return jsonify({'error': f'No existe la foto {foto_id}'}), 404
    if foto.estado == 'pendiente_evidencia':
        return jsonify({
            'error': 'Esta foto nunca se guardó',
            'detalle': foto.storage_ref,
        }), 410
    try:
        contenido = AlmacenLocal().leer(foto.storage_ref)
    except ErrorAlmacen as e:
        # 410 y no 404: la fila existe y afirma que hay una foto. Que el archivo
        # no esté es una inconsistencia, no un "no encontrado" cualquiera.
        return jsonify({'error': str(e)}), 410
    return Response(contenido, mimetype=foto.mime)


@custodia_bp.route('/custodia/<int:custodia_id>/fotos', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver las fotos de un turno')
def fotos_de_custodia(custodia_id):
    """Qué fotos quedaron de un turno, con su ángulo y su estado.

    Existe porque hasta el 2026-08-03 **no había forma de mirar una foto
    guardada**: el almacén escribía, el endpoint de archivo funcionaba, y
    ninguna pantalla los llamaba. Un almacén que solo puede leer un test no es
    un almacén — es el patrón de función-sin-caller sobre la evidencia misma.

    Devuelve las que faltan también: un ángulo sin foto es un hueco que alguien
    tiene que poder ver, no una ausencia que se disimula no listándola.
    """
    from flota.adaptadores.modelos import Custodia, Foto

    custodia = db.session.get(Custodia, custodia_id)
    if custodia is None:
        return jsonify({'error': f'No existe la custodia {custodia_id}'}), 404

    vehiculo = db.session.get(Vehiculo, custodia.vehiculo_id)
    ficha = db.session.get(FichaTecnica, custodia.vehiculo_id)
    n_llantas, fuente = posiciones_llanta(
        ficha.posiciones_llanta if ficha else None,
        vehiculo.tipo if vehiculo else '')

    filas = Foto.query.filter(
        Foto.entidad_tipo.in_(('custodia_inicio', 'custodia_fin')),
        Foto.entidad_id == custodia_id,
    ).order_by(Foto.id).all()

    return jsonify({
        'custodia_id': custodia_id,
        'placa': vehiculo.placa if vehiculo else None,
        'angulos_esperados': list(angulos_de_custodia(n_llantas)),
        'posiciones_llanta_fuente': fuente,
        'fotos': [{
            'id': f.id,
            # NULL para las anteriores a la migración del ángulo. Se dice, no se
            # adivina por el orden: el frontend filtra las faltantes antes de
            # enviar, así que la posición no identifica nada.
            'angulo': f.angulo,
            'clase': f.clase,
            'momento': f.entidad_tipo,
            'estado': f.estado,
            'ancho': f.ancho, 'alto': f.alto, 'bytes': f.bytes,
            'ts_captura': iso_utc(f.ts_captura),
        } for f in filas],
    }), 200


@custodia_bp.route('/custodia/fuera-de-sede', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver los vehiculos fuera de sede')
def fuera_de_sede():
    """Vehículos que están pasando la noche fuera del control de la empresa.

    No es un caso normal ni un detalle de ubicación: es un camión durmiendo
    afuera, bajo responsabilidad de una persona, con un motivo que alguien
    escribió. Tiene que **verse el lunes**, no descubrirse cuando aparezca un
    golpe y haya que reconstruir dónde estuvo.

    Sale con nombre del responsable. Si un vehículo aparece acá tres semanas
    seguidas, eso ya no es una excepción — es una costumbre que nadie decidió, y
    verla es el primer paso para decidirla.
    """
    from flota.adaptadores.modelos import Custodia

    filas = (
        db.session.query(Custodia, Vehiculo.placa)
        .join(Vehiculo, Vehiculo.id == Custodia.vehiculo_id)
        .filter(Custodia.ubicacion == Ubicacion.FUERA_DE_SEDE.value,
                Custodia.fin_ts.is_(None))
        .order_by(Custodia.inicio_ts.desc())
        .all()
    )
    return jsonify({'fuera_de_sede': [
        {
            'custodia_id': c.id,
            'placa': placa,
            'responde': (c.custodio_conductor.nombre
                         if c.custodio_conductor else 'sin nombre'),
            'desde': iso_utc(c.inicio_ts),
            'motivo': c.ubicacion_motivo,
            'km': c.km_inicio,
        }
        for c, placa in filas
    ]}), 200


@custodia_bp.route('/custodia/cierres-forzados', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver los cierres forzados')
def cierres_forzados():
    """Turnos cerrados sin la firma del custodio anterior, con nombre.

    Salida corta hasta que exista el notificador de la tanda 2: si el custodio
    anterior se entera tres días después, el daño de confianza ya está hecho.
    Acá al menos lo ve el control de flota el lunes.

    **La solución de verdad no es técnica**: el procedimiento FLO-PR-01 exige
    que quien fuerza un cierre le avise al custodio el mismo día, por el mismo
    WhatsApp por el que se hablan todos los días. El sistema registra y muestra;
    la cortesía la pone la persona. Cuando el notificador exista, deja de
    depender de que alguien se acuerde.
    """
    from app.models.usuario import Usuario
    from flota.adaptadores.modelos import Custodia

    filas = (
        db.session.query(Custodia, Vehiculo.placa, Usuario.nombre)
        .join(Vehiculo, Vehiculo.id == Custodia.vehiculo_id)
        .outerjoin(Usuario, Usuario.id == Custodia.cierre_forzado_por_usuario_id)
        .filter(Custodia.cierre_forzado.is_(True))
        .order_by(Custodia.fin_ts.desc())
        .all()
    )
    return jsonify({'cierres': [
        {
            'custodia_id': c.id,
            'placa': placa,
            # Quién lo tenía: es a quien hay que avisarle.
            'lo_tenia': c.custodio_conductor.nombre if c.custodio_conductor else 'sede',
            'forzado_por': forzador or 'desconocido',
            'cuando': iso_utc(c.fin_ts),
            'motivo': c.cierre_forzado_motivo,
        }
        for c, placa, forzador in filas
    ]}), 200


@custodia_bp.route('/custodia/traspaso', methods=['POST'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'registrar un traspaso de turno')
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
        ubicacion = (Ubicacion(datos['ubicacion'])
                     if datos.get('ubicacion') else None)
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Valor inválido: {e}'}), 400

    # Dónde está y quién responde son dos hechos, y la traducción entre ellos
    # vive en UNA función del dominio. Se impone acá porque el cliente no puede
    # elegir la combinación: un vehículo fuera de sede con custodia de sede
    # descarga de responsabilidad a quien efectivamente lo tiene.
    if ubicacion is not None:
        exigido = custodio_de_ubicacion(ubicacion)
        # `!=` y no `is not`. Comparar enums por IDENTIDAD asume que la clase se
        # cargó una sola vez, y con `flota/` fuera de `app/` el mismo módulo
        # puede quedar importado por dos rutas distintas: dos clases, miembros
        # distintos, `is` falso entre valores iguales.
        #
        # El síntoma fue un 400 diciendo «Un vehículo en «sede» queda bajo
        # custodia de tipo «sede», no «sede»» — un mensaje que compara un valor
        # consigo mismo y afirma que difieren. Lo encontró el test de día
        # completo, y solo al correr la suite entera: aislado pasaba.
        if tipo != exigido:
            return jsonify({
                'error': f'Un vehículo en «{ubicacion.value}» queda bajo custodia '
                         f'de tipo «{exigido.value}», no «{tipo.value}».',
                'motivo': ('Fuera de sede el vehículo sigue en manos del '
                           'conductor: pasarlo a la sede diría que responde '
                           'alguien que no lo vio.'
                           if ubicacion == Ubicacion.FUERA_DE_SEDE else
                           'En sede o taller responde la sede, no el conductor '
                           'que ya se fue.'),
            }), 400

    # `quien_pide` sale del ROL, jamás del cuerpo. Si viniera en el JSON, un
    # conductor mandaría `admin_zona` y le cerraría el turno a otro — la regla
    # entera se saltaría con un campo.
    quien = QuienPide.ADMIN_ZONA if _es_gestion() else QuienPide.CONDUCTOR

    try:
        nueva = traspaso.traspasar(
            vehiculo_id=vehiculo.id,
            km=km,
            quien_pide=quien,
            motivo_forzado=datos['motivo_forzado'] if 'motivo_forzado' in datos else None,
            registrado_por_usuario_id=_usuario_id(),
            custodio_tipo=tipo,
            custodio_conductor_id=datos['custodio_conductor_id'] if 'custodio_conductor_id' in datos else None,
            custodio_sede_id=datos['custodio_sede_id'] if 'custodio_sede_id' in datos else None,
            custodio_estado=estado,
            fotos_fin=datos['fotos_fin'] if 'fotos_fin' in datos else None,
            fotos_inicio=datos['fotos_inicio'] if 'fotos_inicio' in datos else None,
            ubicacion=ubicacion,
            ubicacion_motivo=datos['ubicacion_motivo'] if 'ubicacion_motivo' in datos else None,
        )
    except ErrorFlota as e:
        # 409: el estado del mundo no admite este traspaso. No es un error de
        # sintaxis del cliente ni una falla del servidor — es un "no se puede".
        return jsonify({'error': str(e)}), 409

    return jsonify({
        'custodia_id': nueva.id,
        'placa': vehiculo.placa,
        'inicio_ts': iso_utc(nueva.inicio_ts),
        'km_inicio': nueva.km_inicio,
        'linea_base': nueva.linea_base,
        'custodio_estado': nueva.custodio_estado,
    }), 201


@custodia_bp.route('/odometro', methods=['POST'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'registrar una lectura de odometro')
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

    # Que el origen exista en el enum no significa que ESTE gesto lo produzca.
    # `entrega` la escribe el traspaso; `preoperacional` y `ot` todavía no
    # tienen fuente. Aceptarlos acá deja una lectura apuntando a un padre que
    # no existe — y en el caso de `entrega`, un cambio de turno inventado.
    if origen not in ORIGENES_LECTURA_SUELTA:
        permitidos = ', '.join(o.value for o in ORIGENES_LECTURA_SUELTA)
        return jsonify({
            'error': f'Una lectura suelta no puede tener origen "{origen.value}". '
                     f'Válidos hoy: {permitidos}.',
            # Indexado directo, no `.get(x, '')`: el mapa es total sobre los
            # orígenes excluidos y un test del dominio lo mantiene así.
            'motivo': MOTIVO_ORIGEN_NO_SUELTO[origen],
        }), 400

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

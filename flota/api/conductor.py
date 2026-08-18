"""
Lo que ve el conductor en su app. Solo lo suyo.

`GET /flota/conductor/mi-turno` — qué vehículo le toca hoy y en qué estado está.
`GET /flota/conductor/mis-reportes` — sus hallazgos y en qué van.

No hay endpoint de escritura acá: el recibo de turno usa
`POST /flota/custodia/traspaso` como todos, pero con `quien_pide=conductor`, que
es lo que impide que le quite el turno a otro.

**La identidad sale del token, nunca del cuerpo.** Un conductor no puede pedir
el turno de otro cambiando un id en el JSON.
"""
from datetime import date

from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.utils.fecha import dia_operativo
from app.routes._auth_helpers import Roles
from flota.api._permisos import exige
from app.models.conductor import Conductor
from app.models.ruta_despacho import RutaDespacho
from app.models.vehiculo import Vehiculo
from flota.api._tiempo import iso_utc
from flota.adaptadores import traspaso
from flota.adaptadores.modelos import Custodia
from flota.dominio import odometro as dom_odo
from flota.dominio.turno import Candidato, resolver_vehiculo_del_dia
from flota.dominio.valores import SIN_DATO

conductor_bp = Blueprint('flota_conductor', __name__)


def _conductor_del_token():
    """El Conductor vinculado a la sesión, o None.

    Un usuario con rol conductor pero sin fila de `Conductor` no puede operar —
    y eso pasa: `usuario_id` es nullable y hasta el 2026-08-03 no había forma de
    vincular un conductor existente a una cuenta.
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    return Conductor.query.filter_by(usuario_id=uid, activo=True).first()


@conductor_bp.route('/conductor/mi-turno', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver tu turno')
def mi_turno():
    """Qué vehículo le toca hoy, de dónde salió esa placa, y si está libre."""
    conductor = _conductor_del_token()
    if conductor is None:
        return jsonify({
            'error': 'Tu usuario no está vinculado a un conductor',
            'detalle': 'Pedile a administración que te vincule la ficha; sin eso '
                       'la app no sabe qué vehículo es el tuyo.',
        }), 404

    vigente = Custodia.query.filter(
        Custodia.custodio_conductor_id == conductor.id,
        Custodia.fin_ts.is_(None),
    ).one_or_none()

    # La ruta de hoy es SUGERENCIA. Si no está armada, la cascada sigue de largo
    # y el conductor elige — nunca se queda sin poder registrar.
    ruta_hoy = RutaDespacho.query.filter(
        RutaDespacho.conductor_id == conductor.id,
        RutaDespacho.fecha_programada == dia_operativo(),
        RutaDespacho.vehiculo_id.isnot(None),
    ).first()

    # Quién tiene cada vehículo ahora — para poder decir el nombre en pantalla
    # en vez de un 409 crudo.
    #
    # Solo cuenta como "ocupado" si lo tiene un CONDUCTOR. Una sede no es un
    # custodio que haya que convencer de soltar: es donde queda un vehículo
    # bien entregado (ver `custodia.puede_recibir`), y cualquiera lo puede
    # tomar sin fricción — exactamente el caso de todos los días a las 5 a.m.
    # Marcarlo acá como ocupado lo dejaba deshabilitado en la lista aunque el
    # backend ya lo permitiera.
    ocupados = {
        c.vehiculo_id: c.custodio_conductor.nombre
        for c in Custodia.query.filter(Custodia.fin_ts.is_(None)).all()
        if c.custodio_conductor is not None
    }
    candidatos = [
        Candidato(vehiculo_id=v.id, placa=v.placa, tipo=v.tipo,
                  ocupado_por=(ocupados[v.id] if v.id in ocupados else None))
        for v in Vehiculo.query.filter(Vehiculo.activo.is_(True))
                               .order_by(Vehiculo.placa).all()
    ]

    turno = resolver_vehiculo_del_dia(
        custodia_activa=vigente,
        vehiculo_de_ruta_hoy=ruta_hoy.vehiculo_id if ruta_hoy else None,
        candidatos=candidatos,
    )

    km = SIN_DATO
    if turno.vehiculo_id is not None:
        from flota.api.custodia import _lecturas_dominio
        km = dom_odo.odometro_actual(_lecturas_dominio(turno.vehiculo_id))

    return jsonify({
        'conductor': {'id': conductor.id, 'nombre': conductor.nombre},
        'origen': turno.origen.value,
        'vehiculo_id': turno.vehiculo_id,
        'placa': turno.placa,
        'requiere_confirmacion': turno.requiere_confirmacion,
        # `sin_dato` viaja como palabra: un vehículo sin lecturas no tiene 0 km.
        'odometro_actual': km if km is not SIN_DATO else str(SIN_DATO),
        'tiene_turno_abierto': vigente is not None,
        'candidatos': [
            {'vehiculo_id': c.vehiculo_id, 'placa': c.placa, 'tipo': c.tipo,
             'ocupado_por': c.ocupado_por}
            for c in turno.candidatos
        ],
    }), 200


@conductor_bp.route('/conductor/mis-reportes', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver tus reportes')
def mis_reportes():
    """Sus turnos y en qué van.

    **"Mis reportes y en qué van" no es una comodidad: es lo que hace que la app
    sea el respaldo del conductor** y no un registro sobre él hecho por otro. Si
    reporta y no ve qué pasó con lo que reportó, deja de reportar.

    En la tanda 1 lo que hay son sus custodias — los hallazgos con plazo llegan
    en la tanda 2 y se suman acá.
    """
    conductor = _conductor_del_token()
    if conductor is None:
        return jsonify({'error': 'Tu usuario no está vinculado a un conductor'}), 404

    filas = (
        db.session.query(Custodia, Vehiculo.placa)
        .join(Vehiculo, Vehiculo.id == Custodia.vehiculo_id)
        .filter(Custodia.custodio_conductor_id == conductor.id)
        .order_by(Custodia.inicio_ts.desc())
        .limit(20)
        .all()
    )
    return jsonify({'turnos': [
        {
            'custodia_id': c.id,
            'placa': placa,
            'inicio': iso_utc(c.inicio_ts),
            'fin': iso_utc(c.fin_ts),
            'km_inicio': c.km_inicio,
            'km_fin': c.km_fin,
            'abierto': c.fin_ts is None,
            'linea_base': c.linea_base,
            # Que vea si le cerraron el turno a la fuerza, y por qué. Enterarse
            # tres días después por un tercero es lo que rompe la confianza.
            'cerrado_a_la_fuerza': c.cierre_forzado,
            'motivo_del_cierre_forzado': c.cierre_forzado_motivo,
        }
        for c, placa in filas
    ]}), 200


__all__ = ['conductor_bp']

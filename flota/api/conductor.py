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


def _preventivo_urgente(vehiculo_id):
    """Las tareas del plan que ya vencieron. Vacío si el preventivo no corre.

    QUÉ AFIRMA: que esas tareas pasaron su kilometraje.

    QUÉ NO AFIRMA: que el vehículo no pueda salir. Informa (regla 1).

    Una tarea **sin línea base** —nunca ejecutada— no entra: no está al día ni
    vencida, y decirle al conductor que «venció» algo que nadie hizo nunca es
    inventarle una deuda. El health la cuenta aparte como
    `tareas_sin_linea_base`.
    """
    from flota.adaptadores import preventivo
    from flota.dominio.preventivo import EstadoTarea

    try:
        plan = preventivo.diagnostico_de(vehiculo_id)
    except Exception:
        # Regla 5 al revés: si el preventivo revienta, el conductor tiene que
        # poder recibir su turno igual. Se degrada a "no hay nada que decir",
        # NO a "está todo bien" — la diferencia la lleva el health, que sí
        # falla ruidosamente cuando una medición no se puede hacer.
        return []
    return [d for d in plan if d.get('estado') == EstadoTarea.VENCIDA]


def _rendimiento_del_turno(vehiculo_id):
    """El km/galón del vehículo del turno, o por qué todavía no se muestra.

    **Tres estados, no dos.** «Hay número y se puede sostener», «hay número y
    todavía no» y «no hay número» se corrigen distinto: el primero no necesita
    nada, el segundo solo necesita que pase el tiempo, y el tercero necesita que
    alguien marque el tanque al tanquear. Colapsar los dos últimos haría que un
    vehículo midiendo desde hace un mes se viera igual que uno que nadie tanqueó
    nunca.

    Cuando `publicable` es `False` **el número no viaja**. No es prudencia
    excesiva: el que lo lee es la persona cuya conducción se está midiendo, y un
    `18 km/gal` sobre dos ventanas de la misma semana es ruido con su nombre
    encima. El panel de control de flota sí lo ve, con su advertencia; acá no.

    Sin vehículo asignado devuelve `None` — no un dict con ceros. «No tenés
    camión hoy» no es «tu camión rinde 0».
    """
    if vehiculo_id is None:
        return None
    from flota.adaptadores.gastos import numero_legible, rendimiento_publicable_de

    r = rendimiento_publicable_de(vehiculo_id)
    return {
        'km_galon': (numero_legible(r['km_galon']) if r['publicable']
                     else str(SIN_DATO)),
        'publicable': r['publicable'],
        'motivo': r['motivo'],
        # Los dos viajan SIEMPRE, publicable o no: son la condición 2 del dueño
        # y son lo que separa una medición de un promedio con autoridad
        # prestada.
        'ventanas': r['ventanas'],
        'tanqueos_fuera_por_parcial': r['tanqueos_fuera_por_parcial'],
        'dias_historia': r['dias_historia'],
        'base': ('kilómetros y galones sumados sobre las ventanas de tanque '
                 'lleno a tanque lleno de ESTE vehículo'),
        # Lo que el número NO afirma, en la pantalla y no en un instructivo
        # aparte. Es la regla 2: el sistema dice cuánto rindió el camión, no
        # quién lo manejó.
        'no_afirma': ('Mide el VEHÍCULO, no a quien maneja. Una ruta con más '
                      'montaña, un filtro tapado y un sifón dan el mismo '
                      'número.'),
    }


def _estado_del_vehiculo(vehiculo_id):
    """Lo que el conductor tiene que saber ANTES de arrancar.

    QUÉ AFIRMA: lo que el sistema sabe hoy del vehículo — daños vivos, si la
    inspección de hoy se hizo y con qué veredicto, y papeles vencidos.

    QUÉ NO AFIRMA: que el camión pueda o no salir. **Esto informa, no bloquea**
    — es la secuencia obligatoria del módulo (medir, corregir, imponer) y el
    criterio de la regla 1: dejar un camión en el patio por una pantalla es cómo
    la operación desmonta el sistema en 48 horas. Lo que hace es que el
    conductor no pueda decir «no sabía».

    `None` cuando no hay vehículo: no es «está todo bien», es que no hay a qué
    mirarle el estado (regla 4).
    """
    if vehiculo_id is None:
        return None

    from datetime import datetime

    from app.utils.fecha import dia_operativo
    from flota.adaptadores import hallazgos as dom_hallazgos
    from flota.adaptadores.modelos import DocumentoVehiculo, Inspeccion
    from flota.dominio.hallazgo import vencido
    from flota.dominio.inspeccion import habilita_despacho

    ahora = datetime.utcnow()
    abiertos = dom_hallazgos.abiertos_de(vehiculo_id)

    # La inspección de HOY en día operativo, no en UTC: a las 8 p.m. de Colombia
    # ya es mañana en UTC, y el conductor del turno de la tarde vería «no la
    # hiciste» sobre la que acaba de contestar.
    hoy = dia_operativo()
    insp = (Inspeccion.query
            .filter_by(vehiculo_id=vehiculo_id, dia=hoy)
            .order_by(Inspeccion.respondida_ts.desc()).first())

    vencidos = [d for d in DocumentoVehiculo.query.filter_by(
        vehiculo_id=vehiculo_id, estado='vigente').all()
        if d.fecha_vencimiento is not None
        and (d.fecha_vencimiento - hoy).days < 0]

    return {
        'hallazgos_abiertos': len(abiertos),
        'hallazgos_vencidos': sum(1 for h in abiertos
                                  if vencido(h.a_dominio(), ahora)),
        # El más grave que tiene vivo, para poder nombrarlo en una línea.
        'hallazgo_peor': (
            {'criticidad': abiertos[0].criticidad,
             'descripcion': abiertos[0].descripcion,
             'vencido': vencido(abiertos[0].a_dominio(), ahora)}
            if abiertos else None),
        'inspeccion_de_hoy': (
            {'hecha': True, 'veredicto': insp.veredicto,
             # **El consumidor que le faltaba.** `habilita_despacho` se
             # calculaba, se publicaba en la respuesta de la inspección, y su
             # único lector era un mensaje que desaparecía. Acá le llega a quien
             # tiene que decidir si arranca.
             'habilita_despacho': habilita_despacho(insp.veredicto)}
            if insp is not None else {'hecha': False, 'veredicto': None,
                                      'habilita_despacho': None}),
        # Preventivo: **lo que necesita SABER, no una pantalla para navegar.**
        #
        # `GET /flota/preventivo/<placa>` autoriza al conductor y solo lo ofrece
        # el panel del encargado. La pregunta correcta no era «¿le doy la
        # pantalla?» sino qué necesita: su panel dura dos minutos a las 5 a.m. y
        # no es un navegador de expedientes. Necesita saber que la correa está
        # vencida, no poder recorrer nueve tareas.
        #
        # Una correa que revienta en motor de interferencia es motor nuevo, y
        # `distribucion_km_cambio` llevaba meses en la base sin un solo lector.
        'preventivo_vencido': [
            {'tarea': d['tarea'], 'faltan_km': d.get('faltan_km')}
            for d in _preventivo_urgente(vehiculo_id)],
        'documentos_vencidos': [
            {'tipo': d.tipo, 'vencio': d.fecha_vencimiento.isoformat()}
            for d in vencidos],
    }


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
        # Qué le pasa AL CAMIÓN, no solo qué camión es.
        #
        # Hasta el 2026-09-03 esta respuesta traía ocho campos —quién sos, qué
        # placa, cuántos kilómetros— y **nada del estado del vehículo**. El
        # conductor escribía en el sistema y el sistema no le contestaba: no se
        # enteraba de un daño bloqueante vencido que él mismo había reportado,
        # ni de una tecnomecánica vencida, ni de si ya había hecho la inspección
        # de hoy.
        #
        # El encargado sí veía los contadores agregados en su tablero. El que
        # está parado al lado del camión a las 5 a.m. veía su placa.
        'estado_vehiculo': _estado_del_vehiculo(turno.vehiculo_id),
        # Rendimiento km/galón — el número que su ficha de procedimiento le
        # promete desde el 2026-08-04 (`piso-conductor.md:149`) y que el sistema
        # le negaba.
        #
        # **km/galón y NO el CPK.** El CPK divide por pesos que él no controla
        # —pólizas, impuestos, multas, una entrada a taller— y un número que
        # alguien no puede mover es un número que aprende a ignorar. Los
        # kilómetros por galón sí son lo que su conducción mueve.
        #
        # Las cuatro condiciones del dueño, y dónde se cumple cada una:
        #
        #   · agregado del VEHÍCULO, no de la persona → la firma de
        #     `rendimiento_publicable` no recibe conductor y no debe recibirlo
        #     nunca (regla 2, comprobada por `inspect.signature`);
        #   · declara sobre cuántas ventanas y cuántos tanqueos quedaron fuera
        #     → viajan `ventanas` y `tanqueos_fuera_por_parcial`;
        #   · sin ranking ni comparación entre personas → esto devuelve UN
        #     vehículo, el del turno. No hay forma de pedir una lista;
        #   · ≥6 ventanas y ≥60 días → `publicable`, con su motivo.
        'rendimiento': _rendimiento_del_turno(turno.vehiculo_id),
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

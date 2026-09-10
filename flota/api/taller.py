"""
Endpoints de taller y garantía — la visita, los trabajos y lo que cubren.

Nacen con su pantalla (`app/static/pwa/flota.js`), no antes. Un endpoint sin
forma de llamarse es la regla 12 rota y el patrón que ya apareció seis veces en
este repo: capacidad construida, probada, desplegada, y el gesto que la enciende
nunca escrito.

## Quién puede qué, y por qué es uno solo

| | Rol | Motivo |
|---|---|---|
| Todo lo de acá | `MAESTROS_FLOTA` (sin conductor) | Mandar un camión al taller, declarar qué se hizo y registrar la factura son decisiones de control de flota (FLO-PR-01), no del turno |

**No hay asimetría de roles en esta fase, y es una decisión, no un olvido.** En
el hallazgo la hay —el conductor reporta y no cierra— porque el que ve el golpe
es el que maneja. Acá el conductor no abre órdenes: no elige a qué taller va el
camión ni negocia una garantía. El día que se le quiera dar «pedir taller», eso
es un hallazgo bloqueante, que ya puede reportar.

`abierta_por_usuario_id` y `registrada_por_usuario_id` salen del token, nunca
del cuerpo. Quién dice que mandó el camión no lo elige quien manda el JSON.

## Lo que estos endpoints NO tienen

· **Ningún campo de garantía calculado en el cuerpo.** `garantia_hasta_fecha` y
  `garantia_hasta_km` **devuelven 400 si vienen**, no se ignoran. Es la
  divergencia consciente con `/flota/hallazgos`, que ignora una `fecha_limite`
  enviada («por eso ni se mira»): acá se eligió la puerta ruidosa, por el mismo
  motivo que `gastos._periodo` — ignorarlo deja a quien lo mandó creyendo que la
  garantía que escribió quedó registrada, y eso solo se descubre el día que se
  reclame.
· **Ningún verbo que decida quién paga.** Una garantía vigente es un argumento
  para llamar al taller, no una decisión tomada. El sistema **propone, no
  bloquea**.
· **Ningún borrado ni edición.** Una orden abierta por error se **anula** con
  motivo escrito; un trabajo mal registrado no se corrige todavía, y la primera
  corrección real que alguien pida define el gesto. Queda como deuda declarada
  en `ESTADO.md` con su condición de disparo.
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from app.utils.fecha import dia_operativo
from flota.adaptadores import taller as adaptador
from flota.adaptadores.gastos import numero_legible
from flota.adaptadores.modelos import OrdenTrabajo
from flota.api._permisos import DECIDE_FLOTA, MAESTROS_FLOTA, exige
from flota.api._tiempo import iso_utc
from flota.dominio import costos
from flota.dominio import taller as dom
from flota.dominio.errores import ErrorFlota

taller_bp = Blueprint('flota_taller', __name__)

#: Campos que la intervención **calcula** y que por lo tanto nadie puede mandar.
#:
#: Se rechazan con 400 en vez de ignorarse. Regla 6 del módulo aplicada a la
#: garantía: si alguien puede escribir «ésta vence en marzo», la garantía dejó
#: de significar algo — y un cuerpo ignorado en silencio deja a quien lo mandó
#: creyendo exactamente eso.
CAMPOS_CALCULADOS = ('garantia_hasta_fecha', 'garantia_hasta_km')


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _fecha(datos: dict, campo: str):
    """`YYYY-MM-DD` → `date`, obligatoria. Sin `.get(campo, hoy)`.

    Un default de hoy sobre la fecha de una factura la haría entrar al CPK del
    mes equivocado sin que nada falle. Regla 5.
    """
    if campo not in datos or datos[campo] in (None, ''):
        raise ValueError(f'falta {campo}')
    try:
        return date.fromisoformat(str(datos[campo]))
    except ValueError:
        raise ValueError(f'{campo} no es una fecha YYYY-MM-DD: {datos[campo]!r}')


def _json_orden(o: OrdenTrabajo, dia: date, km_actual) -> dict:
    """Una orden como la lee la pantalla, con sus trabajos y sus vigencias.

    Los juicios de vigencia los emite el **dominio** vía el adaptador, no un
    `if` acá: una regla escrita dos veces diverge, y la copia que diverge es la
    de la pantalla — la que la gente mira.
    """
    return {
        'id': o.id,
        'vehiculo_id': o.vehiculo_id,
        'tipo': o.tipo,
        'estado': o.estado,
        'taller': o.taller,
        'descripcion': o.descripcion,
        'lectura_id': o.lectura_id,
        'km': o.lectura.valor_km,
        'hallazgo_id': o.hallazgo_id,
        'abierta_ts': iso_utc(o.abierta_ts),
        'abierta_por_usuario_id': o.abierta_por_usuario_id,
        'cerrada_ts': iso_utc(o.cerrada_ts),
        'motivo_cierre': o.motivo_cierre,
        'intervenciones': adaptador.garantias_de(o, dia, km_actual),
        # Se publica para que la pantalla no lo deduzca contando `gasto_id` en
        # el arreglo de arriba. Deducirlo sería la segunda copia de la regla.
        'sin_factura': len(adaptador.sin_factura_de(o)),
    }


@taller_bp.route('/ordenes/<placa>', methods=['GET'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'ver las órdenes de trabajo de un vehículo')
def listar(placa):
    """Las órdenes del vehículo, y **las garantías que todavía cubren**.

    `garantias_vigentes` viaja en la misma respuesta y no en un endpoint aparte
    a propósito: la pregunta *«¿esto ya lo arreglamos y todavía está en
    garantía?»* se hace **mientras se llena el formulario de abrir la orden**, y
    un segundo viaje al servidor a mitad del formulario es un renglón que a
    veces no llega. La pantalla filtra por sistema sobre la lista ya juzgada —
    filtrar por igualdad de cadena no es copiar una política; recalcular la
    vigencia sí lo sería.

    El día lo da `dia_operativo()` y no `date.today()`: en Railway, a partir de
    las 7 p.m. de Colombia `today()` ya es mañana, y una garantía que vence hoy
    aparecería vencida la noche anterior.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    hoy = dia_operativo()
    km = adaptador.km_actual_de(vehiculo.id)
    ordenes = adaptador.ordenes_de(vehiculo.id)

    return jsonify({
        'placa': vehiculo.placa,
        'dia': hoy.isoformat(),
        # `null` y no 0: un vehículo sin lecturas no recorrió cero kilómetros,
        # no se sabe cuántos (regla 4). Con un 0, toda garantía por kilómetros
        # saldría vigente sobre un parque sin odómetro.
        'km_actual': km,
        'ordenes': [_json_orden(o, hoy, km) for o in ordenes],
        'abiertas': sum(1 for o in ordenes if o.estado == 'abierta'),
        'garantias_vigentes': adaptador.garantias_vigentes_de(vehiculo.id, hoy),
        'tipos': list(dom.TIPOS_OT),
        'sistemas': list(dom.SISTEMAS),
        'garantia_declarada': list(dom.GARANTIA_DECLARADA),
        'categorias_de_taller': list(adaptador.CATEGORIAS_DE_TALLER),
        'origenes_costo': list(costos.ORIGENES_COSTO),
    })


@taller_bp.route('/ordenes', methods=['POST'])
@jwt_required()
@exige(DECIDE_FLOTA, 'abrir una orden de trabajo')
def abrir():
    """Manda el camión al taller.

    El kilometraje es obligatorio (regla 3): sin él no se puede calcular hasta
    qué kilómetro cubre lo que se repare, ni auditar después si el mantenimiento
    era necesario.

    `hallazgo_id` es opcional y se **valida**: mismo vehículo y abierto. Un
    hallazgo ajeno colgado acá haría que cerrar la orden cerrara el daño
    equivocado.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'tipo', 'taller', 'descripcion', 'km')
                 if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        km = int(datos['km'])
        hallazgo_id = (int(datos['hallazgo_id'])
                       if 'hallazgo_id' in datos
                       and datos['hallazgo_id'] not in (None, '') else None)
    except (ValueError, TypeError):
        return jsonify({'error': f'km u hallazgo_id inválidos: '
                                 f'{datos["km"]!r}'}), 400

    try:
        fila = adaptador.abrir(
            vehiculo_id=vehiculo.id, tipo=datos['tipo'], taller=datos['taller'],
            descripcion=datos['descripcion'], km=km,
            abierta_por_usuario_id=_usuario_id(), hallazgo_id=hallazgo_id)
    except ErrorFlota as e:
        # 409 y no 400: el cuerpo puede estar perfecto y el estado del mundo no
        # admitirlo — un odómetro que retrocede o un hallazgo que alguien acaba
        # de cerrar son lo segundo.
        return jsonify({'error': str(e)}), 409

    hoy = dia_operativo()
    return jsonify(_json_orden(fila, hoy,
                               adaptador.km_actual_de(vehiculo.id))), 201


@taller_bp.route('/ordenes/<int:orden_id>/intervenciones', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'registrar un trabajo de taller')
def registrar_intervencion(orden_id):
    """Registra qué se hizo, con la garantía **calculada al nacer**.

    Un cuerpo que traiga `garantia_hasta_fecha` o `garantia_hasta_km` devuelve
    **400**, no un silencio. Ver `CAMPOS_CALCULADOS`.
    """
    datos = request.get_json(silent=True) or {}

    intrusos = [c for c in CAMPOS_CALCULADOS if c in datos]
    if intrusos:
        return jsonify({
            'error': f'{", ".join(intrusos)} no se manda: se calcula. La '
                     f'garantía sale del plazo que declara la factura '
                     f'(garantia_meses / garantia_km) contra el día y el '
                     f'kilometraje del trabajo. Si se pudiera escribir a mano, '
                     f'«vence en marzo» sería una frase y no una garantía.'
        }), 400

    faltantes = [c for c in ('sistema', 'garantia_declarada') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        fila = adaptador.registrar_intervencion(
            orden_trabajo_id=orden_id,
            sistema=datos['sistema'],
            garantia_declarada=datos['garantia_declarada'],
            registrada_por_usuario_id=_usuario_id(),
            descripcion=datos['descripcion'] if 'descripcion' in datos else None,
            garantia_meses=(datos['garantia_meses']
                            if 'garantia_meses' in datos else None),
            garantia_km=(datos['garantia_km']
                         if 'garantia_km' in datos else None),
        )
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    hoy = dia_operativo()
    km = adaptador.km_actual_de(fila.orden.vehiculo_id)
    return jsonify(adaptador.vigencia_de(fila, hoy, km)), 201


@taller_bp.route('/ordenes/<int:orden_id>/cerrar', methods=['POST'])
@jwt_required()
@exige(DECIDE_FLOTA, 'cerrar una orden de trabajo')
def cerrar(orden_id):
    """El vehículo volvió del taller. Puede cerrar el daño que la abrió.

    `cerrar_hallazgo` **nace en `False`**: un camión puede volver con el daño
    todavía abierto —faltó un repuesto, se arregló otra cosa— y cerrarlo por
    defecto inventaría una reparación. Y cuando se pide, se hace por
    `hallazgos.cerrar`: una segunda vía de cierre sería la que se olvide de mover
    `cerrado_ts`, el campo del que sale `dias_hallazgo_abierto`.
    """
    datos = request.get_json(silent=True) or {}
    try:
        fila = adaptador.cerrar(
            orden_trabajo_id=orden_id, usuario_id=_usuario_id(),
            cerrar_hallazgo=bool(datos['cerrar_hallazgo']
                                 if 'cerrar_hallazgo' in datos else False),
            nota=datos['nota'] if 'nota' in datos else None)
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    hoy = dia_operativo()
    return jsonify(_json_orden(fila, hoy,
                               adaptador.km_actual_de(fila.vehiculo_id)))


@taller_bp.route('/ordenes/<int:orden_id>/anular', methods=['POST'])
@jwt_required()
@exige(DECIDE_FLOTA, 'anular una orden de trabajo')
def anular(orden_id):
    """La visita no ocurrió. Exige motivo escrito y **no toca el hallazgo**.

    Si la visita se anuló, el daño sigue ahí: cerrarlo acá inventaría una
    reparación que nadie hizo.
    """
    datos = request.get_json(silent=True) or {}
    try:
        fila = adaptador.anular(
            orden_trabajo_id=orden_id, usuario_id=_usuario_id(),
            motivo=(datos['motivo'] if 'motivo' in datos else '') or '')
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    hoy = dia_operativo()
    return jsonify(_json_orden(fila, hoy,
                               adaptador.km_actual_de(fila.vehiculo_id)))


@taller_bp.route('/ordenes/<int:orden_id>/factura', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'registrar la factura de una orden de trabajo')
def registrar_factura(orden_id):
    """La factura llegó: escribe el gasto y lo cuelga de los trabajos.

    **No pide kilometraje.** La factura se digita treinta días después, en una
    oficina, sin el vehículo delante: el gasto se ancla a la lectura de la orden,
    que es el odómetro al que el trabajo se hizo. Ver el encabezado de
    `flota/adaptadores/taller.py`.

    `intervenciones` es obligatorio y no tiene default de «todas»: una factura
    de taller puede cubrir dos de los tres trabajos de la visita, y asumir que
    los cubre todos dejaría el tercero contado como facturado sin que nadie lo
    mirara.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('intervenciones', 'categoria', 'fecha', 'valor',
                             'proveedor', 'origen_costo') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        fecha = _fecha(datos, 'fecha')
        ids = [int(x) for x in (datos['intervenciones'] or [])]
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400

    try:
        gasto = adaptador.registrar_factura(
            orden_trabajo_id=orden_id,
            intervencion_ids=ids,
            categoria=datos['categoria'],
            fecha=fecha,
            valor=datos['valor'],
            proveedor=datos['proveedor'],
            origen_costo=datos['origen_costo'],
            registrado_por_usuario_id=_usuario_id(),
            documento_numero=(datos['documento_numero']
                              if 'documento_numero' in datos else None),
            centro_op=datos['centro_op'] if 'centro_op' in datos else None,
            descripcion=datos['descripcion'] if 'descripcion' in datos else None,
        )
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify({
        'gasto_id': gasto.id,
        'categoria': gasto.categoria,
        'valor': numero_legible(gasto.valor),
        'documento_numero': gasto.documento_numero,
        'km': gasto.lectura.valor_km,
        'intervenciones': sorted(ids),
        # Se dice acá y no solo en el tablero: quien registra la factura es
        # quien puede conseguir el número, y decírselo después es tarde.
        'sin_documento': gasto.documento_numero is None,
    }), 201


__all__ = ['taller_bp', 'CAMPOS_CALCULADOS']

"""
Endpoints de la plata que sale — gastos y tanqueos.

Nacen con su pantalla (`app/static/pwa/flota.js`), no antes. Un endpoint sin
forma de llamarse es la regla 12 rota y el patrón que ya apareció cinco veces en
este repo: capacidad construida, probada, desplegada, y el gesto que la enciende
nunca escrito.

## Quién puede qué, y por qué no es lo mismo

| | Rol | Motivo |
|---|---|---|
| **Registrar un tanqueo** | `LECTURA_FLOTA` (incluye conductor) | El que tanquea es el que maneja. Un tanqueo que solo puede registrar un jefe se registra el lunes, y para el lunes la factura ya se perdió — que es literalmente lo que pasa hoy. Mismo criterio que reportar un daño |
| **Registrar cualquier otro gasto** | `MAESTROS_FLOTA` (sin conductor) | El SOAT, el impuesto vehicular y una entrada a taller no los paga el conductor. Son maestros del vehículo, como la ficha y los documentos, y el rol que los levanta es el de control de flota (FLO-PR-01) |
| **Ver los gastos y el CPK** | `MAESTROS_FLOTA` (sin conductor) | *«¿Cuánto cuesta este camión?»* no es una pregunta del turno. Y un CPK visible en la pantalla del conductor está a un paso de leerse como una medida suya, que es exactamente lo que la regla 2 prohíbe — el número no mide a nadie y la pantalla no debe insinuar que sí |

`registrado_por_usuario_id` sale del token, nunca del cuerpo. Quién dice que
registró el gasto no lo elige quien manda el JSON.

## Lo que estos endpoints NO tienen

· **Ningún verbo de aprobación.** En esta fase nadie aprueba un gasto dentro del
  WMS: la aprobación real es la causación en Siesa, y un segundo «aprobado» acá
  sería un estado que contradice al ERP sin poder corregirlo. Si la respuesta
  del dueño a *«¿tarjeta o efectivo del conductor?»* es efectivo, cada registro
  pasa a ser también una legalización de gasto y **ahí** aparece la pregunta de
  quién aprueba — se decide cuando la respuesta llegue, no antes.
· **Ningún borrado ni edición.** Una factura mal digitada se corrige con el
  mismo gesto con que se corrige un odómetro: registrando el hecho, no
  reescribiendo el anterior. Queda como deuda declarada en `ESTADO.md` con su
  condición de disparo, porque hoy no hay una sola fila que corregir.
· **Ningún endpoint por conductor.** No existe la pregunta «cuánto gastó
  Fulano»: no hay columna de conductor en la tabla y no la va a haber (regla 2).
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from app.routes._auth_helpers import Roles
from app.utils.fecha import dia_operativo
from flota.adaptadores import gastos as adaptador
from flota.adaptadores.gastos import numero_legible
from flota.adaptadores.medicion import _motivo_cpk
from flota.adaptadores.modelos import Gasto
from flota.api._permisos import MAESTROS_FLOTA, exige
from flota.dominio import costos
from flota.dominio.errores import ErrorFlota, PermisoInsuficiente
from flota.dominio.valores import palabra_de_confianza

gastos_bp = Blueprint('flota_gastos', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _fecha(datos: dict, campo: str, obligatorio: bool):
    """`YYYY-MM-DD` → `date`. Sin `.get(campo, hoy)`.

    Un default de hoy sobre `periodo_desde` haría que un SOAT anual mal enviado
    se repartiera sobre el día en curso y el CPK del mes se disparara sin que
    nada falle. Regla 5.
    """
    if campo not in datos or datos[campo] in (None, ''):
        if obligatorio:
            raise ValueError(f'falta {campo}')
        return None
    try:
        return date.fromisoformat(str(datos[campo]))
    except ValueError:
        raise ValueError(f'{campo} no es una fecha YYYY-MM-DD: {datos[campo]!r}')


def _json_gasto(g: Gasto) -> dict:
    """Un gasto como lo lee la pantalla.

    `precio_galon` se **calcula** y no se guarda: con `valor` y `galones` en la
    fila, una tercera columna con el precio contradice a las otras dos el día
    que alguien corrija una factura mal digitada.

    `excede_capacidad` viaja con las tres respuestas posibles —`true`, `false` y
    `"sin_dato"`—, y la tercera no es un `false` disfrazado: significa que la
    ficha no tiene capacidad y **no hay contra qué revisar**.
    """
    tq = g.tanqueo
    salida = {
        'id': g.id,
        'categoria': g.categoria,
        'fecha': g.fecha.isoformat(),
        'valor': numero_legible(g.valor),
        'proveedor': g.proveedor,
        'documento_numero': g.documento_numero,
        'centro_op': g.centro_op,
        'origen_costo': g.origen_costo,
        'descripcion': g.descripcion,
        'periodo_desde': g.periodo_desde.isoformat(),
        'periodo_hasta': g.periodo_hasta.isoformat(),
        'cubre_periodo': costos.exige_periodo(g.categoria),
        'lectura_id': g.lectura_id,
        'km': g.lectura.valor_km,
        'registrado_por_usuario_id': g.registrado_por_usuario_id,
        'tanqueo': None,
    }
    if tq is not None:
        salida['tanqueo'] = {
            'galones': numero_legible(tq.galones, 3),
            'tanque': tq.tanque,
            'estacion': tq.estacion,
            'precio_galon': numero_legible(costos.precio_por_galon(
                valor=g.valor, galones=tq.galones)),
            'excede_capacidad': adaptador.excede_capacidad_de(tq),
        }
    return salida


@gastos_bp.route('/gastos/<placa>', methods=['GET'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'ver los gastos de un vehículo')
def listar(placa):
    """Los gastos del vehículo, con el CPK del mes y el rendimiento.

    La ventana por defecto es **el mes en curso en Bogotá**, no los últimos 30
    días: el CPK se compara contra el mes anterior y una ventana móvil no se
    puede comparar con nada. `?desde=&hasta=` la mueve.

    El día lo da `dia_operativo()` y no `date.today()`: en Railway, a partir de
    las 7 p.m. de Colombia `today()` ya es mañana, y el 31 de marzo a las 8 p.m.
    el CPK «del mes» sería el de abril con un día.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    hoy = dia_operativo()
    try:
        desde = _fecha(request.args, 'desde', obligatorio=False) \
            or hoy.replace(day=1)
        hasta = _fecha(request.args, 'hasta', obligatorio=False) or hoy
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if hasta < desde:
        return jsonify({'error': f'ventana invertida: {hasta} < {desde}'}), 400

    filas = (Gasto.query.filter_by(vehiculo_id=vehiculo.id)
             .order_by(Gasto.fecha.desc(), Gasto.id.desc()).all())
    cpk = adaptador.cpk_de(vehiculo.id, desde, hasta)
    capacidad = adaptador.capacidad_de_tanque(vehiculo.id)

    return jsonify({
        'placa': vehiculo.placa,
        'desde': desde.isoformat(),
        'hasta': hasta.isoformat(),
        'gastos': [_json_gasto(g) for g in filas],
        # El CPK sale con sus dos insumos. Un número suelto no se puede auditar
        # y este va a un tablero: quien lo mire tiene que poder rehacer la
        # división.
        'cpk': numero_legible(cpk['cpk']),
        # `palabra_de_confianza` y no `str()`: hoy es un no-op porque la marca
        # ya viene convertida, y se cambia igual — el día que vuelva a ser un
        # `Confianza`, `str()` publicaría `'Confianza.DUDOSA'` en la pantalla.
        'cpk_marca': palabra_de_confianza(cpk['marca']),
        # El MISMO motivo que publica el tablero (`medicion._motivo_cpk`), no
        # una segunda explicación escrita acá. Dos textos para la misma causa
        # divergen, y el que diverge es el que menos gente lee.
        'cpk_motivo': _motivo_cpk(cpk),
        'pesos_imputados': numero_legible(cpk['pesos']),
        'km_recorridos': cpk['km'],
        'lecturas_en_ventana': cpk['lecturas'],
        'rendimiento_km_galon': numero_legible(adaptador.rendimiento_de(vehiculo.id)),
        # La capacidad viaja para que la pantalla pueda decir POR QUÉ un tanqueo
        # salió `sin_dato` en vez de dejar el hueco sin explicación.
        'capacidad_tanque_galones': (numero_legible(capacidad) if capacidad is not None
                                     else None),
        'categorias': list(costos.CATEGORIAS_GASTO),
        'categorias_con_periodo': list(costos.CATEGORIAS_CON_PERIODO),
        'categorias_de_campo': sorted(adaptador.ORIGEN_DE_LECTURA),
        'origenes_costo': list(costos.ORIGENES_COSTO),
        'estados_tanque': list(costos.ESTADOS_TANQUE),
    })


@gastos_bp.route('/gastos', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'registrar un gasto de flota')
def registrar():
    """Registra un gasto que no es un tanqueo.

    El período **no se elige con una casilla**: lo decide la categoría
    (`costos.exige_periodo`). Mandar un período sobre una categoría que se
    consume el mismo día devuelve 400 en vez de ignorarlo — ignorarlo dejaría a
    quien lo mandó creyendo que el reparto ocurrió.

    El kilometraje solo se pide para las categorías **de campo**; las de
    escritorio se cuelgan de la última lectura conocida. Ver el encabezado de
    `flota/adaptadores/gastos.py`.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'categoria', 'fecha', 'valor',
                             'proveedor', 'origen_costo') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400
    if datos['categoria'] == 'combustible':
        return jsonify({
            'error': 'un tanqueo se registra en /flota/tanqueos: sin galones, '
                     'estación y estado del tanque, el gasto entra al costo por '
                     'kilómetro y no aporta un solo galón al rendimiento.'
        }), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        fecha = _fecha(datos, 'fecha', obligatorio=True)
        desde = _fecha(datos, 'periodo_desde', obligatorio=False)
        hasta = _fecha(datos, 'periodo_hasta', obligatorio=False)
        # Sin `.get('km')`: el `None` implícito de un `.get` de un solo
        # argumento es un default igual que cualquier otro, y acá decide si el
        # gasto se ancla a una lectura nueva o a la última conocida.
        km = (int(datos['km'])
              if 'km' in datos and datos['km'] not in (None, '') else None)
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400

    try:
        fila = adaptador.registrar_gasto(
            vehiculo_id=vehiculo.id,
            categoria=datos['categoria'],
            fecha=fecha,
            valor=datos['valor'],
            proveedor=datos['proveedor'],
            origen_costo=datos['origen_costo'],
            registrado_por_usuario_id=_usuario_id(),
            km=km,
            documento_numero=datos['documento_numero'] if 'documento_numero' in datos else None,
            centro_op=datos['centro_op'] if 'centro_op' in datos else None,
            descripcion=datos['descripcion'] if 'descripcion' in datos else None,
            periodo_desde=desde, periodo_hasta=hasta,
        )
    except ErrorFlota as e:
        # 409 y no 400: el cuerpo puede estar perfecto y el estado del mundo no
        # admitirlo — un vehículo sin ninguna lectura de odómetro es lo segundo.
        return jsonify({'error': str(e)}), 409

    return jsonify(_json_gasto(fila)), 201


@gastos_bp.route('/tanqueos', methods=['POST'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'registrar un tanqueo')
def registrar_tanqueo():
    """Registra un tanqueo: el gasto y su extremidad, en una transacción.

    `tanque` es obligatorio y **no trae ninguna opción marcada**: el rendimiento
    solo existe de tanque lleno a tanque lleno, y un `lleno` puesto por inercia
    no produce un error — produce una ventana basura que infla o hunde la
    métrica sin que nada se vea raro.

    La respuesta trae `excede_capacidad`. **No bloquea el registro**: es la
    secuencia obligatoria del módulo —medir, corregir, imponer—. Y no acusa a
    nadie: afirma que la capacidad de la ficha y los galones registrados no
    pueden ser los dos ciertos.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'fecha', 'valor', 'galones', 'tanque',
                             'estacion', 'km', 'proveedor', 'origen_costo')
                 if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        fecha = _fecha(datos, 'fecha', obligatorio=True)
        km = int(datos['km'])
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400

    try:
        fila = adaptador.registrar_tanqueo(
            vehiculo_id=vehiculo.id, fecha=fecha, valor=datos['valor'],
            galones=datos['galones'], tanque=datos['tanque'],
            estacion=datos['estacion'], km=km, proveedor=datos['proveedor'],
            origen_costo=datos['origen_costo'],
            registrado_por_usuario_id=_usuario_id(),
            documento_numero=datos['documento_numero'] if 'documento_numero' in datos else None,
            centro_op=datos['centro_op'] if 'centro_op' in datos else None,
            descripcion=datos['descripcion'] if 'descripcion' in datos else None,
        )
    except PermisoInsuficiente as e:
        return jsonify({'error': str(e)}), 403
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    salida = _json_gasto(fila.gasto)
    return jsonify(salida), 201


__all__ = ['gastos_bp']

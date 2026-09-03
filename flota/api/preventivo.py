"""
Endpoints del mantenimiento preventivo — la correa que envejece mientras la
ficha ya dice a qué kilometraje toca cambiarla.

Nacen con su pantalla (`app/static/pwa/flota.js`), no antes. Un endpoint sin
forma de llamarse es el patrón que este repo lleva pagando toda la semana:
capacidad construida, probada, desplegada, y el gesto que la enciende nunca
escrito.

## Quién puede qué, y por qué no es lo mismo

| | Rol | Motivo |
|---|---|---|
| Ver el plan | `LECTURA_FLOTA` (incluye conductor) | El que maneja el camión tiene derecho a saber que la correa está vencida. Es la información que decide si sale a ruta |
| Sembrar desde la ficha | `MAESTROS_FLOTA` | Escribe el plan del vehículo, que es un maestro. Mismo criterio que la ficha de la que sale |
| Fijar el intervalo | `MAESTROS_FLOTA` | Es escribir un número con autoridad sobre un vehículo, con su procedencia. Exactamente lo que `km_inicial` enseñó a no dejar en manos de cualquiera |
| Registrar que se hizo | `MAESTROS_FLOTA` | **Regla 11.** La forma de maximizar este tablero sin hacer el trabajo es marcar todo como hecho: reinicia el reloj de las seis tareas en veinte segundos y el vehículo aparece impecable. Si el que declara «ya se cambió» es el mismo que se beneficia de que no salga en rojo, el registro queda decorativo |

`registrado_por_usuario_id` sale del token, nunca del cuerpo.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from app.routes._auth_helpers import Roles
from flota.adaptadores import preventivo as adaptador
from flota.api._permisos import MAESTROS_FLOTA, exige
from flota.dominio.errores import ErrorFlota
from flota.dominio.preventivo import (DIAS_AVISO_PREVENTIVO,
                                      ESTADOS_QUE_PIDEN_TALLER, PlanInvalido)

preventivo_bp = Blueprint('flota_preventivo', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _numero(valor):
    """Un `Decimal` como número JSON, dejando pasar `SIN_DATO` tal cual.

    `jsonify` no sabe serializar `Decimal` y `str(Decimal)` mandaría
    `'12.3333333'` a la pantalla. `SIN_DATO` es una cadena y viaja entera: lo
    que ve quien lee es la misma palabra que está en el dominio.
    """
    from decimal import Decimal

    if isinstance(valor, Decimal):
        return float(round(valor, 2))
    return valor


def _ritmo_json(ritmo) -> dict:
    """El km/día del vehículo **con todo lo que hace falta para dudar de él**.

    `n` y `dias` no son adorno: el mismo cociente sobre dos lecturas de un día
    y sobre cuarenta de tres meses son dos números distintos, y sin ellos quien
    lo lea no puede distinguirlos. `marca` es la confianza del tramo, calculada
    por `confianza_del_tramo` y no reimplementada acá.
    """
    return {'km_dia': _numero(ritmo.km_dia), 'marca': str(ritmo.marca),
            'n': ritmo.n, 'dias': _numero(ritmo.dias), 'motivo': ritmo.motivo}


@preventivo_bp.route('/preventivo/<placa>', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver el plan preventivo de un vehículo')
def ver_plan(placa):
    """El plan del vehículo, cada tarea con su estado y sus insumos.

    Devuelve también el `ritmo` y `dias_aviso`. Los dos van a propósito:

    · el **ritmo** es lo que convierte «faltan 500 km» en «unos 6 días», y sin
      publicarlo la pantalla mostraría una proyección sin decir sobre qué está
      parada;
    · **`dias_aviso`** es el único umbral de esta fase. Publicarlo hace que la
      pantalla pueda decir «por vencer = dentro de 15 días» en vez de dejar al
      que mira adivinando por qué una tarea está amarilla. Un umbral que la
      interfaz no nombra es un umbral que nadie discute.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    tareas = adaptador.diagnostico_de(vehiculo.id)
    return jsonify({
        'placa': vehiculo.placa,
        'tareas': tareas,
        'ritmo': _ritmo_json(adaptador.ritmo_de(vehiculo.id)),
        'dias_aviso': DIAS_AVISO_PREVENTIVO,
        # Contadores separados y no un total: «una vencida» y «tres sin
        # intervalo» se atienden con dos llamadas distintas, a dos personas
        # distintas. Sumarlos esconde la que urge.
        'piden_taller': sum(1 for t in tareas
                            if t['estado'] in ESTADOS_QUE_PIDEN_TALLER),
    })


@preventivo_bp.route('/preventivo/<placa>/sembrar', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'sembrar el plan preventivo desde la ficha')
def sembrar(placa):
    """Traduce la ficha técnica del vehículo a filas de plan. Idempotente.

    No recibe cuerpo: **no hay nada que elegir**. Qué tareas propone el
    vehículo lo dice su ficha, y dejar que el cliente mandara la lista sería
    dejarle inventar un plan que la ficha no respalda.

    Devuelve 200 y no 201 aunque cree filas: la operación es «dejá el plan
    igual a la ficha», y correrla dos veces seguidas es correcta y no crea
    nada la segunda vez. Un 201 diría que hubo un recurso nuevo, y la mitad de
    las veces no lo hay.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        resumen = adaptador.sembrar_desde_ficha(vehiculo.id)
    except ErrorFlota as e:
        # 409 y no 400: el pedido está perfecto y el estado del mundo no lo
        # admite — un vehículo sin ficha es lo segundo.
        return jsonify({'error': str(e)}), 409

    return jsonify({'placa': vehiculo.placa, 'resumen': resumen,
                    'tareas': adaptador.diagnostico_de(vehiculo.id)})


@preventivo_bp.route('/preventivo/tarea/<int:plan_id>/ejecucion',
                     methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'registrar que una tarea preventiva se hizo')
def registrar_ejecucion(plan_id):
    """Se hizo. Establece la línea base contra la que se cuenta el próximo.

    `km` es obligatorio (regla 3) y no se acepta «el último conocido»: ese es
    exactamente el default peligroso que este módulo tiene prohibido. Quien
    registra el cambio de aceite miró el tablero, o no debería estar
    registrándolo.
    """
    datos = request.get_json(silent=True) or {}
    if 'km' not in datos:
        return jsonify({'error': 'Campo requerido: km'}), 400
    try:
        km = int(datos['km'])
    except (ValueError, TypeError):
        return jsonify({'error': f'km inválido: {datos["km"]!r}'}), 400

    try:
        fila = adaptador.registrar_ejecucion(
            plan_id=plan_id, km=km, usuario_id=_usuario_id(),
            taller=datos['taller'] if 'taller' in datos else None,
            nota=datos['nota'] if 'nota' in datos else None)
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    plan = fila.plan
    return jsonify({
        'ejecucion_id': fila.id,
        'plan_id': fila.plan_id,
        'placa': plan.vehiculo.placa if plan.vehiculo is not None else '',
        'tareas': adaptador.diagnostico_de(plan.vehiculo_id),
    }), 201


@preventivo_bp.route('/preventivo/tarea/<int:plan_id>', methods=['PUT'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'fijar el intervalo de una tarea preventiva')
def fijar(plan_id):
    """El intervalo que la ficha no trae, **con de dónde salió**.

    `fuente` es obligatoria en el cuerpo y no tiene default. Es la decisión 2
    del plan hecha frontera: un intervalo que entrara sin procedencia se
    mostraría después idéntico a uno leído del manual del fabricante, y quien
    lo mire no tendría cómo saber que se lo dijo alguien por teléfono.

    `intervalo_km: null` es legítimo y significa «retiro el número»: la tarea
    vuelve a `sin_intervalo`, que es lo cierto, en vez de quedarse con uno que
    resultó estar mal.
    """
    datos = request.get_json(silent=True) or {}
    if 'fuente' not in datos:
        return jsonify({
            'error': 'Campo requerido: fuente. Un intervalo sin procedencia se '
                     'lee después como si alguien lo hubiera verificado.'}), 400

    intervalo = datos['intervalo_km'] if 'intervalo_km' in datos else None
    if intervalo is not None:
        try:
            intervalo = int(intervalo)
        except (ValueError, TypeError):
            return jsonify({'error': f'intervalo_km inválido: {intervalo!r}'}), 400

    try:
        fila = adaptador.fijar_intervalo(
            plan_id=plan_id, intervalo_km=intervalo, fuente=datos['fuente'],
            activo=datos['activo'] if 'activo' in datos else None,
            nota=datos['nota'] if 'nota' in datos else None)
    except PlanInvalido as e:
        # Vocabulario desconocido: es un cuerpo malo, no un estado del mundo.
        return jsonify({'error': str(e)}), 400
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify({'plan_id': fila.id,
                    'tareas': adaptador.diagnostico_de(fila.vehiculo_id)})


__all__ = ['preventivo_bp']

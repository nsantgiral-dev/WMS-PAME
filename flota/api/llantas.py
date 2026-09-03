"""
Endpoints de llantas — el alta, el montaje y el desmontaje.

Nacen con su pantalla (`app/static/pwa/flota.js`), no antes. Un endpoint sin
forma de llamarse es la regla 12 rota y el patrón que ya apareció seis veces en
este repo: capacidad construida, probada, desplegada, y el gesto que la enciende
nunca escrito.

## Quién puede qué

| | Rol | Motivo |
|---|---|---|
| **Todo** (alta, montaje, desmontaje, ver) | `MAESTROS_FLOTA` (sin conductor) | Una llanta es un activo del vehículo, como la ficha y los documentos, y quien la monta es el taller o control de flota (FLO-PR-01). El conductor no la registra porque el código de la llanta se lee con el camión en el gato, no en ruta |

**Y esa decisión es la que hay que medir, no la que hay que defender.** El
conductor que cambia una pinchada en carretera hoy no tiene dónde decirlo: lo
que puede hacer es reportar un hallazgo, que sí es suyo. Si resulta que el
montaje del repuesto es un gesto que solo el conductor puede hacer a tiempo —y
que por no poder hacerlo no se hace nunca—, **el rol cambia**; medir primero es
la secuencia obligatoria del módulo. Queda declarado en `ESTADO.md` con su
condición de disparo en vez de resuelto a ojo hoy.

`registrada_por_usuario_id` y `montada_por_usuario_id` salen del token, nunca del
cuerpo. Quién dice que registró no lo elige quien manda el JSON.

## Lo que estos endpoints NO tienen

· **Ninguna baja de llanta.** Todavía no se dio de baja una sola; el gesto lo
  define la primera que ocurra de verdad (regla 12).
· **Ningún borrado ni edición.** Un montaje mal registrado se corrige como un
  odómetro: registrando el hecho. Hoy no hay una sola fila que corregir.
· **Ningún umbral de vida útil.** Regla 13: no hay una sola llanta medida en esta
  flota. Se publica el hecho por posición y cuántas vidas faltan para poder
  fijarlo con dato.
· **Ningún endpoint por conductor.** No existe la pregunta «a quién se le gastan
  más las llantas»: no hay columna de conductor en la tabla y no la va a haber
  (regla 2).
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from flota.adaptadores import llantas as adaptador
from flota.adaptadores.modelos import MontajeLlanta
from flota.api._permisos import MAESTROS_FLOTA, exige
from flota.dominio import llantas as dom
from flota.dominio.errores import ErrorFlota

llantas_bp = Blueprint('flota_llantas', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _json_montaje(m: MontajeLlanta) -> dict:
    """Un montaje como lo lee la pantalla, **con su kilometraje calculado**.

    `km` viaja con sus tres respuestas posibles —un número, `"vigente"` y
    `"sin_dato"`— y las tres son distintas. `"vigente"` **no es un cero
    disfrazado**: la llanta sigue puesta y su vida no terminó. `"sin_dato"`
    tampoco: los dos extremos del tramo son kilometrajes en duda y no hay número
    que publicar hasta que alguien pase por la cola de verificación.

    Se calcula acá y no se guarda: con 24 llantas, una columna `km_acumulado`
    es garantía de divergencia.
    """
    km, marca = adaptador.tramo_de(m)
    return {
        'id': m.id,
        'llanta_id': m.llanta_id,
        'codigo': m.llanta.codigo,
        'marca_llanta': m.llanta.marca,
        'medida': m.llanta.medida,
        'posicion': m.posicion,
        'inicio': m.inicio_ts.isoformat(),
        'fin': m.fin_ts.isoformat() if m.fin_ts is not None else None,
        'vigente': m.fin_ts is None,
        'km_inicio': m.lectura_inicio.valor_km,
        'km_fin': (m.lectura_fin.valor_km if m.lectura_fin is not None else None),
        'km': str(km),
        'km_marca': str(marca),
        'motivo_desmontaje': m.motivo_desmontaje,
        'gasto_id': m.gasto_id,
        'observacion': m.observacion,
    }


def _json_llanta(ll) -> dict:
    """Una llanta del catálogo, con su acumulado y dónde está.

    El último motivo de desmontaje viaja **a propósito**: hoy no hay baja de
    llanta (regla 12), así que una desmontada por `corte_flanco` sigue en el
    desplegable de montaje. Que quien elige lo vea es lo único que impide que
    vuelva al camión sin que nadie se entere.
    """
    km, marca = adaptador.km_de_llanta(ll.id)
    historia = adaptador.historia_de(ll.id)
    ultimo = historia[-1] if historia else None
    return {
        'id': ll.id,
        'codigo': ll.codigo,
        'marca': ll.marca,
        'medida': ll.medida,
        'km_acumulado': str(km),
        'km_marca': str(marca),
        'montajes': len(historia),
        'ultimo_motivo': (ultimo.motivo_desmontaje if ultimo is not None
                          else None),
    }


@llantas_bp.route('/llantas/<placa>', methods=['GET'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'ver las llantas de un vehículo')
def expediente(placa):
    """El expediente de llantas del vehículo: qué hay puesto, qué falta, qué se
    midió.

    `posiciones_libres` sale como lista y **puede salir `"sin_dato"`**: un
    vehículo sin ficha técnica no declara cuántas posiciones tiene, y `[]` ahí
    significaría «están todas cubiertas». Es el mismo corazón que
    `excede_capacidad` con la capacidad del tanque: sin ficha no hay contra qué
    revisar, y un vehículo sin ficha saldría limpio para siempre.

    `km_por_posicion` es un **hecho** y no una vida útil: cada posición trae
    cuántas vidas completas se midieron, cuáles fueron, y cuántas faltan para
    poder publicar una mediana (regla 13).
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    exp = adaptador.expediente_de(vehiculo.id)
    libres = exp['libres']
    return jsonify({
        'placa': vehiculo.placa,
        'posiciones_declaradas': exp['posiciones_declaradas'],
        'posiciones_libres': libres if isinstance(libres, list) else str(libres),
        'montajes': [_json_montaje(m) for m in exp['montajes']],
        'km_por_posicion': [
            {**f, 'mediana_km': str(f['mediana_km'])}
            for f in exp['km_por_posicion']],
        # El catálogo de las que se pueden montar, para que el formulario no
        # tenga que adivinarlo ni copiar la regla de qué está libre.
        'disponibles': [_json_llanta(ll) for ll in adaptador.sin_montar()],
        # El vocabulario lo publica el servidor y la pantalla NO lo copia: si el
        # JS llevara su propia lista, el día que se agregue un motivo el
        # desplegable no lo ofrecería y esa causa de desmontaje no existiría
        # nunca en los datos. Regla 0 con consecuencia.
        'motivos_desmontaje': list(dom.MOTIVOS_DESMONTAJE),
        'vidas_para_fijar_util': dom.MONTAJES_CERRADOS_PARA_VIDA_UTIL,
    })


@llantas_bp.route('/llantas', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'dar de alta una llanta')
def alta():
    """Da de alta una llanta física. **No la monta.**

    Son dos gestos porque son dos hechos: la llanta llega a bodega y entra al
    inventario; el camión entra al taller y se la ponen. Que puedan pasar
    semanas entre los dos no es un cabo suelto — es lo que ocurre.
    """
    datos = request.get_json(silent=True) or {}
    faltantes = [c for c in ('codigo', 'medida') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        fila = adaptador.dar_de_alta(
            codigo=datos['codigo'], medida=datos['medida'],
            marca=datos['marca'] if 'marca' in datos else None,
            registrada_por_usuario_id=_usuario_id())
    except ErrorFlota as e:
        # 409 y no 400: el cuerpo puede estar perfecto y el estado del mundo no
        # admitirlo — un código ya usado es lo segundo.
        return jsonify({'error': str(e)}), 409

    return jsonify(_json_llanta(fila)), 201


@llantas_bp.route('/montajes', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'montar una llanta')
def montar():
    """Monta una llanta en una posición. Abre el tramo con su kilometraje.

    El kilometraje es obligatorio (regla 3) y **se pide**, no se hereda: montar
    una llanta es un gesto de taller y alguien está viendo el tablero. Las nueve
    categorías de gasto que se cuelgan de la última lectura conocida son las que
    se pagan en una oficina; ésta no.

    Las dos combinaciones imposibles devuelven 409 con el mensaje que dice cuál
    es —la posición ocupada dice por qué llanta y desde cuándo; la llanta ya
    montada dice en qué placa y posición—, porque quien las recibe está mirando
    una pantalla que ya se le desactualizó.
    """
    datos = request.get_json(silent=True) or {}
    faltantes = [c for c in ('placa', 'llanta_id', 'posicion', 'km')
                 if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        # Sin `.get(x, default)`: un `posicion` ilegible degradado a 1 pondría la
        # llanta en la direccional izquierda de un camión que nadie tocó, y el
        # km por posición de esa rueda quedaría hablando de otra.
        llanta_id = int(datos['llanta_id'])
        posicion = int(datos['posicion'])
        km = int(datos['km'])
        gasto_id = (int(datos['gasto_id'])
                    if 'gasto_id' in datos and datos['gasto_id'] not in (None, '')
                    else None)
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'campo numérico ilegible: {e}'}), 400

    try:
        fila = adaptador.montar(
            llanta_id=llanta_id, vehiculo_id=vehiculo.id, posicion=posicion,
            km=km, montada_por_usuario_id=_usuario_id(), gasto_id=gasto_id,
            observacion=datos['observacion'] if 'observacion' in datos else None)
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify(_json_montaje(fila)), 201


@llantas_bp.route('/montajes/<int:montaje_id>/desmontar', methods=['POST'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'desmontar una llanta')
def desmontar(montaje_id):
    """Cierra el tramo: kilometraje de salida y **motivo obligatorio**.

    El motivo no tiene opción por defecto ni en el cuerpo ni en la pantalla. Es
    el eje entero del análisis: sin `desgaste_irregular`, una llanta que murió
    por una desalineación y una que cumplió su vida quedan indistinguibles, y el
    eje que come flancos no aparece nunca. `sin_dato` es una respuesta legítima y
    hay que escribirla.
    """
    datos = request.get_json(silent=True) or {}
    faltantes = [c for c in ('km', 'motivo') if c not in datos]
    if faltantes:
        return jsonify({'error': f'Campos requeridos: {", ".join(faltantes)}'}), 400

    try:
        km = int(datos['km'])
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'km ilegible: {e}'}), 400

    try:
        fila = adaptador.desmontar(
            montaje_id=montaje_id, km=km, motivo=datos['motivo'],
            desmontada_por_usuario_id=_usuario_id(),
            observacion=datos['observacion'] if 'observacion' in datos else None)
    except ErrorFlota as e:
        return jsonify({'error': str(e)}), 409

    return jsonify(_json_montaje(fila)), 200


__all__ = ['llantas_bp']

"""
Endpoints de la inspección diaria — donde el conductor contesta.

Nacen con su pantalla (`app/static/pwa/flota.js`, bloque `flotaCond*`), no
antes. `flota/adaptadores/inspecciones.py` estuvo un día entero con 370 líneas,
cero tests y cero callers: el patrón «función sin caller» que este repo lleva
pagando toda la semana, cometido dentro del módulo escrito para evitarlo.

Dos verbos y ninguno más:

```
GET  /flota/inspeccion/items/<placa>   qué se le pregunta hoy, en qué orden
POST /flota/inspeccion                 la inspección respondida, con veredicto
```

No hay `PUT` ni `DELETE`. Una inspección es lo que alguien dijo haber visto en
un momento; editarla después es cambiar el testimonio sin dejar rastro. El
adaptador tampoco los tiene, y la frontera no puede ofrecer lo que la política
de abajo no permite.

## Quién puede qué, y por qué es la misma asimetría del hallazgo

| | Rol | Motivo |
|---|---|---|
| Ver los ítems del día | `LECTURA_FLOTA` (incluye conductor) | El que inspecciona es el que maneja. Si necesitara un jefe para abrir la lista, la inspección se hace a las nueve o no se hace |
| Registrar la inspección | `LECTURA_FLOTA` (incluye conductor) | Es **su** turno y **su** respaldo. Que un admin la registre por él convierte la app en un registro *sobre* el conductor hecho por otro, y entonces deja de servirle a él — que es lo que sostiene la adopción (regla 12) |
| Cerrar el daño que la inspección produjo | `MAESTROS_FLOTA` (sin conductor), en `flota/api/hallazgos.py` | Regla 11: si quien reporta también cierra, el camino barato es marcar `no_apto` y cerrarlo en el mismo minuto |

La asimetría **ya está y no se repite acá**: los hallazgos que nacen de un
`no_apto` pasan por `hallazgos.reportar` y solo se cierran por los endpoints de
hallazgo, que exigen `MAESTROS_FLOTA`. Escribir un rol distinto en este archivo
habría sido una segunda política sobre la misma pregunta.

`inspeccionada_por_usuario_id` sale del token, nunca del cuerpo. Quién dice
haber mirado el camión no lo elige quien manda el JSON — es el mismo criterio
que `reportado_por_usuario_id` en el hallazgo, y acá pesa más: esta firma es la
que se lee frente a una aseguradora.

## Lo que la frontera NO recibe, aunque el cliente lo mande

`veredicto`, `orden_mostrado` y `fecha_limite` no se leen del cuerpo. Los tres
se calculan (reglas 1, 6 y 11). Recibirlos dejaría que quien responde declare
`apto` sobre una lista a medias, afirme una pantalla que nadie vio, o le ponga
mil días de plazo a un freno.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.models.vehiculo import Vehiculo
from app.routes._auth_helpers import Roles
from app.utils.fecha import dia_operativo
from flota.adaptadores import inspecciones as adaptador
from flota.adaptadores.modelos import Inspeccion
from flota.api._permisos import exige
from flota.api._tiempo import iso_utc
from flota.dominio import inspeccion as dom
from flota.dominio.errores import ErrorFlota

inspecciones_bp = Blueprint('flota_inspecciones', __name__)


def _usuario_id():
    return int(get_jwt_identity())


def _vehiculo_por_placa(placa: str) -> Vehiculo:
    v = Vehiculo.query.filter_by(placa=(placa or '').strip().upper()).first()
    if v is None:
        raise LookupError(f'No existe vehículo con placa {placa}')
    return v


def _json_item(item, orden: int) -> dict:
    """Un ítem como lo pinta la pantalla, con su posición YA decidida.

    `orden_mostrado` viaja calculado desde el servidor y el cliente lo devuelve
    tal cual — no lo elige. Es lo que hace auditable el barajado de la regla 11:
    si el cliente pudiera declarar el orden, `flota_respuesta_item` afirmaría
    una pantalla que nadie vio.

    `gesto` va siempre, no detrás de un «ver más»: sin él la criticidad es
    decorativa. «Revisar frenos» termina en un óptimo marcado sin mirar; «pisar
    a fondo y sostener 5 segundos» no.
    """
    return {
        'item_id': item.id,
        'nombre': item.nombre,
        'gesto': item.gesto,
        'criticidad': item.criticidad,
        'periodicidad': item.periodicidad,
        'orden_mostrado': orden,
        'bloqueante': item.criticidad == 'bloqueante',
        # El plazo que va a llevar el hallazgo si se marca `no_apto`. Se publica
        # para que la pantalla no lo deduzca de la criticidad: sería la cuarta
        # copia de la regla 6, y la copia de la pantalla es la que la gente mira.
        'dias_de_plazo': dom.dias_de_plazo(item.criticidad),
    }


def _json_inspeccion(fila: Inspeccion) -> dict:
    """Una inspección como la lee la pantalla. El juicio lo emite el DOMINIO.

    QUÉ AFIRMA `habilita_despacho`: que se contestaron todos los ítems del día y
    que ningún bloqueante falló.

    QUÉ NO AFIRMA: que alguien vaya a impedir la salida. Hoy esto se publica y
    no bloquea — medir → corregir → imponer, en ese orden.

    No se calcula acá con un `== 'apto'`: lo contesta
    `dominio.inspeccion.habilita_despacho`, en un solo lugar, para que el día
    que `incompleta` empiece a bloquear de verdad haya una línea que cambiar.
    """
    return {
        'id': fila.id,
        'vehiculo_id': fila.vehiculo_id,
        'dia': fila.dia.isoformat(),
        'veredicto': fila.veredicto,
        'habilita_despacho': dom.habilita_despacho(fila.veredicto),
        'items_esperados': fila.items_esperados,
        'items_sin_dato': fila.items_sin_dato,
        'bloqueantes_no_aptos': fila.bloqueantes_no_aptos,
        # Regla 11: se publica el HECHO, sin umbral (regla 13 — no hay una sola
        # medición todavía). Quien lo lea decide; el sistema no juzga con un
        # número que nadie midió.
        'segundos_llenado': fila.segundos_llenado,
        'respondida_ts': iso_utc(fila.respondida_ts),
        'inspeccionada_por_usuario_id': fila.inspeccionada_por_usuario_id,
        'lectura_id': fila.lectura_id,
        'custodia_id': fila.custodia_id,
        'observacion': fila.observacion,
        # Los daños que nacieron de esta inspección, con su ítem. Se publican
        # acá y no se piden aparte porque son la consecuencia visible de haber
        # contestado: sin esto, marcar `no_apto` no muestra nada y el conductor
        # no sabe si su reporte llegó.
        'hallazgos': [
            {'hallazgo_id': r.hallazgo_id, 'item_id': r.item_id,
             'nombre': r.item.nombre, 'criticidad': r.item.criticidad,
             'nota': r.nota}
            for r in fila.respuestas if r.hallazgo_id is not None
        ],
    }


@inspecciones_bp.route('/inspeccion/items/<placa>', methods=['GET'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'ver los ítems de la inspección de hoy')
def items_del_dia(placa):
    """Los ítems que toca preguntar hoy, en el orden en que van en pantalla.

    QUÉ AFIRMA: que este es el orden que ve todo el que inspeccione hoy ese tipo
    de vehículo, y que es reconstruible mañana para auditar.

    QUÉ NO AFIRMA: que alguien los haya mirado. Eso lo afirman las filas de
    `flota_respuesta_item`, que es otra cosa y llega por el POST.

    Devuelve además **las inspecciones que ya se hicieron hoy** sobre este
    vehículo. No para bloquear la segunda —dos turnos en un día son reales, y
    negar la segunda empuja a no inspeccionar— sino para que quien abra la
    pantalla vea que ya hay una y con qué veredicto. Es la lista de `del_dia`,
    la más reciente primero.
    """
    try:
        vehiculo = _vehiculo_por_placa(placa)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    # El día lo decide el servidor en hora de Bogotá (regla 5 del WMS). Si
    # viniera del cliente, un teléfono con la fecha corrida pediría la lista de
    # otro día —los semanales solo entran los lunes— y el registro afirmaría un
    # formulario que hoy no correspondía.
    dia = dia_operativo()
    try:
        items = adaptador.items_del_dia_de(vehiculo, dia)
        plantilla = adaptador.plantilla_de(vehiculo)
    except ErrorFlota as e:
        # 409 y no 400: el cuerpo está bien y el estado del mundo no admite la
        # operación — un tipo de vehículo sin catálogo sembrado es eso.
        return jsonify({'error': str(e)}), 409

    return jsonify({
        'placa': vehiculo.placa,
        'dia': dia.isoformat(),
        'plantilla': plantilla.codigo,
        'items': [_json_item(i, orden) for orden, i in enumerate(items, 1)],
        # Contador aparte del total: los bloqueantes son los que deciden si el
        # camión sale, y saber «9 de 28» antes de empezar es lo que hace que la
        # pantalla se lea como una tarea de dos minutos y no como un trámite.
        'bloqueantes': sum(1 for i in items if i.criticidad == 'bloqueante'),
        'ya_respondidas_hoy': [
            _json_inspeccion(f) for f in adaptador.del_dia(vehiculo.id, dia)
        ],
    })


@inspecciones_bp.route('/inspeccion', methods=['POST'])
@jwt_required()
@exige(Roles.LECTURA_FLOTA, 'registrar la inspección preoperacional')
def registrar():
    """Registra la inspección respondida y devuelve su veredicto.

    El cuerpo trae `placa`, `km`, `respuestas` y `segundos_llenado`. Los cuatro
    son obligatorios y **ninguno tiene default**:

    · sin `km` no se persiste ningún evento de flota (regla 3);
    · sin `respuestas` no hay nada que juzgar;
    · sin `segundos_llenado` se pierde el único dato que distingue mirar de
      marcar (regla 11), y un `0` puesto por el servidor haría ver idéntica una
      inspección que no midió el tiempo y una contestada al instante.

    Un ítem que no venga en `respuestas` **no es un error**: entra como
    `sin_dato` y arrastra el veredicto a `incompleta` (regla 1). Es la
    diferencia entre «no sé» y «está mal», y ninguna de las dos autoriza.
    """
    datos = request.get_json(silent=True) or {}

    faltantes = [c for c in ('placa', 'km', 'respuestas', 'segundos_llenado')
                 if c not in datos]
    if faltantes:
        return jsonify({
            'error': f'Campos requeridos: {", ".join(faltantes)}',
            'detalle': 'Ninguno tiene valor por defecto: un km inventado o un '
                       'cronómetro en cero valen menos que un 400.',
        }), 400

    try:
        vehiculo = _vehiculo_por_placa(datos['placa'])
    except LookupError as e:
        return jsonify({'error': str(e)}), 404

    try:
        km = int(datos['km'])
    except (ValueError, TypeError):
        return jsonify({'error': f'km inválido: {datos["km"]!r}'}), 400

    try:
        fila = adaptador.registrar(
            vehiculo_id=vehiculo.id,
            tipo_vehiculo_obj=vehiculo,
            km=km,
            inspeccionada_por_usuario_id=_usuario_id(),
            respuestas=datos['respuestas'],
            segundos_llenado=datos['segundos_llenado'],
            observacion=datos['observacion'] if 'observacion' in datos else None,
        )
    except ErrorFlota as e:
        # 409: el JSON puede estar perfecto y el mundo no admitirlo — un
        # odómetro que retrocede, un catálogo sin sembrar, un ítem que hoy no
        # tocaba. Es un dato malo, no un fallo del sistema.
        return jsonify({'error': str(e)}), 409

    return jsonify(_json_inspeccion(fila)), 201


__all__ = ['inspecciones_bp']

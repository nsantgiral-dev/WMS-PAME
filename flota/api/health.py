"""
`GET /flota/health` — declara estado, no dice OK.

Un health que responde `{"ok": true}` no sirve para decidir nada. Este responde
qué sabe y qué no sabe, campo por campo, y la diferencia entre las dos cosas es
explícita: un número es una afirmación sobre la flota; `null` es una afirmación
sobre el sistema —"esto todavía no se puede medir"—. Ningún campo cae a 0 por
defecto.

Es también el instrumento de la secuencia obligatoria del módulo: toda
validación nueva nace reportando acá y solo después se convierte en excepción.
Si el primer día la app deja un camión en patio por un campo mal llenado, la
operación desmonta el sistema en 48 horas.
"""
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_control_flota
from flota.adaptadores.medicion import MedidorSQL

flota_bp = Blueprint('flota', __name__)


# Los campos del health, en el orden de `docs/flota/ESPECIFICACION_T1.md` §5.
#
# Es una lista declarada y no un dict por comprensión sobre `dir(medidor)`
# porque un campo que desaparece de la respuesta sin que nadie lo note es la
# misma clase de defecto que un campo en cero: el que lee no distingue "no hay
# problema" de "dejé de mirar".
_CAMPOS = (
    'ambiente',
    'datos_reales',
    'vehiculos_activos',
    'fichas_completas',
    'atributos_sin_dato',
    'vehiculos_sin_custodia_activa',
    'custodias_pendiente_sede',
    'custodias_cerradas_forzadas',
    'custodias_sin_foto_completa',
    'fotos_pendiente_evidencia',
    'conductores_activos_sin_cuenta',
    'documentos_no_encontrados',
    'documentos_vencidos',
    'documentos_por_vencer_30d',
    'rutas_historicas_sin_placa',
    # ── Odómetro (2026-09-01) ────────────────────────────────────────────
    # El health no tenía ni un campo del odómetro, y por eso nada avisó de lo
    # que había adentro: 26 lecturas, cero con foto, 20 duplicados y un salto
    # de +16,3 millones de km que dejó un camión sin poder medirse.
    #
    # Publican HECHOS, no umbrales. `salto_km_maximo_30d` no dice si está mal:
    # dice cuánto fue, de qué vehículo y en cuántas horas — para poder fijar
    # un techo con dato dentro de un mes en vez de a ojo hoy.
    'vehiculos_sin_lectura',
    'lecturas_sin_foto',
    'lecturas_correccion_30d',
    'salto_km_maximo_30d',
    'lecturas_ts_duplicado',
    'fichas_con_ancla_incoherente',
    # ── Confianza del kilómetro (2026-09-02) ─────────────────────────────
    # Los dos van juntos y **separados**, por la misma razón que
    # `hallazgos_vencidos` no se suma a `hallazgos_abiertos`: uno mide una
    # deuda —números que nadie puede respaldar y que dejan un CPK en
    # `sin_dato`— y el otro mide si alguien la está pagando. Un solo total
    # escondería el caso peor, que es una cola creciendo con nadie mirándola.
    'lecturas_dudosas_pendientes',
    'lecturas_verificadas_30d',
    # ── Hallazgos (2026-09-01) ───────────────────────────────────────────
    # La tabla nace en esta misma tanda y su pantalla es por vehículo. Sin
    # estos dos campos, saber si hay un daño vencido exige abrir los seis
    # expedientes de a uno, y eso nadie lo hace el martes.
    'hallazgos_abiertos',
    'hallazgos_vencidos',
    # ── Inspección diaria (2026-09-02) ───────────────────────────────────
    # La tabla y su adaptador nacieron ayer sin un solo lector. Los tres
    # contestan preguntas distintas y por eso no se suman: cuántos camiones
    # nadie miró, cuántos se miraron a medias, y cuánto se tardó en mirarlos.
    #
    # `segundos_llenado_30d` publica un HECHO y ningún umbral (regla 13): no
    # hay una sola medición todavía, y un techo escrito hoy sería a ojo.
    'vehiculos_sin_inspeccion_hoy',
    'inspecciones_incompletas_hoy',
    'segundos_llenado_30d',
    # ── La plata que sale (2026-09-02) ───────────────────────────────────
    # `flota_gasto` y `flota_tanqueo` nacen con sus medidas, no un mes después.
    #
    # Los dos primeros van juntos y **separados**: «tanqueos que exceden la
    # capacidad» y «tanqueos que no se pudieron mirar porque la ficha no dice
    # cuántos galones caben» sumados dan un número sin significado, y sin el
    # segundo un parque entero sin capacidad levantada se ve igual que un parque
    # limpio. Es cómo se apaga un detector sin que nadie lo note.
    #
    # `cpk_mes` es un HECHO por vehículo, no un promedio de flota: el canon dice
    # que el CPK no compara vehículos.
    'tanqueos_sobre_capacidad',
    'tanqueos_sin_capacidad_declarada',
    'gastos_sin_documento',
    'cpk_mes',
    # ── Taller y garantía (2026-09-02) ───────────────────────────────────
    # `flota_orden_trabajo` y `flota_intervencion` nacen con sus medidas.
    #
    # Los tres van SEPARADOS y ninguno se suma a otro. «Camiones adentro»,
    # «trabajos que volvieron sin factura» y «garantías que todavía cubren»
    # se atienden distinto: al primero se le llama al taller, al segundo a
    # contabilidad, y el tercero no se atiende — se consulta antes de mandar
    # el camión.
    #
    # `trabajos_sin_factura` es **el precio de que la OT no lleve valor**: el
    # camión entra hoy y la factura llega el 30, y lo que impide que esa
    # decisión se vuelva un agujero es que la ausencia se cuente.
    #
    # `garantias_vigentes` es el campo que dice si la fase está haciendo algo.
    # En cero durante meses significa que la búsqueda que evita pagar dos veces
    # no tiene sobre qué pronunciarse, y eso es distinto de «no hubo taller».
    'ot_abiertas',
    'trabajos_sin_factura',
    'garantias_vigentes',
    # ── Llantas (2026-09-02) ─────────────────────────────────────────────
    # `flota_llanta` y `flota_montaje_llanta` nacen con sus medidas.
    #
    # Los dos primeros van juntos y **separados**, por la misma razón que
    # `tanqueos_sobre_capacidad` y `tanqueos_sin_capacidad_declarada`: sin
    # ficha no se sabe cuántas posiciones tiene un vehículo, y contar eso como
    # «0 posiciones sin llanta» haría que un parque entero sin ficha se viera
    # idéntico a uno con las 24 llantas registradas. Es cómo se apaga un
    # detector sin que nadie lo note.
    #
    # `posiciones_sin_llanta` casi nunca significa que el camión ande sin
    # rueda: significa que la llanta está puesta y nadie la registró. Es la
    # medida de cuánto le falta al inventario para describir el vehículo real.
    #
    # `km_por_posicion` publica un HECHO y **ningún umbral** (regla 13): no hay
    # una sola llanta medida en esta flota, y un «se cambia a los X km» escrito
    # hoy sería a ojo. Va por vehículo y posición porque el modo de fallo caro
    # no es que se gasten, es que se gasten MAL —desalineación, presión, un eje
    # que come el flanco interno— y eso solo se ve por posición.
    'posiciones_sin_llanta',
    'vehiculos_sin_posiciones_llanta',
    'llantas_montadas',
    'km_por_posicion',
    # ── Preventivo (2026-09-02) ──────────────────────────────────────────
    # `flota_ficha_tecnica.distribucion_km_cambio` está cargado en la base
    # desde la tanda 1 y **nadie lo lee**. Es la tabla diciendo a qué
    # kilometraje toca cambiar la correa, mientras la correa envejece. Estos
    # cinco campos son el lector.
    #
    # Los cuatro contadores van SEPARADOS y ninguno se suma a otro, y no por
    # simetría: cada uno se corrige llamando a una persona distinta. Vencida →
    # al taller. Por vencer → a conseguir el repuesto. Sin línea base → a quien
    # sepa cuándo se hizo la última vez. Sin intervalo → al concesionario. Un
    # solo total no dice a quién llamar, que es la lección de los 639 avisos.
    #
    # `tareas_sin_linea_base` es la decisión que hace que un preventivo recién
    # sembrado NO dispare cuarenta avisos el día uno: una tarea que nunca se
    # ejecutó no está al día ni vencida, porque no hay contra qué comparar.
    #
    # `tareas_sin_intervalo` es el que impide que los otros tres se apaguen sin
    # que nadie lo note: la ficha dice QUÉ aceite lleva el motor y no CADA
    # CUÁNTOS KM se cambia, y sin este contador un parque entero sin intervalos
    # levantados se vería idéntico a uno impecable. Mismo motivo que
    # `tanqueos_sin_capacidad_declarada`.
    #
    # `km_dia_por_vehiculo` publica un HECHO y es **el que descarga la regla
    # 13**: `DIAS_AVISO_PREVENTIVO` es el único umbral de la fase y no hay una
    # sola medición de km/día con la que fijarlo. Sale con `n`, `dias` y la
    # marca de confianza, igual que `salto_km_maximo_30d`.
    'tareas_vencidas',
    'tareas_por_vencer',
    'tareas_sin_linea_base',
    'tareas_sin_intervalo',
    'km_dia_por_vehiculo',
)


@flota_bp.route('/health', methods=['GET'])
@jwt_required()
def flota_health():
    """Estado del módulo de flota. Requiere rol de gestión.

    No es público: declara a qué ambiente apunta el sistema, si los números son
    reales, y el inventario de lo que el sistema todavía no sabe. Eso es
    reconocimiento de superficie y no hay razón para regalarlo.

    Lo lee gestión **y** el rol `control_flota` — que es de lectura y no aprueba
    nada. Si control_flota entrara por `_es_gestion()`, el procedimiento
    FLO-PR-01 diría "no aprueba gastos" y el sistema le dejaría aprobarlos.

    No atrapa excepciones. Si una medición revienta, el endpoint devuelve error
    y eso es información — un health que responde ceros porque algo falló
    adentro es evidencia falsa de que todo está bien (regla 5).
    """
    if not _es_control_flota():
        return jsonify({
            'error': 'Acceso restringido a gestión y control de flota'
        }), 403

    medidor = MedidorSQL()
    return jsonify({campo: getattr(medidor, campo)() for campo in _CAMPOS}), 200


__all__ = ['flota_bp', '_CAMPOS']

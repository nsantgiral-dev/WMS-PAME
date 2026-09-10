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
    # ── Analítica despromediada (2026-09-04) ─────────────────────────────
    #
    # Los contadores de este archivo contestan «¿cuántos?». Estos tres
    # contestan «¿cuál?», que con seis vehículos es la única pregunta que se
    # puede contestar honestamente — un p90 de seis datos es el máximo con otro
    # nombre, y las bisagras de un boxplot de seis son dos camiones con placa.
    #
    # `procedencia_del_tablero` va JUNTO a `ambiente` y `datos_reales`, y exento
    # de la regla «`None` si falta la tabla» por la misma razón que ellos: mide
    # el sistema, no una tabla de flota. Existe porque la regla 13 también
    # aplica a la página y no solo a la fila — `rutas_historicas_sin_placa`
    # valió 0 en una SQLite vacía y se leyó como «todas las rutas tienen placa»
    # dos veces, la segunda después de la advertencia.
    #
    # Se llamaba `procedencia` a secas durante veinte minutos. El trinquete de
    # colisiones lo rechazó: esa palabra ya aparece en `compras_ia.js`,
    # `kardex.js` y `rutas.js` por motivos ajenos. Declarar tres exenciones
    # habría sido más corto y peor — un nombre que choca con tres módulos que no
    # tienen nada que ver va a volver a chocar, y cada choque gasta una
    # exención que después nadie revisa.
    'procedencia_del_tablero',
    'cobertura_por_vehiculo',
    'lecturas_por_vehiculo',
    'vehiculos_activos',
    'fichas_completas',
    'atributos_sin_dato',
    'vehiculos_sin_custodia_activa',
    'custodias_pendiente_sede',
    'custodias_cerradas_forzadas',
    'custodias_sin_foto_completa',
    # ── Las dos enumeraciones que faltaban (2026-09-09) ──────────────────
    #
    # `custodias_cerradas_forzadas` y `custodias_sin_foto_completa` son dos de
    # las cinco señales con las que la ficha de control de flota dice que se
    # mide al rol, y las dos salían como un entero pelado. «3» no dice a qué
    # camión llamar; con seis vehículos, despromediar es enumerar.
    #
    # Los dos contadores de arriba **derivan de esta lista** — una política, una
    # función. El predicado de «sin foto completa» tiene cuatro entradas (ficha,
    # tipo, ángulos, posiciones de llanta) y escrito dos veces diverge sin que
    # nadie lo note: los dos siguen devolviendo un entero plausible.
    'custodias_por_vehiculo',
    'fotos_pendiente_evidencia',
    'conductores_activos_sin_cuenta',
    'documentos_no_encontrados',
    'documentos_vencidos',
    'documentos_por_vencer_30d',
    # Los tres de arriba derivan de esta lista, por lo mismo. La fila lleva tres
    # banderas y no una categoría: mapean uno a uno contra los tres contadores,
    # que es lo que hace la derivación verificable sin una tabla de traducción
    # en el medio. Son disjuntas hoy porque lo impone un CHECK de la base
    # (`ck_flota_doc_estado_coherente`), no esta función.
    'documentos_por_vehiculo',
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
    #
    # `dias_hallazgo_abierto` (2026-09-04) es el tercero y **cierra un desfase
    # documental**: `especialista-control-flota.md:119` lo promete como señal de
    # desempeño de ese rol desde el 2026-08-04, el canon existe desde el 08-03, y
    # ninguna pantalla lo mostraba. Sale despromediado —los casos con placa
    # primero, el promedio con su `n` después— porque con un puñado de hallazgos
    # el promedio no dice a qué camión llamar, y el canon prohíbe compararlo
    # entre zonas.
    'hallazgos_abiertos',
    'hallazgos_vencidos',
    'dias_hallazgo_abierto',
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
    # `rendimiento_por_vehiculo` (2026-09-04) cierra el segundo desfase
    # documental: `piso-conductor.md:149` promete «Rendimiento km/galón del
    # vehículo» desde el 2026-08-04 y el sistema se lo negaba.
    #
    # Publica el número **aunque no sea publicable**, con lo que le falta. La
    # pantalla del conductor NO lo hace, y la diferencia es deliberada: él
    # pregunta «¿cómo vengo?» y un número que no se sostiene, con su nombre
    # encima, es ruido que no puede corregir; control de flota pregunta «¿ya se
    # puede medir esto?» y necesita ver el provisional.
    'rendimiento_por_vehiculo',
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


#: Los campos que NO miden una tabla de flota: miden el sistema.
#:
#: Están exentos de la regla «`None` cuando falta la tabla», y forzársela los
#: volvería mentira — un `ambiente: null` diría «no sé a qué base apunto», que
#: es falso y peor que cualquier respuesta.
#:
#: **Vive acá, junto a `_CAMPOS`, y no en los tests.** Estaba escrita dos veces
#: —`tests/flota/test_health_flota.py` y `tests/flota/test_mundos_incompletos.py`,
#: esta última inline como tupla literal—, así que agregar un campo exento
#: exigía acordarse de los dos sitios. El 2026-09-04 no me acordé, y el segundo
#: test fue el que lo dijo. Una política, una función.
_CAMPOS_SIN_TABLA = ('ambiente', 'datos_reales', 'procedencia_del_tablero')


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


__all__ = ['flota_bp', '_CAMPOS', '_CAMPOS_SIN_TABLA']

"""
Health-check de integración Siesa/Connekta.

Rutas:
  GET /api/health/ping  — pública, solo ok/error (Railway probe + monitoring externo)
  GET /api/health/siesa — requiere JWT admin/gestion (detalle completo de configuración)

No verifica `API_v2_Conceptos` porque ese endpoint no está en el contrato
documentado de Connekta para esta instalación. La validez del motivo de traslado
se confirma en el primer despacho real — Siesa rechaza con error claro si es inválido.
"""
import logging
from datetime import datetime
from flask import Blueprint, current_app, jsonify
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_gestion

health_bp = Blueprint('health', __name__)
logger = logging.getLogger(__name__)

# El catálogo de variables NO vive acá. Vivía, y esa era la falla: el gateway
# tenía su propia lista de obligatorias y las dos divergieron hasta que
# `SIESA_TIPO_DOCTO_AJUSTE` quedó fuera de ambas y costó 93 jobs en FALLIDO.
# Ver `app/services/vars_criticas.py`.


@health_bp.route('/ping', methods=['GET'])
def health_ping():
    """Endpoint público mínimo — solo indica si el servicio está activo."""
    from app.services.connekta_gateway import connekta
    try:
        # El modo alimenta el banner global: si los datos de pantalla NO son la
        # realidad, quien los mira debe saberlo. Ver un número de ensayo sin
        # etiqueta entrena a obedecer números falsos.
        #
        # WMS_ENSAYO es INDEPENDIENTE de Connekta a propósito. Los flags de
        # connekta describen si los POST llegan a Siesa; NO describen si los
        # datos en pantalla son de prueba. En un ensayo con vestuario hay
        # credenciales reales y datos ficticios a la vez — sin esta variable el
        # banner se apagaría justo cuando más se necesita.
        # A QUÉ SIESA APUNTA. `connekta.modo_datos()` es LA función — esto lo
        # calculaba por su cuenta, y otras tres partes también. Dos de las
        # cuatro no miraban el host, y una de esas pintaba «PRODUCCIÓN» en
        # verde sobre QA.
        modo = connekta.modo_datos()
        _h = connekta.host_siesa.lower()
        return jsonify({
            'ok': True,
            'modo': modo,
            'siesa_host': _h or None,
            'modo_simulacion': connekta.modo_simulacion,
            'circuit_breaker': connekta.circuit_state(),
        }), 200
    except Exception:
        return jsonify({'ok': False}), 503



#: Columnas que `_filas_nc_encabezado` necesita de la consulta dinámica.
#: `f350_total_db` NO está acá a propósito — verificado en vivo el 2026-08-06
#: que es siempre 0 en Elaboración (ver docstring de `get_consec_nc_creada`),
#: así que dejó de usarse para identificar la NC.
_COLUMNAS_NC_CONSECUTIVO = (
    'f350_rowid', 'f350_id_co', 'f350_id_tipo_docto', 'f350_consec_docto',
    'f350_fecha', 'f350_ind_estado',
)


@health_bp.route('/nc-consecutivo', methods=['GET'])
@jwt_required()
def health_nc_consecutivo():
    """Prueba la consulta del consecutivo de NC SIN escribir nada en Siesa.

    Existe por el mismo motivo que «Verificar ingesta de facturación» en Vigía:
    encender una integración y ver si se rompe en producción no es un método.
    Acá el costo de equivocarse es peor que un error visible — una consulta que
    devuelve columnas con otro nombre haría que `get_consec_nc_creada` no
    encontrara candidatas y el motivo DIAN quedara manual **en silencio**,
    indistinguible de no haberlo configurado.

    Acepta `?consulta=<nombre>` para probar una consulta recién registrada
    ANTES de ponerla en `CONNEKTA_CONSULTA_NC_CONSECUTIVO`. Sin el parámetro
    prueba la que está configurada.

    Es de solo lectura: un GET a Connekta y nada más.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Acceso restringido a roles de gestión'}), 403
    from flask import request

    from app.services.connekta_gateway import connekta

    nombre = (request.args.get('consulta') or '').strip() or connekta.consulta_nc_consecutivo
    if not nombre:
        return jsonify({
            'apto': False,
            'motivo': 'No hay consulta configurada ni pasada en ?consulta=',
            'siguiente_paso': (
                'Registrar la consulta dinámica en Connekta y probarla acá con '
                '?consulta=<nombre> antes de fijar CONNEKTA_CONSULTA_NC_CONSECUTIVO.'
            ),
        }), 200

    _original = connekta.consulta_nc_consecutivo
    try:
        connekta.consulta_nc_consecutivo = nombre
        filas = connekta._filas_nc_encabezado()
    except Exception as e:
        return jsonify({
            'apto': False, 'consulta': nombre,
            'motivo': f'La consulta falló: {str(e)[:300]}',
            'siguiente_paso': 'Verificar el nombre exacto en Connekta y que devuelva filas.',
        }), 200
    finally:
        connekta.consulta_nc_consecutivo = _original

    if not filas:
        # Cero filas NO es apto: puede ser una consulta correcta sobre una
        # empresa sin NC, o una consulta rota. No se distingue, y una
        # integración que no se puede distinguir de rota no se enciende.
        return jsonify({
            'apto': False, 'consulta': nombre, 'filas': 0,
            'motivo': 'La consulta respondió sin filas — no se puede verificar '
                      'qué columnas trae.',
            'siguiente_paso': 'Crear una NC de prueba en Siesa, o revisar el WHERE '
                              f'(debe filtrar f350_id_tipo_docto = {connekta.tipo_docto_nota_credito}).',
        }), 200

    presentes = set(filas[0].keys())
    faltan = [c for c in _COLUMNAS_NC_CONSECUTIVO if c not in presentes]
    # Fantasmas de paginación (regla 10): filas con todo en NULL.
    fantasmas = sum(1 for f in filas if not f.get('f350_rowid'))

    apto = not faltan
    return jsonify({
        'apto': apto,
        'consulta': nombre,
        'configurada_ahora': _original or None,
        'filas': len(filas),
        'columnas_faltantes': faltan,
        'columnas_recibidas': sorted(presentes),
        'filas_fantasma_descartadas': fantasmas,
        'ejemplo': filas[0] if filas else None,
        'motivo': None if apto else (
            f'Faltan columnas que el WMS necesita: {", ".join(faltan)}. '
            'Con una columna ausente la NC no se puede identificar y el motivo '
            'DIAN queda manual sin avisar.'
        ),
        'siguiente_paso': (
            f'Poner CONNEKTA_CONSULTA_NC_CONSECUTIVO={nombre} en Railway.'
            if apto and _original != nombre else
            'Ya está configurada y responde bien.' if apto else
            'Corregir el SELECT de la consulta en Connekta y volver a probar.'
        ),
    }), 200


@health_bp.route('/siesa', methods=['GET'])
@jwt_required()
def health_siesa():
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Acceso restringido a roles de gestión'}), 403
    import os
    from app.services.connekta_gateway import connekta

    # Longitud de la SECRET_KEY, nunca la clave. Es la forma de MEDIR producción
    # antes de imponer una validación que podría impedir el arranque.
    import os as _os
    _sk = len((_os.getenv('SECRET_KEY') or '').encode())
    _host_connekta = connekta.host_siesa
    _parece_qa = connekta.apunta_a_pruebas

    resultado = {
        'timestamp': datetime.utcnow().isoformat(),
        # Decía 'produccion' con solo tener credenciales, aunque apuntara a QA.
        'modo': connekta.modo_datos(),
        # A QUÉ SIESA APUNTA. El default del gateway es
        # serviciosqa.siesacloud.com — QA. Si CONNEKTA_URL no está fija en
        # Railway, todo lo que se descargue viene del ambiente de pruebas.
        #
        # No es un detalle de infraestructura: el kardex que va a alimentar la
        # decisión de compra del comité saldría de ahí. Datos de pruebas
        # decidiendo cientos de millones, sin que nada en pantalla lo diga.
        'siesa_destino': {
            'host': _host_connekta,
            'parece_qa': _parece_qa,
            'url_explicita': bool(_os.environ.get('CONNEKTA_URL')),
            'advertencia': (
                'CONNEKTA_URL NO está configurada: se está usando el default, que '
                'es el ambiente de PRUEBAS (serviciosqa). Todo dato descargado '
                'vendría de QA.'
            ) if not _os.environ.get('CONNEKTA_URL') else (
                'El host apunta a un ambiente que parece de PRUEBAS. Verificar '
                'antes de usar estos datos para decidir compras.'
            ) if _parece_qa else None,
        },
        'secret_key': {
            'bytes': _sk,
            'minimo_rfc7518': 32,
            'suficiente': _sk >= 32,
            'accion': None if _sk >= 32 else (
                'Rotar a una clave de 32+ bytes EN VENTANA TRANQUILA: rotar '
                'invalida los JWT vivos y deja sin sesión a quien esté en ruta.'
            ),
        },
        'variables': {},
        'conectividad': None,
        'dlq': {},
        'ok': True,
        'advertencias': [],
    }

    # ── 1. Variables de entorno ──────────────────────────────────────────────
    #
    # Se reportan TODAS las del catálogo, no solo las rotas: el que lee el
    # health a las 6 a.m. necesita ver qué valor va a usar el gateway, no
    # solamente qué falta. `valor_efectivo` muestra el default heredado — ver
    # una variable VACIA y su default al lado es lo que hace obvio que el
    # problema es la declaración en blanco, no la ausencia.
    from app.services.vars_criticas import (
        VARS_CRITICAS, estado as _estado_var, valor_efectivo, problemas,
    )

    for _v in VARS_CRITICAS:
        resultado['variables'][_v.nombre] = {
            'valor': valor_efectivo(_v),
            'estado': _estado_var(_v),
            'descripcion': _v.rompe,
        }

    for _p in problemas():
        resultado['ok'] = False
        resultado['advertencias'].append(
            f'Variable {_p["variable"]} [{_p["estado"]}]: {_p["detalle"]} '
            f'Rompe: {_p["rompe"]}'
        )

    # Verificación especial: bodega_transito vs modo
    bod_transito = os.getenv('SIESA_BODEGA_TRANSITO', '')
    tipo_salida = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', '')
    tipo_entrada = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', '')
    if bod_transito and (not tipo_salida or not tipo_entrada):
        resultado['advertencias'].append(
            'SIESA_BODEGA_TRANSITO configurada pero faltan TIPO_DOCTO_TRANSITO_SALIDA o ENTRADA — '
            'el despacho EN_TRANSITO fallará'
        )
        resultado['ok'] = False

    # QUÉ CRONS CORREN EN ESTE PROCESO. Un hecho, no una lectura de variables.
    #
    # `HEAVY_SCHEDULERS` ausente apaga ocho schedulers en silencio, y entre
    # ellos está `[ALERTAS_SCHEDULER]` — el que manda los correos, incluido el
    # de rutas entregadas sin liquidar. **Una alerta apagada no falla: se
    # calla**, y callarse es indistinguible de «no hubo nada que avisar».
    #
    # Se reporta lo que arrancó y no lo que la variable dice, porque son cosas
    # distintas: un import que revienta deja la variable en `true` y el cron
    # sin correr.
    _activos = current_app.config.get('SCHEDULERS_ACTIVOS') or []
    resultado['schedulers'] = {
        'activos': _activos,
        'omitidos': current_app.config.get('SCHEDULERS_OMITIDOS') or [],
        'alertas_por_correo': '[ALERTAS_SCHEDULER]' in _activos,
        'nota': ('`alertas_por_correo` en false significa que NINGUNA alerta por '
                 'correo sale de este proceso — ni la de rutas sin liquidar, ni '
                 'la de stock crítico. Si tampoco está en el worker, no existen. '
                 'Se consulta en cada servicio por separado.'),
    }

    # Pasos que siguen siendo manuales en Siesa. No son un fallo — son trabajo
    # de una persona todos los días, y la única forma de que nadie los olvide
    # (o los haga de más) es que estén escritos en un sitio consultable.
    _dian_auto = connekta.puede_fijar_motivo_dian
    resultado['pasos_manuales_nc'] = {
        'cruzar_cartera': {
            'automatizado': True,
            'detalle': f'conector {connekta.conector_nota_credito_cruzar} (crea + cruza)',
        },
        'motivo_dian': {
            'automatizado': _dian_auto,
            'detalle': (
                f'conector {connekta.conector_nc_motivo_dian}, consulta '
                f'{connekta.consulta_nc_consecutivo}'
            ) if _dian_auto else (
                'MANUAL — falta registrar la consulta dinámica del consecutivo '
                'en Connekta y ponerla en CONNEKTA_CONSULTA_NC_CONSECUTIVO. '
                'Mientras tanto contabilidad pone el concepto DIAN a mano '
                '(tab Entidades → FE_CONCEPTOS NC 2.1).'
            ),
        },
        'aprobar': {
            'automatizado': False,
            'detalle': (
                'MANUAL sin solución de API — el motor de Siesa procesa '
                'Entidades (753) después del registro que aprueba (461). '
                'Ver CLAUDE.md "Por qué la aprobación de NC NO se pudo automatizar".'
            ),
        },
    }

    # ── 2. Conectividad Connekta ─────────────────────────────────────────────
    if connekta.modo_simulacion:
        resultado['conectividad'] = {'estado': 'simulado', 'detalle': 'modo_simulacion activo'}
    else:
        try:
            # GET inocuo — consulta 1 ítem del catálogo. Si Connekta responde, hay red.
            r = connekta.get_items_catalogo(pagina=1)
            simulado = r.get('simulado', False)
            resultado['conectividad'] = {
                'estado': 'ok',
                'simulado': simulado,
                'detalle': 'Connekta respondió correctamente',
            }
        except Exception as e:
            resultado['conectividad'] = {
                'estado': 'ERROR',
                'detalle': str(e)[:200],
            }
            resultado['ok'] = False
            resultado['advertencias'].append(f'Connekta no responde: {str(e)[:100]}')

    # ── 3. DLQ — jobs fallidos pendientes ───────────────────────────────────
    try:
        from app.services.siesa_job_service import get_jobs_fallidos
        fallidos = get_jobs_fallidos()
        resultado['dlq'] = {
            'jobs_fallidos': len(fallidos),
            'alerta': (
                f'{len(fallidos)} job(s) en FALLIDO — requieren reintento manual'
                if fallidos else None
            ),
        }
        if fallidos:
            resultado['advertencias'].append(f'DLQ: {len(fallidos)} job(s) fallidos sin resolver')
    except Exception as e:
        resultado['dlq'] = {'estado': 'error_leyendo', 'detalle': str(e)[:100]}

    # ── 4. Nota sobre motivo ─────────────────────────────────────────────────
    motivo = os.getenv('SIESA_MOTIVO_TRASLADO', '')
    resultado['nota_motivo'] = (
        f'Motivo "{motivo}" configurado. Validez confirmada por consultor Siesa. '
        'Si Siesa lo rechaza en producción, el error llegará vía siesa_error en el traslado '
        'y email DLQ — no se necesita health-check dinámico de maestros.'
    )

    resultado['circuit_breaker'] = connekta.circuit_state()

    status_code = 200 if resultado['ok'] else 503
    return jsonify(resultado), status_code

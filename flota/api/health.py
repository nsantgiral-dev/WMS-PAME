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

from app.routes._auth_helpers import _es_gestion
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
    'custodias_sin_foto_completa',
    'fotos_pendiente_evidencia',
    'conductores_activos_sin_cuenta',
    'documentos_no_encontrados',
    'documentos_vencidos',
    'documentos_por_vencer_30d',
    'rutas_historicas_sin_placa',
)


@flota_bp.route('/health', methods=['GET'])
@jwt_required()
def flota_health():
    """Estado del módulo de flota. Requiere rol de gestión.

    No es público: declara a qué ambiente apunta el sistema, si los números son
    reales, y el inventario de lo que el sistema todavía no sabe. Eso es
    reconocimiento de superficie y no hay razón para regalarlo. Mismo criterio
    que `/api/health/siesa`. Si hiciera falta lectura sin sesión, se resuelve con
    un token de solo lectura, no abriendo el endpoint.

    No atrapa excepciones. Si una medición revienta, el endpoint devuelve error
    y eso es información — un health que responde ceros porque algo falló
    adentro es evidencia falsa de que todo está bien (regla 5).
    """
    if not _es_gestion():
        return jsonify({'error': 'Acceso restringido a roles de gestión'}), 403

    medidor = MedidorSQL()
    return jsonify({campo: getattr(medidor, campo)() for campo in _CAMPOS}), 200


__all__ = ['flota_bp', '_CAMPOS']

"""
Analítica — lectura de la bitácora de acciones (Fase 0, 2026-09-24).

Desde la Fase 1 la consume la pantalla 📈 Analítica → 📜 Bitácora
(`analitica_bitacora.js`). Cada fila sale enriquecida por
`analitica_salud.describir_acciones`: quién (nombre), la frase legible, el
pedido, la hora Bogotá y el antes → después campo por campo. **Solo lectura**:
la lógica de registro vive en `app/services/bitacora.py` y no se toca acá.
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion

analitica_bp = Blueprint('analitica', __name__)


def _fecha(nombre):
    valor = (request.args.get(nombre) or '').strip()
    if not valor:
        return None
    return date.fromisoformat(valor)


@analitica_bp.route('/bitacora', methods=['GET'])
@jwt_required()
def listar_bitacora():
    """Acciones registradas, de la más reciente a la más vieja.

    Filtros: `dia` (un día operativo) o `desde`/`hasta` (YYYY-MM-DD, Bogotá),
    `accion`, `entidad`, `entidad_id`, `usuario_id`, `almacen_id`. Paginado:
    `page`, `per_page` (máx. 200).

    Solo gestión: trae motivos, montos y quién hizo qué — el mismo criterio
    que `/api/auditoria/flujo`.
    """
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la bitácora'}), 403

    from app.models.bitacora import BitacoraAccion
    from app.services.bitacora import ACCIONES

    try:
        dia = _fecha('dia')
        desde = _fecha('desde')
        hasta = _fecha('hasta')
    except ValueError:
        return jsonify({'error': 'Fechas en formato YYYY-MM-DD'}), 400

    accion = (request.args.get('accion') or '').strip().upper() or None
    if accion and accion not in ACCIONES:
        return jsonify({'error': f'accion desconocida: {accion}',
                        'vocabulario': list(ACCIONES)}), 400

    q = BitacoraAccion.query
    if dia:
        q = q.filter(BitacoraAccion.dia_operativo == dia)
    if desde:
        q = q.filter(BitacoraAccion.dia_operativo >= desde)
    if hasta:
        q = q.filter(BitacoraAccion.dia_operativo <= hasta)
    if accion:
        q = q.filter(BitacoraAccion.accion == accion)
    entidad = (request.args.get('entidad') or '').strip()
    if entidad:
        q = q.filter(BitacoraAccion.entidad == entidad)
    for campo in ('entidad_id', 'usuario_id', 'almacen_id'):
        crudo = (request.args.get(campo) or '').strip()
        if not crudo:
            continue
        # `type=int` de Flask convertía la basura en None y el filtro se
        # ignoraba en silencio: pedir «almacén x» devolvía todos.
        if not crudo.isdigit():
            return jsonify({'error': f'{campo}: debe ser un número entero'}), 400
        q = q.filter(getattr(BitacoraAccion, campo) == int(crudo))

    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(max(1, request.args.get('per_page', 50, type=int)), 200)
    pagina = (q.order_by(BitacoraAccion.ocurrido_en.desc(), BitacoraAccion.id.desc())
              .paginate(page=page, per_page=per_page, error_out=False))
    from app.services.analitica_salud import describir_acciones
    return jsonify({
        'acciones': describir_acciones([a.to_dict() for a in pagina.items]),
        'total': pagina.total,
        'pagina': page,
        'por_pagina': per_page,
        'vocabulario': list(ACCIONES),
    }), 200

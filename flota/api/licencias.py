"""
Licencias de conducción — la carga la hace quien gestiona la flota
(`MAESTROS_FLOTA`: control de flota y gestión; el gerente solo mira).

`GET /flota/conductores/licencias` y `PUT /flota/conductores/<id>/licencia`.

Existe aparte de `PUT /api/rutas/conductores/<id>` (solo admin) porque
control de flota —que es quien persigue los papeles— no ve Rutas, y la
licencia entra a la política de salida desde el 2026-09-27: sin cargar, cada
despacho pide motivo. La validación y la bitácora son las mismas
(`RutaService._actualizar_licencia` → `salida.problemas_de_licencia`).

El número de la licencia es un dato personal: esta lista es para quien la
carga, no para toda la flota.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from flota.api._permisos import MAESTROS_FLOTA, exige

licencias_bp = Blueprint('flota_licencias', __name__)


def _fila(c, hoy):
    from flota.adaptadores.salida import licencia_de
    return {
        'id': c.id, 'nombre': c.nombre,
        'licencia_numero': c.licencia_numero or '',
        'licencia_categoria': c.licencia_categoria or '',
        'licencia_vence': c.licencia_vence.isoformat() if c.licencia_vence else None,
        'licencia_estado': licencia_de(c, hoy).estado,
    }


@licencias_bp.route('/conductores/licencias', methods=['GET'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'ver las licencias de los conductores')
def listar_licencias():
    from app.models.conductor import Conductor
    from app.utils.fecha import dia_operativo
    from flota.dominio.salida import CATEGORIAS_LICENCIA

    hoy = dia_operativo()
    filas = (Conductor.query.filter(Conductor.activo.is_(True))
             .order_by(Conductor.nombre).all())
    return jsonify({'conductores': [_fila(c, hoy) for c in filas],
                    'categorias_licencia': list(CATEGORIAS_LICENCIA)}), 200


@licencias_bp.route('/conductores/<int:conductor_id>/licencia', methods=['PUT'])
@jwt_required()
@exige(MAESTROS_FLOTA, 'cargar la licencia de un conductor')
def guardar_licencia(conductor_id):
    from app.services.ruta_service import RutaService
    from app.utils.fecha import dia_operativo

    datos = request.get_json(silent=True) or {}
    campos = {k: datos[k] for k in ('licencia_numero', 'licencia_categoria',
                                     'licencia_vence', 'motivo') if k in datos}
    try:
        c = RutaService.actualizar_licencia(conductor_id, campos,
                                            usuario_id=int(get_jwt_identity()))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'conductor': _fila(c, dia_operativo())}), 200


__all__ = ['licencias_bp']

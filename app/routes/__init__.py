def register_routes(app):
    from app.routes.health import health_bp
    from app.routes.auth import auth_bp
    from app.routes.productos import productos_bp
    from app.routes.inventario import inventario_bp
    from app.routes.almacenes import almacenes_bp
    from app.routes.picking import picking_bp
    from app.routes.packing import packing_bp
    from app.routes.recepcion import recepcion_bp
    from app.routes.conteo import conteo_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.mobile import mobile_bp
    from app.routes.siesa import siesa_bp
    from app.routes.devoluciones import devoluciones_bp
    from app.routes.muelle import muelle_bp
    from app.routes.rutas import rutas_bp
    from app.routes.traslados import traslados_bp
    from app.routes.config_siesa import config_siesa_bp
    from app.routes.empaques import empaques_bp
    from app.routes.reposicion import reposicion_bp
    from app.routes.remision_admin import remision_admin_bp
    from app.routes.factura_admin import factura_admin_bp
    from app.routes.despacho_parcial import despacho_parcial_bp
    from app.routes.compras import compras_bp
    from app.routes.bloqueo_recompra import bloqueo_bp
    from app.routes.kardex import kardex_bp
    from app.routes.tienda_oc import tienda_oc_bp
    from app.routes.vigia import vigia_bp
    from app.routes.armador import armador_bp

    app.register_blueprint(health_bp, url_prefix='/api/health')
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(productos_bp, url_prefix='/api/productos')
    app.register_blueprint(inventario_bp, url_prefix='/api/inventario')
    app.register_blueprint(almacenes_bp, url_prefix='/api/almacenes')
    app.register_blueprint(picking_bp, url_prefix='/api/picking')
    app.register_blueprint(packing_bp, url_prefix='/api/packing')
    app.register_blueprint(recepcion_bp, url_prefix='/api/recepcion')
    app.register_blueprint(conteo_bp, url_prefix='/api/conteo')
    app.register_blueprint(dashboard_bp, url_prefix='/api/dashboard')
    app.register_blueprint(mobile_bp, url_prefix='/api/mobile')
    app.register_blueprint(siesa_bp, url_prefix='/api/siesa')
    app.register_blueprint(devoluciones_bp, url_prefix='/api/devoluciones')
    app.register_blueprint(muelle_bp, url_prefix='/api/muelle')
    app.register_blueprint(rutas_bp, url_prefix='/api/rutas')
    app.register_blueprint(traslados_bp, url_prefix='/api/traslados')
    app.register_blueprint(config_siesa_bp, url_prefix='/api/config')
    app.register_blueprint(empaques_bp, url_prefix='/api/empaques')
    app.register_blueprint(reposicion_bp, url_prefix='/api/reposicion')
    app.register_blueprint(remision_admin_bp, url_prefix='/api/admin/remision')
    app.register_blueprint(factura_admin_bp, url_prefix='/api/admin/factura')
    app.register_blueprint(despacho_parcial_bp, url_prefix='/api/despacho_parcial')
    app.register_blueprint(compras_bp, url_prefix='/api/compras')
    app.register_blueprint(bloqueo_bp, url_prefix='/api/compras')
    app.register_blueprint(kardex_bp, url_prefix='/api/kardex')
    app.register_blueprint(tienda_oc_bp, url_prefix='/api/tienda-oc')
    app.register_blueprint(vigia_bp, url_prefix='/api/vigia')
    app.register_blueprint(armador_bp, url_prefix='/api/compras')

    # Módulo flota — vive fuera de `app/` para que su dominio no pueda importar
    # framework ni base. Esta es la única línea de acoplamiento (tanda 1).
    #
    # El import va blindado: si `flota/` no llega al contenedor o revienta al
    # importarse, el WMS ARRANCA IGUAL y `/flota/*` responde 503 con el motivo.
    # El WMS todavía no sale a producción y un módulo nuevo sin estrenar no
    # puede tumbar el arranque de la aplicación que sí va al corte.
    #
    # No contradice la regla 5 del módulo ("ningún adaptador degrada hacia algo
    # que se parezca al éxito"): un 503 con motivo declarado y registrado en el
    # log es lo contrario de un éxito silencioso. Lo prohibido es responder 200
    # con datos vacíos, no responder "esto está caído y por esto".
    _registrar_flota(app)


def _registrar_flota(app):
    """Monta el módulo de flota, o su sustituto declarado si no se puede."""
    import logging

    log = logging.getLogger(__name__)
    try:
        from flota.api import registrar_flota
    except Exception as exc:
        log.error(
            '[FLOTA] módulo NO montado: %s: %s. /flota/* responderá 503. '
            'El resto del WMS arranca normal.',
            type(exc).__name__, exc, exc_info=True,
        )
        _registrar_flota_caida(app, f'{type(exc).__name__}: {exc}')
    else:
        registrar_flota(app)


def _registrar_flota_caida(app, motivo):
    """Sustituto visible: todo `/flota/*` responde 503 diciendo por qué.

    Vive en `app/` y no en `flota/` a propósito — si `flota/` es justamente lo
    que no se pudo importar, un sustituto que viviera ahí tampoco cargaría.
    """
    from flask import Blueprint, jsonify

    bp = Blueprint('flota_caida', __name__)

    @bp.route('/', defaults={'ruta': ''},
              methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    @bp.route('/<path:ruta>',
              methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    def _flota_no_disponible(ruta):
        return jsonify({
            'error': 'modulo_flota_no_disponible',
            'motivo': motivo,
            'detalle': 'El módulo de flota no pudo importarse. '
                       'El resto del WMS funciona normal.',
        }), 503

    app.register_blueprint(bp, url_prefix='/flota')
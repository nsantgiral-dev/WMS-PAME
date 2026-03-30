def register_routes(app):
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
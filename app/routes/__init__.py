def register_routes(app):
    from app.routes.auth import auth_bp
    from app.routes.productos import productos_bp
    from app.routes.inventario import inventario_bp
    from app.routes.almacenes import almacenes_bp
    
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(productos_bp, url_prefix='/api/productos')
    app.register_blueprint(inventario_bp, url_prefix='/api/inventario')
    app.register_blueprint(almacenes_bp, url_prefix='/api/almacenes')
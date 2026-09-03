"""
Capa HTTP del módulo de flota. Traduce HTTP ↔ dominio y nada más.

`registrar_flota(app)` es el único punto por el que `app/` conoce a `flota/`.
"""


def registrar_flota(app):
    """Monta el blueprint de flota bajo `/flota`.

    Se llama desde `app.routes.register_routes`. Es la única línea de
    acoplamiento con el repo existente en la tanda 1.
    """
    # Importar los modelos ANTES de montar el blueprint: registra las cinco
    # tablas en `db.metadata`, que es de donde salen tanto el `create_all()` de
    # los tests como el autogenerate de Alembic. Un modelo que nadie importa no
    # existe para ninguno de los dos, y esa es la forma más silenciosa de que
    # una tabla no llegue a producción.
    from flota.adaptadores import modelos  # noqa: F401
    from flota.api.avisos import avisos_bp
    from flota.api.conductor import conductor_bp
    from flota.api.custodia import custodia_bp
    from flota.api.documentos import documentos_bp
    from flota.api.ficha import ficha_bp
    from flota.api.gastos import gastos_bp
    from flota.api.hallazgos import hallazgos_bp
    from flota.api.health import flota_bp
    from flota.api.inspecciones import inspecciones_bp
    from flota.api.llantas import llantas_bp
    from flota.api.preventivo import preventivo_bp
    from flota.api.taller import taller_bp

    app.register_blueprint(flota_bp, url_prefix='/flota')
    app.register_blueprint(custodia_bp, url_prefix='/flota')
    app.register_blueprint(ficha_bp, url_prefix='/flota')
    app.register_blueprint(documentos_bp, url_prefix='/flota')
    app.register_blueprint(conductor_bp, url_prefix='/flota')
    app.register_blueprint(avisos_bp, url_prefix='/flota')
    app.register_blueprint(hallazgos_bp, url_prefix='/flota')
    app.register_blueprint(gastos_bp, url_prefix='/flota')
    app.register_blueprint(inspecciones_bp, url_prefix='/flota')
    app.register_blueprint(taller_bp, url_prefix='/flota')
    app.register_blueprint(llantas_bp, url_prefix='/flota')
    app.register_blueprint(preventivo_bp, url_prefix='/flota')


__all__ = ['registrar_flota']

"""
Capa HTTP del módulo de flota. Traduce HTTP ↔ dominio y nada más.

`registrar_flota(app)` es el único punto por el que `app/` conoce a `flota/`.
"""


def registrar_flota(app):
    """Monta el blueprint de flota bajo `/flota`.

    Se llama desde `app.routes.register_routes`. Es la única línea de
    acoplamiento con el repo existente en la tanda 1.
    """
    from flota.api.health import flota_bp

    app.register_blueprint(flota_bp, url_prefix='/flota')


__all__ = ['registrar_flota']

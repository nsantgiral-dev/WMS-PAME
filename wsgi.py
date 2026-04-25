from app import create_app

app = create_app()


def post_fork(server, worker):
    """
    Gunicorn hook: se ejecuta en cada worker justo después del fork.
    Con --preload, el master abrió conexiones de BD que no son seguras
    de compartir entre procesos. SQLAlchemy las recicla aquí.
    """
    from app.extensions import db
    with app.app_context():
        db.engine.dispose()

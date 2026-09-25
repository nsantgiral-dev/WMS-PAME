"""
Configuración de Gunicorn — solo los hooks. Gunicorn la lee sola
(`./gunicorn.conf.py`, desde el directorio de arranque); los parámetros siguen
en el `startCommand` de `railway.toml` / el `Procfile`.

`post_fork` vivía en `wsgi.py`, donde Gunicorn no lo busca: nunca corrió
(auditoría 2026-09-25, P3). Con `--preload` el maestro crea la app —y abre
conexiones— antes de hacer fork; compartir esas conexiones entre procesos
corrompe el protocolo de PostgreSQL. Cada worker las descarta al nacer.
"""


def post_fork(server, worker):
    from wsgi import app
    from app.extensions import db
    with app.app_context():
        db.engine.dispose()

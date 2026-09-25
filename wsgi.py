from app import create_app

app = create_app()

# El hook `post_fork` que vivía acá no corría nunca: Gunicorn solo lee hooks
# de su archivo de configuración. Vive en `gunicorn.conf.py` (P3, 2026-09-25).

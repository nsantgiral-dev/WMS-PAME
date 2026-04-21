release: flask db upgrade
web: gunicorn wsgi:app --workers=2 --threads=2 --timeout=120

release: flask db upgrade heads
web: gunicorn wsgi:app --workers=2 --threads=2 --timeout=120 --access-logfile - --error-logfile - --log-level info

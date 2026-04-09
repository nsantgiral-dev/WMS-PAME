import os
import click
from datetime import timedelta
from flask import Flask, send_from_directory
from dotenv import load_dotenv
from app.extensions import db, migrate, jwt, cors

load_dotenv()

def create_app():
    app = Flask(__name__)

    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JWT_SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": "*"}})

    from app.routes import register_routes
    register_routes(app)

    @app.route('/static/pwa/<path:filename>')
    def pwa_files(filename):
        pwa_dir = os.path.join(app.root_path, 'static', 'pwa')
        resp = send_from_directory(pwa_dir, filename)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp

    @app.route('/pwa')
    def pwa():
        pwa_dir = os.path.join(app.root_path, 'static', 'pwa')
        resp = send_from_directory(pwa_dir, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp

    # ── CLI: flask sync-productos ──────────────────────────────────────────
    @app.cli.command('sync-productos')
    def cmd_sync_productos():
        """Sincroniza el catálogo de productos desde Siesa (upsert)."""
        from app.services.siesa_sync_service import ejecutar_sync
        resultado = ejecutar_sync()
        click.echo(resultado)

    # ── Scheduler: sync automático cada hora 7am–8pm (Bogotá) ─────────────
    if os.getenv('SYNC_SCHEDULER', 'true').lower() == 'true':
        try:
            from app.services.siesa_sync_service import init_scheduler
            init_scheduler(app)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'[SCHEDULER] No se pudo iniciar: {e}')
        try:
            from app.services.pedidos_sync_service import init_scheduler as init_pedidos_scheduler
            init_pedidos_scheduler(app)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'[PEDIDOS_SCHEDULER] No se pudo iniciar: {e}')
        try:
            from app.services.abc_service import ABCService
            ABCService.init_scheduler(app)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'[ABC_SCHEDULER] No se pudo iniciar: {e}')

    return app
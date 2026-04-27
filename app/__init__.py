import os
import sys
import click
import logging
from datetime import timedelta
from flask import Flask, send_from_directory
from dotenv import load_dotenv
from app.extensions import db, migrate, jwt, cors

load_dotenv()

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)

def create_app():
    app = Flask(__name__)

    secret_key = os.getenv('SECRET_KEY')
    if not secret_key:
        raise RuntimeError(
            'SECRET_KEY no está configurada en las variables de entorno. '
            'Agrega SECRET_KEY en Railway (o en tu .env local) antes de arrancar la app.'
        )

    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JWT_SECRET_KEY'] = secret_key
    app.config['SECRET_KEY'] = secret_key
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    allowed_origin = os.getenv('APP_URL', '*')
    cors.init_app(app, resources={r"/api/*": {"origins": allowed_origin}})

    # Revocación de tokens: si el usuario se desactiva (activo=False),
    # sus tokens existentes se tratan como revocados inmediatamente.
    # Flask-JWT-Extended llama esto en cada @jwt_required() antes de continuar.
    from app.extensions import jwt as _jwt

    @_jwt.token_in_blocklist_loader
    def _check_usuario_activo(jwt_header, jwt_payload):
        uid = jwt_payload.get('sub')
        if not uid:
            return True   # token inválido — revocar
        try:
            from app.models.usuario import Usuario
            u = Usuario.query.get(int(uid))
            return u is None or not u.activo
        except Exception as _e:
            logging.getLogger(__name__).warning(
                f'[JWT] Error al verificar blocklist para uid={uid}: {_e} — bloqueando (fail-closed)'
            )
            return True   # fail-closed: preferir seguridad sobre disponibilidad

    @_jwt.revoked_token_loader
    def _revoked_token_response(jwt_header, jwt_payload):
        from flask import jsonify
        return jsonify({'error': 'Usuario desactivado o sesión revocada'}), 401

    from flask import jsonify

    @app.errorhandler(500)
    def error_500(e):
        import traceback
        logging.getLogger(__name__).error(
            'ERROR 500:\n' + traceback.format_exc()
        )
        return jsonify({'error': 'Error interno del servidor'}), 500

    @app.errorhandler(404)
    def error_404(e):
        return jsonify({'error': 'Recurso no encontrado'}), 404

    from app.routes import register_routes
    register_routes(app)

    @app.route('/health')
    def health():
        return jsonify({'status': 'ok'}), 200

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

    # ── CLI: flask create-admin ────────────────────────────────────────────
    @app.cli.command('create-admin')
    @click.option('--email',    default='admin@papeleria.com', help='Email del admin')
    @click.option('--password', default=None, help='Contraseña (obligatorio)')
    @click.option('--nombre',   default='Administrador', help='Nombre del usuario')
    def cmd_create_admin(email, password, nombre):
        """Crea o actualiza el usuario admin. Uso: flask create-admin --password TuClave"""
        if not password:
            click.echo('ERROR: debes pasar --password. Ejemplo: flask create-admin --password MiClave123')
            return
        from app.models.usuario import Usuario
        u = Usuario.query.filter_by(email=email).first()
        if u:
            u.set_password(password)
            u.activo = True
            u.rol = 'admin'
            db.session.commit()
            click.echo(f'Contraseña de {email} actualizada correctamente.')
        else:
            u = Usuario(nombre=nombre, email=email, rol='admin', activo=True)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            click.echo(f'Admin {email} creado correctamente.')

    # ── CLI: flask sync-productos ──────────────────────────────────────────
    @app.cli.command('sync-productos')
    def cmd_sync_productos():
        """Sincroniza el catálogo de productos desde Siesa (upsert)."""
        from app.services.siesa_sync_service import ejecutar_sync
        resultado = ejecutar_sync()
        click.echo(resultado)

    # ── Scheduler: sync automático cada hora 7am–8pm (Bogotá) ─────────────
    if os.getenv('SYNC_SCHEDULER', 'true').lower() == 'true':
        _scheduler_modules = [
            ('app.services.siesa_sync_service',         'init_scheduler',          '[SCHEDULER]'),
            ('app.services.pedidos_sync_service',       'init_scheduler',          '[PEDIDOS_SCHEDULER]'),
            ('app.services.siesa_barcode_sync_service', 'init_scheduler',          '[BARCODE_SCHEDULER]'),
            ('app.services.traslado_monitor_service',   'init_scheduler',          '[TRASLADO_MONITOR]'),
            ('app.services.empaques_sync_service',      'init_scheduler',          '[EMPAQUES_SCHEDULER]'),
            ('app.services.ubicaciones_sync_service',   'init_scheduler',          '[UBICACIONES_SCHEDULER]'),
            ('app.services.siesa_job_service',          'init_scheduler',          '[DLQ_SCHEDULER]'),
            ('app.services.alertas_service',            'init_scheduler',          '[ALERTAS_SCHEDULER]'),
        ]
        _app_logger = logging.getLogger(__name__)
        for _mod_path, _fn_name, _tag in _scheduler_modules:
            try:
                import importlib as _il
                _mod = _il.import_module(_mod_path)
                getattr(_mod, _fn_name)(app)
            except Exception as e:
                _app_logger.error(f'{_tag} No se pudo iniciar: {e}', exc_info=True)

        # ABCService tiene una interfaz diferente (método de clase en vez de función suelta)
        try:
            from app.services.abc_service import ABCService
            ABCService.init_scheduler(app)
        except Exception as e:
            logging.getLogger(__name__).error(f'[ABC_SCHEDULER] No se pudo iniciar: {e}', exc_info=True)

    return app
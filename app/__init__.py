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

    _db_url = os.getenv('DATABASE_URL', '')
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # pool_size/max_overflow solo aplican a PostgreSQL — SQLite (tests) los rechaza
    _engine_opts = {'pool_pre_ping': True}
    if not _db_url.startswith('sqlite'):
        _engine_opts.update({
            'pool_size': int(os.getenv('DB_POOL_SIZE', '20')),
            'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', '10')),
            'pool_recycle': 1800,  # reciclar conexiones cada 30min (Railway puede cerrar idle)
            # Sin esto, una query real puede quedar esperando indefinidamente detrás
            # de una transacción larga de los schedulers de sync (ej. upsert de miles
            # de productos con un solo commit por bodega) — incidente 2026-07-22: /health
            # tardando 20-170s sin ninguna excepción ni WORKER TIMEOUT porque Postgres
            # esperaba el lock sin límite. lock_timeout falla rápido si choca con una fila
            # bloqueada; statement_timeout es la red de seguridad para cualquier otra query
            # colgada. Ambos por sesión vía libpq 'options', configurables por env.
            'connect_args': {
                'options': (
                    f"-c statement_timeout={os.getenv('DB_STATEMENT_TIMEOUT_MS', '25000')} "
                    f"-c lock_timeout={os.getenv('DB_LOCK_TIMEOUT_MS', '8000')}"
                ),
            },
        })
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = _engine_opts
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

    # Cache en memoria para evitar query DB en cada request (TTL 60s)
    _blocklist_cache = {}  # {uid: (activo, timestamp)}
    _BLOCKLIST_TTL = 60

    @_jwt.token_in_blocklist_loader
    def _check_usuario_activo(jwt_header, jwt_payload):
        uid = jwt_payload.get('sub')
        if not uid:
            return True   # token inválido — revocar
        import time
        ahora = time.time()
        cached = _blocklist_cache.get(uid)
        if cached and (ahora - cached[1]) < _BLOCKLIST_TTL:
            return not cached[0]  # cached[0] = activo, retorna True si NO activo
        try:
            from app.models.usuario import Usuario
            u = Usuario.query.get(int(uid))
            activo = u is not None and u.activo
            _blocklist_cache[uid] = (activo, ahora)
            return not activo
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
        # [M10] Verify DB connectivity — Railway uses this to detect unhealthy instances
        try:
            db.session.execute(db.text('SELECT 1'))
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).error('[HEALTH] DB check failed: %s', _e, exc_info=True)
            return jsonify({'status': 'unhealthy'}), 503
        return jsonify({'status': 'ok'}), 200

    @app.route('/static/pwa/sw.js')
    def pwa_sw():
        from flask import make_response as _mkr
        pwa_dir = os.path.join(app.root_path, 'static', 'pwa')
        with open(os.path.join(pwa_dir, 'sw.js'), encoding='utf-8') as _f:
            _content = _f.read()
        try:
            _v = int(os.path.getmtime(os.path.join(pwa_dir, 'app.js')))
        except Exception:
            _v = 0
        resp = _mkr(f'// v{_v}\n' + _content, 200)
        resp.headers['Content-Type'] = 'application/javascript'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp

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

    # ── Schedulers ─────────────────────────────────────────────────────────
    # Arquitectura: web server corre DLQ + pedidos. Worker separado corre
    # los pesados (sync, inventory, prewarm). Nunca compiten por la DB.
    if os.getenv('SYNC_SCHEDULER', 'true').lower() == 'true':
        _app_logger = logging.getLogger(__name__)
        import importlib as _il

        # Esenciales (DLQ + pedidos) — solo en web server, no en worker
        if os.getenv('WORKER_SKIP_ESSENTIAL', 'false').lower() != 'true':
            _scheduler_esenciales = [
                ('app.services.siesa_job_service',          'init_scheduler',          '[DLQ_SCHEDULER]'),
                ('app.services.pedidos_sync_service',       'init_scheduler',          '[PEDIDOS_SCHEDULER]'),
                # Esencial y no pesado: adopcion_picking es la métrica de adopción del
                # go-live. Corre 1 vez por semana y hace upsert idempotente por
                # (serie, semana), así que repetirlo no ensucia nada. Va aquí y no en
                # _scheduler_pesados para que no dependa de HEAVY_SCHEDULERS=true.
                ('app.services.vigia_service',              'init_scheduler',          '[VIGIA_SCHEDULER]'),
            ]
            for _mod_path, _fn_name, _tag in _scheduler_esenciales:
                try:
                    _mod = _il.import_module(_mod_path)
                    getattr(_mod, _fn_name)(app)
                except Exception as e:
                    _app_logger.error(f'{_tag} No se pudo iniciar: {e}', exc_info=True)

        # Pesados — solo en worker separado (HEAVY_SCHEDULERS=true)
        if os.getenv('HEAVY_SCHEDULERS', 'false').lower() == 'true':
            _scheduler_pesados = [
                ('app.services.siesa_sync_service',         'init_scheduler',          '[SCHEDULER]'),
                ('app.services.siesa_barcode_sync_service', 'init_scheduler',          '[BARCODE_SCHEDULER]'),
                ('app.services.empaques_sync_service',      'init_scheduler',          '[EMPAQUES_SCHEDULER]'),
                ('app.services.ubicaciones_sync_service',   'init_scheduler',          '[UBICACIONES_SCHEDULER]'),
                ('app.services.reconciliacion_service',     'init_scheduler',          '[RECONCILIACION_SCHEDULER]'),
                ('app.services.traslado_service',           'init_scheduler',          '[STOCK_PREWARM]'),
                ('app.services.alertas_service',            'init_scheduler',          '[ALERTAS_SCHEDULER]'),
                ('app.services.traslado_monitor_service',   'init_scheduler',          '[TRASLADO_MONITOR]'),
            ]
            for _mod_path, _fn_name, _tag in _scheduler_pesados:
                try:
                    _mod = _il.import_module(_mod_path)
                    getattr(_mod, _fn_name)(app)
                except Exception as e:
                    _app_logger.error(f'{_tag} No se pudo iniciar: {e}', exc_info=True)
            try:
                from app.services.abc_service import ABCService
                ABCService.init_scheduler(app)
            except Exception as e:
                _app_logger.error(f'[ABC_SCHEDULER] No se pudo iniciar: {e}', exc_info=True)
            try:
                from app.services.inventario_siesa_service import iniciar_refresh_periodico
                iniciar_refresh_periodico(app=app)
            except Exception as e:
                _app_logger.error(f'[INV-SIESA] No se pudo iniciar: {e}')
            _app_logger.info('[STARTUP] Schedulers pesados activos (worker mode)')
        else:
            _app_logger.info('[STARTUP] Solo DLQ + pedidos. Pesados corren en worker separado.')

    return app
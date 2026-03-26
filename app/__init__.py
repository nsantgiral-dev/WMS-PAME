import os
from flask import Flask
from dotenv import load_dotenv
from app.extensions import db, migrate, jwt, cors

load_dotenv()

def create_app():
    app = Flask(__name__)
    
    # Configuración
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JWT_SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')
    
    # Inicializar extensiones
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": "*"}})
    
    # Registrar blueprints
    from app.routes import register_routes
    register_routes(app)
    
    return app
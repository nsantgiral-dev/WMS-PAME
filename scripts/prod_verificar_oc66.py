"""
Solo lectura contra la Postgres REAL de produccion (.env, DATABASE_URL) --
NO usa create_app() a proposito (evita levantar el scheduler / hilos de
fondo de la app contra la base compartida). Conexion directa con
SQLAlchemy core, un SELECT nada mas.

Verifica el estado de la recepcion de OC66 (DISPAPELES SAS) y si el job
ENTRADA_OC (142948) ya se disparo hacia Siesa.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from dotenv import dotenv_values
cfg = dotenv_values(os.path.join(REPO_ROOT, '.env'))
db_url = cfg.get('DATABASE_URL')
if not db_url:
    print('[ERROR] DATABASE_URL no encontrado en .env')
    sys.exit(1)

from sqlalchemy import create_engine, text

engine = create_engine(db_url, pool_pre_ping=True)

with engine.connect() as conn:
    print('=== Recepciones para OC66 ===')
    rows = conn.execute(text(
        "SELECT id, numero_oc_siesa, proveedor_nombre, estado, siesa_triggered, "
        "siesa_response, fecha_inicio, fecha_confirmacion "
        "FROM recepciones WHERE numero_oc_siesa = 'OC66' "
        "ORDER BY id DESC LIMIT 5"
    )).mappings().all()
    for r in rows:
        print(dict(r))

    if rows:
        rid = rows[0]['id']
        print(f'\n=== Items de la recepcion {rid} ===')
        items = conn.execute(text(
            "SELECT * FROM items_recepcion WHERE recepcion_id = :rid"
        ), {'rid': rid}).mappings().all()
        for it in items:
            print(dict(it))

        print(f'\n=== SiesaJob ENTRADA_OC para recepcion {rid} ===')
        jobs = conn.execute(text(
            "SELECT id, tipo, estado, intentos, error_ultimo, fecha_creacion "
            "FROM siesa_jobs WHERE referencia_tipo = 'RecepcionMercancia' "
            "AND referencia_id = :rid ORDER BY id DESC"
        ), {'rid': rid}).mappings().all()
        for j in jobs:
            print(dict(j))
    else:
        print('(sin filas — la recepcion de OC66 no aparece todavia en la base)')

import os; os.environ['PYTHONIOENCODING']='utf-8'
from app import create_app; from app.extensions import db; from sqlalchemy import text
app=create_app(); app.app_context().push()

r=db.session.execute(text("SELECT codigo,estado,bodega_origen_siesa FROM solicitudes_traslado WHERE codigo='ST-20260604-6FE6'")).fetchone()
print('SOL:', r)

picks=db.session.execute(text("SELECT id,codigo,estado,cantidad_recogida,cantidad_solicitada,operario_id FROM tareas_picking WHERE referencia_documento='ST-20260604-6FE6'")).fetchall()
print('PICKS:', picks)

sol_id=db.session.execute(text("SELECT id FROM solicitudes_traslado WHERE codigo='ST-20260604-6FE6'")).scalar()
packs=db.session.execute(text("SELECT id,codigo,estado,tipo_documento,solicitud_id,empacador_id,bodega_origen_siesa,almacen_id FROM tareas_packing WHERE solicitud_id=:sid"), {'sid': sol_id}).fetchall()
print('PACKS:', packs)

users=db.session.execute(text("SELECT id,nombre,rol,bodega_siesa_id,almacen_id FROM usuarios WHERE rol IN ('packer_traslado','packer')")).fetchall()
print('PACKER USERS:', users)

# Todas las TareasPacking TRASLADO PENDIENTE
all_packs=db.session.execute(text("SELECT id,codigo,estado,tipo_documento,empacador_id,bodega_origen_siesa,almacen_id FROM tareas_packing WHERE tipo_documento='TRASLADO' ORDER BY id DESC LIMIT 5")).fetchall()
print('ALL TRASLADO PACKS:', all_packs)

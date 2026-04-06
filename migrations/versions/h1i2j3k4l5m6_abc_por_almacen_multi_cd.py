"""abc por almacen multi CD

Revision ID: h1i2j3k4l5m6
Revises: g7h8i9j0k1l2
Create Date: 2026-04-06

Cambios:
- almacenes: +bodega_siesa_id, +centro_op_siesa
- nueva tabla producto_clasificacion_abc (producto_id, almacen_id, clasificacion)
- migra datos: productos.clasificacion_abc → nueva tabla, asociados al primer almacén activo
- productos.clasificacion_abc se mantiene nullable para rollback seguro
"""
from alembic import op
import sqlalchemy as sa

revision = 'h1i2j3k4l5m6'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Nuevos campos en almacenes
    op.add_column('almacenes',
        sa.Column('bodega_siesa_id', sa.String(20), nullable=True))
    op.add_column('almacenes',
        sa.Column('centro_op_siesa', sa.String(20), nullable=True))

    # 2. Nueva tabla ABC por almacén
    op.create_table(
        'producto_clasificacion_abc',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('producto_id', sa.Integer,
                  sa.ForeignKey('productos.id'), nullable=False, index=True),
        sa.Column('almacen_id', sa.Integer,
                  sa.ForeignKey('almacenes.id'), nullable=False, index=True),
        sa.Column('clasificacion', sa.String(1), nullable=False),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(),
                  onupdate=sa.func.now()),
        sa.UniqueConstraint('producto_id', 'almacen_id',
                            name='uq_producto_almacen_abc'),
    )

    # 3. Migrar datos existentes: asociar clasificacion_abc actual
    #    al primer almacén activo (Bodega Principal Papelería Medellín)
    op.execute("""
        INSERT INTO producto_clasificacion_abc
            (producto_id, almacen_id, clasificacion, updated_at)
        SELECT
            p.id,
            a.id,
            p.clasificacion_abc,
            NOW()
        FROM productos p
        CROSS JOIN (
            SELECT id FROM almacenes WHERE activo = true ORDER BY id LIMIT 1
        ) a
        WHERE p.clasificacion_abc IN ('A', 'B', 'C')
        ON CONFLICT (producto_id, almacen_id) DO NOTHING
    """)


def downgrade():
    op.drop_table('producto_clasificacion_abc')
    op.drop_column('almacenes', 'centro_op_siesa')
    op.drop_column('almacenes', 'bodega_siesa_id')

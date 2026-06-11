"""set centro_op_siesa en almacenes según mapeo oficial Siesa PAME

Revision ID: a8b9c0d1e2f3
Revises: z7a8b9c0d1e2
Create Date: 2026-06-11

Mapeo certificado por consultor Siesa:
  NC1 → 002, NS1 → 001, PC1 → 004, FC1 → 006,
  FF1 → 009, FN1 → 007, NB1 → 003, PT1 → 005
"""
from alembic import op
import sqlalchemy as sa

revision = 'a8b9c0d1e2f3'
down_revision = 'z7a8b9c0d1e2'
branch_labels = None
depends_on = None

BODEGA_CO_MAP = {
    'NC1': '002',
    'NS1': '001',
    'PC1': '004',
    'FC1': '006',
    'FF1': '009',
    'FN1': '007',
    'NB1': '003',
    'PT1': '005',
}


def upgrade():
    conn = op.get_bind()
    for bodega, co in BODEGA_CO_MAP.items():
        conn.execute(
            sa.text(
                "UPDATE almacenes SET centro_op_siesa = :co "
                "WHERE bodega_siesa_id = :bodega"
            ),
            {'co': co, 'bodega': bodega},
        )


def downgrade():
    conn = op.get_bind()
    for bodega in BODEGA_CO_MAP:
        conn.execute(
            sa.text(
                "UPDATE almacenes SET centro_op_siesa = NULL "
                "WHERE bodega_siesa_id = :bodega"
            ),
            {'bodega': bodega},
        )

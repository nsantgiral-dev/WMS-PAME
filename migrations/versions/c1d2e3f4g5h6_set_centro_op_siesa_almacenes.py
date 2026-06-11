"""set centro_op_siesa en almacenes según mapeo oficial Siesa PAME

Revision ID: c1d2e3f4g5h6
Revises: b3c4d5e6f7g8
Create Date: 2026-06-11

Mapeo certificado por consultor Siesa:
  NC1 → 002, NS1 → 001, PC1 → 004, FC1 → 006,
  FF1 → 009, FN1 → 007, NB1 → 003, PT1 → 005
"""
from alembic import op
import sqlalchemy as sa

revision = 'c1d2e3f4g5h6'
down_revision = 'b3c4d5e6f7g8'
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

"""flota: plantillas de inspeccion versionadas y su catalogo de items

Revision ID: f10ta2plantillas
Revises: f10ta1cimientos
Create Date: 2026-08-03

PURAMENTE ADITIVA. Dos tablas nuevas, ningun ALTER, ningun DROP.

Prepara la tanda 2. Solo el esquema — el catalogo se siembra aparte con
`scripts/sembrar_plantillas_flota.py`, que es idempotente: las plantillas son
datos que se leen y se auditan, no esquema, y quiero ver el diff cuando alguien
cambie un gesto.

Cuerpo emitido desde `db.metadata`, no transcrito a mano.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta2plantillas'
down_revision = 'f10ta1cimientos'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'flota_plantilla_inspeccion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('codigo', sa.String(length=40), nullable=False),
        sa.Column('nombre', sa.String(length=100), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('aplica_a', sa.String(length=20), nullable=False),
        sa.Column('activa', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('fecha_creacion', sa.DateTime(), nullable=True),
        sa.CheckConstraint('version > 0', name='ck_flota_plantilla_version'),
        sa.UniqueConstraint('codigo', name=None),
        sa.UniqueConstraint('aplica_a', 'version', name='uq_flota_plantilla_version'),
        sa.CheckConstraint("aplica_a IN ('furgon_liviano', 'camion', 'motocarro')", name='ck_flota_aplica_a'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'flota_item_inspeccion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plantilla_id', sa.Integer(), nullable=False),
        sa.Column('orden', sa.Integer(), nullable=False),
        sa.Column('nombre', sa.String(length=120), nullable=False),
        sa.Column('gesto', sa.Text(), nullable=False),
        sa.Column('criticidad', sa.String(length=20), nullable=False),
        sa.Column('periodicidad', sa.String(length=20), nullable=False, server_default='diaria'),
        sa.ForeignKeyConstraint(['plantilla_id'], ['flota_plantilla_inspeccion.id']),
        sa.CheckConstraint('length(trim(nombre)) > 0', name='ck_flota_item_nombre_no_vacio'),
        sa.CheckConstraint('length(trim(gesto)) > 0', name='ck_flota_item_gesto_no_vacio'),
        sa.CheckConstraint("periodicidad IN ('diaria', 'semanal')", name='ck_flota_periodicidad'),
        sa.UniqueConstraint('plantilla_id', 'orden', name='uq_flota_item_orden'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("criticidad IN ('bloqueante', 'mayor', 'menor')", name='ck_flota_criticidad'),
    )
    op.create_index('ix_flota_item_inspeccion_plantilla_id', 'flota_item_inspeccion', ['plantilla_id'], unique=False)



def downgrade():
    op.drop_table('flota_item_inspeccion')
    op.drop_table('flota_plantilla_inspeccion')

"""Registro de avisos de flota (WhatsApp)

Revision ID: f10ta7aviso
Revises: f10ta6adjunto
Create Date: 2026-08-05

Una fila por aviso mandado, con `clave` única: el barrido puede correr todas
las noches sin repetir el mismo vencimiento. Un documento renovado sí vuelve a
avisar, porque la clave lleva la fecha del hito.

`entregado_al_proveedor` y `entregado` son estados distintos a propósito.
Gupshup responde `submitted` y eso significa "lo recibí", no "llegó". En cartera
esa confusión costó semanas de tablero diciendo enviado con el teléfono en
silencio; acá pesa más, porque el módulo existe justamente para que un
vencimiento no se quede quieto.

`simulado` es columna y no solo log (regla 8 de flota): `CanalNotificacionDev`
ya costó una hora de creer que 1.485 personas habían recibido un cobro que nunca
salió.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta7aviso'
down_revision = 'f10ta6adjunto'
branch_labels = None
depends_on = None

_ESTADOS = ('encolado', 'entregado_al_proveedor', 'entregado', 'leido', 'fallido')


def upgrade():
    op.create_table(
        'flota_aviso',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clave', sa.String(200), nullable=False),
        sa.Column('plantilla', sa.String(60), nullable=False),
        sa.Column('telefono', sa.String(30), nullable=False),
        sa.Column('parametros', sa.Text(), nullable=False),
        sa.Column('estado', sa.String(30), nullable=False, server_default='encolado'),
        sa.Column('proveedor_msg_id', sa.String(120), nullable=True),
        sa.Column('detalle', sa.Text(), nullable=True),
        sa.Column('simulado', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('creado_ts', sa.DateTime(), nullable=False),
        sa.Column('entregado_ts', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('clave', name='uq_flota_aviso_clave'),
        sa.CheckConstraint(
            'estado IN (%s)' % ', '.join(f"'{e}'" for e in _ESTADOS),
            name='ck_flota_estado'),
        sa.CheckConstraint('length(trim(telefono)) > 0',
                           name='ck_flota_aviso_telefono'),
        # Un aviso que salió sin id del proveedor es incomprobable: no hay con
        # qué cruzar el evento de entrega. Se impone en la base, no solo en el
        # servicio, porque un INSERT desde psql lo esquivaría.
        sa.CheckConstraint(
            "estado IN ('encolado', 'fallido') OR "
            "(proveedor_msg_id IS NOT NULL AND length(trim(proveedor_msg_id)) > 0)",
            name='ck_flota_aviso_id_proveedor'),
    )
    op.create_index('ix_flota_aviso_proveedor', 'flota_aviso', ['proveedor_msg_id'])


def downgrade():
    op.drop_index('ix_flota_aviso_proveedor', table_name='flota_aviso')
    op.drop_table('flota_aviso')

"""Flota — la cola del conductor no pierde nada (T2 de la auditoría del 2026-09-27)

Revision ID: m051flotakm
Revises: m050inv2
Create Date: 2026-09-27

Aditiva, sin backfill.

· **`flota_rechazo_cola`** — lo que la cola del conductor mandó y el servidor
  rechazó (400/403/404/409). Hasta hoy un rechazo borraba la operación del
  teléfono con sus fotos y el servidor no guardaba nada: un tanqueo pagado o
  una inspección hecha desaparecían sin que control de flota se enterara. Una
  fila por (usuario, clave de reenvío); la anota `@idempotente` en su propia
  transacción, y pasa a `resuelto` cuando la misma clave entra.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051flotakm'
down_revision = 'm050inv2'
branch_labels = None
depends_on = None

_OPERACIONES = ('traspaso', 'hallazgo', 'inspeccion', 'tanqueo')
_ESTADOS = ('abierto', 'resuelto', 'descartado', 'cerrado')


def _en(columna, valores):
    return '%s IN (%s)' % (columna, ', '.join(f"'{v}'" for v in valores))


def upgrade():
    op.create_table(
        'flota_rechazo_cola',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('clave', sa.String(64), nullable=False),
        sa.Column('operacion', sa.String(20), nullable=False),
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuarios.id'),
                  nullable=False),
        sa.Column('placa', sa.String(20), nullable=True),
        sa.Column('vehiculo_id', sa.Integer(), sa.ForeignKey('vehiculos.id'),
                  nullable=True),
        sa.Column('status_http', sa.Integer(), nullable=False),
        sa.Column('mensaje', sa.Text(), nullable=False),
        sa.Column('intentos', sa.Integer(), nullable=False),
        sa.Column('primer_ts', sa.DateTime(), nullable=False),
        sa.Column('ultimo_ts', sa.DateTime(), nullable=False),
        sa.Column('ts_dispositivo', sa.DateTime(), nullable=True),
        sa.Column('estado', sa.String(12), nullable=False),
        sa.Column('pidio_ayuda_ts', sa.DateTime(), nullable=True),
        sa.Column('cerrado_ts', sa.DateTime(), nullable=True),
        sa.Column('cerrado_por_usuario_id', sa.Integer(),
                  sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('cierre_motivo', sa.Text(), nullable=True),
        sa.UniqueConstraint('usuario_id', 'clave', name='uq_flota_rechazo_cola_clave'),
        sa.CheckConstraint(_en('operacion', _OPERACIONES),
                           name='ck_flota_rechazo_cola_operacion'),
        sa.CheckConstraint(_en('estado', _ESTADOS),
                           name='ck_flota_rechazo_cola_estado'),
        sa.CheckConstraint(
            "estado <> 'cerrado' OR (cerrado_por_usuario_id IS NOT NULL AND "
            "cierre_motivo IS NOT NULL AND length(trim(cierre_motivo)) > 0)",
            name='ck_flota_rechazo_cola_cierre_con_autor'),
    )


def downgrade():
    op.drop_table('flota_rechazo_cola')

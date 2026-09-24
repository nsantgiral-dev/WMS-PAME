"""Flota — la pantalla del conductor: reenvío sin duplicar y foto del recibo

Revision ID: m039flcond
Revises: m037kpi
Create Date: 2026-09-24

Dos cambios, los dos aditivos y sin backfill:

· **`flota_idempotencia`** — la clave que el celular pone al ENCOLAR un recibo,
  una inspección, un daño o un tanqueo hecho sin señal. Un reenvío con la misma
  clave devuelve lo que ya se hizo en vez de ejecutarlo otra vez. Sin esto, el
  segundo envío de un recibo cerraba la custodia recién abierta y abría otra con
  los mismos kilómetros (la ventana de 90 s de `traspaso.py` no cubre una cola
  que se vacía horas después).

· **`flota_foto.entidad_tipo` admite `gasto`** — la foto del recibo de un
  tanqueo. No se reusó `documento`: ese padre son los papeles del vehículo, que
  el conductor no debe poder bajar.

Las filas existentes cumplen el CHECK nuevo (es un superconjunto del viejo).
"""
import sqlalchemy as sa
from alembic import op

revision = 'm039flcond'
down_revision = 'm037kpi'
branch_labels = None
depends_on = None

_ENTIDADES_VIEJAS = ('custodia_inicio', 'custodia_fin', 'odometro', 'documento',
                     'hallazgo')
_ENTIDADES_NUEVAS = _ENTIDADES_VIEJAS + ('gasto',)
_OPERACIONES = ('traspaso', 'hallazgo', 'inspeccion', 'tanqueo')


def _en(columna, valores):
    return '%s IN (%s)' % (columna, ', '.join(f"'{v}'" for v in valores))


def upgrade():
    op.create_table(
        'flota_idempotencia',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('clave', sa.String(64), nullable=False),
        sa.Column('operacion', sa.String(20), nullable=False),
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuarios.id'),
                  nullable=False),
        sa.Column('entidad_id', sa.Integer(), nullable=True),
        sa.Column('respuesta', sa.Text(), nullable=True),
        sa.Column('creado_ts', sa.DateTime(), nullable=False),
        sa.Column('ts_dispositivo', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('clave', name='flota_idempotencia_clave_key'),
        sa.CheckConstraint(_en('operacion', _OPERACIONES),
                           name='ck_flota_idempotencia_operacion'),
    )
    with op.batch_alter_table('flota_foto') as batch:
        batch.drop_constraint('ck_flota_entidad_tipo', type_='check')
        batch.create_check_constraint('ck_flota_entidad_tipo',
                                      _en('entidad_tipo', _ENTIDADES_NUEVAS))


def downgrade():
    # Las fotos de recibo no caben en el CHECK viejo. Se borran sus FILAS, no
    # sus archivos: el binario sigue en el almacén. Mismo criterio que
    # `f10ta6adjunto`.
    op.execute("DELETE FROM flota_foto WHERE entidad_tipo = 'gasto'")
    with op.batch_alter_table('flota_foto') as batch:
        batch.drop_constraint('ck_flota_entidad_tipo', type_='check')
        batch.create_check_constraint('ck_flota_entidad_tipo',
                                      _en('entidad_tipo', _ENTIDADES_VIEJAS))
    op.drop_table('flota_idempotencia')

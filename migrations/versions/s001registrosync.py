"""Registro persistente de corridas de sync

Revision ID: s001registrosync
Revises: f10ta8sinvence
Create Date: 2026-08-10

El estado de los syncs de arranque vivía en diccionarios de módulo y cada deploy
los ponía en `None`. Medido en producción el 2026-08-10: `resultado_catalogo:
null` después de tres deploys el mismo día — sin forma de saber si el catálogo
estaba cargado o si simplemente el proceso se había reiniciado.

Importa el día del corte: catálogo → códigos de barras → **stock inicial una
sola vez**. Cargar el stock dos veces duplica el inventario de arranque, y la
única defensa era la memoria de quien lo hizo.

`ok` admite NULL a propósito: una corrida abierta (proceso muerto a mitad) no es
lo mismo que una fallida, y el día del corte esa diferencia es la que se
pregunta.
"""
from alembic import op
import sqlalchemy as sa

revision = 's001registrosync'
down_revision = 'f10ta8sinvence'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'registros_sync',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=40), nullable=False),
        sa.Column('inicio', sa.DateTime(), nullable=False),
        sa.Column('fin', sa.DateTime(), nullable=True),
        sa.Column('ok', sa.Boolean(), nullable=True),
        sa.Column('resultado', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        # Una corrida cerrada tiene fin; una abierta no tiene ninguno de los
        # dos. `ok` con `fin` en NULL seria un cierre a medias que se leeria
        # como corrida terminada.
        sa.CheckConstraint(
            '(ok IS NULL AND fin IS NULL) OR (ok IS NOT NULL AND fin IS NOT NULL)',
            name='ck_registro_sync_cierre_completo'),
    )
    op.create_index('ix_registros_sync_tipo', 'registros_sync', ['tipo'])
    # La consulta real siempre es "la ultima de este tipo" y "la ultima OK de
    # este tipo". Sin este indice cada lectura del health escanea la tabla.
    op.create_index('ix_registros_sync_tipo_inicio', 'registros_sync',
                    ['tipo', 'inicio'])


def downgrade():
    op.drop_index('ix_registros_sync_tipo_inicio', table_name='registros_sync')
    op.drop_index('ix_registros_sync_tipo', table_name='registros_sync')
    op.drop_table('registros_sync')

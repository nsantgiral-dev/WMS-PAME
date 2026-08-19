"""Decisión explícita del admin sobre la retención declarada en campo

Revision ID: m010retencionconfirmada
Revises: m009fuenteexistencia
Create Date: 2026-08-19

`motivo_descuento` es lo que el CONDUCTOR anotó en la pantalla de pago
parcial — el motivo tributario que el cliente dijo (RETEFUENTE, ReteIVA,
ICA), sin que nadie lo haya verificado. En Liquidación eso solo aparecía
como una casilla premarcada "sugerida"; nada obligaba al admin a decidir si
el cliente de verdad tenía derecho al descuento antes de crear el recibo de
caja (RC).

Esta migración agrega la decisión como su propio dato — no un booleano
reutilizado, porque `None` (pendiente) y `False` (rechazada) tienen que
comportarse distinto (el primero exige decidir; el segundo bloquea el RC
hasta que el cliente pague el valor completo) y un booleano no distingue
"no sé" de "no".
"""
from alembic import op
import sqlalchemy as sa

revision = 'm010retencionconfirmada'
down_revision = 'm009fuenteexistencia'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('retencion_confirmada', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('retencion_confirmada_por', sa.Integer(),
                                    sa.ForeignKey('usuarios.id'), nullable=True))
        batch.add_column(sa.Column('retencion_confirmada_en', sa.DateTime(), nullable=True))
    # Los recaudos ya existentes con motivo_descuento pero sin RC disparado
    # quedan en None (pendiente) — es lo correcto: nadie decidió nada sobre
    # ellos todavía, y None es exactamente "pendiente de decisión". Los que
    # YA tienen RC disparado no pasan por este guard nunca más (registra_cobro
    # solo lo revisa antes de crear el RC), así que no hace falta tocarlos.


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_column('retencion_confirmada_en')
        batch.drop_column('retencion_confirmada_por')
        batch.drop_column('retencion_confirmada')

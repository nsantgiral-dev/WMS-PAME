"""add siesa_jobs — Dead Letter Queue para jobs asíncronos hacia Siesa

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-04-20

Todo job que dispara un conector POST a Siesa pasa por esta tabla.
Si Connekta rechaza (periodo cerrado, ítem bloqueado, timeout), el sistema
reintenta automáticamente con backoff exponencial hasta 3 veces.
Tras 3 fallos: estado=FALLIDO → alerta roja en el dashboard del admin.
"""
from alembic import op
import sqlalchemy as sa

revision = 'z7a8b9c0d1e2'
down_revision = 'y6z7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'siesa_jobs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('tipo', sa.String(50), nullable=False),
        # Tipos: TRANSFERENCIA_UBICACIONES | AJUSTE_INVENTARIO | ENTRADA_OC | DESPACHO_PEDIDO

        sa.Column('payload', sa.Text, nullable=False),         # JSON del body a enviar
        sa.Column('referencia_tipo', sa.String(50), nullable=True),  # 'TareaReposicion', etc.
        sa.Column('referencia_id', sa.Integer, nullable=True),

        sa.Column('estado', sa.String(20), nullable=False, server_default='PENDIENTE'),
        # PENDIENTE → PROCESANDO → COMPLETADO | FALLIDO

        sa.Column('intentos', sa.Integer, nullable=False, server_default='0'),
        sa.Column('max_intentos', sa.Integer, nullable=False, server_default='3'),
        sa.Column('proximo_intento', sa.DateTime, nullable=True),  # NULL = procesar ya
        sa.Column('resultado', sa.Text, nullable=True),            # JSON respuesta Siesa OK
        sa.Column('error_ultimo', sa.Text, nullable=True),         # último mensaje de error

        sa.Column('creado_por_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_completado', sa.DateTime, nullable=True),
    )
    op.create_index('ix_siesa_jobs_estado', 'siesa_jobs', ['estado'])
    op.create_index('ix_siesa_jobs_proximo_intento', 'siesa_jobs', ['proximo_intento'])
    op.create_index('ix_siesa_jobs_referencia', 'siesa_jobs', ['referencia_tipo', 'referencia_id'])


def downgrade():
    op.drop_table('siesa_jobs')

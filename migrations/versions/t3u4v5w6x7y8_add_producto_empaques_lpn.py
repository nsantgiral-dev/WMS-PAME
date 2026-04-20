"""add producto_empaques y lpn

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-04-20

producto_empaques: catálogo de empaques por producto (DUN-14, pacas, cajas).
lpn: instancias físicas de pacas/empaques en el almacén (License Plate Numbers).
"""
from alembic import op
import sqlalchemy as sa

revision = 't3u4v5w6x7y8'
down_revision = 'v5w6x7y8z9a0'
branch_labels = None
depends_on = None


def upgrade():
    # ── producto_empaques ──────────────────────────────────────────────────────
    # Catálogo de niveles de empaque por producto.
    # Una fila por cada código de barras que representa un nivel de empaque.
    # Un producto puede tener múltiples filas (ej: CAJ=5, PACA=60, UND=1).
    op.create_table(
        'producto_empaques',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id', ondelete='CASCADE'), nullable=False),
        sa.Column('referencia_item', sa.String(50), nullable=False,
                  comment='codigo_siesa del producto — para re-sync sin JOIN'),
        sa.Column('codigo_barras', sa.String(50), nullable=False,
                  comment='Lo que lee el scanner: DUN-14, EAN-13 de caja, LPN-XXXXXXX'),
        sa.Column('unidad_medida', sa.String(20), nullable=False,
                  comment='CAJ, PACA, PAQ, DOC, UND — según Siesa'),
        sa.Column('factor_conversion', sa.Integer, nullable=False,
                  comment='Cuántas unidades base (UND) contiene este empaque'),
        sa.Column('origen', sa.String(20), nullable=False, default='SIESA_GS1',
                  comment='SIESA_GS1: vino del worker | WMS_LPN: creado manualmente en recepción'),
        sa.Column('activo', sa.Boolean, nullable=False, default=True),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_actualizacion', sa.DateTime, server_default=sa.func.now(),
                  onupdate=sa.func.now()),
    )
    op.create_index('ix_producto_empaques_codigo_barras', 'producto_empaques', ['codigo_barras'])
    op.create_index('ix_producto_empaques_producto_id', 'producto_empaques', ['producto_id'])
    op.create_index('ix_producto_empaques_referencia_item', 'producto_empaques', ['referencia_item'])

    # ── lpn ───────────────────────────────────────────────────────────────────
    # Instancias físicas de empaques en el almacén.
    # Un LPN = una paca/caja física con etiqueta impresa pegada.
    # Nace en recepción, viaja en traslados, muere cuando se abre el empaque.
    op.create_table(
        'lpn',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('codigo', sa.String(20), unique=True, nullable=False,
                  comment='LPN-0000001 — secuencial opaco, nunca embedar factor aquí'),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('empaque_id', sa.Integer, sa.ForeignKey('producto_empaques.id'), nullable=True,
                  comment='Template del empaque — NULL si es LPN huérfano (sin GS1)'),
        sa.Column('factor_conversion', sa.Integer, nullable=False,
                  comment='Copia del factor al momento de creación — inmutable aunque cambie el empaque'),
        sa.Column('cantidad_actual', sa.Integer, nullable=False,
                  comment='Unidades base reales en esta paca (puede ser < factor si está parcialmente abierta)'),
        sa.Column('estado', sa.String(20), nullable=False, default='ACTIVO',
                  comment='ACTIVO | EN_TRANSITO | CONSUMIDO'),
        sa.Column('almacen_id', sa.Integer, sa.ForeignKey('almacenes.id'), nullable=True),
        sa.Column('ubicacion_id', sa.Integer, sa.ForeignKey('ubicaciones.id'), nullable=True),
        sa.Column('recepcion_id', sa.Integer, sa.ForeignKey('recepciones.id'), nullable=True,
                  comment='Recepción que creó este LPN — trazabilidad completa'),
        sa.Column('traslado_id', sa.Integer, sa.ForeignKey('solicitudes_traslado.id'), nullable=True,
                  comment='Traslado activo que transporta este LPN'),
        sa.Column('creado_por_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_consumo', sa.DateTime, nullable=True),
        sa.Column('notas', sa.Text, nullable=True,
                  comment='Observaciones del operario al crear el LPN'),
    )
    op.create_index('ix_lpn_codigo', 'lpn', ['codigo'])
    op.create_index('ix_lpn_producto_id', 'lpn', ['producto_id'])
    op.create_index('ix_lpn_estado', 'lpn', ['estado'])
    op.create_index('ix_lpn_almacen_id', 'lpn', ['almacen_id'])


def downgrade():
    op.drop_table('lpn')
    op.drop_table('producto_empaques')

"""`tareas_picking.disponible_siesa` — cuánto seguía comprometido en Siesa
para esta línea al crear la tarea

Revision ID: m017disponiblesiesapicking
Revises: m016ubicacioncodigoporalmacen
Create Date: 2026-09-02

PD1447 (2026-09-02): el operario pickeó y empacó 4 unidades de BELLESB1382
que el WMS creía disponibles, pero Siesa las tenía comprometidas para otros
pedidos — el 244328 (CompromisosPedido) rechazó con "No hay suficiente
cantidad disponible" y ni la remisión ni la factura llegaron a dispararse.
El operario nunca tuvo forma de saberlo mientras contaba.

`backorder_service.compromisos_por_siesa()` ya consulta, por SKU, cuánto
sigue comprometido en Siesa para ese pedido puntual
(`f405_cant_por_remisionar_base`) — antes solo se usaba para bloquear la
línea si el valor era cero (backorder). Esta columna guarda ese número al
crear la tarea (`/api/siesa/iniciar-despacho`) para que el HUD del operario
pueda mostrar "disponible X de Y" mientras cuenta, en vez de que el rechazo
aparezca recién en el despacho, minutos u horas después.

`NULL` = no se consultó (traslado, tarea manual, o Siesa no respondió ese
ciclo) — nunca se inventa un número (Regla 0).
"""
from alembic import op
import sqlalchemy as sa

revision = 'm017disponiblesiesapicking'
down_revision = 'm016ubicacioncodigoporalmacen'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'tareas_picking',
        sa.Column('disponible_siesa', sa.Numeric(12, 2), nullable=True),
    )


def downgrade():
    op.drop_column('tareas_picking', 'disponible_siesa')

"""El conteo guarda también la foto de Siesa de cuando se ABRIÓ la tarea

Revision ID: m029fotoinicioconteo
Revises: m028fotosiesaconteo
Create Date: 2026-09-23

## Ventas durante un mismo conteo

m028 guardó la foto de Siesa del CIERRE: se lee al confirmar el conteo. Pero el
físico no se cuenta en ese instante: se cuenta en el intervalo entre abrir la
tarea y confirmarla. Si en ese intervalo Siesa se mueve —un cliente con el
producto en la canasta paga en caja; una unidad ya contada se vende; se acumula
el POS—, el físico y la foto del cierre miden instantes distintos y el conteo
fabrica un sobrante o un faltante que no existe. Si pasa igual en CC1 y CC2 se
auto-ajusta mal; si pasa en el CC3, el definitivo, llega a Siesa.

Con la foto de la apertura se sabe: si existencia, POS o salida sin confirmar
cambiaron entre las dos, el conteo se descarta y se recuenta
(`ConteoService.movimiento_durante_conteo`). `conteos_descartados` guarda lo que
se contó y se descartó, porque el recuento se hace sobre la misma sesión.

## Por qué nullable, y qué pasa con las sesiones viejas

Una sesión abierta antes de esta migración no tiene foto de inicio, y no se le
inventa: NULL es «no se sabe». El servicio trata igual «Siesa no respondió al
abrir» y «la sesión es anterior a la columna» — Regla 0, la misma decisión que
m028 tomó con la foto del cierre:

- una sesión vieja EN_PROCESO se puede terminar de contar, y su conteo decide
  MATCH o segundo conteo como siempre, pero **no ajusta**;
- una sesión vieja en DESCUADRE esperando aprobación ya **no se aprueba**: se
  recuenta con un conteo nuevo. Un ajuste de inventario siempre puede esperar.

No se agrega a m028 porque m028 ya está en `qa` y QA ya la aplicó.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm029fotoinicioconteo'
down_revision = 'm028fotosiesaconteo'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('existencia_inicio_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('cant_pos_inicio_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('salida_sin_conf_inicio_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('foto_inicio_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('conteos_descartados', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('conteos_descartados')
        batch.drop_column('foto_inicio_at')
        batch.drop_column('salida_sin_conf_inicio_siesa')
        batch.drop_column('cant_pos_inicio_siesa')
        batch.drop_column('existencia_inicio_siesa')

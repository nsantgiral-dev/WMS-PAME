"""Conteo: la unidad contra Siesa es SKU × almacén; quién omitió la verificación

Revision ID: m051conteo
Revises: m051asignacion (re-encadenada en la integración final del 2026-09-29; se escribió sobre m051flotalegal)
Create Date: 2026-09-27

Índice parcial único `ix_sesion_conteo_sku_activa_unica` sobre
`(producto_id, almacen_id)` de las raíces vivas (mismo predicado que
`ix_sesion_conteo_activa_unica`, que era por hueco). Antes dos cadenas del
mismo SKU en dos huecos del mismo almacén comparaban cada una su parte contra
el TOTAL de Siesa (la foto es por ítem × bodega) y ajustaban las dos.

**Un paso previo, imprescindible para el índice:** si ya hay dos raíces vivas
del mismo SKU en el mismo almacén, las PENDIENTE sobrantes (nadie las abrió:
no tienen conteo ni hijos) se cancelan con su motivo, quedándose la que ya
se está contando o, si ninguna, la más vieja. Medido en producción el
2026-09-27: cero grupos duplicados. Si quedaran dos que YA se están contando,
la migración se detiene y los nombra: cancelar un conteo en curso lo decide
una persona, no un deploy.

`sesiones_conteo.verificacion_omitida_*` (nullable, sin backfill): quién
saltó el 2º conteo de una cadena, por qué y cuándo
(`ConteoService.omitir_verificacion`). Quien omitió no aprueba ese ajuste.
`cantidad_corregida_por_id`: quién corrigió a mano la cifra (`/editar`); tampoco
lo aprueba. `sin_fila_en_siesa`: la foto era la fila en cero de un ítem que
Siesa no tenía en la bodega (esa entrada la firma el admin).
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051conteo'
down_revision = 'm051asignacion'
branch_labels = None
depends_on = None

_VIVAS = "('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO')"


def upgrade():
    bind = op.get_bind()
    falso = 'false' if bind.dialect.name == 'postgresql' else '0'
    bind.execute(sa.text(f"""
        UPDATE sesiones_conteo
        SET estado = 'CANCELADO',
            fecha_cierre = CURRENT_TIMESTAMP,
            motivo_edicion = COALESCE(motivo_edicion || ' · ', '') ||
                '[m051conteo] cancelada: otra cadena del mismo producto en el mismo almacén ya estaba abierta (la unidad del conteo es SKU × almacén)'
        WHERE id IN (
            SELECT id FROM (
                SELECT id, estado,
                       ROW_NUMBER() OVER (
                           PARTITION BY producto_id, almacen_id
                           ORDER BY CASE WHEN estado = 'PENDIENTE' THEN 1 ELSE 0 END, id
                       ) AS rn
                FROM sesiones_conteo
                WHERE estado IN {_VIVAS} AND es_segundo_conteo = {falso}
            ) t
            WHERE t.rn > 1 AND t.estado = 'PENDIENTE'
        )
    """))
    quedan = bind.execute(sa.text(f"""
        SELECT producto_id, almacen_id, COUNT(*) FROM sesiones_conteo
        WHERE estado IN {_VIVAS} AND es_segundo_conteo = {falso}
        GROUP BY producto_id, almacen_id HAVING COUNT(*) > 1
    """)).fetchall()
    if quedan:
        raise RuntimeError(
            'm051conteo: hay productos con dos conteos EN CURSO en el mismo almacén '
            f'(producto, almacén, cuántos): {list(quedan)[:20]}. Cancele uno de cada '
            'par desde Inventario Cíclico y vuelva a desplegar.')
    with op.batch_alter_table('sesiones_conteo') as b:
        b.add_column(sa.Column('verificacion_omitida_por_id', sa.Integer(),
                               sa.ForeignKey('usuarios.id',
                                             name='fk_sesion_conteo_omitida_por'),
                               nullable=True))
        b.add_column(sa.Column('verificacion_omitida_motivo', sa.Text(), nullable=True))
        b.add_column(sa.Column('verificacion_omitida_en', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('cantidad_corregida_por_id', sa.Integer(),
                               sa.ForeignKey('usuarios.id',
                                             name='fk_sesion_conteo_corregida_por'),
                               nullable=True))
        b.add_column(sa.Column('sin_fila_en_siesa', sa.Boolean(), nullable=True))
    op.create_index(
        'ix_sesion_conteo_sku_activa_unica',
        'sesiones_conteo',
        ['producto_id', 'almacen_id'],
        unique=True,
        postgresql_where=sa.text(f"estado IN {_VIVAS} AND es_segundo_conteo = false"),
        sqlite_where=sa.text(f"estado IN {_VIVAS} AND es_segundo_conteo = 0"),
    )


def downgrade():
    op.drop_index('ix_sesion_conteo_sku_activa_unica', table_name='sesiones_conteo')
    with op.batch_alter_table('sesiones_conteo') as b:
        b.drop_column('sin_fila_en_siesa')
        b.drop_constraint('fk_sesion_conteo_corregida_por', type_='foreignkey')
        b.drop_column('cantidad_corregida_por_id')
        b.drop_constraint('fk_sesion_conteo_omitida_por', type_='foreignkey')
        b.drop_column('verificacion_omitida_en')
        b.drop_column('verificacion_omitida_motivo')
        b.drop_column('verificacion_omitida_por_id')

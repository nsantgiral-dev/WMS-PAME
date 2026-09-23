"""Conteo: motivo del bloqueo como campo, y novedades «mercancía sin código»

Revision ID: m031hud
Revises: m031guardaenproceso
Create Date: 2026-09-23

## Para qué

El HUD de conteo del operario cierra una tarea de tres formas explícitas —y
ninguna es un cero implícito—: «ya revisé todo, conté N», «no lo encontré» y
«encontré mercancía sin código».

- **«No lo encontré»** deja la sesión BLOQUEADA hasta que el líder la reabra o
  la cancele. El motivo iba solo dentro del texto de `motivo_edicion`
  (`'[UBICACION_VACIA] …'`), que un admin pisa al editar: se vuelve campo
  (`motivo_bloqueo`) con su instante (`bloqueado_en`).
- **«Mercancía sin código»** no bloquea nada: es una nota para el líder. No
  tenía dónde vivir; nace `novedades_conteo`.

## Sin backfill

Los BLOQUEADO anteriores tienen el motivo solo en la prosa de
`motivo_edicion`. No se parsea: se listan como «sin motivo registrado» junto a
ese texto, que el líder lee tal cual.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm031hud'
down_revision = 'm031guardaenproceso'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('motivo_bloqueo', sa.String(30), nullable=True))
        batch.add_column(sa.Column('bloqueado_en', sa.DateTime(), nullable=True))

    op.create_table(
        'novedades_conteo',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tipo', sa.String(30), nullable=False),
        sa.Column('sesion_id', sa.Integer(), sa.ForeignKey('sesiones_conteo.id'), nullable=True),
        sa.Column('almacen_id', sa.Integer(), sa.ForeignKey('almacenes.id'), nullable=True),
        sa.Column('reportado_por', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=False),
        sa.Column('descripcion', sa.Text(), nullable=False),
        sa.Column('estado', sa.String(15), nullable=False),
        sa.Column('fecha_creacion', sa.DateTime(), nullable=False),
        sa.Column('resuelta_por', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('resuelta_en', sa.DateTime(), nullable=True),
        sa.Column('nota_resolucion', sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table('novedades_conteo')
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('bloqueado_en')
        batch.drop_column('motivo_bloqueo')

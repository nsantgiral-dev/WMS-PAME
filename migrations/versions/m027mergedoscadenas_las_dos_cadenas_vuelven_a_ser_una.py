"""Las dos cadenas de migraciones vuelven a ser una

Revision ID: m027mergedoscadenas
Revises: m026trasladoaverias, n021cantidadpedidatareapicking
Create Date: 2026-09-18

## No hace nada, y ese es el punto

Una migración de merge no toca el esquema: existe para que Alembic tenga **un
solo head**. `railway.toml` corre `flask db upgrade` como
`releaseCommand`, y
con dos heads el release falla — así que sin este archivo no se puede desplegar
ninguna de las dos ramas.

## Cómo se llegó a dos

Las dos cuelgan del mismo punto, `m019eventosstockagotado`:

    m019eventosstockagotado ─┬── n020 ── n021              main
                             └── m016 ── … ── m026        flota/tandas-2026-09

`main` siguió con la serie `n0*` (scan de recepción, backorder parcial de
picking) mientras esta rama seguía con la `m0*` (flota, geo, triggers, averías).
Ninguna de las dos supo de la otra durante ocho días.

## Por qué no se descubrió antes

Porque el síntoma se lee como otra cosa. Producción está sellada en `n021`, y
buscar esa revisión desde esta rama da cero resultados — la conclusión natural
es «alguien selló la base a mano con un nombre inventado», no «hay otra cadena».

El hallazgo llegó midiendo: el esquema real de producción tenía **exactamente
una columna** que esta rama no conoce (`items_recepcion.ultimo_scan_id`), y el
nombre de la revisión fantasma —`n020additemscanid`— la nombraba. No era una
migración huérfana de código muerto: era código vivo, desplegando.

## Qué NO resuelve este archivo

El orden de aplicación. Producción está en `n021` y le faltan las once de la
serie `m0*`; al correr el upgrade, Alembic las aplicará y después este merge.
Eso se ensayó sobre una réplica del estado real de producción —cadena hasta
`m019` + la columna de `n020` + el sello— y terminó limpio, con los 45 tests
de constraints de PostgreSQL en verde sobre el esquema resultante.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm027mergedoscadenas'
down_revision = ('m026trasladoaverias', 'n021cantidadpedidatareapicking')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass

"""Dos columnas que viven en los modelos y ninguna migración creaba

Revision ID: m014columnassinmigracion
Revises: m013cadenatraslado
Create Date: 2026-08-20

`flask db check` contra una base construida **solo con migraciones** las
reporta como faltantes:

    usuarios.puede_usar_camara
    tareas_picking.empaques_escaneados

Las dos están en uso:

· `puede_usar_camara` la escriben `routes/auth.py:81` (alta de usuario) y
  `:150` (edición), sale en `Usuario.to_dict()` y la lee `picking.js` para
  decidir si el operario puede escanear con la cámara.
· `empaques_escaneados` la escriben `routes/picking.py:348` y
  `mobile_service.py`. Existe una migración que la agrega —
  `v5w6x7y8z9a0_add_conversion_empaque`— **pero sobre `items_recepcion`**, que
  es otra tabla. El modelo la declara en las dos (`recepcion.py:124` y
  `picking.py:34`) y solo una tenía migración.

## Por qué no se había notado

Producción no se construyó corriendo la cadena desde cero: las columnas
entraron por otra vía —`create_all()` o a mano— y desde entonces el esquema
real y el historial de migraciones divergen sin que nada lo compare. La cadena
completa **nunca se había ejercido** (dos migraciones la rompían; ver `m012` y
las notas en `a8b9c0d1e2f4` y `7d08d3a13326`, corregidas el 2026-08-20), así
que la divergencia no tenía cómo salir a la luz.

Es la misma forma que el resto de esta semana: la falla no daba error, daba
silencio — hasta que alguien intentó levantar una base nueva.

## `IF NOT EXISTS`, a propósito

No sabemos si producción ya las tiene (casi seguro que sí) ni qué otras copias
existen. `ADD COLUMN IF NOT EXISTS` hace que esta migración sea correcta en
los dos mundos: no toca nada donde ya están, y las crea donde faltan.

Los defaults replican el del modelo, y el backfill es explícito: las filas
existentes quedan con el mismo valor que tendrían si la columna hubiera estado
desde el principio. `puede_usar_camara` va a `true` porque el modelo declara
`default=True` y `to_dict()` trata `None` como `True` — dejarlo NULL haría que
la pantalla dijera una cosa y la base otra.
"""
from alembic import op

revision = 'm014columnassinmigracion'
down_revision = 'm013cadenatraslado'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        'ALTER TABLE usuarios '
        'ADD COLUMN IF NOT EXISTS puede_usar_camara BOOLEAN DEFAULT TRUE')
    op.execute(
        'UPDATE usuarios SET puede_usar_camara = TRUE '
        'WHERE puede_usar_camara IS NULL')

    op.execute(
        'ALTER TABLE tareas_picking '
        'ADD COLUMN IF NOT EXISTS empaques_escaneados INTEGER DEFAULT 0')
    op.execute(
        'UPDATE tareas_picking SET empaques_escaneados = 0 '
        'WHERE empaques_escaneados IS NULL')


def downgrade():
    # `IF EXISTS` por simetría: si producción las tenía de antes, este
    # downgrade las quitaría igual — que es lo correcto para revertir esta
    # revisión, pero conviene saber que borra datos que existían antes de ella.
    op.execute('ALTER TABLE tareas_picking DROP COLUMN IF EXISTS empaques_escaneados')
    op.execute('ALTER TABLE usuarios DROP COLUMN IF EXISTS puede_usar_camara')

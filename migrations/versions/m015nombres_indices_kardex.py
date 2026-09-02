"""Los tres índices de kardex se llamaban distinto que en el modelo

Revision ID: m015nombresindiceskardex
Revises: m014columnassinmigracion
Create Date: 2026-08-20

`KardexMovimiento` declara `index=True` en `fecha`, `bodega` y `referencia`, y
SQLAlchemy deriva el nombre de la tabla: `ix_kardex_movimientos_*`. La
migración que los creó los nombró `ix_kardex_*`.

Mismo índice, mismas columnas, mismo efecto — y `flask db check` reportando
seis líneas de deriva para siempre (tres «sobra» y tres «falta»).

## Por qué se limpia si no rompe nada

`db check` es el único mecanismo que compara el esquema real contra los
modelos, y por lo tanto **el único que atrapa una columna declarada en un
modelo que ninguna migración crea** — que es exactamente lo que pasó con
`usuarios.puede_usar_camara` y `tareas_picking.empaques_escaneados`, sin que
nada avisara hasta que se intentó levantar una base nueva (ver `m014`).

Con trece líneas de ruido nadie lo corre, y el próximo faltante se cuela por
el mismo hueco. Limpiar la deriva no arregla un defecto: **deja utilizable el
detector que arregla los siguientes.** Es la lección de los 639 avisos
conocidos, aplicada a un canal distinto.

## `ALTER INDEX ... RENAME`

Instantáneo y no reconstruye: Postgres solo cambia la entrada del catálogo.
No hay ventana sin índice, así que no degrada ninguna consulta mientras corre.

`IF EXISTS` en cada uno porque no sabemos con qué nombre están en cada copia
—producción los tiene con el nombre viejo; una base construida desde cero
después de este cambio ya los tendría con el nuevo—. La migración es correcta
en los dos mundos.
"""
from alembic import op

revision = 'm015nombresindiceskardex'
down_revision = 'm014columnassinmigracion'
branch_labels = None
depends_on = None

_RENOMBRES = [
    ('ix_kardex_fecha', 'ix_kardex_movimientos_fecha'),
    ('ix_kardex_bodega', 'ix_kardex_movimientos_bodega'),
    ('ix_kardex_referencia', 'ix_kardex_movimientos_referencia'),
]


def upgrade():
    for viejo, nuevo in _RENOMBRES:
        op.execute(f'ALTER INDEX IF EXISTS {viejo} RENAME TO {nuevo}')


def downgrade():
    for viejo, nuevo in _RENOMBRES:
        op.execute(f'ALTER INDEX IF EXISTS {nuevo} RENAME TO {viejo}')

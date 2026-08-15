"""El despacho dejaba de suponer que el 174720 entró

Revision ID: m008compromisosrit
Revises: m007retencionesporpuc
Create Date: 2026-08-15

`despachar()` elegía el conector así:

    elif s.siesa_requisicion_consec:
        # Flujo completo: 174646 + 174720 ya disparados — usar 174930

Eso es una **suposición**, no una comprobación: solo mira que exista el
consecutivo de la requisición.

Y el 174930 no manda cantidades — Siesa las toma de lo comprometido en la RIT.
Si el 174720 falló, ahí siguen las del 174646, o sea **lo pedido**, no lo
empacado:

    packing confirma      7 de 10
    174720 falla          → siesa_error = '174720: ...'
    el job se encola      → muere sin handler a las ~6 h
    el despacho sigue     → 174930 con las cantidades ORIGINALES
    Siesa registra        10 en tránsito
    salen físicamente      7
                          ─────
                          3 unidades en el limbo

Y el limbo de traslados es el caso que el propio módulo declara como el peor:
el stock no falta ni sobra, está en la bodega puente, donde nadie pregunta.

## Por qué NO se frena el despacho

El primer instinto es no despachar hasta que los compromisos entren. No hace
falta: **el 173076 sí lleva `cantidad_enviada`**, que es lo real. Cuando el
174720 no entró se cae a esa vía —exactamente lo que el código ya hace cuando
no puede leer el consecutivo de la RIT— y la mercancía sale con los números
correctos. Lo que queda es una RIT suelta, que es una condición conocida, ya
declarada y con su propio reporte.

Frenar habría cambiado el modo de fallo de un flujo que funciona, para evitar
algo que la vía de respaldo ya resuelve.

## El relleno del histórico

Las filas con RIT y **sin** error de 174720 se dan por comprometidas: es la
mejor inferencia disponible y evita crear RITs sueltas en traslados que hoy
están en vuelo y sí registraron sus compromisos.

Ante la duda se deja en `false`, y el costo de equivocarse hacia ese lado es
una RIT suelta con la mercancía correcta — no un STS por cantidades que nadie
empacó. Regla 0.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm008compromisosrit'
down_revision = 'm007retencionesporpuc'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('solicitudes_traslado') as batch:
        batch.add_column(sa.Column(
            'siesa_compromisos_ok', sa.Boolean(),
            server_default='false', nullable=False))
    # `NOT LIKE` con el patrón entre comillas simples: Postgres y SQLite lo
    # aceptan igual. Nada de `= 1` sobre un booleano — eso ya tumbó un deploy
    # (ver m007: SQLite lo acepta, Postgres es un error de tipo, y los tests
    # usan create_all() así que ninguna prueba lo ejercitaba).
    op.execute(
        "UPDATE solicitudes_traslado SET siesa_compromisos_ok = true "
        "WHERE siesa_requisicion_consec IS NOT NULL "
        "AND (siesa_error IS NULL OR siesa_error NOT LIKE '174720:%')"
    )


def downgrade():
    with op.batch_alter_table('solicitudes_traslado') as batch:
        batch.drop_column('siesa_compromisos_ok')

"""geo: donde estuvo el camion, y donde queda la tienda

Revision ID: m020geoentrega
Revises: m019flotaconfianza
Create Date: 2026-09-02

PURAMENTE ADITIVA. Dos tablas nuevas, ningun ALTER, ningun DROP.

## Por que DOS tablas y no una

| | Que afirma | En el acta de corte |
|---|---|---|
| `entregas_geo` | donde estuvo **el camion** el dia que entrego esa factura | `OPERATIVAS` |
| `clientes_geo` | donde queda **la tienda** | `PROTEGIDAS_MAESTRAS` |

El maestro se construye a partir de los eventos: con una sola tabla, la segunda
captura pisaria a la primera y no habria contra que sacar mediana ni dispersion.

Y la separacion **decide sola** una pregunta que con una tabla no tiene buena
respuesta: el evento es registro del ensayo y se vacia en el corte; el maestro es
el activo que tarda tres meses en construirse y no se toca. Con una tabla hay que
elegir, y cualquiera de las dos elecciones esta mal.

## Los CHECK que valen mas de lo que parecen

**La caja de Colombia atrapa dos cosas, no una.** Lo obvio es `0,0` — un punto
real en el Golfo de Guinea que cualquier «no hay dato» convertido a numero
produce. Lo que no es obvio: atrapa **los ejes cambiados**. `lat = -75.28` pasa
cualquier validacion global de latitud (esta entre -90 y 90) y manda al conductor
a Peru con el dato pareciendo sano.

**`ck_*_coordenada_o_motivo` es la regla 4 del modulo puesta en la base**: o hay
par completo con procedencia, o hay motivo escrito de por que no lo hay. Un GPS
negado, apagado o sin senal **no es la coordenada 0,0**, y los tres motivos se
arreglan distinto.

`cliente_clave` es la PK del maestro y no un `id` autoincremental a proposito: el
cliente no tiene fila propia en este WMS todavia — la clave se deriva del nombre
y el municipio que traen los pedidos. El dia que exista un maestro de terceros
con NIT, esto se migra contra el; la deuda esta declarada en
`docs/flota/ESTADO.md` con su condicion de disparo.

Backup de referencia: PITR activo en Railway. Head previo: m019flotaconfianza.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm020geoentrega'
down_revision = 'm019flotaconfianza'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'entregas_geo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recaudo_id', sa.Integer(), nullable=False),
        sa.Column('cliente_clave', sa.String(length=220), nullable=False),
        sa.Column('lat', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('lon', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('precision_m', sa.Numeric(precision=8, scale=1), nullable=True),
        sa.Column('fuente', sa.String(length=20), server_default='sin_dato',
                  nullable=False),
        sa.Column('motivo_sin_dato', sa.String(length=24), nullable=True),
        sa.Column('capturado_en', sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato'"
            "      AND motivo_sin_dato IS NULL) OR (lat IS NULL AND lon IS NULL "
            "AND fuente = 'sin_dato'      AND motivo_sin_dato IS NOT NULL)",
            name='ck_entrega_geo_coordenada_o_motivo'),
        sa.CheckConstraint(
            "fuente IN ('gps_conductor','corregida_a_mano','sin_dato')",
            name='ck_entrega_geo_fuente'),
        sa.CheckConstraint(
            "motivo_sin_dato IS NULL OR motivo_sin_dato IN ('permiso_denegado',"
            "'sin_senal','no_soportado','timeout','no_se_pidio','fuera_de_rango',"
            "'no_declarado')",
            name='ck_entrega_geo_motivo'),
        sa.CheckConstraint('lat IS NULL OR (lat BETWEEN -4.5 AND 13.7)',
                           name='ck_entrega_geo_lat_colombia'),
        sa.CheckConstraint('lon IS NULL OR (lon BETWEEN -82.2 AND -66.5)',
                           name='ck_entrega_geo_lon_colombia'),
        sa.CheckConstraint('precision_m IS NULL OR precision_m > 0',
                           name='ck_entrega_geo_precision_positiva'),
        sa.ForeignKeyConstraint(['recaudo_id'], ['recaudos_entrega.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('recaudo_id'),
    )
    op.create_index(op.f('ix_entregas_geo_cliente_clave'), 'entregas_geo',
                    ['cliente_clave'], unique=False)

    op.create_table(
        'clientes_geo',
        sa.Column('cliente_clave', sa.String(length=220), nullable=False),
        sa.Column('cliente_nombre', sa.String(length=200), nullable=True),
        sa.Column('municipio', sa.String(length=100), nullable=True),
        sa.Column('lat', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('lon', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('precision_m', sa.Numeric(precision=8, scale=1), nullable=True),
        sa.Column('fuente', sa.String(length=20), server_default='sin_dato',
                  nullable=False),
        sa.Column('motivo_sin_maestro', sa.String(length=28), nullable=True),
        sa.Column('capturas_consideradas', sa.Integer(), server_default='0',
                  nullable=False),
        sa.Column('capturas_descartadas', sa.Integer(), server_default='0',
                  nullable=False),
        sa.Column('elegido_en', sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato'"
            "      AND motivo_sin_maestro IS NULL) OR (lat IS NULL AND lon IS NULL "
            "AND fuente = 'sin_dato'      AND motivo_sin_maestro IS NOT NULL)",
            name='ck_cliente_geo_coordenada_o_motivo'),
        sa.CheckConstraint(
            "fuente IN ('gps_conductor','corregida_a_mano','sin_dato')",
            name='ck_cliente_geo_fuente'),
        sa.CheckConstraint(
            "motivo_sin_maestro IS NULL OR motivo_sin_maestro IN ('sin_capturas',"
            "'precision_insuficiente','capturas_dispersas')",
            name='ck_cliente_geo_motivo'),
        sa.CheckConstraint(
            'capturas_consideradas >= 0 AND capturas_descartadas >= 0',
            name='ck_cliente_geo_conteos_no_negativos'),
        sa.CheckConstraint('lat IS NULL OR (lat BETWEEN -4.5 AND 13.7)',
                           name='ck_cliente_geo_lat_colombia'),
        sa.CheckConstraint('lon IS NULL OR (lon BETWEEN -82.2 AND -66.5)',
                           name='ck_cliente_geo_lon_colombia'),
        sa.CheckConstraint('precision_m IS NULL OR precision_m > 0',
                           name='ck_cliente_geo_precision_positiva'),
        sa.PrimaryKeyConstraint('cliente_clave'),
    )


def downgrade():
    # `clientes_geo` primero: no cuelga de nadie, pero el orden inverso al
    # upgrade es la convención de este repo y no cuesta nada respetarla.
    op.drop_table('clientes_geo')
    op.drop_index(op.f('ix_entregas_geo_cliente_clave'), table_name='entregas_geo')
    op.drop_table('entregas_geo')

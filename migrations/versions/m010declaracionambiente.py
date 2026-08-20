"""El ambiente no se detecta: se declara y se contrasta

Revision ID: m010declaracionambiente
Revises: m009fuenteexistencia
Create Date: 2026-08-19

El 19 de agosto de 2026 el Gestor de Cartera pasó **ocho horas mostrando
cartera de QA creyendo que era producción**. Las cuatro comprobaciones
técnicas dieron en verde, y las cuatro las hereda una copia:

    el host decía servicios.siesacloud.com    →  no distingue
    la compañía era la 1, la única del ERP    →  no distingue
    había documentos con fecha de hoy         →  QA también recibe escrituras
    los montos cuadraban entre sí             →  consistencia no es verdad

La causa estaba dos capas por debajo de lo que se puede medir desde la API: el
Módulo de conectividad de Connekta tenía la conexión SQL de **producción**
apuntando a `SUnoEE_Papeleriamed_Imple`. Quien lo desmintió no fue ninguna
medición: fue el dueño del negocio reconociendo sus propias facturas de prueba
en la pantalla.

El WMS comparte esa plataforma, y **escribe**. Un documento creado en la base
equivocada no se corrige cambiando la conexión: queda escrito, hay que
reversarlo uno por uno, y para entonces la mercancía ya salió del CD.

## Qué guarda esta tabla y por qué es la única evidencia que sirve

El contraste de una cifra del WMS contra **algo de afuera**, con el nombre de
quien lo hizo. Es el único dato del sistema que no se puede fabricar desde
adentro.

`huella_config` hace que la declaración **caduque sola**: vale para el host y
la compañía con los que se hizo. Si cambian, el estado vuelve a ALARMA aunque
antes estuviera verde — que es exactamente el escenario del 19 de agosto, donde
alguien podría haber declarado en QA y el corte habría heredado ese verde.

## No hay backfill, y es deliberado

Cero filas al arrancar significa ALARMA, que es la verdad: hoy nadie ha
contrastado nada. Sembrar una declaración inicial sería fabricar la única
evidencia que este mecanismo existe para no fabricar.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm010declaracionambiente'
down_revision = 'm009fuenteexistencia'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'declaraciones_ambiente',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('declarado_por', sa.Integer(), nullable=False),
        sa.Column('declarado_en', sa.DateTime(), nullable=False),
        sa.Column('huella_config', sa.String(length=32), nullable=False),
        sa.Column('host', sa.String(length=200), nullable=False),
        sa.Column('id_compania', sa.String(length=20), nullable=False),
        sa.Column('concepto', sa.String(length=200), nullable=False),
        sa.Column('cifra_wms', sa.String(length=60), nullable=False),
        sa.Column('cifra_externa', sa.String(length=60), nullable=False),
        sa.Column('fuente_externa', sa.String(length=200), nullable=False),
        sa.Column('notas', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['declarado_por'], ['usuarios.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_declaraciones_ambiente_declarado_en',
                    'declaraciones_ambiente', ['declarado_en'])


def downgrade():
    op.drop_index('ix_declaraciones_ambiente_declarado_en',
                  table_name='declaraciones_ambiente')
    op.drop_table('declaraciones_ambiente')

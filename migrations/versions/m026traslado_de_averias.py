"""Un traslado no podía decir que lo que mueve está roto

Revision ID: m026trasladoaverias
Revises: m025averiaenrecepcion
Create Date: 2026-09-17

## El proceso que falta

El dueño lo describió así: «las averías en teoría tienen que ser como un
traslado; como se haga el traslado del punto de venta es que se hace el de
avería, solo que ahí si el jefe de bodega del punto genera la avería el admin
tiene que validar y dejar la evidencia, y en NB1 recepción valida, y antes de
ubicar también el admin o supervisor de NB1 tiene que validarlo».

Cuatro momentos de validación. Dos ya existen y no se tocan:
  1. **Generar** — crear la solicitud. Ya queda firmado por `solicitante_id`
     y `fecha_creacion`.
  3. **Recibir** — contar lo que llegó. Ya es `cantidad_recibida` por ítem,
     con el CHECK `ck_traslado_cadena_no_crece` impidiendo que crezca.

Los otros dos no tenían dónde vivir:
  2. **El admin del punto valida y deja evidencia** → `averia_evidencia`
     (la firma de quién y cuándo ya la da `aprobador_id`/`fecha_aprobacion`:
     en un traslado de averías, aprobar la solicitud ES ese acto).
  4. **El admin/supervisor de NB1 dictamina antes de ubicar** → el bloque
     `averia_veredicto*`.

## Por qué el veredicto es un Boolean NULLABLE y no un estado nuevo

Un estado después de `ENTREGADA` habría sido lo primero que uno escribe, y
está mal por dos razones medibles:

  · Cegaría TRA-10 y TRA-11, que filtran por `ENTREGADA` para detectar
    traslados sin documento de cierre en Siesa
    (`app/services/auditoria/traslados.py:117,140`).
  · No tendría reloj. `fecha_entrega` ya está puesta, así que nada distinguiría
    «dictaminado hace un minuto» de «lleva tres semanas sin que nadie mire».

El tri-estado sí distingue los tres desenlaces y cada uno tiene consecuencia
propia: `None` = nadie miró (la mercancía está en el limbo, y eso es una alerta
con su propio reloj en `averia_veredicto_at`), `True` = a la zona de averías y
sale el documento a AV1, `False` = vuelve al inventario vendible y NO sale
ningún documento. El precedente exacto es
`RecaudoEntrega.retencion_confirmada` (`m010retencionconfirmada.py:16`), donde
«no sé» también bloquea distinto que «no».

## Por qué el ítem NO lleva `cantidad_averiada`

En un traslado de averías la mercancía averiada es el documento entero: la
cantidad la dice `cantidad_solicitada` y la cadena la recorta paso a paso.
Una segunda columna con el mismo número sería una segunda fuente de verdad.
En `ItemRecepcion` sí existe esa columna porque allá la avería es un
subconjunto de lo recibido — dos problemas distintos, dos formas distintas.

## Lo que NO cambia en Siesa

Nada. Un traslado de averías viaja por los mismos conectores que cualquier
otro: STS 173076 al despachar, ETS 173079 al recibir. Para el ERP es un
traslado entre bodegas. El segundo tramo (NB1 → AV1) es el documento clase 67
que ya existe y ya funciona (`encolar_traslado_averias`). La clase que se
agrega acá decide qué pasa **en el WMS al llegar**, no en el ERP.

## Los dos CHECK

`ck_traslado_clase_conocida` — una clase desconocida no daría error: se
comportaría como NORMAL en todos los `if`, y un traslado de averías se
ubicaría como mercancía vendible.

`ck_traslado_veredicto_solo_en_averias` — un traslado NORMAL con veredicto
escrito empezaría a comportarse como uno de averías y el stock vendible
desaparecería del pool sin que nadie lo pidiera. La dirección contraria (un
AVERIAS sin veredicto) es válida: es el estado inicial.

SQLite no soporta agregar un CHECK con ALTER; en ese motor las columnas se
agregan y los CHECK los aplica `create_all` al armar el esquema de tests — por
eso los dos están TAMBIÉN en `__table_args__` del modelo, que es el único
camino por el que los tests los ejercitan.

## El backfill

Ninguno hace falta. Las 174 solicitudes que existen hoy son todas NORMAL —
hasta hoy no había otra cosa— y `server_default='TRASLADO_NORMAL'` las cubre. Los cinco
campos de avería quedan NULL, que es lo correcto: nadie los dictaminó.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm026trasladoaverias'
down_revision = 'm025averiaenrecepcion'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table('solicitudes_traslado') as batch:
        batch.add_column(sa.Column('clase_traslado', sa.String(length=20),
                                   nullable=False, server_default='TRASLADO_NORMAL'))
        batch.add_column(sa.Column('averia_evidencia', sa.Text(), nullable=True))
        batch.add_column(sa.Column('averia_veredicto', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('averia_veredicto_por', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('averia_veredicto_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('averia_veredicto_nota', sa.String(length=200),
                                   nullable=True))

    with op.batch_alter_table('items_solicitud_traslado') as batch:
        batch.add_column(sa.Column('motivo_averia', sa.String(length=200),
                                   nullable=True))

    if bind.dialect.name == 'postgresql':
        # La FK va aparte del `add_column`: `batch_alter_table` sobre Postgres
        # emite ALTER planos y una FK inline en el mismo batch no se nombra.
        # Un nombre explícito es lo que permite dropearla en el downgrade.
        op.create_foreign_key(
            'fk_traslado_averia_veredicto_por_usuario',
            'solicitudes_traslado', 'usuarios',
            ['averia_veredicto_por'], ['id'])
        op.create_check_constraint(
            'ck_traslado_clase_conocida', 'solicitudes_traslado',
            "clase_traslado IN ('TRASLADO_NORMAL', 'TRASLADO_AVERIAS')")
        op.create_check_constraint(
            'ck_traslado_veredicto_solo_en_averias', 'solicitudes_traslado',
            "clase_traslado = 'TRASLADO_AVERIAS' OR ("
            " averia_veredicto IS NULL AND"
            " averia_evidencia IS NULL AND"
            " averia_veredicto_por IS NULL AND"
            " averia_veredicto_at IS NULL AND"
            " averia_veredicto_nota IS NULL)")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.drop_constraint('ck_traslado_veredicto_solo_en_averias',
                           'solicitudes_traslado', type_='check')
        op.drop_constraint('ck_traslado_clase_conocida',
                           'solicitudes_traslado', type_='check')
        op.drop_constraint('fk_traslado_averia_veredicto_por_usuario',
                           'solicitudes_traslado', type_='foreignkey')

    with op.batch_alter_table('items_solicitud_traslado') as batch:
        batch.drop_column('motivo_averia')

    with op.batch_alter_table('solicitudes_traslado') as batch:
        batch.drop_column('averia_veredicto_nota')
        batch.drop_column('averia_veredicto_at')
        batch.drop_column('averia_veredicto_por')
        batch.drop_column('averia_veredicto')
        batch.drop_column('averia_evidencia')
        batch.drop_column('clase_traslado')

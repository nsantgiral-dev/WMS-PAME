"""TRA-01 deja de ser detective: la cadena del traslado no puede crecer

Revision ID: m013cadenatraslado
Revises: m012unicidadoperativa
Create Date: 2026-08-19

`solicitada ≥ aprobada ≥ enviada ≥ recibida`. Cada paso puede recortar;
ninguno puede inventar. Una desigualdad al revés es mercancía que apareció de
la nada entre dos etapas.

Estaba declarado como invariante `BLOQUEA` en
`app/services/auditoria/traslados.py`, y era **detective**: solo aparecía si
alguien abría el panel de auditoría. Para entonces `cantidad_recibida` ya
había viajado en el payload del ETS 173079 (`traslado_service.py:933`), o sea
que Siesa ya había metido en la bodega destino más unidades de las que
salieron del origen.

Las cuatro columnas viven en **la misma fila**, así que el CHECK es trivial y
es preventivo. Ni `confirmar_recepcion` ni `aprobar_solicitud` comparaban
contra el eslabón anterior antes de escribir.

## Los NULL pasan, y no es laxitud

`cantidad_aprobada` es NULL hasta que el admin aprueba; `cantidad_enviada` y
`cantidad_recibida` lo son hasta su etapa. Un CHECK que los rechazara
impediría **crear una solicitud**. Cada comparación se activa solo cuando sus
dos lados existen.

## Y recortar sigue siendo legítimo

Es lo que más importa de este constraint: se aprueba menos de lo pedido, se
envía menos de lo aprobado (picking parcial) y se recibe menos de lo enviado
(faltante). Un CHECK de igualdad habría prohibido la operación real. Ver
`tests/test_bloqueo_cadena_traslado.py::test_recortar_en_cada_paso_es_legitimo`.

## Si hay filas históricas que ya lo violan

`ADD CONSTRAINT` falla sobre datos sucios. Se comprueba antes y se nombran las
solicitudes concretas: son las que TRA-01 venía reportando. **No se corrigen
solas** — cuál cantidad es la buena lo dice el conteo físico, no un despeje.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm013cadenatraslado'
down_revision = 'm012unicidadoperativa'
branch_labels = None
depends_on = None

_NOMBRE = 'ck_traslado_cadena_no_crece'
_CONDICION = (
    '(cantidad_aprobada IS NULL OR cantidad_aprobada <= cantidad_solicitada) AND '
    '(cantidad_enviada IS NULL OR cantidad_aprobada IS NULL OR '
    ' cantidad_enviada <= cantidad_aprobada) AND '
    '(cantidad_recibida IS NULL OR cantidad_enviada IS NULL OR '
    ' cantidad_recibida <= cantidad_enviada)'
)


def upgrade():
    conn = op.get_bind()
    sucias = conn.execute(sa.text(f"""
        SELECT s.codigo, i.producto_codigo_siesa,
               i.cantidad_solicitada, i.cantidad_aprobada,
               i.cantidad_enviada, i.cantidad_recibida
          FROM items_solicitud_traslado i
          JOIN solicitudes_traslado s ON s.id = i.solicitud_id
         WHERE NOT ({_CONDICION})
         LIMIT 30
    """)).fetchall()

    if sucias:
        detalle = '\n'.join(
            f'  · {f[0]} / {f[1]}: solicitada={f[2]} aprobada={f[3]} '
            f'enviada={f[4]} recibida={f[5]}' for f in sucias)
        raise RuntimeError(
            'No se puede crear el CHECK: hay ítems de traslado cuya cadena '
            'crece entre etapas.\n\n' + detalle + '\n\n'
            'Son los que TRA-01 venía reportando. Corregilos antes de migrar: '
            'la cantidad buena la dice el conteo físico de la bodega destino, '
            'no un despeje aritmético. Si la mercancía sobrante existe de '
            'verdad, entró por otra vía y hay que registrarla como tal.')

    op.create_check_constraint(_NOMBRE, 'items_solicitud_traslado',
                               sa.text(_CONDICION))


def downgrade():
    op.drop_constraint(_NOMBRE, 'items_solicitud_traslado', type_='check')

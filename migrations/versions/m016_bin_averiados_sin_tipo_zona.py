"""El bin AVERIADOS estaba marcado como zona vendible y se despachaba a clientes

Revision ID: m016binaveriadossintipozona
Revises: m015nombresindiceskardex
Create Date: 2026-08-20

`devolucion_cliente_service._resolver_ubicacion(..., es_averiado=True)` —el
escritor VIVO; `devolucion_service` está DEPRECATED desde el 2026-07-28 y no
tiene un solo caller de producción— crea el bin
`AVERIADOS` —donde el recepcionista deja lo que llega roto— con
`zona='CUARENTENA'` y `tipo='cuarentena'`, y **sin `tipo_zona`**. El modelo
tiene default `'GENERAL'` (`app/models/ubicacion.py:37`), así que cada uno de
esos bins quedó registrado como zona vendible.

Las dos consultas que preguntan «¿esto se puede vender?» miran `tipo_zona` y
solo `tipo_zona`:

  · `picking_service.calcular_fefo` — excluye AVERIAS del surtido de pedidos
  · `dashboard_service.consulta_productos_bajo_minimo` — no cuenta AVERIAS
    como cobertura del mínimo

Con `tipo_zona='GENERAL'`, ninguna de las dos ve el bin. Lo que costaba, en las
dos direcciones a la vez: la mercancía averiada **entra al FEFO** (y como ese
stock no se mueve, su `fecha_ingreso` es la más vieja del producto, así que
suele ganar el desempate) y sale hacia un cliente; y esas mismas unidades
**tapan el mínimo** del tablero, así que la reposición de lo que sí falta nunca
se dispara. Dos veredictos opuestos alimentados por la misma fila mal marcada.

El código ya quedó arreglado (la política única vive en `picking_service`, y el
escritor la usa). Esta migración es por las filas que YA existen: los bins
creados antes del arreglo siguen siendo asignados a pedidos de clientes hasta
que alguien les cambie el campo.

## El `WHERE`, y por qué es estrecho a propósito

    codigo = 'AVERIADOS'
    AND tipo_zona <> 'AVERIAS'
    AND (tipo = 'cuarentena' OR zona = 'CUARENTENA')

Tres condiciones para una sola fila por almacén, porque **el error caro está en
la dirección contraria**: marcar de más saca una ubicación del FEFO y esconde
stock vendible sin que nada avise —un agotado inventado, que para la canasta
constitucional es carísimo (Regla 0, el caso de Florencia)—. Dejar sin marcar
un bin raro, en cambio, lo vuelve a corregir el propio servicio la próxima vez
que alguien confirme una avería ahí.

  · `codigo = 'AVERIADOS'` — el literal exacto y único que escriben los dos
    servicios de devolución (`_UBICACION_AVERIADOS`). No se toca ningún `AVE<n>`
    (esos los crea `layout_service` ya bien marcados) ni nada que solo *parezca*
    de averías por el nombre: sin `LIKE`, sin prefijos.
  · `tipo = 'cuarentena' OR zona = 'CUARENTENA'` — las marcas que el escritor sí
    ponía. Si alguien creó a mano una estantería normal llamada AVERIADOS
    (`tipo='estanteria'`, `zona='GENERAL'`), esta migración NO la toca: no hay
    evidencia de que sea un bin de averías, y esconder su stock sería el error
    caro.
  · `tipo_zona <> 'AVERIAS'` — hace la sentencia idempotente: correrla otra vez
    no actualiza ninguna fila. No sabemos qué copias de la base existen ni cuál
    ya pasó por acá (el ambiente no se detecta solo).

## Idempotencia y tolerancia a bases distintas

La tabla y las tres columnas se verifican con el inspector antes de ejecutar:
si falta alguna, la migración no hace nada en vez de reventar. Una base recién
creada después de este arreglo simplemente no tiene filas que corregir.

## Downgrade: no-op deliberado

Volver `tipo_zona` a `'GENERAL'` no sería un rollback: sería reponer el defecto
—mercancía dañada disponible para pedidos de cliente— sobre datos que ya no
distinguen cuál fila estaba mal antes. Bajar de revisión deja los bins bien
marcados, que es el estado correcto en las dos versiones del código.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm016binaveriadossintipozona'
down_revision = 'm015nombresindiceskardex'
branch_labels = None
depends_on = None


# Expuesto a nivel de módulo a propósito: el trinquete
# (`tests/test_politica_vendible_unica.py`) ejercita ESTA sentencia contra una
# base real, en las dos direcciones —corrige el bin de devoluciones, no toca la
# estantería homónima—. Un `WHERE` que solo se lee no está verificado.
SQL_BACKFILL = """
UPDATE ubicaciones
   SET tipo_zona = 'AVERIAS'
 WHERE codigo = 'AVERIADOS'
   AND tipo_zona <> 'AVERIAS'
   AND (tipo = 'cuarentena' OR zona = 'CUARENTENA')
"""

_COLUMNAS_NECESARIAS = {'codigo', 'tipo_zona', 'tipo', 'zona'}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'ubicaciones' not in inspector.get_table_names():
        return
    columnas = {c['name'] for c in inspector.get_columns('ubicaciones')}
    if not _COLUMNAS_NECESARIAS.issubset(columnas):
        return

    resultado = bind.execute(sa.text(SQL_BACKFILL))
    corregidas = resultado.rowcount if resultado.rowcount is not None else -1
    print(f'[m016] Bins AVERIADOS corregidos a tipo_zona=AVERIAS: {corregidas}')


def downgrade():
    # Ver el docstring: deshacerlo sería reponer el defecto, no revertir un cambio.
    pass

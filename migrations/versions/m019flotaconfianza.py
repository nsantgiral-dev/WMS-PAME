"""flota: el tercer estado del odometro — confianza

Revision ID: m019flotaconfianza
Revises: m018flotainspecciongasto
Create Date: 2026-09-02

ADITIVA sobre `flota_lectura_odometro`: cuatro columnas, cuatro CHECK, dos
triggers nuevos y uno REEMPLAZADO. Ninguna fila se borra; las 26 existentes se
rellenan con la misma regla que usa el codigo, no con un valor inventado.

## Por que existe

`confianza_al_nacer` y `confianza_del_tramo` vivian en `flota/dominio/odometro.py`
**sin columna donde guardar el resultado**: la politica decidia algo que nadie
persistia. Y el CPK de la fase 1 marcaba todo `declarada` porque no tenia de
donde leer. Regla 4 del modulo: un estado que puede ser «no se» se modela con
palabras.

## El orden importa, y no es cosmetico

1. Las columnas nacen **nullable**: `confianza` no puede nacer NOT NULL sobre 26
   filas que no la tienen.
2. **Se cae el trigger append-only ANTES del backfill.** Bloquea todo `UPDATE`
   sobre esta tabla desde la tanda 1 — con el trigger en pie, el `UPDATE` del
   punto 3 aborta la migracion entera. Se reemplaza igual en el paso 6, asi que
   tirarlo antes no es un apano: es el orden natural.
3. Backfill. **Solo la regla 1** de `confianza_al_nacer` (sin foto → dudosa),
   porque «no tiene foto» es un hecho de la fila. Las reglas 2 y 3 (Δt=0 y salto
   ×10) NO se aplican hacia atras: marcar hoy con una regla lo que nacio sin
   ella cambia el significado del historico sin que nadie lo haya mirado.
   Medido contra produccion el 2026-09-01: **26 de 26 sin foto**, el mismo
   numero que publica `medicion.lecturas_sin_foto`.
4. Recien ahi `SET NOT NULL`. **Sin default**: un default convierte «no se
   calculo» en «declarada», que es el default optimista que la regla 1 del
   modulo prohibe.

## El trigger que se reemplaza, y por que no es aflojarlo

`flota_odometro_no_update` bloqueaba **todo** UPDATE. Su motivo es **el numero**:
una lectura no se edita, se corrige con un registro nuevo. Lo que la verificacion
humana escribe no es el numero — es que alguien lo miro contra la foto.

El reemplazo es **mas estricto** en algo que el original no decia: la confianza
solo puede ir HACIA `verificada`, y de ahi no se sale. Las ocho columnas del
hecho siguen congeladas.

Se descarto una tabla aparte de verificaciones: dejaria la confianza repartida
entre dos tablas, y `confianza_del_tramo` recibe UN campo — el dia que alguien
consultara la lectura sin el JOIN publicaria un CPK sobre una verificacion que
no vio.

`IS NOT DISTINCT FROM` y no `=`: con `=`, un `motivo_correccion` NULL da NULL en
vez de verdadero y el trigger abortaria la verificacion legitima.

## El trigger del ancla — SOLO la mitad segura

`NEW.km_inicial < OLD.km_inicial` no es decoracion. Sin esa mitad, la condicion
seria «el ancla quedo por debajo del maximo», y **eso ya es cierto hoy para
cuatro de las seis fichas** (la del THP696 dice 433.434 y su primera lectura es
55.349, verificado 2026-09-01). Cualquier edicion de esas fichas —el aceite, la
medida de llanta— quedaria bloqueada aunque nadie tocara el kilometraje: trabar
el vehiculo por una segunda via, que es lo que esta tanda decidio NO hacer.

Con las dos mitades se impide el **gesto** (bajar el ancla), no el estado
heredado. El piso duro `MAX(historico, km_inicial)` sigue sin implementarse; su
condicion de disparo esta en `docs/flota/ESTADO.md`.

Backup de referencia: PITR activo en Railway. Head previo: m018flotainspecciongasto.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm019flotaconfianza'
down_revision = 'm018flotainspecciongasto'
branch_labels = None
depends_on = None

_MOTIVO_SIN_FOTO = ('sin foto del tablero: el número no se puede cotejar '
                    'contra el vehículo')

_TRIGGERS_PG = """
DROP TRIGGER IF EXISTS flota_odometro_no_update ON flota_lectura_odometro;

CREATE OR REPLACE FUNCTION flota_odometro_solo_verificacion() RETURNS trigger AS $$
BEGIN
  IF NEW.id IS NOT DISTINCT FROM OLD.id
     AND NEW.vehiculo_id IS NOT DISTINCT FROM OLD.vehiculo_id
     AND NEW.valor_km IS NOT DISTINCT FROM OLD.valor_km
     AND NEW.ts IS NOT DISTINCT FROM OLD.ts
     AND NEW.origen IS NOT DISTINCT FROM OLD.origen
     AND NEW.foto_id IS NOT DISTINCT FROM OLD.foto_id
     AND NEW.autor_usuario_id IS NOT DISTINCT FROM OLD.autor_usuario_id
     AND NEW.motivo_correccion IS NOT DISTINCT FROM OLD.motivo_correccion
     AND NEW.motivo_dudosa IS NOT DISTINCT FROM OLD.motivo_dudosa
     AND OLD.confianza <> 'verificada' AND NEW.confianza = 'verificada' THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'flota: lectura_odometro es append-only — se corrige con un registro nuevo';
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_no_update BEFORE UPDATE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_solo_verificacion();

CREATE OR REPLACE FUNCTION flota_odometro_nace_no_verificada() RETURNS trigger AS $$
BEGIN
  IF NEW.confianza = 'verificada' THEN
    RAISE EXCEPTION 'flota: ninguna lectura nace verificada — esa marca la escribe una persona por la cola de verificacion';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS flota_odometro_nace_no_verificada ON flota_lectura_odometro;
CREATE TRIGGER flota_odometro_nace_no_verificada BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_nace_no_verificada();

CREATE OR REPLACE FUNCTION flota_ficha_ancla_no_baja() RETURNS trigger AS $$
BEGIN
  IF NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (
        SELECT COALESCE(MAX(l.valor_km), NEW.km_inicial)
          FROM flota_lectura_odometro l
         WHERE l.vehiculo_id = NEW.vehiculo_id) THEN
    RAISE EXCEPTION 'flota: km_inicial no puede bajar por debajo del maximo ya registrado en flota_lectura_odometro para ese vehiculo';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS flota_ficha_ancla_no_baja ON flota_ficha_tecnica;
CREATE TRIGGER flota_ficha_ancla_no_baja BEFORE UPDATE ON flota_ficha_tecnica
FOR EACH ROW EXECUTE FUNCTION flota_ficha_ancla_no_baja();
"""


def upgrade():
    es_pg = op.get_bind().dialect.name == 'postgresql'

    # ── 1 · Las columnas, nullable ────────────────────────────────────────
    op.execute('ALTER TABLE flota_lectura_odometro '
               'ADD COLUMN IF NOT EXISTS confianza VARCHAR(20)')
    op.execute('ALTER TABLE flota_lectura_odometro '
               'ADD COLUMN IF NOT EXISTS motivo_dudosa TEXT')
    op.execute('ALTER TABLE flota_lectura_odometro ADD COLUMN IF NOT EXISTS '
               'verificada_por_usuario_id INTEGER REFERENCES usuarios(id)')
    op.execute('ALTER TABLE flota_lectura_odometro '
               'ADD COLUMN IF NOT EXISTS verificada_ts TIMESTAMP')

    # ── 2 · Se cae el append-only ANTES del backfill ─────────────────────
    # Con el trigger en pie, el UPDATE de abajo aborta la migración entera. Se
    # reemplaza igual en el paso 5; tirarlo acá no es un apaño, es el orden.
    if es_pg:
        op.execute('DROP TRIGGER IF EXISTS flota_odometro_no_update '
                   'ON flota_lectura_odometro')

    # ── 3 · Backfill con la MISMA regla que usa el código ────────────────
    op.execute(sa.text(
        "UPDATE flota_lectura_odometro SET confianza = 'dudosa', "
        "motivo_dudosa = :m WHERE foto_id IS NULL"
    ).bindparams(m=_MOTIVO_SIN_FOTO))
    op.execute("UPDATE flota_lectura_odometro SET confianza = 'declarada' "
               "WHERE foto_id IS NOT NULL")

    # ── 4 · Recién ahora NOT NULL, y sin default ─────────────────────────
    # Un default convierte «no se calculó» en «declarada»: el default optimista
    # que la regla 1 del módulo prohíbe.
    if es_pg:
        op.execute('ALTER TABLE flota_lectura_odometro '
                   'ALTER COLUMN confianza SET NOT NULL')

        op.create_check_constraint(
            'ck_flota_confianza', 'flota_lectura_odometro',
            "confianza IN ('declarada', 'dudosa', 'verificada')")
        op.create_check_constraint(
            'ck_flota_dudosa_con_motivo', 'flota_lectura_odometro',
            "confianza <> 'dudosa' OR (motivo_dudosa IS NOT NULL "
            "AND length(trim(motivo_dudosa)) > 0)")
        op.create_check_constraint(
            'ck_flota_declarada_sin_motivo', 'flota_lectura_odometro',
            "confianza <> 'declarada' OR motivo_dudosa IS NULL")
        # Quién verificó y cuándo, o ninguno de los dos. Una marca `verificada`
        # sin autor es una firma en blanco.
        op.create_check_constraint(
            'ck_flota_verificada_con_autor', 'flota_lectura_odometro',
            "(confianza = 'verificada' AND verificada_por_usuario_id IS NOT NULL "
            " AND verificada_ts IS NOT NULL) OR "
            "(confianza <> 'verificada' AND verificada_por_usuario_id IS NULL "
            " AND verificada_ts IS NULL)")

        # ── 5 · Los triggers ─────────────────────────────────────────────
        for sentencia in [s.strip() for s in _TRIGGERS_PG.split(';\n\n') if s.strip()]:
            op.execute(sentencia)


def downgrade():
    es_pg = op.get_bind().dialect.name == 'postgresql'
    if es_pg:
        op.execute('DROP TRIGGER IF EXISTS flota_ficha_ancla_no_baja '
                   'ON flota_ficha_tecnica')
        op.execute('DROP FUNCTION IF EXISTS flota_ficha_ancla_no_baja()')
        op.execute('DROP TRIGGER IF EXISTS flota_odometro_nace_no_verificada '
                   'ON flota_lectura_odometro')
        op.execute('DROP FUNCTION IF EXISTS flota_odometro_nace_no_verificada()')
        op.execute('DROP TRIGGER IF EXISTS flota_odometro_no_update '
                   'ON flota_lectura_odometro')
        op.execute('DROP FUNCTION IF EXISTS flota_odometro_solo_verificacion()')

        for nombre in ('ck_flota_verificada_con_autor',
                       'ck_flota_declarada_sin_motivo',
                       'ck_flota_dudosa_con_motivo', 'ck_flota_confianza'):
            op.drop_constraint(nombre, 'flota_lectura_odometro', type_='check')

        # Se restaura el append-only ORIGINAL: sin las columnas de verificación,
        # el trigger permisivo no tiene sobre qué decidir. `flota_odometro_append_only()`
        # nunca se borró — la sigue usando el trigger de DELETE.
        op.execute('CREATE TRIGGER flota_odometro_no_update '
                   'BEFORE UPDATE ON flota_lectura_odometro '
                   'FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only()')

    for col in ('verificada_ts', 'verificada_por_usuario_id', 'motivo_dudosa',
                'confianza'):
        op.execute(f'ALTER TABLE flota_lectura_odometro DROP COLUMN IF EXISTS {col}')

"""flota: dos invariantes que vivian en los modelos y no en produccion

Revision ID: m024triggersfaltantes
Revises: m023flotallantas
Create Date: 2026-09-02

Ningun cambio de esquema. Dos triggers de PostgreSQL: uno se REDEFINE y otro
nace. Los dos existian en `flota/adaptadores/modelos.py` —o sea, en la base que
construyen los tests con `create_all()`— y **no en ninguna migracion**, o sea,
no en la base que corre el negocio.

Los encontro la auditoria adversarial del 2026-09-02 comparando `create_all()`
contra `flask db upgrade` desde cero sobre PostgreSQL: 221 columnas, 102 CHECK,
10 UNIQUE, 52 FK y 55 indices coincidian. **Triggers: 7 contra 6.**

El guard que deberia haberlo visto —`tests/test_deriva_esquema.py`— compara
**solo columnas, y por texto**. No sabe que existen los triggers. Esa deuda
queda declarada en `docs/flota/ESTADO.md`.

## 1 · `flota_odometro_monotonia` — la ventana de correccion

El 2026-09-01 se cambio la semantica: una lectura con `origen='correccion'`
**supersede** a las anteriores, y a partir de ahi el techo se calcula solo
sobre las lecturas posteriores a la ultima correccion. Se escribio en el
modelo; **la migracion nunca se escribio**.

Consecuencia medida en vivo: en produccion sigue el trigger viejo, que compara
contra el maximo historico sin ventana. O sea que **la via de escape documentada
no desbloquea nada**: el THP696 —16.697.948 km de un salto— se traba, el mensaje
de error le ofrece al conductor registrar una correccion, y la correccion no le
sirve. Un sistema que ofrece una salida que no existe es peor que uno que no
ofrece ninguna.

`CREATE OR REPLACE FUNCTION` basta: el trigger ya apunta a esta funcion y no
hay que recrearlo.

## 2 · `flota_montaje_posicion_de_la_ficha` — nace

`m023flotallantas` no tiene un solo `CREATE TRIGGER`. El invariante «la posicion
tiene que existir en ESE vehiculo» vive en el modelo y en SQLite; en el motor
migrado, montar una llanta en la posicion 9 de un furgon de 4 **se aceptaba**.

No lo puede hacer un CHECK: mira otra tabla (`flota_ficha_tecnica`). Es la misma
razon por la que el no-solape de custodia y la monotonia del odometro son
triggers y no constraints.

`COALESCE(..., 0)` y no `COALESCE(..., MAX_POSICIONES_LLANTA)`: un vehiculo sin
ficha no tiene posiciones declaradas, y el lado conservador (regla 0) es no
dejar montar nada hasta que alguien levante la ficha — no suponerle doce.

Backup de referencia: PITR activo en Railway. Head previo: m023flotallantas.
"""
from alembic import op

revision = 'm024triggersfaltantes'
down_revision = 'm023flotallantas'
branch_labels = None
depends_on = None

_MSG_MONOTONIA = ('flota: el odometro no puede decrecer sin origen=correccion')
_MSG_POSICION = ('flota: la posicion no existe en la ficha de ese vehiculo')

_ARRIBA = f"""
CREATE OR REPLACE FUNCTION flota_odometro_monotonia() RETURNS trigger AS $$
BEGIN
  IF NEW.origen <> 'correccion' AND EXISTS (
      SELECT 1 FROM flota_lectura_odometro l
      WHERE l.vehiculo_id = NEW.vehiculo_id AND l.valor_km > NEW.valor_km
        AND l.ts >= COALESCE(
            (SELECT MAX(c.ts) FROM flota_lectura_odometro c
              WHERE c.vehiculo_id = NEW.vehiculo_id
                AND c.origen = 'correccion'),
            l.ts)) THEN
    RAISE EXCEPTION '{_MSG_MONOTONIA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION flota_montaje_posicion_de_la_ficha() RETURNS trigger AS $$
BEGIN
  IF NEW.posicion > COALESCE(
        (SELECT f.posiciones_llanta FROM flota_ficha_tecnica f
          WHERE f.vehiculo_id = NEW.vehiculo_id), 0) THEN
    RAISE EXCEPTION '{_MSG_POSICION}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS flota_montaje_posicion_de_la_ficha ON flota_montaje_llanta;

CREATE TRIGGER flota_montaje_posicion_de_la_ficha BEFORE INSERT ON flota_montaje_llanta
FOR EACH ROW EXECUTE FUNCTION flota_montaje_posicion_de_la_ficha();
"""

#: El trigger de monotonia SIN la ventana — el que estaba en produccion hasta
#: hoy. El `downgrade` lo restaura para que revertir devuelva el estado real,
#: no uno intermedio que nunca existio.
_ABAJO = f"""
CREATE OR REPLACE FUNCTION flota_odometro_monotonia() RETURNS trigger AS $$
BEGIN
  IF NEW.origen <> 'correccion' AND EXISTS (
      SELECT 1 FROM flota_lectura_odometro
      WHERE vehiculo_id = NEW.vehiculo_id AND valor_km > NEW.valor_km) THEN
    RAISE EXCEPTION '{_MSG_MONOTONIA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS flota_montaje_posicion_de_la_ficha ON flota_montaje_llanta;
DROP FUNCTION IF EXISTS flota_montaje_posicion_de_la_ficha();
"""


def _ejecutar(bloque):
    # SQLite no tiene plpgsql, y alla los triggers los rehace `create_all()`
    # desde el modelo — que es como corren los tests. Mismo criterio que
    # `f10ta1cimientos`.
    if op.get_bind().dialect.name != 'postgresql':
        return
    for sentencia in [s.strip() for s in bloque.split(';\n\n') if s.strip()]:
        op.execute(sentencia)


def upgrade():
    _ejecutar(_ARRIBA)


def downgrade():
    _ejecutar(_ABAJO)

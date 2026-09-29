"""Flota — la cola del conductor no pierde nada, y el odómetro no se envenena (T2 y T3, 2026-09-27)

Revision ID: m051flotakm
Revises: m051flotalegal
Create Date: 2026-09-27

Aditiva, sin backfill.

· **`flota_rechazo_cola`** — lo que la cola del conductor mandó y el servidor
  rechazó (400/403/404/409). Hasta hoy un rechazo borraba la operación del
  teléfono con sus fotos y el servidor no guardaba nada: un tanqueo pagado o
  una inspección hecha desaparecían sin que control de flota se enterara. Una
  fila por (usuario, clave de reenvío); la anota `@idempotente` en su propia
  transacción, y pasa a `resuelto` cuando la misma clave entra.

· **`flota_lectura_odometro.anula_lectura_id` y `.serie`** — una corrección
  ANULA una lectura puntual (antes dejaba atrás toda la historia anterior), y
  cada lectura dice su lugar en la serie: `cuenta`, `salto` (×10 o km en cero
  tiempo: no sube el tope ni mueve el odómetro hasta que alguien lo verifique)
  o `tardia` (un gasto que llegó después de lecturas con más km: se guarda en
  duda y no mueve el odómetro). NULL = anterior a la marca: cuenta como antes.
  **Sin backfill**: los saltos viejos siguen contando (la ventana de las
  correcciones viejas se conserva).
· **Trigger de monotonía** — el tope son las lecturas que cuentan; una tardía
  no se valida (la pide el adaptador y el `before_insert` comprueba que de
  verdad es menor). Y el de «solo verificar» protege también las dos columnas
  nuevas.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051flotakm'
down_revision = 'm051flotalegal'
branch_labels = None
depends_on = None

_OPERACIONES = ('traspaso', 'hallazgo', 'inspeccion', 'tanqueo')
_ESTADOS = ('abierto', 'resuelto', 'descartado', 'cerrado')


def _en(columna, valores):
    return '%s IN (%s)' % (columna, ', '.join(f"'{v}'" for v in valores))


_MSG_MONOTONIA = 'flota: el odometro no puede decrecer sin origen=correccion'
_MSG_APPEND_ONLY = 'flota: lectura_odometro es append-only — se corrige con un registro nuevo'
_CUENTA_SQL = ("(COALESCE(l.serie, 'cuenta') = 'cuenta' OR "
               "(l.serie IN ('salto', 'contradice') AND l.confianza = 'verificada'))")

_INMUTABLES_VIEJAS = ('id', 'vehiculo_id', 'valor_km', 'ts', 'origen', 'foto_id',
                      'autor_usuario_id', 'motivo_correccion', 'motivo_dudosa')
_INMUTABLES_NUEVAS = _INMUTABLES_VIEJAS + ('anula_lectura_id', 'serie')


def _solo_verificacion(columnas):
    iguales = '\n     AND '.join(f'NEW.{c} IS NOT DISTINCT FROM OLD.{c}' for c in columnas)
    return f"""
CREATE OR REPLACE FUNCTION flota_odometro_solo_verificacion() RETURNS trigger AS $$
BEGIN
  IF {iguales}
     AND OLD.confianza <> 'verificada' AND NEW.confianza = 'verificada' THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '{_MSG_APPEND_ONLY}';
END; $$ LANGUAGE plpgsql"""


_MONOTONIA_NUEVA = f"""
CREATE OR REPLACE FUNCTION flota_odometro_monotonia() RETURNS trigger AS $$
BEGIN
  IF NEW.origen <> 'correccion' AND COALESCE(NEW.serie, 'cuenta') NOT IN ('tardia', 'contradice')
     AND EXISTS (
      SELECT 1 FROM flota_lectura_odometro l
      WHERE l.vehiculo_id = NEW.vehiculo_id AND l.valor_km > NEW.valor_km
        AND {_CUENTA_SQL}
        AND NOT EXISTS (SELECT 1 FROM flota_lectura_odometro a
                         WHERE a.anula_lectura_id = l.id)
        AND l.ts >= COALESCE(
            (SELECT MAX(c.ts) FROM flota_lectura_odometro c
              WHERE c.vehiculo_id = NEW.vehiculo_id
                AND c.origen = 'correccion' AND c.anula_lectura_id IS NULL),
            l.ts)) THEN
    RAISE EXCEPTION '{_MSG_MONOTONIA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql"""

#: La de `m024triggersfaltantes`: el downgrade devuelve el estado real.
_MONOTONIA_VIEJA = f"""
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
END; $$ LANGUAGE plpgsql"""


def _es_pg():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    op.create_table(
        'flota_rechazo_cola',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('clave', sa.String(64), nullable=False),
        sa.Column('operacion', sa.String(20), nullable=False),
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuarios.id'),
                  nullable=False),
        sa.Column('placa', sa.String(20), nullable=True),
        sa.Column('vehiculo_id', sa.Integer(), sa.ForeignKey('vehiculos.id'),
                  nullable=True),
        sa.Column('status_http', sa.Integer(), nullable=False),
        sa.Column('mensaje', sa.Text(), nullable=False),
        sa.Column('intentos', sa.Integer(), nullable=False),
        sa.Column('primer_ts', sa.DateTime(), nullable=False),
        sa.Column('ultimo_ts', sa.DateTime(), nullable=False),
        sa.Column('ts_dispositivo', sa.DateTime(), nullable=True),
        sa.Column('estado', sa.String(12), nullable=False),
        sa.Column('pidio_ayuda_ts', sa.DateTime(), nullable=True),
        sa.Column('cerrado_ts', sa.DateTime(), nullable=True),
        sa.Column('cerrado_por_usuario_id', sa.Integer(),
                  sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('cierre_motivo', sa.Text(), nullable=True),
        sa.UniqueConstraint('usuario_id', 'clave', name='uq_flota_rechazo_cola_clave'),
        sa.CheckConstraint(_en('operacion', _OPERACIONES),
                           name='ck_flota_rechazo_cola_operacion'),
        sa.CheckConstraint(_en('estado', _ESTADOS),
                           name='ck_flota_rechazo_cola_estado'),
        sa.CheckConstraint(
            "estado <> 'cerrado' OR (cerrado_por_usuario_id IS NOT NULL AND "
            "cierre_motivo IS NOT NULL AND length(trim(cierre_motivo)) > 0)",
            name='ck_flota_rechazo_cola_cierre_con_autor'),
    )

    with op.batch_alter_table('flota_lectura_odometro') as b:
        b.add_column(sa.Column('anula_lectura_id', sa.Integer(),
                               sa.ForeignKey('flota_lectura_odometro.id',
                                             name='fk_flota_lectura_anula'),
                               nullable=True))
        b.add_column(sa.Column('serie', sa.String(10), nullable=True))
        b.create_check_constraint(
            'ck_flota_lectura_serie',
            "serie IS NULL OR serie IN ('cuenta', 'salto', 'tardia', 'contradice')")
        b.create_check_constraint(
            'ck_flota_lectura_fuera_de_serie_en_duda',
            "serie IS NULL OR serie = 'cuenta' OR confianza <> 'declarada'")
        b.create_check_constraint(
            'ck_flota_lectura_anula_solo_correccion',
            "anula_lectura_id IS NULL OR (origen = 'correccion' AND anula_lectura_id <> id)")
    op.create_index('uq_flota_lectura_anula', 'flota_lectura_odometro',
                    ['anula_lectura_id'], unique=True,
                    postgresql_where=sa.text('anula_lectura_id IS NOT NULL'),
                    sqlite_where=sa.text('anula_lectura_id IS NOT NULL'))
    # SQLite no tiene plpgsql: allá los triggers los rehace `create_all()`
    # desde el modelo (así corren los tests). Mismo criterio que m024.
    if _es_pg():
        op.execute(_MONOTONIA_NUEVA)
        op.execute(_solo_verificacion(_INMUTABLES_NUEVAS))


def downgrade():
    if _es_pg():
        op.execute(_MONOTONIA_VIEJA)
        op.execute(_solo_verificacion(_INMUTABLES_VIEJAS))
    op.drop_index('uq_flota_lectura_anula', table_name='flota_lectura_odometro')
    with op.batch_alter_table('flota_lectura_odometro') as b:
        b.drop_constraint('ck_flota_lectura_anula_solo_correccion', type_='check')
        b.drop_constraint('ck_flota_lectura_fuera_de_serie_en_duda', type_='check')
        b.drop_constraint('ck_flota_lectura_serie', type_='check')
        b.drop_column('serie')
        b.drop_column('anula_lectura_id')
    op.drop_table('flota_rechazo_cola')

"""flota tanda 1: ficha, documentos, odometro, custodia y fotos

Revision ID: f10ta1cimientos
Revises: c0a1cecc16dc
Create Date: 2026-08-01

PURAMENTE ADITIVA. Cinco tablas nuevas, ningun ALTER, ningun DROP, ninguna
migracion de datos. No toca una sola de las 169.495 filas existentes.

El cuerpo NO se escribio a mano: se emitio desde `db.metadata` para que no haya
diferencia posible entre lo que dicen los modelos y lo que crea la migracion.
Una transcripcion manual de 5 tablas, 31 CHECK y 5 indices es donde se pierde
un constraint sin que nadie lo note — y un invariante que la base no impone es
una sugerencia.

Los triggers van aparte, al final, y solo en PostgreSQL: son lo que ningun
CHECK puede hacer porque miran otras filas.
  · monotonia del odometro    (BEFORE INSERT)
  · append-only del odometro  (BEFORE UPDATE / BEFORE DELETE)
  · no-solape de custodia     (BEFORE INSERT)

Backup de referencia: snapshot manual Railway 2026-08-01 23:27 + PITR activo.
Estado previo verificable en scratchpad/estado_pre_migracion.json
(head c0a1cecc16dc, 48 tablas, 169.495 filas).
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta1cimientos'
down_revision = 'c0a1cecc16dc'
branch_labels = None
depends_on = None


_TRIGGERS_PG = """
CREATE OR REPLACE FUNCTION flota_odometro_monotonia() RETURNS trigger AS $$
BEGIN
  IF NEW.origen <> 'correccion' AND EXISTS (
      SELECT 1 FROM flota_lectura_odometro
      WHERE vehiculo_id = NEW.vehiculo_id AND valor_km > NEW.valor_km) THEN
    RAISE EXCEPTION 'flota: el odometro no puede decrecer sin origen=correccion';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_monotonia BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_monotonia();

CREATE OR REPLACE FUNCTION flota_odometro_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'flota: lectura_odometro es append-only'; END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_no_update BEFORE UPDATE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only();

CREATE TRIGGER flota_odometro_no_delete BEFORE DELETE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only();

CREATE OR REPLACE FUNCTION flota_custodia_no_solapa() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM flota_custodia
             WHERE vehiculo_id = NEW.vehiculo_id
               AND (inicio_ts > NEW.inicio_ts OR fin_ts > NEW.inicio_ts)) THEN
    RAISE EXCEPTION 'flota: la custodia nueva se solapa o antecede a la anterior';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_custodia_no_solapa BEFORE INSERT ON flota_custodia
FOR EACH ROW EXECUTE FUNCTION flota_custodia_no_solapa();
"""


def upgrade():
    op.create_table(
        'flota_foto',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('clase', sa.String(length=20), nullable=False),
        sa.Column('entidad_tipo', sa.String(length=20), nullable=False),
        sa.Column('entidad_id', sa.Integer(), nullable=False),
        sa.Column('storage_ref', sa.Text(), nullable=False),
        sa.Column('hash_sha256', sa.String(length=64), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('ancho', sa.Integer(), nullable=False),
        sa.Column('alto', sa.Integer(), nullable=False),
        sa.Column('mime', sa.String(length=40), nullable=False),
        sa.Column('ts_captura', sa.DateTime(), nullable=False),
        sa.Column('gps_lat', sa.Float(), nullable=True),
        sa.Column('gps_lon', sa.Float(), nullable=True),
        sa.Column('autor_usuario_id', sa.Integer(), nullable=False),
        sa.Column('simulado', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('estado', sa.String(length=30), nullable=False, server_default='ok'),
        sa.ForeignKeyConstraint(['autor_usuario_id'], ['usuarios.id']),
        sa.CheckConstraint("clase IN ('evidencia_estado', 'foto_dato')", name='ck_flota_clase'),
        sa.CheckConstraint("clase <> 'foto_dato' OR estado = 'pendiente_evidencia' OR ancho >= 1600 OR alto >= 1600", name='ck_flota_foto_dato_resolucion'),
        sa.CheckConstraint("estado IN ('ok', 'pendiente_evidencia')", name='ck_flota_estado'),
        sa.CheckConstraint("storage_ref NOT LIKE 'data:%' AND length(storage_ref) < 500", name='ck_flota_foto_es_referencia_no_binario'),
        sa.CheckConstraint("entidad_tipo IN ('custodia_inicio', 'custodia_fin', 'odometro', 'documento', 'hallazgo')", name='ck_flota_entidad_tipo'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('bytes > 0 AND ancho > 0 AND alto > 0', name='ck_flota_foto_medidas'),
    )
    op.create_index('ix_flota_foto_padre', 'flota_foto', ['entidad_tipo', 'entidad_id'], unique=False)

    op.create_table(
        'flota_ficha_tecnica',
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('combustible', sa.String(length=20), nullable=False, server_default='sin_dato'),
        sa.Column('sistema_frenos', sa.String(length=30), nullable=False, server_default='sin_dato'),
        sa.Column('tiene_freno_escape', sa.String(length=10), nullable=False, server_default='sin_dato'),
        sa.Column('distribucion', sa.String(length=10), nullable=False, server_default='sin_dato'),
        sa.Column('transmision_final', sa.String(length=10), nullable=False, server_default='sin_dato'),
        sa.Column('distribucion_km_cambio', sa.Integer(), nullable=True),
        sa.Column('norma_emisiones', sa.Text(), nullable=True),
        sa.Column('aceite_motor_spec', sa.Text(), nullable=False, server_default='sin_dato'),
        sa.Column('aceite_motor_litros', sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column('aceite_caja_spec', sa.Text(), nullable=True),
        sa.Column('aceite_diferencial_spec', sa.Text(), nullable=True),
        sa.Column('refrigerante_spec', sa.Text(), nullable=True),
        sa.Column('posiciones_llanta', sa.Integer(), nullable=False),
        sa.Column('medida_llanta', sa.Text(), nullable=True),
        sa.Column('tiene_furgon', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('km_inicial', sa.Integer(), nullable=False),
        sa.Column('km_inicial_ts', sa.DateTime(), nullable=False),
        sa.Column('distribucion_fuente', sa.String(length=30), nullable=False, server_default='sin_dato'),
        sa.Column('distribucion_verificado_ts', sa.DateTime(), nullable=True),
        sa.Column('frenos_fuente', sa.String(length=30), nullable=False, server_default='sin_dato'),
        sa.Column('frenos_verificado_ts', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id']),
        sa.CheckConstraint("transmision_final IN ('cadena', 'correa', 'cardan', 'sin_dato')", name='ck_flota_transmision_final'),
        sa.CheckConstraint("frenos_fuente IN ('manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado', 'sin_dato')", name='ck_flota_frenos_fuente'),
        sa.CheckConstraint('km_inicial >= 0', name='ck_flota_km_inicial'),
        sa.CheckConstraint("tiene_freno_escape IN ('si', 'no', 'sin_dato')", name='ck_flota_tiene_freno_escape'),
        sa.CheckConstraint("sistema_frenos = 'sin_dato' OR frenos_fuente <> 'sin_dato'", name='ck_flota_frenos_con_procedencia'),
        sa.PrimaryKeyConstraint('vehiculo_id'),
        sa.CheckConstraint("distribucion = 'sin_dato' OR distribucion_fuente <> 'sin_dato'", name='ck_flota_distribucion_con_procedencia'),
        sa.CheckConstraint("distribucion IN ('correa', 'cadena', 'sin_dato')", name='ck_flota_distribucion'),
        sa.CheckConstraint("distribucion_fuente IN ('manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado', 'sin_dato')", name='ck_flota_distribucion_fuente'),
        sa.CheckConstraint('posiciones_llanta > 0', name='ck_flota_posiciones_llanta'),
        sa.CheckConstraint("combustible IN ('gasolina', 'diesel', 'sin_dato')", name='ck_flota_combustible'),
        sa.CheckConstraint("sistema_frenos IN ('hidraulico', 'aire_sobre_hidraulico', 'aire_full', 'sin_dato')", name='ck_flota_sistema_frenos'),
    )

    op.create_table(
        'flota_documento_vehiculo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('numero', sa.String(length=50), nullable=False, server_default=''),
        sa.Column('entidad', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('fecha_expedicion', sa.Date(), nullable=True),
        sa.Column('fecha_vencimiento', sa.Date(), nullable=True),
        sa.Column('foto_id', sa.Integer(), nullable=True),
        sa.Column('estado', sa.String(length=20), nullable=False, server_default='vigente'),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id']),
        sa.ForeignKeyConstraint(['foto_id'], ['flota_foto.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("tipo IN ('soat', 'rtm', 'poliza_rc', 'tarjeta_propiedad')", name='ck_flota_tipo'),
        sa.CheckConstraint('fecha_vencimiento IS NULL OR fecha_vencimiento >= fecha_expedicion', name='ck_flota_doc_vigencia'),
        sa.UniqueConstraint('vehiculo_id', 'tipo', 'numero', name='uq_flota_doc'),
        sa.CheckConstraint("(estado = 'vigente' AND fecha_expedicion IS NOT NULL  AND fecha_vencimiento IS NOT NULL  AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0) OR (estado = 'no_encontrado' AND fecha_expedicion IS NULL  AND fecha_vencimiento IS NULL)", name='ck_flota_doc_estado_coherente'),
        sa.CheckConstraint("estado IN ('vigente', 'no_encontrado')", name='ck_flota_estado'),
    )
    op.create_index('ix_flota_documento_vehiculo_vehiculo_id', 'flota_documento_vehiculo', ['vehiculo_id'], unique=False)

    op.create_table(
        'flota_lectura_odometro',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('valor_km', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('origen', sa.String(length=20), nullable=False),
        sa.Column('foto_id', sa.Integer(), nullable=True),
        sa.Column('autor_usuario_id', sa.Integer(), nullable=False),
        sa.Column('motivo_correccion', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id']),
        sa.ForeignKeyConstraint(['foto_id'], ['flota_foto.id']),
        sa.ForeignKeyConstraint(['autor_usuario_id'], ['usuarios.id']),
        sa.CheckConstraint("origen IN ('entrega', 'preoperacional', 'cierre_dia', 'ot', 'tanqueo', 'correccion')", name='ck_flota_origen'),
        sa.CheckConstraint("origen <> 'correccion' OR (motivo_correccion IS NOT NULL AND length(trim(motivo_correccion)) > 0)", name='ck_flota_correccion_con_motivo'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('valor_km >= 0', name='ck_flota_valor_km'),
    )
    op.create_index('ix_flota_lectura_odometro_vehiculo_id', 'flota_lectura_odometro', ['vehiculo_id'], unique=False)

    op.create_table(
        'flota_custodia',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('custodio_tipo', sa.String(length=20), nullable=False),
        sa.Column('custodio_conductor_id', sa.Integer(), nullable=True),
        sa.Column('custodio_sede_id', sa.Integer(), nullable=True),
        sa.Column('registrado_por_usuario_id', sa.Integer(), nullable=False),
        sa.Column('inicio_ts', sa.DateTime(), nullable=False),
        sa.Column('fin_ts', sa.DateTime(), nullable=True),
        sa.Column('km_inicio', sa.Integer(), nullable=False),
        sa.Column('km_fin', sa.Integer(), nullable=True),
        sa.Column('linea_base', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('custodio_estado', sa.String(length=20), nullable=False, server_default='resuelto'),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id']),
        sa.ForeignKeyConstraint(['custodio_conductor_id'], ['conductores.id']),
        sa.ForeignKeyConstraint(['custodio_sede_id'], ['almacenes.id']),
        sa.ForeignKeyConstraint(['registrado_por_usuario_id'], ['usuarios.id']),
        sa.CheckConstraint('fin_ts IS NULL OR fin_ts >= inicio_ts', name='ck_flota_custodia_cierre_posterior'),
        sa.CheckConstraint("(custodio_estado = 'resuelto' AND  (CASE WHEN custodio_conductor_id IS NOT NULL THEN 1 ELSE 0 END +   CASE WHEN custodio_sede_id IS NOT NULL THEN 1 ELSE 0 END) = 1) OR (custodio_estado = 'pendiente_sede' AND  custodio_conductor_id IS NULL AND custodio_sede_id IS NULL)", name='ck_flota_custodia_arco_exclusivo'),
        sa.CheckConstraint('km_fin IS NULL OR km_fin >= km_inicio', name='ck_flota_custodia_km_no_decrece'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("custodio_tipo IN ('conductor', 'sede')", name='ck_flota_custodio_tipo'),
        sa.CheckConstraint("custodio_estado = 'pendiente_sede' OR (custodio_tipo = 'conductor' AND custodio_conductor_id IS NOT NULL) OR (custodio_tipo = 'sede' AND custodio_sede_id IS NOT NULL)", name='ck_flota_custodia_tipo_coherente'),
        sa.CheckConstraint("custodio_estado <> 'pendiente_sede' OR custodio_tipo = 'sede'", name='ck_flota_pendiente_sede_solo_es_sede'),
        sa.CheckConstraint("custodio_estado IN ('resuelto', 'pendiente_sede')", name='ck_flota_custodio_estado'),
    )
    op.create_index('ix_flota_custodia_vehiculo_id', 'flota_custodia', ['vehiculo_id'], unique=False)
    op.create_index('uq_flota_custodia_activa', 'flota_custodia', ['vehiculo_id'], unique=True, postgresql_where=sa.text('fin_ts IS NULL'))

    # Triggers: solo PostgreSQL. En los tests las tablas nacen por create_all y
    # los triggers cuelgan del `after_create` de cada tabla, asi que existen
    # tambien alli — un invariante impuesto en produccion pero no en tests es
    # un invariante que nadie ejercio.
    if op.get_bind().dialect.name == 'postgresql':
        for sentencia in [s.strip() for s in _TRIGGERS_PG.split(';\n\n') if s.strip()]:
            op.execute(sentencia)


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        for t, f in (('flota_odometro_monotonia', 'flota_lectura_odometro'),
                     ('flota_odometro_no_update', 'flota_lectura_odometro'),
                     ('flota_odometro_no_delete', 'flota_lectura_odometro'),
                     ('flota_custodia_no_solapa', 'flota_custodia')):
            op.execute(f'DROP TRIGGER IF EXISTS {t} ON {f}')
        for fn in ('flota_odometro_monotonia', 'flota_odometro_append_only',
                   'flota_custodia_no_solapa'):
            op.execute(f'DROP FUNCTION IF EXISTS {fn}()')
    # Orden inverso: custodia y odometro apuntan a foto.
    for t in ('flota_custodia', 'flota_lectura_odometro',
              'flota_documento_vehiculo', 'flota_ficha_tecnica', 'flota_foto'):
        op.drop_table(t)

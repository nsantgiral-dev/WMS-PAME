#!/usr/bin/env bash
#
# Los 33 invariantes de flota que SOLO existen en PostgreSQL.
#
#   ./scripts/verificar_flota_postgres.sh
#
# Por qué hace falta un script: `railway.toml` corre la suite con
# `-m "not postgres"` y el contenedor de build no tiene una base. Esos 33 tests
# **nunca han corrido en CI**, y son justo los que miden la propiedad real en
# vez de la proxy:
#
#   · que los 51 CHECK lleguen de verdad a la base
#   · que el `DELETE` sobre `flota_lectura_odometro` esté bloqueado — protección
#     que **solo existe en PostgreSQL**: se omitió en SQLite porque rompía el
#     teardown de otros tests
#   · que los índices únicos parciales funcionen (dos custodias activas
#     colisionan, N cerradas conviven)
#   · que `flota_limpiar_vehiculo.py` **restaure** el trigger que deshabilita —
#     si su `finally` fallara, el guard queda apagado y no da error: deja pasar
#   · que la migración corra en el orden correcto contra datos reales
#
# El archivo existe por un caso fechado: el 2026-08-01 un CHECK escrito como
# `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests contra SQLite y reventó
# el `CREATE TABLE` en el release, porque PostgreSQL no suma booleanos.
#
# Esa lección ya cambió una decisión de diseño: `ck_flota_hallazgo_desenlace`
# (2026-09-01) se escribió con `CASE WHEN` en vez de sumando predicados, y acá
# se comprueba que de verdad compila y de verdad rechaza. El mismo día, este
# script destapó que el fixture borraba `flota_lectura_odometro` sin borrar
# antes su hijo `flota_hallazgo` — un error de orden por FK que SQLite no ve.
#
# La suite normal corre contra SQLite en memoria. **La mitad de los invariantes
# de este módulo solo se prueba contra el motor equivocado.**
#
# Última corrida verde: 2026-09-01, PostgreSQL 17 local, 33 passed en 1,67 s.
set -euo pipefail

BASE="${1:-wms_flota_verif_$$}"
HOST="${PGHOST:-localhost}"

# Solo local, a propósito. `FLOTA_TEST_PG_URL` apuntando a producción haría que
# estos tests —que crean, borran y deshabilitan triggers— corran contra la base
# del negocio. El repo ya tiene el precedente: `reset_transaccional.py` exige
# escribir el host justamente para que nadie lo haga por inercia.
case "$HOST" in
  localhost|127.0.0.1|/tmp|"") ;;
  *)
    echo "ABORTA: PGHOST='$HOST' no es local." >&2
    echo "Estos tests CREAN Y BORRAN tablas y deshabilitan triggers." >&2
    exit 2
    ;;
esac

if ! pg_isready -q; then
  echo "No hay PostgreSQL escuchando. Arrancalo y volvé a correr." >&2
  exit 2
fi

limpiar() { dropdb --if-exists "$BASE" 2>/dev/null || true; }
trap limpiar EXIT

createdb "$BASE"
echo "── base temporal: $BASE"

FLOTA_TEST_PG_URL="postgresql://${HOST}/${BASE}" \
TZ=UTC venv/bin/python -m pytest tests/flota/test_constraints_postgres.py \
  -q -m postgres -p no:randomly

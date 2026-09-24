"""El lock por NIT de la compuerta G1, contra PostgreSQL real (`@postgres`,
`FLOTA_TEST_PG_URL`, local y desechable). En SQLite `advisory_lock` concede
siempre: la exclusión solo se puede ver acá.

Dos pedidos a crédito del mismo cliente evaluados a la par verían el mismo cupo
libre; el lock (conexión dedicada, `RANGO_CARTERA_NIT`, fuera de int32) hace
que el segundo reciba «otro despacho de este cliente se está evaluando».
"""
import os

import pytest

_HOSTS_LOCALES = {'localhost', '127.0.0.1', '::1', None, ''}


@pytest.fixture(scope='module')
def app_pg():
    import sqlalchemy as sa
    from flask import Flask

    from app.extensions import db
    url = os.getenv('FLOTA_TEST_PG_URL')
    if not url:
        pytest.fail('FLOTA_TEST_PG_URL no está definida (PostgreSQL local, desechable).')
    u = sa.engine.make_url(url)
    if u.get_backend_name() != 'postgresql' or u.host not in _HOSTS_LOCALES:
        pytest.fail(f'FLOTA_TEST_PG_URL debe ser un PostgreSQL local, no {u.host!r}')
    app = Flask('cartera_pg')
    app.config['SQLALCHEMY_DATABASE_URI'] = url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_size': 5}
    db.init_app(app)
    yield app, db
    with app.app_context():
        db.engine.dispose()


@pytest.mark.postgres
class TestElLockDelClienteExcluye:

    def _lock(self, nit):
        from app.services.cartera_service import n_lock_nit
        from app.utils.lock import RANGO_CARTERA_NIT, advisory_lock, clave_en_rango
        return advisory_lock(clave_en_rango(RANGO_CARTERA_NIT, n_lock_nit(nit)), nit)

    def test_el_mismo_cliente_espera_y_otro_cliente_no(self, app_pg):
        app, db = app_pg
        with app.app_context():
            with self._lock('900111222') as primero:
                assert primero is True
                db.session.commit()
                with self._lock('900111222-7') as mismo:
                    assert mismo is False, 'dos despachos del mismo NIT a la vez'
                with self._lock('800555444') as otro:
                    assert otro is True
            with self._lock('900111222') as despues:
                assert despues is True, 'el lock no se soltó al salir del with'

    def test_la_clave_cabe_en_bigint_y_no_choca_con_el_registro(self, app_pg):
        from app.services.cartera_service import clave_lock_nit
        from app.utils import lock as L
        k = clave_lock_nit('900111222')
        assert k > 2 ** 31
        assert k not in L._claves_fijas().values()

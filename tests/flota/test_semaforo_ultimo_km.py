"""«El último kilometraje está en duda» solo si la ÚLTIMA lectura lo está.

QA e2e 2026-09-24 (menor): el semáforo lo decía si CUALQUIER lectura dudosa
del vehículo seguía pendiente, aunque después se hubiera leído el km con foto.
La lectura vieja sigue en Pendientes («kilometraje X en duda»), una por una;
lo que cambia es que no pinta de ámbar un km actual que sí está respaldado.
"""
from datetime import datetime, timedelta

from tests.test_qa_e2e_flota_20260924 import _auth, mundo  # noqa: F401

TEXTO = 'el último kilometraje está en duda'


def _lectura(db, m, km, ts, foto):
    from flota.adaptadores.modelos import Foto, LecturaOdometro
    f = None
    if foto:
        f = Foto(clase='foto_dato', entidad_tipo='odometro', entidad_id=0,
                 storage_ref=f'test/{m.placa}-{km}', hash_sha256='0' * 64, bytes=100,
                 ancho=1600, alto=1200, mime='image/jpeg', ts_captura=ts,
                 autor_usuario_id=m.u_admin.id)
        db.session.add(f)
        db.session.flush()
    l = LecturaOdometro(vehiculo_id=m.vehiculo_id, valor_km=km, ts=ts, origen='cierre_dia',
                        foto_id=f.id if f else None, autor_usuario_id=m.u_admin.id)
    db.session.add(l)
    db.session.flush()
    if f is not None:
        f.entidad_id = l.id
    db.session.commit()
    return l


def _fila(client, m):
    r = client.get('/flota/bandeja', headers=_auth(m.t_flota))
    assert r.status_code == 200, r.get_json()
    b = r.get_json()
    [f] = [x for x in b['hoy'] if x['placa'] == m.placa]
    return b, ' | '.join(p['texto'] for p in f['semaforo']['porque']), f


class TestElUltimoKmEnDuda:
    def test_una_dudosa_vieja_no_pinta_el_km_actual(self, client, db, mundo):  # noqa: F811
        t = datetime.utcnow()
        vieja = _lectura(db, mundo, 1000, t - timedelta(days=3), foto=False)
        _lectura(db, mundo, 1200, t - timedelta(hours=2), foto=True)
        b, textos, f = _fila(client, mundo)
        assert TEXTO not in textos, textos
        assert f['km']['en_duda'] is False
        # la vieja sigue pendiente, una por una
        assert any(p['clase'] == 'km_dudoso' and p['placa'] == mundo.placa
                   for p in b['pendientes']), vieja.id

    def test_si_la_ultima_esta_en_duda_se_dice(self, client, db, mundo):  # noqa: F811
        t = datetime.utcnow()
        _lectura(db, mundo, 1000, t - timedelta(days=3), foto=True)
        _lectura(db, mundo, 1200, t - timedelta(hours=2), foto=False)
        _b, textos, f = _fila(client, mundo)
        assert TEXTO in textos, textos
        assert f['km']['en_duda'] is True

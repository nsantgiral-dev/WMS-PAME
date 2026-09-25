"""El fin de un día normal no es señal ni pendiente (QA e2e 2026-09-24, P2).

**La clase:** *una comparación contra el estado de AHORA para juzgar algo que
ya terminó*. La señal `turno_de_la_ruta` comparaba una ruta ENTREGADA contra el
turno vigente del vehículo; al final de un día normal el camión está en la
sede y salía «ya salió y sigue en custodia de la sede» — cada noche. Y el
turno de la SEDE salía como pendiente «sin fotos de inicio (0 de 11)», cuando
nadie fotografía a la sede: las fotos del relevo cuelgan del turno del
conductor.

Ahora una ruta ya entregada se juzga contra el turno **vigente al cierre**
(`bandeja._turno_en`), y la sede no tiene fotos exigidas. Los casos de abajo
son los que el cambio NO debe apagar: la ruta que de verdad se hizo con el
camión en la sede, o con el turno a nombre de otro, sigue siendo señal.
"""
from datetime import datetime, timedelta

from tests.test_qa_e2e_flota_20260924 import _auth, _ruta_con_parada, mundo  # noqa: F401


def _custodia(db, m, inicio, fin=None, conductor_id=None, vehiculo_id=None, forzado=None):
    from flota.adaptadores.modelos import Custodia
    c = Custodia(vehiculo_id=vehiculo_id or m.vehiculo_id,
                 custodio_tipo='conductor' if conductor_id else 'sede',
                 custodio_conductor_id=conductor_id,
                 custodio_sede_id=None if conductor_id else m.almacen.id,
                 registrado_por_usuario_id=m.u_admin.id,
                 inicio_ts=inicio, fin_ts=fin, km_inicio=1000,
                 km_fin=1000 if fin else None,
                 cierre_forzado=bool(forzado),
                 cierre_forzado_por_usuario_id=m.u_admin.id if forzado else None,
                 cierre_forzado_motivo=forzado)
    db.session.add(c)
    db.session.commit()
    return c


def _entregada(db, m, cierre):
    from app.models.ruta_despacho import RutaDespacho
    ruta, _t = _ruta_con_parada(db, m, estado='ENTREGADA', bulto_estado='ENTREGADO')
    RutaDespacho.query.get(ruta.id).fecha_entregada = cierre
    db.session.commit()
    return ruta


def _bandeja(client, m):
    r = client.get('/flota/bandeja', headers=_auth(m.t_flota))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _sen(b, m):
    return [s for s in b['senales'] if s['clase'] == 'turno_de_la_ruta' and s['placa'] == m.placa]


AHORA = datetime.utcnow


class TestLaRutaEntregadaSeJuzgaContraElTurnoDelCierre:
    def test_dia_normal_sin_senal(self, client, db, mundo):
        t = AHORA()
        _custodia(db, mundo, t - timedelta(hours=10), t - timedelta(hours=1),
                  conductor_id=mundo.conductor_id)
        _custodia(db, mundo, t - timedelta(hours=1))
        _entregada(db, mundo, t - timedelta(hours=2))
        assert _sen(_bandeja(client, mundo), mundo) == []

    def test_cierre_y_entrega_del_camion_en_el_mismo_instante(self, client, db, mundo):
        """El turno que termina justo al cierre es el de la ruta; el que empieza
        en ese instante es el siguiente (el traspaso cierra y abre en el mismo
        `ahora`)."""
        t = AHORA() - timedelta(hours=1)
        _custodia(db, mundo, t - timedelta(hours=9), t, conductor_id=mundo.conductor_id)
        _custodia(db, mundo, t)
        _entregada(db, mundo, t)
        assert _sen(_bandeja(client, mundo), mundo) == []

    def test_la_ruta_se_hizo_con_el_camion_en_la_sede_sigue_siendo_senal(self, client, db, mundo):
        t = AHORA()
        _custodia(db, mundo, t - timedelta(hours=10))
        _entregada(db, mundo, t - timedelta(hours=2))
        [s] = _sen(_bandeja(client, mundo), mundo)
        assert s['evidencia']['forma'] == 'salio_sin_turno'
        assert 'estaba en custodia de la sede' in s['texto']

    def test_la_ruta_se_hizo_con_el_turno_de_otro_sigue_siendo_senal(self, client, db, mundo):
        from app.models.conductor import Conductor
        otro = Conductor(nombre='Beto QA', cedula='QA-BETO', activo=True)
        db.session.add(otro)
        db.session.commit()
        t = AHORA()
        _custodia(db, mundo, t - timedelta(hours=10), t - timedelta(hours=1), conductor_id=otro.id)
        _custodia(db, mundo, t - timedelta(hours=1))
        _entregada(db, mundo, t - timedelta(hours=2))
        [s] = _sen(_bandeja(client, mundo), mundo)
        assert s['evidencia']['forma'] == 'turno_de_otro'

    def test_entregada_sin_hora_de_cierre_se_declara(self, client, db, mundo):
        t = AHORA()
        _custodia(db, mundo, t - timedelta(hours=1))
        _entregada(db, mundo, None)
        b = _bandeja(client, mundo)
        assert _sen(b, mundo) == []
        assert any(n['clase'] == 'turno_de_la_ruta' and 'sin hora de cierre' in n['motivo']
                   for n in b['senales_no_evaluables'])

    def test_en_transito_se_sigue_comparando_contra_el_turno_de_ahora(self, client, db, mundo):
        t = AHORA()
        _custodia(db, mundo, t - timedelta(hours=10))
        _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
        [s] = _sen(_bandeja(client, mundo), mundo)
        assert s['evidencia']['forma'] == 'salio_sin_turno'
        assert 'sigue en custodia de la sede' in s['texto']


class TestLaSedeNoTieneFotosExigidas:
    def _filas(self):
        from flota.adaptadores.medicion import MedidorSQL
        return MedidorSQL().custodias_por_vehiculo() or []

    def test_la_sede_sin_fotos_no_es_turno_sin_fotos(self, db, mundo):
        c = _custodia(db, mundo, AHORA() - timedelta(hours=1))
        assert not [f for f in self._filas() if f['custodia_id'] == c.id]

    def test_el_conductor_sin_fotos_de_inicio_sigue_contando(self, db, mundo):
        c = _custodia(db, mundo, AHORA() - timedelta(hours=1), conductor_id=mundo.conductor_id)
        [f] = [f for f in self._filas() if f['custodia_id'] == c.id]
        assert f['mitad_incompleta'] == 'inicio'

    def test_el_cierre_forzado_de_la_sede_sigue_contando(self, db, mundo):
        t = AHORA()
        c = _custodia(db, mundo, t - timedelta(hours=5), t - timedelta(hours=1),
                      forzado='se lo llevó sin recibir')
        [f] = [f for f in self._filas() if f['custodia_id'] == c.id]
        assert f['cierre_forzado'] and not f['sin_foto_completa']

"""
Rutas que nunca cierran y rezago de la noche (validación de la plata,
2026-09-26).

- Una ruta EN_TRANSITO hace más de `HORAS_SIN_CERRAR` aparece en Liquidación y
  en el resumen diario; quien liquida pide el cierre (el teléfono lo muestra) y
  el administrador lo fuerza.
- Una ruta entregada desde las `HORA_ENTREGA_DIA_SIGUIENTE` (Bogotá) es del día
  operativo siguiente para el rezago: la alerta de las 06:30 ya no la llama
  atrasada (decisión del CTO; en temporada se opera hasta la medianoche).
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from tests.test_cartera_retencion import _jwt, _recaudo, _tarea, _usuario
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture


def _utc(d, hora_bogota):
    return datetime(d.year, d.month, d.day, hora_bogota) + timedelta(hours=5)


class TestElRezagoDeLaNoche:

    def test_entregada_de_noche_no_amanece_atrasada(self):
        from app.services import rezago_liquidacion as rl
        ayer, hoy = date(2026, 9, 15), date(2026, 9, 16)
        noche = SimpleNamespace(fecha_entregada=_utc(ayer, 21), fecha_programada=ayer)
        tarde = SimpleNamespace(fecha_entregada=_utc(ayer, 15), fecha_programada=ayer)
        assert rl.urgencia(noche, hoy) == rl.OK and rl.dias_de_rezago(noche, hoy) == 0
        assert rl.urgencia(tarde, hoy) == rl.ATRASADA and rl.dias_de_rezago(tarde, hoy) == 1
        # Al cierre del día siguiente sin liquidar, sí.
        assert rl.urgencia(noche, hoy + timedelta(days=1)) == rl.ATRASADA

    def test_el_mes_es_el_de_la_entrega_real(self):
        from app.services import rezago_liquidacion as rl
        fin = SimpleNamespace(fecha_entregada=_utc(date(2026, 9, 30), 21),
                              fecha_programada=date(2026, 9, 30))
        assert rl.urgencia(fin, date(2026, 10, 1)) == rl.CRUZA_MES


class TestLasRutasSinCerrar:

    def test_aparecen_y_el_resumen_las_nombra(self, app, client, db, mundo):
        from app.models.ruta_despacho import RutaDespacho
        from app.services import rezago_liquidacion as rl
        f, uc, ad = mundo
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        ruta.fecha_cierre = datetime.utcnow() - timedelta(hours=5)
        db.session.commit()
        assert rl.rutas_sin_cerrar() == []
        ruta.fecha_cierre = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()
        [x] = rl.rutas_sin_cerrar()
        assert x['ruta_id'] == f.ruta_id and x['horas'] >= 30
        [linea] = rl.lineas_de_aviso_sin_cerrar()
        assert str(f.ruta_id) in linea
        d = client.get('/api/rutas/liquidacion/dashboard', headers=_jwt(app, ad)).get_json()
        assert [r['ruta_id'] for r in d['rutas_sin_cerrar']] == [f.ruta_id]

    def test_quien_liquida_pide_el_cierre_y_el_conductor_lo_ve(self, app, client, db, mundo):
        f, uc, ad = mundo
        url = f'/api/rutas/{f.ruta_id}/pedir-cierre'
        assert client.post(url, headers=_jwt(app, _usuario(db, 'lider_cartera'))).status_code == 403
        r = client.post(url, headers=_jwt(app, _usuario(db, 'liquidador')))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['ruta']['cierre_pedido_en']
        from app.services.ruta_service import RutaService
        assert RutaService.listar_paradas(f.ruta_id)['cierre_pedido_en']

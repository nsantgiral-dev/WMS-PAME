"""
Historia de las líneas de pedido (m036fotos).

`pedidos_siesa` borra lo cumplido y no guarda cuándo vio algo por primera vez.
`pedidos_historia` sí. La regla que manda: **un barrido incompleto no registra
ninguna salida** — ni «cumplido» ni «desaparecido». Con páginas sin leer, lo
que no se leyó se ve igual que lo que ya no existe (Regla 0).
"""
from datetime import datetime, timedelta

import pytest


def _fila(rowid, consec=1502, pedida=10, remisionada=0, co='003', estado=3, item='SKU1'):
    return {
        'f431_rowid': rowid, 'f430_id_co': co, 'f430_id_tipo_docto': 'PD ',
        'f430_consec_docto': consec, 'f150_id': 'NB1', 'f120_referencia': item,
        'f120_id': 7, 'f200_id_pedido_fact': '900', 'f200_razon_social_pedido_fact': 'CLI',
        'f015_id_depto_pe': '41', 'f015_id_ciudad_pe': '001', 'f200_id_pedido_vend': 'V1',
        'f430_id_cond_pago': 'C02', 'f430_id_fecha': '2026-09-20T00:00:00',
        'f430_fecha_entrega': '2026-09-21T00:00:00', 'f431_cant1_pedida': pedida,
        'f431_cant1_remisionada': remisionada, 'f431_cant1_comprometida': pedida,
        'f431_vlr_neto': 1000.0, 'f430_ind_estado': estado,
    }


#: 01:30 UTC del 25 = 20:30 del 24 en Bogotá. La franja en que el día UTC y el
#: de Bogotá no coinciden (Regla 5).
_NOCHE = datetime(2026, 9, 25, 1, 30)


def _h(rowid):
    from app.models.pedido_historia import PedidoHistoria
    return PedidoHistoria.query.filter_by(linea_rowid=rowid).one()


class TestPrimeraVez:

    def test_registra_la_linea_con_el_dia_de_bogota(self, db):
        from app.services.pedidos_historia import registrar_barrido
        r = registrar_barrido([_fila(1)], True, ahora=_NOCHE, co_barrido='003')
        h = _h(1)
        assert r['nuevas'] == 1
        assert h.primer_dia_visto.isoformat() == '2026-09-24', \
            'a las 20:30 de Bogotá el día UTC ya es el 25'
        assert h.pedido_clave == '003-PD-1502'
        assert h.tipo_docto == 'PD', 'el tipo viene con espacio de Siesa'
        assert float(h.cantidad_pedida_inicial) == 10
        assert h.salida_at is None

    def test_la_cantidad_inicial_no_se_pisa(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1, pedida=10)], True, ahora=_NOCHE, co_barrido='003')
        registrar_barrido([_fila(1, pedida=12)], True, ahora=_NOCHE + timedelta(minutes=1),
                          co_barrido='003')
        h = _h(1)
        assert float(h.cantidad_pedida_inicial) == 10 and float(h.cantidad_pedida) == 12


class TestUnBarridoIncompletoNoRegistraSalidas:

    def test_una_linea_ausente_no_desaparece(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1), _fila(2)], True, ahora=_NOCHE, co_barrido='003')
        r = registrar_barrido([_fila(1)], False, ahora=_NOCHE + timedelta(minutes=1),
                              co_barrido='003')
        assert _h(2).salida_at is None
        assert r['desaparecidas'] == 0 and r['salidas_registradas'] is False

    def test_una_linea_remisionada_no_se_marca_cumplida(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1, remisionada=10)], False, ahora=_NOCHE, co_barrido='003')
        assert _h(1).motivo_salida is None

    def test_el_barrido_completo_si_decide(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1), _fila(2)], True, ahora=_NOCHE, co_barrido='003')
        r = registrar_barrido([_fila(1, remisionada=10)], True,
                              ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        assert _h(1).motivo_salida == 'CUMPLIDO'
        assert _h(2).motivo_salida == 'DESAPARECIDO'
        assert _h(2).salida_dia.isoformat() == '2026-09-24'
        assert r['cumplidas'] == 1 and r['desaparecidas'] == 1

    def test_no_decide_sobre_otro_co(self, db):
        """El sync pregunta por UN CO. Una línea de otro no está ausente: no se
        preguntó por ella."""
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(9, co='001')], False, ahora=_NOCHE, co_barrido='001')
        registrar_barrido([], True, ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        assert _h(9).salida_at is None


class TestCuandoSeEscribe:

    def test_no_reescribe_cada_minuto_si_nada_cambio(self, db):
        from app.services.pedidos_historia import REFRESCO_MINUTOS, registrar_barrido
        registrar_barrido([_fila(1)], True, ahora=_NOCHE, co_barrido='003')
        r = registrar_barrido([_fila(1)], True, ahora=_NOCHE + timedelta(minutes=1),
                              co_barrido='003')
        assert r['actualizadas'] == 0 and _h(1).ultima_vez_vista_at == _NOCHE
        tarde = _NOCHE + timedelta(minutes=REFRESCO_MINUTOS + 1)
        r = registrar_barrido([_fila(1)], True, ahora=tarde, co_barrido='003')
        assert r['actualizadas'] == 1 and _h(1).ultima_vez_vista_at == tarde

    def test_un_cambio_de_cantidad_se_escribe_ya(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1)], True, ahora=_NOCHE, co_barrido='003')
        r = registrar_barrido([_fila(1, remisionada=3)], True,
                              ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        assert r['actualizadas'] == 1 and float(_h(1).cantidad_remisionada) == 3

    def test_una_linea_que_vuelve_se_reabre(self, db):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1)], True, ahora=_NOCHE, co_barrido='003')
        registrar_barrido([], True, ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        assert _h(1).motivo_salida == 'DESAPARECIDO'
        r = registrar_barrido([_fila(1)], True, ahora=_NOCHE + timedelta(minutes=2),
                              co_barrido='003')
        assert _h(1).salida_at is None and _h(1).reapariciones == 1 and r['reabiertas'] == 1

    def test_una_fila_sin_rowid_se_cuenta_no_se_inventa(self, db):
        from app.models.pedido_historia import PedidoHistoria
        from app.services.pedidos_historia import registrar_barrido
        f = _fila(1)
        f.pop('f431_rowid')
        r = registrar_barrido([f], True, ahora=_NOCHE, co_barrido='003')
        assert r['sin_rowid'] == 1 and PedidoHistoria.query.count() == 0


class TestClasificarDesaparecidas:

    class _G:
        def __init__(self, estados):
            self.estados, self.llamadas = estados, []

        def get_estado_pedido(self, tipo, consec):
            self.llamadas.append((tipo, consec))
            return self.estados.get(consec)

    @pytest.mark.parametrize('estado,motivo', [(9, 'ANULADO'), (4, 'CUMPLIDO'),
                                               (2, 'OTRO_ESTADO')])
    def test_pregunta_a_siesa_y_clasifica(self, db, estado, motivo):
        from app.services.pedidos_historia import clasificar_desaparecidas, registrar_barrido
        registrar_barrido([_fila(1, consec=5)], True, ahora=_NOCHE, co_barrido='003')
        registrar_barrido([], True, ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        g = self._G({5: estado})
        clasificar_desaparecidas(g)
        assert _h(1).motivo_salida == motivo and _h(1).estado_siesa_salida == estado

    def test_sin_respuesta_queda_desaparecido_y_se_reintenta(self, db):
        from app.services.pedidos_historia import clasificar_desaparecidas, registrar_barrido
        registrar_barrido([_fila(1, consec=5)], True, ahora=_NOCHE, co_barrido='003')
        registrar_barrido([], True, ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        clasificar_desaparecidas(self._G({}))
        assert _h(1).motivo_salida == 'DESAPARECIDO' and _h(1).estado_siesa_salida is None

    def test_como_mucho_n_consultas_por_ciclo(self, db):
        from app.services.pedidos_historia import clasificar_desaparecidas, registrar_barrido
        registrar_barrido([_fila(i, consec=i) for i in range(1, 9)], True, ahora=_NOCHE,
                          co_barrido='003')
        registrar_barrido([], True, ahora=_NOCHE + timedelta(minutes=1), co_barrido='003')
        g = self._G({})
        clasificar_desaparecidas(g, maximo=3)
        assert len(g.llamadas) == 3


class TestElSyncLaEscribe:
    """Por el sync real, con Siesa falso: la historia recibe la misma bandera
    que el borrado de `pedidos_siesa`."""

    def _sync(self, app, monkeypatch, paginas):
        from app.services import pedidos_sync_service as sync
        from app.services.connekta_gateway import ConnektaGateway
        seq = iter(paginas)

        def _get(self, api, params=None, *a, **k):
            if api == self.api_pedidos:
                return {'detalle': {'Table': next(seq)}}
            return {'detalle': {'Table': []}}
        monkeypatch.setattr(ConnektaGateway, '_get', _get)
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: None)
        sync._run_sync(app)
        return sync._sync_estado['ultimo_resultado'] or {}

    def test_un_rechazo_a_mitad_no_marca_nada(self, app, db, monkeypatch):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1), _fila(500, consec=77)], True, ahora=_NOCHE,
                          co_barrido='003')
        pag1 = [_fila(i, consec=1000 + i) for i in range(100, 200)]
        r = self._sync(app, monkeypatch, [pag1, [{'alerta': 'filtro rechazado'}]])
        assert r.get('paginacion_completa') is False, r
        assert _h(1).salida_at is None and _h(500).salida_at is None
        assert r['historia']['nuevas'] == 100

    def test_un_barrido_completo_si(self, app, db, monkeypatch):
        from app.services.pedidos_historia import registrar_barrido
        registrar_barrido([_fila(1)], True, ahora=_NOCHE, co_barrido='003')
        r = self._sync(app, monkeypatch, [[_fila(2)]])
        assert r.get('paginacion_completa') is True
        assert _h(1).motivo_salida == 'DESAPARECIDO'
        assert _h(2).salida_at is None

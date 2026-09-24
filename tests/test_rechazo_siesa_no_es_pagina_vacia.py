"""Un rechazo de Siesa (`[{'alerta': …}]`) no es una página vacía.

## El defecto (2026-09-24)

Siesa contesta un rechazo —filtro inválido, tamPag excedido, error interno—
con UNA fila de una sola clave, `alerta`, en el lugar de los datos
(`connekta_gateway._alerta_de`). Dos lectores la trataban como datos:

- **`pedidos_sync_service._run_sync`** (cada minuto de 7 a 20 h): la fila
  medía menos que `TAM_PAG`, así que «página corta = última página» marcaba el
  barrido COMPLETO con cero pedidos y **borraba todo `pedidos_siesa`**. El
  operario se quedaba sin nada que pickear y la causa no aparecía en ningún lado.
- **`VigiaService._facturas_de_semana`**: `break` y devolvía lo leído —`[]` en
  la primera página—, así que la semana se escribía en **facturación cero** y
  el CUSUM lo leía como colapso de la plaza. Además, agotar las 99 páginas
  devolvía un total parcial como si fuera el total.

La clase: **un rechazo, o una lectura truncada, leído como «no hay nada»**.
«No hay nada» se escribe; «no sabemos» se declara (Regla 0).
"""
import pytest


def _respuesta(filas):
    return {'detalle': {'Table': filas}}


class TestElSyncDePedidosNoBorraConUnRechazo:

    def _pedido(self, db, consec):
        from app.models.pedido_siesa import PedidoSiesa
        db.session.add(PedidoSiesa(
            tipo_docto='PD', consec_docto=consec, centro_op='003', bodega='NB1',
            numero_pedido=f'PD-{consec}', item_codigo='SKU1', cantidad_pedida=5,
            cantidad_pendiente=5, estado_siesa=3))
        db.session.commit()

    def test_un_rechazo_en_la_primera_pagina_no_borra_nada(self, app, db, monkeypatch):
        from app.models.pedido_siesa import PedidoSiesa
        from app.services import pedidos_sync_service as sync
        from app.services.connekta_gateway import ConnektaGateway
        self._pedido(db, 101)
        self._pedido(db, 102)
        llamadas = []

        def _get(self, *a, **k):
            llamadas.append(a)
            return _respuesta([{'alerta': 'El tamaño de página excede el permitido'}])

        monkeypatch.setattr(ConnektaGateway, '_get', _get)
        sync._run_sync(app)
        assert llamadas, 'el sync no llegó a consultar Siesa: el test no mide nada'
        assert PedidoSiesa.query.count() == 2
        r = sync._sync_estado['ultimo_resultado'] or {}
        assert r.get('paginacion_completa') is False, r

    def test_una_pagina_corta_de_datos_si_cierra_el_barrido(self, app, db, monkeypatch):
        """La otra cara: una respuesta vacía de verdad sigue siendo «no hay
        pedidos» y limpia lo que ya no existe."""
        from app.models.pedido_siesa import PedidoSiesa
        from app.services import pedidos_sync_service as sync
        from app.services.connekta_gateway import ConnektaGateway
        self._pedido(db, 103)
        monkeypatch.setattr(ConnektaGateway, '_get', lambda self, *a, **k: _respuesta([]))
        sync._run_sync(app)
        r = sync._sync_estado['ultimo_resultado'] or {}
        assert r.get('paginacion_completa') is True, r
        assert PedidoSiesa.query.count() == 0


class TestVigiaNoEscribeCeroPorUnRechazo:

    def _llamar(self, respuestas):
        from datetime import date
        from app.services.vigia_service import VigiaService
        it = iter(respuestas)

        class _C:
            def _get(self, *a, **k):
                return next(it)

        return VigiaService._facturas_de_semana(_C(), '003', date(2026, 9, 14), date(2026, 9, 20))

    def test_un_rechazo_es_no_sabemos(self):
        assert self._llamar([_respuesta([{'alerta': 'filtro inválido'}])]) is None

    def test_un_rechazo_a_mitad_no_devuelve_lo_parcial(self):
        pagina = [{'f350_ind_estado': 1, 'f470_cant_base': 1, 'f470_vlr_neto': 10,
                   'f350_id_tipo_docto': 'FE', 'f350_consec_docto': i}
                  for i in range(100)]
        assert self._llamar([_respuesta(pagina), _respuesta([{'alerta': 'x'}])]) is None

    def test_agotar_las_paginas_es_no_sabemos(self):
        pagina = [{'f350_ind_estado': 1, 'f470_cant_base': 1, 'f470_vlr_neto': 10,
                   'f350_id_tipo_docto': 'FE', 'f350_consec_docto': i}
                  for i in range(100)]
        assert self._llamar([_respuesta(pagina)] * 99) is None

    def test_una_semana_sin_facturas_si_es_cero(self):
        assert self._llamar([_respuesta([])]) == []

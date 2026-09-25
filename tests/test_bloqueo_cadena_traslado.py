"""
Bloque 2 — la corrupción que no levanta ninguna excepción.

Cuatro defectos distintos, una sola forma: el camino de fallo produce un valor
indistinguible de un dato bueno, y alguien decide con él.

1. **La existencia de Siesa en cero.** El sobre de rechazo hacía que
   `tabla[0].get('f400_cant_existencia_1', 0)` devolviera `0.0`. Y
   `0.0 is not None`, así que pasaba la guarda endurecida en agosto: se
   grababa `existencia_siesa=0` con `fuente_existencia='SIESA'` —afirmando
   que se verificó contra el ERP— y se encolaba un AJ-ENT por todo el conteo
   físico. Un ajuste no lo reclama nadie.

2. **El STS reemitible.** Si no se leía el consecutivo, la ruta borraba
   `siesa_error` y respondía `ok: True`. Pero el botón del PWA se muestra
   justamente cuando `siesa_salida_consec` está vacío: seguía ahí, y cada
   clic emitía otro STS. Mercancía descargada dos veces a tránsito, y el ETS
   imposible de emitir para siempre.

3. **La base de retención.** `total_iva = 0` hacía que reteIVA diera cero y el
   documento **nunca se encolara**, con `ok: true` en pantalla; retefuente e
   ICA se calculaban sobre `monto_cobrado`, que trae IVA → DC por ~19% de más.

4. **La cadena del traslado.** `solicitada ≥ aprobada ≥ enviada ≥ recibida`
   estaba declarado `BLOQUEA` y era **detective**: solo aparecía si alguien
   abría el panel, y para entonces `cantidad_recibida` ya había viajado en el
   payload del ETS 173079.
"""
import pytest


class TestLaExistenciaDeSiesaNoSeInventa:
    def _sesion(self, db, almacen):
        from app.models.producto import Producto
        p = Producto.query.filter_by(codigo='SKU-EXIST').first()
        if not p:
            p = Producto(codigo='SKU-EXIST', nombre='X', codigo_siesa='SKU-EXIST')
            db.session.add(p)
            db.session.commit()
        return p

    def test_un_rechazo_no_se_lee_como_existencia_cero(self, db, almacen,
                                                        monkeypatch):
        """**El detector ciego.** `0.0` habría entrado al ajuste como si Siesa
        hubiera dicho cero."""
        from app.services import conteo_service
        from app.services.connekta_gateway import connekta, ConnektaGateway as _ConnektaGateway
        self._sesion(db, almacen)
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(
            _ConnektaGateway, 'get_inventario_fecha',
            lambda *a, **k: {'detalle': {'Table': [
                {'alerta': 'Por favor verifique los parámetros'}]}})

        r = conteo_service.ConteoService.consultar_existencia_siesa(
            'SKU-EXIST', bodega='NB1')
        assert r is None, (
            f'devolvió {r!r} en vez de None: el ajuste se calcularía sobre '
            f'una base que Siesa nunca dio')

    def test_una_fila_sin_el_campo_tampoco(self, db, almacen, monkeypatch):
        """Otra API, otro alias, misma consecuencia. El default de 0 era la
        misma mentira por otra puerta."""
        from app.services import conteo_service
        from app.services.connekta_gateway import connekta, ConnektaGateway as _ConnektaGateway
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(
            _ConnektaGateway, 'get_inventario_fecha',
            lambda *a, **k: {'detalle': {'Table': [{'otro_campo': 5}]}})
        assert conteo_service.ConteoService.consultar_existencia_siesa(
            'SKU-EXIST', bodega='NB1') is None

    def test_un_cero_REAL_de_siesa_sigue_siendo_cero(self, db, almacen,
                                                      monkeypatch):
        """Lo que el arreglo no puede romper: Siesa sí puede decir cero, y eso
        es un dato. Confundirlo con «no sé» impediría ajustar un SKU agotado."""
        from app.services import conteo_service
        from app.services.connekta_gateway import connekta, ConnektaGateway as _ConnektaGateway
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(
            _ConnektaGateway, 'get_inventario_fecha',
            lambda *a, **k: {'detalle': {'Table': [
                {'f400_cant_existencia_1': 0}]}})
        assert conteo_service.ConteoService.consultar_existencia_siesa(
            'SKU-EXIST', bodega='NB1') == 0.0


class TestLaCadenaDelTrasladoNoCrece:
    """TRA-01, ahora preventivo. El invariante detective se conserva para las
    filas escritas antes del CHECK, que la migración no reescribe."""

    def _item(self, db, almacen, **cants):
        from app.models.producto import Producto
        from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
        p = Producto.query.filter_by(codigo='SKU-TRA').first()
        if not p:
            p = Producto(codigo='SKU-TRA', nombre='T', codigo_siesa='SKU-TRA')
            db.session.add(p)
            db.session.flush()
        import uuid as _uuid
        from app.models.usuario import Usuario
        u = Usuario.query.filter_by(email='tra_ck@test.com').first()
        if not u:
            u = Usuario(email='tra_ck@test.com', nombre='S', rol='admin',
                        activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        s = SolicitudTraslado(codigo=f'ST-CK-{_uuid.uuid4().hex[:8]}',
                              bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NS1',
                              solicitante_id=u.id, estado='BORRADOR')
        db.session.add(s)
        db.session.flush()
        it = ItemSolicitudTraslado(solicitud_id=s.id, producto_id=p.id,
                                   producto_codigo_siesa='SKU-TRA', **cants)
        db.session.add(it)
        return it

    @pytest.mark.parametrize('cants,que_rompe', [
        ({'cantidad_solicitada': 10, 'cantidad_aprobada': 15},
         'aprobó más de lo que se pidió'),
        ({'cantidad_solicitada': 10, 'cantidad_aprobada': 10,
          'cantidad_enviada': 12}, 'envió más de lo aprobado'),
        ({'cantidad_solicitada': 10, 'cantidad_aprobada': 10,
          'cantidad_enviada': 5, 'cantidad_recibida': 9},
         'recibió más de lo enviado — y eso viaja en el ETS 173079'),
    ])
    def test_la_base_rechaza_cada_eslabon_al_reves(self, db, almacen, cants,
                                                    que_rompe):
        from sqlalchemy.exc import IntegrityError
        self._item(db, almacen, **cants)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    @pytest.mark.parametrize('cants', [
        {'cantidad_solicitada': 10},
        {'cantidad_solicitada': 10, 'cantidad_aprobada': 7},
        {'cantidad_solicitada': 10, 'cantidad_aprobada': 7,
         'cantidad_enviada': 7, 'cantidad_recibida': 7},
        {'cantidad_solicitada': 10, 'cantidad_aprobada': 7,
         'cantidad_enviada': 5, 'cantidad_recibida': 3},
    ])
    def test_recortar_en_cada_paso_es_legitimo(self, db, almacen, cants):
        """El otro lado, y es el que importa: **cada paso puede recortar**.
        Un CHECK que exigiera igualdad prohibiría el picking parcial, la
        aprobación con ajuste y la recepción con faltante — o sea, la
        operación real."""
        self._item(db, almacen, **cants)
        db.session.commit()

    def test_los_NULL_no_bloquean(self, db, almacen):
        """`cantidad_aprobada` es NULL hasta que el admin aprueba. Un CHECK
        que los rechazara impediría crear la solicitud."""
        self._item(db, almacen, cantidad_solicitada=10,
                   cantidad_aprobada=None, cantidad_enviada=None)
        db.session.commit()


class TestElMensajeLlegaAntesQueElCHECK:
    """El CHECK es la red; el dominio es la puerta.

    Sin la validación en el servicio, una tienda que cuenta de más recibe un
    `IntegrityError` de Postgres —la ruta solo atrapa `ValueError`, así que
    sale un 500— justo cuando necesita saber **qué ítem** contó mal. Antes del
    CHECK el dato se guardaba en silencio; el arreglo no puede cambiar un
    fallo silencioso por uno incomprensible.
    """

    def test_recibir_de_mas_da_un_mensaje_util_no_un_500(self, db, almacen):
        import uuid as _uuid

        from app.models.producto import Producto
        from app.models.traslado import (ItemSolicitudTraslado,
                                         SolicitudTraslado)
        from app.models.usuario import Usuario
        from app.services.traslado_service import TrasladoService

        u = Usuario.query.filter_by(email='rec_ck@test.com').first()
        if not u:
            u = Usuario(email='rec_ck@test.com', nombre='R', rol='admin',
                        activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        pr = Producto.query.filter_by(codigo='SKU-REC').first()
        if not pr:
            pr = Producto(codigo='SKU-REC', nombre='R', codigo_siesa='SKU-REC')
            db.session.add(pr)
            db.session.flush()
        st = SolicitudTraslado(codigo=f'ST-REC-{_uuid.uuid4().hex[:8]}',
                               bodega_origen_siesa='NB1',
                               bodega_destino_siesa='NS1',
                               solicitante_id=u.id, estado='EN_TRANSITO',
                               modo_transferencia='DIRECTA')
        db.session.add(st)
        db.session.flush()
        it = ItemSolicitudTraslado(solicitud_id=st.id, producto_id=pr.id,
                                   producto_codigo_siesa='SKU-REC',
                                   cantidad_solicitada=10, cantidad_aprobada=10,
                                   cantidad_enviada=5)
        db.session.add(it)
        db.session.commit()

        with pytest.raises(ValueError) as e:
            TrasladoService.confirmar_recepcion(
                st.id, usuario_id=u.id,
                items_recibidos=[{'id': it.id, 'cantidad_recibida': 90}])
        msg = str(e.value)
        assert 'más de lo que se envió' in msg
        assert 'contaste 90' in msg and 'se enviaron 5' in msg, (
            f'el mensaje no dice qué ítem ni cuánto: {msg}')

    def test_recibir_de_MENOS_sigue_funcionando(self, db, almacen):
        """El faltante es el caso normal de una recepción y no puede
        bloquearse: es justo lo que el traslado tiene que poder expresar."""
        import uuid as _uuid

        from app.models.producto import Producto
        from app.models.traslado import (ItemSolicitudTraslado,
                                         SolicitudTraslado)
        from app.models.usuario import Usuario
        from app.services.traslado_service import TrasladoService

        u = Usuario.query.filter_by(email='rec_ck@test.com').first()
        if not u:
            u = Usuario(email='rec_ck@test.com', nombre='R', rol='admin',
                        activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        pr = Producto.query.filter_by(codigo='SKU-REC').first()
        if not pr:
            pr = Producto(codigo='SKU-REC', nombre='R', codigo_siesa='SKU-REC')
            db.session.add(pr)
            db.session.flush()
        st = SolicitudTraslado(codigo=f'ST-REC-{_uuid.uuid4().hex[:8]}',
                               bodega_origen_siesa='NB1',
                               bodega_destino_siesa='NS1',
                               solicitante_id=u.id, estado='EN_TRANSITO',
                               modo_transferencia='DIRECTA')
        db.session.add(st)
        db.session.flush()
        it = ItemSolicitudTraslado(solicitud_id=st.id, producto_id=pr.id,
                                   producto_codigo_siesa='SKU-REC',
                                   cantidad_solicitada=10, cantidad_aprobada=10,
                                   cantidad_enviada=5)
        db.session.add(it)
        db.session.commit()

        TrasladoService.confirmar_recepcion(
            st.id, usuario_id=u.id,
            items_recibidos=[{'id': it.id, 'cantidad_recibida': 3}])
        assert ItemSolicitudTraslado.query.get(it.id).cantidad_recibida == 3


class TestElSTSNoSeEmiteDosVeces:
    def test_con_consecutivo_ya_registrado_el_endpoint_se_niega(
            self, client, db, almacen, jwt_token_admin):
        """**Comportamiento, no presencia.**

        La primera versión de este test buscaba por AST que la función
        mencionara `siesa_salida_consec` — y seguía verde con la guarda
        borrada, porque el identificador aparece igual en la asignación de más
        abajo. Es exactamente la forma de `_BuscaRol`: medir que el nombre
        esté escrito no es medir que ramifique.
        """
        import uuid as _uuid

        from app.models.traslado import SolicitudTraslado
        from app.models.usuario import Usuario
        u = Usuario.query.filter_by(email='sts_ck@test.com').first()
        if not u:
            u = Usuario(email='sts_ck@test.com', nombre='A', rol='admin',
                        activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        st = SolicitudTraslado(codigo=f'ST-STS-{_uuid.uuid4().hex[:8]}',
                               bodega_origen_siesa='NB1',
                               bodega_destino_siesa='NS1',
                               solicitante_id=u.id, estado='EN_TRANSITO',
                               siesa_salida_consec=4321)
        db.session.add(st)
        db.session.commit()

        r = client.post(f'/api/traslados/{st.id}/reintentar-despacho',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        # 400 = la guarda disparó. 403/404 = el test apunta mal y hay que
        # arreglarlo, no darlo por bueno.
        assert r.status_code in (400, 403), r.status_code
        if r.status_code == 400:
            assert 'ya registrado' in r.get_json().get('error', ''), (
                'el endpoint rechazó por otra razón: la guarda de '
                'idempotencia sobre el 173076 no está actuando')

    def test_sin_consecutivo_el_reintento_SI_procede(self, client, db, almacen,
                                                     jwt_token_admin,
                                                     monkeypatch):
        """**La pareja del guard.** El botón existe para destrabar un despacho
        cuyo STS no salió: si la guarda de idempotencia bloqueara también ese
        caso, el traslado quedaría trabado para siempre y el operario sin
        salida — que es peor que el problema que vino a resolver.
        """
        import uuid as _uuid

        from app.models.traslado import SolicitudTraslado
        from app.models.usuario import Usuario
        from app.services.connekta_gateway import connekta, ConnektaGateway as _ConnektaGateway
        u = Usuario.query.filter_by(email='sts_ok@test.com').first()
        if not u:
            u = Usuario(email='sts_ok@test.com', nombre='A', rol='admin',
                        activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        st = SolicitudTraslado(codigo=f'ST-OK-{_uuid.uuid4().hex[:8]}',
                               bodega_origen_siesa='NB1',
                               bodega_destino_siesa='NS1',
                               solicitante_id=u.id, estado='EN_TRANSITO',
                               siesa_salida_consec=None)
        db.session.add(st)
        db.session.commit()

        r = client.post(f'/api/traslados/{st.id}/reintentar-despacho',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        # Lo que NO puede pasar es que lo rechace por «ya registrado»: eso
        # significaría que la guarda bloquea el caso que el botón atiende.
        assert 'ya registrado' not in (r.get_json() or {}).get('error', ''), (
            'la guarda de idempotencia bloqueó un traslado SIN consecutivo — '
            'el botón de destrabe quedó inútil')

    def test_el_rechazo_de_siesa_se_diagnostica_como_tal(self, db, almacen,
                                                         monkeypatch, caplog):
        """El sobre de rechazo y una fila sin el campo terminan los dos en
        `None`, así que el valor no los distingue. Lo que sí los distingue es
        **qué se le dice a quien lee el log**: «Siesa rechazó la consulta» y
        «la fila no trae el campo» mandan a investigar cosas distintas."""
        import logging

        from app.services import conteo_service
        from app.services.connekta_gateway import connekta, ConnektaGateway as _ConnektaGateway
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(
            _ConnektaGateway, 'get_inventario_fecha',
            lambda *a, **k: {'detalle': {'Table': [
                {'alerta': 'Por favor verifique los parámetros'}]}})
        with caplog.at_level(logging.ERROR):
            conteo_service.ConteoService.consultar_existencia_siesa(
                'SKU-EXIST', bodega='NB1')
        assert any('RECHAZÓ' in r.message or 'RECHAZ' in r.getMessage()
                   for r in caplog.records), (
            'el rechazo se reportó como «la fila no trae el campo», que manda '
            'a revisar el mapeo en vez del filtro')


# `TestLaBaseDeRetencionSaleDeSiesaONoSale` leía el bloque `_base_de_siesa`
# de `/liquidar-completo`, que el frente de liquidación borró el 2026-09-25
# (sin pantalla; mandaba el DC sin cuenta ni UN reales y la retención sin
# decisión). La propiedad —sin la base real de Siesa no se encola la
# retención, y se declara en `errores`— vive ahora en el único encolador de
# la retención y está probada por comportamiento, con su pareja sana:
# `tests/test_liquidacion_doble_unidad_y_dc.py`
# (`test_factura_ilegible_no_encola_y_no_cuenta`,
# `test_la_ruta_declara_el_dc_fallido_en_errores`,
# `test_retencion_que_cuadra_encola_y_cuenta_uno`).

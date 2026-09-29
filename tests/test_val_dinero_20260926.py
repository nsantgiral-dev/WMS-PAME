"""
Validación crítica 2026-09-26 (auditor de la plata, val-dinero). Cada test
reproduce un hallazgo y está marcado xfail(strict=True): pasa a rojo el día
que se arregle, para que alguien quite la marca.
"""
import json
from types import SimpleNamespace

import pytest

from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture
from tests.test_parada_de_oficina import FOTO, _cerrar, _url


def _valor(db, f):
    from app.models.packing import TareaPacking
    return float(db.session.get(TareaPacking, f.packing_id).valor_factura or 0) or 1000.0


def _conductor_confirma(client, app, f, uc, valor, **extra):
    cuerpo = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
              'monto_cobrado': valor, 'version_formulario': 4,
              'observaciones': 'entregado'}
    cuerpo.update(extra)
    return client.post(_url(f), headers=_jwt(app, uc), json=cuerpo)


class TestElLiquidadorNoReescribeLoDelConductor:
    """P1 — `puede_registrar_parada_tardia` (admin + liquidador) abre el mismo
    endpoint de confirmación sobre CUALQUIER parada de una ruta ENTREGADA, no
    solo sobre las sin gestionar. El servicio solo protege lo que registró la
    oficina, no lo que confirmó el conductor. Resultado: el liquidador —que
    NO tiene `puede_corregir_cobro`— reescribe estado, forma de pago y monto
    del conductor (p. ej. EFECTIVO → «no pagó y se quedó») antes de encolar
    el RC. La matriz del dueño reservó «corregir cobro» a admin + líder."""

    def test_el_liquidador_no_convierte_un_cobro_en_efectivo_en_sin_pago(
            self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        v = _valor(db, f)
        r = _conductor_confirma(client, app, f, uc, v)
        assert r.status_code == 200, r.get_json()
        _cerrar(db, f, uc)
        liq = _usuario(db, 'liquidador')
        # Corregir el cobro le está prohibido por la matriz…
        rid = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one().id
        r403 = client.post(f'/api/rutas/{f.ruta_id}/recaudos/{rid}/corregir-monto',
                           headers=_jwt(app, liq), json={'monto': 1, 'razon': 'x'})
        assert r403.status_code == 403
        # …pero por la puerta de la parada tardía lo reescribe entero.
        r = client.post(_url(f), headers=_jwt(app, liq), json={
            'motivo_tardia': 'lo dijo el cliente', 'version_formulario': 4,
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
            'observaciones': 'no pagó', 'foto_entrega': FOTO})
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        # 409 y no 400/403 (arreglo 2026-09-26): con `motivo_tardia` la oficina
        # dice «registro una parada sin gestionar»; que ya esté confirmada es
        # que el conductor se le adelantó mientras llenaba el formulario. Lo que
        # importa es que no pisó nada.
        assert r.status_code in (400, 403, 409) and rec.estado_entrega == 'ENTREGADO'
        # Y si lo pide como corrección, no tiene el permiso.
        r = client.post(_url(f), headers=_jwt(app, liq), json={
            'motivo_correccion': 'lo dijo el cliente', 'version_formulario': 4,
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
            'observaciones': 'no pagó', 'foto_entrega': FOTO})
        assert r.status_code == 403 and RecaudoEntrega.query.filter_by(
            ruta_id=f.ruta_id).one().estado_entrega == 'ENTREGADO'


class TestUnCierreForzadoNoTiraLaConfirmacionReal:
    """P1 — «Cerrar las paradas que faltan» (forzar cierre) crea recaudos
    RECHAZADO sin `registrada_por_oficina`. La confirmación real del
    conductor que llega después por la cola (entregó y cobró) choca con
    `previa is not None` y recibe 400: no entra ni se guarda aparte. La
    pantalla dice lo contrario (rutas.js: «una confirmación que llegue
    después por la cola del conductor entra sola»). La plata cobrada queda
    fuera del WMS y la devolución EN_CAMION termina en FALTANTE contra el
    conductor por mercancía que sí entregó."""

    def test_la_cola_del_conductor_tras_forzar_cierre_se_guarda(self, app, client, db, mundo):
        from app.services.ruta_service import RutaService
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        v = _valor(db, f)
        _cerrar(db, f, uc)
        RutaService.forzar_cierre_ruta(f.ruta_id, ad.id, motivo='sin señal, no se sabe')
        r = _conductor_confirma(client, app, f, uc, v, via_cola=True)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        # Lo mínimo: no se pierde (200 + versión aparte / diferencia marcada).
        assert r.status_code == 200 and (rec.version_conductor or rec.estado_entrega == 'ENTREGADO')


class TestLaRetencionQueNoSalioTieneSalida:
    """P1 — `documentos_pendientes` (lo que decide si la planilla ofrece
    «Enviar a Siesa») solo conoce NC y RC. Si el DC de una retención
    CONFIRMADA no se encoló (Siesa no respondió al leer la factura, o PARCIAL
    sin devolución amarrada: `_encolar_documento_contable` → NO_ENCOLADO,
    «sale en la próxima liquidación»), una vez que el RC está en cola y la NC
    salió no queda ningún botón que lo encole: Liquidación esconde «Registrar
    cobro» con `rc_en_cola` y la planilla esconde «Enviar a Siesa». El aviso
    de la tanda 2 · D se apaga al contar la devolución. La factura queda con
    el saldo de la retención en cartera, sin señal."""

    def test_una_retencion_confirmada_sin_dc_es_un_pendiente(self):
        from app.services import politica_cobro as pc
        rec = SimpleNamespace(
            id=None, estado_entrega='ENTREGADO', monto_cobrado=97500, forma_pago='EFECTIVO',
            siesa_nc_triggered=False, siesa_rc_triggered=True, siesa_dc_triggered=False,
            motivo_descuento='RETEFUENTE_2.5', retencion_confirmada=True, tarea=None)
        assert 'DC' in pc.documentos_pendientes(rec, tarea=SimpleNamespace())


class TestUnaRetencionSinVerificarNoSeCierraReintentando:
    """P1 — la clase de P0-4 («la bandera sola no es idempotente») se cerró
    para el RC pero no para el DC (ni para las NC): el DC que falla sin
    respuesta clara deja la cuenta PUC marcada y el job FALLIDO; «Reintentar»
    (Liquidación, `puede_reintentar_job`) no lo frena
    (`exigir_no_sin_verificar` solo conoce ENTRADA_OC/TRASLADO_AVERIAS/
    AJUSTE_CONTEO) y el ejecutor lo cierra COMPLETADO «idempotente» por la
    bandera, sin que nadie haya mirado Siesa. La pantalla pinta «DC ✓»."""

    def test_reintentar_un_dc_sin_verificar_se_niega(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        f, uc, ad = mundo
        v = _valor(db, f)
        assert _conductor_confirma(client, app, f, uc, v).status_code == 200
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        rec.marcar_puc_enviada('13551501')           # pre-flag puesto, POST sin respuesta
        job = SiesaJob(tipo='DOCUMENTO_CONTABLE_RET', estado='FALLIDO',
                       payload=json.dumps({'recaudo_id': rec.id, 'cuenta_puc': '13551501',
                                           'tipo_retencion': 'RETEFUENTE_2.5'}),
                       referencia_tipo='RecaudoEntrega', referencia_id=rec.id,
                       error_ultimo='DOCUMENTO_CONTABLE_RET: el envío falló sin respuesta clara')
        db.session.add(job)
        db.session.commit()
        with pytest.raises(ValueError):
            sjs.reintentar_job(job.id, usuario_id=ad.id)


class TestUnaRutaLiquidadaEnTransitoNoSeEdita:
    """P2 — `liquidar_ruta` acepta EN_TRANSITO (la planilla ofrece «Liquidar
    Ruta» con todas las paradas gestionadas aunque el camión no haya cerrado)
    y la guarda «ruta liquidada: no se corrige» del servicio vive solo en la
    rama ENTREGADA. Liquidada en tránsito, el conductor sigue reescribiendo
    forma de pago y monto hasta que alguien encola el RC."""

    def test_el_conductor_no_edita_una_parada_de_una_ruta_liquidada(self, app, client, db, mundo):
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        v = _valor(db, f)
        assert _conductor_confirma(client, app, f, uc, v).status_code == 200
        # Arreglo (2026-09-26): liquidar exige la ruta cerrada…
        with pytest.raises(ValueError, match='se liquida cuando el conductor la cierra'):
            RutaService.liquidar_ruta(f.ruta_id, usuario_id=ad.id)
        # …y la guarda de «liquidada» vale en toda edición (una ruta vieja
        # liquidada en tránsito antes del arreglo).
        from app.models.ruta_despacho import RutaDespacho
        db.session.get(RutaDespacho, f.ruta_id).estado_financiero = 'LIQUIDADA'
        db.session.commit()
        r = _conductor_confirma(client, app, f, uc, v, forma_pago='TRANSFERENCIA',
                                referencia_pago='12345678', foto_comprobante=FOTO)
        assert r.status_code == 400


from tests.test_politica_cobro import recaudo  # noqa: E402,F401 — fixture


class TestElMontoDeUnaParcialNoLoCambiaQuienLiquida:
    """P1 — en una PARCIAL, `monto_rc` toma el `monto_override` de quien
    liquida tal cual y `registrar_cobro_recaudo` solo valida la diferencia
    contra lo declarado en ENTREGADO (`_validar_diferencia_declarada`). La
    pantalla ofrece el input editable («Usar Siesa» / «Usar conductor») en
    toda PARCIAL. El liquidador manda el RC por menos de lo que el conductor
    declaró y entregó en efectivo: el faltante queda como saldo del cliente en
    cartera. Es «corregir el cobro», que la matriz reservó a admin + líder."""

    def test_el_rc_de_una_parcial_no_sale_por_menos_de_lo_declarado(self, db, recaudo):
        from unittest.mock import MagicMock, patch
        from tests.test_politica_cobro import LINEA
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='PARCIAL', monto=600000)
        m = MagicMock()
        m.get_rowids_factura.return_value = [dict(LINEA)]
        m.get_pedido_cabecera.return_value = {'f430_id_co': '003', 'f200_id_pedido_fact': '900'}
        m.get_cxc_general.return_value = []
        with patch('app.services.connekta_gateway.connekta', m), \
                patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '1')):
            with pytest.raises(ValueError):
                LiquidacionService.registrar_cobro_recaudo(r.id, admin_id=1,
                                                           monto_override=400000)

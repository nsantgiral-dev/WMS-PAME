"""
El caso en que el cliente se queda con la mercancía y no paga.

Hasta el 2026-08-13 era un **motivo** dentro del estado `RECHAZADO`. El estado
afirmaba que los bultos volvían al camión y el motivo decía lo contrario.

Mientras fue excepcional se podía vivir con eso. El control «si no paga
completo, no se entrega» lo vuelve cotidiano, y entonces **cada consumidor del
estado tiene que acordarse de mirar el motivo**. Alguno se olvida: ya pasó con
la lista de reingreso, que mandaba a bodega a buscar cajas que nunca volvieron.

Decidido por Dirección de Operaciones, en línea con BK-OPS-01 v2.1 §4.2.

## La forma de la solución importa tanto como la decisión

El conductor **no gana un botón**. Sigue contestando la pregunta que sabe
contestar —«¿volvió la mercancía?»— y el servidor traduce eso al estado que
corresponde, **una vez, en la frontera**. Cada consumidor recibe la verdad sin
tener que acordarse de nada: los de hoy y los que se escriban después.
"""
import pytest

from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
from app.services.ruta_service import FormaPago, RutaService


@pytest.fixture
def parada(db, almacen):
    """Ruta EN_TRANSITO con tarea y dos bultos — mínimo para confirmar."""
    import uuid

    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario

    u = Usuario.query.filter_by(email='cond_sinpago@test.com').first()
    if not u:
        u = Usuario(email='cond_sinpago@test.com', nombre='Conductor',
                    rol='conductor', activo=True)
        u.set_password('test123')
        db.session.add(u); db.session.flush()

    ruta = RutaDespacho(conductor_id=u.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
    db.session.add(ruta); db.session.flush()

    tarea = TareaPacking(codigo=f'PK-SP-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=901, numero_pedido_siesa='PED-SP')
    db.session.add(tarea); db.session.flush()

    for i in (1, 2):
        db.session.add(Bulto(tarea_id=tarea.id, ruta_despacho_id=ruta.id,
                             codigo_barras=f'B-{uuid.uuid4().hex[:8]}',
                             tipo='CAJA', numero=i, total=2))
    db.session.commit()
    return ruta, tarea, u


def _confirmar(ruta, tarea, uid, **extra):
    datos = {'estado_entrega': 'RECHAZADO', 'observaciones': 'se la llevó'}
    datos.update(extra)
    return RutaService.confirmar_parada(ruta.id, tarea.id, uid, datos)


class TestDeMotivoAEstado:

    def test_el_motivo_sin_retorno_produce_el_estado_propio(self, db, parada):
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()
        assert r.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO

    def test_un_rechazo_normal_sigue_siendo_rechazo(self, db, parada):
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='CLIENTE_CERRADO')
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()
        assert r.estado_entrega == EstadoEntrega.RECHAZADO

    def test_el_motivo_se_conserva(self, db, parada):
        """El estado dice QUÉ pasó; el motivo, por qué. No se reemplazan."""
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()
        assert r.motivo_rechazo == 'NO_PAGO_SE_QUEDO'

    def test_no_es_un_boton_del_conductor(self, db, parada):
        """La pregunta al conductor sigue siendo «¿volvió la mercancía?», no
        «¿qué estado contable corresponde?». Mandarlo directo se rechaza."""
        ruta, tarea, u = parada
        with pytest.raises(ValueError, match='estado_entrega debe ser'):
            _confirmar(ruta, tarea, u.id, estado_entrega='ENTREGADO_SIN_PAGO',
                       motivo_rechazo='NO_PAGO_SE_QUEDO')


class TestElInventarioNoMiente:

    def test_los_bultos_no_quedan_marcados_como_devueltos(self, db, parada):
        """Marcarlos `RECHAZADO` diría que están en el camión. Es la mentira de
        inventario que este estado vino a impedir."""
        from app.models.bulto import Bulto, EstadoBulto
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        estados = {b.estado for b in Bulto.query.filter_by(tarea_id=tarea.id).all()}
        assert estados == {EstadoBulto.ENTREGADO}

    def test_un_rechazo_de_verdad_si_los_devuelve(self, db, parada):
        from app.models.bulto import Bulto, EstadoBulto
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='CLIENTE_CERRADO')
        estados = {b.estado for b in Bulto.query.filter_by(tarea_id=tarea.id).all()}
        assert estados == {EstadoBulto.RECHAZADO}

    def test_no_entran_a_la_lista_de_reingreso(self, db, parada):
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        r = RutaService.bultos_rechazados(page=1, limit=50)
        assert r['total'] == 0
        # Los DOS bultos de la parada, no solo los que alguien marcó: si el
        # cliente se quedó con la mercancía, se quedó con toda la de esa parada.
        assert r['no_retornados']['total'] == 2


class TestLaBaseImpideLaContradiccion:
    """El CHECK existe porque `confirmar_parada` no es el único camino: un
    script, una carga o un endpoint futuro pueden escribir directo."""

    def test_rechazado_con_motivo_sin_retorno_lo_rechaza_la_base(self, db, parada):
        from sqlalchemy.exc import IntegrityError
        ruta, tarea, _ = parada
        db.session.add(RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=EstadoEntrega.RECHAZADO,
            motivo_rechazo='NO_PAGO_SE_QUEDO', monto_cobrado=0))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_un_estado_inventado_tampoco_entra(self, db, parada):
        from sqlalchemy.exc import IntegrityError
        ruta, tarea, _ = parada
        db.session.add(RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega='ENTREGADO_A_MEDIAS', monto_cobrado=0))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestLaLiquidacionNoInventaDocumentos:

    def test_no_genera_ni_nota_credito_ni_recibo(self, db, parada):
        """Nada volvió que devolver y no entró plata que registrar. La factura
        queda abierta en cartera, que es donde el Gestor la ve."""
        from app.services.liquidacion_service import _procesar_recaudo
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()

        res = _procesar_recaudo(r, 'notas')
        assert res['nc'] == 0 and res['rc'] == 0 and res['dc'] == 0
        assert res['sin_pago'] == 1

    def test_se_cuenta_aparte_de_credito(self, db, parada):
        """Un crédito lo autorizó alguien. Esto no lo autorizó nadie —
        mezclarlos escondería el caso dentro de una cifra que se ve sana."""
        from app.services.liquidacion_service import _procesar_recaudo
        ruta, tarea, u = parada
        _confirmar(ruta, tarea, u.id, motivo_rechazo='NO_PAGO_SE_QUEDO')
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()
        res = _procesar_recaudo(r, 'notas')
        assert res['credito'] == 0


class TestElContadoNoSeConvierteEnCredito:
    """La restricción del punto 4 (BK-OPS-01 §3.4).

    El conductor no tiene facultad de otorgar crédito en la puerta. Si el
    cliente no paga, el camino es el motivo tipificado — que deja documento.

    Se valida contra lo ANOTADO en la tarea, no contra Siesa: la confirmación
    tiene que funcionar sin señal.
    """

    def _con_condicion(self, db, tarea, valor):
        tarea.cond_pago = valor
        db.session.commit()

    @pytest.fixture(autouse=True)
    def _codigos(self, monkeypatch):
        """Los DOS códigos, siempre.

        Fijar solo `cond_pago_ventas` dejaba a `cond_pago_ruta` en lo que
        trajera el entorno —vacío— y ahí C02 y C04 son indistinguibles: todo
        caía a «no se sabe» y **nada se bloqueaba**. La versión anterior de
        `test_credito_sobre_parada_a_credito_pasa` sembraba C02 y pasaba en
        verde por esa rama, no por la política que decía probar.
        """
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01')
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02')

    @pytest.mark.parametrize('cond,etiqueta', [
        ('C01', 'contado de mostrador'),
        ('C02', 'ruta — el conductor cobra y el RC salda'),
    ])
    def test_credito_donde_habia_que_cobrar_se_rechaza(self, db, parada, cond, etiqueta):
        """**C02 es el caso que importa**, y hasta el 2026-08-14 no bloqueaba.

        La restricción preguntaba por contado documental, y toda venta de ruta
        es C02 → 'credito' → no bloqueaba nunca. El control estaba en verde
        sobre la única población a la que se aplica.
        """
        ruta, tarea, u = parada
        self._con_condicion(db, tarea, cond)
        with pytest.raises(ValueError, match='se cobra en la entrega'):
            RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
                'estado_entrega': 'ENTREGADO',
                'forma_pago': FormaPago.CREDITO})

    def test_credito_sobre_credito_real_pasa(self, db, parada):
        """C04 a 30 días: entregar y firmar sin cobrar es lo correcto."""
        ruta, tarea, u = parada
        self._con_condicion(db, tarea, 'C04')
        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': FormaPago.CREDITO})
        r = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()
        assert r.forma_pago == FormaPago.CREDITO

    def test_sin_condicion_anotada_no_bloquea(self, db, parada):
        """Regla 0. No saber no es evidencia, y una parada trabada en la calle
        no la desbloquea nadie."""
        ruta, tarea, u = parada
        assert tarea.cond_pago is None
        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': FormaPago.CREDITO})

    def test_sin_la_condicion_de_ruta_configurada_no_bloquea(self, db, parada, monkeypatch):
        """El costo declarado de la degradación.

        Sin `SIESA_COND_PAGO_RUTA`, C02 y C04 no se distinguen y la restricción
        deja de proteger. Se prefiere eso a bloquear a ciegas —una parada
        trabada en la calle no la desbloquea nadie— pero queda escrito acá para
        que nadie lo descubra en producción.
        """
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ruta', '')
        ruta, tarea, u = parada
        self._con_condicion(db, tarea, 'C02')
        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': FormaPago.CREDITO})

    def test_efectivo_sobre_contado_pasa(self, db, parada):
        """La restricción es sobre CREDITO, no sobre cobrar."""
        ruta, tarea, u = parada
        self._con_condicion(db, tarea, 'C01')
        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': FormaPago.EFECTIVO,
            'monto_cobrado': 1000})


class TestLaPantallaNoOfreceCobrarLoQueNoSeCobro:
    """El defecto que este estado destapa en el cliente.

    `liquidacion.js` mostraba «Registrar Cobro (RC)» cuando la parada no era
    CRÉDITO, no era EXENTO y no era RECHAZADO. `ENTREGADO_SIN_PAGO` no es
    ninguna de las tres —su `forma_pago` es `null`— así que **el botón
    aparecía**: la pantalla ofrecía registrar plata que nadie recibió.

    Es el mismo hueco que ya apareció con `modo_pantalla`: el servidor decide
    bien y el navegador no se entera. Por eso el guard mira el JS.
    """

    import pathlib as _pl
    _JS = _pl.Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa' / 'liquidacion.js'

    def test_el_boton_de_cobro_excluye_el_estado(self):
        fuente = self._JS.read_text(encoding='utf-8')
        i = fuente.find("liqToggleCobro")
        assert i != -1, '¿se renombró el botón de cobro?'
        guarda = fuente[max(0, i - 700):i]
        assert "estado !== 'ENTREGADO_SIN_PAGO'" in guarda, (
            '\nLa pantalla volvió a ofrecer «Registrar Cobro» sobre una parada '
            'que no pagó. Su forma_pago es null, así que las exclusiones por '
            'CREDITO/EXENTO no la atrapan.')

    def test_no_se_pinta_como_rechazo(self):
        """Un rechazo dice que la mercancía volvió. Reusar ese bloque haría que
        quien liquida creyera que hay algo que reingresar."""
        fuente = self._JS.read_text(encoding='utf-8')
        assert "estado === 'ENTREGADO_SIN_PAGO'" in fuente
        assert 'ENTREGADO SIN PAGO' in fuente


class TestElClienteNoPuedeContradecirElEstado:
    """Lo que una mutación destapó.

    La rama que vacía `bultos_rechazados_ids` parecía código muerto: sin ella,
    los tests seguían en verde. Y es cierto **mientras el conductor no mande
    bultos marcados** — el default ya es lista vacía.

    Pero la app sí los manda: el checklist de «bulto rechazado» viaja en el
    payload. Con la rama apagada, una parada `ENTREGADO_SIN_PAGO` con bultos
    tildados los dejaba en `RECHAZADO`, o sea declarando que volvieron al
    camión. La rama no era muerta: le faltaba el test que la ejerciera.
    """

    def test_los_bultos_marcados_por_el_conductor_se_ignoran(self, db, parada):
        from app.models.bulto import Bulto, EstadoBulto
        ruta, tarea, u = parada
        ids = [b.id for b in Bulto.query.filter_by(tarea_id=tarea.id).all()]

        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'RECHAZADO',
            'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
            'observaciones': 'se la llevó',
            'bultos_rechazados': ids,          # ← el conductor los tildó
        })

        estados = {b.estado for b in Bulto.query.filter_by(tarea_id=tarea.id).all()}
        assert estados == {EstadoBulto.ENTREGADO}, (
            'un bulto que se quedó con el cliente quedó marcado como devuelto: '
            'el inventario dice que está en el camión y no está')

    def test_en_un_rechazo_normal_si_se_respetan(self, db, parada):
        from app.models.bulto import Bulto, EstadoBulto
        ruta, tarea, u = parada
        ids = [b.id for b in Bulto.query.filter_by(tarea_id=tarea.id).all()]
        RutaService.confirmar_parada(ruta.id, tarea.id, u.id, {
            'estado_entrega': 'RECHAZADO',
            'motivo_rechazo': 'CLIENTE_CERRADO',
            'observaciones': 'cerrado',
            'bultos_rechazados': ids[:1],
        })
        estados = sorted(b.estado for b in Bulto.query.filter_by(tarea_id=tarea.id).all())
        assert estados == sorted([EstadoBulto.RECHAZADO, EstadoBulto.ENTREGADO])

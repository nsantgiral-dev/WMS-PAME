"""
Ventas durante un mismo conteo, y traslados que entran mientras se cuenta.

## El defecto

Desde m028 el conteo se compara contra la foto de Siesa tomada **al confirmar**
(`registrar_conteo`). Pero el físico no se cuenta en ese instante: se cuenta
entre que el operario abre la tarea y la confirma. Si en ese intervalo Siesa se
mueve —un cliente con el producto en la canasta paga en caja; una unidad ya
contada se vende; se acumula el POS—, el físico y la foto miden instantes
distintos y el conteo fabrica un sobrante o un faltante que no existe. Si pasa
igual en CC1 y CC2 se auto-ajusta mal; si pasa en el CC3 (definitivo) llega a
Siesa con firma de supervisor.

Y un traslado ENTRANTE a la bodega que se cuenta —en tránsito, o recibido hace
poco con las cajas sin abrir— produce un faltante falso que la salida sin
confirmar no delata, porque es una entrada.

## La clase, no el caso

1. *«El físico se mide en un intervalo y Siesa en un instante»* — todo sitio que
   ABRE un conteo toma la foto de inicio. Trinquete por AST:
   `TestTodaAperturaTomaFotoDeInicio`.
2. *«Un ajuste sale de un conteo que no se midió entre dos fotos»* — los caminos
   que ajustan sin exigir la foto de inicio están declarados, con su motivo, y
   la lista solo encoge. Trinquete: `TestAjustesSinFotoDeInicioDeclarados`.

## Los casos (con fotos simuladas y los servicios reales)

A–C: movimientos ENTRE conteos — ya los resolvía m028, quedan con nombre.
D–F: movimientos DURANTE un conteo (CC1, CC2, CC3).
G:   sin foto de inicio. H: traslado entrante. I: sesiones anteriores a m029.
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from tests.test_conteo_teorico_pos import SKU, _jobs, _un_job, siesa, tienda  # noqa: F401 (fixtures)

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _sesion(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


def _nuevo_cc1(tienda):
    from app.models.conteo import SesionConteo
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
    return SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id


def _abrir(sid, operario):
    return _svc().obtener_tarea_operario(sid, operario.id)


def _contar(sid, operario, fisico):
    return _svc().registrar_conteo(sid, operario.id, fisico)


def _cc1_limpio(tienda, siesa, fisico, existencia, pos):
    """CC1 abierto y cerrado con la misma foto: sin movimiento durante."""
    siesa.poner(existencia=existencia, pos=pos)
    cc1 = _nuevo_cc1(tienda)
    _abrir(cc1, tienda['a'])
    return cc1, _contar(cc1, tienda['a'], fisico)


# ─────────────────────────────────────────────────────────────────────────────
# A–C · movimientos ENTRE CC1 y CC2 — se comparan diferencias, cada una contra
# su propia foto. Cada conteo, por dentro, está limpio.
# ─────────────────────────────────────────────────────────────────────────────

class TestMovimientosEntreConteos:

    def test_a_venta_pos_entre_cc1_y_cc2(self, db, siesa, tienda):
        """El caso que preguntó el usuario. CC1: existencia 10, POS 2, físico 7
        (teórico 8, −1). Se vende 1 por caja. CC2: existencia 10, POS 3,
        físico 6 (teórico 7, −1). Coinciden: ajuste −1, sin tercer conteo."""
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        siesa.poner(existencia=10, pos=3)                     # la venta, entre conteos
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 6)
        assert r2['resultado'] == 'DESCUADRE' and r2['auto_encolado'] is True, r2
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_b_devolucion_pos_entre_cc1_y_cc2(self, db, siesa, tienda):
        """Un cliente devuelve 1 en caja: el POS baja de 2 a 1 y la unidad
        vuelve al estante. CC1 7 contra teórico 8 (−1); CC2 8 contra teórico 9
        (−1). Coinciden: ajuste −1."""
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        siesa.poner(existencia=10, pos=1)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 8)
        assert r2['auto_encolado'] is True, r2
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_c_acumulacion_entre_cc1_y_cc2(self, db, siesa, tienda):
        """Siesa acumula el POS: existencia 10 → 7, POS 3 → 0. El teórico no
        cambia (7) y nada se movió del estante. CC1 y CC2 cuentan 6: −1 los dos."""
        cc1, r1 = _cc1_limpio(tienda, siesa, 6, existencia=10, pos=3)
        siesa.poner(existencia=7, pos=0)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 6)
        assert r2['auto_encolado'] is True, r2
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)
        raiz = _sesion(db, cc1)
        assert (raiz.existencia_siesa, raiz.cant_pos_siesa, raiz.teorico_siesa) == (7, 0, 7)


# ─────────────────────────────────────────────────────────────────────────────
# D–F · movimientos DURANTE un conteo — el conteo se descarta y se recuenta
# ─────────────────────────────────────────────────────────────────────────────

class TestVentaDuranteElConteo:

    def test_d_venta_durante_cc1_pide_recontar(self, db, siesa, tienda):
        """CC1 se abre con existencia 10 / POS 2. Mientras cuenta, se vende 1
        por caja (POS 3). Cuenta 7. Contra el cierre (teórico 7) eso daría
        **MATCH** — un faltante real de 1 escondido por la venta. No: el conteo
        se descarta, la misma sesión queda abierta para recontar, y no nace
        ningún CC2 ni ningún ajuste."""
        from app.models.conteo import EstadoConteo
        siesa.poner(existencia=10, pos=2)
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        siesa.poner(existencia=10, pos=3)                     # la venta, DURANTE
        r = _contar(cc1, tienda['a'], 7)

        assert r['resultado'] == 'RECONTAR', r
        assert 'cant_pos 2→3' in r['movimiento']
        s = _sesion(db, cc1)
        assert s.estado == EstadoConteo.EN_PROCESO
        assert s.operario_id == tienda['a'].id
        assert s.cantidad_fisica is None and s.diferencia is None
        assert s.hijo_conteo is None, 'un conteo contaminado no crea CC2'
        assert _jobs(cc1) == []
        # Lo contado no se pierde: queda para auditoría con las dos fotos.
        (desc,) = s.lista_conteos_descartados()
        assert desc['cantidad_fisica'] == 7
        assert (desc['inicio']['cant_pos'], desc['cierre']['cant_pos']) == (2, 3)
        assert s.to_dict()['conteos_descartados'][0]['movimiento'] == r['movimiento']
        # La foto del cierre descartado es la de inicio del recuento.
        assert (s.existencia_inicio_siesa, s.cant_pos_inicio_siesa) == (10, 3)

        # El recuento, con Siesa quieta, sí vale: 6 contra teórico 7 = −1.
        r2 = _contar(cc1, tienda['a'], 6)
        assert r2['resultado'] == 'SEGUNDO_CONTEO', r2
        assert _sesion(db, cc1).diferencia == -1

    def test_d_acumulacion_durante_el_conteo_tambien_recuenta(self, db, siesa, tienda):
        """La acumulación deja el teórico igual, pero se compara campo por
        campo: una venta y una devolución que se cancelan en el teórico
        también movieron mercancía mientras se contaba. Recontar cuesta un
        conteo; creer una foto que no midió lo mismo cuesta un ajuste."""
        siesa.poner(existencia=10, pos=3)
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        siesa.poner(existencia=7, pos=0)
        assert _contar(cc1, tienda['a'], 6)['resultado'] == 'RECONTAR'

    def test_d_remision_que_aparece_durante_el_conteo(self, db, siesa, tienda):
        """Una salida sin confirmar que no es POS aparece mientras se cuenta
        (alguien está sacando mercancía para una remisión): recontar."""
        siesa.poner(existencia=10, pos=2)
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        siesa.poner(existencia=10, pos=2, salida_sin_conf=5)
        assert _contar(cc1, tienda['a'], 5)['resultado'] == 'RECONTAR'

    def test_e_venta_durante_cc2_no_auto_ajusta(self, db, siesa, tienda):
        """CC1 limpio (−1). CC2 se abre con POS 2, se vende 1 mientras cuenta
        y cuenta 6: contra el cierre (teórico 7) sería −1 y «coincidiría» con
        CC1. No: CC2 se recuenta y CC1 sigue esperando."""
        from app.models.conteo import EstadoConteo
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        cc2 = r1['segundo_conteo_id']
        _abrir(cc2, tienda['b'])
        siesa.poner(existencia=10, pos=3)                     # DURANTE CC2
        r2 = _contar(cc2, tienda['b'], 6)
        assert r2['resultado'] == 'RECONTAR', r2
        assert _jobs(cc1) == []
        assert _sesion(db, cc1).estado == EstadoConteo.SEGUNDO_CONTEO
        # Recontado con Siesa quieta: ahora sí confirma a CC1.
        r2b = _contar(cc2, tienda['b'], 6)
        assert r2b['auto_encolado'] is True, r2b
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_f_venta_durante_cc3_no_se_aprueba(self, db, siesa, tienda):
        """El definitivo. CC1 −1, CC2 −3 → CC3. El supervisor abre el CC3, se
        vende 1 mientras cuenta: el CC3 se recuenta, la raíz sigue en
        TERCER_CONTEO y no hay ajuste que aprobar."""
        from app.models.conteo import EstadoConteo
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 5)
        assert r2['resultado'] == 'TERCER_CONTEO'
        cc3, sup = r2['tercer_conteo_id'], tienda['supervisor']
        _abrir(cc3, sup)
        siesa.poner(existencia=10, pos=3)                     # DURANTE CC3
        r3 = _contar(cc3, sup, 6)
        assert r3['resultado'] == 'RECONTAR', r3
        assert _sesion(db, cc1).estado == EstadoConteo.TERCER_CONTEO
        with pytest.raises(ValueError, match='DESCUADRE'):
            _svc().confirmar_ajuste(cc1, sup.id)
        db.session.rollback()
        assert _jobs(cc1) == []
        # El recuento del CC3 con Siesa quieta sí resuelve y se aprueba.
        r3b = _contar(cc3, sup, 6)
        assert r3b['resultado'] == 'DESCUADRE' and r3b['ajuste_bloqueado'] is None, r3b
        _svc().confirmar_ajuste(cc1, sup.id)
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_el_recuento_llega_por_el_camino_movil(self, db, siesa, tienda):
        """`/api/mobile/confirmar` (CONTEO) devuelve el RECONTAR tal cual: la
        pantalla del operario lo muestra y vuelve a pedir la tarea, que es la
        misma, con el contador en cero."""
        from app.services.mobile_service import MobileService
        siesa.poner(existencia=10, pos=2)
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        _sesion(db, cc1).cantidad_fisica = 7
        db.session.commit()
        siesa.poner(existencia=10, pos=3)
        r = MobileService.confirmar_tarea(tienda['a'].id, cc1, 'CONTEO', total_contado=7)
        assert r['resultado'] == 'RECONTAR'
        assert _sesion(db, cc1).cantidad_fisica is None


# ─────────────────────────────────────────────────────────────────────────────
# G · sin foto de inicio
# ─────────────────────────────────────────────────────────────────────────────

class TestSinFotoDeInicio:

    def test_g_siesa_no_responde_al_abrir_la_apertura_no_falla(self, db, siesa, tienda):
        from app.models.conteo import EstadoConteo
        siesa.fila = None                                     # Siesa sin respuesta
        cc1 = _nuevo_cc1(tienda)
        vista = _abrir(cc1, tienda['a'])
        assert vista['id'] == cc1
        s = _sesion(db, cc1)
        assert s.estado == EstadoConteo.EN_PROCESO and s.foto_inicio_at is None

    def test_g_ni_una_excepcion_rompe_la_apertura(self, db, siesa, tienda, monkeypatch):
        from app.models.conteo import EstadoConteo
        from app.services.conteo_service import ConteoService

        def revienta(*a, **k):
            raise RuntimeError('lo que sea')
        monkeypatch.setattr(ConteoService, 'consultar_foto_siesa', staticmethod(revienta))
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        s = _sesion(db, cc1)
        assert s.estado == EstadoConteo.EN_PROCESO and s.foto_inicio_at is None

    def test_g_sin_foto_de_inicio_en_cc2_no_ajusta(self, db, siesa, tienda):
        """CC1 limpio. Al abrir CC2 Siesa no responde; al cerrarlo sí. CC2
        coincide con CC1, pero el ajuste no sale: ni solo ni aprobado."""
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        siesa.fila = None
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        siesa.poner(existencia=10, pos=2)
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert r2['resultado'] == 'DESCUADRE' and r2['auto_encolado'] is False, r2
        assert 'APERTURA' in (r2['ajuste_bloqueado'] or '')
        assert _jobs(cc1) == []
        assert 'APERTURA' in _sesion(db, cc1).to_dict()['bloqueo_ajuste']
        with pytest.raises(ValueError, match='APERTURA'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        db.session.rollback()
        assert _jobs(cc1) == []

    def test_g_sin_foto_de_inicio_en_cc1_no_auto_ajusta(self, db, siesa, tienda):
        """CC1 sin foto de inicio decide el segundo conteo, pero no AVALA un
        ajuste automático. CC2 limpio coincide: queda para el supervisor, que
        sí puede aprobarlo — un conteo limpio más una firma, como el CC3."""
        siesa.fila = None
        cc1 = _nuevo_cc1(tienda)
        _abrir(cc1, tienda['a'])
        siesa.poner(existencia=10, pos=2)
        r1 = _contar(cc1, tienda['a'], 7)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert r2['auto_encolado'] is False and r2['ajuste_bloqueado'] is None, r2
        assert 'aprobación manual' in r2['mensaje']
        assert _jobs(cc1) == []
        _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        p = _un_job(cc1)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_g_sin_foto_de_inicio_en_cc3_no_se_aprueba(self, db, siesa, tienda):
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 5)
        cc3, sup = r2['tercer_conteo_id'], tienda['supervisor']
        siesa.fila = None
        _abrir(cc3, sup)
        siesa.poner(existencia=10, pos=2)
        r3 = _contar(cc3, sup, 7)
        assert r3['resultado'] == 'DESCUADRE'
        assert 'APERTURA' in (r3['ajuste_bloqueado'] or '')
        with pytest.raises(ValueError, match='APERTURA'):
            _svc().confirmar_ajuste(cc1, sup.id)
        db.session.rollback()
        assert _jobs(cc1) == []


# ─────────────────────────────────────────────────────────────────────────────
# Las aperturas del camino móvil también toman la foto
# ─────────────────────────────────────────────────────────────────────────────

class TestAperturasMoviles:

    def test_conteo_preasignado(self, db, siesa, tienda):
        from app.services.mobile_service import MobileService
        siesa.poner(existencia=10, pos=2)
        _svc().crear_conteo_manual(tienda['almacen'].id, SKU, operario_id=tienda['a'].id)
        d = MobileService._get_conteo_preassignado(tienda['a'].id)
        s = _sesion(db, d['id'])
        assert s.foto_inicio_at is not None and s.cant_pos_inicio_siesa == 2

    def test_dispatcher_de_tienda(self, db, siesa, tienda):
        from app.services.mobile_service import MobileService
        siesa.poner(existencia=10, pos=2)
        cc1 = _nuevo_cc1(tienda)
        d = MobileService._next_conteo_tienda(tienda['a'].id, tienda['almacen'].id)
        assert d['id'] == cc1
        assert _sesion(db, cc1).existencia_inicio_siesa == 10

    def test_dispatcher_general(self, db, siesa, tienda):
        from app.services.mobile_service import MobileService
        siesa.poner(existencia=10, pos=2)
        cc1 = _nuevo_cc1(tienda)
        d = MobileService.get_tarea_actual(tienda['a'].id)
        assert d and d['id'] == cc1, d
        assert _sesion(db, cc1).foto_inicio_at is not None


# ─────────────────────────────────────────────────────────────────────────────
# H · traslado entrante
# ─────────────────────────────────────────────────────────────────────────────

# 10 a.m. del 23 en Bogotá. La ventana (hoy o ayer) arranca el 22 a las 00:00
# de Bogotá = 2026-09-22 05:00 UTC.
INSTANTE = datetime(2026, 9, 23, 15, 0)


def _producto_otro(db):
    from app.models.producto import Producto
    p = Producto(codigo='OTROSKU', nombre='Otro', codigo_siesa='OTROSKU',
                 unidad_negocio_id='001')
    db.session.add(p)
    db.session.flush()
    return p


def _traslado(db, tienda, *, n, estado='EN_TRANSITO', destino='NS1', producto=None,
              despacho=INSTANTE - timedelta(hours=3), entrega=None):
    from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
    s = SolicitudTraslado(codigo=f'ST-TEST-{n}', bodega_origen_siesa='NB1',
                          bodega_destino_siesa=destino, estado=estado,
                          solicitante_id=tienda['supervisor'].id,
                          fecha_despacho=despacho, fecha_entrega=entrega)
    db.session.add(s)
    db.session.flush()
    prod = producto or tienda['producto']
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=prod.id, producto_codigo_siesa=prod.codigo_siesa,
        cantidad_solicitada=5, cantidad_enviada=5))
    db.session.commit()
    return s


def _sesion_contada(db, tienda, instante=INSTANTE):
    """Una sesión contada limpia en `instante`: dos fotos iguales, sin salidas
    no POS. Lo único que puede bloquearla es el traslado."""
    from app.models.conteo import SesionConteo
    s = SesionConteo(
        codigo=f'CC-TR-{instante:%H%M%S%f}', tipo='MANUAL', estado='DESCUADRE',
        ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
        producto_id=tienda['producto'].id, producto_codigo_siesa=SKU,
        cantidad_fisica=5, fuente_existencia='SIESA',
        existencia_siesa=10, cant_pos_siesa=0, salida_sin_conf_siesa=0, teorico_siesa=10,
        foto_siesa_at=instante,
        existencia_inicio_siesa=10, cant_pos_inicio_siesa=0,
        salida_sin_conf_inicio_siesa=0, foto_inicio_at=instante - timedelta(minutes=10),
        diferencia=-5)
    db.session.add(s)
    db.session.commit()
    return s


class TestTrasladoEntrante:

    @pytest.mark.parametrize('kw', [
        dict(estado='EN_TRANSITO'),
        dict(estado='EN_TRANSITO', despacho=None),                       # sin fecha: vivo
        dict(estado='ENTREGADA', entrega=INSTANTE - timedelta(hours=2)),   # hoy
        dict(estado='ENTREGADA', entrega=datetime(2026, 9, 22, 23, 0)),    # ayer, 6 p.m.
        dict(estado='ENTREGADA', entrega=INSTANTE + timedelta(hours=5)),   # en camino al contar
        dict(estado='ENTREGADA', entrega=None),                            # sin fecha: vivo
    ], ids=['en_transito', 'en_transito_sin_fecha', 'recibido_hoy', 'recibido_ayer',
            'recibido_despues_del_conteo', 'entregado_sin_fecha'])
    def test_h_bloquea(self, db, tienda, kw):
        _traslado(db, tienda, n=1, **kw)
        s = _sesion_contada(db, tienda)
        assert _svc().traslado_entrante_vivo(s)
        bloqueo = _svc().motivo_bloqueo_ajuste(s)
        assert 'traslado entrando' in (bloqueo or ''), bloqueo
        assert 'ST-TEST-1' in bloqueo

    @pytest.mark.parametrize('kw', [
        dict(estado='ENTREGADA', entrega=datetime(2026, 9, 20, 15, 0)),    # tres días
        # 23:00 del 21 en Bogotá, aunque en UTC ya sea el 22: fuera de la ventana.
        dict(estado='ENTREGADA', entrega=datetime(2026, 9, 22, 4, 0)),
        dict(estado='EN_TRANSITO', destino='NC1'),                         # otra bodega
        dict(estado='EN_TRANSITO', despacho=INSTANTE + timedelta(hours=1)),  # después
        dict(estado='PREPARADO', despacho=None),                           # no ha salido
        dict(estado='CANCELADA', despacho=None),
    ], ids=['recibido_hace_tres_dias', 'recibido_anteayer_hora_bogota', 'otra_bodega',
            'despachado_despues_del_conteo', 'preparado', 'cancelado'])
    def test_h_no_bloquea(self, db, tienda, kw):
        _traslado(db, tienda, n=2, **kw)
        s = _sesion_contada(db, tienda)
        assert _svc().traslado_entrante_vivo(s) is None
        assert _svc().motivo_bloqueo_ajuste(s) is None

    def test_h_otro_sku_no_bloquea(self, db, tienda):
        _traslado(db, tienda, n=3, producto=_producto_otro(db))
        s = _sesion_contada(db, tienda)
        assert _svc().motivo_bloqueo_ajuste(s) is None

    def test_h_se_juzga_contra_el_instante_del_conteo_no_contra_ahora(self, db, tienda):
        """Aprobar días después no cambia la respuesta: el delta se fijó al
        contar, y ahí el traslado estaba entrando."""
        _traslado(db, tienda, n=4, estado='ENTREGADA', entrega=INSTANTE + timedelta(days=1))
        s = _sesion_contada(db, tienda)
        assert _svc().traslado_entrante_vivo(s)

    def test_h_de_punta_a_punta_no_auto_ajusta_ni_se_aprueba(self, db, siesa, tienda):
        ahora = datetime.utcnow()
        _traslado(db, tienda, n=5, despacho=ahora - timedelta(hours=1))
        cc1, r1 = _cc1_limpio(tienda, siesa, 7, existencia=10, pos=2)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert r2['auto_encolado'] is False, r2
        assert 'traslado' in (r2['ajuste_bloqueado'] or '')
        with pytest.raises(ValueError, match='traslado'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        db.session.rollback()
        assert _jobs(cc1) == []

    def test_h_la_auditoria_de_picking_tambien_lo_respeta(self, db, siesa, tienda, usuario_admin):
        """El camino que no exige foto de inicio sigue exigiendo lo demás."""
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.models.picking import EstadoPicking, TareaPicking
        from app.services.picking_service import PickingService
        _traslado(db, tienda, n=6, despacho=datetime.utcnow() - timedelta(hours=1))
        t = TareaPicking(codigo='PICK-TR', producto_id=tienda['producto'].id,
                         cantidad_solicitada=5, cantidad_recogida=0,
                         ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
                         estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='FALTANTE')
        tienda['registro'].bloqueado = 5
        db.session.add(t)
        db.session.commit()
        siesa.poner(existencia=10, pos=2)
        PickingService.auditar_tarea(t.id, admin_id=usuario_admin.id,
                                     resultado='ENCONTRADO_COMPLETO')
        db.session.commit()
        s = SesionConteo.query.filter_by(tarea_picking_id=t.id).one()
        assert s.estado == EstadoConteo.DESCUADRE
        assert _jobs(s.id) == []


# ─────────────────────────────────────────────────────────────────────────────
# I · sesiones anteriores a m029 (sin columnas de inicio)
# ─────────────────────────────────────────────────────────────────────────────

class TestSesionesViejas:
    """Decisión documentada en m029: NULL es «no se sabe». Una sesión vieja se
    puede terminar de contar y decide MATCH / segundo conteo como siempre, pero
    no ajusta; una vieja en DESCUADRE ya no se aprueba: se recuenta."""

    def _vieja(self, db, tienda, **kw):
        from app.models.conteo import SesionConteo
        base = dict(codigo='CC-VIEJA', tipo='MANUAL', ubicacion_id=tienda['ubicacion'].id,
                    almacen_id=tienda['almacen'].id, producto_id=tienda['producto'].id,
                    producto_codigo_siesa=SKU, operario_id=tienda['a'].id,
                    fecha_inicio=datetime.utcnow() - timedelta(minutes=30))
        base.update(kw)
        s = SesionConteo(**base)
        db.session.add(s)
        db.session.commit()
        return s.id

    def test_i_en_proceso_se_termina_y_decide_segundo_conteo(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=2)
        cc1 = self._vieja(db, tienda, estado='EN_PROCESO')
        r1 = _contar(cc1, tienda['a'], 7)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        # Y no la avala sola: CC2 limpio coincide, pero va al supervisor.
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert r2['auto_encolado'] is False and _jobs(cc1) == []

    def test_i_en_proceso_puede_dar_match(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=2)
        cc1 = self._vieja(db, tienda, estado='EN_PROCESO')
        assert _contar(cc1, tienda['a'], 8)['resultado'] == 'MATCH'

    def test_i_reabrir_una_tarea_ya_abierta_no_le_inventa_foto(self, db, siesa, tienda):
        """Una tarea EN_PROCESO pudo haber empezado a contarse: una foto
        tomada ahora no sería la de su apertura."""
        siesa.poner(existencia=10, pos=2)
        cc1 = self._vieja(db, tienda, estado='EN_PROCESO')
        lecturas = siesa.lecturas
        _abrir(cc1, tienda['a'])
        assert siesa.lecturas == lecturas
        assert _sesion(db, cc1).foto_inicio_at is None

    def test_i_descuadre_viejo_ya_no_se_aprueba(self, db, tienda):
        cc1 = self._vieja(db, tienda, estado='DESCUADRE', cantidad_fisica=7,
                          fuente_existencia='SIESA', existencia_siesa=10, cant_pos_siesa=2,
                          salida_sin_conf_siesa=2, teorico_siesa=8,
                          foto_siesa_at=datetime.utcnow(), diferencia=-1)
        with pytest.raises(ValueError, match='APERTURA'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        db.session.rollback()
        assert _jobs(cc1) == []


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete 1 — toda apertura de un conteo toma la foto de inicio
# ─────────────────────────────────────────────────────────────────────────────

#: Las funciones que TOMAN la foto de inicio. Una apertura que no llama a
#: ninguna deja un conteo sin foto de inicio que nadie pidió: no va a poder
#: ajustar, y nadie sabe por qué.
_TOMAN_FOTO = {'registrar_foto_inicio', '_grabar_foto_inicio'}

#: Aperturas (`estado = EstadoConteo.EN_PROCESO`, o una `SesionConteo(...)` que
#: nace EN_PROCESO) que NO toman la foto de inicio, con su motivo. Vacío: hoy
#: todas la toman. La clave es `archivo::Clase.funcion`.
APERTURAS_SIN_FOTO_DECLARADAS = {}


def _alias_de_estado_conteo(arbol):
    """Los nombres con que un módulo se refiere a `EstadoConteo`."""
    alias = {'EstadoConteo'}
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom) and n.module == 'app.models.conteo':
            for a in n.names:
                if a.name == 'EstadoConteo':
                    alias.add(a.asname or a.name)
    return alias


def _es_en_proceso_de_conteo(valor, alias):
    return (isinstance(valor, ast.Attribute) and valor.attr == 'EN_PROCESO'
            and isinstance(valor.value, ast.Name) and valor.value.id in alias)


def _aperturas(base=None):
    """`({archivo::funcion: toma_foto}, archivos)` de cada función de `app/`
    que abre un conteo.

    Abre un conteo: `x.estado = EstadoConteo.EN_PROCESO` (o con el alias con
    que el módulo lo importó), o `SesionConteo(..., estado=...EN_PROCESO | 'EN_PROCESO')`.

    NO ve un `x.estado = 'EN_PROCESO'` con el literal suelto: sin tipos no se
    sabe si `x` es un conteo o un picking. Hoy no hay ninguno en `app/`
    (medido el 2026-09-23); el día que aparezca, este detector no lo ve.
    """
    base = base or RAIZ
    hallados, archivos = {}, 0
    for f in sorted((base / 'app').rglob('*.py')):
        rel = str(f.relative_to(base))
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        archivos += 1
        alias = _alias_de_estado_conteo(arbol)

        def llama_foto(func):
            for n in ast.walk(func):
                if isinstance(n, ast.Call):
                    nombre = (n.func.attr if isinstance(n.func, ast.Attribute)
                              else getattr(n.func, 'id', None))
                    if nombre in _TOMAN_FOTO:
                        return True
            return False

        def abre(nodo):
            for n in ast.walk(nodo):
                if isinstance(n, ast.Assign) and any(
                        isinstance(t, ast.Attribute) and t.attr == 'estado'
                        for t in n.targets) and _es_en_proceso_de_conteo(n.value, alias):
                    return True
                if (isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'SesionConteo'):
                    for kw in n.keywords:
                        if kw.arg == 'estado' and (
                                _es_en_proceso_de_conteo(kw.value, alias)
                                or (isinstance(kw.value, ast.Constant)
                                    and kw.value.value == 'EN_PROCESO')):
                            return True
            return False

        def visitar(nodo, pila):
            for hijo in ast.iter_child_nodes(nodo):
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Solo el cuerpo propio: una función anidada es otra clave.
                    propio = ast.Module(body=[b for b in hijo.body if not isinstance(
                        b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
                        type_ignores=[])
                    if abre(propio):
                        clave = f'{rel}::{".".join(pila + [hijo.name])}'
                        hallados[clave] = llama_foto(propio)
                    visitar(hijo, pila + [hijo.name])
                elif isinstance(hijo, ast.ClassDef):
                    visitar(hijo, pila + [hijo.name])
                else:
                    visitar(hijo, pila)

        visitar(arbol, [])
    return hallados, archivos


class TestTodaAperturaTomaFotoDeInicio:

    def test_ninguna_apertura_sin_foto(self):
        hallados, _ = _aperturas()
        sin_foto = {k for k, toma in hallados.items()
                    if not toma and k not in APERTURAS_SIN_FOTO_DECLARADAS}
        assert not sin_foto, (
            f'\nAperturas de conteo que no toman la foto de inicio: {sin_foto}\n'
            'Llamá ConteoService.registrar_foto_inicio(sesion) después del commit '
            'que la pasa a EN_PROCESO. Sin ella el conteo no puede saber si hubo '
            'ventas mientras se contaba, y no va a poder ajustar.')

    def test_la_lista_solo_encoge(self):
        hallados, _ = _aperturas()
        sobran = [k for k in APERTURAS_SIN_FOTO_DECLARADAS
                  if k not in hallados or hallados[k]]
        assert not sobran, f'Declaradas que ya no aplican: {sobran}. Sacarlas.'

    def test_toda_declaracion_dice_por_que(self):
        assert not [k for k, m in APERTURAS_SIN_FOTO_DECLARADAS.items() if len(m) < 80]

    def test_piso_de_aperturas(self):
        """Hoy: obtener_tarea_operario, los tres despachadores móviles y el
        recuento. Un detector roto devuelve cero aperturas y cero hallazgos."""
        hallados, archivos = _aperturas()
        assert archivos >= 100
        assert len(hallados) >= 5, hallados
        assert 'app/services/conteo_service.py::ConteoService.obtener_tarea_operario' in hallados


class TestElDetectorDeAperturasMuerde:

    def _en(self, fuente, tmp_path):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _aperturas(base=tmp_path)[0]

    @pytest.mark.parametrize('fuente', [
        'def f(s):\n    s.estado = EstadoConteo.EN_PROCESO\n',
        ('from app.models.conteo import EstadoConteo as _EC\n'
         'def f(s):\n    s.estado = _EC.EN_PROCESO\n'),
        "def f():\n    return SesionConteo(codigo='X', estado='EN_PROCESO')\n",
        'class M:\n    def f(s):\n        s.estado = EstadoConteo.EN_PROCESO\n',
    ])
    def test_ve_una_apertura_sin_foto(self, fuente, tmp_path):
        hallados = self._en(fuente, tmp_path)
        assert hallados and not any(hallados.values()), (fuente, hallados)

    def test_ve_que_la_apertura_si_toma_la_foto(self, tmp_path):
        src = ('def f(s):\n    s.estado = EstadoConteo.EN_PROCESO\n'
               '    db.session.commit()\n    ConteoService.registrar_foto_inicio(s)\n')
        assert self._en(src, tmp_path) == {'app/x.py::f': True}

    def test_la_foto_de_una_funcion_anidada_no_cuenta_para_la_de_afuera(self, tmp_path):
        src = ('def f(s):\n    s.estado = EstadoConteo.EN_PROCESO\n'
               '    def g():\n        registrar_foto_inicio(s)\n')
        assert self._en(src, tmp_path) == {'app/x.py::f': False}

    @pytest.mark.parametrize('fuente', [
        't.estado = EstadoPicking.EN_PROCESO\n',
        'def f(t):\n    t.estado = EstadoPicking.EN_PROCESO\n',
        'def f(s):\n    """s.estado = EstadoConteo.EN_PROCESO"""\n',
        'def f(s):\n    # s.estado = EstadoConteo.EN_PROCESO\n    pass\n',
        'def f(s):\n    return s.estado == EstadoConteo.EN_PROCESO\n',
    ])
    def test_no_marca_lo_que_no_es(self, fuente, tmp_path):
        assert self._en(fuente, tmp_path) == {}, fuente


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete 2 — los ajustes que no exigen foto de inicio, declarados
# ─────────────────────────────────────────────────────────────────────────────

#: Cada función que pide un ajuste SIN exigir la foto de inicio
#: (`exige_foto_inicio=` algo que no es `True`), con su motivo.
AJUSTES_SIN_FOTO_DE_INICIO = {
    'app/services/conteo_service.py::ConteoService.ajustar_desde_auditoria_picking': (
        'La auditoría de picking no tiene apertura que fotografiar: el supervisor '
        'cuenta y resuelve en un solo gesto, y la foto se toma al resolver. Hueco '
        'conocido — una venta de caja durante ese conteo no se ve. El camino vivo '
        'de la auditoría (ENCONTRADO) ya no ajusta: fuerza un conteo cíclico, que '
        'sí tiene apertura; este queda por ENCONTRADO_COMPLETO/PARCIAL.'),
}

_PIDEN_AJUSTE = {'motivo_bloqueo_ajuste', '_encolar_ajuste_fisico'}


def _sin_foto_de_inicio(base=None):
    base = base or RAIZ
    hallados = {}
    for f in sorted((base / 'app').rglob('*.py')):
        rel = str(f.relative_to(base))
        arbol = ast.parse(f.read_text(encoding='utf-8'))

        def visitar(nodo, pila):
            for hijo in ast.iter_child_nodes(nodo):
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visitar(hijo, pila + [hijo.name])
                    continue
                if isinstance(hijo, ast.Call):
                    nombre = (hijo.func.attr if isinstance(hijo.func, ast.Attribute)
                              else getattr(hijo.func, 'id', None))
                    if nombre in _PIDEN_AJUSTE:
                        for kw in hijo.keywords:
                            # `exige_foto_inicio=exige_foto_inicio` reenvía el
                            # parámetro propio: el hueco, si lo hay, está en quien
                            # llama a esta función, y ahí se mide.
                            reenvia = (isinstance(kw.value, ast.Name)
                                       and kw.value.id == 'exige_foto_inicio')
                            if kw.arg == 'exige_foto_inicio' and not reenvia and not (
                                    isinstance(kw.value, ast.Constant) and kw.value.value is True):
                                clave = f'{rel}::{".".join(pila) or "<modulo>"}'
                                hallados.setdefault(clave, []).append(hijo.lineno)
                visitar(hijo, pila)

        visitar(arbol, [])
    return hallados


class TestAjustesSinFotoDeInicioDeclarados:

    def test_ninguno_sin_declarar(self):
        nuevos = {k: v for k, v in _sin_foto_de_inicio().items()
                  if k not in AJUSTES_SIN_FOTO_DE_INICIO}
        assert not nuevos, (
            f'\nAjustes que no exigen la foto de inicio: {nuevos}\n'
            'Un conteo que no se midió entre dos fotos no sabe si hubo ventas '
            'mientras se contaba. Declaralo con su motivo o exigí la foto.')

    def test_la_lista_solo_encoge(self):
        hallados = _sin_foto_de_inicio()
        sobran = [k for k in AJUSTES_SIN_FOTO_DE_INICIO if k not in hallados]
        assert not sobran, f'Declarados que ya exigen la foto: {sobran}. Sacarlos.'

    def test_toda_declaracion_dice_por_que(self):
        assert not [k for k, m in AJUSTES_SIN_FOTO_DE_INICIO.items() if len(m) < 80]

    def test_el_default_exige_la_foto(self):
        """Olvidarse del argumento tiene que ser el lado seguro."""
        import inspect
        from app.services.conteo_service import ConteoService
        for fn in (ConteoService.motivo_bloqueo_ajuste, ConteoService._encolar_ajuste_fisico):
            assert inspect.signature(fn).parameters['exige_foto_inicio'].default is True


class TestElDetectorDeAjustesMuerde:

    def _en(self, fuente, tmp_path):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return set(_sin_foto_de_inicio(base=tmp_path))

    @pytest.mark.parametrize('fuente', [
        'def f(s):\n    motivo_bloqueo_ajuste(s, exige_foto_inicio=False)\n',
        'def f(s, x):\n    C._encolar_ajuste_fisico(s, exige_foto_inicio=x)\n',
        'def f(s):\n    C.motivo_bloqueo_ajuste(s, exige_foto_inicio=0)\n',
    ])
    def test_ve_el_hueco(self, fuente, tmp_path):
        assert self._en(fuente, tmp_path) == {'app/x.py::f'}

    @pytest.mark.parametrize('fuente', [
        'def f(s):\n    C.motivo_bloqueo_ajuste(s)\n',
        'def f(s):\n    C.motivo_bloqueo_ajuste(s, exige_foto_inicio=True)\n',
        'def f(s):\n    """motivo_bloqueo_ajuste(s, exige_foto_inicio=False)"""\n',
        'def f(s):\n    otra_cosa(s, exige_foto_inicio=False)\n',
        ('def f(s, exige_foto_inicio=True):\n'
         '    C.motivo_bloqueo_ajuste(s, exige_foto_inicio=exige_foto_inicio)\n'),
    ])
    def test_no_marca_lo_que_no_es(self, fuente, tmp_path):
        assert self._en(fuente, tmp_path) == set(), fuente

    def test_piso(self):
        assert len(_sin_foto_de_inicio()) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# PWA — el aviso de cajas POS sale en cada sitio donde un conteo empieza
# ─────────────────────────────────────────────────────────────────────────────
#
# Una caja que vende sin conexión no sube `f400_cant_pos_1` a Siesa central
# hasta sincronizar: el teórico queda alto, el conteo ve un faltante falso, se
# ajusta, y la acumulación posterior descuenta otra vez. Ninguna foto de Siesa
# ve una venta que todavía no le llegó, así que se le pide a quien cuenta que
# lo confirme. El texto vive en `conteo.js` (`AVISO_CAJAS_POS`) y cada pantalla
# llama `avisoCajasPosHtml()`.
#
# Qué es «un sitio donde un conteo empieza», medido sobre el JS con el mismo
# troceo y grafo de llamadas de `test_frontend_integrity.py`:
#   · un HUD de conteo: la función que pinta «CONTEO CIEGO»;
#   · una apertura: la función que pide la tarea a contar
#     (`/api/mobile/tarea-actual`, `/api/conteo/<id>/tarea`) — tiene que
#     LLEGAR, por el grafo, a una función que pinta el aviso;
#   · las entradas declaradas abajo (el formulario de conteo manual), que no
#     tienen una marca propia que un detector pueda ver.

import re  # noqa: E402

from tests.test_frontend_integrity import (  # noqa: E402
    _referencias, _sin_comentarios, _trocear)

_PWA = RAIZ / 'app' / 'static' / 'pwa'
_MARCA_HUD = 'CONTEO CIEGO'
_APERTURA = re.compile(r"/api/mobile/tarea-actual|/api/conteo/\$\{[^}]*\}/tarea\b")
_LLAMA_AVISO = re.compile(r'(?<![.\w$])avisoCajasPosHtml\s*\(')

#: Entradas sin marca detectable, con por qué son un inicio de conteo.
ENTRADAS_DE_CONTEO_DECLARADAS = {
    'conteosMostrarFormManual': (
        'Formulario de conteo manual: quien lo lanza suele ser el jefe de la '
        'tienda que va a contar o a mandar a contar ya mismo.'),
}


def _cuerpos(fuentes):
    cuerpos = {}
    for f in sorted(fuentes):
        c, _ = _trocear(fuentes[f])
        for nombre, cuerpo in c.items():
            cuerpos[nombre] = cuerpos.get(nombre, '') + '\n' + _sin_comentarios(cuerpo)
    return cuerpos


def _llega_al_aviso(cuerpos, fn):
    vistos, pendientes = set(), [fn]
    while pendientes:
        f = pendientes.pop()
        if f in vistos or f not in cuerpos:
            continue
        vistos.add(f)
        if _LLAMA_AVISO.search(cuerpos[f]):
            return True
        pendientes += [n for n in _referencias(cuerpos[f]) if n in cuerpos]
    return False


def _inicios_de_conteo(fuentes):
    """`{funcion: (tipo, muestra_el_aviso)}` de cada inicio de conteo."""
    cuerpos = _cuerpos(fuentes)
    hallados = {}
    for fn, cuerpo in cuerpos.items():
        if fn == 'avisoCajasPosHtml':
            continue
        if _MARCA_HUD in cuerpo or fn in ENTRADAS_DE_CONTEO_DECLARADAS:
            hallados[fn] = ('hud' if _MARCA_HUD in cuerpo else 'declarada',
                            bool(_LLAMA_AVISO.search(cuerpo)))
        elif _APERTURA.search(cuerpo):
            hallados[fn] = ('apertura', _llega_al_aviso(cuerpos, fn))
    return hallados


def _fuentes_pwa():
    return {f.name: f.read_text(encoding='utf-8') for f in sorted(_PWA.glob('*.js'))}


class TestAvisoCajasPosEnCadaInicioDeConteo:

    def test_cada_inicio_de_conteo_muestra_el_aviso(self):
        sin_aviso = {fn: t for fn, (t, ok) in _inicios_de_conteo(_fuentes_pwa()).items()
                     if not ok}
        assert not sin_aviso, (
            f'\nInicios de conteo sin el aviso de cajas POS: {sin_aviso}\n'
            'Llamá avisoCajasPosHtml() (conteo.js) en la pantalla donde se empieza '
            'a contar. Una caja caída deja el POS sin subir y el conteo ajusta un '
            'faltante que la acumulación vuelve a descontar.')

    def test_piso_y_puntos_conocidos(self):
        """Un troceo roto devuelve cero inicios y cero hallazgos: verde falso."""
        h = _inicios_de_conteo(_fuentes_pwa())
        # Un solo HUD de conteo desde P0-HUD (2026-09-23): `conteoHudHtml` lo
        # pinta para el operario (vía renderTarea) y para el Definitivo.
        assert {'conteoHudHtml', 'pedirTarea', 'defAbrirConteo',
                'conteosMostrarFormManual'} <= set(h), h
        assert h['conteoHudHtml'][0] == 'hud'
        assert h['pedirTarea'][0] == 'apertura' and h['defAbrirConteo'][0] == 'apertura'

    def test_las_declaradas_existen_y_dicen_por_que(self):
        cuerpos = _cuerpos(_fuentes_pwa())
        assert set(ENTRADAS_DE_CONTEO_DECLARADAS) <= set(cuerpos)
        assert not [k for k, m in ENTRADAS_DE_CONTEO_DECLARADAS.items() if len(m) < 40]

    def test_el_formulario_manual_tiene_donde_pintarlo(self):
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        form = html[html.index('id="conteo-form-manual"'):]
        form = form[:form.index('crearConteoManual()')]
        assert 'id="conteo-manual-aviso-pos"' in form

    def test_el_texto_vive_en_un_solo_sitio(self):
        """Una constante, no N copias que divergen."""
        frase = 'cajas POS de la tienda'
        apariciones = sum(f.read_text(encoding='utf-8').count(frase)
                          for f in list(_PWA.glob('*.js')) + [_PWA / 'index.html'])
        assert apariciones == 1, apariciones

    def test_el_html_generado_dice_lo_pedido(self):
        """Se evalúa la función real con Node, sin stubs."""
        import shutil
        import subprocess
        if not shutil.which('node'):
            pytest.skip('sin node')
        src = (_PWA / 'conteo.js').read_text(encoding='utf-8')
        ini = src.index('const AVISO_CAJAS_POS')
        fin = src.index('\n}\n', src.index('function avisoCajasPosHtml')) + 3
        programa = src[ini:fin] + '\nprocess.stdout.write(avisoCajasPosHtml());'
        salida = subprocess.run(['node', '-e', programa], capture_output=True,
                                text=True, check=True).stdout
        assert ('Antes de contar: confirma que todas las cajas POS de la tienda están en '
                'línea y al día — que la última venta de cada caja ya aparezca en Siesa '
                'central. Si una caja estuvo caída, cuenta después de que sincronice.') in salida
        assert 'confirm(' not in salida


class TestElDetectorDelAvisoMuerde:

    def test_ve_un_hud_sin_aviso(self):
        src = 'function f(t) { el.innerHTML = `<div>CONTEO CIEGO</div>`; }\n'
        assert _inicios_de_conteo({'x.js': src}) == {'f': ('hud', False)}

    def test_un_aviso_en_un_comentario_no_cuenta(self):
        src = ('function f(t) {\n  // avisoCajasPosHtml()\n'
               '  el.innerHTML = `<div>CONTEO CIEGO</div>`;\n}\n')
        assert _inicios_de_conteo({'x.js': src}) == {'f': ('hud', False)}

    def test_ve_el_hud_con_aviso(self):
        src = ('function avisoCajasPosHtml() { return "x"; }\n'
               'function f(t) { el.innerHTML = `${avisoCajasPosHtml()}<div>CONTEO CIEGO</div>`; }\n')
        assert _inicios_de_conteo({'x.js': src}) == {'f': ('hud', True)}

    def test_una_apertura_que_no_llega_al_aviso(self):
        src = ('async function abrir(id) { const t = await get(`/api/conteo/${id}/tarea`); pintar(t); }\n'
               'function pintar(t) { el.innerHTML = `<b>${esc(t.x)}</b>`; }\n')
        assert _inicios_de_conteo({'x.js': src}) == {'abrir': ('apertura', False)}

    def test_una_apertura_que_llega_por_el_grafo(self):
        src = ('function avisoCajasPosHtml() { return "x"; }\n'
               'async function abrir(id) { const t = await get(`/api/conteo/${id}/tarea`); pintar(t); }\n'
               'function pintar(t) { el.innerHTML = `${avisoCajasPosHtml()}<b>CONTEO CIEGO</b>`; }\n')
        h = _inicios_de_conteo({'x.js': src})
        assert h == {'abrir': ('apertura', True), 'pintar': ('hud', True)}

    def test_no_marca_lo_que_no_es(self):
        src = ('function f() { return get("/api/conteo/definitivos"); }\n'
               'function g() { /* CONTEO CIEGO */ return 1; }\n')
        assert _inicios_de_conteo({'x.js': src}) == {}

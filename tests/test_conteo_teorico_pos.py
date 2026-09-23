"""
El conteo cíclico se mide contra el **teórico** de Siesa, y el delta se fija al
contar.

## El defecto (medido el 2026-09-23 contra Siesa QA)

`API_v2_Inventarios_InvFecha` trae por ítem × bodega, además de la existencia,
la venta de caja que Siesa todavía no acumuló: `f400_cant_pos_1`. En 600 de 600
filas con POS pendiente (NS1, NC1, PC1, FC1, FN1, NB1) la salida sin confirmar
era exactamente igual al POS: **el POS pendiente vive dentro de la salida sin
confirmar**.

El conteo comparaba el físico contra `f400_cant_existencia_1` sola. En una
tienda con POS pendiente eso fabrica un faltante del tamaño del POS: la
mercancía ya se fue por caja, la existencia todavía la cuenta. CC1 == CC2 lo
ajustaba solo, y al día siguiente la acumulación del POS descontaba otra vez.
**Doble descuento.**

Y el ajuste se recalculaba al aprobar contra la existencia de ESE momento: dos
instantes en una resta. Conteo 95 contra 100; sale una remisión de 10 → 90; al
aprobar 95 − 90 = **+5 AJ-ENT**, cuando lo contado era un faltante de 5.

## La clase, no el caso

1. *«La base de un conteo es la existencia cruda de Siesa»* — cualquier resta
   contra `existencia_siesa` es la misma forma. Trinquete por AST:
   `TestNingunaRestaContraLaExistenciaCruda`.
2. *«La base del ajuste se lee en un instante distinto al del conteo»* — toda
   lectura de la existencia de Siesa está declarada, con su motivo, y ninguna
   vive en el camino de aprobación. Trinquete: `TestLecturasDeExistenciaDeclaradas`.

Los dos llevan meta-tests (lo que deben ver, lo que no, un piso) y la mutación
de la fórmula está hecha y verificada: ver el PR.
"""
import ast
import json
import pathlib

import pytest
from werkzeug.security import generate_password_hash

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SKU = 'POSITEM'


# ─────────────────────────────────────────────────────────────────────────────
# Siesa de mentira — a nivel de la API cruda, no de la foto
# ─────────────────────────────────────────────────────────────────────────────

class SiesaFalsa:
    """Una fila de `API_v2_Inventarios_InvFecha` que el test mueve a voluntad.

    Se stubea `get_inventario_fecha` —la respuesta HTTP ya decodificada— y no
    `consultar_foto_siesa`: así el test ejerce los nombres de campo reales
    (`f400_cant_pos_1`, `f400_cant_salida_sin_conf_1`) y la regla de campo
    ausente. Stubear la foto probaría el stub.
    """

    def __init__(self):
        self.fila = None
        self.lecturas = 0

    def poner(self, existencia, pos=0, salida_sin_conf=None, comprometida=0,
              quitar=()):
        ssc = pos if salida_sin_conf is None else salida_sin_conf
        fila = {
            'f120_referencia': SKU, 'f150_id': 'NS1',
            'f400_cant_existencia_1': float(existencia),
            'f400_cant_comprometida_1': float(comprometida),
            'f400_cant_salida_sin_conf_1': float(ssc),
            'f400_cant_pos_1': float(pos),
            'f400_id_lote': '', 'f400_id_ubicacion_aux': None,
        }
        for campo in quitar:
            fila.pop(campo)
        self.fila = fila

    def respuesta(self, *_a, **_k):
        self.lecturas += 1
        return {'detalle': {'Table': [dict(self.fila)] if self.fila else []}}


@pytest.fixture
def siesa(monkeypatch):
    """Parcheado sobre la CLASE (CLAUDE.md, refactor del gateway): un método
    parcheado en la instancia `connekta` sobrevive al teardown y deja sordos
    los parches por clase de otros archivos. `modo_simulacion` es config, no
    método: ése sí va en la instancia.

    Y por la misma razón se quita cualquier sombra que haya dejado otro
    archivo: con una sola que quede en `connekta.__dict__`, este parche por
    clase no se ve y el test habla con la Siesa de OTRO test. Pasó en la suite
    completa (`test_bloqueo_cadena_traslado` parcheaba la instancia; ya no)."""
    from app.services.connekta_gateway import ConnektaGateway, connekta
    falsa = SiesaFalsa()
    if 'get_inventario_fecha' in vars(connekta):
        monkeypatch.delattr(connekta, 'get_inventario_fecha')
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.setattr(ConnektaGateway, 'get_inventario_fecha',
                        lambda self, codigo, bodega=None: falsa.respuesta(codigo, bodega))
    return falsa


@pytest.fixture
def tienda(db, almacen):
    """Una tienda (NS1, CO 001) con un SKU, un hueco y dos contadores pares."""
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    from app.models.usuario import Usuario

    almacen.bodega_siesa_id = 'NS1'
    almacen.centro_op_siesa = '001'
    producto = Producto(codigo=SKU, nombre='Item con POS', codigo_siesa=SKU,
                        unidad_negocio_id='001')
    db.session.add(producto)
    db.session.flush()
    ub = Ubicacion(codigo='POS-UB', almacen_id=almacen.id, tipo_zona='GENERAL',
                   stock_minimo=0, stock_maximo=9999, secuencia_ruteo=1, activo=True)
    db.session.add(ub)
    db.session.flush()
    reg = UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                            cantidad=10, reservado=0, bloqueado=0)
    db.session.add(reg)

    def _actor(email, rol):
        u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                    rol=rol, puede_picar=True, almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.flush()
        return u

    mundo = {
        'almacen': almacen, 'producto': producto, 'ubicacion': ub, 'registro': reg,
        'a': _actor('pos-a@test.com', 'operario'),
        'b': _actor('pos-b@test.com', 'operario'),
        'supervisor': _actor('pos-sup@test.com', 'supervisor'),
    }
    db.session.commit()
    return mundo


def _cc1(tienda, fisico):
    from app.models.conteo import SesionConteo
    from app.services.conteo_service import ConteoService
    creado = ConteoService.crear_conteo_manual(tienda['almacen'].id, SKU)
    cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one()
    ConteoService.obtener_tarea_operario(cc1.id, tienda['a'].id)
    # El operario DECLARA lo que contó, cero incluido («revisé y no hay»).
    r1 = ConteoService.registrar_conteo(cc1.id, tienda['a'].id, fisico, cero_confirmado=True)
    return cc1.id, r1


def _cc2(tienda, r1, fisico):
    from app.services.conteo_service import ConteoService
    cc2_id = r1['segundo_conteo_id']
    ConteoService.obtener_tarea_operario(cc2_id, tienda['b'].id)
    return ConteoService.registrar_conteo(cc2_id, tienda['b'].id, fisico, cero_confirmado=True)


def _jobs(sesion_id):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_tipo='SesionConteo',
                                    referencia_id=sesion_id).all()


def _un_job(sesion_id):
    jobs = _jobs(sesion_id)
    assert len(jobs) == 1, f'se esperaba un AJUSTE_CONTEO, hay {len(jobs)}'
    return json.loads(jobs[0].payload)


# ─────────────────────────────────────────────────────────────────────────────
# Los casos del problema
# ─────────────────────────────────────────────────────────────────────────────

class TestLaBaseEsElTeorico:

    def test_a_pos_pendiente_no_fabrica_faltante(self, db, siesa, tienda):
        """Existencia 10, POS 2, físico 7 → ajuste −1 (AJ-SAL 1).

        Con la existencia cruda habría sido 7 − 10 = −3: los 2 del POS se
        habrían descontado acá y otra vez al acumularse el POS."""
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=10, pos=2)
        cc1_id, r1 = _cc1(tienda, 7)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        r2 = _cc2(tienda, r1, 7)
        assert r2['auto_encolado'] is True, r2

        raiz = db.session.get(SesionConteo, cc1_id)
        assert raiz.existencia_siesa == 10
        assert raiz.cant_pos_siesa == 2
        assert raiz.teorico_siesa == 8
        assert raiz.diferencia == -1
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)
        viejo = 7 - raiz.existencia_siesa
        assert viejo == -3 and p['cantidad'] != abs(viejo)

    def test_a_llega_asi_al_142951(self, db, siesa, tienda, monkeypatch):
        """El payload no miente sobre lo que viaja: se corre el job real y se
        mira qué recibe `enviar_ajuste_inventario`."""
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_job_service import _ejecutar_job
        enviados = []
        monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                            lambda self, **kw: enviados.append(kw) or {'codigo': 0})
        siesa.poner(existencia=10, pos=2)
        cc1_id, r1 = _cc1(tienda, 7)
        _cc2(tienda, r1, 7)
        job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=cc1_id).one()
        _ejecutar_job(job)
        assert len(enviados) == 1
        assert (enviados[0]['motivo_codigo'], enviados[0]['cantidad'],
                enviados[0]['bodega']) == ('AJ-SAL', 1, 'NS1')

    def test_b_existencia_cero_con_pos_es_sobrante(self, db, siesa, tienda):
        """Existencia 0, POS 1, físico 0 → teórico −1 → ajuste +1 (AJ-ENT 1).

        Cuando el POS acumule, Siesa quedará en 0 + 1 − 1 = 0, que es lo que
        hay. Con la existencia cruda: 0 − 0 = MATCH, y Siesa terminaría en −1."""
        siesa.poner(existencia=0, pos=1)
        cc1_id, r1 = _cc1(tienda, 0)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        r2 = _cc2(tienda, r1, 0)
        assert r2['auto_encolado'] is True, r2
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-ENT', 1)

    def test_c_sin_pos_no_cambia_nada(self, db, siesa, tienda):
        """NB1 típico: sin POS, el teórico ES la existencia y el ajuste es el
        de siempre."""
        siesa.poner(existencia=100, pos=0)
        cc1_id, r1 = _cc1(tienda, 95)
        r2 = _cc2(tienda, r1, 95)
        assert r2['auto_encolado'] is True, r2
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 5)

    def test_c_sin_pos_el_match_sigue_siendo_match(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        _cc1_id, r1 = _cc1(tienda, 10)
        assert r1['resultado'] == 'MATCH'

    def test_comprometida_no_entra_al_teorico(self, db, siesa, tienda):
        """Un pedido comprometido sigue en el estante: el contador lo ve."""
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=10, pos=2, comprometida=5)
        foto = ConteoService.consultar_foto_siesa(SKU, 'NS1')
        assert foto['teorico'] == 8
        assert foto['comprometida'] == 5


class TestPOSMayorQueLaExistencia:
    """El teórico da NEGATIVO, y no es un dato inventado.

    Medido contra Siesa QA el 2026-09-23: `PAPELSP9218` en PC1 viene con
    `existencia=140` y `cant_pos=152`. Teórico = **−12**. Siesa dice que en el
    estante debería haber menos doce unidades, que es físicamente imposible.

    No es un error de Siesa que haya que corregir acá: es el estado normal de
    una tienda que vendió por caja más de lo que el acumulado alcanzó a
    descontar. Cuando el POS se acumule, la existencia va a quedar en −12 sola.

    Por eso la fórmula **no debe recortar el negativo a cero**. Si el estante
    está vacío y se ajusta `+12`, Siesa queda en 152; al acumularse los 152 del
    POS queda en 0, que es la verdad. Recortar a cero dejaría la existencia en
    140, el acumulado la llevaría a −12, y el faltante reaparecería mañana.

    Este caso no estaba cubierto: de los 47 tests del archivo, ninguno ponía
    `pos > existencia`.
    """

    def test_el_teorico_puede_ser_negativo(self):
        """La fórmula no recorta. Es una resta, y el signo es información."""
        from app.services.conteo_service import ConteoService
        assert ConteoService.teorico(140, 152) == -12

    def test_el_estante_vacio_contra_teorico_negativo_es_sobrante(
            self, db, siesa, tienda):
        """Existencia 140, POS 152, físico 0 → ajuste **+12** (AJ-ENT).

        El código viejo habría mandado −140 (AJ-SAL de 140), vaciando en Siesa
        una bodega que el acumulado del POS iba a vaciar igual: doble descuento
        de 140 unidades."""
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=140, pos=152)
        cc1_id, r1 = _cc1(tienda, 0)
        r2 = _cc2(tienda, r1, 0)
        assert r2['auto_encolado'] is True, r2

        raiz = db.session.get(SesionConteo, cc1_id)
        assert raiz.existencia_siesa == 140
        assert raiz.cant_pos_siesa == 152
        assert raiz.teorico_siesa == -12, (
            'el teórico se recortó: un POS mayor que la existencia deja de '
            'verse y el faltante vuelve mañana')
        assert raiz.diferencia == 12

        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-ENT', 12)
        viejo = 0 - raiz.existencia_siesa
        assert viejo == -140 and p['cantidad'] != abs(viejo)

    def test_contar_lo_que_el_pos_ya_vendio_no_fabrica_entrada(
            self, db, siesa, tienda):
        """La otra dirección: si el físico coincide con el teórico negativo no
        hay nada que ajustar. Como el físico no puede ser negativo, el caso se
        arma con el teórico en cero — POS igual a la existencia."""
        siesa.poner(existencia=152, pos=152)
        cc1_id, r1 = _cc1(tienda, 0)
        assert r1['resultado'] != 'SEGUNDO_CONTEO', (
            'un estante vacío con todo el POS pendiente es un MATCH: no hay '
            f'diferencia que ajustar, y sin embargo pidió CC2 ({r1})')


class TestElDeltaSeFijaAlContar:

    def test_d_aprobacion_diferida_con_siesa_movido(self, db, siesa, tienda):
        """Conteo 95 contra 100. Sale una remisión de 10 (Siesa 90) antes de
        que el supervisor apruebe. El ajuste sigue siendo −5 — antes salía
        95 − 90 = +5 AJ-ENT, con el signo invertido.

        Y al aprobar **no se lee Siesa**: la lectura es la que traía el otro
        instante a la resta."""
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=100)
        cc1_id, r1 = _cc1(tienda, 95)
        r2 = _cc2(tienda, r1, 97)                 # discordantes → CC3
        assert r2['resultado'] == 'TERCER_CONTEO'
        cc3_id = r2['tercer_conteo_id']
        sup = tienda['supervisor'].id
        ConteoService.obtener_tarea_operario(cc3_id, sup)
        r3 = ConteoService.registrar_conteo(cc3_id, sup, 95)
        assert r3['resultado'] == 'DESCUADRE' and r3['raiz_id'] == cc1_id
        assert r3['ajuste_bloqueado'] is None

        siesa.poner(existencia=90)                # la remisión, ya confirmada
        lecturas_antes = siesa.lecturas
        ConteoService.confirmar_ajuste(cc1_id, sup)

        assert siesa.lecturas == lecturas_antes, 'la aprobación volvió a leer Siesa'
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 5)

    def test_reencolar_una_sesion_trabada_usa_el_delta_fijado(self, db, siesa, tienda):
        """La rama de recuperación de `confirmar_ajuste` (AJUSTANDO sin job)
        re-encolaba con `fisico − existencia_siesa`: el doble descuento
        volvía justo en la recuperación."""
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.services.conteo_service import ConteoService
        s = SesionConteo(
            codigo='CC-TRABADA', tipo='MANUAL', estado=EstadoConteo.AJUSTANDO,
            ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
            producto_id=tienda['producto'].id, producto_codigo_siesa=SKU,
            cantidad_fisica=7, existencia_siesa=10, cant_pos_siesa=2,
            salida_sin_conf_siesa=2, teorico_siesa=8, fuente_existencia='SIESA',
            diferencia=-1, motivo_codigo='AJ-SAL')
        db.session.add(s)
        db.session.commit()
        ConteoService.confirmar_ajuste(s.id, tienda['supervisor'].id)
        p = _un_job(s.id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)


class TestSalidasQueNoSonPOS:

    def test_e_no_auto_ajusta_y_no_se_aprueba(self, db, siesa, tienda):
        """Salida sin confirmar 5, POS 2: hay 3 und de una salida que no es de
        caja y no se sabe si ya dejaron el estante. Ni automático ni manual."""
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=10, pos=2, salida_sin_conf=5)
        cc1_id, r1 = _cc1(tienda, 7)
        r2 = _cc2(tienda, r1, 7)
        assert r2['resultado'] == 'DESCUADRE'
        assert r2['auto_encolado'] is False
        assert 'NO son venta POS' in (r2['ajuste_bloqueado'] or '')
        assert _jobs(cc1_id) == []

        raiz = db.session.get(SesionConteo, cc1_id)
        assert raiz.estado == EstadoConteo.DESCUADRE
        assert (raiz.salida_sin_conf_siesa, raiz.cant_pos_siesa) == (5, 2)
        assert raiz.to_dict()['bloqueo_ajuste'], 'la pantalla no se entera'

        with pytest.raises(ValueError, match='salidas sin confirmar'):
            ConteoService.confirmar_ajuste(cc1_id, tienda['supervisor'].id)
        db.session.rollback()
        assert _jobs(cc1_id) == []

    def test_e_el_match_no_se_bloquea(self, db, siesa, tienda):
        """Cuadrar con el teórico no manda documento: no hay nada que bloquear."""
        siesa.poner(existencia=10, pos=2, salida_sin_conf=5)
        _cc1_id, r1 = _cc1(tienda, 8)
        assert r1['resultado'] == 'MATCH'


class TestFotoIncompleta:

    @pytest.mark.parametrize('falta', ['f400_cant_pos_1', 'f400_cant_salida_sin_conf_1',
                                       'f400_cant_existencia_1'])
    def test_campo_ausente_no_es_cero(self, db, siesa, tienda, falta):
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=10, pos=2, quitar=(falta,))
        assert ConteoService.consultar_foto_siesa(SKU, 'NS1') is None

    def test_existencia_suelta_sigue_leyendose(self, db, siesa, tienda):
        """`consultar_existencia_siesa` mantiene su contrato para quien solo
        mira (scripts, prewarm): sin POS en la fila, la existencia sí está."""
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=10, pos=2, quitar=('f400_cant_pos_1',))
        assert ConteoService.consultar_existencia_siesa(SKU, 'NS1') == 10.0

    def test_f_sin_pos_no_ajusta(self, db, siesa, tienda):
        """Sin `f400_cant_pos_1` el conteo cae al WMS para decidir si hay
        segundo conteo, y ningún ajuste sale: ni el automático ni el manual."""
        from app.models.conteo import SesionConteo
        from app.services.conteo_service import ConteoService
        siesa.poner(existencia=10, pos=2, quitar=('f400_cant_pos_1',))
        cc1_id, r1 = _cc1(tienda, 7)             # WMS tiene 10
        raiz = db.session.get(SesionConteo, cc1_id)
        assert raiz.fuente_existencia == 'WMS' and raiz.teorico_siesa is None
        r2 = _cc2(tienda, r1, 7)
        assert r2['auto_encolado'] is False
        assert 'foto de Siesa' in (r2['ajuste_bloqueado'] or '')
        assert _jobs(cc1_id) == []
        with pytest.raises(ValueError, match='siempre puede esperar'):
            ConteoService.confirmar_ajuste(cc1_id, tienda['supervisor'].id)
        db.session.rollback()
        assert _jobs(cc1_id) == []

    def test_rechazo_de_connekta_no_es_foto(self, db, siesa, tienda, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.conteo_service import ConteoService
        monkeypatch.setattr(ConnektaGateway, 'get_inventario_fecha',
                            lambda self, c, bodega=None: {'detalle': {'Table': [
                                {'alerta': 'No autorizado'}]}})
        assert ConteoService.consultar_foto_siesa(SKU, 'NS1') is None


class TestCC1YCC2CoincidenPorDiferencia:

    def test_g_venta_entre_cc1_y_cc2_no_pide_cc3(self, db, siesa, tienda):
        """CC1 cuenta 7 con existencia 10 / POS 2 (diferencia −1). Se vende 1
        por caja. CC2 cuenta 6 con existencia 10 / POS 3 (diferencia −1).
        Físicos 7 ≠ 6, pero los dos vieron el mismo faltante: MATCH, ajuste −1,
        sin tercer conteo."""
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=10, pos=2)
        cc1_id, r1 = _cc1(tienda, 7)
        siesa.poner(existencia=10, pos=3)         # la venta de caja
        r2 = _cc2(tienda, r1, 6)
        assert r2['resultado'] == 'DESCUADRE', r2
        assert r2['auto_encolado'] is True
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)
        # La raíz queda con la observación que resolvió: una sola foto.
        raiz = db.session.get(SesionConteo, cc1_id)
        assert (raiz.cantidad_fisica, raiz.cant_pos_siesa, raiz.diferencia) == (6, 3, -1)

    def test_diferencias_distintas_si_piden_cc3(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=2)
        _cc1_id, r1 = _cc1(tienda, 7)
        r2 = _cc2(tienda, r1, 5)
        assert r2['resultado'] == 'TERCER_CONTEO'

    def test_sin_las_dos_fotos_se_comparan_fisicos(self):
        """Una diferencia contra el WMS y otra contra el teórico de Siesa miden
        cosas distintas: ahí manda el físico, como antes."""
        from app.models.conteo import SesionConteo
        from app.services.conteo_service import ConteoService
        cc1 = SesionConteo(cantidad_fisica=7, diferencia=-3, teorico_siesa=None)
        cc2 = SesionConteo(cantidad_fisica=7, diferencia=-1, teorico_siesa=8)
        assert ConteoService.conteos_coinciden(cc1, cc2) is True
        cc2.cantidad_fisica = 6
        assert ConteoService.conteos_coinciden(cc1, cc2) is False


class TestLaAuditoriaDePickingMideIgual:
    """El otro camino al 142951: `ajustar_desde_auditoria_picking`."""

    def _tarea(self, db, tienda):
        from app.models.picking import EstadoPicking, TareaPicking
        t = TareaPicking(codigo='PICK-POS', producto_id=tienda['producto'].id,
                         cantidad_solicitada=5, cantidad_recogida=0,
                         ubicacion_id=tienda['ubicacion'].id,
                         almacen_id=tienda['almacen'].id,
                         estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='FALTANTE')
        tienda['registro'].bloqueado = 5
        db.session.add(t)
        db.session.commit()
        return t

    def test_usa_el_teorico(self, db, siesa, tienda, usuario_admin):
        from app.models.conteo import SesionConteo
        from app.services.picking_service import PickingService
        t = self._tarea(db, tienda)
        siesa.poner(existencia=10, pos=2)         # WMS dice 10 = físico hallado
        PickingService.auditar_tarea(t.id, admin_id=usuario_admin.id,
                                     resultado='ENCONTRADO_COMPLETO')
        s = SesionConteo.query.filter_by(tarea_picking_id=t.id).one()
        assert (s.teorico_siesa, s.diferencia) == (8, 2)
        p = _un_job(s.id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-ENT', 2)

    def test_salidas_no_pos_no_traban_la_auditoria(self, db, siesa, tienda, usuario_admin):
        """El ajuste no sale, pero la tarea de picking se resuelve: esto puede
        durar días y no es culpa de la operación."""
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.models.picking import EstadoPicking, TareaPicking
        from app.services.picking_service import PickingService
        t = self._tarea(db, tienda)
        siesa.poner(existencia=10, pos=2, salida_sin_conf=6)
        PickingService.auditar_tarea(t.id, admin_id=usuario_admin.id,
                                     resultado='ENCONTRADO_COMPLETO')
        db.session.commit()
        s = SesionConteo.query.filter_by(tarea_picking_id=t.id).one()
        assert s.estado == EstadoConteo.DESCUADRE
        assert _jobs(s.id) == []
        assert db.session.get(TareaPicking, t.id).estado != EstadoPicking.BLOQUEADO


class TestCNT08:

    def _res(self):
        from app.services import auditoria
        r = auditoria.auditar('conteo')
        return next(x for x in r['resultados'] if x['codigo'] == 'CNT-08')

    def _sesion(self, db, tienda, **kw):
        from app.models.conteo import SesionConteo
        base = dict(codigo=f'CC-08-{kw.pop("n")}', tipo='MANUAL', estado='AJUSTADO',
                    ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
                    producto_id=tienda['producto'].id, producto_codigo_siesa=SKU,
                    cantidad_fisica=7, existencia_siesa=10, teorico_siesa=8,
                    diferencia=-1, fuente_existencia='SIESA', siesa_triggered=True)
        base.update(kw)
        s = SesionConteo(**base)
        db.session.add(s)
        db.session.commit()
        return s

    def test_ve_un_ajuste_con_salidas_no_pos(self, db, tienda):
        self._sesion(db, tienda, n=1, cant_pos_siesa=2, salida_sin_conf_siesa=5)
        assert self._res()['total'] == 1

    def test_con_salida_igual_al_pos_no(self, db, tienda):
        self._sesion(db, tienda, n=2, cant_pos_siesa=2, salida_sin_conf_siesa=2)
        assert self._res()['total'] == 0

    def test_el_historico_sin_foto_no_cuenta(self, db, tienda):
        self._sesion(db, tienda, n=3, teorico_siesa=None, diferencia=-3)
        assert self._res()['total'] == 0

    def test_cnt01_resta_el_teorico_y_no_la_existencia(self, db, tienda):
        """Una sesión sana de tienda (diferencia contra el teórico) no es un
        hallazgo de CNT-01; la misma con la diferencia de la existencia cruda,
        sí."""
        from app.services import auditoria
        s = self._sesion(db, tienda, n=4, cant_pos_siesa=2, salida_sin_conf_siesa=2)

        def cnt01():
            r = auditoria.auditar('conteo')
            return next(x for x in r['resultados'] if x['codigo'] == 'CNT-01')['total']
        assert cnt01() == 0
        s.diferencia = s.cantidad_fisica - s.existencia_siesa   # −3, el defecto
        db.session.commit()
        assert cnt01() == 1


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete 1 — ninguna resta contra la existencia cruda
# ─────────────────────────────────────────────────────────────────────────────

#: Sitios de `app/` que restan la existencia cruda de Siesa de algo. Vacío a
#: propósito: la base de un conteo es `ConteoService.base_de_comparacion` (el
#: teórico) y la única resta con la existencia es la del teórico mismo, que la
#: tiene a la IZQUIERDA (`existencia − cant_pos`). Un sitio nuevo necesita su
#: motivo escrito acá, y la persona que lo escriba va a tener que explicar por
#: qué no es el doble descuento del POS.
RESTAS_CONTRA_EXISTENCIA_DECLARADAS = {}


def _es_existencia(nodo):
    """¿Este operando ES la existencia cruda de Siesa?

    Tres escrituras de lo mismo: `s.existencia_siesa`, una variable local
    `existencia_siesa`, y `foto['existencia']`. Se desenvuelve `x or 0` —la
    forma de la rama de recuperación que tenía el defecto—.
    """
    while isinstance(nodo, ast.BoolOp):
        nodo = nodo.values[0]
    if isinstance(nodo, ast.Attribute) and nodo.attr == 'existencia_siesa':
        return True
    if isinstance(nodo, ast.Name) and nodo.id == 'existencia_siesa':
        return True
    return (isinstance(nodo, ast.Subscript) and isinstance(nodo.slice, ast.Constant)
            and nodo.slice.value == 'existencia')


def _restas_contra_existencia(base=None):
    base = base or RAIZ
    hallados, archivos = {}, 0
    for f in sorted((base / 'app').rglob('*.py')):
        rel = str(f.relative_to(base))
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        archivos += 1
        for n in ast.walk(arbol):
            restado = None
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub):
                restado = n.right
            elif isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Sub):
                restado = n.value
            if restado is not None and _es_existencia(restado):
                hallados.setdefault(rel, []).append(n.lineno)
    return hallados, archivos


class TestNingunaRestaContraLaExistenciaCruda:

    def test_no_hay_restas_sin_declarar(self):
        hallados, _ = _restas_contra_existencia()
        nuevos = {a: ls for a, ls in hallados.items()
                  if a not in RESTAS_CONTRA_EXISTENCIA_DECLARADAS}
        assert not nuevos, (
            f'\nRestas contra la existencia cruda de Siesa: {nuevos}\n'
            'La base de un conteo es el teórico (existencia − POS pendiente), '
            'vía ConteoService.base_de_comparacion. Restar la existencia sola '
            'fabrica un faltante igual al POS y el ajuste lo descuenta dos veces.')

    def test_la_lista_solo_encoge(self):
        hallados, _ = _restas_contra_existencia()
        sobran = [a for a in RESTAS_CONTRA_EXISTENCIA_DECLARADAS if a not in hallados]
        assert not sobran, f'Declaradas que ya no restan: {sobran}. Sacarlas.'

    def test_toda_declaracion_dice_por_que(self):
        flojas = [a for a, m in RESTAS_CONTRA_EXISTENCIA_DECLARADAS.items() if len(m) < 80]
        assert not flojas


class TestElDetectorDeRestasMuerde:
    """Corren el escáner REAL sobre un árbol de mentira."""

    def _en(self, fuente, tmp_path):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _restas_contra_existencia(base=tmp_path)[0]

    @pytest.mark.parametrize('fuente', [
        'd = s.cantidad_fisica - s.existencia_siesa\n',
        'd = (s.cantidad_fisica or 0) - (s.existencia_siesa or 0)\n',
        'd = fisico - existencia_siesa\n',
        "d = fisico - foto['existencia']\n",
        'd -= s.existencia_siesa\n',
    ])
    def test_ve_las_escrituras_del_defecto(self, fuente, tmp_path):
        assert self._en(fuente, tmp_path), fuente

    @pytest.mark.parametrize('fuente', [
        't = s.existencia_siesa - s.cant_pos_siesa\n',       # el teórico: a la izquierda
        'd = s.cantidad_fisica - s.teorico_siesa\n',
        '"""d = s.cantidad_fisica - s.existencia_siesa"""\n',  # docstring
        '# d = fisico - existencia_siesa\n',
    ])
    def test_no_marca_lo_que_no_es(self, fuente, tmp_path):
        assert not self._en(fuente, tmp_path), fuente

    def test_piso_de_archivos(self):
        """Un rglob roto devuelve cero archivos y cero hallazgos: un verde falso."""
        _, archivos = _restas_contra_existencia()
        assert archivos >= 100


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete 2 — quién lee la existencia de Siesa, y nunca al aprobar
# ─────────────────────────────────────────────────────────────────────────────

_LECTORAS = {'get_inventario_fecha', 'consultar_existencia_siesa',
             'consultar_foto_siesa', '_fila_invfecha'}

#: Cada función de `app/` que llama una lectura de existencia de Siesa, con por
#: qué puede. La clave es `archivo::Clase.funcion` (el nombre calificado de la
#: función que contiene la llamada).
LECTURAS_DECLARADAS = {
    'app/services/conteo_service.py::ConteoService._fila_invfecha': (
        'La única llamada HTTP del conteo a InvFecha. Todo lo demás pasa por '
        'acá para que el sobre de rechazo y la tabla vacía se traten igual.'),
    'app/services/conteo_service.py::ConteoService.consultar_existencia_siesa': (
        'Existencia suelta para quien solo MIRA: scripts de prueba real y el '
        'prewarm del turno. No es base de ningún conteo ni ajuste.'),
    'app/services/conteo_service.py::ConteoService.consultar_foto_siesa': (
        'Arma la foto (existencia, POS, salida sin confirmar, teórico) desde la '
        'fila cruda. Es la única que calcula el teórico.'),
    'app/services/conteo_service.py::ConteoService.registrar_conteo': (
        'El instante del conteo: la foto se toma acá, antes del lock, y queda '
        'guardada en la sesión como base del delta.'),
    'app/services/conteo_service.py::ConteoService.registrar_foto_inicio': (
        'La foto de la APERTURA del conteo (m029): se toma al abrir la tarea, '
        'antes de que el operario cuente. No entra al delta: solo decide si '
        'Siesa se movió mientras se contaba (recontar) comparándola con la del '
        'cierre. Ver tests/test_conteo_ventas_durante_conteo.py.'),
    'app/services/conteo_service.py::ConteoService.ajustar_desde_auditoria_picking': (
        'La auditoría de picking ES un conteo físico del supervisor, hecho en '
        'ese instante: toma su foto igual que registrar_conteo y la guarda.'),
    'app/services/abc_service.py::ABCService.init_scheduler._prewarm_pre_turno._warm_one': (
        'Prewarm de las 5:55: lee la existencia y la descarta. No escribe en '
        'ninguna sesión ni decide nada — si desapareciera, nada cambiaría de valor.'),
    'app/services/connekta_gateway.py::ConnektaGateway.get_inventario_fecha': (
        'Delegado delgado del refactor del gateway: reenvía al dominio de '
        'consultas con la misma firma. No es un consumidor.'),
}

#: Las funciones del camino de aprobación. Si una de estas lee Siesa, el delta
#: vuelve a mezclar dos instantes: el defecto del signo invertido.
CAMINO_DE_APROBACION = {
    'app/services/conteo_service.py::ConteoService._encolar_ajuste_fisico',
    'app/services/conteo_service.py::ConteoService.confirmar_ajuste',
}


def _lecturas(base=None):
    """`{archivo::funcion_calificada: [lineas]}` de cada llamada lectora."""
    base = base or RAIZ
    hallados = {}

    def nombre(call):
        f = call.func
        if isinstance(f, ast.Attribute):
            return f.attr
        if isinstance(f, ast.Name):
            return f.id
        return None

    for f in sorted((base / 'app').rglob('*.py')):
        rel = str(f.relative_to(base))
        arbol = ast.parse(f.read_text(encoding='utf-8'))

        def visitar(nodo, pila):
            for hijo in ast.iter_child_nodes(nodo):
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visitar(hijo, pila + [hijo.name])
                    continue
                if isinstance(hijo, ast.Call) and nombre(hijo) in _LECTORAS:
                    clave = f'{rel}::{".".join(pila) or "<modulo>"}'
                    hallados.setdefault(clave, []).append(hijo.lineno)
                visitar(hijo, pila)

        visitar(arbol, [])
    return hallados


class TestLecturasDeExistenciaDeclaradas:

    def test_ninguna_lectura_sin_declarar(self):
        nuevas = {k: v for k, v in _lecturas().items() if k not in LECTURAS_DECLARADAS}
        assert not nuevas, (
            f'\nLecturas de la existencia de Siesa sin declarar: {nuevas}\n'
            'Si decide un ajuste, tiene que salir de la foto del conteo '
            '(ConteoService.consultar_foto_siesa en el instante de contar), no '
            'de una lectura nueva. Declarala con su motivo si no decide nada.')

    def test_la_lista_solo_encoge(self):
        hallados = _lecturas()
        sobran = [k for k in LECTURAS_DECLARADAS if k not in hallados]
        assert not sobran, f'Declaradas que ya no leen: {sobran}. Sacarlas.'

    def test_toda_declaracion_dice_por_que(self):
        flojas = [k for k, m in LECTURAS_DECLARADAS.items() if len(m) < 80]
        assert not flojas

    def test_la_aprobacion_no_lee_siesa(self):
        """El caso con nombre propio: ni declarándolo."""
        leen = CAMINO_DE_APROBACION & (set(_lecturas()) | set(LECTURAS_DECLARADAS))
        assert not leen, (
            f'{leen} lee la existencia de Siesa. El ajuste es un delta medido al '
            'contar; leer al aprobar mete otro instante en la resta (conteo 95 '
            'vs 100, remisión de 10, aprobación: 95 − 90 = +5 con el signo '
            'invertido).')


class TestElDetectorDeLecturasMuerde:

    def _en(self, fuente, tmp_path):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _lecturas(base=tmp_path)

    def test_ve_la_llamada_por_atributo_en_un_metodo(self, tmp_path):
        src = ('class ConteoService:\n'
               '    def _encolar_ajuste_fisico(s):\n'
               '        return ConteoService.consultar_existencia_siesa("X", "NB1")\n')
        assert set(self._en(src, tmp_path)) == {'app/x.py::ConteoService._encolar_ajuste_fisico'}

    def test_ve_la_llamada_al_gateway_y_por_nombre_suelto(self, tmp_path):
        src = ('def a():\n    connekta.get_inventario_fecha("X")\n'
               'def b():\n    consultar_foto_siesa("X")\n')
        assert set(self._en(src, tmp_path)) == {'app/x.py::a', 'app/x.py::b'}

    def test_ve_una_funcion_anidada(self, tmp_path):
        src = 'def f():\n    def g():\n        _fila_invfecha("X")\n'
        assert set(self._en(src, tmp_path)) == {'app/x.py::f.g'}

    def test_no_marca_la_definicion_ni_el_texto(self, tmp_path):
        src = ('def consultar_existencia_siesa(x):\n'
               '    """llama a get_inventario_fecha(x)"""\n'
               '    # consultar_foto_siesa(x)\n'
               '    return "get_inventario_fecha"\n')
        assert self._en(src, tmp_path) == {}

    def test_piso_minimo_en_el_repo(self):
        assert len(_lecturas()) >= 6

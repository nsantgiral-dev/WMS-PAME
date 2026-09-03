"""
La puerta de la plata que sale — adaptador y frontera HTTP, contra la base real.

`tests/flota/test_costos.py` prueba la aritmética. Acá se prueba lo otro: que la
fila quede escrita, que la regla 3 se cumpla sin depender de que alguien se
acuerde, y que **la frontera no afloje ninguna política del adaptador**. Un
endpoint que acepta lo que el adaptador rechaza es un rodeo alrededor de todo lo
demás — y como devuelve 201, nadie se entera.

## La asimetría de roles que este archivo existe para afirmar

| | Quién | Por qué |
|---|---|---|
| Registrar un tanqueo | conductor incluido | El que tanquea es el que maneja. Si solo lo registra un jefe, se registra el lunes y la factura ya se perdió |
| Registrar cualquier otro gasto | sin conductor | El SOAT y una entrada a taller no los paga el conductor |
| Ver los gastos y el CPK | sin conductor | Un CPK en la pantalla del conductor está a un paso de leerse como una medida suya (regla 2) |

Un test que solo probara el camino feliz con token de admin no vería nunca esas
tres puertas.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from flota.adaptadores import gastos as adaptador
from flota.dominio.costos import SIN_DATO

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


def _alguien_paso_por_la_cola(db, vehiculo_id, usuario_id):
    """Verifica las lecturas dudosas del vehículo, como haría una persona.

    **Existe desde el 2026-09-02 y su necesidad es el hallazgo, no un andamio.**
    Toda lectura sin foto del tablero nace `dudosa` (`confianza_al_nacer`,
    regla 1), y las que nacen de un tanqueo o de una entrada a taller **no
    tienen foto**: el formulario de gasto no la pide. Con los dos extremos del
    tramo dudosos, `confianza_del_tramo` devuelve `SIN_DATO` y el CPK del mes no
    se publica — que es exactamente lo que la fase 0 quiso.

    Así que un CPK publicable exige que alguien haya pasado por la cola de
    verificación. Estos tests lo hacen explícito en vez de asumirlo: sin esta
    llamada, el canon de marzo da `sin_dato`, y hay un test que lo afirma
    (`TestElCPKNoSePublicaSobreUnTramoQueNadiePuedeRespaldar`).
    """
    from flota.adaptadores import verificacion

    for lectura, _placa in verificacion.pendientes():
        if lectura.vehiculo_id == vehiculo_id:
            verificacion.verificar(lectura_id=lectura.id, usuario_id=usuario_id)


@pytest.fixture
def mundo(db, almacen):
    """Un vehículo con ficha (capacidad 15 galones), otro sin ficha, y las
    tres clases de usuario que el módulo distingue."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import FichaTecnica

    veh = Vehiculo(placa='GST100', tipo='NHR', activo=True)
    sin_ficha = Vehiculo(placa='GST200', tipo='NHR', activo=True)
    cond = Usuario(nombre='Conductor GST', email='gst_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control GST', email='gst_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda GST', email='gst_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, sin_ficha, cond, flota, tienda])
    db.session.flush()
    db.session.add(FichaTecnica(
        vehiculo_id=veh.id, posiciones_llanta=6, km_inicial=99000,
        km_inicial_ts=datetime(2026, 1, 1), combustible='diesel',
        capacidad_tanque_galones=Decimal('15'),
        capacidad_tanque_fuente='manual_fabricante'))
    db.session.add(FichaTecnica(
        vehiculo_id=sin_ficha.id, posiciones_llanta=4, km_inicial=0,
        km_inicial_ts=datetime(2026, 1, 1)))
    db.session.commit()
    return {
        'vehiculo_id': veh.id, 'placa': veh.placa,
        'sin_capacidad_id': sin_ficha.id, 'placa_sin_capacidad': sin_ficha.placa,
        'usuario_id': flota.id,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        'db': db,
    }


def _tanquear(mundo, *, km, galones='12', tanque='lleno', valor='168000',
              fecha=None, ts=None, origen_costo='tarjeta_convenio', **extra):
    return adaptador.registrar_tanqueo(
        vehiculo_id=mundo['vehiculo_id'],
        fecha=fecha or date(2026, 3, 5), valor=valor, galones=galones,
        tanque=tanque, estacion='Terpel Neiva', km=km, proveedor='Terpel',
        origen_costo=origen_costo,
        registrado_por_usuario_id=mundo['usuario_id'],
        ts=ts or datetime(2026, 3, 5, 10, 0), **extra)


# ══════════════════════════════════════════════════════════════════════════
# Regla 3 — sin odómetro no se persiste ningún evento de flota
# ══════════════════════════════════════════════════════════════════════════

class TestLaLecturaSeAnclaNoSeFabrica:
    """`_anclar_odometro` de `hallazgos`, **no una copia** (regla 0).

    Escribir una lectura nueva por cada gasto produciría desde adentro del
    sistema el mismo patrón que `lecturas_ts_duplicado` existe para contar
    —lecturas con el mismo segundo, de un reintento— y el contador dejaría de
    distinguir un reintento de la operación normal.
    """

    def test_el_tanqueo_nace_con_su_lectura(self, app, db, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        tq = _tanquear(mundo, km=100000)
        lec = LecturaOdometro.query.get(tq.gasto.lectura_id)
        assert lec is not None
        assert lec.valor_km == 100000
        assert tq.gasto.lectura_id is not None

    def test_el_origen_de_la_lectura_de_un_tanqueo_es_tanqueo(self, app, db, mundo):
        """La columna `origen` existe para no tener que adivinar de dónde vino
        la lectura. Escribir `ot` acá sería una fila que dice de dónde vino y
        miente."""
        from flota.adaptadores.modelos import LecturaOdometro

        tq = _tanquear(mundo, km=100000)
        assert LecturaOdometro.query.get(tq.gasto.lectura_id).origen == 'tanqueo'

    def test_dos_gastos_con_el_mismo_km_comparten_la_lectura(self, app, db, mundo):
        """**El punto entero de reutilizar `anclar_odometro`.**

        Dos gastos del mismo momento —tanquear y pagar el lavado al lado— no
        producen dos lecturas del mismo segundo."""
        from flota.adaptadores.modelos import LecturaOdometro

        t1 = _tanquear(mundo, km=100000)
        t2 = _tanquear(mundo, km=100000, documento_numero='F-2',
                       ts=datetime(2026, 3, 5, 10, 5))
        assert t1.gasto.lectura_id == t2.gasto.lectura_id
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == 1

    def test_un_km_distinto_SI_produce_una_lectura_nueva(self, app, db, mundo):
        """La otra dirección: si el odómetro se movió, eso es información nueva.

        Sin este test, «reutilizar siempre la última» pasaría el de arriba y el
        kilometraje que el conductor tecleó se perdería — que es la falla que el
        módulo entero persigue."""
        from flota.adaptadores.modelos import LecturaOdometro

        t1 = _tanquear(mundo, km=100000)
        t2 = _tanquear(mundo, km=100392, documento_numero='F-2',
                       ts=datetime(2026, 3, 15, 10, 0))
        assert t1.gasto.lectura_id != t2.gasto.lectura_id
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == 2

    def test_un_odometro_que_retrocede_no_entra_por_esta_puerta(self, app, db, mundo):
        """La validación de monotonía es del dominio y la hereda el gasto por
        usar la misma política. Una segunda puerta con su propia validación es
        cómo se abre el agujero."""
        from flota.dominio.errores import ErrorFlota

        _tanquear(mundo, km=100392)
        with pytest.raises(ErrorFlota):
            _tanquear(mundo, km=100000, documento_numero='F-2',
                      ts=datetime(2026, 3, 20, 10, 0))


class TestElGastoDeEscritorioNoInventaUnOrigen:
    """Nueve categorías se pagan sin el vehículo delante, y el vocabulario de
    `origen` no tiene un valor que las describa.

    La salida no es escribir `ot` en la lectura de un impuesto vehicular: es
    colgar el gasto de la última lectura conocida, que es lo que
    `anclar_odometro` ya declara que su fila significa —**no afirma haberse
    tomado para este evento**—.
    """

    def _soat(self, mundo, **extra):
        cuerpo = dict(vehiculo_id=mundo['vehiculo_id'], categoria='soat',
                      fecha=date(2026, 1, 1), valor='730000',
                      proveedor='Seguros del Estado',
                      origen_costo='credito_proveedor',
                      registrado_por_usuario_id=mundo['usuario_id'],
                      periodo_desde=date(2026, 1, 1),
                      periodo_hasta=date(2026, 12, 31))
        cuerpo.update(extra)
        return adaptador.registrar_gasto(**cuerpo)

    def test_se_cuelga_de_la_ultima_lectura_sin_crear_ninguna(self, app, db, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        tq = _tanquear(mundo, km=100000)
        antes = LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count()
        soat = self._soat(mundo)
        assert soat.lectura_id == tq.gasto.lectura_id
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == antes

    def test_sin_ninguna_lectura_el_gasto_NO_se_registra(self, app, db, mundo):
        """Regla 3, y no es burocracia: ese gasto no podría entrar a ningún CPK.

        Guardarlo igual sería guardar una fila que nadie va a poder dividir — y
        `lectura_id` es NOT NULL justamente para que esto no dependa de que
        alguien se acuerde."""
        with pytest.raises(adaptador.GastoInvalido) as e:
            self._soat(mundo)
        assert 'lectura' in str(e.value).lower()

    def test_mandarle_kilometraje_a_un_gasto_de_escritorio_LEVANTA(self, app, db, mundo):
        """No se ignora: se levanta. Ignorarlo dejaría a quien lo mandó
        creyendo que la lectura se registró."""
        _tanquear(mundo, km=100000)
        with pytest.raises(adaptador.GastoInvalido) as e:
            self._soat(mundo, km=100500)
        assert 'kilometraje' in str(e.value)

    def test_un_gasto_de_campo_SIN_kilometraje_LEVANTA(self, app, db, mundo):
        """La otra dirección de la misma política."""
        with pytest.raises(adaptador.GastoInvalido) as e:
            adaptador.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 3, 10), valor='190000', proveedor='Taller X',
                origen_costo='credito_proveedor',
                registrado_por_usuario_id=mundo['usuario_id'])
        assert 'kilometraje' in str(e.value)

    def test_el_reparto_de_categorias_cubre_TODO_el_vocabulario(self):
        """Total, sin `.get(cat, False)`: una categoría nueva no puede caer del
        lado de escritorio por omisión. Si alguien agrega `todo_riesgo` al
        catálogo, `es_de_campo` la clasifica explícitamente o revienta."""
        from flota.dominio import costos

        for cat in costos.CATEGORIAS_GASTO:
            assert adaptador.es_de_campo(cat) in (True, False)
        with pytest.raises(adaptador.GastoInvalido):
            adaptador.es_de_campo('peritaje')

    def test_ningun_origen_de_lectura_miente_sobre_su_gesto(self):
        """Las cuatro categorías de campo se mapean a un origen que las
        describe. `combustible→tanqueo` y taller→`ot`; ninguna a `entrega` ni a
        `preoperacional`, que son gestos de turno y no de gasto."""
        from flota.dominio.valores import OrigenLectura

        assert adaptador.ORIGEN_DE_LECTURA['combustible'] == OrigenLectura.TANQUEO
        for cat in ('mantenimiento', 'repuesto', 'llanta'):
            assert adaptador.ORIGEN_DE_LECTURA[cat] == OrigenLectura.OT
        assert set(adaptador.ORIGEN_DE_LECTURA) <= set(
            __import__('flota.dominio.costos', fromlist=['x']).CATEGORIAS_GASTO)


# ══════════════════════════════════════════════════════════════════════════
# El período lo decide la categoría, no una casilla (regla 11)
# ══════════════════════════════════════════════════════════════════════════

class TestElPeriodoNoSeNegocia:

    def test_una_categoria_con_periodo_lo_exige(self, app, db, mundo):
        _tanquear(mundo, km=100000)
        with pytest.raises(adaptador.GastoInvalido) as e:
            adaptador.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='soat',
                fecha=date(2026, 1, 1), valor='730000', proveedor='Seguros',
                origen_costo='credito_proveedor',
                registrado_por_usuario_id=mundo['usuario_id'])
        assert 'período' in str(e.value)

    def test_una_categoria_sin_periodo_lo_RECHAZA(self, app, db, mundo):
        """Aceptarlo en silencio dejaría un tanqueo repartido sobre un año, y el
        CPK del mes se hundiría sin que nada falle."""
        with pytest.raises(adaptador.GastoInvalido) as e:
            adaptador.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 3, 10), valor='190000', proveedor='Taller X',
                origen_costo='credito_proveedor', km=100000,
                registrado_por_usuario_id=mundo['usuario_id'],
                periodo_desde=date(2026, 1, 1), periodo_hasta=date(2026, 12, 31))
        assert 'no admite período' in str(e.value)

    def test_sin_periodo_declarado_el_gasto_cubre_su_propio_dia(self, app, db, mundo):
        g = adaptador.registrar_gasto(
            vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
            fecha=date(2026, 3, 10), valor='190000', proveedor='Taller X',
            origen_costo='credito_proveedor', km=100000,
            registrado_por_usuario_id=mundo['usuario_id'])
        assert g.periodo_desde == g.periodo_hasta == date(2026, 3, 10)

    def test_un_periodo_invertido_LEVANTA(self, app, db, mundo):
        _tanquear(mundo, km=100000)
        with pytest.raises(adaptador.GastoInvalido):
            adaptador.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='soat',
                fecha=date(2026, 1, 1), valor='730000', proveedor='Seguros',
                origen_costo='credito_proveedor',
                registrado_por_usuario_id=mundo['usuario_id'],
                periodo_desde=date(2026, 12, 31), periodo_hasta=date(2026, 1, 1))


class TestLoQueNoSeGuardaAMedias:

    def test_un_valor_de_cero_no_entra(self, app, db, mundo):
        """Un gasto de $0 no es un gasto barato: es una fila que alguien guardó
        sin saber el valor."""
        with pytest.raises(adaptador.GastoInvalido):
            _tanquear(mundo, km=100000, valor='0')

    def test_un_valor_ilegible_LEVANTA_en_vez_de_valer_cero(self, app, db, mundo):
        """Regla 5: un valor que se degrada a cero baja el CPK del vehículo y no
        se ve raro en ninguna pantalla."""
        with pytest.raises(adaptador.GastoInvalido) as e:
            _tanquear(mundo, km=100000, valor='ciento sesenta mil')
        assert 'valor' in str(e.value)

    def test_otro_sin_descripcion_no_entra(self, app, db, mundo):
        """`otro` es la válvula del catálogo cerrado y no puede ser la salida
        cómoda: sin esto se convierte en el 40% de los gastos."""
        _tanquear(mundo, km=100000)
        with pytest.raises(adaptador.GastoInvalido):
            adaptador.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='otro',
                fecha=date(2026, 3, 10), valor='50000', proveedor='X',
                origen_costo='sin_dato',
                registrado_por_usuario_id=mundo['usuario_id'])

    def test_otro_CON_descripcion_si_entra(self, app, db, mundo):
        """La otra dirección: la válvula tiene que funcionar, o los cuatro
        gastos que el catálogo no previó dan 400 — que es lo que pasó con
        `TIPOS_DOCUMENTO`."""
        _tanquear(mundo, km=100000)
        g = adaptador.registrar_gasto(
            vehiculo_id=mundo['vehiculo_id'], categoria='otro',
            fecha=date(2026, 3, 10), valor='50000', proveedor='X',
            origen_costo='sin_dato', descripcion='peritaje de motor',
            registrado_por_usuario_id=mundo['usuario_id'])
        assert g.descripcion == 'peritaje de motor'

    def test_un_origen_de_costo_inventado_no_entra(self, app, db, mundo):
        with pytest.raises(adaptador.GastoInvalido) as e:
            _tanquear(mundo, km=100000, origen_costo='caja_menor')
        assert 'sin_dato' in str(e.value)

    def test_sin_dato_SI_es_una_respuesta_valida(self, app, db, mundo):
        """La pregunta que el dueño no contestó se escribe, no se adivina."""
        tq = _tanquear(mundo, km=100000, origen_costo='sin_dato')
        assert tq.gasto.origen_costo == 'sin_dato'

    def test_documento_vacio_se_guarda_como_NULL_no_como_cadena(self, app, db, mundo):
        """Dos representaciones de la misma ausencia son dos consultas que dan
        números distintos, y una de las dos es la que cuenta los gastos sin
        factura."""
        tq = _tanquear(mundo, km=100000, documento_numero='   ')
        assert tq.gasto.documento_numero is None

    def test_un_tanqueo_que_falla_no_deja_el_gasto_suelto(self, app, db, mundo):
        """**Las dos filas van en una transacción.**

        Un gasto de combustible sin su tanqueo sería un peso que entra al CPK y
        no aporta un solo galón al rendimiento: equivocado en las dos métricas y
        visible en ninguna pantalla."""
        from flota.adaptadores.modelos import Gasto

        with pytest.raises(adaptador.GastoInvalido):
            _tanquear(mundo, km=100000, tanque='medio')
        assert Gasto.query.filter_by(vehiculo_id=mundo['vehiculo_id']).count() == 0

    def test_si_la_extremidad_falla_DESPUES_el_gasto_tampoco_queda(
            self, app, db, mundo, monkeypatch):
        """**El que de verdad prueba la transacción, y el que faltaba.**

        El test de arriba pasa por una validación que ocurre **antes** de
        escribir nada: no hay nada que deshacer, así que pasaría igual con el
        gasto en su propia transacción. La mutación 17 del arnés del 2026-09-02
        —`commit=False` → `commit=True`— sobrevivió exactamente por eso: el
        guard medía una propiedad que la vía de prueba satisfacía por
        construcción.

        Acá el gasto ya está escrito y la fila de tanqueo revienta después. Si
        el gasto se hubiera confirmado por su cuenta quedaría **un gasto de
        combustible sin galones**: suma al costo por kilómetro y no aporta un
        solo galón al rendimiento — equivocado en las dos métricas y visible en
        ninguna pantalla.
        """
        from flota.adaptadores import gastos as mod
        from flota.adaptadores.modelos import Gasto, LecturaOdometro

        def _revienta(**kwargs):
            raise RuntimeError('la extremidad no se pudo escribir')

        monkeypatch.setattr(mod, 'Tanqueo', _revienta)
        with pytest.raises(RuntimeError):
            _tanquear(mundo, km=100000)

        assert Gasto.query.filter_by(vehiculo_id=mundo['vehiculo_id']).count() == 0
        # Y la lectura que se ancló en la misma transacción tampoco queda: una
        # lectura huérfana de un tanqueo que no ocurrió es kilometraje que nadie
        # midió, que es la falla que este módulo entero persigue.
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == 0

    def test_la_misma_factura_no_entra_dos_veces_sobre_el_mismo_vehiculo(
            self, app, db, mundo):
        """El invariante de idempotencia de la fase, y lo ataja **la base**: la
        vía típica de la doble carga es un reintento del formulario, y para
        entonces el adaptador ya devolvió.

        Un CPK que cuenta dos veces el mismo tanqueo no se ve mal — es un número
        plausible — y no hay forma de detectarlo después."""
        from sqlalchemy.exc import IntegrityError

        _tanquear(mundo, km=100000, documento_numero='FAC-991')
        with pytest.raises(IntegrityError):
            _tanquear(mundo, km=100392, documento_numero='FAC-991',
                      ts=datetime(2026, 3, 15, 10, 0))

    def test_dos_gastos_SIN_documento_si_conviven(self, app, db, mundo):
        """La otra dirección: los NULL son distintos entre sí. Si no lo fueran,
        el segundo gasto sin factura de un vehículo sería imposible de
        registrar — y hoy son la mayoría."""
        _tanquear(mundo, km=100000)
        _tanquear(mundo, km=100392, ts=datetime(2026, 3, 15, 10, 0))
        from flota.adaptadores.modelos import Gasto
        assert Gasto.query.filter_by(vehiculo_id=mundo['vehiculo_id'],
                                     documento_numero=None).count() == 2


# ══════════════════════════════════════════════════════════════════════════
# El detector: galones por encima de la capacidad del tanque
# ══════════════════════════════════════════════════════════════════════════

class TestElDetectorDeCapacidad:
    """El único de la fase, y el que no necesita umbral ni canon ni historia.

    Lo que afirma: que la capacidad de la ficha y los galones registrados **no
    pueden ser los dos ciertos**. No afirma que alguien se haya llevado nada — y
    las cuatro explicaciones posibles (capacidad mal levantada, tanque auxiliar
    que la ficha no conoce, dos vehículos en la misma factura, un dedo en el
    teclado) se investigan igual de rápido.
    """

    def test_dispara_sobre_un_tanqueo_imposible(self, app, db, mundo):
        tq = _tanquear(mundo, km=100000, galones='22')
        assert adaptador.excede_capacidad_de(tq) is True

    def test_NO_dispara_sobre_un_tanqueo_normal(self, app, db, mundo):
        """La otra dirección. Sin esto, un detector que marca todo pasaría el de
        arriba, y a la semana nadie mira el tablero."""
        tq = _tanquear(mundo, km=100000, galones='12')
        assert adaptador.excede_capacidad_de(tq) is False

    def test_sin_capacidad_en_la_ficha_es_SIN_DATO_no_False(self, app, db, mundo):
        """Un vehículo sin capacidad declarada saldría limpio para siempre: así
        es como un detector se apaga sin que nadie lo note."""
        tq = adaptador.registrar_tanqueo(
            vehiculo_id=mundo['sin_capacidad_id'], fecha=date(2026, 3, 5),
            valor='300000', galones='40', tanque='lleno', estacion='Terpel',
            km=5000, proveedor='Terpel', origen_costo='sin_dato',
            registrado_por_usuario_id=mundo['usuario_id'])
        r = adaptador.excede_capacidad_de(tq)
        assert r is SIN_DATO
        assert r is not False

    def test_el_tanqueo_excedido_SE_REGISTRA_igual(self, app, db, mundo):
        """Medir → corregir → imponer, en ese orden.

        Si el primer día la app rechaza un tanqueo por una capacidad mal
        levantada, la operación desmonta el sistema en 48 horas. El detector
        informa; no bloquea."""
        from flota.adaptadores.modelos import Gasto

        _tanquear(mundo, km=100000, galones='22')
        assert Gasto.query.filter_by(vehiculo_id=mundo['vehiculo_id']).count() == 1


# ══════════════════════════════════════════════════════════════════════════
# El CPK, contra la base — el canon §5 recorrido de punta a punta
# ══════════════════════════════════════════════════════════════════════════

class TestElCanonDePuntaAPunta:
    """El mes de marzo del canon, escrito en la base y leído por `cpk_de`.

    `tests/flota/test_costos.py` prueba que la aritmética da 840. Esto prueba
    que **los datos llegan a esa aritmética**: es la diferencia entre una
    fórmula correcta y una fórmula que nadie alimenta.
    """

    @pytest.fixture
    def marzo(self, app, db, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        # **En orden cronológico, y no es cosmético**: la validación de
        # monotonía del odómetro mira el MÁXIMO ya registrado, así que una
        # lectura retroactiva no entra. Es correcto que no entre —un odómetro
        # que baja se registra con `origen=correccion` y motivo— y significa que
        # el orden de carga importa: una factura de taller cargada después del
        # tanqueo del 25 no puede traer el kilometraje del día 10.
        _tanquear(mundo, km=100000, galones='12', valor='168000',
                  fecha=date(2026, 3, 5), ts=datetime(2026, 3, 5, 8, 0))
        adaptador.registrar_gasto(
            vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
            fecha=date(2026, 3, 10), valor='190000', proveedor='Taller Neiva',
            origen_costo='credito_proveedor', km=100200,
            registrado_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 10, 8, 0))
        _tanquear(mundo, km=100392, galones='14', valor='196000',
                  fecha=date(2026, 3, 15), ts=datetime(2026, 3, 15, 8, 0))
        _tanquear(mundo, km=100720, galones='16', valor='224000',
                  fecha=date(2026, 3, 25), ts=datetime(2026, 3, 25, 8, 0))
        # El SOAT anual, de escritorio: se cuelga de la última lectura.
        adaptador.registrar_gasto(
            vehiculo_id=mundo['vehiculo_id'], categoria='soat',
            fecha=date(2026, 1, 1), valor='730000',
            proveedor='Seguros del Estado', origen_costo='credito_proveedor',
            registrado_por_usuario_id=mundo['usuario_id'],
            periodo_desde=date(2026, 1, 1), periodo_hasta=date(2026, 12, 31),
            ts=datetime(2026, 3, 25, 9, 0))
        # La lectura de cierre de mes: el canon mide 100.000 → 101.000.
        db.session.add(LecturaOdometro(
            vehiculo_id=mundo['vehiculo_id'], valor_km=101000,
            ts=datetime(2026, 3, 31, 18, 0), origen='cierre_dia',
            autor_usuario_id=mundo['usuario_id']))
        db.session.commit()
        # Ninguna de estas lecturas tiene foto del tablero —el formulario de
        # gasto no la pide— así que todas nacen `dudosa`. Alguien pasó por la
        # cola: sin eso el mes entero sale `sin_dato`, y hay un test aparte que
        # lo afirma.
        _alguien_paso_por_la_cola(db, mundo['vehiculo_id'], mundo['usuario_id'])
        return mundo

    def test_el_CPK_de_marzo_es_840(self, app, db, marzo):
        r = adaptador.cpk_de(marzo['vehiculo_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['pesos'] == Decimal('840000')
        assert r['km'] == 1000
        assert r['cpk'] == Decimal('840')
        # `verificada` y no `declarada`: los dos extremos del tramo los
        # confirmó una persona por la cola. La marca viaja con el número
        # justamente para que se pueda leer eso.
        assert r['marca'] == 'verificada'

    def test_el_SOAT_entra_repartido_no_entero(self, app, db, marzo):
        """La comprobación que separa este CPK de uno mal hecho: con el SOAT
        entero, marzo daría $1.508 por kilómetro."""
        r = adaptador.cpk_de(marzo['vehiculo_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['pesos'] == Decimal('840000')
        assert r['pesos'] != Decimal('730000') + Decimal('778000')

    def test_el_rendimiento_de_marzo_es_24(self, app, db, marzo):
        """Agregado sumando, no promediando: 24,0 y no 24,25."""
        assert adaptador.rendimiento_de(marzo['vehiculo_id']) == Decimal('24')

    def test_un_mes_sin_gastos_da_CERO_medido_no_sin_dato(self, app, db, marzo):
        """Los dos ceros del canon, contra la base.

        Abril tiene lecturas de odómetro pero ningún gasto que caiga adentro: el
        CPK es **cero medido**, no `sin_dato`. Y el SOAT sí aporta, porque su
        período cubre abril."""
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add_all([
            LecturaOdometro(vehiculo_id=marzo['vehiculo_id'], valor_km=101100,
                            ts=datetime(2026, 4, 2, 8, 0), origen='cierre_dia',
                            autor_usuario_id=marzo['usuario_id']),
            LecturaOdometro(vehiculo_id=marzo['vehiculo_id'], valor_km=101300,
                            ts=datetime(2026, 4, 20, 8, 0), origen='cierre_dia',
                            autor_usuario_id=marzo['usuario_id'])])
        db.session.commit()
        # Las dos de abril también nacen sin foto: el cero de este test tiene
        # que salir del gasto, no de un tramo que nadie pudo respaldar.
        _alguien_paso_por_la_cola(db, marzo['vehiculo_id'], marzo['usuario_id'])
        r = adaptador.cpk_de(marzo['vehiculo_id'], date(2026, 4, 1),
                             date(2026, 4, 30))
        # 30 días de SOAT a $2.000, sobre 200 km.
        assert r['pesos'] == Decimal('60000')
        assert r['cpk'] == Decimal('300')

    def test_un_vehiculo_sin_ningun_gasto_da_SIN_DATO(self, app, db, mundo):
        """«Nadie registró nada» ≠ «no costó nada». Un CPK de $0 se leería como
        un vehículo gratis."""
        r = adaptador.cpk_de(mundo['sin_capacidad_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['cpk'] is SIN_DATO
        assert r['hubo_gastos'] is False

    def test_un_vehiculo_QUE_RODO_y_sin_gastos_tambien_da_SIN_DATO(
            self, app, db, mundo):
        """**El caso que separa los dos `sin_dato`, y el que faltaba.**

        La mutación 15 del arnés del 2026-09-02 —`hubo_gastos=True` fijo—
        sobrevivió a la primera pasada: el único vehículo sin gastos que los
        tests miraban tampoco tenía kilómetros, así que la guarda del odómetro
        contestaba primero y tapaba la del gasto. Un guard verde sobre una
        propiedad que la vía de prueba satisfacía por otra razón — la lección
        de los seis de la auditoría del 2026-08-15.

        Con dos lecturas y ningún gasto, el CPK **tiene** que ser `sin_dato`:
        un camión que rodó 300 km sin un peso registrado no costó cero pesos
        por kilómetro; es que nadie registró nada.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add_all([
            LecturaOdometro(vehiculo_id=mundo['sin_capacidad_id'], valor_km=100,
                            ts=datetime(2026, 3, 2, 8, 0), origen='cierre_dia',
                            autor_usuario_id=mundo['usuario_id']),
            LecturaOdometro(vehiculo_id=mundo['sin_capacidad_id'], valor_km=400,
                            ts=datetime(2026, 3, 20, 8, 0), origen='cierre_dia',
                            autor_usuario_id=mundo['usuario_id'])])
        db.session.commit()
        # **Verificarlas es parte del test, no un trámite.** Sin esto el
        # `sin_dato` seguiría saliendo, pero por el tramo dudoso: el guard
        # volvería a estar verde por una razón distinta de la que afirma, que es
        # el defecto que este mismo test nació para arreglar.
        _alguien_paso_por_la_cola(db, mundo['sin_capacidad_id'],
                                  mundo['usuario_id'])

        r = adaptador.cpk_de(mundo['sin_capacidad_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['km'] == 300
        assert r['hubo_gastos'] is False
        assert r['cpk'] is SIN_DATO
        assert r['cpk'] != Decimal('0')

    def test_los_kilometros_salen_de_la_VENTANA_no_de_toda_la_historia(
            self, app, db, marzo):
        """El denominador es el tramo de la ventana, no el odómetro entero.

        Sin el filtro, los kilómetros de abril entrarían al CPK de marzo: el
        numerador miraría un mes y el denominador otro, y el número resultante
        no sería el costo de ningún período.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add(LecturaOdometro(
            vehiculo_id=marzo['vehiculo_id'], valor_km=105000,
            ts=datetime(2026, 4, 15, 8, 0), origen='cierre_dia',
            autor_usuario_id=marzo['usuario_id']))
        db.session.commit()
        r = adaptador.cpk_de(marzo['vehiculo_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['km'] == 1000
        assert r['cpk'] == Decimal('840')

    def test_una_ventana_con_una_sola_lectura_da_SIN_DATO(self, app, db, mundo):
        """Una lectura no delimita un tramo. **No son 0 km recorridos**: es que
        no se puede medir, y un denominador inventado publicaría un CPK que
        nadie midió."""
        _tanquear(mundo, km=100000)
        r = adaptador.cpk_de(mundo['vehiculo_id'], date(2026, 3, 1),
                             date(2026, 3, 31))
        assert r['km'] == 0
        assert r['cpk'] is SIN_DATO

    def test_los_tanqueos_salen_ordenados_por_odometro(self, app, db, marzo):
        """Por kilometraje y no por fecha: una factura cargada tarde con la
        fecha del día en que se digitó desordenaría la serie por el lado que
        arma las ventanas."""
        km = [t['km'] for t in adaptador.tanqueos_de(marzo['vehiculo_id'])]
        assert km == sorted(km) == [100000, 100392, 100720]


# ══════════════════════════════════════════════════════════════════════════
# La frontera HTTP
# ══════════════════════════════════════════════════════════════════════════

def _cuerpo_tanqueo(placa, **extra):
    cuerpo = dict(placa=placa, fecha='2026-03-05', valor='168000',
                  galones='12', tanque='lleno', estacion='Terpel Neiva',
                  km=100000, proveedor='Terpel',
                  origen_costo='tarjeta_convenio')
    cuerpo.update(extra)
    return cuerpo


class TestSesionObligatoria:

    def test_los_tres_exigen_sesion(self, client, mundo):
        assert client.get(f"/flota/gastos/{mundo['placa']}").status_code == 401
        assert client.post('/flota/gastos', json={}).status_code == 401
        assert client.post('/flota/tanqueos', json={}).status_code == 401


class TestQuienPuedeQue:
    """La asimetría deliberada. Cada permiso se prueba en las dos direcciones:
    quien no puede recibe 403, y quien sí puede **no** recibe 403 — sin lo
    segundo, un `exige()` que rechazara a todo el mundo pasaría igual."""

    def test_el_conductor_SI_registra_un_tanqueo(self, client, mundo):
        r = client.post('/flota/tanqueos', json=_cuerpo_tanqueo(mundo['placa']),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()

    def test_el_conductor_NO_ve_el_CPK(self, client, mundo):
        """Un CPK en su pantalla está a un paso de leerse como una medida suya,
        y el número no mide a nadie (regla 2)."""
        r = client.get(f"/flota/gastos/{mundo['placa']}",
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 403
        assert r.get_json()['tu_rol'] == 'conductor'

    def test_el_conductor_NO_registra_un_SOAT(self, client, mundo):
        """El SOAT no lo paga el conductor. Es un maestro del vehículo, como la
        ficha técnica y los documentos."""
        r = client.post('/flota/gastos', json={
            'placa': mundo['placa'], 'categoria': 'soat', 'fecha': '2026-01-01',
            'valor': '730000', 'proveedor': 'Seguros',
            'origen_costo': 'credito_proveedor',
            'periodo_desde': '2026-01-01', 'periodo_hasta': '2026-12-31',
        }, headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_control_de_flota_SI_ve_el_CPK(self, client, mundo):
        r = client.get(f"/flota/gastos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 200

    def test_control_de_flota_SI_registra_un_SOAT(self, client, mundo):
        client.post('/flota/tanqueos', json=_cuerpo_tanqueo(mundo['placa']),
                    headers=_auth(mundo['t_flota']))
        r = client.post('/flota/gastos', json={
            'placa': mundo['placa'], 'categoria': 'soat', 'fecha': '2026-01-01',
            'valor': '730000', 'proveedor': 'Seguros',
            'origen_costo': 'credito_proveedor',
            'periodo_desde': '2026-01-01', 'periodo_hasta': '2026-12-31',
        }, headers=_auth(mundo['t_flota']))
        assert r.status_code == 201, r.get_json()

    def test_un_usuario_de_tienda_no_toca_nada(self, client, mundo):
        assert client.post('/flota/tanqueos', json=_cuerpo_tanqueo(mundo['placa']),
                           headers=_auth(mundo['t_tienda'])).status_code == 403
        assert client.get(f"/flota/gastos/{mundo['placa']}",
                          headers=_auth(mundo['t_tienda'])).status_code == 403


class TestLaFronteraNoAflojaNingunaPolitica:

    def test_el_endpoint_de_gastos_rechaza_combustible(self, client, mundo):
        """Un tanqueo por la puerta del gasto genérico entraría **sin galones**:
        sumaría al CPK y no aportaría un solo galón al rendimiento."""
        r = client.post('/flota/gastos', json={
            'placa': mundo['placa'], 'categoria': 'combustible',
            'fecha': '2026-03-05', 'valor': '168000', 'proveedor': 'Terpel',
            'origen_costo': 'sin_dato', 'km': 100000,
        }, headers=_auth(mundo['t_flota']))
        assert r.status_code == 400
        assert '/flota/tanqueos' in r.get_json()['error']

    def test_falta_de_campos_da_400_con_la_lista(self, client, mundo):
        r = client.post('/flota/tanqueos', json={'placa': mundo['placa']},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 400
        assert 'galones' in r.get_json()['error']
        assert 'tanque' in r.get_json()['error']

    def test_una_placa_que_no_existe_da_404(self, client, mundo):
        r = client.post('/flota/tanqueos', json=_cuerpo_tanqueo('NOEXISTE'),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 404

    def test_un_estado_de_tanque_inventado_da_409_no_500(self, client, mundo):
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], tanque='medio'),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409
        assert 'lleno' in r.get_json()['error']

    def test_una_fecha_mal_formada_da_400(self, client, mundo):
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], fecha='05/03/2026'),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 400

    def test_un_odometro_que_retrocede_da_409_no_500(self, client, mundo):
        client.post('/flota/tanqueos', json=_cuerpo_tanqueo(mundo['placa'], km=100392),
                    headers=_auth(mundo['t_flota']))
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], km=100000,
                                             documento_numero='F-2'),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409


class TestLoQueLaPantallaRecibe:

    def test_el_tanqueo_devuelve_su_precio_por_galon_calculado(self, client, mundo):
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], valor='168000',
                                             galones='12'),
                        headers=_auth(mundo['t_flota']))
        assert r.get_json()['tanqueo']['precio_galon'] == '14000.00'

    def test_el_tanqueo_excedido_lo_declara_y_devuelve_201(self, client, mundo):
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], galones='22'),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 201
        assert r.get_json()['tanqueo']['excede_capacidad'] is True

    def test_sin_capacidad_la_respuesta_dice_sin_dato_no_false(self, client, mundo):
        r = client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa_sin_capacidad'],
                                             galones='40', km=5000),
                        headers=_auth(mundo['t_flota']))
        assert r.get_json()['tanqueo']['excede_capacidad'] == 'sin_dato'

    def test_el_listado_trae_el_CPK_con_sus_dos_insumos(self, client, mundo):
        """Un número suelto no se puede auditar, y éste va a un tablero: quien
        lo mire tiene que poder rehacer la división."""
        client.post('/flota/tanqueos', json=_cuerpo_tanqueo(mundo['placa']),
                    headers=_auth(mundo['t_flota']))
        d = client.get(f"/flota/gastos/{mundo['placa']}?desde=2026-03-01"
                       f"&hasta=2026-03-31",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert 'cpk' in d and 'pesos_imputados' in d and 'km_recorridos' in d
        # Un solo tanqueo es UNA lectura, y una lectura no delimita un tramo:
        # desde el 2026-09-02 la marca lo dice con `sin_dato` en vez de afirmar
        # `declarada` sobre una resta que nunca se hizo. Los dos insumos siguen
        # viajando — es lo que permite ver por qué no hay número.
        assert d['cpk_marca'] == 'sin_dato'
        assert d['km_recorridos'] == 0

    def test_el_listado_publica_los_vocabularios_para_que_la_pantalla_no_los_copie(
            self, client, mundo):
        """Regla 0: si el JS escribiera su propia lista de categorías, el día
        que se agregue una el desplegable no la tendría y nadie lo notaría."""
        d = client.get(f"/flota/gastos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert 'combustible' in d['categorias']
        assert d['categorias_con_periodo'] == ['soat', 'rtm',
                                               'impuesto_vehicular', 'seguro']
        assert d['estados_tanque'] == ['lleno', 'parcial', 'sin_dato']
        assert 'sin_dato' in d['origenes_costo']

    def test_la_ventana_por_defecto_es_el_mes_en_curso(self, client, mundo):
        """En Bogotá, no en UTC: el 31 a las 8 p.m. `date.today()` ya es el mes
        siguiente y el CPK «del mes» sería el de un día."""
        from app.utils.fecha import dia_operativo

        d = client.get(f"/flota/gastos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['desde'] == dia_operativo().replace(day=1).isoformat()
        assert d['hasta'] == dia_operativo().isoformat()

    def test_una_ventana_invertida_da_400(self, client, mundo):
        r = client.get(f"/flota/gastos/{mundo['placa']}"
                       f"?desde=2026-03-31&hasta=2026-03-01",
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 400


class TestNingunMensajeAcusaANadie:
    """Regla 2, sobre lo que de verdad le llega a una persona.

    `test_costos.py` mira las cadenas del código por AST. Esto mira **las
    respuestas HTTP**, que es donde el defecto llegaría al usuario aunque el
    código estuviera limpio."""

    PROHIBIDAS = ('robo', 'hurto', 'ladrón', 'ladron', 'culpable', 'sisar')

    def test_ni_el_exceso_de_capacidad_ni_los_errores_imputan_nada(
            self, client, mundo):
        respuestas = [
            client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], galones='22'),
                        headers=_auth(mundo['t_flota'])),
            client.post('/flota/tanqueos',
                        json=_cuerpo_tanqueo(mundo['placa'], tanque='medio'),
                        headers=_auth(mundo['t_flota'])),
            client.get(f"/flota/gastos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])),
        ]
        for r in respuestas:
            texto = r.get_data(as_text=True).lower()
            malas = [p for p in self.PROHIBIDAS if p in texto]
            assert not malas, (
                f'la respuesta le muestra {malas} a una persona. El sistema '
                f'afirma que dos datos no pueden ser los dos ciertos; a quién '
                f'se le cobra lo decide un humano (regla 2).')


class TestElHealthCuentaLaPlata:
    """Las medidas nacen con la tabla, no un mes después.

    `flota_lectura_odometro` vivió un mes con cero campos en el health y por eso
    nada avisó de las 26 lecturas sin foto ni del salto de 16,3 millones de km:
    un sistema de captura sin un solo lector. Acá los cuatro campos nacen con
    `flota_gasto`.
    """

    def test_los_cuatro_campos_estan_en_el_health(self):
        from flota.api.health import _CAMPOS

        for campo in ('tanqueos_sobre_capacidad',
                      'tanqueos_sin_capacidad_declarada',
                      'gastos_sin_documento', 'cpk_mes'):
            assert campo in _CAMPOS

    def test_el_endpoint_los_devuelve(self, client, mundo):
        d = client.get('/flota/health', headers=_auth(mundo['t_flota'])).get_json()
        assert d['gastos_sin_documento'] == 0
        assert d['tanqueos_sobre_capacidad'] == 0
        assert d['cpk_mes'] == []

    def test_cuenta_los_gastos_sin_documento_y_NO_los_que_lo_tienen(
            self, app, db, mundo):
        """Las dos direcciones. Un contador que suma todo pasaría la primera
        mitad, y el número dejaría de contestar la pregunta que existe para
        contestar: si la operación está entregando las facturas."""
        from flota.adaptadores.medicion import MedidorSQL

        _tanquear(mundo, km=100000, documento_numero='FAC-1')
        _tanquear(mundo, km=100392, ts=datetime(2026, 3, 15, 10, 0))
        assert MedidorSQL().gastos_sin_documento() == 1

    def test_cuenta_los_tanqueos_excedidos_y_NO_los_normales(self, app, db, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        _tanquear(mundo, km=100000, galones='12')
        _tanquear(mundo, km=100392, galones='22',
                  ts=datetime(2026, 3, 15, 10, 0))
        assert MedidorSQL().tanqueos_sobre_capacidad() == 1

    def test_los_que_no_se_pudieron_revisar_van_APARTE(self, app, db, mundo):
        """**El contador que impide que el detector se apague en silencio.**

        Un tanqueo de un vehículo sin capacidad en la ficha no es un tanqueo
        limpio: es uno que nadie miró. Sumado a cero excesos, un parque entero
        sin capacidad levantada se ve idéntico a un parque sano."""
        from flota.adaptadores.medicion import MedidorSQL

        _tanquear(mundo, km=100000, galones='12')          # con capacidad, sano
        adaptador.registrar_tanqueo(
            vehiculo_id=mundo['sin_capacidad_id'], fecha=date(2026, 3, 5),
            valor='300000', galones='40', tanque='lleno', estacion='Terpel',
            km=5000, proveedor='Terpel', origen_costo='sin_dato',
            registrado_por_usuario_id=mundo['usuario_id'])
        medidor = MedidorSQL()
        assert medidor.tanqueos_sobre_capacidad() == 0
        assert medidor.tanqueos_sin_capacidad_declarada() == 1

    def test_el_cpk_del_mes_sale_por_vehiculo_y_solo_de_los_que_tienen_gastos(
            self, app, db, mundo):
        """Una lista y no un promedio de flota: el canon §3 dice que el CPK no
        compara vehículos. Y los vehículos sin un peso registrado **no salen con
        cero**: no salen."""
        from flota.adaptadores.medicion import MedidorSQL

        _tanquear(mundo, km=100000)
        filas = MedidorSQL().cpk_mes()
        assert [f['placa'] for f in filas] == [mundo['placa']]
        assert 'cpk' in filas[0] and 'pesos' in filas[0] and 'km' in filas[0]

    def test_el_cpk_no_sale_en_notacion_cientifica(self, app, db, mundo):
        """`1.400E+4` en un tablero no es un número: es un error de lectura
        esperando. Ya pasó en este módulo con `Carlos Pérez · undefined`."""
        from flota.adaptadores.medicion import MedidorSQL

        _tanquear(mundo, km=100000, valor='120000')
        for fila in MedidorSQL().cpk_mes():
            assert 'E+' not in fila['cpk'] and 'E+' not in fila['pesos']


class TestElEndpointEstaMontado:
    """Trinquete propio: un blueprint escrito y no registrado es superficie que
    responde 404 y nadie lo nota hasta que la pantalla falla."""

    def test_las_tres_rutas_existen_en_el_url_map(self, app):
        rutas = {str(r) for r in app.url_map.iter_rules()}
        assert '/flota/gastos' in rutas
        assert '/flota/gastos/<placa>' in rutas
        assert '/flota/tanqueos' in rutas

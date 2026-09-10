"""
La puerta del taller — adaptador y frontera HTTP, contra la base real.

`tests/flota/test_garantia.py` prueba la aritmética. Acá se prueba lo otro: que
la fila quede escrita, que la regla 3 se cumpla sin depender de que alguien se
acuerde, y que **la frontera no afloje ninguna política del adaptador**. Un
endpoint que acepta lo que el adaptador rechaza es un rodeo alrededor de todo lo
demás — y como devuelve 201, nadie se entera.

## Las cuatro propiedades que este archivo existe para clavar

1. **La OT no lleva el costo, y la ausencia se cuenta.** El camión entra hoy y
   la factura llega el 30. Si la tabla tuviera `valor NOT NULL` habría que
   inventar un número para cerrarla.
2. **La garantía se calcula, no se teclea.** Un cuerpo que mande
   `garantia_hasta_fecha` devuelve **400**, no un silencio.
3. **La búsqueda de garantía vigente propone y no bloquea.** Abrir una OT sobre
   un sistema con garantía viva **funciona**: lo que cambia es que el caso sale
   en la respuesta.
4. **El hallazgo se cierra por `hallazgos.cerrar`.** Una segunda vía sería la
   que se olvide de mover `cerrado_ts`, el campo del que sale
   `dias_hallazgo_abierto`.

Y todas en las dos direcciones: que el guard dispare al violarlo, y que **NO**
dispare sobre operación sana.
"""
from datetime import date, datetime

import pytest

from flota.adaptadores import taller as adaptador
from flota.dominio.valores import SIN_DATO

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


@pytest.fixture
def mundo(db, almacen):
    """Un vehículo, otro sin lecturas, y las tres clases de usuario."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='TLR100', tipo='NHR', activo=True)
    virgen = Vehiculo(placa='TLR200', tipo='NHR', activo=True)
    cond = Usuario(nombre='Conductor TLR', email='tlr_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control TLR', email='tlr_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda TLR', email='tlr_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    # Desde el 2026-09-09 abrir, cerrar y anular una orden son `DECIDE_FLOTA`
    # (gestión): mandar un camión al taller compromete plata. Registrar el
    # trabajo y la factura siguen siendo de control de flota.
    jefe = Usuario(nombre='Gestion TLR', email='tlr_gestion@test.com',
                   password_hash=generate_password_hash('x'), rol='admin',
                   almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, virgen, cond, flota, tienda, jefe])
    db.session.commit()
    return {
        'vehiculo_id': veh.id, 'placa': veh.placa,
        'virgen_id': virgen.id, 'placa_virgen': virgen.placa,
        'usuario_id': flota.id,
        'usuario_gestion_id': jefe.id,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_gestion': create_access_token(identity=str(jefe.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        'db': db,
    }


def _abrir(mundo, *, km=100000, tipo='correctiva', hallazgo_id=None,
           ts=None, descripcion='revisar ruido al embragar'):
    return adaptador.abrir(
        vehiculo_id=mundo['vehiculo_id'], tipo=tipo,
        taller='Taller Los Andes', descripcion=descripcion, km=km,
        abierta_por_usuario_id=mundo['usuario_id'], hallazgo_id=hallazgo_id,
        ts=ts or datetime(2026, 3, 5, 14, 0))


def _trabajo(orden, mundo, *, sistema='embrague', declarada='si', meses=6,
             kms=None, descripcion=None, ts=None):
    return adaptador.registrar_intervencion(
        orden_trabajo_id=orden.id, sistema=sistema,
        garantia_declarada=declarada, garantia_meses=meses, garantia_km=kms,
        descripcion=descripcion, registrada_por_usuario_id=mundo['usuario_id'],
        ts=ts or datetime(2026, 3, 5, 14, 30))


# ══════════════════════════════════════════════════════════════════════════
# Regla 3 — sin odómetro no se persiste ningún evento de flota
# ══════════════════════════════════════════════════════════════════════════

class TestLaLecturaSeAnclaNoSeFabrica:
    """`hallazgos.anclar_odometro`, **no una copia** (regla 0). El valor `ot` del
    vocabulario existía desde la tanda 1 y no tenía nada detrás: esta tabla es lo
    que había detrás."""

    def test_la_orden_nace_con_su_lectura_y_con_origen_ot(self, app, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        o = _abrir(mundo, km=100000)
        lec = LecturaOdometro.query.get(o.lectura_id)
        assert lec is not None
        assert lec.valor_km == 100000
        assert lec.origen == 'ot'
        assert lec.vehiculo_id == mundo['vehiculo_id']

    def test_dos_ordenes_con_el_mismo_km_comparten_lectura(self, app, mundo):
        """Escribir una lectura por cada orden produciría desde adentro del
        sistema el ruido que `lecturas_ts_duplicado` existe para contar."""
        from flota.adaptadores.modelos import LecturaOdometro

        a = _abrir(mundo, km=100000)
        b = _abrir(mundo, km=100000, ts=datetime(2026, 3, 6, 9, 0))
        assert a.lectura_id == b.lectura_id
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == 1

    def test_un_km_distinto_SI_produce_una_lectura_nueva(self, app, mundo):
        """La otra dirección: el odómetro se movió y eso es información nueva.
        Reutilizar siempre guardaría un kilometraje que nadie midió."""
        from flota.adaptadores.modelos import LecturaOdometro

        a = _abrir(mundo, km=100000)
        b = _abrir(mundo, km=101000, ts=datetime(2026, 3, 20, 9, 0))
        assert a.lectura_id != b.lectura_id
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['vehiculo_id']).count() == 2

    def test_un_odometro_que_retrocede_no_entra_por_esta_puerta(self, app, mundo):
        _abrir(mundo, km=100000)
        with pytest.raises(Exception):
            _abrir(mundo, km=90000, ts=datetime(2026, 3, 20, 9, 0))

    def test_si_la_orden_revienta_no_queda_la_lectura_suelta(self, app, mundo):
        """Una lectura `ot` sin orden es exactamente lo que
        `MOTIVO_ORIGEN_NO_SUELTO` dice que no puede existir."""
        from flota.adaptadores.modelos import LecturaOdometro, OrdenTrabajo

        antes = LecturaOdometro.query.count()
        with pytest.raises(adaptador.OrdenInvalida):
            adaptador.abrir(
                vehiculo_id=mundo['vehiculo_id'], tipo='correctiva',
                taller='   ', descripcion='x', km=100000,
                abierta_por_usuario_id=mundo['usuario_id'])
        assert LecturaOdometro.query.count() == antes
        assert OrdenTrabajo.query.count() == 0


# ══════════════════════════════════════════════════════════════════════════
# La OT no lleva el costo — y por eso la ausencia se cuenta
# ══════════════════════════════════════════════════════════════════════════

class TestLaOrdenNoLlevaElCosto:
    """*Si aparece una columna `valor`, `costo` o `monto` en una tabla `flota_*`
    fuera de `flota_gasto`, la frontera se cruzó.* El trinquete del plan, escrito
    contra los modelos y no contra el texto del archivo."""

    def test_ninguna_tabla_de_flota_fuera_de_gasto_tiene_columna_de_plata(
            self, app):
        from app.extensions import db as _db

        prohibidas = ('valor', 'costo', 'monto', 'precio', 'total')
        malas = []
        for nombre, tabla in _db.metadata.tables.items():
            if not nombre.startswith('flota_') or nombre == 'flota_gasto':
                continue
            for col in tabla.columns:
                # `valor_km` es un kilometraje, no plata: lo que se persigue es
                # una segunda columna de PESOS, que es la que convierte el CPK
                # en un UNION de N ramas.
                if col.name in prohibidas:
                    malas.append(f'{nombre}.{col.name}')
        assert not malas, (
            f'\nColumnas de plata fuera de `flota_gasto`: {malas}\n'
            'Si cada tabla lleva su propia columna de valor, el costo por '
            'kilómetro es un UNION de N ramas y alguien va a olvidar la N+1. '
            'Es `_BODEGA_CO_MAP` otra vez, con plata.')

    def test_el_detector_ve_el_patron(self, app):
        """Sin esto, un cambio en el criterio deja el guard en verde para
        siempre — que es la forma exacta del trinquete que mide una proxy."""
        assert 'valor' in [c.name for c in
                           __import__('app').extensions.db.metadata
                           .tables['flota_gasto'].columns]

    def test_una_orden_se_cierra_sin_saber_cuanto_costo(self, app, mundo):
        """El caso de negocio literal: el camión entra hoy, la factura llega el
        30. Con `valor NOT NULL` habría que inventar un número."""
        o = _abrir(mundo)
        _trabajo(o, mundo)
        cerrada = adaptador.cerrar(orden_trabajo_id=o.id,
                                   usuario_id=mundo['usuario_id'])
        assert cerrada.estado == 'cerrada'
        assert adaptador.sin_factura_de(cerrada)


# ══════════════════════════════════════════════════════════════════════════
# La garantía se CALCULA al nacer (regla 6 aplicada a otra cosa)
# ══════════════════════════════════════════════════════════════════════════

class TestLaGarantiaNoSeTeclea:

    def test_la_fecha_sale_del_plazo_y_del_dia_del_trabajo(self, app, mundo):
        o = _abrir(mundo, km=100000)
        i = _trabajo(o, mundo, meses=6, ts=datetime(2026, 3, 5, 14, 30))
        assert i.garantia_hasta_fecha == date(2026, 9, 5)

    def test_el_km_sale_del_odometro_DE_LA_ORDEN_no_del_de_hoy(self, app, mundo):
        """Contra el de hoy la garantía se llevaría de regalo los kilómetros que
        pasaron entre el taller y el momento en que alguien tecleó la factura."""
        from flota.adaptadores.hallazgos import anclar_odometro

        o = _abrir(mundo, km=100000)
        # El camión sigue rodando mientras está el trabajo pendiente de cargar.
        anclar_odometro(mundo['vehiculo_id'], 118000, mundo['usuario_id'],
                        datetime(2026, 4, 1, 8, 0))
        i = _trabajo(o, mundo, meses=None, kms=10000,
                     ts=datetime(2026, 4, 1, 9, 0))
        assert i.garantia_hasta_km == 110000
        assert i.garantia_hasta_km != 128000

    def test_el_dia_se_calcula_en_BOGOTA_no_en_UTC(self, app, mundo):
        """A las 8 p.m. de Colombia, en UTC ya es el día siguiente. Con
        `utcnow().date()` la garantía empezaría a contar un día tarde y vencería
        un día tarde — y el día en que se discuta va a ser exactamente el
        último. Regla 5 del WMS."""
        o = _abrir(mundo, km=100000, ts=datetime(2026, 3, 5, 14, 0))
        # 2026-03-06 02:00 UTC = 2026-03-05 21:00 en Bogotá.
        i = _trabajo(o, mundo, meses=6, ts=datetime(2026, 3, 6, 2, 0))
        assert i.garantia_hasta_fecha == date(2026, 9, 5)

    def test_una_garantia_de_meses_Y_km_calcula_las_dos(self, app, mundo):
        o = _abrir(mundo, km=100000)
        i = _trabajo(o, mundo, meses=6, kms=10000)
        assert i.garantia_hasta_fecha == date(2026, 9, 5)
        assert i.garantia_hasta_km == 110000

    def test_el_recorte_de_fin_de_mes_llega_hasta_la_fila(self, app, mundo):
        """El caso del dominio, ejercido de punta a punta: si el adaptador
        hiciera su propia suma con `timedelta(days=30)`, esto lo dice."""
        o = _abrir(mundo, km=100000, ts=datetime(2026, 1, 31, 14, 0))
        i = _trabajo(o, mundo, meses=1, ts=datetime(2026, 1, 31, 15, 0))
        assert i.garantia_hasta_fecha == date(2026, 2, 28)


class TestLaCoherenciaDeLaGarantia:

    def test_declarar_si_sin_ningun_plazo_levanta(self, app, mundo):
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            _trabajo(o, mundo, declarada='si', meses=None, kms=None)
        assert 'se ve verde' in str(e.value)

    def test_declarar_no_con_plazos_levanta_en_vez_de_ignorarlos(self, app, mundo):
        """Ignorarlo dejaría a quien lo mandó creyendo que la garantía quedó
        registrada, que es peor que un error — mismo criterio que
        `gastos._periodo`."""
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida):
            _trabajo(o, mundo, declarada='no', meses=6)

    def test_sin_dato_tampoco_admite_plazos(self, app, mundo):
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida):
            _trabajo(o, mundo, declarada='sin_dato', kms=10000)

    def test_sin_dato_SIN_plazos_es_una_respuesta_legitima(self, app, mundo):
        """La otra dirección, y es el punto entero de la regla 4: una factura
        que no dice nada **no es una factura sin garantía**."""
        o = _abrir(mundo)
        i = _trabajo(o, mundo, declarada='sin_dato', meses=None)
        assert i.garantia_declarada == 'sin_dato'
        assert i.garantia_hasta_fecha is None
        assert i.garantia_hasta_km is None

    def test_una_garantia_de_cero_meses_levanta(self, app, mundo):
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            _trabajo(o, mundo, meses=0)
        assert 'no se declaró' in str(e.value)

    def test_un_plazo_ilegible_levanta_en_vez_de_guardarse_como_ausente(
            self, app, mundo):
        """Un `int()` con `except: None` produciría una fila que dice «garantía
        sí» y no cubre nada — y eso no se ve raro hasta el día que se reclama."""
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida):
            _trabajo(o, mundo, meses='seis')

    def test_sistema_otro_exige_descripcion(self, app, mundo):
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida):
            _trabajo(o, mundo, sistema='otro', descripcion='  ')

    def test_sistema_otro_CON_descripcion_pasa(self, app, mundo):
        o = _abrir(mundo)
        i = _trabajo(o, mundo, sistema='otro', descripcion='cambio de espejo')
        assert i.sistema == 'otro'

    def test_la_base_tambien_lo_impide_no_solo_el_adaptador(self, app, mundo, db):
        """El CHECK, ejercido con un INSERT que esquiva el adaptador. Si un
        `INSERT` crudo puede violar el invariante, el modelo está incompleto.

        Lo que la base SÍ puede respaldar: **un vencimiento que no viene de
        ningún plazo declarado**. No puede verificar la aritmética; eso lo
        garantiza que haya una sola puerta.
        """
        from sqlalchemy.exc import IntegrityError

        from flota.adaptadores.modelos import Intervencion

        o = _abrir(mundo)
        db.session.add(Intervencion(
            orden_trabajo_id=o.id, sistema='frenos', garantia_declarada='no',
            garantia_meses=None, garantia_km=None,
            garantia_hasta_fecha=date(2027, 1, 1), garantia_hasta_km=None,
            registrada_ts=datetime(2026, 3, 5), registrada_por_usuario_id=1))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_y_la_via_sana_pasa_ese_mismo_CHECK(self, app, mundo, db):
        """La otra dirección: un CHECK que rechazara todo pasaría el test de
        arriba. Es una de las seis formas fechadas en `CLAUDE.md`."""
        from flota.adaptadores.modelos import Intervencion

        o = _abrir(mundo)
        db.session.add(Intervencion(
            orden_trabajo_id=o.id, sistema='frenos', garantia_declarada='si',
            garantia_meses=6, garantia_km=None,
            garantia_hasta_fecha=date(2026, 9, 5), garantia_hasta_km=None,
            registrada_ts=datetime(2026, 3, 5),
            registrada_por_usuario_id=mundo['usuario_id']))
        db.session.commit()
        assert Intervencion.query.count() == 1


# ══════════════════════════════════════════════════════════════════════════
# Lo que hace que valga la plata — la búsqueda de garantía vigente
# ══════════════════════════════════════════════════════════════════════════

class TestLaBusquedaDeGarantiaVigente:

    def _con_garantia(self, mundo, *, sistema='embrague', meses=6, kms=None,
                      km=100000):
        o = _abrir(mundo, km=km)
        _trabajo(o, mundo, sistema=sistema, meses=meses, kms=kms)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        return o

    def test_encuentra_la_del_mismo_sistema(self, app, mundo):
        self._con_garantia(mundo, sistema='embrague')
        vivas = adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2026, 7, 1), sistema='embrague')
        assert len(vivas) == 1
        assert vivas[0]['sistema'] == 'embrague'
        assert vivas[0]['cubre'] is True

    def test_NO_devuelve_la_de_otro_sistema(self, app, mundo):
        """La otra dirección. Sin esto, un filtro degenerado en «todas» pasaría
        el test de arriba y propondría reclamar el embrague por unos frenos."""
        self._con_garantia(mundo, sistema='embrague')
        assert adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2026, 7, 1), sistema='frenos') == []

    def test_NO_devuelve_la_de_otro_vehiculo(self, app, mundo):
        self._con_garantia(mundo, sistema='embrague')
        assert adaptador.garantias_vigentes_de(
            mundo['virgen_id'], date(2026, 7, 1)) == []

    def test_una_vencida_no_se_propone(self, app, mundo):
        self._con_garantia(mundo, sistema='embrague', meses=6)
        assert adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2027, 7, 1), sistema='embrague') == []

    def test_una_que_NO_se_pudo_juzgar_no_cuenta_como_vigente(self, app, mundo):
        """El vehículo sin odómetro y una garantía solo por kilómetros. `cubre`
        es `False` y `vigente` es `SIN_DATO` — contarla sería el default
        optimista de la regla 1 en el número que decide si se reclama."""
        o = adaptador.abrir(
            vehiculo_id=mundo['virgen_id'], tipo='correctiva',
            taller='T', descripcion='x', km=50000,
            abierta_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 5, 14, 0))
        _trabajo(o, mundo, sistema='frenos', meses=None, kms=10000)
        # Se le quita el odómetro al vehículo para reproducir el caso: la
        # garantía existe y no hay contra qué compararla.
        detalle = adaptador.vigencia_de(o.intervenciones[0], date(2026, 4, 1),
                                        km_actual=None)
        assert detalle['por_km'] is SIN_DATO
        assert detalle['vigente'] is SIN_DATO
        assert detalle['cubre'] is False

    def test_una_orden_anulada_no_deja_garantia(self, app, mundo):
        """Si la visita no ocurrió, su garantía tampoco. Una orden anulada no
        puede tener trabajos, así que este caso solo se puede armar por la
        puerta de atrás — y por eso se arma así."""
        from flota.adaptadores.modelos import OrdenTrabajo

        o = self._con_garantia(mundo, sistema='embrague')
        fila = OrdenTrabajo.query.get(o.id)
        fila.estado = 'anulada'
        fila.motivo_cierre = 'cargada sobre el camión equivocado'
        mundo['db'].session.commit()
        assert adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2026, 7, 1)) == []

    def test_las_declaradas_no_o_sin_dato_no_entran(self, app, mundo):
        o = _abrir(mundo)
        _trabajo(o, mundo, sistema='frenos', declarada='no', meses=None)
        _trabajo(o, mundo, sistema='motor', declarada='sin_dato', meses=None)
        assert adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2026, 4, 1)) == []

    def test_PROPONE_y_NO_bloquea(self, app, mundo):
        """El caso entero de la fase, y la propiedad más fácil de romper por
        «mejorarla»: abrir una orden sobre un sistema con garantía viva
        **funciona**. Bloquear dejaría un camión roto en patio por un dato que
        puede estar mal levantado."""
        self._con_garantia(mundo, sistema='embrague')
        otra = _abrir(mundo, km=105000, ts=datetime(2026, 4, 10, 9, 0),
                      descripcion='vuelve a patinar el embrague')
        assert otra.estado == 'abierta'
        assert adaptador.garantias_vigentes_de(
            mundo['vehiculo_id'], date(2026, 4, 10), sistema='embrague')

    def test_un_sistema_inventado_en_el_filtro_levanta(self, app, mundo):
        with pytest.raises(ValueError):
            adaptador.garantias_vigentes_de(
                mundo['vehiculo_id'], date(2026, 4, 1), sistema='cardan')


# ══════════════════════════════════════════════════════════════════════════
# El desenlace de la orden
# ══════════════════════════════════════════════════════════════════════════

class TestCerrarYAnular:

    def test_no_se_puede_cerrar_una_orden_sin_ningun_trabajo(self, app, mundo):
        """Cerrarla dejaría una visita al taller sin registro de qué se hizo, y
        es el camino barato de la regla 11: cerrar es lo que saca el renglón
        rojo del tablero."""
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            adaptador.cerrar(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'])
        assert 'sin registro de qué se hizo' in str(e.value)

    def test_con_un_trabajo_SI_se_cierra(self, app, mundo):
        o = _abrir(mundo)
        _trabajo(o, mundo)
        assert adaptador.cerrar(
            orden_trabajo_id=o.id, usuario_id=mundo['usuario_id']).estado == 'cerrada'

    def test_anular_exige_motivo_escrito(self, app, mundo):
        o = _abrir(mundo)
        with pytest.raises(adaptador.OrdenInvalida):
            adaptador.anular(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'], motivo='   ')

    def test_anular_con_motivo_pasa_y_lo_guarda(self, app, mundo):
        o = _abrir(mundo)
        a = adaptador.anular(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'],
                             motivo='se cargó sobre el camión equivocado')
        assert a.estado == 'anulada'
        assert 'camión equivocado' in a.motivo_cierre

    def test_no_se_anula_una_orden_con_trabajos_hechos(self, app, mundo):
        """Eso ya ocurrió: anularlo lo borraría del expediente."""
        o = _abrir(mundo)
        _trabajo(o, mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            adaptador.anular(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'], motivo='no fue nada')
        assert 'se cierra' in str(e.value)

    def test_cerrar_dos_veces_levanta(self, app, mundo):
        o = _abrir(mundo)
        _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(adaptador.OrdenInvalida):
            adaptador.cerrar(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'])

    def test_no_se_agregan_trabajos_a_una_orden_cerrada(self, app, mundo):
        o = _abrir(mundo)
        _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(adaptador.OrdenInvalida):
            _trabajo(o, mundo, sistema='frenos')


class TestElHallazgoSeCierraPorSuPuerta:
    """`hallazgos.cerrar`, no un `UPDATE` propio. La segunda vía es siempre la
    que se olvida de mover `cerrado_ts` — el campo del que sale
    `dias_hallazgo_abierto`."""

    def _hallazgo(self, mundo, km=99000):
        from flota.adaptadores import hallazgos

        return hallazgos.reportar(
            vehiculo_id=mundo['vehiculo_id'], criticidad='mayor',
            descripcion='patina el embrague', km=km,
            reportado_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 1, 8, 0))

    def test_la_orden_puede_nacer_de_un_hallazgo(self, app, mundo):
        h = self._hallazgo(mundo)
        o = _abrir(mundo, hallazgo_id=h.id)
        assert o.hallazgo_id == h.id

    def test_un_hallazgo_de_otro_vehiculo_levanta(self, app, mundo):
        """Colgarlo acá haría que cerrar la orden cerrara el daño equivocado, y
        `dias_hallazgo_abierto` se publicaría sobre una reparación que nunca
        ocurrió."""
        from flota.adaptadores import hallazgos

        h = hallazgos.reportar(
            vehiculo_id=mundo['virgen_id'], criticidad='menor',
            descripcion='espejo rayado', km=10,
            reportado_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 1, 8, 0))
        with pytest.raises(adaptador.OrdenInvalida) as e:
            _abrir(mundo, hallazgo_id=h.id)
        assert 'otro vehículo' in str(e.value)

    def test_un_hallazgo_ya_cerrado_levanta(self, app, mundo):
        from flota.adaptadores import hallazgos

        h = self._hallazgo(mundo)
        hallazgos.cerrar(hallazgo_id=h.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(adaptador.OrdenInvalida):
            _abrir(mundo, hallazgo_id=h.id)

    def test_cerrar_la_orden_puede_cerrar_el_hallazgo(self, app, mundo):
        from flota.adaptadores.modelos import Hallazgo

        h = self._hallazgo(mundo)
        o = _abrir(mundo, hallazgo_id=h.id)
        _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'],
                         cerrar_hallazgo=True,
                         ts=datetime(2026, 3, 10, 17, 0))
        fila = Hallazgo.query.get(h.id)
        assert fila.estado == 'cerrado'
        # El campo del que sale `dias_hallazgo_abierto`: si la orden lo cerrara
        # con un UPDATE propio, esto es lo que se olvidaría.
        assert fila.cerrado_ts == datetime(2026, 3, 10, 17, 0)
        assert 'orden de trabajo' in fila.motivo_cierre

    def test_por_defecto_NO_lo_cierra(self, app, mundo):
        """Un camión puede volver del taller con el daño todavía abierto —faltó
        un repuesto, se arregló otra cosa—. Cerrarlo por defecto inventaría una
        reparación."""
        from flota.adaptadores.modelos import Hallazgo

        h = self._hallazgo(mundo)
        o = _abrir(mundo, hallazgo_id=h.id)
        _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        assert Hallazgo.query.get(h.id).estado == 'abierto'

    def test_pedir_cerrar_un_hallazgo_que_no_existe_levanta(self, app, mundo):
        o = _abrir(mundo)
        _trabajo(o, mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            adaptador.cerrar(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'],
                             cerrar_hallazgo=True)
        assert 'no nació de ningún daño' in str(e.value)

    def test_si_el_hallazgo_ya_lo_cerro_otro_la_orden_NO_se_cierra(
            self, app, mundo):
        """Las dos cosas van en la misma transacción. Cerrar la orden y dejar el
        error del hallazgo sin efecto dejaría a quien lo pidió creyendo que las
        dos ocurrieron."""
        from flota.adaptadores import hallazgos
        from flota.adaptadores.modelos import OrdenTrabajo

        h = self._hallazgo(mundo)
        o = _abrir(mundo, hallazgo_id=h.id)
        _trabajo(o, mundo)
        hallazgos.cerrar(hallazgo_id=h.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(Exception):
            adaptador.cerrar(orden_trabajo_id=o.id,
                             usuario_id=mundo['usuario_id'],
                             cerrar_hallazgo=True)
        assert OrdenTrabajo.query.get(o.id).estado == 'abierta'

    def test_anular_NO_toca_el_hallazgo(self, app, mundo):
        """Si la visita se anuló, el daño sigue ahí: cerrarlo inventaría una
        reparación que nadie hizo."""
        from flota.adaptadores.modelos import Hallazgo

        h = self._hallazgo(mundo)
        o = _abrir(mundo, hallazgo_id=h.id)
        adaptador.anular(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'],
                         motivo='el taller no lo recibió')
        assert Hallazgo.query.get(h.id).estado == 'abierto'


# ══════════════════════════════════════════════════════════════════════════
# La factura que llega después — la segunda extremidad de `flota_gasto`
# ══════════════════════════════════════════════════════════════════════════

class TestLaFacturaCuelgaDeLaEspina:

    def _visita(self, mundo, *, km=100000):
        o = _abrir(mundo, km=km)
        a = _trabajo(o, mundo, sistema='embrague', meses=6)
        b = _trabajo(o, mundo, sistema='frenos', declarada='no', meses=None)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        return o, a, b

    def _facturar(self, mundo, orden, ids, **extra):
        datos = dict(categoria='mantenimiento', fecha=date(2026, 4, 30),
                     valor='1200000', proveedor='Taller Los Andes',
                     origen_costo='credito_proveedor')
        datos.update(extra)
        return adaptador.registrar_factura(
            orden_trabajo_id=orden.id, intervencion_ids=ids,
            registrado_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 4, 30, 10, 0), **datos)

    def test_el_valor_vive_en_flota_gasto_y_los_trabajos_apuntan_ahi(
            self, app, mundo):
        from flota.adaptadores.modelos import Intervencion

        o, a, b = self._visita(mundo)
        g = self._facturar(mundo, o, [a.id, b.id])
        assert str(g.valor) == '1200000.00'
        assert Intervencion.query.get(a.id).gasto_id == g.id
        assert Intervencion.query.get(b.id).gasto_id == g.id

    def test_N_trabajos_sobre_UN_gasto_no_duplican_un_peso(self, app, mundo):
        """El CPK suma la espina, no las extremidades. Es la razón exacta por la
        que el valor vive en un solo lugar."""
        from flota.adaptadores.gastos import cpk_de

        o, a, b = self._visita(mundo)
        self._facturar(mundo, o, [a.id, b.id])
        r = cpk_de(mundo['vehiculo_id'], date(2026, 4, 1), date(2026, 4, 30))
        assert r['pesos'] == 1200000

    def test_la_factura_NO_pide_kilometraje_y_se_ancla_a_la_de_la_orden(
            self, app, mundo):
        """La factura se digita treinta días después, en una oficina, sin el
        vehículo delante. El kilometraje correcto es el del trabajo."""
        from flota.adaptadores.hallazgos import anclar_odometro

        o, a, _b = self._visita(mundo, km=100000)
        anclar_odometro(mundo['vehiculo_id'], 118000, mundo['usuario_id'],
                        datetime(2026, 4, 20, 8, 0))
        g = self._facturar(mundo, o, [a.id])
        assert g.lectura_id == o.lectura_id
        assert g.lectura.valor_km == 100000

    def test_facturar_dos_veces_el_mismo_trabajo_levanta(self, app, mundo):
        """Sumaría el mismo trabajo dos veces al CPK, y un CPK inflado es un
        número plausible que nadie puede desmentir después."""
        o, a, b = self._visita(mundo)
        self._facturar(mundo, o, [a.id], documento_numero='F-1')
        with pytest.raises(adaptador.OrdenInvalida) as e:
            self._facturar(mundo, o, [a.id, b.id], documento_numero='F-2')
        assert 'ya tienen factura' in str(e.value)

    def test_una_lista_vacia_levanta(self, app, mundo):
        o, _a, _b = self._visita(mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            self._facturar(mundo, o, [])
        assert 'qué trabajos cubre' in str(e.value)

    def test_un_trabajo_de_otra_orden_levanta(self, app, mundo):
        o, a, _b = self._visita(mundo)
        otra, c, _d = self._visita(mundo, km=120000)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            self._facturar(mundo, o, [a.id, c.id])
        assert 'no son de la orden' in str(e.value)

    def test_una_categoria_que_no_es_de_taller_levanta(self, app, mundo):
        """Por acá el gasto quedaría colgado de una lectura que dice que nació
        de una orden de trabajo."""
        o, a, _b = self._visita(mundo)
        with pytest.raises(adaptador.OrdenInvalida) as e:
            self._facturar(mundo, o, [a.id], categoria='soat')
        assert 'no se registra como' in str(e.value)

    def test_si_la_vinculacion_revienta_no_queda_el_gasto(self, app, mundo):
        """Un gasto escrito con los trabajos sin vincular sería plata en el CPK
        y trabajos que siguen figurando sin factura."""
        from flota.adaptadores.modelos import Gasto

        o, a, _b = self._visita(mundo)
        antes = Gasto.query.count()
        with pytest.raises(adaptador.OrdenInvalida):
            self._facturar(mundo, o, [a.id, 999999])
        assert Gasto.query.count() == antes

    def test_facturar_una_sola_de_las_dos_deja_la_otra_pendiente(self, app, mundo):
        """La otra dirección de «no hay default de todas»."""
        o, a, b = self._visita(mundo)
        self._facturar(mundo, o, [a.id])
        pendientes = [i.id for i in adaptador.sin_factura_de(o)]
        assert pendientes == [b.id]


class TestLaLecturaDeclaradaEnLaPuertaDelGasto:
    """La tercera rama de `gastos._lectura_para`, y lo que se verifica de ella.
    Un ancla equivocada es peor que ninguna."""

    def test_una_lectura_de_otro_vehiculo_levanta(self, app, mundo):
        from flota.adaptadores import gastos
        from flota.adaptadores.hallazgos import anclar_odometro

        ajena = anclar_odometro(mundo['virgen_id'], 5000, mundo['usuario_id'],
                                datetime(2026, 3, 1, 8, 0))
        with pytest.raises(gastos.GastoInvalido) as e:
            gastos.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 4, 1), valor='100', proveedor='T',
                origen_costo='sin_dato',
                registrado_por_usuario_id=mundo['usuario_id'],
                lectura_id=ajena.id)
        assert 'otro vehículo' in str(e.value)

    def test_km_y_lectura_id_a_la_vez_levantan(self, app, mundo):
        from flota.adaptadores import gastos

        o = _abrir(mundo)
        with pytest.raises(gastos.GastoInvalido) as e:
            gastos.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 4, 1), valor='100', proveedor='T',
                origen_costo='sin_dato',
                registrado_por_usuario_id=mundo['usuario_id'],
                km=101000, lectura_id=o.lectura_id)
        assert 'una de las dos se ignoraría' in str(e.value)

    def test_una_lectura_inexistente_levanta(self, app, mundo):
        from flota.adaptadores import gastos

        with pytest.raises(gastos.GastoInvalido):
            gastos.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 4, 1), valor='100', proveedor='T',
                origen_costo='sin_dato',
                registrado_por_usuario_id=mundo['usuario_id'],
                lectura_id=999999)

    def test_sin_lectura_id_el_gasto_de_campo_SIGUE_pidiendo_km(self, app, mundo):
        """La otra dirección: la rama nueva no puede haber aflojado las dos
        viejas."""
        from flota.adaptadores import gastos

        with pytest.raises(gastos.GastoInvalido) as e:
            gastos.registrar_gasto(
                vehiculo_id=mundo['vehiculo_id'], categoria='mantenimiento',
                fecha=date(2026, 4, 1), valor='100', proveedor='T',
                origen_costo='sin_dato',
                registrado_por_usuario_id=mundo['usuario_id'])
        assert 'kilometraje es obligatorio' in str(e.value)


# ══════════════════════════════════════════════════════════════════════════
# La frontera HTTP — que no afloje nada del adaptador
# ══════════════════════════════════════════════════════════════════════════

class TestPermisos:
    """No hay asimetría en esta fase, y es una decisión: el conductor no elige a
    qué taller va el camión ni negocia una garantía. Lo que sí puede es reportar
    un daño bloqueante, que ya tiene su puerta."""

    def test_el_conductor_no_abre_ordenes(self, app, client, mundo):
        r = client.post('/flota/ordenes', headers=_auth(mundo['t_cond']),
                        json={'placa': mundo['placa'], 'tipo': 'correctiva',
                              'taller': 'T', 'descripcion': 'x', 'km': 100000})
        assert r.status_code == 403

    def test_tienda_tampoco_las_ve(self, app, client, mundo):
        r = client.get(f'/flota/ordenes/{mundo["placa"]}',
                       headers=_auth(mundo['t_tienda']))
        assert r.status_code == 403

    def test_control_de_flota_si(self, app, client, mundo):
        r = client.get(f'/flota/ordenes/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 200


class TestLaFronteraRechazaLoCalculado:
    """**400, no un silencio.** Es la divergencia consciente con
    `/flota/hallazgos`, que ignora una `fecha_limite` enviada. Se eligió la
    puerta ruidosa por el motivo de `gastos._periodo`: ignorarlo deja a quien lo
    mandó creyendo que la garantía que escribió quedó registrada, y eso solo se
    descubre el día que se reclame."""

    def test_mandar_garantia_hasta_fecha_da_400(self, app, client, mundo):
        o = _abrir(mundo)
        r = client.post(f'/flota/ordenes/{o.id}/intervenciones',
                        headers=_auth(mundo['t_flota']),
                        json={'sistema': 'embrague', 'garantia_declarada': 'si',
                              'garantia_meses': 6,
                              'garantia_hasta_fecha': '2030-01-01'})
        assert r.status_code == 400
        assert 'se calcula' in r.get_json()['error']

    def test_mandar_garantia_hasta_km_tambien(self, app, client, mundo):
        o = _abrir(mundo)
        r = client.post(f'/flota/ordenes/{o.id}/intervenciones',
                        headers=_auth(mundo['t_flota']),
                        json={'sistema': 'embrague', 'garantia_declarada': 'si',
                              'garantia_km': 10000,
                              'garantia_hasta_km': 9999999})
        assert r.status_code == 400

    def test_el_mismo_cuerpo_SIN_los_calculados_pasa(self, app, client, mundo):
        """La otra dirección: un guard que rechazara todo pasaría los dos de
        arriba y dejaría la pantalla sin poder registrar nada."""
        o = _abrir(mundo)
        r = client.post(f'/flota/ordenes/{o.id}/intervenciones',
                        headers=_auth(mundo['t_flota']),
                        json={'sistema': 'embrague', 'garantia_declarada': 'si',
                              'garantia_meses': 6})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['hasta_fecha'] is not None


class TestLaRespuestaDelListado:

    def test_trae_las_garantias_vigentes_ya_juzgadas(self, app, client, mundo):
        o = _abrir(mundo, km=100000, ts=datetime(2026, 3, 5, 14, 0))
        _trabajo(o, mundo, sistema='embrague', meses=600)  # 50 años: sigue viva
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])

        r = client.get(f'/flota/ordenes/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota']))
        d = r.get_json()
        assert d['abiertas'] == 0
        assert len(d['garantias_vigentes']) == 1
        g = d['garantias_vigentes'][0]
        assert g['sistema'] == 'embrague'
        # Los insumos viajan con el veredicto: un `sin_dato` suelto no se puede
        # interpretar, y esto va a una pantalla donde alguien decide.
        for campo in ('por_fecha', 'por_km', 'vigente', 'hasta_fecha',
                      'hasta_km', 'km_actual'):
            assert campo in g

    def test_km_actual_es_null_y_no_cero_sin_lecturas(self, app, client, mundo):
        r = client.get(f'/flota/ordenes/{mundo["placa_virgen"]}',
                       headers=_auth(mundo['t_flota']))
        assert r.get_json()['km_actual'] is None

    def test_una_placa_que_no_existe_da_404(self, app, client, mundo):
        r = client.get('/flota/ordenes/NOEXISTE',
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 404

    def test_publica_los_vocabularios_para_que_la_pantalla_no_los_copie(
            self, app, client, mundo):
        """Si el JS llevara su propia lista de sistemas, el día que se agregue
        uno el formulario no lo ofrecería — y la búsqueda de garantía sobre ese
        sistema no encontraría nada. Regla 0 con consecuencia."""
        d = client.get(f'/flota/ordenes/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        from flota.dominio import taller as domt

        assert d['sistemas'] == list(domt.SISTEMAS)
        assert d['tipos'] == list(domt.TIPOS_OT)
        assert d['categorias_de_taller'] == list(adaptador.CATEGORIAS_DE_TALLER)


class TestLosVerbosPorHTTP:

    def test_abrir_cerrar_y_facturar_de_punta_a_punta(self, app, client, mundo):
        t = _auth(mundo['t_gestion'])
        r = client.post('/flota/ordenes', headers=t,
                        json={'placa': mundo['placa'], 'tipo': 'correctiva',
                              'taller': 'Taller Los Andes',
                              'descripcion': 'ruido al embragar', 'km': 100000})
        assert r.status_code == 201, r.get_json()
        oid = r.get_json()['id']

        r = client.post(f'/flota/ordenes/{oid}/intervenciones', headers=t,
                        json={'sistema': 'embrague', 'garantia_declarada': 'si',
                              'garantia_meses': 6, 'garantia_km': 10000})
        assert r.status_code == 201
        iid = r.get_json()['intervencion_id']
        assert r.get_json()['hasta_km'] == 110000

        r = client.post(f'/flota/ordenes/{oid}/cerrar', headers=t, json={})
        assert r.status_code == 200
        assert r.get_json()['estado'] == 'cerrada'
        assert r.get_json()['sin_factura'] == 1

        r = client.post(f'/flota/ordenes/{oid}/factura', headers=t,
                        json={'intervenciones': [iid],
                              'categoria': 'mantenimiento',
                              'fecha': '2026-04-30', 'valor': '1200000',
                              'proveedor': 'Taller Los Andes',
                              'origen_costo': 'credito_proveedor'})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['km'] == 100000
        # Se le dice a quien registra, no solo al tablero: es quien puede
        # conseguir el número de factura.
        assert r.get_json()['sin_documento'] is True

    def test_anular_por_http_exige_motivo(self, app, client, mundo):
        o = _abrir(mundo)
        r = client.post(f'/flota/ordenes/{o.id}/anular',
                        headers=_auth(mundo['t_gestion']), json={})
        assert r.status_code == 409
        assert 'motivo escrito' in r.get_json()['error']

    def test_cerrar_una_orden_sin_trabajos_da_409_por_http(self, app, client,
                                                           mundo):
        o = _abrir(mundo)
        r = client.post(f'/flota/ordenes/{o.id}/cerrar',
                        headers=_auth(mundo['t_gestion']), json={})
        assert r.status_code == 409

    def test_una_orden_inexistente_da_409_y_no_500(self, app, client, mundo):
        r = client.post('/flota/ordenes/999999/cerrar',
                        headers=_auth(mundo['t_gestion']), json={})
        assert r.status_code == 409

    def test_el_usuario_sale_del_TOKEN_y_no_del_cuerpo(self, app, client, mundo):
        r = client.post('/flota/ordenes', headers=_auth(mundo['t_gestion']),
                        json={'placa': mundo['placa'], 'tipo': 'correctiva',
                              'taller': 'T', 'descripcion': 'x', 'km': 100000,
                              'abierta_por_usuario_id': 999999})
        assert r.get_json()['abierta_por_usuario_id'] == mundo['usuario_gestion_id']


# ══════════════════════════════════════════════════════════════════════════
# El health — las tres medidas nacen con la tabla
# ══════════════════════════════════════════════════════════════════════════

    def test_control_de_flota_NO_manda_el_camion_al_taller(self, app, client,
                                                           mundo):
        """La dirección nueva (2026-09-09). Abrir, cerrar y anular una visita
        comprometen plata: son decisión, no registro.

        Su ficha lo decía desde el 2026-08-04 —«no aprobás órdenes de trabajo ni
        gastos»— y afirmaba que el código lo imponía. No lo imponía.
        """
        o = _abrir(mundo)
        t = _auth(mundo['t_flota'])
        assert client.post('/flota/ordenes', headers=t, json={
            'placa': mundo['placa'], 'tipo': 'correctiva', 'taller': 'T',
            'descripcion': 'x', 'km': 100000}).status_code == 403
        assert client.post(f'/flota/ordenes/{o.id}/cerrar', headers=t,
                           json={}).status_code == 403
        assert client.post(f'/flota/ordenes/{o.id}/anular', headers=t,
                           json={'motivo': 'no fue'}).status_code == 403

    def test_pero_SIGUE_registrando_el_trabajo_y_la_factura(self, app, client,
                                                            mundo):
        """La contraparte. Un recorte de permisos sin esta mitad deja al rol sin
        poder hacer su trabajo y nadie se entera hasta que lo intenta — es el
        mismo motivo por el que existe `test_gestion_SI_cierra`."""
        o = _abrir(mundo)
        t = _auth(mundo['t_flota'])
        r = client.post(f'/flota/ordenes/{o.id}/intervenciones', headers=t,
                        json={'sistema': 'embrague',
                              'garantia_declarada': 'no'})
        assert r.status_code == 201, r.get_json()
        iid = r.get_json()['intervencion_id']

        client.post(f'/flota/ordenes/{o.id}/cerrar',
                    headers=_auth(mundo['t_gestion']), json={})
        r = client.post(f'/flota/ordenes/{o.id}/factura', headers=t,
                        json={'intervenciones': [iid],
                              'categoria': 'mantenimiento',
                              'fecha': '2026-04-30', 'valor': '1200000',
                              'proveedor': 'Taller Los Andes',
                              'origen_costo': 'credito_proveedor'})
        assert r.status_code == 201, r.get_json()


class TestLaMedidaNaceConLaTabla:
    """`flota_lectura_odometro` vivió un mes con cero campos en el health y por
    eso nada avisó del salto de 16,3 millones de km. Acá los tres campos nacen
    con las tablas, **separados** y sin sumarse."""

    def _medidor(self):
        from flota.adaptadores.medicion import MedidorSQL

        return MedidorSQL()

    def test_los_tres_campos_estan_en_la_lista_del_health(self):
        from flota.api.health import _CAMPOS

        for campo in ('ot_abiertas', 'trabajos_sin_factura',
                      'garantias_vigentes'):
            assert campo in _CAMPOS

    def test_una_flota_sin_taller_reporta_ceros_medidos(self, app, mundo):
        m = self._medidor()
        assert m.ot_abiertas() == 0
        assert m.trabajos_sin_factura() == 0
        assert m.garantias_vigentes() == 0

    def test_ot_abiertas_cuenta_las_abiertas_y_solo_esas(self, app, mundo):
        o = _abrir(mundo)
        assert self._medidor().ot_abiertas() == 1
        _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        assert self._medidor().ot_abiertas() == 0

    def test_trabajos_sin_factura_NO_cuenta_los_de_ordenes_abiertas(
            self, app, mundo):
        """Un camión que sigue adentro no es una factura perdida. Contarlo
        dejaría el campo permanentemente en rojo — que es cómo un tablero se
        deja de mirar."""
        o = _abrir(mundo)
        _trabajo(o, mundo)
        assert self._medidor().trabajos_sin_factura() == 0

    def test_y_SI_cuenta_los_de_una_orden_cerrada(self, app, mundo):
        o = _abrir(mundo)
        a = _trabajo(o, mundo)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        assert self._medidor().trabajos_sin_factura() == 1

        adaptador.registrar_factura(
            orden_trabajo_id=o.id, intervencion_ids=[a.id],
            categoria='mantenimiento', fecha=date(2026, 4, 30),
            valor='1200000', proveedor='T', origen_costo='sin_dato',
            registrado_por_usuario_id=mundo['usuario_id'])
        assert self._medidor().trabajos_sin_factura() == 0

    def test_garantias_vigentes_cuenta_las_que_cubren(self, app, mundo):
        o = _abrir(mundo, km=100000)
        _trabajo(o, mundo, sistema='embrague', meses=600)
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'])
        assert self._medidor().garantias_vigentes() == 1

    def test_una_garantia_vencida_NO_cuenta(self, app, mundo):
        """La otra dirección: un contador que sumara todas las intervenciones
        con `garantia_declarada='si'` pasaría el test de arriba."""
        o = _abrir(mundo, km=100000, ts=datetime(2020, 3, 5, 14, 0))
        _trabajo(o, mundo, sistema='embrague', meses=1,
                 ts=datetime(2020, 3, 5, 15, 0))
        adaptador.cerrar(orden_trabajo_id=o.id, usuario_id=mundo['usuario_id'],
                         ts=datetime(2020, 3, 10, 10, 0))
        assert self._medidor().garantias_vigentes() == 0

    def test_el_health_los_devuelve(self, app, client, mundo, db):
        from app.models.usuario import Usuario
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash

        admin = Usuario(nombre='Admin TLR', email='tlr_admin@test.com',
                        password_hash=generate_password_hash('x'),
                        rol='admin', activo=True)
        db.session.add(admin)
        db.session.commit()
        token = create_access_token(identity=str(admin.id))

        _abrir(mundo)
        d = client.get('/flota/health', headers=_auth(token)).get_json()
        assert d['ot_abiertas'] == 1
        assert d['trabajos_sin_factura'] == 0
        assert d['garantias_vigentes'] == 0


# ══════════════════════════════════════════════════════════════════════════
# El vocabulario de `origen` de lectura, actualizado
# ══════════════════════════════════════════════════════════════════════════

class TestElOrigenOTYaTieneAlgoDetras:

    def test_ot_sigue_fuera_de_las_lecturas_sueltas(self):
        """Y por el motivo NUEVO: antes no había padre, ahora sí lo hay y la
        lectura nace con él. Una suelta quedaría indistinguible de las que sí
        tienen una orden detrás, y esa serie es la que dice cuánto taller
        hubo."""
        from flota.dominio.valores import (MOTIVO_ORIGEN_NO_SUELTO,
                                           ORIGENES_LECTURA_SUELTA,
                                           OrigenLectura)

        assert OrigenLectura.OT not in ORIGENES_LECTURA_SUELTA
        motivo = MOTIVO_ORIGEN_NO_SUELTO[OrigenLectura.OT]
        assert 'orden de trabajo' in motivo
        assert 'misma transacción' in motivo

    def test_el_motivo_ya_no_dice_que_las_ordenes_no_existen(self):
        """Un motivo que dejó de ser cierto es peor que ninguno: se lee con
        confianza y se lee mal. Este test es lo que lo mantiene honesto."""
        from flota.dominio.valores import (MOTIVO_ORIGEN_NO_SUELTO,
                                           OrigenLectura)

        motivo = MOTIVO_ORIGEN_NO_SUELTO[OrigenLectura.OT]
        assert 'todavía no existen' not in motivo
        assert 'tanda 3' not in motivo

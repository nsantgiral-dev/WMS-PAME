"""El hallazgo, desde que nace hasta que se cierra — contra la base, no contra mocks.

## Por qué existe

`flota/dominio/hallazgo.py` tenía 153 líneas de política, 23 tests, canon
cerrado por Santiago… y **cero callers de producción**. Los 53 ítems de
inspección sembrados en producción esperaban una tabla que no existía. Un daño
no podía nacer.

Estos tests ejercen la vía completa: adaptador → base → CHECK. Un test que
construya `Hallazgo(...)` en memoria y verifique sus atributos pasaría con la
tabla sin ningún constraint, que es exactamente el modo de falla que el módulo
persigue.

## Las dos direcciones

Cada invariante se prueba de los dos lados: que **dispara** cuando se lo viola
y que **NO dispara** sobre la operación sana. Un detector que solo se prueba en
un sentido está probado a la mitad, y el sentido que falta es el que rompe
producción — es la lección de los seis guards en verde sobre propiedades que la
vía sana satisfacía por construcción.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.almacen import Almacen
from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo
from flota.adaptadores import hallazgos as adaptador
from flota.adaptadores.modelos import Hallazgo, LecturaOdometro
from flota.adaptadores.traspaso import traspasar
from flota.dominio.errores import ErrorFlota
from flota.dominio.hallazgo import (EstadoHallazgo, dias_hallazgo_abierto,
                                    dias_transcurridos, entra_al_indicador,
                                    vencido)
from flota.dominio.valores import CustodioTipo, OrigenLectura


@pytest.fixture
def escenario(db):
    """Un vehículo, un usuario y nada más. Sin custodia: es el arranque real.

    Depende de `db` y no de `app`: `app` es de sesión y no limpia nada entre
    tests, así que la segunda placa idéntica choca contra el UNIQUE. Es el
    fixture `db` el que trunca al terminar.
    """
    v = Vehiculo(placa='HZG001', tipo='camion', activo=True)
    u = Usuario(email='hz_reporta@test.com', nombre='Quien Vio',
                rol='control_flota', activo=True)
    u.set_password('x')
    sede = Almacen(codigo='HZ-SEDE', nombre='Patio de prueba', activo=True)
    db.session.add_all([v, u, sede])
    db.session.commit()
    return {'vehiculo_id': v.id, 'usuario_id': u.id, 'placa': v.placa,
            'sede_id': sede.id, 'db': db}


def _custodiar(escenario, km, linea_base=False):
    """Deja el vehículo bajo custodia de la sede.

    Con `linea_base=False` hace DOS traspasos: el primero de cualquier vehículo
    es línea base por construcción (`traspaso.py`), así que un solo traspaso no
    produce el turno normal que la mayoría de los tests necesita.

    El segundo va con **otro kilometraje** a propósito. Con el mismo,
    `_traspaso_reciente_identico` lo reconoce como reintento —90 segundos de
    ventana— y devuelve la custodia de línea base sin abrir ninguna: el
    escenario quedaba siendo el contrario del que el test dice montar, y sin
    esta línea el fallo se leía como un defecto de `linea_base`.
    """
    c = traspasar(vehiculo_id=escenario['vehiculo_id'], km=km,
                  registrado_por_usuario_id=escenario['usuario_id'],
                  custodio_tipo=CustodioTipo.SEDE,
                  custodio_sede_id=escenario['sede_id'])
    if linea_base:
        return c
    return traspasar(vehiculo_id=escenario['vehiculo_id'], km=km + 1,
                     registrado_por_usuario_id=escenario['usuario_id'],
                     custodio_tipo=CustodioTipo.SEDE,
                     custodio_sede_id=escenario['sede_id'])


def _reportar(escenario, **extra):
    campos = dict(vehiculo_id=escenario['vehiculo_id'], criticidad='mayor',
                  descripcion='algo', km=500,
                  reportado_por_usuario_id=escenario['usuario_id'])
    campos.update(extra)
    return adaptador.reportar(**campos)


class TestNaceConSuReloj:
    """Regla 6: severidad y fecha límite, calculada al nacer."""

    def test_un_dano_queda_escrito_con_todo_lo_que_necesita(self, escenario):
        h = _reportar(escenario, descripcion='Fuga de aceite en el diferencial',
                      km=1000)
        assert h.estado == EstadoHallazgo.ABIERTO
        assert h.cerrado_ts is None
        assert h.fecha_limite is not None
        assert h.lectura_id is not None, 'regla 3: sin odómetro no se persiste'
        assert h.aplazado_veces == 0

    @pytest.mark.parametrize('criticidad,dias', [
        ('bloqueante', 0), ('mayor', 7), ('menor', 30)])
    def test_el_plazo_sale_de_la_severidad_y_no_de_quien_reporta(
            self, escenario, criticidad, dias):
        """El plazo se CALCULA. Si se pudiera escribir a dedo, «bloqueante»
        pasaría a ser una etiqueta que se negocia."""
        ahora = datetime(2026, 9, 1, 14, 0)      # 09:00 en Bogotá
        h = _reportar(escenario, criticidad=criticidad, km=1000 + dias, ts=ahora)
        # Medianoche de Bogotá del día siguiente al último día a tiempo.
        assert h.fecha_limite == datetime(2026, 9, 2, 5, 0) + timedelta(days=dias)

    def test_un_bloqueante_no_nace_vencido(self, escenario):
        """**El caso que decide si el rojo significa algo.**

        Con `fecha_limite = reportado_ts + 0 días`, todo bloqueante sale vencido
        un segundo después de nacer y el rojo deja de distinguir. «Mismo día»
        quiere decir *hasta que termine el día*.
        """
        ahora = datetime(2026, 9, 1, 14, 0)      # 09:00 Bogotá
        h = _reportar(escenario, criticidad='bloqueante',
                      descripcion='Freno de servicio sin presión', km=1000,
                      ts=ahora)
        dom = h.a_dominio()
        assert not vencido(dom, ahora + timedelta(minutes=1))
        # A las 11 de la noche de Bogotá del MISMO día: todavía a tiempo.
        assert not vencido(dom, datetime(2026, 9, 2, 4, 0))
        # Pasada la medianoche de Bogotá: vencido.
        assert vencido(dom, datetime(2026, 9, 2, 5, 30))

    def test_un_reporte_de_la_noche_no_nace_vencido_por_el_uso_horario(
            self, escenario):
        """Regla 5 del WMS aplicada al plazo.

        A las 8 p.m. de Colombia, en UTC ya es el día siguiente. Con
        `utcnow().date()` un bloqueante de la noche nacería con la fecha límite
        del día que ya pasó — vencido antes de que el mecánico llegue.
        """
        # 2026-09-01 20:30 Bogotá = 2026-09-02 01:30 UTC
        ahora = datetime(2026, 9, 2, 1, 30)
        h = _reportar(escenario, criticidad='bloqueante',
                      descripcion='Luz de freno quemada', km=1000, ts=ahora)
        assert not vencido(h.a_dominio(), ahora), (
            'nació vencido: el día se calculó en UTC, no en Bogotá')
        # El plazo llega al final del 1 de septiembre EN BOGOTÁ.
        assert h.fecha_limite == datetime(2026, 9, 2, 5, 0)

    def test_una_criticidad_inventada_no_degrada_a_menor(self, escenario):
        """Regla 5: o funciona, o falla. Un `.get(x, 'menor')` convertiría un
        bloqueante mal escrito en un hallazgo con treinta días de plazo."""
        with pytest.raises(ErrorFlota) as e:
            _reportar(escenario, criticidad='urgente')
        assert 'urgente' in str(e.value)
        assert Hallazgo.query.count() == 0, 'no puede quedar fila a medias'

    def test_sin_descripcion_no_se_escribe_nada(self, escenario):
        with pytest.raises(ErrorFlota):
            _reportar(escenario, descripcion='   ')
        assert Hallazgo.query.count() == 0
        assert LecturaOdometro.query.count() == 0, (
            'la lectura se escribió antes de validar: quedó un odómetro '
            'huérfano de un hallazgo que nunca existió')


class TestElOdometroEntraPorLaMismaPuerta:
    """Regla 3, y el ruido que produce cumplirla mal."""

    def test_la_primera_lectura_del_vehiculo_nace_con_el_hallazgo(self, escenario):
        h = _reportar(escenario, criticidad='menor',
                      descripcion='Rayón en el cajón', km=54321)
        l = LecturaOdometro.query.get(h.lectura_id)
        assert l.valor_km == 54321
        assert l.origen == OrigenLectura.HALLAZGO.value, (
            'el origen miente sobre de dónde vino la lectura')

    def test_tres_hallazgos_del_mismo_km_cuelgan_de_UNA_lectura(self, escenario):
        """**Lo que evita fabricar el ruido que `lecturas_ts_duplicado` cuenta.**

        En producción hay diez lecturas con el mismo segundo, de un reintento.
        Escribir una lectura idéntica por cada hallazgo reportado produciría ese
        mismo patrón desde adentro del sistema — y entonces el contador dejaría
        de distinguir un reintento de la operación normal.
        """
        ids = {_reportar(escenario, criticidad='menor', descripcion=t,
                         km=7000).lectura_id
               for t in ('golpe puerta', 'espejo flojo', 'luz trasera')}
        assert len(ids) == 1
        assert LecturaOdometro.query.count() == 1

    def test_un_km_distinto_SI_escribe_lectura_nueva(self, escenario):
        """La otra dirección. Reutilizar siempre descartaría el número que el
        reportante tecleó y guardaría un kilometraje que nadie midió."""
        a = _reportar(escenario, criticidad='menor', descripcion='uno', km=7000)
        b = _reportar(escenario, criticidad='menor', descripcion='dos', km=7250)
        assert a.lectura_id != b.lectura_id
        assert LecturaOdometro.query.get(b.lectura_id).valor_km == 7250

    def test_un_odometro_que_retrocede_no_entra_por_esta_puerta(self, escenario):
        """El hallazgo no puede ser el agujero por el que se salta la monotonía.
        Es la puerta nueva, y las puertas nuevas son por donde se cuela lo que
        las viejas frenan."""
        _reportar(escenario, criticidad='menor', descripcion='uno', km=9000)
        with pytest.raises(ErrorFlota):
            _reportar(escenario, criticidad='menor', descripcion='dos', km=100)
        assert Hallazgo.query.count() == 1, 'quedó un hallazgo huérfano'


class TestLineaBaseYCustodia:
    """Regla 2: el sistema anota hechos, no imputa responsables."""

    def test_sin_custodia_el_dano_nace_preexistente(self, escenario):
        """Sin cadena de custodia nadie recibió el vehículo intacto: el daño no
        se le puede empezar a contar a nadie (regla 0, lado conservador)."""
        h = _reportar(escenario, criticidad='menor',
                      descripcion='abolladura vieja', km=100)
        assert h.linea_base is True
        assert h.custodia_id is None

    def test_con_custodia_normal_el_dano_SI_cuenta(self, escenario):
        """La otra dirección: si `linea_base` fuera siempre True, ningún
        hallazgo entraría nunca al indicador y el indicador diría cero para
        siempre — un cero que se lee como «no hubo daños»."""
        c2 = _custodiar(escenario, km=10)
        h = _reportar(escenario, descripcion='golpe nuevo', km=25)
        assert h.custodia_id == c2.id
        assert h.linea_base is False
        assert entra_al_indicador(h.a_dominio()) is True

    def test_bajo_la_custodia_de_linea_base_el_dano_no_le_cuenta_a_nadie(
            self, escenario):
        """La primera custodia levanta lo que ya había. El desorden viejo no
        entra al indicador de quien lo encontró."""
        c = _custodiar(escenario, km=10, linea_base=True)
        assert c.linea_base is True, 'el escenario no es el que se quería probar'
        h = _reportar(escenario, descripcion='lo que ya estaba', km=10)
        assert h.linea_base is True
        assert entra_al_indicador(h.a_dominio()) is False


class TestLasTresSalidas:
    def test_cerrar_para_el_reloj(self, escenario):
        # Con custodia normal: sin ella el hallazgo nace de línea base y
        # `dias_hallazgo_abierto` levanta a propósito. Lo que se mide acá es el
        # reloj del indicador, así que el hallazgo tiene que entrar en él.
        _custodiar(escenario, km=1)
        h = _reportar(escenario)
        nacio = h.reportado_ts
        adaptador.cerrar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'],
                         ts=nacio + timedelta(days=3))
        escenario['db'].session.refresh(h)
        assert h.estado == EstadoHallazgo.CERRADO
        assert dias_hallazgo_abierto(h.a_dominio()) == 3

    def test_cerrar_dos_veces_levanta(self, escenario):
        """Un segundo cierre movería un número que ya se publicó. El silencio
        haría que un cierre equivocado se tapara con otro."""
        h = _reportar(escenario)
        adaptador.cerrar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'])
        with pytest.raises(ErrorFlota) as e:
            adaptador.cerrar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'])
        assert 'cerrado' in str(e.value)

    def test_descartar_sin_motivo_no_pasa(self, escenario):
        """Es la única salida que no exige reparar nada: la que usaría quien no
        quiere hacer el trabajo (regla 11). Lo que la hace cara es que queda
        escrito quién dijo que no era nada."""
        h = _reportar(escenario)
        with pytest.raises(ErrorFlota):
            adaptador.descartar(hallazgo_id=h.id,
                                usuario_id=escenario['usuario_id'], motivo='  ')
        escenario['db'].session.refresh(h)
        assert h.estado == EstadoHallazgo.ABIERTO

    def test_descartado_no_mejora_ningun_promedio(self, escenario):
        """La otra mitad de por qué descartar no es el camino barato: no entra
        al indicador ni siquiera como cerrado rápido."""
        h = _reportar(escenario)
        adaptador.descartar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'],
                            motivo='era barro, no una fuga')
        escenario['db'].session.refresh(h)
        assert h.estado == EstadoHallazgo.DESCARTADO
        assert h.motivo_cierre == 'era barro, no una fuga'
        assert entra_al_indicador(h.a_dominio()) is False

    def test_aplazar_mueve_el_plazo_y_NO_borra_el_tiempo_abierto(self, escenario):
        """El aplazamiento crónico tiene que ser visible: uno aplazado cuatro
        veces no está gestionado, está evitado."""
        h = _reportar(escenario)
        limite_original, nacio = h.fecha_limite, h.reportado_ts
        adaptador.aplazar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'],
                          motivo='no llegó el repuesto')
        escenario['db'].session.refresh(h)
        assert h.fecha_limite == limite_original + timedelta(
            days=adaptador.DIAS_POR_APLAZAMIENTO)
        assert h.aplazado_veces == 1
        assert h.reportado_ts == nacio, 'aplazar movió el origen del reloj'
        assert dias_transcurridos(h.a_dominio(), nacio + timedelta(days=20)) == 20

    def test_la_bitacora_acumula_y_no_pisa(self, escenario):
        """El tercer aplazamiento no borra por qué se hizo el primero."""
        h = _reportar(escenario)
        motivos = ('sin repuesto', 'taller lleno', 'esperando cotización')
        for m in motivos:
            adaptador.aplazar(hallazgo_id=h.id,
                              usuario_id=escenario['usuario_id'], motivo=m)
        escenario['db'].session.refresh(h)
        assert h.aplazado_veces == 3
        for m in motivos:
            assert m in h.bitacora

    def test_el_aplazamiento_NO_se_guarda_en_motivo_cierre(self, escenario):
        """Un nombre que promete una cosa y contiene otra se lee con confianza
        y se lee mal. `motivo_cierre` es del desenlace; la bitácora es de lo
        que pasó mientras estuvo abierto."""
        h = _reportar(escenario)
        adaptador.aplazar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'],
                          motivo='sin repuesto')
        escenario['db'].session.refresh(h)
        assert h.motivo_cierre is None
        assert 'sin repuesto' in h.bitacora

    def test_aplazar_algo_ya_cerrado_levanta(self, escenario):
        h = _reportar(escenario)
        adaptador.cerrar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'])
        with pytest.raises(ErrorFlota):
            adaptador.aplazar(hallazgo_id=h.id,
                              usuario_id=escenario['usuario_id'], motivo='x')


class TestLaBaseImponeLoQueElAdaptadorPromete:
    """Los CHECK, ejercidos a mano. Un invariante que solo vive en el adaptador
    es una sugerencia: la segunda vía de escritura no lo hereda."""

    def _lectura(self, escenario):
        db = escenario['db']
        l = LecturaOdometro(vehiculo_id=escenario['vehiculo_id'], valor_km=1,
                            ts=datetime(2026, 9, 1), origen='entrega',
                            autor_usuario_id=escenario['usuario_id'])
        db.session.add(l)
        db.session.flush()
        return l.id

    def _base(self, escenario, **extra):
        campos = dict(
            vehiculo_id=escenario['vehiculo_id'], criticidad='menor',
            descripcion='x', reportado_ts=datetime(2026, 9, 1),
            reportado_por_usuario_id=escenario['usuario_id'],
            fecha_limite=datetime(2026, 10, 1), estado=EstadoHallazgo.ABIERTO,
            lectura_id=self._lectura(escenario),
        )
        campos.update(extra)
        return campos

    def test_una_fila_sana_entra(self, escenario):
        """La otra dirección de los tests de abajo. Sin esto, un CHECK escrito
        de más —que rechace todo— los pasaría a todos."""
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(escenario)))
        db.session.commit()
        assert Hallazgo.query.count() == 1

    def test_un_abierto_con_fecha_de_cierre_es_rechazado(self, escenario):
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(
            escenario, estado=EstadoHallazgo.ABIERTO,
            cerrado_ts=datetime(2026, 9, 2))))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_un_cerrado_sin_fecha_de_cierre_es_rechazado(self, escenario):
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(
            escenario, estado=EstadoHallazgo.CERRADO, cerrado_ts=None)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_un_descartado_sin_motivo_es_rechazado_POR_LA_BASE(self, escenario):
        """No solo por el adaptador. Un `UPDATE` a mano en producción es la
        segunda vía, y es justo la que usaría alguien haciendo desaparecer un
        hallazgo incómodo."""
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(
            escenario, estado=EstadoHallazgo.DESCARTADO,
            cerrado_ts=datetime(2026, 9, 2), motivo_cierre='   ')))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_una_criticidad_fuera_del_vocabulario_es_rechazada(self, escenario):
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(escenario, criticidad='urgentisimo')))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_una_descripcion_en_blanco_es_rechazada(self, escenario):
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(escenario, descripcion='   ')))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_no_se_puede_escribir_un_hallazgo_sin_lectura(self, escenario):
        """Regla 3, impuesta por la base y no por acordarse. `lectura_id` es
        NOT NULL: un daño sin kilometraje no se cruza con nada después."""
        db = escenario['db']
        db.session.add(Hallazgo(**self._base(escenario, lectura_id=None)))
        with pytest.raises(IntegrityError):
            db.session.commit()


class TestElOrdenDeLaLista:
    def test_el_bloqueante_primero_y_el_mas_viejo_antes(self, escenario):
        """No es cosmético: `bloqueante` es hoy, y dentro de la misma severidad
        el que lleva más tiempo es el que peor está."""
        base = datetime(2026, 9, 1, 12, 0)
        _reportar(escenario, criticidad='menor', descripcion='menor viejo',
                  km=10, ts=base)
        _reportar(escenario, criticidad='mayor', descripcion='mayor nuevo',
                  km=20, ts=base + timedelta(days=2))
        _reportar(escenario, criticidad='bloqueante', descripcion='freno',
                  km=30, ts=base + timedelta(days=3))
        _reportar(escenario, criticidad='mayor', descripcion='mayor viejo',
                  km=40, ts=base + timedelta(days=1))

        orden = [h.descripcion
                 for h in adaptador.abiertos_de(escenario['vehiculo_id'])]
        assert orden == ['freno', 'mayor viejo', 'mayor nuevo', 'menor viejo']

    def test_lo_cerrado_no_sale_en_los_abiertos(self, escenario):
        h = _reportar(escenario, criticidad='menor', descripcion='ya arreglado',
                      km=10)
        adaptador.cerrar(hallazgo_id=h.id, usuario_id=escenario['usuario_id'])
        assert adaptador.abiertos_de(escenario['vehiculo_id']) == []

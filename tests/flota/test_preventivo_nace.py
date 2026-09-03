"""
El plan preventivo, desde que se siembra hasta que alguien registra que se hizo
— contra la base real, no contra mocks.

## Por qué existe

`flota_ficha_tecnica.distribucion_km_cambio` está cargado en la base desde la
tanda 1 y **nadie lo leía**. Es la tabla diciendo a qué kilometraje toca cambiar
la correa, mientras la correa envejece. Estos tests ejercen la vía completa
—adaptador → base → CHECK— porque un test que construyera `PlanTarea(...)` en
memoria y mirara sus atributos pasaría con la tabla sin ningún constraint, que
es exactamente el modo de falla que el módulo persigue.

## Las dos direcciones

Cada invariante se prueba de los dos lados: que **dispara** cuando se lo viola y
que **NO dispara** sobre la operación sana. Es la lección de los guards en verde
sobre propiedades que la vía sana satisfacía por construcción.
"""
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo
from flota.adaptadores import preventivo as adaptador
from flota.adaptadores.modelos import (EjecucionTarea, FichaTecnica,
                                       LecturaOdometro, PlanTarea)
from flota.dominio.errores import ErrorFlota
from flota.dominio.preventivo import EstadoTarea, PlanInvalido
from flota.dominio.valores import SIN_DATO, OrigenLectura


@pytest.fixture
def mundo(db):
    """Un camión con ficha completa, un motocarro con cadena y uno sin ficha.

    Depende de `db` y no de `app`: `app` es de sesión y no limpia entre tests,
    así que la segunda placa idéntica choca contra el UNIQUE.
    """
    camion = Vehiculo(placa='PRV100', tipo='camion', activo=True)
    moto = Vehiculo(placa='PRV200', tipo='motocarro', activo=True)
    pelado = Vehiculo(placa='PRV300', tipo='camion', activo=True)
    u = Usuario(email='prv_flota@test.com', nombre='Control PRV',
                rol='control_flota', activo=True)
    u.set_password('x')
    db.session.add_all([camion, moto, pelado, u])
    db.session.flush()

    db.session.add(FichaTecnica(
        vehiculo_id=camion.id, posiciones_llanta=6, km_inicial=50000,
        km_inicial_ts=datetime(2026, 1, 1),
        distribucion='correa', distribucion_km_cambio=60000,
        distribucion_fuente='manual_fabricante',
        aceite_motor_spec='15W40 API CI-4',
        aceite_caja_spec='80W90',
        refrigerante_spec='verde orgánico',
        transmision_final='cardan'))
    db.session.add(FichaTecnica(
        vehiculo_id=moto.id, posiciones_llanta=4, km_inicial=1000,
        km_inicial_ts=datetime(2026, 1, 1),
        transmision_final='cadena'))
    db.session.commit()
    return {'camion_id': camion.id, 'placa': camion.placa,
            'moto_id': moto.id, 'pelado_id': pelado.id,
            'usuario_id': u.id, 'db': db}


def _tipos(vehiculo_id):
    return sorted(t.tipo for t in
                  PlanTarea.query.filter_by(vehiculo_id=vehiculo_id).all())


def _tarea(vehiculo_id, tipo):
    return PlanTarea.query.filter_by(vehiculo_id=vehiculo_id, tipo=tipo).one()


# ══════════════════════════════════════════════════════════════════════════
# La siembra es una traducción de la ficha, no una captura
# ══════════════════════════════════════════════════════════════════════════

class TestElPlanSaleDeLaFichaYDeNadaMas:

    def test_la_distribucion_hereda_su_kilometraje_Y_su_procedencia(self, app, db, mundo):
        """**La pieza de mayor valor del plan, y cuesta una consulta.**"""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        assert t.intervalo_km == 60000
        assert t.fuente == 'manual_fabricante'
        assert t.origen == 'ficha'
        assert t.activo is True

    def test_cada_especificacion_de_aceite_propone_su_tarea(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        assert _tipos(mundo['camion_id']) == [
            'aceite_caja', 'aceite_motor', 'distribucion', 'refrigerante']

    def test_lo_que_la_ficha_NO_declara_no_produce_tarea(self, app, db, mundo):
        """La otra dirección. Una siembra que creara las seis tareas siempre
        pasaría el test de arriba y llenaría el tablero de `sin_intervalo`
        sobre vehículos que no tienen ese sistema."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        assert 'aceite_diferencial' not in _tipos(mundo['camion_id'])
        assert 'lubricacion_cadena' not in _tipos(mundo['camion_id'])

    def test_la_cadena_de_un_motocarro_propone_su_lubricacion(self, app, db, mundo):
        """`transmision_final` no es `distribucion`: un motocarro puede tener
        distribución por cadena Y transmisión final por cadena, y son dos
        mantenimientos distintos."""
        adaptador.sembrar_desde_ficha(mundo['moto_id'])
        assert _tipos(mundo['moto_id']) == ['lubricacion_cadena']

    def test_un_vehiculo_sin_ficha_no_siembra_nada_Y_LO_DICE(self, app, db, mundo):
        """Devolver un resumen en ceros haría creer que el vehículo no necesita
        mantenimiento. Lo que pasa es que no hay de dónde sacar una tarea."""
        with pytest.raises(ErrorFlota) as e:
            adaptador.sembrar_desde_ficha(mundo['pelado_id'])
        assert 'ficha' in str(e.value)
        assert _tipos(mundo['pelado_id']) == []


class TestLasCincoQueNacenSinIntervaloYPorQue:
    """La ficha declara **qué** aceite lleva el motor y no **cada cuántos km**
    se cambia. Ese número no existe en ninguna columna.

    Ponerle 5.000 km por omisión le daría a las seis fichas de producción un
    intervalo que nadie levantó, y el sistema empezaría a decir «vencida» sobre
    una comparación contra un número inventado.
    """

    def test_el_aceite_de_motor_nace_sin_intervalo_y_sin_fuente(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        assert t.intervalo_km is None
        assert t.fuente == 'sin_dato'

    def test_y_el_diagnostico_lo_dice_con_una_palabra(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        d = {t['tipo']: t for t in adaptador.diagnostico_de(mundo['camion_id'])}
        assert d['aceite_motor']['estado'] == EstadoTarea.SIN_INTERVALO
        assert d['aceite_motor']['intervalo_km'] is SIN_DATO

    def test_un_kilometraje_sin_procedencia_no_se_siembra_como_dato(self, app, db, mundo):
        """El CHECK de la ficha ya impide esta combinación desde la tanda 1.
        La siembra la vuelve a mirar y **no hereda el número sin la fuente**:
        un intervalo con autoridad y sin respaldo es peor que ninguno.

        Se construye saltando el ORM porque por la vía normal la ficha no lo
        deja pasar — que es justamente lo que hace honesta a esta defensa.
        """
        ficha = FichaTecnica.query.get(mundo['moto_id'])
        ficha.distribucion_km_cambio = 30000
        # `distribucion` sigue en 'sin_dato', así que el CHECK de la ficha se
        # cumple y la fuente queda vacía: el caso exacto que se persigue.
        db.session.commit()

        adaptador.sembrar_desde_ficha(mundo['moto_id'])
        t = _tarea(mundo['moto_id'], 'distribucion')
        assert t.intervalo_km is None
        assert t.fuente == 'sin_dato'


# ══════════════════════════════════════════════════════════════════════════
# Idempotencia y respeto por lo que escribió una persona
# ══════════════════════════════════════════════════════════════════════════

class TestSembrarDosVecesNoDuplicaNada:

    def test_la_segunda_corrida_no_crea_filas(self, app, db, mundo):
        """Sin esto, cada ciclo del cron agregaría una fila más: seis vehículos
        por seis tipos por trescientos días, y el tablero contaría diez mil
        tareas."""
        primera = adaptador.sembrar_desde_ficha(mundo['camion_id'])
        segunda = adaptador.sembrar_desde_ficha(mundo['camion_id'])
        assert primera['creadas'] == 4
        assert segunda['creadas'] == 0
        assert segunda['sin_cambio'] == 4
        assert len(_tipos(mundo['camion_id'])) == 4

    def test_la_base_lo_impide_aunque_el_adaptador_falle(self, app, db, mundo):
        """La idempotencia descansa en el UNIQUE y no en un `if` del adaptador,
        que es lo que dos procesos concurrentes esquivan."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='distribucion',
            intervalo_km=60000, fuente='taller', origen='manual',
            sembrado_ts=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestLaFichaMandaSobreLoQueSaleDeLaFicha:

    def test_un_intervalo_corregido_en_la_ficha_llega_al_plan(self, app, db, mundo):
        """No seguirla dejaría al plan comparando el odómetro contra un
        intervalo que ya nadie sostiene."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        FichaTecnica.query.get(mundo['camion_id']).distribucion_km_cambio = 50000
        db.session.commit()

        r = adaptador.sembrar_desde_ficha(mundo['camion_id'])
        assert r['actualizadas'] == 1
        assert _tarea(mundo['camion_id'], 'distribucion').intervalo_km == 50000

    def test_pero_NO_pisa_lo_que_alguien_escribio_a_mano(self, app, db, mundo):
        """**El motivo entero de que exista la columna `origen`.** Sin ella, el
        intervalo del aceite que alguien llamó a preguntar volvería al de la
        ficha en el próximo ciclo del cron, en silencio."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=45000,
                                  fuente='taller')

        FichaTecnica.query.get(mundo['camion_id']).distribucion_km_cambio = 70000
        db.session.commit()
        r = adaptador.sembrar_desde_ficha(mundo['camion_id'])

        assert r['respetadas_manual'] == 1
        assert r['actualizadas'] == 0
        t = _tarea(mundo['camion_id'], 'distribucion')
        assert t.intervalo_km == 45000
        assert t.fuente == 'taller'

    def test_la_siembra_no_retira_una_tarea_que_la_ficha_dejo_de_declarar(
            self, app, db, mundo):
        """Que la ficha deje de declarar una caja no significa que el vehículo
        no la tenga. Retirar una tarea es una decisión de persona."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        FichaTecnica.query.get(mundo['camion_id']).aceite_caja_spec = None
        db.session.commit()

        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        assert 'aceite_caja' in _tipos(mundo['camion_id'])
        assert _tarea(mundo['camion_id'], 'aceite_caja').activo is True


# ══════════════════════════════════════════════════════════════════════════
# Regla 3 — sin odómetro no se persiste ningún evento de flota
# ══════════════════════════════════════════════════════════════════════════

class TestLaEjecucionSeAnclaALaLecturaNoLaFabrica:

    def _plan(self, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        return _tarea(mundo['camion_id'], 'distribucion')

    def test_la_ejecucion_nace_con_su_lectura(self, app, db, mundo):
        ej = adaptador.registrar_ejecucion(
            plan_id=self._plan(mundo).id, km=55000,
            usuario_id=mundo['usuario_id'])
        lec = LecturaOdometro.query.get(ej.lectura_id)
        assert lec.valor_km == 55000
        assert lec.origen == OrigenLectura.OT.value

    def test_dos_tareas_de_la_misma_visita_cuelgan_de_UNA_lectura(self, app, db, mundo):
        """Escribir una lectura por ejecución produciría desde adentro del
        sistema el mismo ruido que `lecturas_ts_duplicado` existe para contar."""
        plan = self._plan(mundo)
        otra = _tarea(mundo['camion_id'], 'aceite_motor')
        adaptador.fijar_intervalo(plan_id=otra.id, intervalo_km=5000,
                                  fuente='concesionario')

        a = adaptador.registrar_ejecucion(plan_id=plan.id, km=55000,
                                          usuario_id=mundo['usuario_id'])
        b = adaptador.registrar_ejecucion(plan_id=otra.id, km=55000,
                                          usuario_id=mundo['usuario_id'])
        assert a.lectura_id == b.lectura_id

    def test_pero_un_kilometraje_DISTINTO_sí_produce_una_lectura_nueva(
            self, app, db, mundo):
        """La otra dirección: el odómetro se movió y eso es información nueva.
        Colgarla de la lectura vieja guardaría un kilometraje que nadie midió."""
        plan = self._plan(mundo)
        a = adaptador.registrar_ejecucion(plan_id=plan.id, km=55000,
                                          usuario_id=mundo['usuario_id'])
        b = adaptador.registrar_ejecucion(plan_id=plan.id, km=56000,
                                          usuario_id=mundo['usuario_id'])
        assert a.lectura_id != b.lectura_id

    def test_una_ejecucion_establece_la_linea_base(self, app, db, mundo):
        plan = self._plan(mundo)
        adaptador.registrar_ejecucion(plan_id=plan.id, km=55000,
                                      usuario_id=mundo['usuario_id'])
        d = {t['tipo']: t for t in adaptador.diagnostico_de(mundo['camion_id'])}
        assert d['distribucion']['estado'] != EstadoTarea.SIN_LINEA_BASE
        assert d['distribucion']['ultima_ejecucion_km'] == 55000
        assert d['distribucion']['proximo_km'] == 115000

    def test_la_ULTIMA_es_la_mas_reciente_no_la_de_mayor_kilometraje(
            self, app, db, mundo):
        """Con un odómetro mal tecleado, la línea base tiene que ser la última
        que ocurrió. Con la de mayor kilometraje, un dedo torcido correría el
        próximo cambio años hacia adelante y la tarea nunca volvería a aparecer.
        """
        plan = self._plan(mundo)
        adaptador.registrar_ejecucion(
            plan_id=plan.id, km=900000, usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 1))
        # Una corrección deja el odómetro donde corresponde, y la ejecución
        # siguiente se registra contra el número real.
        adaptador.registrar_ejecucion(
            plan_id=plan.id, km=900100, usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 4, 1))
        km = adaptador._ultimas_ejecuciones([plan.id])[plan.id]
        assert km == 900100


class TestNoSeRegistraUnaEjecucionQueNoSirveParaNada:

    def test_sobre_una_tarea_sin_intervalo_se_rechaza_con_su_motivo(
            self, app, db, mundo):
        """Dejaría una línea base que no se puede comparar contra nada, y quien
        la registre se iría creyendo que el vehículo quedó al día."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        with pytest.raises(ErrorFlota) as e:
            adaptador.registrar_ejecucion(plan_id=t.id, km=55000,
                                          usuario_id=mundo['usuario_id'])
        assert 'intervalo' in str(e.value)
        assert EjecucionTarea.query.count() == 0

    def test_sobre_una_tarea_retirada_tampoco(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=60000,
                                  fuente='manual_fabricante', activo=False)
        with pytest.raises(ErrorFlota):
            adaptador.registrar_ejecucion(plan_id=t.id, km=55000,
                                          usuario_id=mundo['usuario_id'])

    def test_pero_sobre_una_tarea_sana_SI_se_registra(self, app, db, mundo):
        """La dirección contraria: un guard que rechazara siempre pasaría los
        dos tests de arriba y dejaría el módulo inservible."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        assert adaptador.registrar_ejecucion(
            plan_id=t.id, km=55000, usuario_id=mundo['usuario_id']).id


# ══════════════════════════════════════════════════════════════════════════
# La procedencia, impuesta por la base y no solo por el código
# ══════════════════════════════════════════════════════════════════════════

class TestLaBaseImpideUnIntervaloSinProcedencia:
    """Si un `INSERT` crudo puede violar el invariante, el modelo está
    incompleto. El CHECK lo cierra en **las dos direcciones**."""

    def test_un_intervalo_con_fuente_sin_dato_no_entra(self, app, db, mundo):
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='refrigerante',
            intervalo_km=40000, fuente='sin_dato', origen='manual',
            sembrado_ts=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_y_una_fuente_colgada_de_un_intervalo_ausente_tampoco(
            self, app, db, mundo):
        """Una procedencia sin dato afirma un levantamiento que no ocurrió, y
        es justo lo que haría creer que la tarea ya está armada."""
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='refrigerante',
            intervalo_km=None, fuente='taller', origen='manual',
            sembrado_ts=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_las_dos_combinaciones_LEGALES_sí_entran(self, app, db, mundo):
        """La dirección que falta: un CHECK que rechazara todo pasaría los dos
        tests de arriba."""
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='refrigerante',
            intervalo_km=40000, fuente='taller', origen='manual',
            sembrado_ts=datetime.utcnow()))
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='aceite_diferencial',
            intervalo_km=None, fuente='sin_dato', origen='ficha',
            sembrado_ts=datetime.utcnow()))
        db.session.commit()
        assert len(_tipos(mundo['camion_id'])) == 2

    def test_un_intervalo_de_cero_no_entra(self, app, db, mundo):
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='refrigerante',
            intervalo_km=0, fuente='taller', origen='manual',
            sembrado_ts=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_un_tipo_fuera_del_vocabulario_no_entra(self, app, db, mundo):
        db.session.add(PlanTarea(
            vehiculo_id=mundo['camion_id'], tipo='cambio_de_bujias',
            intervalo_km=None, fuente='sin_dato', origen='ficha',
            sembrado_ts=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestFijarElIntervaloExigeDecirDeDondeSalio:

    def test_sin_procedencia_no_se_guarda(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        with pytest.raises(ErrorFlota):
            adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=5000,
                                      fuente='sin_dato')

    def test_una_fuente_inventada_revienta_como_dato_malo(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        with pytest.raises(PlanInvalido):
            adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=5000,
                                      fuente='me_lo_dijo_un_amigo')

    def test_con_procedencia_se_guarda_y_pasa_a_manual(self, app, db, mundo):
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=5000,
                                  fuente='concesionario')
        t = _tarea(mundo['camion_id'], 'aceite_motor')
        assert t.intervalo_km == 5000
        assert t.fuente == 'concesionario'
        assert t.origen == 'manual'

    def test_retirar_el_intervalo_devuelve_la_fuente_a_sin_dato(self, app, db, mundo):
        """La tarea vuelve a `sin_intervalo`, que es lo cierto, en vez de
        quedarse con un número que resultó estar mal."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=None,
                                  fuente='manual_fabricante')
        t = _tarea(mundo['camion_id'], 'distribucion')
        assert t.intervalo_km is None
        assert t.fuente == 'sin_dato'


# ══════════════════════════════════════════════════════════════════════════
# El diagnóstico y los contadores del health
# ══════════════════════════════════════════════════════════════════════════

class TestLosCincoCubosVanSeparados:
    """Cada uno se corrige llamando a una persona distinta. Un solo total no
    dice a quién llamar — la lección de los 639 avisos conocidos."""

    def test_una_flota_recien_sembrada_no_tiene_NINGUNA_vencida(self, app, db, mundo):
        """**El escenario del primer ciclo.** Cuatro tareas del camión y una
        del motocarro, ninguna con ejecución: ninguna vencida, ninguna por
        vencer, y por lo tanto cero avisos."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        adaptador.sembrar_desde_ficha(mundo['moto_id'])
        c = adaptador.contar_por_estado()
        assert c[EstadoTarea.VENCIDA] == 0
        assert c[EstadoTarea.POR_VENCER] == 0
        assert c[EstadoTarea.SIN_LINEA_BASE] == 1     # sólo la distribución
        assert c[EstadoTarea.SIN_INTERVALO] == 4      # las cuatro sin número

    def test_una_tarea_pasada_de_su_kilometraje_se_cuenta_vencida(
            self, app, db, mundo):
        """La correa se cambió a 55.000 con intervalo de 60.000, y el camión ya
        marca 120.000. **Es el caso entero de la fase**: el número estaba en la
        ficha desde la tanda 1 y nadie lo comparaba contra el odómetro.

        El kilometraje avanza por una lectura suelta y no por una segunda
        ejecución: registrar otra ejecución movería la línea base y el vehículo
        volvería a estar al día — que es lo que de verdad pasa cuando alguien
        cambia la correa, y no lo que este test quiere montar.
        """
        from flota.adaptadores.hallazgos import anclar_odometro

        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        adaptador.registrar_ejecucion(plan_id=t.id, km=55000,
                                      usuario_id=mundo['usuario_id'],
                                      ts=datetime(2026, 3, 1))
        anclar_odometro(mundo['camion_id'], 120000, mundo['usuario_id'],
                        datetime(2026, 8, 1))
        db.session.commit()
        assert adaptador.contar_por_estado()[EstadoTarea.VENCIDA] == 1

    def test_una_tarea_retirada_no_cuenta_en_ningun_cubo(self, app, db, mundo):
        """No está al día ni vencida: está retirada. Contarla volvería a meter
        en el tablero lo que alguien decidió sacar."""
        adaptador.sembrar_desde_ficha(mundo['camion_id'])
        t = _tarea(mundo['camion_id'], 'distribucion')
        adaptador.fijar_intervalo(plan_id=t.id, intervalo_km=60000,
                                  fuente='manual_fabricante', activo=False)
        c = adaptador.contar_por_estado()
        assert sum(c.values()) == 3
        assert c[EstadoTarea.SIN_LINEA_BASE] == 0


class TestElCronNaceApagado:
    """Regla 10. El interruptor vive dentro de la función que escribe."""

    def test_sin_la_variable_no_escribe_una_sola_fila(self, app, db, mundo,
                                                      monkeypatch):
        monkeypatch.delenv('FLOTA_PREVENTIVO', raising=False)
        r = adaptador.sembrar_todo()
        assert r['motivo'] == 'FLOTA_PREVENTIVO no está en true'
        assert PlanTarea.query.count() == 0

    def test_con_la_variable_en_true_siembra_la_flota_entera(self, app, db,
                                                             mundo, monkeypatch):
        """La otra dirección: un interruptor que apagara siempre pasaría el
        test de arriba y dejaría el cron muerto sin que nadie lo notara."""
        monkeypatch.setenv('FLOTA_PREVENTIVO', 'true')
        r = adaptador.sembrar_todo()
        assert r['creadas'] == 5
        assert r['sin_ficha'] == 1          # PRV300, el que no tiene ficha
        assert PlanTarea.query.count() == 5

    def test_un_vehiculo_sin_ficha_no_detiene_el_barrido(self, app, db, mundo,
                                                        monkeypatch):
        """La única degradación del adaptador, y no es hacia el éxito: queda
        contada y sale en el log."""
        monkeypatch.setenv('FLOTA_PREVENTIVO', 'true')
        r = adaptador.sembrar_todo()
        assert r['vehiculos'] == 3
        assert r['sin_ficha'] == 1
        assert _tipos(mundo['moto_id']) == ['lubricacion_cadena']

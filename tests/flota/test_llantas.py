"""
Llantas — dominio, adaptador y frontera HTTP, contra la base real.

`flota_montaje_llanta` es **literalmente la forma de `flota_custodia`**, así que
lo que hay que probar es lo mismo que ahí y en las dos direcciones:

1. que los dos índices únicos parciales **rechacen** las dos combinaciones
   imposibles —dos llantas en una posición, una llanta en dos sitios—, y
2. que **NO** disparen sobre operación sana: N montajes cerrados de la misma
   posición conviven, y una llanta desmontada se vuelve a montar sin problema.

La segunda mitad es la que casi nunca se escribe y es la que importa: un índice
único sin filtro parcial pasaría el punto 1 entero y volvería imposible la
historia de una posición, que es justo lo que la tabla existe para guardar.

## Lo demás que este archivo afirma, y por qué no es cosmético

· **La posición se valida contra la ficha en la BASE**, no en un `if` del
  adaptador: hay un test que inserta por SQL crudo saltándose el adaptador y
  exige que el trigger lo frene. Un invariante que solo vive en Python es una
  validación que el próximo escritor se olvida de copiar.
· **`km_acumulado` no existe como columna.** Se afirma por introspección del
  modelo, no leyendo el código: con 24 llantas, denormalizar es garantizar
  divergencia.
· **Ninguna tabla de llantas lleva plata.** El trinquete del plan es literal —
  si aparece `valor`, `costo` o `monto` fuera de `flota_gasto`, la frontera se
  cruzó y el CPK se vuelve un `UNION` de N ramas.
· **Los tres estados del kilometraje** —un número, `vigente`, `sin_dato`— y que
  ninguno sea cero ni `None`.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from flota.adaptadores import llantas as adaptador
from flota.dominio import llantas as dom
from flota.dominio.llantas import SIN_DATO, VIGENTE

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


def _verificar_lecturas(vehiculo_id, usuario_id):
    """Pasa por la cola de verificación, como haría una persona con la foto.

    **Necesario y no un andamio.** El formulario de montaje no pide foto del
    tablero, así que toda lectura que nace de un montaje nace `dudosa`
    (`confianza_al_nacer`, regla 1). Con los dos extremos dudosos,
    `confianza_del_tramo` devuelve `SIN_DATO` y el kilometraje de la llanta no se
    publica — que es exactamente lo que la fase 0 quiso, y hay un test que lo
    afirma (`TestElKmNoSePublicaSobreUnTramoQueNadiePuedeRespaldar`).
    """
    from flota.adaptadores import verificacion

    for lectura, _placa in verificacion.pendientes():
        if lectura.vehiculo_id == vehiculo_id:
            verificacion.verificar(lectura_id=lectura.id, usuario_id=usuario_id)


@pytest.fixture
def mundo(db, almacen):
    """Un camión de 6 posiciones, un furgón de 4, uno SIN ficha, y las tres
    clases de usuario que el módulo distingue."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import FichaTecnica

    camion = Vehiculo(placa='LLA100', tipo='NHR', activo=True)
    furgon = Vehiculo(placa='LLA200', tipo='furgon', activo=True)
    sin_ficha = Vehiculo(placa='LLA300', tipo='NHR', activo=True)
    cond = Usuario(nombre='Conductor LL', email='lla_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control LL', email='lla_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda LL', email='lla_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    db.session.add_all([camion, furgon, sin_ficha, cond, flota, tienda])
    db.session.flush()
    db.session.add(FichaTecnica(
        vehiculo_id=camion.id, posiciones_llanta=6, km_inicial=0,
        km_inicial_ts=datetime(2026, 1, 1), medida_llanta='215/75R17.5'))
    db.session.add(FichaTecnica(
        vehiculo_id=furgon.id, posiciones_llanta=4, km_inicial=0,
        km_inicial_ts=datetime(2026, 1, 1)))
    db.session.commit()
    return {
        'camion_id': camion.id, 'placa': camion.placa,
        'furgon_id': furgon.id, 'placa_furgon': furgon.placa,
        'sin_ficha_id': sin_ficha.id, 'placa_sin_ficha': sin_ficha.placa,
        'usuario_id': flota.id,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        'db': db,
    }


def _llanta(mundo, codigo='L-001', medida='215/75R17.5'):
    return adaptador.dar_de_alta(codigo=codigo, medida=medida,
                                 registrada_por_usuario_id=mundo['usuario_id'])


def _montar(mundo, llanta, posicion=1, km=1000, vehiculo=None, **extra):
    return adaptador.montar(
        llanta_id=llanta.id, vehiculo_id=vehiculo or mundo['camion_id'],
        posicion=posicion, km=km,
        montada_por_usuario_id=mundo['usuario_id'], **extra)


def _desmontar(mundo, montaje, km=5000, motivo='desgaste_normal', **extra):
    return adaptador.desmontar(
        montaje_id=montaje.id, km=km, motivo=motivo,
        desmontada_por_usuario_id=mundo['usuario_id'], **extra)


# ══════════════════════════════════════════════════════════════════════════
# LOS DOS ÍNDICES ÚNICOS PARCIALES — las dos direcciones de cada uno
# ══════════════════════════════════════════════════════════════════════════

class TestDosLlantasEnLaMismaPosicionNoPuedenExistir:
    """`uq_flota_montaje_posicion_vigente` — la primera combinación imposible.

    Es la forma de `uq_flota_custodia_activa`: índice parcial sobre las filas
    abiertas.
    """

    def test_el_adaptador_lo_rechaza_con_un_mensaje_legible(self, app, db, mundo):
        a, b = _llanta(mundo, 'A-1'), _llanta(mundo, 'B-1')
        _montar(mundo, a, posicion=1)
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _montar(mundo, b, posicion=1, km=1200)
        assert 'A-1' in str(e.value)          # dice CUÁL está puesta

    def test_y_la_BASE_lo_impide_aunque_el_adaptador_no_mire(self, app, db, mundo):
        """**El punto entero.** Un `check-then-insert` sin índice detrás no es un
        invariante: es una carrera. Se inserta por SQL crudo, saltándose el
        adaptador, que es lo que hace una migración o un script de alguien."""
        from flota.adaptadores.modelos import MontajeLlanta

        a, b = _llanta(mundo, 'A-2'), _llanta(mundo, 'B-2')
        m = _montar(mundo, a, posicion=3)
        with pytest.raises(IntegrityError):
            db.session.add(MontajeLlanta(
                llanta_id=b.id, vehiculo_id=mundo['camion_id'], posicion=3,
                inicio_ts=datetime(2026, 5, 1),
                lectura_inicio_id=m.lectura_inicio_id,
                montada_por_usuario_id=mundo['usuario_id']))
            db.session.flush()
        db.session.rollback()

    def test_la_MISMA_posicion_admite_N_montajes_CERRADOS(self, app, db, mundo):
        """**La otra dirección, y es la que casi nunca se escribe.**

        Un índice único sin filtro parcial pasaría los dos tests de arriba y
        volvería imposible la historia de una posición — que es exactamente lo
        que esta tabla existe para guardar. Tres llantas distintas pasaron por la
        posición 2 y las tres filas conviven.
        """
        from flota.adaptadores.modelos import MontajeLlanta

        for i, km in enumerate([(1000, 5000), (5000, 9000), (9000, 13000)]):
            ll = _llanta(mundo, f'C-{i}')
            m = _montar(mundo, ll, posicion=2, km=km[0])
            _desmontar(mundo, m, km=km[1])
        vivos = _montar(mundo, _llanta(mundo, 'C-vivo'), posicion=2, km=13000)

        assert MontajeLlanta.query.filter_by(
            vehiculo_id=mundo['camion_id'], posicion=2).count() == 4
        assert vivos.fin_ts is None

    def test_la_misma_posicion_de_OTRO_vehiculo_no_colisiona(self, app, db, mundo):
        """El índice es por (vehículo, posición). La posición 1 del furgón no es
        la posición 1 del camión."""
        a, b = _llanta(mundo, 'D-1'), _llanta(mundo, 'D-2')
        _montar(mundo, a, posicion=1)
        m = _montar(mundo, b, posicion=1, vehiculo=mundo['furgon_id'], km=800)
        assert m.fin_ts is None


class TestUnaLlantaNoPuedeEstarEnDosSitios:
    """`uq_flota_montaje_llanta_vigente` — la segunda combinación imposible.

    Es el hermano que en `Custodia` faltó: aquel impedía dos custodias de un
    vehículo, y no había nada que impidiera que un conductor tuviera dos
    vehículos. Acá las dos preguntas nacen con su índice.

    Sin éste, montar sin desmontar antes deja la misma llanta viva en dos
    posiciones y **sus kilómetros se cuentan dos veces, sin que nada falle**.
    """

    def test_no_se_puede_montar_en_dos_posiciones_del_mismo_vehiculo(
            self, app, db, mundo):
        ll = _llanta(mundo, 'E-1')
        _montar(mundo, ll, posicion=1)
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _montar(mundo, ll, posicion=2, km=1100)
        assert 'dos sitios' in str(e.value)

    def test_no_se_puede_montar_en_dos_vehiculos(self, app, db, mundo):
        ll = _llanta(mundo, 'E-2')
        _montar(mundo, ll, posicion=1)
        with pytest.raises(adaptador.LlantaInvalida):
            _montar(mundo, ll, posicion=1, vehiculo=mundo['furgon_id'], km=900)

    def test_y_la_BASE_lo_impide_aunque_el_adaptador_no_mire(self, app, db, mundo):
        from flota.adaptadores.modelos import MontajeLlanta

        ll = _llanta(mundo, 'E-3')
        m = _montar(mundo, ll, posicion=1)
        with pytest.raises(IntegrityError):
            db.session.add(MontajeLlanta(
                llanta_id=ll.id, vehiculo_id=mundo['furgon_id'], posicion=4,
                inicio_ts=datetime(2026, 5, 1),
                lectura_inicio_id=m.lectura_inicio_id,
                montada_por_usuario_id=mundo['usuario_id']))
            db.session.flush()
        db.session.rollback()

    def test_desmontada_SI_se_puede_volver_a_montar(self, app, db, mundo):
        """La otra dirección. Una llanta que rota de posición —o que va a otro
        camión— es el caso normal, y es la razón entera de que la llanta sea una
        entidad. Un índice sin filtro parcial la habría dejado inmontable para
        siempre después del primer desmontaje."""
        ll = _llanta(mundo, 'E-4')
        m = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m, km=5000, motivo='rotacion')
        otro = _montar(mundo, ll, posicion=5, km=5000)
        assert otro.fin_ts is None
        assert len(adaptador.historia_de(ll.id)) == 2


# ══════════════════════════════════════════════════════════════════════════
# La posición tiene que existir en ESE vehículo — y lo impide la base
# ══════════════════════════════════════════════════════════════════════════

class TestLaPosicionSaleDeLaFichaYNoDeUnIf:

    def test_la_posicion_6_de_un_furgon_de_4_no_entra(self, app, db, mundo):
        ll = _llanta(mundo, 'F-1')
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _montar(mundo, ll, posicion=6, vehiculo=mundo['furgon_id'], km=100)
        assert '4' in str(e.value)             # dice cuántas declara la ficha

    def test_la_posicion_6_de_un_camion_de_6_SI_entra(self, app, db, mundo):
        """La otra dirección: el guard no puede prohibir operación sana."""
        m = _montar(mundo, _llanta(mundo, 'F-2'), posicion=6)
        assert m.posicion == 6

    def test_un_vehiculo_SIN_ficha_no_admite_montajes(self, app, db, mundo):
        """Lado conservador (regla 0): sin ficha no hay contra qué validar la
        posición, y aceptar por omisión dejaría entrar la posición 9 de un
        motocarro sin que nada fallara."""
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _montar(mundo, _llanta(mundo, 'F-3'), posicion=1,
                    vehiculo=mundo['sin_ficha_id'], km=100)
        assert 'ficha' in str(e.value)

    def test_el_TRIGGER_lo_impide_aunque_nadie_llame_al_adaptador(
            self, app, db, mundo):
        """`posiciones_llanta` vive en otra tabla, así que ningún CHECK puede
        mirarla: la impone un trigger. Se inserta por SQL crudo — es lo que hace
        un script, una migración, o el segundo escritor que se olvide de copiar
        la validación."""
        from flota.adaptadores.modelos import MontajeLlanta

        ll = _llanta(mundo, 'F-4')
        base = _montar(mundo, _llanta(mundo, 'F-5'), posicion=1)
        with pytest.raises((IntegrityError, OperationalError)):
            db.session.add(MontajeLlanta(
                llanta_id=ll.id, vehiculo_id=mundo['furgon_id'], posicion=9,
                inicio_ts=datetime(2026, 5, 1),
                lectura_inicio_id=base.lectura_inicio_id,
                montada_por_usuario_id=mundo['usuario_id']))
            db.session.flush()
        db.session.rollback()

    def test_el_CHECK_de_rango_frena_una_posicion_absurda(self, app, db, mundo):
        """El techo del vocabulario (`MAX_POSICIONES_LLANTA`), que un CHECK sí
        puede saber solo."""
        from flota.dominio.valores import MAX_POSICIONES_LLANTA

        assert not dom.posicion_valida(MAX_POSICIONES_LLANTA + 1, 99)
        assert not dom.posicion_valida(0, 6)
        assert dom.posicion_valida(6, 6)


# ══════════════════════════════════════════════════════════════════════════
# El tercer estado — `vigente` no es cero y no es «no sé»
# ══════════════════════════════════════════════════════════════════════════

class TestVigenteEsUnTercerEstado:

    def test_una_llanta_montada_no_tiene_cero_km(self, app, db, mundo):
        """Regla 4. Devolver 0 la haría aparecer como la que menos dura del
        parque, que es exactamente al revés."""
        ll = _llanta(mundo, 'G-1')
        _montar(mundo, ll, posicion=1)
        km, marca = adaptador.km_de_llanta(ll.id)
        assert km == VIGENTE
        assert km != 0 and km is not None
        assert marca == VIGENTE

    def test_vigente_es_verdadero_en_contexto_booleano(self):
        """Un `vigente` falsy invita a `valor or 0`, que es el default optimista
        que la regla 1 prohíbe."""
        assert bool(VIGENTE) is True
        assert VIGENTE != 0 and VIGENTE != '' and VIGENTE is not None

    def test_vigente_NO_es_sin_dato(self):
        """«Todavía está rodando» y «no sé cuánto rodó» son dos cosas distintas.
        Colapsarlas dejaría la pantalla sin poder decir la única frase útil sobre
        una llanta montada."""
        assert VIGENTE != SIN_DATO

    def test_una_llanta_que_nunca_se_monto_es_sin_dato_no_cero(self, app, db, mundo):
        ll = _llanta(mundo, 'G-2')
        km, _m = adaptador.km_de_llanta(ll.id)
        assert km == SIN_DATO


class TestElKmSeCalculaYNoSeGuarda:

    def test_no_existe_ninguna_columna_km_acumulado(self, app):
        """Por introspección del modelo, no leyendo el código: con 24 llantas
        una columna denormalizada es garantía de divergencia."""
        from flota.adaptadores.modelos import Llanta, MontajeLlanta

        for modelo in (Llanta, MontajeLlanta):
            nombres = {c.name for c in modelo.__table__.columns}
            assert not [n for n in nombres if 'acumulad' in n]

    def test_la_resta_sale_de_las_dos_lecturas(self, app, db, mundo):
        ll = _llanta(mundo, 'H-1')
        m = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m, km=41000)
        _verificar_lecturas(mundo['camion_id'], mundo['usuario_id'])
        km, _marca = adaptador.km_de_llanta(ll.id)
        assert km == 40000

    def test_suma_los_tramos_de_dos_montajes(self, app, db, mundo):
        """Lo que solo se puede hacer si la llanta es una entidad: la misma
        llanta en dos posiciones distintas, sumada."""
        ll = _llanta(mundo, 'H-2')
        m1 = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m1, km=21000, motivo='rotacion')
        m2 = _montar(mundo, ll, posicion=4, km=21000)
        _desmontar(mundo, m2, km=36000)
        _verificar_lecturas(mundo['camion_id'], mundo['usuario_id'])
        km, _marca = adaptador.km_de_llanta(ll.id)
        assert km == 35000

    def test_un_montaje_vigente_NO_suma_un_parcial(self, app, db, mundo):
        """Sumarle un tramo a medias mezclaría una vida terminada con una en
        curso, y el resultado no sería ni una cosa ni la otra."""
        ll = _llanta(mundo, 'H-3')
        m1 = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m1, km=21000, motivo='rotacion')
        _montar(mundo, ll, posicion=4, km=21000)
        _verificar_lecturas(mundo['camion_id'], mundo['usuario_id'])
        km, _marca = adaptador.km_de_llanta(ll.id)
        assert km == 20000


class TestElKmNoSePublicaSobreUnTramoQueNadiePuedeRespaldar:
    """La confianza viaja hasta el número, y no se reimplementa acá.

    El formulario de montaje no pide foto del tablero, así que las lecturas
    nacen `dudosa` (regla 1). Con los dos extremos dudosos,
    `confianza_del_tramo` dice que no hay número — el mismo contrato que el CPK.
    """

    def test_dos_lecturas_dudosas_dan_sin_dato_y_no_un_numero(self, app, db, mundo):
        ll = _llanta(mundo, 'I-1')
        m = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m, km=41000)
        # SIN pasar por la cola de verificación, que es el estado real de hoy.
        km, marca = adaptador.km_de_llanta(ll.id)
        assert km == SIN_DATO
        assert marca == SIN_DATO

    def test_un_extremo_dudoso_SI_publica_el_numero_pero_marcado(
            self, app, db, mundo):
        """«Se publica con su marca, nunca como firme». Hay un extremo sólido y
        una resta real: el número sale, y quien lo agregue puede excluirlo."""
        from flota.adaptadores.modelos import MontajeLlanta

        ll = _llanta(mundo, 'I-2')
        m = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m, km=41000)
        # Se verifica SOLO la lectura de apertura.
        from flota.adaptadores import verificacion
        fila = MontajeLlanta.query.get(m.id)
        verificacion.verificar(lectura_id=fila.lectura_inicio_id,
                               usuario_id=mundo['usuario_id'])
        km, marca = adaptador.km_de_llanta(ll.id)
        assert km == 40000
        assert marca == 'dudosa'

    def test_un_tramo_sin_dato_hace_sin_dato_toda_la_suma(self, app, db, mundo):
        """Un sumando desconocido hace desconocida la suma. Publicar el resto
        sería un total que se lee como completo y no lo es."""
        assert dom.km_acumulado([(1000, 'declarada'),
                                 (SIN_DATO, SIN_DATO)]) == (SIN_DATO, SIN_DATO)

    def test_el_total_hereda_la_PEOR_marca_de_sus_sumandos(self):
        """Un total no puede ser más confiable que su peor sumando."""
        assert dom.km_acumulado([(10, 'verificada'), (20, 'dudosa')]) == (30, 'dudosa')
        assert dom.km_acumulado([(10, 'verificada'), (20, 'verificada')]) == (30, 'verificada')


# ══════════════════════════════════════════════════════════════════════════
# El desenlace: vigente y cerrado se ven distinto en TODAS las columnas
# ══════════════════════════════════════════════════════════════════════════

class TestElCierreEsCompletoONoEs:

    def test_un_fin_sin_lectura_de_cierre_no_entra(self, app, db, mundo):
        """La fila silenciosa que el CHECK impide: un tramo que la pantalla
        muestra como terminado y que el cálculo no puede cerrar."""
        m = _montar(mundo, _llanta(mundo, 'J-1'), posicion=1)
        with pytest.raises(IntegrityError) as e:
            db.session.execute(text(
                'UPDATE flota_montaje_llanta SET fin_ts = :t WHERE id = :i'
            ), {'t': datetime(2099, 6, 1), 'i': m.id})
            db.session.commit()
        assert 'desenlace' in str(e.value)
        db.session.rollback()

    def test_una_lectura_de_cierre_sobre_un_montaje_abierto_tampoco(
            self, app, db, mundo):
        """La otra dirección del mismo CHECK."""
        m = _montar(mundo, _llanta(mundo, 'J-2'), posicion=1)
        with pytest.raises(IntegrityError) as e:
            db.session.execute(text(
                'UPDATE flota_montaje_llanta SET lectura_fin_id = :l WHERE id = :i'
            ), {'l': m.lectura_inicio_id, 'i': m.id})
            db.session.commit()
        assert 'desenlace' in str(e.value)
        db.session.rollback()

    def test_un_cierre_completo_SI_entra(self, app, db, mundo):
        """Y el guard no puede prohibir operación sana."""
        m = _desmontar(mundo, _montar(mundo, _llanta(mundo, 'J-3'), posicion=1))
        assert m.fin_ts is not None
        assert m.lectura_fin_id is not None
        assert m.desmontada_por_usuario_id is not None
        assert m.motivo_desmontaje == 'desgaste_normal'

    def test_desmontar_dos_veces_levanta(self, app, db, mundo):
        """El segundo cierre movería un número de kilómetros que ya se publicó, y
        el silencio haría que un cierre equivocado se tapara con otro."""
        m = _desmontar(mundo, _montar(mundo, _llanta(mundo, 'J-4'), posicion=1))
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _desmontar(mundo, m, km=9000)
        assert 'ya se cerró' in str(e.value)

    def test_un_motivo_inventado_no_entra(self, app, db, mundo):
        m = _montar(mundo, _llanta(mundo, 'J-5'), posicion=1)
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _desmontar(mundo, m, motivo='se_veia_fea')
        assert 'sin_dato' in str(e.value)      # dice cuál es la salida honesta

    def test_sin_dato_ES_un_motivo_legitimo(self, app, db, mundo):
        """Regla 1: si «no sé» no fuera una respuesta posible, la cómoda sería
        siempre `desgaste_normal`."""
        m = _desmontar(mundo, _montar(mundo, _llanta(mundo, 'J-6'), posicion=1),
                       motivo='sin_dato')
        assert m.motivo_desmontaje == 'sin_dato'


# ══════════════════════════════════════════════════════════════════════════
# Regla 3 — el odómetro se ancla, no se fabrica
# ══════════════════════════════════════════════════════════════════════════

class TestLaLecturaSeAnclaNoSeCopia:

    def test_el_montaje_nace_con_su_lectura_y_el_origen_es_ot(self, app, db, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        m = _montar(mundo, _llanta(mundo, 'K-1'), posicion=1, km=1234)
        lec = LecturaOdometro.query.get(m.lectura_inicio_id)
        assert lec.valor_km == 1234
        # El mismo origen que `gastos.ORIGEN_DE_LECTURA` le da a la categoría
        # `llanta`: las dos vías dicen lo mismo sobre el mismo gesto.
        assert lec.origen == 'ot'

    def test_seis_llantas_del_mismo_taller_cuelgan_de_UNA_lectura(
            self, app, db, mundo):
        """El punto entero de reutilizar `anclar_odometro`: no fabricar desde
        adentro del sistema el ruido que `lecturas_ts_duplicado` existe para
        contar."""
        from flota.adaptadores.modelos import LecturaOdometro

        ids = set()
        for p in range(1, 7):
            ids.add(_montar(mundo, _llanta(mundo, f'K-{p}'), posicion=p,
                            km=8000).lectura_inicio_id)
        assert len(ids) == 1
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['camion_id']).count() == 1

    def test_un_km_distinto_SI_produce_una_lectura_nueva(self, app, db, mundo):
        """La otra dirección: si el odómetro se movió, eso es información nueva y
        descartarla guardaría un kilometraje que nadie midió."""
        from flota.adaptadores.modelos import LecturaOdometro

        _montar(mundo, _llanta(mundo, 'K-a'), posicion=1, km=8000)
        _montar(mundo, _llanta(mundo, 'K-b'), posicion=2, km=8400)
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=mundo['camion_id']).count() == 2

    def test_un_odometro_que_retrocede_no_entra_por_esta_puerta(
            self, app, db, mundo):
        """La monotonía es del dominio y la hereda el montaje por usar la misma
        política. Una segunda puerta con su propia validación es cómo se abre el
        agujero."""
        from flota.dominio.errores import ErrorFlota

        m = _montar(mundo, _llanta(mundo, 'K-x'), posicion=1, km=9000)
        with pytest.raises(ErrorFlota):
            _desmontar(mundo, m, km=100)


# ══════════════════════════════════════════════════════════════════════════
# La espina: ninguna tabla de llantas lleva plata
# ══════════════════════════════════════════════════════════════════════════

class TestElDineroSigueEnLaEspina:

    def test_ninguna_columna_de_valor_costo_o_monto(self, app):
        """El trinquete del plan, literal. Con el valor repartido en N tablas el
        CPK se vuelve un `UNION` de N ramas y alguien va a olvidar la N+1 — ya
        pasó en este repo con `_BODEGA_CO_MAP`, y con plata."""
        from flota.adaptadores.modelos import Llanta, MontajeLlanta

        for modelo in (Llanta, MontajeLlanta):
            for c in modelo.__table__.columns:
                assert c.name not in ('valor', 'costo', 'monto', 'precio'), (
                    f'{modelo.__tablename__}.{c.name} cruza la frontera del '
                    f'dinero: el valor vive en flota_gasto')

    def test_el_montaje_se_puede_atar_a_un_gasto_de_categoria_llanta(
            self, app, db, mundo):
        from flota.adaptadores import gastos

        g = gastos.registrar_gasto(
            vehiculo_id=mundo['camion_id'], categoria='llanta',
            fecha=date(2026, 5, 1), valor=Decimal('1800000'),
            proveedor='Llantas del Huila', origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usuario_id'], km=8000,
            documento_numero='FV-9911')
        m = _montar(mundo, _llanta(mundo, 'M-1'), posicion=1, km=8000,
                    gasto_id=g.id)
        assert m.gasto_id == g.id

    def test_una_factura_de_seis_llantas_es_UN_gasto_y_seis_montajes(
            self, app, db, mundo):
        """`gasto_id` no es único a propósito: `uq_flota_gasto_documento` es por
        vehículo y documento, así que seis llantas de una misma factura son UNA
        fila de gasto. El valor no se reparte entre los montajes — se queda
        entero en la espina, que es donde el CPK lo divide."""
        from flota.adaptadores import gastos
        from flota.adaptadores.modelos import Gasto

        g = gastos.registrar_gasto(
            vehiculo_id=mundo['camion_id'], categoria='llanta',
            fecha=date(2026, 5, 1), valor=Decimal('5400000'),
            proveedor='Llantas del Huila', origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usuario_id'], km=8000,
            documento_numero='FV-9912')
        for p in range(1, 7):
            _montar(mundo, _llanta(mundo, f'N-{p}'), posicion=p, km=8000,
                    gasto_id=g.id)
        assert Gasto.query.filter_by(vehiculo_id=mundo['camion_id']).count() == 1

    def test_el_gasto_de_OTRO_vehiculo_se_rechaza(self, app, db, mundo):
        """Un montaje colgado de la factura del camión equivocado no rompe
        ninguna FK y no se ve raro: la respuesta a «¿de qué factura salió esta
        llanta?» apunta a un documento real que no es el suyo, y se lee con
        confianza."""
        from flota.adaptadores import gastos

        g = gastos.registrar_gasto(
            vehiculo_id=mundo['furgon_id'], categoria='llanta',
            fecha=date(2026, 5, 1), valor=Decimal('900000'),
            proveedor='Llantas del Huila', origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usuario_id'], km=500)
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _montar(mundo, _llanta(mundo, 'O-1'), posicion=1, km=8000,
                    gasto_id=g.id)
        assert 'otro vehículo' in str(e.value)

    def test_una_rotacion_no_lleva_gasto(self, app, db, mundo):
        """`gasto_id` nullable: montar una llanta que ya se tenía no compra
        nada."""
        m = _montar(mundo, _llanta(mundo, 'O-2'), posicion=1)
        assert m.gasto_id is None


# ══════════════════════════════════════════════════════════════════════════
# El análisis es POR POSICIÓN — y sin umbral (regla 13)
# ══════════════════════════════════════════════════════════════════════════

class TestLaVidaUtilEsUnHechoNoUnUmbral:

    def test_con_pocas_vidas_la_mediana_es_sin_dato_y_dice_cuantas_faltan(self):
        fila = dom.vida_util_por_posicion({1: [40000, 38000]})[0]
        assert fila['n'] == 2
        assert fila['mediana_km'] == SIN_DATO
        assert fila['faltan'] == dom.MONTAJES_CERRADOS_PARA_VIDA_UTIL - 2
        # El hecho se publica igual: los kilómetros medidos están ahí.
        assert fila['km'] == [38000, 40000]

    def test_con_suficientes_vidas_SI_publica_la_mediana(self):
        """La otra dirección: el guard no puede negarse para siempre, o el campo
        no sirve para fijar nada."""
        km = [30000, 32000, 34000, 36000, 38000, 40000]
        fila = dom.vida_util_por_posicion({3: km})[0]
        assert fila['mediana_km'] == 35000
        assert fila['faltan'] == 0

    def test_ningun_numero_de_kilometraje_de_cambio_esta_escrito_en_el_modulo(self):
        """Regla 13. **No hay una sola llanta medida en esta flota**, así que
        cualquier «se cambia a los X km» escrito hoy sería a ojo. La única
        constante numérica del módulo es cuántas vidas hacen falta para poder
        fijarlo con dato — y no marca ninguna llanta ni dispara ningún aviso."""
        import inspect

        fuente = inspect.getsource(dom)
        assert 'MONTAJES_CERRADOS_PARA_VIDA_UTIL = 6' in fuente
        for palabra in ('KM_MAXIMO', 'KM_DE_CAMBIO', 'VIDA_UTIL_KM',
                        'LIMITE_KM'):
            assert palabra not in fuente

    def test_agrupa_por_posicion_y_no_solo_por_vehiculo(self, app, db, mundo):
        """El modo de fallo caro no es que se gasten, es que se gasten mal — y
        eso solo se ve por posición."""
        # El odómetro es del VEHÍCULO y no de la llanta: las dos posiciones se
        # montan con la misma lectura y se desmontan en dos momentos distintos.
        # Escribirlo al revés violaría la monotonía, y que la violara es lo que
        # demuestra que el montaje no tiene una puerta propia al odómetro.
        m1 = _montar(mundo, _llanta(mundo, 'P-1'), posicion=1, km=1000)
        m2 = _montar(mundo, _llanta(mundo, 'P-2'), posicion=2, km=1000)
        _desmontar(mundo, m1, km=41000, motivo='desgaste_irregular')
        _desmontar(mundo, m2, km=52000, motivo='desgaste_normal')
        _verificar_lecturas(mundo['camion_id'], mundo['usuario_id'])
        filas = {f['posicion']: f for f in
                 adaptador.km_por_posicion(mundo['camion_id'])}
        assert filas[1]['km'] == [40000]
        assert filas[2]['km'] == [51000]

    def test_sin_llantas_desmontadas_devuelve_lista_vacia_y_no_un_cero(
            self, app, db, mundo):
        _montar(mundo, _llanta(mundo, 'P-x'), posicion=1)
        assert adaptador.km_por_posicion(mundo['camion_id']) == []

    def test_los_tramos_sin_dato_no_entran_al_hecho(self, app, db, mundo):
        """Un tramo que nadie puede respaldar no se rellena con nada: se
        excluye, y por eso el hecho medido sigue siendo un hecho."""
        m = _montar(mundo, _llanta(mundo, 'P-y'), posicion=1, km=1000)
        _desmontar(mundo, m, km=41000)         # sin verificar: los dos dudosos
        assert adaptador.km_por_posicion(mundo['camion_id']) == []


# ══════════════════════════════════════════════════════════════════════════
# Posiciones sin llanta — y el contador que impide que el detector se apague
# ══════════════════════════════════════════════════════════════════════════

class TestPosicionesSinLlanta:

    def test_las_seis_estan_libres_al_principio(self, app, db, mundo):
        assert adaptador.posiciones_libres(mundo['camion_id']) == [1, 2, 3, 4, 5, 6]

    def test_montar_una_la_saca_de_la_lista(self, app, db, mundo):
        _montar(mundo, _llanta(mundo, 'Q-1'), posicion=3)
        assert adaptador.posiciones_libres(mundo['camion_id']) == [1, 2, 4, 5, 6]

    def test_desmontarla_la_devuelve_a_la_lista(self, app, db, mundo):
        """La otra dirección: una posición cuya llanta salió vuelve a estar
        libre, y es lo que hace que el número mida algo."""
        m = _montar(mundo, _llanta(mundo, 'Q-2'), posicion=3)
        _desmontar(mundo, m, km=9000)
        assert 3 in adaptador.posiciones_libres(mundo['camion_id'])

    def test_sin_ficha_devuelve_SIN_DATO_y_jamas_lista_vacia(self, app, db, mundo):
        """**El corazón del detector.** `[]` significaría «se revisó y están
        todas cubiertas»; lo que pasa es que no se sabe cuántas hay. Un vehículo
        sin ficha saldría limpio para siempre — que es cómo un detector se apaga
        sin que nadie lo note."""
        libres = adaptador.posiciones_libres(mundo['sin_ficha_id'])
        assert libres == SIN_DATO
        assert libres != []


# ══════════════════════════════════════════════════════════════════════════
# Alta de llanta — identidad
# ══════════════════════════════════════════════════════════════════════════

class TestLaIdentidadDeLaLlanta:

    def test_dos_llantas_con_el_mismo_codigo_no_pueden_existir(self, app, db, mundo):
        _llanta(mundo, 'R-1')
        with pytest.raises(adaptador.LlantaInvalida) as e:
            _llanta(mundo, 'R-1')
        assert 'mezcladas' in str(e.value)

    def test_y_la_BASE_lo_impide(self, app, db, mundo):
        from flota.adaptadores.modelos import Llanta

        _llanta(mundo, 'R-2')
        with pytest.raises(IntegrityError):
            db.session.add(Llanta(codigo='R-2', medida='x',
                                  registrada_por_usuario_id=mundo['usuario_id'],
                                  creada_ts=datetime(2026, 5, 1)))
            db.session.flush()
        db.session.rollback()

    def test_sin_codigo_no_entra(self, app, db, mundo):
        with pytest.raises(adaptador.LlantaInvalida):
            adaptador.dar_de_alta(codigo='   ', medida='215/75R17.5',
                                  registrada_por_usuario_id=mundo['usuario_id'])

    def test_sin_medida_tampoco(self, app, db, mundo):
        with pytest.raises(adaptador.LlantaInvalida):
            adaptador.dar_de_alta(codigo='R-3', medida='',
                                  registrada_por_usuario_id=mundo['usuario_id'])

    def test_la_marca_sin_declarar_es_una_palabra_no_un_nulo(self, app, db, mundo):
        assert _llanta(mundo, 'R-4').marca == 'sin_dato'

    def test_una_llanta_dada_de_alta_no_esta_montada(self, app, db, mundo):
        """El alta y el montaje son dos gestos porque son dos hechos: la llanta
        llega a bodega, y semanas después el camión entra al taller."""
        ll = _llanta(mundo, 'R-5')
        assert adaptador.montaje_vigente_de(ll.id) is None
        assert ll in adaptador.sin_montar()

    def test_una_llanta_montada_no_aparece_como_disponible(self, app, db, mundo):
        ll = _llanta(mundo, 'R-6')
        _montar(mundo, ll, posicion=1)
        assert ll not in adaptador.sin_montar()

    def test_la_historia_de_una_llanta_cruza_dos_vehiculos(self, app, db, mundo):
        """Lo que solo se puede preguntar si la llanta es una entidad. Como
        renglón de factura, esta lista no se podría armar."""
        ll = _llanta(mundo, 'R-7')
        m = _montar(mundo, ll, posicion=1, km=1000)
        _desmontar(mundo, m, km=21000, motivo='rotacion')
        _montar(mundo, ll, posicion=2, vehiculo=mundo['furgon_id'], km=300)
        historia = adaptador.historia_de(ll.id)
        assert [h.vehiculo_id for h in historia] == [mundo['camion_id'],
                                                     mundo['furgon_id']]


# ══════════════════════════════════════════════════════════════════════════
# La frontera HTTP — y que no afloje ninguna política del adaptador
# ══════════════════════════════════════════════════════════════════════════

class TestQuienPuedeQue:

    def test_el_conductor_no_ve_las_llantas(self, client, db, mundo):
        r = client.get(f'/flota/llantas/{mundo["placa"]}',
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_tienda_tampoco(self, client, db, mundo):
        r = client.get(f'/flota/llantas/{mundo["placa"]}',
                       headers=_auth(mundo['t_tienda']))
        assert r.status_code == 403

    def test_control_de_flota_SI(self, client, db, mundo):
        """La otra dirección: un permiso que le cierra la puerta a todos no es un
        permiso, es un endpoint muerto."""
        r = client.get(f'/flota/llantas/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 200

    def test_el_conductor_no_puede_montar(self, client, db, mundo):
        ll = _llanta(mundo, 'S-1')
        r = client.post('/flota/montajes', headers=_auth(mundo['t_cond']),
                        json={'placa': mundo['placa'], 'llanta_id': ll.id,
                              'posicion': 1, 'km': 1000})
        assert r.status_code == 403

    def test_el_conductor_no_puede_dar_de_alta(self, client, db, mundo):
        r = client.post('/flota/llantas', headers=_auth(mundo['t_cond']),
                        json={'codigo': 'S-2', 'medida': 'x'})
        assert r.status_code == 403


class TestLaFronteraNoAflojaNada:

    def test_montar_devuelve_201_y_el_montaje(self, client, db, mundo):
        ll = _llanta(mundo, 'T-1')
        r = client.post('/flota/montajes', headers=_auth(mundo['t_flota']),
                        json={'placa': mundo['placa'], 'llanta_id': ll.id,
                              'posicion': 2, 'km': 1000})
        assert r.status_code == 201
        assert r.get_json()['posicion'] == 2
        assert r.get_json()['vigente'] is True
        assert r.get_json()['km'] == 'vigente'

    def test_la_posicion_imposible_da_409_y_no_201(self, client, db, mundo):
        ll = _llanta(mundo, 'T-2')
        r = client.post('/flota/montajes', headers=_auth(mundo['t_flota']),
                        json={'placa': mundo['placa_furgon'],
                              'llanta_id': ll.id, 'posicion': 6, 'km': 100})
        assert r.status_code == 409

    def test_la_posicion_ocupada_da_409_y_dice_cual(self, client, db, mundo):
        a, b = _llanta(mundo, 'T-3'), _llanta(mundo, 'T-4')
        _montar(mundo, a, posicion=1)
        r = client.post('/flota/montajes', headers=_auth(mundo['t_flota']),
                        json={'placa': mundo['placa'], 'llanta_id': b.id,
                              'posicion': 1, 'km': 1200})
        assert r.status_code == 409
        assert 'T-3' in r.get_json()['error']

    def test_una_placa_que_no_existe_da_404(self, client, db, mundo):
        r = client.get('/flota/llantas/ZZZ999', headers=_auth(mundo['t_flota']))
        assert r.status_code == 404

    def test_faltan_campos_da_400(self, client, db, mundo):
        r = client.post('/flota/montajes', headers=_auth(mundo['t_flota']),
                        json={'placa': mundo['placa']})
        assert r.status_code == 400

    def test_desmontar_sin_motivo_da_400(self, client, db, mundo):
        m = _montar(mundo, _llanta(mundo, 'T-5'), posicion=1)
        r = client.post(f'/flota/montajes/{m.id}/desmontar',
                        headers=_auth(mundo['t_flota']), json={'km': 9000})
        assert r.status_code == 400

    def test_desmontar_con_motivo_inventado_da_409(self, client, db, mundo):
        m = _montar(mundo, _llanta(mundo, 'T-6'), posicion=1)
        r = client.post(f'/flota/montajes/{m.id}/desmontar',
                        headers=_auth(mundo['t_flota']),
                        json={'km': 9000, 'motivo': 'se_veia_fea'})
        assert r.status_code == 409

    def test_desmontar_bien_da_200_con_el_motivo(self, client, db, mundo):
        m = _montar(mundo, _llanta(mundo, 'T-7'), posicion=1)
        r = client.post(f'/flota/montajes/{m.id}/desmontar',
                        headers=_auth(mundo['t_flota']),
                        json={'km': 9000, 'motivo': 'desgaste_irregular'})
        assert r.status_code == 200
        assert r.get_json()['motivo_desmontaje'] == 'desgaste_irregular'
        assert r.get_json()['vigente'] is False

    def test_el_expediente_publica_el_vocabulario_para_que_el_JS_no_lo_copie(
            self, client, db, mundo):
        """Si el JS llevara su propia lista de motivos, el día que se agregue uno
        el desplegable no lo ofrecería y esa causa no existiría nunca en los
        datos. Regla 0 con consecuencia."""
        d = client.get(f'/flota/llantas/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['motivos_desmontaje'] == list(dom.MOTIVOS_DESMONTAJE)
        assert d['vidas_para_fijar_util'] == dom.MONTAJES_CERRADOS_PARA_VIDA_UTIL

    def test_un_vehiculo_sin_ficha_dice_sin_dato_y_no_lista_vacia(
            self, client, db, mundo):
        d = client.get(f'/flota/llantas/{mundo["placa_sin_ficha"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['posiciones_libres'] == 'sin_dato'
        assert d['posiciones_declaradas'] is None

    def test_ninguna_respuesta_imputa_un_delito(self, client, db, mundo):
        """Regla 2. Un desgaste irregular es una desalineación, una presión mal
        llevada o un eje torcido, y ninguna de las tres se investiga mejor con un
        nombre al lado."""
        m = _montar(mundo, _llanta(mundo, 'T-8'), posicion=1)
        _desmontar(mundo, m, km=9000, motivo='desgaste_irregular')
        d = client.get(f'/flota/llantas/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_data(as_text=True).lower()
        for palabra in ('culpable', 'negligencia', 'responsable', 'sanción',
                        'conductor'):
            assert palabra not in d


# ══════════════════════════════════════════════════════════════════════════
# La medida nace con la tabla
# ══════════════════════════════════════════════════════════════════════════

class TestElHealthMideLasLlantasDesdeElPrimerDia:

    def test_los_cuatro_campos_estan_en_CAMPOS(self):
        """Un campo que nadie mira es el defecto que este módulo lleva toda la
        semana arreglando: `flota_lectura_odometro` vivió un mes con cero campos
        acá."""
        from flota.api.health import _CAMPOS

        for c in ('posiciones_sin_llanta', 'vehiculos_sin_posiciones_llanta',
                  'llantas_montadas', 'km_por_posicion'):
            assert c in _CAMPOS

    def test_el_medidor_los_implementa_todos(self, app, db, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        m = MedidorSQL()
        # 6 del camión + 4 del furgón, ninguna montada.
        assert m.posiciones_sin_llanta() == 10
        assert m.vehiculos_sin_posiciones_llanta() == 1     # el que no tiene ficha
        assert m.llantas_montadas() == 0
        assert m.km_por_posicion() == []

    def test_montar_mueve_los_dos_contadores(self, app, db, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        _montar(mundo, _llanta(mundo, 'U-1'), posicion=1)
        m = MedidorSQL()
        assert m.posiciones_sin_llanta() == 9
        assert m.llantas_montadas() == 1

    def test_el_contador_de_los_que_no_se_pudieron_mirar_va_APARTE(
            self, app, db, mundo):
        """Sin este campo, un parque entero sin ficha se vería idéntico a uno con
        las 24 llantas registradas: los dos dan `posiciones_sin_llanta` bajo. Es
        la forma exacta de `tanqueos_sin_capacidad_declarada`."""
        from flota.adaptadores.medicion import MedidorSQL

        assert MedidorSQL().vehiculos_sin_posiciones_llanta() == 1

    def test_el_km_por_posicion_sale_con_placa_y_como_hecho(self, app, db, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        m = _montar(mundo, _llanta(mundo, 'U-2'), posicion=1, km=1000)
        _desmontar(mundo, m, km=41000)
        _verificar_lecturas(mundo['camion_id'], mundo['usuario_id'])
        filas = MedidorSQL().km_por_posicion()
        assert len(filas) == 1
        assert filas[0]['placa'] == mundo['placa']
        assert filas[0]['km'] == [40000]
        # Regla 13: con una sola vida medida NO se publica ninguna mediana.
        assert filas[0]['mediana_km'] == 'sin_dato'
        assert filas[0]['faltan'] == dom.MONTAJES_CERRADOS_PARA_VIDA_UTIL - 1

    def test_el_health_responde_con_los_campos(self, client, db, mundo):
        d = client.get('/flota/health', headers=_auth(mundo['t_flota'])).get_json()
        assert d['llantas_montadas'] == 0
        assert d['posiciones_sin_llanta'] == 10
        assert d['km_por_posicion'] == []


# ══════════════════════════════════════════════════════════════════════════
# El acta de corte — deny-by-default, y las dos tablas NO van juntas
# ══════════════════════════════════════════════════════════════════════════

class TestElResetClasificaLasDosTablas:
    """El error de dejar una tabla sin clasificar ya costó tres veces. Acá se
    afirma **la clasificación concreta**, no solo que aparezcan."""

    def test_el_montaje_es_operativo_y_se_borra(self):
        """Cada fila dice «esta llanta estuvo acá entre estos dos kilometrajes»,
        y los kilometrajes del ensayo son del ensayo."""
        from scripts.reset_transaccional import OPERATIVAS, PROTEGIDAS_MAESTRAS

        assert 'flota_montaje_llanta' in OPERATIVAS
        assert 'flota_montaje_llanta' not in PROTEGIDAS_MAESTRAS

    def test_la_llanta_es_un_activo_y_NO_se_borra(self):
        """No es registro de una prueba: es un activo comprado que existe
        físicamente. El día después del corte las 24 llantas siguen atornilladas
        a los camiones y su código está marcado en el caucho — borrarlas obliga a
        recorrer el patio leyendo flancos, y la segunda vez nadie la hace."""
        from scripts.reset_transaccional import OPERATIVAS, PROTEGIDAS_MAESTRAS

        assert 'flota_llanta' in PROTEGIDAS_MAESTRAS
        assert 'flota_llanta' not in OPERATIVAS

    def test_el_montaje_se_borra_ANTES_que_el_gasto_y_las_lecturas(self):
        """Orden por FK: `flota_montaje_llanta` apunta a `flota_gasto` y dos
        veces a `flota_lectura_odometro`. Al revés, el DELETE del padre falla por
        clave foránea, el `except` del bucle lo imprime como un aviso más y el
        corte termina a medias — la misma forma que ya costó tres veces."""
        from scripts.reset_transaccional import OPERATIVAS

        i = OPERATIVAS.index('flota_montaje_llanta')
        assert i < OPERATIVAS.index('flota_gasto')
        assert i < OPERATIVAS.index('flota_lectura_odometro')

    def test_el_guard_del_reset_sigue_verde_con_las_dos_clasificadas(self):
        """Deny-by-default: si alguien mueve `flota_llanta` a OPERATIVAS, el
        script se niega a correr."""
        from scripts.reset_transaccional import _guard

        assert _guard() is True

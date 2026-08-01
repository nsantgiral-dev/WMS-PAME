"""
Los invariantes contra la BASE, con `INSERT` crudo.

`test_propiedades_t1.py` prueba las políticas del dominio. Este archivo prueba
que **no exista camino que las esquive**: si un `INSERT` desde psql, desde una
migración o desde el script de alguien puede violar el invariante, el modelo
está incompleto y el dominio es una sugerencia.

Por eso acá se escribe SQL crudo y no ORM: el ORM pasa por el dominio, y probar
el dominio contra el dominio es exactamente el error que dejó pasar una fórmula
dimensionalmente imposible con 631 tests en verde.

Lo que la base garantiza y lo que no:

| # | Invariante | Mecanismo | Alcance |
|---|---|---|---|
| 1 | Monotonía + append-only | trigger | total |
| 2 | 0 o 1 custodia activa | índice único parcial | total |
| 3 | Cobertura temporal | trigger de no-solape | **parcial** |
| 4 | Arco exclusivo | CHECK | total |
| 5 | Paternidad de fotos | — | ninguno: es polimórfica |

El 3 es parcial por una razón que no es pereza: **un hueco de cobertura lo
produce una escritura que NO ocurre, y una restricción solo juzga escrituras que
sí ocurren.** No se puede constreñir una ausencia. La base impide lo que un
`INSERT` sí puede romper —solape y viaje en el tiempo— y el hueco se detecta.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from flota.adaptadores.modelos import LADO_LARGO_MINIMO_FOTO_DATO

_T0 = datetime(2026, 8, 1, 5, 0)
_RECHAZO = (IntegrityError, OperationalError)


def _ts(minutos=0):
    return (_T0 + timedelta(minutes=minutos)).isoformat(sep=' ')


@pytest.fixture
def semilla(db):
    """Un vehículo, un conductor, un usuario y un almacén reales."""
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='TGZ653', tipo='NHR', activo=True)
    alm = Almacen(codigo='CT-FLOTA', nombre='Sede prueba')
    usr = Usuario(email='flota_ct@test.com', nombre='Gestor', rol='admin', activo=True)
    usr.set_password('x')
    db.session.add_all([veh, alm, usr])
    db.session.flush()
    con = Conductor(nombre='Conductor CT', cedula='CT-9001', activo=True)
    db.session.add(con)
    db.session.commit()
    return {'vehiculo': veh.id, 'almacen': alm.id, 'usuario': usr.id, 'conductor': con.id}


def _insertar_lectura(db, s, km, minutos, origen='entrega', motivo='NULL'):
    motivo_sql = 'NULL' if motivo == 'NULL' else f"'{motivo}'"
    db.session.execute(text(
        f"INSERT INTO flota_lectura_odometro "
        f"(vehiculo_id, valor_km, ts, origen, autor_usuario_id, motivo_correccion) "
        f"VALUES ({s['vehiculo']}, {km}, '{_ts(minutos)}', '{origen}', "
        f"{s['usuario']}, {motivo_sql})"
    ))
    db.session.commit()


def _insertar_custodia(db, s, inicio, fin=None, tipo='conductor',
                       conductor='auto', sede='NULL', estado='resuelto'):
    cid = s['conductor'] if conductor == 'auto' else conductor
    fin_sql = 'NULL' if fin is None else f"'{_ts(fin)}'"
    db.session.execute(text(
        f"INSERT INTO flota_custodia (vehiculo_id, custodio_tipo, "
        f"custodio_conductor_id, custodio_sede_id, registrado_por_usuario_id, "
        f"inicio_ts, fin_ts, km_inicio, linea_base, custodio_estado) "
        f"VALUES ({s['vehiculo']}, '{tipo}', {cid}, {sede}, {s['usuario']}, "
        f"'{_ts(inicio)}', {fin_sql}, 100000, 0, '{estado}')"
    ))
    db.session.commit()


# ══════════════════════════════════════════════════════════════════════════
# INVARIANTE 1 — monotonía y append-only, por trigger
# ══════════════════════════════════════════════════════════════════════════

class TestInvariante1EnLaBase:

    def test_un_insert_crudo_no_puede_bajar_el_odometro(self, db, semilla):
        _insertar_lectura(db, semilla, 100_000, 0)
        with pytest.raises(_RECHAZO):
            _insertar_lectura(db, semilla, 99_500, 60)
        db.session.rollback()

    def test_una_correccion_con_motivo_si_puede(self, db, semilla):
        _insertar_lectura(db, semilla, 100_000, 0)
        _insertar_lectura(db, semilla, 99_500, 60,
                          origen='correccion', motivo='digitacion')

    def test_una_correccion_sin_motivo_la_rechaza_el_CHECK(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_lectura(db, semilla, 99_500, 60, origen='correccion')
        db.session.rollback()

    def test_una_correccion_con_motivo_en_blanco_tambien(self, db, semilla):
        """`'   '` no es un motivo. El CHECK usa trim, no solo NOT NULL."""
        with pytest.raises(_RECHAZO):
            _insertar_lectura(db, semilla, 99_500, 60,
                              origen='correccion', motivo='   ')
        db.session.rollback()

    def test_no_se_puede_editar_una_lectura(self, db, semilla):
        """Append-only de verdad: el UPDATE lo bloquea la base, no la costumbre."""
        _insertar_lectura(db, semilla, 100_000, 0)
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                'UPDATE flota_lectura_odometro SET valor_km = 1'))
            db.session.commit()
        db.session.rollback()

    def test_el_bloqueo_de_DELETE_existe_en_postgres_aunque_no_corra_aqui(self):
        """Asimetría declarada, verificada — no olvidada.

        El trigger de DELETE va solo en PostgreSQL. En SQLite rompería el
        teardown de `conftest`, que limpia con `DELETE FROM` sobre toda la
        metadata y tiene un `except: rollback()` que al fallar una tabla
        descarta la limpieza de las anteriores: 24 tests ajenos a flota
        empezaron a fallar por datos que sobrevivían entre tests.

        Este test no ejerce el trigger —no corre acá— pero impide que
        desaparezca del DDL de producción sin que nadie lo note.
        """
        from flota.adaptadores import modelos

        assert 'flota_odometro_no_delete' in modelos._PG_DDL
        assert 'BEFORE DELETE ON flota_lectura_odometro' in modelos._PG_DDL
        assert 'no_delete' not in modelos._SQLITE_DDL

    def test_un_origen_inventado_no_entra(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_lectura(db, semilla, 100_000, 0, origen='lo_que_sea')
        db.session.rollback()


# ══════════════════════════════════════════════════════════════════════════
# INVARIANTE 2 — 0 o 1 custodia activa, por índice único parcial
# ══════════════════════════════════════════════════════════════════════════

class TestInvariante2EnLaBase:

    def test_dos_custodias_abiertas_colisionan(self, db, semilla):
        _insertar_custodia(db, semilla, inicio=0)
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=120)
        db.session.rollback()

    def test_cerrada_mas_abierta_conviven(self, db, semilla):
        """El índice es PARCIAL: solo indexa las abiertas."""
        _insertar_custodia(db, semilla, inicio=0, fin=120)
        _insertar_custodia(db, semilla, inicio=120)

    def test_muchas_cerradas_conviven(self, db, semilla):
        _insertar_custodia(db, semilla, inicio=0, fin=120)
        _insertar_custodia(db, semilla, inicio=120, fin=240)
        _insertar_custodia(db, semilla, inicio=240, fin=360)


# ══════════════════════════════════════════════════════════════════════════
# INVARIANTE 3 — cobertura temporal: garantía PARCIAL, y se dice cuál
# ══════════════════════════════════════════════════════════════════════════

class TestInvariante3EnLaBase:

    def test_una_custodia_no_puede_solaparse_con_la_anterior(self, db, semilla):
        _insertar_custodia(db, semilla, inicio=0, fin=120)
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=60)   # empieza antes de que cierre
        db.session.rollback()

    def test_una_custodia_no_puede_nacer_antes_que_una_existente(self, db, semilla):
        """Sin esto se puede insertar historia hacia atrás y fabricar coartadas."""
        _insertar_custodia(db, semilla, inicio=120, fin=240)
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, fin=60)
        db.session.rollback()

    def test_el_hueco_SI_se_puede_crear_y_por_eso_se_detecta(self, db, semilla):
        """La frontera honesta de la garantía.

        Cerrar a los 120 y abrir a los 180 deja una hora sin custodio, y la base
        lo ACEPTA: no hay escritura ilegal — la ilegalidad es la escritura que
        falta. Por eso el invariante 3 se detecta con
        `huecos_de_cobertura` y se cuenta en el health, en vez de fingir que un
        constraint lo cubre.
        """
        _insertar_custodia(db, semilla, inicio=0, fin=120)
        _insertar_custodia(db, semilla, inicio=180, fin=240)   # entra sin error

        from flota.dominio.custodia import huecos_de_cobertura
        from flota.dominio.valores import Custodia as CustodiaDom
        from flota.dominio.valores import CustodioTipo

        def _dom(ini, fin):
            return CustodiaDom(
                vehiculo_id=1, custodio_tipo=CustodioTipo.CONDUCTOR,
                inicio_ts=_T0 + timedelta(minutes=ini),
                fin_ts=_T0 + timedelta(minutes=fin),
                registrado_por_usuario_id=1, km_inicio=100_000,
                custodio_conductor_id=1,
            )

        huecos = huecos_de_cobertura([_dom(0, 120), _dom(180, 240)],
                                     ahora=_T0 + timedelta(minutes=240))
        assert len(huecos) == 1, 'el detector tiene que ver lo que la base deja pasar'


# ══════════════════════════════════════════════════════════════════════════
# INVARIANTE 4 — arco exclusivo, por CHECK
# ══════════════════════════════════════════════════════════════════════════

class TestInvariante4EnLaBase:

    def test_los_dos_custodios_llenos_no_entran(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, sede=semilla['almacen'])
        db.session.rollback()

    def test_ningun_custodio_no_entra(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, conductor='NULL')
        db.session.rollback()

    def test_el_tipo_tiene_que_corresponder_al_lleno(self, db, semilla):
        """`tipo=sede` con el conductor lleno: un acta que no dice de quién es."""
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, tipo='sede')
        db.session.rollback()

    def test_pendiente_sede_entra_sin_ningun_custodio(self, db, semilla):
        """El hueco de `almacenes` se declara, no se tapa con una sede cualquiera."""
        _insertar_custodia(db, semilla, inicio=0, tipo='sede',
                           conductor='NULL', sede='NULL', estado='pendiente_sede')

    def test_pendiente_sede_con_custodio_puesto_no_entra(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, tipo='sede', conductor='NULL',
                               sede=semilla['almacen'], estado='pendiente_sede')
        db.session.rollback()

    def test_pendiente_sede_de_tipo_conductor_no_entra(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, tipo='conductor',
                               conductor='NULL', estado='pendiente_sede')
        db.session.rollback()

    def test_el_estado_nuevo_no_afloja_el_invariante_para_las_filas_normales(self, db, semilla):
        """Con `resuelto`, sigue siendo obligatorio exactamente un custodio."""
        with pytest.raises(_RECHAZO):
            _insertar_custodia(db, semilla, inicio=0, conductor='NULL', estado='resuelto')
        db.session.rollback()

    def test_sede_con_su_id_si_entra(self, db, semilla):
        _insertar_custodia(db, semilla, inicio=0, tipo='sede',
                           conductor='NULL', sede=semilla['almacen'])


# ══════════════════════════════════════════════════════════════════════════
# FOTOS — regla 7 impuesta por la base
# ══════════════════════════════════════════════════════════════════════════

def _insertar_foto(db, s, clase='foto_dato', ancho=1600, alto=1200,
                   storage_ref='s3://flota/a.jpg', estado='ok'):
    db.session.execute(text(
        f"INSERT INTO flota_foto (clase, entidad_tipo, entidad_id, storage_ref, "
        f"hash_sha256, bytes, ancho, alto, mime, ts_captura, autor_usuario_id, "
        f"simulado, estado) VALUES ('{clase}', 'odometro', 1, '{storage_ref}', "
        f"'{'0' * 64}', 1000, {ancho}, {alto}, 'image/jpeg', '{_ts()}', "
        f"{s['usuario']}, 0, '{estado}')"
    ))
    db.session.commit()


class TestFotosEnLaBase:

    def test_no_se_puede_guardar_un_binario_en_storage_ref(self, db, semilla):
        """Regla 7 en la base. `recaudo_entrega.foto_entrega` empezó así."""
        with pytest.raises(_RECHAZO):
            _insertar_foto(db, semilla, storage_ref='data:image/jpeg;base64,/9j/4AAQ')
        db.session.rollback()

    def test_una_foto_dato_por_debajo_del_minimo_no_entra_como_ok(self, db, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_foto(db, semilla, ancho=800, alto=600)
        db.session.rollback()

    def test_pero_si_entra_declarada_pendiente_evidencia(self, db, semilla):
        """Si la compresión falla, se guarda declarada rota. Nunca `pass`."""
        _insertar_foto(db, semilla, ancho=800, alto=600, estado='pendiente_evidencia')

    def test_una_evidencia_de_estado_a_800x600_es_valida(self, db, semilla):
        _insertar_foto(db, semilla, clase='evidencia_estado', ancho=800, alto=600)

    def test_el_minimo_declarado_es_el_que_usa_la_base(self, db, semilla):
        """El número del CHECK y el del dominio no pueden divergir."""
        from flota.dominio.fotos import LADO_LARGO_MINIMO_FOTO_DATO as EN_DOMINIO
        assert EN_DOMINIO == LADO_LARGO_MINIMO_FOTO_DATO == 1600


# ══════════════════════════════════════════════════════════════════════════
# FICHA — procedencia obligatoria del dato con autoridad
# ══════════════════════════════════════════════════════════════════════════

class TestFichaEnLaBase:

    def _insertar_ficha(self, db, s, distribucion='sin_dato', fuente='sin_dato'):
        db.session.execute(text(
            f"INSERT INTO flota_ficha_tecnica (vehiculo_id, combustible, "
            f"sistema_frenos, tiene_freno_escape, distribucion, aceite_motor_spec, "
            f"posiciones_llanta, tiene_furgon, km_inicial, km_inicial_ts, "
            f"distribucion_fuente, frenos_fuente) "
            f"VALUES ({s['vehiculo']}, 'diesel', 'sin_dato', 'sin_dato', "
            f"'{distribucion}', '15W40 CI-4', 4, 0, 100000, '{_ts()}', "
            f"'{fuente}', 'sin_dato')"
        ))
        db.session.commit()

    def test_una_distribucion_conocida_sin_fuente_no_entra(self, db, semilla):
        """Un dato con autoridad lleva de dónde salió, o no vale.

        `distribucion` dispara una tarea de seguridad: si dice 'correa' sin
        decir quién lo dijo, alguien va a programar un cambio de correa contra
        una suposición.
        """
        with pytest.raises(_RECHAZO):
            self._insertar_ficha(db, semilla, distribucion='correa', fuente='sin_dato')
        db.session.rollback()

    def test_con_fuente_si_entra(self, db, semilla):
        self._insertar_ficha(db, semilla, distribucion='correa', fuente='concesionario')

    def test_sin_dato_sin_fuente_es_coherente(self, db, semilla):
        """No saber la distribución Y no saber quién lo dijo no es contradictorio."""
        self._insertar_ficha(db, semilla)

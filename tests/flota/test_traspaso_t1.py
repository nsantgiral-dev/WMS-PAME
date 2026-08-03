"""
Traspaso atómico — §2. La única defensa real del invariante 3.

La base no puede impedir un hueco de cobertura: un hueco lo produce una
escritura que NO ocurre. Lo que sí se puede garantizar es que el cierre y la
apertura pasen en la misma transacción y con la misma marca de tiempo, y eso es
lo que estos tests ejercen — incluida la mitad que casi nunca se prueba: **qué
queda en la base cuando el traspaso falla a la mitad.**
"""
from datetime import datetime, timedelta

import pytest

from flota.adaptadores import traspaso
from flota.adaptadores.modelos import Custodia, Foto, LecturaOdometro
from flota.dominio.custodia import huecos_de_cobertura
from flota.dominio.errores import CustodiaInvalida, LecturaRechazada
from flota.dominio.valores import CustodioEstado, CustodioTipo, QuienPide

_T0 = datetime(2026, 8, 1, 5, 0)


@pytest.fixture
def mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='TRA001', tipo='NHR', activo=True)
    alm = Almacen(codigo='TRA-SEDE', nombre='Sede traspaso')
    usr = Usuario(email='traspaso@test.com', nombre='Jefe', rol='admin', activo=True)
    usr.set_password('x')
    db.session.add_all([veh, alm, usr])
    db.session.flush()
    c1 = Conductor(nombre='Turno A', cedula='TRA-1', activo=True)
    c2 = Conductor(nombre='Turno B', cedula='TRA-2', activo=True)
    db.session.add_all([c1, c2])
    db.session.commit()
    return {'db': db, 'veh': veh.id, 'alm': alm.id, 'usr': usr.id,
            'c1': c1.id, 'c2': c2.id}


def _traspasar(mundo, conductor, km, minutos, **kw):
    """Traspaso desde escritorio (admin de zona).

    Lleva `motivo_forzado` por defecto porque cerrar el turno de otro sin fotos
    de cierre ES un forzado desde el 2026-08-03 — y estos tests hacen justo eso.
    Los que prueban el camino normal pasan `fotos_fin`.
    """
    kw.setdefault('motivo_forzado', 'turno anterior sin cerrar')
    return traspaso.traspasar(
        vehiculo_id=mundo['veh'], km=km,
        registrado_por_usuario_id=mundo['usr'],
        custodio_tipo=CustodioTipo.CONDUCTOR,
        custodio_conductor_id=mundo[conductor],
        ts=_T0 + timedelta(minutes=minutos),
        **kw,
    )


# JPEG mínimo real: el almacén decodifica y escribe bytes de verdad.
_JPEG = ('data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJ'
         'CQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/'
         '2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy'
         'MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA'
         'AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKB'
         'kaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk'
         'ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG'
         'x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+iiigD//Z')


def _foto(i, clase='evidencia_estado'):
    return {'clase': clase, 'data_url': _JPEG,
            'ancho': 800, 'alto': 600, 'mime': 'image/jpeg'}


class TestSinHueco:

    def test_el_cierre_y_la_apertura_comparten_el_instante(self, mundo):
        """No es que el hueco sea corto: es que no existe."""
        _traspasar(mundo, 'c1', 100_000, 0)
        nueva = _traspasar(mundo, 'c2', 100_240, 480)

        anterior = Custodia.query.filter(Custodia.id != nueva.id).one()
        assert anterior.fin_ts == nueva.inicio_ts

    def test_el_detector_de_huecos_no_ve_nada(self, mundo):
        """La comprobación que importa: el dominio juzgando lo que el adaptador escribió."""
        _traspasar(mundo, 'c1', 100_000, 0)
        _traspasar(mundo, 'c2', 100_240, 480)
        _traspasar(mundo, 'c1', 100_500, 960)

        filas = Custodia.query.order_by(Custodia.inicio_ts).all()
        huecos = huecos_de_cobertura(
            [traspaso._a_dominio(f) for f in filas],
            ahora=_T0 + timedelta(minutes=1440),
        )
        assert huecos == []

    def test_el_kilometraje_es_continuo_entre_turnos(self, mundo):
        _traspasar(mundo, 'c1', 100_000, 0)
        nueva = _traspasar(mundo, 'c2', 100_240, 480)
        anterior = Custodia.query.filter(Custodia.id != nueva.id).one()
        assert anterior.km_fin == nueva.km_inicio == 100_240


class TestAtomicidad:
    """La mitad que casi nunca se prueba: qué queda cuando falla."""

    def test_un_custodio_invalido_no_deja_la_anterior_cerrada(self, mundo):
        """Si se rechaza el nuevo turno, el vehículo NO queda sin responsable."""
        _traspasar(mundo, 'c1', 100_000, 0)

        with pytest.raises(CustodiaInvalida):
            traspaso.traspasar(
                vehiculo_id=mundo['veh'], km=100_240,
                registrado_por_usuario_id=mundo['usr'],
                custodio_tipo=CustodioTipo.CONDUCTOR,
                custodio_conductor_id=None, custodio_sede_id=None,
                ts=_T0 + timedelta(minutes=480),
            )

        vigente = traspaso.custodia_activa(mundo['veh'])
        assert vigente is not None, 'el vehículo quedó sin custodio tras un traspaso fallido'
        assert vigente.custodio_conductor_id == mundo['c1']
        assert Custodia.query.count() == 1

    def test_un_odometro_que_decrece_aborta_todo(self, mundo):
        """Regla 3: el kilometraje entra por la misma puerta o no entra ninguno."""
        _traspasar(mundo, 'c1', 100_000, 0)

        with pytest.raises(LecturaRechazada):
            _traspasar(mundo, 'c2', 99_000, 480)

        assert Custodia.query.count() == 1
        assert traspaso.custodia_activa(mundo['veh']).fin_ts is None
        assert LecturaOdometro.query.count() == 1

    def test_el_cliente_no_puede_falsear_la_referencia_de_la_foto(self, mundo):
        """`storage_ref` y `hash_sha256` los pone el SERVIDOR, no el request.

        Hasta el 2026-08-03 el frontend mandaba la referencia y el hash, y
        mandaba `'inline://pendiente-subida'` con ceros. Ahora manda la imagen y
        el servidor decide: o la guarda y pone la ruta y el hash reales, o la
        marca `pendiente_evidencia`. Un cliente no puede afirmar que una foto
        existe.
        """
        nueva = _traspasar(mundo, 'c1', 100_000, 0, fotos_inicio=[
            _foto(1) | {'storage_ref': 'data:image/jpeg;base64,MENTIRA',
                        'hash_sha256': 'f' * 64}])
        f = Foto.query.filter_by(entidad_id=nueva.id).one()
        assert not f.storage_ref.startswith('data:')
        assert f.hash_sha256 != 'f' * 64

    def test_una_foto_que_no_se_pudo_guardar_queda_declarada_y_no_tumba_el_turno(
            self, mundo, monkeypatch):
        """El conductor está en el patio a las 5 a.m.: el turno se registra.

        Bloquear el traspaso porque el almacén falló deja el camión adentro. Lo
        que NO se hace es callarlo — la foto queda `pendiente_evidencia`, el
        health la cuenta, y alguien puede ir a buscar la que no quedó.
        """
        monkeypatch.delenv('FLOTA_FOTOS_DIR', raising=False)
        nueva = _traspasar(mundo, 'c1', 100_000, 0, fotos_inicio=[_foto(1)])
        f = Foto.query.filter_by(entidad_id=nueva.id).one()
        assert f.estado == 'pendiente_evidencia'
        assert f.hash_sha256 == ''
        assert 'FLOTA_FOTOS_DIR' in f.storage_ref     # dice POR QUÉ no se guardó


class TestArranqueEnFrio:

    def test_la_primera_custodia_es_linea_base(self, mundo):
        """Sus daños nacen preexistentes: nadie paga por lo que no sabemos cuándo apareció."""
        assert _traspasar(mundo, 'c1', 100_000, 0).linea_base is True

    def test_la_segunda_ya_no(self, mundo):
        _traspasar(mundo, 'c1', 100_000, 0)
        assert _traspasar(mundo, 'c2', 100_240, 480).linea_base is False


class TestNoBloqueaLaOperacion:

    def test_menos_de_ocho_fotos_abre_igual_y_el_health_lo_cuenta(self, mundo):
        """Bloquear acá deja camiones en el patio y la operación desmonta el sistema."""
        from flota.adaptadores.medicion import MedidorSQL

        nueva = _traspasar(mundo, 'c1', 100_000, 0,
                           fotos_inicio=[_foto(i) for i in range(3)])
        assert nueva.id is not None
        assert MedidorSQL().custodias_sin_foto_completa() >= 1

    def test_las_fotos_quedan_atadas_a_su_custodia(self, mundo):
        """Regla 7: un archivo sin padre es un bug."""
        nueva = _traspasar(mundo, 'c1', 100_000, 0,
                           fotos_inicio=[_foto(i) for i in range(8)])
        assert Foto.query.filter_by(
            entidad_tipo='custodia_inicio', entidad_id=nueva.id).count() == 8


class TestSedeSinFila:

    def test_se_puede_entregar_a_una_sede_que_el_wms_no_representa(self, mundo):
        """Pitalito Terminal no tiene fila en `almacenes`. El turno cierra igual."""
        _traspasar(mundo, 'c1', 100_000, 0)
        nueva = traspaso.traspasar(
            vehiculo_id=mundo['veh'], km=100_240,
            registrado_por_usuario_id=mundo['usr'],
            custodio_tipo=CustodioTipo.SEDE,
            custodio_estado=CustodioEstado.PENDIENTE_SEDE,
            ts=_T0 + timedelta(minutes=480),
            motivo_forzado='el conductor entregó en Pitalito y se fue',
        )
        assert nueva.custodio_estado == 'pendiente_sede'
        assert nueva.custodio_sede_id is None
        assert traspaso.custodia_activa(mundo['veh']).id == nueva.id


class TestQuienPuedeRecibir:
    """La regla que convierte una restricción de base en una conversación.

    Un conductor no puede quitarle el turno a otro: eso lo arreglan ellos dos.
    Un admin de zona sí, porque es la única salida cuando alguien se fue sin
    cerrar y el camión tiene que salir a las 5 a.m.
    """

    def _recibir(self, mundo, conductor, quien, **kw):
        return traspaso.traspasar(
            vehiculo_id=mundo['veh'], km=100_500,
            registrado_por_usuario_id=mundo['usr'],
            custodio_tipo=CustodioTipo.CONDUCTOR,
            custodio_conductor_id=mundo[conductor],
            quien_pide=quien,
            ts=_T0 + timedelta(minutes=900),
            **kw,
        )

    def test_un_conductor_no_puede_quitarle_el_turno_a_otro(self, mundo):
        _traspasar(mundo, 'c1', 100_000, 0)
        with pytest.raises(CustodiaInvalida) as e:
            self._recibir(mundo, 'c2', QuienPide.CONDUCTOR)
        # El mensaje nombra a la persona y dice qué tiene que pasar.
        assert 'Turno A' in str(e.value)
        assert 'cerrar su turno primero' in str(e.value)

    def test_y_el_turno_del_otro_queda_intacto(self, mundo):
        """Un rechazo no puede dejar a medias lo que rechazó."""
        _traspasar(mundo, 'c1', 100_000, 0)
        with pytest.raises(CustodiaInvalida):
            self._recibir(mundo, 'c2', QuienPide.CONDUCTOR)
        vigente = traspaso.custodia_activa(mundo['veh'])
        assert vigente.custodio_conductor_id == mundo['c1']
        assert vigente.fin_ts is None

    def test_recibir_lo_que_ya_se_tiene_no_es_conflicto(self, mundo):
        """Un no-op, no una colisión: el vehículo ya es suyo."""
        _traspasar(mundo, 'c1', 100_000, 0)
        self._recibir(mundo, 'c1', QuienPide.CONDUCTOR)
        assert traspaso.custodia_activa(mundo['veh']).custodio_conductor_id == mundo['c1']

    def test_el_admin_si_puede_pero_exige_motivo_escrito(self, mundo):
        _traspasar(mundo, 'c1', 100_000, 0)
        with pytest.raises(CustodiaInvalida, match='motivo escrito'):
            self._recibir(mundo, 'c2', QuienPide.ADMIN_ZONA)

    def test_con_motivo_pasa_y_queda_marcado_el_forzado(self, mundo):
        _traspasar(mundo, 'c1', 100_000, 0)
        self._recibir(mundo, 'c2', QuienPide.ADMIN_ZONA,
                      motivo_forzado='Turno A se fue sin cerrar, el camión sale a las 6')
        anterior = Custodia.query.filter(Custodia.fin_ts.isnot(None)).order_by(
            Custodia.id.desc()).first()
        assert anterior.cierre_forzado is True
        assert anterior.cierre_forzado_por_usuario_id == mundo['usr']
        assert 'sin cerrar' in anterior.cierre_forzado_motivo

    def test_cerrar_CON_fotos_no_es_forzado(self, mundo):
        """Forzado no es "cerrar el de otro": es cerrarlo sin nada con qué
        comparar el turno siguiente. Con las fotos, es un cierre normal."""
        _traspasar(mundo, 'c1', 100_000, 0)
        self._recibir(mundo, 'c2', QuienPide.ADMIN_ZONA,
                      fotos_fin=[_foto(i) for i in range(8)])
        anterior = Custodia.query.filter(Custodia.fin_ts.isnot(None)).order_by(
            Custodia.id.desc()).first()
        assert anterior.cierre_forzado is False

    def test_el_health_cuenta_los_forzados(self, mundo):
        """Si el número sube, el problema no es el sistema: es que los
        conductores no están cerrando turno."""
        from flota.adaptadores.medicion import MedidorSQL

        medidor = MedidorSQL()
        antes = medidor.custodias_cerradas_forzadas()
        _traspasar(mundo, 'c1', 100_000, 0)
        self._recibir(mundo, 'c2', QuienPide.ADMIN_ZONA, motivo_forzado='sin cerrar')
        assert medidor.custodias_cerradas_forzadas() == antes + 1

    def test_un_forzado_sin_autor_no_entra_ni_por_SQL_crudo(self, mundo):
        """La base también lo impone: un cierre anónimo dejaría el rastro de
        que pasó algo raro y ninguna forma de saber quién ni por qué."""
        from sqlalchemy import text

        c = _traspasar(mundo, 'c1', 100_000, 0)
        with pytest.raises(Exception):
            mundo['db'].session.execute(text(
                'UPDATE flota_custodia SET cierre_forzado = 1 WHERE id = :i'), {'i': c.id})
            mundo['db'].session.commit()
        mundo['db'].session.rollback()

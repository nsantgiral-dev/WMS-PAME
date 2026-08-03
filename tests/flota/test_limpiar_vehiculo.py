"""
El script que borra. Lo único de este repo que puede destruir algo con un typo.

Se prueba porque **está a punto de correrse contra producción**, y porque un
script destructivo sin cobertura es el mismo patrón que llevamos toda la semana
persiguiendo: código escrito, desplegado, y nunca ejercitado hasta el día que
importa.

Lo que se verifica no es que borre — es que **borre solo lo que dijo**:

  · Un vehículo, no los demás.
  · Fotos, custodias y lecturas; nunca la ficha ni los documentos.
  · Que el trigger append-only quede **restaurado** al final. Si quedara
    apagado, `flota_lectura_odometro` perdería su invariante para siempre y
    nadie se enteraría hasta el próximo DELETE accidental.
"""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parents[2] / 'scripts' / 'flota_limpiar_vehiculo.py'


def _mod():
    spec = importlib.util.spec_from_file_location('flota_limpiar_vehiculo', _RUTA)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def dos_vehiculos(db):
    """El objetivo y un testigo. El testigo es la mitad de la prueba."""
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import (Custodia, FichaTecnica,
                                           LecturaOdometro)

    objetivo = Vehiculo(placa='LIM100', tipo='Turbo', activo=True)
    testigo = Vehiculo(placa='LIM200', tipo='Van', activo=True)
    db.session.add_all([objetivo, testigo])
    db.session.flush()
    con = Conductor(nombre='Conductor LIM', cedula='LIM-1', activo=True)
    db.session.add(con)
    db.session.flush()

    ahora = datetime(2026, 8, 3, 12, 0)
    for v, km in ((objetivo, 1000), (testigo, 2000)):
        db.session.add(FichaTecnica(
            vehiculo_id=v.id, posiciones_llanta=6, km_inicial=km,
            km_inicial_ts=ahora))
        db.session.add(LecturaOdometro(
            vehiculo_id=v.id, valor_km=km, ts=ahora, origen='entrega',
            autor_usuario_id=1))
        db.session.add(Custodia(
            vehiculo_id=v.id, custodio_tipo='conductor',
            custodio_conductor_id=con.id, custodio_estado='resuelto',
            registrado_por_usuario_id=1, inicio_ts=ahora, km_inicio=km))
    db.session.commit()
    return {'objetivo': objetivo.id, 'testigo': testigo.id}


class TestContar:

    def test_encuentra_lo_que_hay(self, db, dos_vehiculos):
        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        assert vid == dos_vehiculos['objetivo']
        assert len(obj['custodias']) == 1
        assert len(obj['lecturas']) == 1

    def test_no_cuenta_lo_del_otro_vehiculo(self, db, dos_vehiculos):
        m = _mod()
        _, obj = m.contar(db, 'LIM100')
        _, otro = m.contar(db, 'LIM200')
        assert set(obj['custodias']).isdisjoint(otro['custodias'])
        assert set(obj['lecturas']).isdisjoint(otro['lecturas'])

    def test_la_placa_no_distingue_mayusculas_ni_espacios(self, db, dos_vehiculos):
        m = _mod()
        vid, _ = m.contar(db, '  lim100 ')
        assert vid == dos_vehiculos['objetivo']

    def test_una_placa_que_no_existe_levanta(self, db, dos_vehiculos):
        """No devuelve `None` para que el llamador improvise un id."""
        m = _mod()
        with pytest.raises(m.VehiculoNoExiste):
            m.contar(db, 'NOEXISTE')

    def test_contar_no_borra(self, db, dos_vehiculos):
        from flota.adaptadores.modelos import Custodia

        m = _mod()
        antes = Custodia.query.count()
        m.contar(db, 'LIM100')
        assert Custodia.query.count() == antes


class TestBorrar:

    def test_borra_el_objetivo(self, db, dos_vehiculos):
        from flota.adaptadores.modelos import Custodia, LecturaOdometro

        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        assert Custodia.query.filter_by(vehiculo_id=vid).count() == 0
        assert LecturaOdometro.query.filter_by(vehiculo_id=vid).count() == 0

    def test_NO_toca_al_testigo(self, db, dos_vehiculos):
        """La mitad de la prueba. Un borrado de más no se nota hasta que hace
        falta el dato que ya no está."""
        from flota.adaptadores.modelos import Custodia, LecturaOdometro

        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        t = dos_vehiculos['testigo']
        assert Custodia.query.filter_by(vehiculo_id=t).count() == 1
        assert LecturaOdometro.query.filter_by(vehiculo_id=t).count() == 1

    def test_la_ficha_tecnica_sobrevive(self, db, dos_vehiculos):
        """Media mañana de levantamiento de campo. La segunda vez nadie la hace."""
        from flota.adaptadores.modelos import FichaTecnica

        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        assert db.session.get(FichaTecnica, vid) is not None

    def test_el_vehiculo_sigue_existiendo(self, db, dos_vehiculos):
        """Se limpia su rastro de flota, no se da de baja el camión."""
        from app.models.vehiculo import Vehiculo

        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        assert db.session.get(Vehiculo, vid) is not None

    def test_verificar_confirma_que_quedo_en_cero(self, db, dos_vehiculos):
        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        assert m.verificar(db, vid) == {'custodias': 0, 'lecturas': 0}

    def test_borrar_nada_no_revienta(self, db, dos_vehiculos):
        """Correrlo dos veces tiene que ser inofensivo — se corre desde una
        consola, y la segunda vez es para confirmar que quedó limpio."""
        m = _mod()
        vid, obj = m.contar(db, 'LIM100')
        m.borrar(db, vid, obj)
        vid2, obj2 = m.contar(db, 'LIM100')
        m.borrar(db, vid2, obj2)
        assert m.verificar(db, vid) == {'custodias': 0, 'lecturas': 0}


class TestFotos:

    def test_borra_las_fotos_de_sus_custodias_y_no_las_ajenas(
            self, db, dos_vehiculos):
        from flota.adaptadores.modelos import Custodia, Foto

        m = _mod()
        ahora = datetime(2026, 8, 3, 12, 0)
        for v in ('objetivo', 'testigo'):
            c = Custodia.query.filter_by(vehiculo_id=dos_vehiculos[v]).first()
            db.session.add(Foto(
                clase='evidencia_estado', entidad_tipo='custodia_inicio',
                entidad_id=c.id, angulo='frontal', storage_ref=f'x/{v}.jpg',
                hash_sha256='a' * 64, bytes=10, ancho=800, alto=600,
                mime='image/jpeg', ts_captura=ahora, autor_usuario_id=1))
        db.session.commit()

        vid, obj = m.contar(db, 'LIM100')
        assert len(obj['fotos']) == 1
        m.borrar(db, vid, obj)
        quedan = {f.storage_ref for f in Foto.query.all()}
        assert quedan == {'x/testigo.jpg'}

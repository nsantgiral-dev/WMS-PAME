"""
Un conductor, un vehículo — ahora con respaldo en disco.

`uq_flota_custodia_activa` impone 0-o-1 custodia activa **por vehículo**.
`validar_un_vehiculo_por_conductor` impone 0-o-1 **por conductor**, y hasta el
2026-08-19 vivía **solo en Python**, comprobada con un `.all()` sin bloqueo.

## Las dos reglas fallaban distinto, y esa era la trampa

Dos aperturas simultáneas sobre el mismo vehículo chocan contra el índice: el
usuario ve un error feo y **el dato queda íntegro**. Dos aperturas simultáneas
del mismo conductor sobre vehículos distintos **no chocaban con nada** —
pasaban las dos, sin error.

Una revisión externa señaló la primera («el usuario recibe un 500 feo, agregá
`with_for_update()`») y no vio la segunda. Vale anotarlo porque la conclusión
se invierte: la que tiene el error feo es la que estaba protegida, y
`with_for_update()` **no arregla** la otra — bloquea filas existentes, no impide
que dos transacciones inserten filas nuevas que juntas violan la regla. Lo que
faltaba era el índice.

## Y no hacía falta una carrera para romperlo

El 2026-08-13 un conductor acumuló tres custodias abiertas —TGZ653, TGZ655,
UPQ606— y tumbó `/flota/conductor/mi-turno` con `MultipleResultsFound`. Su
pantalla quedó en blanco.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def piezas(db):
    """Un conductor, dos vehículos, un usuario que registra."""
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    u = Usuario.query.filter_by(email='cust@test.com').first()
    if not u:
        u = Usuario(email='cust@test.com', nombre='Jefe', rol='admin',
                    activo=True)
        u.set_password('test123')
        db.session.add(u)
        db.session.flush()

    c = Conductor(nombre='Conductor Uno', cedula='CC-CUST-1', activo=True)
    db.session.add(c)
    vs = []
    for placa in ('TGZ653', 'TGZ655'):
        v = Vehiculo.query.filter_by(placa=placa).first()
        if not v:
            v = Vehiculo(placa=placa, tipo='CAMION', activo=True)
            db.session.add(v)
        vs.append(v)
    db.session.commit()
    return {'usuario': u, 'conductor': c, 'vehiculos': vs}


def _custodia(db, vehiculo, conductor, usuario, fin=None, km=1000):
    from flota.adaptadores.modelos import Custodia
    # Si la custodia nace cerrada, su inicio va ANTES del fin: el CHECK
    # `fin_ts IS NULL OR fin_ts >= inicio_ts` lo exige, y con razón.
    inicio = (fin - timedelta(hours=8)) if fin else datetime.utcnow()
    c = Custodia(
        vehiculo_id=vehiculo.id,
        custodio_tipo='conductor',
        custodio_conductor_id=conductor.id,
        inicio_ts=inicio,
        fin_ts=fin,
        km_inicio=km,
        km_fin=km if fin else None,
        registrado_por_usuario_id=usuario.id,
    )
    db.session.add(c)
    return c


class TestElIndiceExiste:
    def test_esta_declarado_en_el_modelo(self):
        from flota.adaptadores.modelos import Custodia
        idx = {i.name: i for i in Custodia.__table__.indexes}
        assert 'uq_flota_custodia_conductor_activa' in idx, (
            'el invariante «un conductor, un vehículo» sigue viviendo solo en '
            'Python')
        i = idx['uq_flota_custodia_conductor_activa']
        assert i.unique
        assert [c.name for c in i.columns] == ['custodio_conductor_id']

    def test_es_parcial_sobre_las_abiertas(self):
        """Sin el `WHERE fin_ts IS NULL` el índice prohibiría que un conductor
        haya tenido dos custodias **cerradas** — o sea, trabajar dos días."""
        from flota.adaptadores.modelos import Custodia
        i = {x.name: x for x in Custodia.__table__.indexes}[
            'uq_flota_custodia_conductor_activa']
        donde = str(i.dialect_options['postgresql'].get('where', ''))
        assert 'fin_ts IS NULL' in donde


class TestLoQueElIndiceImpide:
    def test_dos_custodias_abiertas_del_mismo_conductor_chocan(self, db, piezas):
        """**El detector ciego.** Es el caso del 2026-08-13: recibir un
        segundo vehículo sin entregar el primero."""
        v1, v2 = piezas['vehiculos']
        _custodia(db, v1, piezas['conductor'], piezas['usuario'])
        db.session.commit()

        _custodia(db, v2, piezas['conductor'], piezas['usuario'])
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestLoQueElIndiceNoPuedeImpedir:
    """Un índice que prohíbe de más rompe la operación, y eso no se ve hasta
    que alguien no puede trabajar."""

    def test_el_mismo_conductor_puede_tener_muchas_CERRADAS(self, db, piezas):
        """Es su historial: un conductor lleva vehículos todos los días."""
        v1, v2 = piezas['vehiculos']
        ayer = datetime.utcnow() - timedelta(days=1)
        _custodia(db, v1, piezas['conductor'], piezas['usuario'], fin=ayer)
        _custodia(db, v2, piezas['conductor'], piezas['usuario'], fin=ayer)
        db.session.commit()   # no debe levantar

    def test_entregar_y_recibir_otro_funciona(self, db, piezas):
        """El flujo normal: cierra una, abre la siguiente."""
        v1, v2 = piezas['vehiculos']
        c1 = _custodia(db, v1, piezas['conductor'], piezas['usuario'])
        db.session.commit()
        c1.fin_ts = datetime.utcnow()
        db.session.commit()
        _custodia(db, v2, piezas['conductor'], piezas['usuario'])
        db.session.commit()

    def test_varias_custodias_de_SEDE_conviven(self, db, almacen, piezas):
        """`custodio_conductor_id` es NULL cuando el custodio es una sede, y
        los NULL no colisionan. Si colisionaran, **una sola sede del país
        podría tener un vehículo** — el índice habría roto la operación
        entera sin que ningún test de conductores lo notara."""
        from flota.adaptadores.modelos import Custodia
        sede = almacen
        v1, v2 = piezas['vehiculos']
        for v in (v1, v2):
            db.session.add(Custodia(
                vehiculo_id=v.id, custodio_tipo='sede',
                custodio_sede_id=sede.id, inicio_ts=datetime.utcnow(),
                km_inicio=1000,
                registrado_por_usuario_id=piezas['usuario'].id))
        db.session.commit()   # no debe levantar

    def test_dos_conductores_distintos_conviven(self, db, piezas):
        from app.models.conductor import Conductor
        otro = Conductor(nombre='Conductor Dos', cedula='CC-CUST-2',
                         activo=True)
        db.session.add(otro)
        db.session.commit()
        v1, v2 = piezas['vehiculos']
        _custodia(db, v1, piezas['conductor'], piezas['usuario'])
        _custodia(db, v2, otro, piezas['usuario'])
        db.session.commit()


class TestLaValidacionDeDominioSigueSiendoLaPuertaBuena:
    """El índice es la red, no el mensaje. Un `IntegrityError` en la cara del
    conductor no le dice qué hacer; `validar_un_vehiculo_por_conductor` sí — y
    por eso no se borra ahora que existe el índice."""

    def test_el_dominio_nombra_las_placas_y_dice_qué_hacer(self):
        from flota.dominio.custodia import (CustodiaInvalida,
                                            validar_un_vehiculo_por_conductor)
        with pytest.raises(CustodiaInvalida) as e:
            validar_un_vehiculo_por_conductor([('TGZ653', '13/08 a las 07:12')])
        msg = str(e.value)
        assert 'TGZ653' in msg
        assert 'Entregalo primero' in msg

    def test_sin_otras_abiertas_deja_pasar(self):
        from flota.dominio.custodia import validar_un_vehiculo_por_conductor
        validar_un_vehiculo_por_conductor([])

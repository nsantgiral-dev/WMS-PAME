"""Los cuatro momentos de validación de una avería, y quién puede cada uno.

El proceso, como lo describió el dueño: «si el jefe de bodega de los puntos de
venta genera la avería, el admin tiene que validar y dejar la evidencia, y en
NB1 recepción valida, y antes de ubicar también el admin o supervisor de NB1
tiene que validarlo».

Este archivo prueba los momentos 2 y 4 — los dos que no existían. El 1 (crear)
y el 3 (contar al recibir) ya los cubre la máquina de estados desde antes.
"""
from datetime import datetime

import pytest

from app.models.almacen import Almacen
from app.models.traslado import (ClaseTraslado, EstadoTraslado,
                                 SolicitudTraslado)
from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles
from app.services.traslado_service import (BODEGA_AVERIAS_DESTINO,
                                           _ROLES_DICTAMEN_AVERIA,
                                           TrasladoService)


@pytest.fixture
def cd(db):
    """El centro de distribución, con su bodega."""
    a = Almacen.query.filter_by(bodega_siesa_id=BODEGA_AVERIAS_DESTINO).first()
    if not a:
        a = Almacen(codigo='CD-TEST', nombre='Bodega CD',
                    bodega_siesa_id=BODEGA_AVERIAS_DESTINO)
        db.session.add(a)
        db.session.commit()
    return a


def _usuario(db, nombre, rol, bodega=None):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".")}@t.co'.lower(),
                rol=rol, bodega_siesa_id=bodega)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def producto_averiable(db, producto):
    """`aprobar_solicitud` exige `unidad_negocio_id` desde antes de este
    cambio — sin ella el payload a Siesa sale sin unidad de negocio. El
    fixture compartido no la trae; acá sí, porque estos tests recorren el
    flujo hasta ENTREGADA."""
    producto.unidad_negocio_id = '001'
    db.session.commit()
    return producto


@pytest.fixture
def jefe_punto(db):
    """El único usuario administrativo de un punto satélite. En producción
    (medido 2026-09-17) cada punto tiene exactamente uno, con rol `tienda`."""
    return _usuario(db, 'Tienda NC1', 'tienda', 'NC1')


@pytest.fixture
def supervisor_cd(db, cd):
    return _usuario(db, 'Supervisor CD', 'supervisor', BODEGA_AVERIAS_DESTINO)


def _items(producto, motivo='caja aplastada en el estante'):
    return [{'producto_id': producto.id, 'cantidad_solicitada': 4,
             'motivo_averia': motivo}]


def _crear_averia(jefe_punto, producto, **kw):
    return TrasladoService.crear_solicitud(
        solicitante_id=jefe_punto.id,
        bodega_destino=kw.pop('destino', BODEGA_AVERIAS_DESTINO),
        nombre_punto_venta='Neiva Centro',
        items=kw.pop('items', None) or _items(producto),
        bodega_origen=kw.pop('origen', 'NC1'),
        clase_traslado=ClaseTraslado.AVERIAS,
        **kw)


# ── El destino: la pregunta de «¿va directo a AV1?» ────────────────────────

class TestUnaAveriaNoVaDirectoAAV1:

    @pytest.mark.parametrize('destino', ['AV1', 'TRA1', 'NS1', 'PC1', ''])
    def test_el_unico_destino_es_el_cd(self, db, jefe_punto, producto, destino):
        """AV1 no tiene almacén WMS, ni quién reciba, ni CO propio, ni camino
        de vuelta si el veredicto dice que la mercancía estaba bien. El
        recorrido es punto → CD → AV1, en dos tramos."""
        with pytest.raises(ValueError, match='centro de distribución'):
            _crear_averia(jefe_punto, producto, destino=destino)

    def test_al_cd_si_entra(self, db, jefe_punto, producto):
        s = _crear_averia(jefe_punto, producto)
        assert s.bodega_destino_siesa == BODEGA_AVERIAS_DESTINO
        assert s.bodega_origen_siesa == 'NC1'
        assert s.es_averia() is True
        assert s.estado == EstadoTraslado.BORRADOR

    def test_origen_igual_a_destino_no_mueve_nada(self, db, jefe_punto, producto):
        with pytest.raises(ValueError, match='no mueve nada'):
            _crear_averia(jefe_punto, producto, origen=BODEGA_AVERIAS_DESTINO)


class TestCadaLineaDiceporQue:

    @pytest.mark.parametrize('motivo', [None, '', '   '])
    def test_sin_motivo_no_se_puede_declarar(self, db, jefe_punto, producto, motivo):
        """Tres personas van a decidir sobre esta línea sin haber estado ahí."""
        with pytest.raises(ValueError, match='motivo'):
            _crear_averia(jefe_punto, producto,
                          items=[{'producto_id': producto.id,
                                  'cantidad_solicitada': 4,
                                  'motivo_averia': motivo}])

    def test_el_motivo_queda_en_la_linea(self, db, jefe_punto, producto):
        s = _crear_averia(jefe_punto, producto, items=_items(producto, 'mojado'))
        assert s.items[0].motivo_averia == 'mojado'

    def test_un_traslado_normal_no_arrastra_motivo(self, db, jefe_punto, producto):
        """El campo es de averías. En un traslado normal sería un segundo
        `observaciones` por línea, sin nadie que lo lea."""
        s = TrasladoService.crear_solicitud(
            solicitante_id=jefe_punto.id, bodega_destino='NC1',
            nombre_punto_venta='Neiva Centro',
            items=[{'producto_id': producto.id, 'cantidad_solicitada': 2,
                    'motivo_averia': 'se coló'}])
        assert s.es_averia() is False
        assert s.items[0].motivo_averia is None

    def test_una_clase_desconocida_no_entra_por_el_servicio(self, db, jefe_punto,
                                                            producto):
        with pytest.raises(ValueError, match='desconocida'):
            TrasladoService.crear_solicitud(
                solicitante_id=jefe_punto.id, bodega_destino='NC1',
                nombre_punto_venta='x',
                items=[{'producto_id': producto.id, 'cantidad_solicitada': 1}],
                clase_traslado='ROTO')


# ── Momento 2: el administrador del punto valida y deja evidencia ──────────

class TestLaEvidenciaDelPunto:

    def test_sin_evidencia_no_se_aprueba(self, db, jefe_punto, producto):
        s = _crear_averia(jefe_punto, producto)
        TrasladoService.enviar_solicitud(s.id)
        with pytest.raises(ValueError, match='evidencia'):
            TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id)

    def test_evidencia_en_blanco_tampoco(self, db, jefe_punto, producto):
        s = _crear_averia(jefe_punto, producto)
        TrasladoService.enviar_solicitud(s.id)
        with pytest.raises(ValueError, match='evidencia'):
            TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id,
                                              averia_evidencia='   ')

    def test_con_evidencia_avanza_y_queda_firmada(self, db, jefe_punto,
                                                  producto_averiable):
        producto = producto_averiable
        s = _crear_averia(jefe_punto, producto)
        TrasladoService.enviar_solicitud(s.id)
        s = TrasladoService.aprobar_solicitud(
            s.id, aprobador_id=jefe_punto.id,
            averia_evidencia='conté 4, fotos en el grupo, empaque reventado')
        assert s.averia_evidencia.startswith('conté 4')
        assert s.aprobador_id == jefe_punto.id
        assert s.fecha_aprobacion is not None

    def test_el_mismo_usuario_puede_aprobar_lo_que_declaro(self, db, jefe_punto,
                                                           producto_averiable):
        producto = producto_averiable
        """**Medido, no asumido.** Cada punto satélite tiene UN usuario
        administrativo (NC1, NS1, PC1, FC1: uno cada uno, rol `tienda`).
        Un guard de «quien declara no valida» acá no separaría dos personas:
        volvería el proceso imposible en los cuatro puntos, y la salida real
        sería que alguien preste su usuario — peor, porque borra el rastro.

        Lo que sí se separa son dos actos. Y la separación de PERSONAS se
        exige donde sí hay personas distintas: el dictamen del CD."""
        s = _crear_averia(jefe_punto, producto)
        TrasladoService.enviar_solicitud(s.id)
        s = TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id,
                                              averia_evidencia='lo revisé yo')
        assert s.aprobador_id == s.solicitante_id

    def test_un_traslado_normal_no_admite_evidencia_de_averia(self, db, jefe_punto,
                                                              producto_averiable):
        producto = producto_averiable
        """El CHECK de Postgres lo rechazaría al commitear con un IntegrityError
        feo; el mensaje tiene que llegar a quien apretó el botón."""
        s = TrasladoService.crear_solicitud(
            solicitante_id=jefe_punto.id, bodega_destino='NC1',
            nombre_punto_venta='x',
            items=[{'producto_id': producto.id, 'cantidad_solicitada': 1}])
        TrasladoService.enviar_solicitud(s.id)
        with pytest.raises(ValueError, match='no es de averías'):
            TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id,
                                              averia_evidencia='algo')


# ── Momento 4: el dictamen del CD ──────────────────────────────────────────

@pytest.fixture
def entregada(db, jefe_punto, producto_averiable):
    producto = producto_averiable
    """Una avería que ya llegó y ya se contó — lista para dictamen."""
    s = _crear_averia(jefe_punto, producto)
    TrasladoService.enviar_solicitud(s.id)
    TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id,
                                      averia_evidencia='fotos del punto')
    s.estado = EstadoTraslado.ENTREGADA
    s.fecha_entrega = datetime.utcnow()
    db.session.commit()
    return s


class TestElDictamenDelCD:

    @pytest.mark.parametrize('confirmada', [True, False])
    def test_los_dos_veredictos_se_pueden_escribir(self, db, entregada,
                                                   supervisor_cd, confirmada):
        """`False` («no estaba averiada, vuelve a vendible») tiene que poder
        escribirse igual que `True`. Un guard que solo dejara confirmar
        obligaría a mentir para devolver la mercancía al pool."""
        s = TrasladoService.dictaminar_averia(
            entregada.id, supervisor_cd.id, confirmada=confirmada,
            nota='revisado en muelle')
        assert s.averia_veredicto is confirmada
        assert s.averia_veredicto_por == supervisor_cd.id
        assert s.averia_veredicto_at is not None
        assert s.averia_veredicto_nota == 'revisado en muelle'

    def test_none_no_es_un_veredicto(self, db, entregada, supervisor_cd):
        with pytest.raises(ValueError, match='sí o no'):
            TrasladoService.dictaminar_averia(entregada.id, supervisor_cd.id,
                                              confirmada=None)

    def test_sin_dictaminar_el_veredicto_es_none_y_no_false(self, db, entregada):
        """El tri-estado, en el único sitio donde se nota: «nadie miró» no es
        «no estaba averiada». La mercancía llegó, se contó, y está en el limbo."""
        assert entregada.averia_veredicto is None
        assert entregada.to_dict()['averia_veredicto'] is None

    def test_quien_declaro_no_dictamina(self, db, jefe_punto, producto_averiable,
                                        cd):
        producto = producto_averiable
        """Acá sí: en el CD hay personas distintas."""
        s = _crear_averia(jefe_punto, producto)
        TrasladoService.enviar_solicitud(s.id)
        TrasladoService.aprobar_solicitud(s.id, aprobador_id=jefe_punto.id,
                                          averia_evidencia='x')
        s.estado = EstadoTraslado.ENTREGADA
        # Le damos al declarante el rol y la bodega del CD: aun así no puede,
        # porque el guard es sobre la persona, no sobre el permiso.
        jefe_punto.rol = 'supervisor'
        jefe_punto.bodega_siesa_id = BODEGA_AVERIAS_DESTINO
        db.session.commit()
        with pytest.raises(ValueError, match='quien declaró'):
            TrasladoService.dictaminar_averia(s.id, jefe_punto.id, confirmada=True)

    @pytest.mark.parametrize('rol', ['tienda', 'operario', 'recepcionista',
                                     'picker_traslado', 'packer_traslado',
                                     'conductor', 'compras'])
    def test_los_roles_operativos_no_dictaminan(self, db, entregada, rol):
        u = _usuario(db, f'Op {rol}', rol, BODEGA_AVERIAS_DESTINO)
        with pytest.raises(ValueError, match='Dictaminar una avería'):
            TrasladoService.dictaminar_averia(entregada.id, u.id, confirmada=True)

    def test_recepcion_valida_la_cantidad_pero_no_dictamina(self, db, entregada):
        """El proceso los separa a propósito: recepción dice CUÁNTO llegó
        (momento 3, y ya es `cantidad_recibida`); el dictamen dice SI estaba
        averiada (momento 4). Son dos preguntas distintas."""
        rec = _usuario(db, 'Recepcionista CD', 'recepcionista',
                       BODEGA_AVERIAS_DESTINO)
        with pytest.raises(ValueError, match='Dictaminar una avería'):
            TrasladoService.dictaminar_averia(entregada.id, rec.id, confirmada=True)

    def test_un_supervisor_de_otra_bodega_no_dictamina(self, db, entregada):
        """La mercancía está en el CD. Quien dictamina tiene que poder verla."""
        u = _usuario(db, 'Supervisor NS1', 'supervisor', 'NS1')
        with pytest.raises(ValueError, match='no es de'):
            TrasladoService.dictaminar_averia(entregada.id, u.id, confirmada=True)

    def test_el_admin_sin_bodega_si_dictamina(self, db, entregada):
        """Excepción explícita y medida: los 9 usuarios sin bodega de
        producción incluyen al admin. El guard de alcance lo dejaría fuera de
        TODAS las bodegas en vez de dentro de una."""
        u = _usuario(db, 'Administrador', 'admin', None)
        s = TrasladoService.dictaminar_averia(entregada.id, u.id, confirmada=True)
        assert s.averia_veredicto is True

    @pytest.mark.parametrize('estado', [
        EstadoTraslado.BORRADOR, EstadoTraslado.ENVIADA,
        EstadoTraslado.EN_PICKING, EstadoTraslado.EN_PACKING,
        EstadoTraslado.PREPARADO, EstadoTraslado.EN_TRANSITO,
    ])
    def test_no_se_dictamina_antes_de_que_llegue(self, db, entregada,
                                                 supervisor_cd, estado):
        """Dictaminar antes de recibir sería opinar sobre mercancía que
        todavía no se contó."""
        entregada.estado = estado
        db.session.commit()
        with pytest.raises(ValueError, match='después de que recepción'):
            TrasladoService.dictaminar_averia(entregada.id, supervisor_cd.id,
                                              confirmada=True)

    def test_un_traslado_normal_no_se_dictamina(self, db, jefe_punto, producto,
                                                supervisor_cd):
        s = TrasladoService.crear_solicitud(
            solicitante_id=jefe_punto.id, bodega_destino='NC1',
            nombre_punto_venta='x',
            items=[{'producto_id': producto.id, 'cantidad_solicitada': 1}])
        s.estado = EstadoTraslado.ENTREGADA
        db.session.commit()
        with pytest.raises(ValueError, match='no es un traslado de averías'):
            TrasladoService.dictaminar_averia(s.id, supervisor_cd.id,
                                              confirmada=True)


class TestElVeredictoSeCongela:

    @pytest.mark.parametrize('primero,segundo', [(True, False), (False, True),
                                                 (True, True), (False, False)])
    def test_no_se_reescribe(self, db, entregada, supervisor_cd, primero, segundo):
        """De este veredicto cuelga un documento en Siesa. Reescribirlo dejaría
        el WMS diciendo una cosa y el ERP otra, sin nada que reconcilie."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor_cd.id,
                                          confirmada=primero)
        otro = _usuario(db, 'Jefe CD', 'jefe_almacen', BODEGA_AVERIAS_DESTINO)
        with pytest.raises(ValueError, match='ya fue dictaminado'):
            TrasladoService.dictaminar_averia(entregada.id, otro.id,
                                              confirmada=segundo)

    def test_un_false_congela_igual_que_un_true(self, db, entregada, supervisor_cd):
        """El bug que este test existe para cazar: `if s.averia_veredicto:`
        dejaría reescribir un `False` porque es falsy. El guard compara contra
        `is not None`."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor_cd.id,
                                          confirmada=False)
        db.session.refresh(entregada)
        assert entregada.averia_veredicto is False
        otro = _usuario(db, 'Jefe CD 2', 'jefe_almacen', BODEGA_AVERIAS_DESTINO)
        with pytest.raises(ValueError, match='ya fue dictaminado'):
            TrasladoService.dictaminar_averia(entregada.id, otro.id,
                                              confirmada=True)


# ── Trinquetes ─────────────────────────────────────────────────────────────

def test_los_roles_del_dictamen_coinciden_con_la_tupla_del_repo():
    """`_ROLES_DICTAMEN_AVERIA` nombra los roles a mano para no meter una
    dependencia de `app.routes` dentro de un servicio. El precio de esa copia
    es este trinquete: si `Roles.SUPERVISION` cambia y la tupla no, el
    documento dice una cosa y el sistema permite otra."""
    assert set(_ROLES_DICTAMEN_AVERIA) == set(Roles.SUPERVISION), (
        f'_ROLES_DICTAMEN_AVERIA={_ROLES_DICTAMEN_AVERIA} pero '
        f'Roles.SUPERVISION={Roles.SUPERVISION}')


def test_el_destino_de_averias_no_es_un_literal_suelto():
    """Si el CD cambia de código, cambia en un solo lugar."""
    from app.services.traslado_service import BODEGA_ORIGEN_DEFAULT
    assert BODEGA_AVERIAS_DESTINO == BODEGA_ORIGEN_DEFAULT

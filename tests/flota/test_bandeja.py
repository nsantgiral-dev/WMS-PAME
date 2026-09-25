"""`GET /flota/bandeja` contra un mundo armado — cada caso con su vehículo.

## Por qué un vehículo por caso

Un semáforo que sale rojo por cinco motivos a la vez no prueba que ninguno de
los cinco funcione: basta con que uno dispare. Cada placa de este mundo tiene
**un** motivo para su color, su pendiente o su señal, y la placa de al lado
tiene el caso sano o el caso sin dato. Así cada aserto distingue.

| Placa | Qué es |
|---|---|
| `VRD001` | todo en regla: papeles al día, ficha completa, turno con fotos, inspección apta → verde, cero pendientes |
| `ROJ001` | SOAT vencido; ruta de hoy EN LA CALLE con Beto y el turno a nombre de Carla; sin inspección; turno sin fotos de inicio; preventivo vencido |
| `AMB001` | SOAT por vencer; un daño mayor en plazo; sin ficha; km sin foto (en duda); un cierre forzado de anteayer; nunca tuvo ruta |
| `SIN001` | nada registrado: ni km, ni turno, ni papeles |
| `KMR001` | 120 km entre dos días sin ruta (señal); 5 km (tolerancia, no); un tramo con km en duda (no evaluable) |
| `RUT001` | seis recorridos de la ruta Norte de ~100 km y uno de 250 (señal); dos de la ruta Sur (n insuficiente) |
| `GAL001` | ocho tanqueos llenos en 70 días; la última ventana se llevó 16 galones donde se esperaban ~11 (señal) |
| `GAL002` | dos ventanas recientes: rendimiento no sostenible (no evaluable) |
| `PRC001` | un galón a $20.000 contra una flota a $15.000 (señal) |
| `SAL001` | ruta de hoy en la calle con el vehículo en custodia de la sede (señal) |

Las horas se arman en día operativo de Bogotá con `app.utils.fecha`, no con
`date.today()`: el build corre en UTC (regla 5 del WMS).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.utils.fecha import dia_operativo, inicio_del_dia_utc

_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


HOY = dia_operativo()


#: El texto ÚNICO de la política de salida (`flota/dominio/salida.py`), el mismo
#: que ven el despacho y el conductor.
SOAT_VENCIDO = (f'SOAT vencido desde {(HOY - timedelta(days=5)).strftime("%d/%m/%Y")}'
                ' (hace 5 días)')
SOAT_POR_VENCER = (f'SOAT vence el {(HOY + timedelta(days=10)).strftime("%d/%m/%Y")}'
                   ' (en 10 días)')


def T(dias_atras: int, hora: float) -> datetime:
    """Un instante UTC naive a la `hora` de Bogotá de hace `dias_atras` días."""
    return inicio_del_dia_utc(HOY - timedelta(days=dias_atras)) + timedelta(hours=hora)


def HOY_T(fraccion: float) -> datetime:
    """Un instante de HOY que ya pasó, en el orden que diga `fraccion`."""
    ini = inicio_del_dia_utc(HOY)
    return ini + (datetime.utcnow() - ini) * fraccion


@pytest.fixture
def mundo(db):
    from flask_jwt_extended import create_access_token

    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho
    from app.models.ruta_maestra import RutaMaestra
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import catalogo, hallazgos
    from flota.adaptadores import inspecciones as ad_insp
    from flota.adaptadores.gastos import registrar_tanqueo
    from flota.adaptadores.modelos import (Custodia, DocumentoVehiculo,
                                           EjecucionTarea, FichaTecnica, Foto,
                                           LecturaOdometro, PlanTarea)
    from flota.dominio.valores import angulos_de_custodia

    def usuario(email, rol):
        u = Usuario(email=email, nombre=email.split('@')[0].title(), rol=rol,
                    activo=True)
        u.set_password('x')
        db.session.add(u)
        return u

    admin = usuario('jefa@bandeja.test', 'admin')
    flota_u = usuario('yesid@bandeja.test', 'control_flota')
    cond_u = usuario('cond@bandeja.test', 'conductor')
    tienda_u = usuario('tienda@bandeja.test', 'tienda')
    sede = Almacen(codigo='BDJ-NB1', nombre='Bodega CD', activo=True)
    otra_sede = Almacen(codigo='BDJ-NC1', nombre='Neiva Centro', activo=True)
    db.session.add_all([sede, otra_sede])
    db.session.flush()

    conductores = {}
    for i, nombre in enumerate(('Ana', 'Beto', 'Carla', 'Dani', 'Eva'), 1):
        c = Conductor(nombre=nombre, cedula=f'BDJ-{i}', activo=True)
        db.session.add(c)
        conductores[nombre] = c
    placas = ('VRD001', 'ROJ001', 'AMB001', 'SIN001', 'KMR001', 'RUT001',
              'GAL001', 'GAL002', 'PRC001', 'SAL001')
    veh = {p: Vehiculo(placa=p, tipo='NHR', activo=True) for p in placas}
    baja = Vehiculo(placa='BAJ001', tipo='NHR', activo=False)
    db.session.add_all(list(veh.values()) + [baja])
    norte = RutaMaestra(nombre='Norte', tipo_ruta='Municipal')
    sur = RutaMaestra(nombre='Sur', tipo_ruta='Municipal')
    db.session.add_all([norte, sur])
    db.session.commit()
    A = admin.id

    def lectura(p, km, ts, foto=True):
        foto_id = None
        f = None
        if foto:
            f = Foto(clase='foto_dato', entidad_tipo='odometro', entidad_id=0,
                     storage_ref=f'test/{p}-{km}', hash_sha256='0' * 64,
                     bytes=100, ancho=1600, alto=1200, mime='image/jpeg',
                     ts_captura=ts, autor_usuario_id=A)
            db.session.add(f)
            db.session.flush()
            foto_id = f.id
        l = LecturaOdometro(vehiculo_id=veh[p].id, valor_km=km, ts=ts,
                            origen='cierre_dia', foto_id=foto_id,
                            autor_usuario_id=A)
        db.session.add(l)
        db.session.flush()
        if f is not None:
            f.entidad_id = l.id
        return l

    def custodia(p, inicio, km, conductor=None, sede_id=None, fin=None,
                 km_fin=None, forzado=None, fotos=0, ubicacion=None):
        c = Custodia(
            vehiculo_id=veh[p].id,
            custodio_tipo='conductor' if conductor else 'sede',
            custodio_conductor_id=conductores[conductor].id if conductor else None,
            custodio_sede_id=sede_id, registrado_por_usuario_id=A,
            inicio_ts=inicio, fin_ts=fin, km_inicio=km, km_fin=km_fin,
            ubicacion=ubicacion,
            cierre_forzado=bool(forzado),
            cierre_forzado_por_usuario_id=A if forzado else None,
            cierre_forzado_motivo=forzado)
        db.session.add(c)
        db.session.flush()
        for i in range(fotos):
            db.session.add(Foto(
                clase='evidencia_estado', entidad_tipo='custodia_inicio',
                entidad_id=c.id, storage_ref=f'test/cus{c.id}-{i}',
                hash_sha256='1' * 64, bytes=100, ancho=800, alto=600,
                mime='image/jpeg', ts_captura=inicio, autor_usuario_id=A))
        return c

    def papel(p, tipo, vence, estado='vigente'):
        db.session.add(DocumentoVehiculo(
            vehiculo_id=veh[p].id, tipo=tipo,
            numero='' if estado != 'vigente' else f'{tipo}-{p}',
            entidad='' if estado != 'vigente' else 'Aseguradora',
            fecha_expedicion=None if estado != 'vigente' else HOY - timedelta(days=300),
            fecha_vencimiento=vence, estado=estado))

    def papeles_al_dia(p):
        papel(p, 'soat', HOY + timedelta(days=200))
        papel(p, 'rtm', HOY + timedelta(days=200))
        papel(p, 'poliza_rc', HOY + timedelta(days=200))
        papel(p, 'tarjeta_propiedad', None)

    def ficha_completa(p, km):
        db.session.add(FichaTecnica(
            vehiculo_id=veh[p].id, combustible='diesel',
            sistema_frenos='hidraulico', frenos_fuente='manual_fabricante',
            tiene_freno_escape='no', distribucion='correa',
            distribucion_fuente='manual_fabricante', transmision_final='cardan',
            posiciones_llanta=4, km_inicial=km, km_inicial_ts=T(60, 6),
            # Completa exige la capacidad del tanque (2026-09-24): sin ella el
            # detector de sobre-tanqueo está ciego.
            capacidad_tanque_galones=Decimal('40'),
            capacidad_tanque_fuente='manual_fabricante'))

    def ruta(p, dias_atras, conductor, maestra=None, estado='ENTREGADA'):
        r = RutaDespacho(
            conductor_id=conductores[conductor].id,
            vehiculo_id=veh[p].id if p else None,
            ruta_maestra_id=maestra.id if maestra else None,
            tipo_ruta='Municipal',
            fecha_programada=HOY - timedelta(days=dias_atras),
            estado=estado)
        db.session.add(r)
        return r

    exigidas = len(angulos_de_custodia(4))

    # ── VRD001 — todo en regla ───────────────────────────────────────────
    ficha_completa('VRD001', 9000)
    papeles_al_dia('VRD001')
    lectura('VRD001', 10_000, T(1, 6))
    custodia('VRD001', T(1, 6), 10_000, conductor='Ana', fotos=exigidas)
    db.session.commit()
    catalogo.sembrar(db)
    items = ad_insp.items_del_dia_de(veh['VRD001'], HOY)
    ad_insp.registrar(
        vehiculo_id=veh['VRD001'].id, tipo_vehiculo_obj=veh['VRD001'],
        km=10_000, inspeccionada_por_usuario_id=A,
        respuestas=[{'item_id': i.id, 'respuesta': 'optimo'} for i in items],
        segundos_llenado=120, ts=HOY_T(0.3))

    # ── ROJ001 — papel vencido, préstamo sin traspaso, sin inspección ────
    ficha_completa('ROJ001', 19_000)
    papel('ROJ001', 'soat', HOY - timedelta(days=5))
    papel('ROJ001', 'rtm', HOY + timedelta(days=200))
    papel('ROJ001', 'poliza_rc', HOY + timedelta(days=200))
    papel('ROJ001', 'tarjeta_propiedad', None)
    l_rojo = lectura('ROJ001', 20_000, T(1, 6))
    custodia('ROJ001', T(1, 6), 20_000, conductor='Carla', fotos=2)
    ruta('ROJ001', 0, 'Beto', estado='EN_TRANSITO')
    plan = PlanTarea(vehiculo_id=veh['ROJ001'].id, tipo='aceite_motor',
                     intervalo_km=5000, fuente='manual_fabricante',
                     origen='ficha', activo=True)
    db.session.add(plan)
    db.session.flush()
    db.session.add(EjecucionTarea(plan_id=plan.id, lectura_id=l_rojo.id,
                                  ejecutado_ts=T(1, 6),
                                  registrado_por_usuario_id=A))
    # La ejecución se registró a 20.000; para que esté vencida hace falta
    # correr el odómetro más allá de 25.000 con una lectura posterior.
    lectura('ROJ001', 25_600, T(1, 18))

    # ── AMB001 — por vencer, daño en plazo, sin ficha, km en duda ────────
    papel('AMB001', 'soat', HOY + timedelta(days=10))
    papel('AMB001', 'rtm', HOY + timedelta(days=200))
    papel('AMB001', 'poliza_rc', None, estado='no_encontrado')
    custodia('AMB001', T(3, 6), 30_000, conductor='Dani', fin=T(2, 18),
             km_fin=30_050, forzado='el conductor no volvió a la sede',
             fotos=exigidas)
    custodia('AMB001', T(2, 18), 30_050, sede_id=sede.id, fotos=exigidas)
    lectura('AMB001', 30_000, T(3, 6), foto=False)
    lectura('AMB001', 30_050, T(2, 18), foto=False)
    db.session.commit()
    hallazgos.reportar(vehiculo_id=veh['AMB001'].id, criticidad='mayor',
                       descripcion='fuga de aceite en el diferencial',
                       km=30_050, reportado_por_usuario_id=A, ts=T(1, 9))

    # ── KMR001 — kilómetros en días sin ruta ─────────────────────────────
    ficha_completa('KMR001', 19_000)
    papeles_al_dia('KMR001')
    custodia('KMR001', T(12, 6), 20_000, conductor='Eva', fotos=exigidas)
    lectura('KMR001', 20_000, T(10, 7))
    lectura('KMR001', 20_150, T(9, 18))        # día con ruta: no marca
    ruta('KMR001', 9, 'Eva')
    lectura('KMR001', 20_300, T(4, 7))          # 9 → 4 incluye el día 9: no marca
    lectura('KMR001', 20_420, T(3, 18))         # 4 → 3 sin ruta: SEÑAL
    lectura('KMR001', 20_425, T(2, 18))         # 5 km: tolerancia
    lectura('KMR001', 20_625, T(1, 18), foto=False)  # tramo en duda

    # ── RUT001 — la ruta Norte y su mediana ──────────────────────────────
    papeles_al_dia('RUT001')
    ficha_completa('RUT001', 49_000)
    km = 50_000
    for dias_atras, recorrido in ((20, 100), (19, 104), (18, 96), (17, 100),
                                  (16, 102), (15, 98), (2, 250)):
        lectura('RUT001', km, T(dias_atras, 6))
        km += recorrido
        lectura('RUT001', km, T(dias_atras, 18))
        ruta('RUT001', dias_atras, 'Ana', maestra=norte)
    for dias_atras in (8, 7):
        lectura('RUT001', km, T(dias_atras, 6))
        km += 80
        lectura('RUT001', km, T(dias_atras, 18))
        ruta('RUT001', dias_atras, 'Ana', maestra=sur)
    db.session.commit()

    # ── GAL001 / GAL002 / PRC001 — combustible ───────────────────────────
    def tanquear(p, dias_atras, km, galones, valor, tanque='lleno'):
        registrar_tanqueo(
            vehiculo_id=veh[p].id, fecha=HOY - timedelta(days=dias_atras),
            valor=Decimal(valor), galones=Decimal(galones), tanque=tanque,
            estacion='Terpel Neiva', km=km, proveedor='Terpel',
            origen_costo='tarjeta_convenio', registrado_por_usuario_id=A,
            documento_numero=f'F-{p}-{dias_atras}', ts=T(dias_atras, 10))

    km = 70_000
    for i, dias_atras in enumerate((70, 60, 50, 40, 30, 20, 10, 3)):
        galones = 16 if dias_atras == 3 else 10
        tanquear('GAL001', dias_atras, km, galones, 15_000 * galones)
        km += 300
    km = 90_000
    for dias_atras in (12, 8, 4):
        tanquear('GAL002', dias_atras, km, 10, 150_000)
        km += 300
    tanquear('PRC001', 5, 40_000, 10, 200_000)

    # ── SAL001 — salió sin turno: custodia en la sede ────────────────────
    papeles_al_dia('SAL001')
    lectura('SAL001', 60_000, T(1, 18))
    custodia('SAL001', T(1, 18), 60_000, sede_id=sede.id, fotos=exigidas)
    ruta('SAL001', 0, 'Ana', estado='EN_TRANSITO')
    # Una ruta de hoy sin placa: no se puede cruzar contra ningún turno.
    ruta(None, 0, 'Dani', estado='PROGRAMADO')
    db.session.commit()

    tok = {r: create_access_token(identity=str(u.id)) for r, u in (
        ('admin', admin), ('flota', flota_u), ('conductor', cond_u),
        ('tienda', tienda_u))}
    return {'tok': tok, 'sede': sede.id, 'otra_sede': otra_sede.id,
            'veh': {p: v.id for p, v in veh.items()}, 'db': db}


def _bandeja(client, mundo, rol='flota', **params):
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    r = client.get('/flota/bandeja' + (f'?{q}' if q else ''),
                   headers=_auth(mundo['tok'][rol]))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _fila(b, placa):
    return next(f for f in b['hoy'] if f['placa'] == placa)


def _textos(fila):
    return ' | '.join(p['texto'] for p in fila['semaforo']['porque'])


def _pend(b, placa, clase=None):
    return [p for p in b['pendientes']
            if p['placa'] == placa and (clase is None or p['clase'] == clase)]


def _sen(b, placa=None, clase=None):
    return [s for s in b['senales']
            if (placa is None or s['placa'] == placa)
            and (clase is None or s['clase'] == clase)]


def _no_eval(b, placa=None, clase=None):
    return [s for s in b['senales_no_evaluables']
            if (placa is None or s['placa'] == placa)
            and (clase is None or s['clase'] == clase)]


# ═══════════════════════════════════════════════════════════════════════════

class TestQuienEntra:

    def test_sin_sesion_401(self, client, mundo):
        assert client.get('/flota/bandeja').status_code == 401

    def test_el_conductor_no_ve_la_flota_entera(self, client, mundo):
        """Las señales hablan de turnos ajenos: no es su pantalla."""
        r = client.get('/flota/bandeja', headers=_auth(mundo['tok']['conductor']))
        assert r.status_code == 403

    def test_tienda_tampoco(self, client, mundo):
        r = client.get('/flota/bandeja', headers=_auth(mundo['tok']['tienda']))
        assert r.status_code == 403

    def test_control_de_flota_entra_y_no_decide(self, client, mundo):
        """`puede_decidir` sale de DECIDE_FLOTA: la misma tupla que cierra un
        daño. La pantalla esconde los botones que el servidor va a negar."""
        b = _bandeja(client, mundo, 'flota')
        assert b['puede_decidir'] is False

    def test_gestion_entra_y_decide(self, client, mundo):
        b = _bandeja(client, mundo, 'admin')
        assert b['puede_decidir'] is True

    def test_almacen_ilegible_es_400_y_no_todo(self, client, mundo):
        r = client.get('/flota/bandeja?almacen_id=abc',
                       headers=_auth(mundo['tok']['flota']))
        assert r.status_code == 400

    def test_almacen_inexistente_es_400(self, client, mundo):
        r = client.get('/flota/bandeja?almacen_id=999999',
                       headers=_auth(mundo['tok']['flota']))
        assert r.status_code == 400


class TestHoy:

    def test_una_fila_por_vehiculo_activo_y_ninguna_de_baja(self, client, mundo):
        b = _bandeja(client, mundo)
        placas = [f['placa'] for f in b['hoy']]
        assert 'BAJ001' not in placas
        assert len(placas) == 10
        assert [f['pos'] for f in b['hoy']] == list(range(10))

    def test_verde_solo_cuando_no_hay_nada(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'VRD001')
        assert f['semaforo']['color'] == 'verde', _textos(f)
        assert 'Sin pendientes conocidos' in _textos(f)
        assert f['pendientes'] == 0

    def test_quien_lo_tiene_en_palabras(self, client, mundo):
        """«turno de Ana desde ayer a las 06:00», no «custodia #12»."""
        f = _fila(_bandeja(client, mundo), 'VRD001')
        assert f['custodio']['texto'] == 'turno de Ana desde ayer a las 06:00'
        assert f['donde']['texto'] == 'con el conductor (no se registra dónde)'

    def test_km_con_su_confianza(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'VRD001')
        assert f['km']['valor'] == 10_000
        assert f['km']['confianza'] == 'declarada'
        assert '10.000 km · declarado, sin verificar' in f['km']['texto']

    def test_la_inspeccion_de_hoy(self, client, mundo):
        b = _bandeja(client, mundo)
        assert _fila(b, 'VRD001')['inspeccion']['estado'] == 'apta'
        assert _fila(b, 'SIN001')['inspeccion']['estado'] == 'sin_hacer'

    def test_papel_vencido_pinta_rojo_con_su_nombre(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'ROJ001')
        assert f['semaforo']['color'] == 'rojo'
        # El mismo texto que el despacho y el teléfono del conductor.
        assert SOAT_VENCIDO in _textos(f)

    def test_ruta_de_hoy_sin_inspeccion_apta_es_rojo(self, client, mundo):
        """Regla 1: no saber tampoco autoriza a salir."""
        f = _fila(_bandeja(client, mundo), 'ROJ001')
        assert 'Tiene ruta hoy y todavía no tiene la inspección de hoy' in _textos(f)
        assert f['rutas_hoy'][0]['texto'] == 'sin ruta maestra con Beto · en la calle'

    def test_preventivo_vencido_es_rojo(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'ROJ001')
        assert 'Mantenimiento vencido: aceite de motor (600 km pasado)' in _textos(f)

    def test_por_vencer_y_dano_en_plazo_son_ambar(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'AMB001')
        assert f['semaforo']['color'] == 'ambar', _textos(f)
        t = _textos(f)
        assert SOAT_POR_VENCER in t
        assert '1 daño abierto dentro de plazo' in t
        assert 'Póliza de responsabilidad civil: nadie la pudo mostrar' in t
        assert 'Sin ficha técnica' in t
        assert 'El último kilometraje está en duda' in t

    def test_sin_dato_es_ambar_y_lo_dice_nunca_verde(self, client, mundo):
        """Un vehículo del que no se sabe nada no está bien: no se sabe."""
        f = _fila(_bandeja(client, mundo), 'SIN001')
        assert f['semaforo']['color'] == 'ambar'
        t = _textos(f)
        assert 'no se sabe cuántos km tiene' in t
        assert 'Nadie tiene el turno registrado' in t
        # Los cuatro papeles sin cargar van en UN renglón, no en cuatro.
        assert ('Sin cargar, no se sabe si están al día: SOAT, revisión '
                'técnico-mecánica, póliza de responsabilidad civil, tarjeta de '
                'propiedad') in t
        assert f['km']['valor'] == 'sin_dato'

    def test_en_custodia_de_la_sede(self, client, mundo):
        f = _fila(_bandeja(client, mundo), 'SAL001')
        assert f['donde']['texto'] == 'en BDJ-NB1 · Bodega CD'
        assert f['custodio']['tipo'] == 'sede'


class TestPendientes:

    def test_vrd001_no_tiene_ninguno(self, client, mundo):
        assert _pend(_bandeja(client, mundo), 'VRD001') == []

    def test_papel_vencido_por_vencer_y_no_encontrado(self, client, mundo):
        b = _bandeja(client, mundo)
        roj = _pend(b, 'ROJ001', 'documento')
        assert roj[0]['texto'] == SOAT_VENCIDO
        assert roj[0]['urgencia'] == 'rojo'
        assert roj[0]['accion'] == {'tipo': 'expediente', 'pestana': 'documentos'}
        # Un pendiente de papeles POR VEHÍCULO, con sus renglones.
        amb = _pend(b, 'AMB001', 'documento')
        assert len(amb) == 1
        assert amb[0]['lineas'] == [
            SOAT_POR_VENCER,
            'Póliza de responsabilidad civil: nadie la pudo mostrar (no se sabe si existe)',
            'Tarjeta de propiedad sin cargar: no se sabe si está al día']

    def test_papeles_sin_cargar(self, client, mundo):
        p = _pend(_bandeja(client, mundo), 'SIN001', 'documento')
        assert len(p) == 1
        assert p[0]['texto'].startswith('Sin cargar, no se sabe si están al día: SOAT')

    def test_la_cola_de_danos_trae_el_id_para_decidir(self, client, mundo):
        p = _pend(_bandeja(client, mundo), 'AMB001', 'dano')
        assert len(p) == 1
        assert p[0]['texto'] == 'fuga de aceite en el diferencial'
        assert p[0]['accion']['tipo'] == 'decidir_dano'
        assert p[0]['hallazgo_id'] == p[0]['accion']['hallazgo_id']
        assert p[0]['criticidad'] == 'mayor' and p[0]['vencido'] is False

    def test_km_en_duda_para_verificar(self, client, mundo):
        """Agrupado por placa: «2 lecturas de kilometraje en duda», no dos
        renglones iguales salvo el número."""
        p = _pend(_bandeja(client, mundo), 'AMB001', 'km_dudoso')
        assert len(p) == 1
        assert p[0]['texto'] == '2 lecturas de kilometraje en duda'
        assert len(p[0]['lectura_ids']) == 2
        assert p[0]['detalle'].startswith('La última: 30.050 km')
        assert p[0]['accion']['tipo'] == 'verificar_km'

    def test_cierre_forzado_reciente_con_quien_y_por_que(self, client, mundo):
        p = _pend(_bandeja(client, mundo), 'AMB001', 'cierre_forzado')
        assert len(p) == 1
        assert 'turno de Dani cerrado a la fuerza por Jefa' in p[0]['texto']
        assert 'el conductor no volvió a la sede' in p[0]['detalle']
        assert p[0]['accion']['tipo'] == 'fotos_turno'

    def test_turno_sin_fotos_de_inicio(self, client, mundo):
        """Fuga 10: sin línea base un golpe no se ubica en ningún turno."""
        p = _pend(_bandeja(client, mundo), 'ROJ001', 'turno_sin_fotos')
        assert len(p) == 1
        assert 'turno de Carla' in p[0]['texto']
        assert 'sin fotos de inicio (2 de' in p[0]['texto']

    def test_ficha_sin_crear(self, client, mundo):
        p = _pend(_bandeja(client, mundo), 'AMB001', 'ficha')
        assert p[0]['texto'] == 'sin ficha técnica: crear la primera'
        assert p[0]['accion']['pestana'] == 'ficha'

    def test_preventivo_vencido(self, client, mundo):
        p = _pend(_bandeja(client, mundo), 'ROJ001', 'preventivo')
        assert p and 'aceite de motor' in p[0]['texto']
        assert p[0]['urgencia'] == 'rojo'

    def test_lo_rojo_va_primero(self, client, mundo):
        urg = [p['urgencia'] for p in _bandeja(client, mundo)['pendientes']]
        assert urg == sorted(urg, key=lambda u: u != 'rojo')


class TestSenales:

    def test_km_en_dias_sin_ruta_con_su_evidencia(self, client, mundo):
        s = _sen(_bandeja(client, mundo), 'KMR001', 'km_sin_ruta')
        assert len(s) == 1, s
        ev = s[0]['evidencia']
        assert (ev['km_desde'], ev['km_hasta'], ev['km']) == (20_300, 20_420, 120)
        assert ev['rutas_del_vehiculo_esos_dias'] == 0
        assert s[0]['contexto'] == 'turno a nombre de Eva'
        assert s[0]['caso']['tipo'] == 'expediente'

    def test_la_tolerancia_y_el_dia_con_ruta_no_marcan(self, client, mundo):
        """20.150 → 20.300 cruza el día 9, que tuvo ruta; 20.420 → 20.425 son
        5 km. Ninguno de los dos es señal."""
        s = _sen(_bandeja(client, mundo), 'KMR001', 'km_sin_ruta')
        assert all(x['evidencia']['km_desde'] != 20_150 for x in s)
        assert all(x['evidencia']['km_desde'] != 20_420 for x in s)

    def test_un_tramo_en_duda_no_se_juzga_y_se_dice(self, client, mundo):
        n = _no_eval(_bandeja(client, mundo), 'KMR001', 'km_sin_ruta')
        assert any('en duda' in x['motivo'] for x in n)

    def test_sin_rutas_con_placa_no_hay_contra_que_cruzar(self, client, mundo):
        n = _no_eval(_bandeja(client, mundo), 'AMB001', 'km_sin_ruta')
        assert any('ninguna ruta registra esta placa' in x['motivo'] for x in n)

    def test_km_de_ruta_contra_la_mediana(self, client, mundo):
        s = _sen(_bandeja(client, mundo), 'RUT001', 'km_de_ruta')
        assert len(s) == 1, s
        ev = s[0]['evidencia']
        assert ev['km'] == 250 and ev['n'] == 6
        assert Decimal(ev['mediana_km']) == Decimal('100')
        assert ev['ruta_maestra'] == 'Norte'

    def test_ruta_con_pocos_recorridos_no_evaluable(self, client, mundo):
        n = _no_eval(_bandeja(client, mundo), 'RUT001', 'km_de_ruta')
        assert any(x['motivo'].startswith('Sur: 1 recorrido(s)') for x in n), n

    def test_galones_de_mas_con_rendimiento_medido(self, client, mundo):
        s = _sen(_bandeja(client, mundo), 'GAL001', 'galones')
        assert len(s) == 1, s
        ev = s[0]['evidencia']
        assert Decimal(ev['galones']) == Decimal('16')
        assert Decimal(ev['galones_esperados']) < Decimal('12')
        assert ev['ventanas_del_rendimiento'] == 7
        assert s[0]['caso'] == {'tipo': 'expediente', 'pestana': 'gastos',
                                'gasto_id': ev['gasto_id']}

    def test_sin_rendimiento_sostenible_no_hay_senal(self, client, mundo):
        b = _bandeja(client, mundo)
        assert _sen(b, 'GAL002', 'galones') == []
        n = _no_eval(b, 'GAL002', 'galones')
        assert n and 'no se puede sostener' in n[0]['motivo']

    def test_precio_del_galon_atipico(self, client, mundo):
        s = _sen(_bandeja(client, mundo), 'PRC001', 'precio_galon')
        assert len(s) == 1
        ev = s[0]['evidencia']
        assert Decimal(ev['precio_galon']) == Decimal('20000')
        assert Decimal(ev['mediana_flota']) == Decimal('15000')

    def test_el_precio_normal_no_marca(self, client, mundo):
        assert _sen(_bandeja(client, mundo), 'GAL001', 'precio_galon') == []

    def test_ruta_con_otro_conductor_que_el_del_turno(self, client, mundo):
        """Fuga 12. Propone el traspaso; no dice que Beto hizo nada malo."""
        s = _sen(_bandeja(client, mundo), 'ROJ001', 'turno_de_la_ruta')
        assert len(s) == 1
        assert s[0]['evidencia']['forma'] == 'turno_de_otro'
        assert 'la hace Beto' in s[0]['texto']
        assert 'a nombre de Carla' in s[0]['texto']

    def test_ruta_en_la_calle_con_el_vehiculo_en_la_sede(self, client, mundo):
        s = _sen(_bandeja(client, mundo), 'SAL001', 'turno_de_la_ruta')
        assert len(s) == 1
        assert s[0]['evidencia']['forma'] == 'salio_sin_turno'

    def test_ruta_de_hoy_sin_placa_se_declara(self, client, mundo):
        n = _no_eval(_bandeja(client, mundo), None, 'turno_de_la_ruta')
        assert any('no tiene placa' in x['motivo'] for x in n)

    def test_verde_no_tiene_senales(self, client, mundo):
        assert _sen(_bandeja(client, mundo), 'VRD001') == []

    def test_ninguna_senal_nombra_culpable(self, client, mundo):
        """Regla 2: el texto describe hechos. Nada de «robó», «fraude»,
        «responsable»."""
        b = _bandeja(client, mundo)
        todo = ' '.join(s['texto'] + s['contexto'] + s['propone']
                        for s in b['senales']).lower()
        for palabra in ('robo', 'robó', 'fraude', 'culpa', 'responsable',
                        'sanción', 'sancion'):
            assert palabra not in todo, palabra

    def test_los_umbrales_viajan(self, client, mundo):
        u = _bandeja(client, mundo)['umbrales']
        assert u['km_tolerancia_sin_ruta'] == '10'
        assert u['min_recorridos_ruta'] == '5'


class TestFiltroDeAlmacen:

    def test_filtra_por_la_sede_del_ultimo_turno_de_sede(self, client, mundo):
        """AMB001 y SAL001 quedaron por última vez en la sede NB1."""
        b = _bandeja(client, mundo, almacen_id=mundo['sede'])
        assert {f['placa'] for f in b['hoy']} == {'AMB001', 'SAL001'}
        assert b['filtro']['fuera_del_filtro'] == 8
        assert b['filtro']['sin_sede_conocida'] == 8
        assert {p['placa'] for p in b['pendientes']} <= {'AMB001', 'SAL001'}
        assert {s['placa'] for s in b['senales']} <= {'AMB001', 'SAL001'}

    def test_otra_sede_sin_vehiculos_no_inventa(self, client, mundo):
        b = _bandeja(client, mundo, almacen_id=mundo['otra_sede'])
        assert b['hoy'] == [] and b['pendientes'] == [] and b['senales'] == []


class TestNoSeArmaAMedias:

    def test_sin_una_tabla_503_y_no_una_bandeja_vacia(self, client, mundo,
                                                      monkeypatch):
        """Sin la tabla de daños, «sin pendientes» sería mentira (regla 5)."""
        from flota.adaptadores import bandeja as ad

        real = ad._tabla_existe
        monkeypatch.setattr(ad, '_tabla_existe',
                            lambda n: False if n == 'flota_hallazgo' else real(n))
        r = client.get('/flota/bandeja', headers=_auth(mundo['tok']['flota']))
        assert r.status_code == 503
        assert 'flota_hallazgo' in r.get_json()['detalle']

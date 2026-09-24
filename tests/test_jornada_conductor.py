"""
🕒 La jornada del conductor (`app/services/jornada_conductor.py`,
`GET /api/jornada`, `GET /api/jornada/resumen`, `flota_jornada.js`).

## Qué protege

1. **Cada evento dice de dónde sale y cuánto se le cree.** Una parada sin hora
   del teléfono es hora del SERVIDOR y confianza baja; con las columnas del
   teléfono, la hora es la del teléfono corregida por el desfase.
2. **Sin hora del teléfono no hay ráfagas**, y se declara.
3. **Comparar contra COMPAÑEROS de la misma maestra, con `n`**: las rutas
   propias no son pares; con menos de `n_minimo_pares` sale «sin base» y no se
   propone nada.
4. **Un día sin evidencia es «no reconstruible», no «limpio»**: horas y tiempo
   no explicado quedan `None`, nunca 0.
5. **El tiempo no explicado descuenta lo normal** (el p90 de los compañeros o
   el umbral fijo) y lo que el cargue cubre.
6. **Nunca «muerto», nunca sanción**: ni el servicio ni la pantalla lo dicen.
7. Permisos: `Roles.VISTA_FLOTA` y nadie más; 400 ante basura; rango ≤ 92.

## El mundo

Se arma con lo que la operación llama: `traspasar` (recibo y entrega de
turno), `inspecciones.registrar` (preoperacional), `RutaService.confirmar_parada`
(cada parada, con su GPS), `RutaService._marcar_liquidada`. Lo único que se
escribe a mano es lo que en la operación escribe el RELOJ: las marcas de tiempo
se fijan después, para que el día sea reproducible, y los bultos cargados (el
muelle). Las columnas de hora del teléfono se crean en la base cuando el test
las pide y se leen por SQL, igual que las leería el servicio en producción.
"""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest
import sqlalchemy as sa
from flask_jwt_extended import create_access_token

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SERVICIO = RAIZ / 'app' / 'services' / 'jornada_conductor.py'
RUTA = RAIZ / 'app' / 'routes' / 'jornada.py'
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: Un martes ya pasado. Todo el mundo se arma alrededor de este día.
DIA = date(2026, 9, 15)


def _svc():
    from app.services import jornada_conductor
    return jornada_conductor


def _t(dia, hhmm):
    """Hora de Bogotá → UTC naive, como la guarda el WMS."""
    from app.utils.fecha import inicio_del_dia_utc
    h, m = (int(x) for x in hhmm.split(':'))
    return inicio_del_dia_utc(dia) + timedelta(hours=h, minutes=m)


# ─────────────────────────────────────────────────────────────────────────────
# Constructores del mundo
# ─────────────────────────────────────────────────────────────────────────────

class Mundo:
    def __init__(self, db):
        from app.models.almacen import Almacen
        from app.models.ruta_maestra import RutaMaestra
        from app.models.usuario import Usuario
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores import catalogo
        self.db = db
        self.sede = Almacen(codigo='JOR-SEDE', nombre='Patio Neiva', activo=True,
                            bodega_siesa_id='NB1')
        db.session.add(self.sede)
        self.norte = RutaMaestra(nombre='NORTE', tipo_ruta='Municipal', activa=True)
        self.sur = RutaMaestra(nombre='SUR', tipo_ruta='Municipal', activa=True)
        db.session.add_all([self.norte, self.sur])
        self.admin = Usuario(email='admin-jor@test.com', nombre='Admin', rol='admin', activo=True)
        self.admin.set_password('x')
        db.session.add(self.admin)
        self.vehiculos = {}
        for placa in ('JOR001', 'JOR002', 'JOR003'):
            v = Vehiculo(placa=placa, tipo='camion', activo=True)
            db.session.add(v)
            self.vehiculos[placa] = v
        db.session.commit()
        catalogo.sembrar(db)
        self.conductores = {}
        self._n = 0

    def conductor(self, nombre, con_cuenta=True, activo=True):
        from app.models.conductor import Conductor
        from app.models.usuario import Usuario
        u = None
        if con_cuenta:
            u = Usuario(email=f'{nombre.lower()}-jor@test.com', nombre=nombre,
                        rol='conductor', activo=True)
            u.set_password('x')
            self.db.session.add(u)
            self.db.session.flush()
        c = Conductor(nombre=nombre, cedula=f'CC-{nombre}', activo=activo,
                      usuario_id=u.id if u else None)
        self.db.session.add(c)
        self.db.session.commit()
        self.conductores[nombre] = c
        return c

    def _sufijo(self):
        self._n += 1
        return self._n

    # ── Turnos (flota) ─────────────────────────────────────────────────────

    def recibe(self, c, placa, ts, km):
        from flota.adaptadores.traspaso import traspasar
        from flota.dominio.valores import CustodioTipo, QuienPide
        return traspasar(vehiculo_id=self.vehiculos[placa].id, km=km,
                         registrado_por_usuario_id=c.usuario_id,
                         custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=c.id,
                         quien_pide=QuienPide.CONDUCTOR, ts=ts)

    def entrega(self, c, placa, ts, km, fuera=None):
        """Entrega a la sede, o —con `fuera` = motivo— lo deja fuera de sede."""
        from flota.adaptadores.traspaso import traspasar
        from flota.dominio.valores import CustodioTipo, QuienPide, Ubicacion
        if fuera:
            return traspasar(vehiculo_id=self.vehiculos[placa].id, km=km,
                             registrado_por_usuario_id=c.usuario_id,
                             custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=c.id,
                             quien_pide=QuienPide.CONDUCTOR, ubicacion=Ubicacion.FUERA_DE_SEDE,
                             ubicacion_motivo=fuera, ts=ts)
        return traspasar(vehiculo_id=self.vehiculos[placa].id, km=km,
                         registrado_por_usuario_id=c.usuario_id,
                         custodio_tipo=CustodioTipo.SEDE, custodio_sede_id=self.sede.id,
                         quien_pide=QuienPide.CONDUCTOR, ubicacion=Ubicacion.SEDE, ts=ts)

    def inspeccion(self, c, placa, ts, km, segundos):
        from flota.adaptadores import inspecciones
        v = self.vehiculos[placa]
        items = inspecciones.items_del_dia_de(v, inspecciones._dia_bogota(ts))
        return inspecciones.registrar(
            vehiculo_id=v.id, tipo_vehiculo_obj=v, km=km,
            inspeccionada_por_usuario_id=c.usuario_id,
            respuestas=[{'item_id': i.id, 'respuesta': 'optimo'} for i in items],
            segundos_llenado=segundos, ts=ts)

    def tanqueo(self, c, placa, ts, km):
        from flota.adaptadores.modelos import LecturaOdometro
        self.db.session.add(LecturaOdometro(vehiculo_id=self.vehiculos[placa].id, valor_km=km,
                                            ts=ts, origen='tanqueo',
                                            autor_usuario_id=c.usuario_id))
        self.db.session.commit()

    # ── Rutas (WMS) ────────────────────────────────────────────────────────

    def ruta(self, c, maestra, cierre, paradas, entregada=None, liquidada=None,
             cargue=None, placa='JOR001', clientes=None):
        """Una ruta con `paradas` = [(hora_utc, datos_de_confirmar_parada)].

        Los bultos se escriben a mano (los escanea el muelle); cada parada se
        confirma con `RutaService.confirmar_parada`, y después se fija su hora.
        """
        from app.models.bulto import Bulto
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        from app.services.ruta_service import RutaService
        db = self.db
        r = RutaDespacho(conductor_id=c.id, vehiculo_id=self.vehiculos[placa].id,
                         ruta_maestra_id=maestra.id if maestra else None,
                         tipo_ruta='Municipal', estado='EN_TRANSITO',
                         fecha_programada=None, fecha_cierre=cierre)
        db.session.add(r)
        db.session.flush()
        tareas = []
        for i, _ in enumerate(paradas):
            n = self._sufijo()
            cliente = (clientes[i] if clientes else f'CLIENTE {n}')
            t = TareaPacking(codigo=f'PK-JOR-{n}', almacen_id=self.sede.id, estado='DESPACHADO',
                             numero_pedido_siesa=f'PD{9000 + n}', cliente=cliente,
                             municipio='NEIVA')
            db.session.add(t)
            db.session.flush()
            db.session.add(Bulto(tarea_id=t.id, codigo_barras=f'JOR{n}-01', tipo='Caja', numero=1,
                                 total=1, estado='CARGADO', ruta_despacho_id=r.id,
                                 fecha_cargado=(cargue[0] if cargue else None)))
            tareas.append(t)
        if cargue and paradas:
            # el último bulto escaneado cierra el intervalo del cargue
            db.session.flush()
            ultimo = Bulto.query.filter_by(ruta_despacho_id=r.id).order_by(Bulto.id.desc()).first()
            ultimo.fecha_cargado = cargue[1]
        db.session.commit()
        for t, (ts, datos) in zip(tareas, paradas):
            base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': 1000, 'foto_entrega': 'x',
                    'geo': {'lat': 2.93, 'lon': -75.28, 'precision_m': 12}}
            base.update(datos or {})
            quien = base.pop('_quien', c.usuario_id)
            rid, _ = RutaService.confirmar_parada(r.id, t.id, quien, base)
            rec = db.session.get(RecaudoEntrega, rid)
            rec.fecha_creacion = ts
            rec.fecha_confirmacion = ts
            for g in (rec.geo or []):
                g.capturado_en = ts
            db.session.commit()
        if entregada:
            r.estado = 'ENTREGADA'
            r.fecha_entregada = entregada
        if liquidada:
            RutaService._marcar_liquidada(r, self.admin.id)
            r.liquidada_en = liquidada
        db.session.commit()
        return r


@pytest.fixture
def mundo(db):
    return Mundo(db)


def _dia_de_ana(m, dia=DIA, hueco=True):
    """El día de referencia: recibe, inspecciona, carga, cuatro paradas, tanquea,
    cierra y entrega. Con `hueco`, entre la 2.ª y la 3.ª parada pasan 3 h 50."""
    ana = m.conductores.get('Ana') or m.conductor('Ana')
    m.recibe(ana, 'JOR001', _t(dia, '06:30'), 10_000)
    m.inspeccion(ana, 'JOR001', _t(dia, '06:40'), 10_000, segundos=95)
    h = (dict(p3='12:30', p4='13:00', tanqueo='13:30', cierre='14:30', entrega='15:00')
         if hueco else
         dict(p3='09:10', p4='09:40', tanqueo='10:10', cierre='10:40', entrega='11:00'))
    m.ruta(ana, m.norte, cierre=_t(dia, '07:30'),
           cargue=(_t(dia, '06:45'), _t(dia, '07:20')),
           paradas=[(_t(dia, '08:00'), {}), (_t(dia, '08:40'), {}),
                    (_t(dia, h['p3']), {}), (_t(dia, h['p4']), {})],
           entregada=_t(dia, h['cierre']), liquidada=_t(dia, '16:00'))
    m.tanqueo(ana, 'JOR001', _t(dia, h['tanqueo']), 10_080)
    m.entrega(ana, 'JOR001', _t(dia, h['entrega']), 10_120)
    return ana


def _base_de_companeros(m, n=12, duracion_h=4, gap_min=30, maestra=None, conductor=None,
                        motivo_cada=None, desde=DIA, placa='JOR002'):
    """`n` rutas de un compañero en días anteriores: cierre 07:00, tres paradas
    cada `gap_min`, cierre de ruta a las 07:00 + `duracion_h`, liquidada 2 h
    después. Con `motivo_cada=(k, motivo)`, cada k-ésima parada es un rechazo."""
    beto = conductor or m.conductores.get('Beto') or m.conductor('Beto')
    maestra = maestra or m.norte
    k = 0
    for i in range(1, n + 1):
        d = desde - timedelta(days=i)
        paradas = []
        for j in range(3):
            k += 1
            datos = {}
            if motivo_cada and k % motivo_cada[0] == 0:
                datos = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': motivo_cada[1],
                         'observaciones': 'no estaba', 'forma_pago': None}
            paradas.append((_t(d, '07:30') + timedelta(minutes=gap_min * j), datos))
        cierre = _t(d, '07:00')
        m.ruta(beto, maestra, cierre=cierre, paradas=paradas, placa=placa,
               entregada=cierre + timedelta(hours=duracion_h),
               liquidada=cierre + timedelta(hours=duracion_h + 2))
    return beto


def _jornada(conductor, dia=DIA):
    return _svc().jornada(conductor.id, dia)


def _senales(j, clave=None):
    return [s for s in j['senales'] if clave is None or s['clave'] == clave]


# ─────────────────────────────────────────────────────────────────────────────
# 1 · La línea de tiempo
# ─────────────────────────────────────────────────────────────────────────────

class TestLineaDeTiempo:

    def test_el_dia_completo_en_orden_con_fuente_y_confianza(self, db, mundo):
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        tipos = [e['tipo'] for e in j['eventos']]
        assert tipos == ['recibo_turno', 'preoperacional', 'cargue', 'cierre_cargue',
                         'parada', 'parada', 'parada', 'parada', 'tanqueo',
                         'cierre_ruta', 'entrega_turno', 'liquidacion']
        ev = {e['tipo']: e for e in j['eventos']}
        assert ev['recibo_turno']['hora'] == '06:30'
        assert ev['recibo_turno']['hora_fuente'] == 'servidor'
        assert ev['recibo_turno']['confianza'] == 'alta'
        assert ev['recibo_turno']['fuente'] == 'flota_custodia.inicio_ts'
        assert ev['cargue']['hora_fin'] == '07:20'
        assert ev['preoperacional']['detalle']['segundos_llenado'] == 95
        assert ev['entrega_turno']['detalle']['km_turno'] == 120
        # sin columnas del teléfono, toda parada es hora del servidor y baja
        paradas = [e for e in j['eventos'] if e['tipo'] == 'parada']
        assert {p['hora_fuente'] for p in paradas} == {'servidor'}
        assert {p['confianza'] for p in paradas} == {'baja'}
        assert paradas[0]['fuente'] == 'recaudos_entrega.fecha_creacion'
        assert paradas[0]['lat'] == pytest.approx(2.93)
        assert j['estado'] == 'reconstruida'
        assert j['jornada']['primer_evento'] == '06:30'
        assert j['jornada']['ultimo_evento'] == '15:00'
        assert j['jornada']['horas'] == 8.5

    def test_lo_que_no_hizo_el_conductor_no_es_suyo(self, db, mundo):
        """El cargue lo escanea el muelle, el cierre de la ruta no guarda autor,
        la liquidación es de la oficina: ninguno cuenta como gesto propio."""
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        propios = {e['tipo'] for e in j['eventos'] if e['propio']}
        assert propios == {'recibo_turno', 'preoperacional', 'parada', 'tanqueo',
                           'entrega_turno'}

    def test_la_parada_que_confirmo_otra_persona_no_es_suya(self, db, mundo):
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'),
                   paradas=[(_t(DIA, '08:00'), {}),
                            (_t(DIA, '09:00'), {'_quien': mundo.admin.id})])
        j = _jornada(ana)
        tipos = [e['tipo'] for e in j['eventos'] if e['tipo'].startswith('parada')]
        assert tipos == ['parada', 'parada_por_otro']
        assert j['paradas']['propias'] == 1 and j['paradas']['por_otro'] == 1

    def test_un_dia_sin_nada_es_sin_actividad(self, db, mundo):
        ana = mundo.conductor('Ana')
        j = _jornada(ana)
        assert j['estado'] == 'sin_actividad'
        assert j['eventos'] == [] and j['senales'] == []

    def test_un_dia_con_poca_evidencia_es_no_reconstruible_y_no_cero(self, db, mundo):
        """Una sola parada, sin turno: no se puede decir nada de su día.
        `None`, nunca 0."""
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'),
                   paradas=[(_t(DIA, '08:00'), {})])
        j = _jornada(ana)
        assert j['estado'] == 'no_reconstruible'
        assert j['jornada']['horas'] is None
        assert j['jornada']['tramo_observado_h'] is None
        assert j['jornada']['no_explicado_min'] is None
        assert j['huecos'] == []
        assert any('preoperacional' in f for f in j['cobertura']['faltan'])
        assert _senales(j, 'tiempo_no_explicado') == []

    def test_sin_apertura_ni_cierre_se_ve_el_tramo_no_la_jornada(self, db, mundo):
        """Dos paradas sin turno: entre ellas sí se ve, pero cuánto duró su
        día no se sabe — `horas` es None, no las 5 h entre las paradas."""
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'),
                   paradas=[(_t(DIA, '08:00'), {}), (_t(DIA, '13:00'), {})])
        j = _jornada(ana)
        assert j['estado'] == 'parcial'
        assert j['jornada']['horas'] is None
        assert j['jornada']['tramo_observado_h'] == 5.0
        assert j['jornada']['no_explicado_min'] == 240
        assert 'no la jornada' in j['motivo_estado']
        assert _senales(j, 'tiempo_no_explicado')[0]['nivel'] == 'observa'

    def test_el_evento_de_las_ocho_de_la_noche_es_de_ese_dia(self, db, mundo):
        """20:30 Bogotá es 01:30 UTC del día siguiente: pertenece a DIA."""
        ana = mundo.conductor('Ana')
        mundo.recibe(ana, 'JOR001', _t(DIA, '20:30'), 500)
        j = _jornada(ana)
        assert [e['tipo'] for e in j['eventos']] == ['recibo_turno']
        assert _jornada(ana, DIA + timedelta(days=1))['eventos'] == []

    def test_dejar_el_vehiculo_fuera_de_sede_es_una_entrega_con_motivo(self, db, mundo):
        ana = mundo.conductor('Ana')
        mundo.recibe(ana, 'JOR001', _t(DIA, '06:00'), 100)
        mundo.entrega(ana, 'JOR001', _t(DIA, '18:00'), 150, fuera='duerme en Pitalito')
        j = _jornada(ana)
        ent = [e for e in j['eventos'] if e['tipo'] == 'entrega_turno']
        assert len(ent) == 1
        assert ent[0]['detalle']['queda'] == 'fuera_de_sede'
        assert ent[0]['detalle']['motivo_ubicacion'] == 'duerme en Pitalito'
        # y el día siguiente no arranca con un «recibió» inventado
        assert _jornada(ana, DIA + timedelta(days=1))['eventos'] == []

    def test_la_entrega_forzada_por_la_oficina_no_es_suya(self, db, mundo):
        from flota.adaptadores.traspaso import traspasar
        from flota.dominio.valores import CustodioTipo, QuienPide, Ubicacion
        ana = mundo.conductor('Ana')
        mundo.recibe(ana, 'JOR001', _t(DIA, '06:00'), 100)
        traspasar(vehiculo_id=mundo.vehiculos['JOR001'].id, km=150,
                  registrado_por_usuario_id=mundo.admin.id, custodio_tipo=CustodioTipo.SEDE,
                  custodio_sede_id=mundo.sede.id, quien_pide=QuienPide.ADMIN_ZONA,
                  motivo_forzado='se fue sin entregar', ubicacion=Ubicacion.SEDE,
                  ts=_t(DIA, '19:00'))
        j = _jornada(ana)
        ent = [e for e in j['eventos'] if e['tipo'] == 'entrega_turno'][0]
        assert ent['propio'] is False
        assert ent['detalle']['cierre_forzado'] is True


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Huecos y tiempo no explicado
# ─────────────────────────────────────────────────────────────────────────────

class TestHuecos:

    def test_sin_base_compara_contra_el_umbral_fijo_y_lo_dice(self, db, mundo):
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        grande = [h for h in j['huecos'] if h['desde'] == '08:40'][0]
        assert grande['minutos'] == 230
        assert grande['categoria'] == 'entre_paradas'
        assert grande['base'] == 'sin_base' and grande['referencia_min'] == 60
        assert grande['no_explicado_min'] == 170
        assert grande['confianza'] == 'baja'      # dos paradas con hora del servidor
        s = _senales(j, 'tiempo_no_explicado')
        assert len(s) == 1
        assert s[0]['nivel'] == 'observa'          # sin base no se pide revisar
        assert s[0]['confianza'] == 'baja'
        assert j['jornada']['no_explicado_min'] == 170

    def test_con_base_usa_el_p90_de_los_companeros(self, db, mundo):
        _base_de_companeros(mundo, n=12, gap_min=30)
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        grande = [h for h in j['huecos'] if h['desde'] == '08:40'][0]
        assert grande['base'] == 'con_base'
        assert grande['referencia_min'] == 30
        assert grande['n_pares'] == 24             # 12 rutas × 2 tramos
        assert grande['no_explicado_min'] == 200
        assert _senales(j, 'tiempo_no_explicado')[0]['nivel'] == 'revisar'

    def test_lo_que_cubre_el_cargue_no_es_hueco(self, db, mundo):
        """06:45 → 07:30 son 45 min, pero hasta las 07:20 se estuvo cargando:
        quedan 10."""
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        tramo = [h for h in j['huecos'] if h['desde'] == '06:45']
        assert len(tramo) == 1 and tramo[0]['minutos'] == 10
        assert not [h for h in j['huecos'] if h['desde'] == '06:40']   # 5 min, no se lista

    def test_un_dia_sin_huecos_largos_no_propone_nada(self, db, mundo):
        ana = _dia_de_ana(mundo, hueco=False)
        j = _jornada(ana)
        assert j['estado'] == 'reconstruida'
        assert _senales(j, 'tiempo_no_explicado') == []

    def test_el_hueco_sabe_despues_de_que_evento_va(self, db, mundo):
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        grande = [h for h in j['huecos'] if h['desde'] == '08:40'][0]
        assert j['eventos'][grande['despues_de']]['hora'] == '08:40'


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Señales contra los compañeros
# ─────────────────────────────────────────────────────────────────────────────

class TestDuracionDeRuta:

    def test_mas_larga_que_el_p90_de_sus_companeros(self, db, mundo):
        _base_de_companeros(mundo, n=12, duracion_h=4)
        ana = _dia_de_ana(mundo)      # 07:30 → 14:30 = 7 h
        s = _senales(_jornada(ana), 'duracion_ruta')
        assert len(s) == 1
        c = s[0]['comparacion']
        assert c['valor'] == 7.0 and c['mediana'] == 4.0 and c['n'] == 12
        assert c['base'] == 'con_base'
        assert s[0]['evidencia'][0]['cierre_cargue'] == '07:30'

    def test_con_nueve_rutas_de_companeros_no_hay_base(self, db, mundo):
        _base_de_companeros(mundo, n=9, duracion_h=4)
        ana = _dia_de_ana(mundo)
        j = _jornada(ana)
        assert _senales(j, 'duracion_ruta') == []
        assert j['rutas'][0]['duracion_pares']['base'] == 'sin_base'
        assert j['rutas'][0]['duracion_pares']['n'] == 9

    def test_sus_propias_rutas_no_son_sus_pares(self, db, mundo):
        """Si solo Ana maneja la NORTE, no hay con quién compararla: su
        historia no es la vara de sí misma."""
        ana = mundo.conductor('Ana')
        _base_de_companeros(mundo, n=12, duracion_h=4, conductor=ana, placa='JOR001')
        _dia_de_ana(mundo)
        j = _jornada(ana)
        assert j['rutas'][0]['duracion_pares']['n'] == 0
        assert _senales(j, 'duracion_ruta') == []

    def test_la_hora_de_cierre_del_cargue_contra_la_de_la_ruta(self, db, mundo):
        _base_de_companeros(mundo, n=12)
        ana = _dia_de_ana(mundo)
        r = _jornada(ana)['rutas'][0]
        assert r['cierre_cargue'] == '07:30' and r['cierre_cargue_pares'] == '07:00'


class TestKmEntreTurnos:

    def test_el_camion_sumo_km_en_el_patio(self, db, mundo):
        beto = mundo.conductor('Beto')
        ana = mundo.conductor('Ana')
        ayer = DIA - timedelta(days=1)
        mundo.recibe(beto, 'JOR001', _t(ayer, '06:00'), 1000)
        mundo.entrega(beto, 'JOR001', _t(ayer, '18:00'), 1100)
        mundo.recibe(ana, 'JOR001', _t(DIA, '06:00'), 1180)
        s = _senales(_jornada(ana), 'km_entre_turnos')
        assert len(s) == 1
        ev = s[0]['evidencia'][0]
        assert ev['km'] == 80 and ev['entrego'] == 'Beto' and ev['recibio'] == 'Ana'
        assert ev['rol'] == 'recibió'
        assert ev['en_medio'][0]['custodio'] == 'la sede Patio Neiva'
        # y el mismo caso aparece en el día de quien lo entregó antes
        sb = _senales(_jornada(beto, ayer), 'km_entre_turnos')
        assert len(sb) == 1 and sb[0]['evidencia'][0]['rol'] == 'entregó antes'

    def test_sin_km_de_mas_no_hay_senal(self, db, mundo):
        beto = mundo.conductor('Beto')
        ana = mundo.conductor('Ana')
        ayer = DIA - timedelta(days=1)
        mundo.recibe(beto, 'JOR001', _t(ayer, '06:00'), 1000)
        mundo.entrega(beto, 'JOR001', _t(ayer, '18:00'), 1100)
        mundo.recibe(ana, 'JOR001', _t(DIA, '06:00'), 1100)
        assert _senales(_jornada(ana), 'km_entre_turnos') == []

    def test_el_traspaso_directo_no_se_mide(self, db, mundo):
        """Conductor → conductor: `traspasar` escribe el mismo km en los dos
        lados. Medirlo daría cero por construcción."""
        from flota.adaptadores.traspaso import traspasar
        from flota.dominio.valores import CustodioTipo, QuienPide
        beto = mundo.conductor('Beto')
        ana = mundo.conductor('Ana')
        mundo.recibe(beto, 'JOR001', _t(DIA, '06:00'), 1000)
        traspasar(vehiculo_id=mundo.vehiculos['JOR001'].id, km=1100,
                  registrado_por_usuario_id=beto.usuario_id,
                  custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=ana.id,
                  quien_pide=QuienPide.CONDUCTOR, ts=_t(DIA, '12:00'))
        assert _svc()._casos_km_entre_turnos(
            _svc().Mundo(DIA, DIA, _svc().umbrales())) == []


class TestRechazosContraLosCompaneros:

    def _ana_con_rechazos(self, m, n_rutas=4, rechazos=4):
        """Ana: `n_rutas` rutas de 3 paradas en los días anteriores + la de
        DIA; `rechazos` de ellas por cliente cerrado, una el propio DIA."""
        ana = m.conductores.get('Ana') or m.conductor('Ana')
        cerrado = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'CLIENTE_CERRADO',
                   'observaciones': 'cerrado', 'forma_pago': None}
        con_rechazo = set(range(rechazos))          # DIA y los días anteriores
        for i in range(n_rutas, -1, -1):
            d = DIA - timedelta(days=i)
            paradas = []
            for j in range(3):
                datos = dict(cerrado) if (i in con_rechazo and j == 0) else {}
                paradas.append((_t(d, '08:00') + timedelta(minutes=30 * j), datos))
            m.ruta(ana, m.norte, cierre=_t(d, '07:30'), paradas=paradas,
                   entregada=_t(d, '11:00'), placa='JOR001')
        return ana

    def test_cuatro_rechazos_donde_sus_companeros_tendrian_medio(self, db, mundo):
        _base_de_companeros(mundo, n=12, motivo_cada=(18, 'CLIENTE_CERRADO'))  # 2 de 36
        ana = self._ana_con_rechazos(mundo)                                      # 4 de 15
        s = _senales(_jornada(ana), 'rechazo_cliente_cerrado')
        assert len(s) == 1
        c = s[0]['comparacion']
        assert c['observado_comparable'] == 4 and c['n_comparable'] == 15
        assert c['n_pares'] == 36
        assert c['esperado_pares'] == pytest.approx(15 * 2 / 36, abs=0.05)
        assert c['base'] == 'con_base'
        assert s[0]['evidencia'][0]['con_gps'] is True

    def test_sin_base_de_companeros_no_se_propone(self, db, mundo):
        _base_de_companeros(mundo, n=3, motivo_cada=(18, 'CLIENTE_CERRADO'))    # 9 paradas
        ana = self._ana_con_rechazos(mundo)
        assert _senales(_jornada(ana), 'rechazo_cliente_cerrado') == []

    def test_un_dia_sin_ese_motivo_no_repite_la_senal(self, db, mundo):
        _base_de_companeros(mundo, n=12, motivo_cada=(18, 'CLIENTE_CERRADO'))
        ana = self._ana_con_rechazos(mundo)
        otro = DIA + timedelta(days=1)
        mundo.ruta(ana, mundo.norte, cierre=_t(otro, '07:30'),
                   paradas=[(_t(otro, '08:00'), {})], placa='JOR001')
        assert _senales(_jornada(ana, otro), 'rechazo_cliente_cerrado') == []


class TestOtrasSenales:

    def test_ruta_que_cruza_el_dia(self, db, mundo):
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '17:00'),
                   paradas=[(_t(DIA, '18:00'), {})],
                   entregada=_t(DIA + timedelta(days=1), '09:00'))
        s = _senales(_jornada(ana), 'ruta_cruza_el_dia')
        assert len(s) == 1 and s[0]['nivel'] == 'observa'

    def test_preoperacional_rapido_sin_base_y_con_base(self, db, mundo):
        ana = mundo.conductor('Ana')
        mundo.recibe(ana, 'JOR001', _t(DIA, '06:00'), 100)
        mundo.inspeccion(ana, 'JOR001', _t(DIA, '06:05'), 100, segundos=45)
        s = _senales(_jornada(ana), 'preoperacional_rapido')
        assert len(s) == 1 and s[0]['comparacion']['base'] == 'sin_base'
        assert s[0]['nivel'] == 'observa'
        # compañeros que tardan 180 s: con base, 45 s es muy rápido
        beto = mundo.conductor('Beto')
        for i in range(1, 11):
            d = DIA - timedelta(days=i)
            mundo.inspeccion(beto, 'JOR002', _t(d, '06:00'), 500 + i, segundos=180)
        s = _senales(_jornada(ana), 'preoperacional_rapido')
        assert s[0]['comparacion']['base'] == 'con_base'
        assert s[0]['comparacion']['p10'] == 180 and s[0]['nivel'] == 'revisar'

    def test_preoperacional_normal_no_propone(self, db, mundo):
        ana = mundo.conductor('Ana')
        mundo.inspeccion(ana, 'JOR001', _t(DIA, '06:05'), 100, segundos=240)
        assert _senales(_jornada(ana), 'preoperacional_rapido') == []

    def test_tiempo_hasta_liquidar_contra_la_ruta(self, db, mundo):
        _base_de_companeros(mundo, n=12, duracion_h=4)          # se liquidan en 2 h
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'),
                   paradas=[(_t(DIA, '08:00'), {})], entregada=_t(DIA, '11:00'),
                   liquidada=_t(DIA + timedelta(days=2), '11:00'), placa='JOR001')
        s = _senales(_jornada(ana), 'tiempo_hasta_liquidar')
        assert len(s) == 1
        assert s[0]['comparacion']['valor'] == 48.0 and s[0]['comparacion']['p90'] == 2.0

    def test_umbrales_configurables_y_declarados(self, db, mundo, monkeypatch):
        """Fuera de sede y descuentos con la base bajada por entorno."""
        monkeypatch.setenv('JORNADA_UMBRALES', json.dumps(
            {'n_minimo_pares': 2, 'n_minimo_conductor': 2, 'nadie': 1}))
        ana, beto = mundo.conductor('Ana'), mundo.conductor('Beto')
        for i in range(3, 0, -1):
            d = DIA - timedelta(days=i)
            mundo.recibe(beto, 'JOR002', _t(d, '06:00'), 1000 + (3 - i) * 100)
            mundo.entrega(beto, 'JOR002', _t(d, '18:00'), 1050 + (3 - i) * 100)
        for i in range(2, -1, -1):
            d = DIA - timedelta(days=i)
            mundo.recibe(ana, 'JOR001', _t(d, '06:00'), 5000 + (2 - i) * 100)
            mundo.entrega(ana, 'JOR001', _t(d, '18:00'), 5050 + (2 - i) * 100,
                          fuera='en la casa')
        j = _jornada(ana)
        assert j['umbrales']['n_minimo_pares'] == 2
        assert j['umbrales_rechazados'] == {'nadie': 'clave desconocida'}
        s = _senales(j, 'fuera_de_sede_frecuente')
        assert len(s) == 1
        assert s[0]['comparacion']['observado_comparable'] == 3
        # tres cierres de turno, no cinco: re-declararse a la mañana siguiente
        # (recoger el camión de la casa) no es cerrar un turno
        assert s[0]['comparacion']['n_comparable'] == 3
        assert s[0]['evidencia'][0]['motivo'] == 'en la casa'

    def test_descuentos_en_la_puerta(self, db, mundo, monkeypatch):
        monkeypatch.setenv('JORNADA_UMBRALES', json.dumps(
            {'n_minimo_pares': 3, 'n_minimo_conductor': 3}))
        _base_de_companeros(mundo, n=2)
        ana = mundo.conductor('Ana')
        desc = {'motivo_descuento': 'RETEFUENTE_2.5', 'monto_descuento': 500}
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                   paradas=[(_t(DIA, '08:00'), desc), (_t(DIA, '08:30'), desc),
                            (_t(DIA, '09:00'), desc)])
        s = _senales(_jornada(ana), 'descuentos_en_la_puerta')
        assert len(s) == 1 and s[0]['comparacion']['observado_comparable'] == 3
        assert s[0]['evidencia'][0]['motivo'] == 'RETEFUENTE_2.5'

    def test_rechazos_sin_ubicacion(self, db, mundo, monkeypatch):
        monkeypatch.setenv('JORNADA_UMBRALES', json.dumps(
            {'n_minimo_pares': 3, 'n_minimo_conductor': 3}))
        _base_de_companeros(mundo, n=2)
        ana = mundo.conductor('Ana')
        sin_gps = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'DIRECCION_ERRADA',
                   'observaciones': 'no la encontré', 'forma_pago': None,
                   'geo': {'motivo': 'permiso_denegado'}}
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                   paradas=[(_t(DIA, '08:00'), sin_gps), (_t(DIA, '08:30'), sin_gps),
                            (_t(DIA, '09:00'), {})])
        j = _jornada(ana)
        s = _senales(j, 'rechazos_sin_ubicacion')
        assert len(s) == 1
        assert s[0]['evidencia'][0]['gps']['motivo_sin_dato'] == 'permiso_denegado'
        assert j['paradas']['evidencia']['rechazos_sin_gps'] == 2


# ─────────────────────────────────────────────────────────────────────────────
# 4 · La hora del teléfono (columnas de otra tanda)
# ─────────────────────────────────────────────────────────────────────────────

_COLS = {
    'recaudos_entrega': (('ts_dispositivo', 'DATETIME'), ('ts_desfase_s', 'INTEGER'),
                         ('via_cola', 'BOOLEAN')),
    'entregas_geo': (('ts_dispositivo', 'DATETIME'), ('pos_ts_dispositivo', 'DATETIME')),
}


@pytest.fixture
def columnas_telefono(db):
    """Crea en la base las columnas de hora del teléfono si no existen, y las
    quita al terminar si las creó. Con el modelo ya integrado, existen."""
    creadas = []
    insp = sa.inspect(db.engine)
    for tabla, cols in _COLS.items():
        presentes = {c['name'] for c in insp.get_columns(tabla)}
        for nombre, tipo in cols:
            if nombre not in presentes:
                db.session.execute(sa.text(f'ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}'))
                creadas.append((tabla, nombre))
    db.session.commit()
    yield
    db.session.rollback()
    for tabla, nombre in creadas:
        db.session.execute(sa.text(f'ALTER TABLE {tabla} DROP COLUMN {nombre}'))
    db.session.commit()


@pytest.fixture
def sin_columnas_telefono(monkeypatch):
    """La base sin las columnas, aunque el modelo integrado las tenga."""
    monkeypatch.setattr('app.services.jornada_conductor.columnas_hora_del_telefono',
                        lambda: {t: () for t in _COLS})


def _poner_telefono(db, ruta_id, valores):
    """`valores` = [(ts_dispositivo, desfase, via_cola)] en el orden de las
    paradas de la ruta (por id de recaudo)."""
    ids = [r for (r,) in db.session.execute(sa.text(
        'SELECT id FROM recaudos_entrega WHERE ruta_id = :r ORDER BY id'), {'r': ruta_id})]
    for rid, (ts, des, via) in zip(ids, valores):
        db.session.execute(sa.text(
            'UPDATE recaudos_entrega SET ts_dispositivo = :ts, ts_desfase_s = :d, '
            'via_cola = :v WHERE id = :i'),
            {'ts': ts.strftime('%Y-%m-%d %H:%M:%S.%f') if ts else None, 'd': des, 'v': via,
             'i': rid})
    db.session.commit()


class TestHoraDelTelefono:

    def test_sin_columnas_no_hay_rafagas_y_se_declara(self, db, mundo, sin_columnas_telefono):
        ana = mundo.conductor('Ana')
        # tres paradas que llegaron juntas al servidor (sincronización)
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                   paradas=[(_t(DIA, '12:00'), {}), (_t(DIA, '12:00'), {}),
                            (_t(DIA, '12:01'), {})])
        j = _jornada(ana)
        assert j['hora_del_telefono']['disponible'] is False
        assert j['hora_del_telefono']['rafagas_calculadas'] is False
        assert 'NO se calculan ráfagas' in j['hora_del_telefono']['declaracion']
        assert _senales(j, 'rafaga_de_confirmaciones') == []

    def test_con_columnas_la_parada_va_a_la_hora_del_telefono(
            self, db, mundo, columnas_telefono):
        ana = mundo.conductor('Ana')
        r = mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                       paradas=[(_t(DIA, '12:00'), {}), (_t(DIA, '12:00'), {}),
                                (_t(DIA, '12:01'), {})])
        # el teléfono dice 08:00, 09:00, 10:00: llegaron juntas por la cola
        _poner_telefono(db, r.id, [(_t(DIA, '08:00'), 0, True), (_t(DIA, '09:00'), 0, True),
                                   (_t(DIA, '10:00'), 0, True)])
        j = _jornada(ana)
        paradas = [e for e in j['eventos'] if e['tipo'] == 'parada']
        assert [p['hora'] for p in paradas] == ['08:00', '09:00', '10:00']
        assert {p['hora_fuente'] for p in paradas} == {'telefono'}
        assert {p['confianza'] for p in paradas} == {'alta'}
        assert paradas[0]['hora_servidor'] == '12:00'
        assert paradas[0]['fuente'] == 'recaudos_entrega.ts_dispositivo'
        assert j['hora_del_telefono']['disponible'] is True
        assert _senales(j, 'rafaga_de_confirmaciones') == []   # sincronizar no es ráfaga

    def test_el_reloj_corrido_se_corrige_y_baja_la_confianza(
            self, db, mundo, columnas_telefono):
        ana = mundo.conductor('Ana')
        r = mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                       paradas=[(_t(DIA, '09:00'), {})])
        # el teléfono iba 15 min adelantado: marcó 09:15 un hecho de las 09:00
        _poner_telefono(db, r.id, [(_t(DIA, '09:15'), 900, False)])
        p = [e for e in _jornada(ana)['eventos'] if e['tipo'] == 'parada'][0]
        assert p['hora'] == '09:00' and p['confianza'] == 'media'
        assert 'corrido 15 min' in p['confianza_motivo']

    def test_en_linea_sin_hora_del_telefono_la_del_servidor_vale(
            self, db, mundo, columnas_telefono):
        ana = mundo.conductor('Ana')
        r = mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                       paradas=[(_t(DIA, '09:00'), {})])
        _poner_telefono(db, r.id, [(None, None, False)])
        p = [e for e in _jornada(ana)['eventos'] if e['tipo'] == 'parada'][0]
        assert p['hora_fuente'] == 'servidor' and p['confianza'] == 'alta'

    def test_rafaga_de_confirmaciones_segun_el_telefono(self, db, mundo, columnas_telefono):
        ana = mundo.conductor('Ana')
        r = mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                       paradas=[(_t(DIA, '15:00'), {}), (_t(DIA, '15:00'), {}),
                                (_t(DIA, '15:00'), {}), (_t(DIA, '15:00'), {})])
        base = _t(DIA, '14:10')
        _poner_telefono(db, r.id, [(base, 0, False), (base + timedelta(seconds=30), 0, False),
                                   (base + timedelta(seconds=70), 0, False),
                                   (_t(DIA, '09:00'), 0, False)])
        s = _senales(_jornada(ana), 'rafaga_de_confirmaciones')
        assert len(s) == 1
        assert s[0]['comparacion']['valor'] == 3
        assert s[0]['comparacion']['distancia_max_m'] == 0.0   # mismo punto GPS


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Resumen del período
# ─────────────────────────────────────────────────────────────────────────────

class TestResumen:

    def test_la_fila_del_conductor_sale_de_la_misma_funcion_que_el_dia(self, db, mundo):
        _base_de_companeros(mundo, n=12, duracion_h=4)
        ana = _dia_de_ana(mundo)
        mundo.conductor('Quieto')                               # activo, sin nada
        mundo.conductor('Retirado', activo=False)               # inactivo, sin nada
        r = _svc().resumen(DIA - timedelta(days=6), DIA)
        filas = {f['conductor']['nombre']: f for f in r['conductores']}
        assert 'Retirado' not in filas
        assert filas['Quieto']['jornadas'] == 0
        fa = filas['Ana']
        dia = _jornada(ana)
        assert fa['jornadas'] == 1 and fa['reconstruidas'] == 1
        assert fa['no_explicado']['minutos'] == dia['jornada']['no_explicado_min']
        assert fa['senales_abiertas'] == len(dia['senales'])
        assert fa['dias'][0]['dia'] == DIA.isoformat()
        assert fa['cierre_cargue']['mediana'] == '07:30'
        assert fa['cierre_cargue']['pares_mediana'] == '07:00'
        assert fa['rutas']['sobre_p90'] == 1
        assert fa['paradas']['propias'] == 4
        assert set(fa['rechazos']) == set(_svc().MOTIVOS_VIGILADOS)
        # Beto tiene 6 jornadas en la semana sin turno registrado: se ve el
        # tramo entre paradas, no la jornada
        assert filas['Beto']['jornadas'] == 6
        assert filas['Beto']['parciales'] == 6 and filas['Beto']['reconstruidas'] == 0
        assert filas['Beto']['horas_jornada']['mediana'] is None
        assert filas['Beto']['no_explicado']['minutos'] == 0

    def test_declara_lo_que_no_puede_ver_y_el_aviso_legal(self, db, mundo):
        r = _svc().resumen(DIA, DIA)
        assert any('GPS del vehículo' in x for x in r['no_puede_ver'])
        assert 'Ley 1581' in r['aviso_legal'] and 'POR ESCRITO' in r['aviso_legal']
        assert r['umbrales']['n_minimo_pares'] == 10


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Endpoints: permisos y validación
# ─────────────────────────────────────────────────────────────────────────────

def _cab(app, db, rol):
    from app.models.usuario import Usuario
    u = Usuario.query.filter_by(email=f'{rol}-jorn@test.com').first()
    if u is None:
        u = Usuario(email=f'{rol}-jorn@test.com', nombre=rol, rol=rol, activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


ROLES_ADENTRO = ('admin', 'supervisor', 'jefe_almacen', 'gerente', 'control_flota')
ROLES_AFUERA = ('conductor', 'operario', 'empacador', 'tienda', 'compras', 'recepcionista',
                'picker_traslado', 'packer_traslado')


class TestEndpoints:

    def test_los_roles_son_exactamente_vista_flota(self):
        from app.routes._auth_helpers import Roles
        from app.routes.jornada import ROLES_JORNADA
        assert ROLES_JORNADA is Roles.VISTA_FLOTA
        assert set(ROLES_ADENTRO) == set(Roles.VISTA_FLOTA)
        assert 'conductor' not in ROLES_JORNADA

    @pytest.mark.parametrize('rol', ROLES_ADENTRO)
    def test_gestion_y_control_de_flota_entran(self, app, db, client, mundo, rol):
        ana = mundo.conductor('Ana')
        cab = _cab(app, db, rol)
        r = client.get(f'/api/jornada?dia={DIA}&conductor_id={ana.id}', headers=cab)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['conductor']['nombre'] == 'Ana'
        assert client.get('/api/jornada/resumen', headers=cab).status_code == 200

    @pytest.mark.parametrize('rol', ROLES_AFUERA)
    def test_los_demas_roles_no(self, app, db, client, mundo, rol):
        ana = mundo.conductor('Ana')
        cab = _cab(app, db, rol)
        assert client.get(f'/api/jornada?dia={DIA}&conductor_id={ana.id}',
                          headers=cab).status_code == 403
        assert client.get('/api/jornada/resumen', headers=cab).status_code == 403

    def test_sin_sesion_no_entra(self, client):
        assert client.get('/api/jornada/resumen').status_code == 401
        assert client.get('/api/jornada?conductor_id=1').status_code == 401

    @pytest.mark.parametrize('qs', [
        'conductor_id=1&dia=ayer', 'conductor_id=1&dia=2026-13-01',
        'conductor_id=x', 'conductor_id=-1', 'dia=2026-09-15',
        'conductor_id=1&dia=2999-01-01', 'conductor_id=1&dia=1999-01-01',
        'conductor_id=99999999999999'])
    def test_basura_en_el_dia_es_400(self, app, db, client, qs):
        r = client.get(f'/api/jornada?{qs}', headers=_cab(app, db, 'admin'))
        assert r.status_code == 400, qs
        assert r.get_json()['error']

    def test_conductor_inexistente_es_404(self, app, db, client):
        r = client.get(f'/api/jornada?dia={DIA}&conductor_id=424242',
                       headers=_cab(app, db, 'admin'))
        assert r.status_code == 404

    @pytest.mark.parametrize('qs', [
        'desde=2026-09-20&hasta=2026-09-10', 'desde=hoy', 'hasta=2026-02-30',
        'desde=2026-01-01&hasta=2026-04-03', 'hasta=2999-01-01'])
    def test_basura_en_el_rango_es_400(self, app, db, client, qs):
        r = client.get(f'/api/jornada/resumen?{qs}', headers=_cab(app, db, 'admin'))
        assert r.status_code == 400, qs

    def test_noventa_y_dos_dias_si(self, app, db, client):
        hasta = DIA
        desde = hasta - timedelta(days=91)
        r = client.get(f'/api/jornada/resumen?desde={desde}&hasta={hasta}',
                       headers=_cab(app, db, 'admin'))
        assert r.status_code == 200
        assert r.get_json()['dias'] == 92
        desde = hasta - timedelta(days=92)
        r = client.get(f'/api/jornada/resumen?desde={desde}&hasta={hasta}',
                       headers=_cab(app, db, 'admin'))
        assert r.status_code == 400

    def test_sin_rango_son_los_ultimos_siete_dias(self, app, db, client):
        from app.utils.fecha import dia_operativo
        d = client.get('/api/jornada/resumen', headers=_cab(app, db, 'admin')).get_json()
        assert d['hasta'] == dia_operativo().isoformat() and d['dias'] == 7


# ─────────────────────────────────────────────────────────────────────────────
# 7 · Trinquetes: vocabulario, umbrales, forma de las señales
# ─────────────────────────────────────────────────────────────────────────────

#: Palabras que esta vista no puede decir de una persona. «Muerto» afirma lo
#: que el dato no sabe; «sanción», «castigo», «culpa», «infracción» convierten
#: una pregunta en un veredicto (regla 2 de `flota/CLAUDE.md`).
_PROHIBIDAS = re.compile(r'muert|sanci[oó]n|sancion|castig|culpab|culpa\b|infracci', re.I)


def _cadenas_de_codigo(fuente: str):
    """Constantes de texto de un módulo Python, SIN docstrings (que explican
    por qué no se dice, y tienen que poder nombrarlo)."""
    import ast
    arbol = ast.parse(fuente)
    docs = set()
    for n in ast.walk(arbol):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if n.body and isinstance(n.body[0], ast.Expr) and \
                    isinstance(getattr(n.body[0], 'value', None), ast.Constant):
                docs.add(id(n.body[0].value))
    return [n.value for n in ast.walk(arbol)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def _cadenas_js(fuente: str):
    """Literales de texto del JS, sin comentarios."""
    from tests.test_frontend_integrity import _sin_comentarios
    limpio = _sin_comentarios(fuente)
    return re.findall(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`", limpio)


class TestVocabulario:

    def test_el_servicio_y_la_ruta_no_dicen_muerto_ni_sancion(self):
        malas = [s for f in (SERVICIO, RUTA)
                 for s in _cadenas_de_codigo(f.read_text(encoding='utf-8'))
                 if _PROHIBIDAS.search(s)]
        assert not malas, malas

    def test_la_pantalla_tampoco(self):
        malas = [s for s in _cadenas_js((PWA / 'flota_jornada.js').read_text(encoding='utf-8'))
                 if _PROHIBIDAS.search(s)]
        assert not malas, malas

    def test_el_detector_ve_una_palabra_prohibida(self):
        assert _PROHIBIDAS.search('3 h de tiempo muerto')
        assert _PROHIBIDAS.search('Sanción sugerida')
        assert [s for s in _cadenas_de_codigo('x = "tiempo muerto"') if _PROHIBIDAS.search(s)]
        assert not [s for s in _cadenas_de_codigo('def f():\n    """nunca muerto"""\n')
                    if _PROHIBIDAS.search(s)]
        assert [s for s in _cadenas_js("const a = `<b>tiempo muerto</b>`;")
                if _PROHIBIDAS.search(s)]
        assert not [s for s in _cadenas_js('// tiempo muerto\nconst a = 1;')
                    if _PROHIBIDAS.search(s)]

    def test_el_escaner_ve_texto(self):
        """Piso: si el escáner se rompe devuelve cero y parece verde."""
        assert len(_cadenas_de_codigo(SERVICIO.read_text(encoding='utf-8'))) >= 150
        assert len(_cadenas_js((PWA / 'flota_jornada.js').read_text(encoding='utf-8'))) >= 60


class TestUmbralesEnUnSoloSitio:

    def _accesos(self):
        import ast
        arbol = ast.parse(SERVICIO.read_text(encoding='utf-8'))
        claves = []
        for n in ast.walk(arbol):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and \
                    isinstance(n.slice.value, str):
                v = n.value
                es_u = (isinstance(v, ast.Name) and v.id == 'U') or \
                       (isinstance(v, ast.Attribute) and v.attr == 'U')
                if es_u:
                    claves.append(n.slice.value)
        return claves

    def test_todo_umbral_leido_existe(self):
        leidas = set(self._accesos())
        assert leidas <= set(_svc().UMBRALES_POR_DEFECTO), \
            leidas - set(_svc().UMBRALES_POR_DEFECTO)

    def test_ningun_umbral_declarado_esta_muerto(self):
        muertos = set(_svc().UMBRALES_POR_DEFECTO) - set(self._accesos())
        assert not muertos, f'umbrales declarados que nadie lee: {muertos}'

    def test_piso_de_lecturas(self):
        assert len(self._accesos()) >= 20

    def test_un_ajuste_invalido_no_se_aplica(self, monkeypatch):
        monkeypatch.setenv('JORNADA_UMBRALES', '{"n_minimo_pares": "diez", "x": 1}')
        u = _svc().umbrales()
        assert u['n_minimo_pares'] == 10
        assert set(u['_rechazados']) == {'n_minimo_pares', 'x'}
        monkeypatch.setenv('JORNADA_UMBRALES', 'no es json')
        assert _svc().umbrales()['_rechazados'] == {'JORNADA_UMBRALES': 'no es JSON'}


class TestFormaDeLoQueSeEntrega:
    """Toda señal lleva evidencia, comparación con su base y qué hacer; todo
    evento, su fuente y su confianza. Se mira sobre un mundo que las dispara."""

    def _todo(self, mundo, monkeypatch):
        _base_de_companeros(mundo, n=12, duracion_h=4, motivo_cada=(18, 'CLIENTE_CERRADO'))
        ana = _dia_de_ana(mundo)
        beto = mundo.conductores['Beto']
        ayer = DIA - timedelta(days=1)
        mundo.recibe(beto, 'JOR003', _t(ayer, '06:00'), 1000)
        mundo.entrega(beto, 'JOR003', _t(ayer, '18:00'), 1100)
        mundo.recibe(ana, 'JOR003', _t(DIA, '16:00'), 1180)
        mundo.inspeccion(ana, 'JOR003', _t(DIA, '16:05'), 1180, segundos=20)
        return _jornada(ana)

    def test_toda_senal_trae_evidencia_base_y_que_hacer(self, db, mundo, monkeypatch):
        j = self._todo(mundo, monkeypatch)
        claves = {s['clave'] for s in j['senales']}
        assert {'tiempo_no_explicado', 'duracion_ruta', 'km_entre_turnos',
                'preoperacional_rapido'} <= claves
        for s in j['senales']:
            assert s['evidencia'], s['clave']
            assert 'base' in s['comparacion'], s['clave']
            assert s['nivel'] in ('revisar', 'observa')
            assert s['confianza'] in ('alta', 'media', 'baja')
            assert 'no concluye' in s['que_hacer']
            assert s['texto'] and not _PROHIBIDAS.search(s['texto'])
            json.dumps(s)

    def test_todo_evento_trae_fuente_y_confianza(self, db, mundo, monkeypatch):
        j = self._todo(mundo, monkeypatch)
        for e in j['eventos']:
            assert e['tipo'] in _svc().TITULOS
            assert '.' in e['fuente'], e
            assert e['hora_fuente'] in ('telefono', 'servidor')
            assert e['confianza'] in ('alta', 'media', 'baja')
            assert e['confianza_motivo']

    def test_las_fuentes_de_eventos_son_el_punto_de_extension(self):
        nombres = [f.__name__ for f in _svc().FUENTES_DE_EVENTOS]
        assert nombres == ['_eventos_de_turno', '_eventos_de_inspeccion',
                           '_eventos_de_ruta', '_eventos_de_tanqueo']


class TestNoReimplementaLasDelVehiculo:
    """Las señales de VEHÍCULO (km en días sin ruta, custodio distinto del
    conductor de la ruta, galones) son de la bandeja del encargado."""

    def test_ninguna_senal_de_vehiculo(self):
        claves = ' '.join(f.__name__ for f in _svc().SENALES)
        for prohibida in ('galon', 'sin_ruta', 'custodio_distinto', 'rendimiento'):
            assert prohibida not in claves


# ─────────────────────────────────────────────────────────────────────────────
# 8 · La pantalla, ejecutada en Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const G = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
function elemento(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {}, scrollIntoView() {} };
}
const els = {};
const doc = { body: elemento('body'),
  getElementById(id) { if (!(id in els)) els[id] = elemento(id); return els[id]; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, createElement(t) { return elemento(t); } };
const pedidas = [];
const ctx = { console, document: doc, window: { addEventListener() {} },
  setTimeout, clearTimeout,
  get: async (url) => {
    pedidas.push(url);
    if (G.revienta) throw new Error(G.revienta);
    for (const [trozo, p] of Object.entries(G.rutas)) if (String(url).includes(trozo)) return p;
    throw new Error('ruta no sembrada: ' + url);
  } };
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of G.archivos) vm.runInContext(fs.readFileSync(a, 'utf-8'), ctx, { filename: a });
(async () => {
  const cont = doc.getElementById('fj');
  await ctx.flotaJornadaCargar(cont);
  const resumen = cont.innerHTML;
  let conductor = '', dia = '';
  if (G.abrir) {
    ctx.fjAbrirConductor(G.abrir[0]);
    conductor = cont.innerHTML;
    await ctx.fjAbrirDia(G.abrir[0], G.abrir[1]);
    dia = cont.innerHTML;
  }
  process.stdout.write(JSON.stringify({ resumen, conductor, dia, pedidas }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _render(tmp_path, rutas, abrir=None, revienta=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'archivos': [str(PWA / 'util.js'), str(PWA / 'flota_jornada.js')],
                             'rutas': rutas, 'abrir': abrir, 'revienta': revienta}),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(g)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


_MALO = '<img src=x onerror="alert(1)">'


class TestLaPantalla:

    def _payloads(self, mundo, monkeypatch):
        # con las bases bajadas (la semana tiene 6 rutas de Beto y 5 paradas de
        # Ana), el resumen compara: es el caso «… sus compañeros: 0,3» del dueño
        monkeypatch.setenv('JORNADA_UMBRALES', json.dumps(
            {'n_minimo_conductor': 3, 'n_minimo_pares': 5}))
        _base_de_companeros(mundo, n=12, duracion_h=4, motivo_cada=(18, 'CLIENTE_CERRADO'))
        ana = mundo.conductor('Ana')
        ana.nombre = f'Ana {_MALO}'
        mundo.db.session.commit()
        _dia_de_ana(mundo)
        # un rechazo con cliente malicioso
        cerrado = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'CLIENTE_CERRADO',
                   'observaciones': _MALO, 'forma_pago': None}
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:30'), placa='JOR001',
                   paradas=[(_t(DIA, '13:50'), cerrado)], clientes=[_MALO])
        res = json.loads(json.dumps(_svc().resumen(DIA - timedelta(days=6), DIA)))
        i = next(k for k, f in enumerate(res['conductores']) if f['conductor']['id'] == ana.id)
        dias = res['conductores'][i]['dias']
        jd = next(k for k, d in enumerate(dias) if d['dia'] == DIA.isoformat())
        dia = json.loads(json.dumps(_jornada(ana)))
        return res, dia, (i, jd)

    def test_resumen_legible_para_un_jefe_de_bodega(self, db, mundo, tmp_path, monkeypatch):
        res, dia, abrir = self._payloads(mundo, monkeypatch)
        out = _render(tmp_path, {'/api/jornada/resumen': res, '/api/jornada?': dia},
                      abrir=abrir)
        r = out['resumen']
        assert 'Cerró cargue 07:30' in r and 'su ruta suele 07:00' in r
        assert 'entregas' in r and 'sin explicar' in r
        assert '1 rechazo por cliente cerrado, sus compañeros en esa ruta: 0,3' in r
        assert _MALO not in r and '&lt;img' in r
        assert out['pedidas'][0].startswith('/api/jornada/resumen')

    def test_la_linea_de_tiempo_con_huecos_confianza_y_senales(self, db, mundo, tmp_path,
                                                            monkeypatch):
        res, dia, abrir = self._payloads(mundo, monkeypatch)
        out = _render(tmp_path, {'/api/jornada/resumen': res, '/api/jornada?': dia},
                      abrir=abrir)
        d = out['dia']
        assert '06:30' in d and 'Recibió el vehículo' in d
        assert 'hora del servidor' in d
        assert 'sin explicar' in d                     # el hueco resaltado
        assert 'Tiempo que ningún registro explica' in d
        assert 'no concluye' in d
        assert 'Ley 1581' in d
        assert 'GPS del vehículo' in d
        assert _MALO not in d and '&lt;img' in d
        assert f'conductor_id={dia["conductor"]["id"]}' in out['pedidas'][-1]
        assert f'dia={DIA.isoformat()}' in out['pedidas'][-1]
        # los botones pasan posiciones, nunca datos
        for oc in re.findall(r'onclick="([^"]*)"', d + out['resumen'] + out['conductor']):
            assert re.fullmatch(r'[A-Za-z]\w*\(([\d\s,-]*)\)(;\s*return false;?)?', oc), oc

    def test_sin_hora_del_telefono_la_pantalla_lo_dice(self, db, mundo, tmp_path, monkeypatch,
                                                        sin_columnas_telefono):
        res, dia, abrir = self._payloads(mundo, monkeypatch)
        out = _render(tmp_path, {'/api/jornada/resumen': res, '/api/jornada?': dia},
                      abrir=abrir)
        assert 'no se calculan ráfagas' in out['resumen'].lower()

    def test_una_jornada_no_reconstruible_no_pinta_ceros(self, db, mundo, tmp_path):
        ana = mundo.conductor('Ana')
        mundo.ruta(ana, mundo.norte, cierre=_t(DIA, '07:00'), placa='JOR001',
                   paradas=[(_t(DIA, '08:00'), {})])
        res = json.loads(json.dumps(_svc().resumen(DIA, DIA)))
        i = next(k for k, f in enumerate(res['conductores']) if f['conductor']['id'] == ana.id)
        dia = json.loads(json.dumps(_jornada(ana)))
        out = _render(tmp_path, {'/api/jornada/resumen': res, '/api/jornada?': dia},
                      abrir=(i, 0))
        assert 'no reconstruible' in out['resumen'].lower()
        assert 'no se puede reconstruir' in out['dia'].lower()
        assert '0 min sin explicar' not in out['dia']

    def test_si_el_servidor_falla_lo_dice(self, db, mundo, tmp_path):
        out = _render(tmp_path, {}, revienta='sin red')
        assert 'sin red' in out['resumen']

"""Lo que la ley prohíbe y se sabe no sale con cualquier texto (2026-09-27).

La clase: **un vencimiento legal conocido se reconocía igual que un dato
ausente**. El despacho aceptaba cualquier motivo no vacío para sacar un camión
con el SOAT o la revisión técnico-mecánica vencidos, y el muelle lo mostraba en
el mismo cuadro que un papel sin cargar: el jefe aprendía a escribir «ok» para
lo segundo y ese «ok» sacaba lo primero (hallazgo P0-1 de la noche de flota).

Este archivo tiene:

1. la política (`flota/dominio/salida.py`): qué bloquea, quién autoriza, en qué
   cuadro va cada motivo, y las fechas imposibles;
2. el servicio por HTTP con roles reales: el jefe no autoriza, el admin sí con
   un motivo largo, un FORZAR viejo no cuenta, lo que no se sabe sigue siendo un
   motivo simple;
3. la licencia de conducción y los documentos (lo que entra y lo ya cargado);
4. los trinquetes por AST, con inventario que solo encoge, meta-tests y piso:
   - todo escritor de un FORZAR de flota pasa por la autorización;
   - toda construcción de `Hechos` pasa la licencia (salvo la bandeja, declarada);
   - toda escritura de fechas de un papel o de una licencia pasa por su política.
"""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from flota.dominio import salida as dom
from tests.test_fugas_ruta import _ruta, flota_sana  # noqa: F401

RAIZ = Path(__file__).resolve().parents[2]
D = date(2026, 9, 27)


def _hechos(**kw):
    base = dict(
        hoy=D,
        papeles=tuple(dom.Papel(t, dom.VIGENTE, D + timedelta(days=200))
                      for t in dom.NOMBRE_PAPEL),
        danos_bloqueantes=0, danos_vencidos=0, danos_en_plazo=0,
        inspeccion='apta', sale_hoy=True, preventivo_vencidas=(),
        preventivo_por_vencer=0, ot_abiertas=0, km_conocido=True, km_dudoso=False,
        custodia='conductor', custodio_conductor_id=7, conductor_de_la_ruta=7,
        fuera_de_sede=False, ficha='completa', ficha_falta=(),
        licencia=dom.Licencia(dom.VIGENTE, D + timedelta(days=400)))
    base.update(kw)
    return dom.Hechos(**base)


def _papeles(**estados):
    out = []
    for t in dom.NOMBRE_PAPEL:
        if t in estados:
            estado, vence = estados[t]
            out.append(dom.Papel(t, estado, vence))
        else:
            out.append(dom.Papel(t, dom.VIGENTE, D + timedelta(days=200)))
    return tuple(out)


# ═════════════════════════════════════════════════════════════════════════
# 1 · La política
# ═════════════════════════════════════════════════════════════════════════

class TestQueBloquea:

    @pytest.mark.parametrize('kw,clave', [
        ({'papeles': _papeles(soat=(dom.VENCIDO, D - timedelta(days=3)))}, 'soat_vencido'),
        ({'papeles': _papeles(rtm=(dom.VENCIDO, D - timedelta(days=320)))}, 'rtm_vencido'),
        ({'licencia': dom.Licencia(dom.VENCIDO, D - timedelta(days=1))}, 'licencia_vencida'),
    ])
    def test_lo_vencido_que_la_ley_exige_bloquea(self, kw, clave):
        m = next(m for m in dom.evaluar(_hechos(**kw)).motivos if m.clave == clave)
        assert (m.nivel, m.frena, m.bloquea, m.gravedad) == (
            dom.ROJO, True, True, dom.PROHIBIDO)
        assert m in dom.evaluar(_hechos(**kw)).para_despachar()

    @pytest.mark.parametrize('kw,clave', [
        ({'papeles': _papeles(soat=(dom.SIN_CARGAR, None))}, 'soat_sin_registro'),
        ({'papeles': _papeles(rtm=(dom.NO_ENCONTRADO, None))}, 'rtm_no_encontrado'),
        ({'papeles': _papeles(rtm=(dom.DATO_A_CORREGIR, D - timedelta(days=320)))},
         'rtm_dato_a_corregir'),
        ({'licencia': dom.Licencia(dom.SIN_CARGAR)}, 'licencia_sin_cargar'),
    ])
    def test_lo_que_no_se_sabe_frena_pero_no_bloquea(self, kw, clave):
        m = next(m for m in dom.evaluar(_hechos(**kw)).motivos if m.clave == clave)
        assert m.frena and not m.bloquea
        assert m.gravedad == dom.NO_SE_SABE

    def test_lo_que_se_sabe_y_no_es_ilegal_es_una_advertencia(self):
        m = next(m for m in dom.evaluar(_hechos(ot_abiertas=1)).motivos
                 if m.clave == 'en_taller')
        assert (m.bloquea, m.gravedad) == (False, dom.ADVERTENCIA)

    def test_la_poliza_vencida_no_bloquea(self):
        """Solo lo que está en BLOQUEAN_SALIDA: la póliza de RC la exige el
        negocio, no la ley para circular."""
        ev = dom.evaluar(_hechos(papeles=_papeles(
            poliza_rc=(dom.VENCIDO, D - timedelta(days=2)))))
        assert not any(m.bloquea for m in ev.motivos)

    def test_cada_clave_que_bloquea_la_produce_la_politica(self):
        """Meta: una clave en BLOQUEAN_SALIDA que la política no escribe nunca
        sería un bloqueo muerto — el defecto que se está cerrando."""
        vistas = set()
        for kw in ({'papeles': _papeles(soat=(dom.VENCIDO, D - timedelta(days=1)),
                                        rtm=(dom.VENCIDO, D - timedelta(days=1)))},
                   {'licencia': dom.Licencia(dom.VENCIDO, D - timedelta(days=1))}):
            vistas |= {m.clave for m in dom.evaluar(_hechos(**kw)).motivos if m.bloquea}
        assert vistas == set(dom.BLOQUEAN_SALIDA)

    def test_a_dict_publica_bloquea_y_gravedad(self):
        m = dom.motivo_papel(dom.Papel('soat', dom.VENCIDO, D - timedelta(days=1)), D)
        assert m.a_dict()['bloquea'] is True and m.a_dict()['gravedad'] == dom.PROHIBIDO


class TestQuienAutoriza:

    def test_solo_el_rol_de_la_politica(self):
        assert dom.ROLES_AUTORIZAN_SALIDA_PROHIBIDA == ('admin',)
        for rol in ('jefe_almacen', 'supervisor', 'gerente', 'control_flota', None):
            porque = dom.puede_autorizar_salida_prohibida(rol, 'x' * 100)
            assert porque and 'Solo un usuario' in porque

    def test_el_motivo_corto_no_alcanza(self):
        corto = 'ok ' * 3
        assert 'al menos 20' in dom.puede_autorizar_salida_prohibida('admin', corto)
        assert dom.puede_autorizar_salida_prohibida('admin', 'Traslado a CDA para renovar RTM hoy') is None

    def test_espacios_no_cuentan(self):
        assert dom.puede_autorizar_salida_prohibida('admin', ' ' * 40 + 'ok') is not None


class TestFechasImposibles:

    def test_la_rtm_de_bdt261_vence_el_dia_que_se_expidio(self):
        f = date(2025, 11, 11)
        p = dom.fechas_imposibles(f, f, D)
        assert p and 'igual o anterior' in p[0]

    def test_el_anio_0025_de_la_poliza_de_tgz653(self):
        p = dom.fechas_imposibles(date(25, 3, 1), date(2027, 3, 1), D)
        assert p and '0025' in p[0]

    def test_la_expedicion_en_el_futuro_y_el_vencimiento_lejano(self):
        assert dom.fechas_imposibles(D + timedelta(days=3), None, D)
        assert dom.fechas_imposibles(None, D.replace(year=D.year + 16), D)
        assert not dom.fechas_imposibles(D - timedelta(days=10), D.replace(year=D.year + 14), D)

    def test_el_soat_de_otra_duracion_se_rechaza_al_entrar_pero_no_al_leer(self):
        exp, venc = D - timedelta(days=100), D + timedelta(days=20)
        assert dom.problemas_de_fechas('soat', exp, venc, D)
        assert not dom.fechas_imposibles(exp, venc, D)
        assert not dom.problemas_de_fechas('rtm', exp, venc, D)
        assert not dom.problemas_de_fechas('soat', D - timedelta(days=345), D + timedelta(days=20), D)

    def test_la_licencia_se_carga_entera_o_no_se_carga(self):
        assert dom.problemas_de_licencia('', '', None, D) == []
        assert dom.problemas_de_licencia('123', '', None, D)
        assert dom.problemas_de_licencia('123', 'Z9', D + timedelta(days=10), D)
        assert dom.problemas_de_licencia('123', 'C2', date(25, 1, 1), D)
        assert dom.problemas_de_licencia('123', 'c2', D + timedelta(days=10), D) == []

    def test_estado_de_licencia(self):
        assert dom.estado_de_licencia(numero=None, categoria=None, vence=None, hoy=D).estado == dom.SIN_CARGAR
        assert dom.estado_de_licencia(numero='1', categoria='C1', vence=D - timedelta(days=1),
                                      hoy=D).estado == dom.VENCIDO
        assert dom.estado_de_licencia(numero='1', categoria='C1', vence=date(25, 1, 1),
                                      hoy=D).estado == dom.DATO_A_CORREGIR


class TestLoYaCargado:

    def _fila(self, tipo, exp, venc, estado='vigente'):
        return SimpleNamespace(tipo=tipo, estado=estado, fecha_expedicion=exp,
                               fecha_vencimiento=venc)

    def test_la_rtm_imposible_es_dato_a_corregir_y_no_bloquea(self):
        from flota.adaptadores.salida import papeles_de_filas
        f = date(2025, 11, 11)
        [rtm] = [p for p in papeles_de_filas([self._fila('rtm', f, f)], D) if p.tipo == 'rtm']
        assert rtm.estado == dom.DATO_A_CORREGIR and 'igual o anterior' in rtm.problema
        m = dom.motivo_papel(rtm, D)
        assert (m.clave, m.bloquea, m.frena) == ('rtm_dato_a_corregir', False, True)
        assert m.texto.startswith('Revisión técnico-mecánica: dato a corregir.')

    def test_la_bandeja_la_muestra_como_dato_a_corregir(self, app, client, db,
                                                        usuario_admin):
        """La RTM de BDT261 en Pendientes: «dato a corregir», con su consejo, y
        no «vencida hace 320 días»."""
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import DocumentoVehiculo
        v = Vehiculo(placa='BDT261', tipo='Camión', activo=True)
        db.session.add(v)
        db.session.flush()
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        f = date(2025, 11, 11)
        db.session.add(DocumentoVehiculo(vehiculo_id=v.id, tipo='rtm', numero='R', entidad='CDA',
                                         fecha_expedicion=f, fecha_vencimiento=f))
        for t, venc in (('soat', hoy + timedelta(days=200)), ('poliza_rc', hoy + timedelta(days=200)),
                        ('tarjeta_propiedad', None)):
            db.session.add(DocumentoVehiculo(vehiculo_id=v.id, tipo=t, numero='N', entidad='E',
                                             fecha_expedicion=hoy - timedelta(days=165),
                                             fecha_vencimiento=venc))
        db.session.commit()
        r = client.get('/flota/bandeja', headers=_token(app, usuario_admin))
        assert r.status_code == 200, r.get_json()
        [p] = [p for p in r.get_json()['pendientes']
               if p['placa'] == 'BDT261' and p['clase'] == 'documento']
        assert 'Revisión técnico-mecánica: dato a corregir' in p['texto']
        assert 'vencida' not in p['texto']
        assert p['detalle'].startswith('Dato a corregir')

    def test_una_fila_basura_no_esconde_un_vencimiento_conocido(self):
        from flota.adaptadores.salida import papeles_de_filas
        filas = [self._fila('soat', date(2025, 1, 1), D - timedelta(days=5)),
                 self._fila('soat', date(2025, 11, 11), date(2025, 11, 11))]
        [soat] = [p for p in papeles_de_filas(filas, D) if p.tipo == 'soat']
        assert soat.estado == dom.VENCIDO


# ═════════════════════════════════════════════════════════════════════════
# 2 · El servicio, por HTTP con roles reales
# ═════════════════════════════════════════════════════════════════════════

def _usuario(db, rol, almacen):
    from app.models.usuario import Usuario
    u = Usuario(nombre=f'U {rol}', email=f'{rol}@prohibida.test', password_hash='x',
                rol=rol, almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _token(app, u):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


@pytest.fixture
def ruta_con_soat_vencido(db, almacen, flota_sana):  # noqa: F811
    from app.utils.fecha import dia_operativo
    from flota.adaptadores.modelos import DocumentoVehiculo
    v, estado = flota_sana
    ruta, _t, c = _ruta(db, almacen, estado='EN_CARGUE', vehiculo_id=v.id)
    estado['custodio'] = c.id
    c.licencia_numero, c.licencia_categoria = 'LIC-1', 'C2'
    c.licencia_vence = dia_operativo() + timedelta(days=400)
    d = DocumentoVehiculo.query.filter_by(vehiculo_id=v.id, tipo='soat').one()
    d.fecha_vencimiento = dia_operativo() - timedelta(days=2)
    db.session.commit()
    return ruta, c


_MOTIVO_LARGO = 'Va directo al CDA a renovar, autorizado por gerencia'


class TestElDespachoConLoProhibido:

    def test_el_jefe_no_lo_saca_ni_con_un_motivo_largo(self, app, client, db, almacen,
                                                     ruta_con_soat_vencido):
        ruta, _c = ruta_con_soat_vencido
        h = _token(app, _usuario(db, 'jefe_almacen', almacen))
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                        json={'motivo_advertencias': _MOTIVO_LARGO})
        assert r.status_code == 409
        d = r.get_json()
        assert d['salida_prohibida'] is True and d['puede_autorizar'] is False
        assert d['quien_autoriza'] == ['admin'] and d['motivo_minimo'] == 20
        [a] = [a for a in d['advertencias_flota'] if a['clave'] == 'soat_vencido']
        assert a['bloquea'] is True and a['gravedad'] == 'prohibido'
        assert ruta.estado == 'EN_CARGUE'

    def test_el_admin_con_motivo_corto_tampoco(self, app, client, db, almacen,
                                              ruta_con_soat_vencido, usuario_admin):
        ruta, _c = ruta_con_soat_vencido
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=_token(app, usuario_admin),
                        json={'motivo_advertencias': 'ok'})
        assert r.status_code == 409
        d = r.get_json()
        assert d['puede_autorizar'] is True and 'al menos 20' in d['error']

    def test_el_admin_con_motivo_largo_sale_y_queda_escrito(self, app, client, db, almacen,
                                                           ruta_con_soat_vencido, usuario_admin):
        from app.models.bitacora import BitacoraAccion
        from app.services.senales_ruta import despachos_forzados
        ruta, _c = ruta_con_soat_vencido
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=_token(app, usuario_admin),
                        json={'motivo_advertencias': _MOTIVO_LARGO})
        assert r.status_code == 200, r.get_json()
        b = BitacoraAccion.query.filter_by(accion='FORZAR', entidad='RutaDespacho',
                                           entidad_id=ruta.id).one()
        assert b.usuario_id == usuario_admin.id and b.motivo == _MOTIVO_LARGO
        assert b.despues['salida_prohibida_autorizada'] == ['soat_vencido']
        [f] = [f for f in despachos_forzados() if f['ruta_id'] == ruta.id]
        assert f['salida_prohibida'] == ['soat_vencido']

    def test_un_forzar_viejo_sin_autorizacion_no_cuenta(self, app, db, almacen,
                                                        ruta_con_soat_vencido):
        """Un «ok» del jefe escrito antes de esta regla (el FORZAR reconoce la
        clave sin `salida_prohibida_autorizada`) no saca el camión."""
        from app.services.bitacora import FORZADO_ADVERTENCIAS_FLOTA, registrar_accion
        from app.services.ruta_service import RutaService, SalidaProhibida
        ruta, _c = ruta_con_soat_vencido
        jefe = _usuario(db, 'jefe_almacen', almacen)
        registrar_accion('FORZAR', ruta, usuario_id=jefe.id, motivo='ok',
                         despues={'forzado': FORZADO_ADVERTENCIAS_FLOTA,
                                  'momento': 'iniciar_cargue',
                                  'advertencias_flota': ['soat_vencido'], 'detalle': []})
        db.session.commit()
        with pytest.raises(SalidaProhibida):
            RutaService.cerrar_ruta(ruta.id, motivo_advertencias='ok otra vez',
                                    usuario_id=jefe.id)

    def test_lo_autorizado_al_iniciar_no_se_pide_otra_vez(self, app, db, almacen,
                                                          flota_sana, usuario_admin):  # noqa: F811
        from app.utils.fecha import dia_operativo
        from app.services.ruta_service import RutaService
        from flota.adaptadores.modelos import DocumentoVehiculo
        v, estado = flota_sana
        ruta, _t, c = _ruta(db, almacen, estado='PROGRAMADO', vehiculo_id=v.id)
        estado['custodio'] = c.id
        c.licencia_numero, c.licencia_categoria = 'L', 'C1'
        c.licencia_vence = dia_operativo() + timedelta(days=300)
        DocumentoVehiculo.query.filter_by(vehiculo_id=v.id, tipo='rtm').one() \
            .fecha_vencimiento = dia_operativo() - timedelta(days=1)
        db.session.commit()
        RutaService.iniciar_ruta(ruta.id, motivo_advertencias=_MOTIVO_LARGO,
                                 usuario_id=usuario_admin.id)
        jefe = _usuario(db, 'jefe_almacen', almacen)
        RutaService.cerrar_ruta(ruta.id, usuario_id=jefe.id)   # ya autorizado
        assert ruta.estado == 'EN_TRANSITO'

    def test_lo_que_no_se_sabe_lo_reconoce_el_jefe_con_motivo(self, app, client, db, almacen,
                                                             flota_sana):  # noqa: F811
        """El papel sin cargar sigue siendo «informa»: motivo simple, sin admin."""
        from app.utils.fecha import dia_operativo
        from flota.adaptadores.modelos import DocumentoVehiculo
        v, estado = flota_sana
        ruta, _t, c = _ruta(db, almacen, estado='EN_CARGUE', vehiculo_id=v.id)
        estado['custodio'] = c.id
        c.licencia_numero, c.licencia_categoria = 'L', 'C1'
        c.licencia_vence = dia_operativo() + timedelta(days=300)
        DocumentoVehiculo.query.filter_by(vehiculo_id=v.id, tipo='soat').delete()
        db.session.commit()
        h = _token(app, _usuario(db, 'jefe_almacen', almacen))
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h, json={})
        d = r.get_json()
        assert r.status_code == 409 and d['salida_prohibida'] is False
        [a] = [a for a in d['advertencias_flota'] if a['clave'] == 'soat_sin_registro']
        assert a['gravedad'] == 'no_se_sabe' and a['bloquea'] is False
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                        json={'motivo_advertencias': 'lo buscamos mañana'})
        assert r.status_code == 200

    def test_la_licencia_vencida_del_conductor_bloquea(self, app, db, almacen,
                                                       flota_sana):  # noqa: F811
        from app.services import senales_ruta as sr
        from app.utils.fecha import dia_operativo
        v, estado = flota_sana
        ruta, _t, c = _ruta(db, almacen, estado='EN_CARGUE', vehiculo_id=v.id)
        estado['custodio'] = c.id
        c.licencia_numero, c.licencia_categoria = 'L', 'C2'
        c.licencia_vence = dia_operativo() - timedelta(days=4)
        db.session.commit()
        [a] = sr.advertencias_de_flota(ruta)
        assert (a['clave'], a['bloquea']) == ('licencia_vencida', True)
        assert 'hace 4 días' in a['texto']

    def test_sin_licencia_cargada_se_pregunta_como_no_se_sabe(self, db, almacen,
                                                              flota_sana):  # noqa: F811
        from app.services import senales_ruta as sr
        v, estado = flota_sana
        ruta, _t, c = _ruta(db, almacen, estado='EN_CARGUE', vehiculo_id=v.id)
        estado['custodio'] = c.id
        [a] = sr.advertencias_de_flota(ruta)
        assert (a['clave'], a['gravedad'], a['bloquea']) == (
            'licencia_sin_cargar', 'no_se_sabe', False)


# ═════════════════════════════════════════════════════════════════════════
# 3 · Licencia y documentos por HTTP
# ═════════════════════════════════════════════════════════════════════════

class TestLaLicenciaSeCarga:

    def _c(self, db):
        from app.models.conductor import Conductor
        c = Conductor(nombre='Ana Licencia', cedula='LIC-900', activo=True)
        db.session.add(c)
        db.session.commit()
        return c

    def test_invalida_no_entra_y_nada_cambia(self, app, client, db, usuario_admin):
        c = self._c(db)
        h = _token(app, usuario_admin)
        for cuerpo in ({'licencia_numero': '9', 'licencia_categoria': 'Z9',
                        'licencia_vence': '2027-01-01'},
                       {'licencia_numero': '9', 'licencia_categoria': 'C2',
                        'licencia_vence': '0025-01-01'},
                       {'licencia_numero': '9', 'licencia_categoria': 'C2'},
                       {'licencia_numero': '9', 'licencia_categoria': 'C2',
                        'licencia_vence': 'mañana', 'nombre': 'Otro'}):
            r = client.put(f'/api/rutas/conductores/{c.id}', headers=h, json=cuerpo)
            assert r.status_code == 400, cuerpo
        db.session.refresh(c)
        assert c.licencia_numero is None and c.nombre == 'Ana Licencia'

    def test_valida_entra_con_bitacora_y_se_ve_su_estado(self, app, client, db, usuario_admin):
        from app.models.bitacora import BitacoraAccion
        c = self._c(db)
        h = _token(app, usuario_admin)
        r = client.put(f'/api/rutas/conductores/{c.id}', headers=h, json={
            'licencia_numero': 'LC-123', 'licencia_categoria': 'c2',
            'licencia_vence': '2030-01-01'})
        assert r.status_code == 200
        assert r.get_json()['conductor']['licencia_categoria'] == 'C2'
        b = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='Conductor',
                                           entidad_id=c.id).one()
        assert b.despues['licencia_vence'] == '2030-01-01'
        d = client.get('/api/rutas/conductores?activos=false', headers=h).get_json()
        [fila] = [x for x in d['conductores'] if x['id'] == c.id]
        assert fila['licencia_estado'] == 'vigente' and fila['licencia_numero'] == 'LC-123'
        assert d['categorias_licencia'] == list(dom.CATEGORIAS_LICENCIA)

    def test_quien_no_es_de_almacen_no_ve_el_numero(self, db):
        from app.services.ruta_service import RutaService
        c = self._c(db)
        c.licencia_numero, c.licencia_categoria, c.licencia_vence = 'X', 'C1', D
        db.session.commit()
        [fila] = [x for x in RutaService.listar_conductores(False, False) if x['id'] == c.id]
        assert 'licencia_numero' not in fila and fila['licencia_estado']


class TestLosDocumentos:

    @pytest.fixture
    def veh(self, db):
        from app.models.vehiculo import Vehiculo
        v = Vehiculo(placa='DOC271', tipo='Camión', activo=True)
        db.session.add(v)
        db.session.commit()
        return v

    def test_la_rtm_imposible_no_entra(self, app, client, db, veh, usuario_admin):
        r = client.post('/flota/vehiculo/DOC271/documentos', headers=_token(app, usuario_admin),
                        json={'tipo': 'rtm', 'numero': 'R1', 'entidad': 'CDA',
                              'fecha_expedicion': '2025-11-11',
                              'fecha_vencimiento': '2025-11-11'})
        assert r.status_code == 400
        assert 'igual o anterior' in r.get_json()['error']

    def test_un_soat_que_no_dura_un_anio_no_entra(self, app, client, db, veh, usuario_admin):
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        r = client.post('/flota/vehiculo/DOC271/documentos', headers=_token(app, usuario_admin),
                        json={'tipo': 'soat', 'numero': 'S1', 'entidad': 'A',
                              'fecha_expedicion': (hoy - timedelta(days=30)).isoformat(),
                              'fecha_vencimiento': (hoy + timedelta(days=700)).isoformat()})
        assert r.status_code == 400 and 'Un SOAT dura un año' in r.get_json()['error']

    def test_renovar_sin_archivo_no_hereda_el_escaneo(self, app, client, db, veh, usuario_admin):
        from app.utils.fecha import dia_operativo
        from flota.adaptadores.modelos import DocumentoVehiculo, Foto
        hoy = dia_operativo()
        foto = Foto(entidad_tipo='documento', entidad_id=0, ts_captura=datetime.utcnow(),
                    autor_usuario_id=usuario_admin.id, clase='foto_dato', mime='image/jpeg',
                    bytes=10, ancho=1600, alto=1200, storage_ref='2026/08/x.jpg',
                    hash_sha256='a' * 64, estado='ok')
        db.session.add(foto)
        db.session.flush()
        doc = DocumentoVehiculo(vehiculo_id=veh.id, tipo='rtm', numero='R-VIEJA', entidad='CDA',
                                fecha_expedicion=hoy - timedelta(days=300),
                                fecha_vencimiento=hoy + timedelta(days=60), foto_id=foto.id)
        db.session.add(doc)
        db.session.commit()
        h = _token(app, usuario_admin)
        # Corregir una fecha del MISMO papel: conserva el escaneo.
        r = client.post('/flota/vehiculo/DOC271/documentos', headers=h, json={
            'tipo': 'rtm', 'numero': 'R-VIEJA', 'entidad': 'CDA',
            'fecha_expedicion': (hoy - timedelta(days=301)).isoformat(),
            'fecha_vencimiento': (hoy + timedelta(days=60)).isoformat()})
        assert r.status_code == 200 and r.get_json()['foto_id'] == foto.id
        # Renovarlo (otro número) sin archivo: el escaneo viejo no respalda el nuevo.
        r = client.post('/flota/vehiculo/DOC271/documentos', headers=h, json={
            'tipo': 'rtm', 'numero': 'R-NUEVA', 'entidad': 'CDA',
            'fecha_expedicion': hoy.isoformat(),
            'fecha_vencimiento': (hoy + timedelta(days=365)).isoformat()})
        assert r.status_code == 200 and r.get_json()['foto_id'] is None


# ═════════════════════════════════════════════════════════════════════════
# 4 · Trinquetes (AST)
# ═════════════════════════════════════════════════════════════════════════

def _fuentes(carpetas=('app', 'flota')):
    for carpeta in carpetas:
        for p in sorted((RAIZ / carpeta).rglob('*.py')):
            yield p.relative_to(RAIZ).as_posix(), p.read_text(encoding='utf-8')


def _funciones(arbol):
    for fn in ast.walk(arbol):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield fn


def _llama(fn, nombre):
    return any(isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Name) and n.func.id == nombre)
        or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre))
        for n in ast.walk(fn))


def _es_forzar_de_flota(call):
    """`registrar_accion('FORZAR', …, despues={'forzado': FORZADO_ADVERTENCIAS_FLOTA, …})`."""
    f = call.func
    nombre = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else '')
    if nombre != 'registrar_accion' or not call.args:
        return False
    if not (isinstance(call.args[0], ast.Constant) and call.args[0].value == 'FORZAR'):
        return False
    for kw in call.keywords:
        if kw.arg == 'despues' and isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values):
                if (isinstance(k, ast.Constant) and k.value == 'forzado'
                        and isinstance(v, ast.Name) and v.id == 'FORZADO_ADVERTENCIAS_FLOTA'):
                    return True
    return False


def _forzar_sin_autorizacion(fuentes):
    """(escritores, sin_autorizar): funciones que escriben un FORZAR de flota,
    y las que no llaman a `puede_autorizar_salida_prohibida`."""
    escritores, malos = set(), set()
    for nombre, texto in fuentes:
        for fn in _funciones(ast.parse(texto)):
            propias = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            if any(_es_forzar_de_flota(c) for c in propias):
                escritores.add((nombre, fn.name))
                if not _llama(fn, 'puede_autorizar_salida_prohibida'):
                    malos.add((nombre, fn.name))
    return escritores, malos


#: Escritores de un FORZAR de flota que no pasan por la autorización. Vacío,
#: solo encoge.
FORZAR_SIN_AUTORIZACION = {}


class TestTodoForzarDeFlotaPasaPorLaAutorizacion:

    def test_el_repo(self):
        escritores, malos = _forzar_sin_autorizacion(_fuentes())
        assert escritores, 'piso: el detector no encontró ningún FORZAR de flota'
        assert ('app/services/ruta_service.py', '_reconocer_advertencias_flota') in escritores
        nuevos = {m for m in malos if m not in FORZAR_SIN_AUTORIZACION}
        assert not nuevos, ('FORZAR de flota sin `puede_autorizar_salida_prohibida`: '
                            f'{sorted(nuevos)}. Un camión con el SOAT vencido saldría '
                            'con cualquier texto.')

    def test_ve_la_forma_rota(self):
        src = ('def f(ruta):\n'
               '    registrar_accion("FORZAR", ruta, despues={"forzado": FORZADO_ADVERTENCIAS_FLOTA})\n')
        assert _forzar_sin_autorizacion([('x.py', src)])[1] == {('x.py', 'f')}

    def test_no_marca_la_sana_ni_otro_forzado(self):
        sana = ('def f(ruta):\n'
                '    puede_autorizar_salida_prohibida("admin", "x")\n'
                '    registrar_accion("FORZAR", ruta, despues={"forzado": FORZADO_ADVERTENCIAS_FLOTA})\n')
        otro = ('def g(ruta):\n'
                '    registrar_accion("FORZAR", ruta, despues={"forzado": FORZADO_CIERRE_RUTA})\n')
        doc = ('def h(ruta):\n'
               '    """registrar_accion("FORZAR", ruta, despues={"forzado": FORZADO_ADVERTENCIAS_FLOTA})"""\n')
        assert _forzar_sin_autorizacion([('a.py', sana), ('b.py', otro), ('c.py', doc)]) == (
            {('a.py', 'f')}, set())


def _hechos_sin_licencia(fuentes):
    """(llamadas, sin_licencia): `Hechos(...)` y las que no pasan `licencia=`."""
    todas, malas = set(), set()
    for nombre, texto in fuentes:
        for fn in _funciones(ast.parse(texto)):
            for n in ast.walk(fn):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr == 'Hechos') or (
                        isinstance(f, ast.Name) and f.id == 'Hechos'):
                    todas.add((nombre, fn.name))
                    if not any(k.arg == 'licencia' for k in n.keywords):
                        malas.add((nombre, fn.name))
    return todas, malas


#: Construcciones de `Hechos` que no evalúan la licencia, con su porqué. Solo encoge.
HECHOS_SIN_LICENCIA = {
    ('flota/adaptadores/bandeja.py', '_hechos'):
        'La bandeja evalúa vehículos, no personas: no hay conductor de ruta con '
        'licencia que mirar (`conductor_de_la_ruta=None`). La licencia la ve el '
        'despacho y el correo diario.',
}


class TestTodoHechosPasaLaLicencia:

    def test_el_repo(self):
        todas, malas = _hechos_sin_licencia(_fuentes())
        assert len(todas) >= 2, 'piso: el detector no ve las construcciones de Hechos'
        assert malas == set(HECHOS_SIN_LICENCIA), (
            f'Hechos sin `licencia=`: {sorted(malas - set(HECHOS_SIN_LICENCIA))}; '
            f'inventario vencido: {sorted(set(HECHOS_SIN_LICENCIA) - malas)}')

    def test_cada_entrada_dice_por_que(self):
        assert all(len(v) > 40 for v in HECHOS_SIN_LICENCIA.values())

    def test_ve_la_forma_rota_y_no_la_sana(self):
        rota = 'def f():\n    return dom.Hechos(hoy=1)\n'
        sana = 'def g():\n    return dom.Hechos(hoy=1, licencia=None)\n'
        assert _hechos_sin_licencia([('r.py', rota), ('s.py', sana)])[1] == {('r.py', 'f')}


_CAMPOS_PAPEL = ('fecha_vencimiento', 'fecha_expedicion')
_CAMPOS_LICENCIA = ('licencia_vence', 'licencia_numero', 'licencia_categoria')


def _escritores_sin_politica(fuentes):
    """Funciones que escriben las fechas de un papel del vehículo (en una
    función que nombra `DocumentoVehiculo`) sin `problemas_de_fechas`, o una
    licencia sin `problemas_de_licencia`."""
    malos, vistos = set(), set()
    for nombre, texto in fuentes:
        if nombre.endswith('adaptadores/modelos.py') or nombre.startswith('app/models/'):
            continue
        for fn in _funciones(ast.parse(texto)):
            nombra_doc = any(isinstance(n, ast.Name) and n.id == 'DocumentoVehiculo'
                             for n in ast.walk(fn))
            escribe_papel = escribe_lic = False
            for n in ast.walk(fn):
                objetivos = []
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        objetivos += list(t.elts) if isinstance(t, ast.Tuple) else [t]
                for t in objetivos:
                    if isinstance(t, ast.Attribute):
                        escribe_papel |= nombra_doc and t.attr in _CAMPOS_PAPEL
                        escribe_lic |= t.attr in _CAMPOS_LICENCIA
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == 'DocumentoVehiculo'):
                    escribe_papel |= any(k.arg in _CAMPOS_PAPEL for k in n.keywords)
            if escribe_papel:
                vistos.add((nombre, fn.name))
                if not _llama(fn, 'problemas_de_fechas'):
                    malos.add((nombre, fn.name))
            if escribe_lic:
                vistos.add((nombre, fn.name))
                if not _llama(fn, 'problemas_de_licencia'):
                    malos.add((nombre, fn.name))
    return vistos, malos


#: Escritores de fechas de papel o de licencia sin su política. Vacío, solo encoge.
FECHAS_SIN_POLITICA = {}


class TestNingunaFechaEntraSinSuPolitica:

    def test_el_repo(self):
        vistos, malos = _escritores_sin_politica(_fuentes(('app', 'flota', 'scripts')))
        assert ('flota/api/documentos.py', 'guardar_documento') in vistos
        assert ('app/services/ruta_service.py', '_actualizar_licencia') in vistos
        assert not (malos - set(FECHAS_SIN_POLITICA)), sorted(malos)

    def test_ve_las_formas_rotas(self):
        tupla = ('def f(doc):\n    x = DocumentoVehiculo\n'
                 '    doc.fecha_expedicion, doc.fecha_vencimiento = a, b\n')
        ctor = 'def g():\n    return DocumentoVehiculo(tipo="soat", fecha_vencimiento=1)\n'
        lic = 'def h(c):\n    c.licencia_vence = 1\n'
        assert _escritores_sin_politica([('a.py', tupla), ('b.py', ctor), ('c.py', lic)])[1] == {
            ('a.py', 'f'), ('b.py', 'g'), ('c.py', 'h')}

    def test_no_marca_otras_fechas_de_vencimiento(self):
        """El lote de un producto también tiene `fecha_vencimiento`: sin
        `DocumentoVehiculo` en la función no es un papel."""
        lote = 'def f(item):\n    item.fecha_vencimiento = 1\n'
        sana = ('def g(doc):\n    problemas_de_fechas(1, 2, 3, 4)\n'
                '    x = DocumentoVehiculo\n    doc.fecha_vencimiento = 1\n')
        assert _escritores_sin_politica([('a.py', lote), ('b.py', sana)])[1] == set()

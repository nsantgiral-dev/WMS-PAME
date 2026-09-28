"""Validación crítica de flota «lo legal» (2026-09-27) — lo que quedaba abierto,
ya cerrado.

El bloqueo de salida (SOAT/RTM/licencia vencidos → solo admin, motivo ≥ 20)
vivía en el despacho, pero el mismo rol que el despacho frena —el jefe de
almacén, que está en `MAESTROS_FLOTA` por `Roles.GESTION`— podía reescribir el
papel que lo frena en `POST /flota/vehiculo/<placa>/documentos`, sin adjunto y
sin dejar rastro (VAL-FL-1/2). La clase: **una puerta de datos que deshace la
decisión de la puerta de despacho**. Ahora:

1. «no encontrado» sobre un papel para circular que HOY está vencido no se
   acepta, de nadie;
2. darlo por renovado exige otro número y el escaneo, o un rol que autoriza la
   salida (`salida.motivo_no_puede_reescribir_papel`);
3. todo cambio de un papel queda en la bitácora con antes y después
   (trinquete AST);
4. el gerente ve la flota y no escribe (`_permisos.SOLO_LECTURA_FLOTA`, en
   `exige`), medido sobre el `url_map`.

Y los P2/P3 de la misma validación: la expedición mal escrita no esconde un
vencimiento que ya pasó; control de flota carga licencias; el hash de las
fotos de una raíz anterior se recuerda; un aviso `encolado` huérfano se
reintenta; el proceso que pierde el candado no borra el resumen del latido; los
listeners del daño no se acumulan.

Los dos tests de la validación (antes `xfail(strict)`) quedan primero. El
segundo cambió en su paso intermedio: reescribir el SOAT vencido sin escaneo
ya no sale (409); con el número nuevo y el escaneo sí, y queda escrito.
"""
import ast
import base64
import hashlib
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest

from flota.dominio import salida as dom
from tests.flota.test_salida_prohibida import (  # noqa: F401
    _MOTIVO_LARGO, _fuentes, _funciones, _llama, _token, _usuario,
    ruta_con_soat_vencido)
from tests.test_fugas_ruta import flota_sana  # noqa: F401


def _placa(ruta):
    from app.extensions import db
    from app.models.vehiculo import Vehiculo
    return db.session.get(Vehiculo, ruta.vehiculo_id).placa


def _escaneo():
    """Una imagen de verdad de 1600 px (foto_dato del documento)."""
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', (1600, 1200), (200, 200, 200)).save(buf, 'JPEG', quality=80)
    return {'clase': 'documento_adjunto', 'mime': 'image/jpeg',
            'data_url': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()}


# ═════════════════════════════════════════════════════════════════════════
# Los dos de la validación
# ═════════════════════════════════════════════════════════════════════════

def test_el_jefe_no_saca_el_soat_vencido_marcandolo_no_encontrado(
        app, client, db, almacen, ruta_con_soat_vencido):
    ruta, _c = ruta_con_soat_vencido
    h = _token(app, _usuario(db, 'jefe_almacen', almacen))
    r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                    json={'motivo_advertencias': _MOTIVO_LARGO})
    assert r.status_code == 409 and r.get_json()['salida_prohibida'] is True

    r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos', headers=h,
                    json={'tipo': 'soat', 'estado': 'no_encontrado'})
    assert r.status_code == 409 and 'no encontrado' in r.get_json()['error']
    r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                    json={'motivo_advertencias': 'ok'})
    # La propiedad: el SOAT que se SABÍA vencido no sale sin el admin.
    assert r.status_code == 409, r.get_json()


def test_reescribir_el_soat_vencido_deja_rastro(
        app, client, db, almacen, ruta_con_soat_vencido):
    from app.models.bitacora import BitacoraAccion
    from app.utils.fecha import dia_operativo
    ruta, _c = ruta_con_soat_vencido
    jefe = _usuario(db, 'jefe_almacen', almacen)
    h = _token(app, jefe)
    hoy = dia_operativo()
    fechas = {'fecha_expedicion': hoy.isoformat(),
              'fecha_vencimiento': (hoy + timedelta(days=365)).isoformat()}
    # Mismo número y sin escaneo: no es un papel nuevo, es el vencido con
    # otra fecha. No se guarda y el camión sigue frenado.
    r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos', headers=h,
                    json={'tipo': 'soat', 'numero': '1', 'entidad': 'X', **fechas})
    assert r.status_code == 409, r.get_json()
    assert client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                       json={}).status_code == 409
    # Con el papel nuevo (otro número y su escaneo) sí, y queda escrito.
    r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos', headers=h,
                    json={'tipo': 'soat', 'numero': 'SOAT-2027', 'entidad': 'X',
                          'archivo': _escaneo(), **fechas})
    assert r.status_code in (200, 201), r.get_json()
    r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h, json={})
    assert r.status_code == 200, r.get_json()
    [f] = BitacoraAccion.query.filter_by(entidad='DocumentoVehiculo').all()
    assert f.usuario_id == jefe.id and f.accion == 'EDITAR'
    assert f.antes['fecha_vencimiento'] == (hoy - timedelta(days=2)).isoformat()
    assert f.despues['numero'] == 'SOAT-2027' and f.despues['foto_id']


# ═════════════════════════════════════════════════════════════════════════
# Quién reescribe un papel vencido
# ═════════════════════════════════════════════════════════════════════════

class TestQuienReescribeUnPapelVencido:

    def test_el_admin_tampoco_lo_marca_no_encontrado(self, app, client, db, almacen,
                                                     ruta_con_soat_vencido, usuario_admin):
        ruta, _c = ruta_con_soat_vencido
        r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos',
                        headers=_token(app, usuario_admin),
                        json={'tipo': 'soat', 'estado': 'no_encontrado'})
        assert r.status_code == 409

    def test_el_admin_lo_corrige_sin_escaneo_y_queda_escrito(self, app, client, db, almacen,
                                                            ruta_con_soat_vencido, usuario_admin):
        from app.models.bitacora import BitacoraAccion
        from app.utils.fecha import dia_operativo
        ruta, _c = ruta_con_soat_vencido
        hoy = dia_operativo()
        r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos',
                        headers=_token(app, usuario_admin),
                        json={'tipo': 'soat', 'numero': '1', 'entidad': 'X',
                              'motivo': 'la fecha estaba mal digitada',
                              'fecha_expedicion': (hoy - timedelta(days=10)).isoformat(),
                              'fecha_vencimiento': (hoy + timedelta(days=355)).isoformat()})
        assert r.status_code == 200, r.get_json()
        b = BitacoraAccion.query.filter_by(entidad='DocumentoVehiculo').one()
        assert b.usuario_id == usuario_admin.id and b.motivo == 'la fecha estaba mal digitada'

    def test_control_de_flota_sin_escaneo_tampoco(self, app, client, db, almacen,
                                                  ruta_con_soat_vencido):
        from app.utils.fecha import dia_operativo
        ruta, _c = ruta_con_soat_vencido
        hoy = dia_operativo()
        r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos',
                        headers=_token(app, _usuario(db, 'control_flota', almacen)),
                        json={'tipo': 'soat', 'numero': 'NUEVO', 'entidad': 'X',
                              'fecha_expedicion': hoy.isoformat(),
                              'fecha_vencimiento': (hoy + timedelta(days=365)).isoformat()})
        assert r.status_code == 409 and 'escaneo' in r.get_json()['error']

    def test_un_cambio_que_lo_deja_vencido_no_se_frena(self, app, client, db, almacen,
                                                       ruta_con_soat_vencido):
        from app.utils.fecha import dia_operativo
        ruta, _c = ruta_con_soat_vencido
        hoy = dia_operativo()
        r = client.post(f'/flota/vehiculo/{_placa(ruta)}/documentos',
                        headers=_token(app, _usuario(db, 'jefe_almacen', almacen)),
                        json={'tipo': 'soat', 'numero': '1', 'entidad': 'Otra aseguradora',
                              'fecha_expedicion': (hoy - timedelta(days=367)).isoformat(),
                              'fecha_vencimiento': (hoy - timedelta(days=2)).isoformat()})
        assert r.status_code == 200, r.get_json()

    def test_la_politica_uno_por_uno(self):
        hoy = date(2026, 9, 27)
        vencido = dom.Papel('soat', dom.VENCIDO, hoy - timedelta(days=3))
        base = dict(tipo='soat', antes=vencido, numero_antes='1', estado_nuevo='vigente',
                    vence_nuevo=hoy + timedelta(days=360), numero_nuevo='2',
                    trae_archivo=True, rol='jefe_almacen', hoy=hoy)
        assert dom.motivo_no_puede_reescribir_papel(**base) is None
        assert dom.motivo_no_puede_reescribir_papel(**{**base, 'trae_archivo': False})
        assert dom.motivo_no_puede_reescribir_papel(**{**base, 'numero_nuevo': '1'})
        assert dom.motivo_no_puede_reescribir_papel(**{**base, 'numero_nuevo': ' '})
        assert dom.motivo_no_puede_reescribir_papel(
            **{**base, 'trae_archivo': False, 'rol': 'admin'}) is None
        assert dom.motivo_no_puede_reescribir_papel(
            **{**base, 'rol': 'admin', 'estado_nuevo': dom.NO_ENCONTRADO})
        assert dom.motivo_no_puede_reescribir_papel(
            **{**base, 'tipo': 'poliza_rc', 'trae_archivo': False}) is None
        assert dom.motivo_no_puede_reescribir_papel(
            **{**base, 'antes': dom.Papel('soat', dom.VIGENTE, hoy + timedelta(days=9)),
               'trae_archivo': False}) is None


# ═════════════════════════════════════════════════════════════════════════
# Todo cambio de un papel queda en la bitácora (AST)
# ═════════════════════════════════════════════════════════════════════════

_CAMPOS_DEL_PAPEL = ('fecha_vencimiento', 'fecha_expedicion', 'estado', 'numero',
                     'entidad', 'foto_id')


def _escritores_de_papel_sin_bitacora(fuentes):
    """(escritores, sin_bitacora): funciones que escriben un campo de un papel
    (en una función que nombra `DocumentoVehiculo`) o lo construyen, y las que
    no llaman a `registrar_accion`."""
    vistos, malos = set(), set()
    for nombre, texto in fuentes:
        if nombre.endswith('adaptadores/modelos.py') or nombre.startswith('app/models/'):
            continue
        for fn in _funciones(ast.parse(texto)):
            if not any(isinstance(n, ast.Name) and n.id == 'DocumentoVehiculo'
                       for n in ast.walk(fn)):
                continue
            escribe = False
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        for x in (t.elts if isinstance(t, ast.Tuple) else [t]):
                            escribe |= isinstance(x, ast.Attribute) and x.attr in _CAMPOS_DEL_PAPEL \
                                and not (isinstance(x.value, ast.Name) and x.value.id == 'self')
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == 'DocumentoVehiculo'):
                    escribe = True
            if escribe:
                vistos.add((nombre, fn.name))
                if not _llama(fn, 'registrar_accion'):
                    malos.add((nombre, fn.name))
    return vistos, malos


#: Escritores de un papel sin bitácora, con su porqué. Vacío, solo encoge.
PAPEL_SIN_BITACORA = {}


class TestTodoCambioDeUnPapelQuedaEscrito:

    def test_el_repo(self):
        vistos, malos = _escritores_de_papel_sin_bitacora(_fuentes(('app', 'flota', 'scripts')))
        assert ('flota/api/documentos.py', 'guardar_documento') in vistos, 'piso'
        assert not (malos - set(PAPEL_SIN_BITACORA)), sorted(malos)

    def test_ve_la_forma_rota_y_no_la_sana(self):
        rota = 'def f(d):\n    x = DocumentoVehiculo\n    d.estado = "no_encontrado"\n'
        ctor = 'def g():\n    db.session.add(DocumentoVehiculo(tipo="soat"))\n'
        sana = ('def h(d):\n    x = DocumentoVehiculo\n    d.numero = "1"\n'
                '    registrar_accion("EDITAR", d)\n')
        otro = 'def k(t):\n    t.estado = "CANCELADO"\n'
        _v, malos = _escritores_de_papel_sin_bitacora(
            [('a.py', rota), ('b.py', ctor), ('c.py', sana), ('d.py', otro)])
        assert malos == {('a.py', 'f'), ('b.py', 'g')}


# ═════════════════════════════════════════════════════════════════════════
# El gerente ve la flota y no escribe (medido sobre el url_map)
# ═════════════════════════════════════════════════════════════════════════

class TestElGerenteNoEscribeEnFlota:

    def test_ningun_endpoint_de_flota_que_escribe_lo_deja_pasar(self, app, client, db, almacen):
        gerente = _token(app, _usuario(db, 'gerente', almacen))
        escrituras = []
        for regla in app.url_map.iter_rules():
            if not str(regla).startswith('/flota/') or 'entrega' in str(regla):
                continue          # /flota/avisos/entrega: webhook con secreto, sin JWT
            for metodo in sorted(regla.methods - {'GET', 'HEAD', 'OPTIONS'}):
                url = str(regla)
                for arg in regla.arguments:
                    url = url.replace(f'<int:{arg}>', '999999').replace(f'<{arg}>', 'ZZZ999')
                escrituras.append((metodo, url))
        assert len(escrituras) >= 25, f'piso: solo {len(escrituras)} escrituras en /flota'
        dejaron = []
        for metodo, url in escrituras:
            r = client.open(url, method=metodo, headers=gerente, json={})
            if r.status_code != 403:
                dejaron.append((metodo, url, r.status_code))
        assert not dejaron, dejaron

    def test_lee_lo_que_leia(self, app, client, db, almacen):
        h = _token(app, _usuario(db, 'gerente', almacen))
        for url in ('/flota/avisos', '/flota/bandeja', '/flota/conductores/licencias'):
            assert client.get(url, headers=h).status_code == 200, url

    def test_el_jefe_sigue_escribiendo_donde_debe(self, app, client, db, almacen):
        from app.models.vehiculo import Vehiculo
        db.session.add(Vehiculo(placa='JEF271', tipo='Camión', activo=True))
        db.session.commit()
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        r = client.post('/flota/vehiculo/JEF271/documentos',
                        headers=_token(app, _usuario(db, 'jefe_almacen', almacen)),
                        json={'tipo': 'rtm', 'numero': 'R', 'entidad': 'CDA',
                              'fecha_expedicion': hoy.isoformat(),
                              'fecha_vencimiento': (hoy + timedelta(days=365)).isoformat()})
        assert r.status_code == 201, r.get_json()

    def test_las_tuplas_de_escritura_no_lo_nombran_y_el_js_tampoco(self):
        import re
        from pathlib import Path
        from flota.api._permisos import DECIDE_FLOTA, FUERZA_CIERRE, SOLO_LECTURA_FLOTA
        assert 'gerente' in SOLO_LECTURA_FLOTA
        assert 'gerente' not in DECIDE_FLOTA and 'gerente' not in FUERZA_CIERRE
        js = (Path(__file__).resolve().parents[2] / 'app/static/pwa/flota.js').read_text(encoding='utf-8')
        for nombre in ('FLOTA_ROLES_DECIDEN', 'FLOTA_ROLES_FUERZAN_CIERRE'):
            lista = re.search(rf'const {nombre} = \[([^\]]*)\]', js).group(1)
            assert 'gerente' not in lista, nombre


# ═════════════════════════════════════════════════════════════════════════
# P2: la expedición mal escrita no esconde un vencimiento que ya pasó
# ═════════════════════════════════════════════════════════════════════════

class TestLaExpedicionMalEscrita:
    D = date(2026, 9, 27)

    def _p(self, exp, venc):
        return dom.papel_cargado('soat', no_encontrado=False, expedicion=exp,
                                 vencimiento=venc, hoy=self.D)

    def test_anio_0025_y_vencimiento_pasado_es_vencido(self):
        p = self._p(date(25, 9, 1), self.D - timedelta(days=20))
        assert p.estado == dom.VENCIDO
        assert dom.motivo_papel(p, self.D).bloquea

    def test_expedicion_futura_y_vencimiento_pasado_es_vencido(self):
        assert self._p(self.D + timedelta(days=5), self.D - timedelta(days=1)).estado == dom.VENCIDO

    def test_anio_0025_con_vencimiento_futuro_sigue_a_corregir(self):
        assert self._p(date(25, 9, 1), self.D + timedelta(days=100)).estado == dom.DATO_A_CORREGIR

    def test_las_dos_posibles_que_no_cuadran_siguen_a_corregir(self):
        """BDT261: no se sabe cuál de las dos está mal escrita."""
        f = date(2025, 11, 11)
        assert self._p(f, f).estado == dom.DATO_A_CORREGIR

    def test_el_vencimiento_imposible_sigue_a_corregir(self):
        assert self._p(date(2025, 1, 1), date(26, 1, 1)).estado == dom.DATO_A_CORREGIR


# ═════════════════════════════════════════════════════════════════════════
# P2: control de flota carga licencias
# ═════════════════════════════════════════════════════════════════════════

class TestControlDeFlotaCargaLicencias:

    @pytest.fixture
    def conductor(self, db):
        from app.models.conductor import Conductor
        c = Conductor(nombre='Eva Licencias', cedula='EVA-1', activo=True)
        db.session.add(c)
        db.session.commit()
        return c

    @pytest.mark.parametrize('rol,esperado', [
        ('control_flota', 200), ('admin', 200), ('jefe_almacen', 200),
        ('gerente', 403), ('conductor', 403), ('operario', 403)])
    def test_quien_la_carga(self, app, client, db, almacen, conductor, rol, esperado):
        r = client.put(f'/flota/conductores/{conductor.id}/licencia',
                       headers=_token(app, _usuario(db, rol, almacen)),
                       json={'licencia_numero': 'L9', 'licencia_categoria': 'C2',
                             'licencia_vence': '2030-01-01'})
        assert r.status_code == esperado, r.get_json()

    def test_la_valida_y_la_escribe_en_la_bitacora(self, app, client, db, almacen, conductor):
        from app.models.bitacora import BitacoraAccion
        h = _token(app, _usuario(db, 'control_flota', almacen))
        r = client.put(f'/flota/conductores/{conductor.id}/licencia', headers=h,
                       json={'licencia_numero': 'L9', 'licencia_categoria': 'X1',
                             'licencia_vence': '2030-01-01'})
        assert r.status_code == 400
        r = client.put(f'/flota/conductores/{conductor.id}/licencia', headers=h,
                       json={'licencia_numero': 'L9', 'licencia_categoria': 'C2',
                             'licencia_vence': '2030-01-01'})
        assert r.get_json()['conductor']['licencia_estado'] == 'vigente'
        assert BitacoraAccion.query.filter_by(entidad='Conductor', entidad_id=conductor.id).count() == 1
        d = client.get('/flota/conductores/licencias', headers=h).get_json()
        [fila] = [x for x in d['conductores'] if x['id'] == conductor.id]
        assert fila['licencia_numero'] == 'L9' and 'C2' in d['categorias_licencia']

    def test_el_diagnostico_de_flota_la_pinta_y_la_abre(self, tmp_path):
        from tests.test_muelle_salida_prohibida_js import _correr
        d = {'conductores': [{'id': 3, 'nombre': 'Eva <b>', 'licencia_numero': '',
                              'licencia_categoria': '', 'licencia_vence': None,
                              'licencia_estado': 'sin_cargar'}],
             'categorias_licencia': ['C1', 'C2']}
        h = _correr(tmp_path, ['util.js', 'rutas.js', 'flota.js'],
                    f'flotaLicenciasHtml({__import__("json").dumps(d)})')
        assert 'Eva &lt;b&gt; — sin cargar' in h and 'flotaLicenciaEditar(0)' in h


# ═════════════════════════════════════════════════════════════════════════
# P2: la foto de una raíz anterior se hashea una vez
# ═════════════════════════════════════════════════════════════════════════

class TestElHashSeRecuerda:

    def test_dos_health_un_hash(self, db, tmp_path, monkeypatch, usuario_admin):
        from flota.adaptadores import almacen_fotos as af
        from flota.adaptadores.modelos import Foto
        nueva, vieja = tmp_path / 'n', tmp_path / 'v'
        nueva.mkdir()
        (vieja / '2026' / '08').mkdir(parents=True)
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(nueva))
        monkeypatch.setenv('FLOTA_FOTOS_DIRS_ANTERIORES', str(vieja))
        monkeypatch.setattr(af, '_HASH_VERIFICADO', {})
        contenido = b'foto vieja'
        ref = f'2026/08/{hashlib.sha256(contenido).hexdigest()}.jpg'
        (vieja / ref).write_bytes(contenido)
        db.session.add(Foto(entidad_tipo='documento', entidad_id=0, ts_captura=datetime.utcnow(),
                            autor_usuario_id=usuario_admin.id, clase='foto_dato',
                            mime='image/jpeg', bytes=len(contenido), ancho=1600, alto=1200,
                            storage_ref=ref, hash_sha256=hashlib.sha256(contenido).hexdigest(),
                            estado='ok'))
        db.session.commit()
        llamadas = []
        real = af._sha_de_archivo
        monkeypatch.setattr(af, '_sha_de_archivo', lambda r: llamadas.append(r) or real(r))
        for _ in range(2):
            assert af.diagnostico_almacen()['fotos_ok_en_raiz_anterior'] == 1
        assert len(llamadas) == 1


# ═════════════════════════════════════════════════════════════════════════
# P3
# ═════════════════════════════════════════════════════════════════════════

class TestLoBaratoDeLaValidacion:

    def test_un_encolado_huerfano_se_reintenta(self, db, monkeypatch):
        from flota.adaptadores.avisos import MINUTOS_ENCOLADO_HUERFANO, _reintentable
        from flota.adaptadores.modelos import Aviso
        f = Aviso(clave='x', plantilla='p', telefono='correo', parametros='{}',
                  estado='encolado', simulado=False,
                  creado_ts=datetime.utcnow() - timedelta(minutes=MINUTOS_ENCOLADO_HUERFANO + 1))
        db.session.add(f)
        db.session.commit()
        assert _reintentable(f) is True
        f.creado_ts = datetime.utcnow()
        assert _reintentable(f) is False

    def test_los_listeners_del_dano_no_se_acumulan(self):
        from sqlalchemy import event
        from flota.adaptadores import avisos
        from flota.adaptadores.modelos import Hallazgo
        avisos.escuchar_danos_bloqueantes()
        avisos.escuchar_danos_bloqueantes()
        assert event.contains(Hallazgo, 'after_insert', avisos._dano_insertado)

    def test_deshacer_un_savepoint_no_borra_lo_de_afuera(self):
        from types import SimpleNamespace
        from flota.adaptadores.avisos import _transaccion_deshecha
        s = SimpleNamespace(info={'flota_danos_bloqueantes': {7}})
        _transaccion_deshecha(s, SimpleNamespace(nested=True))
        assert s.info['flota_danos_bloqueantes'] == {7}
        _transaccion_deshecha(s, SimpleNamespace(nested=False))
        assert 'flota_danos_bloqueantes' not in s.info

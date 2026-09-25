"""Las costuras del rediseño de flota (2026-09-24).

Cinco frentes en paralelo dejaron cada pieza bien y las uniones mal. Acá, una
clase por costura (la política de salida tiene su propio archivo,
`test_una_politica_de_salida.py`):

- el km que el conductor CAMBIA en el tanqueo llega con la foto del tablero
  (o reutiliza la lectura) y no nace «en duda» sin necesidad;
- los despachos que salieron reconociendo advertencias de flota llegan a la
  bandeja de control de flota, leídos por UNA función;
- la evidencia de las señales se lee en palabras (Node + `util.js` real);
- «Lo que no se pudo revisar» se agrupa;
- la ficha «completa» exige la capacidad del tanque.
"""
import base64
import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'


def _jpeg_url(ancho=1600, alto=1200):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (ancho, alto), (17, 17, 17)).save(buf, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def _foto_tablero():
    return {'clase': 'foto_dato', 'data_url': _jpeg_url(), 'ancho': 1600,
            'alto': 1200, 'mime': 'image/jpeg'}


@pytest.fixture
def camion(db, monkeypatch, tmp_path):
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import LecturaOdometro
    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    u = Usuario(email='flota@costura.test', nombre='Yesid', rol='control_flota',
                activo=True)
    u.set_password('x')
    v = Vehiculo(placa='COS001', tipo='NHR', activo=True)
    db.session.add_all([u, v])
    db.session.commit()
    db.session.add(LecturaOdometro(vehiculo_id=v.id, valor_km=10_000,
                                   ts=datetime.utcnow() - timedelta(days=1),
                                   origen='cierre_dia', autor_usuario_id=u.id))
    db.session.commit()
    return v, u


def _tanquear(v, u, km, **kw):
    from flota.adaptadores.gastos import registrar_tanqueo
    from app.utils.fecha import dia_operativo
    return registrar_tanqueo(
        vehiculo_id=v.id, fecha=dia_operativo(), valor=Decimal(150_000),
        galones=Decimal(10), tanque='lleno', estacion='Terpel', km=km,
        proveedor='Terpel', origen_costo='tarjeta_convenio',
        registrado_por_usuario_id=u.id, **kw)


class TestElKmDelTanqueoNoNaceEnDudaSinNecesidad:

    def test_el_km_de_siempre_reutiliza_la_lectura(self, db, camion):
        from flota.adaptadores.modelos import LecturaOdometro
        v, u = camion
        antes = LecturaOdometro.query.filter_by(vehiculo_id=v.id).count()
        t = _tanquear(v, u, 10_000)
        assert LecturaOdometro.query.filter_by(vehiculo_id=v.id).count() == antes
        assert t.gasto.lectura.valor_km == 10_000

    def test_el_km_que_cambio_con_foto_del_tablero_nace_respaldado(self, db, camion):
        from flota.adaptadores.modelos import Foto
        v, u = camion
        t = _tanquear(v, u, 10_250, foto_tablero=_foto_tablero())
        lec = t.gasto.lectura
        assert lec.valor_km == 10_250
        assert lec.foto_id is not None
        assert lec.confianza != 'dudosa', lec.motivo_dudosa
        foto = db.session.get(Foto, lec.foto_id)
        assert (foto.entidad_tipo, foto.entidad_id, foto.clase) == (
            'odometro', lec.id, 'foto_dato')

    def test_sin_foto_nace_en_duda_como_siempre(self, db, camion):
        """Pedirla no bloquea (regla 1): sin foto se registra igual, y queda
        en la cola de verificación."""
        v, u = camion
        lec = _tanquear(v, u, 10_300).gasto.lectura
        assert lec.foto_id is None and lec.confianza == 'dudosa'

    def test_por_la_puerta_http(self, app, client, db, camion):
        from flask_jwt_extended import create_access_token
        from app.utils.fecha import dia_operativo
        v, u = camion
        with app.app_context():
            tok = create_access_token(identity=str(u.id))
        r = client.post('/flota/tanqueos', headers={'Authorization': f'Bearer {tok}'},
                        json={'placa': 'COS001', 'fecha': dia_operativo().isoformat(),
                              'valor': '150000', 'galones': '10', 'tanque': 'lleno',
                              'estacion': 'Terpel', 'km': 10_400, 'proveedor': 'Terpel',
                              'origen_costo': 'tarjeta_convenio',
                              'foto_tablero': _foto_tablero()})
        assert r.status_code == 201, r.get_json()
        from flota.adaptadores.modelos import LecturaOdometro
        lec = (LecturaOdometro.query.filter_by(vehiculo_id=v.id, valor_km=10_400).one())
        assert lec.foto_id is not None and lec.confianza != 'dudosa'


class TestLosDespachosForzadosLleganAControlDeFlota:
    """El FORZAR del muelle (salió reconociendo advertencias de flota) vivía
    solo en la bitácora, que control de flota no ve (📈 es de gestión)."""

    def _forzar(self, db, v, u, motivo='sale igual, SOAT en trámite', despues=None):
        from app.models.conductor import Conductor
        from app.models.ruta_despacho import RutaDespacho
        from app.services.bitacora import registrar_accion
        c = Conductor.query.filter_by(cedula='COS-1').first()
        if c is None:
            c = Conductor(nombre='Ana', cedula='COS-1', activo=True)
            db.session.add(c)
            db.session.flush()
        r = RutaDespacho(vehiculo_id=v.id, conductor_id=c.id, tipo_ruta='Municipal',
                         estado='EN_TRANSITO')
        db.session.add(r)
        db.session.flush()
        registrar_accion('FORZAR', r, usuario_id=u.id, motivo=motivo,
                         entidad_codigo=f'RUTA-{r.id}', antes={'estado': 'EN_CARGUE'},
                         despues=despues if despues is not None else {
                             'momento': 'despachar',
                             'advertencias_flota': ['soat_vencido'],
                             'detalle': [{'clave': 'soat_vencido',
                                          'texto': 'SOAT vencido desde 01/09/2026 (hace 23 días)'}]})
        db.session.commit()
        return r

    def test_una_funcion_los_lee_y_no_confunde_el_cierre_forzado(self, db, camion):
        from app.services.senales_ruta import despachos_forzados
        v, u = camion
        r = self._forzar(db, v, u)
        # El cierre forzado de una ruta también es FORZAR, sin advertencias.
        self._forzar(db, v, u, motivo='cerrada a mano', despues={'estado': 'ENTREGADA'})
        filas = despachos_forzados()
        assert [f['ruta_id'] for f in filas] == [r.id]
        assert filas[0]['vehiculo_id'] == v.id
        assert filas[0]['claves'] == ['soat_vencido']
        assert filas[0]['motivo'] == 'sale igual, SOAT en trámite'

    def test_llegan_a_pendientes_con_quien_y_por_que(self, db, camion):
        from flota.adaptadores.bandeja import armar_bandeja
        v, u = camion
        self._forzar(db, v, u)
        p = [x for x in armar_bandeja()['pendientes'] if x['clase'] == 'despacho_forzado']
        assert len(p) == 1
        assert p[0]['placa'] == 'COS001'
        assert 'SOAT vencido desde 01/09/2026' in p[0]['texto']
        assert 'Yesid' in p[0]['detalle'] and 'SOAT en trámite' in p[0]['detalle']

    def test_los_de_hace_mas_de_una_semana_no(self, db, camion):
        from app.models.bitacora import BitacoraAccion
        from flota.adaptadores.bandeja import armar_bandeja
        v, u = camion
        self._forzar(db, v, u)
        b = BitacoraAccion.query.filter_by(accion='FORZAR').one()
        b.ocurrido_en = datetime.utcnow() - timedelta(days=10)
        db.session.commit()
        assert not [x for x in armar_bandeja()['pendientes']
                    if x['clase'] == 'despacho_forzado']


class TestLaFichaCompletaNoTieneUnDetectorCiego:

    def test_sin_capacidad_de_tanque_no_es_completa_y_dice_que_se_deja_de_ver(
            self, db, camion):
        from flota.adaptadores.bandeja import armar_bandeja
        from flota.adaptadores.modelos import FichaTecnica
        v, u = camion
        db.session.add(FichaTecnica(
            vehiculo_id=v.id, combustible='diesel', sistema_frenos='hidraulico',
            frenos_fuente='manual_fabricante', tiene_freno_escape='no',
            distribucion='correa', distribucion_fuente='manual_fabricante',
            transmision_final='cardan', posiciones_llanta=4, km_inicial=9_000,
            km_inicial_ts=datetime.utcnow() - timedelta(days=30)))
        db.session.commit()
        b = armar_bandeja()
        fila = next(f for f in b['hoy'] if f['placa'] == 'COS001')
        assert fila['ficha'] == 'incompleta'
        [p] = [x for x in b['pendientes'] if x['clase'] == 'ficha']
        assert 'capacidad del tanque' in p['texto']
        assert 'no se detecta un tanqueo que no cabe' in p['texto']


# ── Lo que se pinta: Node con `util.js` real ────────────────────────────────

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const el = () => ({ style: {}, innerHTML: '', classList: { add() {}, remove() {} } });
const ctx = {
  console, document: { getElementById: () => el(), querySelector: () => null,
    querySelectorAll: () => [], addEventListener() {}, createElement: el },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, setTimeout, clearTimeout, setInterval: () => 0,
  clearInterval() {}, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alerta: () => {}, TOKEN: 'x', API: '', OPERARIO: { rol: 'control_flota' },
  horaColombia: (x) => String(x),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['util.js', 'flota.js', 'flota_analitica.js', 'flota_bandeja.js']) {
  vm.runInContext(fs.readFileSync(PWA + '/' + f, 'utf-8'), ctx);
}
vm.runInContext('var S = ' + JSON.stringify(G.dato) + ';', ctx);
process.stdout.write(JSON.stringify(vm.runInContext(G.expr, ctx)));
"""


def _node(tmp_path, expr, dato):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS)
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'expr': expr, 'dato': dato}))
    p = subprocess.run(['node', str(h), str(PWA), str(g)], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestLaEvidenciaSeLeeEnPalabras:

    SENAL = {'clase': 'galones', 'placa': 'GAL001', 'titulo': 't', 'texto': 'x',
             'contexto': 'c', 'propone': 'p', 'caso': {'pestana': 'gastos'},
             'evidencia': {'km_desde': 71800, 'km_hasta': 72100, 'km': 300,
                           'galones': '16.000',
                           'galones_esperados': '10.85714285714285714285714286',
                           'rendimiento_km_galon': '27.63157894736842105263157895',
                           'ventanas_del_rendimiento': 7, 'fecha': '2026-09-21',
                           'estacion': 'Terpel Neiva', 'gasto_id': 12,
                           'precio_galon': '2.000E+4', 'forma': 'turno_de_otro',
                           'desde': '2026-09-20T12:00:00+00:00'}}

    def test_etiquetas_formatos_y_hora_de_bogota(self, tmp_path):
        html = _node(tmp_path, 'flotaBandejaTarjetaSenal(S, 0)', self.SENAL)
        for esperado in ('Km al empezar:', '71.800', 'Galones esperados:', '10,86',
                         'Rendimiento medido (km por galón):', '27,63', '21/09/2026',
                         'Precio del galón:', '$20.000',
                         # 12:00 UTC = 07:00 Bogotá
                         '20/09', '07:00'):
            assert esperado in html, esperado
        for crudo in ('km desde', 'galones esperados:', '10.857', 'forma',
                      'turno_de_otro', 'gasto id', '2.000E+4', 'T12:00'):
            assert crudo not in html, crudo

    def test_lo_que_no_se_pudo_revisar_se_agrupa(self, tmp_path):
        filas = [{'clase': 'km_sin_ruta', 'placa': p, 'casos': 1,
                  'motivo': 'ninguna ruta registra esta placa'}
                 for p in ('AMB001', 'GAL001', 'VRD001')] + [
                 {'clase': 'galones', 'placa': 'GAL002', 'casos': 2,
                  'motivo': 'el rendimiento no se sostiene'}]
        html = _node(tmp_path, 'flotaBandejaNoEvaluables(S)', filas)
        assert html.count('ninguna ruta registra esta placa') == 1
        assert 'AMB001, GAL001, VRD001' in html
        assert 'Lo que no se pudo revisar (2)' in html
        assert '2 casos' in html

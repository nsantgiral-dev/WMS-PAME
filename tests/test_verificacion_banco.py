"""
Los pagos bancarios de la ruta, vistos en el banco (m051liqcaja, 2026-09-27).

La clase: **se da por recibida plata que nadie vio llegar.** Una transferencia
se daba por pagada con la palabra del conductor y un pantallazo que ninguna
pantalla mostraba; el recibo de caja salía apenas se encolaba.

- `estado`: POR_VERIFICAR / VERIFICADA / NO_ENCONTRADA, solo para bancarios.
- `exigir_para_rc`: por verificar → espera (DependenciaPendiente, sin gastar
  reintento); no encontrada → ErrorDeterminista; el switch lo apaga.
- `verificar`: autor, hora, bitácora, nota obligatoria al «no apareció»; no se
  cambia con el recibo ya enviado; despierta al RC que esperaba.
- La planilla ya no baja fotos; el comprobante se pide por parada.
- Permisos por HTTP y la pantalla en Node con `util.js` real.
- La integración con el ejecutor del RC (otro frente): trinquete xfail
  estricto hasta que la línea exista.
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_caja_conductor import _conductor, _ruta
from tests.test_cartera_retencion import _jwt, _usuario

RAIZ = Path(__file__).resolve().parents[1]
FOTO = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ'


def _bancaria(db, almacen, forma='TRANSFERENCIA_BANCOLOMBIA_AH', monto=50000, foto=True):
    c, _ = _conductor(db)
    ruta, [r] = _ruta(db, almacen, c, [('ENTREGADO', forma, monto, monto)])
    r.referencia_pago = '8877'
    if foto:
        r.foto_comprobante = FOTO
    db.session.commit()
    return ruta, r


class TestElEstado:

    @pytest.mark.parametrize('forma,esperado', [
        ('TRANSFERENCIA_BBVA', 'POR_VERIFICAR'), ('CONSIGNACION', 'POR_VERIFICAR'),
        ('TRANSFERENCIA', 'POR_VERIFICAR'), ('EFECTIVO', None), ('TARJETA', None),
        ('CHEQUE', None)])
    def test_solo_lo_bancario(self, db, almacen, forma, esperado):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen, forma)
        assert vb.estado(r) == esperado

    def test_sin_cobro_no_hay_nada_que_verificar(self, db, almacen):
        from app.services import verificacion_banco as vb
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('RECHAZADO', 'TRANSFERENCIA_BBVA', 0, 5000)])
        assert vb.estado(r) is None


class TestLaCompuertaDelRecibo:

    def test_por_verificar_espera_sin_gastar_reintento(self, db, almacen):
        from app.services import verificacion_banco as vb
        from app.services.siesa_job_service import DependenciaPendiente
        _, r = _bancaria(db, almacen)
        with pytest.raises(DependenciaPendiente, match='no sale hasta que alguien vea'):
            vb.exigir_para_rc(r)

    def test_verificada_pasa(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        vb.verificar(r.id, _usuario(db, 'liquidador').id, True)
        vb.exigir_para_rc(r)

    def test_no_encontrada_es_determinista(self, db, almacen):
        from app.services import verificacion_banco as vb
        from app.services.siesa_job_service import DependenciaPendiente, ErrorDeterminista
        _, r = _bancaria(db, almacen)
        vb.verificar(r.id, _usuario(db, 'lider_cartera').id, False, 'No está en el extracto del 26')
        with pytest.raises(ErrorDeterminista, match='no apareció en el banco') as e:
            vb.exigir_para_rc(r)
        assert not isinstance(e.value, DependenciaPendiente)

    def test_el_switch_apaga_la_espera_no_la_no_encontrada(self, db, almacen, monkeypatch):
        from app.services import verificacion_banco as vb
        from app.services.siesa_job_service import ErrorDeterminista
        monkeypatch.setenv('TRANSFERENCIA_EXIGE_VERIFICACION', 'false')
        _, r = _bancaria(db, almacen)
        vb.exigir_para_rc(r)                     # por verificar: ya no espera
        vb.verificar(r.id, _usuario(db).id, False, 'no está')
        with pytest.raises(ErrorDeterminista):
            vb.exigir_para_rc(r)

    @pytest.mark.parametrize('valor,exige', [(None, True), ('', True), ('true', True),
                                              ('ture', True), ('false', False), ('0', False)])
    def test_solo_un_no_explicito_apaga(self, monkeypatch, valor, exige):
        from app.services import verificacion_banco as vb
        if valor is None:
            monkeypatch.delenv('TRANSFERENCIA_EXIGE_VERIFICACION', raising=False)
        else:
            monkeypatch.setenv('TRANSFERENCIA_EXIGE_VERIFICACION', valor)
        assert vb.exige_verificacion() is exige

    def test_efectivo_no_pasa_por_la_compuerta(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen, 'EFECTIVO')
        vb.exigir_para_rc(r)


class TestVerificar:

    def test_autor_hora_y_bitacora(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        u = _usuario(db, 'liquidador')
        d = vb.verificar(r.id, u.id, True)
        assert d['verificado_banco_resultado'] == 'VERIFICADA'
        db.session.refresh(r)
        assert r.verificado_banco_por == u.id and r.verificado_banco_en is not None
        assert BitacoraAccion.query.filter_by(entidad='RecaudoEntrega', entidad_id=r.id,
                                              accion='EDITAR').count() == 1

    def test_no_aparecio_exige_nota(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        with pytest.raises(ValueError):
            vb.verificar(r.id, _usuario(db).id, False, '  ')

    def test_con_el_recibo_ya_enviado_no_se_cambia(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        vb.verificar(r.id, _usuario(db).id, True)
        r.siesa_rc_triggered = True
        db.session.commit()
        with pytest.raises(ValueError, match='ya salió'):
            vb.verificar(r.id, _usuario(db).id, False, 'me equivoqué')

    def test_efectivo_no_se_verifica_en_el_banco(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen, 'EFECTIVO')
        with pytest.raises(ValueError, match='nada que verificar'):
            vb.verificar(r.id, _usuario(db).id, True)

    def test_despierta_al_recibo_que_esperaba(self, db, almacen):
        import json
        from app.models.siesa_job import SiesaJob
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        j = SiesaJob(tipo='RECIBO_CAJA', referencia_tipo='RecaudoEntrega', referencia_id=r.id,
                     payload=json.dumps({'recaudo_id': r.id}), estado='PENDIENTE',
                     proximo_intento=datetime.utcnow() + timedelta(hours=1))
        db.session.add(j)
        db.session.commit()
        vb.verificar(r.id, _usuario(db).id, True)
        db.session.refresh(j)
        assert j.proximo_intento <= datetime.utcnow()


class TestLaCola:

    def test_por_verificar_sin_la_foto(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        _, ok = _bancaria(db, almacen)
        vb.verificar(ok.id, _usuario(db).id, True)
        _, no = _bancaria(db, almacen)
        vb.verificar(no.id, _usuario(db).id, False, 'no está')
        filas = {f['recaudo_id']: f for f in vb.por_verificar()}
        assert set(filas) >= {r.id, no.id} and ok.id not in filas
        assert filas[r.id]['referencia'] == '8877' and filas[r.id]['tiene_foto'] is True
        assert 'foto' not in filas[r.id]
        assert vb.comprobante(r.id)['foto'] == FOTO

    def test_un_comprobante_sin_encabezado_se_completa(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen, foto=False)
        r.foto_comprobante = '/9j/abc'
        db.session.commit()
        assert vb.comprobante(r.id)['foto'].startswith('data:image/jpeg;base64,')

    def test_aviso_de_mas_de_un_dia(self, db, almacen):
        from app.services import verificacion_banco as vb
        _, r = _bancaria(db, almacen)
        r.fecha_confirmacion = r.fecha_cobro = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()
        assert any('sin verificar en el banco' in x for x in vb.lineas_de_aviso())


class TestLaPlanillaNoBajaFotos:

    def test_sin_fotos_en_la_planilla(self, app, client, db, almacen):
        ruta, r = _bancaria(db, almacen)
        r.foto_entrega = FOTO
        db.session.commit()
        d = client.get(f'/api/rutas/{ruta.id}/planilla',
                       headers=_jwt(app, _usuario(db, 'liquidador'))).get_json()
        rec = d['paradas'][0]['recaudo']
        assert 'foto_comprobante' not in rec and 'foto_entrega' not in rec
        assert rec['tiene_foto_comprobante'] is True


class TestPorHTTP:

    @pytest.mark.parametrize('rol,codigo', [('admin', 200), ('liquidador', 200),
                                            ('lider_cartera', 200), ('gerente', 403),
                                            ('jefe_almacen', 403), ('conductor', 403)])
    def test_quien_verifica(self, app, client, db, almacen, rol, codigo):
        _, r = _bancaria(db, almacen)
        res = client.post(f'/api/rutas/recaudos/{r.id}/verificar-banco',
                          headers=_jwt(app, _usuario(db, rol)), json={'encontrada': True})
        assert res.status_code == codigo

    def test_la_lista_y_el_comprobante(self, app, client, db, almacen):
        _, r = _bancaria(db, almacen)
        h = _jwt(app, _usuario(db, 'gerente'))
        d = client.get('/api/rutas/transferencias/por-verificar', headers=h).get_json()
        assert any(f['recaudo_id'] == r.id for f in d['transferencias'])
        assert d['permisos']['verificar'] is False
        f = client.get(f'/api/rutas/recaudos/{r.id}/comprobante', headers=h).get_json()
        assert f['foto'] == FOTO

    def test_el_conductor_no_baja_comprobantes(self, app, client, db, almacen):
        _, r = _bancaria(db, almacen)
        res = client.get(f'/api/rutas/recaudos/{r.id}/comprobante',
                         headers=_jwt(app, _usuario(db, 'conductor')))
        assert res.status_code == 403


# ═════════════════════════════════════════════════════════════════════════
# La integración con el ejecutor del RC (lo cablea el frente L1 / el integrador)
# ═════════════════════════════════════════════════════════════════════════

def _rama_rc_llama_la_compuerta() -> bool:
    arbol = ast.parse((RAIZ / 'app' / 'services' / 'siesa_job_service.py')
                      .read_text(encoding='utf-8'))
    for n in ast.walk(arbol):
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare) \
                and any(isinstance(c, ast.Constant) and c.value == 'RECIBO_CAJA'
                        for c in n.test.comparators):
            for m in ast.walk(n):
                if isinstance(m, ast.Call):
                    f = m.func
                    nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
                    if nombre == 'exigir_para_rc':
                        return True
    return False


class TestElEjecutorDelReciboEsperaAlBanco:

    @pytest.mark.xfail(strict=True, reason=(
        'La línea `verificacion_banco.exigir_para_rc(recaudo)` va en la rama '
        'RECIBO_CAJA de siesa_job_service._ejecutar_job, antes del POST: ese '
        'archivo es del frente L1 y lo cablea el integrador. Cuando exista, '
        'este test pasa y strict=True lo pone rojo para que se quite la marca.'))
    def test_la_rama_del_rc_pregunta_al_banco(self):
        assert _rama_rc_llama_la_compuerta()

    def test_el_detector_ve_la_llamada(self, tmp_path):
        """Meta-test: el detector encuentra la llamada dentro de la rama, y no
        una en un docstring."""
        fuente = ("def f(job):\n"
                  "    if job.tipo == 'RECIBO_CAJA':\n"
                  "        '''exigir_para_rc(recaudo)'''\n"
                  "        _vb.exigir_para_rc(recaudo)\n")
        arbol = ast.parse(fuente)
        encontrada = any(isinstance(m, ast.Call) and getattr(m.func, 'attr', '') == 'exigir_para_rc'
                         for m in ast.walk(arbol))
        assert encontrada
        sin = ast.parse("def f(job):\n    if job.tipo == 'RECIBO_CAJA':\n"
                        "        '''exigir_para_rc(recaudo)'''\n")
        assert not any(isinstance(m, ast.Call) for m in ast.walk(sin))


# ═════════════════════════════════════════════════════════════════════════
# El Gestor de Cartera sabe lo que ya está en caja (P2-6)
# ═════════════════════════════════════════════════════════════════════════

class TestElGestorSabeLoQueEstaEnCaja:

    def _parada(self, db, almacen, clave, forma='EFECTIVO', monto=80000, nit='900111222'):
        from tests.test_cartera_retencion import _historia
        _historia(db, clave, nit=nit, cond='C02')
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('ENTREGADO', forma, monto, monto)])
        r.tarea.pedido_clave = clave
        r.tarea.fe_tipo, r.tarea.fe_consec = 'FE', clave.split('-')[-1]
        db.session.commit()
        return r

    def test_lo_cobrado_sin_recibo_esta_en_caja(self, app, client, db, almacen, monkeypatch):
        from tests.test_cartera_retencion import TOKEN
        monkeypatch.setenv('CARTERA_GESTOR_TOKEN', TOKEN)
        self._parada(db, almacen, '003-PD-7001')
        no = self._parada(db, almacen, '003-PD-7002', forma='TRANSFERENCIA_BBVA')
        from app.services import verificacion_banco as vb
        vb.verificar(no.id, _usuario(db).id, False, 'no está en el extracto')
        self._parada(db, almacen, '003-PD-7003', nit='800000000')      # otro cliente
        r = client.get('/api/cartera/en-caja?nit=900111222-5',
                       headers={'Authorization': f'Bearer {TOKEN}'})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert [f['pedido_clave'] for f in d['en_caja']] == ['003-PD-7001']
        assert d['en_caja'][0]['estado'] == 'EN_CAJA_CONDUCTOR' and d['total_en_caja'] == 80000
        assert [f['pedido_clave'] for f in d['no_encontradas_en_banco']] == ['003-PD-7002']

    def test_sin_token_o_sin_nit(self, app, client, db, monkeypatch):
        from tests.test_cartera_retencion import TOKEN
        monkeypatch.setenv('CARTERA_GESTOR_TOKEN', TOKEN)
        assert client.get('/api/cartera/en-caja?nit=1').status_code in (401, 403)
        assert client.get('/api/cartera/en-caja',
                          headers={'Authorization': f'Bearer {TOKEN}'}).status_code == 400

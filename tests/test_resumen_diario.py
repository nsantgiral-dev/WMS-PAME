"""
El resumen diario por correo, reparado (P1-14) y con las alertas que no
tenían canal (2026-09-25).

Qué pasaba:
· importaba `PedidoPicking`, que no existe: «Pedidos despachados: N/D» todos
  los días, dentro de un `except` que lo tragaba;
· la ventana de «ayer» eran medianoches naive contra columnas UTC: corrida 5 h;
· reescribía VTA-30 («DESPACHADO sin bultos») con una consulta propia, sin el
  corte de `FECHA_INICIO_AUDITORIA`.

Ahora: despachos por `metricas.pedidos_despachados` en el día Bogotá, la
auditoría por `auditoria.auditar` (con el corte) y `avisos_sin_canal` junta lo
que ningún otro canal avisaba — cartera retenida > 3 días, traslado en limbo
> 24 h, BLOQUEA de la auditoría (con cuántos nuevos ayer), la carga física que
no escribió, crons que fallan o callan y el sello de ambiente.

Trinquete de clase: **un import que no existe se esconde en un `except`**. Todo
`from app… import X` / `from flota… import X` de `app/` y `flota/` resuelve.
"""
import ast
import importlib
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

from app.services import alertas_service

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _correr(app):
    capturado = {}

    def _fake(fecha, pedidos, bultos, rep, ok, fallidos, anomalias=None):
        capturado.update(fecha=fecha, pedidos=pedidos, anomalias=anomalias or [])
    with patch.object(alertas_service, '_enviar_resumen_diario', side_effect=_fake):
        alertas_service.enviar_resumen_diario(app)
    return capturado


def _despacho(db, almacen, cuando_utc, n):
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo=f'PK-RES-{n}', estado='DESPACHADO', almacen_id=almacen.id,
                     numero_pedido_siesa=f'PD{n}', fecha_despachado=cuando_utc)
    db.session.add(t)
    db.session.commit()


class TestElResumenCuentaAyerEnBogota:

    def test_los_despachos_de_ayer_por_la_noche_cuentan(self, app, db, almacen):
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        ayer = dia_operativo() - timedelta(days=1)
        inicio = inicio_del_dia_utc(ayer)                    # 00:00 Bogotá de ayer, en UTC
        _despacho(db, almacen, inicio + timedelta(hours=20, minutes=30), 1)   # 20:30 de ayer
        _despacho(db, almacen, inicio + timedelta(hours=3), 2)                # 03:00 de ayer
        _despacho(db, almacen, inicio - timedelta(hours=7), 3)                # 17:00 de antier
        r = _correr(app)
        # Con medianoches naive (UTC) la de las 20:30 de ayer (01:30 UTC de
        # hoy) caía fuera: el resumen decía 1.
        assert r['pedidos'] == 2, r

    def test_ya_no_es_nd(self, app, db):
        assert _correr(app)['pedidos'] == 0


class TestAvisosSinCanal:

    def _ventana(self):
        ahora = datetime.utcnow()
        return ahora - timedelta(days=1), ahora

    def test_traslado_en_limbo(self, db, usuario_admin):
        from app.models.traslado import SolicitudTraslado
        db.session.add(SolicitudTraslado(
            codigo='ST-LIMBO-1', bodega_origen_siesa='NB1', bodega_destino_siesa='NC1',
            nombre_punto_venta='NC', estado='EN_TRANSITO', solicitante_id=usuario_admin.id,
            fecha_despacho=datetime.utcnow() - timedelta(hours=30)))
        db.session.commit()
        lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('ST-LIMBO-1' in l and '24 h' in l for l in lineas), lineas

    def test_cron_que_falla(self, db):
        from app.models.cron_latido import CronLatido
        db.session.add(CronLatido(nombre='dlq_siesa_jobs', servicio='web', ultimo_ok=False,
                                  ultimo_inicio=datetime.utcnow(), ultimo_fin=datetime.utcnow(),
                                  corridas=3, fallos_seguidos=3))
        db.session.commit()
        lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('dlq_siesa_jobs' in l and 'falló' in l for l in lineas), lineas

    def test_carga_fisica_que_no_escribio(self, db):
        from app.services import registro_sync_service as reg
        from app.services.inventario_siesa_service import FuenteInventarioNoConfiable
        rid = reg.abrir('stock')
        reg.cerrar_error(rid, FuenteInventarioNoConfiable(
            'No se escribe inventario de NB1: la última descarga de Siesa falló.'))
        lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('NO escribió' in l for l in lineas), lineas

    def test_existencias_viejas(self, db):
        from app.models.stock_siesa import StockSiesa
        db.session.add(StockSiesa(bodega='NC1', codigo_siesa='X', existencia=1,
                                  updated_at=datetime.utcnow() - timedelta(days=2)))
        db.session.commit()
        lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('NC1' in l and '24 h' in l for l in lineas), lineas

    def test_cartera_retenida_mas_de_3_dias(self, db):
        with patch('app.services.cartera_service.salud',
                   return_value={'alertas': [{'pedido': 'PD77', 'dias': 5}]}):
            lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('PD77' in l and 'cartera' in l for l in lineas), lineas

    def test_auditoria_bloquea_con_nuevos(self, db):
        rep = {'bloqueantes': 2, 'resultados': [{
            'codigo': 'VTA-30', 'severidad': 'BLOQUEA', 'total': 2, 'hallazgos': [
                {'fecha': (datetime.utcnow() - timedelta(hours=3)).isoformat()},
                {'fecha': (datetime.utcnow() - timedelta(days=9)).isoformat()}]}]}
        with patch('app.services.auditoria.auditar', return_value=rep):
            lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('VTA-30 (2)' in l and '1 nuevos ayer' in l for l in lineas), lineas

    def test_una_fuente_que_revienta_no_tumba_las_otras(self, db):
        with patch('app.services.cartera_service.salud', side_effect=RuntimeError('x')):
            lineas = alertas_service.avisos_sin_canal(*self._ventana())
        assert any('No se pudo revisar la cartera' in l for l in lineas)

    def test_nada_que_avisar_es_lista_vacia(self, db):
        assert alertas_service.avisos_sin_canal(*self._ventana()) == []

    def test_el_resumen_no_arma_consultas_de_invariantes_propias(self):
        """VTA-30 vive en la auditoría: el resumen no vuelve a consultar
        `TareaPacking.estado == 'DESPACHADO'` sin bultos por su cuenta."""
        src = (RAIZ / 'app/services/alertas_service.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == 'enviar_resumen_diario')
        consts = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)}
        assert 'DESPACHADO' not in consts


# ─────────────────────────────────────────────────────────────────────────────
# La clase: ningún import roto escondido en un except
# ─────────────────────────────────────────────────────────────────────────────

def _imports_rotos(base=None):
    base = base or RAIZ
    rotos = []
    for carpeta in ('app', 'flota'):
        for f in sorted((base / carpeta).rglob('*.py')):
            for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if not (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
                        and n.module.split('.')[0] in ('app', 'flota')):
                    continue
                try:
                    m = importlib.import_module(n.module)
                except Exception as e:     # noqa: BLE001
                    rotos.append((str(f.relative_to(base)), n.lineno, n.module, repr(e)[:80]))
                    continue
                for a in n.names:
                    if a.name == '*' or hasattr(m, a.name):
                        continue
                    try:
                        importlib.import_module(f'{n.module}.{a.name}')
                    except Exception:      # noqa: BLE001
                        rotos.append((str(f.relative_to(base)), n.lineno, n.module, a.name))
    return rotos


class TestNingunImportRoto:

    def test_todo_import_interno_resuelve(self, app):
        rotos = _imports_rotos()
        assert not rotos, ('Imports que no existen (un except los vuelve «N/D» callado):\n'
                           + '\n'.join(map(str, rotos)))

    def test_el_detector_ve_el_nombre_inexistente(self, app, tmp_path):
        (tmp_path / 'app').mkdir()
        (tmp_path / 'flota').mkdir()
        (tmp_path / 'app' / 'x.py').write_text(
            'def f():\n    try:\n        from app.models.picking import PedidoPicking\n'
            '    except Exception:\n        return "N/D"\n'
            'from app.models.picking import TareaPicking\n', encoding='utf-8')
        assert [r[3] for r in _imports_rotos(tmp_path)] == ['PedidoPicking']

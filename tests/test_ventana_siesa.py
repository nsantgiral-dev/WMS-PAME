"""
Una ventana de Siesa, una frescura de `stock_siesa` y la cola que no confunde
esperar con atascarse (P2, 2026-09-25).

1. **Ventana**. Había cuatro definiciones (7–20, 6–20, 7–19:30, 7–21) y crons
   que le hablaban a Siesa a las 2:00, 2:30, 3:00, 5:55 o 24/7: empaques y
   ubicaciones fallaban de madrugada sin registro, y el DLQ intentaba un
   recibo de caja a las 20:30 que amanecía FALLIDO. Ahora `ventana_siesa`
   (06:00–19:30 Bogotá) es la única; todo cron que habla con Siesa va envuelto
   en `solo_en_ventana_siesa`; el DLQ fuera de ella solo procesa lo que no va a
   Siesa (`TIPOS_SIN_SIESA`).
2. **Frescura**. Vigía tomaba el MAX global de `stock_siesa.updated_at`: una
   bodega refrescada hacía ver fresco todo. `frescura_stock_siesa`: una bodega
   es tan fresca como su último refresco; un conjunto, como su bodega menos
   reciente.
3. **Cola**. Un despacho retenido por cartera espera una decisión de cartera:
   la 🩺 Salud ya no lo cuenta como cola atascada.

Trinquetes (AST): todo `add_job` está clasificado —habla con Siesa (envuelto)
o no (con su porqué)—; nadie más declara una ventana de Siesa; nadie más lee
`StockSiesa.updated_at` para decidir frescura.
"""
import ast
import pathlib
from datetime import datetime, time, timedelta

import pytest

from app.services import ventana_siesa as vs

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Crons que le hablan a Siesa: van envueltos en `solo_en_ventana_siesa`.
HABLAN_CON_SIESA = {
    'pedidos_siesa_sync', 'sync_productos_siesa', 'sync_barcodes_siesa',
    'sync_empaques_siesa', 'sync_ubicaciones_siesa', 'reconciliacion_despachos',
    'stock_prewarm', 'abc_prewarm_pre_turno', 'vigia_alimentar_series',
    'fotos_siesa_diarias', 'cartera_barrido', 'devoluciones_verificar_nc',
    'compras_oc_abiertas', 'compras_oc_diario', 'kardex_auto',
}
#: El DLQ decide adentro: fuera de ventana procesa solo `TIPOS_SIN_SIESA`.
DECIDEN_ADENTRO = {
    'dlq_siesa_jobs': 'Fuera de ventana procesa solo los correos de alerta (TIPOS_SIN_SIESA).',
}
#: Crons que NO hablan con Siesa, con su porqué. Solo encoge / se mueve arriba.
NO_HABLAN = {
    'abc_conteo_diario': 'Genera tareas de conteo desde datos locales; el teórico de Siesa '
                         'se toma al abrir la tarea, no al generarla.',
    'conteo_liberar_zombis': 'Libera sesiones EN_PROCESO abandonadas: solo la base del WMS.',
    'alertas_huerfanas_email': 'Lee la base del WMS y manda correo (Resend).',
    'alertas_stock_critico': 'Lee stock_siesa ya guardado y manda correo; no consulta Siesa.',
    'alertas_rutas_sin_liquidar': 'Lee rutas del WMS y manda correo.',
    'resumen_operativo_diario': 'Lee la base (métricas, auditoría) y manda correo.',
    'analitica_kpi_diario': 'Calcula el KPI desde tablas del WMS y fotos ya tomadas.',
    'reposicion_barrido_stock_picking': 'Compara huecos PICKING contra su mínimo: local.',
    'monitor_traslados_transito': 'Lee traslados EN_TRANSITO de la base y loguea.',
    'flota_avisos_barrido': 'Vencimientos de papeles de flota; avisa por WhatsApp.',
    'flota_preventivo_siembra': 'Siembra el plan preventivo desde la ficha técnica: local.',
    'flota_reporte_semanal': 'Reporte semanal de flota por correo: local.',
}

#: Ventanas propias que no son «la de Siesa». Solo encoge.
VENTANAS_PROPIAS = {
    'app/services/kardex_auto.py::VENTANA_DEFAULT':
        'Sub-ventana del kardex (07:00–07:55), recortada a la de Siesa en kardex_auto.ventana().',
}


class TestLaVentana:

    @pytest.mark.parametrize('h,m,abierta', [(5, 59, False), (6, 0, True), (12, 0, True),
                                            (19, 30, True), (19, 31, False), (22, 0, False)])
    def test_los_bordes(self, h, m, abierta):
        assert vs.ventana_abierta(datetime(2026, 9, 25, h, m)) is abierta

    def test_el_envoltorio_no_corre_fuera(self):
        vs._RELOJ_FIJO['abierta'] = False
        llamado = []
        r = vs.solo_en_ventana_siesa(lambda: llamado.append(1))()
        assert 'omitido' in r and not llamado
        vs._RELOJ_FIJO['abierta'] = True
        vs.solo_en_ventana_siesa(lambda: llamado.append(1))()
        assert llamado == [1]

    def test_el_dlq_fuera_de_ventana_no_toca_lo_que_va_a_siesa(self, db):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _run_dlq_jobs
        rc = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': 1})
        correo = SiesaJob.encolar('ALERTA_EMAIL', {'asunto': 'x', 'cuerpo_texto': 'y'})
        db.session.commit()
        vs._RELOJ_FIJO['abierta'] = False
        _run_dlq_jobs()
        db.session.expire_all()
        rc = db.session.get(SiesaJob, rc.id)
        assert rc.estado == 'PENDIENTE' and rc.intentos == 0, (
            'el recibo de caja se intentó fuera de la ventana de Siesa')
        c = db.session.get(SiesaJob, correo.id)
        assert c.estado != 'PENDIENTE' or c.intentos > 0, 'el correo tenía que salir igual'


def _jobs(base=None):
    base = base or RAIZ
    out = []
    for carpeta in ('app', 'flota'):
        for f in sorted((base / carpeta).rglob('*.py')):
            for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'add_job'):
                    continue
                ident = next((k.value.value for k in n.keywords if k.arg == 'id'
                              and isinstance(k.value, ast.Constant)), None)
                fn = next((k.value for k in n.keywords if k.arg == 'func'), None) or (
                    n.args[0] if n.args else None)
                en_ventana = any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                                 and x.func.id == 'solo_en_ventana_siesa'
                                 for x in ast.walk(fn)) if fn is not None else False
                out.append((ident, en_ventana))
    return out


class TestTodoCronEstaClasificado:

    def test_ninguno_sin_clasificar(self):
        ids = {i for i, _ in _jobs()}
        sin = ids - HABLAN_CON_SIESA - set(DECIDEN_ADENTRO) - set(NO_HABLAN)
        assert not sin, (f'Crons sin clasificar: {sorted(sin)}. ¿Le hablan a Siesa? Entonces '
                         'envolverlos en solo_en_ventana_siesa; si no, declararlos en NO_HABLAN.')

    def test_las_listas_solo_encogen(self):
        ids = {i for i, _ in _jobs()}
        assert (HABLAN_CON_SIESA | set(DECIDEN_ADENTRO) | set(NO_HABLAN)) <= ids

    def test_los_que_hablan_van_envueltos(self):
        sin = [i for i, env in _jobs() if i in HABLAN_CON_SIESA and not env]
        assert not sin, f'Hablan con Siesa y corren fuera de la ventana: {sin}'

    def test_toda_declaracion_dice_por_que(self):
        assert all(len(v) >= 30 for v in list(NO_HABLAN.values()) + list(DECIDEN_ADENTRO.values()))

    def test_el_detector_ve_el_envoltorio(self, tmp_path):
        (tmp_path / 'app').mkdir()
        (tmp_path / 'flota').mkdir()
        (tmp_path / 'app' / 'x.py').write_text(
            "def i(s):\n"
            "    s.add_job(func=con_latido('a', solo_en_ventana_siesa(f)), id='a')\n"
            "    s.add_job(func=con_latido('b', f), id='b')\n", encoding='utf-8')
        assert _jobs(tmp_path) == [('a', True), ('b', False)]

    def test_piso(self):
        assert len(_jobs()) >= 25 and sum(1 for _i, e in _jobs() if e) >= 14


def _ventanas_declaradas(base=None):
    """`archivo::NOMBRE` de toda asignación `*VENTANA* = (time(...), time(...))`."""
    base = base or RAIZ
    out = set()
    for f in sorted((base / 'app').rglob('*.py')):
        for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
            if not isinstance(n, ast.Assign) or len(n.targets) != 1:
                continue
            t = n.targets[0]
            if not (isinstance(t, ast.Name) and 'VENTANA' in t.id):
                continue
            v = n.value
            if isinstance(v, ast.Tuple) and v.elts and all(
                    isinstance(e, ast.Call) and isinstance(e.func, ast.Name)
                    and e.func.id == 'time' for e in v.elts):
                out.add(f'{f.relative_to(base)}::{t.id}')
    return out


class TestUnaSolaVentana:

    def test_nadie_mas_declara_una_ventana(self):
        extra = _ventanas_declaradas() - {'app/services/ventana_siesa.py::VENTANA'} \
            - set(VENTANAS_PROPIAS)
        assert not extra, f'Otra ventana de Siesa: {sorted(extra)} — usá ventana_siesa.VENTANA'

    def test_la_propia_existe(self):
        assert set(VENTANAS_PROPIAS) <= _ventanas_declaradas()

    def test_el_detector_ve_la_copia(self, tmp_path):
        (tmp_path / 'app').mkdir()
        (tmp_path / 'app' / 'x.py').write_text(
            'MI_VENTANA = (time(7, 0), time(20, 0))\nOTRA = (time(1, 0), time(2, 0))\n'
            '"""VENTANA = (time(1, 0), time(2, 0))"""\n', encoding='utf-8')
        assert _ventanas_declaradas(tmp_path) == {'app/x.py::MI_VENTANA'}


# ─────────────────────────────────────────────────────────────────────────────
# Frescura de stock_siesa: una definición
# ─────────────────────────────────────────────────────────────────────────────

class TestLaFrescura:

    def _stock(self, db, bodega, cuando, n=1):
        from app.models.stock_siesa import StockSiesa
        for i in range(n):
            db.session.add(StockSiesa(bodega=bodega, codigo_siesa=f'{bodega}-{i}',
                                      existencia=1, updated_at=cuando))
        db.session.commit()

    def test_un_conjunto_es_tan_fresco_como_su_bodega_mas_vieja(self, db):
        from app.services.inventario_siesa_service import frescura_stock_siesa
        ahora = datetime.utcnow()
        self._stock(db, 'NB1', ahora)
        self._stock(db, 'NC1', ahora - timedelta(days=3))
        f = frescura_stock_siesa(['NB1', 'NC1', 'PT1'])
        assert f['actualizado_en'] == ahora - timedelta(days=3)
        assert f['sin_dato'] == ['PT1']

    def test_vigia_ya_no_ve_fresco_lo_viejo(self, db):
        from app.services.inventario_siesa_service import _BODEGAS_PV
        from app.services.vigia_service import VigiaService
        ahora = datetime.utcnow()
        self._stock(db, 'NB1', ahora)
        self._stock(db, 'NC1', ahora - timedelta(days=3))
        assert 'NC1' in _BODEGAS_PV
        g0 = VigiaService.salud_conectores()
        stock = next(c for c in g0['conectores'] if c['nombre'].startswith('Stock Siesa'))
        assert stock['latencia_horas'] >= 72 and stock['ok'] is False

    def test_nadie_mas_lee_updated_at_de_stock_siesa(self):
        permitidos = {'app/services/inventario_siesa_service.py',
                      # Misma regla por SKU (la fila más vieja): declarada.
                      'app/services/armador_service.py'}
        hallados = set()
        for f in sorted((RAIZ / 'app').rglob('*.py')):
            for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if (isinstance(n, ast.Attribute) and n.attr == 'updated_at'
                        and isinstance(n.value, ast.Name) and n.value.id == 'StockSiesa'):
                    hallados.add(str(f.relative_to(RAIZ)))
        assert hallados <= permitidos, hallados - permitidos
        assert 'app/services/inventario_siesa_service.py' in hallados


# ─────────────────────────────────────────────────────────────────────────────
# La cola: esperar a cartera no es estar atascado
# ─────────────────────────────────────────────────────────────────────────────

class TestLaColaNoCuentaLaEsperaDeCartera:

    def test_un_despacho_retenido_no_atasca_la_cola(self, db, almacen):
        from app.models.cartera import RetencionCartera
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.services.analitica_salud import cola_siesa
        t = TareaPacking(codigo='PK-RET-1', estado='VERIFICADO', almacen_id=almacen.id,
                         numero_pedido_siesa='PD1', pedido_clave='003-PD-1')
        db.session.add(t)
        db.session.flush()
        db.session.add(RetencionCartera(pedido_clave='003-PD-1', nit='900', compuerta='EMISION',
                                        estado='RETENIDO', tarea_packing_id=t.id))
        j = SiesaJob.encolar('DESPACHO_F470', {'tarea_id': t.id},
                             referencia_tipo='TareaPacking', referencia_id=t.id)
        j.fecha_creacion = datetime.utcnow() - timedelta(days=2)
        db.session.commit()
        c = cola_siesa(datetime.utcnow())
        assert c['en_espera_de_cartera'] == 1 and c['nivel'] == 'ok', c
        assert 'cartera' in c['texto']

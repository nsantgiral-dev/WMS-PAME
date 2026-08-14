"""
La tanda media de la auditoría del 2026-08-13: E, F, G, I, J, K, L.

Siete defectos con siete consecuencias distintas, pero **cuatro comparten una
sola forma**: un valor con dos significados.

    F  el IVA inventado          ← una fórmula distinta en el tercer sitio
    J  el guard mal delimitado   ← «¿el PEDIDO tiene FE?» donde había que
                                    preguntar «¿esta REMISIÓN tiene FE?»
    K  el borrado tras un 429    ← «no vino en la respuesta» = «ya no existe»
    L  el sello fresco           ← `utcnow()` sobre un dato que salió de la BD

Y dos más comparten otra: un contador que mide dos cosas (E) y un guard en la
capa equivocada (G).
"""
import pytest


# ── F · la base del reteIVA ──────────────────────────────────────────────

class TestElIvaNoSeInventa:
    """`base_gravable * 0.19` estaba en `rutas.py` — CLAUDE.md lo prohíbe:
    «usar API 45 (`f461_vlr_bruto`, `f461_vlr_imp`), NO dividir por 1.19»."""

    def test_reteiva_va_sobre_el_iva_real(self):
        from app.services.liquidacion_service import base_de_retencion
        # Factura con exentas: subtotal 1.000.000, IVA real 100.000 (no 190.000)
        assert base_de_retencion('RETEIVA', 1_000_000, 100_000) == 100_000

    def test_las_demas_van_sobre_el_subtotal(self):
        from app.services.liquidacion_service import base_de_retencion
        assert base_de_retencion('RETEFUENTE_2.5', 1_000_000, 100_000) == 1_000_000

    def test_con_lineas_exentas_la_retencion_no_se_infla(self):
        """El caso que costaba plata: el 19% inventado sobre un subtotal con
        exentas retiene de más."""
        from app.services.liquidacion_service import monto_de_retencion
        real = monto_de_retencion('RETEIVA', 1_000_000, 100_000)   # 15% de 100.000
        inventado = round(1_000_000 * 0.19 * 0.15, 2)              # la fórmula vieja
        assert real == 15_000
        assert inventado == 28_500
        assert real < inventado, 'la fórmula vieja retenía casi el doble'

    def test_ningun_sitio_multiplica_por_019(self):
        """Detector por AST, no por texto.

        La primera versión buscaba `'* 0.19'` en el fuente y se atrapaba a sí
        misma: el docstring de `base_de_retencion` cita la fórmula vieja como
        ejemplo de lo que no hay que hacer. Es el mismo tropiezo que CLAUDE.md
        ya documenta —los detectores de texto se encuentran en sus propios
        comentarios— y la respuesta es la misma: mirar el árbol, no la cadena.
        """
        import ast
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parents[1]
        for rel in ('app/routes/rutas.py', 'app/services/liquidacion_service.py'):
            arbol = ast.parse(raiz.joinpath(rel).read_text(encoding='utf-8'))
            for nodo in ast.walk(arbol):
                if (isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Mult)
                        and any(isinstance(x, ast.Constant) and x.value == 0.19
                                for x in (nodo.left, nodo.right))):
                    pytest.fail(f'{rel}:{nodo.lineno} multiplica por 0.19 — '
                                f'el IVA se lee de la factura, no se inventa')


# ── G · cobrar sobre una parada que no pagó ──────────────────────────────

class TestNoSeCobraLoQueNoSeEntrego:

    @pytest.mark.parametrize('estado', ['RECHAZADO', 'ENTREGADO_SIN_PAGO'])
    def test_el_servicio_rechaza_esos_estados(self, app, db, almacen, estado):
        """La validación va en el SERVICIO: el endpoint no es la única puerta,
        que es lo que acabó de costar el guard de packing."""
        import uuid

        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        from app.models.usuario import Usuario
        from app.services.liquidacion_service import LiquidacionService

        u = Usuario.query.filter_by(email='c_tm@test.com').first()
        if not u:
            u = Usuario(email='c_tm@test.com', nombre='C', rol='conductor', activo=True)
            u.set_password('t'); db.session.add(u); db.session.flush()
        ruta = RutaDespacho(conductor_id=u.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta); db.session.flush()
        tarea = TareaPacking(codigo=f'PK-TM-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                             almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                             consec_docto_pedido_siesa=1, numero_pedido_siesa='PED-TM')
        db.session.add(tarea); db.session.flush()
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=tarea.id, estado_entrega=estado,
                           forma_pago='EFECTIVO', monto_cobrado=50000,
                           motivo_rechazo='NO_PAGO_SE_QUEDO' if estado == 'ENTREGADO_SIN_PAGO' else 'NO_PAGO')
        db.session.add(r); db.session.commit()

        with pytest.raises(ValueError, match='dinero recibido'):
            LiquidacionService.registrar_cobro_recaudo(r.id, admin_id=u.id)


# ── E · esperar no es fallar ─────────────────────────────────────────────

class TestEsperarNoGastaReintento:
    """Backoff `[5,15,45,120,180]` × 5 intentos ≈ 6 horas. Lo que desbloquea el
    RC dependiente es una **recepción física** que puede ocurrir mañana."""

    def test_la_dependencia_tiene_su_propia_excepcion(self):
        from app.services.siesa_job_service import DependenciaPendiente
        assert issubclass(DependenciaPendiente, Exception)

    def test_el_dlq_la_reprograma_sin_marcar_fallo(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'siesa_job_service.py').read_text(encoding='utf-8')
        i = fuente.find('isinstance(e, DependenciaPendiente)')
        assert i != -1, 'el DLQ dejó de distinguir espera de fallo'
        bloque = fuente[i:i + 700]
        assert 'marcar_fallo' not in bloque
        assert 'PENDIENTE' in bloque

    def test_el_rc_dependiente_la_levanta(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'siesa_job_service.py').read_text(encoding='utf-8')
        i = fuente.find("if payload.get('depende_de_nc')")
        assert 'raise DependenciaPendiente' in fuente[i:i + 500]


# ── J · el guard anti-duplicado, por remisión ────────────────────────────

class TestElGuardPreguntaPorLaRemision:
    """Preguntar «¿el PEDIDO tiene FE?» antes de facturar una remisión hace que
    el **segundo despacho parcial** se marque hecho sin factura: la FE del
    primer parcial contesta que sí."""

    @staticmethod
    def _cuerpo(nombre):
        import pathlib
        f = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
             / 'despacho_parcial_service.py').read_text(encoding='utf-8')
        i = f.find(f'def {nombre}')
        j = f.find('\n    def ', i + 10)
        return f[i:j]

    @pytest.mark.parametrize('fn', ['facturar_remision_existente',
                                    'facturar_rm_con_consec'])
    def test_usa_el_guard_de_remision(self, fn):
        cuerpo = self._cuerpo(fn)
        assert 'get_factura_desde_remision' in cuerpo, (
            f'{fn} volvió a preguntar por el pedido: el segundo parcial se '
            f'marcaría hecho sin facturar su remisión')

    def test_el_guard_correcto_ya_no_esta_huerfano(self):
        """Existía, estaba probado y no lo llamaba nadie."""
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parents[1]
        usos = sum(
            raiz.joinpath(r).read_text(encoding='utf-8').count('get_factura_desde_remision(')
            for r in ('app/services/despacho_parcial_service.py',))
        assert usos >= 2


# ── K · un barrido incompleto no borra ───────────────────────────────────

class TestUnBarridoIncompletoNoBorra:
    """Una respuesta 429 a mitad de sincronización dejaba fuera de
    `claves_activas` los pedidos de las páginas no leídas — y se borraban,
    reportados en `eliminados` como una limpieza normal."""

    @staticmethod
    def _fuente():
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                / 'pedidos_sync_service.py').read_text(encoding='utf-8')

    def test_solo_la_ultima_pagina_marca_el_barrido_completo(self):
        f = self._fuente()
        assert f.count('paginacion_completa = True') == 1, (
            'más de un camino declara el barrido completo — uno de ellos será '
            'un abort')

    def test_el_borrado_depende_del_barrido_completo(self):
        f = self._fuente()
        assert '] if paginacion_completa else []' in f

    def test_el_resultado_declara_si_fue_completo(self):
        """Sin esto, `eliminados: 0` tras un barrido roto se lee como «no había
        nada que borrar»."""
        f = self._fuente()
        assert "'paginacion_completa': paginacion_completa" in f
        assert "'motivo_incompleta': motivo_incompleta" in f


# ── L · no se sella como fresco lo que salió de la BD ────────────────────

class TestNoSeSellaComoFrescoLoViejo:

    @staticmethod
    def _fuente():
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                / 'inventario_siesa_service.py').read_text(encoding='utf-8')

    def test_la_marca_de_tiempo_solo_avanza_con_datos_de_siesa(self):
        f = self._fuente()
        i = f.find("_cache_inventario_multibodega['degradado'] = _degradado")
        assert i != -1
        assert 'if not _degradado:' in f[i:i + 200]

    def test_el_cache_declara_que_esta_degradado(self):
        f = self._fuente()
        assert "'degradado': False" in f


# ── I · el campo que el 173066 omitía ────────────────────────────────────

class TestEl173066MandaTodoSuSpec:

    def test_no_falta_ningun_campo_del_spec(self):
        import pathlib
        import re
        import zipfile
        from html import unescape

        raiz = pathlib.Path(__file__).resolve().parents[1]
        spec = raiz / 'docs' / 'siesa-specs' / '173066 - API_v1_Inventarios_Comercial_TransferenciaDirecta.docx'
        xml = zipfile.ZipFile(spec).read('word/document.xml').decode('utf-8')
        txt = unescape(re.sub(r'<[^>]+>', '', re.sub(r'</w:p>', '\n', xml)))
        campos = [re.match(r'"([A-Za-z0-9_]+)":', l.strip()).group(1)
                  for l in txt.split('\n') if re.match(r'\s*"([A-Za-z0-9_]+)":', l)]

        gw = raiz.joinpath('app/services/connekta_gateway.py').read_text(encoding='utf-8')
        i = gw.find('def transferir_entre_ubicaciones')
        cuerpo = gw[i:gw.find('\n    def ', i + 10)]
        # La sección `f479_*` (seriales/garantía) no la manda ninguno de los
        # tres conectores hermanos — es consistente, no un olvido.
        faltan = [c for c in campos
                  if f"'{c}'" not in cuerpo and not c.startswith('f479_')]
        assert not faltan, f'173066 omite campos de su spec: {faltan}'

    def test_lo_manda_igual_que_sus_hermanos(self):
        import pathlib
        gw = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
              / 'connekta_gateway.py').read_text(encoding='utf-8')
        for fn in ('transferir_entre_ubicaciones', 'transferencia_transito_salida',
                   'transferencia_transito_entrada'):
            i = gw.find(f'def {fn}')
            cuerpo = gw[i:gw.find('\n    def ', i + 10)]
            assert "'f470_rowid_movto': 0" in cuerpo, fn

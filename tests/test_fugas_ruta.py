"""
Fugas de plata en ruta — lo que el conductor cobra y declara en la calle.

Cada bloque cierra una CLASE, no solo el caso del contrato:

1. **Un cobro bancario sin comprobante.** El RC mandaba `'APP'` o las notas en
   `F358_REFERENCIA_OTROS`: una transferencia falsa entraba a Siesa sin nada
   con qué cruzarla. Trinquete AST: toda llamada a `trigger_recibo_caja` en
   `app/` pasa `referencia_pago`.
2. **«No pagó y se quedó» sin evidencia**, y sin tasa por conductor.
3. **Lo declarado se pisaba con lo contado** en la devolución: la diferencia
   (faltante de retorno) desaparecía.
4. **La hora del teléfono se perdía** al sincronizar la cola offline.
5. **El rechazo no se comparaba con el punto del cliente.**
6. **El efectivo en poder de cada conductor** no se veía.
7. **El vehículo salía sin que nadie mirara la flota.** Trinquete AST: toda
   escritura del despacho pasa por `_reconocer_advertencias_flota`.
8. **Un dato de entrega ausente se rellenaba con «entregado».** Trinquete AST
   con inventario: ningún `.get('entregado', <algo>)`.

Todo son SEÑALES para el encargado (regla 2 de `flota/CLAUDE.md`): ninguna
función de acá sanciona ni cambia un estado por sí sola.
"""
import ast
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
from app.services import senales_ruta as sr
from app.services.ruta_service import AdvertenciasDeFlota, RutaService

_RAIZ = Path(__file__).resolve().parent.parent
_APP = _RAIZ / 'app'

#: Un JPEG mínimo válido (1×1) en base64 — alcanza para «hay foto».
_FOTO = ('/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
         'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB'
         'AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==')


# ─────────────────────────────────────────────────────────────────────────────
# Mundo
# ─────────────────────────────────────────────────────────────────────────────

def _conductor(db, nombre='Conductor Fugas'):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    sufijo = uuid.uuid4().hex[:6]
    u = Usuario(email=f'cf_{sufijo}@test.com', nombre=nombre, rol='conductor', activo=True)
    u.set_password('x')
    db.session.add(u); db.session.flush()
    c = Conductor(usuario_id=u.id, nombre=nombre, cedula=f'CC{sufijo}', activo=True)
    db.session.add(c); db.session.flush()
    return c


def _ruta(db, almacen, conductor=None, estado='EN_TRANSITO', vehiculo_id=None,
          cliente='Tienda La Esquina', municipio='Neiva', n_bultos=2, **tarea_kw):
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    conductor = conductor or _conductor(db)
    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado=estado,
                        vehiculo_id=vehiculo_id, fecha_cierre=datetime.utcnow() - timedelta(hours=2))
    db.session.add(ruta); db.session.flush()
    sufijo = uuid.uuid4().hex[:6]
    tarea = TareaPacking(codigo=f'PK-FG-{sufijo}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=1, numero_pedido_siesa=f'PED-FG-{sufijo}',
                         cliente=cliente, municipio=municipio, **tarea_kw)
    db.session.add(tarea); db.session.flush()
    for i in range(1, n_bultos + 1):
        db.session.add(Bulto(tarea_id=tarea.id, ruta_despacho_id=ruta.id,
                             codigo_barras=f'B-{uuid.uuid4().hex[:8]}', tipo='CAJA',
                             numero=i, total=n_bultos, estado='CARGADO'))
    db.session.commit()
    return ruta, tarea, conductor


def _recaudo(ruta, tarea):
    return RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=tarea.id).first()


_V2 = {'version_formulario': sr.VERSION_FORMULARIO_CON_EVIDENCIA}


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Comprobante de pago bancario
# ═════════════════════════════════════════════════════════════════════════════

class TestQueFormaDePagoPideComprobante:

    @pytest.mark.parametrize('forma', ['TRANSFERENCIA', 'TRANSFERENCIA_BBVA',
                                       'transferencia_davivienda', 'CONSIGNACION',
                                       'TARJETA', 'CHEQUE'])
    def test_las_que_mueven_plata_fuera_de_la_caja(self, forma):
        assert sr.requiere_comprobante(forma) is True

    @pytest.mark.parametrize('forma', ['EFECTIVO', 'CREDITO', 'EXENTO', '', None])
    def test_las_que_no(self, forma):
        assert sr.requiere_comprobante(forma) is False

    def test_toda_forma_valida_del_servicio_esta_clasificada(self):
        """Una forma nueva de `FormaPago.VALIDOS` que empiece por TRANSFERENCIA
        pide comprobante sola; no hay una segunda lista que actualizar."""
        from app.services.ruta_service import FormaPago
        for f in FormaPago.TRANSFERENCIAS_BANCO:
            assert sr.requiere_comprobante(f)

    def test_referencia_se_normaliza_y_exige_minimo(self):
        assert sr.limpiar_referencia('  ab-12 34 ') == 'AB-1234'
        assert sr.limpiar_referencia('123') is None
        assert sr.limpiar_referencia(None) is None
        # Se recorta por la izquierda: los últimos dígitos identifican.
        largo = 'X' * 10 + '1234567890' * 3
        assert sr.limpiar_referencia(largo) == ('1234567890' * 3)


class TestLaParadaBancariaExigeComprobante:

    def _datos(self, **extra):
        d = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'TRANSFERENCIA_BBVA',
             'monto_cobrado': 50000, **_V2}
        d.update(extra)
        return d

    def test_sin_referencia_se_rechaza(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        with pytest.raises(ValueError, match='referencia del comprobante'):
            RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                         self._datos(foto_comprobante=_FOTO))

    def test_sin_foto_del_comprobante_se_rechaza(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        with pytest.raises(ValueError, match='foto al comprobante'):
            RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                         self._datos(referencia_pago='98765'))

    def test_con_las_dos_se_guarda_sin_recomprimir(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(referencia_pago='  98-765 ',
                                                 foto_comprobante=_FOTO))
        r = _recaudo(ruta, tarea)
        assert r.referencia_pago == '98-765'
        assert r.foto_comprobante == _FOTO, 'la foto-dato no se recomprime en servidor'
        assert sr.senales_de_recaudo(r) == []

    def test_la_parada_en_efectivo_no_pide_nada_nuevo(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(forma_pago='EFECTIVO'))
        assert _recaudo(ruta, tarea).referencia_pago is None

    def test_el_formulario_viejo_de_la_cola_no_se_traba_pero_queda_senal(self, db, almacen):
        """Un ítem de la cola offline armado por un PWA anterior: rechazarlo lo
        dejaría trabado en el teléfono para siempre. Entra, y la liquidación lo
        muestra."""
        ruta, tarea, c = _ruta(db, almacen)
        datos = self._datos()
        datos.pop('version_formulario')
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, datos)
        r = _recaudo(ruta, tarea)
        assert r.referencia_pago is None
        assert [s['clave'] for s in sr.senales_de_recaudo(r)] == ['sin_comprobante']

    def test_editar_sin_foto_nueva_conserva_las_dos_fotos(self, db, almacen):
        """Antes la edición borraba `foto_entrega`: corregir un dedazo dejaba la
        parada sin evidencia."""
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(referencia_pago='11112222',
                                                 foto_comprobante=_FOTO, foto_entrega=_FOTO))
        antes = _recaudo(ruta, tarea).foto_entrega
        assert antes
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(referencia_pago='11112222',
                                                 observaciones='corrijo'))
        r = _recaudo(ruta, tarea)
        assert r.foto_entrega == antes
        assert r.foto_comprobante == _FOTO

    def test_si_deja_de_ser_bancaria_la_referencia_vieja_se_borra(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(referencia_pago='11112222',
                                                 foto_comprobante=_FOTO))
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     self._datos(forma_pago='EFECTIVO'))
        assert _recaudo(ruta, tarea).referencia_pago is None


class TestLaReferenciaViajaAlRecibo:
    """Regla 1: `F358_REFERENCIA_OTROS` es Alfanumérico 30 en el DOCX del
    142888 («se requiere si el medio de pago es consignación u otros»)."""

    def _payload(self, app, monkeypatch, **kw):
        from app.services.connekta_gateway import ConnektaGateway, connekta
        capt = {}

        def _fake_post(self, conector, nombre, payload):
            capt['payload'] = payload
            return {'codigo': 0}
        monkeypatch.setattr(ConnektaGateway, '_post', _fake_post)
        connekta.trigger_recibo_caja('900123', '001', 1000.0, 'TRANSFERENCIA_BBVA',
                                     'FEW', '1', **kw)
        return capt['payload']['Caja'][0]

    def test_con_referencia_va_la_referencia(self, app, monkeypatch):
        with app.app_context():
            caja = self._payload(app, monkeypatch, notas='WMS Ruta #9', referencia_pago='98765')
            assert caja['F358_REFERENCIA_OTROS'] == '98765'

    def test_sin_referencia_se_conserva_lo_de_antes(self, app, monkeypatch):
        with app.app_context():
            caja = self._payload(app, monkeypatch, notas='WMS Ruta #9')
            assert caja['F358_REFERENCIA_OTROS'] == 'WMS Ruta #9'

    def test_el_largo_del_docx(self):
        from app.services.connekta_liquidacion_gateway import (
            LARGO_REFERENCIA_OTROS, referencia_otros_rc)
        assert LARGO_REFERENCIA_OTROS == 30
        assert len(referencia_otros_rc('9' * 40, '')) == 30
        assert referencia_otros_rc('', '') == 'APP'

    def test_el_largo_coincide_con_el_docx(self):
        """Se lee del .docx, no de un número copiado a mano."""
        import docx as _docx
        d = _docx.Document(str(_RAIZ / 'docs' / 'siesa-specs' / '142888 API_v1_ReciboCaja.docx'))
        filas = [[c.text.strip() for c in r.cells] for t in d.tables for r in t.rows]
        fila = next(f for f in filas if f and f[0] == 'F358_REFERENCIA_OTROS')
        from app.services.connekta_liquidacion_gateway import LARGO_REFERENCIA_OTROS
        assert int(fila[-1]) == LARGO_REFERENCIA_OTROS


# ── Trinquete de clase: el RC no sale sin la referencia del comprobante ──────

#: Llamadas a `trigger_recibo_caja` que NO pasan `referencia_pago`, con su
#: motivo. Solo encoge.
RC_SIN_REFERENCIA = {}


def _llamadas_rc(fuentes):
    """`[(archivo, línea, pasa_referencia)]` de toda llamada a
    `trigger_recibo_caja` (atributo o nombre) en `fuentes`."""
    salida = []
    for nombre, texto in fuentes:
        arbol = ast.parse(texto)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            f = nodo.func
            n = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if n != 'trigger_recibo_caja':
                continue
            pasa = any(k.arg == 'referencia_pago' for k in nodo.keywords) or \
                any(k.arg is None for k in nodo.keywords)  # **kwargs reenvía
            salida.append((nombre, nodo.lineno, pasa))
    return salida


def _fuentes_app():
    return [(str(p.relative_to(_RAIZ)), p.read_text(encoding='utf-8'))
            for p in _APP.rglob('*.py')]


class TestElReciboNoSaleSinReferencia:

    def test_toda_llamada_pasa_la_referencia(self):
        faltan = [(a, l) for a, l, pasa in _llamadas_rc(_fuentes_app())
                  if not pasa and a not in RC_SIN_REFERENCIA]
        assert not faltan, (
            f'Llamadas a trigger_recibo_caja sin referencia_pago: {faltan}.\n'
            'Un RC bancario sin la referencia del comprobante manda «APP» a '
            'Siesa: nada con qué cruzarlo contra el extracto.')

    def test_piso(self):
        # el delegado de ConnektaGateway + el ejecutor del DLQ
        assert len(_llamadas_rc(_fuentes_app())) >= 2

    def test_el_detector_ve_la_llamada_sin_referencia(self):
        codigo = 'connekta.trigger_recibo_caja(nit, "001", 1, "EFECTIVO", "FE", 1, notas=n)'
        assert _llamadas_rc([('x.py', codigo)]) == [('x.py', 1, False)]

    def test_el_detector_no_marca_la_sana(self):
        codigo = ('connekta.trigger_recibo_caja(nit, "001", 1, "EFECTIVO", "FE", 1, '
                  'referencia_pago=r)\n'
                  '# connekta.trigger_recibo_caja(sin nada)\n'
                  's = "trigger_recibo_caja(sin nada)"\n')
        assert _llamadas_rc([('x.py', codigo)]) == [('x.py', 1, True)]

    def test_el_ejecutor_del_dlq_lee_la_referencia_del_recaudo(self):
        texto = (_APP / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        assert "referencia_pago=(getattr(recaudo, 'referencia_pago', None) or '')" in texto

    def test_inventario_solo_encoge(self):
        assert len(RC_SIN_REFERENCIA) == 0
        for motivo in RC_SIN_REFERENCIA.values():
            assert motivo and len(motivo) > 20


# ═════════════════════════════════════════════════════════════════════════════
# 2 · «No pagó y se quedó con la mercancía»
# ═════════════════════════════════════════════════════════════════════════════

def _sin_pago(**extra):
    d = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
         'observaciones': 'se la llevó', **_V2}
    d.update(extra)
    return d


class TestSinPagoExigeEvidencia:

    def test_sin_foto_se_rechaza(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        with pytest.raises(ValueError, match='foto'):
            RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                         _sin_pago(geo={'fuente': 'sin_dato', 'motivo': 'sin_senal'}))

    def test_sin_estoy_aqui_se_rechaza(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        with pytest.raises(ValueError, match='Estoy aquí'):
            RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                         _sin_pago(foto_entrega=_FOTO,
                                                   geo={'fuente': 'sin_dato', 'motivo': 'no_se_pidio'}))

    def test_gps_intentado_sin_senal_alcanza(self, db, almacen):
        """Un teléfono sin señal no puede dar una coordenada; trabar la parada
        en la calle por eso no la desbloquea nadie."""
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     _sin_pago(foto_entrega=_FOTO,
                                               geo={'fuente': 'sin_dato', 'motivo': 'sin_senal'}))
        r = _recaudo(ruta, tarea)
        assert r.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO
        assert r.foto_entrega

    def test_un_rechazo_normal_no_pide_nada_nuevo(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id,
                                     _sin_pago(motivo_rechazo='CLIENTE_CERRADO'))
        assert _recaudo(ruta, tarea).estado_entrega == EstadoEntrega.RECHAZADO

    def test_formulario_viejo_entra_con_senal(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        datos = _sin_pago()
        datos.pop('version_formulario')
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, datos)
        claves = [s['clave'] for s in sr.senales_de_recaudo(_recaudo(ruta, tarea))]
        assert claves == ['sin_evidencia']

    @pytest.mark.parametrize('geo,esperado', [
        ({'lat': 2.93, 'lon': -75.28}, True),
        ({'fuente': 'sin_dato', 'motivo': 'permiso_denegado'}, True),
        ({'fuente': 'sin_dato', 'motivo': 'no_se_pidio'}, False),
        ({}, False),
        (None, False),
    ])
    def test_geo_intentado(self, geo, esperado):
        assert sr.geo_fue_intentado(geo) is esperado

    def test_el_catalogo_le_dice_a_la_pantalla_cual_exige(self):
        from app.services.motivos_rechazo import para_frontend
        exigen = {m['codigo'] for m in para_frontend() if m['exige_evidencia']}
        assert exigen == {'NO_PAGO_SE_QUEDO'}


class TestTasaSinPagoPorConductor:

    def test_tasa_con_su_denominador_y_sin_foto(self, db, almacen):
        from app.services.analitica_fugas import tasa_sin_pago_por_conductor
        from app.utils.fecha import dia_operativo
        ana = _conductor(db, 'Ana')
        beto = _conductor(db, 'Beto')
        # Ana: 1 de 2 sin pago (con foto). Beto: 1 de 1 sin pago, legado sin foto.
        r1, t1, _ = _ruta(db, almacen, conductor=ana, valor_factura=100000)
        RutaService.confirmar_parada(r1.id, t1.id, ana.usuario_id,
                                     _sin_pago(foto_entrega=_FOTO, geo={'motivo': 'sin_senal'}))
        r2, t2, _ = _ruta(db, almacen, conductor=ana)
        RutaService.confirmar_parada(r2.id, t2.id, ana.usuario_id,
                                     {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                                      'monto_cobrado': 10})
        r3, t3, _ = _ruta(db, almacen, conductor=beto)
        legado = _sin_pago(); legado.pop('version_formulario')
        RutaService.confirmar_parada(r3.id, t3.id, beto.usuario_id, legado)

        hoy = dia_operativo()
        filas = {f['conductor']: f for f in tasa_sin_pago_por_conductor(hoy, hoy)}
        assert filas['Ana']['paradas'] == 2 and filas['Ana']['sin_pago'] == 1
        assert filas['Ana']['tasa'] == 0.5 and filas['Ana']['sin_evidencia'] == 0
        assert filas['Ana']['pesos'] == 100000.0
        assert filas['Beto']['tasa'] == 1.0 and filas['Beto']['sin_evidencia'] == 1
        assert filas['Beto']['sin_valor'] == 1, 'sin valor_factura no es $0'
        # Orden: la tasa más alta primero.
        assert [f['conductor'] for f in tasa_sin_pago_por_conductor(hoy, hoy)][0] == 'Beto'

    def test_la_fuga_lo_trae_en_extra(self, db, almacen):
        from app.services.analitica_fugas import detalle_fuga
        from app.utils.fecha import dia_operativo
        c = _conductor(db, 'Carla')
        r, t, _ = _ruta(db, almacen, conductor=c, valor_factura=5000)
        RutaService.confirmar_parada(r.id, t.id, c.usuario_id,
                                     _sin_pago(foto_entrega=_FOTO, geo={'motivo': 'timeout'}))
        hoy = dia_operativo()
        d = detalle_fuga('entregado_sin_pago', hoy, hoy)
        filas = d['fuga']['extra']['por_conductor']
        assert filas and filas[0]['conductor'] == 'Carla' and filas[0]['sin_pago'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Faltante de retorno — declarado vs contado
# ═════════════════════════════════════════════════════════════════════════════

def _dev(estado='CONFIRMADA', recaudo=5, lineas=()):
    return SimpleNamespace(id=1, codigo='DEVC-1', estado=estado, recaudo_entrega_id=recaudo,
                           lineas=[SimpleNamespace(codigo_siesa=c, producto_id=i,
                                                   cantidad_declarada=dec, cantidad_devuelta=cont)
                                   for i, (c, dec, cont) in enumerate(lineas)])


class TestFaltanteDeRetorno:

    def test_declarado_menos_contado(self):
        f = sr.faltante_de_retorno(_dev(lineas=[('A', 10, 7), ('B', 2, 2), ('C', 1, 3)]))
        assert f['faltante_unidades'] == 3
        assert f['sobrante_unidades'] == 2
        assert [l['codigo_siesa'] for l in f['lineas']] == ['A', 'C']

    def test_sin_contado_todavia_no_hay_faltante(self):
        assert sr.faltante_de_retorno(_dev(estado='ABIERTA', lineas=[('A', 10, 7)])) is None

    def test_de_mostrador_no_aplica(self):
        assert sr.faltante_de_retorno(_dev(recaudo=None, lineas=[('A', 10, 7)])) is None

    def test_sin_declarado_no_se_inventa(self):
        assert sr.faltante_de_retorno(_dev(lineas=[('A', None, 7)])) is None


@pytest.fixture
def devolucion_de_ruta(db, almacen, producto):
    """Una devolución armada por Liquidación: el conductor declaró 10."""
    from app.services.devolucion_cliente_service import DevolucionClienteService
    ruta, tarea, c = _ruta(db, almacen)
    tarea.tipo_documento = 'PEDIDO'
    tarea.siesa_triggered = True
    RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
        'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1,
        'items_entregados': [{'codigo': producto.codigo, 'cantidad_pedida': 20,
                              'cantidad_entregada': 10}]})
    rec = _recaudo(ruta, tarea)
    dev = DevolucionClienteService.crear_devolucion(
        tarea_packing_id=tarea.id, tipo_docto_fe='FEW', consec_fe='77',
        almacen_id=almacen.id, recepcionista_id=None,
        lineas=[{'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 20, 'cantidad_devuelta': 10,
                 'cantidad_declarada': 10, 'f470_id_unidad_medida': 'UND',
                 'f150_id_bodega': 'NB1', 'f470_rowid': '999'}],
        recaudo_entrega_id=rec.id)
    return dev, rec, ruta


class TestRecepcionNoPisaLoDeclarado:

    def _confirmar(self, dev, producto, contado):
        from unittest.mock import patch
        from app.services.devolucion_cliente_service import DevolucionClienteService
        with patch('app.services.devolucion_cliente_service.connekta') as cx, \
                patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
            cx.get_rowids_factura.return_value = [{
                'f470_rowid': '999', 'f120_referencia': producto.codigo_siesa,
                'f470_cant_base': 20, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1'}]
            DevolucionClienteService.confirmar_entrada_fisica(
                dev.id, recepcionista_id=1,
                lineas_ajustadas=[{'producto_id': producto.id, 'cantidad_devuelta': contado}])

    def test_el_declarado_sobrevive_y_la_diferencia_se_ve(self, db, devolucion_de_ruta,
                                                           producto, ub_reserva):
        from app.models.bitacora import BitacoraAccion
        dev, rec, ruta = devolucion_de_ruta
        self._confirmar(dev, producto, 7)
        db.session.refresh(dev)
        linea = dev.lineas[0]
        assert float(linea.cantidad_devuelta) == 7
        assert float(linea.cantidad_declarada) == 10, 'lo declarado no se pisa'
        f = sr.faltantes_de_retorno_de_recaudos([rec.id])[rec.id]
        assert f['faltante_unidades'] == 3
        claves = [s['clave'] for s in sr.senales_de_recaudo(rec, ruta, f)]
        assert 'faltante_retorno' in claves
        b = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='DevolucionCliente',
                                           entidad_id=dev.id).one()
        assert b.antes['lineas'][0]['declarado'] == 10
        assert b.despues['lineas'][0]['contado'] == 7

    def test_devolucion_vieja_sin_declarado_congela_lo_vigente(self, db, devolucion_de_ruta,
                                                                producto, ub_reserva):
        """Una devolución de ruta armada antes de m041flfugas no traía la
        columna: lo vigente antes de que recepción ajuste es lo declarado."""
        dev, rec, _ = devolucion_de_ruta
        dev.lineas[0].cantidad_declarada = None
        db.session.commit()
        self._confirmar(dev, producto, 4)
        db.session.refresh(dev)
        assert float(dev.lineas[0].cantidad_declarada) == 10

    def test_la_liquidacion_usa_lo_que_dijo_el_conductor(self):
        """Si el líder corrigió en la liquidación, lo declarado sigue siendo lo
        del conductor (`cantidad_devuelta_conductor`)."""
        from app.services.liquidacion_service import _declarado_por_el_conductor
        filas = [{'ref': 'A', 'rowid': '1', 'cant_facturada': 20,
                  'producto': SimpleNamespace(codigo='A', codigo_siesa='A')}]
        items = [{'codigo': 'A', 'cantidad_devuelta': 6, 'cantidad_devuelta_conductor': 9}]
        assert _declarado_por_el_conductor(filas, items, 1, [6]) == [9]
        assert _declarado_por_el_conductor(filas, [{'codigo': 'A', 'cantidad_devuelta': 6}],
                                           1, [6]) == [6]

    def test_liquidar_completo_guarda_lo_del_conductor_antes_de_corregir(self):
        texto = (_APP / 'routes' / 'rutas.py').read_text(encoding='utf-8')
        i = texto.find("it['cantidad_devuelta'] = cant_devuelta")
        assert i != -1
        antes = texto[max(0, i - 600):i]
        assert "setdefault('cantidad_devuelta_conductor'" in antes


# ═════════════════════════════════════════════════════════════════════════════
# 4 · La hora del teléfono
# ═════════════════════════════════════════════════════════════════════════════

class TestHoraDelTelefono:

    def test_lee_iso_con_z_y_epoch_ms(self):
        assert sr.leer_ts('2026-09-24T15:00:00.000Z') == datetime(2026, 9, 24, 15)
        assert sr.leer_ts('2026-09-24T10:00:00-05:00') == datetime(2026, 9, 24, 15)
        assert sr.leer_ts(1_790_000_000_000) == datetime.utcfromtimestamp(1_790_000_000)
        assert sr.leer_ts('basura') is None
        assert sr.leer_ts(True) is None
        assert sr.leer_ts(None) is None

    def test_clasificacion(self, monkeypatch):
        monkeypatch.delenv('SENAL_DESFASE_RELOJ_S', raising=False)
        base = datetime(2026, 9, 24, 15)
        salida = base - timedelta(hours=3)
        c = sr.clasificar_hora
        assert c(None, None, base)['estado'] == 'sin_dato'
        assert c(base, 5, base, salida)['estado'] == 'coherente'
        assert c(base, 3600, base + timedelta(hours=1), salida)['estado'] == 'reloj_desfasado'
        assert c(salida - timedelta(hours=2), 0, base, salida)['estado'] == 'antes_de_salir'
        assert c(base - timedelta(hours=2), 0, base, salida)['estado'] == 'diferida'
        # Un teléfono adelantado 2 min que confirmó sin señal: el evento real
        # se ubica corrigiendo por el desfase.
        r = c(base + timedelta(seconds=120), 120, base + timedelta(hours=1), salida)
        assert r['estado'] == 'diferida' and r['retraso_s'] == 3600

    def test_se_guarda_aparte_de_la_del_servidor(self, db, almacen):
        from app.models.geo_entrega import EntregaGeo
        ruta, tarea, c = _ruta(db, almacen)
        ahora = datetime.utcnow()
        confirmado = ahora - timedelta(hours=3)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1,
            'ts_dispositivo': confirmado.isoformat() + 'Z',
            'ts_envio': (ahora + timedelta(seconds=90)).isoformat() + 'Z',
            'via_cola': True,
            'geo': {'lat': 2.93, 'lon': -75.28, 'precision_m': 10,
                    'pos_ts': int((confirmado - timedelta(minutes=1)).timestamp() * 1000)},
        })
        r = _recaudo(ruta, tarea)
        assert abs((r.ts_dispositivo - confirmado).total_seconds()) < 1
        assert 85 <= r.ts_desfase_s <= 95, 'teléfono adelantado ~90 s al enviar'
        assert r.via_cola is True
        assert abs((r.fecha_confirmacion - ahora).total_seconds()) < 60, \
            'la hora del servidor no se reemplaza'
        g = EntregaGeo.query.filter_by(recaudo_id=r.id).one()
        assert g.ts_dispositivo is not None and g.pos_ts_dispositivo is not None
        assert g.pos_ts_dispositivo < g.ts_dispositivo

    def test_sin_hora_es_sin_dato_no_la_del_servidor(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1})
        r = _recaudo(ruta, tarea)
        assert r.ts_dispositivo is None and r.ts_desfase_s is None and r.via_cola is None

    def test_reloj_desfasado_es_senal(self, db, almacen):
        ruta, tarea, c = _ruta(db, almacen)
        ahora = datetime.utcnow()
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1,
            'ts_dispositivo': (ahora + timedelta(hours=2)).isoformat() + 'Z',
            'ts_envio': (ahora + timedelta(hours=2)).isoformat() + 'Z'})
        claves = [s['clave'] for s in sr.senales_de_recaudo(_recaudo(ruta, tarea), ruta)]
        assert claves == ['reloj_desfasado']


# ═════════════════════════════════════════════════════════════════════════════
# 5 · El rechazo registrado lejos del cliente
# ═════════════════════════════════════════════════════════════════════════════

_TIENDA = (2.9300, -75.2800)


def _maestro(db, cliente='Tienda La Esquina', municipio='Neiva', punto=_TIENDA):
    from app.models.geo_entrega import ClienteGeo
    from app.services.geo_cliente import clave_de_cliente
    m = ClienteGeo(cliente_clave=clave_de_cliente(cliente, municipio),
                   lat=punto[0], lon=punto[1], precision_m=20, fuente='gps_conductor',
                   capturas_consideradas=3)
    db.session.add(m); db.session.commit()
    return m


class TestRechazoLejosDelCliente:

    def _rechazar(self, db, almacen, lat, lon, motivo='CLIENTE_CERRADO'):
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': motivo,
            'observaciones': 'cerrado', 'geo': {'lat': lat, 'lon': lon, 'precision_m': 15}})
        return _recaudo(ruta, tarea), ruta

    def test_lejos_da_senal_con_la_distancia(self, db, almacen, monkeypatch):
        monkeypatch.delenv('SENAL_RECHAZO_LEJOS_M', raising=False)
        _maestro(db)
        r, ruta = self._rechazar(db, almacen, _TIENDA[0] + 0.018, _TIENDA[1])  # ~2 km
        assert 1900 < float(r.distancia_cliente_m) < 2100
        s = sr.senales_de_recaudo(r, ruta)
        assert [x['clave'] for x in s] == ['rechazo_lejos']

    def test_se_mide_antes_de_que_la_captura_vote(self, db, almacen):
        """Con el maestro recalculado después, la distancia sigue siendo contra
        el punto que había: si no, la captura se mediría contra su sombra."""
        from app.services.geo_cliente import maestro_de
        _maestro(db)
        r, _ = self._rechazar(db, almacen, _TIENDA[0] + 0.018, _TIENDA[1])
        assert float(r.distancia_cliente_m) > 1900
        m = maestro_de('Tienda La Esquina', 'Neiva')
        assert m is not None  # recalculado sobre las capturas; la distancia no cambió

    def test_cerca_no_da_senal(self, db, almacen):
        _maestro(db)
        r, ruta = self._rechazar(db, almacen, _TIENDA[0] + 0.001, _TIENDA[1])  # ~110 m
        assert sr.senales_de_recaudo(r, ruta) == []

    def test_sin_punto_del_cliente_no_hay_senal(self, db, almacen):
        r, ruta = self._rechazar(db, almacen, _TIENDA[0] + 0.05, _TIENDA[1])
        assert r.distancia_cliente_m is None
        assert sr.senales_de_recaudo(r, ruta) == []

    def test_direccion_errada_no_afirma_presencia(self, db, almacen):
        _maestro(db)
        r, ruta = self._rechazar(db, almacen, _TIENDA[0] + 0.05, _TIENDA[1], 'DIRECCION_ERRADA')
        assert r.distancia_cliente_m is not None
        assert sr.senales_de_recaudo(r, ruta) == []

    def test_umbral_configurable(self, monkeypatch):
        rec = SimpleNamespace(motivo_rechazo='CLIENTE_CERRADO', distancia_cliente_m=800)
        monkeypatch.setenv('SENAL_RECHAZO_LEJOS_M', '1000')
        assert sr.senal_rechazo_lejos(rec) is None
        monkeypatch.setenv('SENAL_RECHAZO_LEJOS_M', '500')
        assert sr.senal_rechazo_lejos(rec)['umbral_m'] == 500.0


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Efectivo en poder de cada conductor
# ═════════════════════════════════════════════════════════════════════════════

class TestEfectivoEnPoder:

    def test_suma_lo_no_liquidado_con_antiguedad(self, db, almacen):
        from app.utils.fecha import dia_operativo
        dora = _conductor(db, 'Dora')
        r1, t1, _ = _ruta(db, almacen, conductor=dora)
        RutaService.confirmar_parada(r1.id, t1.id, dora.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 30000})
        rec = _recaudo(r1, t1)
        rec.fecha_confirmacion = datetime.utcnow() - timedelta(days=3)
        # Una transferencia no es efectivo en poder.
        r2, t2, _ = _ruta(db, almacen, conductor=dora)
        RutaService.confirmar_parada(r2.id, t2.id, dora.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'TRANSFERENCIA_BBVA',
            'monto_cobrado': 99999})
        # Una ruta liquidada ya entregó su plata.
        r3, t3, _ = _ruta(db, almacen, conductor=dora, estado='ENTREGADA')
        from app.models.recaudo_entrega import RecaudoEntrega as _R
        db.session.add(_R(ruta_id=r3.id, tarea_id=t3.id, estado_entrega='ENTREGADO',
                          forma_pago='EFECTIVO', monto_cobrado=77777))
        r3.estado_financiero = 'LIQUIDADA'
        db.session.commit()

        filas = sr.efectivo_en_poder_por_conductor(hoy=dia_operativo())
        fila = next(f for f in filas if f['conductor'] == 'Dora')
        assert fila['efectivo'] == 30000.0
        assert fila['paradas'] == 1 and fila['dias'] == 3
        assert fila['rutas'] == [r1.id]

    def test_sin_efectivo_no_aparece(self, db, almacen):
        assert sr.efectivo_en_poder_por_conductor() == []


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Advertencias de flota al despachar — informa, no bloquea
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def flota_sana(db, monkeypatch):
    """Un vehículo con SOAT/RTM vigentes, inspección apta hoy, sin taller y
    en custodia del conductor de la ruta. Lo que no se puede armar sin la base
    de flota completa se simula en su función pública."""
    from app.models.vehiculo import Vehiculo
    from app.utils.fecha import dia_operativo
    from flota.adaptadores import inspecciones, taller, traspaso
    from flota.adaptadores.modelos import DocumentoVehiculo
    v = Vehiculo(placa=f'FG{uuid.uuid4().hex[:4].upper()}', tipo='NHR', activo=True)
    db.session.add(v); db.session.flush()
    for tipo in ('soat', 'rtm'):
        db.session.add(DocumentoVehiculo(
            vehiculo_id=v.id, tipo=tipo, numero='1', entidad='X',
            fecha_expedicion=dia_operativo() - timedelta(days=30),
            fecha_vencimiento=dia_operativo() + timedelta(days=200)))
    db.session.commit()
    estado = {'veredicto': 'apto', 'custodio': None, 'taller': False}
    monkeypatch.setattr(inspecciones, 'del_dia',
                        lambda vid, dia: [SimpleNamespace(veredicto=estado['veredicto'])])
    monkeypatch.setattr(taller, 'ordenes_de',
                        lambda vid: [SimpleNamespace(estado='abierta')] if estado['taller'] else [])
    monkeypatch.setattr(traspaso, 'custodia_activa',
                        lambda vid: SimpleNamespace(custodio_tipo='conductor',
                                                    custodio_conductor_id=estado['custodio']))
    return v, estado


class TestAdvertenciasDeFlota:

    def _ruta_en_cargue(self, db, almacen, v, estado):
        ruta, tarea, c = _ruta(db, almacen, estado='EN_CARGUE', vehiculo_id=v.id)
        estado['custodio'] = c.id if estado['custodio'] is None else estado['custodio']
        return ruta, c

    def test_vehiculo_en_orden_sale_sin_preguntar(self, db, almacen, flota_sana):
        v, estado = flota_sana
        ruta, _ = self._ruta_en_cargue(db, almacen, v, estado)
        assert sr.advertencias_de_flota(ruta) == []
        RutaService.cerrar_ruta(ruta.id)
        assert ruta.estado == 'EN_TRANSITO'

    @pytest.mark.parametrize('rompe,clave', [
        ('soat', 'soat_vencido'),
        ('inspeccion', 'inspeccion_no_apta'),
        ('taller', 'en_taller'),
        ('custodio', 'custodio_distinto'),
    ])
    def test_cada_problema_se_nombra(self, db, almacen, flota_sana, rompe, clave):
        from app.utils.fecha import dia_operativo
        from flota.adaptadores.modelos import DocumentoVehiculo
        v, estado = flota_sana
        ruta, _ = self._ruta_en_cargue(db, almacen, v, estado)
        if rompe == 'soat':
            d = DocumentoVehiculo.query.filter_by(vehiculo_id=v.id, tipo='soat').one()
            d.fecha_vencimiento = dia_operativo() - timedelta(days=1)
            db.session.commit()
        elif rompe == 'inspeccion':
            estado['veredicto'] = 'no_apto'
        elif rompe == 'taller':
            estado['taller'] = True
        else:
            estado['custodio'] = 999999
        claves = [a['clave'] for a in sr.advertencias_de_flota(ruta)]
        assert claves == [clave]

    def test_sin_motivo_informa_con_motivo_sale_y_queda_en_la_bitacora(self, db, almacen, flota_sana):
        from app.models.bitacora import BitacoraAccion
        v, estado = flota_sana
        estado['taller'] = True
        ruta, c = self._ruta_en_cargue(db, almacen, v, estado)
        with pytest.raises(AdvertenciasDeFlota) as e:
            RutaService.cerrar_ruta(ruta.id)
        assert [a['clave'] for a in e.value.advertencias] == ['en_taller']
        assert ruta.estado == 'EN_CARGUE'
        RutaService.cerrar_ruta(ruta.id, motivo_advertencias='OT de pintura, rueda bien',
                                usuario_id=c.usuario_id)
        b = BitacoraAccion.query.filter_by(accion='FORZAR', entidad='RutaDespacho',
                                           entidad_id=ruta.id).one()
        assert b.motivo == 'OT de pintura, rueda bien'
        assert b.despues['advertencias_flota'] == ['en_taller']
        assert b.despues['momento'] == 'despachar'

    def test_lo_reconocido_al_iniciar_no_se_pide_otra_vez(self, db, almacen, flota_sana):
        v, estado = flota_sana
        estado['taller'] = True
        ruta, tarea, c = _ruta(db, almacen, estado='PROGRAMADO', vehiculo_id=v.id)
        estado['custodio'] = c.id
        RutaService.iniciar_ruta(ruta.id, motivo_advertencias='sale igual')
        RutaService.cerrar_ruta(ruta.id)   # mismas advertencias: no pregunta
        # Una advertencia NUEVA sí se pregunta.
        ruta2, tarea2, c2 = _ruta(db, almacen, estado='PROGRAMADO', vehiculo_id=v.id)
        estado['custodio'] = c2.id
        RutaService.iniciar_ruta(ruta2.id, motivo_advertencias='sale igual')
        estado['veredicto'] = 'incompleta'
        with pytest.raises(AdvertenciasDeFlota):
            RutaService.cerrar_ruta(ruta2.id)

    def test_sin_vehiculo_tambien_se_advierte(self, db, almacen):
        ruta, _, _ = _ruta(db, almacen, estado='EN_CARGUE')
        assert [a['clave'] for a in sr.advertencias_de_flota(ruta)] == ['sin_vehiculo']

    def test_el_endpoint_responde_409_con_la_lista(self, client, db, almacen, flota_sana,
                                                  jwt_token_admin):
        v, estado = flota_sana
        estado['taller'] = True
        ruta, _ = self._ruta_en_cargue(db, almacen, v, estado)
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h, json={})
        assert r.status_code == 409
        d = r.get_json()
        assert d['requiere_motivo'] is True
        assert [a['clave'] for a in d['advertencias_flota']] == ['en_taller']
        r = client.post(f'/api/rutas/{ruta.id}/cerrar', headers=h,
                        json={'motivo_advertencias': 'autorizado por jefe'})
        assert r.status_code == 200


# ── Trinquete de clase: el despacho no esquiva la flota ─────────────────────

#: Funciones que ponen una ruta en EN_CARGUE/EN_TRANSITO sin pasar por
#: `_reconocer_advertencias_flota`, con su motivo. Solo encoge.
TRANSICIONES_SIN_FLOTA = {
    ('app/services/ruta_service.py', 'crear_ruta'):
        'La ruta ad-hoc del muelle nace EN_CARGUE; el camión no sale hasta '
        'cerrar_ruta, que sí consulta la flota.',
}
_ESTADOS_DESPACHO = ('EN_CARGUE', 'EN_TRANSITO')


def _escrituras_de_despacho(fuentes):
    """`{(archivo, función)}` que escriben EN_CARGUE/EN_TRANSITO en una ruta, y
    `{(archivo, función)}` de esas que llaman a `_reconocer_advertencias_flota`."""
    escriben, reconocen = set(), set()
    for nombre, texto in fuentes:
        arbol = ast.parse(texto)
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            clave = (nombre, fn.name)
            for n in ast.walk(fn):
                valores = []
                # `ruta.estado = …` (el nombre de la variable dice qué es)
                if isinstance(n, ast.Assign) and any(
                        isinstance(t, ast.Attribute) and t.attr == 'estado'
                        and isinstance(t.value, ast.Name) and 'ruta' in t.value.id.lower()
                        for t in n.targets):
                    valores.append(n.value)
                # `RutaDespacho(estado=…)` — solo el constructor, no un filter_by
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id == 'RutaDespacho':
                    valores += [k.value for k in n.keywords if k.arg == 'estado']
                for valor in valores:
                    txt = (valor.attr if isinstance(valor, ast.Attribute)
                           else valor.value if isinstance(valor, ast.Constant) else None)
                    if txt in _ESTADOS_DESPACHO:
                        escriben.add(clave)
                if isinstance(n, ast.Call):
                    f = n.func
                    nom = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                    if nom == '_reconocer_advertencias_flota':
                        reconocen.add(clave)
    return escriben, reconocen


class TestElDespachoNoEsquivaLaFlota:

    def test_toda_transicion_consulta_la_flota(self):
        escriben, reconocen = _escrituras_de_despacho(_fuentes_app())
        faltan = sorted(escriben - reconocen - set(TRANSICIONES_SIN_FLOTA))
        assert not faltan, (
            f'Transiciones de despacho sin advertencias de flota: {faltan}. '
            'Un camión que sale sin que nadie mire su SOAT, su inspección o su '
            'custodia es la fuga 13 del contrato.')

    def test_piso(self):
        escriben, reconocen = _escrituras_de_despacho(_fuentes_app())
        assert {f for _, f in escriben & reconocen} >= {'iniciar_ruta', 'cerrar_ruta'}

    def test_el_inventario_no_tiene_entradas_muertas(self):
        escriben, _ = _escrituras_de_despacho(_fuentes_app())
        assert set(TRANSICIONES_SIN_FLOTA) <= escriben
        assert len(TRANSICIONES_SIN_FLOTA) <= 1
        for motivo in TRANSICIONES_SIN_FLOTA.values():
            assert len(motivo) > 30

    def test_el_detector_ve_las_dos_escrituras(self):
        codigo = ('def a(ruta):\n    ruta.estado = EstadoRutaDespacho.EN_TRANSITO\n'
                  'def b():\n    return RutaDespacho(estado=EstadoRutaDespacho.EN_CARGUE)\n'
                  'def c(ruta):\n    ruta.estado = "EN_TRANSITO"\n')
        escriben, _ = _escrituras_de_despacho([('x.py', codigo)])
        assert escriben == {('x.py', 'a'), ('x.py', 'b'), ('x.py', 'c')}

    def test_el_detector_no_marca_lo_sano(self):
        codigo = ('def a(lpn):\n    lpn.estado = "EN_TRANSITO"\n'
                  'def b(ruta):\n    RutaService._reconocer_advertencias_flota(ruta, "x")\n'
                  '    ruta.estado = EstadoRutaDespacho.EN_TRANSITO\n'
                  'def c(ruta):\n    ruta.estado = EstadoRutaDespacho.ENTREGADA\n'
                  'def d(cid):\n    return RutaDespacho.query.filter_by(conductor_id=cid, '
                  'estado=EstadoRutaDespacho.EN_TRANSITO).all()\n')
        escriben, reconocen = _escrituras_de_despacho([('x.py', codigo)])
        assert escriben - reconocen == set()


# ═════════════════════════════════════════════════════════════════════════════
# 8 · `entregar_ruta` no da por entregado lo que nadie declaró
# ═════════════════════════════════════════════════════════════════════════════

class TestEntregarRutaSoloLoDeclarado:

    def test_lo_ausente_conserva_su_estado_y_se_declara(self, db, almacen):
        from app.models.bulto import Bulto
        ruta, tarea, c = _ruta(db, almacen, n_bultos=3)
        b1, b2, b3 = sorted(Bulto.query.filter_by(ruta_despacho_id=ruta.id).all(),
                            key=lambda b: b.id)
        b3.estado = 'RECHAZADO'   # lo marcó la parada
        db.session.commit()
        res = RutaService.entregar_ruta(ruta.id, {'bultos': [
            {'id': b1.id, 'entregado': True},
            {'id': b2.id},                       # sin `entregado`: no es una declaración
        ]}, c.usuario_id)
        db.session.refresh(b1); db.session.refresh(b2); db.session.refresh(b3)
        assert b1.estado == 'ENTREGADO'
        assert b2.estado == 'CARGADO', 'un bulto sin declarar no se inventa entregado'
        assert b3.estado == 'RECHAZADO', 'lo que la parada devolvió no se pisa'
        assert res['entregados'] == 1 and res['rechazados'] == 0
        assert [b['id'] for b in res['sin_declarar']] == [b2.id]

    def test_el_cierre_offline_del_conductor_no_cambia(self, db, almacen):
        """La cola offline manda `bultos: []`: cada parada ya marcó los suyos."""
        from app.models.bulto import Bulto
        ruta, tarea, c = _ruta(db, almacen)
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1})
        res = RutaService.entregar_ruta(ruta.id, {'bultos': []}, c.usuario_id)
        assert {b.estado for b in Bulto.query.filter_by(ruta_despacho_id=ruta.id)} == {'ENTREGADO'}
        assert res['sin_declarar'] == [] and ruta.estado == 'ENTREGADA'

    def test_un_falso_explicito_sigue_rechazando(self, db, almacen):
        from app.models.bulto import Bulto
        ruta, tarea, c = _ruta(db, almacen, n_bultos=1)
        b = Bulto.query.filter_by(ruta_despacho_id=ruta.id).one()
        RutaService.entregar_ruta(ruta.id, {'bultos': [
            {'id': b.id, 'entregado': False, 'motivo_rechazo': 'No había nadie'}]}, c.usuario_id)
        db.session.refresh(b)
        assert b.estado == 'RECHAZADO' and b.motivo_rechazo == 'No había nadie'


# ── Trinquete de clase: un dato de entrega ausente no se rellena con «entregado»

#: Claves que dicen si algo se entregó. Leerlas con un default es declarar
#: por el conductor.
CLAVES_DE_ENTREGA = ('entregado', 'cantidad_entregada')

#: Sitios conocidos que leen una clave de entrega con default, con su motivo.
#: Solo encoge.
DEFAULTS_DE_ENTREGA = {
    ('app/services/ruta_service.py', 'confirmar_parada', 'cantidad_entregada'):
        'items_entregados de una PARCIAL: el PWA manda siempre la cantidad '
        'entregada de cada referencia (precargada con lo pedido); el default '
        'cubre un ítem malformado y lo recorta a [0, pedido].',
}


def _defaults_de_entrega(fuentes):
    """`{(archivo, función, clave)}` de `x.get(CLAVE, <no None>)` y de
    `x.get(CLAVE) or <algo>`."""
    hallados = set()
    for nombre, texto in fuentes:
        arbol = ast.parse(texto)
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                llamada = None
                if isinstance(n, ast.Call):
                    llamada, con_default = n, (len(n.args) >= 2 and not (
                        isinstance(n.args[1], ast.Constant) and n.args[1].value is None))
                    if not con_default:
                        continue
                elif isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or) and \
                        isinstance(n.values[0], ast.Call):
                    llamada = n.values[0]
                else:
                    continue
                f = llamada.func
                if not (isinstance(f, ast.Attribute) and f.attr == 'get' and llamada.args
                        and isinstance(llamada.args[0], ast.Constant)
                        and llamada.args[0].value in CLAVES_DE_ENTREGA):
                    continue
                hallados.add((nombre, fn.name, llamada.args[0].value))
    return hallados


class TestNadieDeclaraPorElConductor:

    def test_ningun_default_nuevo(self):
        nuevos = _defaults_de_entrega(_fuentes_app()) - set(DEFAULTS_DE_ENTREGA)
        assert not nuevos, (
            f'Lectura de una clave de entrega con default: {sorted(nuevos)}. '
            'Un dato de entrega ausente no es «se entregó»: declararlo, no '
            'rellenarlo (fuga 14 — entregar_ruta marcaba ENTREGADO todo lo '
            'que no venía en el payload).')

    def test_inventario_vivo_y_con_motivo(self):
        hallados = _defaults_de_entrega(_fuentes_app())
        assert set(DEFAULTS_DE_ENTREGA) <= hallados, 'entrada muerta: quitala'
        assert len(DEFAULTS_DE_ENTREGA) <= 1
        for m in DEFAULTS_DE_ENTREGA.values():
            assert len(m) > 30

    def test_el_detector_ve_las_dos_escrituras(self):
        codigo = ("def a(c):\n    return c.get('entregado', True)\n"
                  "def b(c):\n    return c.get('entregado') or True\n")
        assert _defaults_de_entrega([('x.py', codigo)]) == {
            ('x.py', 'a', 'entregado'), ('x.py', 'b', 'entregado')}

    def test_el_detector_no_marca_lo_sano(self):
        codigo = ("def a(c):\n    return c.get('entregado')\n"
                  "def b(c):\n    return c.get('entregado', None)\n"
                  "def d(c):\n    return c.get('otra', True)\n"
                  "# c.get('entregado', True)\n")
        assert _defaults_de_entrega([('x.py', codigo)]) == set()

    def test_piso(self):
        assert len(_defaults_de_entrega(_fuentes_app())) >= 1


# ═════════════════════════════════════════════════════════════════════════════
# Liquidación: las señales llegan a quien liquida
# ═════════════════════════════════════════════════════════════════════════════

class TestLaLiquidacionMuestraLasSenales:

    def test_dashboard_trae_efectivo_y_tasa_por_conductor(self, client, db, almacen,
                                                          jwt_token_admin):
        from app.utils.fecha import dia_operativo
        c = _conductor(db, 'Elsa')
        ruta, tarea, _ = _ruta(db, almacen, conductor=c)
        ruta.fecha_programada = dia_operativo()
        db.session.commit()
        legado = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'TRANSFERENCIA_BBVA',
                  'monto_cobrado': 5000}
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, legado)
        RutaService.entregar_ruta(ruta.id, {'bultos': []}, c.usuario_id)
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        hoy = dia_operativo().isoformat()
        d = client.get(f'/api/rutas/liquidacion/dashboard?fecha_desde={hoy}&fecha_hasta={hoy}',
                       headers=h).get_json()
        assert 'senales_conductor' in d
        assert set(d['senales_conductor']) == {'efectivo_en_poder', 'sin_pago_por_conductor'}
        fila = next(r for r in d['rutas'] if r['id'] == ruta.id)
        assert fila['senales'] == {'sin_comprobante': 1}

    def test_detalle_trae_senales_por_parada(self, db, almacen, monkeypatch):
        from app.services.liquidacion_service import LiquidacionService
        monkeypatch.setattr('app.services.fe_resolver.resolver_fe_o_none', lambda t: (None, None))
        c = _conductor(db)
        ruta, tarea, _ = _ruta(db, almacen, conductor=c)
        legado = _sin_pago(); legado.pop('version_formulario')
        RutaService.confirmar_parada(ruta.id, tarea.id, c.usuario_id, legado)
        d = LiquidacionService.preparar_detalle_ruta(ruta.id)
        rec = d['recaudos'][0]
        assert [s['clave'] for s in rec['senales']] == ['sin_evidencia']
        assert rec['hora_dispositivo']['estado'] == 'sin_dato'


# ═════════════════════════════════════════════════════════════════════════════
# Pantalla: la cola offline manda la hora del teléfono (Node, util.js real)
# ═════════════════════════════════════════════════════════════════════════════

class TestLaColaMandaLaHoraDelTelefono:

    def _js(self):
        return (_APP / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')

    def test_sello_de_envio(self, tmp_path):
        import subprocess
        texto = self._js()
        i = texto.find('function _condSelloDeEnvio(')
        j = texto.find('\n}\n', i)
        assert i != -1 and j != -1
        prog = texto[i:j + 3] + """
const a = _condSelloDeEnvio({x: 1, ts_dispositivo: '2026-09-24T10:00:00.000Z'}, true, 5);
const b = _condSelloDeEnvio({x: 1}, true, Date.UTC(2026, 8, 24, 9, 0, 0));
const c = _condSelloDeEnvio({x: 1}, false, null);
console.log(JSON.stringify({a, b, c}));
"""
        f = tmp_path / 'sello.js'
        f.write_text(prog, encoding='utf-8')
        out = json.loads(subprocess.run(['node', str(f)], capture_output=True, text=True,
                                        check=True).stdout)
        assert out['a']['ts_dispositivo'] == '2026-09-24T10:00:00.000Z', 'no se pisa'
        assert out['a']['via_cola'] is True and out['a']['ts_envio']
        assert out['b']['ts_dispositivo'] == '2026-09-24T09:00:00.000Z', \
            'ítem viejo de la cola: la hora a la que se encoló'
        assert out['c']['via_cola'] is False and 'ts_dispositivo' not in out['c']

    def test_la_sincronizacion_y_el_envio_directo_lo_usan(self):
        texto = self._js()
        assert "_condSelloDeEnvio(item.payload, true, item.ts)" in texto
        assert "_condSelloDeEnvio(payload, false, null)" in texto
        assert "ts_dispositivo:    new Date().toISOString()" in texto
        assert "version_formulario: COND_VERSION_FORMULARIO" in texto

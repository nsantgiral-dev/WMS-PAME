"""Defectos del recorrido e2e de UN DÍA COMPLETO por rol (QA eebb15bb, 2026-09-25).

El día se recorrió por HTTP con JWT real de cada rol (admin, jefe de almacén,
supervisor, control de flota, tres conductores, operario, empacador,
recepción, compras y la persona de cartera) contra la app local sobre un SQLite
desechable, con Connekta en simulación y un fake de Siesa para las lecturas
(pedidos, cabecera, compromisos, FE y cartera). Las pantallas clave se pintaron
en Node con `util.js` real y las respuestas reales del día.

Cada test REPRODUCE un defecto observado ese día y está marcado
`xfail(strict=True)` con su motivo: el día que alguien lo arregle, el test pasa,
el xfail estricto se pone rojo y obliga a quitar la marca.

No se tocó código de producción.

| Test | Defecto | Sev. |
|---|---|---|
| `TestElFaltanteTotalSeVeDondeSeLiquida` | Una devolución contada en cero (FALTANTE_TOTAL) no aparece como faltante de retorno en Liquidación ni en 💸 Fugas; Recepción sí la ve (8 und, $152.320) | P0 |
| `TestLiquidacionNoPrometeLoQueNoVaAPasar` | Liquidación dice «pendiente de que recepción confirme» y «NCE por $X (total factura)» sobre una devolución que ya terminó en FALTANTE_TOTAL | P0 |
| `TestElPedidoAprobadoNaceConSuCondicion` | El packing de «Aprobar pedido» (`crear_manual`) nace sin condición de pago: la compuerta de cartera del cierre (G2) nunca evalúa un crédito real | P2 |
| `TestLaCuentaDelConductorEntraConSuCorreo` | «Crear cuenta» guarda el correo en minúsculas y el login compara exacto: el conductor no entra con el correo que se le dio | P2 |
| `TestLaBitacoraNombraAQuienAutorizoEnElGestor` | La autorización/conversión del Gestor sale en la bitácora como «El sistema …» | P2 |
| `TestLaPortadaNoJuzgaUnDiaSinMedir` | «Nivel de servicio» en rojo, «se despacharon 0», con el único día del período sin medir (en curso) y sin decirlo | P2 |
| `TestLlegoElCamionSinCodigos` | «Llegó el camión» pinta el motivo crudo (`NO_PAGO`, `CLIENTE_CERRADO`) | P3 |
| `TestElRecorridoSinCodigos` | El recorrido del pedido pinta el estado de la devolución y el error del job crudos (`FALTANTE_TOTAL`, `RECIBO_CAJA job=…`) | P3 |
| `TestElReenvioIdenticoNoEsUnaEdicion` | La misma confirmación reenviada por la cola queda como «editó la entrega», sin motivo, con cambios fantasma (35700.00 → 35700.0) | P3 |
| `TestElBloqueoNoSeLeAtribuyeAQuienAprobo` | «Laura Admin bloqueó la retención de cartera»: la retuvo la compuerta; Laura intentó aprobar | P3 |
| `TestElMensajeDeRetencionSinDoblePunto` | «Crédito real sin cupo asignado en Siesa.. Lo libera…» | P3 |
| `TestElDashboardCuentaLasCajasCerradas` | Dashboard: «Packing completado hoy 0» y el empacador con 0 cajas tras cerrar 13 (cuenta solo VERIFICADO) | P2 |
"""
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from tests.test_cartera_retencion import (  # noqa: F401 — `fake` es fixture
    NIT, USUARIO_GESTOR, _historia, _jwt, _producto, _recaudo, _retener, _tarea, _usuario, fake)  # noqa: F401
from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible


# ═════════════════════════════════════════════════════════════════════════════
# Mundo: una parada RECHAZADA cuya mercancía el conductor declaró de vuelta
# (8 und) y recepción contó en CERO → FALTANTE_TOTAL
# ═════════════════════════════════════════════════════════════════════════════

def _faltante_total(db, almacen, liquidada=True):
    from app.models.devolucion_cliente import DevolucionCliente, LineaDevolucionCliente
    t = _tarea(db, almacen, '003-PD-1005', esperado=8, cond='C04', fe=('FEW', '6005'),
               valor=152_320)
    t.cliente = 'DISTRIBUIDORA MOROSA SAS'
    r = _recaudo(db, t, estado='RECHAZADO', forma=None)
    r.motivo_rechazo = 'CLIENTE_CERRADO'
    r.bultos_rechazados_ids = []
    if liquidada:
        r.ruta.estado_financiero = 'LIQUIDADA'
    d = DevolucionCliente(codigo=f'DEVC-E2E-{uuid.uuid4().hex[:6]}', tarea_packing_id=t.id,
                          numero_pedido_siesa='PD1005', tipo_docto_fe='FEW', consec_fe='6005',
                          almacen_id=almacen.id, estado='FALTANTE_TOTAL', es_total=True,
                          recaudo_entrega_id=r.id, cliente=t.cliente,
                          fecha_confirmacion=datetime.utcnow())
    db.session.add(d)
    db.session.flush()
    p = t.items[0].producto
    db.session.add(LineaDevolucionCliente(
        devolucion_id=d.id, producto_id=p.id, codigo_siesa=p.codigo_siesa,
        cantidad_facturada=8, cantidad_devuelta=0, cantidad_declarada=8, es_averiado=False))
    db.session.commit()
    return t, r, d


class TestElFaltanteTotalSeVeDondeSeLiquida:
    """**P0 — mercancía y plata.** Ese día: PD1005 (cliente cerrado) volvió
    declarada por el conductor, 8 und; recepción la contó en cero →
    `FALTANTE_TOTAL`. El tablero de Recepción lo mide («faltante de retorno 8 und
    ($152.320)»), pero Liquidación (`faltante_retorno: null`, sin señal) y
    💸 Fugas (`extra.faltante_de_retorno: []`, la cuenta entre las «unidades
    devueltas») no lo ven: quien liquida firma la ruta sin enterarse de que 8
    unidades no están ni en el cliente ni en la bodega.

    Causa: `senales_ruta.faltante_de_retorno` incluye FALTANTE_TOTAL («el caso
    más grande de faltante»), pero el lote que usan Liquidación, Fugas y
    `/liquidar-completo` — `faltantes_de_retorno_de_recaudos`
    (`app/services/senales_ruta.py:392-394`) — filtra `estado == 'CONFIRMADA'`.
    Llamadores: `liquidacion_service.py:202`, `analitica_fugas.py:391`,
    `routes/rutas.py:1253`.
    """

    def test_la_funcion_de_una_devolucion_si_lo_ve(self, db, almacen):
        from app.services import senales_ruta
        _t, _r, d = _faltante_total(db, almacen)
        assert senales_ruta.faltante_de_retorno(d)['faltante_unidades'] == 8

    def test_el_lote_que_usan_liquidacion_y_fugas_tambien(self, db, almacen):
        from app.services import senales_ruta
        _t, r, _d = _faltante_total(db, almacen)
        lote = senales_ruta.faltantes_de_retorno_de_recaudos([r.id])
        assert r.id in lote and lote[r.id]['faltante_unidades'] == 8

    def test_liquidacion_detalle_lo_trae(self, app, client, db, almacen):
        _t, r, _d = _faltante_total(db, almacen)
        admin = _usuario(db)
        resp = client.get(f'/api/rutas/{r.ruta_id}/liquidacion-detalle', headers=_jwt(app, admin))
        assert resp.status_code == 200, resp.get_json()
        rec = next(x for x in resp.get_json()['recaudos'] if x['id'] == r.id)
        assert rec['faltante_retorno'] and rec['faltante_retorno']['faltante_unidades'] == 8


class TestLiquidacionNoPrometeLoQueNoVaAPasar:
    """**P0 — texto que contradice el dato, sobre plata.** Con la devolución ya
    en `FALTANTE_TOTAL` (contada en cero: no entra inventario ni sale NC), la
    tarjeta de la parada en Liquidación dice «NCE por: $152.320 (total factura)»
    y «🔵 Enviado a Devoluciones (…) — pendiente de que recepción confirme».
    Recepción ya confirmó: no volvió nada. `liquidacion.js:734-742` mira solo
    que exista `devolucion_pendiente`, nunca su `estado`.
    """

    def test_la_tarjeta_dice_faltante_y_no_pendiente(self, app, client, db, almacen, tmp_path):
        _t, r, _d = _faltante_total(db, almacen)
        admin = _usuario(db)
        det = client.get(f'/api/rutas/{r.ruta_id}/liquidacion-detalle',
                         headers=_jwt(app, admin)).get_json()
        # Lo que el servidor trae con Siesa real (en simulación la FE no se lee):
        # la factura de PD1005 tal como vino ese día.
        for rec in det['recaudos']:
            rec['factura_siesa'] = {'base_gravable': 128000.0, 'total_iva': 24320.0,
                                    'total_neto': 152320.0, 'lineas': []}
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDetalleRuta = DET;
            _liqRenderDetalle();
            return { html: document.getElementById('liq-modal-body').innerHTML };
        """, globales={'DET': det})
        txt = visible(out['html'])
        assert 'pendiente de que recepción confirme' not in txt, txt
        assert 'NCE por' not in txt, txt


# ═════════════════════════════════════════════════════════════════════════════
# Cartera: el pedido aprobado nace sin su condición (G2 nunca evalúa)
# ═════════════════════════════════════════════════════════════════════════════

class TestElPedidoAprobadoNaceConSuCondicion:
    """**P2 — la compuerta del cierre (G2) está muerta para el camino real.**

    Ese día TODOS los packings creados por «Aprobar pedido»
    (`POST /api/siesa/iniciar-despacho` → `PackingService.crear_manual`)
    llegaron al cierre con `cond_pago = NULL` y G2 los marcó `NO_APLICA`
    —también los de crédito real C04/C05—. `crear_desde_picking` sí toma la
    foto de `pedidos_historia` (`anotar_desde_historia`); `crear_manual`
    (`packing_service.py`) no. La evaluación de verdad quedó toda en la
    EMISIÓN, dentro del DLQ: PD1014 (C04, se le venció una factura entre la
    aprobación y el cierre) cerró caja con «Siesa procesando (se confirma en
    segundos)», quedó DESPACHADO y su job PENDIENTE retenido por cartera —
    exactamente lo que G2 existe para evitar («la caja queda VERIFICADA con sus
    bultos, sin job»). De paso, en un pedido convertido a contado la condición
    del pedido (C04) se pierde: `cond_pago` termina C02 y `difiere_del_pedido`
    nunca lo declara.
    """

    def _aprobar(self, app, client, db, almacen):
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        from app.services import backorder_service
        admin = _usuario(db)
        _historia(db, '003-PD-910', cond='C04')
        p = _producto(db)
        ub = Ubicacion(codigo='PIK-E2E-1', almacen_id=almacen.id, tipo_zona='PICKING',
                       stock_minimo=0, stock_maximo=999, secuencia_ruteo=1, activo=True)
        db.session.add(ub)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id, cantidad=50,
                                         reservado=0, bloqueado=0))
        db.session.commit()
        with patch.object(backorder_service, 'compromisos_por_siesa', return_value=None):
            r = client.post('/api/siesa/iniciar-despacho', headers=_jwt(app, admin), json={
                'numero_pedido': 'PD910', 'tipo_docto': 'PD', 'consec_docto': 910, 'co': '003',
                'almacen_id': almacen.id,
                'items': [{'producto_id': p.id, 'item_codigo': 'SKU1', 'cantidad_pendiente': 10}]})
        assert r.status_code == 201, r.get_json()
        from app.models.packing import TareaPacking
        return db.session.get(TareaPacking, r.get_json()['packing_id'])

    def test_g1_si_evaluo_el_credito(self, app, client, db, almacen, fake):  # noqa: F811
        fake.cliente(cupo=10_000_000)
        t = self._aprobar(app, client, db, almacen)
        assert t is not None and fake.lecturas >= 1

    def test_el_packing_nace_con_la_condicion_del_pedido(self, app, client, db, almacen, fake):  # noqa: F811
        fake.cliente(cupo=10_000_000)
        t = self._aprobar(app, client, db, almacen)
        assert t.cond_pago == 'C04'

    def test_g2_evalua_el_credito_real(self, app, client, db, almacen, fake):  # noqa: F811
        from app.services import cartera_service as cs
        fake.cliente(cupo=10_000_000)
        t = self._aprobar(app, client, db, almacen)
        paso = cs.compuerta_cierre(t)
        assert t.cartera_decision != 'NO_APLICA', paso


# ═════════════════════════════════════════════════════════════════════════════
# Login
# ═════════════════════════════════════════════════════════════════════════════

class TestLaCuentaDelConductorEntraConSuCorreo:
    """**P2 — una persona que no puede entrar.** «Crear cuenta» del conductor
    (`RutaService.crear_cuenta_para_conductor`, `ruta_service.py:141`) guarda
    `email.strip().lower()`; `POST /api/auth/login` (`routes/auth.py:40`)
    compara exacto, y el PWA manda lo tecleado (`app.js:840`, solo `trim`). Ese
    día: la cuenta creada como `condA@e2e.co` no entró con `condA@e2e.co`
    (401 «Credenciales inválidas»); entró solo con `conda@e2e.co`. El teclado
    del teléfono pone mayúscula inicial solo.
    """

    def test_entra_con_el_correo_tal_como_se_lo_dieron(self, app, client, db):
        admin = _usuario(db)
        r = client.post('/api/rutas/conductores', headers=_jwt(app, admin),
                        json={'nombre': 'Ana Ruiz', 'cedula': f'CC{uuid.uuid4().hex[:6]}'})
        assert r.status_code in (200, 201), r.get_json()
        d = r.get_json()
        cid = d.get('id') or d['conductor']['id']
        r = client.post(f'/api/rutas/conductores/{cid}/cuenta', headers=_jwt(app, admin),
                        json={'email': 'Ana.Ruiz@Papeleria.co', 'password': 'Clave-1'})
        assert r.status_code == 201, r.get_json()
        login = client.post('/api/auth/login', json={'email': 'Ana.Ruiz@Papeleria.co',
                                                     'password': 'Clave-1'})
        assert login.status_code == 200, login.get_json()


# ═════════════════════════════════════════════════════════════════════════════
# Bitácora
# ═════════════════════════════════════════════════════════════════════════════

def _frases(db, entidad='RetencionCartera'):
    from app.models.bitacora import BitacoraAccion
    from app.services.analitica_salud import describir_acciones
    filas = [f.to_dict() for f in BitacoraAccion.query.filter_by(entidad=entidad)
             .order_by(BitacoraAccion.id).all()]
    return describir_acciones(filas)


class TestLaBitacoraNombraAQuienAutorizoEnElGestor:
    """**P2 — la regla es «con motivo y nombre», y el nombre no sale.** Ese día
    la Coordinadora de Cartera autorizó PD1005 y pasó PD1006 a contado desde el
    Gestor. La bitácora (📋 Analítica → Bitácora) dice «El sistema levantó con
    tope la retención de cartera PD1005» y «El sistema editó la retención…», y
    el resumen «Por persona» cuenta 2 acciones a «El sistema». El nombre vive en
    `despues.usuario`; `describir_acciones` (`analitica_salud.py:1252`) solo
    mira `usuario_id`, que es `None` para el Gestor.
    """

    def test_la_frase_lleva_el_nombre_del_gestor(self, db, fake, almacen):  # noqa: F811
        from app.services import cartera_service as cs
        r = _retener(db, fake, almacen)
        cs.autorizar(r.id, USUARIO_GESTOR, 'Pagó la vencida hoy', tope_valor=900_000)
        db.session.commit()
        frase = [f for f in _frases(db) if f['accion'] == 'FORZAR'][-1]['frase']
        assert 'Coordinadora Cartera' in frase, frase


class TestElBloqueoNoSeLeAtribuyeAQuienAprobo:
    """**P3 — atribución.** Ese día Laura tocó «Aprobar» en PD1005/1006/1013 y la
    compuerta G1 los retuvo. La bitácora dice «Laura Admin bloqueó la retención
    de cartera PD1005»: se lee como si ella hubiera frenado el pedido.
    `cartera_service.py:1003` registra `BLOQUEAR` con `usuario_id` = quien
    inició el despacho.
    """

    def test_la_frase_no_dice_que_la_persona_bloqueo(self, db, fake, almacen):  # noqa: F811
        u = _usuario(db)
        _retener(db, fake, almacen, iniciador=u.id)
        db.session.commit()
        frase = [f for f in _frases(db) if f['accion'] == 'BLOQUEAR'][-1]['frase']
        assert not frase.startswith(f'{u.nombre} bloqueó'), frase


class TestElMensajeDeRetencionSinDoblePunto:
    """**P3.** El 409 que ve el admin al aprobar: «Retenido por cartera: Crédito
    real sin cupo asignado en Siesa.. Lo libera…» (también con «acuerdo…
    contado..»). `cartera_service._mensaje` agrega `'. '` a un resumen que ya
    termina en punto."""

    def test_sin_doble_punto(self):
        from app.services import cartera_service as cs
        m = cs._mensaje({'resumen': 'Crédito real sin cupo asignado en Siesa.'}, None)
        assert '..' not in m, m


# ═════════════════════════════════════════════════════════════════════════════
# La cola del conductor: un reenvío idéntico no es una edición
# ═════════════════════════════════════════════════════════════════════════════

class TestElReenvioIdenticoNoEsUnaEdicion:
    """**P3 — ruido que acusa.** Sin señal, la cola del conductor reenvía la
    misma confirmación (PD1013, `via_cola`). El servidor la trata como edición:
    bitácora «Ciro Conductor editó la entrega PD1013 — sin motivo», con cambios
    fantasma («monto cobrado 35700.00 → 35700.0», «monto descuento 0.00 → 0.0»,
    solo la hora). Entra al contador «Sin motivo escrito» de la bitácora contra
    un conductor al que nadie le pidió motivo. `confirmar_parada` no tiene clave
    de reenvío (las operaciones de flota sí: `@idempotente`)."""

    def test_la_misma_confirmacion_dos_veces_no_edita(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.models.conductor import Conductor
        from app.models.usuario import Usuario
        from app.services.ruta_service import RutaService
        from tests.flujo import conductor_de_flujo as fl
        u = Usuario(nombre='Ciro', email=f'ciro_{uuid.uuid4().hex[:5]}@t.co', rol='conductor',
                    activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.flush()
        c = Conductor(nombre='Ciro', cedula=f'C{uuid.uuid4().hex[:6]}', usuario_id=u.id, activo=True)
        db.session.add(c)
        db.session.commit()
        cuerpo = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                  'monto_cobrado': 35700, 'via_cola': True}
        f = fl.flujo_completo(db, almacen, u.id, c.id, **cuerpo)
        antes = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='RecaudoEntrega').count()
        RutaService.confirmar_parada(f.ruta_id, f.packing_id, u.id, dict(cuerpo))
        db.session.commit()
        despues = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='RecaudoEntrega').count()
        assert despues == antes


# ═════════════════════════════════════════════════════════════════════════════
# Pantallas: códigos crudos
# ═════════════════════════════════════════════════════════════════════════════

class TestLlegoElCamionSinCodigos:
    """**P3.** «🚚 Llegó el camión» (Recepción) pinta «Devolución total ·
    NO_PAGO · toca para contar» y «· CLIENTE_CERRADO ·»: `recepcion.js:1475`
    hace `esc(d.motivo)` sobre el código. Respuesta real de
    `GET /api/devoluciones/llegadas` de ese día."""

    LLEGADAS = [{
        'conductor': 'Andrés Conductor', 'placa': 'REG101', 'ruta_id': 1,
        'estado_ruta': 'ENTREGADA', 'estado_financiero': 'PENDIENTE', 'horas_sin_contar': 0.0,
        'llegada_cerrada_at': None,
        'cuadre': {'en_camion_sin_recibir': 1, 'entregados': 4, 'exacto': False, 'faltantes': 0,
                   'retornados': 0, 'salieron': 5, 'sin_confirmar': 0},
        'devoluciones': [{'cliente': 'TIENDA DOÑA ROSA', 'codigo': 'DEVC-20260925132403-2-R2',
                          'es_total': True, 'estado': 'EN_CAMION', 'id': 1, 'lineas': 1,
                          'motivo': 'NO_PAGO', 'pedido': 'PD1002', 'problema_factura': None,
                          'sin_declaracion': False,
                          'fecha_creacion': '2026-09-25T18:24:03.602086'}]}]

    @pytest.mark.xfail(strict=True, reason='P3: recepcion.js pinta el motivo de rechazo crudo')
    def test_el_motivo_sale_en_palabras(self, tmp_path):
        out = _node(tmp_path, ['util.js', 'recepcion.js'], {},
                    'return { html: recLlegadasHtml(LLEGADAS) };',
                    globales={'LLEGADAS': self.LLEGADAS})
        assert codigos_crudos(out['html']) == [], visible(out['html'])


class TestElRecorridoSinCodigos:
    """**P3.** 🧭 Recorrido del pedido PD1003/PD1005: «Devolución del cliente
    DEVC-… · FALTANTE_TOTAL» / «PENDIENTE ABIERTA» y «Envío a Siesa: Recibo de
    caja · en cola · RECIBO_CAJA job=21: RC espera la NC del recaudo 6…». Los
    arma el servidor: `analitica_recorrido.py:1190` (estado crudo de la
    devolución como detalle) y `:1203` (`error_ultimo` del job, que empieza con
    el tipo y el id del job)."""

    @pytest.mark.xfail(strict=True, reason='P3: el recorrido publica el estado y el error del job crudos')
    def test_la_linea_de_tiempo_no_trae_codigos(self, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.services.analitica_recorrido import linea_de_tiempo
        _t, r, _d = _faltante_total(db, almacen)
        db.session.add(SiesaJob(tipo='RECIBO_CAJA', payload='{}', referencia_tipo='RecaudoEntrega',
                                referencia_id=r.id, estado='PENDIENTE',
                                error_ultimo=f'RECIBO_CAJA job=21: RC espera la NC del recaudo {r.id}'))
        db.session.commit()
        d = linea_de_tiempo('003-PD-1005')
        textos = ' '.join(f"{e.get('titulo') or ''} {e.get('detalle') or ''}" for e in d['eventos'])
        assert codigos_crudos(textos) == [], textos


# ═════════════════════════════════════════════════════════════════════════════
# Portada: un día sin medir juzgado en rojo
# ═════════════════════════════════════════════════════════════════════════════

class TestLaPortadaNoJuzgaUnDiaSinMedir:
    """**P2 — texto que contradice el dato.** Ese día se despacharon 12 de 14
    pedidos y la portada (🎯 ¿Cómo vamos?, rango «hoy») mostró «Nivel de
    servicio ● Fuera de meta 0 % — De cada 100 unidades pedidas, se despacharon
    0». El período viene `estado: INCOMPLETO`, «1 de 1 día(s) sin medir o
    incompletos» (el día en curso se calcula en vivo sobre el remisionado de
    Siesa, que llega con el sync), y la tarjeta lo juzga en rojo sin decir que
    el día no se midió (`analitica_kpi.semaforo` recibe el valor sin el estado;
    `analitica_portada.js` no pinta el estado del período)."""

    def test_la_tarjeta_dice_que_el_dia_no_se_midio(self, app, client, db, almacen, tmp_path):
        from app.models.pedido_historia import PedidoHistoria
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        for i in range(3):
            db.session.add(PedidoHistoria(
                linea_rowid=uuid.uuid4().int % 10**12, pedido_clave=f'003-PD-95{i}', co='003',
                tipo_docto='PD', consec_docto=950 + i, bodega='NB1', item_codigo='SKU1',
                cliente_id=NIT, cliente='C', cond_pago='C02', cantidad_pedida=10,
                cantidad_pedida_inicial=10, cantidad_remisionada=0, vlr_neto=1000,
                estado_siesa=3, fecha_pedido=hoy, fecha_entrega=hoy, primer_dia_visto=hoy,
                ultimo_dia_visto=hoy))
        db.session.commit()
        admin = _usuario(db)
        q = f'desde={hoy.isoformat()}&hasta={hoy.isoformat()}'
        d = client.get(f'/api/analitica/resumen?{q}', headers=_jwt(app, admin)).get_json()
        tarjeta = next(t for t in d['portada']['tarjetas'] if t['clave'] == 'fill_rate')
        assert tarjeta['periodo']['estado'] == 'INCOMPLETO'
        out = _node(tmp_path, ['util.js', 'analitica.js', 'analitica_portada.js'],
                    {'/api/analitica/resumen': d, '/api/analitica/salud': {}}, """
            AN_PORT.d = D;
            const html = anPortHtml(D);
            return { html };
        """, globales={'D': d})
        txt = visible(out['html'])
        i = txt.find('Nivel de servicio')
        tarjeta_txt = txt[i:i + 700]
        if 'Fuera de meta' in tarjeta_txt:
            assert any(w in tarjeta_txt.lower() for w in ('en curso', 'incomplet', 'sin medir')), tarjeta_txt


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard: la caja cerrada deja de contar
# ═════════════════════════════════════════════════════════════════════════════

class TestElDashboardCuentaLasCajasCerradas:
    """**P2 — el trabajo del empacador en cero.** Al final del día (13 cajas
    cerradas por Elena, 12 facturas), el dashboard decía «Packing completado
    hoy: 0» junto a «facturas generadas hoy: 12», y en Productividad «Elena
    Empaca · packings 0». Las dos cuentas filtran `estado == 'VERIFICADO'`
    (`dashboard_service.py:271-273` y `:423`), un estado de paso: al cerrar la
    caja pasa a `DESPACHADO` (con `fecha_verificado` puesta) y deja de contar.
    Solo cuentan las cajas que quedaron trabadas antes de cerrar."""

    def test_la_caja_cerrada_hoy_cuenta(self, app, client, db, almacen):
        emp = _usuario(db, rol='empacador')
        emp.almacen_id = almacen.id
        t = _tarea(db, almacen, '003-PD-977', estado='DESPACHADO', cond='C02', fe=('FEW', '977'))
        t.empacador_id = emp.id
        t.fecha_verificado = datetime.utcnow()
        db.session.commit()
        admin = _usuario(db)
        d = client.get(f'/api/dashboard/resumen-completo?almacen_id={almacen.id}',
                       headers=_jwt(app, admin)).get_json()
        assert d['kpis']['packing']['completado_hoy'] == 1, d['kpis']['packing']
        fila = next(o for o in d['productividad']['operarios'] if o['operario_id'] == emp.id)
        assert fila['packings_completados'] == 1, fila

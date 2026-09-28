"""Defectos del día real por rol sobre integra-voz d9da743d (validación 2026-09-26).

El día (martes 30 de septiembre, fin de mes, reloj controlado) se recorrió por
HTTP con JWT de cada rol —admin, gerente, jefe de almacén, supervisor,
liquidador, líder de cartera, control de flota, tres conductores, operario,
empacador, recepción, compras, tienda y el token del Gestor— sobre un SQLite
desechable, Connekta en simulación y un fake de lecturas de Siesa; las
pantallas se pintaron en Node con `util.js` y `modal.js` reales.

Cada test REPRODUCE un defecto observado ese día y está marcado
`xfail(strict=True)`: el día que se arregle, el xfail estricto se pone rojo y
obliga a quitar la marca. No se tocó código de producción.
"""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.test_cartera_retencion import _jwt, _recaudo, _tarea, _usuario  # noqa: F401
from tests.test_sin_codigos_en_pantalla import _node, visible

PWA = Path(__file__).resolve().parent.parent / 'app' / 'static' / 'pwa'


def _bulto(db, tarea):
    from app.models.bulto import Bulto
    b = Bulto(tarea_id=tarea.id, tipo='Caja', numero=1, total=1,
              codigo_barras=f'B-{uuid.uuid4().hex[:8].upper()}', estado='PENDIENTE')
    db.session.add(b)
    db.session.commit()
    return b


def _caja_en_cola(db, almacen):
    """Una caja recién cerrada: VERIFICADA, con su bulto y su DESPACHO_F470 en la
    cola esperando el DLQ (lo normal durante los segundos o minutos que tarda
    Siesa; con cola larga en temporada, más)."""
    from app.models.siesa_job import SiesaJob
    t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', estado='VERIFICADO', cond='C02')
    _bulto(db, t)
    SiesaJob.encolar(tipo='DESPACHO_F470', payload={'tarea_id': t.id},
                     referencia_tipo='TareaPacking', referencia_id=t.id)
    db.session.commit()
    return t


# ═════════════════════════════════════════════════════════════════════════════
# P1 · «Limpiar bultos» sobre una caja cuyo cierre está en la cola
# ═════════════════════════════════════════════════════════════════════════════

class TestUnaCajaEnColaNoSeLimpia:
    """**P1 — factura emitida de una caja que ya no puede salir.**

    Ese día, al cerrar las 13 cajas de la mañana, la lista del empacador las
    pintó TODAS «⚠ Reintentar Siesa» con el botón «🗑 Limpiar bultos y
    redeclarar piezas» mientras su `DESPACHO_F470` esperaba en la cola
    (`packing.js:129-130`: `siesaFallo = VERIFICADO && !siesa_triggered`; la
    lista del servidor no dice que hay un job vivo). Para el empacador el botón
    termina en 403 («Solo admin puede resetear»). Para el admin funciona:
    `PackingService.resetear_siesa` (`packing_service.py:749-793`) solo pregunta
    `exigir_sin_documento` (RM/FE/bandera), **no si hay un despacho en cola**
    —`cancelar` sí lo pregunta—. Reproducido en el e2e: reset 200 → bultos
    borrados → el DLQ emite RM-7001 + FE y deja la caja DESPACHADO con **0
    bultos** → no aparece en «listos» del muelle → re-cerrar: 400 «Siesa ya
    procesó este despacho». Cliente facturado, mercancía en la mesa, sin salida.
    """

    @pytest.mark.xfail(strict=True, reason='resetear_siesa no mira el DESPACHO_F470 vivo (packing_service.py:767)')
    def test_resetear_se_niega_con_el_despacho_en_cola(self, app, client, db, almacen):
        from app.models.bulto import Bulto
        t = _caja_en_cola(db, almacen)
        admin = _usuario(db, rol='admin')
        r = client.post(f'/api/packing/{t.id}/resetear-siesa', headers=_jwt(app, admin), json={})
        assert r.status_code in (400, 409), r.get_json()
        assert Bulto.query.filter_by(tarea_id=t.id).count() == 1

    @pytest.mark.xfail(strict=True, reason='la lista del empacador pinta la cola como fallo (packing.js:130)')
    def test_la_lista_no_llama_fallo_a_lo_que_esta_en_cola(self, app, client, db, almacen, tmp_path):
        t = _caja_en_cola(db, almacen)
        emp = _usuario(db, rol='empacador')
        emp.puede_empacar = True
        db.session.commit()
        r = client.get('/api/packing/?activas=true', headers=_jwt(app, emp))
        assert r.status_code == 200, r.get_json()
        assert any(x['id'] == t.id for x in r.get_json()['tareas'])
        out = _node(tmp_path, [str(PWA / 'util.js'), str(PWA / 'modal.js'), str(PWA / 'packing.js')],
                    {'/api/packing/': r.get_json()},
                    "OPERARIO = {rol: 'empacador', puede_empacar: true}; await empCargarTareas();"
                    " return {html: document.getElementById('emp-lista').innerHTML};")
        v = visible(out['html'])
        assert 'Reintentar Siesa' not in v and 'Limpiar bultos' not in v, v[:600]


# ═════════════════════════════════════════════════════════════════════════════
# P1 · Recepción parcial de un traslado: la diferencia queda en TRA1 sin rastro
# ═════════════════════════════════════════════════════════════════════════════

class TestLaDiferenciaDeUnTrasladoNoSeEvapora:
    """**P1 — inventario en el limbo sin que nadie lo sepa.**

    Ese día: ST NB1→NS1, 10 colores despachados (STS 173076 NB1→TRA1 por 10),
    la tienda contó 9 y confirmó «recepción parcial» (el modal de `tienda.js`
    lo permite). El ETS 173079 entró 9 a NS1: **1 unidad quedó en TRA1**. Ningún
    registro del WMS la nombra: `confirmar_recepcion` guarda `cantidad_recibida`
    y sigue (`traslado_service.py:1618-1621`), la solicitud queda ENTREGADA sin
    `siesa_error`, ninguna alerta, y la auditoría de traslados no la ve (TRA-11
    mira el ETS que no entró; TRA-01 lo que crece, no lo que se pierde).
    `💸 Fugas → Mercancía en limbo` depende de FOTOS_SIESA y no la atribuye al
    traslado. Una tienda que recibió de menos no reclama lo que no sabe.
    """

    @pytest.mark.xfail(strict=True, reason='nadie registra enviada − recibida de un traslado ENTREGADO')
    def test_la_auditoria_ve_lo_que_no_llego(self, db, almacen, producto):
        from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
        from app.services import auditoria
        from app.services.traslado_service import TrasladoService
        u = _usuario(db, rol='tienda')
        st = SolicitudTraslado(codigo=f'ST-VAL-{uuid.uuid4().hex[:6]}', bodega_origen_siesa='NB1',
                               bodega_destino_siesa='NS1', solicitante_id=u.id, estado='EN_TRANSITO',
                               modo_transferencia='EN_TRANSITO', bodega_transito_siesa='TRA1',
                               siesa_salida_consec=925, fecha_despacho=datetime.utcnow())
        db.session.add(st)
        db.session.flush()
        it = ItemSolicitudTraslado(solicitud_id=st.id, producto_id=producto.id,
                                   producto_codigo_siesa=producto.codigo_siesa or 'SKU-VAL',
                                   cantidad_solicitada=10, cantidad_aprobada=10, cantidad_enviada=10)
        db.session.add(it)
        db.session.commit()
        TrasladoService.confirmar_recepcion(st.id, usuario_id=u.id,
                                            items_recibidos=[{'id': it.id, 'cantidad_recibida': 9}])
        db.session.refresh(st)
        assert st.estado == 'ENTREGADA'
        # El ETS entró (como en producción): lo único raro es la unidad que faltó.
        st.siesa_entrada_consec = 926
        st.siesa_error = None
        db.session.commit()
        r = auditoria.auditar('traslados')
        vistos = json.dumps([x.get('hallazgos') for x in r['resultados']], ensure_ascii=False, default=str)
        assert st.codigo in vistos, 'la unidad que no llegó no aparece en ningún invariante de traslados'


# ═════════════════════════════════════════════════════════════════════════════
# P2 · Pestañas que el shell le muestra a un rol y el servidor le niega
# ═════════════════════════════════════════════════════════════════════════════

_CARGA_DE_PESTANA = {
    'tab-muelle': '/api/muelle/listos',
    'tab-bodega': '/api/picking/?activas=true&per_page=50',
    'tab-connekta': '/api/siesa/monitor',
    'tab-reposicion': '/api/reposicion/siesa-jobs/fallidos',
}


class TestUnaPestanaVisibleNoCargaEnRojo:
    """**P2 — pantallas que el rol ve y no puede abrir.** El gerente entra al
    shell de admin con TODAS las pestañas (`app.js:195`: `esAdmin` incluye
    `gerente`; solo se le esconde ⛔ Cartera). Medido ese día con el token de
    cada rol sobre la URL que cada pestaña pide al entrar: Muelle → «Error
    cargando muelle · Sin permiso» (y se re-pide cada 8 s), Bodega → «Error
    cargando tareas de bodega», Siesa → 7 lecturas en 403, Reposición → 403.
    Jefe de almacén y supervisor: la pestaña Siesa con 5-6 lecturas en 403. O se
    esconde la pestaña, o el rol puede leerla: las dos cosas a la vez no."""

    @pytest.mark.parametrize('rol', ['gerente', 'jefe_almacen', 'supervisor'])
    def test_toda_pestana_visible_carga(self, app, client, db, rol):
        # Cerrado 2026-09-27: se mide lo que la pestaña PIDE de verdad con ese
        # rol (arnés de `test_pestanas_por_rol`). Reposición sigue visible para
        # el gerente y ya no pide la alerta de jobs fallidos con su rol.
        from tests.test_pestanas_por_rol import pestanas
        d = pestanas(rol)
        visibles = set(d['visibles'])
        u = _usuario(db, rol=rol)
        negadas = []
        for tab, url in _CARGA_DE_PESTANA.items():
            if tab in visibles and url in d['gets'].get(tab, []):
                s = client.get(url, headers=_jwt(app, u)).status_code
                if s == 403:
                    negadas.append((tab, url))
        assert not negadas, negadas


# ═════════════════════════════════════════════════════════════════════════════
# P2 · La reconciliación de ruta cuenta como fuga lo devuelto y lo retenido
# ═════════════════════════════════════════════════════════════════════════════

class TestLaReconciliacionNoInventaFugas:
    """**P2 — un número de plata que miente en la pantalla de quien liquida.**

    Ese día, ruta 2: PD1003 PARCIAL (1 carpeta devuelta, NC $16.660) con
    retención 2,5 % confirmada (NI $2.025). Liquidación → Reconciliación dijo
    «Se entregó y no se registró cobro · 0 docs · **$24.510**» y a la vez
    «Ciclos rotos 0 · dentro de la vara». La fuga real era $5.825 (el conductor
    trajo de menos); los otros $18.685 son la nota crédito y la retención,
    documentos legítimos. `reconciliacion_ruta.py:162-169` suma `valor_factura`
    completo como «debían cobrarse» para PARCIAL y para toda parada con
    descuento — el detalle por caso ya exime la parcial (líneas 221-228), la
    columna no."""

    def test_una_parcial_cobrada_completa_no_es_fuga(self, db, almacen):
        from app.services.reconciliacion_ruta import reconciliar
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C02', valor=113_050)
        r = _recaudo(db, t, estado='PARCIAL', monto=96_390, nc=True)
        d = reconciliar(r.ruta_id)
        fuga = next(f for f in d['fugas'] if f['tramo'] == 'entrega_sin_cobro')
        assert fuga['valor'] <= 1, d

    def test_una_retencion_confirmada_no_es_fuga(self, db, almacen):
        from app.services.reconciliacion_ruta import reconciliar
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C02', valor=100_000)
        r = _recaudo(db, t, estado='ENTREGADO', monto=97_500, descuento=2_500)
        r.motivo_descuento = 'RETEFUENTE_2.5'
        r.retencion_confirmada = True
        db.session.commit()
        d = reconciliar(r.ruta_id)
        fuga = next(f for f in d['fugas'] if f['tramo'] == 'entrega_sin_cobro')
        assert fuga['valor'] <= 1, d


# ═════════════════════════════════════════════════════════════════════════════
# P2/P3 · Parada de la oficina con otra versión del teléfono
# ═════════════════════════════════════════════════════════════════════════════

class TestLaSenalDeLaOficinaNoSeContradice:
    """**P3 — dos señales que se niegan entre sí, sobre plata.** Ese día la
    oficina registró PD1008 en EFECTIVO; a las 18:20 el teléfono del conductor
    mandó TRANSFERENCIA (NEQUI-5566). Liquidación pintó a la vez «La registró
    la oficina…: **el conductor no la confirmó**» y «El conductor mandó después
    otra versión de esta parada (la forma de pago)». `senales_ruta.py:460`
    emite la primera con `diferencia_conductor is not False` — o sea también
    cuando es `True`. (Aparte, y sin test: la ruta se liquidó y el RC salió
    EFECTIVO sin que nadie resolviera la diferencia; no hay acción para adoptar
    ni descartar la versión del teléfono — «Revíselo con él» queda para siempre.)"""

    def test_si_el_conductor_mando_otra_version_no_dice_que_no_la_confirmo(self, db, almacen):
        from app.services.senales_ruta import senales_de_recaudo
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C02', valor=39_270)
        r = _recaudo(db, t, estado='ENTREGADO', monto=39_270)
        r.registrada_por_oficina = True
        r.diferencia_conductor = True
        r.version_conductor = {'forma_pago': 'TRANSFERENCIA', 'campos_que_difieren': ['forma_pago']}
        db.session.commit()
        textos = ' | '.join(s['texto'] for s in senales_de_recaudo(r))
        assert 'otra versión' in textos
        assert 'no la confirmó' not in textos, textos


# ═════════════════════════════════════════════════════════════════════════════
# P2 · Fugas promete una nota crédito que no va a existir
# ═════════════════════════════════════════════════════════════════════════════

class TestFugasNoPrometeUnaNotaCreditoQueNoViene:
    """**P2 — texto que contradice el dato.** Ese día PD1005 (cliente cerrado)
    se contó en cero → FALTANTE_TOTAL: no habrá nota crédito (Liquidación ya lo
    dice bien desde el 25). 💸 Fugas → Devoluciones en ruta pintó el caso como
    «NC: **Sin nota crédito todavía** · Faltó al volver (und): 8» y sumó sus 8
    unidades a «15 unidades devueltas» (volvieron 7)."""

    def test_el_caso_del_faltante_total_no_dice_todavia(self, app, client, db, almacen):
        from app.utils.fecha import dia_operativo
        from tests.test_e2e_total_20260925 import _faltante_total
        _faltante_total(db, almacen)
        hoy = dia_operativo().isoformat()
        admin = _usuario(db, rol='admin')
        r = client.get(f'/api/analitica/fugas/devoluciones_ruta?desde={hoy}&hasta={hoy}&page=1&per_page=25',
                       headers=_jwt(app, admin))
        assert r.status_code == 200, r.get_json()
        caso = next(c for c in r.get_json()['casos'] if c['referencia'] == 'PD1005')
        assert 'todavía' not in (caso['detalle'].get('nota_credito') or ''), caso
        assert 'No habrá' in caso['detalle']['nota_credito'], caso
        # Faltante ≠ devuelto: lo que no volvió no se cuenta como devuelto.
        assert not caso.get('unidades'), caso


# ═════════════════════════════════════════════════════════════════════════════
# P3 · «Envíos» de Liquidación: hora corrida 5 h y códigos crudos
# ═════════════════════════════════════════════════════════════════════════════

class TestLosEnviosDeLiquidacionEnHoraDeBogota:
    """**P3.** El liquidador abrió «Envíos» a las 18:20 y cada RC decía
    «30/9, **11:20 p. m.**» con «RECIBO_CAJA», «DOCUMENTO_CONTABLE_RET» y
    «Ref: RecaudoEntrega #9». `SiesaJob.to_dict` manda `fecha_creacion` UTC sin
    zona y `_liqJobCard` (`liquidacion.js:402`) hace `new Date(...)` sin
    `timeZone`: en un teléfono de Bogotá lo lee como hora local."""

    def test_la_tarjeta_dice_la_hora_de_bogota_y_en_palabras(self, tmp_path):
        job = {'id': 9, 'tipo': 'RECIBO_CAJA', 'estado': 'COMPLETADO', 'intentos': 0, 'max_intentos': 5,
               'fecha_creacion': '2026-09-30T23:20:01.381611', 'referencia_tipo': 'RecaudoEntrega',
               'referencia_id': 9, 'error_ultimo': None, 'puede_reintentar': False}
        out = _node(tmp_path, [str(PWA / 'util.js'), str(PWA / 'liquidacion.js')], {},
                    f"return {{html: _liqJobCard({json.dumps(job)}, false)}};")
        v = visible(out['html'])
        assert '06:20' in v or '6:20' in v, v
        assert 'RECIBO_CAJA' not in v, v


# ═════════════════════════════════════════════════════════════════════════════
# P3 · Voz usted: lo que el detector no ve
# ═════════════════════════════════════════════════════════════════════════════

class TestLaVozUstedSinHuecos:
    """**P3 — la regla del dueño, con cuatro textos visibles en tú** que
    `tests/test_voz_usted.py::hallazgos` no detecta (el subjuntivo «decidas» y
    el imperativo «Actualiza» no están en su vocabulario): la portada de
    Analítica («Todavía no decidas con estos números», `analitica_portada.js:41`,
    la primera línea que ve el gerente), la Salud (`analitica_salud.py:116`),
    Inventario (`conteo.js:600`, `title`) y el 400 de despachar un traslado
    (`traslado_service.py:708`: «Actualiza la página en unos segundos»)."""

    @pytest.mark.parametrize('texto', [
        'Todavía no decidas con estos números',
        'la diferencia queda para que la decidas',
        'se está procesando automáticamente (job 16). Actualiza la página en unos segundos.',
    ])
    def test_el_detector_lo_ve(self, texto):
        from tests.test_voz_usted import hallazgos
        assert hallazgos(texto), texto


# ═════════════════════════════════════════════════════════════════════════════
# P3 · El KPI «Crédito» de Liquidación siempre vale $0
# ═════════════════════════════════════════════════════════════════════════════

class TestElKpiDeCreditoMideAlgo:
    """**P3 — un número que no puede moverse.** Ese día Liquidación mostró
    «Crédito $0» con cuatro entregas a crédito por $821.100. `routes/rutas.py:1456`
    suma `monto_cobrado` de las paradas CREDITO, que es 0 por definición."""

    def test_una_entrega_a_credito_suma(self, app, client, db, almacen):
        from app.utils.fecha import dia_operativo
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C04', valor=357_000)
        r = _recaudo(db, t, estado='ENTREGADO', forma='CREDITO', monto=0)
        r.ruta.fecha_programada = dia_operativo()
        r.fecha_confirmacion = datetime.utcnow()
        db.session.commit()
        hoy = dia_operativo().isoformat()
        admin = _usuario(db, rol='admin')
        d = client.get(f'/api/rutas/liquidacion/dashboard?fecha_desde={hoy}&fecha_hasta={hoy}',
                       headers=_jwt(app, admin)).get_json()
        assert d['resumen']['total_credito'] > 0, d['resumen']

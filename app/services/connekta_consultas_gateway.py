"""
Dominio Consultas (GETs de catálogo/stock/pedidos/OC/CxC) del gateway
Connekta — extraído de ConnektaGateway (2026-09-09, paso 5 de la deuda de
tamaño; ver pasos 1-4: app/utils/siesa_formato.py,
connekta_circuit_breaker.py, connekta_compras_gateway.py,
connekta_ajustes_gateway.py).

Es el dominio más grande del gateway (29 métodos en el análisis original) y
sus métodos NO están contiguos en connekta_gateway.py — están intercalados
entre Traslados y NC/Liquidación. Por eso esta extracción avanza en
sub-lotes, no de un tirón:
  1. bodegas/ubicaciones/stock (el más autocontenido).
  2. pedidos/FE/catálogo/OC/compromisos (este lote, 20 métodos).
  3. terceros/facturas/CxC — pendiente, intercalado en NC/Liquidación.

Dos métodos de este lote se llaman entre sí (ambos migraron juntos, por
eso siguen siendo `self.X`, no `core.X`): `validar_tipo_proveedor` llama a
`self.get_ordenes_compra_aprobadas`, y `get_pedido_rowid_map` llama a
`self.get_compromisos_pedido`.

**No recibe config propia** — mismo patrón que los pasos anteriores: toma
la instancia completa de `ConnektaGateway` como `core`. `ConnektaGateway`
conserva cada método como delegado delgado; el singleton `connekta` y sus
callers no cambian. Confirmado con `tests/test_alertas_dedup.py`, que
mockea `core._get` y llama `gw._fetch_stock_pages(...)` directo — como
`core` es la MISMA instancia (no una copia), el mock se ve igual.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaConsultasGateway:

    # Límite de página de Connekta para API_v2_Inventarios_InvFecha.
    # tamPag=120+ devuelve alerta "registros exceden el permitido"; 100 es el máximo seguro.
    _CONNEKTA_MAX_TAM_PAG = 100
    # Páginas paralelas por bodega — 3 es seguro sin riesgo de 429 (backoff=5min).
    _STOCK_BATCH_SIZE = 3

    def __init__(self, core):
        self._core = core

    def get_bodegas_siesa(self):
        """API_v2_Bodegas (ID 2) — lista todas las bodegas configuradas en Siesa.
        Usar para descubrir los IDs de bodega de los puntos de venta sin esperar a Siesa."""
        return self._core._get('API_v2_Bodegas', {
            'paginacion': 'numPag=1|tamPag=200'
        })

    def get_ubicaciones_siesa(self, bodega_id: str = None, pagina: int = 1):
        """
        API_v2_Ubicaciones (ID 43) — maestro de ubicaciones auxiliares de una bodega.

        Campos certificados (PDF oficial Connekta):
          f150_id          → código de bodega           (string, ej. 'NB1')
          f155_id_cia      → id empresa ERP             (number)
          f155_id          → código de la ubicación     (string, ej. 'PIK-01-A')
          f155_descripcion → descripción de la ubicación (string)
          f155_ind_estado  → 0=Inactivo, 1=Activo       (number)

        IMPORTANTE: La API no expone stock_minimo/stock_maximo — esos campos
        se configuran directamente en el WMS (tabla ubicaciones).

        El tipo_zona se deduce del prefijo del código:
          PIK* → PICKING   |   RES* → RESERVA   |   resto → GENERAL
        """
        import os

        from app.services.siesa_filtro import lit as _lit

        api_name = os.getenv('CONNEKTA_API_UBICACIONES', 'API_v2_Ubicaciones')
        params: dict = {'paginacion': f'numPag={pagina}|tamPag=100'}
        if bodega_id:
            params['parametros'] = f"f150_id = {_lit(bodega_id)}"
        return self._core._get(api_name, params)

    # transferir_entre_ubicaciones() (conector 173066 para RESERVA→PICKING) se
    # retiró 2026-09-07: ambas ubicaciones son la misma bodega Siesa (NB1 no
    # tiene sub-bodegas para picking/reserva), así que no había ningún
    # documento real que declarar. Probado en vivo contra Siesa QA el mismo
    # día: rechazado por tamaño de registro (2658 vs 2700 exigidos) — nunca
    # se había ejercido contra Siesa real antes de esa prueba, pese al
    # comentario que afirmaba lo contrario. transferencia_directa() (en
    # ConnektaGateway) es la función hermana que SÍ sigue en uso — traslados
    # reales inter-bodega, donde origen y destino son bodegas Siesa distintas.

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega específica.

        La API de Connekta usa paginación offset-based que no es determinística
        bajo requests concurrentes: filas se barajan entre páginas, causando que
        productos se dupliquen en una página y desaparezcan de otra.

        Estrategia: dos pasadas paralelas + merge por f120_id para maximizar
        cobertura. La probabilidad de que el mismo producto falte en AMBAS
        pasadas es despreciable (~0.01%)."""
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return core._get(core.api_inventario, {
                'paginacion': 'numPag=1|tamPag=3',
                'parametros': f"f150_id = {_lit(bodega_id)} AND f400_cant_existencia_1 > 0"
            })

        rows_pass1 = self._fetch_stock_pages(bodega_id)
        by_id = {r.get('f120_id'): r for r in rows_pass1}
        dupes = len(rows_pass1) - len(by_id)

        if dupes > 0:
            logger.warning(
                '[CONNEKTA] stock %s pass1: %d filas, %d duplicados por f120_id — '
                'paginación inestable, ejecutando segunda pasada',
                bodega_id, len(rows_pass1), dupes,
            )
            rows_pass2 = self._fetch_stock_pages(bodega_id)
            for r in rows_pass2:
                fid = r.get('f120_id')
                if fid not in by_id:
                    by_id[fid] = r
            logger.info(
                '[CONNEKTA] stock %s merge: %d únicos (pass1=%d, pass2=%d, nuevos=%d)',
                bodega_id, len(by_id), len(rows_pass1), len(rows_pass2),
                len(by_id) - (len(rows_pass1) - dupes),
            )
        else:
            logger.info('[CONNEKTA] stock %s: %d filas, 0 duplicados — paginación OK', bodega_id, len(by_id))

        return {'detalle': {'Table': list(by_id.values())}}

    def _fetch_stock_pages(self, bodega_id: str):
        """Una pasada completa de paginación paralela (batch=3)."""
        from concurrent.futures import ThreadPoolExecutor

        from app.services.connekta_gateway import ConnektaPaginacionError, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        tam = self._CONNEKTA_MAX_TAM_PAG
        batch = self._STOCK_BATCH_SIZE
        all_rows = []
        pag = 1

        while pag <= 200:
            pages_in_batch = list(range(pag, min(pag + batch, 201)))

            def _fetch(p, _bod=bodega_id, _tam=tam):
                try:
                    res = core._get(core.api_inventario, {
                        'paginacion': f'numPag={p}|tamPag={_tam}',
                        'parametros': f"f150_id = {_lit(_bod)} AND f400_cant_existencia_1 > 0"
                    })
                    # `_get` devuelve None cuando el circuit breaker bloquea o
                    # la respuesta no es 200. Sin esto, el `res.get('_error')`
                    # de abajo reventaba con AttributeError sobre None.
                    if res is None:
                        return {'_error': True, 'motivo': 'sin respuesta',
                                'pagina': p, 'detalle': {'Table': []}}
                    return res
                except Exception as e:
                    return {'_error': True, 'motivo': str(e)[:120],
                            'pagina': p, 'detalle': {'Table': []}}

            with ThreadPoolExecutor(max_workers=batch) as ex:
                batch_results = list(ex.map(_fetch, pages_in_batch))

            done = False
            for res in batch_results:
                if res.get('_error'):
                    # NO se sigue de largo. Saltar una página deja su stock
                    # afuera del resultado, y el llamador recibe un inventario
                    # INCOMPLETO que no se distingue de uno completo: los
                    # productos de esa página aparecen como si no existieran.
                    #
                    # Antes esto era `continue` a secas — sin log, sin contador
                    # y sin abortar. Una consulta de diagnóstico que descarta
                    # páginas en silencio informa lo contrario de lo que pasó.
                    logger.error(
                        '[CONNEKTA] stock %s: página %s falló (%s) — se aborta '
                        'la consulta: un inventario parcial que parece completo '
                        'es peor que un error',
                        bodega_id, res.get('pagina', '?'),
                        res.get('motivo', 'sin motivo'))
                    raise ConnektaPaginacionError(
                        f'La consulta de stock de {bodega_id} falló en la página '
                        f'{res.get("pagina", "?")}: {res.get("motivo", "")}. '
                        f'No se devuelve un inventario parcial.'
                    )
                rows = res.get('detalle', {}).get('Table', [])
                # Este módulo ya se niega a devolver un inventario parcial
                # (`ConnektaPaginacionError`, justo arriba). Un rechazo de
                # filtro leído como «se acabaron las páginas» abría el mismo
                # agujero por la otra puerta.
                _exigir_datos(rows, '_fetch_stock_pages',
                              f'bodega={bodega_id}')
                if not rows:
                    done = True
                    break
                all_rows.extend(rows)
                if len(rows) < tam:
                    done = True
                    break

            if done:
                break
            pag += batch

        return all_rows

    def get_estado_pedido(self, tipo_docto: str, consec_docto) -> int | None:
        """
        Consulta el ind_estado actual de un pedido específico en Siesa.
        Retorna:
          - int positivo (1-9): estado real del pedido
          - -1: pedido no encontrado en Siesa (eliminado o nunca existió)
          - None: error de red / tipo_docto vacío (no se pudo consultar)
        Se usa como pre-check en cerrar_packing y detección de anulados en pedidos_sync.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return 3  # simulación asume siempre comprometido

        if not tipo_docto or not str(tipo_docto).strip():
            logger.warning(
                '[CONNEKTA] get_estado_pedido: tipo_docto vacío — '
                'no se puede verificar estado en Siesa (consec=%s)', consec_docto
            )
            return None

        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = core._get(core.api_pedidos, {
                'paginacion': 'numPag=1|tamPag=1',
                'parametros': (
                    f"f430_id_co = {_lit(core.centro_op)} "
                    f"AND f430_id_tipo_docto = {_lit(tipo_docto)} "
                    f"AND f430_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            if not rows:
                return -1  # pedido no encontrado en Siesa (eliminado o nunca existió)
            return rows[0].get('f430_ind_estado')
        except Exception as e:
            logger.warning(f'[CONNEKTA] get_estado_pedido falló silenciosamente: {e}')
            return None  # error de red — no bloqueamos, el POST revelará el error

    def get_factura_desde_pedido(self, tipo_docto: str, consec_docto) -> list:
        """
        Consulta si ya existe una factura activa (no anulada) generada desde un pedido.
        Retorna lista de facturas activas. Lista vacía = sin factura previa, proceder.
        Guard anti-duplicado en cerrar_packing antes de disparar trigger_factura (238925).
        SKIP_FE_CHECK=true omite el guard (solo QA — nunca en producción).
        """
        import os

        from app.services.connekta_gateway import ConnektaConsultaRechazada, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion or os.getenv('SKIP_FE_CHECK', '').lower() == 'true':
            return []

        if not tipo_docto or not str(tipo_docto).strip():
            return []

        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            parametros = (
                f"f350_id_co = {_lit(core.centro_op)} "
                f"AND f430_consec_docto = {consec_int}"
            )
            # `papeleriamedellin_monitos_facturas_wms` (consulta dinámica
            # custom) NO está registrada en Connekta — confirmado 401 en vivo
            # contra Siesa QA (2026-08-14 y otra vez 2026-09-04). La API
            # estándar ya registrada `API_v2_Ventas_Facturas_DesdePedido`
            # acepta el mismo filtro por `f430_consec_docto` (verificado en
            # vivo, 2026-09-04) — es la misma que ya usan
            # `get_rowids_factura`/`get_factura_desde_remision`, solo que acá
            # se filtra por el PEDIDO en vez de por la FE o la RM (todavía no
            # existen en el momento en que este precheck corre).
            res = core._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=50',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            # **La otra puerta.** El `except` de abajo protege del error de red;
            # el sobre de rechazo de Connekta llega por HTTP 200 y no lanza
            # nada. Sin esta línea, `{'alerta': ...}` no trae
            # `f350_ind_estado`, el default `'9'` la marca como anulada, la
            # filtra, y esta función devuelve `[]` = «no hay factura previa,
            # seguí» — con el guard fail-fast intacto justo al lado.
            rows = _exigir_datos(rows, 'get_factura_desde_pedido', parametros)
            return [r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']
        except ConnektaConsultaRechazada:
            raise
        except Exception as e:
            # FAIL-FAST: no retornar [] ante error de red — el caller asumiría que no hay FE
            # y dispararía trigger_factura (238925) generando FE duplicada (riesgo fiscal / DIAN).
            logger.error('[CONNEKTA] get_factura_desde_pedido falló — abortando para evitar FE duplicada: %s', e)
            raise Exception(
                f'No se pudo verificar si ya existe FE para pedido {tipo_docto}-{consec_docto}: {e}. '
                'Reintenta cuando Connekta esté disponible.'
            )

    def get_factura_desde_remision(self, tipo_docto_rm: str, consec_rm) -> list:
        """
        Pre-check anti-duplicado para 142943 (FacturaDesdeRemision).
        Consulta si ya existe una FE activa (no anulada) vinculada a la remisión.
        Retorna lista de facturas activas. Lista vacía = sin factura previa, proceder.
        Usa el mismo API que get_factura_desde_pedido filtrando por el documento base
        (f460_id_tipo_docto / f460_consec_docto) que identifica la RM en RelacionDoctos.
        """
        from app.services.connekta_gateway import ConnektaConsultaRechazada, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return []

        if not tipo_docto_rm or not str(tipo_docto_rm).strip():
            return []

        try:
            consec_int = int(consec_rm) if str(consec_rm).isdigit() else consec_rm
            parametros = (
                f"f350_id_co = {_lit(core.centro_op)} "
                f"AND f460_id_tipo_docto = {_lit(tipo_docto_rm)} "
                f"AND f460_consec_docto = {consec_int}"
            )
            res = core._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=50',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            # Ver la nota de `get_factura_desde_pedido`: el sobre de rechazo
            # entra por la puerta de datos, no por la de excepciones, y el
            # default `'9'` lo hacía desaparecer.
            rows = _exigir_datos(rows, 'get_factura_desde_remision', parametros)
            return [r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']
        except ConnektaConsultaRechazada:
            raise
        except Exception as e:
            # FAIL-FAST: no retornar [] ante error de red — eso haría creer que no hay FE
            # y el caller procedería a crear una FE duplicada (riesgo fiscal / DIAN).
            # El caller debe capturar esta excepción y abortar el despacho.
            logger.error('[CONNEKTA] get_factura_desde_remision falló — abortando para evitar FE duplicada: %s', e)
            raise Exception(
                f'No se pudo verificar si ya existe FE para RM {tipo_docto_rm}-{consec_rm}: {e}. '
                'Reintenta cuando Connekta esté disponible.'
            )

    def get_punto_envio_factura(self, f350_rowid) -> dict:
        """
        Consulta dinámica papeleriamedellin_WMS_PuntoEnvio_FE.
        Retorna datos del punto de envío (T462): contacto, ciudad, dirección,
        barrio, teléfono, celular.

        REQUIERE crear esta consulta en Connekta QA con el SQL:
          SELECT TOP 1
            f462_contacto, f462_barrio, f462_direccion,
            f462_telefono, f462_celular,
            f202_descripcion AS ciudad
          FROM t462_cm_venta_docto_penv penv
          INNER JOIN t461_cm_venta_docto v461
            ON v461.f461_rowid = penv.f462_rowid_cm_venta_docto
          LEFT JOIN t202_co_municipio mun
            ON mun.f202_id_cia = penv.f462_id_cia
            AND mun.f202_id = penv.f462_id_ciudad
          WHERE v461.f461_rowid_co_docto_contable = @rowid
            AND penv.f462_id_cia = 1
        """
        core = self._core
        if core.modo_simulacion or not f350_rowid:
            return {}
        try:
            # Pasar rowid como parámetro directo de URL (no en 'parametros') —
            # el endpoint ejecutarconsulta sustituye @rowid por el valor del param rowid.
            res = core._get(
                'papeleriamedellin_WMS_PuntoEnvio_FE',
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    'rowid': int(f350_rowid),
                },
                url=core.url_get_dinamico,
            )
            rows = (
                res.get('detalle', {}).get('Datos') or
                res.get('detalle', {}).get('Table') or []
            )
            if rows:
                logger.info('[CONNEKTA] get_punto_envio_factura OK rowid=%s keys=%s',
                            f350_rowid, list(rows[0].keys()))
            return rows[0] if rows else {}
        except Exception as e:
            logger.warning('[CONNEKTA] get_punto_envio_factura falló silenciosamente: %s', e)
            return {}

    def get_detalle_factura(self, tipo_docto_rm: str, consec_rm,
                             consec_pedido=None) -> list:
        """
        GET API_v2_Ventas_Facturas_DesdePedido — detalle completo de la FE para impresión.
        Intento 1: filtra por RM (f460_id_tipo_docto / f460_consec_docto).
        Intento 2 (fallback): filtra por consec_pedido si intento 1 devuelve
        vacío **o si Siesa lo rechaza** — un rechazo del filtro RM no implica
        que la FE no exista (visto en vivo el 2026-08-20: el intento por
        pedido la trae completa). Sin `consec_pedido` un rechazo del intento
        1 sí sigue subiendo — no hay con qué reemplazarlo.
        Falla silenciosamente: uso exclusivo de display, nunca de anti-duplicado.
        """
        from app.services.connekta_gateway import ConnektaConsultaRechazada, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return []

        def _query(parametros: str) -> list:
            res = core._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=100',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            # Antes: `[r for r in rows if 'alerta' not in r]` — tiraba el
            # rechazo y seguía con `[]`, que acá se lee «la factura no tiene
            # líneas».
            rows = _exigir_datos(rows, 'get_detalle_factura', parametros)
            if rows:
                logger.info('[CONNEKTA] get_detalle_factura: %d filas, keys=%s',
                            len(rows), list(rows[0].keys()) if rows else [])
            return rows

        try:
            # Intento 1: filtrar por documento base (RM)
            if tipo_docto_rm and str(tipo_docto_rm).strip():
                consec_int = int(consec_rm) if str(consec_rm).isdigit() else consec_rm
                try:
                    rows = _query(
                        f"f350_id_co = {_lit(core.centro_op)} "
                        f"AND f460_id_tipo_docto = {_lit(tipo_docto_rm)} "
                        f"AND f460_consec_docto = {consec_int}"
                    )
                    if rows:
                        return rows
                    logger.info('[CONNEKTA] get_detalle_factura intento RM vacío — probando por pedido')
                except ConnektaConsultaRechazada as e:
                    # Un RECHAZO del intento RM no es "no hay factura" — visto
                    # en vivo el 2026-08-20: Siesa rechaza este filtro para
                    # remisiones reales cuya FE sí existe (confirmado con el
                    # intento por pedido, que la trae completa). Sin este
                    # catch, `resolver_fe` nunca llegaba al intento 2 y
                    # `_valor_y_cond_pago`/`listar_paradas` se quedaban sin
                    # dato (o, antes del fix del unpack, con un 500).
                    #
                    # Sin un segundo intento que lo reemplace (sin
                    # consec_pedido) SÍ hay que dejarlo subir — tragárselo acá
                    # lo convertiría en "no hay nada", justo lo que
                    # test_un_rechazo_de_siesa_no_se_lee_como_lista_vacia
                    # prohíbe.
                    if not consec_pedido:
                        raise
                    logger.warning(
                        '[CONNEKTA] get_detalle_factura intento RM rechazado — '
                        'probando por pedido: %s', e)

            # Intento 2: filtrar por pedido origen
            if consec_pedido:
                consec_ped_int = int(consec_pedido) if str(consec_pedido).isdigit() else consec_pedido
                rows = _query(
                    f"f350_id_co = {_lit(core.centro_op)} "
                    f"AND f430_consec_docto = {consec_ped_int}"
                )
                if rows:
                    return rows
                logger.info('[CONNEKTA] get_detalle_factura intento pedido también vacío')

            return []
        except ConnektaConsultaRechazada:
            # **No se traga.** El `except Exception → return []` de abajo
            # convierte cualquier error en «la factura no tiene líneas» —su
            # propio log lo llamaba «falló silenciosamente»—, así que sin esta
            # línea el rechazo se pierde igual que antes, solo que un nivel
            # más arriba. Un raise que alguien atrapa no es un raise.
            raise
        except Exception as e:
            logger.warning('[CONNEKTA] get_detalle_factura falló: %s', e)
            return []

    def get_pedidos_aprobados(self, sin_filtros: bool = False):
        """
        Cola viva de picking: filtra por CO y estado directo en Connekta.
        Sintaxis oficial: strings con comillas dobles simples ''valor''.
        Pagina solo los resultados filtrados (~pocos registros).
        """
        from app.services.connekta_gateway import _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return core._simular('GET_pedidos_aprobados')

        # Sintaxis oficial Connekta: strings con ''valor'', enteros sin comillas
        if sin_filtros:
            parametros = 'f430_ind_estado = 3'
        else:
            # estado=3 → Comprometido: inventario físicamente reservado en Siesa
            # estado=2 (Aprobado) NO entra — el inventario no está reservado aún
            parametros = f"f430_id_co = {_lit(core.centro_op)} AND f430_ind_estado = 3"

        all_items = []
        _errores_consec = 0
        for pag in range(1, 200):
            try:
                res = core._get(core.api_pedidos, {
                    'paginacion': f'numPag={pag}|tamPag=100',
                    'parametros': parametros
                })
                _errores_consec = 0
            except Exception as e:
                _errores_consec += 1
                logger.warning(
                    f'[CONNEKTA] get_pedidos_aprobados pag={pag} error ({_errores_consec}/3): {e}'
                )
                if _errores_consec >= 3:
                    raise Exception(
                        f'get_pedidos_aprobados abortó tras 3 errores consecutivos en pág={pag} — '
                        f'{len(all_items)} ítems parciales descartados'
                    )
                continue
            rows = res.get('detalle', {}).get('Table', [])
            # Un rechazo no es «se acabaron las páginas»: cortar acá devolvía
            # lo que hubiera juntado, sin decir que se cortó.
            _exigir_datos(rows, 'get_pedidos_aprobados', parametros)
            if not rows:
                break
            all_items.extend(rows)
            if len(rows) < 100:
                break

        items_pendientes = []
        for item in all_items:
            if not sin_filtros:
                if item.get('f150_id', '').strip() != core.bodega:
                    continue
            try:
                cant_pedida = float(item.get('f431_cant1_pedida', 0))
                cant_remisionada = float(item.get('f431_cant1_remisionada', 0))
                cant_pendiente = cant_pedida - cant_remisionada
                if cant_pendiente > 0:
                    items_pendientes.append({
                        'numero_pedido': f"{item.get('f430_id_tipo_docto','').strip()}{item.get('f430_consec_docto','')}",
                        'tipo_docto': item.get('f430_id_tipo_docto', '').strip(),
                        'consec_docto': item.get('f430_consec_docto'),
                        'centro_op': item.get('f430_id_co'),
                        'bodega': item.get('f150_id'),
                        'item_codigo': item.get('f120_referencia'),
                        'item_descripcion': item.get('f120_descripcion'),
                        'item_id_siesa': item.get('f120_id'),
                        'cantidad_pedida': cant_pedida,
                        'cantidad_remisionada': cant_remisionada,
                        'cantidad_pendiente': cant_pendiente,
                        'cliente': item.get('f200_razon_social_pedido_fact'),
                        'fecha_entrega': item.get('f430_fecha_entrega'),
                        'estado': item.get('f430_ind_estado')
                    })
            except (ValueError, TypeError) as e:
                logger.warning(f'[CONNEKTA] Item inválido: {e}')
                continue

        logger.info(f'[CONNEKTA] pedidos: {len(items_pendientes)} pendientes de {len(all_items)} CO{core.centro_op}')
        return {
            'codigo': 0,
            'total_siesa': len(all_items),
            'total_pendientes': len(items_pendientes),
            'items': items_pendientes
        }

    def get_ordenes_compra_aprobadas(self, sin_filtros: bool = False, consec: str = None):
        """API_v2_Compras_Ordenes — muelle de recepción ciega.
        Pagina automáticamente (tamPag=100) hasta agotar los registros.
        Si se pasa consec, filtra por f420_consec_docto (number, sin comillas)
        para traer solo las líneas de esa OC y evitar timeouts.
        """
        from app.services.connekta_gateway import _exigir_datos

        core = self._core
        if consec:
            base_params = {'parametros': f'f420_consec_docto={consec}'}
        elif sin_filtros:
            base_params = {}
        else:
            base_params = {'parametros': 'f420_ind_estado=1'}
        todos = []
        #: **El tope se declara, no se calla.** Antes era `range(1, 6)` con un
        #: comentario —«máximo 5 páginas = 500 items»— y nada en el resultado:
        #: con más de 500 OC aprobadas el WMS veía 500 y el muelle de recepción
        #: leía eso como el universo completo. Un conteo sin su base no es una
        #: medición. Es el mismo tope mudo que el hallazgo K ya había costado
        #: en el sync de pedidos, donde sí quedó declarado.
        _MAX_PAGINAS = 5
        completa = True
        for pag in range(1, _MAX_PAGINAS + 1):
            params = {**base_params, 'paginacion': f'numPag={pag}|tamPag=100'}
            resp = core._get(core.api_ordenes, params)
            if core.modo_simulacion:
                return resp
            rows = resp.get('detalle', {}).get('Table', [])
            _exigir_datos(rows, 'get_ordenes_compra_aprobadas',
                          base_params.get('parametros'))
            if not rows:
                break
            todos.extend(rows)
            if len(rows) < 100:
                break
        else:
            # Se agotaron las páginas sin que ninguna viniera corta: la última
            # llegó llena, así que **hay más del otro lado**.
            completa = False
            logger.warning(
                '[CONNEKTA] get_ordenes_compra_aprobadas: tope de %d páginas '
                'con %d OC — el resultado está TRUNCADO. Filtrar por consec o '
                'subir el tope.', _MAX_PAGINAS, len(todos))
        return {'detalle': {'Table': todos}, 'paginacion_completa': completa}

    def validar_tipo_proveedor(self, nit: str) -> dict:
        """
        Verifica que el NIT tenga tipo_proveedor configurado en el maestro de Siesa.
        Usa las OCs activas para inferirlo — si el proveedor aparece en alguna OC
        y tiene f200_id_tipo_prov, el resultado es positivo.
        Retorna: {configurado: bool, tipo_proveedor: str|None, mensaje: str}
        """
        core = self._core
        if core.modo_simulacion:
            return {'configurado': True, 'tipo_proveedor': '0001', 'mensaje': 'simulado'}
        try:
            # Mismo dominio, mismo objeto — llamada intra-clase, no core.X.
            resultado = self.get_ordenes_compra_aprobadas(sin_filtros=True)
            rows = resultado.get('detalle', {}).get('Table', [])
            for row in rows:
                nit_row = (row.get('f200_nit_prov') or row.get('f200_id_prov') or '').strip()
                if nit_row == nit.strip():
                    tipo = (row.get('f200_id_tipo_prov') or '').strip()
                    if tipo:
                        return {'configurado': True, 'tipo_proveedor': tipo, 'mensaje': ''}
                    # f200_id_tipo_prov no viene en API_v2_Compras_Ordenes — no verificable
                    return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}
            # NIT no aparece en ninguna OC activa — probablemente es correcto pero no podemos verificar
            logger.warning(f'[CONNEKTA] validar_tipo_proveedor: NIT {nit!r} no encontrado en OCs activas')
            return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}
        except Exception as e:
            logger.warning(f'[CONNEKTA] validar_tipo_proveedor falló: {e}')
            return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}

    def get_inventario_fecha(self, item_codigo: str, bodega: str = None):
        """API_v2_Inventarios_InvFecha — existencia real para conteo cíclico.
        bodega: código de bodega Siesa (ej 'NB1'). Si None usa core.bodega del env.
        Timeout reducido a 8s: es user-facing, no puede bloquear un worker Gunicorn.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        _bodega = bodega or core.bodega
        return core._get(core.api_inventario, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f"f120_referencia = {_lit(item_codigo)} AND f150_id = {_lit(_bodega)}"
        }, timeout=8)

    # `get_item_por_barras()` se borró el 2026-08-19: no tenía **ningún**
    # llamador. El escaneo resuelve el EAN contra el catálogo local
    # (`productos`), que alimenta el sync — no contra Siesa en vivo. El camino
    # inverso, `buscar_barras_por_referencia()`, sí se usa y queda abajo.

    def buscar_barras_por_referencia(self, referencia: str):
        """
        Camino inverso de get_item_por_barras(): dada la referencia de un ítem
        (f120_referencia), busca su(s) código(s) de barras en
        API_v2_ItemsBarras — campo f131_id, confirmado 1:1 contra la pantalla
        "Código de barras del ítem" en Siesa (Otros → Código de barras).

        Siesa permite códigos de barras alfanuméricos libres, no solo EAN
        numérico (verificado con el maestro real: el ítem ARTESA898 tiene
        registrado 'F1P' como código de barras, U.M. UND, cantidad fija 1.00)
        — por eso no se valida formato, solo que el campo no esté vacío.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return []
        # El saneo vive en `lit()`. Acá quedaba un `.replace("'", "")` que
        # limpiaba en silencio: buscaba un código distinto del pedido y
        # devolvía un resultado con cara de bueno.
        ref = (referencia or '').strip()
        if not ref:
            return []
        resultado = core._get(core.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f"f120_referencia = {_lit(ref)}"
        })
        tabla = resultado.get('detalle', {}).get('Table', [])
        barras = []
        for row in tabla:
            valor = (row.get('f131_id') or '').strip()
            if valor and valor not in barras:
                barras.append(valor)
        return barras

    def get_items_catalogo(self, pagina: int = 1):
        """API_v2_Items — catálogo completo de productos Siesa (para sync)."""
        import os

        core = self._core
        api_items = os.getenv('CONNEKTA_API_ITEMS', 'API_v2_Items')
        return core._get(api_items, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def buscar_item_por_referencia(self, referencia: str):
        """
        Consulta EN VIVO un único ítem en API_v2_Items filtrado por referencia
        exacta — para la herramienta de Etiquetas cuando el catálogo local
        (sync periódico) aún no trae un ítem recién creado en Siesa.

        No usar en rutas calientes de picking/packing: es una llamada HTTP en
        tiempo real (hasta 30s), a diferencia de get_items_catalogo() que
        alimenta el sync de fondo hacia la tabla local `productos`.
        """
        import os

        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return None
        ref = (referencia or '').strip()
        if not ref:
            return None
        api_items = os.getenv('CONNEKTA_API_ITEMS', 'API_v2_Items')
        # f120_id_cia incluido para calzar con el ejemplo del spec
        # (API_v2_Items.docx: "f120_id_cia = 1 AND f120_referencia = ...").
        try:
            resultado = core._get(api_items, {
                'paginacion': 'numPag=1|tamPag=5',
                'parametros': f"f120_id_cia = {int(core.id_cia_siesa)} AND f120_referencia = {_lit(ref)}"
            })
        except Exception as e:
            # Confirmado en vivo 2026-07-31: cuando el filtro f120_referencia
            # no matchea ninguna fila, Siesa responde HTTP 400 en vez de
            # codigo:0 con Table vacía (contrario a su propio spec, que
            # documenta Table como lista — puede venir vacía). Con una
            # referencia real (ej. PAPELSP01) el mismo filtro sí da 200.
            # Tratamos el 400 como "no encontrado", no como error real —
            # cualquier otro fallo (timeout, 5xx, red) sigue propagándose.
            if '400 client error' in str(e).lower():
                logger.info(
                    '[CONNEKTA] buscar_item_por_referencia(%s): Siesa 400 '
                    '(sin match, tratado como no encontrado)', ref
                )
                return None
            raise
        tabla = resultado.get('detalle', {}).get('Table', [])
        if not tabla:
            return None
        row = tabla[0]
        codigo_siesa = (row.get('f120_referencia') or '').strip()
        if not codigo_siesa:
            return None
        return {
            'codigo_siesa': codigo_siesa,
            'nombre': (row.get('f120_descripcion') or '').strip(),
            'tipo_inventario': (row.get('f120_id_tipo_inv_serv') or '').strip() or None,
        }

    def get_items_unidades_medida(self, pagina: int = 1):
        """API_v2_ItemsUnidadesMedida — factores de conversión de empaques por ítem.
        Campos esperados: f120_referencia, f120_id_unidad_medida, factor (o f120_factor),
        f121_id (código de barras del empaque). Verificar nombres exactos en Paso 0.
        """
        core = self._core
        return core._get(core.api_unidades_medida, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def get_clasificacion_items(self, pagina: int = 1):
        """
        238920 — CLASIFICACION DE ITEMS (conector dinámico)
        Devuelve la clasificación ABC por ítem directamente desde Siesa.
        Reemplaza la carga manual de CSV del reporte 'Recalculo de rotación ABC'.
        Los campos exactos se descubren con /api/siesa/debug-clasificacion-raw.
        """
        core = self._core
        return core._get(core.api_clasificacion, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def get_monitor_facturas_raw(self, fecha: str = None, pagina: int = 1):
        """
        Consulta dinámica papeleriamedellin_monitos_facturas_wms.
        Usa el endpoint /api/connekta/v3/ejecutarconsulta (distinto del estándar).
        fecha: AAAAMMDD — si None usa hoy. Devuelve JSON crudo para descubrir campos.
        """
        core = self._core
        if not fecha:
            fecha = core._fecha_hoy_bogota()

        return core._get(
            'papeleriamedellin_monitos_facturas_wms',
            params_extra={'paginacion': f'numPag={pagina}|tamPag=100'},
            url=core.url_get_dinamico,
        )

    def get_compromisos_pedido(self, tipo_docto: str, consec_docto, f430_rowid=None) -> list:
        """
        GET API_v2_Ventas_Pedidos_Compromisos
        Retorna líneas comprometidas pendientes de remisionar (f405_cant_por_remisionar_base > 0).
        Filtra por f430_rowid cuando está disponible — único campo T430 en el response de la API.
        Campos clave: f120_referencia (SKU), f431_rowid (rowid T431), f405_cant_por_remisionar_base.
        """
        from app.services.connekta_gateway import CompromisosNoDisponibles, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return []
        if not tipo_docto or not str(tipo_docto).strip():
            return []
        try:
            if f430_rowid:
                parametros = f"f430_rowid = {int(f430_rowid)}"
            else:
                consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
                parametros = (
                    f"f430_id_co = {_lit(core.centro_op)} "
                    f"AND f430_id_tipo_docto = {_lit(tipo_docto)} "
                    f"AND f430_consec_docto = {consec_int}"
                )
            res = core._get('API_v2_Ventas_Pedidos_Compromisos', {
                'paginacion': 'numPag=1|tamPag=100',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            # El `[]` de acá significa **«no queda nada por remisionar»**, y
            # alimenta el despacho parcial: la misma forma que ya costó una vez
            # con `CompromisosNoDisponibles`.
            rows = _exigir_datos(rows, 'get_compromisos_pedido', parametros)
            return [r for r in rows if float(r.get('f405_cant_por_remisionar_base') or 0) > 0]
        except Exception as e:
            # NO se devuelve `[]`: el llamador lo leería como «el pedido ya se
            # procesó» y marcaría la tarea DESPACHADO sin remisión ni factura.
            logger.error('[CONNEKTA] get_compromisos_pedido falló: %s', e)
            raise CompromisosNoDisponibles(
                f'No se pudo consultar los compromisos de {tipo_docto}-{consec_docto}: {e}. '
                f'No se marca el despacho: una lista vacía por fallo de red es '
                f'indistinguible de un pedido ya procesado.'
            ) from e

    def get_remision_desde_pedido(self, tipo_docto_pedido: str, consec_docto_pedido) -> dict | None:
        """
        Consulta dinámica papeleriamedellin_WMS_Remision_DesdePedido.
        Busca en BD Siesa la RM más reciente creada para el pedido dado.
        Fallback cuando 142945 no devuelve el consecutivo en su response.
        Retorna {'tipo': 'RM', 'consec': 1234} o None si no existe.

        La query devuelve el MAX(consec_rm) por pedido de los últimos 30 días.
        Filtramos client-side por consec_pd para aislar el pedido específico.
        """
        core = self._core
        if core.modo_simulacion:
            return None
        if not consec_docto_pedido:
            return None
        try:
            consec_int = int(str(consec_docto_pedido).strip())
            res = core._get(
                'papeleriamedellin_WMS_Remision_DesdePedido',
                params_extra={'paginacion': 'numPag=1|tamPag=200'},
                url=core.url_get_dinamico,
            )
            rows = res.get('detalle', {}).get('Datos', [])
            matches = [r for r in rows if r.get('consec_pd') == consec_int]
            if not matches:
                return None
            fila = max(matches, key=lambda r: int(r.get('consec_rm', 0)))
            return {
                'tipo':   str(fila.get('tipo_rm', 'RM')).strip(),
                'consec': int(fila['consec_rm']),
            }
        except Exception as e:
            logger.warning('[CONNEKTA] get_remision_desde_pedido falló: %s', e)
            return None

    def get_pedido_cabecera(self, tipo_docto: str, consec_docto) -> dict | None:
        """
        GET API_v2_Ventas_Pedidos — fila única de cabecera del pedido.
        Devuelve una fila por línea de ítem; se toma rows[0] para extraer campos de cabecera.

        IMPORTANTE — aliases reales vs spec oficial (2026-05-08):
        El procedimiento almacenado usa aliases que difieren del spec v2 (API_v2_Ventas_Pedidos.docx).
        Los nombres abajo son los que devuelve la API real, NO los del spec.
        Ejemplo: spec dice 'f200_id_fact', real devuelve 'f200_id_pedido_fact'.

          f200_id_pedido_fact         → NIT/código tercero cliente (F350_ID_TERCERO en 142943)
          f461_id_sucursal_pedido_rem → sucursal (alias del JOIN a t461/t202)
          f430_id_tipo_cli_fact       → tipo cliente facturación
          f430_id_cond_pago           → condición de pago (ej. 'C01', '30D')
          f430_id_moneda_docto        → moneda del documento
          f430_id_moneda_conv         → moneda conversión
          f430_id_moneda_local        → moneda local
          f430_tasa_conv              → tasa conversión
          f430_tasa_local             → tasa local
          f200_id_pedido_vend         → NIT del vendedor

        f461_id_punto_envio NO existe en esta API — confirmado contra spec oficial y
        respuesta real (120 keys, 2026-05-08). El trigger usa SIESA_PUNTO_ENVIO_DEFAULT
        como valor permanente. Ver trigger_factura_desde_remision().

        Usado exclusivamente por DespachoParialService → trigger_factura_desde_remision.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return None
        if not tipo_docto or not str(tipo_docto).strip():
            return None
        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = core._get(core.api_pedidos, {
                'paginacion': 'numPag=1|tamPag=5',
                'parametros': (
                    f"f430_id_co = {_lit(core.centro_op)} "
                    f"AND f430_id_tipo_docto = {_lit(tipo_docto)} "
                    f"AND f430_consec_docto = {consec_int} "
                    f"AND f430_ind_estado <> 9"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return rows[0] if rows else None
        except Exception as e:
            logger.error('[CONNEKTA] get_pedido_cabecera(%s%s) falló: %s', tipo_docto, consec_docto, e)
            return None

    def get_compromisos_t405(self) -> dict:
        """
        NOTA: Código no invocado en producción — get_pedido_rowid_map usa
        get_compromisos_pedido en su lugar. Conservado por si se necesita
        para reportes bulk. Paginación secuencial hasta 50 páginas.

        papeleriamedellin_compromisos_wms — mapeo {f431_rowid: f405_rowid}.

        T405 no expone claves naturales (co, consec, referencia) directamente.
        Usa f405_rowid_pv_movto (FK a t431_cm_pv_movto) como puente.
        Se combina con get_compromisos_pedido en get_pedido_rowid_map para
        producir {referencia: f405_rowid} por pedido.
        Pagina hasta agotar resultados — sin TOP arbitrario.
        """
        core = self._core
        if core.modo_simulacion:
            return {}
        try:
            result = {}
            for pag in range(1, 50):
                res = core._get(
                    'papeleriamedellin_compromisos_wms',
                    params_extra={'paginacion': f'numPag={pag}|tamPag=100'},
                    url=core.url_get_dinamico,
                )
                rows = res.get('detalle', {}).get('Datos') or []
                if not rows:
                    break
                for r in rows:
                    if r.get('rowid_linea_pedido') and r.get('rowid_compromiso'):
                        result[int(r['rowid_linea_pedido'])] = int(r['rowid_compromiso'])
                if len(rows) < 100:
                    break
            logger.info('[CONNEKTA] get_compromisos_t405: %d compromisos activos', len(result))
            return result
        except Exception as e:
            logger.error('[CONNEKTA] get_compromisos_t405 falló: %s', e)
            raise

    def get_pedido_rowid_map(self, tipo_docto: str, consec_docto, f430_rowid=None) -> dict:
        """
        Devuelve {referencia: f431_rowid} para las líneas del pedido.
        f431_rowid es el ID único de la línea en T431 — valor que 142945 exige en
        f470_rowid_movto para que Siesa respete f470_cant_base (cantidad parcial del picking).
        """
        from app.services.connekta_gateway import CompromisosNoDisponibles

        core = self._core
        if core.modo_simulacion:
            return {}
        if not tipo_docto or not str(tipo_docto).strip():
            return {}
        try:
            # Mismo dominio, mismo objeto — llamada intra-clase, no core.X.
            compromisos = self.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid)
            result = {
                str(r.get('f120_referencia', '')).strip(): int(r['f431_rowid'])
                for r in compromisos
                if r.get('f431_rowid') and r.get('f120_referencia')
            }
            logger.info('[CONNEKTA] get_pedido_rowid_map %s%s: %s', tipo_docto, consec_docto, result)
            return result
        except CompromisosNoDisponibles:
            # Se deja pasar: taparla acá devolvería un mapa vacío, que es la
            # misma mentira un nivel más arriba.
            raise
        except Exception as e:
            logger.warning('[CONNEKTA] get_pedido_rowid_map falló: %s', e)
            return {}


__all__ = ['ConnektaConsultasGateway']

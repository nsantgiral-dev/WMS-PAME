"""
DespachoParialService — despacho parcial vía 244328 → 142945 → 142943.

Flujo confirmado (2026-05-27):
  1. GET API_v2_Ventas_Pedidos_Compromisos  → rowid_map + cant. originales + metadata
  2. POST 244328 (v3.1)  → ajusta f405_cant_por_remisionar_base en T405 (cant. WMS real)
  3. POST 142945 (v3.1)  → crea RemisionPedido (RM) — descarga inventario cuenta 14
  4. Guardar rm_tipo/rm_consec en BD antes de 142943 (idempotencia ante fallo entre pasos)
  5. POST 142943 (v3)    → FacturaDesdeRemision (FE)
  6. _persistir_resultado() → marca tarea DESPACHADO + siesa_triggered=True

Idempotencia:
  - compromisos vacíos en Siesa = Siesa ya procesó el pedido → persistir directo
  - siesa_triggered=True en BD  = tarea ya completada → raise (guard al inicio)
  - rm_tipo/rm_consec en BD     = 142945 ya corrió → DLQ llama facturar_remision_existente

Nota: 142945 y 142943 se llaman por la URL dinámica v3.1 (misma autorización que 244328)
para evitar el HTTP 401 que tenía la URL estándar v3 con las credenciales actuales.
"""
import re
import json
import logging
from datetime import datetime

from app.extensions import db

logger = logging.getLogger(__name__)

# Confirmado por consultor: "Transacción Exitosa. Se generó el documento RM-XXXX"
_RE_RM = re.compile(r'([A-Z]{1,6})-(\d+)', re.IGNORECASE)


class DespachoParialService:

    @staticmethod
    def obtener_compromisos(tipo_docto: str, consec_docto: str) -> list:
        """Devuelve las líneas de compromiso del pedido desde Siesa."""
        from app.services.connekta_gateway import connekta
        return connekta.get_compromisos_pedido(tipo_docto, consec_docto)

    @staticmethod
    def despachar_parcial(tarea, cantidades: dict) -> dict:
        """
        Ejecuta el despacho parcial completo vía conector 244328:
          1. Lee cabecera del pedido en Siesa
          2. Obtiene compromisos vigentes del pedido
          3. Actualiza cantidades reales en T405 con 244328 (Compromiso_PididosV1)
          4. Marca tarea DESPACHADO + siesa_triggered=True
          Siesa automation crea RM + FE automáticamente tras la actualización de T405.

        cantidades: {producto_codigo: float}
        """
        from app.services.connekta_gateway import connekta

        if tarea.siesa_triggered:
            raise ValueError(f'Tarea {tarea.id} ya tiene siesa_triggered=True')

        # Idempotencia: si 142945 ya corrió en un intento anterior y la RM quedó en BD,
        # saltamos 244328/142945 y vamos directo a 142943 — evita crear RM duplicada en Siesa.
        if tarea.rm_tipo and tarea.rm_consec:
            logger.info(
                '[DESPACHO_PARCIAL] RM ya en BD (%s-%s) — saltando 244328/142945, directo a 142943 — tarea=%s',
                tarea.rm_tipo, tarea.rm_consec, tarea.id,
            )
            return DespachoParialService.facturar_remision_existente(tarea)

        tipo_docto   = tarea.tipo_docto_pedido_siesa
        consec_docto = tarea.consec_docto_pedido_siesa
        if not tipo_docto or not consec_docto:
            raise ValueError('Tarea sin tipo_docto/consec_docto — imposible despachar')

        # 1. Cabecera del pedido (necesaria para f430_rowid del GET compromisos)
        cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
        if not cabecera:
            raise ValueError(f'Pedido {tarea.numero_pedido_siesa} no encontrado en Siesa')

        # 2. Compromisos Siesa — fuente única para rowid_map Y payload 244328.
        # f431_rowid → f431_nro_registro en 244328 (campo obligatorio del conector).
        f430_rowid = cabecera.get('f430_rowid')
        compromisos_siesa = connekta.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid)

        # Idempotencia: compromisos vacíos = automation Siesa ya procesó el pedido completo.
        # Se marca DESPACHADO directamente sin volver a llamar 244328.
        if not compromisos_siesa:
            logger.info(
                '[DESPACHO_PARCIAL] compromisos vacíos — automation Siesa ya procesó tarea=%s pedido=%s',
                tarea.id, tarea.numero_pedido_siesa,
            )
            return DespachoParialService._persistir_resultado(
                tarea, '244328-AUTO', {'automatizacion_siesa': True, 'compromisos_vacios': True}
            )

        # Mapa UOM preferida por producto — clave para desambiguar productos dual-unit
        # (PQ + UND) que aparecen con dos líneas en los compromisos de Siesa.
        # Para PAPELSP6741: unidad_empaque='PQ' → preferir la línea PQ (committed=3)
        # sobre la línea UND (committed=1); sin este mapa el dict-comprehension clásico
        # guardaba la ÚLTIMA línea (UND) causando el error 244328 "cant 2 > 1 comprometida".
        _uom_pref = {}
        for _ti in tarea.items:
            if _ti.producto:
                _ref = (_ti.producto.codigo_siesa or _ti.producto.codigo or '').strip()
                _uom = (_ti.producto.unidad_empaque or _ti.producto.unidad_medida or 'UND').upper()
                if _ref:
                    _uom_pref[_ref] = _uom

        # Construir rowid_map con desambiguación dual-unit:
        # 1ª prioridad: f405_id_unidad_medida coincide con unidad_empaque del producto (WMS)
        # 2ª prioridad: mayor f405_cant_por_remisionar_base (línea principal > línea auxiliar)
        rowid_map = {}
        _rowid_score = {}   # {ref: (uom_match:0|1, cant)}
        for _r in compromisos_siesa:
            _ref = str(_r.get('f120_referencia', '')).strip()
            _rid = _r.get('f431_rowid')
            if not _ref or not _rid:
                continue
            _cant   = float(_r.get('f405_cant_por_remisionar_base') or 0)
            _uom_c  = str(_r.get('f405_id_unidad_medida', '')).strip().upper()
            _uom_w  = _uom_pref.get(_ref, '')
            _match  = 1 if (_uom_w and _uom_c == _uom_w) else 0
            _prev   = _rowid_score.get(_ref, (-1, -1))
            if (_match, _cant) > _prev:
                rowid_map[_ref]    = int(_rid)
                _rowid_score[_ref] = (_match, _cant)

        logger.info('[DESPACHO_PARCIAL] rowid_map tarea=%s: %s', tarea.id, rowid_map)

        # Líneas confirmadas agotadas — deben zerarse explícitamente en Siesa (244328
        # con cant_por_remisionar=0). Sin esto, T405 conserva la cantidad original del
        # pedido y esa línea reaparece completa en la RM/FE aunque nunca se haya picado
        # ni pasado por packing. Distinto de una línea simplemente aún no despachada
        # (despacho parcial legítimo, sigue PENDIENTE/EN_PROCESO), que sí debe seguir
        # intacta para una oleada futura.
        #
        # Dos caminos consideran una referencia agotada:
        #  1. Auditada: picking BLOQUEADO → admin audita NO_ENCONTRADO (estado=CANCELADO).
        #  2. Confirmada en 0 directo: el operario recoge 0 y confirma sin pasar por
        #     "reportar problema" (estado=COMPLETADO, cantidad_recogida=0). Sin este
        #     camino, PD1325/PAPELSP9830 (caso real 2026-07-22) se hubiera facturado
        #     con la cantidad original del pedido pese a recogida=0 confirmada.
        from app.models.picking import TareaPicking
        _referencias_agotadas = {
            _tp.producto.codigo_siesa
            for _tp in TareaPicking.query.filter(
                TareaPicking.referencia_documento == tarea.numero_pedido_siesa,
                db.or_(
                    db.and_(
                        TareaPicking.estado == 'CANCELADO',
                        TareaPicking.auditoria_resultado == 'NO_ENCONTRADO',
                    ),
                    db.and_(
                        TareaPicking.estado == 'COMPLETADO',
                        TareaPicking.cantidad_recogida == 0,
                    ),
                ),
            ).all()
            if _tp.producto and _tp.producto.codigo_siesa
        }
        if _referencias_agotadas:
            logger.info(
                '[DESPACHO_PARCIAL] referencias agotadas (auditoría) tarea=%s: %s',
                tarea.id, _referencias_agotadas,
            )

        # 3. Actualizar cantidades reales en Siesa con conector 244328.
        # f120_id viene en el response de API_v2_Ventas_Pedidos_Compromisos (JOIN T431→T120).
        # Tras esta actualización, automation Siesa crea RM + FE automáticamente.
        _compromisos_payload = DespachoParialService._build_compromisos_244328(
            cantidades, rowid_map, compromisos_siesa, uom_map=_uom_pref,
            referencias_agotadas=_referencias_agotadas,
        )
        if _compromisos_payload:
            connekta.trigger_comprometer_pedido(consec_docto, _compromisos_payload)
            logger.info(
                '[DESPACHO_PARCIAL] 244328 OK — T405 actualizado tarea=%s líneas=%d',
                tarea.id, len(_compromisos_payload),
            )
        else:
            logger.warning(
                '[DESPACHO_PARCIAL] 244328 omitido — sin compromisos válidos tarea=%s '
                '(sin rowid o cantidad=0); continuando con 142945',
                tarea.id,
            )

        # 4. Construir items para 142945 desde cantidades reales (WMS) + metadata compromisos.
        # Misma lógica de desambiguación dual-unit que _build_compromisos_244328:
        # prioridad 1 → UOM match con unidad_empaque del producto; prioridad 2 → mayor cant.
        _comp_por_ref_rm = {}
        _comp_score_rm   = {}
        for _r in compromisos_siesa:
            _ref = str(_r.get('f120_referencia', '')).strip()
            if not _ref:
                continue
            _cant  = float(_r.get('f405_cant_por_remisionar_base') or 0)
            _uom_c = str(_r.get('f405_id_unidad_medida', '')).strip().upper()
            _uom_w = _uom_pref.get(_ref, '')
            _match = 1 if (_uom_w and _uom_c == _uom_w) else 0
            _prev  = _comp_score_rm.get(_ref, (-1, -1))
            if (_match, _cant) > _prev:
                _comp_por_ref_rm[_ref] = _r
                _comp_score_rm[_ref]   = (_match, _cant)

        _items_rm = []
        for _ref, _cant_real in cantidades.items():
            _cant_real = float(_cant_real or 0)
            if _cant_real <= 0:
                continue
            _cr = _comp_por_ref_rm.get(_ref, {})
            _items_rm.append({
                'producto_codigo': _ref,
                'cantidad_empacada': _cant_real,
                'lote':             _cr.get('f405_id_lote') or None,
                'unidad_medida':    str(_cr.get('f405_id_unidad_medida') or '').strip().upper() or 'UND',
                'item_id_siesa':    _cr.get('f120_id') or None,
                'rowid_movto':      rowid_map.get(_ref) or None,
            })

        if not _items_rm:
            raise ValueError(
                f'[DESPACHO_PARCIAL] Sin ítems con cantidad>0 para 142945 — '
                f'tarea={tarea.id} cantidades={cantidades}'
            )

        # 5. POST 142945 → RemisionPedido vía URL estándar v3 (conectoresimportarestandar).
        # 142945 usa formato sectioned (Inicial/Remision/Movtoventascomercial/Final),
        # incompatible con v3.1 dinámica que solo acepta formato plano.
        # f470_rowid_movto identifica la línea exacta en T431, garantizando que Siesa
        # remisione la cantidad parcial del WMS en vez del total del pedido.
        logger.info(
            '[DESPACHO_PARCIAL] 142945 enviando RM — tarea=%s pedido=%s ítems=%d',
            tarea.id, tarea.numero_pedido_siesa, len(_items_rm),
        )
        resp_rm = connekta.trigger_despacho(
            tipo_docto, consec_docto, _items_rm,
            # Sin url/extra_params → usa url_post (v3/conectoresimportarestandar)
            # 142945 formato sectioned requiere v3; v3.1 rechaza con "Error en la Estructura"
        )

        # Modo ensayo: el POST fue bloqueado en el gateway, no hay RM real en Siesa.
        # Cortar aquí — no intentar parsear ni consultar Siesa (no hay nada que encontrar)
        # y no avanzar a 142943, que también quedaría bloqueado.
        if resp_rm.get('modo_ensayo'):
            logger.info(
                '[DESPACHO_PARCIAL] 142945 en modo ensayo — POST bloqueado, sin RM real. '
                'tarea=%s pedido=%s', tarea.id, tarea.numero_pedido_siesa,
            )
            return DespachoParialService._persistir_resultado(
                tarea, 'ENSAYO', resp_rm
            )

        # La URL estándar v3 solo devuelve {'codigo':0,'mensaje':'Transacción Exitosa'} sin consecutivo.
        # Estrategia 1: extraer RM del response (funciona si Connekta configura respuesta enriquecida).
        # Estrategia 2: fallback GET papeleriamedellin_WMS_Remision_DesdePedido — busca la RM recién creada.
        try:
            tipo_rm, consec_rm = DespachoParialService._parsear_rm(resp_rm)
        except ValueError:
            logger.info(
                '[DESPACHO_PARCIAL] RM no en response de 142945 — '
                'consultando Siesa vía get_remision_desde_pedido tarea=%s pedido=%s',
                tarea.id, tarea.numero_pedido_siesa,
            )
            rm_siesa = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
            if not rm_siesa:
                raise ValueError(
                    f'142945 OK pero RM no encontrada en response ni en Siesa — '
                    f'tarea={tarea.id} pedido={tarea.numero_pedido_siesa}. '
                    f'Verificar que papeleriamedellin_WMS_Remision_DesdePedido esté '
                    f'configurado en Connekta. Response 142945: {str(resp_rm)[:300]}'
                )
            tipo_rm   = rm_siesa['tipo']
            consec_rm = rm_siesa['consec']
            logger.info(
                '[DESPACHO_PARCIAL] RM recuperada vía Siesa query: %s-%s tarea=%s pedido=%s',
                tipo_rm, consec_rm, tarea.id, tarea.numero_pedido_siesa,
            )

        # Guardar RM en BD ANTES de llamar 142943.
        # Si 142943 falla y la DLQ reintenta, facturar_remision_existente detecta
        # rm_tipo/rm_consec y no vuelve a llamar 142945 — evita RM duplicada en Siesa.
        # P_EMERGENCY_COMMIT: si el commit falla, Siesa ya creó la RM — sin rm_tipo/rm_consec
        # en BD el retry llamaría 142945 de nuevo creando una RM DUPLICADA.
        tarea.rm_tipo   = tipo_rm
        tarea.rm_consec = consec_rm
        try:
            db.session.commit()
        except Exception as _e_rm:
            db.session.rollback()
            logger.critical(
                '[DESPACHO_PARCIAL] Fallo commit rm_tipo/rm_consec — emergency mini-commit '
                'tarea=%s RM=%s-%s: %s', tarea.id, tipo_rm, consec_rm, _e_rm,
            )
            try:
                tarea = db.session.merge(tarea)
                tarea.rm_tipo   = tipo_rm
                tarea.rm_consec = consec_rm
                db.session.commit()
            except Exception as _e_rm2:
                db.session.rollback()
                logger.critical(
                    '[DESPACHO_PARCIAL] DOBLE FALLO — rm_tipo/rm_consec NO persistidos '
                    'tarea=%s RM=%s-%s: %s. RIESGO DE RM DUPLICADA en retry.',
                    tarea.id, tipo_rm, consec_rm, _e_rm2,
                )
                raise
        logger.info(
            '[DESPACHO_PARCIAL] 142945 OK — RM=%s-%s tarea=%s pedido=%s',
            tipo_rm, consec_rm, tarea.id, tarea.numero_pedido_siesa,
        )

        # 6. POST 142943 → FacturaDesdeRemision
        resp_fe = connekta.trigger_factura_desde_remision(tipo_rm, consec_rm, cabecera)
        logger.info(
            '[DESPACHO_PARCIAL] 142943 OK — FE generada tarea=%s pedido=%s',
            tarea.id, tarea.numero_pedido_siesa,
        )

        # 7. Persistir resultado final con referencia a la RM creada
        return DespachoParialService._persistir_resultado(
            tarea, f'{tipo_rm}-{consec_rm}', resp_fe
        )

    @staticmethod
    def facturar_remision_existente(tarea) -> dict:
        """
        Carril de recuperación: detecta la RM que 142945 ya creó y dispara 142943.
        Prioriza el consecutivo guardado en BD sobre la query a Siesa.
        """
        from app.services.connekta_gateway import connekta

        tipo_docto   = tarea.tipo_docto_pedido_siesa
        consec_docto = tarea.consec_docto_pedido_siesa
        if not tipo_docto or not consec_docto:
            raise ValueError('Tarea sin tipo_docto/consec_docto — imposible facturar')

        if tarea.siesa_triggered:
            facturas_check = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
            if facturas_check:
                raise ValueError(f'Tarea {tarea.id} ya procesada — FE confirmada en Siesa')
            logger.warning(
                '[FACTURAR_RM] siesa_triggered=True pero FE ausente en Siesa — '
                'recuperación permitida (tarea=%s)', tarea.id
            )

        # 1. Detectar RM: BD primero, luego API Siesa (puede fallar si no existe en Connekta)
        if tarea.rm_tipo and tarea.rm_consec:
            tipo_rm   = tarea.rm_tipo
            consec_rm = tarea.rm_consec
            logger.info(
                '[FACTURAR_RM] RM en BD: %s-%s — tarea=%s',
                tipo_rm, consec_rm, tarea.id
            )
        else:
            raise ValueError(
                f'No hay consecutivo de RM en BD para {tarea.numero_pedido_siesa}. '
                'Usar POST /facturar-rm-manual con el número de RM visible en Siesa.'
            )

        # 2. Anti-duplicado FE
        facturas_existentes = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
        if facturas_existentes:
            logger.info('[FACTURAR_RM] tarea=%s FE ya existe — marcando done', tarea.id)
            return DespachoParialService._persistir_resultado(
                tarea, f'{tipo_rm}-{consec_rm}',
                {'idempotente': True, 'facturas': facturas_existentes}
            )

        # 3. Cabecera para 142943
        cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
        if not cabecera:
            raise ValueError(
                f'Pedido {tarea.numero_pedido_siesa} no encontrado en Siesa — '
                'no se puede construir la factura sin datos del tercero.'
            )

        # 4. POST 142943
        resp_fe = connekta.trigger_factura_desde_remision(tipo_rm, consec_rm, cabecera)
        return DespachoParialService._persistir_resultado(
            tarea, f'{tipo_rm}-{consec_rm}', resp_fe
        )

    @staticmethod
    def facturar_rm_con_consec(tarea, tipo_rm: str, consec_rm: int) -> dict:
        """
        Carril de emergencia: convierte una RM conocida (tipo+consec) a FE (142943).
        Usar cuando el consecutivo no está en BD y no hay API de remisiones disponible.
        El tipo_rm y consec_rm los ingresa el operario de gestión desde Siesa.
        """
        from app.services.connekta_gateway import connekta

        tipo_docto   = tarea.tipo_docto_pedido_siesa
        consec_docto = tarea.consec_docto_pedido_siesa

        # Anti-duplicado FE
        facturas = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
        if facturas:
            return DespachoParialService._persistir_resultado(
                tarea, f'{tipo_rm}-{consec_rm}',
                {'idempotente': True, 'facturas': facturas}
            )

        cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
        if not cabecera:
            raise ValueError(f'Pedido {tarea.numero_pedido_siesa} no encontrado en Siesa')

        # Guardar consec en BD ANTES de llamar 142943 — idempotencia futura
        tarea.rm_tipo   = tipo_rm
        tarea.rm_consec = consec_rm
        db.session.commit()

        resp_fe = connekta.trigger_factura_desde_remision(tipo_rm, consec_rm, cabecera)
        return DespachoParialService._persistir_resultado(
            tarea, f'{tipo_rm}-{consec_rm}', resp_fe
        )

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _persistir_resultado(tarea, rm_str: str, fe_response: dict) -> dict:
        """Persiste el estado final de la tarea tras despacho exitoso."""
        from app.models.packing import EstadoPacking
        resultado = {'rm': rm_str, 'fe_response': fe_response}
        _es_ensayo = bool(fe_response.get('modo_ensayo'))
        if not _es_ensayo:
            tarea.siesa_triggered    = True
            tarea.siesa_triggered_at = datetime.utcnow()
            tarea.estado             = EstadoPacking.DESPACHADO
            tarea.fecha_despachado   = tarea.fecha_despachado or datetime.utcnow()
        tarea.siesa_response     = json.dumps(resultado)
        try:
            db.session.commit()
        except Exception as _e_persist:
            db.session.rollback()
            logger.critical(
                '[DESPACHO_PARCIAL] Fallo commit _persistir_resultado — emergency mini-commit '
                'siesa_triggered tarea=%s: %s', tarea.id, _e_persist,
            )
            # P_EMERGENCY_COMMIT: al menos persistir siesa_triggered para bloquear retry duplicado
            # (no aplica en modo ensayo — no hubo POST real que proteger de reintento)
            if not _es_ensayo:
                try:
                    tarea = db.session.merge(tarea)
                    tarea.siesa_triggered    = True
                    tarea.siesa_triggered_at = datetime.utcnow()
                    db.session.commit()
                except Exception as _e_persist2:
                    db.session.rollback()
                    logger.critical(
                        '[DESPACHO_PARCIAL] DOBLE FALLO — siesa_triggered NO persistido '
                        'tarea=%s: %s. RIESGO DE FE DUPLICADA en retry.',
                        tarea.id, _e_persist2,
                    )
                    raise
        logger.info('[DESPACHO_PARCIAL] tarea=%s → %s FE=ok (ensayo=%s)', tarea.id, rm_str, _es_ensayo)
        return resultado

    @staticmethod
    def _build_items(tarea, cantidades: dict, rowid_map: dict = None) -> list:
        _rowid = rowid_map or {}
        items = []
        for item in tarea.items:
            if not item.producto:
                continue
            codigo_wms   = item.producto.codigo
            codigo_siesa = item.producto.codigo_siesa or codigo_wms
            qty = float(cantidades.get(codigo_siesa, 0) or cantidades.get(codigo_wms, 0))
            if qty <= 0:
                continue
            items.append({
                'producto_codigo': codigo_siesa,
                'cantidad_empacada': qty,
                'lote': item.lote or None,
                'unidad_medida': (item.producto.unidad_empaque or item.producto.unidad_medida or 'UND'),
                'item_id_siesa': None,
                'rowid_movto': _rowid.get(codigo_siesa) or _rowid.get(codigo_wms),
            })
        return items

    @staticmethod
    def _build_items_from_compromisos(compromisos: list) -> list:
        """
        Construye items de despacho desde API_v2_Ventas_Pedidos_Compromisos.
        Usa f405_cant_por_remisionar_base como cantidad autoritativa de Siesa.
        Solo se invoca cuando _build_items() devuelve vacío (WMS sin cantidades).
        """
        items = []
        for row in compromisos:
            codigo = str(row.get('f120_referencia', '')).strip()
            qty = float(row.get('f405_cant_por_remisionar_base') or 0)
            if not codigo or qty <= 0:
                continue
            items.append({
                'producto_codigo': codigo,
                'cantidad_empacada': qty,
                'lote': row.get('f405_id_lote') or None,
                'unidad_medida': str(row.get('f405_id_unidad_medida', 'UND')).strip() or 'UND',
                'item_id_siesa': row.get('f431_rowid'),
            })
        return items

    @staticmethod
    def _build_compromisos_244328(cantidades: dict, rowid_map: dict,
                                   compromisos_siesa: list,
                                   uom_map: dict = None,
                                   referencias_agotadas: set = None) -> list:
        """
        Construye el payload de Compromisos para el conector 244328.

        Mapeo de campos (confirmado Postman QA 2026-05-26):
          f431_id_item             = f120_id del response de API_v2_Ventas_Pedidos_Compromisos
                                     (campo nativo JOIN T431→T120 — confirmado en Postman 2026-05-26).
                                     El conector NO resuelve por f431_referencia_item (texto SKU).
          f431_nro_registro        = f431_rowid  (rowid de T431, no es 1,2,3)
          f431_cant_base           = f405_cant_por_remisionar_base original en Siesa
          f405_cant_por_remisionar_base = cant. REAL picada (del WMS)

        Solo incluye ítems donde:
          · el admin envió cantidad > 0 en el body, O la línea está en
            referencias_agotadas (auditoría confirmó agotado → se envía en 0 explícito
            para que Siesa no la remisione con la cantidad original del pedido)
          · existe f431_rowid en rowid_map (línea identificada en T431)
          · f120_id está presente en compromisos_siesa (garantiza lookup en 244328)

        uom_map: {codigo_siesa: unidad_empaque} — para desambiguar productos dual-unit
          que aparecen con 2 líneas en compromisos (PQ y UND). Se prefiere la línea
          cuya f405_id_unidad_medida coincida; si no hay match, la de mayor cantidad.

        referencias_agotadas: {codigo_siesa} confirmados NO_ENCONTRADO en auditoría —
          ver despachar_parcial() para el porqué se distingue de "aún no despachado".
        """
        _agotadas = referencias_agotadas or set()
        # Mapa {referencia: fila_completa} — f120_id y f405_cant_por_remisionar_base
        # vienen directamente del response de API_v2_Ventas_Pedidos_Compromisos.
        # Misma lógica de desambiguación dual-unit que rowid_map en despachar_parcial():
        # 1ª prioridad: UOM match; 2ª prioridad: mayor f405_cant_por_remisionar_base.
        _uom = uom_map or {}
        _comp_por_ref  = {}
        _comp_score    = {}   # {ref: (uom_match, cant)}
        for r in compromisos_siesa:
            ref = str(r.get('f120_referencia', '')).strip()
            if not ref:
                continue
            _cant  = float(r.get('f405_cant_por_remisionar_base') or 0)
            _uom_c = str(r.get('f405_id_unidad_medida', '')).strip().upper()
            _uom_w = _uom.get(ref, '')
            _match = 1 if (_uom_w and _uom_c == _uom_w) else 0
            _prev  = _comp_score.get(ref, (-1, -1))
            if (_match, _cant) > _prev:
                _comp_por_ref[ref] = r
                _comp_score[ref]   = (_match, _cant)

        result = []
        for ref, rowid in rowid_map.items():
            cant_real = float(cantidades.get(ref, 0))
            if not rowid:
                continue
            if cant_real <= 0 and ref not in _agotadas:
                continue  # aún no despachado — sigue comprometido para oleada futura
            comp_row  = _comp_por_ref.get(ref, {})
            cant_orig = float(comp_row.get('f405_cant_por_remisionar_base') or cant_real)
            id_item   = comp_row.get('f120_id')          # ID numérico de T120 — fuente primaria

            if not id_item:
                logger.warning(
                    '[DESPACHO_PARCIAL] _build_compromisos_244328: f120_id ausente para ref=%s '
                    '— 244328 fallará (conector no acepta referencia texto)',
                    ref,
                )

            # UOM real de la línea de compromiso Siesa (f405_id_unidad_medida).
            # Siesa devuelve el campo con espacio trailing (ej. "UND ") → strip().upper().
            # Se propaga como f431_id_unidad_medida en 244328 — campo OBLIGATORIO (Si).
            # Para productos dual-unit (PQ + UND) la desambiguación previa ya garantiza
            # que comp_row es la fila correcta, por lo que _uom_linea será la UOM real del movimiento.
            _uom_linea = str(comp_row.get('f405_id_unidad_medida') or '').strip().upper() or None

            result.append({
                'referencia_item':     ref,
                'id_item':             id_item,   # f431_id_item en 244328 — OBLIGATORIO numérico
                'cant_base':           cant_orig,
                'nro_registro':        rowid,      # f431_nro_registro = f431_rowid
                'cant_por_remisionar': cant_real,
                'lote':                comp_row.get('f405_id_lote') or None,
                'uom':                 _uom_linea, # f431_id_unidad_medida — UOM real (spec 244328: Si)
            })
        return result

    @staticmethod
    def _parsear_rm(resp) -> tuple[str, int]:
        """
        Extrae tipo y consecutivo de la RM del response de 142945.
        Estrategia 1: regex "RM-XXXX" en todos los strings (recursivo).
        Estrategia 2: buscar tipo y consec como campos separados en dicts anidados.
        Lanza ValueError con el response completo si no encuentra — facilita diagnóstico.
        """
        if not resp:
            raise ValueError('Response de 142945 vacío')

        # Aplanar recursivamente todos los valores del response
        def _aplanar(obj):
            if isinstance(obj, dict):
                for v in obj.values():
                    yield from _aplanar(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from _aplanar(item)
            elif obj is not None:
                yield obj

        # Estrategia 1: regex "TIPO-CONSEC" en cualquier valor string
        for val in _aplanar(resp):
            match = _RE_RM.search(str(val))
            if match:
                tipo_rm   = match.group(1).upper()
                consec_rm = int(match.group(2))
                logger.info('[DESPACHO_PARCIAL] RM parseado (regex): %s-%s', tipo_rm, consec_rm)
                return tipo_rm, consec_rm

        # Estrategia 2: tipo y consec en campos separados (Connekta v1 structured response)
        _TIPO_KEYS   = {'f350_id_tipo_docto', 'tipodocto', 'tipo_docto', 'tipo', 'tipoDocto', 'TipoDocto'}
        _CONSEC_KEYS = {'f350_consec_docto', 'consecutivo', 'consec_docto', 'consec', 'Consecutivo', 'numero'}

        def _buscar_campos(obj):
            if isinstance(obj, dict):
                tipo_val  = next((str(v).strip().upper() for k, v in obj.items() if k in _TIPO_KEYS and v), None)
                consec_val = next(
                    (int(v) for k, v in obj.items()
                     if k in _CONSEC_KEYS and v is not None and str(v).strip().isdigit() and int(v) > 0),
                    None
                )
                if tipo_val and consec_val:
                    return tipo_val, consec_val
                for v in obj.values():
                    result = _buscar_campos(v)
                    if result:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = _buscar_campos(item)
                    if result:
                        return result
            return None

        result = _buscar_campos(resp)
        if result:
            tipo_rm, consec_rm = result
            logger.info('[DESPACHO_PARCIAL] RM por campos estructurados: %s-%s', tipo_rm, consec_rm)
            return tipo_rm, consec_rm

        raise ValueError(f'RM no encontrada en response de 142945: {str(resp)[:400]}')

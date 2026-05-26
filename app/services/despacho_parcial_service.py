"""
DespachoParialService — despacho parcial vía 142945 → 142943.

Responsabilidad única: orquestar el encadenamiento RemisionPedido → FacturaRemision
para pedidos con compromisos parciales. Completamente separado del flujo de packing;
no importa ni modifica nada de packing.py, cerrar_packing ni TareaPacking directamente
(solo lee la tarea y escribe rm_tipo/rm_consec/siesa_triggered/estado al finalizar).

Idempotencia sin API_v2_Ventas_Remisiones_DesdePedido (no existe en Connekta):
  - rm_tipo + rm_consec en BD: fuente primaria del consecutivo de la RM
  - API_v2_Ventas_Pedidos_Compromisos vacía: señal de que la RM ya fue creada
  - get_factura_desde_pedido: guard anti-duplicado de FE
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
        Ejecuta el despacho parcial completo:
          1. Lee cabecera del pedido en Siesa (tercero, moneda, cond. pago)
          2. Construye items (WMS primary, compromisos Siesa fallback)
          3. Idempotencia via BD (rm_tipo/rm_consec) o compromisos vacíos
          4. POST 142945 → crea RM, guarda consec en BD inmediatamente
          5. POST 142943 → convierte RM a FE
          6. Marca tarea DESPACHADO + siesa_triggered=True

        cantidades: {producto_codigo: float}
        """
        from app.services.connekta_gateway import connekta

        if tarea.siesa_triggered:
            raise ValueError(f'Tarea {tarea.id} ya tiene siesa_triggered=True')

        tipo_docto   = tarea.tipo_docto_pedido_siesa
        consec_docto = tarea.consec_docto_pedido_siesa
        if not tipo_docto or not consec_docto:
            raise ValueError('Tarea sin tipo_docto/consec_docto — imposible despachar')

        # 1. Cabecera del pedido (cliente, moneda, condición de pago)
        cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
        if not cabecera:
            raise ValueError(f'Pedido {tarea.numero_pedido_siesa} no encontrado en Siesa')

        # 2. Compromisos Siesa — fuente única para rowid_map Y payload 244328.
        # Una sola llamada a API_v2_Ventas_Pedidos_Compromisos evita duplicar el GET.
        # f431_rowid → f470_rowid_movto en 142945 y f431_nro_registro en 244328.
        f430_rowid = cabecera.get('f430_rowid')
        compromisos_siesa = connekta.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid)

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
        # Fallback a compromisos de Siesa cuando cantidad_real/esperada son 0 en todos los ítems.
        items = DespachoParialService._build_items(tarea, cantidades, rowid_map)
        if not items:
            logger.warning(
                '[DESPACHO_PARCIAL] tarea=%s — WMS sin cantidades; consultando compromisos Siesa',
                tarea.id
            )
            compromisos = connekta.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid)
            items = DespachoParialService._build_items_from_compromisos(compromisos)
            if not items:
                raise ValueError(
                    f'Pedido {tarea.numero_pedido_siesa}: sin cantidades en WMS ni compromisos '
                    'vigentes en Siesa — verificar estado del pedido.'
                )

        # 3. Idempotencia para 142945 — sin depender de API_v2_Ventas_Remisiones_DesdePedido:
        #   a) Consec guardado en BD: camino rápido y confiable
        #   b) Compromisos vacíos: señal de que la RM ya existe en Siesa
        if tarea.rm_tipo and tarea.rm_consec:
            tipo_rm   = tarea.rm_tipo
            consec_rm = tarea.rm_consec
            logger.info(
                '[DESPACHO_PARCIAL] RM en BD: %s-%s — saltando 142945 (tarea=%s)',
                tipo_rm, consec_rm, tarea.id
            )
        else:
            # Skip compromisos check when WMS has explicit quantities — rm_tipo/rm_consec
            # not in BD already guarantees 142945 was never called successfully for this tarea.
            compromisos_check = (
                [True] if cantidades
                else connekta.get_compromisos_pedido(tipo_docto, consec_docto)
            )
            if not compromisos_check:
                # Compromisos vacíos y WMS sin cantidades: RM ya existe en Siesa sin consecutivo en BD.
                # API_v2_Ventas_Remisiones_DesdePedido no disponible en Connekta — recuperación manual.
                facturas_pre = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
                if facturas_pre:
                    logger.info(
                        '[DESPACHO_PARCIAL] FE ya existe para %s%s — idempotente',
                        tipo_docto, consec_docto
                    )
                    return DespachoParialService._persistir_resultado(
                        tarea, 'PREEXISTENTE', {'idempotente': True, 'facturas': facturas_pre}
                    )
                raise ValueError(
                    f'Pedido {tarea.numero_pedido_siesa}: compromisos vacíos (RM existe en Siesa) '
                    'pero FE no existe y el consecutivo no está en BD. '
                    'Usar POST /facturar-rm-manual con el número de RM visible en Siesa.'
                )
            else:
                # Compromisos vigentes → RM no existe → crear con 142945

                # ── Paso 5: comprometer cantidades reales en Siesa (244328) ──────────
                # Actualiza f405_cant_por_remisionar_base en T405 ANTES de llamar 142945.
                # Sin este paso, 142945 usa la cant. comprometida original (ej. 4)
                # e ignora f470_cant_base (ej. 3) → bug de cantidad resuelta.
                # 244328 reemplaza consulta 7811 + GRANT UPDATE (ya no se requieren).
                # ─────────────────────────────────────────────────────────────────────
                # f120_id viene directamente en el response de API_v2_Ventas_Pedidos_Compromisos
                # (campo nativo del JOIN T431→T120, confirmado Postman QA 2026-05-26).
                # No se necesita PedidoSiesa como fuente — los datos ya están en compromisos_siesa.
                _compromisos_payload = DespachoParialService._build_compromisos_244328(
                    cantidades, rowid_map, compromisos_siesa, uom_map=_uom_pref
                )
                if _compromisos_payload:
                    connekta.trigger_comprometer_pedido(consec_docto, _compromisos_payload)
                    logger.info(
                        '[DESPACHO_PARCIAL] 244328 OK — T405 actualizado tarea=%s líneas=%d',
                        tarea.id, len(_compromisos_payload),
                    )
                else:
                    logger.warning(
                        '[DESPACHO_PARCIAL] 244328 omitido — sin compromisos válidos tarea=%s',
                        tarea.id,
                    )
                # ─────────────────────────────────────────────────────────────────────

                logger.info(
                    '[DESPACHO_PARCIAL] → 142945 tarea=%s pedido=%s cantidades=%s',
                    tarea.id,
                    tarea.numero_pedido_siesa,
                    [(i['producto_codigo'], i['cantidad_empacada']) for i in items],
                )
                try:
                    resp_rm = connekta.trigger_despacho(tipo_docto, consec_docto, items)
                except Exception as e_rm:
                    if 'comprometido' in str(e_rm).lower():
                        logger.warning(
                            '[DESPACHO_PARCIAL] 142945 rechazado (no comprometido) — '
                            'verificando FE para %s%s', tipo_docto, consec_docto
                        )
                        facturas_pre = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
                        if facturas_pre:
                            return DespachoParialService._persistir_resultado(
                                tarea, 'PREEXISTENTE', {'idempotente': True, 'facturas': facturas_pre}
                            )
                        raise ValueError(
                            f'Pedido {tarea.numero_pedido_siesa}: 142945 rechazado y no hay FE activa. '
                            'Usar /facturar-rm-manual con el número de RM visible en Siesa.'
                        )
                    raise

                try:
                    tipo_rm, consec_rm = DespachoParialService._parsear_rm(resp_rm)
                except ValueError as _e_parse:
                    # 142945 no devuelve el consecutivo en su response ("Importacion exitosa").
                    # Auto-detect: consulta dinámica busca en BD Siesa la RM recién creada.
                    rm_detectada = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
                    if rm_detectada:
                        tipo_rm   = rm_detectada['tipo']
                        consec_rm = rm_detectada['consec']
                        logger.info(
                            '[DESPACHO_PARCIAL] RM auto-detectada %s-%s para %s%s (tarea=%s)',
                            tipo_rm, consec_rm, tipo_docto, consec_docto, tarea.id
                        )
                    else:
                        facturas_pre = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
                        if facturas_pre:
                            return DespachoParialService._persistir_resultado(
                                tarea, 'PREEXISTENTE', {'idempotente': True, 'facturas': facturas_pre}
                            )
                        raise ValueError(
                            f'142945 creó la RM para {tarea.numero_pedido_siesa} pero no se pudo '
                            f'detectar el consecutivo. {_e_parse}. '
                            'Buscar en Siesa y usar /facturar-rm-manual con ese número.'
                        ) from _e_parse

                # Guardar consec en BD ANTES de llamar 142943 — garantiza retry sin re-crear RM.
                # Commit independiente: si 142943 falla, el retry lee rm_tipo/rm_consec de BD
                # y salta directamente a 142943 sin volver a llamar 142945.
                tarea.rm_tipo   = tipo_rm
                tarea.rm_consec = consec_rm
                db.session.commit()
                logger.info(
                    '[DESPACHO_PARCIAL] RM %s-%s guardada en BD — tarea=%s',
                    tipo_rm, consec_rm, tarea.id
                )

        # 4. Guard anti-duplicado FE
        facturas_existentes = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
        if facturas_existentes:
            logger.info(
                '[DESPACHO_PARCIAL] tarea=%s pedido=%s%s ya tiene FE — omitiendo 142943',
                tarea.id, tipo_docto, consec_docto
            )
            resp_fe = {'idempotente': True, 'facturas': facturas_existentes}
        else:
            resp_fe = connekta.trigger_factura_desde_remision(tipo_rm, consec_rm, cabecera)

        # 5. Persistir resultado final
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
        resultado = {'rm': rm_str, 'fe_response': fe_response}
        tarea.siesa_triggered    = True
        tarea.siesa_triggered_at = datetime.utcnow()
        tarea.estado             = 'DESPACHADO'
        tarea.fecha_despachado   = tarea.fecha_despachado or datetime.utcnow()
        tarea.siesa_response     = json.dumps(resultado)
        db.session.commit()
        logger.info('[DESPACHO_PARCIAL] tarea=%s → %s FE=ok', tarea.id, rm_str)
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
                                   uom_map: dict = None) -> list:
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
          · el admin envió cantidad > 0 en el body
          · existe f431_rowid en rowid_map (línea identificada en T431)
          · f120_id está presente en compromisos_siesa (garantiza lookup en 244328)

        uom_map: {codigo_siesa: unidad_empaque} — para desambiguar productos dual-unit
          que aparecen con 2 líneas en compromisos (PQ y UND). Se prefiere la línea
          cuya f405_id_unidad_medida coincida; si no hay match, la de mayor cantidad.
        """
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
            if cant_real <= 0 or not rowid:
                continue
            comp_row  = _comp_por_ref.get(ref, {})
            cant_orig = float(comp_row.get('f405_cant_por_remisionar_base') or cant_real)
            id_item   = comp_row.get('f120_id')          # ID numérico de T120 — fuente primaria

            if not id_item:
                logger.warning(
                    '[DESPACHO_PARCIAL] _build_compromisos_244328: f120_id ausente para ref=%s '
                    '— 244328 fallará (conector no acepta referencia texto)',
                    ref,
                )

            result.append({
                'referencia_item':     ref,
                'id_item':             id_item,   # f431_id_item en 244328 — OBLIGATORIO numérico
                'cant_base':           cant_orig,
                'nro_registro':        rowid,      # f431_nro_registro = f431_rowid
                'cant_por_remisionar': cant_real,
                'lote':                comp_row.get('f405_id_lote') or None,
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

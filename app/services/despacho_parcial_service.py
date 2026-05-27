"""
DespachoParialService — despacho parcial vía conector 244328 (Compromiso_PididosV1).

Responsabilidad única: actualizar f405_cant_por_remisionar_base en T405 de Siesa
con las cantidades reales picadas/empacadas en el WMS. Tras esa actualización,
la automatización interna de Siesa crea la RM y la FE sin intervención adicional.

Flujo confirmado (2026-05-26):
  1. GET API_v2_Ventas_Pedidos_Compromisos  → obtiene rowid_map + cant. originales
  2. POST 244328 (v3.1 dynamic URL)         → actualiza T405 con cant. WMS reales
  3. Siesa automation                        → crea RM + FE automáticamente
  4. _persistir_resultado()                  → marca tarea DESPACHADO + siesa_triggered

Idempotencia:
  - compromisos vacíos en Siesa = automation ya procesó el pedido → persistir directo
  - siesa_triggered=True en BD  = tarea ya completada → raise (guard en línea 50)

Nota: los conectores 142945 (RemisionPedido) y 142943 (FacturaRemision) usan la URL
estándar v3 para la que las credenciales Connekta NO están autorizadas (HTTP 401).
Solo 244328 usa v3.1 dinámica y está autorizado — es el único conector necesario.
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

        # 3. Actualizar cantidades reales en Siesa con conector 244328.
        # f120_id viene en el response de API_v2_Ventas_Pedidos_Compromisos (JOIN T431→T120).
        # Tras esta actualización, automation Siesa crea RM + FE automáticamente.
        _compromisos_payload = DespachoParialService._build_compromisos_244328(
            cantidades, rowid_map, compromisos_siesa, uom_map=_uom_pref
        )
        if _compromisos_payload:
            connekta.trigger_comprometer_pedido(consec_docto, _compromisos_payload)
            logger.info(
                '[DESPACHO_PARCIAL] 244328 OK — T405 actualizado tarea=%s líneas=%d; '
                'automation Siesa creará RM + FE automáticamente',
                tarea.id, len(_compromisos_payload),
            )
        else:
            logger.warning(
                '[DESPACHO_PARCIAL] 244328 omitido — sin compromisos válidos tarea=%s '
                '(sin rowid o cantidad=0); automation Siesa manejará el pedido',
                tarea.id,
            )

        # 4. Persistir resultado — Siesa automation gestiona RM + FE internamente
        return DespachoParialService._persistir_resultado(
            tarea, '244328-OK', {'automatizacion_siesa': True}
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

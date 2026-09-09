"""
Dominio NC/Liquidación del gateway Connekta — extraído de ConnektaGateway
(2026-09-09, paso 8 y último de la deuda de tamaño; ver pasos 1-7: app/utils/
siesa_formato.py, connekta_circuit_breaker.py, connekta_compras_gateway.py,
connekta_ajustes_gateway.py, connekta_consultas_gateway.py,
connekta_traslados_gateway.py, connekta_facturacion_gateway.py).

11 miembros: nota crédito (142946 solo-crea, 251126 crea+cruza, 251546
motivo DIAN), su desambiguación de consecutivo (`get_max_rowid_nc`,
`get_consec_nc_creada`, `_filas_nc_encabezado`, `puede_fijar_motivo_dian`),
recibo de caja (142888) y documento contable de retenciones (142882) — los
documentos financieros de la liquidación de ruta, dejados para el final de
la deuda de tamaño por ser los más delicados (cartera, DIAN, partida doble).

**No recibe config propia.** Mismo patrón que los pasos anteriores: toma la
instancia completa de `ConnektaGateway` como `core`. `ConnektaGateway`
conserva cada miembro como delegado delgado (incluida la property
`puede_fijar_motivo_dian`) — el singleton `connekta` y sus callers
(`siesa_job_service.py`, `routes/health.py`) no cambian.

Llamadas intra-dominio que se quedan como `self.X` (mismo objeto, no
`core.X`): `trigger_nota_factura` y `trigger_nota_factura_crear_cruzar` →
`self._build_header_docto_ventas_nc` / `self._build_transportador_vacio`.

Excepción deliberada: `get_max_rowid_nc` y `get_consec_nc_creada` llaman
`core._filas_nc_encabezado()` (vía el delegado en ConnektaGateway), NO
`self._filas_nc_encabezado()`. tests/test_nc_motivo_dian.py y
routes/health.py ya patcheaban `connekta._filas_nc_encabezado` (instancia
o clase) esperando que afecte a estos dos métodos — un `self.X` intra-clase
habría dejado esos patches sin efecto tras la extracción, un regresión real
detectada corriendo la suite completa, no solo los tests nuevos.

La única llamada a un método de OTRO dominio ya delegado en `core` es
`core.get_vencimiento_factura(...)` (Consultas) dentro de
`trigger_nota_factura_crear_cruzar` — sigue siendo un método público de
`core`, no cambia por venir de otro dominio.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaLiquidacionGateway:

    def __init__(self, core):
        self._core = core

    def _build_transportador_vacio(self) -> dict:
        """
        Bloque f462_* (transportador) vacío — registro plano de ancho fijo
        exige estos 12 campos aunque no haya transportador. Compartido entre
        todos los conectores de Ventas Comercial que crean NC (142946/250696,
        251126) — ver Regla 0 del CLAUDE.md (una política, una función):
        duplicar esto por conector es exactamente el patrón que ya divergió
        una vez y costó 3h de diagnóstico.

        Alfanumérico en None = Siesa OMITE el campo del registro plano
        (desalinea todo lo que sigue) — DEBE ser '' (mismo hallazgo ya
        documentado para f470_desc_varible en 173076).
        """
        core = self._core
        return {
            'f462_id_vehiculo': '',
            'f462_id_tercero_transp': '',
            'f462_id_sucursal_transp': '',
            'f462_id_tercero_conductor': '',
            'f462_nombre_conductor': '',
            'f462_identif_conductor': '',
            'f462_numero_guia': '',
            'f462_cajas': core._fmt_decimal_sin_signo(0, 10),
            'f462_peso': core._fmt_decimal_sin_signo(0, 15),
            'f462_volumen': core._fmt_decimal_sin_signo(0, 15),
            'f462_valor_seguros': core._fmt_decimal_sin_signo(0, 15),
            'f462_notas': '',
        }

    def _build_header_docto_ventas_nc(self, tipo_docto_fe: str, consec_fe,
                                       fecha: str) -> dict:
        """
        Campos base de 'Docto ventas comercial' para NC — idénticos entre
        142946/250696 (solo crea) y 251126 (crea+cruza): el header no cambia,
        lo único que difiere entre esos dos conectores es el nombre de la
        sección en el JSON y si además se manda Cuotas CxC. Ver Regla 0.

        F350_IND_ESTADO siempre 0 (Elaboración), NUNCA 1 (Aprobado) —
        verificado en vivo contra Siesa QA (2026-07-29): con estado=1 Siesa
        rechaza el documento ("El valor de la cartera debe ser igual al
        valor de las CxC" en 142946 sin CuotasCxC; "entidades dinámicas
        obligatorias" en 251126, ver CLAUDE.md). Aprobación sigue manual
        (Regla #21).
        """
        core = self._core
        consec_int = int(consec_fe) if str(consec_fe).isdigit() else consec_fe
        return {
            'F_CIA': int(core.id_cia_siesa),
            'F_CONSEC_AUTO_REG': 1,
            'F350_ID_CO': core.centro_op,
            'F350_ID_TIPO_DOCTO': core.tipo_docto_nota_credito,
            'F350_CONSEC_DOCTO': 0,
            'F350_FECHA': fecha,
            'F350_IND_ESTADO': 0,
            'F350_IND_IMPRESION': 0,
            'F430_ID_TIPO_DOCTO': tipo_docto_fe,
            'F430_CONSEC_DOCTO': consec_int,
        }

    def trigger_nota_factura(self, tipo_docto_fe: str, consec_fe,
                              lineas: list, notas: str = '') -> dict:
        """
        142946 → API_v1_Ventas_Comercial_NotaFactura
        Nota crédito amarrada a factura para devoluciones parciales o totales.
        Solo crea en Elaboración — NO cruza cartera (sin sección CuotasCxC).
        Para crear+cruzar en un solo POST, ver trigger_nota_factura_crear_cruzar.

        lineas: lista de dicts con:
          - f470_rowid_movto: rowid del renglón de la FE (del GET)
          - f470_cant_base: cantidad a devolver
          - f470_id_bodega: bodega donde reingresa la mercancía
          - f470_id_motivo: motivo del movimiento
          - f470_id_causal_devol: causal de devolución DIAN
          - f120_referencia: código del producto (para log)
        """
        core = self._core
        if not core.tipo_docto_nota_credito:
            raise ValueError(
                'SIESA_TIPO_DOCTO_NOTA_CREDITO no configurado — requerido para 142946'
            )

        fecha_hoy = core._fecha_hoy_bogota()
        cia = int(core.id_cia_siesa)

        # Orden de claves alineado a la tabla del DOCX (142946) — no confirmado
        # que el orden importe (una prueba en vivo con orden distinto dio el
        # mismo resultado byte a byte), se deja así por legibilidad/trazabilidad
        # contra el spec, no como el fix real. Ver nota en el docstring de la
        # clase sobre el tamaño de registro pendiente de resolver.
        movimientos = []
        for i, lin in enumerate(lineas, 1):
            movimientos.append({
                'F_CIA': cia,
                'f470_id_co': core.centro_op,
                'f470_id_tipo_docto': core.tipo_docto_nota_credito,
                'f470_consec_docto': 0,
                'f470_nro_registro': i,
                'f470_id_item': 0,
                'f470_referencia_item': lin.get('f120_referencia') or '',
                'f470_codigo_barras': '',
                'f470_id_ext1_detalle': '',
                'f470_id_ext2_detalle': '',
                'f470_id_bodega': lin.get('f470_id_bodega') or core.bodega,
                'f470_id_ubicacion_aux': '',
                'f470_id_lote': '',
                # Campo ausente del DOCX original — encontrado en el Asistente UnoEE
                # de Generic Transfer (estructura real f_tipo_reg=470 v12 sub02):
                # obligatorio, valor fijo 502 = "Devolución de ventas". Sin esto,
                # Siesa reportaba "tamaño de registro" corto por los 3 bytes exactos
                # que ocupa este campo entre f470_id_lote y f470_id_motivo.
                'f470_id_concepto': 502,
                'f470_id_motivo': lin.get('f470_id_motivo') or core.motivo_ventas,
                'f470_ind_obsequio': 0,
                'f470_id_co_movto': core.centro_op,
                'f470_id_un_movto': core.unidad_negocio,
                'f470_id_ccosto_movto': '',
                'f470_id_proyecto': '',
                'f470_id_unidad_medida': lin.get('f470_id_unidad_medida') or core.uom_default,
                'f470_cant_base': core._fmt_decimal_sin_signo(lin['f470_cant_base'], 15),
                'f470_cant_2': core._fmt_decimal_sin_signo(0, 15),
                'f470_ind_impto_asumido': 0,
                'f470_desc_variable': '',
                'f470_notas': lin.get('f470_notas') or '',
                'f470_id_causal_devol': lin.get('f470_id_causal_devol') or core.causal_devolucion_default,
                'f470_rowid_movto': int(lin['f470_rowid_movto']),
            })

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Doctoventascomercial': [{
                **self._build_header_docto_ventas_nc(tipo_docto_fe, consec_fe, fecha_hoy),
                **self._build_transportador_vacio(),
            }],
            'Movimientos': movimientos,
            'Final': [{'F_CIA': cia}],
        }

        logger.info(
            '[CONNEKTA] NotaFactura (%s): FE %s-%s, %d líneas, notas=%s',
            core.nombre_conector_nota_factura, tipo_docto_fe, consec_fe,
            len(lineas), notas[:80] if notas else ''
        )
        # Conectores clonados vía Asistente UnoEE (como 250696) quedan registrados
        # en Siesa como dinámicos (v3.1/conectoresimportar + idSistema), no como
        # el estándar original 142946 (v3/conectoresimportarestandar) — mismo
        # patrón ya usado en 173076/174646.
        _es_estandar = core.conector_nota_factura in ('142946',)
        return core._post(
            core.conector_nota_factura,
            core.nombre_conector_nota_factura,
            payload,
            url=core.url_post if _es_estandar else core.url_post_dinamico,
            extra_params=None if _es_estandar else {'idSistema': core.id_sistema},
        )

    def trigger_nota_factura_crear_cruzar(self, tipo_docto_fe: str, consec_fe,
                                           lineas: list, valor_cruce: float,
                                           notas: str = '') -> dict:
        """
        251126 → PapeleriaMedellin_NotaCredito_CrearCruzar_WMS_v2. Crea la NC
        Y cruza la cartera contra la factura en el mismo POST — a diferencia
        de trigger_nota_factura (250696/142946), que solo crea. Ver CLAUDE.md
        "Cruce de cartera SÍ se pudo automatizar — conector 251126".

        lineas: mismo formato que trigger_nota_factura (f470_rowid_movto,
          f470_cant_base, f470_id_bodega, f470_id_motivo, f470_id_causal_devol,
          f120_referencia, f470_id_unidad_medida).
        valor_cruce: suma de f470_vlr_neto PRORRATEADO por cantidad devuelta
          (NUNCA f470_vlr_bruto — bug confirmado en vivo 2026-07-30, ver
          CLAUDE.md). El caller es responsable del prorrateo — esta función
          no tiene visibilidad de cuánto se facturó originalmente por línea.

        Sigue creando en Elaboración (F350_IND_ESTADO=0) — motivo DIAN y
        aprobación siguen manuales (Regla #21), esto solo automatiza crear+cruzar.
        """
        core = self._core
        if not core.tipo_docto_nota_credito:
            raise ValueError(
                'SIESA_TIPO_DOCTO_NOTA_CREDITO no configurado — requerido para 251126'
            )

        fecha_hoy = core._fecha_hoy_bogota()
        cia = int(core.id_cia_siesa)
        co = core.centro_op
        fecha_vcto = core.get_vencimiento_factura(tipo_docto_fe, consec_fe)

        docto_ventas = {
            **self._build_header_docto_ventas_nc(tipo_docto_fe, consec_fe, fecha_hoy),
            **self._build_transportador_vacio(),
        }

        movimientos = []
        for i, lin in enumerate(lineas, 1):
            movimientos.append({
                'F_CIA': cia,
                'f470_id_co': co,
                'f470_id_tipo_docto': core.tipo_docto_nota_credito,
                'f470_consec_docto': 0,
                'f470_nro_registro': i,
                'f470_id_bodega': lin.get('f470_id_bodega') or core.bodega,
                'f470_id_concepto': 502,
                'f470_id_motivo': lin.get('f470_id_motivo') or core.motivo_ventas,
                'f470_ind_obsequio': 0,
                'f470_id_co_movto': co,
                'f470_id_un_movto': core.unidad_negocio,
                'f470_id_unidad_medida': lin.get('f470_id_unidad_medida') or core.uom_default,
                'f470_cant_base': core._fmt_decimal_sin_signo(lin['f470_cant_base'], 15),
                'f470_cant_2': core._fmt_decimal_sin_signo(0, 15),
                'f470_ind_impto_asumido': 0,
                'f470_referencia_item': lin.get('f120_referencia') or '',
                'f470_rowid_movto': int(lin['f470_rowid_movto']),
                'f470_id_item': '',
                'f470_codigo_barras': '',
                'f470_id_ubicacion_aux': '',
                'f470_id_lote': '',
                'f470_id_ccosto_movto': '',
                'f470_id_causal_devol': lin.get('f470_id_causal_devol') or core.causal_devolucion_default,
            })

        cuotas_cxc = {
            'F_CIA': cia,
            'F350_ID_CO': co,
            'F350_ID_TIPO_DOCTO': core.tipo_docto_nota_credito,
            'F350_CONSEC_DOCTO': 0,
            'F353_ID_TIPO_DOCTO_CRUCE': tipo_docto_fe,
            'F353_CONSEC_DOCTO_CRUCE': docto_ventas['F430_CONSEC_DOCTO'],
            'F353_NRO_CUOTA_CRUCE': 0,
            'F353_VLR_CRUCE': core._fmt_decimal_sin_signo(valor_cruce, 15, 4),
            'F_PORCENTAJE_CUOTA': '000.00',
            'F353_FECHA_VCTO': fecha_vcto,
            'F353_VLR__DSCTO_PP': core._fmt_decimal_sin_signo(0, 15),
            'F_PORCENTAJE_PP': '000.00',
            'F353_FECHA_DSCTO_PP': '',
        }

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Docto. ventas comercial': [docto_ventas],
            'Cuotas CxC': [cuotas_cxc],
            'Movimientos': movimientos,
            'Final': [{'F_CIA': cia}],
        }

        logger.info(
            '[CONNEKTA] NotaFactura crear+cruzar (%s): FE %s-%s, %d líneas, '
            'valor_cruce=%.2f, notas=%s',
            core.nombre_conector_nota_credito_cruzar, tipo_docto_fe, consec_fe,
            len(lineas), valor_cruce, notas[:80] if notas else ''
        )
        return core._post(
            core.conector_nota_credito_cruzar,
            core.nombre_conector_nota_credito_cruzar,
            payload,
            url=core.url_post_dinamico,
            extra_params={'idSistema': core.id_sistema},
        )

    # ── Motivo DIAN sobre la NC ya creada (251546, segundo POST) ────────────
    #
    # El paso 3 del "Procedimiento Manual" del CLAUDE.md. No se puede hacer en
    # el mismo POST que crea la NC: Entidades dinámicas necesita el consecutivo
    # REAL del documento, y con `F_CONSEC_AUTO_REG=1` ese número todavía no
    # existe cuando la sección se procesa. De ahí las tres piezas de abajo:
    # mirar el rowid máximo ANTES, crear (251126), y después identificar la
    # fila nueva para dispararle el motivo.

    @property
    def puede_fijar_motivo_dian(self) -> bool:
        """¿Está todo lo necesario para automatizar el motivo DIAN?

        Falta la consulta dinámica registrada en Connekta (la exploración usó
        SQL crudo, no apto para producción). Mientras falte, el motivo se sigue
        poniendo a mano y `/api/health/siesa` lo reporta — nadie tiene que
        adivinar si el paso manual sigue vivo.
        """
        core = self._core
        return bool(
            core.consulta_nc_consecutivo
            and core.conector_nc_motivo_dian
            and not core.modo_simulacion
        )

    def _filas_nc_encabezado(self) -> list:
        """Filas recientes de `t350_co_docto_contable` para NCE en este CO.

        La consulta dinámica `CONNEKTA_CONSULTA_NC_CONSECUTIVO` debe devolver,
        sin parámetros, las columnas crudas de la tabla:

            SELECT TOP 100 f350_rowid, f350_id_co, f350_id_tipo_docto,
                   f350_consec_docto, f350_fecha, f350_ind_estado,
                   f350_total_db
            FROM t350_co_docto_contable
            WHERE f350_id_tipo_docto = ''NCE''
            ORDER BY f350_rowid DESC

        Las comillas van dobladas (regla 15) y el tope es 100 (regla 10:
        páginas grandes devuelven registros fantasma con todo en NULL — y acá
        un fantasma sería una candidata más en la desambiguación).

        El filtrado fino es del lado del WMS a propósito (mismo criterio que
        `get_remision_desde_pedido`): así un cambio de negocio no obliga a
        reeditar una consulta en Siesa.
        """
        core = self._core
        if not core.consulta_nc_consecutivo:
            return []
        res = core._get(
            core.consulta_nc_consecutivo,
            params_extra={'paginacion': 'numPag=1|tamPag=100'},
            url=core.url_get_dinamico,
        )
        detalle = res.get('detalle', {}) if isinstance(res, dict) else {}
        filas = detalle.get('Datos') or detalle.get('Table') or []
        # Un registro fantasma (regla 10) llega con todo en NULL. Descartarlo
        # acá y no en el filtro: una fila sin rowid no es una NC, es basura de
        # paginación, y contarla como candidata rompería la desambiguación.
        return [f for f in filas if f.get('f350_rowid')]

    def get_max_rowid_nc(self) -> int | None:
        """Mayor `f350_rowid` de NCE **antes** de crear la nuestra.

        Es la marca de agua que después distingue "la NC que acabo de crear" de
        una idéntica creada esta misma mañana por otra devolución del mismo
        valor. Nunca propaga la excepción: esto corre en el camino crítico de
        la NC y ningún fallo acá puede impedir que la nota se cree.
        """
        core = self._core
        try:
            # Vía core (delegado), no self: preserva que patchear
            # `connekta._filas_nc_encabezado` (instancia o clase) siga
            # afectando este método — patrón ya usado por tests/test_nc_
            # motivo_dian.py y routes/health.py antes de esta extracción.
            filas = core._filas_nc_encabezado()
            rowids = [int(f.get('f350_rowid') or 0) for f in filas]
            return max(rowids) if rowids else None
        except Exception as e:
            logger.warning('[CONNEKTA] get_max_rowid_nc falló (no bloqueante): %s', e)
            return None

    def get_consec_nc_creada(self, valor_cruce: float, fecha: str,
                             rowid_minimo: int | None = None) -> int:
        """Consecutivo real de la NC recién creada. Levanta si hay duda.

        Filtra por CO + NCE + fecha + estado Elaboración + rowid posterior
        al watermark, y exige **exactamente una** coincidencia. Con cero o
        con varias, falla: escribirle el motivo DIAN a la nota equivocada
        es un error fiscal en un documento de un tercero que no lo pidió, y
        el costo de no hacerlo es que contabilidad siga poniendo el motivo
        a mano un día más. Regla 0 — ante dato ausente, fallar hacia el
        lado conservador.

        NO filtra por `f350_total_db` pese a recibir `valor_cruce` (se deja
        solo para el mensaje de diagnóstico si falla). Verificado en vivo
        el 2026-08-06 contra ~10 NCE reales de Siesa QA (incluida NCE-58,
        la propia devolución de PD1369): `f350_total_db` es SIEMPRE 0
        mientras `f350_ind_estado == 0` (Elaboración — el único estado en
        el que este método busca, Regla #21) y solo se llena cuando alguien
        aprueba el documento a mano en Siesa. Filtrar por ese campo en este
        punto del flujo no distinguía nunca ninguna candidata — todas las
        filas en Elaboración muestran 0 — así que el motivo DIAN nunca se
        ponía solo, sin importar qué tan bien estuviera configurada la
        consulta dinámica. El valor real del cruce sí está disponible de
        inmediato (se ve en vivo en CxC → Facturas de la NC), pero en otra
        tabla (T353, vía `API_v2_CxC_General`) — usarlo requeriría verificar
        en vivo los nombres de campo de esa tabla antes de confiar en ellos
        (mismo criterio que con `f753_id_grupo_entidad` y `G504_1`, ver
        CLAUDE.md); mientras eso no se haga, CO + tipo + fecha + estado +
        rowid-tras-watermark es la desambiguación disponible.
        """
        core = self._core
        # Vía core (delegado), no self — ver nota en get_max_rowid_nc.
        filas = core._filas_nc_encabezado()
        candidatas = []
        for f in filas:
            if (f.get('f350_id_co') or '').strip() != core.centro_op:
                continue
            if (f.get('f350_id_tipo_docto') or '').strip() != core.tipo_docto_nota_credito:
                continue
            if int(f.get('f350_ind_estado') or 0) != 0:
                continue          # ya aprobada: el motivo ya está puesto
            _f = str(f.get('f350_fecha') or '')[:10].replace('-', '')
            if _f != fecha:
                continue
            if rowid_minimo is not None and int(f.get('f350_rowid') or 0) <= rowid_minimo:
                continue          # existía antes de nuestro POST
            candidatas.append(f)

        if len(candidatas) != 1:
            raise Exception(
                f'No se pudo identificar sin ambigüedad la NC recién creada '
                f'({len(candidatas)} candidatas para CO={core.centro_op} '
                f'{core.tipo_docto_nota_credito} fecha={fecha} '
                f'valor_cruce={valor_cruce:.2f} (solo diagnóstico, no filtra) '
                f'rowid>{rowid_minimo}). '
                'El motivo DIAN se deja manual para esta nota — poner el motivo '
                'sobre un documento equivocado es peor que no ponerlo.'
            )
        return int(candidatas[0]['f350_consec_docto'])

    def trigger_motivo_dian_nc(self, consec_nc: int, concepto: str = '') -> dict:
        """251546, **solo** la sección Entidades dinámicas — el paso 3 manual.

        No crea ni aprueba nada: le adjunta el concepto DIAN a una NC que ya
        existe en Elaboración. Verificado en vivo 2026-08-03 contra
        NCE-00000057 (`codigo:0`). Los códigos de maestro no son adivinables y
        no están en el texto de ayuda del Asistente — salieron de consultar
        `t744/t747/t740/t741` directamente; ver CLAUDE.md antes de tocarlos.

        `f753_dato_numerico=0` con `f753_id_maestro*` poblados: el atributo
        `co015_concepto_nc` es de tipo maestro genérico y rechaza el numérico
        suelto.
        """
        core = self._core
        cia = int(core.id_cia_siesa)
        entidad = {
            'F_CIA': cia,
            'F_ACTUALIZA_REG': 1,
            'f350_id_co': core.centro_op,
            'f350_id_tipo_docto': core.tipo_docto_nota_credito,
            'f350_consec_docto': int(consec_nc),
            'f753_id_grupo_entidad': 'FE_CONCEPTOS NC 2.1',
            'f753_id_entidad': 'EUNOECO015',
            'f753_id_atributo': 'co015_concepto_nc',
            'f753_dato_numerico': 0,
            'f753_id_tipo_entidad': 'G504_1',
            'f753_dato_texto': '',
            'f753_id_maestro': 'MUNOECO017',
            'f753_id_maestro_detalle': str(concepto or core.concepto_dian_nc),
        }
        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Entidades dinámicas': [entidad],
            'Final': [{'F_CIA': cia}],
        }
        logger.info(
            '[CONNEKTA] Motivo DIAN (%s): %s-%s concepto=%s',
            core.nombre_conector_nc_motivo_dian, core.tipo_docto_nota_credito,
            consec_nc, entidad['f753_id_maestro_detalle'],
        )
        return core._post(
            core.conector_nc_motivo_dian,
            core.nombre_conector_nc_motivo_dian,
            payload,
            url=core.url_post_dinamico,
            extra_params={'idSistema': core.id_sistema},
        )

    def trigger_recibo_caja(self, tercero_nit: str, sucursal: str,
                             monto: float, forma_pago: str,
                             tipo_docto_fe: str, consec_fe,
                             co_factura: str = '',
                             cuenta_cxc: str = '',
                             unidad_negocio: str = '',
                             notas: str = '',
                             ajuste_valor: float = 0.0,
                             ajuste_es_sobrante: bool = False) -> dict:
        """
        142888 → API_v1_ReciboCaja
        Registra cobro del conductor. Cruza automáticamente contra la factura (CxC).

        Secciones spec 142888: Inicial → RCyotrosingresos → Caja → CxC → Final
        forma_pago: cualquier clave de `core._forma_pago_map` (EFECTIVO, TARJETA,
                    las transferencias por banco, TRANSFERENCIA/CONSIGNACION
                    retrocompatibles) → medio de pago Siesa. `CHEQUE` y `EXENTO`
                    existen como opción en la pantalla del conductor pero NO
                    tienen código Siesa configurado todavía — levanta `ValueError`
                    en vez de reportarlos como EFECTIVO (ver comentario más abajo).
        co_factura: CO de la factura cruzada (puede diferir del CO del RC).
        cuenta_cxc: f253_id real de la factura (ej '13050501'). Si vacío, usa core.cxc_auxiliar
                    como fallback — pero el cruce puede no aplicar si la factura usa otra cuenta.
        unidad_negocio: `f353_id_un_cruce` REAL de la fila de cartera que cruza (no un valor
                    global). PD1411/FE-1416 (2026-08-18): `SIESA_UNIDAD_NEGOCIO=001` fijo se
                    mandaba en toda factura sin mirar la de verdad — esa factura traía UN=99 en
                    Siesa, y `SIESA_UNIDAD_NEGOCIO` sirvió solo de fallback erróneo. Siesa
                    rechazó por dos motivos que eran el mismo: "el auxiliar de caja maneja una
                    U.N. diferente a la del documento" y "el documento de cruce no existe" — la
                    clave compuesta que arma Siesa (`auxiliar-CO-UN-sucursal-tipo-consec-cuota`)
                    nunca podía matchear con la UN equivocada. `gestor-cartera-pame` (otro
                    proyecto del mismo negocio, mismo Siesa) ya resuelve esto leyendo
                    `f353_id_un_cruce` de la fila de cartera real en vez de un env var — mismo
                    fix acá. Si vacío, cae a `core.unidad_negocio` (comportamiento previo).

        ajuste_valor / ajuste_es_sobrante: diferencia al peso entre lo que el
                    conductor entregó y `monto` (el saldo que este RC cancela).
                    `monto` sigue siendo el saldo de la factura — no cambia para
                    quien ya lo usa así. Con `ajuste_valor=0` (default) el
                    comportamiento es idéntico al de antes de este parámetro.

                    Replica el procedimiento manual de cartera, ya probado por
                    gestor-cartera-pame contra el MISMO Siesa: un recibo con
                    ajuste declarado tiene que cancelar el documento completo
                    (`CR + APROVECHA = saldo`, medido en el rechazo real del
                    RC 004-RC-670: "DB: 880.613 CR: 880.614 DIF: 1"). Por eso
                    el faltante se resta de `F354_VALOR_CR` y se declara aparte
                    en `F354_VALOR_APROVECHA` — nunca simplemente se omite.

                    FALTANTE (`ajuste_es_sobrante=False`): el conductor entregó
                    menos. Plata que NUNCA entró a caja — `F357_VALOR_INGRESO`
                    baja con ella.
                    SOBRANTE (`ajuste_es_sobrante=True`): el conductor entregó
                    de más. Plata que SÍ entró — `F357_VALOR_INGRESO` sube, y el
                    excedente sale por el bloque "otros ingresos" (Siesa deriva
                    el valor de la diferencia, no se manda un campo aparte).

                    Retención Y ajuste al peso NO caben en el mismo RC — el
                    142888 no tiene base gravable para justificar una
                    retención en ninguna de sus cinco secciones (medido por
                    gestor-cartera-pame en 13551501/13551701/13551801). Cuando
                    el recaudo lleva retención, el llamador debe mandar
                    `ajuste_valor=0` acá y declarar el ajuste en el
                    `trigger_documento_contable()` de esa misma retención en su
                    lugar (ver ese método).
        """
        core = self._core
        if not core.tipo_docto_recibo_caja:
            raise ValueError(
                'SIESA_TIPO_DOCTO_RECIBO_CAJA no configurado — requerido para 142888'
            )

        fecha_hoy = core._fecha_hoy_bogota()
        cia = int(core.id_cia_siesa)
        consec_int = int(consec_fe) if str(consec_fe).isdigit() else consec_fe
        co = core.centro_op
        co_fact = co_factura or co

        # Medio de pago Siesa según forma de pago WMS
        #
        # SIN default a EFECTIVO. Hasta el 2026-08-31 un `forma_pago` sin
        # entrada en el mapa (CHEQUE, EXENTO — nunca tuvieron código Siesa
        # configurado, ver `docstring` de este método) caía silencioso a
        # `core.medio_pago_efectivo`: un cheque quedaba reportado en Siesa
        # como si hubiera entrado en efectivo, y la caja de ese día cuadraba
        # con plata que nunca llegó en billetes. Regla 0 — ante dato ausente,
        # el lado conservador es declararlo, no inventar el más parecido.
        #
        # Revienta ACÁ (antes del POST, Regla 6) para que el RC quede
        # FALLIDO con motivo explícito en vez de "enviado" con el medio
        # equivocado — un RC que no sale se reintenta y se ve en el DLQ; uno
        # que sale mal casi nunca se nota hasta el cuadre de caja.
        _fp = (forma_pago or '').upper()
        if _fp not in core._forma_pago_map:
            raise ValueError(
                f'forma_pago={_fp!r} sin medio de pago Siesa configurado '
                f'(Maestros → Medios de pago) — el RC no se envía como '
                f'EFECTIVO por defecto. Medios válidos: '
                f'{", ".join(sorted(core._forma_pago_map))}'
            )
        medio_pago = core._forma_pago_map[_fp]
        # Caja según CO (Siesa: Tesorería → Cajas)
        id_caja = core._co_caja_map.get(co, '999')
        un = unidad_negocio or core.unidad_negocio or '99'

        # --- Ajuste al peso: cuánto entró de verdad vs. cuánto cruza ---
        # `monto` sigue siendo el saldo de la factura (F354_VALOR_CR cuando no
        # hay ajuste). Con ajuste, la caja recibe otra cifra y el cruce se
        # completa con la diferencia declarada — nunca se manda un cruce
        # parcial sin decir a dónde fue el resto (eso es lo que deja el saldo
        # abierto para siempre en la factura).
        ajuste_abs = abs(float(ajuste_valor or 0))
        if ajuste_abs and ajuste_abs >= float(monto):
            raise ValueError(
                f'El ajuste al peso (${ajuste_abs:,.2f}) no puede ser mayor o '
                f'igual al saldo que este RC cancela (${monto:,.2f}) — eso no '
                'es un residuo de redondeo, es cobrar prácticamente nada.'
            )
        if ajuste_abs and ajuste_es_sobrante:
            cash_recibido = float(monto) + ajuste_abs
            valor_cr = float(monto)
            valor_aprovecha = 0.0
        elif ajuste_abs:
            cash_recibido = float(monto) - ajuste_abs
            valor_cr = cash_recibido
            valor_aprovecha = ajuste_abs
        else:
            cash_recibido = float(monto)
            valor_cr = float(monto)
            valor_aprovecha = 0.0

        # --- Sección RCyotrosingresos (Header) ---
        header = {
            'F_CIA': cia,
            'F_CONSEC_AUTO_REG': 1,
            'F350_ID_CO': co,
            'F350_ID_TIPO_DOCTO': core.tipo_docto_recibo_caja,
            'F350_CONSEC_DOCTO': 0,
            'F350_FECHA': fecha_hoy,
            'F357_ID_CAJA': id_caja,
            'F357_FECHA_RECAUDO': fecha_hoy,
            'F350_ID_TERCERO': tercero_nit,
            'F357_ID_MONEDA_INGRESO': 'COP',
            'F357_VALOR_INGRESO': core._fmt_valor(cash_recibido),
            'F357_ID_MONEDA_APLICAR': 'COP',
            'F357_VALOR_APLICAR_REAL': core._fmt_valor(cash_recibido),
            'F357_ID_COBRADOR': core.cobrador_rc,
            'F357_ID_UN': un,
            'F357_ID_CCOSTO': '',
            'F357_ID_FE': core.flujo_efectivo_rc,
            'F350_ID_CLASE_DOCTO': 13,
            'F350_IND_ESTADO': 1,
            'F350_IND_IMPRESION': 0,
            'F350_NOTAS': notas[:2000] if notas else '',
            # ── Ajuste y otros ingresos: OCUPAN SU ANCHO SIEMPRE ────
            #
            # Connekta convierte este JSON en un plano posicional. Omitir un
            # campo no lo deja vacío: **acorta la línea y corre todo lo que
            # sigue**. Sin estos once, el registro medía 430 y Siesa exige 596
            # — y el `F357_IND_VALIDA_MEDPAGO` de abajo aterrizaba en la
            # posición 430 en vez de la 596, que es la que el error señalaba
            # como «obligatorio y numérico».
            #
            # Verificado el 2026-08-11 contra `docs/siesa-specs/142888
            # API_v1_ReciboCaja.docx`: la sección son 33 campos, no 22.
            # Mandábamos 22 y ningún RC llegó nunca a Siesa (job 439).
            #
            # Sin ajuste, los once quedan vacíos — comportamiento idéntico al
            # de siempre. Con faltante, la cuenta y el centro de costo van acá
            # (el VALOR va en `F354_VALOR_APROVECHA` del cruce, no acá — Siesa
            # solo deriva el valor del SOBRANTE, no el del faltante, ver
            # `estrategia_faltante` en gestor-cartera-pame para la medición).
            'F351_ID_AUXILIAR_AJUSTE': core.cuenta_ajuste_faltante if valor_aprovecha else '',
            'F351_ID_CCOSTO_AJUSTE': core.ccosto_ajuste if valor_aprovecha else '',
            'F351_ID_AUXILIAR_PP': '',
            'F351_ID_CCOSTO_PP': '',
            'F351_ID_AUXILIAR_OTRO_ING': (
                core.cuenta_ajuste_sobrante if (ajuste_abs and ajuste_es_sobrante) else ''
            ),
            'F351_ID_TERCERO_OTRO_ING': (
                tercero_nit if (ajuste_abs and ajuste_es_sobrante) else ''
            ),
            'F351_ID_SUCURSAL_OTRO_ING': (
                core.sucursal_ajuste if (ajuste_abs and ajuste_es_sobrante) else ''
            ),
            'F351_ID_CO_OTRO_ING': co if (ajuste_abs and ajuste_es_sobrante) else '',
            'F351_ID_UN_OTRO_ING': (
                core.un_ajuste if (ajuste_abs and ajuste_es_sobrante) else ''
            ),
            # Vacío a propósito: la cuenta del sobrante es de INGRESO y no
            # maneja centro de costo (gestor-cartera-pame, 459/459 movimientos
            # medidos sin ccosto en las cinco sedes). Solo el FALTANTE
            # (familia 5xxxxx, arriba) lo lleva.
            'F351_ID_CCOSTO_OTRO_ING': '',
            'F357_REFERENCIA': '',
            'F357_IND_VALIDA_MEDPAGO': 0,
        }

        # --- Sección Caja (Medio de Pago) ---
        # 20 campos según el DOCX. Mandábamos 15: el registro medía 160 contra
        # 478 exigidos. Mismo defecto que el encabezado.
        caja = {
            'F_CIA': cia,
            'F350_ID_CO': co,
            'F350_ID_TIPO_DOCTO': core.tipo_docto_recibo_caja,
            'F350_CONSEC_DOCTO': 0,
            'F358_ID_MEDIOS_PAGO': medio_pago,
            'F358_VALOR': core._fmt_valor(cash_recibido),
            'F358_ID_BANCO': '',
            'F358_NRO_CHEQUE': 0,
            'F358_NRO_CUENTA': '',
            'F358_COD_SEGURIDAD': '',
            'F358_NRO_AUTORIZACION': '',
            'F358_FECHA_VCTO': '',
            'F358_REFERENCIA_OTROS': '',
            'F358_FECHA_CONSIGNACION': '',
            # Igual que arriba: campos 'Dep'/'No' del spec, vacíos porque no
            # aplican (sin cheque devuelto, sin banco alterno) pero el plano
            # de ancho fijo los necesita presentes. Sin esto, el registro
            # llegaba a 160 bytes en vez de los 478 exigidos.
            'F358_ID_CAUSALES_DEVOLUCION': '',
            'F358_ID_TERCERO': '',
            'F358_NOTAS': '',
            'F358_ID_CCOSTO': '',
            'f358_nro_alt_docto_banco': '',
            # Iba en la posición 15 de 15 y le corresponde la 20 de 20: aunque
            # se mandaba, su valor aterrizaba en el offset equivocado. Por eso
            # las consignaciones tampoco habrían funcionado.
            'f358_docto_banco_cg': '',
        }

        # Consignaciones: requieren referencia + fecha + tipo CG
        forma_upper = (forma_pago or '').upper()
        if forma_upper == 'CONSIGNACION' or medio_pago.startswith('T'):
            if medio_pago != core.medio_pago_efectivo:
                caja['F358_REFERENCIA_OTROS'] = notas[:30] if notas else 'APP'
                caja['F358_FECHA_CONSIGNACION'] = fecha_hoy
                caja['f358_docto_banco_cg'] = 'CG'

        # --- Sección CxC (Cruce contra factura) ---
        # F350_ID_CO, F350_ID_TIPO_DOCTO, F350_CONSEC_DOCTO son del RC (no de la factura)
        # — obligatorios según spec DOCX 142888.
        cxc = {
            'F_CIA': cia,
            'F350_ID_CO': co,
            'F350_ID_TIPO_DOCTO': core.tipo_docto_recibo_caja,
            'F350_CONSEC_DOCTO': 0,
            'F353_ID_AUXILIAR_DOCTO_CRUCE': cuenta_cxc or core.cxc_auxiliar,
            'F353_ID_CO_DOCTO_CRUCE': co_fact,
            'F353_ID_UN_DOCTO_CRUCE': un,
            'F353_ID_SUCURSAL_DOCTO_CRUCE': sucursal or '001',
            'F353_ID_TIPO_DOCTO_CRUCE': tipo_docto_fe,
            'F353_CONSEC_DOCTO_CRUCE': consec_int,
            'F353_NRO_CUOTA_CRUCE': 0,
            'F354_VALOR_CR': core._fmt_valor(valor_cr),
            'F354_VALOR_APLICADO_PP': core._fmt_valor(0),
            # El sobrante no usa este campo (sale por "otros ingresos" arriba,
            # que Siesa deriva solo). El faltante sí: `CR + APROVECHA = monto`
            # (el saldo completo) es lo que exige el cruce con ajuste — ver el
            # docstring del método.
            'F354_VALOR_APROVECHA': core._fmt_valor(valor_aprovecha),
            'F354_VALOR_RETENCION': core._fmt_valor(0),
        }

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'RCyotrosingresos': [header],
            'Caja': [caja],
            'CxC': [cxc],
            'Final': [{'F_CIA': cia}],
        }

        logger.info(
            '[CONNEKTA] ReciboCaja 142888: tercero=%s FE=%s-%s monto=%.2f '
            'pago=%s medio=%s caja=%s co_fact=%s',
            tercero_nit, tipo_docto_fe, consec_fe, monto,
            forma_pago, medio_pago, id_caja, co_fact,
        )
        return core._post(
            core.conector_recibo_caja,
            'API_v1_ReciboCaja',
            payload,
        )

    def trigger_documento_contable(self, tercero_nit: str, sucursal: str,
                                     cuenta_puc: str, monto: float,
                                     base_gravable: float,
                                     tipo_docto_fe: str, consec_fe,
                                     co_factura: str = '',
                                     cuenta_cxc: str = '',
                                     unidad_negocio: str = '',
                                     notas: str = '',
                                     ajuste_valor: float = 0.0,
                                     ajuste_razon: str = '') -> dict:
        """
        142882 → DocumentoContable
        Registra retenciones (retefuente, reteIVA, ICA) como documento contable.
        Cruza contra la factura en MovimientoCxC.
        cuenta_puc: cuenta auxiliar PUC débito (ej. '13551501' para retefuente compras 2.5%)
        co_factura: CO de la factura cruzada (puede diferir del CO del RC).
        cuenta_cxc: f253_id real de la factura para cruce crédito. Fallback: core.cxc_auxiliar.
        unidad_negocio: `f353_id_un_cruce` REAL de la fila de cartera que cruza — mismo
                    parámetro y mismo motivo que `trigger_recibo_caja` (PD1411/FE-1416,
                    2026-08-18): antes de esto, este conector nunca lo recibía y usaba
                    siempre `core.unidad_negocio` (el env var global) — job 470 (recaudo
                    19, PD1421, ruta 22, 2026-08-20) es la primera liquidación con
                    retención que corrió de verdad contra Siesa, y quedó FALLIDO 5/5
                    intentos con rechazo estructural genérico. Si vacío, cae a
                    `core.unidad_negocio` (comportamiento previo, no rompe llamadores viejos).

        ajuste_valor: SOLO faltante (el conductor entregó menos que el saldo
                    de la factura) — nunca sobrante, que es plata que sí entró
                    a caja y por eso lo declara el RC, no esta nota. Este
                    documento es donde el ajuste al peso tiene que ir cuando el
                    recaudo TAMBIÉN lleva retención: el 142888 no tiene base
                    gravable para justificar una retención en ninguna de sus
                    cinco secciones, así que retención y ajuste no caben juntos
                    en el RC (medido por gestor-cartera-pame contra el mismo
                    Siesa — ver `trigger_recibo_caja`). Acá sí caben: se agrega
                    un segundo débito (`cuenta_ajuste_faltante`) y el crédito de
                    cartera sube por la misma plata, en la MISMA línea que la
                    retención — dos créditos contra el mismo (tipo, consec,
                    cuota) es algo que Siesa no se le ha visto aceptar.
        `ajuste_valor=0` (default): comportamiento idéntico al de antes de
                    este parámetro.
        """
        core = self._core
        if not core.tipo_docto_docto_contable:
            raise ValueError(
                'SIESA_TIPO_DOCTO_DOCTO_CONTABLE no configurado — requerido para 142882'
            )

        fecha_hoy = core._fecha_hoy_bogota()
        cia = int(core.id_cia_siesa)
        consec_int = int(consec_fe) if str(consec_fe).isdigit() else consec_fe
        co = core.centro_op
        co_fact = co_factura or co
        auxiliar_cxc = cuenta_cxc or core.cxc_auxiliar
        un = unidad_negocio or core.unidad_negocio or '99'
        ajuste_abs = abs(float(ajuste_valor or 0))

        notas_doc = notas[:2000] if notas else ''
        if ajuste_abs:
            _nota_ajuste = f' | Ajuste al peso -${ajuste_abs:,.2f}'
            if ajuste_razon:
                _nota_ajuste += f': {ajuste_razon}'
            notas_doc = (notas_doc + _nota_ajuste)[:2000]

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Documentocontable': [{
                'F_CIA': cia,
                'F_CONSEC_AUTO_REG': 1,
                'F350_ID_CO': co,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_docto_contable,
                'F350_CONSEC_DOCTO': 0,
                'F350_FECHA': fecha_hoy,
                'F350_ID_TERCERO': tercero_nit,
                'F350_ID_CLASE_DOCTO': 30,
                'F350_IND_ESTADO': 1,
                'F350_IND_IMPRESION': 0,
                'F350_NOTAS': notas_doc,
                # Faltaba. Ver la nota de abajo: mismo defecto que el 142888.
                'f350_id_mandato': '',
            }],
            # Orden y campos según `docs/siesa-specs/142882 - API_v1_
            # DocumentoContable 428272.docx`, verificado el 2026-08-11.
            #
            # Faltaban F351_ID_FE, F351_DOCTO_BANCO y F351_NRO_DOCTO_BANCO —
            # el mismo defecto que dejó al 142888 mandando 22 de 33 campos y
            # que hizo que ningún recibo de caja llegara nunca a Siesa.
            #
            # Job 470 (recaudo 19, PD1421, ruta 22, 2026-08-20) fue la primera
            # liquidación con retención que corrió de verdad: quedó FALLIDO
            # 5/5 con rechazo estructural genérico de Siesa. Dos causas
            # encontradas y corregidas: la UN salía siempre del env var global
            # en vez de la real (ver `unidad_negocio` más arriba), y los
            # campos `_ALT` (moneda alterna) llevaban el mismo valor del
            # movimiento en vez de cero, más dos campos (`F351_NRO_REGISTRO`,
            # `F351_ID_SUCURSAL`) que no están en el spec ni en la
            # implementación probada en producción de `gestor-cartera-pame`
            # (mismo Siesa, mismo conector 142882/NI) — quitados.
            'Movimientocontable': [{
                'F_CIA': cia,
                'F350_ID_CO': co,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_docto_contable,
                'F350_CONSEC_DOCTO': 0,
                'F351_ID_AUXILIAR': cuenta_puc,
                'F351_ID_TERCERO': tercero_nit,
                'F351_ID_CO_MOV': co,
                'F351_ID_UN': un,
                'F351_ID_CCOSTO': '',
                'F351_ID_FE': '',
                'F351_VALOR_DB': core._fmt_valor(monto),
                'F351_VALOR_CR': core._fmt_valor(0),
                # Moneda alterna — spec: "si la auxiliar no maneja moneda
                # alterna, debe ir en cero". Las cuentas de retención
                # colombianas (1355xxxx) no manejan una segunda moneda.
                # Iba en `monto` (el mismo valor del débito) — comparado
                # contra `gestor-cartera-pame` (mismo Siesa, en producción,
                # `retencion_payload.py`), que siempre manda cero acá.
                'F351_VALOR_DB_ALT': core._fmt_valor(0),
                'F351_VALOR_CR_ALT': core._fmt_valor(0),
                'F351_BASE_GRAVABLE': core._fmt_valor(base_gravable),
                'F351_DOCTO_BANCO': '',
                'F351_NRO_DOCTO_BANCO': '',
                'F351_NOTAS': '',
            }],
            'MovimientoCxC': [{
                # Campos del spec DOCX 142882 — TODOS los del esquema MovimientoCxC.
                # Cada campo faltante puede causar rechazo silencioso de Siesa.
                'F_CIA': cia,
                'F350_ID_CO': co,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_docto_contable,
                'F350_CONSEC_DOCTO': 0,
                'F351_ID_AUXILIAR': auxiliar_cxc,
                'F351_ID_TERCERO': tercero_nit,
                'F351_ID_CO_MOV': co,
                'F351_ID_UN': un,
                'F351_ID_CCOSTO': '',
                'F351_VALOR_DB': core._fmt_valor(0),
                # El crédito de cartera cierra por TODO lo que el RC no
                # aplicó — retención + ajuste al peso, sumados en esta MISMA
                # línea. Con ajuste_abs=0 (el caso de siempre) esto sigue
                # siendo exactamente `monto`.
                'F351_VALOR_CR': core._fmt_valor(monto + ajuste_abs),
                # Moneda alterna — igual que en Movimientocontable arriba,
                # siempre cero.
                'F351_VALOR_DB_ALT': core._fmt_valor(0),
                'F351_VALOR_CR_ALT': core._fmt_valor(0),
                'F351_NOTAS': '',
                'F353_ID_SUCURSAL': sucursal or '001',
                'F353_ID_TIPO_DOCTO_CRUCE': tipo_docto_fe,
                'F353_CONSEC_DOCTO_CRUCE': consec_int,
                'F353_NRO_CUOTA_CRUCE': 0,
                'F353_FECHA_VCTO': fecha_hoy,
                'F353_FECHA_DSCTO_PP': fecha_hoy,
                'F353_VLR_DSCTO_PP': core._fmt_valor(0),
                'F354_VALOR_APLICADO_PP': core._fmt_valor(0),
                'F354_VALOR_APLICADO_PP_ALT': core._fmt_valor(0),
                'F354_VALOR_APROVECHA': core._fmt_valor(0),
                'F354_VALOR_APROVECHA_ALT': core._fmt_valor(0),
                'F354_VALOR_RETENCION': core._fmt_valor(0),
                'F354_VALOR_RETENCION_ALT': core._fmt_valor(0),
                'F354_TERCERO_VEND': tercero_nit,
                'F354_NOTAS': '',
            }],
            'Final': [{'F_CIA': cia}],
        }
        if ajuste_abs:
            # El gasto por faltante — cuenta de gasto (familia 5xxxxx), por
            # eso SÍ lleva centro de costo (a diferencia de la retención,
            # que es activo 1355xxxx y no lo maneja). Base gravable en cero:
            # una cuenta de gasto no tiene impuesto que justificar — medido
            # por gestor-cartera-pame, 0 de 68 movimientos con base en la
            # cuenta hermana de estampillas.
            payload['Movimientocontable'].append({
                'F_CIA': cia,
                'F350_ID_CO': co,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_docto_contable,
                'F350_CONSEC_DOCTO': 0,
                'F351_ID_AUXILIAR': core.cuenta_ajuste_faltante,
                'F351_ID_TERCERO': tercero_nit,
                'F351_ID_CO_MOV': co,
                'F351_ID_UN': un,
                'F351_ID_CCOSTO': core.ccosto_ajuste,
                'F351_ID_FE': '',
                'F351_VALOR_DB': core._fmt_valor(ajuste_abs),
                'F351_VALOR_CR': core._fmt_valor(0),
                'F351_VALOR_DB_ALT': core._fmt_valor(0),
                'F351_VALOR_CR_ALT': core._fmt_valor(0),
                'F351_BASE_GRAVABLE': core._fmt_valor(0),
                'F351_DOCTO_BANCO': '',
                'F351_NRO_DOCTO_BANCO': '',
                'F351_NOTAS': (ajuste_razon or 'Ajuste al peso')[:255],
            })
        core._verificar_partida_doble_dc(payload)

        logger.info(
            '[CONNEKTA] DoctoContable 142882: tercero=%s PUC=%s FE=%s-%s monto=%.2f '
            'base=%.2f ajuste=%.2f',
            tercero_nit, cuenta_puc, tipo_docto_fe, consec_fe, monto, base_gravable,
            ajuste_abs,
        )
        return core._post(
            core.conector_docto_contable,
            'API_v1_DocumentoContable',
            payload,
        )


__all__ = ['ConnektaLiquidacionGateway']

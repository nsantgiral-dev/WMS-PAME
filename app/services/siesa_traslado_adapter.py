"""
SiesaTrasladoAdapter — Fachada SOLID sobre ConnektaGateway.

Single Responsibility : operaciones Siesa exclusivas de traslados.
Interface Segregation : TrasladoService solo ve los 4 métodos que necesita.
Dependency Inversion  : TrasladoService depende de este adapter, no del gateway concreto.

ConnektaGateway queda cerrado para modificación; este adapter es el punto
de extensión si en el futuro se agrega un nuevo conector de traslado.
"""
import logging
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


class SiesaTrasladoAdapter:

    # ── Config ────────────────────────────────────────────────────────────

    @property
    def bodega_origen(self) -> str:
        return connekta.bodega

    @property
    def bodega_transito(self) -> str:
        return connekta.bodega_transito

    # ── 174646 — Requisición Interna para Transferir (RIT, clase 75) ──────

    def crear_rit(self, bodega_origen: str, bodega_destino: str,
                  items: list, codigo: str) -> dict:
        """
        Compromete el inventario en Siesa (Estado 3 / Aprobado).
        Debe dispararse al aprobar la solicitud — antes del picking.
        Retorna la respuesta raw de Connekta para que el service extraiga el consecutivo.
        """
        logger.info('[TRASLADO_ADAPTER] crear_rit %s  %s→%s  (%d ítems)',
                    codigo, bodega_origen, bodega_destino, len(items))
        return connekta.crear_requisicion_traslado(
            bodega_origen=bodega_origen,
            bodega_destino=bodega_destino,
            items=items,
            codigo_solicitud=codigo,
        )

    # ── 173076 / 173066 — Salida ──────────────────────────────────────────

    def registrar_salida_transito(self, bodega_origen: str, bodega_transito: str,
                                  items: list, codigo: str,
                                  consec_requisicion: int = None) -> dict:
        """
        173076 — Transferencia en Tránsito Salida (clase 65).
        Descarga Cuenta 14 origen → Cuenta Tránsito.
        """
        logger.info('[TRASLADO_ADAPTER] salida_transito %s  %s→%s',
                    codigo, bodega_origen, bodega_transito)
        return connekta.transferencia_transito_salida(
            bodega_origen=bodega_origen,
            bodega_transito=bodega_transito,
            items=items,
            codigo_solicitud=codigo,
            consec_requisicion=consec_requisicion,
        )

    def registrar_salida_directa(self, bodega_origen: str, bodega_destino: str,
                                  items: list, codigo: str) -> dict:
        """
        173066 — Transferencia Directa (sin bodega tránsito).
        Descarga origen y carga destino en un solo documento.
        """
        logger.info('[TRASLADO_ADAPTER] salida_directa %s  %s→%s',
                    codigo, bodega_origen, bodega_destino)
        return connekta.transferencia_directa(
            bodega_origen=bodega_origen,
            bodega_destino=bodega_destino,
            items=items,
            codigo_solicitud=codigo,
        )

    def recuperar_consec_salida(self, codigo: str) -> 'int | None':
        """Recovery: consulta API v2 cuando el consecutivo de 173076 no llegó en la respuesta."""
        return connekta.get_consec_salida_transito_by_alterno(codigo)

    # ── 174720 — Compromisos desde RIT (packing) ─────────────────────────

    def registrar_compromisos(self, consec_rit: int, bodega_origen: str,
                               bodega_destino: str, items: list, codigo: str) -> dict:
        """
        174720 — Registra cantidades y ubicaciones reales confirmadas en packing.
        bodega_origen: bodega de salida real (f441_id_bodega en Siesa).
        Debe dispararse al confirmar packing (EN_PACKING → PREPARADO).
        """
        logger.info('[TRASLADO_ADAPTER] compromisos %s  RIT=%s  %s→%s  (%d ítems)',
                    codigo, consec_rit, bodega_origen, bodega_destino, len(items))
        return connekta.compromisos_desde_requisicion(
            consec_rit=consec_rit,
            bodega_origen=bodega_origen,
            bodega_destino=bodega_destino,
            items=items,
        )

    # ── 174930 — Transfer desde RIT (despacho) ───────────────────────────

    def despachar_desde_rit(self, consec_rit: int, codigo: str) -> dict:
        """
        174930 — Crea STS desde la RIT comprometida. Reemplaza 173076 cuando
        el traslado pasó por el flujo completo RIT → Compromisos.
        """
        logger.info('[TRASLADO_ADAPTER] despachar_desde_rit %s  RIT=%s', codigo, consec_rit)
        return connekta.transferencia_desde_requisicion(consec_rit=consec_rit)

    # ── 173079 — Entrada ──────────────────────────────────────────────────

    def registrar_entrada(self, bodega_transito: str, bodega_destino: str,
                          items: list, codigo: str,
                          consec_salida: int, co_destino: str = None) -> dict:
        """
        173079 — Transferencia en Tránsito Entrada (clase 66).
        Carga Cuenta 14 destino y cierra la Cuenta Tránsito.
        Requiere el consecutivo del 173076 (consec_salida).
        """
        logger.info('[TRASLADO_ADAPTER] entrada_transito %s  consec_salida=%s',
                    codigo, consec_salida)
        return connekta.transferencia_transito_entrada(
            bodega_transito=bodega_transito,
            bodega_destino=bodega_destino,
            items=items,
            codigo_solicitud=codigo,
            consec_salida=consec_salida,
            co_destino=co_destino,
        )


# Singleton — mismo ciclo de vida que connekta
siesa_traslado = SiesaTrasladoAdapter()

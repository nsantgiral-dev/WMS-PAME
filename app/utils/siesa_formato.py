"""
Formato de campos y validaciones de payload puras para Connekta/Siesa.

Extraído de `ConnektaGateway` (2026-09-09) — estas cuatro funciones no tocan
`self`, no dependen de configuración ni del circuit breaker: son funciones
puras sobre sus argumentos, igual que `app/utils/fecha.py` ya extrajo el
cálculo de fechas. `ConnektaGateway` conserva shims `@staticmethod` que
delegan acá, mismo patrón que `_ahora_bogota`/`_fecha_hoy_bogota` — ningún
caller externo (código o tests) cambia.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def fmt_valor(v) -> str:
    """Formato DecimalConSigno requerido por Siesa: +000000000000000.0000 (21 chars).
    Spec: signo(1) + enteros(15) + punto(1) + decimales(4) = 21 chars exactos."""
    signo = '+' if v >= 0 else '-'
    return f'{signo}{abs(v):020.4f}'


def fmt_decimal_sin_signo(v, enteros: int, decimales: int = 4) -> str:
    """Decimal sin signo de ancho fijo (ej. f470_cant_base/f462_cajas):
    enteros + punto + decimales, cero-rellenado. Encontrado en el Asistente
    UnoEE de Generic Transfer: estos campos NO se auto-rellenan como los
    de tipo Entero/FIJO — hay que mandarlos ya formateados al ancho exacto
    o el registro plano queda corto (Siesa lo rechaza por tamaño)."""
    ancho = enteros + 1 + decimales
    return f'{abs(float(v)):0{ancho}.{decimales}f}'


def verificar_partida_doble_dc(payload: dict) -> None:
    """Débitos == créditos en el DocumentoContable (142882), medido sobre
    el payload que se va a mandar. Hasta que solo llevaba una retención
    esto cuadraba por construcción — el débito y el crédito salían del
    mismo monto, no había forma de descuadrarlo. Con el ajuste al peso
    como segundo concepto (entra por el débito, tiene que salir por el
    crédito de cartera) esa garantía deja de ser estructural.

    Revienta ACÁ, antes del POST — mismo patrón que gestor-cartera-pame
    (`retencion_payload.py::_cuadra_la_partida_doble`) contra el mismo
    conector: un descuadre de un peso lo rechaza Siesa después de 30 a 60
    segundos y con el documento a medio camino, no antes de intentarlo.
    """

    def total(seccion: str, campo: str) -> Decimal:
        return sum(
            (Decimal(m[campo].lstrip('+')) for m in payload.get(seccion, ())),
            Decimal('0'),
        )

    debitos = total('Movimientocontable', 'F351_VALOR_DB') + total(
        'MovimientoCxC', 'F351_VALOR_DB')
    creditos = total('Movimientocontable', 'F351_VALOR_CR') + total(
        'MovimientoCxC', 'F351_VALOR_CR')

    if debitos != creditos:
        raise ValueError(
            f'DocumentoContable (142882) no cuadra: débitos ${debitos:,} '
            f'contra créditos ${creditos:,} (diferencia '
            f'${debitos - creditos:,}). No se manda — Siesa lo rechazaría '
            'con el documento a medio camino.'
        )


def safe_int_env(var_name: str, default: int) -> int:
    """Parse int env var safely — logs warning and falls back to default on bad value."""
    import os

    raw = os.getenv(var_name, '')
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning(f'[CONNEKTA] {var_name}={raw!r} no es numérico — usando default {default}')
        return default


__all__ = ['fmt_valor', 'fmt_decimal_sin_signo', 'verificar_partida_doble_dc', 'safe_int_env']

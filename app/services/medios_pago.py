"""
¿Qué medios de pago puede declarar el conductor? (2026-09-27)

**CHEQUE se ofrecía en la puerta sin medio en Siesa**: el recibo de caja
levantaba `ConnektaPayloadInvalido` y quedaba en un bucle de reintentos. Por
decisión por defecto del dueño, la opción aparece solo si
`SIESA_MEDIO_PAGO_CHEQUE` está configurado (el código del medio en Siesa →
Maestros → Medios de pago). **Una función** lo contesta: la pantalla del
conductor (`listar_paradas`), el formulario de la oficina y la liquidación.

Lo que falta para encenderlo, y queda declarado: el mapa del gateway
(`connekta_gateway._forma_pago_map`) necesita la línea
`'CHEQUE': os.getenv('SIESA_MEDIO_PAGO_CHEQUE')`, y la validación de
`confirmar_parada` rechazar CHEQUE cuando esto es falso. Los dos archivos son
de otro frente (2026-09-27).
"""
import os


def cheque_habilitado() -> bool:
    return bool((os.environ.get('SIESA_MEDIO_PAGO_CHEQUE') or '').strip())


def formas_ofrecidas(formas) -> list:
    """`formas` sin CHEQUE si el medio no existe en Siesa."""
    return [f for f in formas if f != 'CHEQUE' or cheque_habilitado()]

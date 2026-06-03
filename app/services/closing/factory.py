"""PackingCloserFactory — O de SOLID: abierto para extensión, cerrado para modificación.

Agregar un nuevo tipo (ej. DEVOLUCION) = nuevo archivo + una línea aquí.
PackingService.cerrar_packing() no cambia nunca.
"""
from .base import IPackingCloser


class PackingCloserFactory:

    @staticmethod
    def get(tipo_documento: str) -> IPackingCloser:
        if tipo_documento == 'TRASLADO':
            from .traslado_closer import TrasladoPackingCloser
            return TrasladoPackingCloser()
        # Default: PEDIDO (mantiene compatibilidad con tareas existentes sin tipo_documento)
        from .pedido_closer import PedidoPackingCloser
        return PedidoPackingCloser()

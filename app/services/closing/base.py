"""IPackingCloser — contrato que todo closer debe cumplir (I + D de SOLID)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CierreResult:
    exitoso: bool
    mensaje: str
    consec_siesa: Optional[int] = None
    error: Optional[str] = None


class IPackingCloser(ABC):
    """Interfaz única para el cierre de caja de cualquier tipo de documento."""

    @abstractmethod
    def ejecutar_cierre(self, tarea_id: int, bultos_data: list,
                        usuario_id: int) -> CierreResult:
        """
        Ejecuta el cierre de caja para una TareaPacking.
        Implementaciones concretas deciden qué conectores Siesa disparan.
        """

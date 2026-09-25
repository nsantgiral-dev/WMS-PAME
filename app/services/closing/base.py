"""IPackingCloser — contrato que todo closer debe cumplir (I + D de SOLID)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


#: Por qué un cierre NO emitió, cuando no es un error del empaque (2026-09-25).
#: La pantalla los distingue: un retenido por cartera no es «Error Siesa», y
#: con Siesa caído no se factura — ninguno de los dos es «procesado».
RETENIDO_CARTERA = 'RETENIDO_CARTERA'
SIESA_NO_DISPONIBLE = 'SIESA_NO_DISPONIBLE'
ESTADOS_NO_EMITIDO = (RETENIDO_CARTERA, SIESA_NO_DISPONIBLE)


class MotivoSiesaNoDisponible(str):
    """El texto de un pre-chequeo que no pudo preguntarle a Siesa. Es un `str`
    (los llamadores lo leen como mensaje) que además dice QUÉ pasó."""


@dataclass
class CierreResult:
    exitoso: bool
    mensaje: str
    consec_siesa: Optional[int] = None
    error: Optional[str] = None
    #: Uno de `ESTADOS_NO_EMITIDO`, o None (error del empaque / éxito).
    estado: Optional[str] = None
    retencion_id: Optional[int] = None


class IPackingCloser(ABC):
    """Interfaz única para el cierre de caja de cualquier tipo de documento."""

    @abstractmethod
    def ejecutar_cierre(self, tarea_id: int, bultos_data: list,
                        usuario_id: int) -> CierreResult:
        """
        Ejecuta el cierre de caja para una TareaPacking.
        Implementaciones concretas deciden qué conectores Siesa disparan.
        """

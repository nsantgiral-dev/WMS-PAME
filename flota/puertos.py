"""
Puertos del módulo de flota — lo que el dominio y la API necesitan del mundo.

Son `Protocol`, no clases base: el dominio declara la forma que necesita y los
adaptadores la cumplen sin heredar de nada. La dirección de la dependencia
queda invertida a propósito — el dominio no sabe que existe SQLAlchemy.

Sin I/O en este archivo. Solo firmas.
"""
from typing import Dict, List, Optional, Protocol, Union

# Un campo del health es un número medido, una lista medida, o `None`.
#
# `None` significa exactamente una cosa: NO SE PUDO MEDIR TODAVÍA, porque la
# fuente aún no existe. Nunca significa cero. La diferencia importa: 0 documentos
# vencidos es una afirmación sobre la flota; `null` es una afirmación sobre el
# sistema. Confundirlas es lo que convierte un tablero en decoración.
CampoMedido = Union[int, bool, str, List[str], None]


class MedidorDeFlota(Protocol):
    """Lo que el health necesita saber del estado real de la flota.

    Cada método devuelve el valor medido o `None`. Ninguno devuelve 0 por
    defecto — si la tabla que lo alimenta no existe, la respuesta honesta es
    `None`, no un cero que se lee como "todo en orden".

    Si una consulta falla por una razón distinta a "la fuente no existe", el
    error se propaga. Un health que se cae es información; un health que
    responde ceros porque algo reventó adentro es evidencia falsa (regla 5).
    """

    def ambiente(self) -> str:
        """A qué ambiente apunta el sistema: `produccion`, `qa`, `simulacion`..."""
        ...

    def datos_reales(self) -> bool:
        """Si los números que se están mostrando son la operación real."""
        ...

    def vehiculos_activos(self) -> Optional[int]:
        ...

    def fichas_completas(self) -> Optional[int]:
        ...

    def atributos_sin_dato(self) -> Optional[List[str]]:
        """`['TGZ653.distribucion', ...]` — placa.atributo, no id.atributo."""
        ...

    def vehiculos_sin_custodia_activa(self) -> Optional[int]:
        ...

    def custodias_pendiente_sede(self) -> Optional[int]:
        """Custodias cuya sede el WMS todavía no tiene como fila en `almacenes`."""
        ...

    def custodias_cerradas_forzadas(self) -> Optional[int]:
        """Turnos cerrados sin firma del custodio anterior. Mide conducta, no fallas."""
        ...

    def custodias_sin_foto_completa(self) -> Optional[int]:
        ...

    def fotos_pendiente_evidencia(self) -> Optional[int]:
        ...

    def conductores_activos_sin_cuenta(self) -> Optional[int]:
        ...

    def documentos_no_encontrados(self) -> Optional[int]:
        """Documentos que se buscaron y no aparecieron. Distinto de vencido."""
        ...

    def documentos_vencidos(self) -> Optional[int]:
        ...

    def documentos_por_vencer_30d(self) -> Optional[int]:
        ...

    def rutas_historicas_sin_placa(self) -> Optional[int]:
        ...


class AlmacenDeFotos(Protocol):
    """Object storage. La base guarda referencia, hash, bytes y dimensiones.

    Nunca el binario — la columna `Text` con base64 de `recaudo_entrega` es
    justamente lo que este módulo no hereda.
    """

    def guardar(self, contenido: bytes, mime: str) -> str:
        """Devuelve `storage_ref`. Levanta si no pudo guardar."""
        ...

    def dimensiones(self, storage_ref: str) -> Dict[str, int]:
        ...


class CanalDeAviso(Protocol):
    """Por dónde sale un aviso hacia una persona.

    Detrás de un `Protocol` para que el servicio que decide QUÉ avisar no sepa
    que existe Gupshup — y para que el doble de pruebas sea un objeto, no un
    monkeypatch. El doble deja rastro propio (`simulado=True` en la fila, regla
    8): `CanalNotificacionDev` ya costó una hora de creer que 1.485 personas
    habían recibido un cobro que nunca salió.

    `enviar` devuelve el id que asignó el proveedor, o levanta. **No devuelve
    `None` ni `False` ante un fallo**: un canal que degrada hacia algo que se
    parece al éxito es la regla 5, y acá el costo es que un hallazgo vencido se
    quede quieto creyendo que se avisó.
    """

    def enviar(self, telefono: str, plantilla: str,
               parametros: List[str]) -> str:
        """Devuelve el id del proveedor. Levanta `AvisoNoEnviado` si no salió."""
        ...

    @property
    def simulado(self) -> bool:
        """¿Este canal manda de verdad? La fila lo guarda, no solo el log."""
        ...


__all__ = ['CampoMedido', 'MedidorDeFlota', 'AlmacenDeFotos', 'CanalDeAviso']

"""
Qué valores toma `tipo_documento`, escritos **una vez**.

## El defecto que lo originó (2026-09-24)

La métrica «venta perdida por agotados $» filtraba
`EventoStockAgotado.tipo_documento == 'PEDIDO'`. Pero el evento copia el tipo
de la `TareaPicking`, y el único camino de producción que crea tareas de venta
(`iniciar_despacho`, `routes/siesa.py`) escribe **`'PEDIDO_SIESA'`**. El filtro
no coincidía con ninguna fila real: la métrica daba ≈ 0 todos los días, y un
cero se lee igual que «no se perdió ninguna venta».

No era la primera vez. PD1487 (`mobile_service`, el blindaje de pedidos
chicos) fue **la misma forma**: un literal `'PEDIDO'` escrito a mano en el
lector, que el escritor nunca produce, y la protección no corría nunca.

**La clase:** un literal de tipo de documento escrito a mano en un lector que
no coincide con el que escribe el flujo. El arreglo del caso es cambiar el
literal; el de la clase es que escritor y lectores importen el mismo valor, y
que un trinquete (`tests/test_tipo_documento_literal.py`) exija que todo
literal que un lector compare contra `tipo_documento` sea uno que algún
escritor de ese mismo modelo produce.

## Qué escribe cada modelo

| Modelo | Valores | Quién |
|---|---|---|
| `TareaPicking` | `PEDIDO_SIESA` | `iniciar_despacho` — la venta real |
| | `TRASLADO` | `traslado_service` |
| | `PEDIDO` / `None` | solo `POST /api/picking/crear` (manual) y tests |
| `TareaPacking` | `PEDIDO` (default de la columna), `TRASLADO` | `packing_service`, `traslado_service` |
| `EventoStockAgotado` | copia el de su `TareaPicking` | `eventos_agotado_service` |

Por eso «¿es venta?» **no se pregunta por igualdad**: se pregunta por
complemento — todo lo que no es traslado. Un tipo de venta nuevo mañana queda
contado sin que nadie se acuerde de agregarlo a una lista.
"""
from sqlalchemy import or_


class TipoDocumento:
    #: La venta real: lo escribe `iniciar_despacho` en cada `TareaPicking`.
    PEDIDO_SIESA = 'PEDIDO_SIESA'
    #: Legado. Default de `TareaPacking.tipo_documento`, y lo que puede mandar
    #: `POST /api/picking/crear` a mano. Ningún flujo automático lo escribe en
    #: una `TareaPicking`.
    PEDIDO = 'PEDIDO'
    TRASLADO = 'TRASLADO'

    #: Todos los valores válidos. `None` también lo es (tarea manual sin tipo)
    #: y se trata como venta.
    TODOS = (PEDIDO_SIESA, PEDIDO, TRASLADO)

    #: Los valores de venta, para quien necesite la lista explícita (un `in`
    #: en Python sobre una instancia). En SQL, preferir `es_venta()`.
    VENTA = (PEDIDO_SIESA, PEDIDO)

    @staticmethod
    def es_venta_valor(valor) -> bool:
        """`True` si una tarea con ese `tipo_documento` es de venta."""
        return valor != TipoDocumento.TRASLADO

    @staticmethod
    def es_venta(columna):
        """Expresión SQL: la fila es de venta — todo lo que no es traslado.

        `IS NULL` explícito: en SQL `NULL != 'TRASLADO'` es NULL, no verdadero,
        y la fila se perdería del filtro.
        """
        return or_(columna.is_(None), columna != TipoDocumento.TRASLADO)

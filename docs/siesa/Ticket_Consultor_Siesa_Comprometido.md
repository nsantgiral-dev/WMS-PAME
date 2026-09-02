# Ticket para consultor Siesa — Stock comprometido/reservado por bodega

## Contexto

El WMS de Papelería Medellín usa la consulta dinámica de Connekta
**`papeleriamedellin_WMS_Stock_Bodega`** para mostrar el stock disponible
en la pantalla de Traslados (cuando una tienda pide mercancía a otra bodega).

## Problema detectado

La consulta actual solo trae existencia física, sin descontar lo que ya
está comprometido o en proceso de salida. Esto hace que el WMS muestre
como "disponible" una cantidad que en realidad puede estar parcial o
totalmente reservada por otro movimiento, permitiendo que dos solicitudes
distintas "vean" el mismo stock y ambas lo pidan.

## Evidencia — SQL actual de la consulta (capturado en Connekta QA)

```sql
SELECT RTRIM(f150_id) AS f150_id, RTRIM(f120_referencia) AS f120_referencia,
       f400_cant_existencia_1
FROM V400_ITEM_REF
WHERE f400_id_cia = 1 AND f400_cant_existencia_1 > 0
```

Como se ve, la consulta solo lee `f400_cant_existencia_1` (existencia)
de `V400_ITEM_REF` — no hay ningún JOIN a una tabla de compromisos o
reservas. Por eso el "disponible" que hoy calcula el WMS es, en la
práctica, igual a la existencia física, sin ningún descuento real.

## Lo que necesitamos

Agregar a esta consulta (o a una nueva, si no se puede modificar la
existente) dos columnas adicionales, agregadas **por bodega + producto**
(no por pedido individual):

1. **Cantidad comprometida** — inventario ya reservado por pedidos de
   venta, traslados en curso u otros movimientos pendientes, que aún no
   ha salido físicamente de la bodega.
2. **Cantidad en salida sin confirmar** — inventario que ya salió de la
   bodega (ej. documento de tránsito emitido) pero cuya entrada en el
   destino aún no se ha confirmado.

## Preguntas puntuales para el consultor

1. ¿Existe en Siesa una tabla o vista que exponga, **agregado por bodega
   y por referencia de producto** (no por pedido/rowid específico), la
   cantidad comprometida y la cantidad en salida sin confirmar? Sabemos
   que existe `t405_cm_compromisos` (usada para compromisos de pedidos
   individuales vía `f405_cant_por_remisionar_base`), pero necesitamos el
   agregado a nivel bodega, no el detalle por pedido.
2. Si existe, ¿se puede hacer `JOIN` de esa tabla/vista con
   `V400_ITEM_REF` usando `f150_id` (bodega) + `f120_referencia`
   (código producto) como llave?
3. Confirmar el nombre exacto de las columnas resultantes, para que el
   WMS las mapee correctamente (evitar otra ronda de "verificar en
   Siesa" — necesitamos la captura real de la tabla/vista, no una
   referencia de memoria).
4. ¿Este mismo cambio aplica igual en el ambiente de producción
   (`servicios.siesacloud.com`) o hay que replicarlo por separado, como
   pasó con otras consultas custom del WMS?

## Nota interna (no enviar al consultor)

Una vez confirmados los nombres de columna reales, el cambio en el WMS
es acotado a una sola función: `_descargar_una_pasada_custom()` en
`app/services/inventario_siesa_service.py:220-232` — reemplazar los
valores hardcodeados `'comprometido': 0.0, 'salida_sin_conf': 0.0` por
la lectura real de las nuevas columnas. No toca el flujo de negocio de
traslados (`traslado_service.py`, `routes/traslados.py`,
`models/traslado.py`), que ya calcula correctamente
`disponible = existencia - comprometida - salida_sin_conf` una vez que
reciba datos reales.

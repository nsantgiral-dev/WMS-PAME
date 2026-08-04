# `picker_traslado` — picking de traslados entre sedes

> **En una frase:** prepara la mercancía que va de una sede a otra. Mismo gesto
> que el picking de venta, **otro documento y otro riesgo.**

---

## Configuración del usuario

| Campo | Valor |
|---|---|
| `rol` | `picker_traslado` |
| `almacen_id` | **obligatorio** — la sede de ORIGEN |

---

## Dónde opera

**CO 003 — Bodega CD (`NB1`)** principalmente, que es de donde sale casi todo.
También puede haber traslados entre puntos de venta.

---

## En qué se diferencia del picking normal

| | Picking de venta | Picking de traslado |
|---|---|---|
| Destino | Un cliente | **Otra sede de la empresa** |
| Documento | Remisión + factura | **Tránsito de salida (STS)** |
| Si falta | Se despacha parcial | Se despacha parcial, pero **la otra sede lo está esperando** |
| Riesgo | El cliente reclama | **El inventario queda en el limbo entre dos bodegas** |

**El riesgo propio de este rol:** un traslado mal preparado deja producto contado
en dos bodegas a la vez, o en ninguna. Es el error más difícil de encontrar
después, porque el total de la empresa cuadra.

---

## El día

```
1. Tomás una solicitud de traslado ya aprobada
2. Picás igual que un pedido normal
3. Confirmás  →  pasa a packer_traslado
```

---

## Los relevos

| | De quién | A quién |
|---|---|---|
| Entrada | De **gestión**, que aprobó la solicitud de la tienda | |
| Salida | | **Al `packer_traslado`** |

---

## Lo que NO podés hacer, y por qué

**No podés picar una solicitud sin aprobar.** El traslado mueve inventario entre
centros de costo: es una decisión de gestión, no operativa.

**No podés cambiar qué se traslada.** Si falta, se traslada parcial y queda
registrado. Sustituir por otro producto rompe el cruce con el documento de Siesa.

---

## Cuando algo falla

### No hay suficiente para el traslado completo
Confirmá lo que hay. La sede destino ve el parcial y sabe qué esperar.
**Avisale a quien aprobó** — puede ser que prefiera esperar el completo.

### El producto está en RESERVA y no en PICKING
Necesita reposición. Avisá al abastecedor (`puede_abastecer`). No lo saques de
reserva por tu cuenta: la ubicación es parte del dato.

---

## Qué queda registrado

La solicitud con tu nombre en cada ítem picado, y el documento de tránsito de
salida en Siesa.

---

## Primera semana

| Día | Qué hace |
|---|---|
| 1 | Observa. **Se le explica el limbo entre bodegas.** |
| 2 | Pica un traslado acompañado |
| 3 | Fallos: faltante, producto en reserva |
| 4-5 | Solo |

---

## Cómo crecer

**Siguiente paso: `packer_traslado`**, y después `jefe_almacen`.

Qué demostrar: 30 días sin traslados que la sede destino reporte incompletos.

---
*Última revisión: 2026-08-04*

# `recepcionista` — recepción de compras

> **En una frase:** recibe lo que llega del proveedor **contando a ciegas**, y su
> confirmación registra la entrada en Siesa.

---

## Configuración del usuario

| Campo | Valor |
|---|---|
| `rol` | `recepcionista` |
| `almacen_id` | **obligatorio** |

Grupo de permisos: `RECEPCION_ROLES` = admin · jefe_almacen · recepcionista.

---

## Dónde opera

**CO 003 — Bodega CD (`NB1`).** Es el único punto que recibe de proveedores.

---

## El escaneo ciego, y por qué

**La pantalla no te muestra cuánto dice la orden de compra hasta que terminás de
contar.**

No es una molestia de diseño: es la única forma de que el conteo signifique algo.
Si ves "50" antes de contar, tu cabeza cuenta hasta 50 y para. Si contás a ciegas
y da 47, encontraste un faltante que de la otra forma no existía.

**Un faltante detectado en recepción se le cobra al proveedor. Uno detectado tres
semanas después, no.**

---

## El día

```
1. Llega el proveedor con la mercancía
2. Abrís la OC en el sistema
3. Escaneás y contás — SIN ver la cantidad esperada
4. Confirmás
5. El sistema compara y te muestra las diferencias
6. Confirmás la entrada  →  142948 registra la entrada en Siesa
```

---

## Los relevos

| | De quién | A quién |
|---|---|---|
| Entrada | Del proveedor (físico) y de compras (la OC en Siesa) | |
| Salida | | **Al inventario** — a partir de acá el producto existe para picking |

---

## Lo que NO podés hacer, y por qué

**No podés ver la cantidad esperada antes de contar.** Ver arriba.

**No podés recibir sin OC.** Si el proveedor trae algo sin orden, no entra al
sistema. Eso es una **fuga de recompra** y la registra compras — es exactamente
lo que el módulo de bloqueos existe para detectar.

**No podés confirmar una entrada dos veces.** El sistema tiene guardas, pero una
entrada duplicada en Siesa es inventario que no existe.

---

## Cuando algo falla

### Llegó menos de lo que dice la OC
Confirmá **lo que contaste**. El sistema registra la diferencia y queda como
soporte para el reclamo al proveedor.

### Llegó producto que no está en la OC
No lo recibas en el sistema. Avisá a compras. Puede ser un error del proveedor o
una compra por fuera del sistema — las dos cosas hay que saberlas.

### El código no está en el catálogo
Producto nuevo sin dar de alta. Avisá a compras **antes** de recibirlo: si entra
sin código, no se puede picar después.

### Siesa está caído
La entrada queda en cola y se procesa sola. El producto **ya está en la bodega**
del WMS — el pendiente es el registro contable, no el físico.

---

## Qué queda registrado de tu trabajo

Cada conteo con tu nombre y la hora. Las diferencias que reportaste son el
soporte de los reclamos a proveedor.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Dónde se ve |
|---|---|
| Diferencias detectadas en recepción vs en conteo cíclico | Si aparecen después, se escaparon acá |
| Tiempo de recepción por OC | Panel de recepción |
| Productos sin código detectados a tiempo | Compras |

---

## Primera semana

| Día | Qué hace | Se verifica con |
|---|---|---|
| 1 | Observa. **Se le explica el escaneo ciego y su motivo.** | Puede explicar por qué no ve la cantidad |
| 2 | Recibe acompañado | 3 OC sin ayuda |
| 3 | **Fallos provocados**: faltante, producto sin OC, código desconocido | Resuelve 3 de 4 |
| 4 | Solo, con alguien cerca | Sin errores de conteo |
| 5 | Turno completo | |

---

## Cómo crecer

**Siguiente paso: `jefe_almacen`** — el que ya no reporta las diferencias sino
que decide qué hacer con ellas.

Qué hay que demostrar:
- 30 días sin diferencias que aparezcan después en conteo cíclico
- sabe cuándo NO recibir
- puede explicar el escaneo ciego a alguien nuevo sin que suene a burocracia

---
*Última revisión: 2026-08-04*

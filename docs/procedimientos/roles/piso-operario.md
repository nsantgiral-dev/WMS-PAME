# `operario` — picking y conteo

> **En una frase:** recoge lo que el sistema le indica, en el orden que le
> indica, y **reporta lo que no cuadra en vez de resolverlo por su cuenta.**

---

## Configuración del usuario

Quien crea la cuenta tiene que marcar esto. No es opcional ni obvio:

| Campo | Valor | Qué habilita |
|---|---|---|
| `rol` | `operario` | La pantalla de operario |
| `puede_picar` | ✅ (viene en ✅ por defecto) | Tomar tareas de picking |
| `puede_empacar` | ⬚ decisión | Si además empaca — ver ficha de `empacador` |
| `puede_abastecer` | ⬚ decisión | Si además hace reposición RESERVA→PICKING |
| `almacen_id` | **obligatorio** | Sin esto no ve tareas |

> **Un operario sin `almacen_id` entra y no ve nada.** No da error: la lista sale
> vacía. Es el fallo de alta más común y el más confuso.

---

## Dónde opera

**CO 003 — Bodega CD (`NB1`).** Es donde está el inventario físico que se pica.

Los puntos de venta (NS1, NC1, PC1, FC1) no tienen picking: reciben producto,
no lo preparan.

---

## Qué ve al entrar

Una lista de **sus** tareas — no las de todos. Cada tarea trae:

- el producto y la cantidad
- la ubicación exacta (pasillo, estante, nivel)
- el orden de recorrido, ya calculado

Y el escáner. Nada más.

---

## El día

```
1. Toma la siguiente tarea de la lista
2. Camina a la ubicación que indica
3. Escanea el producto  → el sistema confirma que es el correcto
4. Cuenta y confirma la cantidad
5. Deja el bulto en la zona de packing
6. Siguiente
```

**No elige qué picar.** El orden lo calcula el sistema por recorrido y prioridad.
Saltarse el orden hace caminar de más a todos los demás.

---

## Los relevos

| | De quién | A quién |
|---|---|---|
| Picking | Del sistema (sync de pedidos de Siesa) | **Al empacador**, dejando el bulto en zona de packing |
| Conteo | Del generador ABC (2 a.m.) | **Al supervisor**, si hay descuadre |

**El relevo al empacador es físico.** Si el bulto queda en el lugar equivocado,
el empacador no lo encuentra y el pedido se atrasa sin que nadie sepa por qué.

---

## Lo que NO podés hacer, y por qué

**No podés aprobar tu propio ajuste de inventario.**
Contás, reportás la diferencia, y **otra persona la aprueba**. Si quien cuenta y
quien aprueba son la misma persona, el conteo no significa nada: cualquier
faltante se puede cerrar solo. Es lo único que hace que el número valga.
> En pantalla: *"Solo un supervisor o admin puede aprobar ajustes de inventario"*

**No podés cambiar la cantidad de una tarea ya confirmada.**
Se corrige con un conteo, no editando el registro. Un número que se puede editar
después no prueba nada de lo que pasó.

**No podés ver ni tocar tareas de otro operario.**
No es desconfianza: es que dos personas sobre la misma tarea generan doble
descuento de inventario.

---

## Cuando algo falla

### El escáner no lee el código
1. Limpiá el código y probá de nuevo, con más luz.
2. Si sigue sin leer, **buscá el producto por código en la app** y confirmá que
   sea el mismo antes de tocar nada.
3. Reportá el código ilegible al jefe de almacén — una etiqueta mala hoy son
   veinte errores esta semana.

**Nunca confirmes una tarea con un producto que no escaneaste.** Si te
equivocás, el error viaja hasta el cliente y ya nadie lo puede rastrear.

### El sistema dice que no hay stock y vos lo tenés en la mano
Eso es un **descuadre**, y es información valiosa, no un estorbo.
1. **No lo tomes igual.** Registrá lo que encontraste.
2. Avisá al supervisor.
3. Va a entrar al conteo cíclico.

El sistema está mal y vos lo detectaste. Ese es el trabajo.

### Hay menos producto del que pide la tarea
Confirmá **lo que realmente hay**, no lo que pide. El sistema está preparado para
un pedido parcial; no está preparado para un número inventado.

### Se cae la señal
La app guarda tu trabajo y lo sincroniza cuando vuelve. Vas a ver el aviso de
*"trabajando con datos en caché"*.
**Seguí trabajando.** No repitas tareas ya confirmadas — se duplican al
sincronizar.

---

## Qué queda registrado de tu trabajo

Cada tarea guarda **quién**, **cuándo** y **cuánto**. Eso no es vigilancia: es tu
respaldo.

Si aparece un faltante de un producto que vos confirmaste bien, el registro dice
que lo hiciste bien. Si no registraste nada, no dice nada — y ahí es cuando la
conversación se vuelve sobre tu palabra.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Dónde se ve |
|---|---|
| Tareas completadas sin reabrir | Panel de operarios |
| Descuadres que reportaste vos | Conteo cíclico |
| Tareas que quedaron EN_PROCESO más de 2 h | El sistema las libera solo — si te pasa seguido, algo está mal |

**El mejor operario no es el más rápido: es el que reporta más descuadres.** Los
descuadres existen igual; la diferencia es si se detectan hoy o en el inventario
de fin de año.

---

## Primera semana

| Día | Qué hace | Se verifica con |
|---|---|---|
| 1 | Observa a un operario con experiencia. Recorre la bodega. | Puede decir de quién recibe y a quién entrega |
| 2 | Pica acompañado | 5 tareas sin ayuda verbal |
| 3 | **Provocamos los fallos**: código ilegible, stock que no cuadra, señal caída | Resuelve 3 de 4 sin llamar |
| 4 | Solo, con alguien cerca | 4 horas sin error de cantidad |
| 5 | Turno completo solo | Cierra sin tareas colgadas |

---

## Cómo crecer

**Siguiente paso: `empacador`** (o el flag `puede_empacar` sobre el mismo rol).

Qué hay que demostrar:
- 30 días sin errores de cantidad
- resuelve los cuatro fallos del día 3 sin llamar
- **puede explicarle el relevo a alguien nuevo** — enseñarlo es la prueba de que
  se entendió

**Después:** `picker_traslado` (traslados entre sedes, más criterio) o
`supervisor` (aprueba ajustes, ya no los reporta).

---
*Última revisión: 2026-08-04 · Permisos verificados contra `app/routes/_auth_helpers.py`*

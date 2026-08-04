# `tienda` — punto de venta

> **En una frase:** vende, solicita reposición a la bodega, y recibe lo que llega
> por traslado.

---

## Configuración

| Campo | Valor | Nota |
|---|---|---|
| `rol` | `tienda` | |
| `nombre_punto_venta` | **obligatorio** | Ej. `Neiva Centro` |
| `bodega_siesa_id` | **obligatorio** | Ej. `NC1` |
| `almacen_id` | **obligatorio** | |

> **Sin bodega configurada, la pantalla no funciona.**
> En pantalla: *"Sin permiso — se requiere rol tienda con bodega configurada"*.
> Es el error de alta más común de este rol.

---

## Dónde opera

| CO | Sede | Bodega |
|---|---|---|
| 001 | Neiva Sur | `NS1` |
| 002 | Neiva Centro | `NC1` |
| 004 | Pitalito Centro | `PC1` |
| 006 | Florencia Centro | `FC1` |
| 005 | Pitalito Terminal | ⬚ *sin fila en `almacenes`* |
| 007–009 | Ferias | ⬚ *sin fila* |

---

## Lo que hacés

```
· Consultás stock de tu punto
· Solicitás traslado a la bodega CD cuando falta producto
· Recibís el traslado cuando llega  → confirma la entrada en Siesa
· Registrás órdenes de compra de tienda
```

---

## El traslado: cómo funciona de tu lado

```
1. Solicitás  →  queda PENDIENTE
2. Gestión aprueba (o ajusta cantidades)
3. La bodega prepara y despacha  →  el producto queda EN TRÁNSITO
4. Llega  →  VOS confirmás la recepción
5. Recién ahí el inventario es tuyo
```

**El paso 4 es tuyo y nadie lo puede hacer por vos.** Entre el despacho y tu
confirmación, el producto no está en ninguna bodega — está en tránsito. Un
traslado que llega y no se confirma es inventario que la empresa tiene y ningún
sistema muestra.

---

## Lo que NO podés hacer, y por qué

**No podés aprobar tu propia solicitud de traslado.** Mueve inventario entre
centros de costo: es una decisión de gestión.

**No podés ajustar tu inventario.** Si no cuadra, va a conteo.

**No ves las otras sedes.** Tu alcance es tu bodega.

---

## Cuando algo falla

### Llegó menos de lo que pediste
Confirmá **lo que llegó**. La diferencia queda registrada y es lo que se
reclama. Confirmar de más para "que cuadre" hace que el faltante desaparezca del
registro y aparezca en tu inventario.

### El traslado no llega
Aparece en el monitor. Avisá — un tránsito colgado es inventario invisible.

### Necesitás algo urgente
Se solicita igual. Pedirlo por fuera del sistema genera una **fuga de recompra**
que aparece en el panel de compras, y nadie sabe reponerlo después.

---

## Qué queda registrado

Tus solicitudes, tus confirmaciones de recepción y tus ventas, con hora.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice |
|---|---|
| Traslados confirmados el mismo día que llegan | |
| Agotados en tu punto | |
| Diferencias entre lo despachado y lo confirmado | |

---

## Primera semana

| Día | Qué hace |
|---|---|
| 1 | Recorre la tienda y entiende de dónde viene el producto |
| 2 | Solicita un traslado acompañado |
| 3 | Recibe y confirma un traslado |
| 4 | **Fallos**: llega de menos, no llega |
| 5 | Turno completo |

---

## Cómo crecer

**Siguiente: `jefe_almacen`** de su sede, o **`compras`** si el interés es
abastecimiento.

Qué demostrar: 30 días confirmando traslados el mismo día, y sabe explicar qué
significa "en tránsito".

---
*Última revisión: 2026-08-04*

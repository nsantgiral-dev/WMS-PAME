# `packer_traslado` — verificación y despacho de traslados

> **En una frase:** verifica lo que preparó el picker y **con su confirmación el
> inventario sale de una bodega y entra en tránsito.**

---

## Configuración del usuario

| Campo | Valor |
|---|---|
| `rol` | `packer_traslado` |
| `almacen_id` | **obligatorio** — sede de origen |

---

## Lo que hace tu confirmación

```
Confirmás
    ↓
174930 / 173076  →  TRÁNSITO DE SALIDA en Siesa
    ↓
El inventario SALE de la bodega origen y queda "en tránsito"
    ↓
No entra a destino hasta que allá confirmen la recepción
```

**Entre tu confirmación y la recepción en destino, el producto no está en ninguna
bodega — está en tránsito.** Si ese estado se queda colgado, hay inventario que
la empresa tiene y ningún sistema muestra.

Por eso importa que el traslado llegue y se confirme del otro lado.

---

## El día

```
1. Tomás un traslado ya picado
2. Verificás ítem por ítem
3. Armás y rotulás
4. Confirmás  →  tránsito de salida
5. Coordinás el envío
```

---

## Los relevos

| | De quién | A quién |
|---|---|---|
| Entrada | Del `picker_traslado` | |
| Salida | | **A la sede destino** — que tiene que confirmar la entrada |

**Este relevo cruza sedes.** Es el único de la operación donde la persona que
recibe no está en el mismo edificio. Si no avisás, nadie sabe que va en camino.

---

## Lo que NO podés hacer, y por qué

**No podés confirmar sin verificar.** Sos el último control antes de que salga
del edificio.

**No podés cancelar con el tránsito ya generado.** Siesa ya movió el inventario.
Se revierte con un documento, no borrando.

---

## Cuando algo falla

### El picker preparó de menos
Verificá contra la solicitud, no contra lo que hay en la caja. Confirmá lo real.

### Siesa está caído
Queda en cola (DLQ) y se reintenta. **No vuelvas a confirmar** — un tránsito
duplicado saca el inventario dos veces.

### El traslado no llega a destino
Después de un tiempo aparece en el monitor de traslados. Avisá a gestión: un
tránsito colgado es inventario invisible.

---

## Qué queda registrado

Tu nombre en el traslado y en el documento de tránsito de Siesa.

---

## Cómo crecer

**Siguiente paso: `jefe_almacen`.**

Qué demostrar: 30 días sin tránsitos colgados, y sabe explicar qué significa
"en tránsito" para el inventario.

---
*Última revisión: 2026-08-04*

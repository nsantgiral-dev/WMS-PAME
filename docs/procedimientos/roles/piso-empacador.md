# `empacador` — packing y cierre del pedido

> **En una frase:** verifica que lo que se picó es lo que el cliente pidió, lo
> empaca en bultos, y **con su confirmación se genera la factura en Siesa.**

---

## Configuración del usuario

| Campo | Valor | Nota |
|---|---|---|
| `rol` | `empacador` | Pantalla de empacador |
| `puede_empacar` | ✅ | **También sirve marcarlo sobre un `operario`** |
| `almacen_id` | **obligatorio** | Sin esto no ve tareas |

> Hay dos formas de habilitar packing: el rol `empacador`, o el flag
> `puede_empacar` sobre un operario. La segunda sirve para quien hace las dos
> cosas según el día. **Decidí cuál usás y sé consistente** — si la mitad son rol
> y la mitad flag, nadie sabe a quién buscar.

---

## Dónde opera

**CO 003 — Bodega CD (`NB1`).**

---

## Lo que hace tu confirmación

Esto es lo más importante de esta ficha, y va primero por eso:

```
Confirmás el packing
      ↓
244328  →  compromete cantidades en Siesa
      ↓
142945  →  genera la REMISIÓN — descarga inventario
      ↓
142943  →  genera la FACTURA ELECTRÓNICA
```

**Tu confirmación emite una factura.** No es un paso administrativo: es el
momento en que el pedido se vuelve un documento legal con la DIAN.

Por eso el sistema no te deja cancelar una tarea si Siesa ya generó la remisión.
> En pantalla: *"No se puede cancelar — Siesa ya generó la remisión"*

---

## El día

```
1. Tomás la siguiente tarea de packing
2. Verificás producto por producto contra el pedido
3. Armás los bultos y los rotulás
4. Confirmás  → se emite la factura
5. El bulto pasa a muelle
```

---

## Los relevos

| | De quién | A quién |
|---|---|---|
| Entrada | **Del operario de picking**, físicamente en zona de packing | |
| Salida | | **A muelle / ruta** (jefe de almacén o supervisor) |

**Este es el relevo que más se rompe** en toda la operación. Un bulto rotulado a
medias o dejado fuera de zona no lo encuentra nadie, y el pedido desaparece sin
que ningún sistema avise.

---

## Lo que NO podés hacer, y por qué

**No podés cancelar una tarea con la remisión ya generada.**
Siesa ya descargó el inventario y emitió el documento. Cancelar del lado del WMS
dejaría los dos sistemas diciendo cosas distintas — y esa inconsistencia no se
detecta automáticamente.
Si hay que revertir, es una **nota crédito**, y la hace liquidación.

**No podés cancelar si hay un job de Siesa en curso.**
> En pantalla: *"No se puede cancelar — hay un job Siesa PROCESANDO"*

**No podés cambiar cantidades.** Si falta producto, se despacha parcial y el
sistema lo maneja. Ajustar el número a mano rompe el cruce con la factura.

---

## Cuando algo falla

### Falta producto respecto al pedido
Se despacha **parcial**. El sistema está hecho para eso: compromete solo lo que
va y el resto queda pendiente. **No completes con un producto parecido.**

### La confirmación se queda pensando
Está hablando con Siesa. **No toques dos veces.**
Un segundo envío puede generar una segunda factura, y una factura duplicada es
un problema contable que dura semanas.
Si pasa un minuto, avisá al supervisor — el trabajo queda en la cola y se
reintenta solo.

### Siesa está caído
La tarea queda en cola (DLQ) y se procesa cuando vuelva. **No es un error tuyo.**
Se reintenta a los 5, 15 y 45 minutos.
Si son más de tres intentos, aparece en el panel del supervisor.

### Un bulto quedó sin rotular
Rotulalo antes de moverlo. Un bulto sin etiqueta en muelle es un bulto perdido:
nadie sabe de qué pedido es.

---

## Qué queda registrado de tu trabajo

Tu nombre queda en la tarea, en cada bulto, y **en la factura de Siesa**.
Es el registro más formal de toda la operación: si un cliente reclama que le
faltó un ítem, lo que hay para responder es lo que vos verificaste.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Dónde se ve |
|---|---|
| Tareas cerradas sin reabrir | Panel de packing |
| Bultos por tarea (consistencia) | Panel de packing |
| Devoluciones por ítem faltante | Liquidación — es la señal más dura |
| Jobs de Siesa fallidos por datos | Panel DLQ |

---

## Primera semana

| Día | Qué hace | Se verifica con |
|---|---|---|
| 1 | Observa. **Se le explica que su confirmación emite una factura.** | Puede explicar por qué no se cancela después |
| 2 | Empaca acompañado | 5 pedidos verificados sin ayuda |
| 3 | **Fallos provocados**: pedido parcial, Siesa lento, bulto sin rótulo | Resuelve 3 de 4 sin llamar |
| 4 | Solo, con alguien cerca | Sin errores de verificación en 4 h |
| 5 | Turno completo | Cierra sin bultos huérfanos |

---

## Cómo crecer

**Siguiente paso: `packer_traslado`** (verificación de traslados entre sedes) o
**`supervisor`**.

Qué hay que demostrar:
- 30 días sin faltantes reportados por cliente
- entiende la cadena 244328 → 142945 → 142943 y puede explicar qué pasa si se
  interrumpe
- resuelve un pedido parcial sin preguntar

---
*Última revisión: 2026-08-04 · Permisos verificados contra `app/routes/_auth_helpers.py`*

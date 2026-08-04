# `gerente` — decisión comercial y de capital

> **En una frase:** ve el negocio completo y decide sobre plata — compras,
> traslados, capital inmovilizado. **No opera inventario.**

---

## Configuración

| Campo | Valor |
|---|---|
| `rol` | `gerente` |

---

## Lo que SÍ podés

| Facultad | Grupo |
|---|---|
| Aprobar traslados entre sedes | `DESPACHO` |
| Compras: acuerdos marco, precios, armador de contenedores | `COMPRAS_ROLES` |
| Capital inmovilizado y desbloqueo de SKUs | `COMPRAS_ROLES` |
| Tablero del Vigía (alarmas de desplome operativo) | `GESTION` |
| Ver todos los paneles | `GESTION` |

---

## Lo que NO podés, y por qué

Esto sorprende a la mayoría, así que va explícito:

**No podés ajustar inventario directamente** (`ALMACEN` = admin + jefe_almacen).
**No podés aprobar ajustes de conteo cíclico** (`LEAD` = admin + supervisor).

No es una limitación de jerarquía: es separación de funciones. El inventario lo
audita quien no lo maneja, y lo maneja quien no lo audita. Un cargo que puede
hacer las dos cosas rompe el control por definición, sin importar quién lo ocupe.

Si necesitás corregir un número, va por el conteo. Esa fricción es el control.

---

## Lo que mirás, y qué te está diciendo

| Panel | La pregunta que responde |
|---|---|
| **Capital inmovilizado** | ¿Cuánta plata hay en SKUs que no rotan hace 12 meses? |
| **Fugas de recompra** | ¿Se está comprando por fuera del sistema? |
| **Armador de contenedores** | ¿Qué conviene traer de China y cuándo? |
| **Vigía** | ¿Alguna sede se está desplomando sin que nadie lo diga? |
| **Deriva de precios** | ¿Los precios de proveedor se movieron respecto al acuerdo? |

**El Vigía es el que menos se mira y el que más vale.** Detecta caídas
sostenidas en facturación, líneas o frecuencia por centro de operación —
señales que en el día a día se explican una por una y solo se ven juntas.

---

## Un dato sensible que hoy ve el rol `compras`

Desde 2026-08-04, el rol `compras` accede a **valor FOB, valor nacionalizado y
proveedor** de los contenedores de importación. Se abrió porque su propia
pantalla los consume y estaba rota sin ellos.

⬚ **Decisión pendiente:** ratificar o revertir. Es sobre a quién le mostrás
márgenes de importación.

---

## Cuando algo falla

### El capital inmovilizado sube mes a mes
No es un problema de compras: es que nadie está bloqueando la recompra de lo que
no rota. La lista de bloqueados existe para eso.

### Una alarma del Vigía en un CO
Antes de pedir explicaciones, mirá si hay alarmas en otros CO el mismo período.
Una caída general es mercado; una sola es local.

### Aparecen fugas de recompra
Alguien compró por fuera del sistema. Puede ser urgencia legítima o falta de
proceso. Los dos casos se resuelven distinto, y no saber cuál es lo peor.

---

## Cómo crecer

Este rol no tiene "siguiente". Lo que crece es la calidad de las preguntas:
pasar de *"¿por qué bajó la venta?"* a *"¿desde qué semana, en qué CO, y qué más
cambió esa semana?"* — que es exactamente lo que el Vigía responde.

---
*Última revisión: 2026-08-04*

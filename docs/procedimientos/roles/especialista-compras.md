# `compras` — abastecimiento y capital

> **En una frase:** decide qué se compra, a quién y cuándo, con los datos de
> rotación y capital inmovilizado a la vista.

---

## Configuración

| Campo | Valor |
|---|---|
| `rol` | `compras` |

Grupo: `COMPRAS_ROLES` = admin · jefe_almacen · gerente · **compras**

---

## Qué ve al entrar

Pantalla propia de compras, con:

| Panel | Qué responde |
|---|---|
| **Acuerdos marco** | Qué se negoció, con quién, hasta cuándo |
| **Comparador de precios** | Qué proveedor está más caro para el mismo ítem |
| **Deriva de precios** | Qué se movió respecto al acuerdo |
| **Armador de contenedores** | Qué conviene traer de China y cuándo |
| **ROP dual** | Punto de reorden nacional (LT 5 d) y China (LT 105 d) |
| **Capital inmovilizado** | Plata en SKUs sin rotación en 12 meses |
| **Fugas de recompra** | Compras hechas por fuera del sistema |
| **Kardex / clasificación S-B** | Rotación por SKU |

---

## Un cambio reciente que tenés que saber

Hasta el 2026-08-04, **este rol no podía usar la mitad de su propia pantalla**:
armador, bloqueos y kardex tenían listas de permisos propias que no incluían a
`compras`. Se abría el panel y los bloques salían vacíos.

Se corrigió. Ahora ves también **valor FOB, valor nacionalizado y proveedor** de
los contenedores.

**Ratificado por gerencia el 2026-08-04:** este rol ve **todo lo de compras**,
incluidos los valores de importación. El criterio es simple y conviene tenerlo
escrito: *quien decide qué comprar necesita ver el costo real de traerlo.* Un
comprador que no ve el FOB ni el nacionalizado está decidiendo sobre el precio
de lista, que no es el costo.

---

## Lo que NO podés hacer, y por qué

**No podés ajustar inventario ni aprobar conteos.** Comprás sobre lo que el
inventario dice; si además pudieras cambiarlo, la decisión de compra se
justificaría sola.

**No podés recibir mercancía.** Eso es recepción, y el escaneo es ciego a
propósito: quien compró no cuenta lo que llegó.

---

## Las tres decisiones que tomás, y qué mirar

### 1 · Qué reponer
**ROP dual.** El nacional se repone con 5 días de lead time; el de China, 105.
Un SKU de China que se pide tarde no llega en el trimestre.

### 2 · Qué NO recomprar
**Capital inmovilizado.** Un SKU con stock y sin picks en 12 meses es plata
quieta. La lista de bloqueados existe para que no se recompre por inercia.

> **Regla de fondo del sistema (regla 0):** ante dato ausente, fallar hacia lo
> conservador **y declararlo**. El motivo no es que el faltante cueste menos —
> para la canasta constitucional el agotado es carísimo, Florencia lo probó. Es
> la **reversibilidad**: un sub-pedido lo corrige un humano mañana; un contenedor
> embarcado es irreversible 120 días.

### 3 · Qué traer en el contenedor
**Armador.** Ordena por margen por metro cúbico, respetando el CBM objetivo.

---

## Cuando algo falla

### Aparece una fuga de recompra
Alguien compró por fuera. Puede ser urgencia legítima o falta de proceso — los
dos se resuelven distinto, y no saber cuál es lo peor de los tres escenarios.

### La deriva de precios se dispara
El proveedor se movió del acuerdo. Puede ser insumo, cambio o renegociación
silenciosa.

### Un SKU bloqueado se sigue pidiendo
El bloqueo es informativo, no impide la compra. Si se ignora, el problema es de
proceso, no de sistema.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice |
|---|---|
| **Capital inmovilizado** (tendencia) | La medida más dura |
| Fugas de recompra por mes | Debería tender a cero |
| Agotados en canasta constitucional | El otro lado de la balanza |
| Contenedores con relleno bien aprovechado | CBM real vs objetivo |

**Las dos primeras se contradicen a propósito.** Si el capital inmovilizado baja
mucho y los agotados suben, se compró de menos. La gracia es sostener las dos.

---

## Cómo crecer

**Siguiente: `gerente`.**

Qué demostrar: bajó capital inmovilizado **sin subir agotados** en 90 días, y
puede explicar por qué la regla 0 prioriza reversibilidad y no costo.

---
*Última revisión: 2026-08-04*

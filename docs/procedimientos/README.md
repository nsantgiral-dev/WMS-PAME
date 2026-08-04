# Procedimientos por rol — WMS Papelería Medellín

Esto no es un manual de botones. Un manual de botones se lee una vez, se guarda
en una carpeta y nadie lo abre cuando de verdad hace falta.

Lo que sigue está escrito para tres momentos concretos:

1. **Cuando alguien entra** — su primera semana, día por día.
2. **Cuando algo falla a las 5 a.m.** — qué hacer sin llamar a nadie.
3. **Cuando alguien quiere crecer** — qué tiene que demostrar para el siguiente rol.

---

## La regla que sostiene todo esto

> **Ningún procedimiento puede prometer lo que el sistema niega, ni permitir lo
> que el sistema impide.**

Si el papel dice *"el supervisor de flota autoriza reparaciones"* y el sistema le
devuelve 403, la persona aprende dos cosas el primer día: que el documento miente
y que los errores se ignoran. A partir de ahí, ningún procedimiento vale.

Por eso cada ficha de rol lleva **los permisos exactos y los mensajes de error
reales** que esa persona va a ver en pantalla. Cuando cambien en el código,
cambian acá — y hay tests que fallan si un endpoint queda sin control de rol.

---

## El sistema autoriza en TRES capas

La mayoría de los procedimientos fallan porque solo documentan la primera.

| Capa | Qué decide | Campo | Quién lo configura |
|---|---|---|---|
| **Rol** | Qué pantalla abre al entrar | `usuarios.rol` | admin |
| **Capacidades** | Qué tareas puede tomar *dentro* de esa pantalla | `puede_picar`, `puede_empacar`, `puede_abastecer` | admin |
| **Alcance** | Sobre qué sede y bodega opera | `almacen_id`, `bodega_siesa_id` | admin |

**Consecuencia práctica:** *"operario"* no es un cargo. Es una pantalla con tres
interruptores. Dos personas con el mismo rol pueden hacer trabajos distintos, y
quien crea el usuario tiene que saber cuáles marcar.

### Qué habilita cada capacidad, de verdad

| Flag | Por defecto | Qué habilita |
|---|---|---|
| `puede_picar` | ✅ **sí** | Tomar tareas de picking. Y **solo con este flag se puede hacer el primer conteo (CC1)** del conteo cíclico |
| `puede_empacar` | ❌ no | **Todo lo que hace un empacador.** El código los trata igual: `_puede_empacar()` = rol `empacador` **o** este flag |
| `puede_abastecer` | ❌ no | Reposición **RESERVA → PICKING** |

### La combinación decide qué pantalla se abre

| picar | empacar | abastecer | Al entrar ve |
|:---:|:---:|:---:|---|
| ✅ | ❌ | ❌ | Operario — picking |
| ❌ | ✅ | ❌ | **Empacador, directo** |
| ❌ | ❌ | ✅ | **Reposición, directo** |
| ✅ | ✅ | ❌ | Operario (empaca cuando se lo asignan) |
| ✅ | ❌ | ✅ | Operario **con botón para cambiar** a reposición |
| ❌ | ❌ | ❌ | Operario **sin nada que hacer** ← la trampa |

### Criterio para marcarlos

> **`puede_empacar` no es "puede ayudar en packing".**
>
> Es la facultad de **emitir una factura electrónica**: confirmar el packing
> dispara `244328 → 142945 → 142943` y genera el documento fiscal.
>
> Marcárselo a alguien porque hoy falta gente es darle la llave de la
> facturación. **Es un ascenso, no una conveniencia.**

| Flag | Se marca cuando | Lo autoriza |
|---|---|---|
| `puede_picar` | Siempre, salvo empacador o abastecedor puro | jefe de almacén |
| `puede_empacar` | **30 días de picking sin errores de cantidad** + entiende que su confirmación factura | jefe de almacén |
| `puede_abastecer` | Conoce el layout y la diferencia reserva/picking | jefe de almacén |

**Se revisan al cambiar de puesto, no solo al ingresar.** Un flag que quedó
marcado de un reemplazo de hace seis meses es un permiso que nadie decidió.

---

## Centros de operación y sedes

| CO | Sede | Código bodega | Caja Siesa | Roles que operan ahí |
|---|---|---|---|---|
| 001 | Neiva Sur | `NS1` | 001 | tienda, operario, empacador |
| 002 | Neiva Centro | `NC1` | 004 | tienda, operario, empacador |
| **003** | **Bodega CD (centro de distribución)** | `NB1` | 999 | **todos los de piso + gestión** |
| 004 | Pitalito Centro | `PC1` | 999 | tienda |
| **005** | **Bodega Pitalito** | ⬚ *falta crear en `almacenes`* | 999 | hoy: almacenamiento · **meta: CDI** |
| 006 | Florencia Centro | `FC1` | 013 | tienda |
| 007–009 | Ferias | ⬚ *falta crear* | 999 | ⬚ *estacional* |

> **Hueco conocido:** los CO 005 y 007–009 no tienen fila en `almacenes`. Una
> custodia de vehículo o un traslado que termine ahí queda declarado como
> `pendiente_sede` — el sistema no inventa la sede, la reporta.

**El CO 003 es el corazón.** Picking, packing, recepción, traslados y flota
ocurren ahí. Las demás sedes son puntos de venta que consumen y solicitan.

### CO 005 — Bodega Pitalito: el segundo centro de distribución

Hoy es **solo almacenamiento**. La meta es convertirlo en **CDI — el próximo
003**, con su propia operación de picking, packing y despacho para el sur.

Eso cambia lo que hay que preparar, y conviene tenerlo claro desde ahora:

| Cuando 005 sea CDI | Qué implica |
|---|---|
| Necesita fila en `almacenes` con `centro_op_siesa = '005'` | Sin eso no se le puede asignar personal ni recibir traslados |
| Roles de piso propios: operario, empacador, recepcionista | Cada uno con `almacen_id` apuntando a 005, **no a 003** |
| Traslados 003 → 005 dejan de ser reposición de tienda | Pasan a ser abastecimiento entre centros |
| Su propia flota y custodia de vehículos | El módulo ya lo soporta: la custodia es por sede |
| Layout y ubicaciones propias | El recorrido de picking se calcula por almacén |

**Lo que NO cambia:** los procedimientos de esta carpeta. Un operario de 005 hace
lo mismo que uno de 003 — cambia el `almacen_id`, no el trabajo. Esa es la
prueba de que estos documentos están escritos al nivel correcto.

> **Riesgo a vigilar en la transición:** durante el período en que 005 recibe
> mercancía pero todavía no despacha, todo lo que le llega queda como inventario
> que nadie consume. Es el mismo estado "en tránsito" del que hablan las fichas
> de traslado, pero sostenido en el tiempo.

---

## Los cinco macroprocesos y sus relevos

Un relevo es el punto donde el trabajo deja de ser de una persona y pasa a ser
de otra. **Es donde se pierde todo.** Cada ficha dice de quién recibe y a quién
entrega.

### 1 · Pedido de venta → entrega al cliente
```
Siesa → sync → PICKING (operario)
                   ↓ relevo 1
              PACKING (empacador) → remisión + factura en Siesa
                   ↓ relevo 2  ← el que más se rompe
              MUELLE / RUTA (jefe_almacen · supervisor)
                   ↓ relevo 3
              ENTREGA (conductor) → recaudo
                   ↓ relevo 4
              LIQUIDACIÓN (supervisor) → NC → RC → DC en Siesa
```

### 2 · Compra → recepción
```
OC aprobada en Siesa → RECEPCIÓN (recepcionista) → escaneo ciego → entrada OC
```

### 3 · Traslado entre sedes
```
TIENDA solicita → GESTIÓN aprueba → PICKER_TRASLADO → PACKER_TRASLADO
      → tránsito de salida → RECEPCIÓN en destino → tránsito de entrada
```

### 4 · Conteo cíclico
```
ABC genera (2 a.m.) → CC1 (operario) → [si difiere] CC2 (otro) → [si difiere] CC3
      → SUPERVISOR aprueba el ajuste → Siesa
```
**Impuesto por el sistema:** nadie aprueba su propio ajuste.

### 5 · Flota
```
CONDUCTOR recibe turno (13 fotos + odómetro) → opera → entrega turno (4 fotos)
      → CONTROL_FLOTA revisa el tablero — señala y escala, NO aprueba
```

---

## Índice de fichas

**Gestión** — abren el panel completo, se diferencian en qué aprueban
- [`admin`](roles/gestion-admin.md)
- [`gerente`](roles/gestion-gerente.md)
- [`jefe_almacen`](roles/gestion-jefe-almacen.md)
- [`supervisor`](roles/gestion-supervisor.md)

**Piso** — una pantalla, sus tareas, sin aprobaciones
- [`operario`](roles/piso-operario.md)
- [`empacador`](roles/piso-empacador.md)
- [`recepcionista`](roles/piso-recepcionista.md)
- [`conductor`](roles/piso-conductor.md)
- [`picker_traslado`](roles/piso-picker-traslado.md)
- [`packer_traslado`](roles/piso-packer-traslado.md)

**Especialistas** — pantalla propia, alcance acotado
- [`tienda`](roles/especialista-tienda.md)
- [`compras`](roles/especialista-compras.md)
- [`control_flota`](roles/especialista-control-flota.md)

**Cómo se capacita** — [el método](00-COMO-CAPACITAR.md)

---

## Cómo se mantiene vivo esto

- Los permisos de cada ficha salen del código, no de la memoria. Si cambian, se
  actualiza la ficha en el mismo commit.
- Los mensajes de error son los literales que la persona ve. Si cambian en el
  código, cambian acá.
- **Un procedimiento sin fecha de última revisión es un procedimiento que nadie
  sabe si sigue siendo cierto.** Cada ficha la lleva al pie.

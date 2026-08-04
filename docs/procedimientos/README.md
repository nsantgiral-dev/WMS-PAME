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
quien crea el usuario tiene que saber cuáles marcar. Cada ficha lo dice explícito.

---

## Centros de operación y sedes

| CO | Sede | Código bodega | Caja Siesa | Roles que operan ahí |
|---|---|---|---|---|
| 001 | Neiva Sur | `NS1` | 001 | tienda, operario, empacador |
| 002 | Neiva Centro | `NC1` | 004 | tienda, operario, empacador |
| **003** | **Bodega CD (centro de distribución)** | `NB1` | 999 | **todos los de piso + gestión** |
| 004 | Pitalito Centro | `PC1` | 999 | tienda |
| 005 | Pitalito Terminal | ⬚ *sin fila en `almacenes`* | 999 | ⬚ |
| 006 | Florencia Centro | `FC1` | 013 | tienda |
| 007–009 | Ferias | ⬚ *sin fila en `almacenes`* | 999 | ⬚ |

> **Hueco conocido:** los CO 005 y 007–009 no tienen fila en `almacenes`. Una
> custodia de vehículo o un traslado que termine ahí queda declarado como
> `pendiente_sede` — el sistema no inventa la sede, la reporta. Mientras no se
> creen, ningún procedimiento debe asumir que existen.

**El CO 003 es el corazón.** Picking, packing, recepción, traslados y flota
ocurren ahí. Las demás sedes son puntos de venta que consumen y solicitan.

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

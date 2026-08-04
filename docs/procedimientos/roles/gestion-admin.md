# `admin` — configuración y llave maestra

> **En una frase:** puede todo. Por eso es el rol que menos gente debería tener.

---

## Lo que SOLO vos podés

| Facultad | Consecuencia si se hace mal |
|---|---|
| **Crear usuarios y asignar roles/capacidades** | Un flag mal marcado da permisos que ningún procedimiento cubre |
| **Configuración de Siesa** (conectores, tipos de documento, motivos) | Un motivo inválido hace que Siesa rechace TODOS los documentos de ese tipo |
| **Maestros**: vehículos, conductores, almacenes, productos | |
| **Reintentar y descartar jobs de la cola DLQ** | Descartar un job pierde una operación hacia Siesa |
| Todo lo de los otros tres roles de gestión | |

---

## La responsabilidad que no está en ningún otro rol

**Vos configurás las tres capas de autorización de todos los demás.**

Cuando creás un usuario:

1. **Rol** → qué pantalla abre
2. **Capacidades** (`puede_picar`, `puede_empacar`, `puede_abastecer`) → qué tareas toma
3. **`almacen_id`** → sobre qué sede

> **El error más frecuente y más confuso:** crear un operario sin `almacen_id`.
> No da error — entra y la lista de tareas sale **vacía**. La persona cree que no
> hay trabajo y vos creés que está trabajando.

⬚ **Falta definir:** quién autoriza marcar `puede_empacar` o `puede_abastecer` y
con qué criterio. Hoy es una casilla que alguien marca sin regla escrita.

---

## Lo que hay que revisar cada semana

| Qué | Por qué |
|---|---|
| **Cola DLQ** | Jobs fallidos = operaciones que no llegaron a Siesa |
| **Salud de conectores** (Vigía G0) | Bandera roja si un conector lleva >24 h sin datos |
| **Ajustes directos de inventario** | Si suben, algo se rompió aguas arriba |
| **Usuarios activos vs personas activas** | Cuentas de gente que ya no está |
| **Cierres forzados de turno** (flota) | Si crecen, no se está cerrando turno |

---

## Cuando algo falla

### Siesa rechaza todos los documentos de un tipo
Casi siempre es un **motivo o tipo de documento mal configurado**. Los motivos son
códigos obligatorios en Siesa (Inventarios → Maestros → Conceptos y Motivos) y
uno inválido causa rechazo duro. Ver `CLAUDE.md`, sección de variables.

### La integración con Siesa "dejó de funcionar"
Mirá el estado del circuit breaker antes de reiniciar nada. Se abre solo cuando
Siesa acumula fallos y se recupera solo con un probe cada cierto tiempo.
> Siesa no opera después de ~8 p.m. Colombia — los timeouts nocturnos son
> esperables.

### Un job de la DLQ falla tres veces
No lo reintentes sin leer el motivo. Tres fallos por el mismo dato malo van a ser
cuatro. Los POST **nunca** se reintentan a ciegas: un timeout no significa que
falló, y reintentar puede duplicar el documento en Siesa.

### Alguien reporta un 403 que no debería
Es un desfase entre el procedimiento y el código. **No aflojes el permiso sin
entender cuál de los dos está mal.** Hay tests que fallan si un endpoint queda
sin control de rol.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice |
|---|---|
| Jobs DLQ pendientes >24 h | Debería ser cero |
| Conectores sin datos >24 h | |
| Ajustes directos por mes | Tendencia, no valor absoluto |
| Cuentas activas sin uso en 30 días | Higiene de accesos |

---

## Lo que no se hace nunca

- **Correr scripts destructivos sin simular primero.** `reset_transaccional.py`
  es el acta de corte: vacía picking, packing, recepciones, rutas y movimientos.
  No es una limpieza puntual. Para un vehículo existe
  `flota_limpiar_vehiculo.py`.
- **Dar `admin` para resolver un permiso puntual.** Se agrega el rol al guard
  correspondiente, que es una línea y queda documentado.
- **Editar datos directamente en la base.** Nada de lo que pasa por ahí queda con
  autor ni motivo.

---
*Última revisión: 2026-08-04*

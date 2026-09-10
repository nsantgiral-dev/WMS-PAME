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

### Criterio para los flags — definido 2026-08-04

| Flag | Se marca cuando | Lo autoriza |
|---|---|---|
| `puede_picar` | Siempre, salvo empacador o abastecedor puro | jefe de almacén |
| `puede_empacar` | **30 días de picking sin errores de cantidad** + entiende que su confirmación emite factura | jefe de almacén |
| `puede_abastecer` | Conoce el layout y la diferencia reserva/picking | jefe de almacén |

> **`puede_empacar` no es "puede ayudar en packing".** Es la facultad de emitir
> una **factura electrónica**: confirmar el packing dispara
> `244328 → 142945 → 142943`. Marcárselo a alguien porque hoy falta gente es
> darle la llave de la facturación. Es un ascenso, no una conveniencia.

**Se revisan al cambiar de puesto, no solo al ingresar.** Un flag que quedó
marcado de un reemplazo de hace seis meses es un permiso que nadie decidió.

**Y la combinación decide qué pantalla abre** — ver la tabla en el
[README](../README.md). Los tres apagados dan la pantalla de operario sin nada
que hacer.

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

## La decisión mensual sobre el gasto de flota

**El costo por kilómetro es tuyo.** No del especialista de control de flota:
él lleva el registro, señala lo vencido y escala — y su ficha dice, en el código
y en el papel, que **no aprueba nada**. La decisión de qué se hace con un camión
que cuesta el doble que el resto la toma quien decide sobre plata.

Dónde: **Flota → Analítica**, una vez al mes. Son **dos paneles y contestan
preguntas distintas**, uno debajo del otro:

| Panel | Contesta | Se compara entre vehículos |
|---|---|---|
| **Costo por kilómetro** | ¿este camión se encareció? | **No.** Un NHR y un motocarro no cuestan igual |
| **Pesos por mes** | ¿cuál se come la plata? | **Sí**, y es la única del tab que se puede: son pesos que salieron |

En *Pesos por mes*, un vehículo **sin un solo gasto registrado va aparte y no
entra al orden**. Metido en la lista con sus $0 saldría de último, es decir
coronado como el más barato de la flota — y el total dice siempre sobre cuántos
vehículos se calculó, porque un total sobre 2 de 6 que no lo declara es el
número que se lleva a una reunión creyendo que es la flota entera.

Tres cosas que el número **no** dice, y conviene tenerlas presentes antes de
decidir sobre ellas:

- **No compara vehículos.** Un NHR y un motocarro no cuestan igual, y la
  diferencia mide la composición del parque, no la operación. Para «¿qué camión
  se come la plata?» la pregunta se contesta en el panel de **Pesos por mes**,
  justo debajo, no en el CPK.
- **No es el costo de tener el camión.** Es lo que se registró: sin
  depreciación, sin financiación, sin el tiempo de quien lo gestiona.
- **No explica una subida.** Un filtro tapado, un mes con más montaña y un
  precio de galón distinto producen el mismo movimiento.

Y un vehículo puede salir **sin cifra**. Cuando pasa, la fila dice cuál de las
tres causas fue —nadie cargó una factura, el odómetro no se puede sostener, o no
hay dos lecturas en el mes— porque las tres se corrigen llamando a alguien
distinto. **Un mes entero sin cifra no significa que la flota sea gratis:
significa que no se está registrando.**

### El reporte de los lunes

Cinco minutos, y es el compromiso del que depende todo lo demás. Si a la tercera
semana no se lee, control de flota deja de mandarlo — y ahí muere el sistema,
igual que en noviembre, y otra vez sin que la herramienta tenga nada que ver.

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

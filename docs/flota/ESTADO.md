# FLOTA — Estado del proyecto

La historia y el estado del módulo. Las **reglas de decisión** viven en
`flota/CLAUDE.md`; la **referencia técnica** de la tanda 1 en
`docs/flota/ESPECIFICACION_T1.md`. Lo que llega acá es estado, no regla — esa
es la distinción que el CLAUDE.md predica y la razón de que este archivo exista.

---

## Compuertas de las tandas

| Tanda | Compuerta de salida |
|---|---|
| 1 — Cimientos y custodia | 5 fichas técnicas cargadas + 5 días seguidos de entrega de turno real |
| 2 — Preoperacional | 10 días seguidos, 5 vehículos, sin huecos |
| 3 — Taller y ruta | 30 días sin huecos → recién ahí el bloqueo pasa de reporte a `raise` |

## Secuencia obligatoria: medir → corregir → imponer

Toda validación nueva nace como reporte en el health y solo después se convierte
en excepción. Nunca al revés.

*Motivo: si el primer día la app deja un camión en patio por un campo mal
llenado, la operación desmonta el sistema en 48 horas.*

---

## Paso 1 — andamiaje (2026-07-31)

Entregado: estructura del módulo, seis trinquetes en cero, `GET /flota/health`
tras JWT de gestión, y los siete invariantes escritos antes de los modelos
(`xfail(strict=True)`: fallan hoy, y el día que se implementen el marcador
estricto obliga a quitarlos).

Sin modelos y sin pantalla, por diseño.

## Paso 2 — modelos de la tanda 1 (2026-07-31)

Las cinco tablas escritas, los siete invariantes implementados y el `xfail`
retirado. Los doce campos del health miden: ninguno queda en `null`.

**La migración NO se ha generado ni corrido.** Se genera y se sube después de
restaurar y verificar un backup.

### Lo que la base garantiza, y lo que no

| # | Invariante | Mecanismo | Alcance |
|---|---|---|---|
| 1 | Monotonía + no se edita | trigger `BEFORE INSERT` / `BEFORE UPDATE` | total |
| 2 | 0 o 1 custodia activa | índice único parcial | total |
| 3 | Cobertura temporal | trigger de no-solape | **parcial** |
| 4 | Arco exclusivo | dos `CHECK` | total |
| 5 | Paternidad de fotos | — | ninguno |

**El 3 es parcial y no por pereza.** Un hueco de cobertura lo produce una
escritura que NO ocurre, y una restricción solo juzga escrituras que sí ocurren:
no se puede constreñir una ausencia. La base impide lo que un `INSERT` sí puede
romper —solape y viaje en el tiempo— y el hueco se detecta con
`huecos_de_cobertura` y se cuenta en el health. Lo que lo previene en la práctica
es que el traspaso sea atómico.

**El 5 no lo puede imponer la base**: `entidad_tipo` + `entidad_id` es
paternidad polimórfica y no admite FK. Queda en el dominio y en el health.

### Asimetría declarada — `DELETE` sobre `flota_lectura_odometro`

El bloqueo de `DELETE` va **solo en PostgreSQL**. En SQLite rompía el teardown de
`tests/conftest.py`, y con él 24 tests ajenos a flota.

**Mina preexistente que esto destapó:** el teardown limpia con `DELETE FROM`
sobre cada tabla de la metadata dentro de un bucle con
`except Exception: rollback()`. Cuando UNA tabla falla, el rollback descarta
también los deletes ya hechos de todas las anteriores — la limpieza se cae
entera y en silencio, y los tests siguientes ven datos de los anteriores.

No se tocó el conftest en esta tanda. **Queda anotado como deuda: una tabla que
falla no debería poder deshacer la limpieza de las otras.** Es el mismo
`except` que traga que el módulo tiene prohibido, viviendo en la infraestructura
de tests.

---

## Mediciones pendientes

### `rutas_historicas_sin_placa` = **3 de 15** — MEDIDO CONTRA LA BASE REAL

Medido el 2026-08-01 contra la base de Railway, en sesión de solo lectura:

```
rutas_despacho WHERE vehiculo_id IS NULL  →  3
rutas_despacho                            → 15
```

**El 20% de las rutas históricas no tiene placa.**

> **`decision_ruta` NO puede asumir placa.** Nace con política para el caso
> nulo, y esa política cubre el 20% de las filas reales, no un caso de borde:
>
> | Caso | Qué significa |
> |---|---|
> | ruta propia | `vehiculo_id` presente → placa desde `vehiculos.placa` |
> | tercerizada con guía | sin `vehiculo_id`, con `numero_guia` → transportadora |
> | `sin_dato` | sin `vehiculo_id` y sin `numero_guia` → no sabemos, y se declara |

La columna `vehiculo_id` es nullable aunque los dos caminos de creación de
`ruta_service` la exijan (`ruta_service.py:239` y `:315`): el riesgo son las
filas viejas, no las nuevas — y ahora se sabe cuántas son.

#### La lectura anterior era FALSA, y se anota para que no se repita

Este documento decía antes que el campo estaba "medido por código, sin valor de
producción", después de que un `0` salido de una **SQLite en memoria con cero
filas en total** se leyera como *"todas las rutas históricas tienen placa"*.

Ese `0` medía la ausencia de datos, no la presencia de placas. La conclusión
—que `decision_ruta` podía construirse sin trabajo previo de vínculo— era
exactamente lo contrario de lo que dice el dato real.

Es el modo de fallo que este módulo entero persigue: **no un número roto, un
número correcto con una lectura equivocada.** Vale anotarlo porque la lectura
falsa se escribió *después* de que alguien advirtiera sobre ese mismo número.
La defensa no es leer con más cuidado: es que ningún número entre a este
documento sin decir contra qué base se midió y en qué fecha.

### Los tres bloqueos de la compuerta de la tanda 1

Medidos el 2026-08-01 contra la base real. **Ninguno es de código.** La compuerta
pide 5 fichas técnicas cargadas y 5 días seguidos de entrega de turno real; hoy
no puede cerrar aunque el código esté completo.

| Bloqueo | Medido | Qué falta |
|---|---|---|
| **Solo 1 vehículo activo de los 5** | `vehiculos WHERE activo` = 1 | Dar de alta 4 filas (placa, tipo, marca, modelo) **antes** de que el dueño de flota salga a campo |
| **`almacenes` cubre 5 de 9 centros** | CO 001, 002, 003, 004, 006 | Faltan CO 005 (Pitalito Terminal) y 007–009 (ferias) |
| **1 conductor activo sin cuenta** | 1 de 3 activos | Ese conductor no puede autenticarse: su entrega de turno la registra otro |

**El primero es el que más duele porque es trivial y bloquea todo lo demás.** El
levantamiento en campo no basta: sin las cuatro filas de vehículo, el dueño de
flota vuelve con las fotos y no tiene dónde ponerlas. Son quince minutos de
`INSERT` que tienen que pasar **antes** de la salida a campo, no después.

El tercero no bloquea: el modelo ya contempla que el jefe de sede registre la
entrega con la cédula del conductor como custodio y su propio usuario como
`registrado_por`. Honesto y auditable, en vez de excluir gente en silencio.

### `custodio_sede_id` apunta a `almacenes` — sin verificar que alcance

§0 de la especificación pedía resolver a qué tabla cuelga la sede antes de
escribir `custodia`. En WMS no hay tabla de sedes: la que hace de centro de
costo es **`almacenes`**, que lleva `codigo`, `nombre`, `ciudad` y
`centro_op_siesa`. La FK apunta ahí.

**Medido el 2026-08-01: `almacenes` tiene 5 filas y cubre CO 001, 002, 003, 004
y 006.** Faltan CO 005 (Pitalito Terminal) y 007–009 (ferias). Una de las cinco
se llama literalmente "Neiva Centro (Prueba)".

Un vehículo que hoy termine turno en Pitalito Terminal **no tiene fila a la cual
apuntar**.

#### Decisión: flota NO crea almacenes

`almacenes` es maestro del WMS y está atada a la parametrización de Siesa. Crear
filas desde este módulo sería meterse en un maestro ajeno para tapar un hueco
propio — y una sede inventada desde flota reaparecería después como un centro de
operación que Siesa no reconoce.

En cambio, **el sistema declara lo que no puede representar**:

- `custodio_sede_id` acepta **solo almacenes existentes** (la FK ya lo garantiza).
- Si el vehículo termina turno en una sede sin fila, la custodia se registra con
  `custodio_estado = 'pendiente_sede'`: existe, tiene responsable declarado como
  sede, y **no miente sobre cuál**.
- El health la cuenta en `custodias_pendiente_sede`.

Es la regla 4 aplicada a una relación: un custodio que no se puede representar no
es `NULL` ni una sede cualquiera — es un estado con nombre. Y como el health lo
cuenta, no se puede acumular sin que nadie lo vea.

Lo de "Neiva Centro (Prueba)" en la base real es basura del maestro. Se saca,
pero lo saca el dueño del maestro.

### Ficha técnica — 5 vehículos

Levantamiento en campo (paso 2), a cargo del dueño de flota. Es la semilla de
datos de la tanda 1: sin los cinco kilometrajes iniciales la compuerta no cierra
aunque el código esté listo. `km_inicial` es el ancla del sistema.

`distribucion` de los N300 se pide al concesionario Chevrolet con el número de
motor. Si no responde, ese campo queda en `sin_dato` y el sistema lo declara —
no bloquea nada más.

---

## Cartera — el camino de NC que no tenía sensor (2026-08-01)

Fuera de flota, pero medido en la misma sesión y con fecha del 17 de agosto.

Hay **dos** caminos que crean notas crédito 142946, y hasta hoy solo uno tenía
dónde anotar si contabilidad la aprobó y cruzó a mano en Siesa:

| Camino | Tabla | ¿Rastreaba aprobación? |
|---|---|---|
| Devolución de cliente | `devoluciones_cliente` | Sí |
| Liquidación de ruta (PARCIAL / RECHAZADO) | `recaudos_entrega` | **No — la columna no existía** |

Medido contra la base real: 2 RECHAZADO y 1 PARCIAL, los tres con
`siesa_nc_triggered = false`. **Hoy no muerde.** El go-live del 17 activa rutas
con rechazos; a partir de ahí, una NC de liquidación aprobada y una sin aprobar
son indistinguibles, y el cliente aparece debiendo algo que ya devolvió.

Hecho: tres columnas espejo en el modelo + `listar_cartera_fantasma()` que
cubre los dos caminos y **declara su cobertura en cada respuesta**.
Migración diseñada en `docs/migraciones_propuestas/`, **no aplicada** — y no
está en `migrations/versions/` a propósito, porque `releaseCommand` la correría
sola en el próximo deploy.

Contribución del WMS a la cartera fantasma, medida el 2026-08-01: **2 registros**,
uno de ellos la prueba de FEW-1463 (Samboni) documentada en CLAUDE.md. No es la
cartera fantasma total — esa vive en Siesa.

---

## Deuda declarada, con condición de disparo

### Tercera copia de la política "a qué SIESA apunta"

La política que decide `ambiente` / `datos_reales` existe hoy **tres veces**:

| Dónde | Forma |
|---|---|
| `app/routes/health.py` — `/api/health/ping` | inline |
| `app/routes/health.py` — `/api/health/siesa` | inline |
| `flota/adaptadores/medicion.py::MedidorSQL.ambiente` | tercera copia |

Contenida por `tests/flota/test_health_flota.py::TestAmbienteNoDiverge`, que
compara la respuesta de `/flota/health` contra la de `/api/health/ping` y
revienta si difieren.

**Pero un test comparativo no es una unificación.** El día que alguien cambie una
copia y ajuste el test para que pase, la deuda vuelve a ser invisible — el test
protege contra el olvido, no contra la decisión.

> **Condición de disparo: la próxima vez que se toque `app/routes/health.py` por
> cualquier motivo, se unifican las tres ahí mismo.** No es un proyecto aparte:
> es un peaje. Al unificarse, `TestAmbienteNoDiverge` se cae solo por falta de
> objeto que comparar, y eso es señal de éxito.

*Motivo: el mismo concepto escrito dos veces divergió en tres horas y costó 25×
de sobreestimación. Escrito tres veces, más rápido.*

---

## Decisiones tomadas

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-07-31 | `/flota/health` va tras JWT de gestión, no público | Declara `ambiente`, `datos_reales` y el inventario de lo que el sistema no sabe. Eso es reconocimiento de superficie. Si hace falta lectura sin sesión, se resuelve con un token de solo lectura, no abriendo el endpoint. |
| 2026-07-31 | El import de `flota.api` está blindado: si falla, el WMS arranca igual y `/flota/*` responde 503 con motivo | El WMS todavía no sale a producción; un módulo nuevo sin estrenar no puede tumbar el arranque. Un 503 con motivo declarado es lo contrario del éxito silencioso que prohíbe la regla 5. |
| 2026-07-31 | `rutas_historicas_sin_placa` se mide, no queda en `null` | Un cero medido y un `null` son afirmaciones distintas. |

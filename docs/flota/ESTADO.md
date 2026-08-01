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

---

## Mediciones pendientes

### `rutas_historicas_sin_placa` — MEDIDO POR CÓDIGO, SIN VALOR DE PRODUCCIÓN

El campo se mide de verdad: `RutaDespacho.vehiculo_id IS NULL`. La
especificación §5 lo mostraba en `null` = "aún no medido"; se corrigió porque la
tabla existe desde antes del módulo y un cero medido es una afirmación distinta
de un `null`.

**El `0` que apareció en la corrida de verificación NO es el valor de
producción.** Salió de una base SQLite en memoria con cero filas en total: mide
la ausencia de datos, no la presencia de placas. Leerlo como "todas las rutas
históricas tienen placa" es exactamente el defecto que este módulo persigue —
un número correcto, plausible, y una lectura equivocada.

Queda pendiente correr el health contra la base real. Hasta entonces:

> **No se sabe cuántas rutas históricas están sin placa**, y por lo tanto no se
> sabe si `decision_ruta` (tanda 3) puede asumir placa sin política para el caso
> nulo. La columna `vehiculo_id` es nullable aunque los dos caminos de creación
> de `ruta_service` la exijan (`ruta_service.py:239` y `:315`), así que las filas
> viejas son el riesgo, no las nuevas.

### Ficha técnica — 5 vehículos

Levantamiento en campo (paso 2), a cargo del dueño de flota. Es la semilla de
datos de la tanda 1: sin los cinco kilometrajes iniciales la compuerta no cierra
aunque el código esté listo. `km_inicial` es el ancla del sistema.

`distribucion` de los N300 se pide al concesionario Chevrolet con el número de
motor. Si no responde, ese campo queda en `sin_dato` y el sistema lo declara —
no bloquea nada más.

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

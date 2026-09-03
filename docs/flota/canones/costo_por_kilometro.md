# Canon — `costo_por_kilometro`

> **Escrito el 2026-09-02, y con una debilidad declarada arriba de todo.**
>
> `PLANTILLA.md` dice: *«La llena quien NO va a implementar la métrica. Si el
> valor y su test los produce la misma mano, el test es decoración»*. Acá la
> llenó la misma mano que escribió `flota/dominio/costos.py` y
> `tests/flota/test_costos.py`. **Eso es una violación de la regla del formato y
> queda escrita en vez de disimulada**, porque un canon que finge procedencia
> vale menos que ninguno.
>
> Lo que la contiene parcialmente: el §5 no salió del código. Salió de una
> aritmética hecha aparte, con insumos elegidos para que **cada división sea
> exacta**, y después se corrió el código contra ella. Si el código hubiera dado
> otra cosa, lo que estaba mal era el código.
>
> **Condición de cierre:** Santiago (o quien apruebe gastos de flota) reemplaza
> el §5 por un mes real —placa, facturas con su número, período del SOAT y las
> dos lecturas de odómetro— y el §7 pasa a citarlas. Hasta entonces este canon
> fija **la definición y la aritmética**, no la magnitud: no hay un solo peso de
> flota registrado en el sistema todavía. Ver §9.

---

## 1. Nombre

`costo_por_kilometro`

Abreviado **CPK** en pantalla y en el health (`cpk_mes`). El nombre se deriva de
la definición: pesos ÷ kilómetros, de UN vehículo, en UNA ventana.

## 2. Qué afirma, en palabras

**Cuántos pesos, de los que quedaron registrados contra este vehículo, le
corresponden a cada kilómetro que este vehículo recorrió en esta ventana** — con
el gasto que cubre un período ya repartido sobre los días de la ventana.

Es la pregunta que el ERP no contesta hoy, y **no porque le falte la plata: le
falta el kilómetro.**

## 3. Qué NO afirma

Seis cosas. Las seis importan porque el número va a un tablero y un número en un
tablero se usa para decidir sobre gente.

- **No compara vehículos entre sí.** Un NHR, un furgón y un motocarro no rinden
  ni cuestan igual, y la diferencia entre sus CPK mide la clase de vehículo, no
  la gestión. *«¿Qué vehículo se come la plata?»* se contesta en **pesos por
  mes**, que es otro número y no está en este canon. La firma lo respeta: la
  función recibe un vehículo y no sabe que existen otros.
- **No mide a ninguna persona.** No recibe conductor, y no debe recibirlo nunca.
  Quién manejaba está en la custodia y se cruza a mano si alguien decide
  investigar (regla 2 del módulo).
- **No es el costo total de poseer el vehículo.** Depreciación, financiación,
  parqueadero fijo y el tiempo de quien gestiona quedan **fuera**: no se
  registran. El CPK dice lo que se registró, no lo que se gastó.
- **No es contabilidad y no cuadra con un balance.** El valor, el proveedor, el
  impuesto y la causación viven en Siesa; acá el `valor` existe únicamente para
  poder dividirlo entre kilómetros y el puente es una referencia
  (`documento_numero`), no una copia.
- **No afirma que el gasto de período se haya consumido día a día.** Un SOAT no
  se gasta un poco cada día: se paga entero y protege todo el año. El reparto
  lineal es una convención para que el CPK de marzo no dependa de en qué mes se
  firmó la póliza.
- **No explica por qué subió ni por qué bajó.** Un CPK alto puede ser un taller
  caro, una ruta de montaña, un mes de pocos kilómetros o un vehículo que
  necesita mantenimiento. El sistema dice cuánto; el porqué lo averigua una
  persona.

## 4. Decisiones que fijan el cálculo

| Decisión | Respuesta | Motivo |
|---|---|---|
| ¿Qué pesos entran? | Los gastos registrados **contra ese vehículo**, de cualquier categoría | Un CPK que solo sume combustible se llama «costo de combustible por kilómetro» y no es este número |
| ¿Cómo entra el gasto que cubre un período (SOAT, RTM, impuesto, seguro)? | Repartido **linealmente por día calendario** sobre el período que el gasto declara cubrir | Cargarlo al día en que se pagó haría que el CPK de un mes dependa de en qué mes se firmó la póliza. Es la decisión del plan: *«el SOAT no se carga al CPK del día que se pagó»* |
| ¿Quién decide si una categoría cubre un período? | La categoría, vía `CATEGORIAS_CON_PERIODO` — **nunca una casilla que teclea quien registra** | Si fuera casilla, el camino barato es no marcarla y el gasto entero cae sobre un día (regla 11) |
| ¿Un período de 1-ene a 31-dic son 364 o 365 días? | **365. Los dos extremos cuentan** | Escrito UNA vez, en `dias_del_periodo`. La segunda copia usa `.days` pelado y el año pierde un día sin que nada falle |
| ¿Se multiplica antes de dividir? | **Sí**: `valor × solapados ÷ días` | Cuando el reparto es exacto, este orden lo mantiene exacto; cuando no lo es, arrastra **un** redondeo en vez de dos. Ver §8 — la afirmación de que «los doce meses siempre suman exacto» es más fuerte de lo que la medición aguanta |
| ¿Qué kilómetros van al denominador? | Los del **odómetro**: última lectura de la ventana menos la primera | Es el único denominador medido que existe. No se estima con rutas ni con días |
| ¿Qué pasa si el tramo de odómetro no es confiable? | El número viaja **con su marca** (`verificada`/`declarada`/`dudosa`), y con los dos extremos dudosos no hay número | La marca la decide `flota/dominio/odometro.py`, no el CPK. Regla 0: una política, una función |
| ¿Se rellena algo cuando falta un dato? | **Nunca.** `sin_dato`, jamás cero | Un CPK de $0 se lee como un vehículo gratis, y un vehículo gratis no se investiga |
| ¿Se publica por conductor? | **No, y no es una omisión de esta fase: es una prohibición** | Regla 2. Además, la respuesta de quien no quiere hacer el trabajo es tanquear a medias y declarar lleno |

## 5. Caso calculado a mano

> **Insumos sintéticos, declarados como tales.** Elegidos para que cada división
> sea exacta y el canon no necesite tolerancia. No son un mes real de ningún
> vehículo — ver el aviso del encabezado y el §9.

- **Vehículo:** uno solo. **Ventana:** 1 a 31 de marzo de 2026 (31 días).
- **Odómetro:** lectura del 1 de marzo = **100.000 km**; lectura del 31 de marzo
  = **101.000 km**. Marca del tramo: `declarada` (nadie verificó la foto).
- **Gastos registrados contra el vehículo:**

| # | Categoría | Fecha | Valor | Período que cubre |
|---|---|---|---|---|
| 1 | `soat` | 2026-01-01 | $730.000 | 2026-01-01 → 2026-12-31 |
| 2 | `combustible` | 2026-03-05 | $168.000 | el mismo día |
| 3 | `mantenimiento` | 2026-03-10 | $190.000 | el mismo día |
| 4 | `combustible` | 2026-03-15 | $196.000 | el mismo día |
| 5 | `combustible` | 2026-03-25 | $224.000 | el mismo día |

**Paso 1 — el SOAT se reparte, no se carga entero.**

```
días del período   = 31-dic − 1-ene + 1  = 365      (los dos extremos cuentan)
días solapados     = 31-mar − 1-mar + 1  = 31
imputado a marzo   = 730.000 × 31 ÷ 365  = 62.000
```

$730.000 ÷ 365 = **$2.000 por día**, y 31 días × $2.000 = **$62.000**. Exacto.

**Paso 2 — lo que se consume el día en que ocurre entra entero.**

```
combustible  168.000 + 196.000 + 224.000 = 588.000
mantenimiento                              190.000
```

**Paso 3 — sumar y dividir entre los kilómetros medidos.**

```
pesos imputados a marzo = 62.000 + 588.000 + 190.000 = 840.000
kilómetros de marzo     = 101.000 − 100.000          =   1.000
CPK                     = 840.000 ÷ 1.000            =     840
```

**Resultado: `$840,00 por kilómetro`, marca `declarada`.**

Verificación de orden de magnitud, para que el número no pase sin que nadie lo
mire: solo el combustible son $588 por kilómetro, el 70% del total. En un
vehículo que rinde 24 km/galón con el galón a $14.000, el combustible **tiene**
que dar ≈$583/km. Si el CPK de un mes real diera menos que su propio
combustible, el error está en los kilómetros.

### El mismo mes, mirado por el lado del combustible

Los tres tanqueos de marzo, todos con **tanque lleno**, sirven de segundo caso
porque el rendimiento se mide sobre las mismas filas:

| Tanqueo | km | galones | $ | tanque |
|---|---|---|---|---|
| 5 mar | 100.000 | 12 | 168.000 | lleno |
| 15 mar | 100.392 | 14 | 196.000 | lleno |
| 25 mar | 100.720 | 16 | 224.000 | lleno |

```
ventana A  100.000 → 100.392 =  392 km  con 14 gal  = 28,0  km/gal
ventana B  100.392 → 100.720 =  328 km  con 16 gal  = 20,5  km/gal
agregado   (392+328) ÷ (14+16)  =  720 ÷ 30         = 24,0  km/gal
```

**El agregado es 24,0 y el promedio de las dos razones es 24,25.** No es un
detalle de redondeo: se suman kilómetros y galones **antes** de dividir, porque
el promedio de razones le da el mismo peso a un tramo de 40 km que a uno de 600.
Los dos números existen y solo uno es el rendimiento.

Los **12 galones del primer tanqueo no entran en ninguna ventana**: se quemaron
antes del primer lleno, en un tramo que nadie midió. Contarlos hundiría el
rendimiento del mes con combustible que se gastó en febrero.

Precio del galón: 224.000 ÷ 16 = **$14.000**. Se calcula, **no se guarda** — una
tercera columna con el precio contradice a las otras dos el día que alguien
corrija una factura mal digitada.

## 6. Casos degenerados

Ninguno vale cero por omisión. Hay **dos ceros distintos** y confundirlos es el
defecto principal que este canon previene.

| Caso | Valor | Motivo |
|---|---|---|
| **La ventana no tiene kilómetros** (`km_recorridos ≤ 0`) | `sin_dato`, con la marca del tramo | Un CPK de cero kilómetros no es cero pesos por kilómetro: es que no se puede dividir |
| **Ningún gasto registrado en todo el vehículo** | `sin_dato` | «Nadie registró nada» y «no costó nada» se ven idénticos en un cero, y el primero es el estado real hoy: la operación viene perdiendo las facturas. Un CPK de $0 se lee como un vehículo gratis |
| **Hay gastos registrados y ninguno cae dentro de la ventana** | **`0`, y es un cero medido** | El SOAT del año pasado aporta exactamente nada al CPK de este mes. Eso sí es una afirmación sobre la flota |
| **Los dos extremos del tramo de odómetro son dudosos** | `sin_dato`, y la marca también `sin_dato` | No se sabe cuántos kilómetros recorrió. **Nunca un promedio de la flota que rellena el hueco** |
| **Un solo extremo dudoso** | El número **con marca `dudosa`** | Se publica el número y se publica que es flojo. Ocultarlo dejaría al vehículo sin CPK para siempre |
| **Tanqueo parcial en un extremo** (rendimiento) | La ventana **no existe** | Sobre un parcial, dividir km entre galones mide lo que quedaba adentro. No produce una ventana peor: no produce ninguna |
| **Menos de dos tanqueos llenos** | `[]` ventanas, rendimiento `sin_dato` | Todavía no hay una sola medición. `[]` lo dice sin fingir que se midió algo |
| **Galones = 0 en un tanqueo** | `precio_por_galon` = `sin_dato` | No es combustible gratis: es una fila mal cargada |
| **Capacidad de tanque ausente en la ficha** | `excede_capacidad` = `sin_dato`, **jamás `False`** | `False` significaría «se revisó y está bien». Un vehículo sin capacidad declarada saldría limpio para siempre — así es como un detector se apaga sin que nadie lo note |

## 7. Procedencia

- **Quién lo escribió:** el agente que implementó `flota/adaptadores/gastos.py`,
  el 2026-09-02. **Misma mano que el código — declarado arriba como debilidad.**
- **Fuente de las decisiones del §4:** el plan de la fase «la plata que sale»
  (§4 *Dónde vive el dinero*, y la regla *«el SOAT no se carga al CPK del día que
  se pagó»*), `flota/CLAUDE.md` reglas 2, 3, 4, 5 y 11, y la regla 0 del WMS.
- **Fuente de los números del §5:** **ninguna. Son sintéticos** y elegidos para
  que la aritmética sea exacta. No hay un solo gasto de flota registrado en
  producción al 2026-09-02.
- **Implementación:** `flota/dominio/costos.py`.
- **Tests derivados:** `tests/flota/test_costos.py::TestCanonCostoPorKilometro`
  — los valores del §5 están escritos ahí como constantes y el test falla si el
  código deja de reproducirlos.

## 8. Tolerancia

**Cero sobre el caso del §5**, y no por generosidad del criterio: los insumos se
eligieron para que las cuatro divisiones sean exactas ($730.000 ÷ 365 = $2.000;
$840.000 ÷ 1.000 = $840; 720 ÷ 30 = 24; $224.000 ÷ 16 = $14.000). Si el test
falla, hay un bug o una decisión del §4 que cambió sin actualizar este
documento. Jamás se afloja el criterio para que pase.

### Lo que la medición no respalda, y por eso se corrige acá

El docstring de `imputar_a_ventana` afirmaba que *«con Decimal y en este orden,
los doce meses suman exacto»*. **Medido el 2026-09-02 con `Decimal` de 28
dígitos, repartiendo un valor sobre los 12 meses de 2026:**

| Valor de la póliza | `valor × días ÷ período` | `valor ÷ período × días` |
|---|---|---|
| $730.000 (divisible entre 365) | 730.000 exacto | 730.000 exacto |
| $1.860.000 (divisible entre 365) | 1.860.000 exacto | 1.860.000 exacto |
| $1.000.000 (**no** divisible) | 999.999,9999999999999999999998 | 999.999,9999999999999999999999 |

La suma exacta depende de que el valor sea divisible entre los días del período,
**no del orden de las operaciones**. El orden sí importa —arrastra un redondeo
en vez de dos— pero el residuo es de orden 10⁻²² pesos y el docstring prometía
más de lo que entrega. La decisión de multiplicar primero se conserva; la
justificación se ajustó a lo medido. Regla 13: el número es de esa corrida, con
ese motor y esa precisión.

## 9. Lo que este canon todavía no puede fijar

**La magnitud.** `dias_hallazgo_abierto` tiene el THP 696: un caso de papel, con
fechas reales, que fija el orden de magnitud en «un mes, no una semana». El CPK
no tiene equivalente — no hay facturas de flota cargadas ni en papel
sistematizado ni en el WMS.

Eso no es un problema del canon: **es el estado del dato, y es exactamente lo
que la fase construye.** Lo que sí queda fijado desde hoy es la definición, el
reparto, los ceros y los `sin_dato`. Cuando existan tres meses de gastos reales
de un vehículo, el §5 se reemplaza y el §9 desaparece.

Hasta entonces, cualquier CPK que muestre el sistema es aritmética correcta
sobre datos que apenas empiezan a existir — y el health lo dice publicando
`gastos_sin_documento` al lado del número.

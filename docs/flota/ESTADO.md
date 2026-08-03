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

**Migración generada el 2026-08-01, pendiente de `upgrade`.**
`migrations/versions/f10ta1cimientos_flota_tanda1.py`, head único
`c0a1cecc16dc → f10ta1cimientos`.

Respaldo bajo el que se corre, con fecha y no de palabra:

| Cobertura | Estado |
|---|---|
| Snapshot manual Railway | **2026-08-01 23:27** (395 MB) |
| Point-in-time recovery | **activo** — restaura a cualquier segundo |
| Schedules | diario (6 días) + semanal (1 mes) |
| Estado previo verificable | `scratchpad/estado_pre_migracion.json` — 48 tablas, 169.495 filas |

PITR importa más que el snapshot para esto: con un snapshot de volumen, un
rollback a las 3pm te devuelve a las 8am y perdés el día. Con PITR volvés a las
2:59pm.

**El cuerpo de la migración NO se escribió a mano**: se emitió desde
`db.metadata`. Una transcripción manual de 5 tablas, 31 CHECK y 5 índices es
donde se pierde un constraint sin que nadie lo note, y un invariante que la base
no impone es una sugerencia. Verificado: cubre las 5 tablas, las 22+9+8+12+16
columnas, los 31 CHECK y los 5 índices, sin faltantes.

**Es puramente aditiva**: cinco `CREATE TABLE`, ningún `ALTER`, ningún `DROP`,
ninguna migración de datos. No toca una sola de las 169.495 filas.

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
| ~~Solo 1 vehículo activo de los 5~~ | **RESUELTO 2026-08-01** | Las cinco placas dadas de alta: TGZ653 y TGZ655 (Van, los N300), THP696, UPQ606 y WHX245 (Camión) |
| **`almacenes` cubre 5 de 9 centros** | CO 001, 002, 003, 004, 006 | Faltan CO 005 (Pitalito Terminal) y 007–009 (ferias) |
| **1 conductor activo sin cuenta** | 1 de 3 activos | Ese conductor no puede autenticarse: su entrega de turno la registra otro |

**El primero es el que más duele porque es trivial y bloquea todo lo demás.** El
levantamiento en campo no basta: sin las cuatro filas de vehículo, el dueño de
flota vuelve con las fotos y no tiene dónde ponerlas. Son quince minutos de
`INSERT` que tienen que pasar **antes** de la salida a campo, no después.

El tercero no bloquea: el modelo ya contempla que el jefe de sede registre la
entrega con la cédula del conductor como custodio y su propio usuario como
`registrado_por`. Honesto y auditable, en vez de excluir gente en silencio.

#### Basura en el maestro de vehículos — para el dueño del maestro

Al dar de alta las cinco placas apareció esto:

| id | placa | activo |
|---|---|---|
| 1 | `FNR*()` | **sí** |
| 2 | `TST999` | no |
| 3 | `TST001` | no |

**El `vehiculos_activos = 1` que midió el health el 2026-08-01 estaba contando
la fila 1**, que tiene una placa imposible. No era un vehículo: era ruido con
cara de dato.

No se tocó —es maestro ajeno y no sé de dónde salió— pero hay que desactivarla:
mientras siga activa, el health dice 6 vehículos y la compuerta de 5 fichas va a
verse eternamente incompleta por una fila que no existe en el patio.

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

## El deploy que falló: los tests corren en SQLite, producción es PostgreSQL

**2026-08-01 23:47.** El release del deploy murió en el `CREATE TABLE` de
`flota_custodia`:

```
operator does not exist: boolean + boolean
CHECK ((custodio_conductor_id IS NOT NULL) + (custodio_sede_id IS NOT NULL)) = 1
```

SQLite acepta esa suma —trata los booleanos como 0/1—. **PostgreSQL no tiene
operador `boolean + boolean`.** Los 25 tests de constraints con `INSERT` crudo
pasaron en verde contra un motor que no es el de producción.

Lo que hace esto peor que un bug común: `test_constraints_t1.py` abre diciendo
*"prueba que no exista camino que las esquive"* y *"si un INSERT crudo puede
violar el invariante, el modelo está incompleto"*. Todo eso era cierto **en
SQLite**. El archivo entero medía la propiedad correcta contra el objeto
equivocado.

Es la misma clase que costó tres builds en 2026-07: **validar contra mi entorno
en vez de contra el artefacto desplegado.** Ahí fue `.git` y el árbol de
archivos; acá es el motor de base de datos. Y `simular_build.sh` lo declara en
cada corrida —"NO cubre el intérprete ni las dependencias"—; habría que agregar
"ni el motor de base".

### Qué se rompió: nada

| | |
|---|---|
| Base | `head=c0a1cecc16dc`, 0 tablas flota, **169.495 filas intactas** |
| Producción | arriba, sirviendo el código anterior (`/flota/health` → 404) |
| Por qué | PostgreSQL hace DDL transaccional: la migración entera se revirtió sola |

El `releaseCommand` falla **antes** de arrancar el código nuevo, así que Railway
siguió sirviendo el deploy anterior. El orden build → tests → migración → start
hizo exactamente lo que tenía que hacer.

### Arreglo y trinquete

`CASE WHEN <predicado> THEN 1 ELSE 0 END`, que es portable. Más el **trinquete 8**:
ningún CHECK de flota puede hacer aritmética sobre predicados.

**Pero el trinquete ataja la clase concreta, no la causa.** La causa es que los
tests corren sobre SQLite. La deuda estructural, anotada con nombre:

### Atacada el 2026-08-02 — con una salvedad que hay que leer

`tests/flota/test_constraints_postgres.py` corre los invariantes contra
PostgreSQL real. Doce tests, marcados `postgres`:

- **`test_las_cinco_tablas_de_flota_existen`** — crear el esquema contra
  PostgreSQL. Eso solo habría atrapado el bug del `(bool) + (bool)`.
- Invariante 4 con `INSERT` crudo, en el motor donde falló.
- El índice parcial `WHERE fin_ts IS NULL`, que los dos motores escriben distinto.
- Los triggers, **incluido el `BEFORE DELETE` que hasta ahora no se ejercía en
  ningún test** porque no existe en SQLite.

**No se saltan: fallan.** Sin `FLOTA_TEST_PG_URL` van a rojo, porque un skip deja
el reporte en verde y la propiedad sin verificar — el mismo falso negativo del
que salió todo esto.

Quedan fuera del `buildCommand` (`-m "not postgres"`) porque el contenedor de
build no tiene base de pruebas. Eso está **declarado** en `pytest.ini` y en
`railway.toml`, que es distinto de estar callado, y el **trinquete 9** verifica
que la declaración siga ahí.

El trinquete 9 además cruza el conteo de CHECK de los modelos contra la
expectativa de la suite de PostgreSQL: agregar un constraint sin correrlo contra
el motor real pone el build en rojo.

**EJECUTADA EN VERDE el 2026-08-03** contra PostgreSQL 17.10 local:
**13 passed.** Ya no es la intención de verificar contra el motor real — es la
verificación.

El trinquete 9 se ejerció solo al día siguiente: al agregar las dos tablas de
plantillas el conteo pasó de 31 a 37 CHECK y el build se puso rojo, obligando a
correr la suite contra PostgreSQL antes de dar los constraints nuevos por
buenos. Funcionó sin que nadie se acordara de hacerlo.

Para correrla:

```bash
FLOTA_TEST_PG_URL=postgresql://user:pass@host:puerto/scratch \
  venv/bin/python -m pytest -m postgres -v
```

---

## Lección: un trinquete que mide una proxy da falsos negativos silenciosos

Pasó dos veces el mismo día, y las dos con la misma forma.

**El tope del CLAUDE.md** medía líneas totales. La propiedad que le importaba era
*sedimentación*. Saltó con la regla 13 —crecimiento legítimo— y no habría saltado
nunca con un documento colándose entre reglas si el archivo era corto.

**El trinquete de huérfanos** verificaba que el endpoint apareciera *mencionado*
en el JS. La propiedad que le importaba era que la función fuera *alcanzable
desde un gesto*. `flotaGuardarFicha` mencionaba su ruta, así que el endpoint
parecía consumido — y no había un solo botón que la llamara. **Quinta aparición
del patrón función-sin-caller en este repo, adentro del módulo construido para
evitarlo.**

Los dos son falsos negativos silenciosos: el guard está verde y la propiedad
está rota. Peor que no tener guard, porque además tranquiliza.

> **Regla: cuando un trinquete no atrapa algo que debía atrapar, revisar QUÉ MIDE
> antes de mover el umbral.** Casi nunca es el número: es que mide algo que se
> parece a la propiedad en vez de la propiedad.

La confirmación de que sirve: al reescribir el guard por alcanzabilidad apareció
**una segunda huérfana que nadie sospechaba** —`flotaRegistrarOdometro`—, o sea
que el endpoint de odómetro tampoco tenía gesto.

---

## Estructura de responsabilidad — definida el 2026-08-01

Lo que faltaba desde el primer día no era código: era dueño. El sistema de papel
de septiembre a noviembre de 2025 detectaba bien y murió el 18 de noviembre por
no tenerlo.

| Nivel | Quién | Qué hace | Tiempo |
|---|---|---|---|
| Custodio diario | Cada conductor | Preoperacional, odómetro, reporta hallazgos | 2 min/día |
| Responsable de zona | Admin C.O. 003 (Neiva) · encargado Pitalito · encargado Florencia | Ejecuta lo que ya ejecuta, **pero registrándolo** | Lo que ya hacen |
| Control de flota | **Yesid** — dueño del registro | Revisa el tablero, persigue lo vencido, escala | 30 min/semana |

El nivel 2 ya existía y funcionaba. Lo único que cambia es que registran.

### Yesid no tiene autoridad jerárquica sobre las zonas, y el diseño depende de eso

**No ordena: señala plazos vencidos y escala.** Llama a la administradora; si no
se resuelve en el plazo, no insiste — le escribe a Santiago.

**La autoridad de la instrucción es del sistema, no de la persona.** El hallazgo
nace con severidad y fecha límite calculadas por regla (bloqueante = mismo día,
mayor = 7, menor = 30), no por opinión de nadie. Yesid no está diciendo qué
hacer: está señalando que un plazo se venció.

Es el mismo diseño que el bloqueo de cartera — **la decisión incómoda la toma la
regla, no la persona.** Sin eso, el rol muere en el primer "yo no le recibo
órdenes a él", y tendría razón quien lo diga.

Lo que **no** funciona es repartirlo entre las tres administradoras: con tres
responsables y ningún dueño, nadie mira el conjunto. Eso es exactamente lo que
pasó entre noviembre y hoy.

### El compromiso que sostiene todo, y no es de código

Santiago lee el reporte de tres líneas **todos los lunes**. Cinco minutos.

Si a la tercera semana no se leyó, Yesid deja de mandarlo, y ahí muere el
sistema — igual que en noviembre, y otra vez sin que la herramienta tenga nada
que ver.

---

## Tanda 2 — alcance agregado el 2026-08-01

### Reporte semanal automático, de tres líneas

Llega **armado** los lunes por correo a Yesid. Tres números y su detalle:

1. Inspecciones completas de la semana
2. Hallazgos vencidos, con días de vencimiento
3. Documentos que vencen en 30 días

**Si Yesid tiene que construirlo, no lo va a construir.** El sistema trabaja
para él, no al revés — esa es la diferencia entre un rol de 30 minutos y uno que
nadie sostiene.

Nace apagado por variable de entorno, como todo cron que escribe (regla 10).

### Notificaciones por WhatsApp (Gupshup) — caso de uso interno

**Tres empleados, dos o tres mensajes por semana, sin respuesta esperada.** No
son clientes. Esa diferencia cambia el diseño respecto del adaptador de cartera:

| Decisión | Flota | Por qué difiere de cartera |
|---|---|---|
| Número | **Compartido** con la app de cartera | El riesgo de *quality rating* nace de que la gente bloquee; tres empleados que esperan el mensaje no bloquean. Y como no se espera respuesta, no compiten por el único callback URL |
| Opt-out | **No aplica** | Es comunicación laboral, no comercial |
| Teléfonos | **En configuración, a mano** | Cuatro números. No salen de `TercerosContacto`, así que las trampas de la paginación sin `ORDER BY` y del caché que reemplaza no tocan este caso |
| Delay y tope diario | **No aplican** | No hay ráfagas |
| Escalonamiento por cohortes | **No aplica** | Los destinatarios YA son los internos |

**Lo que sí se hereda entero del adaptador de cartera, y es lo caro:**

1. **El nombre de plantilla no es su UUID.** `_TEMPLATE_IDS.get(nombre, nombre)`
   hacía que Gupshup respondiera `submitted` y no llegara nada, durante semanas.
   Devuelve `None` y aborta. Es la regla 5 literal: un adaptador que degrada
   hacia algo que se parece al éxito.
2. **`submitted` no es `delivered`.** Acá importa más que en cartera: **el
   propósito del sistema es que un hallazgo vencido no se quede quieto.** Si el
   aviso no llega y nadie se entera, el sistema falló justo donde tenía que
   funcionar. Se consumen los eventos de entrega desde el día uno, y un aviso de
   hallazgo bloqueante que no llegue a `delivered` se registra y escala.
3. **El doble se declara** — `simulado = True` en el registro, no solo en el
   código (regla 8).
4. **El guard de teléfono verifica forma.** `str(None)` es `'None'` y es truthy.
5. **Callback URL verificado el primer día**, o todo lo entrante se pierde en
   silencio.

**Una notificación por evento, no por consulta.** Si el cron corre cada noche y
reenvía el mismo hallazgo vencido, en tres días el chat se silencia. Se manda al
vencer, y se repite solo si escala de nivel.

Tres plantillas, categoría `utility`, parámetros como **lista explícita** (son
posicionales: reordenarlos manda la dirección donde va la fecha, sin error):

| Plantilla | Destinatario |
|---|---|
| `flota_hallazgo_bloqueante` | Admin de zona + Yesid, al instante |
| `flota_hallazgo_vencido` | Yesid; +2 días sin cerrar → Santiago |
| `flota_documento_vence` | Yesid, 15 días antes |

SMS queda **fuera de alcance**: cuesta más, se lee menos, y con tres
destinatarios internos que usan WhatsApp todo el día no aporta.

**Orden:** esto es tanda 2 y no puede adelantarse. Primero tiene que existir el
hallazgo con plazo — que es lo que se notifica. Notificar antes de que haya qué
notificar es superficie sin estrenar, que es la lección más cara del proyecto.

---

## Catálogo de ítems de inspección — sembrado el 2026-08-02

Fuente de las plantillas `furgon_liviano_v1` y `camion_v1`. **Vive acá y en la
base, no en una conversación** — es lo que faltaba cuando se afirmó que estaba
escrito y no lo estaba.

### Criterio de bloqueante — las cuatro condiciones, todas

1. Puede causar accidente, inmovilización por autoridad o varada **hoy**
2. El conductor lo verifica **sin herramienta**
3. En **menos de 20 segundos**
4. Con respuesta **binaria y objetiva**, no un juicio

Si un ítem falla cualquiera de las cuatro, no es bloqueante. La cuarta es la que
más se olvida: "¿está bien la suspensión?" no es binaria y no puede bloquear.

### Bloqueantes — orden fijo, mismo día

| # | Ítem | Gesto | Aplica |
|---|---|---|---|
| 1 | Freno de servicio | Motor encendido, pisar a fondo y sostener 5 segundos. ¿El pedal sigue hundiéndose o llega al piso? | ambos |
| 2 | Freno de estacionamiento | En pendiente, aplicar y soltar el pedal 3 segundos. ¿Se mueve el vehículo? | ambos |
| 3 | Llantas: flanco, labrado y tuercas | Recorrer todas las posiciones. ¿Abultamiento o herida en el costado? ¿Labrado en el testigo? ¿Tuerca floja o faltante? | ambos |
| 4 | Nivel de refrigerante y aceite de motor | Motor frío. ¿Alguno por debajo del mínimo? | ambos |
| 5 | Fuga activa de frenos, combustible o refrigerante | Mirar el piso bajo el vehículo. ¿Charco o goteo activo? (Sudado de aceite es mayor, no bloqueante.) | ambos |
| 6 | Luces traseras: stop, direccionales y cocuyos | Con ayuda o contra una pared. ¿Alguna no enciende? | ambos |
| 7 | Limpiaparabrisas y lavador | Activarlos. ¿Barren limpio o rayan? ¿Sale agua? | ambos |
| 8 | Puertas del furgón aseguran | Cerrar y jalar. ¿Quedan trabadas? | **solo camión** |
| 9 | Documentos y equipo reglamentario | SOAT, tecnomecánica y licencia vigentes. Extintor con carga y sin vencer, dos señales, dos tacos, repuesto con aire, gato y cruceta. | ambos |

**Furgón liviano: 8 bloqueantes** (todos menos el 8). **Camión: 9.**

### Mayores — 7 días

Espejos · batería y bornes · escape y soportes *(bloqueante si entra gas a la
cabina)* · sudado de aceite · luces de reversa · **alarma de retroceso (solo
camión)** · cinturón de seguridad · holguras de suspensión · botiquín · chaleco
reflectivo · linterna · caja de herramienta · **drenaje del separador de agua
(solo camión diésel, periodicidad SEMANAL)**.

### Menores — 30 días

Golpes y rayones · tapones de ruedas · radio y antena · aire acondicionado ·
limpieza · accesorios varios.

---

### Lo que NO va al chequeo diario, y por qué

Esta lista importa tanto como la otra, y está acá **para que nadie la agregue en
tres meses con buena intención**:

> Espesor de pastillas y bandas · estado de amortiguadores · juego de terminales
> de dirección · rodamientos · compresión del motor · estado del turbo ·
> alineación · balanceo · correa de repartición.

Nada de eso lo puede evaluar un conductor en patio. **Cada ítem incontestable en
la pantalla diaria entrena el reflejo de marcar óptimo sin mirar** — es la regla
11 en su forma más concreta. Todo eso va al plan preventivo por kilómetro,
ejecutado en taller.

### Dos decisiones de presentación que son de diseño, no de estética

**Los bloqueantes van primero y en orden fijo.** Se citan por número y el orden
es memoria muscular útil: el freno siempre es el 1.

**Los no bloqueantes se muestran en orden aleatorio cada día.** Con orden fijo,
a la tercera semana el pulgar responde sin leer. La aleatoriedad va sembrada por
fecha, así que es reproducible: dos conductores el mismo día ven el mismo orden
y una inspección se puede auditar.

**Cada ítem lleva su gesto en pantalla**, no en un manual aparte. Sin el gesto,
la criticidad es decorativa: "revisar frenos" no dice qué hacer, "pisar a fondo
y sostener 5 segundos" sí. Por eso `gesto` es NOT NULL con CHECK de no-vacío.

---

### Estado del catálogo — 2026-08-03

Sembrado en código (`flota/adaptadores/catalogo.py`) y en dos tablas
versionadas. **Falta correr el sembrado en producción** tras aplicar la
migración `f10ta2plantillas`:

```bash
venv/bin/python scripts/sembrar_plantillas_flota.py
```

Es idempotente y no pisa lo que exista.

| Plantilla | Bloqueantes | Total ítems |
|---|---|---|
| `furgon_liviano_v1` | 8 | 25 |
| `camion_v1` | 9 | 28 |

Un test comprobó lo que faltaba en la redacción: **el gesto de "Documentos y
equipo reglamentario" era una enumeración, no una pregunta contestable** — falla
la cuarta condición del criterio. Se le agregó "¿Falta alguno, o hay alguno
vencido o descargado?". Enumerar no es preguntar, y una lista sin pregunta se
responde de memoria a la tercera semana.

---

## Tanda 3 — anotado el 2026-08-01, no construido

### Llegan 2 motocarros (Neiva y Pitalito, pedidos express urbanos)

**Necesitan plantilla propia `motocarro_v1`.** No es el checklist de camión con
la mitad de los ítems en N/A: un formulario lleno de casillas inaplicables
entrena a marcar todo sin leer, que es exactamente lo que la regla 11 persigue.

Diferencias que cambian la plantilla, no solo los valores:

| | Camión | Motocarro |
|---|---|---|
| Posiciones de llanta | 4 o 6 | **3** |
| Frenos | un sistema | **delantero y trasero por separado** |
| Transmisión final | cardán | **cadena** — lubricación es tarea de rutina |
| Furgón | sí | **no** |

Por eso `ficha_tecnica` ganó `transmision_final` (2026-08-01): sin ese campo la
tarea de lubricación de cadena no se puede derivar de la ficha, y en un camión
con cardán ni siquiera existe.

### `capacidad_kg` deja de ser opcional

Hoy es nullable en `vehiculos` y el alta solo exige placa y tipo. Cuando
`decision_ruta` reasigne, **necesita capacidad, no solo disponibilidad**: un
motocarro no reemplaza un camión, y un reasignador que solo mira "¿está libre?"
va a mandar tres toneladas en tres llantas.

Se mide antes de imponerlo: cuántos vehículos activos tienen `capacidad_kg` en
`NULL`. Medir → corregir → imponer, como el resto.

### Política express — definida, sin implementar

- Pedido mínimo **$70.000**
- Flete **$5.000**
- Se cobra **aunque el cliente tenga reparto programado** — paga la excepción,
  no la entrega

Dos cosas que hay que vigilar y que salen del mismo registro:

1. **¿El flete cubre el costo real?** Tres entregas en una salida son $15.000
   contra combustible, conductor prorrateado y desgaste. Una sola entrega a
   $5.000 probablemente no. Si a los dos meses el express está en pérdida, el
   número a mirar es entregas por salida, no el precio del flete.
2. **¿Quién llama?** Si en tres meses los mismos cinco clientes concentran el
   60% de los express, eso no es urgencia: es que su frecuencia de reparto está
   mal calibrada. Se arregla cambiando la ruta, no cobrando flete.

### Métrica pendiente — medición manual

**Urgencias urbanas por mes en Neiva y Pitalito, con cliente y producto.**

Sin ese número no se dimensiona nada: ni cuántos motocarros, ni si el flete
cubre, ni si el problema es la ruta. Es medición manual porque hoy las urgencias
no dejan rastro en ningún sistema — se piden por teléfono.

Lleva canon antes de publicarse (`docs/flota/canones/`): qué cuenta como
urgencia urbana, desde qué momento, y qué pasa con la que se pidió y se canceló.

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

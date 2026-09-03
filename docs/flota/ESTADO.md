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

### Para la capacitación de Yesid — decirlo con estas palabras

**"No encontrado" es una respuesta válida al registrar un documento**, y hay que
decírselo **antes** de que salga a buscar papeles.

Si cree que solo puede registrar lo que encuentre, lo que no encuentre se le
queda en la cabeza — y ese es justo el dato más grave que puede levantar: un
vehículo sin SOAT localizable es un hallazgo bloqueante, no un campo vacío.

El sistema lo distingue en tres estados —`vigente`, `no_encontrado` y
`sin_verificar`— y los cuenta por separado en el health. Pero esa distinción solo
sirve si quien llena el formulario sabe que existe.

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

## Cierre forzado de custodia — 2026-08-03

Un conductor **no puede** abrir custodia sobre un vehículo que tiene otro. El
mensaje nombra a la persona y dice qué tiene que pasar: *"El WHX245 lo tiene
Víctor desde 02/08 a las 17:30. Si lo vas a recibir vos, Víctor tiene que cerrar
su turno primero."* Eso convierte una restricción de base en una conversación
entre dos personas, que es lo que de verdad resuelve el problema del custodio.

Un **admin de zona sí puede**, porque es la única salida cuando alguien se fue
sin cerrar y el camión tiene que salir a las 5 a.m.

### Qué cuenta como forzado, y no es lo que parecía

**Forzado = cerrar el turno ajeno SIN fotos de cierre.** No es "cerrar el turno
ajeno" a secas.

La definición se afinó al implementarla: el daño concreto no es la falta de
firma, es que **el turno siguiente arranca sin nada con qué comparar** y el
próximo golpe que aparezca no se le puede atribuir a nadie. Un admin que cierra
con las ocho fotos hizo un cierre completo — solo le faltó la firma del titular,
y ese caso no debe contaminar el contador.

La base impone el rastro: un forzado sin autor ni motivo **no entra ni por SQL
crudo**. Un rastro de que pasó algo raro sin quién lo autorizó es peor que no
tener rastro.

### La notificación al custodio anterior — PENDIENTE de tanda 2

**No está implementada, y hay que decirlo.** El aviso por WhatsApp llega con el
adaptador de Gupshup en la tanda 2. Construir ahora una cola de avisos sin
consumidor sería superficie sin estrenar — el patrón que ya apareció cinco veces
en este repo.

Lo que sí hay: el dato completo está en la fila —quién lo tenía, quién forzó,
cuándo y por qué— y **el tablero de flota lo muestra con nombre**, arriba de
todo, en `GET /flota/custodia/cierres-forzados`.

> **Pero eso lo ve Yesid el lunes, y si el custodio anterior se entera tres días
> después el daño de confianza ya está hecho.**
>
> **La solución de verdad no es técnica y va al procedimiento FLO-PR-01,
> sección 4:** *quien fuerza un cierre le avisa al custodio anterior el mismo
> día*, por el mismo WhatsApp por el que se hablan todos los días. El sistema
> registra y muestra; la cortesía la pone la persona.
>
> **La regla existe en el papel antes que en el código, y eso queda declarado.**
> Cuando el notificador exista en la tanda 2, deja de depender de que alguien se
> acuerde — pero hasta entonces depende de eso, y fingir lo contrario sería
> peor que decirlo.

**FLO-PR-01 §4.1.1 — cerrado el 2026-08-03.** El procedimiento ya contempla el
caso completo: vehículo con turno abierto por otro conductor, quién puede forzar
el cierre, la obligación de avisar al custodio el mismo día, y por qué el cierre
sin las ocho fotografías se contabiliza aparte.

> **El `.docx` vive fuera del repositorio**, así que desde acá no se puede
> verificar y no hay que volver a reportarlo como hueco. Esta línea es el único
> rastro de que existe: sin ella, la próxima revisión del código vuelve a
> concluir que el procedimiento no cubre el caso — porque desde el repo se ve
> igual que si no existiera.

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

### Módulo de tanqueo — primera candidata de la tanda (agregado 2026-08-03)

Hoy el odómetro suelto ya acepta origen `tanqueo`: registra el kilometraje y la
foto del tablero. **Lo que no registra es galones ni valor**, y sin eso no se
calcula nada.

```
tanqueo
  vehiculo_id, lectura_odometro_id
  fecha, galones, valor_total, valor_galon
  estacion, tanque_lleno (bool)
  foto_factura_id      ← foto_dato, no evidencia_estado
  registrado_por_usuario_id
```

**Regla dura:** si `tanque_lleno = false`, el rendimiento de ese tramo es
`SIN_DATO`, **no un número calculado**. El rendimiento solo es válido de tanque
lleno a tanque lleno; el tanqueo parcial es la forma más común de contaminar
esta métrica, y un promedio contaminado es peor que no tenerlo — se usa igual.

Por qué vale más de lo que parece: el combustible es el rubro más grande de la
flota, por encima del mantenimiento, y hoy no se mide por vehículo. Además un
vehículo que baja de rendimiento sin explicación está avisando de un problema
mecánico antes de que se manifieste (inyectores, filtro de aire, frenos que
arrastran, presión de llantas). Y `valor_total` da precio por galón por
estación: en seis meses se sabe dónde cobran más caro.

**Es la única pieza de tanda 2 que no depende de hallazgos ni inspecciones** —
solo del odómetro, que ya funciona. Por eso va primera: genera datos desde el
día uno, mientras las notificaciones todavía no tienen qué notificar.

**Sin código, desde ya:** que los conductores guarden la factura de cada
tanqueo. Cuando exista la pantalla se carga el histórico y se arranca con dos o
tres meses de serie en vez de cero. Va en la capacitación del lunes.

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
posicionales: reordenarlos manda la dirección donde va la fecha, sin error).

**Enviadas a aprobación el 2026-08-03**; al 2026-08-05 las tres ya existen en
WhatsApp Manager. Lo que la pantalla de plantillas muestra como *Not rated* es
la **calificación de calidad**, no el estado de aprobación — son dos cosas
distintas y solo la segunda habilita el envío.

> **Implementado el 2026-08-05, apagado:** `flota_documento_vence` ya tiene
> tubería completa —dominio, canal, tabla, barrido, callback y pantalla— porque
> `DocumentoVehiculo` existe y se está cargando. Las otras dos **no se
> implementaron a propósito**: no existe la tabla `flota_hallazgo` ni un
> endpoint para crear hallazgos, así que no hay de qué avisar. Construirlas hoy
> sería superficie sin estrenar, que es la lección más cara del proyecto.
>
> Falta para encenderlo: los **ids definitivos de Gupshup** (los de abajo son
> los `temp` de mientras estaban `Pending`), `GUPSHUP_TEMPLATE_IDS`,
> `FLOTA_AVISO_TELEFONOS`, `FLOTA_AVISOS=true` y —como segunda decisión
> separada— `FLOTA_AVISOS_REALES=true`. El panel de flota muestra en qué estado
> está sin que haya que ir a mirar variables.

Las tres, tal como se enviaron:

| Plantilla | Destinatario | Cuerpo |
|---|---|---|
| `flota_hallazgo_bloqueante` | Admin de zona + Yesid, al instante | `Vehículo {{1}}: se reportó un hallazgo bloqueante — {{2}}. Debe atenderse hoy, antes de que el vehículo salga a ruta.` |
| `flota_hallazgo_vencido` | Yesid; +2 días sin cerrar → Santiago | `Vehículo {{1}}: el hallazgo "{{2}}" venció hace {{3}} y sigue abierto. Si no se cierra hoy, el sistema lo escala automáticamente.` |
| `flota_documento_vence` | Yesid, 15 días antes | `Vehículo {{1}}: el documento {{2}} vence el {{3}}. Programá la renovación antes de esa fecha.` |

#### Tres trampas resueltas en la redacción, y que el adaptador tiene que respetar

**1. Las variables llevan la unidad y el artículo adentro.** Una plantilla de
WhatsApp no admite condicionales, así que no puede concordar número ni género:

- `{{3}}` de `hallazgo_vencido` recibe **`"3 días"`**, no `3`. Si recibiera el
  entero, con valor 1 el mensaje diría *"venció hace 1 días"*.
- El cuerpo dice `el documento {{2}}` y no `el {{2}}`, porque con
  `{{2}} = tecnomecánica` saldría *"el tecnomecánica"*.

**2. La fecha va en palabras y en hora Bogotá.** `{{3}}` de `documento_vence`
recibe `"15 de agosto de 2026"`. El ISO se lee como un error del sistema —es el
`2026-07-15` que salió a producción en cartera— y el numérico es ambiguo.

> **Resuelto el 2026-08-05.** `flota/adaptadores/medicion.py::_hoy()` usaba
> `date.today()`, aislado en una función esperando exactamente este momento. Ya
> devuelve `dia_operativo()`. Al seguir la raíz aparecieron dos sitios más en
> `flota/` con el mismo problema —`vencido`/`dias_para_vencer` de cada documento
> y la ruta del día del conductor—: el trinquete de fechas solo vigilaba `app/`,
> y `flota/` es un paquete hermano. Ahora enumera sus raíces y verifica que cada
> una exista.

**3. Cada plantilla tiene DOS identificadores.** Descubierto al enviarlas:

| Plantilla | Facebook temp ID | Gupshup temp ID |
|---|---|---|
| `flota_hallazgo_bloqueante` | `1755955632267884` | `c1af2a31-084d-4681-a3f0-aaf60e531498` |
| `flota_hallazgo_vencido` | `1422079923106226` | `25da9107-feb5-4c4b-869c-70a7b07759f2` |
| `flota_documento_vence` | *(aún sin asignar)* | `bd442d44-8308-4d26-bb1c-c2d038811893` |

> **Para enviar por la API de Gupshup va el de Gupshup.** Dos identificadores
> parecidos para la misma cosa y uno solo funciona: es la forma más pura del bug
> que costó semanas en cartera.
>
> **Y los de arriba dicen `temp`.** Son los de mientras está `Pending`. Los que
> entran a `GUPSHUP_TEMPLATE_IDS` son los definitivos, leídos cuando el estado
> pase a `Approved` — anotar el temporal produce un `submitted` que no entrega
> nada.

SMS queda **fuera de alcance**: cuesta más, se lee menos, y con tres
destinatarios internos que usan WhatsApp todo el día no aporta.

**Orden:** esto es tanda 2 y no puede adelantarse. Primero tiene que existir el
hallazgo con plazo — que es lo que se notifica. Notificar antes de que haya qué
notificar es superficie sin estrenar, que es la lección más cara del proyecto.

---

## Decisión: sin `simulado`, se resetea al pasar a producción (2026-08-03)

Se descartó agregar `simulado` a ficha, custodia y odómetro. **Se prueba contra
producción con datos reales y en el corte se resetea.** Es decisión de Santiago
y ahorra trabajo.

### Pero el plan dependía de un script que no conocía flota

`scripts/reset_transaccional.py` **no mencionaba una sola tabla de flota.** El
plan era correcto y el mecanismo no lo cumplía: las fichas y custodias del
ensayo habrían sobrevivido al corte sin que nadie lo notara — justo lo que un
`simulado` habría evitado.

Corregido, y la clasificación no es obvia:

| Se vacía en el corte | Se protege |
|---|---|
| `flota_custodia` | `flota_ficha_tecnica` |
| `flota_lectura_odometro` | `flota_documento_vehiculo` |
| `flota_foto` | `flota_plantilla_inspeccion` · `flota_item_inspeccion` |

**Ficha y documentos NO se borran**, y ese es el punto que casi se pierde: son
media mañana recorriendo cinco vehículos con la foto del tablero y la medida de
llanta en la mano. Borrarlas en el corte obliga a repetir el levantamiento, **y
la segunda vez nadie la hace.** Las plantillas tampoco: borrarlas dejaría las
inspecciones viejas apuntando a ítems que ya no existen.

### Los archivos del volumen no los borra un DELETE

Vaciar `flota_foto` borra las filas, no los archivos. Quedan huérfanos ocupando
disco, y **el volumen de Railway tiene tamaño fijo: llenarse en silencio es el
próximo modo de fallo de este diseño** — a partir de ahí toda foto nueva cae en
`pendiente_evidencia`.

`reset_transaccional.py --fotos` los limpia, y **corre después de vaciar las
filas, nunca antes**: al revés dejaría referencias apuntando a nada, que es peor
que un archivo de más — un hueco silencioso contra disco ocupado.

**Trinquete 11:** toda tabla de flota tiene que estar clasificada en el reset.
Quedar fuera de las dos listas significa sobrevivir por accidente.

---

## Las fotos no se guardaban — 2026-08-03

**Durante tres días el módulo pidió fotos y las tiró.** El frontend mandaba:

```js
storage_ref: 'inline://pendiente-subida',
hash_sha256: '0'.repeat(64),
```

El navegador comprimía la imagen, mandaba el tamaño y las dimensiones, y la
imagen se descartaba. La fila decía que había una foto del tablero, con una
referencia que no apuntaba a nada y un hash de ceros.

**Y el formulario exigía la foto del tablero para dejar guardar.** Obligaba a
tomar una evidencia que después tiraba.

Es exactamente lo que este módulo existe para impedir. La regla 7 dice *"la base
guarda referencia, hash, bytes y dimensiones — nunca el binario"*: se implementó
la mitad de "nunca el binario" y **nunca la referencia**. `AlmacenDeFotos` estaba
declarado como `Protocol` en `puertos.py` sin una sola implementación.

> **Lo que lo hacía indetectable:** los 1088 tests pasaban. Ninguno guardaba una
> foto y la volvía a leer, porque no había con qué. El guard de huérfanos no lo
> vio —el `Protocol` no es un endpoint— y el de fotos-fuera-de-la-base tampoco:
> **estaba en verde justamente porque no se guardaba nada.**

### Arreglado, y por qué así

`flota/adaptadores/almacen_fotos.py`, detrás del `Protocol`. Volumen de Railway,
no S3: es lo que existe el lunes sin cuenta nueva ni credenciales. El día que
haga falta R2 se cambia esa clase y el dominio no se entera — para eso estaba
declarado el puerto.

**Direccionado por contenido:** el archivo se llama como su propio SHA-256. La
misma foto dos veces ocupa un archivo —ocho ángulos por turno, cinco vehículos,
todos los días— y **el hash no se puede falsear**: si el archivo cambia, deja de
coincidir con su nombre.

**El cliente ya no decide `storage_ref` ni `hash_sha256`.** Manda la imagen; el
servidor la escribe y pone la ruta y el hash reales. Un test manda una referencia
falsa y exige que se ignore.

**Si el almacén falla, la fila queda `pendiente_evidencia` con el motivo escrito
y el turno se registra igual.** Bloquear el traspaso porque el disco falló deja
el camión en el patio a las 5 a.m.; callarlo es peor. El health lo cuenta.

### Configuración

`FLOTA_FOTOS_DIR` apuntando al volumen montado. **Sin default silencioso**: si
falta, el almacén levanta. Un default a `/tmp` guardaría las fotos y las perdería
en el próximo deploy — peor que un hueco visible, porque da una evidencia que se
evapora.

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

### Restricción de diseño para la pantalla de preoperacional

Anotada antes de construirla, que es cuando sirve.

El conductor la va a usar **en un celular, en patio, a las 5 a.m., con lluvia y
posiblemente con guantes.** Ahí lo que decide si el dato es bueno no es el color:

- **Los tres botones de respuesta —óptimo, no óptimo, N/A— van grandes y bien
  separados.** Un error de pulgar entre "óptimo" y "no óptimo" produce un dato
  falso que **nadie va a detectar nunca**: no hay excepción, no hay log, no hay
  test que falle. Es la misma familia que un nombre que miente, con la
  diferencia de que lo genera el hardware humano.
- Contraste alto y placa legible sin acercar el ojo.

No es branding. Es que la calidad del dato depende del tamaño del botón.

### Métrica pendiente — medición manual

**Urgencias urbanas por mes en Neiva y Pitalito, con cliente y producto.**

Sin ese número no se dimensiona nada: ni cuántos motocarros, ni si el flete
cubre, ni si el problema es la ruta. Es medición manual porque hoy las urgencias
no dejan rastro en ningún sistema — se piden por teléfono.

Lleva canon antes de publicarse (`docs/flota/canones/`): qué cuenta como
urgencia urbana, desde qué momento, y qué pasa con la que se pidió y se canceló.

---

## `dias_hallazgo_abierto` — canon cerrado el 2026-08-03

Definido por Santiago en `docs/flota/canones/dias_hallazgo_abierto.md`,
implementado en `flota/dominio/hallazgo.py`, con **23 tests derivados del
documento y escritos antes del cálculo**.

Lo que el canon fija y que no era obvio:

- **Para cuando el vehículo vuelve reparado**, no al aprobar la OT ni al entrar
  al taller. Mide riesgo real, no gestión: si cerrara al aprobar, un vehículo
  tres semanas en taller mostraría el indicador limpio. Hay un test con ese
  escenario exacto.
- **El aplazamiento no congela el reloj.** Mueve la fecha límite, no borra el
  tiempo. Si congelara, aplazar sería la forma fácil de limpiar el tablero.
- **La línea base queda excluida, sin reloj y sin responsable.** El desorden
  viejo no entra al indicador: la primera inspección de cada vehículo levanta lo
  que ya había, y la cuenta empieza al día siguiente con todo fechado y con
  nombre.
- **Un hallazgo abierto vale `sin_dato`, jamás 0.** Cero diría "se resolvió al
  instante". Lo que sí existe es cuántos días LLEVA, y ese es otro número
  (`dias_transcurridos`) — el aviso de WhatsApp usa ese, el indicador usa el otro.

### El caso THP 696 quedó con la magnitud fijada y el valor puntual abierto

Entre **32 y 38 días**, según qué par de fechas se tome del rango de inspección
(6–11 oct) y de las dos órdenes de cierre (12 y 13 nov). Las cuatro
combinaciones están probadas.

**No se puede cerrar más, y eso es información:** el canon dice que el reloj
arranca en el timestamp del reporte porque *"el reporte es digital y fechado"*.
THP 696 es un caso de papel — no tiene timestamp, tiene un rango de seis días.
Esa imposibilidad es exactamente el problema que el sistema resuelve. Falta el
par exacto de fechas para tener un valor reproducible.

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

---

## 2026-08-03 — la tarde que la app se usó de verdad

Nueve defectos en una tarde de uso real, con 1193 tests en verde. Ninguna
corrida de tests podía encontrarlos, y eso no es un fallo de los tests: **un
test encuentra lo que alguien pensó en afirmar; una persona usando la app
encuentra lo que nadie pensó.** Ocho de los nueve tienen hoy un test que falla
si reaparecen.

| Qué se rompía | Por qué ningún test lo veía |
|---|---|
| "Entregar turno" ejecutaba un recibo | Un solo `onclick` con el texto cambiando. No fallaba — abría una custodia nueva. Nueve toques → **nueve custodias de 0 km** en el THP696 |
| La placa tapada por `#banner-modo` | `z-index` 9999 vs 900. Nadie pensó en afirmar que la placa fuera *visible*, solo que estuviera |
| Fotos guardadas **sin ángulo** | El orden tampoco las identifica: el frontend filtra las faltantes antes de enviar |
| Hora en UTC (20h cuando eran las 15h) | `datetime.utcnow().isoformat()` no dice de qué zona es; JS la lee como local |
| "Ver foto" = `<a target=_blank>` con JWT en header | 401 siempre. **Nunca funcionó**, y como nadie lo abrió nadie lo supo |
| Selector de origen ofrecía `ot` y `preoperacional` | Y el endpoint aceptaba además `entrega` — un cambio de turno inventado |
| `Carlos Pérez · undefined` | El guard de datos personales quita la cédula; la pantalla imprimía el hueco |
| Una sola foto `llantas` para 4 o 6 ruedas | Un flanco herido está en una rueda concreta |
| CSS de flota duplicado, con un `@media` anidado imposible | 36 líneas muertas (≥1024 **y** ≤480) |

### Decisiones que quedan

| Decisión | Motivo |
|---|---|
| **Ubicación ≠ custodia**, dos columnas y un CHECK | Si el camión duerme en casa del conductor y la ubicación arrastra la custodia a `sede`, se descarga de responsabilidad a la única persona que lo tiene. Va en la base porque es la clase de regla que un refactor borra sin notarlo, y su consecuencia aparece meses después en una discusión sobre quién paga un golpe |
| **Recibir 13 fotos, entregar 4** | Si la entrega exige 13 a las 6 p.m. en un patio, a la tercera semana se toman 13 del piso. Cuatro que se toman bien valen más que trece que se falsifican. Las cuatro son las mismas del recibo: sin el mismo encuadre no hay con qué comparar |
| Idempotencia de 90 s en el traspaso, devolviendo **201** | Regla 9: un timeout no significa que falló. Y un rojo en la cara del conductor lo hace tocar de nuevo — que es exactamente cómo se produjeron las nueve filas |
| `flota_limpiar_vehiculo.py` en vez de `reset_transaccional.py` | El segundo es el acta de corte: vacía picking, packing, recepciones, rutas y movimientos. Y sin `--ejecutar` no hacía nada, con `--ejecutar` hacía demasiado |
| Umbrales decididos **antes** de medir: recibir ≤ 3 min, entregar ≤ 40 s | Con el corte ya elegido (salen las llantas: una llanta en mal estado es hallazgo de inspección, no de traspaso). Si no, la decisión se toma a las 6 p.m. con la gente esperando |

### Lo que sigue sin ejercerse

Cuatro superficies nuevas —entregar turno, ubicación, "cómo estaba",
idempotencia— y **ninguna la tocó una persona**. Pendiente la corrida real con
el camión y los cinco números: recibir, entregar, legibilidad del odómetro,
utilidad del "cómo estaba", y si la referencia abre instantánea.

---

## `flota_hallazgo` — la tabla donde nace un daño (2026-09-01)

Pedido literal: *«llevar control sobre los daños que pasan»*.

### Lo que había

`flota/dominio/hallazgo.py`: 153 líneas de política, canon cerrado por Santiago
el 2026-08-03, 23 tests escritos antes del cálculo — y **cero callers**. Los 53
ítems de inspección estaban sembrados en producción esperando una tabla que no
existía. Un daño no podía nacer.

Es el mismo patrón que este módulo lleva encontrando toda la semana: capacidad
construida, probada y desplegada, y el gesto que la enciende nunca escrito.

### Alcance: el hallazgo solo, sin la inspección

Un daño no necesita la inspección completa para nacer. Hoy llega por tres vías
—inspección diaria, recibo de turno, alguien que vio un golpe en el patio— y la
primera **no tiene pantalla**. Atarlo a `inspeccion_id` lo habría dejado sin
poder nacer por las dos vías que sí ocurren.

Por eso la columna no existe. Una FK que apunta a una tabla ausente no es
previsora, es rota.

### Las cuatro decisiones que no son de quien llama

| | Cómo se resuelve | Por qué no lo elige el cliente |
|---|---|---|
| `fecha_limite` | Se calcula de la criticidad (regla 6) | Si se pudiera escribir a dedo, «bloqueante» sería una etiqueta que se negocia |
| `custodia_id` | Se deriva de la custodia activa | Dejarlo en el cuerpo sería dejar elegir a quién se le cuelga el daño (regla 2) |
| `linea_base` | Se deriva de la custodia | El desorden viejo no le cuenta a quien lo encontró |
| `lectura_id` | NOT NULL, con la lectura anclada en la misma transacción | Regla 3, impuesta por la base y no por acordarse |

### El plazo es un DÍA, no un instante

`bloqueante = 0 días` se guardó como **el final del día de Bogotá**, no como
`reportado_ts`. Con lo segundo, todo bloqueante sale vencido un segundo después
de nacer, el rojo deja de distinguir y el tablero se deja de mirar — la lección
de los 639 avisos conocidos.

Y el día se calcula **en Bogotá**: a las 8 p.m. de Colombia, en UTC ya es el día
siguiente, así que `utcnow().date()` hacía nacer vencido cualquier bloqueante de
la noche. Regla 5 del WMS aplicada al plazo.

### El odómetro se ancla, no se fabrica

`_anclar_odometro` reutiliza la última lectura si el kilometraje coincide, y
solo escribe una nueva si el número cambió.

Escribir una lectura por cada hallazgo habría producido desde adentro del
sistema el mismo patrón que `lecturas_ts_duplicado` existe para contar —diez
lecturas con el mismo segundo, de un reintento— y el contador habría dejado de
distinguir un reintento de la operación normal. Tres hallazgos de una misma
revisión cuelgan de la misma lectura, que es lo que de verdad pasó.

El vocabulario de `origen` se ensanchó con `'hallazgo'`. Reusar `preoperacional`
u `ot` sería una lectura que dice de dónde vino y miente; la columna existe
justamente para no adivinarlo.

### Quién puede qué

**El conductor reporta y no cierra.** Si quien reporta también cierra, el camino
barato es reportar y descartar en el mismo minuto (regla 11) y el registro queda
decorativo. Reportar es `LECTURA_FLOTA` —el que ve el golpe es el que maneja—;
cerrar, descartar y aplazar son `MAESTROS_FLOTA`.

### La medida nace con la tabla

`hallazgos_abiertos` y `hallazgos_vencidos` salen en `/flota/health` **y** en el
bloque de salud del tablero, desde el primer día. `flota_lectura_odometro` vivió
un mes con cero campos en el health y por eso nada avisó de las 26 lecturas sin
foto ni del salto de 16,3 millones de km. Contadores separados: sumarlos esconde
el vencido, que es el único que urge.

### Lo que se descubrió al construirlo, no al planearlo

| Hallazgo | Quién lo encontró |
|---|---|
| La URL de `/aplazar` armada con `+ verbo` dejaba el endpoint sin consumidor | El trinquete de rutas huérfanas, y tenía razón: una URL que solo existe en tiempo de ejecución no se puede auditar leyendo el repo |
| El fixture de PostgreSQL borraba `flota_lectura_odometro` sin borrar antes `flota_hallazgo` | La corrida contra el motor real. Misma forma que ya costó dos veces en `reset_transaccional.py` |
| `motivo_cierre` iba a guardar los aplazamientos de filas ABIERTAS | Revisión antes de la migración. Se separó en `bitacora`: un nombre que promete una cosa y contiene otra se lee con confianza y se lee mal |
| `_colgar_fotos` quedó duplicado palabra por palabra entre `traspaso` y `hallazgos` | Regla 0, corolario. Unificado en `almacen_fotos.colgar_fotos` |
| El arnés de mutación reportaba «sobrevivió» sobre tests que **se saltaban** | `node` fuera del PATH recortado del subproceso. Un skip en verde, producido por el propio arnés que persigue skips en verde |

### Verificación

· 32 tests de dominio+adaptador, 24 de frontera HTTP, 5 de render — todos contra
  la base real, ninguno contra mocks.
· **26 mutaciones aplicadas, 26 muertas.** Incluyen las dos direcciones de cada
  guard: que dispare, y que NO dispare sobre operación sana.
· Suite completa: **3110 passed, 1 skipped, 33 deselected** contra SQLite
  en memoria, el 2026-09-01 (venía de 3048 antes de esta tanda). Regla 13:
  el número es de esa corrida y de ese motor, no de producción.
· **33/33 contra PostgreSQL 17 local** — el CHECK de desenlace es el único de
  flota escrito con `CASE WHEN`, forma elegida para no repetir el bug de
  booleanos sumados que costó el release del 2026-08-01.
· `flask db upgrade` completo contra PostgreSQL limpio hasta `m017flotahallazgo`,
  y `downgrade` de vuelta a `m016`, verificando el CHECK ensanchado y el
  estrechado.

### Lo que NO se hizo, y por qué

| | Motivo |
|---|---|
| Fotos de hallazgo en la pantalla | El endpoint las acepta y las cuelga (`entidad_tipo='hallazgo'` estaba en el CHECK desde la tanda 1). La captura desde el celular no se conectó: **regla 12** — primero que alguien reporte un daño de verdad |
| La inspección diaria | Es la tanda 2 y tiene su propio alcance. El hallazgo se cuelga de ella cuando exista, con `item_id`, que ya está |
| Aviso de WhatsApp por hallazgo vencido | `flota_hallazgo_vencido` está en `PLANTILLAS` desde la tanda 1 y **no está aprobada en Meta**. La deuda es de aprobación, no de código |
| Umbral de aplazamientos («más de N ya es evitar») | Regla 13: no hay una sola medición todavía. El contador existe para poder fijarlo dentro de un mes |

### Lo que sigue sin ejercerse

**Nadie ha reportado un daño real.** Cinco endpoints, una tabla, dos medidas y
una pantalla, y cero uso. La compuerta de esta tanda es un conductor reportando
un golpe desde el celular en el patio, y hasta que eso pase esto es superficie
construida — que es exactamente lo que la regla 12 existe para contar.

---

## `flota_gasto` — la plata que sale (2026-09-02)

Pedido del plan: contestar *«¿cuánto me cuesta el kilómetro del TGZ653?»*, que el
ERP no contesta hoy **y no porque le falte la plata: le falta el kilómetro**.

### Lo que había, y lo que faltaba

`flota/dominio/costos.py`: 415 líneas de cálculo, ocho funciones, los cuatro
vocabularios de los que la tabla saca sus CHECK — y **cero tests y cero
callers**. `flota_gasto` y `flota_tanqueo` existían con sus 20 CHECK y su
migración verificada contra PostgreSQL. Faltaba todo lo que convierte eso en una
capacidad: la puerta, la pantalla, el canon y la medida.

El trinquete de cobertura estaba **rojo por eso mismo**, y tenía razón. Es el
patrón «función sin caller» que este módulo lleva pagando toda la semana,
detectado esta vez por el guard antes que por un auditor.

### El canon del CPK — con una debilidad declarada arriba de todo

`docs/flota/canones/costo_por_kilometro.md`. Fija la definición, el reparto, los
dos ceros y los tres `sin_dato`. Caso trabajado a mano: **$840,00 por kilómetro**
sobre marzo de 2026, con $730.000 de SOAT repartidos a $2.000/día ($62.000 en 31
días), $588.000 de combustible, $190.000 de mantenimiento y 1.000 km de odómetro.
El mismo mes por el lado del combustible da **24,0 km/galón agregando** contra
24,25 promediando las razones — dos números que existen y solo uno es el
rendimiento.

**Tres cosas que el canon dice de sí mismo y que no se disimularon:**

1. **Lo llenó la misma mano que escribió el código.** `PLANTILLA.md` dice que
   no debe ser así. Queda escrito en el encabezado en vez de fingir procedencia:
   un canon que miente sobre quién lo llenó vale menos que ninguno.
2. **Los insumos del §5 son sintéticos**, elegidos para que cada división sea
   exacta. No hay un solo peso de flota registrado en producción, así que **la
   magnitud no está fijada** — a diferencia de `dias_hallazgo_abierto`, que tiene
   el THP 696 de papel. El §9 dice qué falta para cerrarlo.
3. **Corrigió una afirmación del código contra la medición.** El docstring de
   `imputar_a_ventana` decía que «con Decimal y en este orden, los doce meses
   suman exacto». Medido: $730.000 y $1.860.000 suman exacto **en los dos
   órdenes**, y $1.000.000 no suma exacto en ninguno. Lo que decide la exactitud
   es que el valor sea divisible entre los días del período, no el orden. La
   decisión de multiplicar primero se conserva —un redondeo en vez de dos— y la
   justificación se ajustó. Regla 13.

### Lo que se construyó

| | Dónde |
|---|---|
| Tests del dominio, derivados del canon | `tests/flota/test_costos.py` — 77 |
| Adaptador: dos verbos y la única consulta del CPK del repo | `flota/adaptadores/gastos.py` |
| Endpoints: listar con CPK, registrar gasto, registrar tanqueo | `flota/api/gastos.py` |
| Pantalla: expediente de plata por vehículo | `flota.js` · botón «Gastos» |
| Health: cuatro campos, **visibles en el tablero** | `_CAMPOS`, `MedidorDeFlota`, `MedidorSQL`, `flotaBloqueSalud()` |
| Tests de adaptador y frontera, contra la base | `tests/flota/test_gastos.py` — 65 |
| Render ejecutado en Node, no leído | `tests/flota/test_render_gastos_js.py` — 29 |

### La decisión que no estaba en el plan: de dónde sale la lectura

Regla 3: `lectura_id` es NOT NULL. La política de anclaje es
**`hallazgos.anclar_odometro`, reutilizada, no copiada** (regla 0) — si el
kilometraje coincide con la última lectura se reutiliza esa fila, en vez de
fabricar desde adentro del sistema el ruido que `lecturas_ts_duplicado` existe
para contar.

Lo que apareció al construirlo: **el vocabulario de `origen` no tiene un valor
que signifique «pagué el SOAT en una oficina»**. Escribir `ot` ahí sería una
lectura que dice de dónde vino y miente, y la columna existe justamente para no
adivinarlo. Ensancharlo es una migración, y esta fase no toca el esquema.

Así que las categorías se parten en dos **por lo que de verdad pasa**:

| | Categorías | Kilometraje |
|---|---|---|
| **De campo** | `combustible`→`tanqueo`; `mantenimiento`, `repuesto`, `llanta`→`ot` | Se pide, y puede nacer una lectura |
| **De escritorio** | las otras nueve | No se pide: el gasto se cuelga de la última lectura conocida |

Un gasto de escritorio sobre un vehículo **sin ninguna lectura levanta**. No es
burocracia: ese gasto no podría entrar a ningún CPK, y guardarlo sería guardar
una fila que nadie va a poder dividir.

### Quién puede qué

| | Rol | Motivo |
|---|---|---|
| Registrar un **tanqueo** | `LECTURA_FLOTA` (con conductor) | El que tanquea es el que maneja. Si solo lo registra un jefe, se registra el lunes y la factura ya se perdió — que es lo que pasa hoy |
| Registrar **cualquier otro gasto** | `MAESTROS_FLOTA` | El SOAT y una entrada a taller no los paga el conductor: son maestros del vehículo |
| **Ver los gastos y el CPK** | `MAESTROS_FLOTA` | *«¿Cuánto cuesta este camión?»* no es una pregunta del turno. Un CPK en la pantalla del conductor está a un paso de leerse como una medida suya, y el número no mide a nadie (regla 2) |

### El detector que SÍ se construyó, y por qué es el único

`galones > capacidad_tanque`. **No necesita umbral, ni canon, ni un mes de
historia**: un tanque de 15 galones que recibe 22 no es un error de medición.

Y **no acusa a nadie**. Lo que afirma es que *dos datos no pueden ser los dos
ciertos*: la capacidad de la ficha o los galones registrados. Una capacidad mal
levantada, un tanque auxiliar que la ficha no conoce, dos vehículos en la misma
factura y un dedo en el teclado producen el mismo renglón, y las cuatro se
investigan igual de rápido — ninguna se investiga mejor si el sistema ya dictó
sentencia. Está probado: hay un test que recorre las respuestas HTTP y las
cadenas de los tres módulos por AST exigiendo que ninguna palabra impute un
delito.

**El campo que impide que el detector se apague solo:**
`tanqueos_sin_capacidad_declarada`, contador **aparte** del de excesos. Sin
capacidad en la ficha, `excede_capacidad` devuelve `SIN_DATO` y jamás `False` —
`False` significaría «se revisó y está bien»—. Sin ese segundo contador, un
parque entero sin capacidad levantada se ve idéntico a un parque sin un solo
exceso. Es la forma exacta de `REC-01` y las otras cinco de la auditoría del
2026-08-15.

### La medida nace con la tabla

`tanqueos_sobre_capacidad`, `tanqueos_sin_capacidad_declarada`,
`gastos_sin_documento` y `cpk_mes` salen en `/flota/health` **y** en el bloque de
salud del tablero, desde el primer día. `cpk_mes` es una **lista por vehículo y
no un promedio de flota**: el canon dice que el CPK no compara vehículos, y
promediar un NHR con un motocarro mide la composición del parque.

`gastos_sin_documento` no es un defecto del sistema: es **la medida de si la
operación está entregando las facturas**. Por eso se cuenta en vez de bloquearse
— un formulario que exige la factura produce cero gastos registrados, no más
facturas.

### Supuestos declarados (el dueño no contestó)

Los dos están escritos en el docstring de `flota/adaptadores/gastos.py`, no solo
acá:

1. **¿Tanquean con tarjeta/convenio o con efectivo del conductor?**
   `origen_costo` es obligatorio, se escribe con palabras y `sin_dato` **no es el
   default**. Si la respuesta es efectivo, cada registro es además una
   legalización de gasto y **quién aprueba cambia**; la columna ya distingue los
   casos sin migrar. Hoy **nadie aprueba dentro del WMS** y no hay verbo de
   aprobación: la aprobación real es la causación en Siesa, y un segundo
   «aprobado» acá sería un estado que contradice al ERP sin poder corregirlo.
2. **¿Contabilidad causa con placa o con centro de costo por vehículo?**
   `documento_numero` y `centro_op` son nullables y los dos se cuentan. Si causan
   con placa, `documento_numero` deja de ser un campo que alguien teclea y pasa a
   ser una clave de cruce que se podría **leer de Siesa** en vez de digitarse. La
   espina aguanta las dos respuestas; lo que cambia es quién llena el campo.

### Lo que NO se hizo, y por qué

| | Motivo |
|---|---|
| **Detector de caída de rendimiento** contra la historia del vehículo | Necesita ventanas acumuladas: mes 2, no día 1. Deuda declarada abajo con su condición de disparo |
| Foto de la factura del tanqueo | La columna de fotos existe y acepta `foto_dato`. La captura no se conectó: **regla 12** — primero que alguien registre un tanqueo de verdad |
| Editar o borrar un gasto | Hoy no hay una sola fila que corregir. La primera corrección real que alguien pida define el gesto; inventarlo antes es diseñar contra un caso imaginado |
| Precio del galón por estación, agrupado | `costos.precio_por_galon` ya lo calcula por tanqueo y la fila lo muestra. El agrupado por estación pide una serie que todavía no existe |
| Un promedio de CPK de la flota | **No es un olvido: el canon lo prohíbe.** Mediría la composición del parque |
| Umbral de «CPK alto» | Regla 13: no hay una sola medición. El número se publica para poder fijarlo |

### Deuda declarada, con condición de disparo

#### 1. El segundo detector — caída de rendimiento contra la propia historia

**Qué sería:** avisar cuando un vehículo rinde muy por debajo de su propia
mediana. *«El TGZ653 rindió 19 km/gal contra una mediana de 27»* — nunca
comparado contra otro vehículo, y **nunca con un nombre de persona al lado**.

**Por qué no hoy:** con dos o tres ventanas lleno-a-lleno, la mediana la mueve
una sola ventana. Un detector que dispara sobre ruido se apaga en una semana, y
apagado es indistinguible de no tenerlo.

> **Condición de disparo, las tres a la vez:**
>
> 1. **≥ 6 ventanas lleno-a-lleno del mismo vehículo** (o sea 7 tanqueos con
>    `tanque='lleno'`). Con seis hay dos mitades comparables; con tres, cualquier
>    ventana corta manda.
> 2. Esas ventanas **cubren ≥ 60 días**, para que un cambio de ruta o de
>    temporada no se lea como una caída mecánica.
> 3. La mediana se calcula **por vehículo**. Contra la flota mediría la
>    diferencia entre un NHR y un motocarro.
>
> El umbral de caída **no se fija ahora** (regla 13): se fija con esa serie, con
> procedencia y en un canon propio, igual que se hizo con `dias_hallazgo_abierto`.

Lo que ya está listo para eso: `ventanas_lleno_a_lleno` y `rendimiento_km_galon`
existen, probados; `flota_tanqueo.tanque` decide qué ventana es medible; y
`tanqueos_de()` entrega la serie ordenada por odómetro.

#### 2. ~~La marca de confianza del CPK es una constante~~ — SALDADA el 2026-09-02

`MARCA_MIENTRAS_NO_HAY_COLUMNA` ya no existe. La columna `confianza` se creó en
la fase 0 y `cpk_de` llama a `confianza_del_tramo` con las dos lecturas extremas
del tramo, como decía la condición de disparo. El contrato de
`costo_por_kilometro` **no cambió**, que era el punto.

Consecuencia medida y no cosmética: como ninguna lectura de gasto trae foto del
tablero, **los dos extremos nacen `dudosa` y el CPK sale `sin_dato` hasta que
alguien pase por la cola de verificación**. Ver la sección de la fase 0.

#### 3. `origen` de lectura no tiene un valor para el gasto de escritorio

Nueve categorías se cuelgan de la última lectura conocida en vez de tener la
suya. Funciona y es honesto, pero significa que un SOAT y un impuesto pagados el
mismo día comparten ancla con el último tanqueo.

> **Condición de disparo:** la próxima migración que toque
> `flota_lectura_odometro` agrega `'gasto'` al vocabulario en la misma migración.
> Es un peaje, no un proyecto: ensanchar un CHECK a solas no justifica un
> despliegue.

#### 4. `CalculoImposible` no la levanta nadie

Está definida y exportada en `flota/dominio/costos.py` y **ningún camino la
levanta**. No es dañina —es vocabulario, no una función muerta que alguien pueda
llamar creyendo que hace algo—, pero es exactamente la forma que este módulo
persigue, y se declara en vez de dejarla pasar.

> **Condición de disparo:** el primer cálculo que necesite decir «la pregunta
> está mal hecha» en vez de devolver `SIN_DATO` la usa. Si al cerrar la fase 2
> sigue sin un solo `raise`, se borra.

### Verificación

· **174 tests nuevos**: 77 de dominio derivados del canon, 68 de adaptador y
  frontera HTTP contra la base real, 29 de render **ejecutando el `flota.js` real
  en Node** — ninguno contra mocks.
· **30 mutaciones aplicadas, 30 muertas.** El arnés
  (`scratchpad/mutaciones_gastos.py`) distingue `MUERTA` de `NO SE JUZGÓ`
  parseando el resumen de pytest, no el returncode: **un test saltado sale con
  exit 0 igual que uno que pasó**, que es el defecto que el arnés del hallazgo
  produjo el 2026-09-01 al recortarle el `PATH` al subproceso. Acá se le pasa el
  entorno completo y se exige la base en verde antes de mutar nada.
· Cada guard, en **las dos direcciones**: que dispare al violarlo y que **no**
  dispare sobre operación sana. Incluye los tres detectores de la fase, los tres
  permisos y los dos ceros del canon.
· `node --check app/static/pwa/flota.js` limpio.
· Suite completa: **3409 passed, 1 skipped, 33 deselected, 0 failed** contra
  SQLite en memoria, el 2026-09-02. Venía de **3110 passed y 1 failed** — el rojo
  era el trinquete de cobertura sobre `costos.py` sin tests, y era el punto de
  partida de esta fase. Regla 13: ese motor, esa fecha, no producción.
· Las tres rutas montadas y verificadas contra el `url_map` real:
  `/flota/gastos`, `/flota/gastos/<placa>`, `/flota/tanqueos`.
· **Sin correr contra PostgreSQL**: esta fase no toca el esquema. Las tablas y
  su migración ya se verificaron ida y vuelta contra PostgreSQL real antes de
  esto (`m018flotainspecciongasto`).

#### El 30/30 es de la segunda pasada. La primera dio 26/30, y las cuatro valen

Se reportan porque **son el resultado, no un borrador**: cada superviviente era
un test en verde sobre una propiedad que la vía de prueba satisfacía por otra
razón. Las tres primeras se arreglaron con tests nuevos; ninguna aflojando nada.

| Sobrevivió | Por qué el test no podía fallar | Qué se hizo |
|---|---|---|
| `hubo_gastos=True` fijo en `cpk_de` | El único vehículo sin gastos que los tests miraban **tampoco tenía kilómetros**: la guarda del odómetro contestaba primero y tapaba la del gasto | Un vehículo que rodó 300 km y no tiene un peso registrado. Tiene que dar `sin_dato`, no `$0` |
| El gasto del tanqueo con `commit=True` propio | El test de la transacción fallaba en una validación **anterior a escribir nada**: no había nada que deshacer | La extremidad revienta **después** de escrito el gasto. Se exige que no quede ni el gasto ni su lectura |
| Los kilómetros del CPK sin filtrar por ventana | *(reemplazó a una mutación equivalente: `len(lecturas) >= 2` → `>= 1` no cambia ningún resultado, y se dejó escrita en el arnés como equivalente en vez de borrarla)* | Lecturas de abril sobre el CPK de marzo: numerador de un mes y denominador de otro |
| La URL del tanqueo armada por concatenación | **El trinquete de rutas huérfanas se satisfizo con un comentario mío.** La URL estaba escrita dos veces en `flota.js`: en la constante y dentro de un docstring que explicaba por qué usarla | Se reescribió el comentario sin la URL. **Octava vez en este repo que un detector de texto se atrapa en su propio comentario** |

La última es la que más enseña, y no es sobre esta fase: el guard de huérfanas
mide texto, así que **cualquier comentario que cite una URL vuelve inmune a esa
ruta**. Vale para las 24 rutas de flota, no solo para ésta.

### Lo que sigue sin ejercerse

**Nadie ha registrado un gasto real.** Tres endpoints, dos tablas, cuatro medidas
y una pantalla, y cero uso. Lo que falta para la compuerta de esta fase:

1. **Un tanqueo real cargado por quien tanquea**, con su factura en la mano, para
   saber si el formulario se llena en el patio o solo en el escritorio.
2. **La capacidad de tanque de los seis vehículos**, levantada con procedencia.
   Sin ella el único detector de la fase no mira nada — y el health lo dice,
   que es distinto de que nadie se entere.
3. **La respuesta del dueño sobre tarjeta contra efectivo**, que es la que decide
   si esto necesita un verbo de aprobación.

Hasta que eso pase, esto es superficie construida — que es exactamente lo que la
regla 12 existe para contar.

---

## La inspección diaria — donde el conductor contesta (2026-09-02)

### Lo que había, y lo que faltaba

| Pieza | Estado al empezar |
|---|---|
| `flota/dominio/inspeccion.py` | Política completa desde la tanda 1: orden, periodicidad, veredicto, plazos |
| 53 ítems de catálogo | Sembrados en producción desde el 2026-08-02 |
| `flota_inspeccion` + `flota_respuesta_item` | Modelos y migración `m018` verificados contra PostgreSQL, ida y vuelta |
| `flota/adaptadores/inspecciones.py` | **370 líneas, cero tests, cero endpoints, cero pantalla** |

Sexta aparición del patrón «función sin caller» en este módulo, y la más cara de
todas: la política ordenaba una lista que nadie veía, sobre 53 ítems que nadie
podía contestar. **El conductor no tenía dónde responder.**

Esta tanda no escribió política nueva. Escribió los tests, los dos endpoints y
la pantalla — el gesto que enciende lo que ya estaba.

### El hallazgo nace automático, y no lo confirma el conductor

Es la decisión de diseño de la tanda, y va contra la lectura ingenua de la regla
2. Un ítem marcado `no_apto` produce su hallazgo **en la misma transacción**, sin
un segundo gesto:

1. **Regla 11 es la que decide.** Con confirmación, el camino barato no es
   «marco `no_apto` y no confirmo»: es **no marcar `no_apto`**. Un paso extra le
   pone precio a la honestidad, y lo que se paga con ese precio es que el freno
   quede en `optimo`.
2. **Regla 2 no lo prohíbe, porque el hallazgo no imputa a nadie.**
   `reportado_por_usuario_id` es quien lo vio y `custodia_id` es bajo la
   custodia de quién apareció — dos hechos. No hay ningún cálculo señalando a
   una persona: hay un humano que marcó una falla con el dedo, y el automatismo
   le ahorra volver a escribirlo.

`sin_dato` **nunca** produce hallazgo. «No sé» no es «está mal»: bloquea el
despacho vía `incompleta` y no le abre a nadie una tarea con fecha límite sobre
un daño que nadie constató. El CHECK `ck_flota_resp_hallazgo_solo_si_no_apto` lo
respalda en disco, y se ejerce con inserciones crudas — un invariante que solo
vive en el adaptador es una sugerencia.

### Una sola vía de nacimiento

El hallazgo pasa por `hallazgos.reportar(commit=False)`, la misma función que usa
el reporte a mano. No hay un segundo `INSERT INTO flota_hallazgo` en el módulo, y
hay un trinquete **por AST** que lo impide: la segunda vía es siempre la que se
olvida de calcular la fecha límite (regla 6).

Lo mismo con el odómetro: `hallazgos.anclar_odometro(origen=PREOPERACIONAL)`. Una
inspección y los tres daños que encontró cuelgan de **una** lectura, porque eso
es lo que pasó — alguien miró el tablero una vez.

### Quién puede qué

| | Rol | Motivo |
|---|---|---|
| Ver los ítems del día | `LECTURA_FLOTA` (incluye conductor) | Si necesitara un jefe para abrir la lista, la inspección se hace a las nueve o no se hace |
| Registrar la inspección | `LECTURA_FLOTA` (incluye conductor) | Es **su** turno y **su** respaldo. Que la registre un admin por él la convierte en un registro *sobre* el conductor hecho por otro |
| Cerrar el daño que produjo | `MAESTROS_FLOTA` (sin conductor) | Regla 11: si quien inspecciona también cierra, el camino barato es marcar `no_apto` y cerrarlo en el mismo minuto |

La asimetría **no se reescribió**: vive en los endpoints de hallazgo desde el
2026-09-01, y hay un test de punta a punta que comprueba que el puente nuevo no
la rodea.

### La pantalla, y el default que no está

Objetivo dos minutos. **Ningún control nace marcado** — ni un `checked`, ni un
`<select>` con la primera opción puesta. Lo que no se toca **no se manda**, y el
servidor lo escribe como `sin_dato`.

Ese es el único punto donde la regla 1 se puede romper sin que ningún CHECK lo
vea: si la pantalla mandara `optimo` por omisión, el servidor recibiría una
respuesta válida y escribiría `apto` sobre veintiocho ítems que nadie miró. Por
eso el test **ejecuta `flotaCondGuardarInspeccion` en Node** y mira el payload
real, en vez de reimplementar el armado — la primera versión lo reimplementaba y
la mutación sobrevivió.

Dos botones por ítem y no tres: no hay «no sé». No responder ya es `sin_dato`, y
un tercer botón sería un camino de un toque para declarar «no sé» sobre la lista
entera.

### La medida nace con la pantalla

| Campo | Qué contesta |
|---|---|
| `vehiculos_sin_inspeccion_hoy` | Los camiones que **nadie miró**. No aparecen en ningún otro lado del tablero: no tienen daño, no tienen aviso, no tienen fila |
| `inspecciones_incompletas_hoy` | Los que se miraron **a medias**. Aparte, porque se corrige distinto: uno hablando con el conductor, el otro mirando por qué la pantalla se abandona |
| `segundos_llenado_30d` | El mínimo del mes **con cuántos ítems tenía al lado**, más la mediana. Hecho, no umbral |

**Ningún umbral de tiempo** (regla 13): no hay una sola medición todavía. Veinte
segundos con todo óptimo se registra igual y queda a la vista — imponer antes de
medir deja camiones en patio el primer día y la operación desmonta el sistema en
48 horas. Mismo trato que `salto_km_maximo_30d`.

Los tres se ven en `flotaBloqueSalud()`. Un campo que nadie mira es el defecto
que este módulo lleva toda la semana arreglando.

### Verificación

· 63 tests de adaptador y veredicto contra la base real, 31 de frontera HTTP, 23
  de pantalla ejecutada en Node — ninguno contra mocks.
· Los CHECK ejercidos **con inserciones crudas**, no solo por el adaptador: 10
  sobre `ck_flota_insp_veredicto_coherente` y 7 sobre
  `ck_flota_resp_hallazgo_solo_si_no_apto`, cada uno con su dirección sana.
· **29 mutaciones aplicadas, 29 muertas.** El arnés distingue «muerta» de «no se
  juzgó» parseando el resumen de pytest, porque un test saltado sale con exit 0
  igual que uno que pasó — y le pasa el `PATH` real al subproceso, sin el cual
  `node` no existe y los 23 tests de pantalla se saltan solos.
· Suite completa: **3406 passed, 1 skipped, 33 deselected**, contra SQLite en
  memoria, el 2026-09-02 (regla 13: ese motor, esa fecha, no producción).
· `node --check app/static/pwa/flota.js` limpio.

### Lo que NO se hizo, y por qué

| | Motivo |
|---|---|
| Bloquear el despacho de un vehículo `incompleta` o `no_apto` | `habilita_despacho` se publica y **no bloquea**. Medir → corregir → imponer, en ese orden |
| `corregir` y `reabrir` una inspección | Editarla es cambiar el testimonio sin dejar rastro. Si el conductor se equivocó, inspecciona de nuevo y quedan las dos — que además es lo que hace visible el patrón de reinspeccionar hasta que dé apto |
| Fotos del ítem `no_apto` | El hallazgo ya las acepta por su endpoint. Conectarlas acá antes de que alguien use el formulario es la regla 12 al revés |
| Un umbral de `segundos_llenado` | Regla 13. El campo existe para poder fijarlo con dato dentro de un mes |
| La plantilla de `motocarro` | Es tanda 3. El adaptador distingue «no sé qué preguntarle» de «sé qué preguntarle y el catálogo no está sembrado», y el segundo mensaje nombra el script |

### Lo que sigue sin ejercerse

**Nadie ha contestado una inspección real.** Dos endpoints, una pantalla, tres
medidas, y cero uso. La compuerta de esta tanda es un conductor contestando los
28 ítems en el patio a las 5 a.m., y de ahí salen las tres cosas que hoy no se
pueden saber:

1. **Si son dos minutos.** El objetivo está escrito y no medido; el primer
   `segundos_llenado` real es el que lo dice.
2. **Si el orden barajado se lee o se aprende igual.** La defensa de la regla 11
   es una hipótesis hasta que haya tres semanas de datos.
3. **Si marcar `no_apto` se siente barato.** Lo que se está probando es que
   reportar una falla no cueste más que ignorarla — y eso solo lo contesta
   alguien que tuvo la falla enfrente.

Hasta que eso pase, esto es superficie construida, que es exactamente lo que la
regla 12 existe para contar.

---

## El conductor como geocodificador (2026-09-02)

`app/models/geo_entrega.py` · `app/services/geo_cliente.py` ·
`GET /api/rutas/geo/cobertura`

### El diagnóstico, verificado antes de construir

**El sistema no sabe dónde queda ningún cliente, y no es que las direcciones
estén sucias: no hay direcciones.** `pedidos_sync_service.py:124` lee
`f015_id_depto_pe` y `f015_id_ciudad_pe` —códigos de departamento y ciudad— y
persiste el nombre del municipio. Verificado por grep el 2026-09-02: `direccion`
no aparece en ningún modelo del WMS salvo `almacenes.direccion`, que es nuestra.
Una «parada» es un municipio; una ruta maestra, una secuencia de ciudades.

Y geocodificar tampoco es la salida. Números de casa mapeados, medido contra
Overpass: **Neiva 72 · Pitalito 10 · Florencia ~31**, en todo el municipio. Con
esos números, el conductor que ya estuvo parado en la puerta es estrictamente
mejor que cualquier API.

### Dónde vive la coordenada, y por qué son dos tablas

| Tabla | Qué afirma | En el acta de corte |
|---|---|---|
| `entregas_geo` | **dónde estuvo el camión** el día que entregó esa factura | **se vacía** — es registro del ensayo, como las lecturas de odómetro |
| `clientes_geo` | **dónde queda la tienda** | **se conserva** — es el activo que tarda tres meses en construirse |

Las dos cosas son ciertas y distintas. El plan quiere la segunda para rutear,
pero la segunda **se construye a partir de las primeras**: con una sola tabla la
segunda captura tendría que pisar a la primera y no habría contra qué comparar
—ni mediana, ni dispersión, ni forma de notar que dos tiendas comparten razón
social—. Y una sola tabla obliga a elegir en el corte entre borrar el ensayo y
conservar el activo; cualquiera de las dos elecciones está mal.

Clasificadas en `scripts/reset_transaccional.py`: `entregas_geo` en `OPERATIVAS`
**antes de `recaudos_entrega`** (FK hija, mismo tropiezo que ya costó tres
veces) y `clientes_geo` en `PROTEGIDAS_MAESTRAS`.

### Cómo elige el maestro entre varias capturas

`geo_cliente.elegir_coordenada_del_maestro` — **una función, escrita una vez y
fuera de toda consulta**. En orden:

1. Una corrección a mano gana, la más reciente.
2. Entre las de GPS votan solo las de precisión **conocida y ≤ 100 m**.
3. Sin votantes no hay maestro, y el motivo distingue `sin_capturas` de
   `precision_insuficiente` — se arreglan distinto.
4. **Mediana por coordenada** (no geométrica, y lo dice).
5. Si la mitad o más queda a más de **500 m** de esa mediana: `capturas_dispersas`
   y **no se elige punto**. Mitad y mitad no es ruido de GPS: es evidencia de dos
   lugares bajo una clave. Un punto en el medio mandaría al conductor a ninguna
   de las dos direcciones — peor que no tener maestro (Regla 0).

Mediana y no «la más reciente» porque la última confirmación puede venir de una
corrección hecha en la oficina; ni «la más precisa» porque premia lo que el
teléfono dice de sí mismo, no lo que midió.

### La ausencia (Regla 4) y la procedencia (Regla 13)

`fuente ∈ {gps_conductor, corregida_a_mano, sin_dato}` + `motivo_sin_dato`, y
`lat/lon` en NULL cuando no se sabe — **nunca 0,0**, que es un punto real en el
Golfo de Guinea y el default de todo `float` sin inicializar. Cuatro CHECK lo
hacen estructural, incluido el rango de Colombia, que atrapa además los **ejes
cambiados** (`lat=-75.28` pasa cualquier validación global y pone la tienda en
el Pacífico Sur).

**La entrega no se traba por la geografía.** La captura ocurre después del
`commit` de `confirmar_parada` y en su propia transacción: un CHECK que rechace
una coordenada rara pierde una captura, no la entrega. Una parada trabada en la
calle no la desbloquea nadie.

### La medida

`GET /api/rutas/geo/cobertura`, y se pinta sola al abrir **Rutas → Maestras**
(no detrás de un botón: un número que hay que ir a buscar es un número que
nadie mira). Vive en rutas y no en `/flota/health` porque mide una propiedad
del **maestro de clientes**, no del vehículo: flota contesta «¿el camión puede
salir?»; esto, «¿sabemos a dónde mandarlo?».

Denominador = **clientes visitados**, no clientes existentes: uno al que nunca
se le entregó no tuvo oportunidad de tener coordenada, y meterlo haría que la
cifra bajara al crecer el negocio. Regla 11 — la única forma de subirla es
tocar el botón en la puerta del cliente, que es exactamente el trabajo.

Base al cerrar: **0 clientes visitados, 0 con coordenada**, SQLite en memoria,
2026-09-02 (Regla 13). El primer número real sale de la primera ruta.

### Deuda declarada, con condición de disparo

| Deuda | Condición de disparo |
|---|---|
| **Ruteo / optimización / predicción de tiempos** | **≥ 150 clientes en `clientes_geo` con coordenada** (`geo_cliente.UMBRAL_PARA_RUTEAR`, publicado en el propio endpoint). Una ruta urbana tiene 15-25 paradas; optimizar el orden conociendo la mitad de los puntos produce una secuencia que el conductor descarta al segundo desvío. *«No hay nada que entrenar hasta tener las dos cosas. El primer paso honesto es capturar la coordenada y esperar.»* |
| **`tel:` al cliente** | Que el teléfono del cliente esté en la base. **Hoy no está** — verificado por grep: ningún modelo del WMS persiste teléfono de cliente. El camino existe y está nombrado: `connekta.get_terceros_contacto()` (`papeleriamedellin_API_custom_TercerosContacto`) devuelve `f015_celular` / `f015_telefono` por NIT o razón social. Disparo: un sync que los persista. **No se inventó un botón sin dato detrás.** |
| **Botón de navegación por dirección de texto** | Que exista una dirección. Hoy, sin coordenada **no se pinta ningún botón**: uno alimentado con «Neiva» abre el centro de Neiva, y un conductor que abre eso una vez no vuelve a tocarlo nunca. La pantalla dice «no sabemos dónde queda» y cómo se arregla. |
| **Puerta para `corregida_a_mano`** | Que aparezca el primer maestro `capturas_dispersas` o un cliente que se mudó. El valor está en el catálogo y en la elección —la precedencia se escribe una vez o se escribe mal— pero **ningún endpoint lo escribe todavía**. |
| **NIT como clave del cliente** | Que el WMS lo persista. Hoy la clave es razón social + municipio normalizados; el NIT solo existe en vuelo (`liquidacion_service.py:254`). El municipio entra en la clave a propósito: fragmentar es el lado conservador, colisionar no. |
| **Migración de las dos tablas** | La escribe el CTO. El DDL exacto está listo; se dejó fuera para no romper el head único con otra migración en paralelo. Mientras tanto, `tests/test_deriva_esquema.py::_SIN_MIGRACION_ACEPTADO` las declara — **esa lista tiene que volver a quedar vacía**. |

### Lo que NO se construyó, y no por falta de tiempo

**Nada de Nominatim ni de OSRM.** Verificado: Nominatim **prohíbe
explícitamente** este caso de uso (*«package/vehicle tracking applications must
run their own service»*) y el servidor demo de OSRM es solo no comercial.
`tests/test_geo_cliente.py::test_no_hay_ninguna_llamada_a_nominatim_ni_a_osrm`
se pone rojo el día que alguien los agregue «para probar».

**Leer la dirección del tercero desde Siesa** queda fuera: exige un export que
no llegó, y consultar producción está prohibido. No se inventó qué campo trae.

### Lo que sigue sin ejercerse

**Ningún conductor ha tocado «Estoy aquí».** Cero capturas reales, cero
maestros. Lo que solo contesta una tarde de uso real:

1. **Si el botón se toca.** Es opcional y no bloquea nada — a propósito. Si la
   cobertura sigue en cero a la semana, el problema no es el GPS.
2. **Qué precisión reportan los teléfonos de verdad** en el centro de Neiva. El
   umbral de 100 m se eligió por orden de magnitud, no por medición; si descarta
   la mitad de las capturas hay que mirarlo con dato en la mano, no bajarlo.
3. **Si `capturas_dispersas` dispara sobre operación sana.** El radio de 500 m
   es una hipótesis hasta que haya un cliente con seis visitas.

---

## El kilómetro confiable — la columna `confianza` (fase 0, 2026-09-02)

Del plan (`WMS_plan_flota.md` §«Fase 0»): *«sin esto, todo lo demás hereda los
16 millones de kilómetros»*.

### Lo que había

`flota/dominio/odometro.py` tenía `confianza_al_nacer` y `confianza_del_tramo`
—la política completa, con su umbral declarado y justificado— y **ni tests, ni
un solo caller, ni columna donde guardar el resultado**. Una política que decide
algo que nadie persiste, que es el mismo patrón que este módulo lleva pagando
toda la semana: `dominio/hallazgo.py` sin tabla, `dominio/costos.py` sin
adaptador, `dominio/inspeccion.py` sin formulario.

El vacío que tapa es concreto y está medido: `validar_lectura` contesta «¿entra
o no entra?» y por eso **los 16.697.948 km del THP696 entraron** — crecer es lo
único que la monotonía exige. Una vez adentro, ese número es indistinguible de
uno bueno para cualquier cálculo aguas abajo.

### La columna, y por qué no tiene default

`confianza ∈ {declarada, dudosa, verificada}`, NOT NULL, **sin `server_default`**
(regla 4 del módulo, regla 5 del WMS). Un default haría que un `INSERT` que no
diga nada quede `declarada` en silencio; sin él, ese INSERT falla ruidosamente.
Van con ella `motivo_dudosa`, `verificada_por_usuario_id` y `verificada_ts`.

Cuatro CHECK, y dos son la dirección que se olvida: que una `declarada` no pueda
traer motivo de duda, y que **una fila no verificada no pueda llevar el nombre
de un verificador** — sin eso el health contaría trabajo que no afirma nada.

### Se escribe en UN lugar: un `before_insert`, no cinco llamadas

Los escritores de lecturas son cinco (traspaso de turno, lectura suelta,
hallazgo, inspección diaria y gasto de campo) y entran por tres construcciones
—`traspaso.py`, `hallazgos.anclar_odometro`, `api/custodia.py`—, enumeradas por
AST y no de memoria. Una llamada por escritor serían cinco copias de la misma
regla, y la sexta es la que se olvida: **la copia que falta no falla, deja
pasar**, y la lectura mala entra sin marca.

Por eso la marca la escribe el `before_insert` del modelo, que es exactamente el
llamador para el que `confianza_al_nacer` está escrita (*«quien llama a esta
función está dentro de un `before_insert` y tiene columnas, no objetos de
dominio»*). Un trinquete por AST exige que **ningún escritor fije la marca a
mano**: `LecturaOdometro(..., confianza=...)` en un adaptador sería la segunda
implementación de la regla, la que no mira la foto ni el salto.

Lo que el gancho no puede ver es un `INSERT` crudo. Contra eso está la columna
NOT NULL sin default, y el trigger `flota_odometro_nace_no_verificada`.

### `verificada` no la escribe ningún automatismo — y el append-only

Una lectura no puede nacer verificada: lo dice el gancho con un error legible y
lo impone la base con un trigger, para que no exista camino que lo esquive.

El problema estructural fue el otro: `flota_lectura_odometro` es append-only por
trigger, así que **la verificación humana no tenía dónde escribirse**. Se
resolvió reemplazando el bloqueo total de `UPDATE` por uno de forma exacta —toda
otra columna idéntica, y la confianza solo hacia `verificada`—, que es *más*
estricto que el anterior en algo que el anterior no decía: de `verificada` no se
sale. El número sigue sin poder editarse; lo que se escribe no es el número.

La alternativa era una tabla aparte. Se descartó: dejaría la confianza real
repartida entre dos tablas, y `confianza_del_tramo` recibe UN campo — el día que
alguien consultara la lectura sin el JOIN publicaría un CPK sobre una
verificación que no vio.

### La cola, y por qué no es la regla 12 rota

`GET /flota/odometro/dudosas` + pantalla. **Nace con contenido y está medido**:
`medicion.lecturas_sin_foto` cuenta las lecturas con `foto_id IS NULL` y en
producción eran **26 de 26** (2026-09-01). La regla 1 de `confianza_al_nacer`
marca `dudosa` toda lectura sin foto, así que la cola tiene esas 26 desde el
primer día. No es una pantalla esperando un caso hipotético: es la pantalla del
estado actual de la flota.

Dos salidas, y **corregir no vive en la cola**: reusa `POST /flota/odometro` con
`origen=correccion`, la única puerta que exige motivo escrito. Lo que sí vive
acá es la consecuencia — una lectura que una corrección posterior dejó atrás
**sale de la cola**, con la misma ventana que usa `validar_lectura`
(`vigentes_tras_la_ultima_correccion`, extraída al dominio porque ahora tiene
dos consumidores). Sin eso, cada corrección dejaría un ítem eterno y la pantalla
se abandonaría en una semana.

La fila dice **si la lectura no tiene foto**, en amarillo: confirmar sin foto es
la palabra de quien confirma, no la de una evidencia. Esconderlo convertiría 26
lecturas sin respaldo en 26 firmas sobre nada.

### Quién verifica: `MAESTROS_FLOTA`, no `LECTURA_FLOTA`

Verificar un kilometraje no es operar un turno. El corte de `_permisos.py` ya
estaba escrito y esto cae del lado de la ficha técnica: `LECTURA_FLOTA` es
recibir, entregar y registrar odómetro —lo que el conductor hace hoy con su
vehículo—; `MAESTROS` es lo que queda afirmado sobre el vehículo después.

Y hay una razón más concreta que la analogía: **el conductor es casi siempre el
autor de la lectura que habría que verificar**. Con `LECTURA_FLOTA`, quien tecleó
el número certificaría el número, y `verificada` —la única marca que afirma que
alguien miró— sería una casilla que se marca sola (regla 11). `control_flota` sí
entra: el levantamiento de campo es su trabajo (FLO-PR-01).

### El CPK ya usa la marca — y el efecto no es cosmético

`MARCA_MIENTRAS_NO_HAY_COLUMNA` desapareció. `cpk_de` llama a
`confianza_del_tramo` con las dos lecturas extremas del tramo (las que producen
la resta), y con menos de dos lecturas devuelve `SIN_DATO`: no hay tramo que
juzgar, y publicar `declarada` ahí afirmaría que se pudo mirar una resta que no
existió.

> **Lo que esto cambia en producción, dicho antes de que sorprenda:** ninguna
> lectura de gasto trae foto del tablero —el formulario de gasto no la pide—, así
> que los dos extremos nacen `dudosa` y **el CPK de esos vehículos sale
> `sin_dato` hasta que alguien pase por la cola**. No es una regresión: es la
> fase 0 haciendo lo que se le pidió —*«nunca un promedio que rellena»*— y es la
> presión que hace que la cola se atienda. Un extremo verificado alcanza para
> publicar el número **con la marca `dudosa`** al lado.

### El trigger hermano del ancla — SOLO la mitad segura

`flota_ficha_ancla_no_baja`: un `BEFORE UPDATE` sobre `flota_ficha_tecnica` que
impide bajar `km_inicial` por debajo del máximo ya escrito en la serie de ese
vehículo. Sin él, el piso se mueve bajo el techo y el invariante se rompe sin dar
error.

**La condición exige también que el valor BAJE**, y esa mitad la decidió la
medición: sin ella, la condición sería «el ancla quedó por debajo del máximo» —y
eso ya es cierto para las cuatro fichas incoherentes—, así que cualquier edición
de esas fichas quedaría bloqueada aunque nadie tocara el kilometraje.

### La medida nace con la columna

`lecturas_dudosas_pendientes` y `lecturas_verificadas_30d`, en `/flota/health`,
en el puerto, en `MedidorSQL` **y en el bloque de salud del tablero**. Separados
y no sumados: uno es la deuda y el otro dice si alguien la está pagando —«cero
verificadas sobre cero dudosas» (flota sana) y «cero sobre veinte» (cola que
nadie abre) son los dos estados que hay que distinguir.

El primero **lo cuenta `verificacion.pendientes()`**, no un `WHERE` propio: con
dos consultas, el health diría un número y la pantalla mostraría otra lista, y no
habría forma de saber cuál creer (regla 0). El caso que las separa es la
corrección.

### Lo que se descubrió al construirlo, no al planearlo

| Hallazgo | Quién lo encontró |
|---|---|
| La verificación humana **no tenía dónde escribirse**: la tabla es append-only por trigger | Escribir el adaptador. Obligó a rediseñar el trigger en vez de agregar una tabla |
| `str()` sobre un enum de Python 3.11 devuelve `'Confianza.DUDOSA'` — eso iba a llegar al tablero tal cual | El primer render del CPK. Se pasa `.value` |
| `flota/api/ficha.py` atrapaba solo `IntegrityError`: el `RAISE EXCEPTION` de plpgsql llega como `InternalError`, así que el trigger del ancla daba **409 en la suite y 500 en producción** | Leer el motor, no la suite. La suite corre contra SQLite y no puede ver esta clase |
| El CPK del canon dejó de dar 840: sus lecturas nacen `dudosa` | La suite. Los fixtures ahora pasan por la cola explícitamente, y hay un test que afirma que **sin** verificar da `sin_dato` |
| Un test que ya afirmaba `sin_dato` seguía en verde **por otra razón** (el tramo dudoso, no la falta de gastos) | El mismo cambio. Se le agregó la verificación para que siga probando lo que dice |

### Verificación

· **62 tests nuevos** en `test_confianza_odometro.py` (las cinco vías de
  escritura, los cuatro CHECK, los tres triggers, la cola, los permisos, el CPK
  y el health) y **15 de render** ejecutando el `flota.js` real en Node.
· Cada guard, en **las dos direcciones**: que el trigger dispare y que la
  verificación legítima pase; que el ancla no baje y que la ficha ya incoherente
  se pueda seguir editando; que la cola esconda lo superseded y que no esconda
  nada cuando no hubo correcciones.
· **37 mutaciones aplicadas, 37 muertas.** El arnés
  (`scratchpad/mutaciones_confianza.py`) distingue `MUERTA` de `NO SE JUZGÓ`
  parseando el resumen de pytest y le pasa el entorno completo al subproceso:
  **un test saltado sale con exit 0 igual que uno que pasó**.
· Suite completa: **3577 passed, 1 skipped, 45 deselected, 0 failed** contra
  SQLite en memoria, el 2026-09-02. Venía de 3417. Regla 13: ese motor, esa
  fecha, no producción.

#### El 37/37 es de la segunda pasada. La primera dio 30/37, y los seis valen

Se reportan porque **son el resultado, no un borrador**: cada superviviente era
un test en verde sobre una propiedad que la vía de prueba satisfacía por otra
razón. Ninguno se arregló aflojando nada.

| Sobrevivió | Por qué el test no podía fallar | Qué se hizo |
|---|---|---|
| Sacar `valor_km` de las columnas inmutables | El `UPDATE` de prueba tocaba **solo** el número, y ahí lo frenaba la otra mitad de la condición (la confianza no iba hacia `verificada`) | Cinco tests que **verifican Y de paso mueven** el número, el reloj, el vehículo, el origen o el motivo. La rendija no deja pasar nada más |
| Permitir desverificar (`OLD.confianza <> 'verificada'` borrado) | El test bajaba a `declarada`, y eso lo frena la otra mitad | Re-verificar por SQL crudo **con otro nombre**, dejando la confianza en `verificada` |
| Que el gancho no resuelva el `ts` | Todos los tests pasaban `ts` explícito | Una lectura sin `ts`, con `utcnow` congelado: es el caso de las diez filas del reintento del 2026-08-03 |
| `min()` en vez de `max()` sobre las correcciones | Con **una** corrección los dos criterios coinciden | El caso de dos correcciones, que es el único que los separa |
| Borrar el renglón del motivo en la fila de la cola | El motivo del fixture decía «sin foto del tablero», la misma frase que aporta el aviso de «sin foto» | Un motivo que solo puede venir del campo: el salto de ×301 del THP696 |
| Armar la URL de `/verificar` concatenando | **El trinquete global no puede matarla**: trocea la ruta en `/flota/odometro/` y `/verificar`, y `/verificar` ya existe en el PWA por `reposicion.js` (`/api/reposicion/verificar-stock`) | Un guard propio que exige las dos URL escritas enteras. Es la novena aparición del detector que se satisface con un texto ajeno |
· `node --check app/static/pwa/flota.js` limpio.
· **Sin correr contra PostgreSQL.** Los cuatro CHECK, los dos triggers nuevos y
  el `no_update` reemplazado no se ejercieron en el motor real: la suite de
  invariantes está deseleccionada en CI y esta tanda no tuvo base a mano. La
  rama `InternalError` de `ficha.py` tampoco.

### Lo que NO se hizo, y por qué

| | Motivo |
|---|---|
| **El piso duro del ancla** (`MAX(histórico, km_inicial)` en el trigger de monotonía) | Deuda declarada abajo con su condición de disparo. 4 de 6 fichas contradicen su propia serie |
| La migración | **La encadena el CTO.** Había otro agente escribiendo en `migrations/` el mismo día y dos migraciones simultáneas rompen el head único: con el head roto el `releaseCommand` falla y no despliega nada. DDL exacto listo, con su backfill y su orden |
| Pedir foto del tablero en el gasto y en la lectura suelta | Es lo que sacaría de la cola a la mayoría de las lecturas nuevas, y es de otra tanda. Hoy la cola las recibe y las declara |
| Un umbral de km/día (`km_dia_plausible_max`) | Regla 13. `salto_km_maximo_30d` publica el hecho para poder fijarlo con dato dentro de un mes |
| Verificar en lote («confirmar todas») | Es el gesto que vuelve decorativa la marca: la forma de maximizar `verificadas` sin mirar nada. Se confirma de a una, con la foto al lado |
| Un aviso cuando la cola crece | El campo del health está y se ve en el tablero. Un canal más antes del primer uso real es la regla 12 al revés |

### Deuda declarada, con condición de disparo

#### El piso duro del ancla — el `MAX(histórico, km_inicial)` que NO se impuso

**Qué sería:** que el trigger de monotonía rechazara toda lectura por debajo de
`MAX(máximo histórico, ficha.km_inicial)`, y no solo por debajo del máximo
histórico.

**Por qué no hoy, con el número:** verificado contra producción el 2026-09-01,
**4 de 6 fichas contradicen su propia serie** — la del THP696 dice 433.434 y su
primera lectura es 55.349. Con el piso puesto, esos cuatro vehículos no podrían
registrar una lectura real: quedarían trabados por una segunda vía, justo cuando
la primera (la lectura envenenada) acaba de arreglarse. Y la vía de escape sería
marcar todo como `correccion`, o sea mentir — que es la falla que la ventana de
corrección resolvió el 2026-09-01.

> **Condición de disparo, las tres a la vez:**
>
> 1. **`fichas_con_ancla_incoherente = 0` durante un mes corrido.** La medida ya
>    existe y ya sale en el health y en el tablero. Mientras sea > 0, imponer el
>    piso traba un vehículo por cada ficha incoherente.
> 2. **Que se sepa cuál de los dos números miente en cada caso**, ficha por
>    ficha, con alguien que fue a mirar el tablero. Hoy no se sabe, y elegir en
>    silencio es lo que la regla 0 prohíbe.
> 3. **Que exista la vía para arreglar el ancla sin trabar nada**, que es este
>    mismo trigger hermano ya puesto: subir `km_inicial` se puede, bajarlo por
>    debajo de la serie no.
>
> El día que las tres se cumplan, la mitad que falta es una línea en
> `_SQLITE_DDL`/`_PG_DDL` —el `COALESCE` del `SELECT MAX` contra
> `flota_ficha_tecnica.km_inicial`— y su par de tests en las dos direcciones.

### Lo que sigue sin ejercerse

**Nadie ha verificado un kilometraje real.** Dos endpoints, una pantalla, dos
medidas, tres triggers, y cero uso. La compuerta de esta tanda es alguien de
control de flota abriendo la cola con las 26 lecturas de producción adentro y
confirmando o corrigiendo la primera. Hasta que eso pase, tres cosas no se
pueden saber:

1. **Si 26 filas sin foto se pueden verificar de verdad**, o si sin foto lo
   honesto es dejarlas dudosas para siempre y arreglar el problema hacia
   adelante (pidiendo la foto en el gasto y en la lectura suelta).
2. **Cuánto tarda la cola en drenarse**, que es lo que dice si el CPK va a
   existir este mes o no.
3. **Si el ×10 marca operación sana.** El umbral está justificado y no medido:
   el primer mes de lecturas reales es el que lo confirma o lo corrige.

Y una cuarta que no depende de nadie: **la migración no está encadenada**, así
que en producción esta columna todavía no existe. Hasta que el CTO la encadene,
todo esto corre solo contra SQLite.

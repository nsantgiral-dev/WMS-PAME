# FLOTA — Tanda 1: Cimientos y custodia

**Alcance:** identidad del vehículo, ficha técnica, documentos, odómetro, custodia y
almacenamiento de fotos. Una sola pantalla nueva: recibo de turno.

**Compuerta de salida:** las 5 fichas técnicas cargadas y un conductor real haciendo
entrega de turno 5 días seguidos, en producción.

---

## 0. Lo que ya existe y NO se recrea

Verificado contra el código el 31/07/2026:

| Tabla existente | Qué aporta |
|---|---|
| `vehiculos` | `id`, `placa` String(10) unique, `codigo_siesa`, `activo` |
| `conductores` | `id`, `nombre`, `cedula` unique, `telefono`, `activo`, `usuario_id` nullable |
| `usuarios` | cuenta de login, `rol` default `operario` |
| `ruta_despacho` | `conductor_id`, `vehiculo_id` nullable, `numero_guia` |

**No se crea tabla de vehículos ni de conductores.** Todo lo nuevo cuelga por FK.
`placa` es la clave natural de consulta y presentación; `vehiculo_id` es la FK.
`codigo_siesa` **no es la placa**: es un código maestro tipo "DOMICILIOS" de los conectores
142946/142888, y solo aparece cuando se cruza con SIESA.

**Pendiente de verificar antes de escribir `custodia`:** si existe tabla de sedes o centros.
Si no existe, `custodio_sede_id` se modela como FK a la tabla de centros de costo que use
WMS; si tampoco existe, se declara el hueco y se resuelve antes de la migración — no se
inventa un texto libre.

---

## 1. Modelo de datos

### `ficha_tecnica` — 1:1 con vehículo

```
vehiculo_id             FK vehiculos.id, PK
combustible             gasolina | diesel | sin_dato
sistema_frenos          hidraulico | aire_sobre_hidraulico | aire_full | sin_dato
tiene_freno_escape      si | no | sin_dato
distribucion            correa | cadena | sin_dato      -- sincronización del motor
transmision_final       cadena | correa | cardan | sin_dato  -- fuerza a la rueda
distribucion_km_cambio  int nullable
norma_emisiones         text nullable
aceite_motor_spec       text          -- API + viscosidad
aceite_motor_litros     numeric nullable
aceite_caja_spec        text nullable
aceite_diferencial_spec text nullable
refrigerante_spec       text nullable
posiciones_llanta       int           -- 4 en N300, 6 en camiones
medida_llanta           text nullable
tiene_furgon            bool
km_inicial              int           -- ancla del sistema
km_inicial_ts           timestamptz

distribucion_fuente     manual_fabricante | concesionario | placa_motor |
                        taller | estimado | sin_dato
distribucion_verificado_ts   timestamptz nullable
frenos_fuente           (mismos valores)
frenos_verificado_ts    timestamptz nullable
```

`transmision_final` no es `distribucion`. La distribución sincroniza el motor; la
transmisión final lleva la fuerza a la rueda. Un motocarro puede tener las dos por
cadena y son dos mantenimientos distintos: sin este campo no se deriva la lubricación
de cadena, que en un motocarro es rutina y en un camión con cardán no existe.

Los dos atributos que disparan tareas de seguridad —`distribucion` y `sistema_frenos`—
llevan fuente y fecha propias. Los demás no. Es el canon aplicado a un dato de ficha: un
número con autoridad lleva su procedencia.

### `documento_vehiculo`

```
id, vehiculo_id FK
tipo             soat | rtm | poliza_rc | tarjeta_propiedad
estado           vigente | no_encontrado
numero, entidad
fecha_expedicion, fecha_vencimiento   date nullable   -- NULL si no_encontrado
foto_id          FK foto.id nullable   -- clase foto_dato
```

Alerta a 30 y 15 días. En tanda 1 solo reporta en el health; no bloquea nada.

`no_encontrado` **no es un campo vacío: es una afirmación.** Un vehículo sin SOAT
vigente localizable es un hallazgo bloqueante. Registrarlo como ausencia de dato
lo vuelve indistinguible de "todavía no lo hemos mirado", y esas dos cosas exigen
acciones opuestas — buscar el papel, o sacar el camión de ruta. Un tipo que no
aparece en la lista es lo tercero: sin verificar.

### `lectura_odometro`

```
id, vehiculo_id FK
valor_km         int NOT NULL
ts               timestamptz NOT NULL
origen           entrega | preoperacional | cierre_dia | ot | tanqueo | correccion
foto_id          FK foto.id nullable   -- clase foto_dato
autor_usuario_id FK usuarios.id NOT NULL
motivo_correccion text nullable   -- obligatorio si origen = correccion
```

Append-only. Una lectura no se edita: se corrige con un registro nuevo de
`origen = correccion` con motivo y autor.

### `custodia`

```
id, vehiculo_id FK
custodio_tipo             conductor | sede        NOT NULL
custodio_conductor_id     FK conductores.id       nullable
custodio_sede_id          FK (ver sección 0)      nullable
registrado_por_usuario_id FK usuarios.id          NOT NULL
inicio_ts                 timestamptz NOT NULL
fin_ts                    timestamptz nullable    -- NULL = activa
km_inicio                 int NOT NULL
km_fin                    int nullable
fotos_inicio              FK foto[] (8)
fotos_fin                 FK foto[] (8)

CHECK: exactamente uno de custodio_conductor_id / custodio_sede_id
       es no-nulo, y corresponde a custodio_tipo
UNIQUE parcial: (vehiculo_id) WHERE fin_ts IS NULL
```

**Responsabilidad → conductor**, porque lo que hace válida un acta es la cédula, y la
cédula vive en `conductores`. **Autenticación → usuario**, siempre NOT NULL. Si el jefe de
sede registra la entrega porque el conductor no tiene cuenta, queda: custodio = el
conductor con su cédula, `registrado_por` = el jefe. Honesto y auditable, en vez de
excluir gente en silencio.

`custodio_tipo = taller` se agrega en la tanda 3, junto con la tabla `talleres`. En tanda 1,
un vehículo en taller queda bajo custodia de la sede que lo envió.

### `foto`

```
id, clase          evidencia_estado | foto_dato
entidad_tipo       custodia_inicio | custodia_fin | odometro |
                   documento | hallazgo
entidad_id         NOT NULL
storage_ref        text NOT NULL     -- object storage, NUNCA base64
hash_sha256, bytes, ancho, alto, mime
ts_captura, gps_lat, gps_lon
autor_usuario_id   FK
simulado           bool default false
```

**Prohibido base64 en columna Text en este módulo.** El pipeline existente
(`rutas.js:1965` captura, `2151-2160` compresión cliente, `_condDB.enqueue` cola
IndexedDB) se reusa completo; el almacenamiento no. Las fotos viejas de
`recaudo_entrega` se quedan donde están: no se migran.

**Parámetros por clase:**

| Clase | Cliente | Servidor |
|---|---|---|
| `evidencia_estado` | 800×600, calidad 0.65 | recompresión actual |
| `foto_dato` | ≥1600 px lado largo, calidad ≥75 | **sin recompresión** |

Si la compresión de una `foto_dato` falla, el registro queda en `pendiente_evidencia` y
el health lo cuenta. Nunca `pass`.

---

## 2. Máquina de estados — custodia

```
[sin custodia]  → apertura → ACTIVA → cierre → CERRADA
                                 ↘ traspaso ↗
```

`traspaso` es atómico: cierra la anterior con `km_fin` y `fotos_fin`, abre la nueva con
`km_inicio` y `fotos_inicio`, en una transacción. No existe instante sin custodio.

Cuando el conductor marca **"no estaba así"** sobre una novedad heredada, el sistema
**no resuelve nada**: crea un hallazgo de tipo `disputa_custodia` que un humano decide
(regla 2). En tanda 1 el hallazgo se crea y se lista; el flujo completo llega en tanda 2.

**Arranque en frío:** la primera custodia de cada vehículo se marca `linea_base = true`.
Los daños registrados ahí nacen como preexistentes, sin responsable. Nadie paga por lo
que no sabemos cuándo apareció.

---

## 3. Pantalla — recibo de turno

Objetivo: **2 minutos**. Se abre antes de que el conductor reciba el manifiesto de ruta.

1. Placa — preseleccionada por asignación, editable
2. Odómetro — numérico **y** foto del tablero, ambos obligatorios (`foto_dato`)
3. Ocho fotos con guía de encuadre semitransparente, orden fijo:
   frontal · trasera · lateral izq · lateral der · cajón abierto · interior cabina ·
   tablero · llantas
4. Novedades heredadas del turno anterior — por cada una: **reconozco** / **no estaba así**
   + foto
5. Firma con el dedo

Offline-first: cola IndexedDB, UUID generado en cliente, sincronización al volver la señal.

---

## 4. Endpoints

```
POST /flota/custodia/traspaso
GET  /flota/custodia/activa/{placa}
POST /flota/odometro
GET  /flota/vehiculo/{placa}/ficha
PUT  /flota/vehiculo/{placa}/ficha
GET  /flota/vehiculo/{placa}/documentos
POST /flota/vehiculo/{placa}/documentos
GET  /flota/health
```

**Corrección 2026-08-01:** esta sección omitía los dos endpoints de
`documento_vehiculo`. El health ya contaba documentos vencidos y por vencer, la
tabla existía desde la tanda 1, y no había por dónde cargar una fecha — la
primera tarea del control de flota es verificar los cinco SOAT. Una capacidad
contada y no cargable es medio hueco: el tablero pregunta algo que nadie puede
responder.

**Estos endpoints se construyen JUNTO CON su consumidor (§3), en la misma
sesión — nunca antes.** Un endpoint sin forma de llamarse es la regla 12 rota y el
patrón que ya apareció cuatro veces en este repo: la capacidad construida, probada y
desplegada, y el gesto que la enciende nunca escrito. El trinquete de huérfanos de
`tests/flota/test_trinquetes_flota.py` lo impide, y la alternativa —cinco excepciones
declaradas el primer día— convertiría un trinquete en cero en arqueología.

El orden de §3 y §4 en este documento es de lectura, no de ejecución.

`GET /flota/aptitud/{placa}` **no se implementa en tanda 1.** Llega en tanda 2 con el kill
switch `FLOTA_BLOQUEO_DESPACHO=0`.

---

## 5. Health — declara estado, no dice OK

```json
{
  "ambiente": "produccion",
  "datos_reales": true,
  "vehiculos_activos": 5,
  "fichas_completas": 3,
  "atributos_sin_dato": ["TGZ653.distribucion", "TGZ655.distribucion"],
  "vehiculos_sin_custodia_activa": 0,
  "custodias_pendiente_sede": 0,
  "custodias_cerradas_forzadas": 0,
  "custodias_sin_foto_completa": 1,
  "fotos_pendiente_evidencia": 0,
  "conductores_activos_sin_cuenta": 2,
  "documentos_no_encontrados": 1,
  "documentos_vencidos": 5,
  "documentos_por_vencer_30d": 0,
  "rutas_historicas_sin_placa": null
}
```

`rutas_historicas_sin_placa` en `null` significa aún no medido. **No se pone en 0.**
Medido el 2026-08-01 contra la base real: **3 de 15**. Ya no vale `null`.

`documentos_no_encontrados` es un contador aparte de `documentos_vencidos`:
"vencido" es un papel que existe y caducó, "no encontrado" es que nadie pudo
mostrar el papel. Sumarlos esconde el segundo, que es el más grave. Y sin
contador propio desaparecerían de los dos, porque sus fechas son `NULL` y
`fecha_vencimiento < hoy` no matchea `NULL` — un cero silencioso.

`custodias_cerradas_forzadas` cuenta los turnos cerrados sin la firma del
custodio anterior. **No mide una falla del sistema: mide una conducta.** Si sube,
el problema no es que se pueda forzar — es que los conductores no están cerrando
turno, y la corrección es esa. Cada uno deja un turno siguiente sin fotos de
cierre con qué comparar.

`custodias_pendiente_sede` cuenta las custodias cuya sede el WMS no puede
representar: `almacenes` cubre 5 de los 9 centros del mapa de C.O. (medido
2026-08-01). Flota no crea maestros ajenos para tapar el hueco — declara lo que
no puede representar. Una custodia `pendiente_sede` **sí** cubre al vehículo: no
saber *qué* sede no es lo mismo que no tener responsable.

---

## 6. Invariantes de la tanda (tests de propiedad, antes de implementar)

1. **Monotonía** — el odómetro de un vehículo nunca decrece, salvo registro explícito de
   `origen = correccion` con motivo y autor.
2. **Cardinalidad** — un vehículo tiene exactamente 0 o 1 custodia activa. Nunca dos.
3. **Cobertura temporal** — entre la primera custodia y ahora no existe ningún instante sin
   custodio para un vehículo activo.
4. **Arco exclusivo** — toda custodia tiene exactamente un `custodio_*_id` no nulo, y
   corresponde a su `custodio_tipo`.
5. **Paternidad** — toda foto tiene `entidad_tipo` + `entidad_id` que resuelven a una fila
   existente. Ninguna huérfana.
6. **Integridad de clase** — ninguna `foto_dato` sale del servidor con dimensión menor a la
   capturada.
7. **Borde degenerado** — un vehículo sin lecturas de odómetro devuelve `sin_dato`, jamás
   `0`.

Se escriben **antes** de la implementación de cada modelo. Un test escrito después de la
implementación verifica que el código hace lo que el código hace.

---

## 7. Fuera de alcance en tanda 1

Preoperacional. Hallazgos con plazo. Órdenes de trabajo. Talleres. Garantías. Plan
preventivo. `decision_ruta`. Bloqueo de despacho. CPK. GPS. Scoring de conductores.
Combustible. Llantas. Dashboard.

---

## 8. Canon — carpeta creada, contenido en blanco

```
docs/flota/canones/
  dias_hallazgo_abierto.md    (vacío — lo calcula Santiago)
  dias_ruta_caidos.md         (vacío — lo calcula Santiago)
```

Si el valor y su test los produce la misma mano, el test es decoración.

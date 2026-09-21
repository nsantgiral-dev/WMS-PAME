# Ensayo de la migración pendiente — 2026-09-21

Qué se verificó antes de correr `flask db upgrade` contra la base real, y con
qué evidencia. Existe porque el ensayo se hizo una vez y el resultado no se
puede reconstruir de memoria.

## ⚠️ Corrección — a qué base se refieren estos números

**Todo lo medido en este documento es la base de PRODUCCIÓN**, no la de QA. El
`.env` del repo trae `metro.proxy.rlwy.net:29311`, que es el `DATABASE_URL` del
ambiente `production` de Railway; QA vive en `altaria.proxy.rlwy.net:20841`.
`CLAUDE.md` ya lo advertía con ese mismo host y no se verificó antes de medir.

Eso no invalida los hallazgos —son datos reales y el ensayo de la migración se
hizo sobre una copia restaurada, sin escribir en ninguna de las dos— pero hay
que leerlos como producción:

- las 174 solicitudes de traslado, los 27 usuarios, los 4.672 movimientos de
  kardex y el barrido de 146 endpoints por rol son de **producción**;
- el respaldo `wms-20260921-1231.dump` es un respaldo de **producción**;
- los 11 endpoints en 5xx eran el desfase entre los modelos de esta rama y el
  esquema de **producción**, que sigue en `n021`.

**En QA la migración ya corrió**: el push a `origin/qa` dispar el deploy y su
`releaseCommand` aplicó la cadena. Verificado contra
`altaria.proxy.rlwy.net:20841`: revisión `m027mergedoscadenas`, 19 tablas
`flota_*`, `clase_traslado` presente, y **los 11 endpoints en 200**.

## El síntoma que lo motivó

Barrido de los **146 endpoints GET sin parámetro** con las credenciales de
cada rol real de producción. Un admin recibía **11 respuestas 5xx**. Medido
endpoint por endpoint, la causa de diez de ellas es la misma: el modelo
declara columnas y tablas que la base todavía no tiene.

| Endpoint | Falta |
|---|---|
| `/api/traslados/` y sus cuatro hermanos | `solicitudes_traslado.clase_traslado` |
| `/api/recepcion/`, `/api/compras/dock-lock` | `items_recepcion.cantidad_averiada` |
| `/flota/health`, `/flota/odometro/dudosas` | `flota_ficha_tecnica.capacidad_tanque_galones` |
| `/api/rutas/geo/cobertura` | la tabla `entregas_geo` |

La undécima (`/api/siesa/debug-cache-status`) sí era código y quedó arreglada
en `fa3b618`.

**Esto es lo que hacía que el trabajo de flota no se viera en la interfaz.**
No faltaba código: faltaba el esquema.

## El ensayo

1. Respaldo con `pg_dump` 18 — la 17 local falla contra el servidor 18.6:
   `~/respaldos-wms/wms-20260921-1231.dump`, 4,8 MB, **59 tablas con datos**.
2. Restaurado en una base aparte (`wms_ensayo_migracion`, Postgres 18 local):
   **0 errores** de `pg_restore`.
3. `flask db upgrade` sobre esa copia: **0 errores**,
   `n021cantidadpedidatareapicking` → `m027mergedoscadenas`.

### Lo que no se movió

| | antes | después |
|---|---|---|
| traslados | 174 | 174 |
| usuarios | 27 | 27 |
| kardex_movimientos | 4.672 | 4.672 |

Y aparecieron: `clase_traslado`, `cantidad_averiada`, `entregas_geo` y las
**19 tablas `flota_*`** (había 8).

## Lo que se ejercitó sobre la copia migrada

Datos reales, sin consecuencia. No es un test con datos fabricados: es la
operación contra el inventario y los usuarios de producción.

### Los once endpoints que fallaban → **10 en 200**

El restante era el defecto de código, ya arreglado.

### El flujo de averías, de punta a punta — 9 pasos, 9 en verde

    tienda declara (clase=TRASLADO_AVERIAS) → envía → aprueba la suya
    (+evidencia) → picking → packing → despacha → el CD recibe →
    cola de pendientes → supervisor dictamina → veredicto=True

Incluida la excepción por la que el punto aprueba su propia avería, con la
evidencia persistida desde la aprobación hasta el dictamen.

### Flota — **26 de 26 rutas de lectura en 200**

Salud, avisos, vocabulario, turno del conductor, reportes, odómetro dudoso,
custodia fuera de sede, cierres forzados; y por vehículo (dos placas reales):
ficha técnica, documentos, plan preventivo, hallazgos, llantas, órdenes de
taller, gastos, ítems de inspección y custodia activa.

### Flota — los gestos de escritura

| Gesto | |
|---|---|
| inspección preoperacional (28 ítems) | 201 |
| tanqueo del conductor | 201 |
| reportar un daño | 201 |
| gasto de escritorio | 201 |
| traspaso de custodia | **409, y es correcto** |

El 409 dice: *«El BDT261 lo tiene Victor desde 21/09 a las 14:16. Para
recibirlo vos, Victor tiene que entrar con SU usuario y apretar “Entregar
turno”.»* Es la cadena de custodia funcionando.

## Nota sobre los mensajes de error

Tres payloads de esta corrida los armé mal y los tres 409 dijeron exactamente
qué se esperaba y por qué — `respuestas tiene que ser una lista de {item_id,
respuesta}; llegó dict`, `criticidad desconocida: 'media'. Las válidas son
[...]`, `una custodia lleva exactamente un custodio`. Ninguno era un defecto.

El contraste con el único que sí lo era —`{"error": "'id'"}`, un 500 por un
suscrito crudo en la recepción de traslados, arreglado en `7bfe956`— es el
argumento de ese commit.

## Cómo repetirlo

    $CLAUDE_JOB_DIR/tmp/ensayo.sh     # restaura el último dump y migra la copia

El comando que falta correr contra la base real:

    FLASK_APP=run.py venv/bin/flask db upgrade

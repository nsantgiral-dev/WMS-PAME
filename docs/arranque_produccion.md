# Guía de Arranque a Producción — WMS Papelería Medellín

> **Propósito:** Pasos exactos y únicos para el día que se pase de pruebas a operación real.
> Leer completo antes de ejecutar cualquier cosa.

---

## Antes de empezar: qué significa "producción real"

El WMS tiene dos "mundos" de datos:

| Mundo | Qué contiene | Quién lo dueño |
|---|---|---|
| **Siesa Enterprise** | Catálogo, precios, pedidos, stock contable | Siesa (fuente de verdad) |
| **WMS PostgreSQL** | Ubicaciones físicas, tareas, bultos, movimientos de bodega | WMS (fuente de verdad) |

El arranque es el momento en que estos dos mundos se alinean por primera vez con datos reales.

---

## ⚠️ Antes de seguir este documento al pie de la letra

**Casi todo lo de las Fases 1 y 2 ya está hecho.** Medido contra producción el
2026-08-10:

| Qué | Estado medido | Cómo se verifica |
|---|---|---|
| Catálogo | **26.294 productos activos** | `cobertura.hay_catalogo` |
| Códigos de barras | **24.176 de 26.294 — 91,9%** | `cobertura.porcentaje` |
| Ubicaciones físicas (CDI) | **45** | `GET /api/almacenes/1/ubicaciones` |
| Stock inicial | **sin correr** | `persistido.stock.alguna_vez_ok` |
| Almacenes | **7 de 9 CO** (faltan 008 y 009, ferias esporádicas) | `GET /api/almacenes/` |

Correr el sync de catálogo el día del corte «por las dudas» son ~26.000
productos contra Siesa sin necesidad, en el día que menos margen hay.

**Este documento tenía esas casillas sin marcar no porque el trabajo faltara,
sino porque no había forma de medirlo.** Ahora la hay: cada fase de abajo
termina con la consulta que responde si ya se hizo. Una casilla que se marca por
memoria vale lo mismo que no tenerla.

### La consulta que responde por casi todo

Desde la consola del navegador, logueado como admin:

```js
var tk=localStorage.getItem('wms_token')
var hh={Authorization:'Bearer '+tk}
fetch('/api/siesa/setup-inicial-estado',{headers:hh}).then(r=>r.json()).then(d=>console.log(d.persistido,d.cobertura))
```

`cobertura` sale de la base y dice qué hay **ahora**. `persistido` sale de
`registros_sync` y dice qué **corrió**, sobreviviendo a los reinicios.

Las dos responden preguntas distintas y las dos hacen falta: `alguna_vez_ok:
false` con `hay_catalogo: true` significa «el catálogo está, pero ningún sync
quedó registrado» — que es lo normal si se cargó antes de que existiera el
registro (2026-08-10).

---

## Fase 0 — Requisitos previos (hacer antes del día D)

- [ ] Railway tiene la última versión desplegada (verificar en el dashboard)
- [ ] La base de datos tiene todas las migraciones aplicadas (`flask db upgrade`)
- [ ] **Existe el servicio `worker`** con `python worker.py` — sin él los 8
      schedulers pesados no corren **y no avisan**. Se confirma en sus logs:
      `[WORKER] Alive — schedulers corriendo` cada 5 min ✅ *verificado 2026-08-10*
- [ ] Existe al menos un usuario administrador creado
- [x] Las ubicaciones físicas de la bodega están creadas — **45 en el CDI**, verificado 2026-08-10
- [ ] **`CONNEKTA_URL` apunta a producción, no a `serviciosqa`** — con
      `CONNEKTA_IKEY`/`ITOKEN` de producción en el mismo movimiento.
      Se verifica en `/api/health/siesa` → `siesa_destino.parece_qa: false`
- [ ] Las 20 variables críticas en `ok` — `/api/health/siesa` → `advertencias: []`
- [ ] Los operarios tienen usuario y contraseña en el WMS
- [ ] **Cada almacén que va a operar tiene al menos un usuario asignado.** Un
      almacén sin gente es un almacén donde nadie puede facturar ni contar
- [ ] Se eligió una **fecha/hora de corte**: el momento exacto desde el cual el WMS es el sistema oficial

---

## Fase 1 — Sincronizar el catálogo de productos

**Qué hace:** Trae todos los productos de Siesa al WMS (nombre, código, referencia, unidad).
No toca stock ni ubicaciones.

**Cuándo:** El día del arranque, antes de cualquier operación.

```
Admin → pestaña Siesa → Sincronizar catálogo
```

- Esperar a que termine (puede tomar 2–5 minutos dependiendo del catálogo)
- **Repetible sin riesgo:** si falla a mitad, se puede volver a ejecutar

**¿Hace falta?** Antes de correrlo, medir:

```js
fetch('/api/productos/?per_page=1',{headers:hh}).then(r=>r.json()).then(d=>console.log('productos:',d.total))
```

Si ya hay decenas de miles, **el catálogo está** y esta fase se salta. El
2026-08-10 había 26.294. Correrlo igual no rompe nada, pero son ~26.000
consultas a Siesa el día que menos margen hay.

---

## Fase 2 — Sincronizar códigos de barras EAN

**Qué hace:** Trae los EAN/UPC de Siesa y los asocia a cada producto local.
Necesario para que el escaneo con cámara/pistola funcione desde el primer día.

**Cuándo:** Inmediatamente después del catálogo, mismo día.

```
Admin → pestaña Siesa → Sync códigos de barras EAN
```

- El proceso corre en segundo plano (~5–10 minutos)
- Puedes seguir con la Fase 3 mientras corre
- **Repetible sin riesgo:** sobreescribe el campo `codigo_barras` con el valor de Siesa
- Después del arranque, el cron nocturno (02:00) lo mantiene actualizado automáticamente

**Verificar cobertura, no que «terminó».** Un sync que actualizó 3 productos
reporta lo mismo que uno que cubrió 24.000:

```
Admin → Siesa → ¿Cuántos NO se pueden escanear?
```

Da el porcentaje real y la lista de los que faltan, con CSV para repartir en
bodega. El 2026-08-10: **91,9% cubierto, 2.118 sin código**.

Los que falten hay que teclearlos a mano en la operación. Si alguno es de alta
rotación, conviene saberlo **antes** del corte y no cuando el operario esté con
la pistola en la mano.

---

## Fase 3 — Cargar stock inicial

**Qué hace:** Lee la existencia actual de cada producto en Siesa y la registra en la ubicación virtual `SIESA-GENERAL` dentro del WMS.

**Cuándo:** Una sola vez, el día del arranque, **después del catálogo** y **antes de que entre cualquier operación real** (ningún picking, ninguna recepción).

```
Admin → pestaña Siesa → Cargar stock inicial
```

**Antes de apretarlo, comprobar que no se corrió ya:**

```js
fetch('/api/siesa/setup-inicial-estado',{headers:hh}).then(r=>r.json()).then(d=>console.log(d.persistido.stock))
```

- `alguna_vez_ok: false` → nunca corrió. Adelante.
- `alguna_vez_ok: true` → **ya se cargó**. Ver `ultima_exitosa.inicio` para
  cuándo. No volver a correrlo sin decidirlo (ver abajo).
- `ultima_corrida.ok: null` → una corrida quedó **abierta**: el proceso murió a
  mitad. No es lo mismo que fallida — hay que mirar qué alcanzó a entrar antes
  de decidir.

Ese registro sobrevive a los reinicios de Railway. Hasta el 2026-08-10 vivía en
memoria y se borraba en cada deploy: la única defensa contra cargarlo dos veces
era la memoria de quien lo había hecho.

### Por qué solo una vez

Una vez que los operarios empiezan a mover mercancía (recepción → ubicación real, picking → despacho), el stock en el WMS empieza a vivir su propia vida. Si vuelves a presionar "Cargar stock inicial" días después:

- SIESA-GENERAL se sobreescribe con el valor que Siesa reporta en ese momento
- Ese valor puede no coincidir con lo que la WMS ya movió
- Los movimientos reales (picking, packing) no se pierden, pero el stock virtual de SIESA-GENERAL quedaría descuadrado

**En resumen: este botón es para el arranque. Después del arranque, no se vuelve a presionar salvo decisión explícita de un ajuste de inventario.**

### La excepción: ajuste de inventario periódico

Si en algún momento se hace un conteo físico y se quiere resetear el stock virtual al valor de Siesa, se puede presionar de nuevo **con conciencia**: ese día se congela toda operación, se reconcilia, y se reinicia. No es una operación rutinaria.

---

## Fase 4 — Verificación antes de operar

Antes de dar luz verde a los operarios:

- [ ] Buscar 3–5 productos por código en el PWA → aparecen con nombre correcto
- [ ] Escanear el código de barras de un producto con la cámara → lo reconoce
- [ ] **Escanear uno de los que NO tienen código** (lista en `Admin → Siesa →
      ¿Cuántos NO se pueden escanear?`) → tiene que fallar con un mensaje
      entendible, no en silencio. Son 2.118 SKU y el operario se los va a
      encontrar
- [ ] Verificar que una tarea de picking de prueba aparece en la lista del operario
- [ ] Confirmar que la reconciliación (`Admin → Reconciliación`) muestra datos coherentes con Siesa
- [ ] Confirmar que el sync nocturno está activo (el scheduler arranca automáticamente con la app)

---

## Fase 5 — Operación normal (post-arranque)

Una vez en producción, estos procesos corren solos:

| Proceso | Frecuencia | Manual? |
|---|---|---|
| Sync catálogo productos | Cada hora 7am–8pm | No (scheduler) |
| Sync códigos de barras EAN | Diario 02:00 | No (scheduler) |
| Sync pedidos | Cada 90s | No (scheduler) |
| Cargar stock inicial | **Nunca más** (salvo ajuste) | Solo admin |

---

## Qué hacer si algo sale mal

### "El catálogo no trajo todos los productos"
→ Volver a ejecutar sync de catálogo. Es idempotente (upsert), no duplica ni borra.

### "Un producto no se encuentra al escanear"
→ Verificar si el EAN está en Siesa con el panel de diagnóstico (`Admin → Siesa → Diagnóstico códigos de barras`).
→ Si Siesa lo tiene, ejecutar manualmente el sync de barcodes.
→ Si Siesa no lo tiene, el problema es de datos maestros en Siesa (reportar al consultor Siesa).

### "El stock del WMS no cuadra con Siesa al arranque"
→ Usar reconciliación para ver la diferencia.
→ Si es el primer día y no hay operaciones reales aún: se puede volver a correr "Cargar stock inicial".
→ Si ya hay operaciones: analizar caso por caso, no presionar stock inicial a ciegas.

### "Un operario no puede iniciar sesión"
→ Crear o resetear usuario desde panel Admin.

---

## Regla de oro

> **Siesa es la fuente de verdad para productos y stock contable.
> El WMS es la fuente de verdad para ubicaciones físicas y movimientos de bodega.
> Nunca editar stock en el WMS manualmente para "cuadrar" con Siesa — usar sync.**

---

*Documento generado para WMS-PAME — versión inicial abril 2026.*

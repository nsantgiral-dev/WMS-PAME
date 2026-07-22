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

## Fase 0 — Requisitos previos (hacer antes del día D)

- [ ] Railway tiene la última versión desplegada (verificar en el dashboard)
- [ ] La base de datos tiene todas las migraciones aplicadas (`flask db upgrade`)
- [ ] Existe al menos un usuario administrador creado
- [ ] Las ubicaciones físicas de la bodega están creadas en el WMS (estantes, zonas)
- [ ] Se coordinó con Siesa/Connekta que el entorno de producción está activo
- [ ] Los operarios tienen usuario y contraseña en el WMS
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
- Verificar que el contador de productos subió correctamente
- **Repetible sin riesgo:** si falla a mitad, se puede volver a ejecutar

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

---

## Fase 3 — Cargar stock inicial

**Qué hace:** Lee la existencia actual de cada producto en Siesa y la registra en la ubicación virtual `SIESA-GENERAL` dentro del WMS.

**Cuándo:** Una sola vez, el día del arranque, **después del catálogo** y **antes de que entre cualquier operación real** (ningún picking, ninguna recepción).

```
Admin → pestaña Siesa → Cargar stock inicial
```

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

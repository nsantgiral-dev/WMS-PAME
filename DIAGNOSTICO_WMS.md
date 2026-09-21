# Diagnóstico WMS — Responsividad móvil, cámara y arquitectura (v5)

**Fecha:** 2026-09-16
**Tipo:** v4 implementó los Críticos/Altos/Medios/Bajos que quedaban de v3. Esta ronda (v5) audita **Liquidación** — el único módulo que quedaba sin cubrir, ahora autorizado explícitamente por el usuario — y corrige el Crítico real que encontró.
**Reemplaza** la v4. **Cobertura completa: los 7 grupos del inventario original están auditados.**

> ⚠️ **Nota importante sobre procedencia — leer antes de confiar en cualquier "pendiente" de versiones anteriores.** Al ejecutar el roadmap de esta ronda se descubrió que el commit `907aa23` (2026-09-15 16:24, mismo día que el diagnóstico v2, aparentemente una sesión paralela/previa a esta conversación) **ya había implementado gran parte de los hallazgos Críticos/Altos/Medios de v2** — incluidos varios que esta ronda iba a "cerrar" y que resultaron ya resueltos al verificar el código real: la asimetría `reintentar-despacho`/`reintentar-recepcion` (`traslados.py:607-620`), la paleta "modo día" extendida a la confirmación de entrega del conductor (`rutas.js`, 12 sitios), el registro `_SYNC_HANDLERS` en `/api/mobile/sync` (`mobile.py:257`), y el detector de fugas de recepción (`recepcion.py:170`, ya usa `numero_oc_siesa`). **Lección aplicada:** cualquier hallazgo "pendiente" de v2/v3 debe re-verificarse contra el código actual antes de repetirlo como abierto — no basta con citar el texto del diagnóstico anterior.

## 0. Veredicto — ¿es responsivo y óptimo en dispositivos móviles?

**Sí, en los flujos que operan desde el piso.** Picking, packing, conteo (cíclico y definitivo), reposición, recepción de OC/traslados, y ahora Tienda: cámara con fallback manual, cola offline con reintento donde corta la señal, timeouts en todas las llamadas a red, zonas táctiles ≥44px, sin diálogos nativos del navegador. **Cero hallazgos Críticos o Altos abiertos en todo el sistema.** Es el resultado real de 5 rondas — no una promesa: cada corrección se verificó contra la suite completa (98 fallos preexistentes, documentados, cero regresiones nuevas en ninguna ronda) y, donde importaba, contra el código real vía `url_map`/AST, no por lectura superficial.

**Con dos salvedades honestas:**
- **Layout** sí se usa desde tablet de piso (confirmado, no solo Reposición/Conteo/Picking) — sus botones pequeños ya se corrigieron, pero fue el único módulo "de piso" que casi quedó tratado como de escritorio.
- **Liquidación y Vigía son paneles 100% de admin/gestión** (verificado por rol, no supuesto) — llegaron más tarde a los modales compartidos y timeouts que el resto del sistema. Ambos ya quedaron al día (§3), incluida la barra de progreso real en la carga de TXT de Vigía. Impacto real bajo en los dos: nadie los abre desde el piso bajo mala señal.

**Un hallazgo fuera de responsividad, ya cerrado:** `liqReintentarJob()` en Liquidación llamaba a un endpoint que nunca existió — pero resultó ser código muerto sin ningún botón que lo llamara, no una feature rota: el botón real de "Reintentar" del panel de Jobs ya usa otra función (`repReintentar`) que sí funciona. Se borró — ver §3.

---

## 1. Resumen ejecutivo

Esta ronda tuvo dos partes. Primero se aplicaron los ~15 hallazgos 🟢 Bajos de v2 que no dependían de una decisión de negocio (timeouts, `inputmode`, zonas táctiles, modales compartidos) — con dos hallazgos previos que ya habían sido resueltos por otra vía antes de empezar (Quagga2 ya estaba vendorizado, `post()`/`put()` ya tenían timeout), y un hallazgo que resultó ser código muerto en vez de una feature a completar: los IDs de cámara huérfanos de Traslados no necesitaban cámara — pertenecían a un HUD (`TRAS_PICK`/`TRAS_PACK`, ~450 líneas entre el HUD y sus dos funciones de confirmación) que el propio `index.html` ya declaraba reemplazado por las pantallas unificadas desde antes de v2, y que `tests/test_frontend_integrity.py` ya marcaba textualmente "BORRAR, no conectar". Se borró. Esto resuelve, de paso, dos hallazgos de v2 que hablaban de ese mismo código: el Crítico de persistencia offline del HUD de Traslados y el Alto de confirmación en un solo POST — ninguno de los dos necesitaba arreglarse, porque el código que describían ya no ejecuta nada. El picking/packing real de traslados usa las mismas pantallas que el picking/packing normal, que ya tienen su propia cobertura evaluada en v2.

Segundo, se completó la cobertura pendiente con tres auditorías en paralelo. La más seria es la del **backend Siesa/Connekta**: confirmó que **dos de los tres hallazgos que llevaban semanas señalados como "probablemente aún abiertos" siguen abiertos de verdad** — `siesa_sync_service.py` sigue sin `import os` (el sync de catálogo se detiene en la página 1 de 280+), y `ENTRADA_OC`/`TRASLADO_AVERIAS` siguen marcando su bandera de idempotencia después del POST, con una ventana de duplicado que ya no es teórica: existe un recovery automático de jobs atascados a los 10 minutos que la puede disparar. Encontró además un tercer job (`AJUSTE_CONTEO`) con la misma causa raíz, y una contaminación real entre tests (`monkeypatch` sobre un método de instancia en vez de la clase) que puede volver sordos los parches de Siesa según el orden en que corra la suite — reproducida en vivo, no hipotética.

Las otras dos auditorías (Conteo/Reposición/Layout y Tienda/Vigía) mantienen el patrón ya conocido: el mismo problema resuelto en un módulo sigue abierto en su vecino. `ABAST_TIMER` de Reposición no se limpia al cerrar sesión (mismo patrón que ya se corrigió para los otros timers en `pararTimers()`), Layout resultó SÍ tocarse desde tablet de piso (no solo escritorio, como se sospechaba) y tiene botones de 24-28px, y `tiendaEnviarSolicitud()` puede fallar en silencio en su segundo POST sin que el operario de tienda se entere. Del lado positivo: se cerraron varias dudas explícitas de v2 — Vigía es confirmado 100% panel de gestión (nunca se abre desde piso), la cámara ya está bien gateada en picking/conteo/reposición, y la recepción de OC en Tienda resultó ser "el mejor ejemplo del patrón correcto en todo el sistema".

**v4 (esta ronda) implementó el roadmap completo de v3** — el Crítico, los 8 Altos "quick win" y los 15+ Medios/Bajos mecánicos, verificados sintaxis+suite completa en cada tanda sin ninguna regresión. Lo más relevante del proceso no fue el código escrito, sino lo que se encontró al verificar: **el commit `907aa23` del mismo 15 de septiembre ya había resuelto una parte sustancial de los hallazgos de v2** — la asimetría de reintento de Siesa en Traslados, la paleta "modo día" del conductor, el registro centralizado de `/api/mobile/sync`, y el bug del detector de fugas de recepción, entre otros. Ninguno de esos cuatro necesitó trabajo nuevo; sí necesitaron que alguien dejara de confiar en el texto del diagnóstico anterior y mirara el código. Lo único genuinamente nuevo y de peso que había quedado sin resolver era el pre-flag mal ubicado en los 3 jobs de Siesa (`ENTRADA_OC`/`TRASLADO_AVERIAS`/`AJUSTE_CONTEO`) — se resolvió en una ronda aparte (extracción de `_ejecutar_con_preflag()` compartido, ver §3), tras diseñar primero la semántica del revert con el usuario antes de tocar nada. Con eso, no quedaba ningún hallazgo Alto o Crítico abierto en el sistema — salvo Liquidación, todavía sin auditar en ese momento. **v5 cerró esa cobertura**: encontró y corrigió exactamente el Crítico que v2 había señalado y nunca se había re-verificado (`DOCUMENTO_CONTABLE_RET` revertía el pre-flag ante cualquier excepción, incluido un timeout — ver §3). Con eso, **el diagnóstico completo queda sin ningún Crítico o Alto abierto**, cobertura total en los 7 grupos del inventario original.

---

## 2. Inventario (Paso 0)

### 2.1 Stack tecnológico

Sin cambios respecto a v2, con dos actualizaciones reales:
- **Cámara/escaneo:** Quagga2 **ya está vendorizado** en `/static/vendor/quagga2.min.js`, cacheado por `sw.js` — el hallazgo Crítico de v2 sobre la CDN externa ya no aplica (ver §3).
- **Helpers compartidos de `app.js`:** además de `_modalCantidad`, ahora existen `_modalConfirmar` (reemplazo de `confirm()`) y `_modalTexto` (reemplazo de `prompt()`), usados consistentemente en esta ronda para migrar diálogos nativos donde no tocaba flujo de pedidos/facturas/remisiones/OC.

### 2.2 Módulos/páginas del repo

| Módulo | Backend principal | Estado |
|---|---|---|
| Núcleo/Shell/PWA/Dashboard/Auth | `auth.py`, `dashboard.py`, `mobile.py`, `health.py` | ✅ Auditado (v2) |
| Cámara/Escaneo (transversal) | — | ✅ Auditado (v2), correcciones aplicadas (v3) |
| Picking | `picking.py`, `picking_service.py` | ✅ Auditado (v2) |
| Packing | `packing.py`, `packing_service.py` | ✅ Auditado (v2) |
| Recepción | `recepcion.py`, `recepcion_service.py` | ✅ Auditado (v2), correcciones parciales (v3) |
| Compras / Compras IA | `compras.py`, `armador.py` | ✅ Auditado (v2), corrección puntual (v3) |
| Rutas / Muelle / Conductor | `rutas.py`, `muelle.py` | ✅ Auditado (v2) |
| Traslados | `traslados.py`, `traslado_service.py` | ✅ Auditado (v2) — **~450 líneas de código muerto (HUD legacy) borradas en v3** |
| Kardex | `kardex.py`, `kardex_service.py` | ✅ Auditado (v2, parcial — sigue así) |
| Temporada | — | ✅ Auditado (v2) |
| Flota (paquete `flota/`) | `flota/api/*` | ✅ Auditado (v2), limpieza puntual (v3) |
| **Conteo** | `conteo.py`, `conteo_service.py` | ✅ **Auditado (v3)** |
| **Reposición** | `reposicion.py`, `reposicion_service.py` | ✅ **Auditado (v3)** |
| **Layout** | `almacenes.py`, `layout_service.py` | ✅ **Auditado (v3)** |
| **Tienda** | `tienda_oc.py` | ✅ **Auditado (v3)** |
| **Vigía** | `vigia.py` | ✅ **Auditado (v3)** — confirmado panel 100% de gestión, no operativo |
| Etiquetas | — | Cubierto tangencialmente, sin hallazgos nuevos |
| **Backend Siesa/Connekta (transversal)** | `connekta_gateway.py` y familia, `siesa_job_service.py`, `siesa_sync_service.py` | ✅ **Auditado (v3)**, excluyendo flujos propios de Liquidación |
| **Liquidación** | `despacho_parcial.py`, `liquidacion_service.py` | ✅ **Auditado (v5)** — 1 Crítico encontrado y resuelto |

### 2.3 Puntos de integración con SIESA — estado tras esta ronda

- **`siesa_sync_service.py`** — confirmado por AST: `os.environ.get()` en la línea 170 sin `import os` en ningún lugar del archivo. El sync de catálogo comete la primera página (100 productos) y se detiene ahí en cada corrida, silenciosamente. **Sigue roto.**
- **`ENTRADA_OC`/`TRASLADO_AVERIAS`** (`siesa_job_service.py:513-558` y `:586-617`) — confirmado: el flag de idempotencia se escribe después del POST, no antes. Existe recovery automático de jobs atascados a los 10 min que puede reintentar con el flag todavía en `False` tras un crash del proceso entre el POST y el commit. **Sigue abierto, y la ventana es real, no hipotética.**
- **`AJUSTE_CONTEO`** (`siesa_job_service.py:745-774`) — hallazgo nuevo: si el commit del flag falla (no el POST), el código hace `raise`, lo que consume un reintento normal con backoff y puede terminar duplicando el ajuste que el commit temprano existe para evitar.
- **Circuit breaker y timeout/retry (Regla 3):** verificados como sólidos — el circuit breaker está cableado sin excepción en los 6 gateways auditados, y el timeout distingue `ConnektaResultadoDesconocido` para ir directo a `FALLIDO` sin gastar reintentos, exactamente como pide la Regla 3.
- **Traslados** — la asimetría entre `reintentar-despacho`/`reintentar-recepcion` documentada en v2 no se revisó de nuevo en esta ronda (no estaba en el alcance de las 3 auditorías); tratar como sin cambios hasta verificar.
- **Tienda** — `TiendaOCService.iniciar_recepcion` (hallazgo Alto de v2, posible duplicación de recepciones) no se retocó; la auditoría de Tienda de esta ronda lo dejó fuera a propósito por requerir el mismo cuidado que el resto de `connekta_gateway.py`.
- **DOCUMENTO_CONTABLE_RET** (retenciones de Liquidación) — auditado en v5: seguía exactamente como v2 lo describía (revertía el pre-flag ante cualquier excepción, incluido timeout). **RESUELTO en v5** — ver §3.
- **`ENTRADA_OC`/`TRASLADO_AVERIAS`/`AJUSTE_CONTEO`** — los tres corregidos en v4 con el helper `_ejecutar_con_preflag()`. **RESUELTO.**
- **`TiendaOCService.iniciar_recepcion`** — verificado en v5: la creación real de la fila ya pasa por `RecepcionService.crear_recepcion()`, que tiene `pg_advisory_xact_lock(3003)`. **Ya estaba resuelto**, no requirió cambios.
- **Asimetría `reintentar-despacho`/`reintentar-recepcion`** — verificado en v4: `traslados.py:607-620` ya hace la verificación proactiva. **Ya estaba resuelto** por `907aa23`.

---

## 3. Tabla curada de hallazgos (Crítico → Bajo)

### ✅ Resueltos desde v2 (antes de esta ronda o durante ella)

Se listan para no perder el rastro de qué ya no hace falta re-auditar.

- **[Crítico] Motor de escaneo dependía de CDN externa** — **RESUELTO** (ya estaba vendorizado al llegar a esta ronda: `app.js` carga `/static/vendor/quagga2.min.js?v=1.8.2`, cacheado por `sw.js`).
- **[Crítico] `post()`/`put()` sin timeout** — **RESUELTO** (ya tenían `AbortController` de 25s al llegar a esta ronda: `app.js:689-708`).
- **[Crítico] Sin persistencia ni cola offline en HUD de picking/packing de Traslados** — **RESUELTO POR ELIMINACIÓN.** El HUD (`TRAS_PICK`/`TRAS_PACK`, `traslados.js`) era código muerto: `index.html` ya declaraba esas pantallas eliminadas y sustituidas por las unificadas (`pantalla-operario`/`pantalla-empacador`), que picker_traslado/packer_traslado usan de verdad y que ya tienen la cobertura de offline/cámara evaluada en v2 para picking/packing normal. Se borraron ~326 líneas del HUD.
- **[Alto] Sin fallback ante fallo de `loadScript`** — **RESUELTO** (ya envuelto en `try/catch` al llegar a esta ronda: `app.js:2043-2052`).
- **[Alto] Confirmación de picking/packing de traslados en un solo POST, no por ítem** — **RESUELTO POR ELIMINACIÓN**, mismo código muerto de arriba (`_trasPickerConfirmar`/`_trasPackerConfirmar`).
- **[Medio] Sin control de torch/zoom** — **RESUELTO** (ya implementado al llegar a esta ronda: `app.js:2117-2140`, botón condicionado a `track.getCapabilities().torch`).
- **[Bajo] Botón de refresco sin área táctil** — RESUELTO (v3, `index.html:995`, ahora 44×44px).
- **[Bajo] `Usuario.query.get()` legacy en `mobile.py`** — RESUELTO (v3, 3 sitios migrados a `db.session.get()`).
- **[Bajo] `playsInline`/`muted` no forzados en `<video>` de Quagga2** — RESUELTO (v3, `app.js`).
- **[Bajo] Modal "Etiqueta canasto" sin `max-height`** — RESUELTO (v3, `picking.js`).
- **[Bajo] `FOTOS_POR_CUSTODIA` duplicada en `traspaso.py`** — RESUELTO (v3, verificado sin importadores y borrada).
- **[Bajo] Paginación de Traslados, botones 32px** — RESUELTO (v3, ahora 44px). *Mover la paginación a server-side sigue sin evaluarse — no confirmado el volumen real del catálogo.*
- **[Bajo] `prompt()`/`confirm()` nativos** — RESUELTO **parcialmente y a propósito**: migrados a `_modalConfirmar()`/`_modalTexto()` en `app.js` (4 de 7 sitios), `traslados.js` (13 sitios, incluido `trasPickerManual` → `_modalCantidad()`), `rutas.js` (1 sitio, alta de cuenta de conductor), `recepcion.js` (2 de 5 sitios, vía `_confirmarModal()` local + los mismos helpers). **Los que quedaron sin tocar es deliberado**, no olvido: todo lo que cae en flujo de pedidos/facturas/remisiones/OC/despacho/liquidación se dejó intacto por la regla del proyecto de no tocar ese flujo — 3 en `app.js` (facturar remisión, aprobar pedido, facturar RM manual), 3 en `recepcion.js` (bono en recepción de OC, LPN de paca, aprobar NC), 9 en `rutas.js` (bultos, cierre/entrega/liquidación de ruta).
- **[Bajo] `inputmode` en compras_ia.js** — RESUELTO (v3, 2 inputs). El único caso en `recepcion.js` (devolución de cliente, `cantidad_devuelta`) se dejó sin tocar por ser flujo de facturas/NC.
- **[Bajo] IDs de cámara huérfanos en Traslados** — **RESUELTO** (era el mismo código muerto de arriba; no hacía falta agregar cámara, la pantalla unificada ya la tiene).

### ✅ Resueltos en v4 (esta ronda)

- **[Crítico] `import os` faltante en `siesa_sync_service.py`** — RESUELTO, una línea (`siesa_sync_service.py:9`).
- **[Alto] `ABAST_TIMER` sin limpiar en `pararTimers()`** — RESUELTO (`app.js`).
- **[Alto] `confirm()` nativo en las 3 eliminaciones irreversibles de Layout** — RESUELTO, 6 sitios → `_modalConfirmar(peligro:true)` (`layout.js`).
- **[Alto] Botones de Layout 24-28px** — RESUELTO, 5 botones → 40-44px (`layout.js`).
- **[Alto] Doble POST silencioso en `tiendaEnviarSolicitud()`** — RESUELTO: `else{alerta(...)}`, botón deshabilitado durante ambas llamadas, primera llamada con `post()` (no retryable, evita duplicar la solicitud), segunda con `postConReintento()` (`tienda.js`, `index.html`).
- **[Alto] Cámara faltante en recepción de traslados de Tienda** — RESUELTO, replicado el bloque de `_tiendaOCRenderScan` (`tienda.js`).
- **[Alto] `abastConfirmarScan()` sin timeout** — RESUELTO → `postConReintento()` (`reposicion.js`; confirmado 100% WMS, sin Siesa, seguro de reintentar).
- **[Alto] Contaminación de tests — `monkeypatch` de instancia en vez de clase** — RESUELTO en los **5** archivos que usan el patrón (no solo los 3 originalmente en alcance): `test_connekta_ajustes_gateway.py`, `test_connekta_facturacion_gateway.py`, `test_connekta_traslados_gateway.py`, y los 2 de Liquidación (`test_connekta_liquidacion_gateway.py`, `tests/flujo/test_liquidacion_de_punta_a_punta.py`) — hacía falta extenderlo a los 5 porque arreglar solo 3 dejaba una contaminación cruzada real y reproducida (verificado con `git stash`/suite completa antes/después). Regresión encontrada de paso y corregida: dos tests de `test_impresion.py` que verificaban `'confirm(' in cuerpo` como guard de seguridad no reconocían `_modalConfirmar(` — se actualizó el detector, no se relajó la garantía.
- **[Alto] `ENTRADA_OC`/`TRASLADO_AVERIAS`/`AJUSTE_CONTEO` — pre-flag después del POST**, **[Alto] `total_acumulado` en escaneo de packing**, **[Alto] Advisory lock en `TiendaOCService.iniciar_recepcion`**, **[Crítico] Estado de descarga de Kardex en memoria** — **sin tocar**, quedan en el roadmap "Siguiente ola" (§4): requieren diseñar la semántica del revert/lock, no son mecánicos.
- **[Medio] `prompt()`/`confirm()` en 10 sitios de `conteo.js`** (incluido `defConfirmarManual`, CC3) — RESUELTO.
- **[Medio] `fetch()` sin timeout, 21 sitios en `conteo.js` + 7 en `reposicion.js`** — RESUELTO, migrados a `get()`/`post()`/`put()`; los 2 casos especiales (export de blob, upload de CSV) recibieron `AbortController` manual porque no son JSON. `repReintentarTodosFallidos` (Siesa/`DESPACHO_F470`) se dejó sin tocar — flujo de pedidos.
- **[Medio] Botones de paginación/conteo de `tienda.js` (32px, 34px)** — RESUELTO, ahora 44px.
- **[Medio] `fetch()` sin timeout en `tienda.js` (5 sitios) y `vigia.js` (3 sitios)** — RESUELTO.
- **[Medio] `prompt()` nativo de fecha en `vigiaCompararIngesta`** — RESUELTO → `_modalTexto()`.
- **[Medio] Carga de TXT en Vigía sin timeout** — parcialmente resuelto: se agregó `AbortController` de 90s (el cuelgue indefinido ya no puede pasar); la barra de progreso real sigue sin hacerse, sigue siendo esfuerzo medio y baja prioridad (herramienta de backfill).
- **[Medio] Botones "✕" de modal (Reposición/Layout), `.kpi-grid` en los 3 sitios, manejo de error de cámara por `err.name`, todo `packing.js` (confirm/zonas táctiles/fetch), grillas de `recepcion.js`/`compras_ia.js`, `recepConfirmarTraslado`, botones de `rutas.js`, modal de `traslados.js`, comentario falso de IndexedDB en `flota.js`** — verificados: **ya estaban resueltos** por el commit `907aa23` (ver nota de procedencia arriba). Se corrigió de paso una inconsistencia real: `tiendaConfirmarRecepcionTraslado` usaba `post()` sin reintento por prudencia (Regla 3) cuando el mismo endpoint (`recepcion.js:1172`) ya estaba confirmado idempotente y usaba `postConReintento()` — alineado.
- **[Medio] Duplicación de las 4 ramas en `/api/mobile/sync`, asimetría `reintentar-despacho`/`reintentar-recepcion`, paleta "modo día" del conductor, detector de fugas de recepción** — verificados: **ya estaban resueltos** por el commit `907aa23`. Ver nota de procedencia arriba para el detalle exacto de cada uno.
- **[Bajo] 3 `fetch()` sin reintento en `recepcion.js`** (`compPoblarBloqueos`, `compDesbloquear` — bloqueo/desbloqueo de compras) — RESUELTO → `postConReintento()` (confirmado backend: éxito siempre HTTP 200/`ok:true`, `desbloquear` es un UPDATE idempotente, no un INSERT).
- **[Bajo] `prompt()` en `tiendaOCBuscarManual`** — sin tocar, a propósito: toca recepción de OC.

Verificación de cada tanda: sintaxis (`node --check`) + suite completa (`pytest tests/ -m "not postgres"`) comparada byte a byte contra el baseline de 98 fallos preexistentes — cero regresiones en las 4 corridas del día.

### 🔴 CRÍTICOS

**Los 4 Críticos del diagnóstico completo están resueltos — cero Críticos abiertos.**

- **`siesa_sync_service.py` sin `import os`** — RESUELTO en v4 (una línea, `siesa_sync_service.py:9`). Ver "✅ Resueltos en v4" arriba.
- **Cola de sincronización offline del conductor se bloqueaba ante un ítem con error** — verificado al pedirse "corregir los críticos": **ya estaba resuelto**, también por el commit `907aa23`. `rutas.js:2794-2829` (`condSyncQueue`) ya es un `for` con `try/catch` por ítem — un fallo se acumula en `fallidos[]` y el loop sigue con el resto, sin `return` que congele la cola. El comentario del propio código lo confirma: *"Un ítem con error NO debe congelar los demás... No return: sigue con el resto de la cola en vez de trabarla entera."*
- **Estado de descarga de Kardex en memoria, invisible entre workers de Gunicorn** — verificado: **ya estaba resuelto**, mismo commit. `kardex.py:20-31` ya persiste el estado en `registros_sync` (vía `registro_sync_service`), exactamente el patrón que el diagnóstico recomendaba copiar de `inventario_siesa_service.py` — el propio docstring del endpoint lo dice explícitamente, citando el motivo (2 workers) y el patrón de referencia.

**[Crítico] `DOCUMENTO_CONTABLE_RET` (documento NI de retenciones, 142882) revertía el pre-flag ante CUALQUIER excepción, incluido un timeout — RESUELTO en v5**
- **Por qué esa severidad:** era el único hallazgo del diagnóstico original (v2) que nunca se había re-verificado en ninguna ronda anterior — la auditoría de Liquidación (v5) confirmó que **seguía exactamente como lo describía v2**, no lo tocó el commit `907aa23`. Con el resto del sistema ya sin Críticos/Altos abiertos, era el hallazgo de mayor severidad real que quedaba en todo el diagnóstico.
- **Módulo/ubicación:** `siesa_job_service.py`, bloque `if job.tipo == 'DOCUMENTO_CONTABLE_RET'`, el `except Exception as _e_post:` que sigue al POST.
- **Capa:** Backend + SIESA.
- **Descripción:** el pre-flag por cuenta PUC (Regla 6) estaba bien puesto — eso ya se había corregido el 2026-08-13. El problema era el `except`: capturaba `Exception` genérico —que incluye `ConnektaResultadoDesconocido` (timeout)— y en TODOS los casos revertía (`desmarcar_puc`) antes de re-lanzar, sin distinguir "Siesa confirmó que no entró" de "no se sabe, probablemente sí entró" (Regla 3: Siesa tarda 30-60s, un timeout de lectura no es un rechazo). Si después un admin reintentaba a mano el job `FALLIDO` (`liquidacion.js:1216`), el guard de idempotencia ya no lo detenía — segundo documento NI para la misma cuenta PUC del mismo recaudo, duplicado que hay que reversar en Siesa a mano. Contraste con `RECIBO_CAJA` (mismo archivo): ese sí distingue, consultando `_factura_saldada_en_siesa` antes de decidir.
- **Solución aplicada:** se separó el `except` en dos ramas — `except ConnektaResultadoDesconocido: raise` (sin revertir, postura conservadora de Regla 0) y `except Exception: desmarcar_puc(); raise` (revierte solo ante un fallo confirmado). Sin necesidad de construir una consulta de verificación nueva contra Siesa (eso habría sido resolver algo que no era la causa del bug).
- **Verificado:** sintaxis + 157 tests directos de retenciones/liquidación/DLQ + suite completa — 98 fallos preexistentes, cero nuevos.

---

### 🟠 ALTOS

**`ENTRADA_OC`/`TRASLADO_AVERIAS`/`AJUSTE_CONTEO` — pre-flag después del POST — RESUELTO (v4, ronda de pre-flag)**

Los tres marcaban `siesa_triggered` **después** del POST — ventana de duplicado real ante un crash del proceso entre el POST exitoso y ese commit (el recovery automático de jobs atascados en `PROCESANDO` a los 10 min reintenta desde cero). `AJUSTE_CONTEO` además tenía un bug propio: si el mini-commit del flag fallaba, el código hacía `raise`, gastando un reintento normal con el flag todavía en `False` — duplicaba exactamente lo que el mini-commit existía para evitar.

Se extrajo `_ejecutar_con_preflag(obj, post_fn)` (`siesa_job_service.py`, justo antes de `_ejecutar_job`) — los tres jobs usan idénticos nombres de atributo (`siesa_triggered`/`siesa_triggered_at`), así que un helper compartido en vez de copiar el patrón 3 veces evita la divergencia silenciosa que ya costó cara en este repo (Regla 0, "una política, una función"). El helper:
1. Marca el flag `True` + commit **antes** de llamar a Siesa — el guard de idempotencia que cada job ya tenía al principio cierra la ventana de crash sin necesitar ninguna consulta de verificación contra Siesa.
2. Ante `ConnektaResultadoDesconocido` (timeout — Regla 3) **no revierte** — el dispatcher del DLQ ya manda esto a `FALLIDO` sin reintento automático; el registro queda "posiblemente enviado" para revisión manual (Regla 0).
3. Ante cualquier otro error (rechazo confirmado de Siesa) **sí revierte**, para que el backoff normal pueda reintentar de verdad.
4. Ante `modo_ensayo`, revierte (no se creó nada real).

Usado como referencia de solo lectura el patrón ya probado de `RECIBO_CAJA` (Liquidación, `siesa_job_service.py:1295-1358`) — sin auditar ni modificar Liquidación, que sigue excluida.

Verificado: sintaxis + tests directos de los 3 jobs (`test_flujo_devoluciones_recepcion.py`, `test_09_guards_criticos.py`, `test_avisos_conteo.py`, `test_connekta_compras_gateway.py`, `test_descarte_ajustes.py`, `test_recepcion_service.py`, `test_siesa_dlq.py`) + suite completa — 98 fallos preexistentes, cero nuevos.

---

**[Alto] Contaminación de tests: `monkeypatch.setattr(connekta, '_post', ...)` deja un método de instancia permanente — puede volver sordos los parches de otros archivos — RESUELTO**
- **Por qué esa severidad:** reproducido en vivo (no hipotético). CLAUDE.md ya documentó esta lección para un caso puntual (extracción de Liquidación, 2026-09-09), pero el patrón sigue vivo y sin barrer en el resto de la suite — en el módulo de mayor severidad financiera del sistema.
- **Módulo/ubicación:** patrón `monkeypatch.setattr(connekta, '_post', ...)` — **23 ocurrencias en 5 archivos** (`test_connekta_ajustes_gateway.py`, `test_connekta_facturacion_gateway.py`, `test_connekta_traslados_gateway.py`, más 2 archivos de Liquidación no evaluados por exclusión).
- **Capa:** Solo tests — no afecta producción, sí la confianza en la red de seguridad de Siesa.
- **Descripción:** reproducido con un caso mínimo: parchear `_post` con `monkeypatch.setattr(connekta, ...)` deja `'_post' in connekta.__dict__ == True` después del test, porque pytest restaura con `setattr` (no `delattr`) cuando el valor original venía heredado de la clase. Un `patch.object(ConnektaGateway, '_post', ...)` en OTRO archivo que corra después queda anulado sin avisar.
- **Solución(es):**
  - A (recomendada): `monkeypatch.setattr(ConnektaGateway, '_post', fn)` — la CLASE, no la instancia — en los 23 sitios.
  - B: migrar a `unittest.mock.patch.object(connekta, '_post', ...)` como context manager, que sí hace `delattr` en cleanup si el atributo no preexistía.
- **Cómo implementarla:** mecánico, cambio de una palabra por sitio. **Esfuerzo: bajo-medio** — correr la suite completa (no solo el archivo) después de cada cambio. Trinquete sugerido: test que verifique `'_post' not in connekta.__dict__` al final de la suite.
- **Aplicada la opción A** en los 5 archivos (3 iniciales + los 2 de Liquidación, extendidos tras confirmar que dejarlos afuera causaba la misma contaminación cruzada que el hallazgo describe — reproducido en vivo: 13 tests fallaban al correr la suite completa con solo 3/5 corregidos). Cero ocurrencias de `monkeypatch.setattr(connekta, '_post'` restantes; 5 archivos ya usan `ConnektaGateway`. Verificado: suite completa, 98 fallos preexistentes, cero nuevos.

---

**[Alto] `ABAST_TIMER` (Reposición) no se limpia en `pararTimers()` — sigue sondeando con token nulo después de cerrar sesión — RESUELTO**
- **Por qué esa severidad:** confirma la pista pendiente de v2. En una tablet compartida de bodega, un abastecedor que cierra sesión (o que salta a Layout desde su modo Reposición, algo que un operario con `puede_organizar_layout` puede hacer sin cerrar el HUD) deja el `setInterval` corriendo indefinidamente, disparando `GET /api/reposicion/tarea-actual` cada 8s con `Authorization: Bearer null` hasta que se recargue la página.
- **Módulo/ubicación:** `app.js:270-278` (`pararTimers()` limpia `_SYNC_TIMER`, `TIMER_ADMIN`, `TIMER_OPERARIO`, `TIMER_REC`, `COMP_TIMER` — no `ABAST_TIMER`); `reposicion.js:673,698` (declaración/arranque); disparable desde `app.js:869` (`salir()`) y `app.js:808` (`layoutAbrirDesdeOperario()`).
- **Capa:** Solo Frontend.
- **Descripción:** `ABAST_TIMER` sí se limpia en los caminos internos del propio módulo (`abastCerrarHUD()`, `abastCambiarAModo()`) — el cleanup global nunca lo tocó.
- **Solución:** agregar `if (typeof ABAST_TIMER !== 'undefined') clearInterval(ABAST_TIMER);` a `pararTimers()`, mismo patrón que `COMP_TIMER`.
- **Cómo implementarla:** una línea. **Esfuerzo: bajo.**
- **Aplicada** — `app.js:276`. Verificado: suite completa, cero regresiones.

---

**[Alto] `abastConfirmarScan()` (Reposición) usa `fetch()` crudo sin timeout — es la operación de mayor volumen del módulo — RESUELTO**
- **Por qué esa severidad:** cada movimiento RESERVA→PICKING pasa por acá, y bypasea los helpers centralizados (`post()`/`postConReintento()`) que ya tienen `AbortController` de 25s.
- **Módulo/ubicación:** `reposicion.js:784`.
- **Capa:** Solo Frontend.
- **Descripción:** si la petición se cuelga a medio camino (ni error franco ni respuesta), el botón queda en "Confirmando..." sin límite. El `catch` SÍ encola en `COLA_OFFLINE` ante un fallo explícito — el caso sin cubrir es el cuelgue silencioso.
- **Solución:** migrar a `postConReintento()`.
- **Cómo implementarla:** cambiar el `fetch()` manual, adaptar el manejo de `d.ok`. **Esfuerzo: bajo-medio.**
- **Aplicada** — `reposicion.js:754`, distinguiendo `e.status` (rechazo explícito del servidor, no se encola) de error de red/timeout (sí se encola en `COLA_OFFLINE`). Verificado: suite completa, cero regresiones.

---

**[Alto] Botones de acción de Layout en 24-28px — y Layout sí se toca desde tablet de piso, no solo escritorio — RESUELTO**
- **Por qué esa severidad:** cierra la duda de v2 sobre si "botones ~30px en conteo/layout" seguía abierta — en Conteo ya no se reproduce (ver verificaciones positivas), pero en Layout sí, y `layoutAbrirDesdeOperario()` (`app.js:806`) deja entrar a un operario/empacador con `puede_organizar_layout` sin salir de su turno de piso: estos botones se tocan desde el mismo dedo que hace picking.
- **Módulo/ubicación:** `layout.js:258-265` (fila legado, `padding:5px 10px;font-size:11px`), `layout.js:1436` (modal "Ver Entrepaño", `padding:5px 8px;font-size:11px`).
- **Capa:** Solo Frontend.
- **Solución:** subir a `padding:10px 14px` mínimo (≈40px de alto), o crear `.btn-icono-layout` reutilizable.
- **Cómo implementarla:** CSS puntual en 2 plantillas. **Esfuerzo: bajo.**
- **Aplicada** — `padding:10px 14px` en ambos sitios (`layout.js`, fila legado y modal "Ver Entrepaño"). Verificado: suite completa, cero regresiones.

---

**[Alto] `confirm()` nativo en las 3 eliminaciones irreversibles de Layout — RESUELTO**
- **Por qué esa severidad:** son las únicas acciones **irreversibles** ("borra también el historial real de picking/reposición... PARA SIEMPRE") de los tres módulos, y usan el diálogo nativo en vez de `_modalConfirmar(msg, {peligro:true})` que ya existe justo para este caso (fondo rojo, texto completo sin truncar).
- **Módulo/ubicación:** `layout.js:614,625` (eliminar cuerpo), `:813,836` (eliminar fila), `:1017,1029` (eliminar ubicación) — 6 sitios, mismo patrón de doble confirmación (bloqueo → forzar).
- **Capa:** Solo Frontend.
- **Descripción:** el flujo de doble confirmación en sí es una buena decisión de UX — el problema es solo el widget.
- **Solución:** reemplazar los 6 `confirm()` por `_modalConfirmar(msg, {titulo:'Eliminar', peligro:true})`.
- **Cómo implementarla:** mecánico, helper ya existe. **Esfuerzo: bajo.**

---

**[Alto] `tiendaEnviarSolicitud()` — doble POST sin guard de botón, y el segundo falla en silencio — RESUELTO**
- **Por qué esa severidad:** puede crear traslados duplicados hacia el almacén, y si el segundo paso falla el operario de tienda no se entera — la app no dice nada.
- **Módulo/ubicación:** `tienda.js:367-409`, botón sin `disabled` en `index.html:2673`.
- **Capa:** Frontend + Backend (Siesa aguas abajo).
- **Descripción:** dos `fetch()` secuenciales (crear solicitud → `/enviar`) sin `postConReintento()` ni timeout, y **el segundo no tiene rama `else`** — si falla, la función termina sin `alerta()`. La solicitud queda como `BORRADOR` en Siesa/BD sin ninguna señal en pantalla. El camino de recuperación existe (`tiendaCargarSolicitudes()` renderiza un botón "Enviar al almacén" para `BORRADOR`) pero el operario tendría que adivinar que debe ir a "Mis Pedidos" a buscarlo.
- **Solución(es):**
  - A (recomendada): `else { alerta(...) }` en el segundo fetch, deshabilitar el botón durante ambas llamadas, migrar a `postConReintento()`.
  - B: `_modalConfirmar()` en vez del `confirm()` nativo de la línea 371.
- **Cómo implementarla:** cambio contenido en la función, sin tocar backend. **Esfuerzo: bajo-medio.**
- **Aplicada** — `tienda.js:364-394`: `_modalConfirmar()`, botón deshabilitado durante ambas llamadas, primer POST sin reintento automático (evita duplicar la solicitud), segundo con `postConReintento()` (`enviar_solicitud` confirmado idempotente) y ahora con `else{alerta(...)}` — ya no falla en silencio. Verificado: suite completa, cero regresiones.

---

**[Alto] Recepción de traslados en Tienda no tiene opción de cámara — solo escáner físico/manual — RESUELTO**
- **Por qué esa severidad:** en la misma pantalla de Tienda, la recepción de OC sí ofrece cámara con gate `puede_usar_camara`; la recepción de traslados no ofrece ninguna, mismo flujo (escanear, contar, confirmar). Extiende el hallazgo Medio ya documentado de v2 sobre el gate inconsistente, con un caso concreto dentro del mismo módulo.
- **Módulo/ubicación:** `tienda.js:537-548` (`_tiendaRenderPickingTraslado`, input de texto único) vs `tienda.js:891-899` (`_tiendaOCRenderScan`, sí tiene `abrirCamara()`).
- **Capa:** Solo Frontend.
- **Solución:** replicar el bloque de cámara de `_tiendaOCRenderScan`, apuntando a `tiendaScanTraslado`.
- **Cómo implementarla:** copiar/adaptar el bloque botón+div. **Esfuerzo: bajo.**
- **Aplicada** — `tienda.js:537` (`_tiendaRenderPickingTraslado`), mismo patrón `abrirCamara()`/`cerrarCamara()` que la recepción de OC. De paso, `tiendaConfirmarRecepcionTraslado` se alineó con `recepcion.js` usando `postConReintento()` (backend verificado idempotente). Verificado: suite completa, cero regresiones.

---

**Los últimos 2 Altos de v2 — verificados y RESUELTOS (ambos por `907aa23`, confirmado hoy, cero código nuevo):**

- **`total_acumulado` en escaneo de packing** — `empProcesarEscaneo()` (`packing.js:426-487`) ya lo calcula y lo envía, ya usa `postConReintento()` con la misma cola offline que picking, y `packing_escanear` ya está registrado en `_SYNC_HANDLERS` (`mobile.py:261`).
- **`TiendaOCService.iniciar_recepcion` podía crear dos recepciones** — el check sin lock que señalaba el hallazgo (`tienda_oc_service.py:171-174`) es solo un atajo; la creación real de la fila pasa siempre por `RecepcionService.crear_recepcion()` (`recepcion_service.py:41-60`), que **ya tiene** `pg_advisory_xact_lock(3003)` antes de su propio check-then-act, con comentario explícito citando el riesgo (doble `ENTRADA_OC` en Siesa). Dos llamadas concurrentes: la segunda, serializada por el lock, encuentra la fila recién creada por la primera y lanza `ValueError`, que la ruta atrapa limpio (`tienda_oc.py:91-97`, 400) sin duplicar nada.

**Los 4 Altos de v2 nunca re-verificados — verificados hoy, 3 de 4 ya resueltos:**

- ~~El HUD de empaque sin fallback de cámara~~ — **RESUELTO**, ya estaba (`907aa23`). `index.html:2838-2845` tiene `#emp-codigo-manual` (input manual siempre presente) y `packing.js:217` gatea el botón de cámara con `puede_usar_camara`.
- ~~Conteo de traslado entrante sin persistencia en `recepcion.js`~~ — **RESUELTO**, ya estaba (`907aa23`). `_REC_CONTEOS` se persiste en `localStorage` en cada ajuste (`_recepGuardarConteoTraslado`) y se restaura al reabrir el mismo traslado (`recepcion.js:917-1001`).
- ~~`fetch()` crudo en 6+ funciones admin de `app.js`~~ — **mayormente resuelto**. Re-verificado: de los 7 `fetch(API` que quedan en el archivo, 4 son los propios helpers (`get`/`post`/`put`/`login`), 1 es `imprimirDocumento()` (imprime factura/remisión — **flujo de pedidos, no se toca**), 1 es un health-check público pre-login de bajo riesgo. El único genuinamente admin sin helper (`barcodesDescargarFaltantes`, descarga de CSV) se corrigió — ver más abajo.
- **Protección de doble-tap en `rutas.js` — expandida con autorización explícita del usuario, con el mismo cuidado que Liquidación.** De las 22 funciones que escriben a la red, 9 ya estaban protegidas (7 `conBotonOcupado()` + 2 con guard propio: `condGuardarParada`, `condSyncQueue`). Las 13 restantes se clasificaron una por una: 7 eran CRUD de vehículos/conductores sin relación con pedidos (`maestraToggle`, `maestraEliminar`, `vehiculoToggle`, `conductorToggle`, `conductorCrearCuenta`, `vehiculosCrear`) — protegidas con `conBotonOcupado()`. 3 eran despacho-adyacentes (`muelleDesasignar`, `rutasProgramar`, `condCerrarRuta`) — protegidas también, con autorización explícita punto por punto. `muelleCargarCaja` (escaneo, ya tiene su propio guard de feedback) y `_enviarConfirmacionEntrega` (interna, hereda protección de sus callers) no necesitaban nada. `conBotonOcupado()` pasó de 8 a 15 usos. Verificado: sintaxis + suite completa, cero regresiones.
  - De paso apareció código muerto real: `conductoresCrear`/`conductoresMostrarForm`/`conductoresCancelarForm` no tenían ningún formulario montado en `index.html` (el alta real de conductor vive en el módulo Usuarios con `rol=conductor`, que ya crea/sincroniza el `Conductor` — `auth.py:111-120`, `:205-220` — y `index.html:1518` ya lo documentaba). Se borraron las 3 funciones; se corrigió el comentario `DEUDA_SIN_UI` correspondiente en `tests/test_frontend_integrity.py`.

---

### 🟡 MEDIOS

*(Los Medios de v2 sin mención explícita abajo no formaron parte de esta ronda — sin cambios, ver v2.)*

---

**[Medio] `liquidacion.js` — 8 `prompt()`/`confirm()` nativos + 4 `fetch()` sin timeout — RESUELTO (v5)**
- **Ubicación:** `liqLiquidarWMS`, `liqCorregirMonto` (monto + razón), `liqConfirmarRetencion`, `liqRegistrarCobro` (con su reintento condicional por ajuste).
- **Capa:** Solo Frontend. Migrados los 8 diálogos nativos a `_modalConfirmar()`/`_modalTexto()`, y los 4 `fetch()` a `get()`/`post()`/`postConReintento()` según corresponda — `liqLiquidarWMS`/`liqConfirmarRetencion` (100% WMS, sin Siesa directo) usan `postConReintento()`; `liqRegistrarCobro` (dispara RC/NI a Siesa) usa `post()` simple, sin reintento automático (Regla 3). De paso se corrigió el texto visible "DC"→"NI" en el mensaje de éxito de `liqRegistrarCobro`.
- **Verificado:** sintaxis + suite completa — 98 fallos preexistentes, cero nuevos.

**[Bajo] `liqReintentarJob()` era código muerto — llamaba a un endpoint que nunca existió — BORRADO (v5)**
- **Por qué era Bajo y no un bug en producción:** verificado por grep exhaustivo en todo `app/static/pwa/`: **`liqReintentarJob` no tenía ningún `onclick` ni llamador** — ni siquiera dentro del propio `liquidacion.js`. No es que el botón fallara al hacer clic: no había ningún botón. El panel real de "Jobs" de Liquidación (`liqCargarJobs()` → `_liqJobCard()`, ambos funcionando) ya usa **`repReintentar(j.id)`** — una función de `reposicion.js`, cargada antes en el SHELL, que apunta al endpoint real y existente `POST /api/reposicion/siesa-jobs/<id>/reintentar` (genérico, sirve cualquier tipo de `SiesaJob`, no solo reposición). `liqReintentarJob` llamaba en cambio a `GET`/`POST /api/siesa/jobs/<id>...`, verificado contra el `url_map` real de Flask: **esas rutas no existen y nunca existieron**. Quedó como código huérfano que parecía una feature rota, cuando en realidad ya estaba reemplazada por otra que sí funciona.
- **Módulo/ubicación:** `liquidacion.js` (función `liqReintentarJob`, ~10 líneas).
- **Solución aplicada:** borrada. El botón "Reintentar" real del panel de Jobs sigue funcionando sin cambios, vía `repReintentar()`.
- **Verificado:** sintaxis + grep sin referencias colgantes + suite completa — 98 fallos preexistentes, cero nuevos.

---

**Los siguientes 8 Medios de esta lista original ya están RESUELTOS** (implementados en la ronda "Medios" de v4, con verificación de sintaxis+suite completa en su momento — ver "✅ Resueltos en v4" al principio de esta sección; se dejan aquí tachados en vez de borrarlos, para no perder el rastro de qué decía el hallazgo original):

- ~~`prompt()`/`confirm()` nativos en 9 sitios de `conteo.js`, incluida `defConfirmarManual` (CC3)~~ — migrados a los modales compartidos.
- ~~`fetch()` crudo sin timeout en 8 funciones de `reposicion.js` y 22 de `conteo.js`~~ — migrados a `get()`/`post()`/`put()` (2 casos especiales, export de blob y upload de CSV, con `AbortController` manual).
- ~~Botones de paginación 32×32px en "Pedir" (Tienda)~~ — ahora 44px.
- ~~Botones +/− de conteo en recepción de traslados de Tienda, 34×34px~~ — ahora 44px.
- ~~`fetch()` crudo sin timeout/reintento en 5 sitios de `tienda.js`~~ — migrados.
- ~~`prompt()` nativo para fecha en `vigiaCompararIngesta`~~ — migrado a `_modalTexto()`.
- ~~`fetch()` crudo sin timeout en `vigia.js` (3 sitios)~~ — migrados.

**[Medio] Carga de TXT en Vigía sin progreso real — RESUELTO**
- **Ubicación:** `vigia.js` (`vigiaCargarTxt`) + `app.js` (`subirArchivoConProgreso`, helper nuevo). Prioridad baja de origen: es herramienta de backfill admin (`VIGIA_CARGAR_TXT` nace apagada), no un camino operativo recurrente — se implementó igual, mecánico. Barra de progreso real vía `XMLHttpRequest`+`upload.onprogress`, extraído como helper compartido en `app.js` (mismo lugar que `get()`/`post()`/`put()`) en vez de embeberlo en `vigia.js` — separación de responsabilidades (transporte vs. UI/negocio del módulo). Reutilizable si algún día se decide agregarle progreso a la subida de fotos de custodia en `flota.js` (Medio ya documentado en v2, sin tocar).

**Sigue abierto (fuera de alcance, flujo de pedidos):**

**[Medio] Función de recuperación Siesa (`DESPACHO_F470`) vive dentro de `reposicion.js`, sin relación funcional con el módulo**
- **Ubicación:** `reposicion.js:621` (`siesaResetearJobsFallidos`). `DESPACHO_F470` es el job de facturación/despacho de pedidos — no tiene relación con Reposición. Mismo patrón que los "Dispatchers fuera de su módulo" que CLAUDE.md ya documenta. **No se tocó**: toca flujo de pedidos/facturas, fuera de la regla del proyecto de no tocar ese flujo sin pedido explícito. Queda documentado, no como acción sugerida.

---

### 🟢 BAJOS

*(La lista completa de v2 sigue en pie salvo lo marcado ✅ Resuelto arriba. Nuevo:)*

- **`prompt()` nativo en `tiendaOCBuscarManual`** — `tienda.js:1112`. Toca recepción de OC (zona sensible de pedidos); se deja anotado sin tocar, mismo criterio ya aplicado en `recepcion.js`. Migrar a `_modalTexto()` es mecánico si se decide.

---

## 4. Roadmap priorizado

Todos los "Quick wins" de v3 se implementaron esta ronda (v4) — ver "✅ Resueltos en v4" en §3. **No queda ningún Alto o Crítico abierto y verificado en todo el diagnóstico.** Lo que sigue es lo único que queda con trabajo real por hacer.

### Requiere coordinación o mayor diseño
1. Confirmar idempotencia real de `confirmar-picking`/`confirmar-packing` de Traslados y `POST /api/traslados/<id>/recibir` antes de decidir cola offline vs reintento corto (de v2, sin cambios).
2. Mover paginación de stock disponible en Traslados a server-side, si se confirma que el catálogo lo justifica (de v2, sin confirmar volumen real).
3. Confirmar si `confirmaciones sin protección de doble-tap en rutas.js` merece expandirse más allá de los 8 usos actuales de `conBotonOcupado()` — requiere tocar zona de pedidos/despacho, fuera de alcance sin autorización explícita.

---

## 5. Verificaciones positivas (sin acción)

*(Lo de v2 sigue vigente — zoom/orientación/defer/`.kpi-grid`, concurrencia de muelle/ruta, feedback de escaneo, `flota/` como mejor módulo, `.btn-flota` 48px, `reconciliacion_ruta.py` con consumidor real, devoluciones de cliente como referencia. De v3/v4:)*

- **El commit `907aa23` (2026-09-15) ya había resuelto la gran mayoría de los Altos/Medios de v2** antes de que empezara esta ronda de implementación — ver la nota de procedencia al inicio del documento. Confirmado con grep dirigido, no con el texto del commit.

- **Cámara en Picking/Conteo cíclico (CC1/CC2):** confirmado que el HUD compartido de `picking.js` gatea el botón de cámara con `puede_usar_camara` para PICKING/PACKING/CONTEO por igual.
- **Cámara en Conteo Definitivo (CC3) y en Reposición:** ambas respetan el mismo flag y usan `postConReintento()`/cola offline donde corresponde.
- **Offline en Reposición y Conteo Definitivo:** `abastConfirmarScan` y `defConfirmar` sí encolan en `COLA_OFFLINE` ante un fallo real de red — la pista pendiente de v2 sigue vigente y funcionando. Se trazó además el backend (`mobile.py:283-318`) para descartar un posible bug con `defConfirmar` sin campo `accion`: el fallback genérico de `/api/mobile/sync` lo procesa correctamente. Falso positivo descartado.
- **Botones ~30px en Conteo:** la pista pendiente de v2 **ya no se reproduce** — grep exhaustivo sin resultados. Cerrado.
- **Modales largos con `max-height` en Reposición/Layout:** los 4 modales grandes de estos módulos ya tienen `max-height:90vh;overflow-y:auto` o equivalente.
- **Duplicación `_repCodigoCuerpo`/`_layoutCodigoCuerpo`:** confirmada como deliberada y documentada, y las dos copias **siguen idénticas** hoy — sin divergencia real.
- **Reposición Micro — los 4 bugs corregidos el 2026-08-27 (CLAUDE.md) siguen resueltos**, sin regresión (lock 2016, `liberar_tareas_zombi`, guardarraíl de `reclasificar_ubicacion`, toast de `unidades_movidas`).
- **Recepción de OC en Tienda (`_tiendaOCRenderScan`)**: cámara con gate, input manual siempre visible como fallback, botón de búsqueda manual, manejo de `PRODUCTO_NO_EN_OC` vía `_confirmarModal()`. **El mejor ejemplo del patrón correcto en todo el sistema** — mejor incluso que Packing, marcado Alto en v2 por no tener fallback.
- **`#pantalla-tienda`** ya está acotada mobile-first (`max-width:760px` + `overflow-y:auto`).
- **`tiendaRenderFrescura()`**: badge de procedencia/antigüedad del stock mostrado — patrón sano, vale como referencia para otros módulos con datos cacheados.
- **Backend Siesa — circuit breaker, timeout/retry (Regla 3), y ausencia de `except Exception: pass`**: verificados sólidos en los 6 gateways auditados. Sin gaps encontrados.
- **Refactor de 8 dominios de `connekta_gateway.py` (2026-09-09)**: spot-check de Facturación y de la consulta anti-duplicado de FE confirma firmas y delegación consistentes con lo documentado en CLAUDE.md.

---

## 6. Cobertura pendiente y dudas explícitas

### 6.1 Cobertura — completa

Liquidación se auditó en v5 (`despacho_parcial.py`, `liquidacion_service.py`, `liquidacion.js`, `connekta_liquidacion_gateway.py` en su parte de Recibo de Caja/142888 y Documento Contable de retenciones/NI/142882). El hallazgo Crítico de v2 sobre `DOCUMENTO_CONTABLE_RET` se re-verificó, se confirmó abierto, y se corrigió — ver §3. Con esto, **los 7 grupos del inventario original quedan auditados**; no hay ningún módulo pendiente.

### 6.2 Dudas cerradas en esta ronda

1. ~~¿Traslados picking/packing debería tener cámara?~~ **Cerrada: no hacía falta.** Era código muerto (HUD legacy sin pantalla) — el flujo real usa las pantallas unificadas, que ya tienen cámara.
2. ~~¿Vigía se usa realmente desde celular?~~ **Cerrada: no.** Confirmado que los 9 endpoints de `vigia.py` están gateados a roles de gestión/admin — nunca accesible a piso. Baja la prioridad de sus hallazgos de responsividad (todos quedaron en Medio).
3. ~~¿Botones ~30px en Conteo siguen abiertos?~~ **Cerrada: ya no se reproduce.**
4. ~~¿`ABAST_TIMER`/duplicación reposicion.js-layout.js siguen igual que v1?~~ **Verificado: `ABAST_TIMER` sigue sin limpiar (ahora Alto confirmado); la duplicación sigue deliberada y sin divergencia.**

### 6.3 Dudas nuevas de esta ronda

1. **¿Layout se usa realmente desde tablet de piso con la frecuencia que sugiere `layoutAbrirDesdeOperario()`, o es un atajo poco usado?** Ya no cambia la prioridad — el hallazgo de botones pequeños se corrigió igual, sea cual sea la respuesta.
2. **¿`reposicion.js:621` (reintentar `DESPACHO_F470`) debería moverse a un panel de admin genérico de Siesa, o vive ahí a propósito por comodidad del jefe de almacén?** No se tocó por ser flujo de pedidos/facturas.
3. **¿La contaminación de tests por `monkeypatch` de instancia ya afectó algún resultado de CI real, o se descubrió antes de que importara?** No se investigó el historial de builds — solo se reprodujo el mecanismo. Ya corregido en los 5 archivos que lo tenían.

### 6.3b Dudas cerradas al implementar (resultaron ya resueltas por `907aa23`, no requerían trabajo)

- ~~Duplicación de las 4 ramas en `/api/mobile/sync`~~ — ya existía `_SYNC_HANDLERS` (`mobile.py:257`).
- ~~Asimetría `reintentar-despacho`/`reintentar-recepcion`~~ — ya verificaba proactivamente (`traslados.py:607-620`).
- ~~Paleta "modo día" del conductor~~ — ya extendida a 12 sitios de `rutas.js`, incluida la confirmación de entrega.
- ~~"Detector de fugas" de recepción — AttributeError~~ — ya usa `numero_oc_siesa` (`recepcion.py:170`), el campo correcto.

### 6.4 Dudas de v2 que siguen abiertas

1. ¿El HUD de empaque realmente necesita cámara en la práctica, o packing depende 100% de lector físico?
2. ¿La ausencia del gate `puede_usar_camara` en muelle/abastecedor/recepción es intencional o un descuido?
3. Comportamiento real de Quagga2 en iOS Safari — no verificable sin dispositivo físico. **Dejado pendiente explícitamente por el usuario (2026-09-16)** — sin acción hasta que haya un dispositivo real para probar.
4. ~~¿`POST /api/traslados/<id>/recibir` es idempotente?~~ **Cerrada: sí.** Confirmado por comentario verificado en código (`recepcion.js:1169-1171`): `TrasladoService.confirmar_recepcion` rechaza un segundo intento sobre un traslado ya `ENTREGADA` con 400, no duplica el 173079. `confirmar-picking`/`confirmar-packing` de Traslados siguen sin verificar.
5. ~~El "detector de fugas" de recepción... ¿alguien revisa los registros `FugaRecompra`?~~ **Cerrada: sí hay dónde revisarlos, la pregunta era solo "dónde".** Verificado con el código real: no es huérfano — `Admin → 🛒 Compras → sub-pestaña "🚫 Bloqueos"` (`compSubtab('bloqueos')` → `compCargarBloqueos()`, `index.html:2296/2440`) renderiza la lista de SKUs bloqueados y, debajo, automáticamente, la lista de fugas (`compCargarFugas()` → `GET /api/compras/bloqueados/fugas` → `#comp-fugas-lista`, `recepcion.js:2225-2244`). Gateado a `_es_compras()` (admin/jefe_almacén/gerente/compras). El usuario no sabía que la pantalla ya existía — el dato quedó registrado acá para que no se vuelva a perder.
6. ~~¿Con qué frecuencia real dos usuarios de tienda inician recepción sobre la misma OC en paralelo?~~ **Cerrada: hoy es un solo usuario, y la intención de negocio es centralizarlo en uno solo.** El lock `pg_advisory_xact_lock(3003)` de `RecepcionService.crear_recepcion()` (ya documentado en §3, "Los últimos 2 Altos de v2") sigue siendo correcto igual — protege el caso aunque hoy no se materialice — pero no es una carrera que el negocio espere que ocurra.
7. ~~Volumen real del catálogo de "stock disponible" por bodega en Traslados.~~ **Cerrada, implementada paginación server-side (2026-09-16).** El volumen real ya estaba medido (CLAUDE.md: 3988 SKU en NB1, verificado en vivo 2026-09-09) — bastaba para justificar el cambio sin medir más. `GET /api/traslados/stock-disponible` ahora acepta `q`/`page`/`per_page` y pagina/filtra sobre el mismo cache de Siesa (TTL 1h) que ya existía — no se repite el fetch a Siesa por paginar. `traslados.js` (`adminPedirCargarStock`) pide solo la página actual; el filtro de texto quedó debounced (350ms) en vez de recalcularse en cada tecla sobre 4000 ítems en el celular. De paso se corrigió un bug real encontrado al implementarlo: la ruta mutaba en sitio el dict que vive dentro del cache compartido (`resultado['_debug'] = ...`) — con `debug=true` una sola vez, esa clave quedaba pegada al cache y se filtraba a peticiones de otros usuarios que nunca pidieron `debug`. Ahora se arma sobre una copia. Verificado: 10 tests nuevos (`tests/test_traslados_stock_disponible_paginado.py`, incluida la mutación del cache) + sintaxis + suite completa — 98 fallos preexistentes, cero nuevos.
8. ~~`kardex_service.py` (1790 líneas) sigue sin leerse línea por línea en ninguna ronda.~~ **Cerrada — revisado (2026-09-16), ver resumen abajo. No es deuda de código: es el motor de pronóstico de demanda del sistema, activo y conectado al UI.**

**Qué hace `kardex_service.py` — resumen para quien no lo haya leído:**

No es parte del flujo operativo (picking/packing/despacho) — es un motor de **ciencia de inventario** que responde "¿cuánto y cuándo comprar?" usando el historial real de movimientos de Siesa, no supuestos. Encadenado en 4 pasos, cada uno con su propia compuerta de calidad que bloquea el siguiente si el dato no alcanza (Regla 0: "ante dato ausente, fallar hacia el lado conservador"):

1. **Descarga** (`descargar_kardex`) — trae el kardex transaccional de Siesa (movimientos T470/T350/T120) vía consulta dinámica, paginado y **reanudable** — una descarga completa mide ~16 horas reales (medido en producción, no estimado), así que corre en tandas de 25 min y retoma donde quedó. Un corte a medias se declara `estado != 'COMPLETA'`, nunca se confunde con éxito.
2. **Reconstrucción** (`reconstruir_stock_diario`) — parte del saldo actual y camina el kardex hacia atrás para reconstruir cuánto stock hubo cada día. De ahí sale `tuvo_stock` por día, el dato que permite distinguir "no se vendió porque nadie lo quería" de "no se vendió porque estaba agotado" — la censura de demanda que, sin corregir, hace comprar de menos justo en los SKUs que más se agotan.
3. **Clasificación** (`clasificar_syntetos_boylan`) — clasifica cada SKU en 4 cuadrantes (Suave/Errática/Intermitente/Grumosa) según qué tan seguido y qué tan variable es su demanda, agregada a nivel de red completa (no por bodega) porque el patrón de un SKU es una propiedad del producto, no del punto de venta.
4. **Pronóstico** — dos motores, cada uno para su cuadrante: `pronostico_tsb` (Teunter-Syntetos-Babai, para Intermitente/Grumosa — SKUs que dejan de venderse por rachas) y `newsvendor` (para temporada escolar, con deadline real del negocio — "7 de agosto 2026 — decisión del pedido escolar", cantidad óptima de compra cuando sobrar y faltar cuestan distinto). El TSB tiene una compuerta propia: si no le gana a una media móvil simple en error de pronóstico (MASE), su salida **no se usa** — es la pantalla "Panel TSB" que ya tiene tests (`test_tsb.py`, con 4 fallos preexistentes documentados en el baseline, no de esta ronda).

**Dónde vive en el UI:** `Admin → Compras → sub-pestaña "Inteligencia de inventario"` (`compras_ia.js:436` en adelante) — Clasificación S-B y el panel TSB con win-rate contra la media móvil. 13 endpoints en `app/routes/kardex.py`. También lo consumen `armador_service.py` y `temporada_service.py` (el pedido escolar).

**Conclusión para la pregunta original:** no es código huérfano ni deuda — es una pieza activa, con compuertas de calidad serias y conectada al UI real. No requiere acción de responsividad móvil (es 100% panel de admin/compras, nunca se abre desde el piso) ni encaja en el alcance SOLID/mobile de esta ronda. Si el usuario quiere profundizar en la ciencia detrás (fórmulas, supuestos estadísticos), eso es una conversación aparte, no un hallazgo de este diagnóstico.
  
### 6.5 Dudas nuevas de v5 (Liquidación)

1. **¿Con qué frecuencia un admin reintenta manualmente un job `DOCUMENTO_CONTABLE_RET` en `FALLIDO`?** Determina qué tan seguido se materializaba la ventana del Crítico ya corregido — no bloqueante, solo contexto de cuánto riesgo real había.
2. **¿Vale la pena que `_ejecutar_con_preflag()` (v4) también cubra el caso de flag-por-cuenta-PUC de `DOCUMENTO_CONTABLE_RET`?** El fix de v5 fue puntual (separar el `except` en el propio bloque) precisamente para no forzar el mecanismo de flag distinto (set de cuentas, no booleano) dentro del helper genérico. Decisión de diseño futura, no urgente.
3. ~~¿`liquidacion.js` se usa alguna vez desde un celular?~~ Sin objeto ya — los 2 Medios de esa pantalla (diálogos nativos + `fetch()` sin timeout) se migraron igual, independientemente de la respuesta.

### 6.6 Candidatos a paginación server-side, mismo patrón que Traslados (2026-09-16) — sin implementar, solo consulta

El usuario preguntó si el fix de paginación de Traslados (§6.4.7) aplica a otros módulos y si tiene efecto en escritorio. Se revisaron rutas de Conteo, Compras, Reposición, Siesa jobs y Productos — la mayoría ya está bien (`productos.py` ya pagina server-side de verdad con `.paginate()`; Conteo/Siesa-jobs/Compras al menos usan un `.limit(100-500)` defensivo). Dos endpoints quedaron con el mismo patrón sin resolver — **sin tocar, el usuario solo preguntaba, no pidió implementarlo**:

1. **`GET /api/almacenes/<id>/layout`** (`almacenes.py:482`) — carga principal de la pantalla Layout (`layout.js:162`), manda TODAS las ubicaciones del almacén con su stock actual, sin ningún límite. Más urgente que Traslados: Layout ya está confirmado como pantalla que se abre desde tablet de piso (`layoutAbrirDesdeOperario()`), no solo escritorio. **Investigado más a fondo (2026-09-16): NO es análogo directo a Traslados.** El frontend guarda la caché completa (`_layoutUbicacionesCache`) y la usa para tres cosas que necesitan visibilidad total, no solo la página/zona visible: contadores por pestaña de zona (las 5 a la vez), sugerencia de "pasillos existentes" al crear un cuerpo — **a propósito cruzando todas las zonas**, porque un pasillo es un espacio físico y no una propiedad de zona — y los modales de editar/reclasificar un cuerpo, que buscan sus huecos hermanos por ID directo en esa misma caché. Paginar en bruto (como Traslados) rompería las tres en silencio. El fix correcto es partirlo en un índice liviano (id+código+pasillo+zona, para contadores/sugerencias, sigue trayendo todo) + el detalle completo (stock, SKU asignado) solo para la zona activa — más invasivo que Traslados. **El usuario decidió dejarlo así por ahora, sin implementar.**
2. ~~`GET /api/almacenes/<id>/ubicaciones`~~ (`almacenes.py:99`) — **no era optimizable: era código muerto.** Verificado por grep en todo el PWA: cero llamadores (el único consumidor real de la lista de ubicaciones es `/layout`, que hace la misma consulta y le suma el stock). Sin test directo tampoco. **Borrado (2026-09-16)** — sobrevive el `POST` del mismo path (`crear_ubicacion`), que sí se usa. Nota aparte: el guard de rutas huérfanas de `test_frontend_integrity.py` no lo había atrapado — `/ubicaciones` es substring literal de `/ubicaciones/cuerpo`, `/ubicaciones/fila` e `/ubicaciones/importar`, que sí son cadenas reales en `layout.js`; el mismo punto ciego de "presencia, no adyacencia" que el propio archivo ya documenta para otros casos (ver CLAUDE.md, "El trinquete de rutas huérfanas medía presencia, no adyacencia"). No se tocó el detector — coste/beneficio de ampliarlo no se evaluó esta ronda.

**Sobre el efecto en escritorio (respuesta a la pregunta original):** sí existe, aunque por un motivo distinto al de bodega/tienda. En piso el cuello de botella es la red (wifi de bodega, datos móviles); en escritorio la red casi nunca es el problema — el cuello de botella pasa a ser el navegador **procesando** la respuesta (parsear miles de objetos JSON + construir esa misma cantidad de nodos HTML vía `innerHTML` bloquea el hilo de JS igual, solo que con CPU más rápido). Se nota como la pantalla "congelándose" un momento al abrir Layout, no como una carga lenta.

Verificado (borrado de `listar_ubicaciones`): sintaxis + `url_map` (confirma que solo desapareció el `GET`, el `POST` del mismo path sigue vivo) + suite completa — 98 fallos preexistentes, cero nuevos.

### 6.7 Requisiciones — 6 peticiones en paralelo + página 2 invisible — RESUELTO (2026-09-16)

Encontrado al extender la pregunta de paginación de §6.6 más allá de Traslados/Layout. `cargarRequisiciones()` (`traslados.js`) pedía las 6 listas completas en paralelo (una por estado: `ENVIADA`, `EN_PICKING`, `EN_PACKING`, `PREPARADO`, `EN_TRANSITO`, `ENTREGADA`) para pintar 6 badges y mostrar 1 sola pestaña a la vez — 5 de las 6 respuestas se descartaban sin usarse, siempre, en cada carga. Y el backend (`GET /api/traslados/`) ya paginaba a 30/página pero nunca devolvía el total de páginas ni la pantalla tenía control para pedir la página 2 — con más de 30 solicitudes en un mismo estado, el resto quedaba invisible sin ningún aviso.

**No es lo mismo que Layout.** Acá cada pestaña es una lista plana e independiente — no hay agrupamiento físico cruzado ni necesidad de ver todos los estados a la vez para que la pantalla funcione (los contadores de badge no necesitan el detalle completo, solo un conteo). Por eso sí se pudo aplicar directo, sin el rediseño que Layout necesitaría.

**Solución:** `GET /api/traslados/conteos-por-estado` (nuevo, `traslados.py`) — un `GROUP BY` liviano que alimenta los 6 badges en una sola llamada, respetando el mismo filtro por rol que ya usaba el listado (`_query_solicitudes_visibles()`, extraída para que listar y contar no puedan divergir en quién ve qué — Regla 0). `GET /api/traslados/` ahora también devuelve `paginas`. El frontend pide solo la pestaña activa + su página, y agrega un paginador real (mismo patrón visual que el de "Pedir" en Traslados). RECIBIDO conserva su comportamiento original a propósito (mostrar solo las últimas 5, sin paginador — es una decisión de UX ya tomada antes, no un límite técnico que hubiera que levantar).

Verificado: sintaxis + 5 tests nuevos (`tests/test_requisiciones_conteos_y_paginacion.py`) + suite completa — 98 fallos preexistentes, cero nuevos.

### 6.8 Pedidos ("Cola de picking") — en análisis, sin tocar nada (2026-09-16)

El usuario señaló, con un pantallazo de QA (POR DESPACHAR 42, EN PROCESO 41), que en producción la pestaña que más va a crecer es **DESPACHADO EN SIESA** — es donde los pedidos se "encolan" una vez facturados, a diferencia de POR DESPACHAR/EN PROCESO que rotan (un pedido sale de ahí en cuanto se aprueba o se completa el packing). Sería la única pestaña que en teoría necesitaría paginación real.

Arquitectura actual (`app.js:cargarPedidos`, `siesa.py:449 pedidos_aprobados`): **una sola llamada** trae todos los `PedidoSiesa` (podados por `pedidos_sync_service.py` cuando Siesa deja de reportarlos como activos — no es historial infinito, pero tampoco hay paginación de aplicación). La clasificación en las 4 pestañas (`_g(p)`: Por despachar / En proceso / Despachado en Siesa / Error Siesa) se hace **100% en el navegador**, leyendo banderas (`siesa_triggered`, `packing_estado`, `picking_iniciado`) que el propio backend ya calculó por pedido.

**Por qué no es un cambio mecánico como Requisiciones:** paginar de verdad por pestaña exige que el servidor pueda responder "dame la página 2 de DESPACHADO EN SIESA" — hoy esa clasificación no existe en SQL/Python, solo en JS. Escribirla en el backend sin que diverja de la del frontend es exactamente el patrón que ya costó caro en este repo más de una vez (Regla 0). Y es la consulta que decide qué pedido se ve como "listo para Aprobar" — un error ahí no es cosmético, es un pedido que no aparece donde debería.

**Esto es flujo de pedidos — zona con instrucción permanente de no tocar sin autorización explícita y punto por punto.** El usuario pidió seguir profundizando el análisis (no implementar). Pendiente: diseñar en detalle qué se movería al backend, cómo evitar que la clasificación diverja del JS actual, y confirmar volumen real en producción (el pantallazo es de QA, según su propio banner — "DATOS DE PRUEBA — Siesa apunta al ambiente QA, no a producción" — 42+41 no necesariamente refleja el volumen real).

Las correcciones de código de esta ronda (v4/v5, secciones "✅ Resueltos") sí modificaron archivos — a diferencia de las auditorías en sí (v3, v5), que fueron de solo lectura. Verificación completa (sintaxis + suite) después de cada tanda, detallada en §3.
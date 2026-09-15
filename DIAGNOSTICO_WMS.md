# Diagnóstico WMS — Responsividad móvil, cámara y arquitectura (v2)

**Fecha:** 2026-09-15
**Tipo:** Auditoría de solo lectura. Ningún archivo de código fue modificado.
**Reemplaza** la versión anterior de este archivo (auditoría original + ronda de correcciones aplicadas sobre ella). Esta versión relee el código **actual**, después de esas correcciones, con foco reforzado en responsividad móvil y cámara, y un formato curado por severidad con capa e implementación por hallazgo.

> ⚠️ **Cobertura parcial — léase antes que el resto del informe.** Esta ronda se ejecutó como 9 auditorías paralelas por grupo de módulo. **6 de 9 terminaron y están en este informe.** Las 3 restantes —**Conteo/Reposición/Layout**, **Liquidación/Tienda/Vigía**, y el **backend de integración Siesa/Connekta**— fueron interrumpidas a pedido explícito y quedan **pendientes, no reintentadas**. Ver §6 para el detalle de qué falta y qué se sabe de esos módulos por auditorías previas (no verificado en esta ronda).

---

## 1. Resumen ejecutivo

El sistema mejoró de forma real desde la última ronda: el zoom ya no está bloqueado, la PWA ya no fuerza orientación vertical, los 19 módulos JS cargan con `defer` sin bloquear el primer render, y los cinco flujos operativos de mayor volumen (picking, packing, conteo, reposición, recepción) ya tienen algún nivel de reintento automático o cola offline ante cortes de wifi — antes casi ninguno lo tenía. La cámara sigue siendo confiable en lo básico (feedback de escaneo, formatos de código, sin brecha QR/DataMatrix) y el módulo de Flota se confirma, otra vez, como el mejor diseñado del repositorio.

Pero el patrón de fondo que motivó la primera auditoría se repite: las correcciones se aplicaron **por partes**, no de forma sistemática, así que el mismo problema resuelto en un módulo sigue abierto en su vecino. El caso más claro es la cola offline de escaneo: picking la tiene, packing tiene el backend listo para lo mismo pero el frontend nunca lo activó, y traslados no tiene absolutamente nada — sigue perdiendo todo el progreso de un HUD completo si el navegador recicla la pestaña. Lo mismo pasa con el modal de captura de cantidad (ya existe, reutilizable, pero solo se usó en picking) y con el timeout de red (existe en `get()` desde siempre, nunca se replicó a `post()`/`put()`, que son las funciones detrás de cada confirmación operativa del sistema).

Hay además dos hallazgos nuevos de peso real encontrados en esta ronda: una condición de carrera sin corregir en `TiendaOCService.iniciar_recepcion` que puede generar dos entradas de inventario duplicadas en Siesa para la misma orden de compra, y una asimetría en cómo Traslados reintenta contra Siesa (`reintentar-recepcion` verifica proactivamente antes de reenviar; `reintentar-despacho`, con el mismo riesgo documentado en el propio código, no lo hace). Ninguno de los dos es visible para el operario en el momento en que ocurre — aparecen días después, como un duplicado que nadie relaciona con su causa.

---

## 2. Inventario (Paso 0)

### 2.1 Stack tecnológico

| Capa | Detalle |
|---|---|
| Backend | Flask 3.1 + SQLAlchemy 2.0 + PostgreSQL + Gunicorn (`--workers=2 --threads=2 --preload`) sobre Railway |
| Frontend | PWA vanilla JS sin framework ni bundler — un `index.html` de ~3650 líneas como shell/SPA + 19 `<script defer>` (18 módulos + `app.js`), todas las funciones globales, comunicación entre módulos por `onclick` en runtime |
| Estado | Sin gestor formal — variables globales por módulo; helpers compartidos crecientes en `app.js` (`get/post/put`, `postConReintento`, `guardarOffline/syncOffline`, `_modalCantidad`, `resolverEscaneoEmpaque`, `generarScanId`) |
| CSS | Sin librería de UI — CSS inline generado por JS + bloque `<style>` central en `index.html` con variables de tema, breakpoints (480/767/768/1024/1440) y clases responsive reutilizables (`.kpi-grid`) |
| Gráficos | Chart.js 4.4.1 (CDN) |
| Cámara/escaneo | Quagga2 vía CDN externo (no vendorizado — ver hallazgo Crítico) |
| Fotos (no escaneo) | `<input type="file" capture="environment">` nativo (solo `flota.js`) |
| Integración ERP | Connekta V2/V3 → Siesa Enterprise, patrón DLQ (`SiesaJob`, backoff 5→15→45min) + circuit breaker |
| Paquete aparte | `flota/` — arquitectura hexagonal independiente, fuera de `app/`, sin integración directa con Siesa |

### 2.2 Módulos/páginas del repo

| Módulo | Backend principal | Estado en esta ronda |
|---|---|---|
| Núcleo/Shell/PWA/Dashboard/Auth | `auth.py`, `dashboard.py`, `mobile.py`, `health.py` | ✅ Auditado (v2) |
| Cámara/Escaneo (transversal) | — | ✅ Auditado (v2) |
| Picking | `picking.py`, `picking_service.py` | ✅ Auditado (v2) |
| Packing | `packing.py`, `packing_service.py` | ✅ Auditado (v2) |
| Recepción | `recepcion.py`, `recepcion_service.py` | ✅ Auditado (v2) |
| Compras / Compras IA | `compras.py`, `armador.py` | ✅ Auditado (v2) |
| Rutas / Muelle / Conductor | `rutas.py`, `muelle.py` | ✅ Auditado (v2) |
| Traslados | `traslados.py`, `traslado_service.py` | ✅ Auditado (v2) |
| Kardex | `kardex.py`, `kardex_service.py` | ✅ Auditado (v2, parcial — ver §6) |
| Temporada | — | ✅ Auditado (v2) |
| Flota (paquete `flota/`) | `flota/api/*` | ✅ Auditado (v2) |
| **Conteo** | `conteo.py`, `conteo_service.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| **Reposición** | `reposicion.py`, `reposicion_service.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| **Layout** | `almacenes.py`, `layout_service.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| **Liquidación** | `despacho_parcial.py`, `liquidacion_service.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| **Tienda** | `tienda_oc.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| **Vigía** | `vigia.py` | ⏳ **Pendiente** — no reauditado esta ronda |
| Etiquetas | — | Cubierto tangencialmente, sin hallazgos nuevos |
| **Backend Siesa/Connekta (transversal)** | `connekta_gateway.py` y familia, `siesa_job_service.py`, `siesa_sync_service.py` | ⏳ **Pendiente** — no reauditado esta ronda |

### 2.3 Puntos de integración con SIESA confirmados en esta ronda

- **Recepción** — confirmación dispara `EntradaOC` (SiesaJob). Nuevo hallazgo Alto: `TiendaOCService.iniciar_recepcion` puede duplicar esta entrada (§4).
- **Traslados** — cadena completa de conectores (RequisicionTraslado, TransitoSalida/TransitoEntrada). Nuevo hallazgo Alto: asimetría de verificación proactiva entre reintento de despacho y de recepción (§4).
- **Kardex** — descarga ~17.000 movimientos desde Siesa (`kardex_service.py`, no releído línea por línea esta ronda — ver §6). El estado de esa descarga es el hallazgo Crítico de este grupo.
- **`app/services/ambiente.py`** — consulta Siesa para el "tamiz" que detecta si el ambiente es producción o QA/copia; alimenta el banner `#banner-modo` visible en **todas** las pantallas del sistema.
- **`app.js`** — panel de administración "Recuperación Siesa" y herramientas de debug (`/api/siesa/monitor`, `/jobs-fallidos`, `/sync-barcodes`, `/reconciliacion`) consumidas directo desde el shell.
- **`mobile_service.py`** — el dispensador de tareas de picking consulta `PedidoSiesa` (modelo espejo) para mostrar el nombre del cliente.
- **Flota** — sin integración directa (solo lee la bandera de ambiente).
- **Backend Connekta/Siesa dedicado** — no se reauditó esta ronda. La ronda anterior había encontrado y **nunca se corrigieron** dos hallazgos Crítico/Alto ahí (sync de catálogo roto por `import os` faltante, e idempotencia incompleta en `DOCUMENTO_CONTABLE_RET`/`ENTRADA_OC`) — ver §6, no verificados de nuevo en esta pasada, tratar como probablemente aún abiertos.

---

## 3. Tabla curada de hallazgos (Crítico → Bajo)

### 🔴 CRÍTICOS

---

**[Crítico] Motor de escaneo depende de una CDN externa no cubierta por el modo offline**
- **Por qué esa severidad:** la cámara es la interacción móvil más crítica del flujo operativo (foco explícito del encargo), y falla justo en la condición de red que el resto del sistema ya tolera.
- **Módulo/ubicación:** `app.js:1976` (`loadScript('https://cdn.jsdelivr.net/...quagga2.../quagga.min.js')`); `sw.js:8` (SHELL no lo incluye), `sw.js:61` (descarta cualquier origen externo del caché).
- **Capa:** Solo Frontend.
- **Descripción:** Quagga2 se descarga en caliente la primera vez que se abre cámara en la sesión. Si eso coincide con wifi de bodega caída, el botón de escanear queda sin reaccionar. JsBarcode ya tuvo este mismo problema y ya está resuelto (vendorizada localmente) — el patrón de solución ya existe en el propio repo.
- **Solución(es):**
  - A (recomendada): vendorizar Quagga2 igual que JsBarcode — descargar el bundle, servirlo desde `/static/vendor/`, agregarlo al `SHELL` de `sw.js`. Sin trade-off real.
  - B: mantener CDN + preload + reintento con backoff — más rápido de escribir, no resuelve el caso de wifi caída en la primera apertura.
- **Cómo implementarla:** descargar el bundle, cambiar la URL en `loadScript()`, agregar al array `SHELL`. **Esfuerzo: bajo.**

---

**[Crítico] `post()` y `put()` siguen sin timeout, a diferencia de `get()`**
- **Por qué esa severidad:** confirmado en esta ronda que **sigue sin corregirse** desde la primera auditoría. Son las funciones detrás de cada confirmación operativa (picking, packing, conteo, recepción, liquidación). En wifi inestable, una petición colgada a medias deja el botón en "Confirmando..." indefinidamente sin que el operario sepa si reintentar.
- **Módulo/ubicación:** `app.js:583-605` (`post`/`put`), contrastar con `app.js:547-557` (`get`, sí tiene `AbortController`+15s).
- **Capa:** Solo Frontend, agravado en las llamadas que disparan Siesa aguas abajo (packing, liquidación) — ahí el cuelgue hereda la latencia de un sistema externo.
- **Descripción:** sin `AbortController`/`signal`. Si la red se degrada a medio camino (ni error franco ni respuesta), la promesa nunca resuelve.
- **Solución(es):** aplicar el mismo patrón de `get()` a `post()`/`put()`.
- **Cómo implementarla:** `AbortController` + timeout en `app.js:583-605`, distinguiendo `AbortError` con el mismo mensaje que ya usa `get()`. **Cuidado:** `/api/mobile/confirmar` ya devuelve 503 con `retry_after` para "Siesa aún no respondió" — sugiere que operaciones legítimas pueden tardar más de 15s. Usar 25-30s para `post()`/`put()` en vez de los 15s de `get()`, o timeout configurable por llamada. **Esfuerzo: bajo-medio** (la lógica es simple; calibrar el valor de timeout correcto requiere criterio de negocio).

---

**[Crítico] La cola de sincronización offline del conductor se bloquea por completo ante un solo ítem con error**
- **Por qué esa severidad:** el conductor es el rol con peor conectividad del sistema (datos móviles en la calle). Si UNA confirmación falla al sincronizar, TODAS las que están detrás —entregas reales ya hechas— quedan congeladas sin ningún camino de recuperación visible.
- **Módulo/ubicación:** `rutas.js:2794-2829` (`condSyncQueue`).
- **Capa:** Solo Frontend.
- **Descripción:** el bucle hace `return` inmediato en el primer `catch`, sin `continue`. El conductor solo puede reintentar todo el lote una y otra vez, repitiendo el mismo fallo.
- **Solución(es):**
  - A (recomendada): aislar el error por ítem — `continue` en vez de `return`, acumular fallos para mostrarlos aparte.
  - B: UI dedicada de "ítems con problema" con reintentar/descartar individual (`_condDB.dequeue(id)` ya existe).
- **Cómo implementarla:** A es ~10 líneas en `condSyncQueue()`. **Esfuerzo: bajo** (A) / **medio** (B).

---

**[Crítico] Sin persistencia ni cola offline en los HUD de picking/packing de Traslados**
- **Por qué esa severidad:** exactamente el escenario que el foco de esta auditoría pide evitar. Mismas condiciones de wifi que picking/packing normales (que ya tienen cola offline), y puede tener docenas de ítems por traslado.
- **Módulo/ubicación:** `traslados.js:1164` (`TRAS_PICK`), `1318` (`TRAS_PACK`) — variables JS puras, confirmado por grep exhaustivo: cero uso de `localStorage`/`indexedDB` en todo el archivo.
- **Capa:** Frontend + Backend (el patrón de referencia, `_condDB` de `rutas.js`, ya existe en el repo).
- **Descripción:** un refresh, reciclaje de tab (común en Android), o navegación a otra pestaña borra todo el progreso sin aviso.
- **Solución(es):**
  - A (rápida, parcial): persistir `TRAS_PICK`/`TRAS_PACK` en `localStorage`, restaurar al reabrir. No resuelve sync real offline, sí sobrevive un refresh.
  - B (robusta): extender el mecanismo `guardarOffline()`/`accion` de `/api/mobile/sync` (ya usado 4 veces) con una acción `traslado_picking_confirmar`/`traslado_packing_confirmar`.
- **Cómo implementarla:** A esfuerzo **bajo-medio**. B requiere backend (guard de idempotencia + rama nueva, mecánico) y frontend (cambiar `_trasPickerConfirmar`/`_trasPackerConfirmar` a `guardarOffline()`) — pero antes hay que analizar si "confirmar picking/packing de traslado" es una operación idempotente por sí sola (no lo es hoy, a diferencia de picking normal con `total_acumulado`) — **esfuerzo: medio-alto**.

---

**[Crítico] Estado de descarga de Kardex vive en memoria de un solo proceso, invisible entre los 2 workers de Gunicorn**
- **Por qué esa severidad:** un administrador dispara una operación real contra Siesa en producción (~17.000 peticiones) y puede quedarse sin saber si terminó — fallo silencioso en la operación más pesada y sensible al horario de todo el módulo.
- **Módulo/ubicación:** `kardex.py:20` (dict a nivel de módulo) + `Procfile:1` (`--workers=2 --preload`) + `kardex.js:188-203` (polling cada 10s).
- **Capa:** Frontend + Backend + SIESA.
- **Descripción:** con `--preload`, cada worker tiene su propia copia del dict tras el `fork()`. Si el polling cae en un worker distinto al que corrió la descarga, ve "sin descargas" indefinidamente aunque haya terminado en el otro.
- **Solución(es):**
  - A (recomendada): persistir el estado en una tabla (una fila, actualizada por el hilo de background) — mismo patrón que `SiesaJob`/DLQ ya usa en el repo. `inventario_siesa_service.py` ya implementa correctamente este patrón para otra descarga — copiar de ahí.
  - B: almacén compartido entre procesos (Redis) — más rápido si ya hay Redis en infraestructura, dependencia nueva si no.
  - C (paliativo): afinidad de sesión a un worker vía Gunicorn/proxy — frágil, se rompe con cualquier redeploy.
- **Cómo implementarla:** A — modelo simple (`en_curso`, `resultado` JSON, `actualizado_en`), el hilo hace `commit()` en vez de mutar el dict. **Esfuerzo: bajo-medio.**

---

### 🟠 ALTOS

---

**[Alto] El HUD de empaque (packing) no tiene ningún fallback si la cámara falla**
- **Por qué esa severidad:** a diferencia de TODOS los demás módulos con cámara, packing no tiene vía alterna — si el permiso está denegado o el hardware falla, la estación queda inoperable. Es además el paso que dispara la factura en Siesa.
- **Módulo/ubicación:** `index.html:2834-2846` (HUD de empaque, sin `<input>` manual). Tampoco respeta el flag `OPERARIO.puede_usar_camara` que sí aplican picking y conteo CC3.
- **Capa:** Solo Frontend.
- **Solución(es):** agregar `<input id="emp-codigo-manual">` (mismo patrón que recepción/tienda) + envolver el botón de cámara en el condicional de permiso.
- **Cómo implementarla:** editar el bloque HTML del HUD + `packing.js` para leer `puede_usar_camara`. **Esfuerzo: bajo-medio.** *(Duda abierta: confirmar si packing realmente usa cámara en la práctica o depende 100% del lector físico — cambia la prioridad, ver §6.)*

---

**[Alto] Sin fallback ante fallo de `loadScript` (promesa sin catch)**
- **Por qué esa severidad:** combinado con el Crítico de Quagga2, determina si el operario ve algo o si la pantalla "no hace nada" — la peor experiencia posible.
- **Módulo/ubicación:** `app.js:1975-1977`.
- **Capa:** Solo Frontend.
- **Solución:** envolver en `try/catch`, mostrar el mismo mensaje que ya existe para fallo de `Quagga.init`.
- **Cómo implementarla:** 4 líneas. **Esfuerzo: bajo.**

---

**[Alto] `fetch()` crudo bypassa los helpers centralizados en 6+ funciones admin**
- **Por qué esa severidad:** mismo patrón de riesgo que el propio proyecto ya documentó como recurrente — un fix futuro a `post()`/`get()` no se hereda aquí.
- **Módulo/ubicación:** `app.js:1035, 1048, 1079, 1798, 2283, 2968`.
- **Capa:** Solo Frontend.
- **Solución:** migrar a los helpers existentes.
- **Cómo implementarla:** reemplazo mecánico, salvo `/api/productos/sin-codigo-barras?formato=csv` que devuelve CSV, no JSON — necesita una variante `getBlob()`/`getText()` del helper. **Esfuerzo: bajo.**

---

**[Alto] El escaneo individual de packing no envía `total_acumulado` — quedó afuera de la cola offline que sí tiene picking**
- **Por qué esa severidad:** mismo riesgo que tenía picking antes de corregirse: un empacador puede perder un escaneo por 2 segundos de corte de wifi, sin nada pendiente de reintento. El backend YA soporta el modo idempotente para PACKING (`mobile_service.py:833-891`) — el frontend simplemente no lo usa.
- **Módulo/ubicación:** `packing.js:416-473` (`empProcesarEscaneo`), `553-574` (`_elegirEmpaquePacking`).
- **Capa:** Frontend + Backend (backend ya listo, solo falta que el frontend lo consuma + una rama `accion:'packing_escanear'` mecánica en `/sync`, mismo patrón que `picking_escanear`).
- **Solución:** llevar un contador local (`cantidad_real` por ítem, ya disponible en `EMP_ITEMS`) y enviarlo como `total_acumulado`, exactamente como hace picking con `_pickingTotal`.
- **Cómo implementarla:** reordenar la búsqueda del ítem ANTES del POST, calcular el total esperado, replicar el bloque condicional de picking en el catch. **Esfuerzo: medio** (patrón ya probado, solo hay que aplicarlo).

---

**[Alto] Conteo de traslado entrante (recepción) vive solo en variable JS, sin persistencia**
- **Por qué esa severidad:** mismo patrón de riesgo que el Crítico ya documentado para `traslados.js`, aquí en el lado de recepción. Un traslado entrante puede tener decenas de ítems.
- **Módulo/ubicación:** `recepcion.js:1081-1122` (`_REC_CONTEOS`, objeto JS plano).
- **Capa:** Solo Frontend.
- **Solución:** persistir en `localStorage` en cada conteo, restaurar al reabrir la pantalla.
- **Cómo implementarla:** `localStorage.setItem('wms_rec_traslado_conteo_'+id, ...)` + restauración condicional. **Esfuerzo: bajo.**

---

**[Alto] `TiendaOCService.iniciar_recepcion` puede crear dos recepciones para la misma OC**
- **Por qué esa severidad:** si ambas se confirman, dispararían **dos** `SiesaJob` `ENTRADA_OC` para la misma orden — entrada de inventario duplicada en Siesa. Misma clase de bug ya corregida en `escanear_producto`, sin corregir aquí.
- **Módulo/ubicación:** `tienda_oc_service.py:171-185` → `recepcion_service.py:44-49` (`crear_recepcion`) — check-then-act sin lock.
- **Capa:** Frontend + Backend + SIESA.
- **Solución(es):**
  - A: constraint `UNIQUE` en BD sobre `(numero_oc_siesa, co_oc_siesa)` excluyendo `CANCELADA` — robusto, requiere migración.
  - B: advisory lock de Postgres por `numero_oc_siesa` antes del check+insert — más rápido, no requiere migración de esquema (la fila aún no existe en el primer intento, así que un `with_for_update()` de fila no alcanza).
- **Cómo implementarla:** B primero. **Esfuerzo: medio** — requiere diseño cuidadoso, no es un cambio de una línea.

---

**[Alto] Confirmaciones sin protección de doble-tap en la mayoría de las acciones de red de `rutas.js`**
- **Por qué esa severidad:** con latencia de 2-5s (común en wifi de bodega), un operario que no ve reacción toca de nuevo. Mitigado en integridad de datos por el backend, pero genera dobles peticiones/toasts innecesarios.
- **Módulo/ubicación:** solo 3 de ~20 funciones de red en `rutas.js` deshabilitan su botón durante el `await`.
- **Capa:** Solo Frontend.
- **Solución(es):**
  - A: generalizar el patrón ya usado en `condGuardarParada`.
  - B (recomendada): un helper único `conBotonOcupado(btnId, fn)` reutilizable por todos los módulos.
- **Cómo implementarla:** B es más sostenible a largo plazo. **Esfuerzo: medio** (función nueva + refactor de ~15-20 call-sites).

---

**[Alto] Confirmación de picking/packing de traslados en un solo POST al final, no por ítem**
- **Por qué esa severidad:** agrava el hallazgo Crítico de persistencia — el riesgo se concentra en el momento de mayor probabilidad de fallo (una sola petición grande al final, sin `postConReintento()`).
- **Módulo/ubicación:** `traslados.js:1283-1300` y `1416-1431` — a diferencia de `trasPickerScan` (que sí tiene reintento desde la ronda anterior).
- **Capa:** Solo Frontend.
- **Solución(es):** A (mínima) — envolver con `postConReintento()`. B — confirmar por ítem si el backend lo permite (no confirmado, ver §6).
- **Cómo implementarla:** A, cambio de una línea por función. **Esfuerzo: bajo.**

---

**[Alto] Asimetría de verificación proactiva contra Siesa entre `reintentar-despacho` y `reintentar-recepcion`**
- **Por qué esa severidad:** nace puramente de cómo se sincroniza con Siesa — consecuencia financiera/inventario real si se dispara en el peor momento (Siesa caído, admin bajo presión).
- **Módulo/ubicación:** `traslados.py:583-678` (`reintentar_despacho`) vs `681-750+` (`reintentar_recepcion_siesa`) — el segundo re-verifica contra Siesa ANTES de reenviar (con un comentario explícito de por qué), el primero solo confía en el campo local.
- **Capa:** Frontend + Backend + SIESA.
- **Solución:** replicar en `reintentar_despacho` la misma verificación previa que ya tiene `reintentar_recepcion_siesa` — la función (`recuperar_consec_salida`) ya existe en `siesa_traslado_adapter.py:84-86`.
- **Cómo implementarla:** agregar el call-site antes del `try` de reenvío. Backend puro, sin tocar frontend. **Esfuerzo: bajo.**

---

### 🟡 MEDIOS

---

**[Medio] Botones "✕" de cierre de modal por debajo del mínimo táctil**
- **Ubicación:** `index.html:3137` (Reposición), `3442` (Layout) — 30×30px.
- **Capa:** Solo Frontend. **Solución:** subir a 40-44px, o crear `.btn-cerrar-modal` reutilizable para los ~10+ modales que reimplementan este botón. **Esfuerzo: bajo** (puntual) / **medio** (clase compartida).

**[Medio] Grid de 4 columnas fijas repetido sin la clase `.kpi-grid` ya existente**
- **Ubicación:** `index.html:2256` (tab Compras admin), `2567` (pantalla Compras standalone), `tablero_bi.js:173` (Tablero BI).
- **Capa:** Solo Frontend. **Solución:** reemplazar por `class="kpi-grid"` en los 3 sitios — mismo fix ya aplicado al dashboard principal. **Esfuerzo: bajo.** Considerar un test que detecte este patrón para que no reaparezca una cuarta vez.

**[Medio] Duplicación de patrón entre las 4 ramas de `accion` en `/api/mobile/sync`**
- **Ubicación:** `mobile.py:169-286` (`cerrar_packing`, `reposicion_confirmar`, `picking_escanear`, `recepcion_escanear`).
- **Capa:** Frontend + Backend. **Solución:** registro de acciones (`dict` accion→manejador) para que una 5ª rama futura no pueda olvidar el formato de respuesta. **Esfuerzo: medio** — no urgente, riesgo hoy es hipotético.

**[Medio] Manejo de error de cámara genérico, sin distinguir causa**
- **Ubicación:** `app.js:2008-2014`. **Solución:** inspeccionar `err.name` (`NotAllowedError` es el caso más común y accionable). **Esfuerzo: bajo.**

**[Medio] Sin control de torch/zoom para poca luz**
- **Ubicación:** config de `Quagga.init`. **Solución:** botón condicionado a `track.getCapabilities().torch` (solo Android/Chrome, documentar como limitación de iOS). **Esfuerzo: medio** (requiere prueba en dispositivo real).

**[Medio] Gate de permiso `puede_usar_camara` inconsistente — aplicado en 2 de 9 puntos de entrada**
- **Ubicación:** picking.js y conteo.js lo aplican; empaque, recepción (×3), tienda, muelle, abastecedor no. **Solución:** envolver cada botón con la misma condición. **Esfuerzo: bajo**, pero confirmar primero si la ausencia en conductor/abastecedor es intencional (ver §6).

**[Medio] Zonas táctiles de 32×32px en el modal de declarar bultos de packing**
- **Ubicación:** `packing.js:666-669`, vs 48×48px en picking para el mismo control. **Solución:** subir a 44-48px. **Esfuerzo: bajo.**

**[Medio] `confirm()` nativo en 3 puntos de packing.js**
- **Ubicación:** `packing.js:264, 280, 598` (este último con contenido dinámico largo). **Solución:** modal de confirmación genérico compartido. **Esfuerzo: bajo-medio.**

**[Medio] 8 llamadas `fetch()` sin timeout en packing.js fuera de `bultosConfirmar`**
- **Ubicación:** `packing.js:213-220, 266-270, 282-285, 306-311, 588-603`. **Solución:** `AbortController`+45s (rápido) o migrar a helpers centralizados (robusto). **Esfuerzo: bajo / medio.**

**[Medio] Grillas fijas sin colapsar en pantalla chica (Compras)**
- **Ubicación:** `recepcion.js:1828,1872,1954`, `compras_ia.js:36,274,392,452,508,628`. **Solución:** `repeat(auto-fit,minmax(Xpx,1fr))`, mismo patrón ya usado en `comp-dock-stats`. **Esfuerzo: bajo**, 9 sitios mecánicos.

**[Medio] Confirmación final de traslado entrante sin reintento ni cola offline**
- **Ubicación:** `recepcion.js:1141-1164` (`recepConfirmarTraslado`), `fetch()` crudo. **Capa:** Frontend + Backend (depende de confirmar idempotencia de `POST /api/traslados/<id>/recibir`, no verificado — ver §6). **Solución:** `postConReintento()` como mínimo; cola offline si se confirma idempotencia. **Esfuerzo: bajo / medio.**

**[Medio] "Detector de fugas" en `confirmar_recepcion` nunca se ejecuta — `AttributeError` silencioso**
- **Ubicación:** `recepcion.py:170` — referencia `recepcion.oc_siesa`, el campo real se llama `numero_oc_siesa`. Capturado por un `except Exception` que lo silencia desde que se escribió.
- **Capa:** Backend puro, cero superficie visible para el usuario.
- **Solución:** corregir el nombre del atributo. **Esfuerzo: bajo** — pero confirmar primero con el equipo si el detector sigue siendo funcionalidad de negocio activa (ver §6).

**[Medio] Botones de reordenar de muelle/ruta en 28×28px**
- **Ubicación:** `rutas.js:58-62, 435-439`. **Solución:** subir a 40-44px. **Esfuerzo: bajo.**

**[Medio] Paleta oscura de bajo contraste en toda la pantalla del conductor, salvo un punto**
- **Ubicación:** patrón sistemático en `rutas.js`; único contraejemplo, `rutaVerManifiesto` (~línea 2270, "paleta modo día"). **Solución:** extender esos mismos tokens de color a la vista de conductor (lista de paradas, confirmación de entrega). **Esfuerzo: medio** (~15-20 bloques de estilo, sin lógica nueva).

**[Medio] Texto de advertencia de "Reintentar Siesa" más alarmante que el riesgo real**
- **Ubicación:** `traslados.js:776-782, 799-805`. **Solución:** una vez corregida la asimetría (hallazgo Alto), actualizar el texto para reflejar que el sistema ya verifica automáticamente. **Esfuerzo: bajo**, depende de cerrar primero el Alto relacionado.

**[Medio] Manejo de error genérico y silencioso en ~12-15 funciones administrativas de traslados.js**
- **Ubicación:** ver lista en el hallazgo original — el peor caso, `cargarTrasladosOperario`, vacía la pantalla sin ningún mensaje. **Solución:** A (mínima) nunca dejar una sección en silencio. B (completa) migrar a helpers centralizados. **Esfuerzo: bajo / medio-alto.**

**[Medio] Modal de reasignar operario con ancho fijo 320px**
- **Ubicación:** `traslados.js:555`, vs el patrón correcto (`width:100%;max-width:440px`) dos funciones más abajo en el mismo archivo. **Esfuerzo: bajo.**

**[Medio] El comentario de cabecera de `flota.js` afirma una "cola IndexedDB" para fotos offline que no existe**
- **Ubicación:** `flota.js:11-14` (comentario) vs las 3 funciones de envío real (`fetch()` directo, sin cola). Confirmado otra vez por grep exhaustivo: cero uso de IndexedDB en el archivo.
- **Capa:** Solo Frontend.
- **Solución(es):** A (rápida) — corregir el comentario para no prometer una garantía que no existe. B (robusta) — extraer `_condDB` de `rutas.js` a una utilidad compartida (mismo precedente que `postConReintento`/`guardarOffline`) y conectarla a las 3 funciones de envío de flota.js.
- **Cómo implementarla:** B es la correcta dado el contexto (patio, 5am, peor señal) — no es diseño nuevo, es extender uno ya probado en producción en el archivo hermano. **Esfuerzo: medio-alto.**

---

### 🟢 BAJOS

*(Agrupados por ser de esfuerzo bajo y menor impacto individual — todos con solución mecánica ya identificada)*

- **Botón de refresco (↻) del header sin área táctil** — `index.html:995`. Agregar padding. Bajo.
- **`prompt()`/`confirm()` nativos en 7 acciones admin de `app.js`** — líneas 1033,1044,1046,2326,2353,3166,3205. Reemplazar por modal compartido si se confirma uso móvil real (ver §6). Bajo-medio.
- **`Usuario.query.get()` estilo legacy en `mobile.py:38,50`** — cambiar a `db.session.get()`. Bajo.
- **IDs de cámara huérfanos en Traslados** — `traslados.js:1304,1435`, `cerrarCamara` sobre IDs que no existen. Decidir si Traslados debería tener cámara o eliminar las llamadas muertas. Bajo.
- **`playsInline`/`muted` no forzados explícitamente en el `<video>` de Quagga2** — `app.js:2019-2028`. Asignar explícitamente en vez de depender de la librería. Bajo (verificación real requiere iPhone físico).
- **Modal "Etiqueta canasto" de picking sin `max-height`/scroll** — `picking.js:577-598`. Una línea de CSS. Bajo.
- **`prompt()`/`confirm()` en recepcion.js, incluidos 3 encadenados** — `recepcion.js:661,795,1368,2178,2194-2198`. Reusar `_modalCantidad()` ya existente + construir un `_modalTexto()` genérico. Bajo-medio.
- **Cero `inputmode` en recepcion.js/compras_ia.js** — agregar a los inputs numéricos existentes. Bajo.
- **3 llamadas `fetch()` sin reintento en recepcion.js** — `1180, 2202` + la de traslados ya listada como Medio. Migrar a `postConReintento()`. Bajo.
- **`confirm()`/`prompt()` nativos en rutas.js/traslados.js (~20+ sitios), incluido `trasPickerManual` pese a que `_modalCantidad()` ya existe** — Bajo (la función compartida ya existe, es cablear cada call-site).
- **Paginación de stock disponible client-side + botones de página en 32px** — `traslados.js:281-292, 327-385`. Botones: bajo. Mover a server-side: medio-alto, depende del volumen real del catálogo (no confirmado).
- **Tabla de Kardex sin `overflow-x` explícito por eje** — `kardex.js:431-441`. Ya funciona vía `overflow:auto` shorthand — cosmético. Bajo, no priorizar.
- **`FOTOS_POR_CUSTODIA` duplicada y obsoleta en `flota/adaptadores/traspaso.py:43`** — verificar que nada la importe desde ahí y borrar. Bajo.
- **Subida de fotos de custodia sin barra de progreso real** — `flota.js:658-679`. Bajo impacto real (fotos ya comprimidas a cientos de KB). Migrar a `XMLHttpRequest` con `upload.onprogress` solo si se reportan subidas largas en producción. Medio si se decide hacer.

---

## 4. Roadmap priorizado

### Quick wins — solo frontend, bajo esfuerzo, alto impacto
1. Vendorizar Quagga2 (Crítico, cámara).
2. Timeout en `post()`/`put()` (Crítico, núcleo) — calibrar el valor con cuidado.
3. `try/catch` en `loadScript()` (Alto, cámara).
4. Persistir conteo de traslado entrante en `localStorage` (Alto, recepción).
5. `postConReintento()` en confirmación final de picking/packing de traslados (Alto).
6. Verificación proactiva en `reintentar_despacho` (Alto, Siesa) — backend puro, una función que ya existe.
7. Corregir `recepcion.oc_siesa` → `numero_oc_siesa` (Medio, backend, bug real silencioso).
8. `class="kpi-grid"` en los 3 grids repetidos (Medio).
9. Zonas táctiles pequeñas (botones ✕, reordenar muelle/ruta, modal bultos packing) — todos cambios de CSS puntuales.

### Siguiente ola — Frontend + Backend
1. `total_acumulado` en escaneo de packing + rama `packing_escanear`-equivalente en `/sync` (Alto).
2. Cola offline real para HUD de Traslados (Crítico) — requiere decidir semántica de idempotencia primero.
3. Advisory lock en `TiendaOCService.iniciar_recepcion` (Alto, con superficie en Siesa).
4. Helper `conBotonOcupado()` compartido + aplicarlo en rutas.js/traslados.js (Alto).
5. Extender `_condDB` (IndexedDB) del Conductor a `flota.js` (Medio, pero de alto impacto real en campo).
6. Estado de descarga de Kardex persistido en tabla (Crítico) — patrón ya existe en `inventario_siesa_service.py`, copiar de ahí.

### Requiere coordinación con Siesa o mayor diseño
1. Todo lo pendiente del backend Connekta/Siesa (§6) — retomar esa auditoría antes de tocar nada ahí, dado que incluye los hallazgos de mayor severidad conocidos del sistema (sync de catálogo, idempotencia de documentos fiscales).
2. Confirmar idempotencia real de `POST /api/traslados/<id>/recibir` y de `confirmar-picking`/`confirmar-packing` antes de decidir si se les puede dar cola offline completa o solo reintento.
3. Paleta "modo día" generalizada a toda la vista de conductor (Medio) — no depende de Siesa, pero es un cambio de diseño visual que vale la pena validar con el equipo antes de aplicar a ~15-20 bloques.

---

## 5. Verificaciones positivas (sin acción)

- Zoom, orientación, `defer`, `.kpi-grid` base, scrollbar visible, `inputmode` en index.html: **todos confirmados corregidos y estables**.
- Concurrencia de muelle y ruta (`muelle_service.py`, `ruta_service.py`): bien resuelta, sin condición de carrera.
- Feedback de escaneo (vibración/flash/sonido/debounce): consistente en todo el sistema, sin brecha de formato QR/DataMatrix.
- `flota/`: confirmado otra vez como el módulo mejor diseñado del repositorio — arquitectura hexagonal real, idempotencia explícita, invariantes con mensajes accionables.
- `.btn-flota` cumple el mínimo táctil (48px) — cierra una duda que había quedado abierta.
- `reconciliacion_ruta.py` sí tiene consumidor real (liquidacion.js) — no es código huérfano, cierra otra duda abierta.
- Devoluciones de cliente (`recepcion.js`): patrón de reintento manual + idempotencia real en backend ya es correcto, usado como referencia.

---

## 6. Cobertura pendiente y dudas explícitas

### 6.1 Grupos no reauditados esta ronda (interrumpidos a pedido del usuario)

- **Conteo / Reposición / Layout** — la ronda de correcciones anterior sí tocó `defConfirmar()` (conteo, cola offline) y `abastConfirmarScan()` (reposición, cola offline); el resto de estos tres módulos (incluyendo `layout.js` completo) no se reauditó. La auditoría previa había dejado abierto: `ABAST_TIMER` sin limpiar en `pararTimers()` (fuga de polling en tablets compartidas), botones de acción ~30px en conteo/layout, duplicación deliberada y documentada entre reposicion.js/layout.js. Nada de esto fue verificado de nuevo en esta ronda.
- **Liquidación / Tienda / Vigía** — ninguno de los tres recibió corrección en la ronda anterior. Quedaban abiertos: zonas táctiles pequeñas en liquidación, doble POST silencioso sin guard en `tiendaEnviarSolicitud()` (Alto, con un camino de recuperación BORRADOR sin aprovechar), carga de TXT sin progreso en vigía. No verificado de nuevo.
- **Backend Siesa/Connekta (transversal)** — el grupo de mayor severidad potencial. La auditoría previa (no esta ronda) había encontrado y documentado con `git blame`:
  - **Crítico, probablemente aún abierto:** `siesa_sync_service.py` usa `os.environ.get()` sin importar `os` — rompía el sync de catálogo de productos tras la primera página (100 productos). Nunca se corrigió en las rondas de fixes de esta conversación.
  - **Crítico, probablemente aún abierto:** `DOCUMENTO_CONTABLE_RET` revierte idempotencia ante timeout/resultado desconocido, a diferencia de RC/NC.
  - **Alto, probablemente aún abierto:** `ENTRADA_OC`/`TRASLADO_AVERIAS` escriben el pre-flag de idempotencia después del POST, no antes.

  **Recomendación fuerte: retomar esta auditoría específica antes que cualquier otra**, dado que estos tres hallazgos, si siguen abiertos, son de mayor severidad real que casi todo lo listado en §3.

### 6.2 Dudas explícitas de los grupos sí auditados

1. **¿El HUD de empaque realmente necesita cámara en la práctica, o packing depende 100% de lector físico?** Cambia la severidad del hallazgo Alto correspondiente.
2. **¿La ausencia del gate `puede_usar_camara` en muelle/abastecedor/recepción/tienda es intencional** (control de permiso reservado a roles de piso puro) **o un descuido?** No aplicar el gate a ciegas sin confirmar.
3. **¿Traslados picking/packing debería tener cámara?** (IDs huérfanos, Bajo).
4. **Comportamiento real de Quagga2 en iOS Safari** — no verificable sin vendorizar la librería y probar en dispositivo físico.
5. **¿`POST /api/traslados/<id>/recibir` y `confirmar-picking`/`confirmar-packing` son idempotentes ante reintento?** Determina si varios hallazgos de Alto/Medio de Traslados pueden resolverse con cola offline completa o solo reintento corto.
6. **¿El "detector de fugas" de recepción sigue siendo funcionalidad de negocio activa?** Lleva tiempo rota en silencio sin que nadie lo reportara — confirmar antes de simplemente corregir la línea.
7. **¿Con qué frecuencia real dos usuarios de tienda inician recepción sobre la misma OC en paralelo?** Afecta la prioridad real del hallazgo Alto de `TiendaOCService`.
8. **Volumen real del catálogo de "stock disponible" por bodega en Traslados** — determina si vale la pena mover la paginación a server-side.
9. **¿Las funciones admin con `prompt()`/`confirm()` nativos (app.js, recepción, rutas) se usan realmente desde celular?** Si son 100% desktop, esos hallazgos Bajo pierden relevancia frente al foco móvil del encargo.
10. **`kardex_service.py` (1790 líneas, el que ejecuta las llamadas reales a Siesa) no se leyó en ninguna ronda de esta auditoría** — pendiente si se necesita el detalle exacto de esa integración.

Ningún archivo de código fue modificado durante esta auditoría.

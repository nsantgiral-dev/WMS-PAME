# Pendientes del WMS

**Corte: 2026-08-13.** Contrastado contra BK-OPS-01 v2.1 §4.2, que es la lista
autoritativa del lado del negocio.

Este documento existe porque la lista se venía reconstruyendo de memoria en cada
conversación, y así es como se cuelan errores de atribución: en la penúltima
pasada el respaldo de base de datos figuraba como pendiente del WMS cuando el
diagnóstico lo asigna a Sistemas sobre la base de **Siesa**.

**Nada de lo que queda es código.** Lo que falta es configuración y verificación
contra producción — la única decisión de negocio que bloqueaba se cerró el
2026-08-13 y ya está implementada.

---

## 1 · Configuración — bloquea, y una tiene fecha

| Variable | Dónde | Qué pasa si falta |
|----------|-------|-------------------|
| `SIESA_COND_PAGO_RUTA` | Railway (WMS) | **La facturación se detiene.** A propósito: el fallback anterior emitía el código de contado, y una FE de contado no se aprueba. Ver «La factura de ruta no puede ser de contado» en `CLAUDE.md`. Valor: el código de crédito a un día |
| `HEAVY_SCHEDULERS=true` | Railway, servicio worker | **Las cuatro alertas por correo no salen** — incluida la de rutas entregadas sin liquidar. Verificar con `GET /api/health/siesa` → `schedulers.alertas_por_correo` |
| `CONNEKTA_URL` | Railway (WMS) | Sigue apuntando a QA. Ningún documento llega a producción hasta que cambie |
| `GUPSHUP_SOURCE` | Railway (WMS) | Avisos de vencimiento de flota en simulado. **Es la misma línea que BK-OPS-01 §4.3 pide para el Gestor** — un número, dos consumidores, y su habilitación depende de un tercero |
| `CONNEKTA_CONSULTA_NC_CONSECUTIVO` | Connekta + Railway | El motivo DIAN de la nota crédito sigue siendo manual. El SQL exacto está en el docstring de `_filas_nc_encabezado` |

`GET /api/health/siesa` responde por todas: `variables`, `schedulers` y
`pasos_manuales_nc`.

### Encendidos que van después de verificar, no antes

| Variable | Requisito previo |
|----------|------------------|
| `VIGIA_INGESTA_FACTURACION` | Correr **Vigía → «Verificar ingesta de facturación»** sobre un lunes ya cargado por TXT. Si mide distinto que el histórico, el CUSUM leería el cambio de método como un desplome del negocio |
| `FLOTA_AVISOS` → `FLOTA_AVISOS_REALES` | Dos decisiones separadas a propósito. Ejercer el barrido a mano antes («Revisar vencimientos ahora») |

---

## 2 · Decisión de negocio — **CERRADA el 2026-08-13**

`NO_PAGO_SE_QUEDO` es un **estado propio**, no un motivo. Decidido por
Dirección de Operaciones, en línea con lo que BK-OPS-01 v2.1 §4.2 ya razonaba.

Implementado junto con la **restricción del punto 4**, que compartía la
validación. Ver «`ENTREGADO_SIN_PAGO` — el cuarto estado» en `CLAUDE.md`.

**No queda ninguna decisión de negocio bloqueando al WMS.**

---

## 3 · Verificación contra producción

### La que resuelve todo lo demás

**Una liquidación de ruta completa, con los tres documentos visibles en
pantalla de Siesa.** Que el conector responda sin error no alcanza: hay que
abrir Auditoría y ver el documento.

Los tres conectores financieros —recibo de caja, nota crédito, documento de
retenciones— **no han completado ni una corrida exitosa contra producción**. Es
el único tramo del ciclo que sigue descansando en lectura de código, y es la
solución de raíz de la cartera fantasma.

Incluir una liquidación **con retención**, que nunca ha corrido.

### Bloques construidos y nunca ejercitados

Ninguno tiene un solo uso real. El de recuperación tiene reloj: después del
corte a producción ya no se puede probar sin consecuencias.

- **Impresión de etiquetas** — el bloqueo técnico se cerró el 2026-08-14
  (JsBarcode servido desde `/static/vendor/` y cacheado por el service worker;
  antes venía de un CDN que el SW no cachea y los cuatro sitios que lo usaban
  se tragaban su ausencia en un `catch` vacío, imprimiendo etiquetas sin
  código). **Falta el ejercicio físico y no lo reemplaza ningún test:** imprimir
  una de cada tipo —producto, ubicación, paca/caja, bulto— y **pasarlas por el
  láser del CD**. Media hora de una persona
- **Recuperación de estado desde Siesa** — el 2026-08-15 se cablearon los tres
  botones de traslado que existían sin `onclick` (revertir, reintentar despacho,
  reintentar recepción). Hasta ese día el `siesa_error` y el correo de alerta
  mandaban al operario a «WMS Admin → Traslados → Reintentar despacho», que **no
  existía**. Los botones solo aparecen cuando falta el consecutivo que
  corresponde. **Falta el ejercicio real:** trabar un traslado en QA (STS o ETS
  fallido) y recuperarlo desde la pantalla, verificando en Siesa que no quedó
  documento duplicado. Después del corte esto ya no se puede probar sin
  consecuencias
- Kardex
- Los seis arreglos de flota de la ronda de uso real

### Medición del daño ya ocurrido

`GET /api/rutas/liquidacion/desglose` responde tres cosas sin tocar Siesa:

| Campo | Qué dice |
|-------|----------|
| `condicion_pago_ausente.a_revisar_en_siesa` | Remisiones facturadas bajo el fallback viejo (contado). **Cada una pudo dejar una FE en Elaboración con el inventario ya descargado** |
| `fe_contado_no_aprobable` | Pedidos de ruta que declararon contado |
| `condicion_declarada` | Qué condición trae cada pedido de ruta, sobre todos a la vez |

---

## 4 · Lo que NO es del WMS

Se anota porque se ha mezclado más de una vez:

| | Dueño real |
|---|---|
| Respaldo de base de datos con restauración probada | **Sistemas, sobre la base de Siesa** (BK-OPS-01 §4.1), antes de limpiar el maestro y cargar cupos |
| Caja en preferencias de usuario | Gerencia General, en Siesa. Habilita la venta de contado **en mostrador** — no aplica a ruta |
| Perfiles, métodos de aprobación, maestros | Gerencia General / Líder de Cartera |
| Cablear la clasificación de contado a cupo y mora | Gestor de Cartera |
| Diez facturas de contado >180 días | Consulta sobre cartera de Siesa |
| Pedidos retenidos que nunca pasaron a comprometido | **El WMS no lo puede responder**: solo ve los pedidos aprobados. Sale de Siesa o no sale |

---

## 5 · Cerrado en esta pasada (2026-08-13)

De la lista del §4.2, cuatro de cinco:

- ✅ Anotar la condición de pago en la tarea — `m005condpagoparada`
- ✅ Motivos de rechazo tipificados con declaración de retorno
- ✅ Separación de los bultos que no vuelven en su propia lista
- ✅ Alerta de ruta entregada sin liquidar — `services/rezago_liquidacion.py`,
  cron 06:30, con la regla de cierre de mes (`cruza_mes`)
- ✅ `ENTREGADO_SIN_PAGO` como estado propio, con la restricción del punto 4 —
  `m006entregadosinpago`
- ⏳ Que el recibo de caja entre a Siesa — **no es código**

Y cuatro diseños **retirados**, que no hay que implementar por inercia: diferir
la factura a la liquidación, la bifurcación por forma de pago en el packing, el
límite de exposición de contado, y la devolución de remisión a mano.

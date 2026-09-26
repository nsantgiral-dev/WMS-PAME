# WMS-PAME — Referencia Central

## Stack

- **Backend**: Flask + SQLAlchemy + PostgreSQL + Gunicorn (Railway). **Dos
  paquetes, no uno**: `app/` (127 archivos) y `flota/` (29) — ver abajo
- **Frontend**: PWA vanilla JS modularizada (app.js + 16 módulos)
- **Integración ERP**: Connekta V2/V3 → Siesa Enterprise
- **DLQ**: SiesaJob con reintentos + backoff (5→15→45→120 min; `max_intentos` = 5, al 5.º fallo FALLIDO). Con `SIESA_VENTANA` configurada y fuera de ella solo procesa `ALERTA_EMAIL`; **sin la variable no hay restricción de horario** (en simulación la ventana no aplica: `dlq_puede_postear`). Ver «Tanda 2 del 2026-09-25»
- **Tests**: pytest (612 passing), CI en Railway buildCommand

## Arquitectura JS (Frontend)

```
util.js                         Base sin dependencias: `esc()`, `fmtPesos()`, `fmtUsd()` y `hoyBogota()` (el día de Bogotá: la única). Carga PRIMERO
modal.js                        El modal propio: `_modalConfirmar`, `_modalTexto`, `_modalCantidad`. Después de util, antes de app (2026-09-25)
app.js          (2,294 líneas)  Core: auth, helpers, dashboard, camera, admin
picking.js        (747)         Escaneo operario, confirmación
packing.js        (865)         Empacador HUD, bultos, etiquetas
recepcion.js    (1,921)         OCs, escaneo ciego, traslados entrantes, devoluciones
rutas.js        (2,627)         Muelle, conductor, planilla, maestras, vehículos
traslados.js    (1,366)         Panel admin traslados
conteo.js       (1,028)         Inventario cíclico, ABC
reposicion.js     (678)         Reposición RESERVA→PICKING
liquidacion.js    (722)         Liquidación financiera NCE→RC→DC
layout.js       (1,186)         Ubicaciones físicas 5 ejes
tienda.js       (1,160)         Módulo tienda
etiquetas.js      (110)         Impresión de etiquetas
vigia.js          (513)         Panel CUSUM, alarmas, carga de series
compras_ia.js     (394)         Acuerdos marco, Armador, deriva, inteligencia inventario (⚙️ Avanzado de Compras)
compras_bandeja.js              La pantalla del comprador: franja, 🛒 Bandeja, 🚢 Contenedor, 🎒 Temporada, 📦 Lo pedido (`cmpTab`)
flota.js        (2,198)         Custodia de vehículos, ficha, documentos, avisos, daños
flota_analitica.js              Sub-tab de analítica de flota (15 paneles, sin canvas) + despachador de sub-pestañas
flota_bandeja.js                Bandeja de flota: Hoy · Pendientes · Señales · Vehículos (GET /flota/bandeja)
kardex.js         (446)         Motor kardex
cartera.js                      Bloque «Retenidos por cartera» del tablero + respaldo en el WMS (autorizar / contado / re-evaluar)
temporada.js      (365)         Temporada escolar
```

Orden de carga: util → modal → app → picking → packing → recepcion → rutas → traslados → conteo → reposicion → liquidacion → layout → tienda → etiquetas → vigia → compras_ia → kardex → temporada → flota → flota_analitica → flota_bandeja. Todas las funciones son globales. Cross-module calls son runtime (onclick), nunca parse-time.

La lista autoritativa del orden real es el `SHELL` de `app/static/pwa/sw.js` —
es la que el service worker cachea. Si esta tabla y ese arreglo divergen, el
arreglo gana.

### ⚠️ `flota/` vive FUERA de `app/`

Paquete propio en la raíz, con arquitectura hexagonal (`dominio/`,
`adaptadores/`, `api/`, `puertos.py`) — distinta del resto del repo, que es
`routes/` + `services/` + `models/`.

**Un `grep` acotado a `app/` no lo ve.** Sus endpoints —**45 pares
ruta×método sobre 43 rutas**, contados contra `url_map` el 2026-09-24; este
archivo decía «21» desde la tanda 1— están registrados (`/flota/*`) y funcionan; buscarlos en `app/routes/` da cero resultados y la
conclusión natural —«esto es UI muerta»— es falsa. Para verificar si un
endpoint existe, la fuente es el `url_map`:

```bash
venv/bin/python -c "
from app import create_app
print([str(r) for r in create_app().url_map.iter_rules() if 'flota' in str(r)])"
```

| Variable | Qué hace |
|----------|----------|
| `FLOTA_AVISOS` | Enciende el barrido de vencimientos. **Nace apagado** — un cron que escribe no se enciende solo |
| `FLOTA_AVISOS_REALES` | Segunda decisión explícita: sin ella el barrido registra pero no manda |
| `FLOTA_AVISO_TELEFONOS` | Destinatarios |
| `FLOTA_PREVENTIVO` | Enciende el cron de **siembra del plan preventivo** desde la ficha técnica (05:30 Bogotá). **Nace apagado**, default ausente = `false`. El primer ciclo escribe hasta ~36 filas de plan y **manda cero avisos**: ninguna tarea tiene ejecución registrada todavía, y una tarea sin línea base no está al día ni vencida. Nace apagado igual, porque la regla 10 no se dobla con un argumento y porque el ciclo peligroso no es el primero sino el que sigue a una carga masiva del historial del taller |
| `FLOTA_FOTOS_DIR` | Almacén de fotos de custodia. **Ruta absoluta** en un volumen montado (relativa = error de configuración). El health (`almacen_fotos`) dice desde el proceso que sirve qué carpeta mira, si es un volumen y cuántas fotos `ok` no tienen archivo — ver «Pantallas de la operación diaria» |
| `GUPSHUP_API_KEY` · `GUPSHUP_SOURCE` · `GUPSHUP_APP_NAME` · `GUPSHUP_TEMPLATE_IDS` | Canal WhatsApp. **`GUPSHUP_SOURCE` es la línea de mensajería cuya habilitación a producción depende de un tercero** — la misma que BK-OPS-01 §4.3 lista bajo Gestor de Cartera. Un número, dos consumidores |

### Dispatchers fuera de su módulo

Dos sub-navegaciones viven en un archivo distinto al de la lógica que invocan.
Buscar aquí antes de darlos por inexistentes:

| Dispatcher | Definido en | Invoca lógica de |
|-----------|-------------|------------------|
| `cmpTab()` | `compras_bandeja.js` | las pestañas de Compras; ⚙️ Avanzado delega en `compSubtab()` |
| `compSubtab()` | `recepcion.js` | ⚙️ Avanzado de Compras: `compras_ia.js` (modelos, acuerdos, armador, punto de pedido) y la operación de bodega (`recepcion.js`) |
| `invSubtab()` | `conteo.js:94` | `compras_ia.js` (inteligencia inventario) |

---

### Todo dato que se pinta va con `esc()` (2026-09-09)

**La PWA arma su HTML con literales de plantilla y lo asigna a `innerHTML`.**
Cualquier texto que escribió una persona —una descripción, un motivo, el nombre
de un producto— se envuelve en `esc(...)` de `util.js` antes de interpolarlo.

El caso concreto: `POST /flota/custodia/traspaso` es `LECTURA_FLOTA`, o sea que
**el conductor escribe** el motivo de un cierre forzado, y ese texto se pinta en
la pantalla de gestión. Del rol con menos permisos a la sesión del que más
tiene. Hoy hay `esc()` en **1.265 interpolaciones de 19 archivos**, y el guard
`TestNingunDatoLlegaCrudoAlInnerHTML` (`tests/test_frontend_integrity.py`)
impide que entre una nueva sin escapar.

**Los dos atajos que NO sirven, para no volver a proponerlos:**

- **Sanear el HTML ya armado.** Esta app pone sus propios `onclick=` en la misma
  cadena que los datos. Después de concatenar no se distingue el marcado propio
  del inyectado: un saneador que borre manejadores inline mata la interfaz, y
  uno que los respete deja pasar el ataque.
- **Escapar la respuesta en `get()`.** Corrompe el dato: un texto que se edita y
  se reenvía viajaría con `&lt;` adentro y se guardaría así. El escape pertenece
  al sitio donde el dato se vuelve HTML, no al sitio donde llega.

**`esc()` vive en `util.js` y no en `app.js`** porque los arneses de Node
stubbean lo que viene de `app.js` (`get`, `horaColombia`, `alerta`). Un `esc`
stubbeado convierte todos los tests de escapado en tests del stub: verde con la
función real rota. Todo arnés nuevo tiene que cargar `util.js` de verdad.

**Lo que el guard NO cubre**, medido el 2026-09-09 y escrito para que nadie lo
suponga cubierto:

| Hueco | Sitios |
|---|---|
| HTML armado por concatenación con `+` | 75 |
| Dato dentro de JS dentro de un atributo — `onclick="fn('${x}')"` | 206 |
| Identificadores sueltos (`${cuerpo}`): pueden ser marcado construido | — |

El segundo merece su renglón: **`esc` no protege ahí y no puede**. El navegador
decodifica las entidades del atributo antes de que el JS corra, así que un
`&#39;` vuelve a ser `'` y rompe la cadena igual. No empeora nada —sin `esc`
rompía idéntico— pero el arreglo es otro: pasar un id y buscar el dato, no el
texto.

## Mapa de Conectores Siesa

### Escritura (POST)

| ID | Nombre | Qué hace | Flujo WMS | Job DLQ | Función gateway |
|----|--------|----------|-----------|---------|-----------------|
| 238925 | FacturaDesdePedido | FE directa desde pedido comprometido | **No usado** — `trigger_factura()` no tiene ningún caller; todo cierre (completo o parcial) pasa por 244328→142945→142943 | — | `trigger_factura()` (código muerto) |
| 142945 | RemisionPedido | Remisión — descarga inventario cuenta 14 | Cierre packing, completo o parcial (unificado en `DespachoParialService`) | DESPACHO_F470 | `trigger_despacho()` |
| 142943 | FacturaDesdeRemision | FE desde remisión existente | Post-142945 (cadena) | — (inline) | `trigger_factura_desde_remision()` |
| 142948 | EntradaOC | Entrada por orden de compra | Recepción confirmada | ENTRADA_OC | `confirmar_entrada_compras()` |
| 142951 | DocumentoInv | Ajuste físico / transferencia averías | Conteo cíclico / devolución | AJUSTE_CONTEO / TRASLADO_AVERIAS | `enviar_ajuste_inventario()` / `transferir_a_averias()` |
| 173066 | TransferenciaDirecta | Transferencia intra-bodega | Reposición RESERVA→PICKING | TRANSFERENCIA_UBICACIONES | `transferir_entre_ubicaciones()` |
| 173076 | TransitoSalida (STS) | Salida en tránsito inter-bodega | Despacho traslado | DESPACHO_TRASLADO | `transferencia_transito_salida()` |
| 173079 | TransitoEntrada (ETS) | Llegada en tránsito | Recepción traslado | — (inline) | `transferencia_transito_entrada()` |
| 174646 | RequisicionTraslado (RIT) | Requisición de transferencia | Aprobación traslado | — (inline) | `crear_requisicion_traslado()` |
| 174930 | TransferenciaDesdeRIT | STS desde RIT existente | Despacho traslado (con RIT) | DESPACHO_TRASLADO | `despachar_desde_requisicion()` |
| 244328 | CompromisosPedido | Actualiza cantidades comprometidas (paso 1 del cierre de pedido) | Cierre packing, completo o parcial (unificado en `DespachoParialService`) | DESPACHO_F470 | `trigger_comprometer_pedido()` |
| 142946 / 250696 | NotaFactura (NCE) | Nota crédito, **sin** cruce automático de cartera | ❌ Código muerto — `trigger_nota_factura()` no tiene ningún caller desde el commit `07cb5df` (2026-08-13); ni siquiera está registrado en Connekta. Ver "NCE — qué conector usar" | — | `trigger_nota_factura()` (sin caller) |
| 251126 | NotaCredito CrearCruzar | Crea la NC **y cruza cartera** en un solo POST | Devolución de Cliente confirmada por recepción **y** Liquidación de ruta (mismo conector para las dos desde `07cb5df`) | NOTA_CREDITO_DEVOLUCION_CLIENTE, NOTA_CREDITO_FACTURA | `trigger_nota_factura_crear_cruzar()` |
| 251546 | NotaCredito MotivoDIAN | Segundo POST: fija el motivo DIAN sobre la NC ya creada | Encadenado tras `NOTA_CREDITO_DEVOLUCION_CLIENTE` | MOTIVO_DIAN_NC | `trigger_motivo_dian_nc()` |
| 142888 | ReciboCaja (RC) | Registro de cobro del conductor | Liquidación: CONTADO | RECIBO_CAJA | `trigger_recibo_caja()` |
| 142882 | DocumentoContable, tipo **NI** (Nota de legalización) | Retenciones tributarias | Liquidación: con retención | DOCUMENTO_CONTABLE_RET | `trigger_documento_contable()` |

### Consulta (GET)

| API | Función gateway | Qué consulta |
|-----|-----------------|--------------|
| API_v2_Ventas_Pedidos | `get_pedidos_aprobados()`, `get_estado_pedido()`, `get_pedido_cabecera()` | Pedidos aprobados, estado, cabecera (NIT, cond_pago) |
| API_v2_Compras_Ordenes | `get_ordenes_compra_aprobadas()` | OCs para recepción |
| API_v2_Inventarios_InvFecha | `get_inventario_fecha()`, `get_stock_bodega()`, `fotos_siesa_service._costos_de_bodega()` | Stock actual por bodega; costo de la foto diaria. **Pagina inestable** (filas repetidas entre páginas) |
| API_v2_Items | `get_items_catalogo()` | Catálogo productos |
| API_v2_ItemsBarras | `get_item_por_barras()` | Barcode → SKU |
| API_v2_ItemsUnidadesMedida | `get_items_unidades_medida()` | Factores de conversión empaque |
| API_v2_Ubicaciones | `get_ubicaciones_siesa()` | Ubicaciones por bodega |
| API_v2_Ventas_Facturas_DesdePedido | `get_factura_desde_remision()`, `get_rowids_factura()`, `vigia_service._facturas_de_semana()` | Anti-duplicado de la FE **desde remisión**, rowids para NCE, base gravable, foto diaria de ventas (`fotos_siesa_service`). **Fecha entre comillas** (`lit_fecha`): sin ellas devuelve cero filas. Ojo: `get_factura_desde_pedido()` NO usa esta API pese al nombre — usa la dinámica `papeleriamedellin_monitos_facturas_wms` |
| API_v2_CxC_General | `get_cxc_general()`, `fotos_siesa_service.fotografiar_cartera()` | Cuentas por cobrar (f253_id para cruce); foto diaria de cartera 1305 abierta |
| API_v2_Bodegas | `get_bodegas_siesa()` (`connekta_gateway`) | Maestro de bodegas configuradas en Siesa |
| API_v2_Ventas_Pedidos_Compromisos | `get_compromisos_pedido()` + una ruta de auditoría en `routes/siesa.py` | Cantidades comprometidas por línea de pedido |
| API_v2_Inventarios_Transferencia_Salida_Transito | `get_sts_info_by_alterno()` | Recovery: encontrar el STS ya creado tras un timeout (`f450_docto_alterno`) |
| API_v2_Inventarios_RequisicionesParaTransferir | `get_consec_rit_by_referencia()` | Recovery: encontrar la RIT ya creada tras un timeout (`f440_referencia`) |
| `238920` (dinámico) | `get_clasificacion_items()` | Clasificación ABC por ítem — reemplaza el CSV manual de rotación |
| `papeleriamedellin_WMS_Stock_Bodega_v2` | `inventario_siesa_service._descargar_una_pasada_custom()` | **Existencias multi-bodega** — la que llena `stock_siesa` |
| `papeleriamedellin_papeleriamedellin_API_custom_KardexWMS` | `kardex_service` (`KARDEX_CONSULTA_NOMBRE`) | Movimientos de kardex para demanda y costeo |
| `papeleriamedellin_compromisos_wms` | `get_compromisos_t405()` | Compromisos por pedido (variante dinámica, lee t405) |
| `papeleriamedellin_WMS_Remision_DesdePedido` | `get_remision_desde_pedido()` | Remisión asociada a un pedido. **Tres estados** (encontrada / `None` = barrido completo sin ella / `RemisionNoDisponible` = no se sabe), `tamPag=100` paginado (2026-09-25) |
| `papeleriamedellin_WMS_PuntoEnvio_FE` | `get_punto_envio_factura()` | Punto de envío para la FE (fallback de `SIESA_PUNTO_ENVIO_DEFAULT`) |
| `papeleriamedellin_API_custom_TercerosContacto` | `get_terceros_contacto()` | Contacto del tercero |
| `papeleriamedellin_monitos_facturas_wms` | `get_factura_desde_pedido()`, `get_monitor_facturas_raw()` | Factura del día por pedido — **es la guarda anti-duplicado de FE**, no solo un monitor |
| `papeleriamedellin_pame_descubrir_tablas` | `routes/factura_admin.py` | Descubrimiento de tablas — **solo diagnóstico** |

Dos más son **configurables sin default**, así que su nombre no está en el
repo: `CONNEKTA_CONSULTA_NC_CONSECUTIVO` (consecutivo real de la NC, ver
«Motivo DIAN») y el `api_abc` que `abc_service.sincronizar_clasificacion_desde_siesa`
recibe por parámetro.

> ⚠️ **La Regla 1 no se puede cumplir para la mayoría de estas lecturas.**
> De los 22 nombres de la tabla, **6 tienen contrato completo** en
> `docs/siesa-specs/`. El resto se codificó descubriendo campos contra la API
> viva. Ver «Specs DOCX del Consultor → Cobertura del lado LECTURA» antes de
> tocar cualquier conector de esta tabla.
| papeleriamedellin_WMS_Vendedor_Contacto | `get_vendedor_contacto()` | Nombre + teléfono real del asesor (JOIN T210×T200×T015), para mostrárselo al conductor en pago parcial |
| API_v2_Clientes | `cartera_service.FuenteSiesa.clientes()` | Maestro de clientes por NIT (cupo, gracia, bloqueos) para la retención de cartera. **Sin contrato** en `docs/siesa-specs/`: campos descubiertos en vivo (2026-09-24). Trae **las dos compañías** (`f201_id_cia`) |

---

## Variables de Entorno Críticas

### Autenticación Connekta

| Variable | Default | Descripción |
|----------|---------|-------------|
| `CONNEKTA_IKEY` | `''` | API key (ConniKey header). Vacío = modo simulación |
| `CONNEKTA_ITOKEN` | `''` | Token (ConniToken header). Estáticos — no expiran |
| `CONNEKTA_URL` | `https://serviciosqa.siesacloud.com` | QA o producción |
| `CONNEKTA_ID_COMPANIA` | `8215` | Tenant Connekta (NO es F_CIA) |
| `CONNEKTA_ID_SISTEMA` | `''` | Para conectores dinámicos v3.1 (244328, 142945) |
| `MODO_ENSAYO` | `''` | `'true'` = GETs reales, POSTs bloqueados |

### Identidad Empresa

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SIESA_ID_CIA` | `1` | F_CIA en payloads. **NUNCA 8215** |
| `CONNEKTA_BODEGA` | `NB1` | Bodega por defecto |
| `CONNEKTA_CENTRO_OP` | `003` | Centro de operación por defecto |
| `SIESA_CO_TRASLADO` | fallback a CENTRO_OP | CO para traslados inter-bodega |
| `SIESA_UNIDAD_NEGOCIO` | `''` | **OBLIGATORIO** — Siesa NO hereda de bodega en traslados |
| `SIESA_NIT_EMPRESA` | `''` | NIT empresa para f350_id_tercero |

### Tipos de Documento (por clase Siesa)

| Variable | Default | Clase | Conectores |
|----------|---------|-------|------------|
| `SIESA_TIPO_DOCTO_FACTURA` | `FE` (confirmado 2026-09-22: Railway QA y producción ya lo tenían puesto; código y `.env.qa` alineados el mismo día, era `FEW`) | FE | 142943 |
| `SIESA_TIPO_DOCTO_REMISION` | `''` | RM | 142945 |
| `SIESA_TIPO_DOCTO_NOTA_CREDITO` | `NCE` | NC | 142946 |
| `SIESA_TIPO_DOCTO_RECIBO_CAJA` | `RC` | 13 | 142888 |
| `SIESA_TIPO_DOCTO_DOCTO_CONTABLE` | `NI` (cambiado 2026-09-04, era `DC`) | 30 | 142882 |
| `SIESA_TIPO_DOCTO_ENTRADA_OC` | `''` | EO | 142948 |
| `SIESA_TIPO_DOCTO_AJUSTE` | `ADI` | 63 | 142951 (ajustes) |
| `SIESA_TIPO_DOCTO_TRASLADO` | `TRA` | 67 | 142951 (averías), 173066 |
| `SIESA_TIPO_DOCTO_RIT` | fallback TRASLADO | 75 | 174646, 174720 |
| `SIESA_TIPO_DOCTO_TRANSITO_SALIDA` | `''` | 65 (STS) | 173076, 174930 |
| `SIESA_TIPO_DOCTO_TRANSITO_ENTRADA` | `''` | 66 (ETS) | 173079 |

### Motivos y Conceptos

Motivos son códigos **obligatorios** en Siesa (Inventarios > Maestros > Conceptos y Motivos). Enviar un motivo inválido causa rechazo duro.

| Variable | Default | Concepto | Conectores | Naturaleza |
|----------|---------|----------|------------|------------|
| `SIESA_MOTIVO_TRASLADO` | `''` **OBLIGATORIO** | 607 | 173066, 173076, 174646 | Transferencia |
| `SIESA_MOTIVO_TRASLADO_ENTRADA` | `02` | 607 | 173079 | Entrada tránsito |
| `SIESA_MOTIVO_AVERIA` | fallback MOTIVO_TRASLADO | 607 | 142951 (averías) | Transferencia |
| `SIESA_ID_MOTIVO_VENTAS` | `''` | 501 | 142945, 142946 | Venta/devolución |
| `SIESA_ID_MOTIVO_COMPRAS` | `01` | 401 | 142948 | Entrada compras |
| `SIESA_MOTIVO_AJUSTE_ENTRADA` | `01` | 603 | 142951 (AJ-ENT) | Sobrante |
| `SIESA_MOTIVO_AJUSTE_SALIDA` | `02` | 603 | 142951 (AJ-SAL) | Faltante |

### Liquidación (RC + DC)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SIESA_COBRADOR` | `9876` | Cobrador dedicado para WMS (Maestros > Vendedores) |
| `SIESA_FLUJO_EFECTIVO` | `1103` | Flujo de efectivo (Tesorería > Flujos) |
| `SIESA_CXC_AUXILIAR` | `13050501` | Cuenta CxC fallback. **Preferir f253_id real de API 20** |
| `SIESA_MEDIO_PAGO_EFECTIVO` | `EFE` | Medio de pago efectivo |
| `SIESA_MEDIO_PAGO_TRANSFERENCIA` | `TBA` | Medio de pago transferencia bancaria |
| `SIESA_MEDIO_PAGO_TARJETA` | `TDC` | Medio de pago tarjeta |
| `SIESA_COND_PAGO_VENTAS` | `''` | **El código de CONTADO (C01).** No se emite — se configura para reconocerlo y NO emitirlo. Dos FE de contado quedaron en Elaboración (2026-08-13); **no es universal**: en QA ~24 FE C01 del WMS quedaron Aprobadas (ver «Contado contraentrega») |
| `COBRO_CONTRAENTREGA_MAX_DIAS` | `15` | Hasta cuántos días de crédito una FE se **cobra al entregar**; también el vencimiento de la FE de contado. Ver «Contado contraentrega vs crédito real» |
| `SIESA_COND_PAGO_DIAS` / `CONNEKTA_CONSULTA_COND_PAGO` | — | Días por condición (JSON) / consulta del maestro (sin default). Misma sección |
| `SIESA_COND_PAGO_RUTA` | `''` | **OBLIGATORIO** — la condición que lleva la FE de ruta si el pedido no trae ninguna. Crédito a un día (C02); el RC del conductor la salda |

### Transporte (173076/173079)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SIESA_VEHICULO_TRASLADO` | `''` | Código vehículo (Maestros > Vehículos) |
| `SIESA_NIT_TRANSPORTADOR` | `''` | NIT del transportador |
| `SIESA_SUCURSAL_TRANSPORTADOR` | `001` | Sucursal del transportador |
| `SIESA_NOMBRE_CONDUCTOR` | `''` | Nombre del conductor |
| `SIESA_BODEGA_TRANSITO` | `''` | Bodega tránsito (ej. TRA1) |
| `SIESA_UBICACION_ENTRADA_DEFAULT` | `None` | Ubicación ancla destino para 173079 (REC) |
| `SIESA_REQ_SOLICITANTE` | `''` | Solicitante en requisiciones (max 5 chars) |

### Otros

| Variable | Default | Descripción |
|----------|---------|-------------|
| `SIESA_LISTA_PRECIO` | `''` | Lista de precios para 142945. **Obligatorio en producción** |
| `SIESA_UOM_DEFAULT` | `UND` | Unidad de medida por defecto |
| `SIESA_PUNTO_ENVIO_DEFAULT` | `None` | Punto de envío fallback para 142943 |
| `SIESA_BODEGA_AVERIAS` | `AV1` | Bodega destino para transferencias de averías |
| `SIESA_CAUSAL_DEVOLUCION` | `01` | Causal DIAN para notas crédito (142946) |
| `SKIP_FE_CHECK` | `''` | `'true'` = skip anti-duplicado FE. Solo QA |
| `SIESA_VENTANA` | — (**sin restricción**) | `HH:MM-HH:MM` Bogotá: fuera de ella ni los crons que hablan con Siesa, ni la DLQ, ni el cierre de caja le hablan a Siesa. Sugerida en QA: `06:00-19:30` (lo medido allá). **Producción: sin la variable.** Ilegible = sin restricción y declarado en `/api/health/siesa` → `ventana_siesa`. Ver Regla 14 y «Tanda 2 del 2026-09-25 · H» |

---

## Mappings

### Bodegas y Centros de Operación (maestro real de Siesa)

Verificado contra el maestro de Siesa el 2026-08-10 (`docs/siesa-specs/`, export
de bodegas + `CO PAME`). Vive acá y no solo en el `.docx` porque un `.docx` no se
puede grepear, y esta es la tabla que hace falta cada vez que se toca un traslado.

| CO | Descripción Siesa | Bodega | Nombre de bodega | Notas |
|----|-------------------|--------|------------------|-------|
| 001 | NEIVA SUR | `NS1` | NEIVA SUR PRINCIPAL | |
| 001 | NEIVA SUR | `NS2` | NEIVA SUR FUNDACIÓN | **Bodega de PARQUEO de licitaciones**, no punto de venta. Se sacó el 2026-08-10 «tras verificar 0 usuarios asignados» y se devolvió el 2026-08-14: ese criterio responde «¿alguien trabaja ahí?» cuando la pregunta era «¿algo se mueve por ahí?». Tenía stock (6 SKU / 121 und) y es el destino del traslado que licitaciones ya hacía a mano |
| 002 | NEIVA CENTRO | `NC1` | NEIVA CENTRO | |
| 003 | NEIVA BODEGA CD | `NB1` | NEIVA BODEGA CD | CDI. Default de `CONNEKTA_BODEGA`/`CENTRO_OP` |
| 004 | PITALITO CENTRO | `PC1` | PITALITO CENTRO | |
| 005 | PITALITO TERMINAL | `PT1` | PITALITO TERMINAL | En proceso de volverse CDI como NB1 |
| 006 | FLORENCIA CENTRO | `FC1` | FLORENCIA CENTRO | |
| 007 | FERIA NEIVA | `FN1` | FERIA NEIVA | **Es Santa Lucía Plaza y opera todo el año** — el nombre en Siesa es viejo |
| 008 | FERIA PITALITO | `FP1` | FERIA PITALITO | Temporal, muy esporádica |
| 009 | FERIA FLORENCIA | `FF1` | FERIA FLORENCIA | Temporal, muy esporádica |
| 999 | ADMINISTRATIVO | — | — | Contable. **No lleva almacén en el WMS** |

**Bodegas de servicio** (no son puntos de venta, no llevan almacén):
`AV1` Averías CDI (`SIESA_BODEGA_AVERIAS`) · `TRA1` Bodega en Tránsito
(`SIESA_BODEGA_TRANSITO`) · `BC99` Bodega Contratación (no la usa el WMS).

⚠️ **«No lleva almacén en el WMS» ya no significa «el WMS no las ve».** Desde
el 2026-08-20, `AV1` y `TRA1` **se descargan de Siesa y se persisten en
`stock_siesa`** igual que un punto de venta (`_BODEGAS_SERVICIO` →
`_BODEGAS_INVENTARIO` en `inventario_siesa_service`). No descargarlas no las
hacía desaparecer: las volvía invisibles — cada avería que `siesa_job_service`
movía de NB1 a AV1 producía un `WMS_MAYOR` permanente en NB1 que nadie sabía
explicar. `BC99` sigue fuera: el WMS no la lee ni la escribe.

Dos consecuencias, y las dos hay que tenerlas presentes antes de decidir algo
sobre esas bodegas:

1. **`stock_siesa` ya no es «lo que el WMS opera»**: una consulta que la sume
   sin filtrar por bodega está sumando averías y mercancía en limbo. Ver abajo.
2. **Esta tabla es fuente normativa para la reconciliación.**
   `inventario_siesa_service._incomparable_esperado()` exime a `AV1`/`TRA1` del
   veredicto «sin diferencias» citando por escrito que *este documento* las
   declara bodegas de servicio. La exención pide **dos** condiciones —estar en
   `_BODEGAS_SERVICIO` **y** tener contraparte declarada en
   `_JUSTIFICACION_SIN_ALMACEN`—, justamente para que agregar una bodega a la
   lista de descarga no la exima de paso: eximir cuesta escribir por qué. Si
   alguien mueve una bodega de servicio a operada acá, tiene que mover también
   esas dos.

**Ignorar:** `FD1`, `ND1`, `PD1` — bodegas «DUPLICADA» en Siesa. El WMS no las
toca y no debe empezar a tocarlas.

### Bodega → CO: `app/services/bodegas.py`, y ningún otro sitio

**`co_de_bodega()` es la única función que contesta esta pregunta.** Lee
`almacenes.centro_op_siesa` (la autoridad) y cae al maestro certificado
`BODEGA_CO` cuando esa fila todavía no existe. Un mapa nuevo bodega→CO en
cualquier otro archivo **rompe el build** — se descubre por AST, no por texto.

#### Lo que costó tenerlo escrito tres veces (2026-08-14)

Había tres diccionarios llamados `_BODEGA_CO_MAP`, con **10, 9 y 8** entradas:

| Archivo | Entradas | Faltaban |
|---|---|---|
| `routes/tienda_oc.py` | 10 | — |
| `routes/traslados.py` | 9 | `FP1` |
| `services/traslado_service.py` | 8 | `FP1`, `NS2` |

La de 8 es la que usa `TrasladoService.confirmar_recepcion`, o sea **la vía
viva del ETS 173079**. `.get('NS2')` devolvía `None`, y
`transferencia_transito_entrada` hace `co_destino or self.centro_op`: el
documento salía con **CO 003** y `bodega_entrada` NS2. Siesa valida
`CO(bodega_entrada) == CO(doc)` (46089/46090) y lo rechaza — la mercancía se
queda en la bodega de tránsito, que es el limbo exacto que los invariantes de
traslado existen para detectar. Y nadie reclama: una tienda que no recibió un
traslado que no pidió, no llama.

Peor: dentro del **mismo payload**, el CO base se resolvía con
`connekta._co_de_bodega()` (que sí lee `almacenes`) y el CO de entrada con el
diccionario literal. Una pregunta, dos políticas, un documento.

**El trinquete estaba en verde con las tres divergiendo**, porque leía solo la
copia de `tienda_oc.py` — y su docstring afirmaba que era «el único sitio del
código con las 10». Era el único *completo*, no el único que *existía*.

> La lección no es que faltaba mirar dos archivos más. Es que **el guard medía
> una copia cuando la propiedad era «todas coinciden»**: mientras el detector
> lleve escrita a mano la lista de sitios que revisa, el sitio nuevo no entra.
> Es la misma forma que ya costó en `test_impresion`, en el Nivel 4 de rutas
> huérfanas y en la fórmula de retención.

La causa de fondo es anterior:
`migrations/versions/c1d2e3f4g5h6_set_centro_op_siesa_almacenes.py` quedó como
no-op —*«centro_op_siesa ya manejado por dict en código»*—. Ese es el día en
que el maestro dejó de ser el maestro sin que nadie lo declarara.

#### Las tres listas de bodegas — cuál decide qué (corregido 2026-08-20)

Viven todas en `inventario_siesa_service` y **no son intercambiables**. El
nombre de la primera miente un poco y por eso hay que leer las tres juntas:

| Constante | Qué significa de verdad | Qué decide |
|---|---|---|
| `_BODEGAS_PV` | «bodegas que el WMS **opera**» — incluye `NS2`, que es parqueo de licitaciones y no punto de venta | El universo operado. `armador_service.rop_dual` filtra `stock_siesa` por ella |
| `_BODEGAS_SERVICIO` | `AV1` + `TRA1`. Fuera de `_BODEGAS_PV` **a propósito**: meterlas ahí las volvería destino válido de un traslado y opción de un desplegable de sede | Nada por sí sola |
| `_BODEGAS_INVENTARIO` | `_BODEGAS_PV + _BODEGAS_SERVICIO` | **Qué se le pide a Siesa y qué entra a `stock_siesa`** |

**Estas listas dejaron de ser cosméticas.** Hasta el 2026-08-20 este documento
decía de ellas *«no mueven inventario ni deciden un CO: un desacuerdo ahí
muestra un código crudo donde debería ir un nombre»*, y recomendaba no
unificarlas por ser refactor cosmético. Eso era cierto cuando la única lista
era de nombres para pintar. Hoy no:

- `_BODEGAS_INVENTARIO` es la **lista blanca de ingreso a `stock_siesa`**: lo
  que no está ahí, `_descargar_una_pasada_custom` lo descarta y nunca se
  persiste.
- `stock_siesa` alimenta al **Armador** (`rop_dual`), que calcula
  `posicion = stock + en_transito − comprometido − salida_sin_conf` y de ahí el
  `deficit` que **dimensiona el contenedor**. Regla 0: irreversible 120 días.

El defecto que esto ya causó: al agregar `AV1`/`TRA1` al universo descargado,
sus filas entraron a `stock_siesa` y la consulta del Armador —que sumaba
*todas* las bodegas de la tabla, y no cambió— empezó a contar averías y
mercancía en tránsito como existencia comprable. Medido sobre un SKU real:
`{'NB1': 100, 'AV1': 40, 'TRA1': 25}` → el Armador veía **165** donde lo
vendible era **100**. Nadie tocó esa línea; **cambió lo que había en la tabla**.
Y era la única de las tres respuestas a «¿esto se puede vender?» que las
incluía: `dashboard_service` ya declaraba AVERIAS zona no vendible y
`picking_service` ya la excluía del FEFO — el único de los tres que **compra**
era el que las sumaba.

El arreglo es **lista blanca, no negra**: `.filter(StockSiesa.bodega.in_(
_BODEGAS_PV))`. Un `notin_(['AV1','TRA1'])` dejaría pasar `BC99`, las
«DUPLICADA» `FD1`/`ND1`/`PD1` y —sobre todo— la bodega de servicio que alguien
agregue mañana, que es exactamente el defecto que se está arreglando. Con lista
blanca, una bodega nueva es invisible hasta que se declare operada.

> Antes de agregar una bodega a `_BODEGAS_INVENTARIO`, preguntá quién suma
> `stock_siesa`. Hoy: `armador_service.rop_dual` (filtra por `_BODEGAS_PV`),
> `kardex_service` (ancla de saldo, por bodega), `vigia_service` (frescura),
> `inventario_siesa_service._leer_stock_de_bd` (fallback por bodega). La tabla
> es **acumulativa — upsert sin borrado**: una fila persistida una vez se sigue
> devolviendo aunque la API deje de reportarla.

~~**Hueco conocido:** `ItemEnTransito` no tiene ningún escritor en `app/`, así
que el `en_transito` de `posicion` es siempre 0.~~ **Cerrado el 2026-09-24**
(m046compras): el término sale de `compras_fuentes.en_camino` (OCs abiertas de
Siesa + contenedores cargados). Ver «Compras: las fuentes de datos».

#### Lo que sigue repartido (y por qué se deja)

Fuera de `inventario_siesa_service` queda `traslado_service._BODEGAS_PREWARM`:
una cuarta lista, con las 10 operadas, que solo decide a cuáles se les calienta
el caché de stock. No filtra ni persiste nada — quedarse corta cuesta una
consulta lenta, no un número equivocado. Igual el trinquete la cruza contra
`_BODEGAS_PV`, porque una bodega operada que no se pre-calienta es una tienda
que espera.

Los mapas de **nombres** del JS sí siguen siendo cosméticos y siguen en varios
sitios: `traslados.js`, `tienda.js`, `app.js` ×3 (uno inline en un `onchange`).
Un desacuerdo ahí muestra un código crudo donde debería ir un nombre; no mueve
inventario ni decide un CO. Unificarlos antes de un corte es «validar contra
producción real» al revés.

Trinquete: `tests/test_bodegas_coherentes.py` (18 tests) cruza
`_BODEGAS_PREWARM` y `_BODEGAS_PV` entre sí y contra el maestro;
`tests/test_reconciliacion_por_bodega.py` exige que `AV1`/`TRA1` estén en
`_BODEGAS_INVENTARIO` y **no** en `_BODEGAS_PV`;
`tests/test_armador_bodegas_servicio.py` exige que el Armador sume solo las
operadas. El detector de copias está probado por mutación — reintroducir un
mapa lo pone rojo.

### CO → Caja

```
001 → 001 (Neiva Sur)
002 → 004 (Neiva Centro)
003 → 999 (Bodega CD)
004 → 999 (Pitalito Centro)
005 → 999 (Pitalito Terminal)
006 → 013 (Florencia Centro)
007-009 → 999 (Ferias)
```

Override: `SIESA_CO_CAJA_MAP` (JSON string). Caja 999 = CAJA GENERAL, existe en todos los COs.

### forma_pago → Medio Siesa

```
EFECTIVO      → EFE
TRANSFERENCIA → TBA
TARJETA       → TDC
CONSIGNACION  → TBA
```

Para transferencias/consignaciones, campos adicionales requeridos en Caja:
- `F358_REFERENCIA_OTROS` (número comprobante)
- `F358_FECHA_CONSIGNACION` (YYYYMMDD)
- `f358_docto_banco_cg` = `'CG'`
- `F358_ID_BANCO` = `''` (vacío — la cuenta bancaria va en maestro de medios de pago, no en payload)

### Retenciones PUC

Cuentas del grupo **1355** (activos: retenciones a favor del vendedor). NO 2365 (pasivos).

| Motivo | Cuenta PUC | Tasa | Base de cálculo |
|--------|-----------|------|-----------------|
| RETEFUENTE_2.5 | 13551501 | 2.5% | Subtotal (f461_vlr_bruto) |
| RETEFUENTE_1.5 | 13551502 | 1.5% | Subtotal |
| RETEIVA | 13551701 | 15% | IVA (f461_vlr_imp) |
| ICA_3 | 13551801 | 0.3% | Subtotal |
| ICA_4.14 | 13551802 | 0.414% | Subtotal |
| ICA_6.9 | 13551803 | 0.69% | Subtotal |
| ICA_8 | 13551804 | 0.8% | Subtotal |
| ICA_11.04 | 13551805 | 1.104% | Subtotal |

Para IVA/subtotal: usar API 45 (`f461_vlr_bruto`, `f461_vlr_imp`), NO dividir por 1.19.

### Cuentas Bancarias por Medio de Pago

Configurar en Siesa: Maestros asociados > Medios de pago > "Cnta. bancaria"

| Medio | Cuenta bancaria |
|-------|----------------|
| TBA | 011 (Bancolombia Ahorro) |
| TBC | 002 (Bancolombia Corriente) |
| TBB | 005 (BBVA Corriente) |
| TBG | 004 (Bogotá Corriente) |
| TAA | 008 (Agrario Ahorro) |
| TAC | 001 (Agrario Corriente) |
| TDV | 003 (Davivienda Corriente) |

---

## Cómo se cierra un defecto acá (2026-09-14)

**Arreglar el caso no cierra el defecto. Cierra la instancia.** La regla de este
repo es que un error no puede volver dos veces, y eso exige tres cosas, en este
orden:

1. **Nombrar la CLASE, no el caso.** «La avería de picking restaba sin destino»
   es el caso. La clase es *«se resta inventario sin declarar a dónde fue»*, y
   en `app/` había **diez** sitios que restan. El arreglo es del caso; el
   trinquete es de la clase.

2. **Un trinquete con inventario declarado.** No una heurística que adivine —
   un guard con falsos positivos se termina desactivando, y eso es peor que no
   tenerlo. La forma que funciona en este repo: una lista de sitios conocidos
   con su motivo escrito, y tres tests — que la lista **no crezca** sin
   decisión, que **solo encoja**, y que cada entrada **diga por qué**.
   Ver `tests/test_inventario_no_se_resta_sin_destino.py`.

3. **Meta-tests que prueben que el guard muerde.** Un escáner que se
   desincroniza devuelve cero, y **un cero se lee igual que «acá no hay nada que
   hacer»**. Todo trinquete nuevo lleva:
   - la forma que debe detectar (y las dos escrituras de la misma operación:
     `x -= n` y `x = max(0, x - n)` son la misma y una regex ve solo una);
   - la que **NO** debe marcar — un detector que marca todo prueba la mitad;
   - un **piso mínimo** (`assert total >= N`) que se pone rojo si el escáner se
     rompe, en vez de reportar cero tranquilamente.

**Y la mutación es obligatoria, con su propia verificación.** Se rompe la
propiedad, se comprueba que el test se pone rojo, se restaura. Antes de correr,
**verificar que la mutación quitó de verdad lo que se cree**: el 2026-09-14 una
mutación no puso rojo nada porque cortó media cadena y la palabra que el test
buscaba vivía en la otra línea. El verde era de la mutación, no del código.

> **Contar cambios no es medir cobertura.** Ese mismo día, un barrido reportó
> «194 interpolaciones envueltas» mientras el ataque real pasaba entero. El
> número era cierto y no medía la propiedad. Después de barrer, **ejecutar la
> carga real y mirar el resultado**, no el diff.

## Reglas Inquebrantables

0. **ANTE DATO AUSENTE, FALLAR HACIA EL LADO CONSERVADOR Y DECLARARLO.**

   El motivo NO es que el faltante cueste menos que el sobrante — para la
   canasta constitucional el agotado es carísimo, Florencia lo probó. El motivo
   es la **reversibilidad**: un sub-pedido declarado es una decisión que un
   humano corrige mañana; un contenedor embarcado es irreversible 120 días y ya
   se llevó la caja.

   Queda escrito así para que nadie "corrija" este sesgo en el futuro por
   parecerle timorato. Implementación: `kardex_service.dias_expuestos()`.

   **Corolario — una política, una función.** El mismo concepto implementado
   dos veces divergió en tres horas: la clasificación S-B caía a días
   calendario (conservador) y la descensura a días-con-venta (25× de
   sobreestimación en SKUs grumosos, multiplicada después por el colchón). Si
   un fallback se parchea en dos sitios, la tercera implementación divergirá y
   esa vez nadie estará comparando.

1. **LEER EL DOCX DEL CONECTOR ANTES DE CODIFICAR** — Costo de no hacerlo: 5+ rondas de prueba-error
2. **F_CIA = 1, NUNCA 8215** — 8215 es el tenant Connekta, no la compañía Siesa
3. **POST NUNCA reintenta en 5xx/timeout** — solo en 429. Un timeout no significa que falló (incidente RC-00002744)
4. **DecimalConSigno = 21 chars exactos** — `+000000000000000.0000` → `f'{signo}{abs(v):020.4f}'`
5. **Fechas = YYYYMMDD sin separadores** — timezone Bogotá (UTC-5), no UTC. Después de 7PM Colombia, UTC es el día siguiente.

   **Una sola implementación: `app/utils/fecha.py`.** Esta regla ya existía, ya
   tenía un helper y ya tenía cuatro tests en verde — y se aplicaba en 4 de 16
   sitios. Los tests verificaban EL HELPER en aislamiento, nunca que alguien lo
   llamara. Auditado el 2026-08-04: 16 fechas en el gateway + 4 fuera + 13
   códigos, todas en UTC.

   Lo que costaba, y no era solo `f350_fecha`:
   · vencimientos de cartera a 30 días contados desde el día equivocado;
   · `SIESA-INI-{prod}-{fecha}` como clave de idempotencia — cambiaba a las
     7 p.m., y dos cargas de stock separadas por ese minuto entraban las dos;
   · el KPI "completado hoy" reiniciándose a las 7 p.m., en mitad del turno;
   · el cupo diario de conteo del operario, reiniciado a la misma hora.

   Los timestamps técnicos (`created_at`) SIGUEN en UTC y eso es correcto. Lo
   que no puede salir de UTC es una fecha que alguien LEE como día.
   Trinquete: `tests/test_siesa_fecha_bogota.py`.
6. **Pre-flag antes de POST** — `siesa_*_triggered = True` ANTES del POST, revert si falla
7. **Secuencialidad DLQ: NC → RC → DC** — cada uno espera al anterior
8. **`f470_desc_varible` es TYPO INTENCIONAL** — nombre exacto del spec para 142951, 173066, 173076, 173079. Sin este campo (2000 chars), el registro plano se desalinea
9. **`f470_desc_variable` (correcto) para 142945 y 142946** — estos conectores SÍ usan el nombre correcto
10. **tamPag máximo = 100** — ≥500 causa registros fantasma con todos los campos NULL
11. **F353_ID_AUXILIAR_DOCTO_CRUCE = f253_id real** — NUNCA hardcodear. Propagar desde API 20
12. **F358_ID_BANCO vacío para transferencias** — la cuenta bancaria va en maestro de medios de pago en Siesa
13. **F350_ID_CLASE_DOCTO = 30 en 142882** — NI requiere clase 30 (documento contable genérico)
14. **Siesa QA no operó después de ~8 PM Colombia** — TCP timeout de 30 s.
    **Es un hecho medido en Siesa QA, no probado en producción** (reescrita el
    2026-09-25, decisión del dueño). En temporada la operación trabaja hasta
    las 12 a. m. y la facturación no puede quedar bloqueada por reloj: la
    ventana es **`SIESA_VENTANA`** (`HH:MM-HH:MM`, Bogotá; sugerida en QA
    `06:00-19:30`, **en producción sin la variable = sin restricción**). Lo que
    protege cuando Siesa no responde es el circuit breaker, el precheck del
    cierre de caja y la jerarquía «no sé ≠ no» — no la hora. Ver «Tanda 2 del
    2026-09-25 · H».
15. **Strings en filtros SQL con doble comilla simple** — `''texto''` no `'texto'`
16. **Consultas dinámicas usan clave "Datos", no "Table"** — endpoint y respuesta diferentes
17. **F353_PREFIJO_CRUCE NO existe en 142888** — el prefijo va DENTRO de F353_ID_TIPO_DOCTO_CRUCE
18. **PK documental = CO + tipo_docto + consecutivo + cuota** — omitir cualquiera mezcla documentos
19. **ConniKey y ConniToken son estáticos** — no expiran, no hay refresh flow
20. **Después de POST exitoso, Siesa tarda ~10-12s en procesar** — no consultar inmediatamente
21. **Siesa no aprueba un documento cuya cartera no cuadre con sus CxC.**

    Se descubrió en la nota crédito y se creyó una rareza suya. El 2026-08-13
    apareció **el mismo mensaje** en una factura de venta de contado (dos
    intentos, $263.963 y $14.200, las dos en Elaboración) — ver «La factura de
    ruta no puede ser de contado». No es del 142946: es el invariante de
    aprobación. Cualquier documento que se mande con `F350_IND_ESTADO=1` y no
    traiga su cruce resuelto queda trabado.

    **142946 (NotaFactura) SIEMPRE con `F350_IND_ESTADO=0` (Elaboración), NUNCA 1 (Aprobado)** —
    verificado en vivo contra Siesa QA (2026-07-29): con estado=1 Siesa rechaza el documento
    completo con `"El valor de la cartera debe ser igual al valor de las CxC"`. El conector
    estándar `API_v1_Ventas_Comercial_NotaFactura` (y su clon 250696) no tiene sección
    `CuotasCxC` para declarar el cruce — ni el Asistente de Personalizar Estructura permite
    agregarla. Con estado=0 el POST es aceptado sin error; el cruce contra la factura queda
    pendiente de **aprobación manual en el escritorio de Siesa** — decisión de diseño para
    Devolución de Cliente, no un workaround temporal. Confirmado también que una NC en
    Elaboración ya consume "cantidad pendiente por devolver" de la factura (bloquea intentos
    duplicados contra la misma línea, incluso sin aprobar).

---

## NCE — qué conector usar (y cuáles NO), leer antes de tocar este flujo

La saga completa vive en las secciones de abajo (250878 → 251192 → 251126 →
251546 → unificación 2026-08-13), pero mezclan intentos fallidos con lo que sí
quedó en producción — fácil confundirse sobre cuál tocar. Tabla resumen,
verificada contra Railway y contra Connekta (conectores registrados) el 2026-08-13:

| Conector | Rol | Estado | Usar cuando... |
|---|---|---|---|
| **251126** | Crea la NC **y cruza cartera** en un solo POST | ✅ **EN USO** — `trigger_nota_factura_crear_cruzar()`, jobs `NOTA_CREDITO_DEVOLUCION_CLIENTE` **y** `NOTA_CREDITO_FACTURA` (unificados el 2026-08-13, commit `07cb5df` — un solo conector para las dos NC, Regla 0) | Siempre que se dispare una NC real, venga de Devolución de Cliente o de Liquidación de ruta |
| **251546** | Segundo POST: fija el **motivo DIAN** sobre la NC ya creada (necesita el consecutivo real, no puede ir en el mismo POST que 251126) | ✅ **EN USO**, encendido desde 2026-08-06 (`CONNEKTA_CONSULTA_NC_CONSECUTIVO` registrada) — job `MOTIVO_DIAN_NC` | Se encadena solo, no se llama a mano |
| **250696** (clon de 142946) | Crea la NC simple, **sin** cruce automático de cartera | ❌ **CÓDIGO MUERTO** — `trigger_nota_factura()` no tiene ningún caller desde `07cb5df` (2026-08-13). Y **no está registrado en Connekta** (confirmado el mismo día) — si algo llegara a llamarlo, fallaría duro, no solo "sin cruzar" | No reconectarlo a ningún job nuevo. Si hace falta un tercer flujo de NC, apuntarlo a 251126 |
| **250878** | Intento de crear+cruzar+motivo+**aprobar**, todo en un solo POST | ❌ **ABANDONADO** — bloqueo estructural irresoluble (753 siempre se procesa después de 461, el registro que aprueba). No reintentar con variaciones de payload, ya se agotaron | Nunca — dejar como referencia histórica de qué NO intentar |
| **251192** | Intento de referenciar una NC existente + motivo + **aprobar**, sin crear nada | ❌ **ABANDONADO** — mismo bloqueo que 250878, confirmado también en POST separado de la creación | Nunca |
| — | **Aprobar** la NC en Siesa | Sin conector — el único paso que sigue 100% manual, sin solución de API | Escritorio de Siesa, ver "Procedimiento Manual" abajo |

---

## Procedimiento Manual — Aprobar NC de Devolución de Cliente en Siesa

Consecuencia operativa de la Regla #21: el WMS crea la Nota Crédito (142946)
en **Elaboración**, nunca Aprobada. Alguien en contabilidad debe completar el
cruce y la aprobación a mano en el escritorio de Siesa. Verificado en vivo
contra Siesa QA (2026-07-29) con NCE-00000050 / factura FEW-1463 (Samboni
Benavides Aldivar), de punta a punta:

> **Nota (2026-07-31):** el paso 2 (cruzar cartera) ya tiene solución de API
> verificada — ver "Cruce de cartera SÍ se pudo automatizar — conector
> 251126" más abajo.
> **Nota (2026-08-03):** el paso 3 (motivo DIAN) también tiene solución de
> API verificada — ver "Motivo DIAN SÍ se pudo automatizar — conector 251546
> + segundo POST" más abajo. De los 3 pasos manuales originales (cruzar,
> motivo, aprobar) solo **Aprobar** queda sin solución de API. Todo pendiente
> de integrar a código; hasta entonces este procedimiento completo sigue
> siendo el vigente en producción.

1. **Ubicar el documento**: Financiero → Auditoría de documentos → filtrar
   por tercero/fecha → doble clic sobre la fila `NCE-0000xxxx` (Estado: En
   elaboración). El menú "Nota crédito desde factura → Desde factura..." crea
   un documento **nuevo vacío** — no sirve para esto, abre siempre el ya
   existente.
2. **Cruzar la cartera**: tab **CxC → Facturas** → botón **Automático**
   (abajo a la derecha). Esto llena `Aplicar PCGA`/`Aplicar NIIF` con el saldo
   completo de la factura y deja `Nuevo saldo` en $0. Guardar (Ctrl+S /
   ícono disquete).
3. **Motivo DIAN**: tab **Entidades** → `Grupo Entidad: FE_CONCEPTOS NC 2.1`
   → sub-tab "Conceptos NC - FE 2.1" → `Concepto notas crédito` = **1 —
   Devolución parcial de los bienes** (código genérico de devolución, aunque
   diga "parcial"). Sin este campo el botón Aprobar no se habilita.
4. **Aprobar**: ícono "Aprobar" en la barra de herramientas superior (después
   de las flechas ← →, tooltip "Aprobar"). Aparece un popup: *"Esta es una
   factura desde remisión con base en pedido. ¿Desea reversar el pedido
   relacionado con esta factura?"* → responder **No**, salvo que exista una
   razón de negocio explícita para reabrir ese pedido (reabrirlo puede
   desincronizar el estado `DESPACHADO` que el WMS ya tiene registrado).
5. **Verificar cierre**: tab CxC muestra `Pendiente PCGA`/`Pendiente NIIF` en
   $0 y `Estado: Aprobado`. Este mismo paso reingresa el inventario devuelto
   a la bodega de Siesa (el tab Items trae la bodega por línea) — en paralelo
   al inventario del WMS, que ya se actualizó al confirmar la devolución
   física.

Aprobar sin haber cruzado primero (paso 2) deja el botón Aprobar
deshabilitado — el cruce de cartera es un prerrequisito de la aprobación, no
un paso posterior opcional.

---

## Por qué la aprobación de NC NO se pudo automatizar (2026-07-30)

Investigación exhaustiva (~4h, Siesa QA en vivo) para eliminar el paso manual
de la sección anterior. Se construyó un conector especial vía Asistente
UnoEE (Generic Transfer → 4_Especial → `07_Nota_Credito_Entidades_Aprobacion`),
registrado como **250878** (`PapeleriaMedellin_NotaCredito_Aprobacion_Completa_WMS`),
con secciones Docto. ventas comercial + Cuotas CxC + Documentos + Movimientos
+ Entidades dinámicas. **Conclusión: no es posible con las herramientas
actuales de Siesa — es una limitación estructural del motor de importación,
no un problema de configuración.**

### Cadena de hallazgos (todos verificados en vivo, no teóricos)

1. `F350_ID_CLASE_DOCTO` para `NCE` debe ser **525** ("Nota crédito directa"),
   no 521 ni 526 — Siesa valida contra una lista corta (520/521/522/525/542)
   en la sección "Docto. ventas comercial", distinta de la lista larga que
   aparece en "Entidades dinámicas" (que sí incluye 526 pero no aplica aquí).
   Debe coincidir en **ambas** secciones.
2. `f461_id_tercero_vendedor` es obligatorio en este conector (a diferencia
   de 142946/250696) — usar `"Generico"` (el valor real que trae
   `f200_id_vendedor` en la factura), no un código numérico.
3. **250878 no crea una NC nueva — completa una que ya existe en
   Elaboración** (creada previamente vía 250696). La sección "Documentos"
   (T461 subtipo 04) exige referenciar un documento existente en estado
   Elaboración; si no existe o ya está Aprobado, rechaza con "el documento
   no existe" / "debe estar en elaboración".
4. `F353_CONSEC_DOCTO_CRUCE` (Cuotas CxC) sí está disponible como campo
   variable pese a que la guía DOCX descargada no lo mostraba — la guía
   puede quedar desactualizada tras ediciones en el Asistente; verificar en
   vivo, no confiar en el DOCX si hay dudas.
5. `f753_id_grupo_entidad` real es **`FE_CONCEPTOS NC 2.1`** (guion bajo
   entre "FE" y "CONCEPTOS") — el valor puesto a mano en el Asistente tenía
   un espacio en su lugar. Confirmado contra `t744_mm_grupo_entidad` y contra
   el propio mensaje de error de Siesa, que sí lo citaba con guion bajo.
   Entidad real: `EUNOECO015` (`t742_mm_entidad`, etiqueta "Conceptos NC -
   FE 2.1"). Atributo real: `co015_concepto_nc` (`t743_mm_entidad_atributo`).
6. **Bloqueo final, irresoluble**: `F350_IND_ESTADO=1` (aprobar) exige
   `F_CONSEC_AUTO_REG=1` ("automático", crea documento nuevo) — Siesa
   rechaza con "el indicador de consecutivo automático del plano debe ser
   automático" si se manda `auto_reg=0` para apuntar a un documento ya
   existente. Pero **Entidades dinámicas es tipo de registro 753, que
   siempre se procesa después de 461** (el registro que aprueba) dentro del
   mismo plano — así que un documento recién creado (`auto_reg=1`) nunca ve
   su propia sección de Entidades a tiempo para satisfacer la validación
   "el tipo de documento maneja entidades dinámicas obligatorias" que corre
   sobre 461. Verificado con `f753_dato_numerico` formateado correctamente,
   códigos reales de entidad/atributo, y grupo entidad corregido — el
   resultado no cambia.

   Es decir: **crear+cruzar+motivo+aprobar en un solo POST es imposible**
   (motivo llega tarde), y **completar una NC existente con motivo ya
   puesto a mano tampoco** (aprobar exige `auto_reg=1`, que fuerza crear
   una NC nueva vacía en vez de tomar la existente).

### Por qué no seguir intentando

No es una combinación de campos sin probar — se agotaron las combinaciones
relevantes de `F_CONSEC_AUTO_REG` × `F350_IND_ESTADO` × orden de secciones
en el JSON, con y sin motivo pre-existente. El bloqueo es el orden interno
de procesamiento por tipo de registro (`753 > 461`), algo que no se controla
desde el payload. Cualquier intento futuro de automatizar esto requiere que
**Siesa** exponga un mecanismo distinto (ver ticket de soporte).

### Vía de escape NO explorada (mayor riesgo/costo, ver conversación 2026-07-29)

RPA de navegador contra el cliente web (SiesaEE Cloud corre en HTML5,
técnicamente scripteable con Playwright/Selenium) — descartado por ahora:
requiere guardar credenciales de Siesa en el backend, correr navegador
headless en el worker, y se rompe con cualquier cambio de UI. Reservar solo
si soporte Siesa confirma que no habrá solución de API.

---

## Cruce de cartera SÍ se pudo automatizar — conector 251126 (2026-07-31)

A diferencia de 250878 (bloqueo estructural irresoluble, ver arriba), **sí
es posible crear la NC Y cruzar la cartera en un solo POST** usando un
conector distinto, construido sobre un plano base diferente. Reduce el
procedimiento manual de 3 pasos a 2 — motivo DIAN y aprobar siguen siendo
manuales, cruzar cartera ya no.

### Por qué este conector sí funciona y 250878 no

250878 se construyó sobre `07_Nota_Credito_Entidades_Aprobacion`, que usa
**Docto. ventas comercial v9** — esa versión trae incorporada la validación
"entidades dinámicas obligatorias" (registro 753) que siempre se procesa
después del registro que aprueba (461), bloqueo irresoluble.

**251126** (`PapeleriaMedellin_NotaCredito_CrearCruzar_WMS_v2`) se construyó
vía Generic Transfer → Personalizar Estructura sobre el plano
`Tecnocedi_Nota_credito_Desde_Factura_WMS`, que usa **Docto. ventas
comercial v3** — la misma versión de header que ya usa 250696 en
producción, y que **no exige entidades dinámicas**. Se le agregó la sección
`Cuotas CxC (v1)` (ausente en 250696) para poder declarar el cruce en el
mismo POST. Secciones finales: Inicial + Docto. ventas comercial (v3) +
Cuotas CxC (v1) + Movimientos (v12 Sub2) + Final — sin Documentos ni
Entidades dinámicas (no existen en este plano).

### El bug que casi lo descarta por error: `F353_VLR_CRUCE`

Primer intento (2026-07-30, factura FEW-1465): el POST devolvió
`codigo:0` pero el cruce no aplicó — la NC quedó creada con **Debito/Credito
PCGA en $0** y T353 sin cambios. Se investigó ~1h (incluyendo revisar campo
por campo la definición completa de la sección Cuotas CxC contra la spec de
Siesa, sin encontrar campos faltantes) antes de encontrar la causa real:

`F353_VLR_CRUCE` se estaba llenando con la **suma de `f470_vlr_bruto`**
(subtotal sin IVA, ej. $61,471) en vez del **saldo real de la factura**
(con IVA — lo que Siesa muestra como "Saldo PCGA" en el tab CxC→Facturas y
lo que trae `f353_total_db` en `API_v2_CxC_General`, ej. $73,150). Ambos
valores pueden coincidir por casualidad en pruebas con los mismos SKUs, lo
que ocultó el bug la primera vez.

**Regla:** `F353_VLR_CRUCE` = suma de `f470_vlr_neto` de las líneas de la
factura (`get_rowids_factura`), o el `f353_total_db` real vía
`API_v2_CxC_General` — nunca `f470_vlr_bruto`.

Nota sobre `$0/$0` en Auditoría de documentos: **no es señal de fallo por sí
sola** — toda NC recién creada en Elaboración muestra Debito/Credito PCGA en
$0 hasta que el cruce se aprueba (el valor de items si se refleja de
inmediato en el tab Items — eso sí hay que verificar ahí, no en la columna
de Auditoría).

### Verificado en vivo de punta a punta (2026-07-31)

Factura FEW-00001466 (pedido PD1352, cliente GOMEZ CHICO SERGIO, NIT
1000134388, CO 003). Líneas: PAPELSP9218 x5 (rowid 2857568, neto $72,750),
PAPELSP9830 x4 (rowid 2857569, neto $400). Total neto = **$73,150**.

1. POST a 251126 con `F_CONSEC_AUTO_REG=1`, `F350_IND_ESTADO=0` (crea, no
   aprueba), `F353_VLR_CRUCE=73150.0000` → `codigo:0`.
2. NCE-00000056 creada: tab **Items** con las 2 líneas y valor neto exacto
   $73,150 (Siesa deriva precio/IVA automáticamente desde `f470_rowid_movto`
   — no hace falta mandar precio/valor explícito, igual que 142946).
3. Tab **CxC → Facturas**: FEW-00001466-0 con **Aplicar PCGA = $73,150,
   Nuevo saldo = $0** — el cruce quedó aplicado/staged sin tocar el botón
   Automático.
4. Manual: tab Entidades → `FE_CONCEPTOS NC 2.1` → concepto `1` → Aprobar.
   Sin error, sin pedir cruzar de nuevo.
5. Confirmado contra el ledger real (`API_v2_CxC_General`, no solo la vista
   del documento): `f353_total_cr = 73150.0 = f353_total_db`,
   `f353_fecha_cancelacion = 2026-07-31`. La factura quedó saldada de verdad,
   no solo en apariencia.

### Estado: integrado a `connekta_gateway.py` (2026-07-31)

`trigger_nota_factura_crear_cruzar()` reemplaza a `trigger_nota_factura()`
en el job `NOTA_CREDITO_DEVOLUCION_CLIENTE` (`siesa_job_service.py`) — el
submódulo de Devolución de Cliente ahora crea Y cruza la cartera en un solo
POST. `trigger_nota_factura()` (250696) sigue existiendo intacta y la sigue
usando `NOTA_CREDITO_FACTURA` (Liquidación de ruta) — no se tocó ese flujo.

**Prorrateo de valor (nuevo, no existía antes):** `get_rowids_factura()` da
el `f470_vlr_neto` de la línea **completa facturada**, pero Devolución de
Cliente permite devolver menos de lo facturado. `_construir_lineas_nc()`
(rama `es_total=False`, la única que usa Devolución) ahora calcula
`f470_vlr_neto_prorrateado = vlr_neto_linea × cant_devuelta / cant_facturada`
por línea con `Decimal` (no float, para no arrastrar error de redondeo al
sumar varias líneas). El job suma esos prorrateos para `valor_cruce`. La
rama `es_total=True` (Liquidación) no se tocó — no prorratea, sigue igual.

**Helpers compartidos (Regla 0 — una política, una función):**
`_build_transportador_vacio()` y `_build_header_docto_ventas_nc()` en
`connekta_gateway.py` — el bloque `f462_*` y el header base de
`Docto ventas comercial` son idénticos entre 250696 y 251126; extraídos
para que un fix futuro no se aplique en un solo conector y diverja en el
otro (exactamente el patrón que ya costó 3h una vez, ver Regla 0 arriba).

**Vencimiento real:** `get_vencimiento_factura()` consulta
`API_v2_CxC_General` por `f353_fecha_vcto` real de la factura cruzada, con
fallback a hoy+30 días si no se encuentra (no bloqueante — verificado en
vivo que Siesa acepta el cruce aunque el vencimiento no sea exacto).

**Variable de entorno nueva:** `CONNEKTA_CONECTOR_NOTA_CREDITO_CRUZAR`
(default `251126`) / `CONNEKTA_NOMBRE_CONECTOR_NOTA_CREDITO_CRUZAR`.
`SIESA_TIPO_DOCTO_NOTA_CREDITO` (NCE) se reutiliza sin cambios.

**Pendiente de probar en vivo antes de confiar ciegamente:** el prorrateo
solo se verificó con test unitario (mock), no contra Siesa QA real con una
devolución parcial genuina (todas las pruebas en vivo de hoy fueron
devoluciones de línea completa). Probar un caso de devolución parcial real
antes de dar esto por cerrado.

Actualiza automáticamente el paso 2 del "Procedimiento Manual" de arriba:
cruzar cartera ya no es manual para Devolución de Cliente. Motivo DIAN (paso 3)
también dejó de ser manual — ver siguiente sección. Aprobar (paso 4) sigue sin
solución de API.
- Correr contra 2-3 casos más (facturas con descuentos, con más de 2 líneas)
  antes de reemplazar 250696 en producción — solo se probó un caso simple.

---

## Motivo DIAN SÍ se pudo automatizar — conector 251546 + segundo POST (2026-08-03)

A diferencia de 250878/251192 (bloqueo estructural irresoluble en el mismo
POST de creación, ver arriba), **sí es posible fijar el motivo DIAN sin
tocar el escritorio de Siesa** — pero no en el mismo POST que crea la NC,
sino en un **segundo POST separado, contra el documento ya persistido**.
Reduce el procedimiento manual de 3 pasos a 1 — solo Aprobar sigue siendo
manual.

### Comparación con los intentos anteriores

| Conector | Enfoque | Resultado |
|----------|---------|-----------|
| 250878 | Crear + cruzar + motivo + **aprobar**, todo en un solo POST (`auto_reg=1`, `estado=1`) | Bloqueado: 753 (Entidades) siempre se procesa después de 461 (el registro que aprueba) — el motivo llega tarde para satisfacer la validación de aprobación. Irresoluble desde el payload. |
| 251192 | Referenciar una NC ya existente (`Documentos`, T461 subtipo 04) + motivo + **aprobar**, sin crear nada | Mismo bloqueo — cualquier registro 461 (crear o referenciar) dispara la validación de "entidades obligatorias" antes de que el 753 se procese, así vaya en un POST separado de la creación. |
| **251546, un solo POST** | Crear + cruzar + motivo, **sin aprobar** (`auto_reg=1`, `estado=0`) | El bloqueo de aprobación **no aplica** (no se pide `estado=1`) — pero aparece un problema distinto: Entidades dinámicas no acepta `consec_docto=0` como "el documento de esta misma transacción" (Cuotas CxC y Movimientos sí lo aceptan). Falla con "el documento o movimiento no existe". |
| **251546, segundo POST** | Solo la sección Entidades dinámicas, referenciando el **consecutivo real** de una NC que ya existe en Elaboración (creada antes por 251126) | **`codigo:0` — Transacción Exitosa.** Verificado en vivo. |

La diferencia clave con 250878/251192: aquellos estaban bloqueados por el
**orden de procesamiento de registros dentro de una transacción de
aprobación** (753 > 461, un límite del motor, no evitable). Esto es un
problema distinto y sí evitable: Entidades dinámicas necesita que el
documento **ya exista de verdad** (con su consecutivo real, no un
placeholder de auto-creación) — separar la creación del motivo en dos
POSTs distintos lo resuelve limpio, sin pelear con el motor.

### El gap que había que cerrar primero: el WMS no sabía el consecutivo real

Ya documentado como pendiente desde el 2026-07-31 ("el WMS nunca sabe qué
consecutivo de NCE asigna Siesa"). Se cerró hoy con **consultas SQL directas
contra las tablas reales de Siesa**, usando el mecanismo de "Consultas
dinámicas" de Generic Transfer (una consulta ya existente,
`papeleriamedellin_pame_descubrir_tablas`, permite correr SQL crudo contra
el esquema real — mucho más confiable que el texto de ayuda del Asistente,
que ya se había demostrado desactualizado antes con `f753_id_grupo_entidad`).

**Tabla real del encabezado de documento**: `t350_co_docto_contable` (a
pesar del nombre, es el encabezado genérico compartido por todo documento
comercial, no solo contable — confirmado en vivo: la fila de NCE-00000056,
ya conocida de la sesión anterior, aparece ahí con `f350_total_db =
f350_total_cr = 73150.0000`, `f350_id_clase_docto = 526`, `f350_ind_estado =
1`). Después de crear+cruzar con 251126, consultar:

```sql
SELECT * FROM t350_co_docto_contable
WHERE f350_id_co = '<CO>' AND f350_id_tipo_docto = 'NCE'
ORDER BY f350_rowid DESC
```

y tomar el `f350_consec_docto` de la fila más reciente que coincida con el
tercero (`f350_rowid_tercero`) y el valor (`f350_total_db`) esperados.

### Valores de maestro reales, antes desconocidos

- **`f753_id_tipo_entidad` correcto = `G504_1`** ("Facturas y notas
  documentos", tabla relacionada `t350`) — confirmado sin ambigüedad en
  `t747_mm_grupo_entidad_tipo` filtrando por `f747_rowid_grupo_entidad = 9`
  (rowid de `FE_CONCEPTOS NC 2.1` en `t744_mm_grupo_entidad`) y
  `f747_id_tipo_docto = 'NCE'`. **No está en la lista de 8 códigos que
  muestra el texto de ayuda del campo en el Asistente** (esa lista solo
  cubre Facturas de servicio, OVS, CVS, RFVS, OC, Pedidos de venta y
  Entradas de almacén — otra vez el texto de ayuda desactualizado/
  incompleto, igual que pasó con `f753_id_grupo_entidad` en 250878).
- **El atributo `co015_concepto_nc` es de tipo "maestro genérico"**, no
  numérico simple — rechaza `f753_dato_numerico` con el error "el código y
  el detalle del maestro es necesario, el atributo es de tipo maestro
  genérico". Requiere:
  - `f753_id_maestro = 'MUNOECO017'` (código del catálogo en
    `t740_mm_maestro`, rowid 65, descripción "Conceptos Notas Credito - FE
    2.1").
  - `f753_id_maestro_detalle` = código del concepto en
    `t741_mm_maestro_detalle` (rowid_maestro=65): **1**=Devolución parcial
    de bienes, **2**=Anulación de factura electrónica, **3**=Rebaja o
    descuento parcial, **4**=Ajuste de precio, **5**=Otros.

### La receta que funciona — 2 POSTs

1. **POST 1** — `trigger_nota_factura_crear_cruzar()` (251126, sin cambios,
   ya en producción): crea la NC en Elaboración y cruza la cartera.
2. **Consulta** — `t350_co_docto_contable` (arriba) para obtener el
   `f350_consec_docto` real recién asignado.
3. **POST 2** — mismo conector **251546**
   (`PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS`), pero enviando
   **solo** las secciones `Inicial` + `Entidades dinámicas` + `Final` (no
   hace falta reenviar Docto ventas comercial/Cuotas CxC/Movimientos), con
   `f350_consec_docto` = el valor real del paso 2 y los valores de maestro
   de arriba. Verificado en vivo hoy contra NCE-00000057 (factura FEW-1465,
   CO 003): `codigo:0 — Transacción Exitosa`.
4. **Aprobar** sigue siendo manual en el escritorio de Siesa (Regla #21 no
   cambia) — pero contabilidad ya no tiene que buscar ni seleccionar el
   motivo DIAN a mano.

### Conector 251546 — notas de configuración (por si se reconstruye)

Construido sobre el plano `01_Notas credito con entidades` (Generic
Transfer → `03_Comercial` → `4_Especial`, hermano de `07_Nota_Credito_
Entidades_Aprobacion` pero sin las secciones `Documentos`/`Descuentos` y
con `Docto. ventas comercial` en v2, no v9 — por eso no hereda el bloqueo
de 250878). Secciones: Inicial + Docto. ventas comercial (v2) + Cuotas CxC
(v1) + Movimientos (v11) + Entidades dinámicas (v2) + Final.

Bugs de configuración reales encontrados y corregidos en vivo durante el
armado (varios son el mismo patrón: **el Asistente mostraba un valor de
"ejemplo" en el cuadro Fijo que nunca quedó realmente guardado** — hay que
verificar/reescribir cada uno explícitamente, no confiar en lo que se ve):

- `F_CIA` quedó en `020` (plantilla de otra compañía) en vez de `1`.
- `F350_ID_TIPO_DOCTO` (header) y `f470_id_tipo_docto` (Movimientos)
  quedaron fijos en `NDG` (placeholder) en vez de variable.
- `F_LIQUIDA_IMPUESTO`/`F_LIQUIDA_RETENCION` deben ser `0`, no `1` — con
  `1` Siesa exige cuotas porcentuales (suma 100) en vez del valor de cruce
  directo que mandamos en `F353_VLR_CRUCE`.
- `f470_ind_solo_valor` debe ser `0` para motivo `502-01` — confirmado
  contra `t146_mc_motivos` (fila real: concepto 502, motivo 01 =
  "Devolución de venta Nacionales", `ind_naturaleza=1`, `ind_obsequio=0`,
  `ind_solo_valor=0`).
- `f470_id_un_movto` (Unidad de Negocio en Movimientos) debe ser **`99`**
  (ADMON) — el valor global `SIESA_UNIDAD_NEGOCIO` (`040`) **no existe** en
  el maestro real `t281_co_unidades_negocio` para esta compañía (códigos
  válidos: `99` y `001`-`014`, categorías de producto). 251126 nunca choca
  con esto porque su Movimientos usa otra versión/subtipo que no valida
  este campo tan estricto.
- `f753_id_tipo_entidad` y `F461_IND_GENERA_KIT` sufrieron el mismo bug de
  "valor de ejemplo no guardado" — quedaron resueltos poniéndolos Fijo
  explícitamente (`G504_1` y `0` respectivamente).

### Estado: integrado a código (2026-08-05), **ENCENDIDO en producción desde 2026-08-06**

`trigger_motivo_dian_nc()` (POST 2) + `get_consec_nc_creada()` +
`get_max_rowid_nc()` en `connekta_gateway.py`; job `MOTIVO_DIAN_NC`
encadenado en `siesa_job_service.py` después de que
`NOTA_CREDITO_DEVOLUCION_CLIENTE` confirme éxito. Trinquete:
`tests/test_nc_motivo_dian.py`.

`CONNEKTA_CONSULTA_NC_CONSECUTIVO=papeleriamedellin_WMS_NC_Consecutivo` está
registrada en Railway (verificado 2026-08-13) — `connekta.puede_fijar_motivo_dian`
da `True` en producción. Evidencia de que ya corrió con éxito de punta a punta:
`DevolucionCliente` id 7 (`DEVC-20260806163719-367`, 2026-08-06) quedó con
`siesa_motivo_dian='AUTOMATICO'`, `siesa_nc_consec=59`, detalle `"NCE-59
concepto=1"` — sin que nadie tocara Siesa a mano. La devolución id 6, creada
ese mismo día un poco antes, sí quedó `MANUAL` (detalle "falta
CONNEKTA_CONSULTA_NC_CONSECUTIVO") — la variable se activó entre esas dos.

**Job aparte, no inline.** Si el motivo fallara dentro del job de la NC, el
reintento del DLQ entraría por la guarda `siesa_nc_triggered` y devolvería
`{'idempotente': True}` sin volver a intentarlo nunca — un reintento que
parece exitoso y no hace nada. Y la NC ya existe en Siesa cuando el motivo se
intenta: nada de este paso puede ponerla en riesgo.

**Cómo se identifica la NC recién creada** (el gap del 2026-07-31, "el WMS
nunca sabe qué consecutivo asigna Siesa"): marca de agua `MAX(f350_rowid)`
tomada **antes** del POST de creación, y después filtro por CO + NCE + fecha +
estado Elaboración + valor exacto del cruce. Exige **exactamente una**
coincidencia: con cero o con varias falla y el motivo queda manual. Escribirle
el concepto DIAN al documento de otro tercero es un error fiscal; el costo de
no hacerlo es un paso a mano más. Regla 0.

El consecutivo real queda en `DevolucionCliente.siesa_nc_consec` — vale por sí
solo aunque el motivo falle: contabilidad tenía que buscar el documento en
Auditoría para aprobarlo.

La consulta dinámica ya está registrada en Connekta y referenciada en
`CONNEKTA_CONSULTA_NC_CONSECUTIVO` (ver arriba). Debe devolver, sin
parámetros, las columnas crudas de `t350_co_docto_contable` (`f350_rowid`,
`f350_id_co`, `f350_id_tipo_docto`, `f350_consec_docto`, `f350_fecha`,
`f350_ind_estado`, `f350_total_db`) para NCE, ordenadas por rowid
descendente — el SQL exacto está en el docstring de `_filas_nc_encabezado`.
Si esa variable alguna vez queda vacía (rotación de credenciales, cambio de
ambiente), el motivo vuelve a quedar manual automáticamente — sin cambio de
código — y **`/api/health/siesa` lo declara** en `pasos_manuales_nc` (junto
con "aprobar", que sigue sin solución de API y es el único paso que queda).

Pendientes menores:
- `SIESA_CONCEPTO_DIAN_NC` (default `1`=Devolución parcial) es global. Si
  alguna vez aplica `2`/`3`/`4` según el caso de negocio, el mapeo va acá.
- Probar con una devolución parcial genuina end-to-end (pendiente también
  de 251126) antes de confiar en esto para producción.

---

## DLQ — Dead Letter Queue

### Schedulers registrados (`app/__init__.py`)

Dos listas con semántica distinta — elegir mal tiene consecuencias silenciosas:

- **`_scheduler_esenciales`** — corren siempre (salvo `WORKER_SKIP_ESSENTIAL=true`).
  DLQ, sync de pedidos, Vigía.
- **`_scheduler_pesados`** — solo si `HEAVY_SCHEDULERS=true`. Si esa variable
  falta en Railway, **no corren y nadie se entera**. No poner aquí nada cuyo
  silencio sea costoso.

---

## `ENTREGADO_SIN_PAGO` — el cuarto estado (2026-08-13)

Decidido por Dirección de Operaciones. `NO_PAGO_SE_QUEDO` era un **motivo**
dentro de `RECHAZADO`, y `RECHAZADO` significa «los bultos vuelven al camión».
**El estado afirmaba una cosa y el motivo la negaba.**

Mientras fue excepcional se podía vivir con eso. El control «si no paga
completo, no se entrega» lo vuelve cotidiano, y entonces cada consumidor del
estado tiene que acordarse de mirar el motivo. Alguno se olvida — ya pasó con
la lista de reingreso, que mandaba a bodega a buscar cajas que nunca volvieron.

### El conductor no gana un botón

Sigue contestando la pregunta que sabe contestar —**¿volvió la mercancía?**— y
el servidor traduce eso al estado, **una vez, en la frontera**
(`confirmar_parada`). Cada consumidor recibe la verdad sin acordarse de nada:
los de hoy y los que se escriban después.

Por eso `EstadoEntrega.ACEPTADOS_DEL_CONDUCTOR` excluye el estado nuevo: es
real, pero no es una opción de pantalla.

| | Qué significa | Documentos |
|---|---|---|
| `RECHAZADO` | Los bultos vuelven | Nota crédito total |
| `ENTREGADO_SIN_PAGO` | **Quedaron con el cliente, sin pagar** | **Ninguno.** La factura queda abierta en cartera |

No se automatiza nada a propósito: tratarlo como crédito otorgado sería dar
crédito que nadie evaluó. La parada llega marcada y quien liquida decide si
escala (BK-OPS-01 §3.5).

### Y la restricción del punto 4, que iba junta

`forma_pago = CREDITO` sobre una parada declarada de contado se rechaza. Se
valida contra `tareas_packing.cond_pago` (anotada al cargar la ruta), **no
contra Siesa**: la confirmación tiene que funcionar sin señal. Si la condición
no se alcanzó a anotar **no se bloquea** — no saber no es evidencia de contado
(Regla 0), y una parada trabada en la calle no la desbloquea nadie.

> **Superado el 2026-09-24** («Contado contraentrega vs crédito real»): sin
> condición anotada la parada **se cobra** como contado supuesto, y la guarda
> rechaza CREDITO y EXENTO por días de la condición (≤ 15), no por código.

### Dos cosas que se arreglaron de paso

`EstadoEntrega` estaba definida **dos veces** —`models/recaudo_entrega.py` y
`services/ruta_service.py`— con los mismos valores y distinto nombre de tupla
(`TODOS` / `VALIDOS`). Agregar un estado a una sola era cuestión de tiempo.
Ahora el servicio importa la del modelo.

Y `bultos_rechazados()` **subcontaba**: miraba solo bultos marcados
`RECHAZADO`, así que los que el conductor no tildó desaparecían del bloque de
responsabilidad. Ahora el bloque lo define la **parada**, no el bulto.

Trinquete: `tests/test_entregado_sin_pago.py` (19 tests, 7 mutaciones).
Migración: `m006entregadosinpago` — reclasifica el histórico y pone dos CHECK,
uno de ellos el invariante que impide que la combinación vieja vuelva a entrar
por un camino que no pase por `confirmar_parada`.

---

## La tanda media de la auditoría — E, F, G, I, J, K, L (2026-08-13)

Siete defectos, y **cuatro comparten una sola forma: un valor con dos
significados.**

| | Qué pasaba | Ahora |
|---|---|---|
| **F** | `base_gravable * 0.19` en `rutas.py` **inventaba el IVA**. Con líneas exentas la retención salía casi al doble (28.500 vs 15.000 sobre un caso real). Tercera copia de la fórmula, y la única equivocada | `base_de_retencion()` / `monto_de_retencion()` — una función, tres sitios. Detector **por AST**: el de texto se atrapaba en su propio docstring |
| **G** | El cobro validaba `forma_pago` y no `estado_entrega`: se registraba dinero sobre una parada RECHAZADA o ENTREGADO_SIN_PAGO | Validado **en el servicio**, no en la ruta — el endpoint no es la única puerta |
| **E** | El RC que espera su NC gastaba reintento. Backoff `[5,15,45,120,180]` × 5 ≈ **6 horas**, y lo que lo desbloquea es una recepción física que puede ser mañana → FALLIDO, cobro nunca enviado | `DependenciaPendiente` no gasta reintento. El precedente estaba al lado: `ConnektaCircuitOpenError` tampoco |
| **J** | Antes de facturar una remisión se preguntaba «¿el **pedido** tiene FE?». En un segundo parcial, la FE del primero contestaba que sí → tarea marcada hecha **sin facturar la segunda remisión** | `get_factura_desde_remision` — que existía, estaba probada y **no tenía un solo caller** |
| **K** | Un 429 a mitad de sincronización abortaba la paginación, y los pedidos de las páginas no leídas **se borraban**, reportados en `eliminados` como limpieza normal | El borrado exige barrido completo. El resultado declara `paginacion_completa` |
| **L** | La API de inventario falla → se cae a la BD (horas o días vieja) → y se le pone `utcnow()`. **Sello fresco sobre dato viejo**, usado para proponer traslados | La marca de tiempo solo avanza con datos de Siesa; el cache declara `degradado` |
| **I** | El 173066 omitía `f470_rowid_movto`, que sus dos hermanos mandan en `0` | Se agrega. **Y deja un dato**: venía corriendo en producción sin él, lo que es evidencia —no prueba— de que Connekta mapea por nombre y no por posición |

Trinquete: `tests/test_auditoria_tanda_media.py` (7 mutaciones, las 7 rojas).

---

## Dos escaladas de autorización (2026-08-13)

Las dos verificadas ejecutando la aplicación con credenciales de cada rol.

### El atajo que pedía menos que sus partes

```
/liquidar          → _solo_admin
/liquidar-siesa    → _solo_admin
/liquidar-completo → _es_admin_o_jefe   ← hace las DOS, y encima las retenciones
```

Un jefe de almacén recibía **403 en las dos operaciones granulares y 200 en la
que las ejecuta a las dos**.

El invariante, que vale más allá del caso: **un endpoint compuesto no puede
exigir menos que el más estricto de sus componentes.** La forma se repite sola
— alguien agrupa pasos para que la pantalla haga una sola llamada y le pone el
permiso de quien va a usar la pantalla, no el de lo que el endpoint ejecuta.
Trinquete: `tests/test_permiso_compuesto.py`.

> **2026-09-25:** `/liquidar-completo` se borró (sin pantalla, y mandaba la
> retención sin decisión y sin cuenta ni UN reales). La misma forma volvió por
> «Registrar cobro» (encola RC y DC, pedía admin-o-jefe), que la lista a mano
> del trinquete no nombraba: ahora el trinquete **descubre por AST** toda ruta
> que llega a un encolador de plata. Ver «La plata del conductor hasta Siesa».

### Packing tenía dos puertas y una sin guardia

```
PUT  /api/packing/<id>/confirmar     → permiso de empaque + propiedad ✓
POST /api/mobile/confirmar (PACKING) → ninguno de los dos ✗
```

La vía móvil llamaba al servicio **sin pasar el usuario**, así que no había
nada que verificar: cualquier operario confirmaba el packing de otro, y sin
permiso de empaque.

**La causa es de capa, y picking ya la tenía bien**: `confirmar_picking`
verifica la propiedad *dentro del servicio*, así que toda vía la hereda.
Packing la tenía en la ruta, y la segunda ruta la esquivaba.

> Un guard en la ruta protege esa ruta. Un guard en el servicio protege la
> operación.

El servicio usa `_puede_empacar` —la misma función de la ruta, que incluye el
flag `puede_empacar` y no solo el rol— y la supervisión salta la **propiedad**,
no el permiso de empaque. `jefe_almacen` no está en `PACKING_ROLES` y sin el
flag tampoco confirma: era así antes y sigue igual.
Trinquete: `tests/test_packing_dos_puertas.py`.

---

## El recibo de caja duplicado — una búsqueda escrita tres veces (2026-08-13)

`f353_id_tipo_docto_cruce` / `f353_consec_docto_cruce` traen el **PEDIDO**, no
la factura. Verificado en vivo el 2026-08-11.

Esa búsqueda estaba escrita **tres veces** en el repo. Dos usaban la clave
correcta; la tercera —`_factura_saldada_en_siesa`, en `siesa_job_service`—
buscaba por la **FACTURA**, citando la misma verificación en vivo.

Y esa tercera es la que decide, tras un POST que lanzó excepción, si el recibo
de caja **sí entró**. Al no encontrar nunca la fila respondía «no entró», el job
revertía la bandera y la cola reenviaba: **segundo recibo de caja**. Que es
exactamente el incidente RC-00002744 que la Regla 3 existe para prevenir.

### Y «no encontré» devolvía lo mismo que «tiene saldo»

`esta_saldada()` ahora devuelve `True` | `False` | **`None`**. Ante `None` la
Regla 3 manda: **no reintentar**. Un recibo duplicado es un documento
financiero que alguien reversa a mano; una factura sin recibo queda con saldo
abierto y eso el desglose lo ve.

Una sola función: `services/cxc_cruce.py`. Trinquete: `tests/test_cxc_cruce.py`
exige que **ningún** sitio vuelva a armar el match a mano — así fue como
divergieron.

---

## Despacho marcado sin documento fiscal — `[]` con tres significados (2026-08-13)

`get_compromisos_pedido` devolvía `[]` en tres situaciones distintas:

```
modo simulación          → []
parámetros inválidos     → []
CUALQUIER excepción      → []      ← red caída, timeout, 429, Siesa fuera
```

Y `DespachoParialService` lee la lista vacía como **«la automatización de Siesa
ya procesó el pedido completo»**: marca la tarea `DESPACHADO` y
`siesa_triggered = True`. **Sin remisión y sin factura.**

Mercancía saliendo del CD sin respaldo fiscal, en verde en el tablero. Y la
guarda `if tarea.siesa_triggered` bloqueaba el reintento **para siempre**.

Se disparaba con cualquier caída del ERP durante un cierre de empaque.

Ahora levanta `CompromisosNoDisponibles`. Es la misma regla que ya tenía
`ConnektaPaginacionError` en el mismo archivo, y el mismo criterio que
`get_factura_desde_pedido` ya aplicaba: **ante dato ausente, declararlo, no
rellenarlo con silencio.** Esta consulta era la inconsistente, y la que más
costaba.

Trinquete: `tests/test_compromisos_vacios.py`.

> **Superado el 2026-09-25** («Fiscal y despacho», abajo): la rama de
> compromisos vacíos ya **no marca DESPACHADO**. Busca la remisión que los
> consumió y la factura; sin RM identificada, resultado desconocido.

---

## Fiscal y despacho: ninguna mercancía sale sin documento (2026-09-25)

**Decisión del dueño:** *si Siesa está caído y no se puede facturar, se para
todo.* No hay modo contingencia: ninguna caja se cierra, ningún pedido queda
DESPACHADO y ningún bulto va al muelle ni a la ruta sin **remisión Y factura
confirmadas**. El texto que ve la pantalla lo pone el servidor:
«Siesa no está disponible: no se puede facturar, la caja queda esperando.»

**La clase:** *«no pude preguntarle a Siesa» leído como «no existe» o como «ya
está hecho».* Trinquete: `tests/test_documento_fiscal.py` (87 tests, 24 mutaciones, las 24 rojas).

| Qué pasaba | Ahora |
|---|---|
| `reconciliacion_service._ESTADOS_CUMPLIDO = {'9'}`: un pedido **anulado** mientras la caja esperaba (p. ej. retenida por cartera) quedaba `siesa_triggered` + DESPACHADO, y el muelle lo dejaba subir sin RM ni FE | **Una tabla de estados de pedido**, `estado_pedido_siesa` (9 = ANULADO en sync, cierre, historia y reconciliación). La reconciliación solo reconcilia con **la factura y su consecutivo**; el estado sirve para declarar un anulado (`pedido_anulado_siesa`), nunca `siesa_triggered`. Tres respuestas (`reconciliado` / `no_se: False` / `no_se: True`); el DLQ no manda nada si «no se sabe». El barrido rota por `reconciliacion_intento_at` (antes `.limit(10)` sin orden) y no mira anulados |
| `get_remision_desde_pedido` devolvía `None` ante cualquier error y pedía `tamPag=200`; un 142945 sin consecutivo terminaba en `ValueError` → el reintento **reenviaba el 142945** (RM #2), o caía en compromisos vacíos → `244328-AUTO` DESPACHADO sin RM ni FE | Pre-flag **`rm_enviada_at`** antes del 142945 (Regla 6): se revierte solo ante un «no» explícito (`ConnektaRechazado`: 4xx, 429, `codigo≠0`) o una petición que no salió. Con el flag y sin RM, **nunca se reenvía**: se identifica la RM (tres estados). Recién enviada, `EsperandoRemision` (Regla 20, sin gastar reintento, gracia 15 min); después, `RemisionNoIdentificada` (subclase de `ConnektaResultadoDesconocido` → FALLIDO sin reintento). La salida: **facturar-rm-manual** (con la RM que se ve en Siesa) o `{"rm_inexistente": true, "motivo"}` en el mismo endpoint, que vuelve a preguntar antes de quitar el flag. `_persistir_resultado` se niega sin `rm_consec` |
| Compromisos vacíos = DESPACHADO `244328-AUTO` | Busca la RM que los consumió y factura (con el anti-duplicado de FE por pedido); sin RM, resultado desconocido. Nunca DESPACHADO sin `rm_consec` |
| El cierre de caja marcaba **DESPACHADO al encolar** el job, antes de Siesa; el precheck caído decía «Reintentá…» | La caja queda **VERIFICADA con sus bultos** hasta que `_persistir_resultado` (RM + FE) la despacha. Con el circuito abierto, fuera de la ventana de la Regla 14 (solo si `SIESA_VENTANA` está configurada, tanda 2 · H) o sin respuesta del precheck, el cierre **se niega sin tocar nada** (ni bultos, ni job, ni estado). Con una RM enviada sin confirmar no se re-encola. El DLQ tampoco intenta el DESPACHO_F470 fuera de ventana (espera sin gastar reintento) |
| El muelle y la ruta decidían con `siesa_triggered`; `asignar_a_ruta` no miraba nada y una ruta quedaba trabada en `cerrar_ruta` | **`documento_fiscal.despachable`** (+ gemela SQL `filtro_despachable`): pedido = `siesa_triggered` + `rm_consec` + FE confirmada (`fe_consec` o `fe_confirmada_at`); traslado = su regla de siempre. La usan la lista del muelle, asignar (por bultos y por pedido), cargar, los sugeridos y `cerrar_ruta` (una ruta no sale con un bulto sin documento, aunque ya esté cargado) |
| Cancelar, resetear-siesa, iniciar-despacho y «devuelto al estante» contestaban cada uno a su manera «¿esta caja tiene documento?» (cancelar solo miraba jobs vivos: RM + job FALLIDO → otra caja → RM #2) | **`documento_fiscal.tiene_documento_en_siesa`** (+ `filtro_tiene_documento`): `siesa_triggered`, RM, FE o el pre-flag. `cancelar`, `resetear_siesa`, `confirmar_packing` (re-confirmar), `crear_manual`, `crear_desde_picking`, `iniciar-despacho` (**antes** de crear el picking) y el filtro de «devuelto al estante» |
| `except Timeout` atrapaba `ConnectTimeout` (la petición nunca salió) como «resultado desconocido» | `ConnektaNoEnviado`: se reintenta. `ReadTimeout` sigue siendo desconocido (Regla 3). Un 5xx sigue siendo `Exception` genérica (no es un «no») |
| Una caja retenida por cartera en la emisión: job PENDIENTE sin fin, cancelar se negaba por «job vivo», la tarea figuraba DESPACHADO | Cancelar la caja **descarta el job** (`descartar_job_retenido`, solo PENDIENTE y sin documento) y **cancela la retención** (`cartera_service.cancelar`), con bitácora. La política de retención no cambió |
| `descartar_job` de un DESPACHO_F470 con RM y sin FE lo sacaba de todo contador | Se niega salvo `reconoce_remision_sin_factura: true` (queda en la bitácora) |

**Invariantes nuevos:** **VTA-31** BLOQUEA — `siesa_triggered` o DESPACHADO
sin RM + FE confirmadas (misma política del muelle); `defecto_corregido`
`m048fiscal`: lo anterior es artefacto. **VTA-32** BLOQUEA — remisión (o
142945 sin confirmar) sin factura hace más de 24 h (factura extemporánea).

**Migración `m048fiscal`** (aditiva, nullable): `tareas_packing.rm_enviada_at`,
`fe_confirmada_at`, `reconciliacion_intento_at`. **Un backfill,
imprescindible:** `fe_confirmada_at` de las filas con `siesa_triggered` y
`rm_consec` (hasta hoy esa combinación solo la escribía `_persistir_resultado`
después de la FE); sin él, sus bultos pendientes desaparecían del muelle.

**Trinquetes (AST):** todo llamador de `get_estado_pedido` interpreta el número
con la tabla (inventario: el delegado del gateway); toda función que crea,
cancela o reabre una caja o borra sus bultos pregunta la política (exento: el
traslado); los módulos del muelle y la ruta no leen `siesa_triggered`; toda
caja de pedido nace con su condición (`anotar_desde_historia` — `crear_manual`
no la anotaba y la compuerta G2 daba por contado un C04). Meta-tests y pisos.
`tests/conftest.py` fija el reloj de la ventana (`_siesa_en_horario_de_facturacion`):
con el reloj real, todo cierre con Siesa mockeada fallaba de noche y en el CI.

**Lo que NO cubre:**
- Un `ConnectionError` que no es timeout (conexión cortada a mitad) sigue
  siendo `Exception` genérica: puede haber salido. No se separó.
- El 142943 no tiene pre-flag propio: un timeout de la FE queda FALLIDO
  (desconocido) y su reintento pregunta la FE **por pedido** (el mismo
  anti-duplicado de siempre, con el riesgo del doble parcial ya declarado).
- El reset de PROCESANDO a 10 min se dejó: con el pre-flag, el reintento de
  un 142945 colgado identifica la RM en vez de reenviarla.
- Las tareas DESPACHADO sin documento de antes de este cambio siguen así
  (VTA-31 las cuenta como artefacto); la salida es facturar-rm-manual o, si
  el pedido no salió, anular en Siesa y cancelar.
- El botón «la RM no existe» no tiene pantalla todavía (el endpoint sí).

---

## El trinquete de rutas huérfanas medía presencia, no adyacencia (2026-08-13)

```python
if all(s.rstrip('/') in blob for s in self._segmentos_literales(ruta)):
    continue            # ← la ruta se eximía, en silencio
```

Para `/api/picking/<int:id>/confirmar` los trozos son `/api/picking/` y
`/confirmar`. **Los dos existen en el frontend, por separado** — el primero lo
aporta `/api/picking/${id}/reabrir`, el segundo cualquier otro endpoint. La ruta
se declaraba usada sin que nadie la llamara.

**La clase que el agujero tapaba es exactamente la parametrizada, que es la que
mueve inventario.** Por eso ninguna figuraba en la deuda declarada.

Al exigir adyacencia aparecieron **once**, todas de esa clase: confirmar
picking, escanear en packing, registrar un conteo, despachar un parcial. Quedan
declaradas en `DEUDA_SIN_UI` con su motivo, y **son candidatas a borrar, no a
conectar**: cada una es una segunda puerta a una operación crítica, sin la
idempotencia de la vía viva.

### Y la adyacencia tampoco alcanzaba: ahora mide invocación (2026-08-15)

Presencia y adyacencia comparten el mismo punto ciego, que es el que importa:
**una URL escrita dentro de una función que nadie llama está escrita.**

Los tres botones de recuperación de traslados —`trasRevertir`,
`trasReintentarDespachoSiesa`, `trasReintentarRecepcionSiesa`— lo satisfacían
con holgura: `fetch(API + \`/api/traslados/${id}/revertir\`)` es adyacencia de
manual. Ningún `onclick` las alcanzaba. Y mientras tanto `traslado_service`
le mandaba al operario, por `siesa_error` y por correo, «WMS Admin → Traslados
→ Reintentar despacho»: **el sistema daba una instrucción imposible** justo
cuando la mercancía ya salió y Siesa no tiene documento.

El guard construye ahora un **grafo de llamadas** del PWA y solo cuenta las URL
que viven en código alcanzable desde un `onclick` del HTML, el arranque de un
módulo, un `addEventListener` o una función ya alcanzable. Destapó seis rutas
más, declaradas con su razón verificada; tres de ellas son el JS de las
pantallas `picker-traslado`/`packer-traslado`, **borradas del HTML y vivas en el
código** — ~280 líneas que un auditor lee como si describieran la operación de
hoy.

Lo que el guard no puede ver está escrito en su encabezado (invocación por
string, sobre todo). Y como un detector de alcance roto marca **todo** como
alcanzable —el troceo se come un archivo y el guard se apaga en silencio—,
`TestElDetectorDeAlcanceSeMide` le pone pisos mínimos, un canario por cada
forma de conexión, y mutaciones: se le quita al repo el `onclick` de cada botón
**en memoria** y se exige que la ruta caiga como huérfana.

---

## Las tres banderas de idempotencia financiera (2026-08-13)

Tres auditorías independientes convergieron sobre el mismo par de líneas:

```
rutas.py:1244              recaudo.siesa_dc_triggered = True   # «evitar doble encolado»
siesa_job_service.py:1249  if recaudo.siesa_dc_triggered: return {'idempotente': True}
```

El endpoint de liquidar-completo encolaba los documentos de retención y acto
seguido encendía **la bandera que el ejecutor usa como guarda**. Cada job la
leía, se declaraba idempotente y se marcaba completado **sin enviar nada**.

**Ningún documento de retención llegó nunca a Siesa por esa vía**, y el log, la
pantalla y el tablero informaban éxito.

Lo que lo volvía indetectable: la verificación ya pedida —«una liquidación con
retención, que nunca ha corrido»— **no lo habría descubierto**. La corrida se ve
exitosa desde el WMS. Solo abrir Siesa y no encontrar el documento lo revela.

### La causa de fondo: una bandera, dos significados

«Ya encolé» y «ya envié» no son lo mismo. Con el mismo booleano para los dos,
uno de los dos está siempre mal.

| | Antes | Ahora |
|---|---|---|
| Anti-doble-encolado | la bandera de envío | `_pucs_en_cola` — mira la cola |
| Guarda del ejecutor | `siesa_dc_triggered` (booleano) | `pucs_enviadas()` — **por cuenta PUC** |

### Y las retenciones son N, no una

Retefuente + reteIVA + ICA son **tres documentos**. Con la guarda sobre un
booleano, el primer job la encendía y los otros dos se declaraban idempotentes
sin enviarse: tres jobs completados, un documento en Siesa.

`siesa_dc_pucs` (`m007retencionesporpuc`) guarda las cuentas ya enviadas. Se
escribe ANTES del POST y se revierte solo ante fallo explícito — mismo patrón
de la Regla 6, por documento en vez de por recaudo. Un JSON ilegible se lee
como «no sé qué se envió» y **no reenvía**: un documento contable duplicado es
un ajuste manual en el ERP.

`siesa_dc_triggered` se conserva y sigue significando «se envió al menos una».
Lo que dejó de ser es la guarda.

### La nota crédito marcaba DESPUÉS del POST

Era la única de las tres. Un crash entre el POST y el commit dejaba la bandera
en False, el DLQ reintentaba y Siesa recibía una **segunda nota crédito**. RC y
DC ya pre-marcaban; ésta no, y nada explicaba por qué.

Trinquete: `tests/test_idempotencia_retenciones.py` (5 mutaciones).

---

## Probar el respaldo antes del corte (2026-08-14)

**Un respaldo que existe no es un respaldo: es un archivo.** Lo que hace falta
saber es si se puede volver a operar desde él, y eso solo se sabe restaurándolo
una vez en una base **aparte** y mirándolo.

```bash
# 1 · foto de producción — SOLO LEE
venv/bin/python scripts/verificar_restauracion.py --foto produccion.json

# 2 · restaurar el respaldo en una base NUEVA (Railway)

# 3 · comparar
DATABASE_URL='…copia…' venv/bin/python \
    scripts/verificar_restauracion.py --contra produccion.json
```

### No toda diferencia es un fallo

Un respaldo es de un momento anterior, así que **es normal que le falten filas
operativas**. Lo que no puede faltar es lo que no se regenera:

| | |
|---|---|
| `kardex_movimientos`, `serie_vigia`, `stock_diario`, `precios_realizados`… | **irrecuperable** — no vuelve desde Siesa |
| `pedidos_siesa`, `stock_siesa`, `siesa_jobs`, `movimientos_inventario` | se recargan |
| cualquier otra | ante la duda, se rechaza |

Tratarlas igual produce una de dos cosas malas: un respaldo bueno rechazado por
ruido, o **uno malo aprobado porque «total, faltan pocas»**.

También compara la **cabeza de migraciones**: una copia en otra revisión no la
levanta la app.

### Lo que este script NO prueba

Que la aplicación arranque contra la copia. Eso es apuntar `DATABASE_URL` a la
copia y abrir `/api/health/ping`. **Un esquema íntegro con la app caída sigue
siendo una noche perdida.**

---

## El acta de corte no cortaba del todo (2026-08-14)

`reset_transaccional.py` es **deny-by-default**: solo vacía lo que está en
`OPERATIVAS`. Eso protege la memoria analítica, y tiene una consecuencia que no
se ve — **una tabla que no está en ninguna lista sobrevive al corte con los
datos del ensayo**.

Había **cinco sin clasificar**, y dos eran `devoluciones_cliente` y
`lineas_devolucion_cliente`: justo las tres devoluciones de prueba del 28 de
julio que la auditoría de flujo venía reportando. Después del corte habrían
seguido ahí, reportándose para siempre como si fueran operación real.

### Y dos órdenes que habrían hecho fallar el corte

| Tabla | Apunta a | Se borraba |
|---|---|---|
| `devoluciones_cliente` | `tareas_packing`, `recaudos_entrega` | **después** |
| `sesiones_conteo` | `tareas_picking` | **catorce posiciones después** |

El `DELETE` del padre falla por clave foránea y el `except` del bucle lo imprime
como un aviso entre otros. Es el mismo tropiezo que ya costó una vez con
`flota_lectura_odometro` —documentado en el propio script— y que el orden mal
puesto reintroducía.

**Lo encontró el trinquete, no una corrida.** Una corrida solo lo habría
mostrado el día del corte, que es el día en que alguien improvisa un `DELETE` a
mano — exactamente lo que el script existe para evitar.

### El corte tiene que afirmar que cortó

`ok` solo medía que la memoria analítica hubiera sobrevivido. Con tablas
operativas llenas el script imprimía la línea roja y **devolvía 0**: decía
«RESET COMPLETO» y daba permiso para arrancar. Ahora el éxito exige las dos
cosas.

### `precios_realizados` es analítica, no operativa

No es una lista de precios: es `valor / cantidad` sobre ventas reales, neto de
descuentos. Alimenta el **Cu del newsvendor** —margen medido en vez de
supuesto— y mide la escalera de precios entre C.O. Borrarla devuelve los
modelos al margen supuesto sin que nadie lo note.

### El script no verificaba contra qué base borraba

Tomaba `DATABASE_URL` y ejecutaba. Y **el `DATABASE_URL` de una sesión de
desarrollo apunta a la base de producción en Railway** — comprobado el
2026-08-14: `metro.proxy.rlwy.net/railway`, 51.808 filas.

Con eso, un `--ejecutar` recuperado del historial de la terminal vacía
producción. No hace falta equivocarse: basta con repetir un comando.

Ahora `--ejecutar` no alcanza — hay que **escribir el host**:

```bash
venv/bin/python scripts/reset_transaccional.py                      # simulacro
venv/bin/python scripts/reset_transaccional.py --ejecutar \
    --confirmar-destino <host>                                      # de verdad
```

Es el único gesto que no se puede hacer por inercia, y obliga a mirar a dónde
va el borrado antes de que ocurra. El simulacro sigue sin fricción: es lo que
alguien corre para decidir, y ponerle trabas lo empuja a saltárselo.

Trinquete: `tests/test_acta_de_corte.py` — invoca el script de verdad contra un
sqlite temporal y exige `exit 2`. Las versiones anteriores buscaban el nombre
de la función en el fuente: quitar la llamada dejaba la definición intacta y el
test seguía verde.

---

## Las 28 requisiciones huérfanas — consultar demasiado pronto (2026-08-14)

La primera auditoría contra producción agrupó los 53 errores de traslado por
causa. **La más numerosa —28— no era un rechazo:**

> «174646 aceptada por Siesa pero el WMS no pudo leer el consecutivo. El
> despacho usará 173076 (fallback). La RIT huérfana debe cerrarse manualmente.»

La RIT **sí entra**. Lo que falla es leerla de vuelta — y la causa está escrita
en este mismo archivo:

**Regla 20:** *«Después de POST exitoso, Siesa tarda ~10-12 s en procesar — no
consultar inmediatamente.»*

`aprobar_solicitud` hace el POST del 174646 y consulta el consecutivo **en la
línea siguiente**. Llega temprano, no encuentra nada, marca la RIT como
huérfana y despacha por el fallback. Veintiocho requisiciones sueltas en Siesa
que alguien tiene que cerrar a mano.

Se descartó el truncamiento antes de buscar en otro lado: `f440_referencia`
mide 20 y el código del traslado 16.

**El reintento va en el despacho, no en un `sleep`.** Dormir 10 segundos en el
request de aprobación castiga a quien aprueba por un problema de tiempos del
ERP; el despacho ocurre minutos u horas después y para entonces la espera que
la Regla 20 pedía ya pasó sin que nadie la haya esperado.

Trinquete: `tests/test_rit_huerfana.py`.

### Y la agrupación por causa es lo que lo hizo visible

`TRA-12` devolvía **53 filas** y ninguna se podía triar. Agrupadas por firma
—el mensaje sin los identificadores que cambian— quedaron **4 causas**, y tres
de las cuatro **no son defectos de código**: permisos del conector (4), red
(2), y esta lectura temprana (28). El rechazo estructural real son 19.

Un hallazgo por fila convierte un problema en una lista, y una lista larga se
ignora.

---

## Auditoría de invariantes de frontera (2026-08-13)

`app/services/auditoria/` · `tests/flujo/` · `GET /api/auditoria/flujo`

### El hueco que tapa

La suite tiene ~1900 tests y **ninguno puede fallar por un defecto de
frontera**: cada archivo arma su propia `TareaPacking(...)` desde cero, así que
verifica cada etapa con datos que él mismo fabricó coherentes. La coherencia
*entre* etapas no se ejercita nunca.

No es un fallo de los tests — es su forma. Un unitario que construyera todo el
flujo dejaría de ser unitario.

### Un invariante, dos fuentes de datos

```
tests/flujo/     → un pedido sintético recorrido con los SERVICIOS REALES
/api/auditoria   → los datos que ya están en la base
```

La regla se escribe **una vez**. Escribirla dos veces sería la divergencia que
la Regla 0 prohíbe: el test pasaría, la auditoría diría otra cosa, y nadie
sabría cuál creer.

### El arnés no inserta filas

Cada etapa se avanza llamando al servicio que la operación llama
(`confirmar_picking`, `crear_desde_picking`, `confirmar_parada`). Un arnés que
escribe `TareaPacking(...)` directo solo prueba que la base acepta esas filas.

**El camino por defecto hace picking parcial** (recoge 7 de 10). Operaciones
confirmó que es el caso real; un arnés que solo ejerce el caso feliz verifica
un flujo que nadie tiene.

### Severidad

`BLOQUEA` el dato ya está mal · `AVISA` se degrada solo · `OBSERVA` hay que
poder contarlo. La distinción existe para que el canal siga siendo legible.

### Lo que encontró al escribirlo

`pedidos_sync_service.py:145` deja `producto_id = None` cuando el ítem no está
en el catálogo local — **sin contador y sin alerta**, y en
`packing_service.py:188` eso se convierte en «Producto None» en la pantalla del
empacador. El sync de barras, el de empaques, temporada e inventario **todos
cuentan sus `sin_producto`**; el de pedidos es el único que no. Es `VTA-01`.

### La primera corrida contra producción encontró 21 bloqueantes — y dos eran míos

Al correrla sobre datos reales aparecieron 21 hallazgos. **Dos invariantes
estaban mal planteados y había que arreglarlos antes de que alguien
investigara:**

`VTA-20` comparaba `TareaPicking.cantidad_recogida` **en vivo** contra lo
empacado. Pero `reabrir_picking` **pone esa cantidad en cero**
(`picking_service.py:519`), así que un pedido pickeado, empacado y con su
picking reabierto después salía como «empacado 7 > recogido 3» sin que nadie
hubiera empacado de más. **Comparar un valor mutable contra un consumo
histórico mide dos momentos distintos.** Ahora compara contra
`ItemPacking.cantidad_esperada`, el snapshot que el propio packing guardó. La
divergencia con el picking actual vive en `VTA-22`, como `AVISA`.

`VTA-21` («packing sin picking») bloqueaba, pero `PackingService.crear_manual`
existe, no exige picking y **no marca el packing de ninguna forma**. Un packing
manual legítimo es indistinguible de un picking perdido: no se puede bloquear
sobre una pregunta que el modelo no sabe responder. Bajó a `AVISA`.

**Un falso positivo quema la herramienta entera.** Mandar a alguien a
investigar seis casos que no lo son es cómo se aprende a ignorar el canal — la
misma lección de los 639 avisos conocidos.

Los otros hallazgos (`VTA-30` despachos sin bultos, `VTA-60` cobros que no
llegaron al ERP, con cifras) quedan en pie.

### Cobertura — 6 flujos, 39 invariantes

| Flujo | Invariantes | El riesgo propio de ese flujo |
|---|---|---|
| `venta` | 11 | Sale mercancía sin documento fiscal |
| `traslados` | 7 | **El limbo**: el STS disparó y el ETS no. El stock no falta ni sobra — está en la bodega puente, donde nadie pregunta. Y nadie reclama: una tienda que no recibió un traslado que no pidió, no llama |
| `conteo` | 6 | **Nadie reclama un ajuste.** Entra al ERP, cuadra el papel contra la realidad equivocada, y reaparece en el siguiente conteo físico meses después |
| `devoluciones` | 5 | Mercancía y dinero vuelven **por caminos distintos**; si uno ocurre y el otro no, no se ve desde ninguno de los dos |
| `recepcion` | 4 | El espejo de venta: entra mercancía que el ERP no registró |
| `reposicion` | 5 | **Ningún cuadre por sumas lo ve**: el total no cambia, se mueve de una ubicación a otra. Aparece cuando un picker no encuentra en PICKING lo que el sistema dice, y reporta un faltante que está en RESERVA |

**Lo que falta está escrito**, no olvidado: `tests/flujo/test_cobertura_invariantes.py::SIN_CUBRIR` lista los flujos sin invariantes con el motivo. Un auditor que cubre uno de seis devuelve `0 hallazgos` para lo que no mira — el denominador tiene que ser visible.

### La regla del guard — leer antes de escribir cualquier invariante

> **¿Qué escribe el valor que estoy comprobando, y puede el camino roto
> escribirlo igual?** Si la respuesta es sí, el guard no sirve.

Es la lección más cara de la auditoría del 2026-08-15: **seis guards en verde
sobre propiedades que la vía sana satisface por construcción.**

| Guard | Medía | Por qué no podía fallar |
|---|---|---|
| `TRA-01` | `enviada ≥ recibida` | tienda escribía los dos **iguales** |
| `REP-02` | `siesa_enviado` sin `COMPLETADA` | la bandera solo existe **dentro** de ese estado |
| `CNT-04` | ¿existe fila hija? | «omitir segundo conteo» la conserva en `CANCELADO` |
| `REC-01` | `siesa_triggered` | se fuerza a `True` **cuando falla** |
| `test_bodegas` | un mapa CO | había **tres**, y leía el bueno |
| Integridad N4 | la URL está adyacente | estaba **dentro de una función sin llamador** |

Los seis tenían el test de «no dispara cuando está sano». **Ninguno tenía el
otro.** Los seis se habrían caído al primer intento.

**Por eso `@invariante(...)` exige `detector_ciego`**: la referencia
`archivo::Clase::test` del test que construye la violación y verifica que el
invariante la vea. `tests/test_detector_ciego_obligatorio.py` comprueba **por
AST que la referencia resuelva** — una que apunta a un test borrado afirma una
cobertura que no existe.

Hoy: **28 de 28 `BLOQUEA` lo cumplen**, y `SIN_DETECTOR` está vacía.

⚠️ **Y ningún detector puede ser de texto.** Solo AST. Los detectores de texto
se atraparon en sus propios docstrings **siete veces** en una semana — la
séptima fue el regex que medía esta misma regla y se le escapó `CNT-07`, diez
minutos después de que se escribiera la advertencia.

### Cómo agregar uno

Decorar con `@invariante(...)` en el módulo del flujo. El registro es por
decorador y no por lista al final: una lista que hay que acordarse de
actualizar es un invariante que algún día no corre y nadie nota.

**Todo invariante nuevo necesita su test de detector ciego** — romper el flujo
a propósito y exigir que lo vea. Sin eso `0 hallazgos` no significa nada.

---

## Reposición Micro — RESERVA→PICKING (2026-08-27)

Reposición **micro** es la única que queda en el módulo — la macro (alerta
diaria a Compras por stock total del almacén, sin importar layout físico) se
retiró el mismo día: `verificar_y_alertar_stock_macro()` y
`_enviar_alerta_stock_macro()` salieron de `alertas_service.py` completas, sin
dejar código huérfano. Micro trabaja a nivel de hueco físico, no de
referencia — son cosas distintas, no una versión reducida de la otra.

### Es 100% cálculo local — cero Siesa

La decisión de "¿hay que reponer este hueco?" nunca consulta Siesa. Vive
entera en `UbicacionProducto` (una fila por hueco × SKU) y dos campos:
`cantidad` (lo físico contado ahí) y `reservado` (comprometido a un
pedido/traslado, todavía no sacado). Reposición mira
`disponible = cantidad - reservado`, no `cantidad` cruda — así reacciona
desde el momento en que un pedido **compromete** el hueco, no cuando el
operario efectivamente lo vacía.

El descuento pasa en dos tiempos, no uno:

1. **Al crear la tarea de picking** (`PickingService.crear_tareas()`, FEFO)
   — reserva: `reg.reservado += cantidad`. Antes de que el operario camine.
2. **Al confirmar** (`confirmar_picking()`) — descuento real:
   `reg.cantidad -= cantidad_recogida`, reserva liberada.

Siesa se entera después y solo para contabilidad — `confirmar_reposicion()`
dispara el job 173076 (tránsito entre ubicaciones) una vez el WMS ya decidió
y ejecutó, nunca antes.

### Tres disparadores, no uno

| Disparador | Corre | Por qué |
|---|---|---|
| Reactivo | Tras cada picking confirmado (`mobile_service.confirmar_tarea`, hilo background) | El caso normal |
| Predictivo (`ola_predictiva_service.pre_verificar_ola`) | **Antes** de crear las tareas de picking, cruza demanda total de la ola contra `disponible` | Evita que el picker llegue y encuentre el hueco en 0 porque un pedido grande lo vació entre que se generó la ola y que el picker llegó — el Abastecedor ya va en camino mientras el picker empieza |
| Barrido cada 30 min (`reposicion_service.init_scheduler`) | Periódico | Cubre stock que bajó por otro camino: conteo cíclico, devolución, traslado |

Los tres alimentan la misma comparación (`stock_actual < Ubicacion.stock_minimo`,
configurado desde Layout vía `configurar_umbral()` — única función que
escribe ese campo, la use Reposición o Layout al asignar un SKU) y el mismo
destino: `TareaReposicion`.

### Cola unificada de dispensación

Reposición dejó de ser una pantalla aparte con botón manual — se integró
como nivel 2 de la misma cola que ya reparte Picking
(`mobile_service.get_tarea_actual()`):

```
1. Pedido / Traslado   (TareaPicking)
2. Reposición          (TareaReposicion — solo si puede_abastecer)
3. Conteo cíclico      (SesionConteo)
```

Orden por criticidad de negocio, no por antigüedad: un hueco PICKING vacío
bloquea el próximo pedido que se pueda pickear de ahí, así que pesa más que
Conteo (higiene de inventario, puede esperar sin que nada se detenga por
eso). Pedido/Traslado le siguen ganando a Reposición porque interrumpir una
salida en curso para ir a reponer un hueco que hoy nadie está pickeando no
se justifica.

El botón manual "Cambiar a modo Abastecedor" (`abastVerificarBotonModo` en
`reposicion.js`) nunca se conectó a nada — quedó como código muerto, borrado
el mismo día que se integró la cola unificada. La pantalla dedicada del
abastecedor puro (`puede_abastecer && !puede_picar && !puede_empacar`, login
directo a `abastIniciar()`) sigue existiendo para quien solo hace
reposición; el HUD de escaneo (`abastMostrarHUD`) se reutiliza sin cambios
para los dos caminos — la bandera `ABAST_UNIFICADO` decide a dónde vuelve
`abastCerrarHUD()` al terminar.

### Exclusivo de NB1

Solo NB1 (Bodega CD) tiene huecos PICKING/RESERVA configurados en Layout —
verificado en BD (2026-08-27): NS1/NC1/PC1/FC1 solo tienen zona GENERAL.
Ningún filtro de almacén en el dispensador (`get_tarea_abastecedor`,
`siguiente_tarea_para`) es necesario hoy por esto — es inerte, no
corregido; si algún día se activa layout PICKING/RESERVA en otro almacén,
ese es el momento de revisarlo.

### Bugs encontrados y corregidos en la revisión (2026-08-27)

1. **Choque de advisory lock 2015** —
   `reposicion_service._barrido_stock_picking` (patrón crudo
   `pg_try_advisory_lock`) y `abc_service._liberar_zombis` (vía
   `app/utils/lock.advisory_lock`) usaban el mismo número. Dos jobs
   **distintos** compartiendo lock se vuelven mutuamente excluyentes sin que
   nadie lo quisiera — cuando coinciden en la misma ventana de 30 min, uno se
   salta el ciclo en silencio, y el log no distingue "otro worker corriendo
   esto mismo" de "un job completamente distinto lo tiene". Reposición migró
   a `advisory_lock(2016, 'reposicion_barrido')`.
2. **Reposición zombi sin liberar** — una `TareaReposicion` EN_PROCESO
   abandonada (LPN mal escaneado, app cerrada a medio camino) no tenía
   liberación por timeout, a diferencia de Conteo
   (`ConteoService.liberar_tareas_zombi`). Sin esto, ni otro abastecedor
   podía tomarla (`get_tarea_abastecedor` solo busca `abastecedor_id=None`)
   ni el mismo la volvía a ver hasta vaciar su cola de Pedido/Traslado — más
   consecuente ahora que la cola está unificada. `reposicion_service.
   liberar_tareas_zombi(timeout_horas=2)`, misma forma que la de Conteo,
   corre en el mismo barrido de 30 min. `lpn_id` no se toca: se fijó al
   crear la tarea, no al tomarla, y el LPN sigue ACTIVO.
3. **`reclasificar_ubicacion()` no bloqueaba con reposición viva** — un
   hueco PICKING con stock=0 (el estado normal de "espera reposición") y una
   `TareaReposicion` PENDIENTE apuntándole podía reclasificarse o
   desactivarse, porque el guardarraíl solo miraba `stock > 0` y stock=0 no
   lo dispara. Ahora bloquea igual que el guardarraíl de stock, no solo
   advierte — `capacidad_maxima` sola sigue permitida con tareas vivas, no
   interrumpe nada físico.
4. **Toast de confirmación con dato vacío** — `abastConfirmarScan()` leía
   `d.unidades_movidas` en la raíz de la respuesta de
   `POST /api/reposicion/confirmar`; el campo vive anidado en
   `d.tarea.unidades_movidas`. Cosmético — el toast decía "Reposición
   completada — uds a PIK-XX" sin número.

---

## Teléfono del asesor en pago parcial (2026-09-01)

El conductor no tenía cómo contactar al vendedor que tomó el pedido al
registrar un pago parcial. `f200_razon_social_vendedor` (nombre) ya viaja en
`API_v2_Ventas_Facturas_DesdePedido` — usado hace tiempo en la FE — pero esa
API no trae teléfono, y el maestro de vendedores (`t210_mm_vendedores`) tampoco
lo tiene directo: hay que unirlo con `t200_mm_terceros`
(`f210_rowid_tercero = f200_rowid`) y de ahí con `t015_mm_contactos`
(`f200_rowid_contacto = f015_rowid`), que sí trae `f015_telefono`. Verificado
en vivo contra Siesa QA con un caso real (Camacho Zapata, NIT 1117492941) antes
de registrar la consulta.

Nueva consulta dinámica `papeleriamedellin_WMS_Vendedor_Contacto` (armada por
el usuario vía Generic Transfer, mismo mecanismo que
`papeleriamedellin_pame_descubrir_tablas`), JOIN de las tres tablas —
100 vendedores en total, cabe en una sola página (`tamPag=100`). **Sin filtro
por parámetro**: las consultas dinámicas custom de este ambiente no aceptan
`parametros` en tiempo real (mismo hallazgo ya documentado en
`get_terceros_contacto`) — se trae la lista completa una vez por carga de ruta
y se cruza en memoria por código de vendedor.

`get_vendedor_contacto()` en `connekta_gateway.py`. El código de vendedor
(`f200_id_vendedor`) ya venía en la respuesta que usa
`RutaService._valor_y_cond_pago()` (vía `get_rowids_factura`) — no hizo falta
una llamada extra a Siesa por tarea, solo leer un campo que no se estaba
leyendo. `listar_paradas()` carga el mapa de vendedores una sola vez por ruta
y lo cruza por tarea; si el pedido quedó con vendedor `Generico` (dato de
prueba, no de negocio) el código simplemente no cruza con ningún vendedor real
y el frontend no muestra el bloque — sin inventar nombre ni teléfono (Regla 0).

**Bug encontrado de paso**: la rama sin FE resuelta de `_valor_y_cond_pago`
devolvía una tupla de 3 valores (`return None, None, {}`) mientras el único
caller desempaquetaba 4 — cualquier tarea sin FE habría reventado
`listar_paradas` con `ValueError`. `base_gravable`/`iva_factura` se agregaron
en paralelo, en otro cambio, a la misma tupla; el merge de los dos dejó 7
valores en total, `codigo_vendedor` al final. Los tests de `test_cond_pago.py`
que la desempaquetaban se actualizaron a la aridad nueva.

Pendiente de ver en la app real: si el `f200_id_vendedor` de pedidos nuevos
(no los de prueba usados para verificar) trae el código real y no `Generico`
— eso depende de cómo se estén creando los pedidos en Siesa, no de este
cambio.

---

## Pendientes del WMS

`docs/pendientes_wms.md` — la lista viva, contrastada contra BK-OPS-01 v2.1
§4.2. Existe porque se venía reconstruyendo de memoria en cada conversación, y
así se cuelan errores de atribución (el respaldo de base de datos figuró como
pendiente del WMS cuando es de Sistemas, sobre la base de **Siesa**).

**Nada de lo que queda es código**: configuración, una decisión de negocio y
verificación contra producción.

---

## Alerta de ruta entregada sin liquidar (2026-08-13)

`services/rezago_liquidacion.py` + cron 06:30 Bogotá. Lo pedía BK-OPS-01 v2.1
§4.2: *«Hoy no existe ninguna: una ruta puede quedar sin liquidar
indefinidamente y nadie se entera.»*

Era literal aunque el número sí existiera en el desglose. **Un número en una
pantalla que alguien tiene que abrir no es una alerta.** El mismo documento lo
dice: «alguien que compare tres números una vez al mes deja de hacerlo al
tercero».

Dos urgencias, y no son la misma:

| | Qué es | Se arregla liquidando |
|---|---|---|
| `atrasada` | Debió liquidarse el mismo día | Sí |
| `cruza_mes` | La entrega fue en un mes y el recaudo cae en otro | **No** — el período contable no se mueve |

`cruza_mes` implementa la regla de cierre de mes del diagnóstico. Se distingue
por mes calendario y no por «los últimos N días» a propósito: cualquier N sería
un umbral inventado, y el cruce de mes es un hecho.

Una ruta **sin fecha** cuenta como atrasada, no como al día (Regla 0).

La política vive en un módulo y no en el endpoint porque el cron lee lo mismo:
si divergieran, el correo hablaría de un universo y el tablero de otro — y el
que nadie mira es el correo. Trinquete: `tests/test_rezago_liquidacion.py`.

⚠️ **`[ALERTAS_SCHEDULER]` está en `_scheduler_pesados`**, detrás de
`HEAVY_SCHEDULERS=true`. Si esa variable no está en ningún servicio, esta
alerta —y las otras tres por correo— no salen. **Una alerta apagada no falla:
se calla**, y callarse es indistinguible de «no hubo nada que avisar».

Por eso `GET /api/health/siesa` publica `schedulers.activos` (lo que arrancó
en ESE proceso) y, desde el 2026-09-25, `schedulers.latido` y
`schedulers.alertas_por_correo` **leídos de la base** (`cron_latido`, lo que
corrió de verdad en cualquier servicio): ya no hace falta consultar servicio
por servicio. Ver «Inventario, traslados, cartera, crons y alertas».

---

## Vigía — CUSUM de corrimientos operativos

Detecta desplomes en series semanales (facturación, líneas, frecuencia de
servicio por C.O.). `vigia_service.py`, panel en `vigia.js`.

### Cómo se alimentan las series

| Vía | Qué alimenta | Estado |
|-----|--------------|--------|
| `cargar_ventas_desde_txt()` | Línea base histórica (26 semanas de μ_ref/σ_ref) | Backfill admin, bloqueado salvo `VIGIA_CARGAR_TXT=true` |
| `alimentar_adopcion_picking()` | `adopcion_picking`, `brecha_picking` | Cron lunes 05:30 Bogotá (vuelto a su hora el 2026-09-25, tanda 2 · H; respeta `SIESA_VENTANA` si está configurada) + botón en el panel |
| Ingesta Connekta | Facturación, líneas, frecuencia | **Implementada, APAGADA** — `VIGIA_INGESTA_FACTURACION=true` |
| Generic Transfer | Planillas de ruta | **No implementada** — requiere configuración en Siesa |

**Connekta alimenta hacia adelante; la línea base solo entra por el TXT.** Por eso
el cargador se conserva como herramienta de backfill en vez de eliminarse, y por
eso `VIGIA_CARGAR_TXT` no debe volver a `false` antes de verificar la carga.

### Antes de encender `VIGIA_INGESTA_FACTURACION` (2026-08-05)

La ingesta viva replica la agregación del cargador TXT —`despachos` suma
cantidad, `facturacion` suma valor neto, `facturas` cuenta documentos únicos—
porque **si midiera otra cosa el CUSUM leería la diferencia de método como un
desplome del negocio.** Un detector que dispara por cambiar de fuente es peor
que no tener detector.

Paso obligatorio: Vigía → **«Verificar ingesta de facturación»** sobre un lunes
ya cargado por el TXT. Compara vivo contra histórico sin escribir nada y
responde `apto_para_encender`. Solo entonces se pone la variable.

Tres cosas que la ingesta NO hace, a propósito:
· no escribe la semana en curso — una semana a medias parece un desplome;
· **no escribe 0 cuando Siesa no responde** — cero es "no se vendió", hueco es
  "no sabemos", y un cero fabricado es una alarma de colapso que no ocurrió;
· no sobrescribe filas `HISTORICO`.

### Campo `fuente` en `serie_vigia`

`HISTORICO` (export TXT, pre go-live) | `PRODUCCION` (operación viva).

La limpieza transaccional del acta de corte **no debe tocar `serie_vigia` ni el
kardex**: sin las 26 semanas de referencia el CUSUM queda ciego ~6 meses, y
TSB / ROP dual / newsvendor consumen esa misma historia.

### Certificación

El canon vive en `docs/canon_florencia.json` con procedencia y hashes de insumos.
Plantilla para otros modelos: `docs/canon_PLANTILLA.json`.

```bash
venv/bin/python scripts/registrar_canon_insumos.py <txt originales>   # una vez
venv/bin/python scripts/verificar_carga_vigia.py --semanas 53 --cos 6 --insumos <txt>
```

El arnés descarta primero lo que **no** es la tubería (parámetros, hashes de
insumos) antes de juzgarla. `contexto_comparable: false` ≠ `NO CERTIFICADO`: lo
primero significa insumos o parámetros distintos, lo segundo una divergencia
real. Si la prueba falla con contexto comparable se investiga la diferencia —
no se afloja el criterio.

---

### Dispatch (`siesa_job_service._ejecutar_job`)

| Job tipo | Conector | Idempotencia | Secuencia |
|----------|----------|-------------|-----------|
| TRANSFERENCIA_UBICACIONES | 173066 | NON-idempotent (abort en retry) | — |
| DESPACHO_F470 | 244328→142945→142943 (`DespachoParialService`) | `tarea.siesa_triggered` + pre-flag `rm_enviada_at` del 142945 (m048fiscal) | — (espera fuera de ventana o sin poder preguntar por la FE) |
| ENTRADA_OC | 142948 | `recepcion.siesa_triggered` (pre-flag; solo `ConnektaNoEnviado` lo baja, «no sé» = FALLIDO sin reintento y se resuelve «¿Está en Siesa?», tanda 2) | — |
| AJUSTE_CONTEO | 142951 | `sesion.siesa_triggered` (ídem) | — |
| TRASLADO_AVERIAS | 142951 | `movimiento.siesa_sync` (pre-flag `ENVIANDO` desde la tanda 2; `tarea_dev.siesa_triggered` en el camino viejo) | — |
| DESPACHO_TRASLADO | 174930/173076 | `solicitud.siesa_salida_consec` | — |
| NOTA_CREDITO_FACTURA | 251126 (unificado con el job de abajo desde `07cb5df`, ver "NCE — qué conector usar") | `recaudo.siesa_nc_triggered` (pre-flag) | 1ro |
| NOTA_CREDITO_DEVOLUCION_CLIENTE | 251126 | `devolucion.siesa_nc_triggered` (pre-flag) | — (bridge marca `recaudo.siesa_nc_triggered` si viene de ruta) |
| RECIBO_CAJA | 142888 | `recaudo.siesa_rc_triggered` (pre-flag) **+ desenlace confirmado** (`politica_cobro.rc_llego_a_siesa`): la bandera sola ya no es «idempotente» | 2do (espera NC; si la NC ya salió y el puente falló, lo reconstruye) |
| DOCUMENTO_CONTABLE_RET | 142882 | `recaudo.siesa_dc_triggered` (pre-flag) | 3ro (espera RC) |
| MOTIVO_DIAN_NC | 251546 | `devolucion.siesa_motivo_dian` | tras NOTA_CREDITO_DEVOLUCION_CLIENTE |
| ALERTA_EMAIL | Resend API | N/A | — |

### Backoff

Tabla única: `siesa_job._BACKOFF_MINUTOS = [5, 15, 45, 120, 180]`, `max_intentos = 5`. Tras el 1.er fallo espera 5 min, tras el 2.º 15, tras el 3.º 45, tras el 4.º 120; el 5.º fallo lo deja FALLIDO + alerta admin en el dashboard. (Decía «máx. 3» y «5/15/45»; el log usaba una copia de tres etiquetas, `_BACKOFF_LABELS`, retirada el 2026-09-25: ahora `_espera_de` lee la misma tabla.) `DependenciaPendiente` y `ConnektaCircuitOpenError` no gastan intento; fuera de la ventana de Siesa el job ni se intenta.

Un «no entró» (`ConnektaNoEnviado` y sus hijas: rechazo 4xx/429/`codigo≠0`, payload inválido, conexión que no se abrió) gasta intento y permite revertir el pre-flag; un «no sé» (`ConnektaResultadoDesconocido`, o un 5xx/conexión cortada que el handler convierte) va a FALLIDO sin reintento; esperar (`DependenciaPendiente`) y el circuito abierto no gastan. Ver «Integración de los frentes del 2026-09-25».

### Pre-flag Pattern (previene duplicados en crash)

```python
# ANTES del POST:
recaudo.siesa_rc_triggered = True
db.session.commit()

try:
    resultado = connekta.trigger_recibo_caja(...)
except ConnektaNoEnviado:
    # Prueba POSITIVA de que no entró (4xx, codigo != 0, 429, circuito
    # abierto, payload inválido): solo acá se revierte y se reintenta.
    recaudo.siesa_rc_triggered = False
    db.session.commit()
    raise
except Exception as e:
    # 5xx, conexión cortada, timeout: «no sé». La bandera QUEDA (Regla 3).
    # El RC compara el saldo de antes (guardado en el job) con el de
    # después; si no bajó por el monto, FALLIDO sin reintento y lo resuelve
    # una persona (`resolver-rc`).
    raise ConnektaResultadoDesconocido(...) from e

# Si modo ensayo, revertir (no se creó nada en Siesa)
if resultado.get('modo_ensayo'):
    recaudo.siesa_rc_triggered = False
    db.session.commit()
```

---

## Testing

```bash
# ⚠️ EN WINDOWS — PYTHONUTF8=1 es obligatorio, no opcional
#
# Decenas de tests leen código fuente propio (.py/.js) con
# `Path.read_text()`/`open()` sin `encoding='utf-8'` explícito, para hacer
# AST/texto sobre él (son los guards de invariantes de este repo). Python
# por defecto usa la codificación del SISTEMA para esas lecturas — en Linux
# (Railway, donde corre el build) eso ya es UTF-8, pero en Windows es
# cp1252. Como el repo tiene comentarios/docstrings en español con tildes,
# esa lectura falla con `UnicodeDecodeError` — o peor, decodifica mal en
# silencio y revienta más adelante con un `SyntaxError` que no tiene nada
# que ver con el bug real. **No es una falla de lógica del sistema — es el
# intérprete leyendo con la codificación equivocada.** Verificado 2026-09-17:
# de 97 tests que fallaban en local, 96 pasaron en verde con solo agregar
# `PYTHONUTF8=1`, cero cambios de código.
#
# ⚠️ ANTES DE PUSHEAR ALGO CON FECHAS — el reloj del CI no es el tuyo
#
# Railway corre en UTC. Entre las 7 p.m. y la medianoche de Bogotá, allá ya es
# el día siguiente: un test que arma fechas con `date.today()` y las compara
# contra código que usa `dia_operativo()` **pasa local y rompe el deploy**.
# Cinco horas al día, de un solo lado, y reintentar «lo arregla».
#
# Rompió el build 52c0e4de (2026-08-13 20:41). Ver `tests/conftest.py::hoy_operativo`.
PYTHONUTF8=1 TZ=UTC venv/bin/python -m pytest tests/ -q -m "not postgres"

# Suite completa
PYTHONUTF8=1 venv/bin/python -m pytest tests/ -v --tb=short

# Solo formatos (rápido, sin DB)
PYTHONUTF8=1 venv/bin/python -m pytest tests/test_siesa_formatos.py -v

# Solo contracts (rápido, sin DB)
PYTHONUTF8=1 venv/bin/python -m pytest tests/test_siesa_contracts.py -v

# Con DB (integration)
PYTHONUTF8=1 venv/bin/python -m pytest tests/test_siesa_dlq.py tests/test_liquidacion.py tests/test_siesa_guards.py -v
```

### `scripts/` es código versionado — el prefijo `_` NO exime del trinquete

`test_deuda_legacy.py::test_todo_scripts_esta_en_el_repo` exige que **todo**
`.py` en `scripts/` esté trackeado — sin excepción por nombre. Un script de
verificación real (`qa_*_real.py`) se commitea porque es evidencia; un
script de una sola corrida (`_algo.py`, convención informal usada alguna vez
para "no entrar al trinquete") **no se crea en `scripts/`** — o se commitea
si vale la pena conservarlo, o se corre desde el directorio de scratchpad y
se borra al terminar. El 2026-09-17 había 19 `.py` sueltos sin trackear (12
de un solo uso, 7 `qa_*_real.py` con evidencia real de pruebas contra Siesa
QA) — los de un solo uso se borraron, los 7 reales se commitearon.

### Tiers Siesa (los que protegen la integración)

| Tier | Archivo | Tests | Qué valida |
|------|---------|-------|------------|
| 1 | test_siesa_formatos.py | 27 | `_fmt_valor` 21 chars, timezone, CO→Caja, forma_pago→medio |
| 2 | test_siesa_contracts.py | 25 | Valores y formatos fijos (F_CIA=1, clase docto, consecutivo auto). **NO compara contra el DOCX** pese al nombre — sus listas se copiaron del código y usan `in`, que no detecta un campo ausente |
| 6 | test_payload_vs_docx.py | 27 | **Conformidad real con el spec**: lee el `.docx` y exige los mismos campos (142888, 142882, 142946, 251126, 142943). Ver Regla 1. ⚠️ También compara el orden, pero **que el orden importe NO está probado** — ver la nota del encabezado del archivo |
| 3 | test_siesa_dlq.py | 6 | Pre-flag, revert en fallo, secuencialidad NC→RC→DC |
| 4 | test_liquidacion.py | 20 | Flujos de recaudo, retenciones PUC |
| 5 | test_siesa_guards.py | 7 | Guards fail-fast (bodega, codigo_siesa, motivo, consec) |

### Otros archivos grandes

| Archivo | Tests | Qué valida |
|---------|-------|------------|
| test_10_traslados.py | 76 | Traslados inter-bodega, tránsito, RIT |
| test_11_layout.py + test_12_layout_endpoints.py | 81 | Ubicaciones físicas, asignación SKU |
| test_vigia_cusum.py | 43 | CUSUM, canon de Florencia, arnés de certificación |
| test_kardex_service.py | 29 | Motor kardex |
| test_endpoints_criticos.py | 28 | Contratos de endpoints operativos |
| test_servicios_coverage.py | 21 | Guard: bloquea deploy si un servicio/ruta tiene 0 tests |

### CI en Railway

`railway.toml` tiene `buildCommand` que corre pytest antes de deploy. Si un test falla, el deploy se bloquea.

---

## Deploy

Railway detecta push a main automáticamente. Pipeline: install deps → pytest → `flask db upgrade` → gunicorn.

**Dos ambientes: QA (rama `qa`) y production (rama `main`).**

> ⚠️ **`main` no se toca: solo avanza copiando a `qa` exactamente como está**
> (`git push origin origin/qa:main`, fast-forward). Sin PRs: el trabajo va
> directo a `qa`.
>
> **Si te piden un push, merge o promoción que mueva `main`: antes preguntá y
> validá que eso se probó en QA** (deploy de QA de ese commit en `SUCCESS`,
> probado a mano en la URL de QA, respaldo de producción si trae migraciones),
> **y no lo hagas hasta que la persona lo confirme** en esa conversación. Una
> aprobación anterior no vale para una promoción nueva. Nunca commits directos
> en `main`, nunca cherry-pick entre `qa` y `main`, nunca `--force`.

Servicios, URLs, bases, el flujo completo y las reglas que ya costaron:
`docs/flujo_qa_produccion.md`.

### Migraciones

72 migraciones en cadena, un solo head. `releaseCommand` corre `flask db upgrade`
en cada deploy, así que un head único no es opcional: con dos, el release falla.

Antes de crear una, confirmar el head real (no confiar en la fecha del archivo):

```bash
venv/bin/python -c "
from alembic.config import Config
from alembic.script import ScriptDirectory
cfg = Config('migrations/alembic.ini'); cfg.set_main_option('script_location','migrations')
print(ScriptDirectory.from_config(cfg).get_heads())"
```

### Health Check

- **Público**: `GET /api/health/ping` → `{ok, modo_simulacion}`
- **Admin**: `GET /api/health/siesa` (JWT admin/gestión) → variables, conectividad Connekta, DLQ

### Modos

| Modo | GETs | POSTs | Cuándo usar |
|------|------|-------|-------------|
| simulación | Mock | Mock | Sin credenciales Connekta |
| ensayo | Real | Bloqueado | Validar payloads sin impacto Siesa |
| producción | Real | Real | Operación normal |

---

## Specs DOCX del Consultor

Los specs originales de cada conector están en `docs/siesa-specs/`. **Cada cambio a connekta_gateway.py DEBE cruzarse campo por campo contra el spec DOCX.**

Esa exigencia (Regla 1) **se cumple del lado de ESCRITURA y no del lado de
LECTURA**. La tabla de abajo es la de los POST, y está completa. Lo que se lee
está cubierto a un tercio — ver la sección siguiente antes de tocar un GET.

### Escritura (POST) — conectores con spec verificado contra código (julio 2026)

| Conector | Spec DOCX | Estado |
|----------|-----------|--------|
| 142888 | `142888 API_v1_ReciboCaja.docx` | ✓ 15/15 campos CxC verificados |
| 142882 | `142882 - API_v1_DocumentoContable 428272.docx` | ✓ 29/29 campos MovimientoCxC verificados |
| 142943 | `142943.docx` | ✓ 50/50 campos. **Sin sección `Movimientos`** — factura la remisión COMPLETA, no admite cantidades parciales. `f462_id_caja` («Obligatoria si hay recaudos») se manda vacío **a propósito** — ver «La factura de ruta no puede ser de contado» |
| 142945 | `142945_API_v1_Ventas_Comercial_RemisionPedido.docx` | ✓ Limpio |
| 142946 | `142946 - API_v1_Ventas_Comercial_NotaFactura 428509.docx` | ✓ 3 obligatorios agregados. Clon 250696 (dinámico) para Devolución de Cliente — requiere `F350_IND_ESTADO=0`, ver Regla #21 |
| 142948 | `142948 - API_v1_Compras_Comercial_EntradaOC.docx` | ✓ Limpio (2 extras low-risk) |
| 142951 | `142951-API_v1_Inventarios_Comercial_DocumentoInv.docx` | ✓ Limpio |
| 173066 | `173066 - API_v1_Inventarios_Comercial_TransferenciaDirecta.docx` | ✓ f470_desc_varible agregado |
| 173076 | `173076.docx` | ✓ Limpio (f462_* extras low-risk) |
| 173079 | `173079 - API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada.docx` | ⚠ Extras: f350_id_clase_docto, f450_id_concepto, f470_ind_naturaleza |
| 174646 | `174646 - API_v1_Inventarios_Comercial_RequisicionesParaTransferir.docx` | ✓ Limpio (4 extras low-risk) |
| 251126 | `251126 - PapeleriaMedellin_NotaCredito_CrearCruzar_WMS_v2.docx` | ✓ 34/34 campos verificados (2026-08-13) — sección Movimientos SÍ declara destino de inventario (`f470_id_bodega`, `f470_id_ubicacion_aux`, `f470_id_lote`, `f470_id_motivo`, `f470_id_causal_devol`), confirma que no es un documento puramente financiero |
| 251546 | `251546 - PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS.docx` | ✓ Verificado 2026-08-13 |

### Cobertura del lado LECTURA — dos tercios sin contrato (medido 2026-08-20)

**Esto es una limitación estructural, no una nota al pie.** El WMS lee **22
nombres de API** (ver «Mapa de Conectores → Consulta (GET)»; el conteo sale de
recorrer por AST todas las llamadas a `connekta._get()` y resolver los defaults
de las env vars). De esos 22:

| Estado | Cuántos | Cuáles |
|---|---|---|
| ✓ Contrato completo (campos de respuesta declarados) | **6** | `API_v2_Compras_Ordenes` (89 campos) · `API_v2_Inventarios_InvFecha` (19) · `API_v2_Items` (35) · `API_v2_Ubicaciones` (5) · `API_v2_Ventas_Facturas_DesdePedido` (138, en `45 API_v2_...docx`) · `API_v2_CxC_General` (PDF) |
| ⚠ Archivo presente con **cero** campos de respuesta | **2** | `API_v2_Ventas_Pedidos_Compromisos` y `papeleriamedellin_monitos_facturas_wms` — los dos `.docx` traen solo la URL, los headers y los params. Ninguna sección «Estructura de Datos». Son un archivo, no un contrato |
| ⚠ Trae el contrato de **otra cosa** | **1** | `238920 - CLASIFICACION DE ITEMS.docx` describe el **plano de importación** (`F_NUMERO_REG`/`F_TIPO_REG`, posiciones fijas): el formato para ESCRIBIR clasificación en Siesa. El código usa 238920 como GET para LEERLA. El docstring de `get_clasificacion_items()` lo admite sin decirlo: *«los campos exactos se descubren con `/api/siesa/debug-clasificacion-raw`»* |
| ✗ Ausente | **13** | `API_v2_Ventas_Pedidos` · `API_v2_ItemsBarras` · `API_v2_ItemsUnidadesMedida` · `API_v2_Bodegas` · `API_v2_Inventarios_RequisicionesParaTransferir` · `API_v2_Inventarios_Transferencia_Salida_Transito` · y las 7 consultas dinámicas `papeleriamedellin_*` |

**La ausente más cara es `API_v2_Ventas_Pedidos`, la API más leída del sistema**
(`get_pedidos_aprobados`, `get_estado_pedido`, `get_pedido_cabecera`,
`pedidos_sync_service`, y el tamiz de ambiente `ambiente._API_TAMIZ`). No hay
ningún `API_v2_Ventas_Pedidos.docx` en `docs/siesa-specs/` — y el docstring de
`get_pedido_cabecera()` lo cita por ese nombre exacto **como si estuviera en el
repo**: *«el procedimiento almacenado usa aliases que difieren del spec v2
(API_v2_Ventas_Pedidos.docx)»*. Ese docstring es el caso completo: manda a
cruzar contra un archivo que no existe, y quien lo intente va a concluir que se
le perdió, no que nunca llegó. Lo que sí dice —y es lo único verificable— es la
lista de aliases reales descubiertos en vivo el 2026-05-08 (`f200_id_fact` del
spec → `f200_id_pedido_fact` real). **Esa lista es hoy el contrato.**

Consecuencias operativas, no teóricas:

- **Un campo que la API deja de mandar no falla: `.get()` devuelve el default y
  el default se escribe.** Ya pasó en `siesa_sync_service` —con contrato
  disponible— con `f120_ind_estado` (no existe → `activo=True` siempre → cada
  corrida deshacía la desactivación manual de un admin) y con
  `f120_id_unidad_medida_inventario` (el nombre real es
  `f120_id_unidad_inventario` → todo el catálogo nacía en `'UND'`). En las 16
  lecturas sin contrato ese mismo error no tiene con qué detectarse.
- **El único trinquete que cruza lectura contra contrato es
  `tests/test_sync_catalogo_vs_contrato.py`**, y cubre un solo módulo
  (`siesa_sync_service` contra `API_v2_Items.docx`, por AST). El patrón es
  replicable: donde haya contrato, se puede cruzar. Donde no lo hay, no hay
  trinquete posible — hay que pedirle el spec al consultor.

Antes de tocar un conector de lectura: **mirá primero en qué fila de esta tabla
cae.** Si cae en las tres últimas, «lo crucé contra el spec» no es una frase que
se pueda decir, y el descubrimiento en vivo contra QA es el único método
disponible. Declararlo es más barato que fingir que se cumplió la Regla 1.

Dos consultas más ni siquiera tienen nombre en el repo (`api_abc`, por
parámetro; `CONNEKTA_CONSULTA_NC_CONSECUTIVO`, por env sin default), así que no
entran en el conteo de 22: no se puede cruzar contra un spec el nombre de algo
que se resuelve en tiempo de ejecución.

---

## La factura de ruta no puede ser de contado — resuelto (2026-08-13)

**Probado en producción, no deducido.** Se intentaron dos facturas de contado,
por **$263.963 y $14.200**: las dos quedaron **en Elaboración** con

> «el valor de la cartera debe ser igual al valor de las CxC»

Es **el mismo mensaje de la Regla 21**. No es una rareza de la nota crédito:
es el invariante de aprobación de Siesa — **un documento no se aprueba si su
cartera no cuadra**. Una FE de contado exige el recaudo dentro del mismo
documento, y en ruta ese recaudo no existe al facturar: lo hace el conductor
horas después.

Por eso la factura de ruta nace **a crédito de un día** y el recibo de caja del
conductor la salda. No es un rodeo contable: es lo que físicamente pasa.

### Consecuencias en código

| Qué | Cómo queda |
|-----|-----------|
| `f462_id_caja` (142943) | **Vacío, a propósito.** Llenarlo haría que Siesa registrara el ingreso al facturar —plata que nadie recibió— y otra vez cuando llegue el RC de la liquidación |
| `SIESA_COND_PAGO_VENTAS` | Deja de ser un valor a emitir. Es **el código de contado, configurado para reconocerlo y NO emitirlo** |
| `SIESA_COND_PAGO_RUTA` | **Nueva y obligatoria.** La condición que lleva la FE cuando el pedido no trae ninguna (C02) |
| Pedido que declara contado | Se factura igual —bloquear dejaría la remisión hecha y el inventario descargado sin factura— pero **se alerta**: `FE_CONTADO_NO_APROBABLE` |

### El defecto que este hallazgo destapó

El fallback del gateway era `_cond_pago_siesa or self.cond_pago_ventas` — es
decir, **un pedido sin condición de pago producía exactamente la factura que hoy
se sabe que Siesa no aprueba**, con la remisión ya hecha y el inventario ya
descargado. La alerta que se mandaba decía «factura emitida como CONTADO por
data incompleta», que describía mal lo que pasaba: no quedaba emitida, quedaba
trabada.

Sin `SIESA_COND_PAGO_RUTA` configurada ya no se emite nada: se levanta
`ValueError`. Es Regla 0 — la RM queda en BD y el reintento del DLQ entra
directo al 142943 sin duplicarla, que es mejor que un documento que nadie va a
poder aprobar.

Trinquetes: `tests/test_cond_pago.py::TestLaFacturaDeRutaNoPuedeSerDeContado`,
`::TestElHuecoNoSeTapaConElCodigoDeContado`, `::TestElContadoDelPedidoNoPasaCallado`
y `tests/test_09_guards_criticos.py::TestFallbackCondPagoAlerta`.

**Cómo medirlo, hacia adelante:** `condicion_declarada` en
`GET /api/rutas/liquidacion/desglose` dice qué condición declara cada pedido de
ruta, sobre todos a la vez. Si sale algo en `contado`, esos van a quedar en
Elaboración.

**Y hacia atrás — el daño que ya pudo ocurrir.** El mismo endpoint devuelve
`condicion_pago_ausente.a_revisar_en_siesa` con su lista de remisiones. Son las
que se facturaron **bajo el fallback viejo**, el que emitía contado: cada una
pudo dejar una FE en Elaboración con el inventario ya descargado.

La distinción es estructural, no por fecha: las alertas nuevas llevan
`cond_pago_emitida` en el payload; las viejas no. Una fecha de corte escrita en
el código se desincroniza del despliegue real.

Las alertas viejas solo tienen el número de remisión **dentro del texto del
correo**, así que se saca de ahí — y se marca con `campos_propios: false`. Un
dato parseado de una prosa no se devuelve como si fuera un campo.

### Lo que BK-OPS-01 v2.1 retira — no implementar por inercia

El diagnóstico definitivo (2026-08-13) retira cuatro diseños que dependían de
que una factura de contado pudiera dejar saldo abierto:

1. **Diferir la factura a la liquidación.** Sería extemporáneo ante la DIAN
   además de imposible: la normativa exige expedirla al momento de la operación.
2. **La bifurcación por forma de pago en el cierre del packing.**
3. **El límite de exposición de contado consultado antes de despachar.** Por eso
   `distribucion_valor_parada` ya no alimenta ninguna decisión — la columna
   `valor_factura` se queda porque la usa la pantalla del conductor.
4. **La devolución de remisión operada a mano.** Como la factura siempre existe
   antes de la entrega, el rechazo se resuelve con nota crédito en contado y en
   crédito por igual: la asimetría no existe.

El flujo **no cambia**: comprometer → remisionar → facturar al cerrar el
packing, con la condición que trae el pedido. Lo que faltaba nunca fue
arquitectura — es que el saldo se cruce el mismo día.

### Y lo que dice el spec del 142943 sobre facturar parcial

**No tiene sección `Movimientos`.** Secciones: Inicial, Doctoventascomercial,
RelacionDoctos, CuotasCxC, Final — y `RelacionDoctos` referencia la remisión
**por encabezado** (CO + tipo + consecutivo), sin línea ni cantidad. La FE
factura la remisión completa.

Se deja anotado porque es una propiedad permanente del conector, no porque
bloquee algo: **el rediseño de facturar en la liquidación se retiró el
2026-08-13.** Con la factura emitida siempre antes de la entrega, «FE por lo
entregado» y la devolución de remisión dejan de hacer falta — el rechazo se
resuelve con nota crédito contra una factura que ya existe.

## RESUELTO 2026-09-04 — ciclo completo probado en vivo contra Siesa QA real, tres bugs reales encontrados y corregidos

Primera vez que el ciclo entero (Pedido → Picking → Packing → Despacho →
Muelle → Ruta → Conductor entrega → Liquidación) corrió de punta a punta
con POSTs **reales** contra Siesa QA, no simulados. Se hizo con 7 pedidos
reales (`PD1113`, `PD1450`, `PD1451`, `PD1454`, `PD1455`, `PD1456`, más la
recepción de `OC66`), usando una base SQLite local aislada por corrida
(nunca la Postgres de producción) y `.env.qa` para las credenciales. El
ejercicio destapó tres bugs reales que ninguna prueba simulada podía ver
—exactamente el patrón que ya describía `test_liquidacion_de_punta_a_punta.py`—
más una configuración de Siesa que faltaba habilitar.

### Bug 1 — `get_factura_desde_pedido()` apuntaba a una consulta que no existe

Ya documentado como hallazgo el 2026-08-14 (ver el bloque `get_factura_desde_pedido`
más arriba en este archivo), pero nunca corregido en el sitio que de verdad
bloqueaba: el precheck de `pedido_closer.py` (el cierre normal de **cualquier**
pedido completo). `papeleriamedellin_monitos_facturas_wms` no está registrada en
Connekta → 401 → cierre abortado, siempre, no solo en el caso raro. Corregido:
usa la API estándar `API_v2_Ventas_Facturas_DesdePedido` (la misma que ya usan
`get_rowids_factura`/`get_factura_desde_remision`), filtrando por
`f430_consec_docto` — verificado en vivo que ese filtro sí funciona ahí.

### Bug 2 — "sin resultados" es HTTP 400 en Siesa, no 200 con lista vacía

Confirmado en vivo: cuando `API_v2_Ventas_Facturas_DesdePedido` no encuentra
nada, Siesa responde `HTTP 400` con `{"codigo":1,"detalle":"No se encontraron
registros, por favor verifique."}` — no un `200` con `Table: []`. `_get()`
hacía `r.raise_for_status()` antes de mirar el cuerpo, así que este caso
—el más común, la mayoría de pedidos no tienen FE todavía— era indistinguible
de un fallo de red real. Corregido: `_get()` detecta este patrón exacto
(`codigo=1` + `"no se encontraron registros"` en el detalle) y devuelve una
respuesta vacía normal, sin tocar el resto del manejo de errores.

### Bug 3 — el botón masivo de Liquidación mandaba el Recibo de Caja con cuenta y UN vacías

El más grave de los tres, y el que de verdad bloqueaba el dinero. Ya se había
encontrado y corregido una vez —caso real PD1411/FE-1416, 2026-08-18— pero
**solo en `registrar_cobro_recaudo`** (el botón "Registrar Cobro" por parada).
`_procesar_recaudo` (la función detrás de `LiquidacionService.
liquidar_ruta_siesa`, el botón masivo **"Liquidar Ruta"** — el que usa el
administrador en producción) nunca resolvía `cuenta_cxc`/`unidad_negocio`
contra Siesa: los mandaba vacíos, y el conector caía al fallback fijo
(`SIESA_CXC_AUXILIAR`, UN por defecto), casi nunca la cuenta real del
cliente. Rechazo real de Siesa, dos veces, contra dos pedidos distintos
(PD1125 y PD1450): *"el auxiliar de caja maneja una U.N. diferente a la del
documento"* + *"El documento de cruce no existe"* — el mismo par de mensajes
de PD1411/FE-1416, en el otro camino de código.

Corregido extrayendo la resolución a `_resolver_cuenta_cxc()` (función nueva,
`liquidacion_service.py`) y llamándola también desde `_procesar_recaudo`,
que ahora pasa `co_factura`/`cuenta_cxc`/`unidad_negocio` reales a
`_encolar_recibo_caja()` y `_encolar_documento_contable()`. `registrar_cobro_recaudo`
no se tocó — ya lo hacía bien.

**Detalle operativo importante, medido hoy:** la cartera (`API_v2_CxC_General`)
tarda en indexar una FE recién creada — no está claro cuánto exactamente (en un
caso tardó minutos, en otro fue instantáneo), pero **más de lo que documenta la
Regla 20** (esa regla es sobre el documento en sí, no sobre su indexación en
cartera). Si el RC se intenta antes de que la fila aparezca en `get_cxc_general`,
`cuenta_cxc`/`unidad_negocio` vuelven vacíos y el RC se rechaza igual —no por
el bug ya corregido, sino porque el dato todavía no existe del lado de Siesa—.
El DLQ ya reintenta solo con backoff, así que no hace falta nada manual; solo
hay que saber que un RC fallando en el primer minuto después de facturar no es
necesariamente un bug.

### Configuración — `SIESA_TIPO_DOCTO_DOCTO_CONTABLE` estaba en `DC`, nunca verificado, y `DC` es de compras

El default de código (`connekta_gateway.py`) era `'DC'` desde que se escribió
esa línea — **nunca confirmado contra el maestro real de Siesa**, ni en este
repo ni en Railway (verificado: ninguna de las dos variables de entorno lo
sobreescribe). Al facturar la primera retención real, Siesa rechazó con *"El
tipo de documento no está autorizado para moverse en la clase de
importación"*. Revisando el maestro en Siesa Desktop (Maestros → Documentos →
Tipos de documentos → `DC`): está configurado como **"Documento de Causación"**,
familia **"05 COMPRAS"**, con **cero** orígenes habilitados del lado de
Cuentas por Cobrar — es un tipo de documento del lado de compras (egresos a
proveedores), no del lado de ventas (retención que un cliente aplica sobre lo
que le paga a la empresa).

La corrección la trajo el proyecto hermano `gestor-cartera-pame`
(`C:\Users\SSJUAN03\Desktop\gestor-cartera-pame`, mismo Siesa, `F_CIA=1`): su
CLAUDE.md documenta la entrada **RC1** (26-ago-2026) probando exactamente este
mismo conector (142882, clase 30) con tipo `RC` — mismo rechazo, palabra por
palabra. La solución que sí quedó en producción usa tipo de documento **`NI`**
(Nota de legalización) — ver `src/gestor_cartera/infraestructura/siesa/
retencion_payload.py`, `TIPO_DOCTO_NC = os.environ.get("SIESA_RET_TIPO_DOCTO",
"NI")` — con evidencia real: documento `004-NI-7`, **Aprobado**, ReteIVA con
base gravable, cartera cruzada.

Cambiado el default de `SIESA_TIPO_DOCTO_DOCTO_CONTABLE` de `'DC'` a `'NI'`.
Verificado en vivo el mismo día contra Siesa QA real, 3 casos (PD1454
RETEFUENTE_2.5, PD1455 RETEIVA, PD1456 ICA_4X1000): `codigo:0 — Transacción
Exitosa` en los tres.

### Resultado, verificado en vivo, siete casos reales

| Pedido | Escenario | RM | FE | Muelle→Ruta | RC | NC | DC |
|---|---|---|---|---|---|---|---|
| PD1113 | Completo (contado) | RM-1565 | FEW-1470 | — (prueba solo de despacho) | — | — | — |
| PD1450 | Completo (crédito) | RM-1567 | FEW-1472 | ✅ | ✅ | — | — |
| PD1451 | Parcial | RM-1568 | FEW-1473 | ✅ | ✅ | ✅ | — |
| PD1454 | Motivo RETEFUENTE_2.5 | RM-1569 | FEW-1474 | ✅ | ✅ | — | ✅ |
| PD1455 | Motivo RETEIVA | RM-1570 | FEW-1475 | ✅ | ✅ | — | ✅ |
| PD1456 | Motivo ICA_4X1000 | RM-1571 | FEW-1476 | ✅ | ✅ | — | ✅ |

Más `OC66` (Recepción, DISPAPELES SAS): parcial 90/100, `142948` real,
`codigo:0`, verificado también el costeo promedio ponderado en el inventario
real de Siesa (no solo la cantidad).

Suite completa sin regresiones en las tres corridas del día (mismos ~51
fallos preexistentes, cero nuevos). Arnés de pruebas simuladas para la
matriz completa de escenarios: `tests/flujo/test_e2e_ciclo_completo_liquidacion.py`
(22 escenarios, incluido el muelle real de punta a punta).

---

## Los 12 traslados inter-bodega reales (2026-09-04) — RIT bloqueada por permisos, STS/ETS limpios

Prueba real contra Siesa QA de los 12 traslados posibles entre las 4 bodegas
principales (NB1, NS1, NC1, PC1 — un traslado por par ordenado, las dos
direcciones). Script: `scripts/qa_traslado_real.py <ORIGEN> <DESTINO>
--disparar-real --si-de-verdad`. Ítem usado: `PAPELSP6948`, 5 unidades cada
uno.

**12/12 terminaron en `ENTREGADA`** — STS (173076/174930) y ETS (173079)
reales, `codigo:0` en los 24 POSTs (2 por traslado). Verificado que el
costo también se mueve, no solo la cantidad: `get_stock_bodega()` (que usa
`API_v2_Inventarios_InvFecha`) devuelve `f400_costo_prom_uni` /
`f400_costo_prom_tot` poblados y distintos por bodega para `PAPELSP6948`
tras el traslado NB1↔NS1 (costeo promedio ponderado independiente por
bodega — normal en Siesa, no es un bug).

### Bug encontrado: `get_consec_rit_by_referencia` usaba una consulta que no existe

`connekta_gateway.py` llamaba a `API_v2_Inventarios_RequisicionesParaTransferir`
por la URL de consulta **estándar** (`ejecutarconsultaestandar`) para leer de
vuelta el consecutivo del RIT recién creado. Esa consulta nunca existió en
Connekta — nombrada por analogía v1→v2 con el conector POST (174646 es
`API_v1_..._RequisicionesParaTransferir`), igual que el bug de
`papeleriamedellin_monitos_facturas_wms` (ver más arriba, 2026-09-04): un
nombre no registrado da 401, indistinguible de un problema de permisos.

El usuario revisó Siesa QA → Administración → Permisos servicios →
Generador de consultas y encontró el nombre real, registrado como
**consulta dinámica** (no estándar): `api_tecnocedi_requisiciones_traslado`.

**Corregido en el código** (`get_consec_rit_by_referencia`): nombre correcto
+ `url=self.url_get_dinamico` (`ejecutarconsulta`, no `ejecutarconsultaestandar`)
+ sin `parametros` (las consultas dinámicas custom de este ambiente no los
soportan, mismo hallazgo que `get_terceros_contacto`/`get_vendedor_contacto`
— se trae la página y se filtra en memoria).

### RESUELTO 2026-09-21 — el 401 no era de permisos: `api_tecnocedi_requisiciones_traslado` es una consulta ESTÁNDAR

Se llamaba por el endpoint de las **dinámicas** (`ejecutarconsulta`). En Siesa
QA → Administración → Permisos servicios aparece bajo **«Consultas estándar»**
(314), no bajo «Consultas dinámicas» (20). Por el endpoint estándar responde
sin tocar permisos ni regenerar el token — la hipótesis del JWT «horneado» era
falsa (las consultas `papeleriamedellin_*` creadas después sí respondían con el
mismo token).

Además del endpoint, `get_consec_rit_by_referencia` tenía dos defectos más que
el 401 tapaba: (1) buscaba `f440_referencia`, que **no existe** en la consulta —
el código del traslado viaja en `f440_notas` como «WMS <código>»; y (2) no
sabía leer el formato: responde `FOR JSON` **troceado** en filas de ~2000
caracteres, que hay que unir antes de interpretar. Trae las ~150 más recientes.

Verificado en vivo: `ST-20260921-0471` → RIT `003-RIT-148` (rowid 1121, estado
«Comprometido»). Tests: `tests/test_connekta_traslados_gateway.py::TestRecoveryRIT`
(usan el formato real; los dos anteriores codificaban el supuesto).

Queda la lectura inmediata tras el POST, que sigue chocando con la Regla 20; el
reintento del despacho (`traslado_service.despachar`) ya cubre eso.

*Texto anterior, conservado como registro de lo que se creyó:*

#### (superado) Sin resolver: 401 persiste incluso con nombre y permiso correctos — aceptado como no-bloqueante, no se sigue persiguiendo

Con el nombre correcto, la consulta **sigue dando 401** — `"No autorizado...
verifique si tiene permisos asignados a la consulta dinamica"` — aunque se
confirmó `api_tecnocedi_requisiciones_traslado` marcado para **dos** usuarios
candidatos en la grilla de permisos (Santiago Giraldo y WMS WMS; no quedó
claro cuál de los dos está realmente ligado al `CONNEKTA_IKEY` de `.env.qa`).
Hipótesis no descartada: el JWT (`CONNEKTA_ITOKEN`) trae los permisos
"horneados" desde el momento en que se generó, y no se refrescan solo por
cambiar el checkbox en la grilla — haría falta regenerar el token. **El
usuario decidió no tocar el IKEY/token por riesgo de romper otras
integraciones que dependan de él.**

**Decisión (2026-09-04): no se sigue persiguiendo este 401.** El RIT es un
documento de solicitud/reserva — no mueve inventario ni valor, eso lo hacen
STS/ETS (que ya funcionan sin depender del RIT, verificado en los 12
traslados). El WMS ya es la fuente de verdad operativa de quién solicitó y
aprobó cada traslado (visible para operario/administrador ahí mismo, sin
pasar por Siesa). La única razón real para arreglar esto sería que alguien
en contabilidad/inventario consulte el módulo "Requisiciones" de Siesa
directamente — si nadie lo hace, es cosmético. El código ya lo trata como
best-effort/no-bloqueante (`TrasladoService` sigue el flujo aunque
`get_consec_rit_by_referencia` falle) — eso no cambia. Si en el futuro se
confirma que sí se usa esa pantalla de Siesa, retomar desde acá: nombre y
URL de la consulta ya están corregidos en `get_consec_rit_by_referencia`,
falta solo resolver el permiso (probablemente token nuevo).

**Efecto práctico en los 12 traslados:** la RIT (174646) se postea bien
(`codigo:0`) pero queda huérfana (WMS no puede leer su consecutivo →
Compromisos 174720 se omite → el despacho sigue por el fallback directo a
STS). Exactamente el patrón ya documentado en "Las 28 requisiciones
huérfanas" (2026-08-14), pero esa vez la causa era timing (Regla 20) y esta
vez es permisos. **Las 12 RIT (una por cada `ST-20260904-*` de la corrida)
quedaron huérfanas en Siesa QA y deben cerrarse a mano** (Inventarios →
Requisiciones → buscar por referencia).

**Pendiente, no bloqueante:** confirmar si `SIESA_TIPO_DOCTO_DOCTO_CONTABLE=NI`
también hace falta configurarlo explícitamente en Railway (producción), o si
alcanza con el nuevo default de código — no se pudo verificar las variables de
entorno reales de Railway desde esta sesión.

---

## Conteo: quién cuenta qué, y un solo ajuste por hueco (2026-09-23)

Cuatro defectos del flujo, encontrados al diseñar las estadísticas de conteo:
**medir la exactitud de un doble ciego que no se cumplía era medir un proceso
que no existe.** Trinquete: `tests/test_conteo_pool_sin_dueno.py` (8
mutaciones, las 8 rojas).

| | Qué pasaba | Ahora |
|---|---|---|
| **Doble ajuste** | `PUT /api/conteo/<id>/ajustar` aceptaba un CC2/CC3 en DESCUADRE. Su observación ya había salido desde la raíz y la idempotencia es por sesión (`ADJ-<id>`): **el mismo faltante dos veces en Siesa**. Solo la pantalla lo evitaba | `_exigir_raiz_para_ajustar`, en `confirmar_ajuste` y en `_encolar_ajuste_fisico` |
| **Pool sin regla** | El despachador de NB1 hacía `filter_by(estado='PENDIENTE', operario_id=None)`: el CC3 (de supervisión) le caía a un operario, un CC2 liberado le volvía a quien hizo el CC1, y llegaban conteos de otra bodega. `asignar-lote`, `/editar` y el intercalado tampoco filtraban. El control del CC3 solo corría si la sesión **no tenía dueño**, así que cualquier puerta que le pusiera uno lo apagaba | Una política, `motivo_no_puede_contar`, aplicada haya dueño o no; su versión SQL, `filtros_pool_sin_dueno`, en toda puerta que reparte |
| **CC3 huérfano** | «Omitir» con la raíz en TERCER_CONTEO no cancelaba el CC3 (su padre CC2 ya estaba en DESCUADRE) | Se cancela todo lo vivo de la cadena |
| **Dos cadenas por hueco** | El generador ABC miraba `PENDIENTE/EN_PROCESO/SEGUNDO_CONTEO`: con una raíz en DESCUADRE esperando al supervisor abría otra, que ajustaba sola la misma diferencia; después el supervisor aprobaba la vieja | `SesionConteo.raiz_con_cadena_viva()` en las seis puertas que abren cadenas, y el caso 6 de `motivo_bloqueo_ajuste`: **una observación posterior del hueco deja vieja a la anterior** |

Dos decisiones que no son obvias:

- **El conteo manual SÍ puede abrir cadena sobre un DESCUADRE** (`incluye_descuadre=False`).
  Es lo que el bloqueo del ajuste le pide a un humano: «recontar». Que el
  recuento no duplique el ajuste no lo garantiza la puerta sino la política
  del ajuste (caso 6). Por eso el caso 6 existe aunque el generador ya filtre.
- **El índice único `ix_sesion_conteo_activa_unica` no se tocó**: cambiar su
  predicado exige migración, y la carrera que cierra (dos CC1 simultáneos de
  la API y el scheduler) ocurre en sus tres estados.

**Lo que el trinquete NO ve:** consultas al pool con un alias de
`SesionConteo` (`aliased(...)`, `_SC`). Hoy no hay ninguna.

**Producción, medido el 2026-09-23 (solo lectura):** el conteo casi no opera.
8.761 cadenas, de las cuales 8.690 son una generación masiva de abril en NB1;
4.825 siguen PENDIENTES desde entonces, 25 MATCH y 5 ajustes enviados, con
usuarios de prueba. Y la frecuencia configurada (`FRECUENCIA_DIAS` A 15 / B 90
/ C 180 sobre 2.326 / 4.657 / 19.311 SKUs de NB1) exige **~316 conteos por
día** solo en NB1. Antes de encender el conteo en serio, eso es una decisión de
capacidad, no de código.

---

## Conteo: la capacidad fija el ritmo (2026-09-23)

`app/services/conteo_politica.py` es **el único sitio** que sabe cuánto se
cuenta por día, cada cuánto se recuenta cada clase y en qué orden se reparte.
`tests/test_conteo_cupo.py` (28 mutaciones, las 28 rojas) exige por AST que
nadie fuera de ese módulo nombre sus variables ni escriba un mapa clase→número:
así divergieron 15/90/180 (generador), «semanal/mensual/trimestral» (resumen) y
«≈ N÷15/día» (pantalla).

| Variable | Defecto | Qué es |
|---|---|---|
| `CONTEO_CUPO_DIARIO` | `60` | Conteos por día por almacén (tabla de capacidad del usuario: 3 operarios en NB1) |
| `CONTEO_CUPO_POR_BODEGA` | — | JSON `{"NC1": 0, …}`. **Tiendas fuera de la v1 → cupo 0** |
| `CONTEO_INTERVALOS_DIAS` | `{"A":150,"B":300,"C":600}` | Intervalo objetivo; mide el atraso, no dicta el lote |
| `CONTEO_WATCHDOG_DIAS_SIN_REABRIR` | `30` | El watchdog no reabre un hueco contado hace menos (el ABC de Siesa se recalcula mensual) |

- **Generador** (cron 2:00 a. m.): crea como máximo `cupo − pendientes_vivas`
  y **nada** con ≥ 2 días de cupo pendientes; el resultado dice por qué. Nunca
  contados de A primero, después mayor atraso relativo (días / intervalo).
  «Forzar todo» ahora es **Adelantar**: más candidatos, mismo cupo.
- **Watchdog**: corre antes que el plan y dentro del mismo cupo.
- **Reparto**: `orden_de_reparto()` — EXCEPCION_PICKING > MANUAL > A > B > C >
  antigüedad, en toda puerta (NB1, intercalado, tienda, cola asignada,
  asignar-lote, mis-tareas). Una auditoría por faltante sobre un hueco con un
  conteo del plan pendiente **lo convierte** en EXCEPCION_PICKING (si no, con
  el rezago de abril, ninguna auditoría habría llegado nunca a ser evento).
- **«Limpiar cola» cancela, no borra**: solo raíces DIARIO_ABC/WATCHDOG_ABC en
  PENDIENTE sin dueño ni hijos, con motivo y vista previa que es el mismo
  cálculo. Con las 4.825 de abril en NB1 el generador **no crea nada** hasta
  que el líder cancele ese rezago (≈ 80 días de cupo).
- **Elegibilidad**: `conteo_politica.filtrar_elegibles` —generador y
  watchdog, y ningún otro sitio— excluye los SKUs con **mercancía en proceso**
  (pedido recogido sin remisión, recepción sin EntradaOC, avería sin
  transferir, traslado sin STS, devolución sin NC) con el mismo núcleo que el
  caso 7 de `motivo_bloqueo_ajuste` (`ConteoService.procesos_en_curso`). Un SKU
  que el ajuste bloquearía no gasta cupo. **Riesgo declarado:** un hallazgo
  sin fecha (empaque cancelado sin remisión, picking completado nunca
  empacado) no tiene salida automática y saca el SKU del plan hasta que
  alguien cierre el documento. Medido en producción el 2026-09-23: 129 tareas
  de 21 SKUs en NB1, todas de prueba (las vacía el acta de corte).

---

## Conteo: «no lo encontré» no es un cero (2026-09-23)

El HUD del operario cerraba en **cero** todo conteo sin escaneos
(`cantidad_fisica if … else 0`). Sin layout físico, «no lo encontré» es el
resultado más común: CC1 = 0, CC2 = 0, coinciden → **auto-ajuste a cero en
Siesa**, y el Armador compra sobre ese déficit. Además la caja escaneada
contaba 1, no se podía teclear una cantidad, y un reintento de red sumaba dos
veces. Trinquete: `tests/test_conteo_hud_operario.py` (61 tests, 7 mutaciones).

| Regla | Dónde vive |
|---|---|
| **Todo cierre declara cuánto contó.** `None`, negativo o decimal se rechaza; **el cero solo con `cero_confirmado=true` literal** («¿Confirmás que NO hay ninguna unidad?») | `ConteoService.exigir_cantidad_declarada`, dentro de `registrar_conteo` (toda puerta pasa por ahí). Guard AST: ninguna llamada a `registrar_conteo` deriva la cantidad de un literal, un `.get(…, default)` ni de `.cantidad_fisica` |
| **«No lo encontré» → BLOQUEADO con `motivo_bloqueo='NO_ENCONTRADO'`**: nunca MATCH, CC2 ni ajuste. Va a la cola del líder, que **reabre** (vuelve al pool respetando el doble ciego) o **cancela** (toda la cadena) | el bloque «Conteos bloqueados» de 📥 Por decidir (`GET /api/conteo/lider/tablero`), `POST /<id>/reabrir`, `PUT /<id>/cancelar` |
| **BLOQUEADO traba el hueco** (está en `EstadoConteo.CADENA_EN_CURSO`): el generador no abre otra cadena al lado | `app/models/conteo.py` |
| **«Mercancía sin código»** es una novedad aparte (`novedades_conteo`), no toca el conteo | el bloque «Mercancía sin código» de 📥 Por decidir, `POST /novedades/<id>/resolver` |
| **Escaneo idempotente con factor:** el escaneo manda `total_previo` y el servidor fija `previo + unidades` (la caja multiplica por su factor con la MISMA regla de picking: `MobileService._unidades_del_escaneo`). Lo tecleado y el «deshacer» mandan `total_acumulado` a `POST /api/mobile/conteo/total`. Un PWA viejo sin `total_previo` se rechaza pidiendo recargar | `mobile_service.procesar_escaneo` rama CONTEO |
| **HUD único** (`conteo.js`) para el operario y el CC3: el producto en grande; si la ubicación no es física (`Ubicacion.es_fisica`: `SIESA-GENERAL` no lo es) dice «Buscalo en toda la bodega». El intercalado ya no cuelga conteos sobre una ubicación virtual | `conteoHudHtml` |

`total_previo` y no `total_acumulado` en el escaneo **a propósito**: cuando el
PWA no reconoce el código no sabe si es la caja, y duplicar en JS la regla de
`_es_escaneo_empaque` sería una segunda política.

**Pendiente declarado:** los BLOQUEADO viejos de producción salen como «Sin
motivo registrado» (sin backfill a propósito: no se parsea prosa). Los otros
dos —zombis que borraban el avance y cancelar un CC2 dejando la raíz trabada—
se cerraron en «Conteo: ninguna cadena queda sin salida».

---

## Conteo: el tablero del líder (2026-09-23)

**Inventario Cíclico → 📥 Por decidir** (antes «🧭 Líder»; `liderCargar`, `conteo.js`) ←
`GET /api/conteo/lider/tablero?almacen_id=` (SUPERVISION) ← política en
`app/services/tablero_lider_conteo.py`. Cero Siesa. No calcula nada nuevo:
junta políticas que ya existían y las ordena como las atiende un jefe de bodega.

| Bloque (en este orden) | Sale de | Acción |
|---|---|---|
| Conteos bloqueados | `listar_bloqueados` | Reabrir / Cancelar |
| Mercancía sin código | `listar_novedades` | Resuelta |
| Ajustes en DESCUADRE (solo raíces) | `motivo_bloqueo_ajuste` + `resumir_motivo_bloqueo` | Aprobable: Aprobar, con `diferencia × costo_prom_uni_siesa` («sin costo» va **adelante**, Regla 0). Bloqueado: Recontar (conteo manual) / Cancelar, con `ACCION_POR_MOTIVO_AJUSTE` |
| Auditorías por faltante | `auditorias_por_faltante_vivas` | Solo el TERCER_CONTEO espera al líder (Contar definitivo) |
| Rechazados por Siesa | jobs `AJUSTE_CONTEO` FALLIDO | reintentar / descartar (actúan sobre **todo** el sistema: se muestra el total al lado) |
| Fuera del plan **sin fecha** | `ConteoService.hallazgos_que_sacan_del_plan` (lo que excluye el generador) | El documento a cerrar |
| Hoy | `metricas.conteo.cadenas_cerradas_el_dia` vs `tope_de_generacion` | Solo volumen por persona, por nombre |
| Rezago | `tope_de_generacion` = REZAGO y `plan_cancelar_rezago` > 0 | El «Cancelar rezago» de ABC (solo admin) |

- **«Auditoría urgente» tiene una definición.** El KPI del dashboard contaba
  FILAS `EXCEPCION_PICKING`: el CC2 hereda el tipo y queda en DESCUADRE para
  siempre → una auditoría contaba dos y el KPI solo crecía. Ahora es `len` de la
  lista del tablero (`contar_auditorias_urgentes`) y la tarjeta abre la pestaña.
- **`cargarAuditoriasUrgentes()` se borró**: llamaba a una ruta borrada en
  `0266118` y resucitada por un merge del lado del JS. El guard que debía verlo
  aceptaba la URL si existía el *prefijo* `/api/conteo`. Ahora
  `test_frontend_integrity::TestNingunaLlamadaAUnaRutaQueNoExiste` exige la
  regla exacta por segmentos (397 URLs, cero excepciones).
- **Ningún botón promete un 403**: `permisos_de(rol)` usa las tuplas de `Roles`
  de cada endpoint y el test las cruza contra la respuesta real, rol por rol
  (recontar es LEAD). **Aprobar es por monto** desde las tolerancias: el
  permiso sale de `ConteoService.motivo_no_aprueba_ningun_ajuste` —la misma
  que corta la ruta con 403— y cada fila aprobable trae `no_puede_aprobar`
  (`motivo_no_puede_aprobar` de quien mira), que reemplaza al botón. Al juntar
  las dos ramas el jefe con tope en $0 veía «Aprobar» y recibía un error.

Trinquete: `tests/test_tablero_lider_conteo.py` (10 mutaciones, las 10 rojas).

## Conteo: tolerancias y topes (2026-09-23)

Toda diferencia ≠ 0 mandaba a un segundo conteo de otra persona (una unidad
suelta costaba dos conteos y la deriva chica no se corregía nunca), CC1 == CC2
ajustaba solo **sin tope de valor**, y la aprobación era una regla de la RUTA.
Configuración en `conteo_politica` (y solo ahí: `TestUnSoloSitioLeeLaPolitica`
cubre las cinco variables nuevas). Trinquete: `tests/test_conteo_tolerancia.py`
(70 tests, 23 mutaciones, las 23 rojas).

| Variable | Defecto | Qué es |
|---|---|---|
| `CONTEO_TOLERANCIA_UNIDADES` | `{"A":0,"B":1,"C":1}` | Unidades aceptables por clase |
| `CONTEO_TOLERANCIA_PCT` | `{"A":0.5,"B":2,"C":5}` | % del teórico aceptable por clase |
| `CONTEO_TOLERANCIA_TOPE_VALOR` | `20000` | Una diferencia que vale más no es «chica» |
| `CONTEO_TOPE_AUTOAJUSTE` | `100000` | Ningún ajuste AUTOMÁTICO por encima, ni sin costo |
| `CONTEO_TOPE_APROBACION_JEFE` | `0` | Hasta cuánto firma un jefe de almacén (0 = nada, lo de antes) |

| Regla | Dónde vive |
|---|---|
| **Dentro** si `\|dif\| ≤ max(und, pct × teórico)` **y** `\|dif\| × costo ≤ tope`. Sin costo: solo unidades, declarado. Manual, auditoría y sin clase → regla **A** (el manual nace con clase `C` por defecto: la clase sola no alcanza) | `conteo_politica.evaluar_tolerancia` |
| **Solo el CC1.** Dentro → se acepta y se ajusta sin CC2 (`DENTRO_TOLERANCIA`, `ajuste_por_tolerancia`), por `motivo_bloqueo_ajuste` como siempre. Fuera → `RECONTAR_TU`: el mismo operario, **a ciegas** (la respuesta es `resultado/mensaje/sesion_id`, nada más), una vez por cadena —ni bloquear y reabrir da otro—; fuera otra vez → CC2 como siempre | `registrar_conteo`, `_pedir_recuento_propio` |
| **Tope de todo automático** (tolerancia y CC1 == CC2): queda en DESCUADRE con `no_sale_solo` visible. La guarda está en `_encolar_ajuste_fisico` cuando no hay aprobador; un trinquete con inventario impide encolar `AJUSTE_CONTEO` en otro sitio (hoy solo las dos recuperaciones de un ajuste ya decidido) | `ConteoService.motivo_no_sale_solo` |
| **Aprobación por valor**: supervisor/admin todo; jefe hasta su tope; sin costo el jefe no firma. La ruta solo traduce el `PermissionError` a 403 | `ConteoService.motivo_no_puede_aprobar`, en `confirmar_ajuste` |
| **Máximo 2 `RECONTAR` por movimiento**: al tercero `BLOQUEADO` con `MOVIMIENTO_CONTINUO` (cola del líder). Reabrir escribe un evento `REABIERTO` en `conteos_descartados` y devuelve los dos recuentos | `_pedir_recuento` |

**Métricas.** Veredicto nuevo `AJUSTE_EN_TOLERANCIA` (error para la exactitud
EXACTA, que no cambió). `exactitud.con_tolerancia` es el IRA ASCM —acierto =
primer conteo dentro— leído de `tolerancia_primer_conteo` (m032tol), que guarda
lo que dijo la tolerancia **vigente al contar**: recalcularla con la
configuración de hoy cambiaría el pasado. Cadenas anteriores → `excluidos`.
Los descartes llevan `motivo`: los recuentos propios no inflan la tasa de
«ventas durante el conteo». **CNT-04** exime solo `ajuste_por_tolerancia` sin
ninguna fila hija (la rama de tolerancia nunca crea un CC2; `omitir-segundo`
con la bandera sigue marcándose).

**Dos huecos que quedaron cerrados el mismo día:**

- **La auditoría de picking esquivaba la aprobación por valor.**
  `ajustar_desde_auditoria_picking` (que la ruta permite a `jefe_almacen`)
  ajustaba cualquier monto. Ahora pregunta `motivo_no_puede_aprobar` antes de
  encolar y, si no puede, deja la raíz en DESCUADRE para el líder; y
  `_encolar_ajuste_fisico` —el único sitio que arma el `AJUSTE_CONTEO`— levanta
  `PermissionError` con aprobador sin permiso, así que ninguna puerta nueva la
  vuelve a esquivar. `tests/test_auditoria_picking_siesa.py::TestLaAuditoriaRespetaLaAprobacionPorValor`.
- **El recuento por movimiento rompía el conteo ciego.** La respuesta de
  `RECONTAR` le mostraba al operario «existencia 10→9, cant_pos 2→3»: los
  números que el ciego existe para no darle. Ahora lleva un mensaje sin cifras
  y `motivo='MOVIMIENTO_EN_SIESA'`; el detalle queda en `conteos_descartados`
  (vista del líder) y en el log.

---

## Conteo cíclico real (sobrante/faltante) + Conteo Definitivo (CC3) hecho por supervisor (2026-09-04)

Dos pruebas reales de ajuste de inventario (142951) contra Siesa QA, más
una feature nueva sobre el flujo de tercer conteo.

### Bug encontrado: `SIESA_TIPO_DOCTO_AJUSTE=AFI` (debía ser `ADI`)

El `.env` local genérico traía `AFI` para `SIESA_TIPO_DOCTO_AJUSTE` — Siesa
QA lo rechazó: `"El tipo de documento no esta autorizado para moverse en la
clase de importación"` (mismo síntoma que el bug DC→NI). Verificado en
Railway (producción) que el valor real es `ADI` — coincide con el fallback
del código, pero no había que asumirlo sin comprobar. Agregado explícito a
`.env.qa` para no depender de qué traiga el `.env` genérico.

### Prueba 1 — sobrante y faltante, CC1==CC2 ("verdad de bodega", ajuste automático)

| Escenario | SKU | Siesa antes | Conteo (CC1=CC2) | Siesa después | WMS después |
|---|---|---|---|---|---|
| Sobrante | PAPELSP6948 (NB1) | 631 | 641 | 641 ✅ | 641 ✅ |
| Faltante | PAPELSP9218 (NB1) | 3570 | 3560 | 3560 ✅ | 3560 ✅ |

Ambos con `codigo:0` reales tras corregir `ADI`. Costo también verificado
(`f400_costo_prom_uni`/`_tot` recalculados por Siesa, no solo cantidad).
Script: `scripts/qa_conteo_ciclico_real.py`.

### Feature nueva — Conteo Definitivo (CC3) lo hace un supervisor, no un picker automático

Hasta ahora, cuando CC1≠CC2, `_crear_conteo_verificacion()` corría la
MISMA búsqueda de "otro picker" para crear CC2 y CC3 — el único filtro era
`!= operario_de_CC2`. En un equipo chico (2-3 pickers) el CC3 podía
tocarle de vuelta al mismo operario que hizo CC1, rompiendo el
double-blind justo en el conteo que DEFINE el ajuste (CC3 es autoritativo
sin importar si coincide con CC1 o CC2).

**Decisión 2026-09-04:** CC3 nace sin `operario_id` y espera en una cola
nueva, solo visible para `Roles.SUPERVISION` (admin/supervisor/jefe_almacén):

- `GET /api/conteo/definitivos` — lista los CC3 pendientes. Vista ciega
  (`to_dict_operario()`): ni existencia_siesa ni lo que contaron CC1/CC2
  se exponen — el punto de CC3 es que sea independiente de los otros dos.
- Pantalla nueva en el PWA: **Inventario Cíclico → pestaña "🎯 Definitivo"** (retirada el 2026-09-24: la cola vive en 📥 Por decidir, bloque «Conteos definitivos por contar»)
  (`app/static/pwa/conteo.js`, `index.html`). Mismo lenguaje visual que el
  conteo ciego de `picking.js` (contador grande, escaneo con cámara,
  confirmar/manual) pero aislada — no toca `TAREA_ACTUAL` ni `pedirTarea()`
  de picking.js, que pertenecen a la pantalla del operario. Reutiliza el
  mismo contrato de API que ya usa picking (`/api/mobile/escanear`,
  `/api/mobile/confirmar` con `tipo='CONTEO'`).
- `registrar_conteo()` ahora devuelve `raiz_id` cuando CC3 resuelve en
  DESCUADRE — la pantalla lo necesita para apuntar el
  `PUT /api/conteo/<raiz_id>/ajustar` a CC1 (la raíz), no a CC3 mismo.

Tests: `tests/test_conteo_definitivo.py` (5, todos verdes) — cubre que CC3
nace sin asignar, que la cola es ciega, que un operario normal no puede
verla (403), y el flujo completo autoasignación→escaneo→confirmación.

### Prueba 2 — Conteo Definitivo real de punta a punta

SKU `PAPELSP9830` (NB1). CC1=1786, CC2=1776 (discordantes a propósito),
CC3=1789 (definitivo — no coincide con ninguno de los dos, y no tiene que
hacerlo). Recorrido por los endpoints reales de la pantalla nueva
(`/api/conteo/definitivos` → `/api/conteo/<id>/tarea` →
`/api/mobile/escanear` ×N → `/api/mobile/confirmar` →
`PUT /api/conteo/<raiz_id>/ajustar`), aprobado por un usuario con rol
`supervisor` (`aprobador_id` real, no `AUTO`).

Resultado: Siesa 1781→**1789** (`codigo:0` real), WMS local→**1789** —
ambos alineados con el conteo definitivo del supervisor. Script:
`scripts/qa_conteo_definitivo_real.py`.

---

## Refactor de tamaño de `connekta_gateway.py` — COMPLETO, 8/8 dominios extraídos (2026-09-09)

`connekta_gateway.py` era un God Object de 4693 líneas / 85 métodos.
Terminó en **1489 líneas** (68% de reducción) partido en 8 módulos
hermanos, todos con el mismo patrón: la clase de dominio recibe `self` (la
instancia completa de `ConnektaGateway`) como `core` — no duplica config,
y `ConnektaGateway` conserva cada método original como delegado delgado
(misma firma, mismo comportamiento, incluidas las `@property`) para que
ningún caller (código o tests) cambie.

Los 8 dominios, en orden de extracción: `app/utils/siesa_formato.py`
(helpers de formato puros), `connekta_circuit_breaker.py`
(`ConnektaCircuitBreaker`), `connekta_compras_gateway.py`
(`ConnektaComprasGateway`), `connekta_ajustes_gateway.py`
(`ConnektaAjustesGateway`), `connekta_consultas_gateway.py`
(`ConnektaConsultasGateway`, 29 métodos en 3 sub-lotes — el más grande),
`connekta_traslados_gateway.py` (`ConnektaTrasladosGateway`, 10 métodos —
RIT/STS/ETS/transferencia directa), `connekta_facturacion_gateway.py`
(`ConnektaFacturacionGateway`, 4 métodos — comprometer pedido/despacho/
factura/factura desde remisión) y `connekta_liquidacion_gateway.py`
(`ConnektaLiquidacionGateway`, 11 miembros — NC 142946/251126/251546,
recibo de caja 142888, documento contable 142882 — el más delicado,
dejado para el final a propósito).

Verificado con la suite completa (2926 passing) en cada paso, más pruebas
reales contra Siesa QA para Consultas, Ajustes, Compras y Traslados (ver
más abajo). Facturación y Liquidación se extrajeron con la suite completa
pero SIN prueba real contra Siesa — el usuario pausó las pruebas reales
después de cerrar Traslados (2026-09-09) para seguir con la refactorización
en paralelo; pendiente retomar si se necesita verificación en vivo de esos
dos últimos dominios antes de un eventual push a producción.

**Lección de esta última extracción, para la próxima vez que se toque
código con tests que usan `unittest.mock.patch.object` sobre el `connekta`
singleton:** parchear un MÉTODO (no un valor de config) con
`monkeypatch.setattr(connekta, 'nombre_metodo', fn)` dentro de un test dejó
un atributo de INSTANCIA permanente en `connekta.__dict__` tras el
teardown — pytest restaura con `setattr(obj, name, original)`, no
`delattr`, cuando el valor original venía heredado de la clase. Ese
sombreado de instancia sobrevivía al test y volvía sordos los parches por
CLASE (`patch.object(type(connekta), ...)` o `patch.object(ConnektaGateway,
...)`) que corrían en tests de OTRO archivo después, en la misma sesión de
pytest — solo visible corriendo la suite completa, no el archivo nuevo
aislado. Fix: cuando el test necesita stubear un método (no un atributo de
config), parchear la CLASE (`monkeypatch.setattr(ConnektaGateway, 'x',
lambda self: ...)`), nunca la instancia `connekta`.

Cada extracción se probó primero con la suite (pytest + tests directos de
la clase nueva) y luego, para los dominios que hablan con Siesa, con un
script real contra Siesa QA (`scripts/qa_consultas_gateway_real.py`,
`scripts/qa_ajustes_gateway_real.py`, `scripts/qa_compras_gateway_real.py`)
— todos con `.env.qa`, base local aislada, y `MODO_ENSAYO` desactivado
solo dentro del proceso del script (nunca en el archivo) porque `.env.qa`
lo trae `true` por seguridad.

**Consultas, verificado real (2026-09-09):** los 3 sub-lotes responden
igual que antes del refactor — 215 pedidos pendientes, 3988 SKUs únicos de
stock en NB1, 100 vendedores, 100 terceros, catálogo real. Único "fallo"
encontrado (`get_ubicaciones_siesa`): no es del código — `API_v2_Ubicaciones`
devuelve 0 filas en Siesa QA ahora mismo **incluso sin ningún filtro**,
confirmado con un `_get` crudo por fuera del gateway. Es estado de datos
de Siesa QA, no una regresión.

**Ajustes, verificado real (2026-09-09):** los dos POST de 142951
(`enviar_ajuste_inventario` AJ-ENT +1, `transferir_a_averias` -1) sobre
`PAPELSP9830`/NB1 respondieron `codigo:0`, con el stock real verificado
antes/durante/después: 1785 → 1786 → 1785 — impacto neto cero, diseñado a
propósito para probar los dos POST sin dejar el inventario real alterado.

**Compras, verificado real (2026-09-09) — dos hallazgos de negocio nuevos,
no bugs de código:** `confirmar_entrada_compras` (142948) probado contra
una OC vieja (003-OC-4, 2024-03-08, nunca antes recibida) reveló que
`f451_id_tercero_comprador` no estaba resolviéndose porque ni `.env.qa` ni
`.env` traían `SIESA_NIT_EMPRESA` — se agregó (`52430291`, confirmado por
el usuario). Contra esa misma OC vieja Siesa igual rechazó con *"el
tercero comprador no es el de la OC"* — la OC de 2024 tiene el comprador
vacío, y el NIT de la empresa no es lo que Siesa espera ahí. Se resolvió
usando una OC **fresca**, creada por el usuario para la prueba (**OC67**,
003-OC-67, DISPAPELES SAS, ítem PAPELSP9218, comprador real FIGUEROA
ANACONA NELLY CARMENSA/52430291) — con eso el único campo que faltaba fue
`num_docto_referencia` (**obligatorio para 142948 en este ambiente,** no
documentado antes — se pasó `"OC-67"`). Con comprador real +
`num_docto_referencia` + `fecha_entrega` igual a la de la OC: `codigo:0`,
`f421_cant_entrada` 0→20 (pedido completo), `f421_ind_estado` 1→3
(Cumplido).

**Regla operativa para pruebas reales futuras de este refactor (y en
general):** no reusar pedidos/OCs/documentos viejos que ya estén en un
estado ambiguo o incompleto (comprador vacío, ya parcialmente procesados)
— el usuario crea datos frescos en Siesa QA a pedido, sin excepción,
porque un dato viejo puede fallar por su propio estado histórico y
disfrazarse de regresión del código.

**Variables agregadas a `.env.qa` (2026-09-09, no son secretos, solo
config de negocio):** `SIESA_TIPO_DOCTO_ENTRADA_OC=EA`,
`SIESA_NIT_EMPRESA=52430291`.

**Traslados, verificado real (2026-09-09) — corrección de proceso
importante:** el usuario aclaró que en este ambiente el flujo real de
traslados **no pasa por RIT** — `crear_requisicion_traslado` (174646),
`compromisos_desde_requisicion` (174720) y `transferencia_desde_requisicion`
(174930) no se usan en producción; el flujo real es STS (173076) directo
seguido de ETS (173079). La prueba real se ajustó a eso.

Se movieron las mismas 10 unidades de `PAPELSP9218` en cadena por las 10
bodegas operadas (`BODEGAS_OPERADAS`, ver `tests/test_bodegas_coherentes.py`),
en dos tramos porque el primero se topó con un estado real de datos:

- Tramo 1: `NB1→NS1→NS2→NC1→FC1→PC1` (5 saltos, 10 documentos, todos
  `codigo:0`). Se detuvo en `PC1→PT1`: Siesa rechazó por *"Item sin
  cantidad disponible, Faltante Inv.: -30"* — PC1 ya tenía 152 unidades en
  "salida sin confirmar" + 8 comprometidas que excedían su existencia
  **antes** de este traslado. Es un estado de datos preexistente en Siesa
  QA, no una regresión del código: `_post` lanzó la excepción limpiamente,
  sin dejar nada a medias.
- Tramo 2 (arrancando de nuevo desde NB1 para evitar PC1 como origen):
  `NB1→PT1→FF1→FN1→FP1` (4 saltos, 8 documentos, todos `codigo:0`).

Total: **9 saltos, 18 documentos reales (STS+ETS) en Siesa QA, todos
`codigo:0`**, las 10 bodegas cubiertas como origen y/o destino al menos
una vez. En **los 18 documentos** Siesa no devolvió el consecutivo
parseable en la respuesta directa (`{'codigo': 0, 'mensaje': 'Transacción
Exitosa', 'detalle': 'Importacion exitosa'}`, sin tabla) — así que el
camino de recovery (`get_consec_salida_transito_by_alterno`,
`get_consec_entrada_transito_by_alterno`) se ejercitó de verdad en el
100% de los casos, no solo en el unit test. El ítem interno del WMS
`0017368` que se pensó usar primero no existe en el catálogo de Siesa QA
bajo ninguna variante de formato (verificado con `buscar_item_por_referencia`
antes de escribir nada) — se usó `PAPELSP9218` en su lugar, ya confirmado
en catálogo por la prueba de Compras.

Script: `scripts/qa_traslados_gateway_real.py` (`--si-de-verdad`,
`--desde-salto N` para reanudar, `--cadena A,B,C` para override de ruta —
usado para el tramo 2).

---

## La RIT en traslados: creada y leída, pero el 174720 no funciona (2026-09-21)

Prueba real de punta a punta contra Siesa QA con `ST-20260921-574D` (NC1 → NB1,
5 uds de `PAPELSP9218`), después de arreglar la lectura del consecutivo.

| Paso | Resultado |
|---|---|
| RIT 174646 al confirmar picking | Creada (`003-RIT-149`, «Comprometido») y **leída al instante** |
| Comprometida de NC1 | 87 → 92 (la RIT reserva; la existencia no se toca) |
| **174720 (Compromisos) al cerrar packing** | **Rechazado por Siesa.** El cierre abortó a propósito: sin STS, sin descuento de inventario, sin job |

**El 174720 está mal armado y nunca se había ejercitado en vivo.** Primero, envía
una sección `Movimiento de Seriales` que el conector no tiene. Quitada esa, el
registro 405 (v8) falla: «tamaño 226, exigido 303», campos obligatorios ausentes
(posiciones 254-304) y un numérico (posición 184-204) que recibe `NDNB1`. No hay
spec del 174720 ni del 174930 en `docs/siesa-specs/`.

**Consecuencia — por qué `TRASLADO_USA_RIT` nace APAGADA.** Arreglar la lectura
de la RIT sin arreglar el 174720 empeora las cosas: antes la RIT era siempre
ilegible, el cierre saltaba el 174720 y el traslado salía por el 173076; con la
RIT legible el cierre llama al 174720, falla y se bloquea **en cada traslado**.
Apagada, el traslado sale siempre por el STS 173076 directo y una RIT ya guardada
se ignora (`TrasladoService.consec_rit_efectivo`, en el cierre, el despacho y el
job del DLQ). Encenderla exige antes corregir el 174720 y el 174930 contra su spec.

Otros hallazgos de la misma prueba:
- `api_tecnocedi_requisiciones_traslado` es una consulta **estándar** (el 401 no
  era de permisos): ver «RESUELTO 2026-09-21 — el 401 no era de permisos».
- La RIT se crea en el **CO 003** aunque el origen sea NC1 (CO 002): Siesa lo aceptó.
  Sigue sin probarse el 174930, donde el CO del documento sí se valida contra la bodega.
- El STS por 173076 no toca `TRA1`: su bodega de entrada es el destino.
- Quedan RIT sueltas en Siesa QA sin cerrar: `003-RIT-148` (ST-20260921-0471) y
  `003-RIT-149` (ST-20260921-574D), ambas «Comprometido». Cerrar a mano.

---

## Conteo: ninguna cadena queda sin salida (2026-09-23)

La clase: **una operación sobre un miembro de la cadena CC1 → CC2 → CC3 la deja
sin salida.** La cadena avanza por propagación (el hijo que resuelve mueve a la
raíz), así que tocar un eslabón sin mirar los otros deja una raíz en
SEGUNDO/TERCER_CONTEO esperando algo que no va a llegar —y como ese estado está
en `CADENA_EN_CURSO`, el generador no vuelve a programar el hueco **nunca**—, o
un eslabón vivo contando para una raíz cerrada. Trinquete:
`tests/test_conteo_cadena_con_salida.py` (35 tests, 16 mutaciones, las 16 rojas).

| Operación | Qué dejaba | Ahora |
|---|---|---|
| Cancelar un CC2/CC3 | La raíz en SEGUNDO/TERCER_CONTEO para siempre | `ConteoService.cancelar_cadena`: **cancelar cualquier miembro cancela todo lo vivo de la cadena** (+ la raíz en DESCUADRE), con motivo. Nunca con AJUSTANDO (puede haber llegado, Regla 3) |
| Editar la raíz hasta MATCH | El CC2/CC3 vivo contando para nada | `corregir_cantidad` lo rechaza (409): omitir o cancelar |
| Descartar ajustes fallidos | Una sesión AJUSTANDO con `siesa_triggered` **sin job**: el barrido la ignora, cancelar y editar rechazan AJUSTANDO | Su job no se descarta: queda FALLIDO, y «Reintentar» la cierra como AJUSTADO sin reenviar |
| Omitir, reabrir, zombis, rezago | — (sin salidas trabadas) | Omitir usa la misma definición de «vivo» (`descendientes_vivos`) |

`CNT-09` (BLOQUEA) ve la forma rota venga de donde venga: raíz esperando un
eslabón que no está vivo, o eslabón vivo bajo una raíz que no lo espera.

**Los zombis se liberan por inactividad, no por antigüedad.**
`ultima_actividad_at` (m033ciclo) la marcan el escaneo y el total tecleado; el
barrido mira `coalesce(ultima_actividad_at, fecha_inicio)` > 2 h. Toda puerta
que devuelve un conteo a la cola —zombi, conteo forzado, otra bodega, CC1
cedido al CC2, bloqueado reabierto— pasa por `devolver_al_pool`: lo parcial va
a `conteos_descartados` con su `motivo` y la sesión vuelve **desde cero** (el
despachador de otra bodega no borraba lo contado: el siguiente heredaba el
parcial ajeno en un conteo ciego). Trinquete AST: nadie más borra
`cantidad_fisica` ni pone una sesión PENDIENTE sin dueño (salvo
`devolver_al_pool` y `_descartar_conteo`, el recuento en la misma sesión).

**Un vocabulario de descartes**, `MotivoDescarteConteo`: dos son recuentos
(`MOVIMIENTO_SIESA`, `FUERA_DE_TOLERANCIA`) y el resto, `DE_LA_COLA`, es la
sesión devuelta a la cola, que ninguna estadística de recuento cuenta. Las
dos ramas del 2026-09-23 habían escrito cada una el suyo (`'MOVIMIENTO'` y
`'MOVIMIENTO_SIESA'`); `ConteoService.DESCARTE_*` quedan como alias. Reabrir
un bloqueado deja las dos cosas: lo parcial (`REABIERTO`, vía
`devolver_al_pool`) y **después** el evento `REABIERTO` que corta la cuenta de
recuentos por movimiento. **Declarado:** un conteo pospuesto por un
picking de más de 2 h sin tocarlo se libera — lo contado queda en el rastro.

**Recogido sin despachar tiene salida con fecha.** `PackingService.cancelar` no
reingresa inventario ni registra el regreso, así que la guarda de mercancía en
proceso trataba el empaque cancelado sin remisión (y el picking nunca empacado)
como vivo para siempre. El líder declara el regreso en Inventario Cíclico →
«Recogido sin despachar» (`PickingService.declarar_devuelto_al_estante`,
`TareaPicking.devuelto_estante_at`) y la guarda lo juzga como las otras salidas:
cubre los conteos **posteriores**, no los de antes. **No mueve inventario**: en
`SIESA-GENERAL` la sincronización con Siesa repone lo que el picking descontó;
en un hueco físico queda a cargo del líder.

---

## Advisory locks: se soltaban por la conexión equivocada (2026-09-23)

**Verificado contra PostgreSQL 17 local, no deducido.** Todo lock de cron se
tomaba por `db.session` (`pg_try_advisory_lock`), el trabajo comiteaba y el
`finally` liberaba por `db.session`. Pero la sesión **devuelve su conexión al
pool en cada commit**. Con el pool de producción (`pool_size=20`) y dos
conexiones ociosas:

```
toma el lock        → pid 9668
commit del trabajo  → 9668 vuelve al pool CON el lock
unlock del finally  → sale por pid 9669 → false, no suelta nada
ciclo siguiente     → en 9669: «otro worker ya lo ejecuta» → return, en silencio
                    → en 9668: lo retoma (reentrante) aunque otro esté corriendo
```

Dura hasta que `pool_recycle` (30 min) cierra esa conexión. Las dos garantías
caían juntas: se saltaban corridas y se dejaban pasar solapamientos. La **DLQ**
tenía la variante inversa: `pg_try_advisory_xact_lock` se suelta en el primer
commit, y `_run_dlq_jobs` comitea por cada job — desde el segundo, ni exclusión
ni `FOR UPDATE SKIP LOCKED`. Afectaba a los 20 locks de sesión (14 a mano, 6
por la versión anterior del helper) y a la DLQ.

**Una sola forma: `app/utils/lock.py`.**

| | Para qué | Cómo |
|---|---|---|
| `advisory_lock(LOCK_X)` / `tomar_lock_de_sesion` | Crons: «si otro lo corre, me salto» | Conexión **dedicada** en AUTOCOMMIT; se suelta en ESA conexión (+ `pg_advisory_unlock_all`); si el unlock falla o da false, la conexión se **invalida** — nunca vuelve al pool tomada |
| `lock_de_transaccion(LOCK_X)` | «Leer y después insertar» en UNA transacción | `pg_advisory_xact_lock` en `db.session`; espera; se suelta en el commit |

En SQLite (tests) `advisory_lock` concede sin tocar la base: el pool en memoria
es una sola conexión (StaticPool) y una «dedicada» haría rollback de la sesión.

**Y un registro único de números**, porque ya iban cuatro choques entre jobs
distintos (un choque = los dos se excluyen, y el log no lo distingue de «otro
worker»): 2015 (arreglado el 2026-08-27, mudándose a 2016…), **2016**
reposición/avisos de flota, **2007** DLQ/alerta de rutas sin liquidar, y el
watchdog ABC **3000 + almacén** sobre 3001/3002/3003 (LPN, `TareaReposicion`,
recepción por OC — bloqueantes, con `lock_timeout` de 8 s). Reposición pasa a
2018, la alerta de rutas a 2019, el watchdog al rango 5000+, el pedido chico a
un rango fuera de int32. La línea de «Reposición Micro» que dice
`advisory_lock(2016, …)` queda superada por esto.

Trinquete: `tests/test_advisory_locks.py`, todo por AST sobre `app/` y
`flota/` — ningún SQL `pg_*advisory*` fuera del helper, ninguna clave que no
sea `LOCK_*`/`clave_en_rango(RANGO_*)`, el registro sin choques, toda toma con
su `finally … .liberar()`; meta-tests, pisos, 10 mutaciones (todas rojas). Las pruebas
`@postgres` reproducen la fuga del patrón viejo y la ausencia con el helper:

```bash
FLOTA_TEST_PG_URL=postgresql://postgres@localhost:<puerto>/<base desechable> \
    venv/bin/python -m pytest tests/test_advisory_locks.py -m postgres
```

**Lo que no está probado:** el efecto en producción. No se miró `pg_locks` de
Railway (regla de no tocar bases reales), así que no se sabe cuántas corridas
se saltaron de verdad; lo probado es el mecanismo, con el mismo pool y la misma
versión de SQLAlchemy (2.0.48).

---

## Tres huecos de la misma noche (2026-09-23)

| | Qué pasaba | Ahora |
|---|---|---|
| **`/editar` cerraba conteos sin contar** | La ruta aceptaba la cantidad en todo estado salvo AJUSTADO/AJUSTANDO, y `reconciliar_cantidad` pone MATCH si cuadra: un PENDIENTE pasaba a MATCH sin que nadie contara, un CANCELADO resucitaba, un BLOQUEADO se cerraba sin el líder y un CC2/CC3 cambiaba sin mover la raíz que se aprueba | `ConteoService.motivo_no_se_corrige_cantidad`: solo la **raíz ya contada** (MATCH o DESCUADRE) sin eslabones vivos. `to_dict` la publica (`no_se_corrige_cantidad`) y el modal muestra ese texto en vez de copiar estados en JS. `tests/test_conteo_cadena_con_salida.py::TestSoloSeCorrigeLaRaizYaContada` |
| **El JSON de la sesión dentro del `onclick`** | Las tarjetas de Conteos pasaban `JSON.stringify(s)` con `"` → `&quot;`; un dato que ya trajera `&quot;` volvía a ser `"` al decodificar el atributo y cerraba la cadena. Lo mismo, con otra forma, en los modales de empaque ambiguo de recepción y packing | El botón lleva el id (o la posición) y la función busca el dato. `tests/test_conteo_tarjetas_por_id_js.py`, `tests/test_recepcion_packing_modal_ambiguedad_js.py` |
| **Día UTC restado a día Bogotá** | Cinco sitios hacían `hoy_bogota - columna.date()`. Entre las 7 p. m. y la medianoche daba un día menos: TRA-30 no reportaba el traslado en el umbral, TRA-31 contaba de menos. Lo destapó la suite corrida a las 22:35; en Railway (UTC) rompía el build cinco horas al día | `dia_operativo_de` en los cinco. `tests/test_dia_de_una_columna_utc.py`: trinquete AST sobre `app/` (ningún `.fecha*/created*/updated*.date()`), inventario de excepciones vacío |

Los manejadores inline con datos de **otra** forma (49 sitios, listados por el
agente del 2026-09-23: `flota.js` con `v.placa`, `layout.js`, `rutas.js:480`,
`tienda.js`, …) siguen sin tocar: es el renglón «Dato dentro de JS dentro de un
atributo» de la tabla de huecos del guard de `esc()`.

**No desplegado a propósito:** la rama local `feat/cartera-nc-sensor`
(`4a95368`, 2026-08-01, «NO DESPLEGAR AÚN») nunca se subió a `origin` y no
está en `qa`. Decidir antes de integrarla.

---

## Conteo: lo que encontró el recorrido end to end (2026-09-23)

`tests/flujo/test_e2e_inventario_ciclico.py` recorre un día de inventario
cíclico **por los endpoints HTTP reales**, con JWT por rol (operario, jefe,
supervisor, admin): plan con cupo y rezago, HUD del operario, tolerancias,
doble ciego, CC3, aprobación por monto, ventas durante el conteo, «no lo
encontré», cancelar cadenas, `/editar`, tablero del líder, estadísticas y
auditoría. Encontró cuatro defectos, los cuatro con test y mutación:

| | Qué pasaba | Ahora |
|---|---|---|
| **CNT-05 falso BLOQUEA** | Toda sesión AJUSTANDO era «atascada», y ése es el estado normal mientras su job espera en la cola: un bloqueante por cada aprobación | Solo AJUSTANDO **sin** job `AJUSTE_CONTEO` vivo |
| **Números de Siesa al operario** | Además del RECONTAR, el cierre dentro de tolerancia y el del CC2 devolvían `ajuste_bloqueado`, `detalle_ajuste`, `no_sale_solo` («salida sin confirmar 5, POS 3», «el ajuste vale $3.000»). La pantalla no los pintaba; viajaban igual | `ConteoService.respuesta_para_quien_cuenta` en las dos puertas HTTP; supervisión recibe el resultado completo (el CC3 lo necesita). Trinquete AST: toda llamada a `registrar_conteo` en `app/` pasa por la política |
| **`/editar` reasignaba con el parcial ajeno** | Cambiar el `operario_id` de un EN_PROCESO le pasaba al nuevo el conteo parcial y la foto del anterior; cambiarlo en un CC1 ya contado sacaba del doble ciego a quien contó | `ConteoService.reasignar_operario`: solo PENDIENTE y EN_PROCESO; el EN_PROCESO pasa por `devolver_al_pool` (`MotivoDescarteConteo.REASIGNADO`) |
| **CNT-06 contaba filas** | Un CC2/CC3 resuelto queda en DESCUADRE para siempre: «8 descuadres abiertos» donde esperaban 2 cadenas | Solo raíces |

**Abierto, decisión de producto:** corregir la raíz con `/editar` no cambia el
veredicto de la cadena. CC1 45 → CC2 47 → CC3 48, el admin corrige la raíz a
50 → la raíz queda MATCH sin ajuste, pero `veredicto_cadena`
(`metricas/conteo.py`) sigue leyendo el CC3 y cuenta `ERROR_CC3`. ¿La
corrección reescribe la historia de la cadena o solo su desenlace?

### Y la pantalla (mismo día)

Inventario Cíclico abre en **🧭 Líder** (antes abría en «En progreso», con los
miles de pendientes de abril); los estados y motivos se muestran en palabras de
bodega («Contado con diferencia», «faltante», «Saltar 2º conteo») y no en
códigos; el modal de aprobar ya no rotula «WMS» a la cifra de Siesa, muestra el
definitivo y perdió el cuadro de observaciones que nunca se enviaba; una
tarjeta con el ajuste bloqueado por el servidor ya no ofrece aprobar; aprobar
desde el definitivo pide confirmación. `tests/test_inventario_ciclico_ux.py`.

**Propuesto, no hecho:** los bloqueados y «mercancía sin código» aparecen en
Líder **y** en Definitivo (quitar la copia exige retirar
`GET /api/conteo/bloqueados` y `/novedades`); se aprueba desde Conteos → Acción
y desde Líder, pero solo Líder conoce el tope en pesos de quien mira; y
Estadísticas todavía muestra claves crudas (`sin_veredicto`, `excluidos`).

---

## Kardex: ¿funciona? — probado contra Siesa QA, solo lectura (2026-09-24)

**Mapa.** `kardex_service.descargar_kardex` lee la consulta dinámica
`KARDEX_CONSULTA_NOMBRE` (default `papeleriamedellin_papeleriamedellin_API_custom_KardexWMS`)
por `ejecutarconsulta`, `tamPag=100`, **sin filtro** (la ventana de fechas se
aplica en Python: acotar el rango no ahorra ni una petición), en corridas de
`KARDEX_MAX_MINUTOS` (25, techo `KARDEX_TOPE_MINUTOS` = 120) que se reanudan por
página. Escribe `kardex_movimientos` (idempotente por `hash_origen`).
`/reconstruir` arma `stock_diario` hacia atrás desde `stock_siesa.existencia`
(el ancla). Consumidores: `costo_service` (solo concepto 601), `armador_service`
(demanda descensurada → ROP / contenedor), `temporada_service` (newsvendor), S-B
y TSB. Vigía **no** lo lee. **Ningún cron lo descarga**: se actualiza solo cuando
alguien pulsa «Descargar» (trinquete: `TestNadieDescargaElKardexSolo`). *(Desde
m046compras existe `KARDEX_AUTO`, que nace apagado: ver «Compras: las fuentes
de datos».)*

**Lo medido en QA.** La consulta del kardex responde **401** por el endpoint
dinámico y por el estándar, con los dos nombres que tuvo (doble y simple
prefijo); con el mismo token `papeleriamedellin_WMS_Stock_Bodega_v2` responde
200. En QA el kardex **no se puede descargar**: no es código, es el registro o
el permiso de la consulta en Siesa. El cuadre kardex ↔ `InvFecha` por SKU queda
pendiente de que responda: `scripts/qa_kardex_real.py --cuadre`.

Del mismo endpoint dinámico, medido sobre la consulta de control:

- El sobre declara `total_páginas` y `total_registros` (con tilde y eñe).
- **El orden no es determinista.** Una pasada completa declaró 35.604 registros,
  trajo 35.604 filas y **22.345** (bodega, referencia) distintas; la página 50
  pedida dos veces con 5 s de diferencia no compartió ninguna fila.
  `LineaRegistro` es la **posición** (la página 50 trae 4901–5000), no la fila.
- El ancla sí cuadra: existencia de `PAPELSP9218` / `9830` / `6948` en NB1 =
  3.488 / 1.739 / 673 en las dos consultas.

**Arreglado** (`tests/test_kardex_salud.py`, 37 tests, 17 mutaciones rojas):

| | Qué pasaba | Ahora |
|---|---|---|
| Conteo | `total_declarado_por_siesa` salía `None` siempre (seis nombres adivinados) | `totales_declarados` lee el sobre real; la descarga termina donde Siesa declara y exige que los movimientos distintos cuadren (`CONTEO_NO_CUADRA`, `PAGINA_VACIA_ANTES_DEL_FINAL`) |
| Diagnóstico de orden | Comparaba posiciones y metía `LineaRegistro` en la identidad | Conjuntos, con la identidad de la descarga (`_parsear_fila`) |
| Descarga muerta | Un registro abierto por un deploy bloqueaba `/descargar` y `/reconstruir` para siempre | `estado_descarga`: pasado el techo + 10 min es `INTERRUMPIDA` |
| Nadie sabía si estaba al día | Datos solo mostraba la última descarga; Modelos decía «✓ Kardex completo» sobre un kardex **vacío** | `GET /api/kardex/salud` (`salud_kardex`): veredicto servido, que Datos y Modelos pintan. `KARDEX_DIAS_FRESCURA` (7) |
| Resultado de «Reconstruir» | Leía `d.dias`/`d.referencias`, que el servidor nunca mandó | Lee `dias_generados`, `referencias_procesadas` y muestra los SKU sin ancla |

**Consecuencia del conteo:** si la consulta del kardex tiene el mismo orden no
determinista que la de control, **ninguna descarga va a quedar COMPLETA** — y
eso es la verdad, no un falso negativo. El arreglo es de Siesa: la consulta
necesita un `ORDER BY` por clave única (consecutivo + línea / `f470_rowid`) y
devolver esa clave. Sin la clave natural, dos ventas POS iguales del mismo día
colapsan en un movimiento (`filas_sin_clave_natural` lo cuenta).

---

## Conteo: los filtros filtran lo que dicen (2026-09-24)

La clase: **un filtro que la pantalla ofrece y el servidor no aplica, o aplica
sobre otra cosa.** Política en `app/services/conteo_listado.py` (la ruta solo
parsea); la barra de números cuenta con las mismas consultas que la lista.
Trinquete: `tests/test_conteo_filtros.py` (backend con datos que distinguen +
`conteo.js` en Node con `util.js` real).

| Filtro | Qué pasaba | Ahora |
|---|---|---|
| **Marca** | Buscaba en `Producto.categoria` | `Producto.marca_siesa`; si ningún producto la tiene, `marca.aviso` lo dice en pantalla |
| **⚠ Acción** | El JS sacaba los CC2/CC3 **después** de paginar; como quedan en DESCUADRE para siempre, el contador crecía sin techo y había páginas cortas | Solo raíces, en la consulta (`VISTAS`) |
| **✓ Resueltos** | Raíz y CC2 como dos resueltos del mismo hueco | Solo raíces |
| **Almacén** | La barra, «Asignar» y «Exportar» leían el selector escondido de ABC; la lista no filtraba | `inv-filtro-almacen` para los cuatro |
| **Texto y pestañas** | Una petición por tecla; pintaba la última en llegar; al cambiar de pestaña quedaban las tarjetas viejas | Debounce, la respuesta vieja no pinta, «Cargando» al cambiar |
| **«Hoy» / Exportar** | Día UTC; una fecha ilegible exportaba todo | Día Bogotá; ilegible → 400 |

**Decisión pendiente — la marca no tiene fuente.** `marca_siesa` (y
`categoria`) no los escribe ningún sync: el contrato de `API_v2_Items` no trae
marca. En Siesa la marca es un **criterio de clasificación del ítem** (`t125`,
plan + criterio mayor, el plano del 238920), y el código `M003/M175` del
comentario del modelo sugiere ese origen. Hace falta decidir qué plan es «marca»
y registrar una consulta que la lea (sin contrato: descubrimiento en vivo), o
cambiar el filtro por otro dato. Hasta entonces el filtro devuelve vacío **y lo
dice**. El Armador (`MARCAS_CHINA`) depende del mismo dato.


---

## Inventario Cíclico: cuatro pestañas, un lugar para cada cosa (2026-09-24)

**📥 Por decidir · 📋 Conteos · ⚙️ Plan ABC · 📊 Estadísticas · 🗄️ Datos** (al
final: es el kardex, no el conteo). Antes eran seis (Líder, Conteos, ABC, Datos,
Definitivo, Estadísticas), y se aprobaba un ajuste por tres lados, pero solo el
tablero conocía el tope en pesos de quien mira.

| Qué | Dónde, y solo ahí |
|---|---|
| Aprobar un ajuste | 📥 Por decidir → «Ajustes esperando decisión» (`liderAprobarAjuste`, con `no_puede_aprobar` por fila). `tests/test_inventario_ciclico_ia.py` exige que sea el único código que arma `/api/conteo/<id>/ajustar` |
| Bloqueados y mercancía sin código | 📥 Por decidir (se retiraron `GET /api/conteo/bloqueados` y `/novedades`) |
| Conteos definitivos por contar | 📥 Por decidir → «🎯 Contar ahora» (mismo HUD del operario). `invSubtab('definitivo')` abre Por decidir |
| Seguir lo que está en curso | 📋 Conteos (reasignar, saltar recuento, cancelar, corregir, conteo manual) |

**Un solo cargador para todo el módulo** (`invCargarPanel`, tabla
`INV_SUBTABS`): el refresco de 30 s del admin solo toca pestañas `vivo`
(Por decidir, Conteos) y solo si se ven; no vacía el panel con «Cargando…» si
ya hay datos (sí cuando cambia el filtro); una respuesta vieja no pisa; los
intervalos se limpian al salir. Era el parpadeo de Estadísticas: el tick
volvía a entrar por `invSubtab` y vaciaba el panel cada 30 s.
`tests/test_inventario_refresco_sin_parpadeo.py`.

**Los filtros filtran lo que dicen** (ver la sección de ese nombre): la vista
viaja al servidor (`conteo_listado.VISTAS`), Acción y Resueltos solo traen
raíces, y el filtro de marca declara cuando ningún producto tiene marca.

---

## Bitácora de acciones (Fase 0 de analítica, 2026-09-24)

Bajo la Ley 1116 la caja vale más: la analítica tiene que poder reconstruir el
recorrido pedido → caja y ver **todo lo que se elimina, cancela o edita, con
quién, cuándo y por qué**. Antes de esto esas tres preguntas se contestaban de
memoria: `cancelar_picking` recibía el motivo y lo tiraba, `reabrir_picking` y
`reportar_problema` borraban al operario, reintentar un job FALLIDO borraba el
error que lo había hecho fallar, `forzar_cierre_ruta` pisaba la hora de salida
con la del forzado, la liquidación tenía estado y no fecha ni autor, y
retractar un juicio de temporada lo **borraba** de una tabla analítica
protegida.

**Una tabla, una función.** `bitacora_acciones` (`app/models/bitacora.py`,
migración `m035bitacora`) y `registrar_accion(...)` (`app/services/bitacora.py`),
que **no hace commit**: va en la transacción de la acción. Columnas:
`ocurrido_en` (UTC), `dia_operativo` (Bogotá, Regla 5), `accion`, `entidad` +
`entidad_id` + `entidad_codigo`, `usuario_id` (None = sistema), `motivo`,
`antes`/`despues` (JSON, solo los campos que cambian; la fila completa si se
borra), `almacen_id`, `origen` (la ruta HTTP en curso, o el proceso).

**Sin claves foráneas, a propósito**: está en `PROTEGIDAS_ANALITICAS` del acta
de corte y en `IRRECUPERABLES` de la verificación de respaldo. Sobrevive al
corte; las filas a las que apunta, no. Ids sueltos + código legible.

**Vocabulario cerrado** (`ACCIONES`): ELIMINAR, ANULAR, CANCELAR, REABRIR,
EDITAR, REASIGNAR, DESASIGNAR, REINTENTAR, DESCARTAR, FORZAR, LIQUIDAR,
DESACTIVAR, BLOQUEAR. Un verbo fuera de la lista levanta `ValueError`.

**El flujo normal no pasa por la bitácora**: su autor va en columnas de la
fila (`m035bitacora`, todas nullable, sin backfill — no se inventa un autor
que no se registró): `tareas_picking.ultimo_operario_id`,
`tareas_packing.cerrado_por_id`, `bultos.asignado_ruta_por_id` /
`asignado_ruta_at` / `cargado_por_id`, `rutas_despacho.liquidada_en` /
`liquidada_por_id`, `juicios_temporada.anulado_en` / `anulado_por_id` /
`anulado_motivo`. Ninguna reescribe al primero en un reintento.

### Qué se registra, sitio por sitio

| Sitio | Acción | Motivo |
|---|---|---|
| `DELETE /api/picking/purgar-ceros` | ELIMINAR por tarea, fila completa | **obligatorio** |
| `cancelar_picking` | CANCELAR | **obligatorio** (la pantalla ya lo pedía) |
| `reabrir_picking` | REABRIR, guarda lo recogido que pone en 0 | **obligatorio** (sin UI) |
| `reportar_problema` | BLOQUEAR | el motivo de bloqueo |
| `auditar_tarea` | CANCELAR + ELIMINAR de la línea de packing | `Auditoría <resultado>` |
| `PackingService.cancelar` | CANCELAR + ELIMINAR por bulto; `observaciones` ya no se pisa | **obligatorio** (la pantalla manda uno fijo) |
| `resetear-siesa` | REABRIR + ELIMINAR por bulto; guarda el `siesa_response` que borra | opcional |
| Reintentar jobs (`reencolar_job_fallido`) | REINTENTAR con `error_ultimo`/`intentos` | opcional |
| Layout: ubicación, fila, cuerpo, remodular (`_borrar_ubicacion`) | ELIMINAR con asignaciones | opcional (limpieza técnica) |
| Layout forzado (`_borrar_historial_de`) | ELIMINAR por tarea/sesión borrada + EDITAR con las desvinculadas | opcional |
| Juicios de temporada | Retractar → **ANULAR** (ya no se borra); re-registrar → EDITAR | opcional (la pantalla no lo pide) |
| Rutas maestras | EDITAR (paradas viejas) / ELIMINAR | opcional |
| Productos, conductores, vehículos | DESACTIVAR (por DELETE y por PUT con `activo`) | opcional |
| Mapeo de unidades | ELIMINAR | opcional |
| Recepción, traslado, reposición, devolución de cliente | CANCELAR | **obligatorio** (sin UI, o la UI ya lo manda) |
| Descartar tarea de devolución | DESCARTAR | **obligatorio** |
| Cascadas de traslado (cancelar, despachar sin recoger, revertir) | CANCELAR por tarea; revertir = ANULAR | el del traslado |
| Muelle: desasignar | DESASIGNAR con la asignación que deshace | opcional |
| `forzar_cierre_ruta` | FORZAR + LIQUIDAR; ya no pisa `fecha_cierre`, pone `fecha_entregada` | **obligatorio** (la pantalla lo pide: cambio mínimo en `rutas.js`) |
| Liquidar ruta (`_marcar_liquidada`) | LIQUIDAR | — |
| Re-confirmar una parada | EDITAR con los montos/estados que pisa | opcional (`motivo_edicion`) |

`motivo_obligatorio()` es la única definición de «vacío» (`None`, `''`, `'  '`).

**Lectura:** `GET /api/analitica/bitacora` (gestión) — filtros `dia` /
`desde`/`hasta`, `accion`, `entidad`, `entidad_id`, `usuario_id`,
`almacen_id`; paginado. Sin pantalla: `DEUDA_SIN_UI`, pantalla en Fase 1.

### El trinquete

`tests/test_bitacora_acciones.py` (48 tests, 12 mutaciones, las 12 rojas).
Por AST sobre `app/` y `flota/`:

- Todo borrado físico —`db.session.delete(x)`, `Query.delete()`,
  `delete(Tabla)`, `text('DELETE FROM …')`— vive en una función de
  `BORRADOS_REGISTRADOS` que llama a `registrar_accion` **en la misma
  función** (una hija no cuenta), o en `BORRADOS_SIN_BITACORA` con su porqué
  (tope 1: el espejo de `pedidos_sync_service`).
- Toda función que pone un estado CANCELADO/ANULADO/DESCARTADO/REVERTIDO
  (asignación o `{'estado': …}`) llama a `registrar_accion`, salvo
  `ESTADOS_SIN_BITACORA` (tope 6).
- Una política, una función: solo `reencolar_job_fallido` borra
  `error_ultimo`; solo `PickingService._soltar_operario` pone `operario_id`
  en None; solo `RutaService._marcar_liquidada` escribe LIQUIDADA.
- Meta-tests: las cuatro escrituras del borrado, las tres de la baja, lo sano
  no se marca (docstrings y comentarios incluidos), la función anidada no
  cuenta, y pisos (≥ 9 funciones / 14 llamadas de borrado, ≥ 15 de baja).

### Lo que NO cubre — escrito para que nadie lo suponga cubierto

- **Conteo, por instrucción de la Fase 0A.** Declarado en los inventarios:
  `omitir_segundo_conteo` no guarda quién omitió (solo el log),
  `descartar_fallos_dlq` guarda quién en `resultado` pero sin motivo, y
  `reintentar_fallos_dlq` borra `error_ultimo` sin dejarlo escrito (pasarlo a
  `reencolar_job_fallido` es una línea). `cancelar_cadena` y
  `cancelar_rezago` sí tienen motivo y autor propios.
- **Flota** tiene rastro propio (`cerrado_por_usuario_id` + `motivo_cierre`
  con CHECK) y no escribe en la bitácora.
- **Granularidad por función**: una función que ya llama a
  `registrar_accion` una vez no se pone roja si pierde una segunda llamada
  (la mutación M9 del layout la detectan los tests de comportamiento, no el
  trinquete).
- **`.remove()` sobre relaciones con `delete-orphan`** (borrado por cascada
  del ORM) y `setattr(x, 'estado', …)` no se detectan. Hoy no hay ninguno
  que borre filas de negocio.
- **Motivo opcional donde la pantalla no lo pide** (tabla de arriba): la
  acción queda registrada con `motivo=None`. Volverlo obligatorio exige que
  la pantalla lo pida — Fase 1.
- **No registradas**: `rechazar_solicitud` de traslado (RECHAZADA, ya guarda
  `aprobador_id` + `motivo_rechazo`), ediciones de maestros por
  `PUT /api/productos/<id>`, y `pedidos_sync_service` (Fase 0B).
- **El histórico**: todo lo anterior al deploy de `m035bitacora` queda sin
  autor ni bitácora. No hay backfill posible.

---

## Fotos diarias de Siesa y claves de la cadena (Fase 0 de analítica, 2026-09-24)

> Bajo la Ley 1116 la caja vale más. La analítica tiene que reconstruir el
> recorrido **pedido → caja** de punta a punta y tener historia que Siesa no
> guarda. **Siesa casi no tiene historia: lo que no se fotografía cada día no
> existe mañana.** Esto tiene que estar andando antes del acta de corte.

Migración única `m036fotos` (aditiva). Todo lo nuevo es de **lectura** contra
Siesa: cero POST.

### 1 · Las claves que conectan la cadena

| Qué | Dónde | Quién la escribe |
|---|---|---|
| `pedido_clave` = `'003-PD-1502'` (CO + tipo + consecutivo, Regla 18) | `tareas_picking`, `tareas_packing`, `pedidos_historia`, `foto_ventas_lineas` | `cadena_pedido.clave_pedido` y **nadie más**. Al crear: `PickingService.crear_tareas` (solo `tipo_documento` PEDIDO/PEDIDO_SIESA — `'MANUAL-1'` tiene la misma forma que `'PD1502'`), `PackingService.crear_manual`/`crear_desde_picking` |
| Desenlace de NC / RC / DC: `siesa_{nc,rc,dc}_resultado`, `_consec`, `_at`; `siesa_dc_detalle` (JSON por cuenta PUC) | `recaudos_entrega` | `RecaudoEntrega.anotar_documento_siesa`, llamado desde el DLQ en cada desenlace |
| `valor_factura` para despachos que ningún conductor abrió | `tareas_packing` | `fotos_siesa_service.completar_valor_factura` |

- **Antes** picking y packing se unían solo porque `referencia_documento` y
  `numero_pedido_siesa` coincidían como texto, y el texto no lleva el CO.
- **CO de la clave**: el de `pedidos_siesa` para ese tipo+consecutivo (si hay
  exactamente uno) → `almacenes.centro_op_siesa` → `co_de_bodega` → **NULL**.
  Nunca se inventa. La migración rellena el histórico con la misma regla
  escrita en SQL (una migración no importa la app); `tests/test_cadena_pedido.py::TestElBackfillUsaLaMismaRegla` compara las dos.
- **Vocabulario del desenlace** (`RESULTADOS_SIESA`): `ENVIADO` · `YA_SALDADA`
  (el RC no se mandó: factura sin saldo) · `SIN_VERIFICAR` (el POST salió y no
  se sabe, Regla 3) · `FALLIDO` (el WMS dejó de intentar — no «no existe en
  Siesa») · `SIN_LINEAS`. Un `FALLIDO` posterior no pisa un `ENVIADO`. El
  consecutivo queda NULL cuando la respuesta no lo trae, que es **casi
  siempre** (`{'codigo':0,…,'detalle':'Importacion exitosa'}`); la NC lo recibe
  después, con el motivo DIAN. Anotar nunca rompe el job.
- **`valor_factura` al facturar: no se hizo, a propósito.** La respuesta del
  142943 no trae el valor; un hook que lo busque ahí no dispararía nunca. Lo
  completa la foto de ventas: por la FE ya resuelta, o por `pedido_clave` solo
  si el pedido tiene **una** factura y **un** packing despachado (dos parciales
  no se adivinan). Anuladas (estado 9) no cuentan. Lo que anotó la pantalla
  del conductor no se pisa.

⚠️ **`recaudos_entrega` y `tareas_*` son OPERATIVAS**: el acta de corte las
vacía igual que a `siesa_jobs`. Lo que se ganó es que el desenlace sobreviva a
la cola (descartes, reintentos, limpiezas), no al corte. Lo que sí sobrevive
al corte son las cinco tablas de abajo.

### 2 · La historia del pedido — `pedidos_historia`

`pedidos_siesa` borra una línea en cuanto deja de tener pendiente y no guarda
cuándo apareció. `pedidos_historia` (una fila por `f431_rowid`, la línea real
de Siesa) guarda primera y última vez vista (UTC + día Bogotá), lo pedido la
primera vez y la última, lo remisionado, cliente, municipio, CO, y **por qué
salió**: `CUMPLIDO` · `DESAPARECIDO` · `ANULADO` · `OTRO_ESTADO`.

La escribe el sync de pedidos (`pedidos_historia.registrar_barrido`, en un
SAVEPOINT, nunca levanta). **Ningún barrido incompleto registra una salida**
—ni cumplido ni desaparecido—: es la misma bandera `paginacion_completa` que
protege el borrado de `pedidos_siesa`. `ultima_vez_vista_at` se refresca cada
10 min salvo que cambie una cantidad. Las `DESAPARECIDO` se clasifican
preguntando `get_estado_pedido`, **3 por ciclo** como mucho.

Límite heredado del sync: solo ve `f430_id_co = CONNEKTA_CENTRO_OP` en estado 3
(244 líneas en QA el 2026-09-24). Un pedido que nunca llegó a comprometido no
tiene historia.

### 3 · Las fotos diarias — `app/services/fotos_siesa_service.py`

| Tabla | Fuente | Clave | Qué guarda |
|---|---|---|---|
| `foto_ventas_lineas` | `API_v2_Ventas_Facturas_DesdePedido`, por CO y día del documento | `f470_rowid` | cantidad, bruto, descuento, IVA, neto, costo, cliente, vendedor, cond. pago, bodega, concepto/motivo, causal, estado, pedido de origen + `pedido_clave` |
| `foto_stock_diaria` | `stock_siesa` + costo de `API_v2_Inventarios_InvFecha` | día × bodega × SKU | existencia, comprometido, salida sin confirmar, `stock_actualizado_at` (frescura de la fila), `rezagada`, `vendible` (solo `_BODEGAS_PV`), costo unitario/total |
| `foto_cartera_diaria` | `API_v2_CxC_General`, `f353_fecha_cancelacion IS NULL AND f253_id LIKE ''1305%''` | día × `f353_rowid` | saldo (db − cr), vencimiento, días vencido, tercero, CO, cuenta |
| `fotos_siesa_corridas` | — | `run_id` × alcance | **el veredicto**: `completa` + motivo, filas, páginas, detalle |

**La corrida es la unidad de completitud.** Un total sale de
`filas_vigentes(tipo, alcance, dia)`: las filas de la última corrida
**completa**, o **`None`** — hueco declarado, no «cero ventas». Una corrida
incompleta (rechazo `alerta`, excepción, breaker, página tope agotada,
paginación inestable dos veces) **no pisa filas existentes**: solo agrega las
nuevas marcadas `completa=False`. Nunca escribe un cero.

| Variable | Defecto | Qué hace |
|---|---|---|
| `FOTOS_SIESA` | ausente = **apagado** | Enciende el cron (`[FOTOS_SIESA]`, en `_scheduler_pesados`, 18:00 Bogotá). El interruptor vive en `correr_fotos`, no en el scheduler |
| `FOTOS_SIESA_COSTO` | `true` | Costo por InvFecha (~50-70 páginas por bodega operada, ~1 min c/u) |
| `FOTOS_SIESA_DIAS_VENTAS` | `3` | Días hacia atrás que se re-fotografían (facturas tardías, anulaciones) |

Corre solo dentro de la ventana de Siesa **si `SIESA_VENTANA` está configurada** (`ventana_siesa`, tanda 2 · H; Regla 14); si la ventana se cierra a
mitad, lo que falta va a `no_corrieron`. Lock `LOCK_FOTOS_SIESA = 2020`.
`GET /api/health/siesa` → `fotos_siesa`: encendido, última corrida por tipo y
`huecos_ultimos_dias`, leídos **de la base** (el cron corre en el worker).

**Cómo encenderlo:** `HEAVY_SCHEDULERS=true` en el worker (ya lo necesita) +
`FOTOS_SIESA=true`. Verificar primero con
`venv/bin/python scripts/qa_fotos_siesa_real.py --co 003 --dia <YYYY-MM-DD>`
(solo GET, SQLite local, POST bloqueado dos veces).

### Verificado en vivo contra Siesa QA (2026-09-24, 12:47 Bogotá, solo GET)

| Foto | Resultado |
|---|---|
| Ventas CO 003, 2026-09-22 | completa · 2 líneas · 2 FE · neto $149.000 · 2 con `pedido_clave` |
| Cartera 1305 abierta | completa · **4.632** documentos · **47** páginas · saldo $4.102.143.835 (QA: 4.613 vencidos) |
| Costo InvFecha NB1 | **incompleta — paginación inestable**: 851 y 897 filas repetidas en dos pasadas; unión 4.028 referencias con costo |
| Ventas CO 003, 2026-09-04 | completa · 17 líneas · 7 FE · neto $6.917.520 · 7 con `pedido_clave` |
| Stock NB1 (tras descargar `stock_siesa` con la consulta de producción: 10 bodegas, NB1 = 3.276 SKU) | completa · 3.276 filas · 0 rezagadas · costo en 2.717 (**559 sin costo, NULL**: sin fila en InvFecha con existencia ≠ 0, o perdidas por la paginación inestable — 877 y 889 repetidas) |

**Hallazgo — la fecha sin comillas devuelve cero.** `f350_fecha >= 20260101
AND f350_fecha <= 20260923` → 0 filas; con `''20260101''` → 100. Siesa no
rechaza: contesta «No se encontraron registros», que `_get` convierte en tabla
vacía. **`vigia_service._facturas_de_semana` estaba así**: encendida la
ingesta, habría escrito cada semana en cero. Arreglado con
`siesa_filtro.lit_fecha`; trinquete AST `tests/test_siesa_filtro_fecha.py`.

**Hallazgo — InvFecha repite filas entre páginas** (sin `ORDER BY` estable).
Las 623 «referencias con varias filas» que se midieron antes eran casi todas
**duplicados de paginación**, no lotes: deduplicado por (ref, lote,
ubicación) quedan 4.028 filas = 4.028 referencias. Sumar sin deduplicar
**duplica el costo**. La foto marca el costo `completo=False` y lo declara; el
costo por fila es correcto, la cobertura no está garantizada.

**Campos reales que el contrato no trae**: `f210_codigo_vendedor` (Ventas),
el pedido viene como `f430_*` (el `.docx` lo documenta como `f350_*`
duplicados), `f201_id_sucursal` (CxC); `f353_prefijo_cruce` del contrato no
viene. Referencias de InvFecha y bodegas vienen con espacios a la derecha.

### Lo que NO cubre (declarado)

- **Kardex**: `…_KardexWMS` bloqueada en QA. `stock_diario` sigue
  reconstruyéndose del kardex; esta foto es otra fuente (observada, no
  reconstruida).
- **Ventas POS de tienda**: no hay consulta. La foto es de facturas **desde
  pedido**; la venta de caja en tienda no aparece.
- **NC y RC como documentos**: solo se ven en `total_cr` de la cartera.
- **`stock_siesa` es acumulativa**: una fila que Siesa dejó de reportar
  conserva su último valor. La foto la copia con `rezagada=True` y su
  `stock_actualizado_at`; no la convierte en cero.
- **La historia del pedido empieza el día que se despliegue**. Lo anterior no
  existe en ningún lado.

### Qué pedirle al consultor

1. `ORDER BY` estable en `API_v2_Inventarios_InvFecha` (o una consulta de
   costo por bodega sin paginación inestable).
2. Contrato de `API_v2_Ventas_Pedidos` (sigue ausente; la historia depende de
   él) y una variante con filtro por fecha de cumplimiento/anulación, para
   clasificar las salidas sin una consulta por pedido.
3. Una consulta de ventas POS por CO y día.
4. Desbloquear `…_KardexWMS` en QA.
5. Confirmar que `f470_rowid` es único en toda la T470 (se asume como clave de
   la foto de ventas).

### Hallazgo del acta de corte — resuelto por `m034agotado`

`eventos_stock_agotado` (PROTEGIDA_ANALITICA) tenía `tarea_picking_id` NOT
NULL con FK a `tareas_picking` (OPERATIVA): con un solo evento, el corte
terminaba en RESET INCOMPLETO. Lo resolvió en el esquema la migración
`m034agotado` (nullable + `ON DELETE SET NULL`, el evento copia su contexto),
y `tests/test_acta_de_corte.py::TestLoProtegidoNoApuntaAlEnsayo` exige que toda
FK de una tabla protegida hacia una operativa esté resuelta así o declarada en
`REFERENCIAS_PROTEGIDAS`. Las cinco tablas de fotos no tienen FKs.

Tests: `test_cadena_pedido.py`, `test_pedidos_historia.py`,
`test_recaudo_desenlace_siesa.py`, `test_fotos_siesa.py`,
`test_siesa_filtro_fecha.py`, `test_connekta_sin_sombras.py`. 22 mutaciones, las 22 rojas. Suite completa
el 2026-09-24: **6930 passed, 0 failed**.

**De paso, en `tests/conftest.py`:** `_sin_sombras_en_el_gateway` borra tras
cada test las «sombras de método» del singleton `connekta` — el
`setattr(connekta, '_get', <método original>)` con que `monkeypatch` restaura
un parche de instancia, y que tapa los parches de CLASE de los tests
siguientes (la lección del refactor del 2026-09-09, que no tenía limpieza). Era
la causa de que `test_rechazo_siesa_no_es_pagina_vacia` fallara solo en la
suite completa.

---

## Analítica — shell y 🧭 Recorrido del pedido (Fase 1, 2026-09-24)

Pestaña admin **📈 Analítica** (`tab-analitica`, segunda del nav — `tab()`
resalta por POSICIÓN, y `test_analitica_recorrido.py` exige que `TABS` y el nav
tengan el mismo orden). La ven `_ROLES_ANALITICA` = `Roles.GESTION` (cruzado
por test); carga al entrar, **nunca por el timer de 30 s**.

**Shell** (`analitica.js`): filtros comunes (`an-f-almacen`, `an-f-desde`,
`an-f-hasta`, 30 días Bogotá por defecto, `anFiltros()`), sub-pestañas de
`AN_VISTAS` (recorrido · fugas · salud · bitácora, llamadas en runtime),
`anCargarPanel` (sin parpadeo, la respuesta vieja no pisa, un fallo conserva lo
que se ve) y formatos que no inventan ceros: `anPesos(null)` = «sin dato»,
`anPct(x, n)` = «82 % de 340» o «— de 0», `anFrescura(meta)` nombra cada
fuente con `completa === false` y su `motivo`. **Convención de `meta.fuentes`**
para todas las vistas: `{nombre, completa, motivo, actualizado_en}`.

**Recorrido** — `services/analitica_recorrido.py` (la única que decide:
`_evaluar`), `routes/analitica_recorrido.py`, `analitica_recorrido.js`:

    GET /api/analitica/recorrido                        embudo + guía + sin_enlazar + meta
    GET /api/analitica/recorrido/etapa/<etapa>?vista=en|llegaron|fuga&page&per_page
    GET /api/analitica/recorrido/pedido/<pedido_clave>  línea de tiempo (o SIN-CLAVE-PK<id>)

| Etapa | Evidencia | Marca |
|---|---|---|
| aprobado | línea en `pedidos_historia` / `pedidos_siesa` | `primera_vez_vista_at` (solo si es anterior al primer registro WMS) |
| recogido | todo picking vivo COMPLETADO | última `fecha_completado` |
| empacado | empaque vivo VERIFICADO/DESPACHADO | `fecha_verificado` |
| despachado | DESPACHADO / remisión | `fecha_despachado` |
| entregado | parada ENTREGADO / PARCIAL / ENTREGADO_SIN_PAGO | `fecha_confirmacion` |
| cobrado | `monto_cobrado` > 0 o RC ENVIADO/YA_SALDADA; CREDITO pasa **sin marca** | idem / `siesa_rc_at` |
| liquidado | ruta LIQUIDADA | `liquidada_en` |

- **Acumulativas**: evidencia de una etapa posterior implica las anteriores;
  la que no dejó hora cuenta en el embudo y sale de los tiempos (`sin_marca`).
- **Cohorte**: día Bogotá en que el pedido entró (historia de Siesa o, si no la
  hay, primer registro WMS; `entrada_fuente`). Pendiente en Siesa sin historia
  ni WMS: `meta.sin_fecha_de_entrada`, no se asigna a ningún rango.
- **Fugas que cortan** (en la etapa que no alcanzó, con motivo): picking
  cancelado (motivo de la bitácora), empaque cancelado, rechazo en ruta
  (`motivos_rechazo.etiqueta`), entregado sin pago, liquidado sin cobro,
  anulado en Siesa. **Detenido**: bloqueo de picking/empaque. **Pérdidas
  parciales** (siguen): recogido incompleto, entrega parcial, NC, devolución.
  **FUERA_DEL_WMS**: salió de pendientes en Siesa sin un registro WMS; fuera
  del denominador.
- **Valor** = suma de `pedidos_historia.vlr_neto`; una línea sin valor deja el
  pedido «sin valor» (contado aparte, nunca parcial).
- **Guía**: ciclo de caja (aprobado → liquidado, mediana/p90 por rango más
  cercano, `n`, `sin_marca`, `negativos`); % del valor sin fuga sobre la
  cohorte y **sobre los ya cerrados** (lo que va en camino no la baja).
- **Sin enlazar**: empaques de pedido sin `pedido_clave` (mini embudo +
  muestra con línea de tiempo), pickings y líneas de historia sin clave. No se
  unen por texto.
- Píldoras: referencias **provisionales** (`AN_REC_REFERENCIAS`: ciclo ≤ 2
  días, sin fuga ≥ 95 %), declaradas en pantalla; no son metas del negocio.

**Lo que no se puede medir bien todavía**: la aprobación real (la historia
empieza con el deploy de `m036fotos`; antes el pedido entra por el WMS y el
tramo aprobado→recogido queda sin marca); el valor en recogido/empacado (el
picking no tiene precio: se arrastra el valor aprobado); el cobro de lo que va
a crédito (pasa a cartera, fuera del WMS); motivos de cancelación anteriores a
`m035bitacora`; y el ciclo de caja con hora en rutas liquidadas antes de
`liquidada_en`.

Trinquete: `tests/test_analitica_recorrido.py` (55 tests, mundo armado con los
servicios reales: completo, cancelado con motivo, rechazado, sin pago, recogido
incompleto, en curso, fuera del WMS, sin valor, crédito, bloqueado, sin clave;
render en Node con `util.js` real). 13 mutaciones, las 13 rojas.

---

## Analítica — Salud del dato y Bitácora (Fase 1, 2026-09-24)

**🩺 Salud del dato** contesta «¿puedo confiar en los números de hoy?» antes de
que alguien decida sobre el recorrido, las fugas o los KPI.
`GET /api/analitica/salud` (gestión, `_es_gestion`) ← `app/services/analitica_salud.py`
← vista `analitica_salud.js` (`anSaludCargar(el, f)`). **Cero Siesa**: lee la
base del WMS y reutiliza lo que ya existía, no lo reimplementa.

| Fuente | De dónde sale el veredicto | Crítica |
|---|---|---|
| Pedidos de Siesa | `registro_sync_service.estado_persistido('pedidos')`; tolerancia 15 min operativos (ventana 7–21) | sí |
| Existencias (`stock_siesa`) | `max(updated_at)` por bodega de `_BODEGAS_INVENTARIO` (o la del almacén); 2 h operativas | sí |
| Fotos ventas / stock / cartera | `fotos_siesa_corridas`: última corrida **completa** por alcance; se espera la de ayer, o la de hoy pasadas las 19:30. Stock con costo incompleto en una bodega vendible → INCOMPLETA | no |
| Kardex | `kardex_service.salud_kardex()` traducido (`_KARDEX_A_VEREDICTO`); un código que no conoce se lee INCOMPLETA, nunca al día | no |
| Vigía adopción / facturación | `serie_vigia` por semana; se espera la semana cerrada anterior (lunes ≥ 06:00) | no |

Además: crons **de este proceso** (`SCHEDULERS_ACTIVOS`, lo mismo que
`/api/health/siesa`), invariantes de `app/services/auditoria/` por flujo, cola
de Siesa (FALLIDO = crítico; pendiente > 1 h operativa o PROCESANDO > 30 min =
advertencia) y **cobertura de `pedido_clave`** en picking/packing de PEDIDO del
rango (< 95 % advertencia, < 80 % crítico; sin tareas = `null`, no 0 %).

- **Cinco veredictos** (`AL_DIA`, `ATRASADA`, `INCOMPLETA`, `APAGADA`,
  `SIN_DATOS`) y **una** traducción a nivel (`_NIVEL_DE_VEREDICTO`, `nivel_de`):
  apagada y sin datos nunca son `ok`; en una fuente crítica son `critico`.
- **Global** (`veredicto_global`): `CONFIABLE` solo si todo está `ok`; cualquier
  crítico → `NO_CONFIABLE`; lo demás `CON_RESERVAS`. Una auditoría que no miró
  todos los flujos, o con consultas truncadas, tampoco deja decir «confiable».
- **Tiempo operativo, no de reloj** (`tiempo_operativo`): de noche Siesa no
  opera (Regla 14) y la fuente no «se atrasa».
- **La web y el worker son procesos distintos.** Los crons del worker (fotos,
  refresco de existencias, alertas) no se ven desde la web: el veredicto sale de
  la **frescura del dato en la base**. Los interruptores (`FOTOS_SIESA`,
  `VIGIA_INGESTA_FACTURACION`) se leen en ESTE proceso y Railway da variables
  por servicio: con dato fresco manda el dato; con dato viejo e interruptor
  apagado acá se declara `APAGADA`.
- **Tope de la auditoría**: resultado guardado 10 min por proceso
  (`AUDITORIA_TTL`); al recalcular no se empieza un flujo nuevo pasados 20 s
  (`AUDITORIA_PRESUPUESTO_S`); lo que no alcanzó va en `flujos_no_evaluados`.
  Los invariantes miran el estado actual, no el rango ni el almacén.
- Filtros: `desde`/`hasta`/`almacen_id` validados (400 ante basura, rango >
  366 días o almacén inexistente). Solo la cobertura (rango + almacén) y las
  existencias (almacén) los respetan; la salud de una fuente es de hoy.

**📜 Bitácora** — `analitica_bitacora.js` (`anBitacoraCargar(el, f)`) sobre
`GET /api/analitica/bitacora` (ya no está en `DEUDA_SIN_UI`) y
`GET /api/analitica/bitacora/patrones`. La lista llega enriquecida por
`describir_acciones` (**solo lectura**, la lógica de registro no se tocó):
«Ana canceló el picking PK-123 del pedido 003-PD-1502 — motivo: …», con nombre
(o «El sistema», o «Usuario #N (ya no existe)»), hora Bogotá, almacén y el
antes → después campo por campo (el pedido sale de lo que la fila guardó, o de
la tarea viva). Patrones: por persona, por acción, por entidad y por hora del
día Bogotá, cada uno con su `n`; `opciones` se calcula solo con rango y
almacén, para que los selectores no se encojan al filtrar. Tope del patrón
por hora: 20.000 filas, declarado. `/bitacora` ahora rechaza `almacen_id`,
`usuario_id` o `entidad_id` ilegibles con 400 (antes `type=int` los ignoraba y
devolvía todo).

**Lo que NO mide todavía:** si el worker está vivo (solo se infiere por sus
datos); la salud del kardex en QA será siempre `SIN_DATOS` (la consulta da
401); las fotos nacen apagadas; Conteo y Flota no escriben en la bitácora.

**Integración (2026-09-24):** el shell `analitica.js` y las tres vistas están
declarados en `tests/test_permisos_por_pantalla.py::PANTALLAS`; las rutas
tienen consumidor y no quedan en `DEUDA_SIN_UI`.

Suite completa en este worktree (2026-09-24): **7256 passed**, 1 failed (el
guard de consumidor de arriba, que espera al shell), 5 skipped, 19 xfailed.

Trinquete: `tests/test_analitica_salud.py` (68 tests: servicio con datos que
distinguen los cinco veredictos, endpoints, render real en Node con `util.js`;
17 mutaciones, las 17 rojas).

---

## Analítica — 💸 Fugas (Fase 1, 2026-09-24)

**Fuga = plata que la operación deja ir en el rango.** Servicio
`app/services/analitica_fugas.py`, rutas `app/routes/analitica_fugas.py`
(`GET /api/analitica/fugas` y `GET /api/analitica/fugas/<clave_fuga>`, rol
`_es_gestion`, `almacen_id`/`desde`/`hasta` validados: basura → 400, clave
desconocida → 404), vista `app/static/pwa/analitica_fugas.js`
(`anFugasCargar(el, f)`, cuelga de `AN_VISTAS.fugas` del shell). Cero Siesa.

**No define políticas: las lee.** Cada fuga llama a la función que ya decide y
solo agrega la valorización del caso, escrita en su docstring:

| Fuga | Política que lee | Pesos del caso | Sin valor cuando… |
|---|---|---|---|
| Venta perdida | `filtros_venta_perdida` (nuevo: el filtro de `metricas/venta_perdida.py` hecho función, `almacen_id=None` = todos) | faltante × precio capturado | el evento no tiene precio (hoy casi todos) |
| Faltantes ajustados | `metricas.conteo._cargar_cadenas` + `_ajustes` | −diferencia × costo de la foto (faltante +, sobrante −); total = **neto perdido** | costo ausente o ≤ 0 |
| Devoluciones en ruta (`devoluciones_ruta`, antes `rechazos_ruta`: el KPI diario de ese nombre mide otra cosa) | `RECHAZADO`/`PARCIAL` por `fecha_confirmacion`, `motivos_rechazo.etiqueta`, desenlace `siesa_nc_*` | total: `valor_factura`; parcial: devuelto × precio unitario de `foto_ventas_lineas` del pedido | sin `valor_factura`, o **una** referencia devuelta sin precio único (un parcial a medias engaña más que ninguno) |
| Entregado sin pago | `ENTREGADO_SIN_PAGO` | `valor_factura − monto_cobrado` | sin `valor_factura` |
| Plata en la calle | `rezago_liquidacion` (rutas entregadas sin liquidar HOY, entrega en el rango; sin fecha → período actual, declarado) | Σ `monto_cobrado` por ruta × almacén | ninguna parada confirmada |
| Mercancía en limbo | `filas_vigentes` de la última foto completa del rango, `bodegas_de_limbo()` = `_BODEGAS_SERVICIO` − `_BODEGAS_PV`, filas `vendible=False`; + TRA-30/TRA-31 | existencia × costo del SKU en la foto de NB1 del mismo día | sin costo en NB1. Sin foto completa → **sin dato** (no cero) |
| Trabajo perdido | `bitacora_acciones`: picking CANCELAR/REABRIR con `antes.cantidad_recogida > 0` (no la de auditoría), empaque CANCELAR ya empezado | — | **siempre**: es trabajo, no mercancía |
| Documentos trabados | `siesa_jobs` FALLIDO creados en el rango (sin ALERTA_EMAIL) | `valor_de_job`: RC/retención `payload.monto`, despacho `valor_factura`, ajuste cantidad × costo | todos los demás tipos |

**Las reglas** (trinquete `tests/test_analitica_fugas.py`, 52 tests, 16
mutaciones, las 16 rojas):

- **Sin valor ≠ 0.** Una fuga sin ningún caso valorizado tiene `pesos: null`;
  con algunos, `pesos` suma solo esos y `es_cota_inferior: true`. `casos: 0` sí
  es `pesos: 0`: no hubo fuga.
- **Período anterior de igual duración** (`periodo_anterior`): `[desde − n,
  desde − 1]`, n = días del rango. Tendencia por pesos si los dos lados tienen
  valor; si no, por casos; si no, `sin_base`.
- **AV1/TRA1 nunca vendibles**: el caso del Armador (NB1 100 + AV1 40 + TRA1
  25) da 65 en limbo, no 165. Con almacén filtrado el limbo se muestra pero no
  suma (no pertenece a un almacén).
- **«Fugas del período» suma solo el `aporte_al_total` de las fugas con valor
  conocido** (nunca negativo: un sobrante neto de conteo no devuelve la plata
  de un rechazo) y lista en `fuera_del_total` las que quedan fuera y por qué.
- **Orden**: sin dato y sin valor con volumen relevante (casos ≥ mediana de
  casos de las fugas con valor) primero; después por pesos; en el detalle, los
  casos sin valor van arriba. Cada caso trae `pedido_clave` cuando existe.

**Integrada con el shell (2026-09-24):** las dos rutas salieron de
`DEUDA_SIN_UI`. El botón «🧭 Recorrido» de un caso llama
`anRecorridoAbrir(pedido_clave)` (`analitica_recorrido.js`), que espera a que
`anSubtab('recorrido')` —ahora devuelve la carga— pinte la vista y después abre
la línea de tiempo de ese pedido. `tests/test_analitica_integracion_js.py`
prueba el cable en Node.

**Lo que NO se puede medir todavía, y por qué:**
- Venta perdida en pesos: `Producto.precio_venta` no lo llena ningún sync → casi
  todo es sin valor (cota inferior). Tampoco hay venta POS de tienda.
- Entregado sin pago «sigue abierta»: el WMS no ve si el cliente pagó por
  fuera; cruzar con `foto_cartera_diaria` queda para cuando la foto corra.
- Limbo y precio de parciales dependen de `FOTOS_SIESA` encendido; hoy apagado
  → limbo sale **sin dato**.
- Trabajo perdido empieza con `m035bitacora`; `meta.fuentes.bitacora.completa`
  dice si el rango es anterior. Costo de mano de obra: no existe en el WMS.
- Documentos trabados solo valoriza tres tipos; NC y traslados no llevan monto
  en el payload.

## Analítica — capa semántica y KPI diario (Fase 1, 2026-09-24)

`app/services/analitica_kpi.py` · `app/routes/analitica_kpi.py` ·
`analitica_kpi_diario` (migración `m037kpi`, sobre `m036fotos`).

Bajo la Ley 1116 la caja vale más: las tendencias y las alertas (Fase 2) tienen
que leer **un número por métrica y por día**, definido una sola vez y guardado
en una tabla que sobreviva al acta de corte. Esto es esa capa.

### El catálogo — 15 métricas, una definición cada una

Cada entrada de `METRICAS` trae nombre de gerencia, qué mide, unidad, dirección
buena (↑/↓), dueño sugerido, fuente, agregación (SUMA / TASA / NIVEL), si se
abre por almacén y la función que la calcula para un día y almacén.
**Reutilizan las políticas existentes**; lo nuevo que hizo falta vive junto a
su política, no en la analítica:

| Clave | Qué es | Fuente / función |
|---|---|---|
| `pedidos_despachados` · `valor_despachado` | Tareas de empaque DESPACHADO ese día y su `valor_factura`. Sin valor → INCOMPLETO (cota) | `metricas.pedidos_despachados` (ahora devuelve `sin_valor_factura`) |
| `fill_rate` | Cohorte con **entrega** ese día: Σ min(remisionado, pedido) / Σ pedido | **`metricas/fill_rate.py`** sobre `pedidos_historia` (abajo) |
| `venta_perdida` | Faltante × precio de pickings de venta bloqueados. Sin precio → INCOMPLETO | `metricas.venta_perdida` |
| `conteos_cerrados` · `exactitud_inventario` · `ajustes_valor` | Cadenas con veredicto; IRA exacta; ajustes \|ent\|+\|sal\| a costo de la foto | `metricas.conteo`: `cadenas_del_dia`, `exactitud_total`, `ajustes_del_dia` (nuevas, sobre `_exactitud`/`_ajustes`) |
| `rutas_sin_liquidar` (NIVEL, total) | Al **cierre** del día: entregadas sin liquidar con urgencia atrasada o cruza_mes | `rezago_liquidacion.rutas_sin_liquidar_al_cierre` (nueva) |
| `entregado_sin_pago` · `rechazos_ruta` | Paradas confirmadas ese día: valor ENTREGADO_SIN_PAGO; RECHAZADO / todas | `recaudos_entrega` + `EstadoEntrega` |
| `jobs_siesa_fallidos` (total) | Cohorte de jobs **encolados** ese día (sin `ALERTA_EMAIL`) que hoy están FALLIDO. Con alguno en cola → INCOMPLETO | `siesa_jobs` (no hay columna «falló el…»: se mide por cohorte) |
| `cancelaciones` | Acciones CANCELAR/ELIMINAR/ANULAR de la bitácora | `bitacora_acciones` |
| `ventas_facturadas` | Neto de la foto de ventas sin anuladas; por almacén = su bodega dentro de la corrida de su CO | `fotos_siesa_service.ventas_del_dia` (nueva) |
| `cartera_abierta` · `cartera_vencida` (NIVEL, total) | Saldo 1305 de la foto del día; vencida = `dias_vencido > 0` | `fotos_siesa_service.cartera_del_dia` (nueva) |

### Las reglas que la hacen confiable

- **AUSENTE ≠ 0**, con CHECK en la base: `(estado='AUSENTE') = (valor IS NULL)`
  y todo lo que no es OK lleva motivo. Un día anterior al **primer registro**
  de su fuente es AUSENTE («el WMS no registraba»), no cero. Una tasa sin
  denominador o con muestra chica es AUSENTE **pero guarda numerador y
  denominador**, para que la semana o el período los sumen (Σnum/Σden).
- **Un AUSENTE no pisa un valor guardado.** El acta de corte vacía tareas,
  recaudos y jobs; recalcular después «descubriría» que no hay datos. La
  tabla existe justamente para sobrevivir a eso (`conservada` en el resultado).
- **Único con NULL**: dos índices parciales (almacén / total). Un UNIQUE normal
  deja pasar dos filas «total» del mismo día.
- **El día en curso no se guarda**: la serie lo calcula en vivo, `en_curso`,
  INCOMPLETO. Un día pasado sin fila es AUSENTE «sin calcular» — no se calcula
  al vuelo: el valor del día es el guardado.
- **Día Bogotá** en todo, también 19:00–23:59 (`rango_dia_operativo_utc`).

### Fill rate sin sesgo de supervivencia

`pedidos_siesa` borra lo cumplido: el fill rate de ahí sale bajo por
construcción. Sobre `pedidos_historia`, solo entran las líneas **pedidas desde
el primer día de historia** (las anteriores pudieron salir sin que nadie las
viera; se excluyen y el día queda INCOMPLETO). DESAPARECIDO, OTRO_ESTADO y
CUMPLIDO-por-estado-4 (su remisionado es el del último barrido **antes** de
salir) son desenlace desconocido: fuera y declarados. ANULADO sale del
denominador y se cuenta aparte. Sin historia: AUSENTE.

### Rezago de liquidación hacia atrás

Con `liquidada_en` (m035). Una ruta LIQUIDADA **sin** `liquidada_en` se liquidó
antes de que existiera la columna: se sabe que estaba liquidada solo para días
posteriores a la primera `liquidada_en` registrada; antes, va a `desconocidas`
y el día es INCOMPLETO. Para hoy da el mismo universo que la alerta.

### Cron, lock, frescura

`[ANALITICA_KPI]` en `_scheduler_pesados`, 04:30 Bogotá. **Nace apagado**
(`ANALITICA_KPI=true`); el interruptor vive en `correr_kpi`. Calcula ayer y
recalcula los `ANALITICA_KPI_DIAS` anteriores (defecto 7, tope 60): una foto
re-tomada, un job que terminó de fallar, un recaudo del otro día.
`LOCK_ANALITICA_KPI = 2021` (registro de `app/utils/lock.py`), el mismo que
toma el recálculo manual — si está tomado, 409. `/api/health/siesa` →
`analitica_kpi`: encendido, último cálculo, totales por día y
`dias_sin_calcular`, leídos de la base.

### Endpoints (gestión; recalcular solo admin)

| | |
|---|---|
| `GET /api/analitica/metricas` | catálogo |
| `GET /api/analitica/serie?metrica=&desde=&hasta=&almacen_id=` | un punto por día: `valor, n, numerador, denominador, estado, motivo, detalle, en_curso` |
| `GET /api/analitica/resumen?desde=&hasta=&almacen_id=` | por métrica: `periodo`, `anterior` (misma duración), `variacion` (`absoluta, relativa, favorable, comparable`), `alerta` |
| `POST /api/analitica/kpi/recalcular` `{desde, hasta, almacen_id?}` | idempotente, tope 31 días, `hasta` < hoy |

Por defecto los últimos 30 días; fecha ilegible, rango invertido, futuro,
almacén inexistente o rango > 366 días → 400. Todas en `DEUDA_SIN_UI`
(«pantalla de tendencias en Fase 2»).

### La alerta es el CUSUM de Vigía, no una copia

`vigia_service.cusum_bilateral` es ahora la única implementación (pura);
`ejecutar_cusum` la usa y persiste alarmas, la analítica la usa sin escribir
nada. Se corre sobre **semanas cerradas y completas** (Vigía está certificado
en semanal; una serie diaria tiene domingos en cero). Una semana con un día
sin medir se excluye y se cuenta; con menos de `MIN_SEMANAS_CUSUM` (8) no hay
alerta y se dice. Trinquete AST: la recursión `max(0, s ± z − k)` vive solo en
`vigia_service` (piso: exactamente 2 sitios; meta-tests de copia y de lo sano).

### Tests y verificación

`tests/test_analitica_kpi.py` (70): cada métrica contra su mundo —servicios
reales para bitácora, historia de pedidos, fotos (Siesa falsa), cola de jobs,
agotados y el **mundo dorado del conteo** (el KPI da lo mismo que el reporte:
6 cerrados, IRA 3/6 sin tasa por n<30, ajustes $5.500); filas de modelo para
despachos, paradas y rutas, igual que sus propios tests. **14 mutaciones, las
14 rojas.** La cadena entera de migraciones corrió contra un PostgreSQL 17
desechable (upgrade, downgrade, upgrade) y un recálculo real sobre él (lock
suelto al terminar, despacho de las 20:30 en su día).
Suite completa el 2026-09-24: **7262 passed, 0 failed** (5 skipped, 19 xfailed).

### Lo que NO mide todavía (declarado)

- **Ciclo de caja** (pedido aprobado → recaudo liquidado): falta unir historia,
  packing y recaudo por `pedido_clave`; es pieza de otro agente / Fase 2.
- **Jobs fallidos por día de falla**: `siesa_jobs` no guarda cuándo falló; se
  mide por cohorte de encolado y cambia si alguien reintenta o descarta.
- **Ventas POS de tienda**: no hay consulta (ver fotos).
- **Rutas, cartera y jobs por almacén**: sus tablas no tienen almacén.
- **Todo lo anterior a cada fuente**: AUSENTE, sin backfill posible.

---

## Fugas de plata en ruta — comprobante, evidencia, hora, distancia, retorno, flota (2026-09-24)

Frente «fugas» del contrato de flota (1, 2, 3, 6, 8, 9, 13, 14). **Todo es
señal para el encargado, nunca sanción** (regla 2 de `flota/CLAUDE.md`): nada
de esto cambia un estado por sí solo ni culpa a nadie. La política vive en
**`app/services/senales_ruta.py`** (una función por pregunta); la tasa de «no
pagó y se quedó» vive en `analitica_fugas.tasa_sin_pago_por_conductor`, junto a
la fuga que ya valorizaba `ENTREGADO_SIN_PAGO`. Migración **`m041flfugas`**
(aditiva, nullable, sin backfill; `down_revision='m037kpi'`, el integrador
re-encadena). Trinquete: `tests/test_fugas_ruta.py` (90 tests, 16 mutaciones,
las 16 rojas).

**La parada normal no pide nada nuevo.** La evidencia se pide solo en las
excepciones, que es donde está la plata:

| Fuga | Qué se hizo | Dónde |
|---|---|---|
| **Transferencia falsa** | Pago bancario (transferencias, consignación, tarjeta, cheque — `requiere_comprobante`) con monto > 0 exige **referencia** (≥ 4 alfanuméricos, `limpiar_referencia`) y **foto del comprobante** (foto-dato: 1600 px, calidad 0.8, **sin recomprimir** en servidor). La referencia viaja al RC en `F358_REFERENCIA_OTROS` (Alfanumérico 30 según el DOCX del 142888 — el test lo lee del `.docx`), leída del recaudo en el ejecutor del DLQ; sin referencia (parada vieja) se conserva lo de antes (`notas`/`'APP'`), porque el campo es obligatorio en consignación | `recaudos_entrega.referencia_pago`, `foto_comprobante`; `connekta_liquidacion_gateway.referencia_otros_rc` |
| **«No pagó y se quedó» falso** | Foto y «📍 Estoy aquí» **intentado** obligatorios (sin señal o sin permiso se acepta: se intentó; `no_se_pidio` no). El conductor ve el aviso y el **contacto del asesor** al elegir el motivo. Tasa por conductor, con su denominador y cuántas sin foto, en Liquidación y en 💸 Fugas (`extra.por_conductor`) | `senales_ruta.exige_evidencia` (lee `motivos_rechazo.SIN_RETORNO`), `para_frontend().exige_evidencia` |
| **Devolución parcial inflada** | `lineas_devolucion_cliente.cantidad_declarada` = lo que dijo el **conductor**; recepción sigue escribiendo lo contado en `cantidad_devuelta` y **no pisa** lo declarado (si falta, se congela lo vigente antes de ajustar). La corrección de recepción va a la bitácora (EDITAR). (`/liquidar-completo`, que guardaba lo del conductor antes de la corrección del líder, se borró el 2026-09-25.) Faltante de retorno = declarado − contado, en Liquidación (señal por parada y total por ruta) y en 💸 Fugas (`devoluciones_ruta.extra.faltante_de_retorno` por conductor) | `senales_ruta.faltante_de_retorno`, `liquidacion_service._declarado_por_el_conductor` |
| **Hora de la parada** | Nombres acordados con el integrador: `recaudos_entrega.ts_dispositivo` (hora del teléfono al confirmar, UTC), `ts_desfase_s` (teléfono − servidor **medido al enviar**, con `ts_envio`; positivo = adelantado), `via_cola`; `entregas_geo.ts_dispositivo` y `pos_ts_dispositivo` (`pos.timestamp`). **No reemplazan** `fecha_confirmacion`/`capturado_en`. La cola offline las manda (`_condSelloDeEnvio`); un ítem viejo usa la hora a la que se encoló. `clasificar_hora`: `reloj_desfasado` / `antes_de_salir` son señal; `diferida` (sin señal) es informativa | `senales_ruta.leer_ts/desfase_s/clasificar_hora` |
| **Rechazo lejos del cliente** | `recaudos_entrega.distancia_cliente_m`, medida contra el maestro **antes** de que la captura vote (si no, se mide contra su propia sombra). Señal solo si el motivo afirma presencia (`CLIENTE_CERRADO`, `FUERA_DE_HORARIO`) y supera el umbral. Sin punto del cliente → sin señal | `senales_ruta.senal_rechazo_lejos` |
| **Efectivo en la calle** | Por conductor: efectivo cobrado en rutas EN_TRANSITO/ENTREGADA no liquidadas, con antigüedad (día Bogotá de la confirmación más vieja). Cabecera de Liquidación | `senales_ruta.efectivo_en_poder_por_conductor` |
| **Despacho sin mirar la flota** | Al **iniciar el cargue** y al **despachar**: SOAT/RTM vencidos, no registrados o no encontrados, sin inspección apta hoy, OT de taller abierta, custodia de otro conductor / en sede / de nadie, sin vehículo. **Informa, no bloquea**: 409 con la lista; con `motivo_advertencias` sale y queda FORZAR en la bitácora (con las claves). Lo ya reconocido en la misma ruta no se pregunta dos veces. Lee `flota/` solo por sus funciones públicas (`custodia_activa`, `inspecciones.del_dia`, `taller.ordenes_de`, `MedidorSQL.documentos_por_vehiculo`) + el modelo `DocumentoVehiculo` para «no registrado» | `senales_ruta.advertencias_de_flota`, `RutaService._reconocer_advertencias_flota` |
| **`entregar_ruta` marcaba ENTREGADO lo ausente** | Solo se toca un bulto con `entregado` **booleano literal**. Lo demás conserva su estado (el de la parada) y lo que nadie declaró va en `sin_declarar`. El cierre del conductor (`bultos: []`, también offline) no cambia | `RutaService.entregar_ruta` |

**Tres trinquetes de clase**, por AST y con inventario que solo encoge:
- toda llamada a `trigger_recibo_caja` en `app/` pasa `referencia_pago` (inventario vacío);
- toda función que pone una ruta en `EN_CARGUE`/`EN_TRANSITO` llama a
  `_reconocer_advertencias_flota` (única excepción: `crear_ruta`, que nace
  EN_CARGUE y despacha por `cerrar_ruta`);
- ningún `.get('entregado'|'cantidad_entregada', <default>)` (una excepción
  declarada: los ítems de una PARCIAL, que el PWA manda siempre).

**El formulario viejo en caché.** La exigencia de comprobante y evidencia se
aplica al payload con `version_formulario >= 2` (el PWA nuevo lo manda). Un
ítem de la cola offline armado por un PWA anterior **no se rechaza** —quedaría
trabado en el teléfono para siempre— y aparece como señal (`sin_comprobante`,
`sin_evidencia`). Esto también es una puerta: quien arme el POST a mano sin el
campo se salta la exigencia, y la señal es lo único que lo delata.

**De paso:** re-confirmar una parada sin foto nueva **borraba** la foto
anterior (la pantalla decía «Foto guardada»); ahora la conserva. La compresión
fallida de la foto ya no es un `except: pass`: se guarda como llegó y se loguea.
`liquidar-completo` mutaba los dicts del JSON en su sitio (SQLAlchemy puede no
ver el cambio): ahora copia profunda.

| Variable | Defecto | Qué es |
|---|---|---|
| `SENAL_DESFASE_RELOJ_S` | `300` | Desde cuántos segundos de desfase el reloj del teléfono es señal |
| `SENAL_RECHAZO_LEJOS_M` | `geo_cliente.RADIO_COHERENCIA_M` (500) | Desde cuántos metros un «cliente cerrado» está lejos del cliente |

**Lo que NO cubre (declarado):**
- Fugas 4 (km fuera de ruta), 5 (combustible), 7 (licencia del conductor),
  10–12 (fotos de custodia, cierre forzado, custodio ≠ conductor fuera del
  despacho): no son de este frente.
- `CHEQUE` exige comprobante pero el 142888 sigue sin código de medio para él
  (levanta `ValueError`, como antes).
- La foto del comprobante se guarda en la fila (base64, como `foto_entrega`), no
  en el almacén de fotos de flota; nadie la cruza todavía contra el extracto.
- El faltante de retorno se mide en unidades; en 💸 Fugas no se valoriza (el
  precio por línea existe en la foto de ventas, falta decidir si suma a la fuga).
- (`/liquidar-completo`, donde el líder podía bajar a 0 una línea y perder su
  declarado para el faltante, se borró el 2026-09-25.)
- La advertencia de flota se pide en el servicio: `scripts/qa_*` y los tests
  que despachan sin mundo de flota pasan un motivo explícito.
- `flota/CLAUDE.md` no se tocó (este frente no edita `flota/`).

`tests/test_cond_pago.py::test_el_payload_de_confirmacion_manda_el_modo` medía
una ventana fija de 6.000 caracteres de `condGuardarParada`: al crecer la
función, `modo_pantalla` quedó fuera y el test se puso rojo con el payload
intacto. Ahora mide la función entera (mutación verificada: quitar el campo lo
pone rojo).

Suite completa (2026-09-24, este worktree, sqlite): **7529 passed, 0 failed**
(5 skipped, 19 xfailed) — 7528 + el test de arriba ya corregido.

## Flota: la bandeja primero, el catálogo después (2026-09-24)

`GET /flota/bandeja` (`VISTA_FLOTA`, `?almacen_id=` opcional) ←
`flota/adaptadores/bandeja.py` ← `flota/dominio/senales.py` (puro) ←
`app/static/pwa/flota_bandeja.js`. El tab Flota de `control_flota` y gestión
abre en **Hoy · Pendientes · Señales · Vehículos · Analítica**; las cuatro
primeras salen de UN viaje (antes: seis `fetch` en serie y `/flota/health` dos
veces). `cargarFlota()` delega en la bandeja.

| Sección | Qué trae | De dónde (no se recalcula nada) |
|---|---|---|
| Hoy | una fila por vehículo activo: «turno de Ana desde las 06:05», dónde, km y su confianza, inspección de hoy, ruta de hoy, **semáforo con su porqué** | `custodia_activa`, `odometro_actual`, `Inspeccion` del día, `RutaDespacho` |
| Pendientes | papeles, **cola de daños de toda la flota** (Reparado / Aplazar / No era nada, solo si `puede_decidir` = `DECIDE_FLOTA`), km en duda, cierres forzados de la semana (fuga 11), turnos sin fotos de inicio (fuga 10), ficha, preventivo vencido | `MedidorSQL.documentos_por_vehiculo` / `custodias_por_vehiculo`, `verificacion.pendientes`, `diagnostico_de_la_flota` |
| Señales | km en días sin ruta (fuga 4), km de una ruta vs la mediana de su ruta maestra, galones vs esperados y precio del galón (fuga 5), ruta de hoy con conductor/vehículo ≠ custodio (fuga 12) | `vigentes_tras_la_ultima_correccion`, `confianza_del_tramo`, `rendimiento_publicable_de`, `ventanas_lleno_a_lleno`, `precio_por_galon` |

- **Semáforo**: rojo = no debería salir o hay que actuar hoy (papel vencido,
  daño bloqueante o vencido, inspección no apta, ruta de hoy sin inspección
  apta, preventivo vencido). Ámbar = trabajo con plazo **o no se sabe** (km sin
  dato o en duda, papel sin cargar, sin turno, ficha incompleta). Verde dice
  «sin pendientes conocidos», nunca «todo bien».
- **Señales: tres estados**, `senal` / `normal` / `no_evaluable`. Sin dato no
  hay señal y se publica por qué en `senales_no_evaluables` (tramo con km en
  duda, ruta sin placa ese día, <5 recorridos de la ruta, rendimiento no
  sostenible, <5 tanqueos para el precio). Los umbrales viajan en `umbrales`:
  son a ojo, declarados, para fijarlos con un mes de bandeja.
- **Proponen, no culpan** (regla 2 de flota): ninguna función del dominio
  recibe una persona; el turno aparece como «contexto», y la pantalla lo dice
  encima de las señales. «Abrir el caso» abre el expediente donde está la
  evidencia; no crea nada.
- **Expediente con pestañas** (Resumen · Daños · Gastos · Taller · Llantas ·
  Preventivo · Documentos · Ficha) reutilizando los modales de siempre; en
  cada `onclick` viajan solo posiciones.
- La Salud se mudó a Analítica (con el mismo health, `flotaHealth()`, pedido
  una vez); los avisos, `FLOTA_AVISOS` y los contadores técnicos
  (`pendiente_sede`, lecturas del mismo segundo) al **Diagnóstico plegado**;
  fuera de sede y todos los cierres forzados, a «Historial de turnos» plegado.
  Rutas → la subpestaña «Flota» se llama **«Alta de vehículos»**.
- **P0 arreglado**: `flotaAbrirFicha` reventaba con `ficha: null` (vehículo sin
  ficha) y se quedaba en «Cargando…»: no se podía crear la primera ficha. La
  clase —«un campo leído de un objeto que el servidor declara nullable»— tiene
  trinquete: `test_ficha_primera_vez_js.py::TestTodoExpedienteAbreConUnVehiculoNuevo`
  abre cada pantalla del expediente contra las respuestas **reales** del
  servidor para un vehículo recién dado de alta, con inventario declarado.
- De paso: `flotaBarrerAvisos` llamaba a `flotaTablero()`, que no existe;
  `flotaBloqueDudosas` se retiró (sus filas están en Pendientes).

Tests: `tests/flota/test_bandeja.py` (47, mundo armado con un caso por placa),
`test_senales_dominio.py` (47), `test_bandeja_js.py` (38, Node + `util.js`
real), `test_ficha_primera_vez_js.py` (17). 17 mutaciones, las 17 rojas.
Suite completa el 2026-09-24 (`-m "not postgres"`, TZ=UTC): **7585 passed**,
1 failed —el inventario del reloj de `tests/flujo/conductor_de_flota.py`, que
no conocía los dos módulos nuevos; declarados en `FUERA_DEL_RELOJ` con su
motivo y verde después—, 5 skipped, 19 xfailed.

**Lo que NO cubre, declarado:**
- **Casi todas las señales de km duermen hoy en producción**: las lecturas sin
  foto nacen `dudosa`, y un tramo en duda no se juzga. Van a
  `no_evaluables` («verificalo primero»): se encienden verificando kilometrajes.
- **Galones** necesita rendimiento publicable (≥6 ventanas y ≥60 días): en una
  flota nueva, dos meses de tanqueos llenos.
- **Sin memoria de casos**: una señal explicada vuelve a aparecer hasta que
  sale de la ventana de 30 días. Una tabla de casos (abierto / explicado /
  escalado) es la decisión abierta para el dueño; no se creó migración.
- El almacén de un vehículo es la sede de su **último turno de sede**; uno que
  nunca pasó por una sede no pertenece a ninguna (se cuenta, no se esconde).
- Papeles y Custodia **siguen también en Analítica** (la decisión «tab
  completo» de sus 15 paneles tiene tests); en Pendientes está la versión
  accionable.

## Flota: el rol no alcanza — sobre QUÉ vehículo opera el conductor (2026-09-24)

**La clase:** *un endpoint `LECTURA_FLOTA` que recibe una placa, un vehículo o
una entidad opera sin verificar que el conductor tiene derecho sobre ella.*
`@exige` pregunta por el rol; la placa la elige el cuerpo del request. Casos
ejecutados ese día: un conductor dejaba el camión de la sede **a nombre de un
compañero**; reescribía con `origen=correccion` el odómetro —CPK, preventivo—
de un camión que no manejaba, y registraba inspección, daño y tanqueo sobre
cualquier placa; `GET /flota/foto/<id>` le daba el **escaneo del SOAT o de la
tarjeta de propiedad** de cualquier vehículo.

| Qué | Regla | Dónde |
|---|---|---|
| Traspaso | Un conductor deja la custodia **solo a su nombre**, y a la sede solo el camión que tiene. 403. El relevo entre conductores es entregar en la sede y recibir de la sede: cada uno firma su mitad con sus fotos | `dominio.custodia.custodio_que_puede_nombrar`, llamada por `traspaso.traspasar` |
| Odómetro, inspección, daño, tanqueo | El conductor, **solo sobre el vehículo de su custodia ACTIVA** (no el de su ruta: la ruta es sugerencia, y Regla 0). Gestión y control de flota, sobre cualquiera | `_permisos.sin_derecho_sobre_vehiculo` |
| `origen=correccion` | `MAESTROS_FLOTA` aunque la puerta sea `LECTURA_FLOTA`: la más reciente reescribe el odómetro. El conductor avisa | `_permisos.sin_permiso_de_corregir` |
| Fotos | El conductor ve las de sus custodias (por **padre**, no por autor), sus daños y sus lecturas. **Nunca documentos** | `_permisos.FOTO_DEL_CONDUCTOR` (total sobre `EntidadFoto`) |
| `/custodia/<id>/fotos` | Conductor: solo sus turnos | `_permisos.sin_derecho_sobre_custodia` |

El 403 de derecho lleva `motivo: 'sin_derecho'` y no `roles_permitidos`: al
que lo recibe no le falta un rol, le falta el vehículo.

**Decisión — cierre forzado para `control_flota`:** se le **da** (antes veía el
campo del motivo y el backend lo trataba como conductor → 409). El patio lo
opera él. Tiene `QuienPide.CONTROL_FLOTA` y, a diferencia del admin, **las
fotos de cierre no lo eximen**: siempre motivo, siempre marcado con su nombre
(`Veredicto.fotos_no_eximen`). Es la regla 11: «cierres forzados por semana» es
una de sus señales y «cero en un mes» su criterio para crecer — un cierre ajeno
con fotos que no contara sería el botón que baja su propio contador. UI:
`FLOTA_ROLES_FUERZAN_CIERRE` = `FUERZA_CIERRE` (test), y el campo solo aparece
si el turno vigente es de un conductor.

**Decisión — alta de vehículos y cuentas de conductor: NO para
`control_flota`.** Tocan despacho e identidad, no el registro de flota; siguen
`_solo_admin` en `app/routes/rutas.py` (sin tocar). Lo que se arregló es que la
pantalla no lo mande a «Rutas → Vehículos», una pestaña que no ve:
`flotaDondeSeDaDeAlta()` le dice «pedíselo a administración».

**Trinquete:** `tests/flota/test_derecho_del_conductor.py` — inventario por AST
de toda ruta de `flota/api/` cuya tupla de `@exige` admite al conductor
(`VERIFICAN` 6 · `VERIFICAN_EN_EL_ADAPTADOR` 1 · `SIN_VERIFICAR` 6 con motivo,
tope que solo encoge) y **cada puerta verificada ejercida por HTTP**: JWT de A
sobre lo de B → 403, sobre lo propio → no 403. Meta-tests (docstring,
comentario y función anidada no cuentan; tupla que no sabe resolver → error,
no cero; piso 13). **13 mutaciones, las 13 rojas.** Helper de tests:
`tests/flota/_turno.py::dar_turno` (el conductor recibe por el adaptador real).

**Lo que NO cubre, dicho:**
- Las **lecturas** del conductor siguen abiertas sobre cualquier placa, a
  propósito y declaradas: `custodia/activa`, `hallazgos/<placa>`,
  `inspeccion/items/<placa>` (recibir exige ver lo heredado antes de tenerlo).
- La verificación vive en la **frontera**, no en los adaptadores: hoy cada
  operación tiene una sola puerta (verificado). Un servicio nuevo que llame a
  `registrar_tanqueo` desde `app/` no la heredaría; el trinquete solo mira
  `flota/api/`.
- La **cola offline** del conductor: si un tanqueo o una inspección se
  sincroniza DESPUÉS de la entrega del turno, ahora es 403 (sin gracia, Regla
  0). Una cola FIFO lo evita; una que reordene, no.
- **El tanqueo tardío** (registrado después de entregar el turno, caso que el
  módulo declara habitual): el conductor ya no puede, ni por el km de cierre
  (que entraba sin marca) ni por `correccion` (que borraba la historia). Lo
  registra control de flota — y para él el hueco sigue: el km real del
  surtidor da 409 y el de cierre pasa colgado de la lectura `entrega`. Falta
  exponer `lectura_id` en `POST /flota/tanqueos`. Medido en
  `tests/flujo/test_flujo_dia_torcido.py`, que también cambió: la inspección
  antes de recibir el turno ahora es 403 (antes 201 con daños que no contaban
  y nadie lo decía).
- Las fotos con `entidad_tipo='odometro'` no tienen escritor todavía; la regla
  (autor de la lectura) está escrita para cuando lo tengan.
- `rutas.py`/`almacenes.py` usan `LECTURA_FLOTA` fuera de `flota/` para leer
  maestros; no están en este inventario.

Suite completa en el worktree (2026-09-24, `-m "not postgres"`, TZ=UTC):
**7497 passed, 0 failed** (5 skipped, 19 xfailed).

## Flota — «Mi camión hoy»: la pantalla del conductor (2026-09-24)

`app/static/pwa/flota.js` (bloque del conductor, funciones `flotaCond*`,
`flotaRec*`, `flotaDano*`, `flotaTanqueo*`, `flotaCola*`) · `index.html`
(clases `.flota-hoy*`, `.flota-hoja*`, `.flota-paso*`, `.flota-opcion*`) ·
`flota/api/_idempotencia.py` · migración `m039flcond` (sobre `m037kpi`).

### Lo que cambió en la pantalla

| Antes | Ahora |
|---|---|
| Seis botones de 54 px apilados sobre las rutas | **Una tarjeta, un botón**: sin turno → «Recibir el camión»; turno sin inspección de hoy → «Inspeccionar»; inspeccionado → una franja `🚚 placa · semáforo · Más`. «Más» abre una hoja inferior con Daño, Tanqueo, Entregar, Mis turnos, el detalle del estado y el rendimiento |
| Estado en varias líneas siempre visibles | **Semáforo de una línea** (lo más grave primero, `+N`), solo cuando hay algo. Informa, no bloquea |
| `#cond-flota` y `#flota-modal` en oscuro | En la **zona clara fija** (selector de tokens del tema claro), con trinquete en `test_legibilidad_pwa` |
| Recibo: grilla de 13 botones | **Checklist guiado**: tablero + km primero, después un ángulo por pantalla con su guía (convención de lados y llantas incluida), `4/12`, la foto abre la cámara y avanza sola, «Saltar» = faltante, resumen con «tomar/repetir» |
| Km pedido 3 veces por turno | Una vez: inspección, daño y tanqueo lo **heredan** (último conocido, la cola manda sobre el servidor) con «Cambió». Sin dato → se pide (regla 4). La entrega sí lo pide: es el del final del día |
| Daño: texto obligatorio, gravedad en «Mayor», sin foto | **Tres toques**: foto (o «no puedo tomarla»), gravedad en tres botones sin ninguno elegido, texto opcional (obligatorio sin foto) |
| Tanqueo: 12 campos, categorías, códigos crudos, fecha vacía | Foto del recibo (`foto_dato`), galones, valor, «¿lo llenaste?», «¿con qué pagaste?» (sin preselección, sin `sin_dato`), estación, km heredado; fecha = hoy Bogotá (`flotaHoyBogota`) |
| «Odómetro» y «Corrección» al conductor | Retirado. El panel de gastos perdió su «modo tanqueo» |
| `d.error` crudo, `no_apto`, `tarjeta_propiedad`… | `flotaMensajeDeError` (roles en palabras) y `flotaPalabra` |

### La cola sin señal

Toda operación se guarda en `_condDB` (`wms_cond`, la base de las entregas)
**con sus fotos** antes de intentarse, con `clave_idempotencia` y
`ts_dispositivo`. Sale en orden y se detiene en la primera que no sale; un
`hecho` o `rechazado` la quita; un rechazo sin nadie mirando queda en la
tarjeta hasta «Entendido». Un 403 `sin_derecho` (sin turno abierto sobre ese
camión, regla del frente de permisos) es un rechazo, no un reintento: sale de
la cola con «pedíselo al encargado de flota». Aviso «N registros pendientes de sincronizar» con
«Sincronizar», y el evento `online` la vacía solo. La tarjeta cuenta lo
pendiente: un recibo encolado ya es turno abierto.

**Servidor:** `@idempotente(operacion, campo_id)` debajo de `@exige` en
traspaso, hallazgo, inspección y tanqueo. La fila de `flota_idempotencia` entra
en la **misma transacción** que el hecho (el adaptador hace el commit); un
rechazo hace rollback y no gasta la clave; la clave de otro usuario u otra
operación → 409. `ts_dispositivo` se guarda sin creerle (el hecho lleva la hora
del servidor).

### Dos defectos que se cerraron de paso

- **Una `foto_dato` bajo 1600 px reventaba el recibo con un 500**: salía `ok`
  y el CHECK de resolución la rechazaba en el commit. La pantalla prometía
  «queda `pendiente_evidencia`» desde la tanda 1. Ahora `guardar_foto` la
  declara así sobre lo medido; el CHECK no se aflojó (test nuevo).
- `hallazgo_peor.descripcion` se pintaba sin `esc()` en la pantalla del
  conductor.

### Tests y mutaciones

`tests/flota/test_mi_camion_hoy_js.py` (46, Node con `util.js` real: estados de
la tarjeta, cola, sin códigos, sin preselección, km, fecha Bogotá, recibo
guiado, contrato de `mi-turno`) y `tests/flota/test_cola_del_conductor.py` (16:
reenvío, rechazo, clave ajena, fotos de daño y recibo, inventario de la cola
contra `@idempotente`). **17 mutaciones, las 17 rojas** (la del daño con
gravedad preseleccionada en el opener sobrevivía al primer arnés: se agregó el
test por la puerta). Migración: upgrade → downgrade → upgrade contra un
PostgreSQL local desechable; `test_gemelos_del_esquema` y
`test_constraints_postgres` en verde (103 CHECK). Suite completa el
2026-09-24 (`-m "not postgres"`): **7501 passed, 0 failed** (5 skipped, 19
xfailed); después se agregó un test (403 `sin_derecho`), verde por separado.

### Lo que NO cubre

- **Que las rutas se vean sin bajar en 390×844 no está medido**: Node no hace
  layout. El test mide un proxy (controles por estado, sin `btn-flota`).
- **La hora del hecho sigue siendo la del servidor**: un recibo de las 5 a.m.
  que sincroniza a las 11 queda de las 11. La hora del teléfono queda en
  `flota_idempotencia.ts_dispositivo` para verlo, no para decidir.
- **El km heredado del tanqueo puede estar viejo** si el conductor no toca
  «Cambió» (el rendimiento se mide entre tanqueos llenos). La pantalla lo dice;
  nada lo impide.
- **`/flota/odometro` sigue autorizando al conductor** aunque ya no tenga el
  gesto: declarado en `_ANCHOS_ACEPTADOS` hasta que el frente de permisos
  estreche la tupla.
- La inspección sin señal usa la última lista bajada (se dice de qué día); el
  veredicto lo da el servidor al llegar.

## 🕒 La jornada del conductor — Fase 0 (2026-09-24)

El dueño: *los conductores, que además de manejar entregan y cobran, no son
controlados y «van y hacen otras cosas».* Esta vista **no contesta eso**.
Contesta lo que los datos de hoy permiten contestar con honestidad: qué dejó
registrado cada conductor en su día, a qué hora, con qué confianza, y dónde
hay tiempo que ningún registro explica. **Cero pasos nuevos al conductor.**

`app/services/jornada_conductor.py` · `app/routes/jornada.py` ·
`app/static/pwa/flota_jornada.js` · `tests/test_jornada_conductor.py`.
Sin migración.

    GET /api/jornada?dia=&conductor_id=     línea de tiempo + indicadores + señales
    GET /api/jornada/resumen?desde=&hasta=  por conductor (≤ 92 días; por defecto 7)

Rol: `Roles.VISTA_FLOTA` (gestión + control de flota), **el conductor no**:
es el registro de trabajo de sus compañeros. Basura, rango invertido, futuro o
> 92 días → 400; conductor inexistente → 404.

### La línea de tiempo (conductor × día Bogotá)

| Evento | Fuente | Hora | Confianza | ¿Gesto suyo? |
|---|---|---|---|---|
| Recibió / entregó el vehículo | `flota_custodia.inicio_ts` / `fin_ts` | servidor | alta (solo existe en línea) | si lo registró él y no fue cierre forzado |
| Preoperacional | `flota_inspeccion.respondida_ts` | servidor | alta | si la hizo él |
| Cargue en el muelle | `bultos.fecha_cargado` (intervalo) | servidor | alta | no (lo escanea el muelle) |
| Cierre del cargue | `rutas_despacho.fecha_cierre` | servidor | alta | no — **no es la salida física** |
| Parada | `recaudos_entrega.fecha_creacion` (primera confirmación) | servidor | **baja**: sin señal es la hora de sincronizar | si `confirmado_por` es su usuario |
| Parada con hora del teléfono | `ts_dispositivo − ts_desfase_s` | teléfono | alta; media si el reloj estaba corrido o el desfase no se midió | ídem |
| Tanqueo | `flota_lectura_odometro.ts` (origen tanqueo) | servidor | alta | sí |
| Cierre de ruta | `rutas_despacho.fecha_entregada` | servidor | media | **no**: `entregar_ruta` no guarda autor |
| Liquidación | `rutas_despacho.liquidada_en` | servidor | alta | no (oficina) |

Joins: `conductores` ↔ `flota_custodia.custodio_conductor_id` ↔
`rutas_despacho.conductor_id` ↔ `recaudos_entrega.ruta_id`; los gestos por
usuario (inspección, tanqueo, confirmación) por `conductores.usuario_id`. Un
conductor **sin cuenta** no tiene gestos propios: sus paradas las confirma
otro y la pantalla lo dice.

**Estados del día** (`estado_de`, una función): `reconstruida` (apertura y
cierre de turno registrados, cobertura ≥ `cobertura_minima`) · `parcial`
(hay tramo entre registros pero falta la apertura o el cierre: `horas` es
`None`, se da `tramo_observado_h`) · `no_reconstruible` (< 2 gestos propios o
cobertura baja: horas y tiempo no explicado `None`, nunca 0) ·
`sin_actividad`. La cobertura mide el **registro**, no el trabajo.

**Tiempo no explicado** — nunca «muerto»: entre dos registros consecutivos
dentro de la jornada, lo que excede el p90 de los compañeros en esa maestra
para ese tipo de tramo (salida, entre paradas, regreso) o, sin base, el umbral
fijo. Se descuenta lo normal **a favor de la persona**, y lo que el cargue
cubre.

### Señales (nivel conductor)

Todas: evidencia concreta, comparación contra **otros conductores de la misma
ruta maestra** con su `n` (las rutas propias no son pares), «sin base» con
menos de `n_minimo_pares`, `que_hacer` = preguntarle. **Proponen, no
sancionan** (regla 2 de `flota/CLAUDE.md`).

`tiempo_no_explicado` · `duracion_ruta` (> p90) · `km_entre_turnos` (km
sumados con la sede como custodio entre dos turnos de conductor; aparece en el
día de quien entregó antes y de quien recibió) · `rechazo_<motivo>` (cliente
cerrado, fuera de horario, no pagó y se quedó, dirección errada: observado vs
`Σ paradas × tasa de los compañeros`) · `ruta_cruza_el_dia` ·
`fuera_de_sede_frecuente` · `rechazos_sin_ubicacion` · `preoperacional_rapido`
(p10 de los compañeros, misma plantilla) · `descuentos_en_la_puerta` ·
`tiempo_hasta_liquidar` · `rafaga_de_confirmaciones` (**solo** con hora del
teléfono).

Las de **vehículo** (km en días sin ruta, custodio ≠ conductor de la ruta,
galones) son de la bandeja del encargado: no se reimplementan acá; se unifican
al integrar.

**Umbrales en un solo sitio**: `UMBRALES_POR_DEFECTO`, ajustables con
`JORNADA_UMBRALES` (JSON). Un ajuste inválido no se aplica y se declara
(`umbrales_rechazados`). Todos van en cada respuesta. Son **provisionales**.

### Hora del teléfono

Las columnas (`ts_dispositivo`, `ts_desfase_s`, `via_cola` en
`recaudos_entrega`; `ts_dispositivo`, `pos_ts_dispositivo` en `entregas_geo`,
de la tanda de fugas) se leen **por inspección de la base**, no del modelo:
con ellas o sin ellas funciona. **Sin ellas no se calculan ráfagas** —una
ráfaga de sincronización y una de «confirmé todo desde la esquina» son
idénticas con hora del servidor— y la respuesta y la pantalla lo declaran.

### Lo que NO puede ver (va en cada respuesta, `no_puede_ver`)

- **La salida física**: `fecha_cierre` es el cierre del manifiesto.
- **Dónde estuvo el vehículo entre dos eventos**: sin GPS del vehículo, un
  tramo no explicado no dice a dónde fue.
- **La hora real de una parada sin señal** mientras la app no mande la del
  teléfono.
- **Quién cerró la ruta**; el orden y la hora **planeados** (la maestra solo
  ordena municipios); horas extra, pausas legales, viáticos — no mide
  cumplimiento de horario.
- La ubicación del cliente como verdad: `clientes_geo` se arma con capturas de
  los conductores; con un solo conductor por cliente, compararlo es circular
  (por eso esta vista no lo hace; la distancia la mide la tanda de fugas).

### Fase 1 y Fase 3 — dónde entran

`FUENTES_DE_EVENTOS` es la tupla de funciones que producen `Evento`s (tipo,
ts, fuente, confianza, lat/lon). **Fase 1**: una tabla `jornada_evento` (GPS
automático al abrir la ruta y cada parada, «salí», «volví») y la **hora
planeada de salida por maestra** entran como una función más; con eso
«cierre del cargue» deja de ser la proxy de la salida. **Fase 3**: el GPS del
vehículo por la API del proveedor, igual. Huecos, cobertura y señales no
cambian.

### Aviso legal (Ley 1581 de 2012)

Antes de usar esta vista para hablar con un conductor, él tiene que haber sido
**informado por escrito** de la finalidad —control operativo de la ruta, del
vehículo y del dinero recaudado— y cubre **solo su jornada laboral**. El texto
va en cada respuesta y al pie de la pantalla. La decisión de informar es del
dueño/RR. HH., no del sistema.

### Cableada (2026-09-24)

Flota → **🕒 Jornada** (entre Señales y Vehículos): `flotaSubtab('jornada')`
esconde la bandeja y la analítica, muestra `#flota-jornada` y llama
`flotaJornadaCargar('flota-jornada')`. Las dos rutas salieron de
`DEUDA_SIN_UI`. `tests/flota/test_subtab_jornada_js.py` prueba el cable en Node
(mutación: quitar la llamada lo pone rojo).

**Hallazgo del guard de alcance** (sin tocar, es de otro archivo): el detector
de `test_frontend_integrity` lee un `nombre(` en un **comentario de nivel de
módulo** como llamada de arranque. El encabezado de este módulo decía
`flotaJornadaCargar(el)` y bastó para declarar «conectadas» dos rutas sin
ningún botón. Se esquivó escribiendo el nombre sin paréntesis; el hueco
queda para quien sea dueño del guard.

### Tests

`tests/test_jornada_conductor.py` (86): mundo armado con `traspasar`,
`inspecciones.registrar`, `RutaService.confirmar_parada` y
`_marcar_liquidada`; cada señal con su caso que dispara y el que no (sin base,
sin dato, rutas propias), con y sin columnas de hora del teléfono, permisos
por rol, 400/404, render en Node con `util.js` real (dato malicioso escapado,
`onclick` solo con posiciones), trinquetes de vocabulario (ni «muerto» ni
«sanción» en código ni pantalla, con meta-tests) y de umbrales (todo umbral
leído existe y todo umbral declarado se lee). **20 mutaciones, las 20 rojas.**

Rendimiento medido (SQLite, 12 conductores, 183 días, 26.352 paradas):
resumen de 92 días 2,5 s; un día 0,9 s.

Suite completa en este worktree (2026-09-24, sobre qa `ed730f7`): **7524 passed, 0 failed** (5 skipped, 19 xfailed).

## Contado contraentrega vs crédito real (2026-09-24)

**Regla del dueño:** las facturas de ruta salen en Siesa como «crédito» corto
solo por lo que tarda la ruta; en la calle se cobran **contraentrega**. *«Hasta
15 días las condiciones de pago son de contado, después es crédito. Si digamos
tiene 1 día, debes tener en cuenta que toca esperar el despacho y también el
tiempo de ruta.»* Si un cliente de contado no paga, la mercancía **vuelve**
(`NO_PAGO`); «no pagó y se quedó» sigue siendo la excepción con evidencia.

**La clase, no el caso:** *una decisión de cobro tomada fuera de la política*.
El caso era C03 (8 días): `cobra_en_la_puerta` cobraba solo con C01 o
exactamente `SIESA_COND_PAGO_RUTA`, así que C03 salía «💳 no se cobra», la
forma de pago quedaba CREDITO sola, la liquidación no hacía nada y la
reconciliación la sacaba del denominador. EXENTO pasaba en contado, un
ENTREGADO con $0 pasaba, y sin condición («no sé», `None`) la guarda no
actuaba.

### Una política, una función — `app/services/cond_pago.py`

`cobro_contraentrega(codigo) → {cobrar, dias, codigo, origen, umbral}`. **Solo
`cobrar=False` si los días se CONOCEN y superan el umbral.**

| Origen | Cuándo | ¿Cobra? |
|---|---|---|
| `MAESTRO` | código en la tabla | ≤ umbral sí (C01 0, C02 1, C03 8); > umbral no (C04 30 … P10 180) |
| `SUPUESTO_AUSENTE` | sin código (vacío o no se pudo leer) | sí, y la pantalla lo dice |
| `SUPUESTO_DESCONOCIDO` | código que la tabla no tiene | sí, declarado |
| `SIN_MAESTRO` | tabla vacía | sí, declarado |
| `NO_APLICA` | traslado entre sedes (no se guarda) | no: no es una venta |

**La tabla** es copia del reporte «CONDICIONES DE PAGO» de Siesa
(`docs/siesa-specs/Condiciones de pago.pdf`, 17/04/2026), columna **«Dias
Vcto», nunca la descripción** (P08 dice «40 DIAS» y vence a 180; P09 y P10
también a 180). Un test la compara contra el PDF con `pdftotext`.

| Variable | Defecto | Qué hace |
|---|---|---|
| `COBRO_CONTRAENTREGA_MAX_DIAS` | `15` | Umbral. Inválido (vacío, texto, negativo) → 15, declarado en el health |
| `SIESA_COND_PAGO_DIAS` | — | JSON `{"C10": 20}`: sobreescribe por código. Entradas inválidas se ignoran y se declaran |
| `CONNEKTA_CONSULTA_COND_PAGO` | — (sin default) | Gancho para una consulta dinámica del maestro (probablemente `t208_mm_condiciones_pago`; hoy **no hay API, 401**). Si existe y responde, **manda**; `/api/health/siesa` → `cobro_contraentrega.divergencias_con_siesa`. Se refresca con TTL de 6 h en los momentos con señal (listar paradas, health); **ninguna decisión de cobro sale a la red** |

Las tres están en `vars_criticas` (con default / condicional) y el health
publica `cobro_contraentrega` (umbral, tabla, fuente, problemas, divergencias).

`cobra_en_la_puerta` y `modo_pantalla` derivan de ella; `clasificar` y
`aprobable_en_ruta` (¿Siesa aprueba la FE?) **no cambiaron**: es otra pregunta.

### El snapshot (m043contado, aditiva, nullable, sin backfill)

`tareas_packing`: `cond_pago_fe` (lo que la FE lleva de verdad), `dias_credito`,
`cobro_contraentrega`, `clasif_origen` (CHECK), `clasif_en`. Se escribe lo
antes posible, **sin red**: al crear el packing (`pedidos_historia.cond_pago`
por `pedido_clave`), al emitir la FE (el gateway devuelve `cond_pago_emitida`;
no en ensayo), al iniciar/despachar la ruta y al listar paradas (lee
`f461_id_cond_pago`, que ya viene en `get_rowids_factura` — **verificado en
vivo contra QA: FEW-1467 → C04**). **Si la FE difiere del pedido, gana la FE y
se declara** (`cond_pago_difiere` en la parada, log). La primera lectura del
pedido no se pisa (condición cambiada en Siesa después de cargar).

`cobro_de_tarea` **recalcula** sobre los códigos anotados (barato, sin red): un
cambio de tabla o de umbral aplica ya, y el snapshot no puede desfasarse de los
códigos que lo produjeron. Las columnas son la copia para reportes.

`recaudos_entrega`: `cobro_contraentrega` **congelado al confirmar, solo si la
clasificación era leída** (MAESTRO). Un supuesto no se congela —queda NULL,
declarado— para que la condición real, si aparece después, decida. Más
`credito_autorizado_por`/`_razon`/`_en`.

### La guarda — en el SERVICIO (`confirmar_parada`)

Con `version_formulario >= 3` (`cond_pago.VERSION_FORMULARIO_CONTADO`), sobre
una parada que se cobra (incluido el supuesto):

- **CREDITO y EXENTO se rechazan** en ENTREGADO y PARCIAL;
- **ENTREGADO con $0**, o con `monto + descuento < valor_factura − tolerancia`
  (`tope_diferencia_recaudo`, $100) → «si no pagó, marcá No pagó; si pagó una
  parte, Parcial»;
- RECHAZADO (`NO_PAGO`, `NO_PAGO_SE_QUEDO`) **siempre disponible**: la guarda no
  lo toca, aunque el select traiga un CREDITO viejo.

Un ítem viejo de la cola (sin versión o < 3) **no se traba**: conserva solo la
regla de antes (`regla_anterior_rechaza_credito`: CREDITO sobre contado o ruta)
y lo demás llega a la liquidación como `credito_no_autorizado`. Aplica también
al admin que confirma a mano (la salida para él es «autorizar como crédito»).

### La pantalla del conductor (`rutas.js`, offline)

Todo viaja en el payload de paradas que el teléfono cachea:
`cobro_contraentrega`, `dias_credito`, `cond_pago_fe`, `clasif_origen`,
`cobro_etiqueta` (`etiqueta_conductor`), y arriba `formas_que_no_cobran`,
`tolerancia_cobro`, `version_formulario`. `_condFormasPago(p)`: en contado (y
supuesto) el select **no ofrece CREDITO ni EXENTO**. El aviso:
«Contado contraentrega · C02 (1 día) — cobrá al entregar» · «Crédito 30 días —
no se cobra» · «Sin condición de pago: cobrá al entregar» (tono de advertencia).
Una caché vieja sin los campos cae a `modo_pago`. Validación local de la misma
regla del servidor, para no encolar una parada que va a volver rechazada.

### La liquidación

`cond_pago.trato_de_cobro(recaudo)` → `CONTADO` (entró plata: RC) · `CREDITO`
(crédito real o autorizado) · `CREDITO_NO_AUTORIZADO`. Ramifican por él
`_procesar_recaudo`, `preview_acciones_recaudo`, `registrar_cobro_recaudo` y
`liquidacion.js`, **no por `forma_pago`**. Un no autorizado:

- **nunca al contador `credito`**: va a `errores` con `codigo:
  'credito_no_autorizado'` y a `resumen.credito_no_autorizado`;
- en PARCIAL la devolución pendiente se arma igual (la mercancía volvió y
  recepción tiene que poder recibirla);
- **`RutaService.liquidar_ruta` no marca LIQUIDADA** mientras haya uno.

**Autorizar como crédito**: `POST /api/rutas/<ruta>/recaudos/<id>/autorizar-credito`
(`_solo_admin`, como liquidar), **razón obligatoria**, columnas del recaudo +
bitácora `EDITAR` (sin verbo nuevo). Solo sobre lo que hoy es no autorizado.
No toca forma de pago ni monto: cambia cómo lo trata la liquidación. Botón en
la tarjeta del recaudo (`liqAutorizarCredito`, solo ids en el `onclick`).

### El despacho informa, no bloquea

`RutaService._informe_de_cobro` al iniciar el cargue y al despachar:
`cobro_supuesto`, `fe_contado` (contado documental) y `fe_saldada` (solo si
`cxc_cruce.esta_saldada` lo sabe: `None` no dice nada; tope 40 consultas, se
deja de preguntar al primer fallo; nunca en simulación). No pide motivo —la
política ya cobra ante la duda— y de paso escribe el snapshot de cada tarea.
Llega en `informe_cobro` y el muelle lo muestra como aviso. Traslados excluidos.

### Vencimiento de la FE — `cond_pago.vencimiento_fe` (decisión del dueño)

`F353_FECHA_VCTO` del **142943** y del **238925** (muerto): contado
contraentrega (incluido el supuesto) → `fecha_factura + umbral` (15: despacho
+ ruta; C02 a 1 día vencería antes de que salga el camión); crédito real →
`fecha_factura + sus días` (C04 +30, C05 +45). Fecha Bogotá (la de
`F350_FECHA`), `YYYYMMDD`. El campo y su formato no cambiaron: el
`test_payload_vs_docx` del 142943 sigue verde. **Las FE ya emitidas quedan con
+30 (sin backfill)**: hasta este cambio el gateway mandaba `hoy + 30` fijo a
toda FE y Siesa lo respeta — por eso la cartera no servía para saber la
condición. `F353_FECHA_DSCTO_PP` va con la misma fecha.

### Reportes que leen la política

| Dónde | Qué |
|---|---|
| `/api/rutas/liquidacion/desglose` | `por_dias_credito` (código, días, contado/supuesto/crédito real) y `credito_no_autorizado` (lista, `valor_sin_cobrar`, `sin_valor`); cada parada CRÉDITO trae `credito_no_autorizado`/`credito_autorizado` |
| 💸 Fugas | fuga nueva **«Crédito no autorizado»** (`valor_factura − cobrado`; en PARCIAL es cota superior) |
| 🧭 Recorrido | un CREDITO sobre contado **ya no es «cobro ok»**: corte `CREDITO_NO_AUTORIZADO` |
| Auditoría | **VTA-62** (BLOQUEA, detector ciego en `test_contado_contraentrega.py`) |
| Reconciliación | `_debia_cobrarse` usa la política; el supuesto entra al denominador y se cuenta en `paradas_sin_condicion`; el crédito autorizado sale |

### Hallazgos (sin cambiar comportamiento)

- **«Una FE de contado no se aprueba» no es universal.** En QA ~24 FE C01
  emitidas por el WMS quedaron **Aprobadas**, con cartera a 30 días (el `+30`
  fijo). Lo del 2026-08-13 (dos FE de contado en Elaboración) sigue siendo
  cierto para esos casos, pero no alcanza para afirmar que ninguna se aprueba.
  `aprobable_en_ruta`, VTA-61 y la alerta `FE_CONTADO_NO_APROBABLE` no se
  tocaron.
- **La cartera de las FE del WMS se indexa por FACTURA**, no por pedido
  (comentario corregido en `cxc_cruce.py`; la búsqueda sigue probando pedido y
  después FE, sin cambio).
- En vivo (QA, solo GET, 2026-09-24): de 50 FE del CO 003 desde agosto, C02
  22, C01 13, C04 15; `get_rowids_factura` trae `f461_id_cond_pago` (FEW-1467 →
  C04). Del coordinador: `f430_id_cond_pago` (pedido) = `f461` (FE) en 72/72, y
  el maestro de clientes difiere del pedido en ~37 % — por eso no se usa.

### Trinquetes — `tests/test_contado_contraentrega.py` (151 tests)

- Por AST, **ninguna comparación de una forma de pago contra CREDITO/EXENTO**
  (`==`, `!=`, `in`, `not in`, literal o `FormaPago.X`) fuera de `cond_pago.py`;
  inventario de 2 (desglose y tablero, reportes) que solo encoge.
- Por AST, **ninguna función lee `.cond_pago`/`.cond_pago_fe` sin llamar a la
  política de cobro**; inventario de 2 (`to_dict`, VTA-61 documental).
- Por AST, **ningún `F353_FECHA_VCTO` fuera de `vencimiento_fe`**; inventario
  de 2 (NC 251126 con el vencimiento real de la factura que cruza; DC de
  retención con la fecha del día).
- Meta-tests (las escrituras que debe ver, lo sano que no, docstrings y
  comentarios), pisos, y Node con `util.js` real para el select y el aviso.
- **20 mutaciones, las 20 rojas**, entre ellas `<=`→`<` (15 días), quitar EXENTO
  de la guarda, el vencimiento a los días de la condición, volver a `+30`, el
  pedido mandando sobre la FE, congelar el supuesto, autorizar sin bitácora.

Tests viejos que codificaban la política anterior se reescribieron con su
porqué (`test_cond_pago.py`, `test_cobra_en_la_puerta.py`, reconciliación,
e2e, recorrido, `test_ruta_despacho_service`): donde un test necesitaba un
CREDITO legítimo, ahora el cliente es C04 (en el recorrido, sembrado en la
historia del pedido: ejercita el snapshot al crear el packing); donde una
entrega de contado pasaba con $0, ahora trae su cobro. EXENTO salió de la
lista de formas del select de contado en `test_flujo_conductor_pagos`.

Suite completa (2026-09-24, este worktree, `-m "not postgres"`, TZ=UTC):
**8039 passed, 0 failed**, 5 skipped, 19 xfailed.

### Lo que NO cubre, dicho

- **Granularidad por función** del trinquete de condición cruda: una lectura
  cruda agregada dentro de una función que YA llama a la política no se ve
  (probado: la mutación en `_procesar_recaudo` sobrevive; en `_obtener_tercero`
  es roja).
- **El trinquete de forma de pago mira Python**; en el JS quedan comparaciones
  de presentación (`liquidacion.js` usa `trato_cobro` del servidor con
  respaldo a `forma_pago` para un servidor viejo).
- **La tabla es una copia**: sin la consulta dinámica, una condición nueva en
  Siesa es `SUPUESTO_DESCONOCIDO` (se cobra) hasta que alguien la agregue a
  `SIESA_COND_PAGO_DIAS`.
- **Paradas anteriores al deploy** sin snapshot se juzgan con la tarea de hoy:
  un CREDITO viejo sobre contado aparece como no autorizado y bloquea la
  liquidación de su ruta hasta autorizarlo.
- **El KPI diario** (capa semántica) no tiene todavía la métrica de crédito no
  autorizado.
- La consulta de cartera del despacho (`fe_saldada`) sale a la red: acotada y
  sin insistir, pero agrega latencia al despacho con Siesa lento.

### Decisiones abiertas para el dueño

1. ¿Quién autoriza crédito? Hoy `_solo_admin`. ¿También supervisor/jefe con tope?
2. ¿El vencimiento de contado es la ventana entera (15) aunque la ruta sea del
   mismo día? Es lo decidido; alternativa: días de la condición + margen fijo.
3. Registrar la consulta del maestro de condiciones en Connekta
   (`CONNEKTA_CONSULTA_COND_PAGO`) para no depender de la copia del PDF.
4. Paradas viejas bloqueadas por `credito_no_autorizado`: ¿autorizarlas en lote
   con una razón común, o una por una?

## Flota: las costuras del rediseño (2026-09-24)

El rediseño de Flota lo hicieron cinco frentes en paralelo (bandeja, conductor,
permisos, jornada, fugas). Cada pieza quedó bien; **las uniones no**: tres
pantallas contestaban la misma pregunta con tres políticas, dos detectores de
«km que nadie explica» usaban criterios distintos, y la pantalla le prometía
al encargado botones y pestañas que no existían.

### P0 · «¿Puede salir este camión?» — UNA política

`flota/dominio/salida.py` (puro) decide el nivel y escribe el texto;
`flota/adaptadores/salida.py` traduce filas a hechos con **una función por
hecho**, la misma para la bandeja (que lee en bloque) y para
`evaluar_vehiculo` (uno: el despacho y el conductor).

| Pantalla | Antes | Ahora |
|---|---|---|
| Semáforo de la bandeja | `senales.semaforo`; no sabía de la OT abierta | `Evaluacion.semaforo()` |
| Advertencias al despachar (`senales_ruta.advertencias_de_flota`) | lista propia; **no sumaba el daño bloqueante ni el preventivo vencido** | `Evaluacion.para_despachar()` (los motivos que `frena`) |
| Aviso del conductor (`mi-turno` → `flotaCondAvisos`) | decidía el color **en el teléfono** y lo llamaba «amarillo» | el servidor manda `estado_vehiculo.salida = {color, motivos}`; el JS pinta |

- **Un vocabulario**: `rojo` (no debería salir / actuar hoy) · `ambar` (con
  plazo **o no se sabe**) · `verde` («sin pendientes conocidos»).
- **Un texto por hecho**: «SOAT vencido desde 19/09/2026 (hace 5 días)» en las
  tres pantallas, en Pendientes y (con la misma forma) en Documentos.
- Cada motivo lleva `ambito` (`salida` lo ve el conductor · `turno` el despacho
  y el encargado · `gestion` solo el encargado) y `frena` (el despacho pide
  motivo escrito, FORZAR). **Informa, no bloquea.**
- Las claves del despacho se conservan (`soat_vencido`, `en_taller`,
  `custodio_distinto`…): los FORZAR viejos las siguen reconociendo. Nuevas:
  `dano_bloqueante`, `dano_vencido`, `preventivo_vencido`,
  `inspeccion_incompleta` (antes caía en `inspeccion_no_apta`).
- La bandeja **no** compara el turno con la ruta en el semáforo (eso es la
  señal `turno_de_la_ruta`, que otro frente está corrigiendo); el despacho sí.
- Papeles sin cargar se agrupan en UN renglón («Sin cargar, no se sabe si
  están al día: SOAT, revisión técnico-mecánica»).
- **Trinquete** `tests/flota/test_una_politica_de_salida.py`: por AST, ningún
  `'rojo'/'ambar'/'amarillo'/'verde'` ni `ROJO/AMBAR/VERDE` en `flota/` ni en
  `app/` fuera de `salida.py` (inventario vacío, solo encoge; meta-tests de lo
  que ve y lo que no; piso: la política misma da ≥ 10 positivos y se escanean
  ≥ 200 archivos). En el JS de flota, texto acotado sin comentarios: ningún
  nivel nace en el teléfono (tupla `['rojo', …]`, ternario, `nivel = 'rojo'`) y
  «amarillo» no existe. Y la costura entera: **un mundo, tres pantallas, los
  mismos motivos con las mismas palabras** (`TestTresPantallasUnaRespuesta`).

### P0 · Km entre turnos (jornada) con el criterio de la bandeja

`senales.km_sin_explicar(km, marca, tolerancia)` es el núcleo de «km que nadie
explica» para las dos preguntas: la bandeja (`km_sin_ruta`) y la jornada
(`km_entre_turnos`). La jornada restaba `km_inicio − km_fin` **sin mirar si se
les podía creer**: ahora busca la lectura que escribió cada traspaso y juzga el
tramo con `confianza_del_tramo`; en duda (o sin lectura) va a
`senales_no_evaluables` del día con el mismo motivo que la bandeja
(`MOTIVO_TRAMO_EN_DUDA`). **Ni el título ni el texto nombran personas**: los
turnos van en la evidencia, como contexto.

### P0 · Textos que prometían lo que no existe

«Rutas → Vehículos» (6 sitios de Analítica) → `flotaDondeSeDaDeAlta()`; «el
barrido también avisa por WhatsApp», «el botón Daños», «desde Inspección de
hoy» vivían en `flotaBloqueSalud`, que se retiró (abajo). Trinquete:
`test_costuras_flota.py::TestNingunTextoPrometeLoQueNoExiste`.

### P1

| | Qué | Dónde |
|---|---|---|
| 4 | Pendientes: **un renglón de papeles por vehículo** (con `lineas`), **km en duda agrupados por placa** («2 lecturas de kilometraje en duda», la última y su motivo) | `bandeja._pendientes` |
| 5 | Señales: evidencia con etiqueta y formato (hora de Bogotá, fecha, pesos, 2 decimales, miles); **una clave sin etiqueta no se pinta** (adiós `forma: turno_de_otro`, `1.500E+4`); «Lo que no se pudo revisar» agrupado por (señal, motivo) con sus placas. **Galones usa el criterio de confianza de Pendientes**: una ventana con un km en duda no es evidencia (`tanqueos_de` publica `lectura_id`) | `FLOTA_EVIDENCIA`, `flotaBandejaNoEvaluables`, `_senales_combustible` |
| 6 | **Despachos que salieron reconociendo advertencias** (el FORZAR del muelle) → Pendientes «Salidas con advertencias», con quién autorizó y por qué. Una función los lee: `senales_ruta.despachos_forzados()` — distingue el FORZAR de despacho (trae `advertencias_flota`) del cierre forzado de ruta | `senales_ruta.py`, `bandeja.py` |
| 7 | Tanqueo «Cambió»: pide **foto del tablero**; la lectura nace con `foto_id` (foto colgada de `entidad_tipo='odometro'`) y no «en duda». Con el km de siempre se reutiliza la lectura. Sin foto se pregunta y se registra igual (en duda) | `anclar_odometro(foto_tablero=)`, `POST /flota/tanqueos` `foto_tablero`, `flotaTanqueoFotoTablero` |
| 8 | Diagnóstico técnico fuera de Hoy, **plegado al final de Analítica** con el mismo health: todos los números del health en palabras y agrupados + calidad del km + lo que la ficha no dice + avisos a pedido. **Se retiró `flotaBloqueSalud`** (41 renglones) y de Analítica los paneles que repetían la bandeja (Recorrido de la semana, Inspección, Papeles, Custodia). Orden: procedencia → CPK → pesos por mes → rendimiento → taller → llantas → preventivo → ritmo → días de daño abierto → diagnóstico | `flota_analitica.js`, `flota_bandeja.js` |

### P2 · Un vocabulario

- **Un formateador de pesos**: `fmtPesos` en `util.js` (se fueron `flotaPesos`
  y `fjPesos`); `null` es «sin dato», nunca «$0».
- `flotaOpciones(grupo, lista)` en los formularios de gestión (ficha, gastos,
  taller, llantas, Documentos): el código en el `value`, la palabra a la vista;
  `sin_dato` es «No se sabe» en todos. Trinquete: ninguna
  `<option value="${x}">${x}</option>`.
- Documentos: «vencido desde dd/mm/aaaa (hace N días)», «Sin cargar» (y no
  «Sin verificar») ≠ «nadie lo pudo mostrar». Daño, no «hallazgo», en lo visible
  que se tocó; panel «Días de daño abierto».
- Jornada: los motivos de rechazo **los sirve el servidor**
  (`motivos_rechazo` en la respuesta, del catálogo único); «1 no
  reconstruible»; «Lo que esta vista no puede ver» **una vez** (al pie de la
  lista de conductores) y sin jerga (sin `entregar_ruta`, «Fase 1»,
  «manifiesto»); voseo («preguntale»).
- Plurales «(s)» en `flota*.js`: tope 54, solo encoge.
- **Ficha completa exige la capacidad del tanque** (`salida` y la bandeja):
  sin ella el texto dice qué detector queda ciego.
- `GET /flota/conductor/mis-reportes` → **`/mis-turnos`** (devolvía turnos y el
  botón dice «Mis turnos»).

### Tests y mutaciones

`tests/flota/test_una_politica_de_salida.py` (política, tres pantallas contra un
mundo, trinquete AST + JS con meta-tests y pisos) ·
`tests/flota/test_costuras_flota.py` (tanqueo con foto, despachos forzados,
ficha, evidencia, textos falsos, vocabulario) · reescritos:
`test_render_salud_js.py` (el diagnóstico, con el trinquete de campos mudos
intacto), las clases de «Salud» de gastos/llantas/taller/verificación/
preventivo, `test_render_analitica_js.py` (paneles y orden nuevos),
`test_bandeja.py`, `test_bandeja_js.py`, `test_mi_camion_hoy_js.py`,
`test_el_conductor_ve_lo_vencido_js.py` (el doble se arma con la política real),
`test_jornada_conductor.py`, `test_fugas_ruta.py`, `test_senales_dominio.py`.
**24 mutaciones, las 24 rojas** (se verificó que cada texto a mutar existiera
una sola vez; la M11 apuntaba primero a un texto que no estaba y se corrigió
antes de contarla): política (vencido a ámbar, bloqueante sin frenar, OT fuera),
despacho con lista propia, un nivel literal en Python y otro en el JS, teléfono
sin `salida`, jornada sin confianza y con nombres, galones sin confianza, km en
duda desagrupados, FORZAR confundido o no leído, foto del tablero perdida (servidor
y teléfono), ficha completa sin tanque, evidencia cruda, no evaluables sin
agrupar, papeles uno por papel, Papeles de vuelta en Analítica, campo del health
mudo, copia del catálogo de motivos, opción con código crudo, «no puede ver» en
cada día. Tanda de los tests de flota + jornada + fugas + guards de PWA: 3.257
en verde y 6 rojos corregidos (455 re-corridos en verde).

### Lo que NO se cubre, dicho

- **Verde con pendientes de turno**: el semáforo no suma cierres forzados,
  turnos sin fotos ni despachos forzados (son del turno, no de si el camión
  puede salir). Un vehículo puede estar verde con un pendiente de turno.
- El **rendimiento** de referencia de galones se sigue calculando con todas las
  ventanas (dudosas incluidas): lo que cambió es qué ventana se juzga.
- La jornada busca la lectura del traspaso por (vehículo, instante, km): un
  turno anterior a que el traspaso escribiera su lectura sale «no evaluable».
- El vocabulario visible (daño/turno/papel) se barrió en lo tocado; queda
  «hallazgo»/«custodia» en textos de gestión que no se tocaron (medido: sin
  trinquete, que tendría que separar identificadores de texto).
- Coordinación con el frente que corrige en paralelo `turno_de_la_ruta` al fin
  del día, fotos de inicio en custodias de sede, ángulo desconocido, FORZAR de
  despacho leído como cierre forzado (jornada ~461, analitica_salud ~1036) y
  códigos crudos: no se tocaron esos puntos. **Roces para el integrador**:
  `senales.py` (quité `semaforo`/`HechosDelVehiculo` y cambié `__all__`), la
  sección `_senales_turno_de_la_ruta` de `bandeja.py` no se tocó, y
  `despachos_forzados()` ya distingue los dos FORZAR — la jornada y
  `analitica_salud` pueden leerla en vez de su propia consulta.

### Propuesto, no hecho (el catálogo `analitica_kpi.METRICAS` lo cambia otro frente)

KPI diarios de flota con meta, cuando se integre: **% de despachos que salieron
reconociendo advertencias** (de `despachos_forzados`, ↓, dueño control de flota),
**vehículos en rojo al abrir el día** (de la política de salida, ↓),
**% de lecturas de km verificadas o con foto** (↑; enciende las señales de km y
galones), **días de daño abierto** (ya en Analítica, sin meta).

Suite completa: la corre el integrador (el Mac estaba saturado).

---

## Analítica: solo lo actual (2026-09-24)

La Analítica, la 🩺 Salud del dato, la auditoría de invariantes y el tablero
mostraban como «errores» cosas que no son de hoy: jobs FALLIDO del ensayo de
abril ya superados por un reintento, hallazgos anteriores a la corrección de su
defecto, paradas juzgadas con una regla de contado que no existía cuando se
confirmaron, la limpieza técnica del layout contada como cancelación. **Un canal
donde la mitad de lo que grita es viejo entrena a no mirarlo** (la lección de
los 639 avisos conocidos).

**La clase:** *un lector que no distingue lo vigente de lo histórico*. Nada se
borra: lo viejo **se cuenta aparte y no sube el nivel ni el veredicto**.
Trinquete: `tests/test_analitica_solo_lo_actual.py` (115 tests, 29 mutaciones,
las 29 rojas).

### 1 · `FECHA_INICIO_AUDITORIA` — `app/services/corte.py`

| | |
|---|---|
| Formato | ISO en hora de **Bogotá**: `2026-10-01` (00:00 Bogotá = 05:00 UTC), `2026-10-01T06:00`, o con zona explícita (`…Z`, `…-05:00`) |
| Sin variable | Sin corte, y **se declara** (`meta.corte.texto`: «Sin FECHA_INICIO_AUDITORIA: se muestra todo…») |
| Inválida | **No corta nada** y se declara inválida (cortar con una fecha adivinada escondería errores reales — Regla 0) |
| Futura | Se acepta y se declara (`futuro: true`): todo lo de hoy cuenta como anterior |
| Registro sin fecha | **Vigente**: no saber cuándo pasó no lo vuelve viejo |

Una función por pregunta: `inicio_auditoria()` (UTC naive), `dia_de_corte()`,
`es_anterior(fecha)`, `recortar_rango(desde, hasta)`, `separar(filas,
fecha_de)`, `estado()` (lo que va en el `meta` de cada vista).

| Lector | Qué hace con lo anterior |
|---|---|
| Auditoría (`auditoria/base.auditar`) | Cada `Hallazgo` lleva `fecha=` (la columna de nacimiento de su entidad). Lo anterior va a `antes_del_corte` por invariante y en el total; **no** entra a `total` ni a `bloqueantes`. Los agregados (TRA-12, TRA-30, TRA-31, INV-03) llevan la fecha del miembro más nuevo |
| Salud | Invariantes: `antes_del_corte` y `artefactos` por flujo; cola de Siesa desde el corte; cobertura de `pedido_clave` desde el corte; `meta.corte` |
| Fugas | El período empieza en el corte; el anterior se recorta y, si cae entero antes, **no hay base** (`sin_base`, no «bajó»); el tramo recortado se evalúa aparte en `antes_del_corte` por fuga; `meta.corte` |
| Recorrido | Pedidos que entraron antes del corte: fuera del embudo, en `meta.antes_del_corte` |
| KPI diario | Los días anteriores se guardan y se muestran marcados (`antes_del_corte`), pero **no** entran al período, a la comparación ni a las semanas de la alerta. `ventas_facturadas`, `cartera_abierta`, `cartera_vencida` leen Siesa: **sin corte** (`METRICAS_SIN_CORTE`) |
| Rezago de liquidación | `rutas_entregadas_sin_liquidar` = desde el corte; `diagnostico()` y `rutas_sin_liquidar_al_cierre()` cuentan `antes_del_corte`. La alerta de las 06:30 y la fuga «Plata en la calle» heredan |
| Dashboard | Toda cola cuenta desde el corte (`_cola`), con `antes_del_corte` por bloque |
| Patrones de la bitácora | Desde el corte, `antes_del_corte`; **la lista** (`/bitacora`) sigue mostrando todo: es el registro |
| Resumen diario por correo | «FALLIDO > 24 h sin resolver» desde el corte |

**Trinquetes (AST):** la variable la nombra solo `corte.py`; ningún invariante
corta con una fecha literal (`datetime(2026, …)`); todo `Hallazgo(...)` pasa
`fecha=` salvo `SIN_FECHA` (5, solo encoge: VTA-01/02 y INV-09 son estado
actual, DEV-04/05 los rehace el agente de devoluciones); y cada lector de
`LECTORES_CON_CORTE` (21) llama a la función del corte que le toca.

### 2 · `siesa_job_service.fallidos_vigentes` — la única que cuenta FALLIDO

Un FALLIDO **deja de estar trabado** si lo superó un COMPLETADO **posterior**
(por id) del mismo tipo y referencia, o si la reconciliación lo cerró:
`ReconciliacionService.reconciliar_despacho`, al encontrar la factura en Siesa,
pasa los `DESPACHO_F470` FALLIDO de esa tarea a COMPLETADO con
`resultado.reconciliado` (en la misma transacción). Separa **recientes** (último
intento en `DIAS_FALLIDO_RECIENTE` = 7) de **viejos**, con edad por tipo
(`desde_utc`, `edad_dias_max`). Edad = `fecha_procesando` o, si nunca se
procesó, `fecha_creacion` (`siesa_jobs` no guarda «falló el…»: declarado).

- **Salud**: solo los recientes ponen CRÍTICO; los viejos, ADVERTENCIA con su
  edad; los superados y los anteriores al corte se nombran y no cuentan.
- **Fuga «Documentos trabados»**, **KPI `jobs_siesa_fallidos`**, el monitor de
  `/api/siesa/monitor`, `/api/health/siesa` (`dlq`; y `corte_auditoria` publica el corte de ese servicio), el desglose de rutas y el
  resumen diario la usan.
- **Descartar**: `POST /api/siesa/jobs/<id>/descartar` (`_solo_admin`,
  `{"motivo"}` obligatorio) → DESCARTADO + bitácora `DESCARTAR` con el error que
  se deja de mirar. **Regla 3: no reenvía nada** ni toca las banderas de su
  referencia. Botón «Descartar» en Siesa → Recuperación (solo el id en el
  `onclick`). De paso: ese panel leía `j.ultimo_error`, que no existe
  (`error_ultimo`): el motivo del fallo salía vacío.

**Trinquete:** ninguna función de `app/`/`flota/` lee `FALLIDO` de un
`SiesaJob` (`EstadoSiesaJob.FALLIDO` fuera de una asignación, o
`SiesaJob.estado` contra `'FALLIDO'`) salvo `CONSULTAS_FALLIDO_OPERATIVAS` (22,
solo encoge): las que OPERAN —reintentar, descartar, reutilizar el job,
decidir si una mercancía sigue en proceso—. Los módulos que muestran números
(`analitica_*`, `dashboard_service`, `health`) no pueden estar en esa lista.

### 3 · `@invariante(defecto_corregido=(commit, fecha))`

Para el invariante que vigila un defecto **ya corregido**: lo anterior a la
corrección se reporta como «artefacto de <commit>: N casos» y **no sube el
nivel**; lo posterior sí bloquea (el arreglo se cayó). La fecha es la del
commit, la más temprana posible del despliegue (ante la duda, se muestra de
más).

| Invariante | Commit | Defecto |
|---|---|---|
| VTA-50 | `359eac0f` (2026-09-24 15:44) | `entregar_ruta` marcaba ENTREGADO lo que el conductor no declaraba |
| VTA-61 | `75c168c8` (2026-08-13 19:56) | el fallback emitía contado a todo pedido sin condición |
| VTA-62 | `d5010187` (2026-09-24 17:35) | la regla ≤ 15 días nace con m043contado |
| CNT-07 | `02d4a80f` (2026-08-15 08:26) | la aprobación sobre base del WMS se niega |
| TRA-01 | `d5e78970` (2026-08-20 06:57) | el CHECK de m013 impide la cadena que crece |

VTA-22 **no** se marcó: no vigila un defecto corregido, vigila un estado
legítimo (la reapertura). El inventario es exacto en el test; un invariante con
`defecto_corregido` no puede tener hallazgos sin fecha.

### 4 · La regla de contado no es retroactiva

`cond_pago.anterior_a_la_regla(recaudo)`: sin snapshot (`cobro_contraentrega`
NULL) **y** `fecha_confirmacion` anterior a `REGLA_CONTADO_DESDE`
(`2026-09-24T17:35:31-05:00`, commit d5010187). Esas paradas se juzgan con la
regla de entonces (`_trato_regla_anterior` → `regla_anterior_rechaza_credito`):
un CREDITO con $0 de antes **no** es crédito no autorizado, salvo que la regla
vieja ya lo rechazara (CREDITO sobre el código de contado o de ruta). Lo leen
`trato_de_cobro` —y por él la liquidación, el recorrido, las fugas, VTA-62 y
`RutaService.liquidar_ruta`— y `reconciliacion_ruta._debia_cobrarse`. **Esto
destraba la liquidación de las rutas viejas** que la sección «Contado
contraentrega» dejaba bloqueadas hasta autorizar una por una.

### 5 · KPI `cancelaciones` — solo el flujo de negocio

`ENTIDADES_DE_NEGOCIO` (lista blanca): TareaPicking, TareaPacking, Bulto,
RecepcionMercancia, SolicitudTraslado, TareaReposicion, DevolucionCliente,
TareaDevolucion, RutaDespacho, RecaudoEntrega, SesionConteo. Lo demás
(Ubicacion del layout, SiesaMapeoUnidades, maestros, juicios de temporada) va a
`detalle.tecnicas`: se cuenta, no sube el número.

### 6 · Dashboard

- **«Conteos con diferencia» = raíces en DESCUADRE esperando decisión**
  (`tablero_lider_conteo.filtro_descuadres_por_decidir`, la misma del tablero
  del líder). Contaba SEGUNDO_CONTEO —un 1er conteo esperando el 2º—, que es
  otra cosa: ahora va en `en_segundo_conteo`.
- **Edad de lo más viejo** en cada cola (`mas_viejo_utc`, `edad_horas`):
  picking, packing, conteos pendientes, con diferencia, definitivos, traslados
  y rutas en marcha. La tarjeta la pinta («lo más viejo: 5 h / 4 días»).
- **IRA de hoy por cadenas** (`conteo.ira_hoy`, `metricas.conteo.exactitud_total`:
  la misma del KPI y de las estadísticas); sin tasa con n < 30. `match_hoy` pasa
  a ser cadenas exactas (antes sumaba el CC2 que confirma).
- **Semáforo de conteo** del servidor (`semaforo_de_conteo`): rojo con
  decisiones esperando; amarillo si las pendientes vivas superan el cupo diario
  (o el cupo no se pudo leer); verde dentro del cupo; gris sin nada.

### 7 · 8 · Pantallas (lo mínimo, el resto lo rehace el agente de pantallas)

- `liquidacion.js`: la tarjeta «Facturas emitidas como contado por dato
  faltante» pinta **`a_revisar_en_siesa`** (el color lo manda eso), con los
  pedidos sin condición al lado; el rezago muestra los anteriores al corte; el
  día por defecto es **Bogotá** (`liqHoyBogota`; `toISOString()` daba mañana
  después de las 7 p. m.).
- `tablero_bi.js`: día por defecto Bogotá (`biHoyBogota`) y `biFechaHora` lee
  una hora **sin zona** como UTC y la muestra en Bogotá (la corría 5 h).

### 9 · Vigía — propuesta, NO hecha

Reiniciar la línea base del CUSUM de Vigía (`serie_vigia`) en el corte
**exige recertificar el canon de Florencia** (`docs/canon_florencia.json`,
`scripts/verificar_carga_vigia.py`): cambia μ_ref/σ_ref y con ellos todas las
alarmas. Costo: una corrida de certificación con los TXT originales y ~8
semanas sin alarma nueva. Las series de facturación tienen su referencia en
el **TXT histórico de Siesa** (anterior al ensayo): el ensayo del WMS no las
ensucia. Las que sí la tienen del WMS son `adopcion_picking`/`brecha_picking`
(`alimentar_adopcion_picking`): la propuesta es excluir de su ventana de
referencia las semanas anteriores a `FECHA_INICIO_AUDITORIA`, **con
recertificación** y con el costo dicho. **No se tocó.** La alerta del KPI diario (que usa `cusum_bilateral` sobre las
semanas del KPI) sí excluye las semanas anteriores al corte
(`semanas_antes_del_corte`): con un corte reciente, **no habrá alerta del KPI
hasta tener 8 semanas cerradas después del corte** — declarado en la respuesta.

### Lo que NO cubre, dicho

- **Jobs reconciliados antes de este cambio** siguen FALLIDO: la marca
  (`resultado.reconciliado`) solo existe hacia adelante. Si son anteriores al
  corte no cuentan; si no, se descartan con motivo (el botón nuevo).
- **Un superado sin referencia** no se puede detectar (`sin_referencia` lo
  cuenta).
- **La regla de contado**: una parada confirmada entre el commit d5010187 y el
  despliegue real se juzga con la regla nueva (se muestra de más).
- **El corte es por nacimiento de la entidad** (`fecha_creacion`/
  `fecha_confirmacion`): una tarea del ensayo que se cierra mal después del
  corte cuenta como anterior. El acta de corte (`reset_transaccional`) sigue
  siendo la forma de vaciar lo operativo; el corte es para lo que no se vacía
  (tablas protegidas) o mientras el acta no se corre.
- **La bitácora como lista** no se filtra (es el registro); la **cola de la
  DLQ** para operar tampoco: se ve todo lo que se puede reintentar o descartar.
- **Pantallas**: la Analítica la rehace otro agente; los campos nuevos
  (`antes_del_corte`, `artefactos`, `corte`, `fallidos_recientes/viejos`,
  `edad_dias`) están en las respuestas para que las pinte.

### Qué tiene que hacer el dueño para que el corte funcione

1. **Desplegar** este cambio (QA primero; sin migración).
2. **Elegir la fecha**: el primer día de operación real — el día siguiente al
   acta de corte, o el día en que se empezó a operar de verdad si el acta no se
   corrió. En hora de Bogotá.
3. **Poner la variable en TODOS los servicios de Railway** (web y worker: la
   alerta de las 06:30 y el resumen diario corren en el worker):
   `FECHA_INICIO_AUDITORIA=2026-10-01` (ejemplo).
4. **Verificar** en `GET /api/analitica/salud` → `meta.corte`: `valido: true` y
   el día correcto. Si dice «inválida», no cortó nada.
5. **Descartar con motivo** los FALLIDO posteriores al corte que ya se
   resolvieron por fuera (Siesa → Recuperación → Descartar). No reenvía nada.
6. **Recalcular el KPI** no hace falta: los días anteriores se marcan al leer.

Suite completa en este worktree (2026-09-24, `-m "not postgres"`, TZ=UTC, sobre
el primer commit del cambio): **8158 passed, 0 failed**, 5 skipped, 19 xfailed.
Después solo se agregó `corte_auditoria` al health y se corrió lo afectado.

---

## Analítica — 🎯 ¿Cómo vamos?: la portada contra la meta (2026-09-24)

El dueño: *la analítica no se ve clara.* El diagnóstico: la pestaña no
contestaba «¿cómo vamos?»; ningún número tenía meta; **el mismo hecho daba
cifras distintas según la pantalla**; los avisos de calidad del dato iban
pegados a cada cifra; y el juicio de confianza (Salud) estaba escondido y en
lenguaje de sistemas. La empresa gana convirtiendo pedido en caja **rápido y
sin fugas**: la portada contesta eso primero.

### La portada — `app/services/analitica_portada.py` · `analitica_portada.js`

Primera sub-pestaña y la que abre por defecto (`AN_VISTA_INICIAL`). Llega por
`GET /api/analitica/resumen` → `portada` (salió de `DEUDA_SIN_UI` con
`/metricas` y `/kpi/recalcular`; queda `/serie`). Seis indicadores, cada uno
con valor, **semáforo contra la meta**, tendencia de 8 ventanas de 7 días
(sparkline SVG sin librerías, colores de tokens, un hueco es una semana sin
dato y no un cero, la línea punteada es la meta), variación contra el período
anterior de igual duración, dueño y —al tocarlo— **su porqué y su lista de qué
hacer** con botón a la pantalla que resuelve.

| Indicador | Meta provisional | Amarillo hasta | Dueño | De dónde sale (la MISMA cifra que…) |
|---|---|---|---|---|
| Llega a caja completo | ≥ 95 % | 90 % | Gerencia de operaciones | `analitica_recorrido._guia` (valor sin fuga, cerrados) — 🧭 Recorrido |
| Ciclo de caja | ≤ 2 días | 3 días | Tesorería | `_guia` (mediana aprobado → liquidado) — 🧭 Recorrido |
| Plata en riesgo | $0 | $1.000.000 | Tesorería | fugas calle + sin pago + crédito no autorizado + documentos de plata (RC, retención, despacho) — 💸 Fugas |
| Nivel de servicio | ≥ 92 % | 85 % | Jefe de bodega | KPI diario `fill_rate` (`metricas/fill_rate.py`) |
| Venta perdida por agotados | $0 | $100.000 **por día** del período | Compras | fuga `venta_perdida` — 💸 Fugas |
| Exactitud de inventario | ≥ 95 % | 90 % | Líder de inventario | KPI diario `exactitud_inventario` |

- **Una cifra, una fuente.** La portada no calcula nada: llama a la función
  que ya decide en la pantalla de detalle. `Medicion` carga la cohorte del
  recorrido UNA vez sobre la ventana más ancha (período + anterior + 8
  semanas) y la reparte por día de entrada con la regla de `cohorte()`; las
  fugas se piden por ventana con un solo `_Ctx`. El test exige igualdad con el
  Recorrido y con Fugas, con y sin almacén.
- **Las metas viven en el catálogo** (`Metrica.meta`, `umbral_amarillo`,
  `meta_por_dia`, `meta_provisional`) y **el semáforo es una función**:
  `analitica_kpi.semaforo(clave, valor, dias, es_piso)`. Sin dato → gris
  (`sin_dato`), nunca rojo; **un piso nunca es verde** (lo que sería verde
  queda amarillo «Sin confirmar: hay casos sin valor», Regla 0). El Recorrido
  pinta sus píldoras con el mismo semáforo (`recorrido.semaforos`): se
  retiraron sus «referencias provisionales» escritas en el JS.
- **Tres métricas de COHORTE** entran al catálogo (`llega_a_caja`,
  `ciclo_caja`, `plata_en_riesgo`, `agregacion = COHORTE`): se miden en vivo
  por período, **no se guardan por día** (una mediana no se agrega sumando
  días, y un «hoy» recalculado para un día viejo mentiría). `/serie` las
  devuelve AUSENTE «en vivo»; `/resumen` las mide con la misma `Medicion` que
  la portada.
- **Nace útil.** Las cuatro en vivo tienen número aunque `ANALITICA_KPI` esté
  apagado. Las dos del KPI diario salen «Sin dato» en gris, y la portada lo
  dice **una vez** con cómo se enciende; el admin tiene «Calcular ahora los
  últimos N días» (POST en tandas de 5 días: `post()` corta a los 25 s).
- **Confianza en una línea** arriba: el veredicto global de 🩺 Salud en
  palabras («Todavía no decidas con estos números · 3 problemas graves y 7
  avisos»), pedido aparte para no frenar las cifras. «Ver por qué ›»: el admin
  va a Diagnóstico; los demás ven la lista ahí mismo.
- **«al menos»** se dice una vez al pie; el valor va en un renglón
  (`white-space:nowrap`) y la tendencia baja al siguiente si no cabe. Grilla
  `minmax(min(100%,270px),1fr)`: una columna en el teléfono.
- **Qué hacer** (`por_que.que_hacer`, botones con posiciones): plata en riesgo
  → «Liquidar ruta ›» (`tab-liquidacion` + `liqAbrirRuta`), «Ver pedido ›»
  (recorrido), «Ir a reintentar ›» (jobs de Liquidación o pestaña Siesa);
  ciclo de caja → cuánto tarda cada tramo (el más lento marcado) + los que más
  llevan esperando y los que más tardaron; llega a caja → dónde se pierde la
  plata + los pedidos con pérdida; venta perdida → por categoría + «Ver en
  Fugas ›». Destinos en lista blanca (`AN_PORT_TABS`), y un botón solo aparece
  si el rol ve esa pestaña.

### 🩺 Diagnóstico (solo admin) — `analitica_diagnostico.js`

Junta lo que sirve para **juzgar** los números: Salud completa, Bitácora,
registros sin clave (antes al pie del Recorrido) y «cómo se mide cada cifra»
(catálogo de `/metricas` con meta, dueño, agregación y fuente, más las
definiciones del recorrido). Salud y Bitácora dejaron de ser sub-pestañas
sueltas; una preferencia vieja con esos nombres lleva a Diagnóstico.

### Arreglos de claridad sin rediseño

- **Fugas**: `estado_de` tiene cinco estados; `sin_dato` y `sin_base` son
  **grises y aparte** (antes «Crece o sin dato» en rojo); la tendencia compara
  en la misma moneda (pesos con pesos, casos con casos). `meta.fuentes` con la
  convención `{nombre, completa, motivo, actualizado_en}` y `completa` global
  que no confunde `None` con `False`.
- **Frescura en una línea**: `anFrescura` es un `<details>` «Dato de hoy 06:44
  p. m. · 2 fuentes incompletas ›» que nombra cada una al abrirse.
- **Bitácora**: el motivo de bloqueo en palabras (`motivo_legible`, con
  `analitica_recorrido.MOTIVOS_BLOQUEO`: una tabla); LIQUIDAR y BLOQUEAR fuera
  de «sin motivo» (`ACCIONES_SIN_MOTIVO_ESPERADO`, `sin_motivo_base`).
- **Recorrido en frases de gerencia**: «De cada $100 que cerraron, $27
  llegaron completos a caja (6 pedidos)», «Un pedido típico tarda…»; n, p90 y
  sin marca en el `title` (el «?»).
- **Línea de tiempo**: los números viajan como números (`cifras:
  [{etiqueta, valor, formato}]`) y la pantalla los formatea con
  `anNum`/`anPesos`; las líneas de Siesa se agrupan en «Siesa aprobó el
  pedido» (códigos de ítem en el `title`); estados, formas de pago y
  documentos en palabras (`Envío a Siesa: Recibo de caja — enviado`).
- **Tablero BI**: sin valor ≠ $0 (`biValorDespachado`, categorías «sin precio
  (N)» con `sin_precio_por_categoria` nuevo en `metricas/venta_perdida.py`),
  filas con pedido y cliente, `esc()` en las etiquetas. «Fill rate» pasó a
  «Servido de lo pendiente»: sale de `pedidos_siesa`, que borra lo cumplido, y
  **no es** el nivel de servicio de la portada — dos números para dos
  preguntas, ahora con nombres distintos.
- **Nav**: «🟢 Operación hoy» (el dashboard) y «📈 Analítica».

### El Tablero BI se queda (decisión)

La portada cubre venta perdida y un nivel de servicio (distinto del BI), pero
**no** los despachos del día (pedidos, líneas, unidades, valor). Retirarlo
dejaría la operación sin ese número: se queda en el Dashboard, con su timer.
Retirarlo exige antes una tarjeta de despachos o llevarlo a Operación hoy como
cifra operativa, no de gerencia.

### Lo que NO cubre (declarado)

- **Censura de cohorte**: en las últimas semanas el ciclo de caja sale más
  corto (los lentos todavía no llegan a caja) y «llega a caja» mira solo lo
  cerrado. Se dice en «Cómo se mide la portada».
- **Plata en riesgo es de lo que pasó en el período, con su estado de hoy**:
  no es un saldo histórico al cierre de cada semana. Una ruta sin fecha cuenta
  solo en el período elegido.
- **Venta perdida** en pesos sigue siendo casi siempre un piso (el precio no lo
  llena ningún sync).
- **Nivel de servicio y exactitud** dependen del KPI diario: sin
  `ANALITICA_KPI` ni recálculo manual, gris.
- `get()` corta a los 15 s: con mucho volumen, `/resumen` (catálogo completo +
  portada) puede acercarse. Medido en el mundo de pruebas: < 2 s.
- La confianza hereda de Salud: la auditoría se guarda 10 min por proceso.

Suite completa en este worktree (2026-09-24, `-m "not postgres"`, TZ=UTC): **8109 passed**, 2 failed —dos contratos del Tablero BI que codificaban la forma vieja (la clave nueva `sin_precio_por_categoria` y el texto de la fila «sin precio»), actualizados y verdes por separado—, 5 skipped, 19 xfailed. Tests nuevos: `tests/test_analitica_portada.py` (51) y `tests/test_analitica_claridad.py` (15); 26 mutaciones, las 26 rojas (M25 sobrevivía y obligó a probar el semáforo del recorrido por sus argumentos).

### Metas provisionales — las confirma el dueño

Todas nacen `meta_provisional: true` y la pantalla lo dice. A confirmar: ciclo
≤ 2 días (¿la ruta se liquida el mismo día?), llega a caja ≥ 95 %, plata en
riesgo $0 con amarillo hasta $1.000.000, nivel de servicio ≥ 92 %, venta
perdida $0 con amarillo hasta $100.000 por día, exactitud ≥ 95 %.
Se cambian en `analitica_kpi._CATALOGO`; el semáforo, la portada, el
Recorrido y Diagnóstico las leen de ahí.

## Retención de cartera — mora y cupo antes de despachar a crédito real (2026-09-24)

**Regla del dueño.** Solo aplica a pedidos de **crédito real** (`cond_pago.
cobro_contraentrega(cond)['cobrar'] is False`: > 15 días, días conocidos).
Contado (≤ 15 días, y todo lo supuesto) **siempre sale**: se cobra al entregar.
Un pedido a crédito no se despacha si el cliente tiene **cualquier** factura
vencida (saldo > `CARTERA_TOLERANCIA_SALDO`, $5.000; sin umbral de 30 días),
si no tiene cupo asignado, si con lo que ya debe lo supera, o si tiene un
**acuerdo de pago vigente** con cartera (sale solo de contado). Los clientes
**institucionales** (licitaciones, colegios, entidades) no se retienen por mora;
el cupo sí les aplica. La excepción la da un **usuario de cartera, con motivo y
nombre**, desde el Gestor de Cartera; quien inició el pedido no puede
autorizarlo.

**Por qué el WMS necesita su propia compuerta** (medido en vivo contra Siesa QA,
solo GET): Siesa retiene por cupo/mora al aprobar, pero el mismo usuario libera
en ~4 s y sin motivo; 15 de 18 pedidos a crédito de clientes con vencidas no
quedaron retenidos.

### Una política, una función — `app/services/cartera_service.evaluar`

`evaluar(nit, sucursal, valor, cond, contexto) → {decision: PASA|RETIENE,
motivos[], vencidas_n, vencido, vencido_neto, saldo_a_favor, dias_max, cupo,
saldo, consumo_wms, disponible, exceso, as_of, origen, canal, ...}`. Nada de
ella escribe: las retenciones son de las compuertas.

| Motivo (código) | Cuándo | ¿Retiene? |
|---|---|---|
| `MORA` | ≥ 1 fila con saldo > tolerancia y hoy (Bogotá) > vencimiento real + gracia de la sucursal | sí |
| `MORA_EXENTA` | lo mismo, canal `INSTITUCIONAL` (dato del Gestor) | no, declarado. **Sin canal no se exime** |
| `SIN_CUPO` | cupo 0/nulo, o el NIT no está en el maestro de esta compañía | sí |
| `CUPO_EXCEDIDO` | `cupo − saldo − consumo_wms − valor < 0` (cupo exacto pasa) | sí |
| `VALOR_DESCONOCIDO` | con cupo, pero no se pudo calcular el valor del pedido | sí |
| `ACUERDO_VIGENTE` | el Gestor declaró un acuerdo de pago vigente | sí (la salida natural es convertir a contado) |
| `SIN_DATO` | Siesa no respondió y no hay foto de < 24 h | sí (crédito); contado pasa |
| `MAESTRO_DUPLICADO` | el maestro trae más de una fila para el NIT | **nunca**, declarado |

Una **excepción vigente** del Gestor (`habilitaciones`) con `tope ≥ valor`
deja pasar (salvo `ACUERDO_VIGENTE`). En `CARTERA_COMPUERTA=INFORMA` evalúa,
loguea `habria_retenido` y deja pasar; un valor ilegible **no apaga** la
compuerta (queda RETIENE, declarado).

**El «maestro duplicado» era multicompañía.** `API_v2_Clientes` devuelve las
filas de `f201_id_cia` 1 **y** 2: de 3.783 NIT+sucursal, 2.217 tienen fila en
las dos; dentro de la compañía 1 no hay ni un duplicado. Los cupos viejos viven
en la 2 (135 clientes con cupo) y en la 1 solo 25. Se usa la fila de
`SIESA_ID_CIA` (Regla 2); la de la otra compañía se declara con su cupo — es la
explicación probable de «tenía cupo y ahora no». Un duplicado de verdad en la
misma compañía usa la fila más reciente (`f201_ts`) y se declara.

**Cupo del NIT** = suma de los cupos de sus sucursales en la compañía propia;
**saldo** = todo el NIT. **Mora** por fila, de cualquier sucursal.

### Qué cuenta como deuda — clasificación de las FE del WMS

La cartera se lee por NIT (`API_v2_CxC_General`, `f353_fecha_cancelacion IS
NULL AND f253_id LIKE '1305%'` — 1 página, ~0,5 s; la misma que la foto
diaria). Cada fila de una FE del WMS (cruzada por **factura**, `fe_tipo` +
`fe_consec`) se clasifica con su parada:

| Clase | Qué es | Cuenta |
|---|---|---|
| `EN_RUTA` | FE del WMS sin entrega confirmada | sí, con su **vencimiento real** |
| `EN_CAJA` | el conductor cobró (trato CONTADO) y el RC no se aplicó, o se aplicó hace < 48 h | **no**: se resta lo cobrado + descuento + retenciones (plata del conductor; el rezago de liquidación ya alerta) |
| `RESIDUO` | RC aplicado y lo que queda ≤ retenciones + descuento + tolerancia | no |
| `DEVUELTA` | RECHAZADO (la mercancía volvió), o PARCIAL con la NC creada | no |
| `DEUDA` | ENTREGADO_SIN_PAGO, crédito real o autorizado, crédito no autorizado, el resto de un parcial sin NC, y toda FE ajena al WMS | sí |
| `A_FAVOR` | saldo negativo (NC sin aplicar, anticipo) | resta del saldo; `vencido_neto` lo descuenta |

**Vencimiento real**: FE del WMS → `cond_pago.vencimiento_fe(f353_fecha,
codigo_vigente(tarea))` (contado +15, crédito +días). Las emitidas antes del
2026-09-24 vencen a +30 fijo en Siesa: artefacto, no se usa. FE ajena →
`f353_fecha_vcto`.

**`consumo_wms`**: pedidos de crédito real del mismo NIT (por
`pedidos_historia.cliente_id`) con packing vivo cuya FE todavía no está en la
cartera leída: `valor_factura` o, sin ella, el valor pendiente de la historia.
Sin esto, dos pedidos seguidos ven el mismo cupo libre.

**Foto** (`cartera_cliente`): cada lectura completa se guarda por NIT; si Siesa
no responde (o, con `SIESA_VENTANA` configurada, está fuera de ella: no se le pregunta, Regla 14),
vale la foto de < 24 h. Dentro de 2 min no se relee el mismo NIT.

### Las compuertas

| | Dónde | Qué hace si retiene |
|---|---|---|
| **G1** `compuerta_inicio` (context manager) | `POST /api/siesa/iniciar-despacho`, antes del backorder y del picking | 409 `retenido_por_cartera` + retención; no se crea nada. **Lock de sesión por NIT** (`RANGO_CARTERA_NIT`, `advisory_lock`) sostenido por el `with` hasta crear el packing; otro despacho del mismo cliente en curso → 409 «se está evaluando» |
| **G2** `compuerta_cierre` | `PedidoPackingCloser.ejecutar_cierre`: evalúa antes del lock pesimista, aplica después de crear los bultos | la caja queda **VERIFICADA con sus bultos**, sin job; `CierreResult` con el motivo. Liberada, la cola ofrece «Cerrar caja» (`carteraCerrarLiberado`, cierre sin bultos nuevos) |
| **EMISIÓN** `compuerta_emision` | `DespachoParialService.despachar_parcial`, con la **cabecera de Siesa** en la mano, antes del 244328 | `RetenidoPorCartera` (subclase de `DependenciaPendiente`: el DLQ espera sin gastar reintento; resolver adelanta el job). Carril admin → 409 |
| **G3** `informe_de_tarea` | `RutaService._informe_de_cobro` (iniciar cargue / despachar) | solo informa: autorizado, convertido a contado, retención viva |

G1/G2 leen la condición anotada (historia del pedido); **la emisión lee la que
la FE va a llevar de verdad**: una condición que era supuesta (sin dato → pasa
como contado) y la cabecera desmiente (C04) se evalúa ahí. Una decisión
tomada (`tareas_packing.cartera_decision`: PASA | AUTORIZADO | CONTADO |
NO_APLICA) no se vuelve a preguntar en un reintento del cierre.

`facturar_remision_existente` y `facturar_rm_con_consec` (la RM ya existe:
retener la FE no devuelve la mercancía) solo aplican `cabecera_para_factura`
— la conversión a contado.

### Resolver una retención

| Acción | Efecto | Bitácora |
|---|---|---|
| **Autorizar** | AUTORIZADO; cubre hasta `tope_valor` (obligatorio) y `vence_en` (≤ 30 días; sin dato, +30); `codigo_excepcion` E1/E2a/E2b/E2c/E3/E4 opcional. **En G2, si lo empacado supera el tope, vuelve a retener** | `FORZAR`, `origen='gestor_cartera'` con el usuario del Gestor en `despues` |
| **Convertir a contado** | CONVERTIDO_CONTADO; la FE sale en `SIESA_COND_PAGO_RUTA` (C02): `cabecera_para_factura` cambia `f430_id_cond_pago`, el gateway la manda en `F461_ID_COND_PAGO` y la vence a 15 días; la tarea queda con `cond_pago_fe='C02'` (snapshot contado, `difiere_del_pedido` lo declara). Sin condición de ruta configurada → 409 | `EDITAR` con `clasif_origen: 'CARTERA'` en `despues` |
| **Re-evaluar** | relee Siesa; si ya no retiene → LIBERADO_PAGO (el pedido se vuelve a intentar) | — |
| **Cancelar** (barrido) | el packing se canceló o el pedido se anuló en Siesa | `CANCELAR` |

Quien inició el pedido no puede autorizarlo: se compara el `usuario` del Gestor
(`username`/`email`/`nombre`) contra el usuario del WMS que tocó «Aprobar»
(`tareas_packing.despacho_iniciado_por_id`) y contra el creador en Siesa
(`f430_usuario_creacion`); en el WMS, además, por id. **Barrido** cada 30 min
(7:00–19:30 Bogotá, esencial, `LOCK_CARTERA_BARRIDO` 2022): una lectura por NIT
retenido; retenido > 3 días → log, `alertas` en la salud y advertencia en
`/api/health/siesa`.

### La API para el Gestor de Cartera — contrato (`app/routes/cartera.py`)

Autenticación **`Authorization: Bearer <CARTERA_GESTOR_TOKEN>`** con
`hmac.compare_digest` (`exige_token_servicio`); sin la variable → 503; token
en la URL no sirve. **`Idempotency-Key` obligatoria en todo POST**: la misma
clave devuelve la misma respuesta con `idempotente: true`; usada en otra
operación → 409; solo se guarda lo exitoso. Retención ya resuelta → 409 con
`estado`. `usuario` es un objeto `{username, nombre, email, rol, sistema}`.
NIT sin dígito de verificación (se normaliza `900123456-7`); sucursal como
`f201_id_sucursal`. Las lecturas **nunca devuelven una lista vacía ante un
error**: 503 con el texto.

| Método y ruta | Cuerpo / query | Respuesta |
|---|---|---|
| `GET /api/cartera/retenciones` | `estado`, `nit`, `cambiados_desde` (ISO; incluye resueltas), `limite` (≤ 500) | `{retenciones[], cursor, politica{version, parametros}, generado_en}` |
| `GET /api/cartera/retenciones/<id>` | — | `{retencion}` |
| `POST /api/cartera/retenciones/<id>/autorizar` | `{usuario, motivo, tope_valor, vence_en?, codigo_excepcion?}` | `{retencion}` |
| `POST /api/cartera/retenciones/<id>/convertir-contado` | `{usuario, motivo}` | `{retencion}` |
| `POST /api/cartera/retenciones/<id>/reevaluar` | `{usuario?}` | `{retencion}` |
| `POST /api/cartera/habilitaciones` | `{usuario, nit, sucursal?, canal, acuerdo_vigente, acuerdo_vence?, excepciones[{codigo, tope, vence}], as_of}` o `{usuario, clientes: [...]}` | `{guardadas[], n}`. Una habilitación con `as_of` más viejo no pisa la guardada |
| `GET /api/cartera/salud` | — | modo, retenidos por antigüedad, alertas, frescura de la foto, habilitaciones, `gestor_token_configurado` |

Cada retención: `id, estado, compuerta, pedido, pedido_clave, cliente, nit,
sucursal, vendedor, cond_pago, dias_credito, valor, motivos[{codigo, texto,
retiene, …cifras}], motivos_codigos, resumen, vencidas[{documento, fecha, vence,
vence_siesa, dias, saldo, cuenta, clase, wms, pedido}], vencido, vencido_neto,
saldo_a_favor, dias_max, cupo, saldo, consumo_wms, pedidos_wms, disponible,
exceso, canal, habilitacion, as_of, origen_dato, politica, iniciado_por{id,
nombre, email}, siesa_usuario_creacion, contexto_siesa (retenido_cupo/mora/…,
usuario_retenido, usuario_aprobacion_cart…), creada_en, actualizada_en,
antiguedad_horas, resolucion, reevaluaciones`.

**Respaldo en el WMS** (`/api/cartera/panel/*`, JWT): ver con gestión o quien
puede decidir; decidir con **`cartera_service.puede_autorizar`**: el **líder de
cartera por su rol**, o la casilla `puede_autorizar_cartera` (por persona, nace
apagada) **solo en el admin** (lista blanca; desde el 2026-09-26, ver
«Roles de la plata»: supervisor, jefe y gerente con la casilla ya no deciden). Bloque «⛔ Retenidos por cartera» en el tablero
(`cartera.js`) y en la pestaña ⛔ Cartera del líder, etiqueta
en la cola de pedidos y en packing. `GET/POST /api/cartera/panel/credito-lote`
(`puede_autorizar_credito`: admin y líder de cartera): autoriza en lote, con un motivo común y bitácora por parada,
las paradas `credito_no_autorizado` confirmadas **antes** de
`CONTADO_DESPLIEGUE_FECHA` (sin la variable → 409, no se adivina).

### Variables

| Variable | Defecto | Qué es |
|---|---|---|
| `CARTERA_COMPUERTA` | `RETIENE` | `INFORMA` para el arranque |
| `CARTERA_TOLERANCIA_SALDO` | `5000` | Saldo mínimo de una vencida |
| `CARTERA_GESTOR_TOKEN` | — | Secreto con el Gestor. Sin él, 503 |
| `CONTADO_DESPLIEGUE_FECHA` | — | Día de m043contado en producción, para el lote |

Migración **`m044cartera`** (down `m043contado`, aditiva, sin backfill): cuatro
tablas (`retenciones_cartera`, `cartera_cliente`, `cartera_habilitaciones`,
`cartera_idempotencia` — las cuatro OPERATIVAS en el acta de corte, sin FK;
`cartera_cliente` REGENERABLE en la verificación de respaldo) +
`tareas_packing.despacho_iniciado_por_id / cartera_decision / cartera_decidido_en`
+ `usuarios.puede_autorizar_cartera`.

### Trinquetes y mutaciones

- `tests/test_cartera_trinquete.py` (por AST sobre `app/`): toda función que
  llama 244328/142945 llama `compuerta_emision`; toda la que llama 142943
  llama `compuerta_emision` o `cabecera_para_factura`; toda la que crea el
  picking de un pedido (`crear_tareas_con_compromiso`) llama
  `compuerta_inicio` o solo la llaman funciones que la llaman; quien encola
  `DESPACHO_F470` está declarado (el helper del closer, llamado solo desde
  `ejecutar_cierre`, que llama `compuerta_cierre`; el legacy sin llamador).
  Meta-tests (las tres escrituras, docstrings, comentarios, función anidada) y
  pisos.
- `tests/test_cartera_retencion.py` (94): mundo con `FakeSiesa`
  (`cartera_service.usar_fuente`) — cada caso de la política, la
  clasificación, las cuatro compuertas (G1 por HTTP con 409 y sin picking, G2
  por el closer real, emisión con `despachar_parcial`), resolver, API, panel,
  lote. `tests/test_cartera_js.py` (Node con `util.js` real).
- `test_todo_endpoint_verifica_rol` ganó `_RUTAS_DE_SERVICIO` (inventario
  exacto de las rutas con `exige_token_servicio`); las siete del Gestor están
  en `DEUDA_SIN_UI` (las llama otro sistema).
- **24 mutaciones, las 24 rojas** (cupo +1, EN_CAJA sin restar, institucional,
  cupo 0, otra compañía, consumo del WMS, compuerta de emisión y de cierre
  quitadas, iniciador autoriza, autorizar sin bitácora, tope ignorado en G2,
  foto vieja, token sin comparar, sin idempotencia, gracia, FE del WMS con el
  +30, acuerdo, conversión sin C02, `esc()` en el JS, G1 que no frena,
  excepción sin tope, lote sin corte, NO_APLICA no re-evaluado, contado
  evaluado).

Suite completa (2026-09-24, este worktree, `-m "not postgres"`, TZ=UTC, con la
máquina saturada): **8160 passed, 1 failed** (`test_deuda_legacy`: las dos
funciones de permiso nuevas usaban `Usuario.query.get`; pasadas a
`db.session.get` y re-corridos los tests afectados, verdes), 5 skipped, 19
xfailed. Los `@postgres` de cartera y de esquema, verdes contra un PostgreSQL 17
desechable; `m044cartera` upgrade → downgrade → upgrade en el mismo.

### Medido en QA (solo GET, 2026-09-24)

Filtros verificados en vivo: `API_v2_Clientes` por `f200_id` (0,5 s),
`API_v2_CxC_General` por NIT + abierta + 1305 (0,5 s), `API_v2_Ventas_Pedidos`
por CO + tipo + consecutivo (1 s, trae `f430_ind_retenido_*`,
`f430_usuario_creacion`, `f430_id_sucursal_pedido_fact`). Sobre los 270
pedidos comprometidos de QA: 80 de crédito real, y de ellos **79 quedarían
retenidos por mora y 79 por cupo 0** en la compañía 1 (los datos de QA tienen
mora desde 2024). Ver «Decisiones abiertas».

**`f201_id_tipo_cli` no sirve como canal**: `0001` mezcla ICBF, SENA y
personas naturales; `0002` son personas naturales. El canal institucional
viene del Gestor (`habilitaciones`); sin él no se exime.

### Lo que NO cubre, dicho

- **La carrera de dos pedidos del mismo NIT** está probada por el mecanismo (el
  lock se toma con su clave y un lock ocupado da 409), no contra PostgreSQL:
  en SQLite `advisory_lock` concede siempre.
- **El valor del pedido** sale de `pedidos_historia` (neto × pendiente /
  pedido); en G2, por línea × `min(1, empacado/esperado)`. Descuentos globales
  del pedido no se prorratean aparte.
- **PARCIAL sin NC** cuenta el resto como deuda aunque parte sea mercancía que
  volvió (el WMS no separa «devuelto» de «pagó una parte» sin la NC).
- ~~**Retenciones en la emisión dentro del DLQ** dejan la tarea DESPACHADO~~
  Corregido el 2026-09-25 (m048fiscal): el cierre ya no marca DESPACHADO al
  encolar; la caja retenida queda VERIFICADA, y **cancelarla es su salida**
  (el job PENDIENTE pasa a DESCARTADO y la retención a CANCELADA, con
  bitácora). Ver «Fiscal y despacho».
- **Ninguna alerta por correo** del retenido > 3 días: log + salud + health.
- **Modo INFORMA** no deja fila: lo que habría retenido solo va al log.
- **No se re-cierra solo** una caja liberada: alguien toca «Cerrar caja».

### Decisiones abiertas para el dueño

1. **Cupos en la compañía 1**: solo 25 clientes tienen cupo; los 135 cupos
   viven en la compañía 2. Con la regla «crédito real sin cupo → retiene», casi
   todo el crédito real queda retenido desde el primer día. ¿Se cargan los
   cupos en la compañía 1 antes de encender, o se arranca en
   `CARTERA_COMPUERTA=INFORMA`?
2. **¿Cupo por NIT (suma de sucursales) o por sucursal?** Hoy por NIT.
3. **Institucionales y cupo**: hoy solo se eximen de la mora; el cupo les aplica.
4. **¿Una caja liberada se cierra sola** (el Gestor autoriza y el WMS emite) o
   la cierra una persona (hoy)?
5. **`vence_en` y `codigo_excepcion`** opcionales en la autorización (sin
   `vence_en`, 30 días). ¿Obligatorios?

## Lo que encontró el recorrido e2e por rol de flota (QA 960d2f1c, 2026-09-24)

`tests/test_qa_e2e_flota_20260924.py` reproducía siete defectos con
`xfail(strict)`; los siete están arreglados y sin marca. Cada uno se cerró por
su **clase**, con trinquete:

| | Clase | Qué pasaba | Ahora | Trinquete |
|---|---|---|---|---|
| **P1** (en producción) | Un mapa de estados en JS que no cubre `EstadoEntrega.TODOS` | «No pagó y se quedó» → `_condRenderParadas` hacía `EST_C['ENTREGADO_SIN_PAGO'].badgeBg` → TypeError: la lista del conductor quedaba en «Cargando paradas...» y **se iba el botón «Cerrar Ruta»**. El manifiesto, igual | Un solo `ENTREGA_ESTILO` en `rutas.js` con los cuatro estados y `estiloEntrega(est)`, que cae a un estilo neutro ante un estado desconocido | `test_mapas_estado_entrega_pwa.py`: todo objeto literal del PWA con ≥ 2 claves de estado cubre los valores del modelo (leídos por AST), inventario de parciales que solo encoge; las dos pantallas pintan en Node cada estado y uno desconocido |
| **P2** | Un verbo de bitácora con dos significados sobre la misma entidad | El FORZAR de «despachar con advertencias de flota» se leía como cierre forzado: la jornada decía «Ruta cerrada a la fuerza por la oficina» (y la sacaba de la duración de ruta) y la bitácora «forzó el cierre» | `bitacora.TIPOS_FORZADO` + `tipo_de_forzado()` (filas viejas por su forma); `registrar_accion('FORZAR')` **exige** `despues['forzado']` | `test_forzar_dice_que_forzo.py`: AST sobre `app/ flota/ scripts/`, y el cierre forzado real se sigue viendo |
| **P2** | Comparar contra el estado de AHORA algo que ya terminó | Ruta ENTREGADA + camión en la sede = señal `turno_de_la_ruta` cada noche normal; y el turno de la SEDE era «sin fotos de inicio (0 de 11)» | Una ruta entregada se juzga contra el turno vigente **al cierre** (`bandeja._turno_en`); sin hora de cierre, no evaluable. La sede no tiene fotos exigidas (su cierre forzado sí cuenta). `test_bandeja.py` sembraba fotos en la sede y lo escondía: corregido | `tests/flota/test_fin_del_dia_normal.py` (día normal, mismo instante, y los casos que DEBEN seguir siendo señal) |
| **P3** | Un valor que un CHECK rechaza llega al commit sin validarse | Ángulo desconocido → 500 en el traspaso; la cola del conductor reintenta 5xx para siempre. Igual una clase desconocida y un data URL ilegible | `almacen_fotos.validar_fotos` (mismo vocabulario del CHECK), antes de escribir; traspaso, daño y tanqueo → 400 | `tests/flota/test_foto_invalida_es_400.py`: AST, toda función de `flota/api` que lee `fotos*` traduce `FotoInvalida`/`ErrorAlmacen` a 400 antes de `ErrorFlota` |
| **P3** | Forma de pago guardada donde no hubo cobro | NO_PAGO_SE_QUEDO con el select en CREDITO quedaba CREDITO → desglose y liquidación la listaban como crédito | `recaudo_entrega.forma_pago_de(estado, forma)`: `None` en `EstadoEntrega.SIN_COBRO` (RECHAZADO, ENTREGADO_SIN_PAGO). La usa quien escribe y quien lee (`to_dict`, desglose): las filas viejas también | `test_forma_pago_sin_cobro.py`: AST, toda escritura de `forma_pago` pasa por la política o es `None` |
| **P3** | Un código del vocabulario pintado crudo | `no_apto` en el muelle, `ENTREGADO_SIN_PAGO` en la liquidación, `salio_sin_turno` en la bandeja, `` `entregar_ruta` `` y `no_se_pidio` en la jornada, `veredicto no_apto` en la analítica, `a_revisar_en_siesa` y la matriz del desglose, y `soat`/`poliza_rc`/`efectivo_conductor`/`sin_dato`/`aire_acondicionado`… en el expediente | Los mapas de palabras de cada módulo (`FLOTA_PALABRAS` + `flotaOpcion`, `FLOTA_VALOR_EVIDENCIA`, `fjPalabra`, `LIQ_ESTADO_ENTREGA`, `palabra_de_veredicto` en el dominio). **«Inspección no apta», en femenino** | `test_sin_codigos_en_pantalla.py`: detector de FORMA (snake_case, MAYÚSCULAS_CON_GUIONES, `` `x` ``) sobre el texto visible de cada pantalla pintada en Node contra respuestas reales |

**Menores:** `condicion_declarada` del desglose rotula con la política de cobro
(C02/C03 = contado, no «credito»), y Liquidación → Desglose muestra
`por_dias_credito` y `credito_no_autorizado`; el semáforo dice «el último
kilometraje está en duda» solo si la **última** lectura lo está (las viejas
siguen en Pendientes); re-confirmar una parada guarda `ts_dispositivo`,
`ts_desfase_s` y `via_cola` anteriores en el EDITAR; el log de condición
ausente dice «se cobra como contado supuesto» (decía «no se asume contado»);
el tipo de vehículo se valida contra `flota.dominio.valores.TIPOS_VEHICULO`
(= el desplegable, por test) → 400 claro; un vehículo viejo con otro tipo se
sigue editando mientras no cambie el tipo.

**Mutaciones: 34, las 34 rojas.** De paso se encontró una trampa del propio
método: una mutación del **mismo tamaño** escrita en el mismo segundo deja el
`.pyc` mutado vigente tras restaurar (Python valida por mtime en segundos +
tamaño), y el test siguiente corre contra el código roto. Correr las
mutaciones con `PYTHONDONTWRITEBYTECODE=1`/`-B`.

**Lo que NO cubre, dicho:**
- El detector de códigos crudos mira las pantallas que pinta; una pantalla
  nueva no entra sola (el expediente sí: `EXPEDIENTE` tiene inventario).
- Un estado de entrega nuevo en el modelo pone rojo el trinquete del PWA
  hasta que alguien le dé estilo; en Python no hay trinquete equivalente.
- `turno_de_la_ruta` sobre una ruta entregada usa el turno vigente al cierre;
  si el traspaso se registró **antes** del cierre de ruta (el conductor dejó
  el camión y cerró la ruta después), la señal «salió sin turno» aparece.
- Las paradas RECHAZADO/ENTREGADO_SIN_PAGO viejas conservan su `forma_pago`
  en la base (sin backfill); solo se LEEN sin ella.
- Un vehículo viejo con un tipo fuera del catálogo no se corrige solo.

Tests que codificaban el comportamiento viejo, reescritos con su porqué:
`test_liquidacion_desglose` (rótulo «credito (C02)»), `test_mi_camion_hoy_js`
(«no apto» → «no apta»), `test_hora_y_angulo` (el ángulo inventado esperaba
el IntegrityError del commit: ahora 400 sin escribir nada, y un test aparte
prueba que el CHECK sigue), `test_render_llantas_js`/`test_render_preventivo_js`
(códigos → palabras) y `test_flujo_dia_de_conductor` (el contador pinchado
`custodias_sin_foto_completa` baja de 3 a 1: las dos de la sede ya no cuentan;
el xfail del «día bien hecho = 0» sigue rojo por la custodia del conductor).

Suite completa en este worktree (2026-09-24, `-m "not postgres"`, TZ=UTC, una
sola corrida con la máquina saturada): **8134 passed, 5 failed** (los cinco
tests de arriba, corregidos después y verdes por archivo: 117 passed, 3
xfailed), 5 skipped, 19 xfailed. No se volvió a correr entera a pedido del
integrador.

## Si el cliente no paga, la mercancía VUELVE — sin cabos sueltos (2026-09-24)

**Regla del dueño:** *si el cliente no paga, la mercancía vuelve y el producto
tiene que volver a entrar al inventario, sin cabos sueltos.* Migración
**`m045devol`** (aditiva, nullable, sin backfill; `down_revision='m043contado'`,
el integrador re-encadena). Política en **`app/services/devolucion_ruta.py`**;
cuenta y liberación en `devolucion_cliente_service`; verificación de la NC en
`devolucion_nc_verificador`. Trinquete: `tests/test_devolucion_vuelve.py`
(84 tests, **22 mutaciones, las 22 rojas**; corridas sobre una copia del
worktree, verificando antes que el reemplazo aplicaba exactamente una vez).

**La clase, no el caso:** *mercancía declarada de vuelta que ningún registro
sigue hasta el inventario vendible*. Lo que había:

| Cabo suelto | Ahora |
|---|---|
| La `DevolucionCliente` nacía **al liquidar**: entre el regreso del camión y la liquidación la mercancía no existía en el WMS | Nace **EN_CAMION** en `confirmar_parada`, **sin red**, con lo declarado (`sincronizar_con_parada` → `_crear_de_ruta`, la ÚNICA que crea devoluciones de ruta). En un SAVEPOINT: la entrega no se traba. Reconfirmar la rehace (bitácora EDITAR) o, si pasó a ENTREGADO/ENTREGADO_SIN_PAGO, la cancela el sistema (CANCELAR con motivo). La liquidación y el cierre forzado la **encuentran** (`asegurar_devolucion`, que solo crea la de una parada vieja) |
| Después podía quedar ABIERTA sin plazo con la ruta LIQUIDADA | `liquidar_ruta` **no liquida con devoluciones sin contar** (`devoluciones_sin_contar: …`, 400); con `motivo_devoluciones` sí, y queda FORZAR en la bitácora. Las dos pantallas (`liqLiquidarWMS`, `rutaLiquidar`) piden el motivo |
| El panel «BULTOS RECHAZADOS — RE-INGRESAR» era informativo (y el recepcionista recibía 403: su endpoint era de admin) | **🚚 Llegó el camión** (`GET /api/devoluciones/llegadas`, recepción): por ruta, escanear el bulto que volvió → `RETORNADO`; **Cerrar llegada** → lo no escaneado `FALTANTE` y EN_CAMION → ABIERTA; cuadre `salieron = entregados + retornados + faltantes` **exacto** solo sin nada colgando. Un bulto entregado de una parada ENTREGADA no se recibe sin corregir la parada; uno de una PARCIAL sí, declarado `no_declarado` |
| Si no volvió nada había que CANCELAR y el faltante desaparecía | Contar en cero es **FALTANTE_TOTAL**: sin inventario ni NC, medido (`senales_ruta.faltante_de_retorno` lo incluye). Cancelar es solo de supervisión con motivo, para una devolución que no debía existir |
| Doble reingreso: mostrador + ruta, y el tope no descontaba las previas | **Una devolución ACTIVA por línea de factura**: índice único parcial `uq_linea_devolucion_rowid_activo` (lo mantiene `cambiar_estado`, la única que escribe el estado) + mensaje claro antes. El tope es `ya devuelto (contadas) + esta ≤ facturado`, por `f470_rowid`. La de mostrador sobre un pedido con devolución de ruta sin contar **se rechaza** («contala en esa») |
| El RC de un parcial de contado esperaba para siempre si la devolución se cancelaba o el job daba `sin_lineas` | `devolucion_ruta.nc_no_llegara`: si la devolución terminó sin NC (FALTANTE_TOTAL o cancelada) el ejecutor deja salir el RC **por lo cobrado** (NC → RC → DC intacto: no hay NC que esperar) y `destrabar_rc_de` lo reprograma ya. La NC sin líneas levanta `NotaCreditoSinLineas` (determinista: FALLIDO + alerta, sin gastar reintentos, antes del pre-flag) en vez de COMPLETADO sin nota |
| Referencia de la factura sin producto WMS: WARNING y devolución corta | `problema_factura` en la devolución, **bloquea el conteo con el mensaje** y llega a `errores` de la liquidación. En `buscar_pedido` viaja `referencias_sin_producto` |
| PARCIAL sin ítems → devolución TOTAL; el servidor no exigía `items_entregados` | Con `version_formulario >= 4` (el PWA nuevo: `COND_VERSION_FORMULARIO = 4`) una PARCIAL exige al menos una referencia devuelta. Un ítem viejo de la cola **no se traba**: su devolución nace con `sin_items` y recepción cuenta contra la factura |
| El reingreso iba directo a picking (vendible) con la NC en Elaboración | Lo **sano** entra al bin `DEVOLUCIONES`, zona **`DEVOLUCION`** (`String(10)`), **no vendible**: `picking_service.ZONAS_NO_VENDIBLES` — la misma política que AVERIAS, así que FEFO, alerta de mínimos, carga inicial, ABC y traslados la heredan (reposición filtra PICKING/RESERVA en positivo; conteo ya la trata como «mercancía en proceso» mientras la NC no conste aprobada). Lo **averiado** va a `AVERIADOS`. Con la NC aprobada, `liberar_reingreso` (la única puerta, trinquete AST) lo mueve al slot de picking o a la ubicación óptima con dos `MovimientoInventario` `LIBERACION_DEVOLUCION` |
| No se podía partir una línea entre sanas y averiadas; `MERCANCIA_AVERIADA` no llegaba premarcada | `lineas_devolucion_cliente.cantidad_averiada`. Premarcada si el motivo es MERCANCIA_AVERIADA. La NC de una línea **mixta** va entera a la bodega de la factura (forma verificada: una línea por `f470_rowid_movto`); lo averiado se traslada a AV1 con el 142951 **al aprobarse la NC**, anclado al movimiento que lo metió en AVERIADOS. Toda averiada → la NC la manda a AV1 como siempre |
| «Ya la aprobé» era un clic sin verificar | Cron **`[DEVOLUCIONES_NC]`** (`_scheduler_pesados`, cada 30 min 7–19:30 Bogotá, `LOCK_DEVOLUCIONES_NC = 2023` (este archivo decía 2022, que es `LOCK_CARTERA_BARRIDO`), **nace apagado**: `DEVOLUCIONES_VERIFICAR_NC=true`) lee `f350_ind_estado` con la consulta ya registrada (`CONNEKTA_CONSULTA_NC_CONSECUTIVO`) y marca `nc_aprobada_fuente='SIESA'` solo si leyó `1` en la fila de ESE consecutivo, tipo y CO. El botón queda de respaldo: **motivo obligatorio y FORZAR** (`MANUAL`). Supervisión tiene «Verificar en Siesa» (`POST /api/devoluciones/verificar-nc`). `/api/health/siesa` → `devoluciones_nc` |
| DEV-04 contaba las NC ya aprobadas, sin antigüedad (y DEV-05 igual) | Solo las sin aprobar, con días, las más viejas primero. DEV-05 excluye las aprobadas |
| Un producto de doble unidad declarado por referencia bloqueaba la devolución (`LineaDevueltaAmbigua`) | `vincular_a_factura` **no reparte**: abre una línea por fila de factura en 0 y recepción cuenta cuál volvió (`linea_id`); lo declarado queda por producto (`declaracion_conductor.declarado_por_producto`) y el faltante se mide contra la suma. Con rowid declarado, manda el rowid. Un producto con UNA línea: lo declarado sin rowid se suma a ella |

**Nuevos estados.** `EstadoDevolucionCliente`: `EN_CAMION → ABIERTA →
CONFIRMADA | FALTANTE_TOTAL | CANCELADA` (CHECK; transiciones en
`TRANSICIONES`). `EstadoBulto`: `RETORNADO`, `FALTANTE`.
`rutas_despacho.llegada_cerrada_at/_por_id`.

**Invariantes nuevos** (`auditoria/devoluciones.py`, detector ciego en
`TestDetectoresDeDevolucionDeRuta`): **DEV-06** BLOQUEA (rechazo/parcial de
ruta LIQUIDADA sin devolución), **DEV-07** AVISA (sin contar > 24 h), **DEV-08**
AVISA (rechazo con la devolución cancelada), **DEV-09** BLOQUEA (RC esperando
una NC que no llegará, > 1 h), **DEV-10** BLOQUEA (dos activas por línea de
factura o suma > facturado), **DEV-11** BLOQUEA (reingreso vendible sin NC
aprobada — solo líneas con `cantidad_averiada` escrita, la marca de m045devol:
el histórico entraba a picking por diseño), **DEV-12** AVISA (NC sin aprobar
> 3 días o anulada), **DEV-13** AVISA (bultos declarados de vuelta tras cerrar
la llegada). El flujo `devoluciones` pasa de 5 a 13.

**Avisos** (`devolucion_ruta.avisos` → tablero de recepción y resumen diario
por correo): sin contar 24/48 h, NC sin aprobar 3 días, NC anulada, RC
esperando su NC 48 h. **Medición** (`devolucion_ruta.medicion`,
`GET /api/devoluciones/tablero`): horas rechazo → conteo y conteo → aprobación
(mediana/p90, verificadas por Siesa aparte), faltante de retorno valorizado
con `valor_unitario` de la línea de factura (`f470_vlr_neto / f470_cant_base`,
sin valor → contado aparte, nunca cero), cuadre de bultos por ruta.

**Trinquetes AST** (con meta-tests: lo que deben ver, lo sano que no, la hija
que no cuenta, pisos): toda función de `app/` que escribe `estado_entrega`
llama a `sincronizar_con_parada` (inventario vacío); solo `_crear_de_ruta`
pasa `recaudo_entrega_id` a `crear_devolucion`; solo `cambiar_estado` escribe
el estado (y `crear_devolucion` el inicial); `_destino_vendible` tiene un solo
llamador (`liberar_reingreso`, que empieza exigiendo `nc_aprobada_siesa`), y
`confirmar_entrada_fisica` solo resuelve el bin de averías con
`es_averiado=True` literal. `RESTAS_DECLARADAS` suma
`devolucion_cliente_service.py` (la liberación, con su contrapartida).

**Endpoints** (`/api/devoluciones`): `GET /llegadas`, `POST /llegadas/<ruta>/bulto`,
`POST /llegadas/<ruta>/cerrar`, `POST /<id>/preparar-conteo` (amarra a la
factura, con red), `GET /tablero` (recepción); `POST /verificar-nc`
(supervisión); `marcar-nc-aprobada` exige `motivo`. `/api/rutas/<id>/liquidar`
acepta `motivo_devoluciones` (`/liquidar-completo`, que también lo aceptaba, se
borró el 2026-09-25).

### Lo que NO cubre, dicho

- **El 251126 con dos líneas del mismo rowid (sanas a NB1 + averiadas a AV1)
  no se probó contra Siesa**: por eso la línea mixta va entera a la bodega de
  la factura y el traslado de averías sale después. Probarlo en QA antes de
  cambiarlo.
- **`f350_ind_estado = 2` como «anulada» es el valor estándar de Siesa, no
  verificado en vivo**: solo se usa para avisar, nunca para decidir.
- **La consulta de NC trae las 100 más recientes**: una NC más vieja queda
  `fuera_de_la_consulta` y se marca a mano. Sin consecutivo conocido
  (motivo DIAN manual), igual.
- **Entre que Siesa aprueba y el cron lo lee** (hasta 30 min), la carga inicial
  de stock puede contar esas unidades dos veces (Siesa ya las tiene; el WMS las
  tiene en DEVOLUCION, que no resta del bucket). Se corrige en la liberación.
- **`Producto.stock_averiado` significa «no vendible»**: ahora incluye la zona
  de devoluciones y el catálogo de admin lo rotula «averiadas».
- **Las devoluciones anteriores al deploy** quedan como estaban; las de rutas
  aún no liquidadas las asegura la liquidación (EN_CAMION) y bloquean hasta
  contarlas o forzar.
- **`/api/rutas/bultos-rechazados`** quedó sin pantalla (la reemplaza
  «Llegó el camión») y declarado en `DEUDA_SIN_UI`; candidato a borrar junto con
  `RutaService.bultos_rechazados`, que sus tests ejercen.
- La devolución se vincula a la factura al recibir/contar/liquidar, nunca al
  confirmar la parada (sin red): una PARCIAL con una referencia que la factura
  no trae se ve recién ahí (aviso, y se cuenta en 0).
- El conductor sigue sin mandar el `f470_rowid` por referencia: la doble unidad
  la resuelve recepción contando por línea.

**Suite completa** (2026-09-24, este worktree, `-m "not postgres"`, TZ=UTC,
máquina saturada): **8126 passed, 5 failed**, 5 skipped, 19 xfailed. Los 5
eran de esta tanda y se arreglaron corriendo solo los afectados (el integrador
corre la suite sobre lo integrado): la ventana de 500 caracteres de
`test_auditoria_tanda_media` (ahora AST), dos `.query.get` nuevos en la
auditoría (deuda legacy), `faltante_de_retorno` sobre un doble sin
`declaracion_conductor`, y el registro de schedulers.

### Decisiones abiertas para el dueño

1. ¿Quién puede forzar la liquidación con devoluciones sin contar? Hoy el mismo
   `_solo_admin` que liquida.
2. Un FALTANTE (total o parcial) deja la factura con saldo en cartera. ¿Se le
   cobra al conductor, se da de baja, o se reclama al cliente? El sistema lo
   mide y lo avisa; no decide.
3. ¿Encender `DEVOLUCIONES_VERIFICAR_NC` en producción? Solo lee (GET) y marca;
   la liberación a picking mueve inventario.
4. Umbrales de aviso (24/48 h, 3 días, 48 h): son constantes provisionales.

---

## Compras: los números correctos (2026-09-24)

Auditoría numérica del motor de compras (ROP dual, Armador de contenedor, S-B,
TSB, newsvendor de temporada, costo, deriva, bloqueo de recompra), con scripts
que corrían el código real y comparaban contra respuestas calculadas a mano.
Catorce defectos; el primero multiplicaba la demanda de los SKU grumosos por
25. Trinquetes: `tests/test_demanda_una_funcion.py` y
`tests/test_compras_numeros_correctos.py` (78 tests, **33 mutaciones, las 33
rojas**, cada una verificada a aplicar exactamente una vez antes de correr).

> **Integrado con «Compras: las fuentes de datos» (abajo).** Lo que viene y el
> lead time quedaron con **un dueño**: `compras_fuentes.en_camino` y
> `compras_fuentes.lead_time` (ver «Integración de los dos frentes», al final
> de esa sección). Donde esta sección nombra `armador_service.en_camino`,
> `FUENTES_EN_CAMINO`, `armador_service.lead_time` o
> `_lead_time_de_proveedor`, eso ya no existe; las reglas (EN_PRODUCCION
> cuenta, D9, `ROP_LT_NACIONAL_DIAS`, `insumo_en_camino`) se conservaron.

### D1 — «días con stock» eran «días con movimiento»

`reconstruir_stock_diario` escribía una fila de `StockDiario` **solo en los
días con movimiento**, y todos los consumidores contaban `count(distinct fecha)
WHERE tuvo_stock`. Un día sin movimiento no existía: el denominador era «días
con venta», justo lo que `dias_expuestos` prohíbe por escrito. Los tests lo
escondían porque **fabricaban `StockDiario` a mano**, una fila por día.

Medido con el reconstructor real: 40 u cada 25 días con el estante lleno daba
40/día en vez de 1,56 (×25,7), ROP 332 en vez de 37, S-B en SUAVE (ADI 1) y la
temporada ×3.

**La cura, sin una fila por día calendario** (serían ~20 millones):
`StockDiario` se lee como **función escalón** — el cierre de un día vale hasta
la siguiente fila — y el reconstructor escribe además la **apertura** (el
cierre del día anterior al primer movimiento) y recalcula toda fila existente
de la clave (sin borrar ninguna). Toda demanda diaria sale de:

| Función (`kardex_service`) | Qué es |
|---|---|
| `serie_demanda(desde, hasta, nivel)` | EL numerador: ventas netas por día. La única que lee 501/502 |
| `intervalos_con_stock(desde, hasta, nivel)` | EL denominador: tramos con stock > 0, unidos entre bodegas. La única que lee `StockDiario` |
| `dias_expuestos` | Sin `StockDiario`: días calendario, `censurado=True` (Regla 0) |
| `demanda_descensurada` | d, σ, censura y «sin venta reciente» por SKU. La usan el ROP, el contenedor, la tasa servida (que ahora es una vista de ella) y el bloqueo |
| `ventana_observada` | La ventana recortada a la **cobertura del kardex** (su primer movimiento): un día que el kardex no observó no es un día sin venta |

Trinquete AST: ninguna función fuera de `LECTORES_DECLARADOS` (6, solo encoge,
cada una con su porqué) nombra `StockDiario`, `tuvo_stock` ni los conceptos de
venta. **Después de desplegar hay que correr `POST /api/kardex/reconstruir`**:
las filas viejas no tienen apertura (se extiende hacia atrás la primera fila,
que es una aproximación).

### Los otros trece

| | Qué pasaba | Ahora |
|---|---|---|
| **D2** | El pedido de temporada publicaba Q* como pedido: no restaba lo que hay ni lo que viene | Cada fila trae `tener` (Q*), `hay`, `viene`, `pedir = max(0, tener − posición)`, con la posición de `armador_service.posicion_inventario` — **la misma del Armador**. Sin fila de stock: `hay_sin_dato`. La pantalla sigue mostrando Q*: el rediseño viene después |
| **D3** | El tope anti-500-días (180) topaba el **S objetivo** de China, por debajo de LT+R = 195: servicio implícito 16,7% | El tope es solo del **relleno** (`cobertura_max_aplica_a`) |
| **D4** | `resolver_costos` tomaba acuerdos **inactivos** y precios en **USD como pesos**; el Armador tenía `1.45 * 4200` quemados | Acuerdos `activo`, cotizaciones `vigente`; `costo_service.a_cop` (USD = FOB × TRM × factor, declarado), `trm_cop_por_usd()` (`TRM_COP_USD`) y `factor_nacionalizacion()` (`FACTOR_NACIONALIZACION`). Otra moneda: excluida. Trinquete AST: ningún 4200/1,45 fuera de `costo_service` |
| **D5** | El precio realizado VIVO salía de `f470_vlr_neto` (**con IVA**) y se restaba de un costo sin IVA: Cu +47% | `vigia_service.valor_linea_sin_impuesto` (neto − impuesto, o bruto − descuentos; si no, la línea no entra). Etiquetas nuevas `VIVO_SIN_IMP` / `SI-AAAA-MM-DD`: las viejas (con IVA) quedan **sin leer**, sin borrarlas. **La serie de facturación de Vigía no cambia** (misma base que el TXT: el canon de Florencia intacto). El `TOTAL` del TXT tiene base no verificada: `precio_sin_impuesto: None` y `resumen_por_fuente.precio_base_no_verificada` |
| **D6** | La descensura neteaba devoluciones del mismo día; la tasa servida, por ventana | `serie_demanda` netea la devolución contra la venta que devuelve (el mismo día y hacia atrás); la de una venta anterior a la ventana no resta (`devolucion_sin_venta`) |
| **D7** | TSB armaba la rejilla hasta la **última venta** y publicaba el ajuste del train: un SKU parado seguía pronosticado | Rejilla hasta la **semana actual**; semanas sin stock y sin venta = `None` (un agotado no es «no se vendía»); el pronóstico sale de la serie entera |
| **D8** | S-B excluía estacionales comparando `'ESTACIONAL'` contra una columna `String(1)`: nunca | `TemporadaService.identificar_skus_temporada`, la misma política del pedido escolar |
| **D9** | Con 3 contenedores, σ_LT medido (2) reemplazaba al conservador (15) | Entre 3 y 5: el **mayor** de los dos (media y σ); desde 6 manda lo medido |
| **D10** | `detectar_deriva` leía `ItemRecepcion.costo_unitario` y `RecepcionMercancia.fecha_recepcion`, que **no existen**: 500 con el primer acuerdo | Enchufe `compras_inteligencia_service.precios_de_compra_recibidos`, **conectado a las OCs de Siesa** (`compras_fuentes.lineas_precio_oc`, con cantidad); sin OCs lo declara en `nota` y `sin_precio_de_compra`; `impacto_estimado_cop` = (OC − pactado) × cantidad, en la misma moneda |
| **D11** | Capital inmovilizado siempre 0 (`Producto.costo_unitario` no existe) | `resolver_costos`; sin costo → `None`, `skus_sin_costo`, `total_es_cota_inferior`, y adelante en la lista |
| **D12** | `POST /newsvendor` hacía m/(m+e) con m sobre precio y e sobre costo; la cabecera de temporada mostraba otro ratio que las filas | `ratio_critico(cu, co)` y `ratio_critico_desde_tasas(m, e) = m/(m + e·(1−m))`, una fórmula para filas y cabecera; margen ≥ 1 → 400 |
| **D13** | El MOQ se usaba como **múltiplo** (60 u, caja 12, MOQ 4 → 96 u); el presupuesto recortaba solo relleno y devolvía un contenedor más caro que el presupuesto sin decirlo | `cajas_a_pedir`: MOQ como **mínimo**, redondeo a caja (`MOQ_INTERPRETACION`, en cada fila). Presupuesto: relleno y después déficit (del menos urgente), `presupuesto_insuficiente` + `deficit_sin_cubrir_por_presupuesto` |
| **D14** | LT nacional 5 d fijo; un SKU que dejó de venderse seguía con d > 0 en el ROP | `compras_fuentes.lead_time(proveedor, origen)` (cascada proveedor → origen → default; `ROP_LT_NACIONAL_DIAS`, `ROP_SIGMA_LT_NACIONAL` configuran el default nacional, `CONFIGURADO`; sin ellas `DEFAULT_CONSERVADOR`, declarado). «Sin venta reciente» (`kardex_service.sin_venta_reciente`): 0 ventas en `ROP_DIAS_SIN_VENTA` (90) días, con stock ≥ la mitad, y ≥ 3 ventas esperadas a su tasa (Poisson) → d = 0 en el ROP, con `motivo_d_cero` |

Además:

- **En camino, declarado**: `compras_fuentes.en_camino()` suma sus dos
  fuentes (`FUENTES_EN_CAMINO`: OCs abiertas de Siesa y `ItemEnTransito`,
  contando contenedores `EN_PRODUCCION`; BORRADOR no, RECIBIDO no). Sin dato,
  `insumo_en_camino.nota` dice que el 0 es «no se sabe»; una fuente que
  revienta va a `fuentes_con_error`.
- **Una posición**: `posicion_inventario()` (bodegas operadas + en camino),
  la misma para el ROP y la temporada.
- **Tamiz de TSB**: pesa con `resolver_costos` (antes `precio_compra`, siempre
  0); `sin_costo_para_ponderar` declarado.
- **Bloqueo de recompra**: la velocidad sale de `demanda_descensurada` (red,
  todas las bodegas) y no de `TareaPicking` —lo que se vende por POS salía
  bloqueado en masa—. Sin kardex que cubra 12 meses y esté al día
  (`KARDEX_DIAS_FRESCURA`) **no se bloquea nada** (`no_se_bloqueo_por`).
- Trinquetes AST de clase: el costo de un producto solo desde
  `costo_service` (ningún `precio_compra`/`costo_unitario` de maestro en
  `app/services`) y la moneda solo en `costo_service`. Los del lead time y lo
  que viene se fusionaron con los de las fuentes en
  `tests/test_compras_fuentes_trinquetes.py`.

### Lo que NO cubre, dicho

- **La pantalla**: temporada sigue mostrando Q* (no «pedir»); el ROP muestra
  las filas con d = 0 sin explicación visual (el `motivo_d_cero` está en la
  respuesta). El rediseño de pantallas viene después. Cambios mínimos: el
  capital sin costo se pinta «sin costo» (no $0), el poblado de bloqueos avisa
  cuando no bloqueó por falta de kardex, y la leyenda del ROP ya no promete el
  tope ▲.
- **«Hay» es de hoy**: lo que se venda entre hoy y el inicio de la temporada
  no está descontado del pedido.
- **Las bodegas de servicio en el denominador**: un día con stock solo en `AV1`
  cuenta como día con stock (sesgo conservador: demanda más baja). No se filtró
  por `_BODEGAS_PV` a propósito — cambiaría todas las series y no era el defecto.
- **La deriva compara solo cuando hay OCs sincronizadas** (el espejo nace
  apagado, `COMPRAS_OC_SYNC`); hasta entonces lo declara.
- **El lead time nacional sigue siendo un supuesto** (5 ± 2 días) hasta que
  el espejo tenga OCs cumplidas con su fecha de entrada.
- **Rendimiento**: `intervalos_con_stock` trae las filas de `StockDiario` de la
  ventana (del orden de las que ya traía la numeración por día de la
  demanda). No se midió contra el volumen de producción.
- El `TOTAL` del TXT de Vigía puede traer IVA: se usa y se declara.

Suite completa en el worktree (2026-09-24, `-m "not postgres"`, TZ=UTC):
**8585 passed, 0 failed** (5 skipped, 19 xfailed).

### Decisiones para el dueño

1. **MOQ**: la ficha no dice si es mínimo por pedido o lote (múltiplo). Hoy se
   lee como **mínimo** y se redondea a caja. Si algún proveedor exige múltiplo,
   hace falta el dato en la ficha.
2. **Nivel de servicio**: el ROP y el S objetivo usan 95% por defecto; sin el
   tope de 180 días los S de China **suben** (a ~LT + R + colchón). ¿95% para
   todo, o distinto para la canasta constitucional?
3. **TRM y factor de nacionalización**: hoy 4.200 y 1,45 **supuestos**
   (`TRM_COP_USD`, `FACTOR_NACIONALIZACION`). ¿Quién los fija y cada cuánto?
   ¿Los precios en USD de los acuerdos son FOB (hoy se asume que sí)?
4. **«Sin venta reciente»**: 90 días y el umbral de Poisson (≥ 3 ventas
   esperadas) son provisionales.

---

## Compras: las fuentes de datos (m046compras, 2026-09-24)

**Diagnóstico:** compras no tenía datos de entrada que se llenaran solos. El
término de tránsito del armador leía `ItemEnTransito`, sin escritor (valía 0);
`proveedores` estaba vacía; el lead time era una constante (5 d nacional,
105 d China); `origen`, `marca_siesa` y `ficha_importacion` no tenían ninguna
fuente; el kardex solo se descargaba con un botón. Esto construye **las
fuentes**, no las pantallas de compras (salvo lo mínimo de carga).

Migración **`m046compras`** (down `m045devol`, aditiva, nullable, sin
backfill): tabla `oc_linea_siesa`; `proveedores.fuente/sincronizado_en`;
`items_en_transito.oc_referencia/bodega_destino`;
`productos.origen_fuente/marca_fuente`.

### Una política, una función — `app/services/compras_fuentes.py`

| Pregunta | Función | Regla |
|---|---|---|
| ¿Cuánto falta por entrar de una línea de OC? | `pendiente_de_linea(fila)` | En unidad de **inventario**: `f421_cant_pedida − f421_cant_entrada` (**en la API, `f421_cant_*` ya es unidad de inventario y `f421_cant_*_base` la de la LÍNEA** — corregido 2026-09-25, ver «lo que encontró la verificación en vivo»); sin ellas, `(pedida_base − entrada_base) × factor`; sin factor, **`None`** (no se inventa: se cuenta como «sin unidad base») |
| ¿Cuánto viene en camino? | `en_camino(skus, bodegas)` | **La única.** Líneas de OC **abiertas** + ítems de contenedor comprados y sin recibir (cuenta el estado del **contenedor** si lo tiene —`ESTADOS_EN_CAMINO`, con `EN_PRODUCCION`—, si no el del ítem; `RECIBIDO` nunca), **solo a `_BODEGAS_PV`** (lista blanca: AV1/TRA1 nunca; una bodega pedida no operada se ignora y se declara). Un contenedor que cita su OC (`oc_referencia` = `CO-TIPO-CONSEC`) y la OC sigue abierta **no se suma dos veces**; si cita una OC ya cerrada no se suma (ya entró o se anuló); sin cita, sobre un SKU con OC abierta, se suma y se marca `solapamiento_posible` (contar de más achica el déficit: es el lado corregible, Regla 0) |
| ¿Cuánto tarda? | `lead_time(proveedor, origen)` | Cascada **proveedor (≥3 OCs medidas) → origen → default declarado** (`default_lead_time`: 5±2 nacional, configurable con `ROP_LT_NACIONAL_DIAS`/`ROP_SIGMA_LT_NACIONAL` → `CONFIGURADO`; 105±15 China), con `n`, `fuente` (MEDIDO ≥6 · PARCIAL ≥3 · CONFIGURADO · DEFAULT_CONSERVADOR), `confianza` y `nivel`. **PARCIAL no baja del default** (D9 del motor): con 3 a 5 observaciones, el mayor entre lo medido y el default, para la media y la σ (`lt_medido`/`sigma_lt_medida` publican lo medido). China → contenedores (`fecha_oc → fecha_recepcion_cedi`); nacional → OCs en COP de proveedores no chinos. Observación de una OC: fecha de la OC → **primera entrada**: la confirmación de la recepción del WMS si existe (fecha física), si no la primera marca de Siesa (`f420_fecha_ts_parcial`/`_cumplido`); una entrada anterior a la OC se descarta y se cuenta |
| ¿A cuánto dice cada OC? | `lineas_precio_oc(refs, desde, proveedor)` | **La única lectura del precio de una OC**: por unidad base (`precio / factor`), en la moneda de la OC, con la cantidad pedida base; sin obsequios ni anuladas |
| ¿A cuánto lo compramos? | `precios_oc(refs)` | OC más reciente **en pesos vía `costo_service.a_cop`** (D4: USD = FOB × TRM × factor, declarado en `conversion`; otra moneda se excluye y se declara con `costo: None` + `motivo_exclusion`); a igual fecha la más alta |
| ¿La OC respetó el acuerdo? | `compras_inteligencia_service.detectar_deriva` | **El único comparador.** El enchufe del motor `precios_de_compra_recibidos` lee `lineas_precio_oc`: OC vs acuerdo en la misma moneda, con cantidad e `impacto_estimado_cop`; `mismo_proveedor` distingue la OC del proveedor del acuerdo de la de otro. (`precio_oc_vs_acuerdo`, que hacía lo mismo sin cantidades ni moneda, se retiró) |

**Armador** (`armador_service.rop_dual`, cambio localizado): el término de
tránsito es `compras_fuentes.en_camino()['por_sku']` y el lead time de cada
SKU es `lead_time(proveedor_habitual(ref), origen)` — el proveedor de su OC más
reciente. Cada fila publica `lt_fuente`, `lt_nivel`, `lt_n`, `lt_proveedor`; el
resultado trae `insumo_en_camino` (la declaración: fuentes, errores,
`hay_dato`, frescura del espejo, líneas sin unidad, solapamientos). La suma la
hace `armador_service.posicion_inventario` (la misma del pedido de temporada,
D2), que consume `en_camino` y no calcula nada. `calcular_sigma_lt_real` delega en
`lead_time(origen='CHINA')`. Los defaults `LT_*`/`SIGMA_LT_*` viven en
`compras_fuentes` y el armador solo los re-exporta.

**Costo** (`costo_service`): capa nueva **`OC_SIESA`** entre `COTIZACION` y
`KARDEX_PROMEDIO`, con antigüedad (`anejo` > 180 días). No cuenta como «costo
hacia adelante» (`confiable`/`pct_hacia_adelante` siguen siendo acuerdo y
cotización): es la última compra real, no un compromiso.

### El espejo de OCs — `app/services/compras_oc_sync.py`

Fuente: `API_v2_Compras_Ordenes` (**contrato completo**, 89 campos, Regla 1).

| Corrida | Filtro | Cuándo |
|---|---|---|
| `sincronizar_ocs` | `f420_ind_estado = 1` y `= 2` (Aprobada, Parcial) | cada 30 min, 7:10–19:40 |
| `sincronizar_historial` | `f420_ind_estado = 3 AND f420_fecha >= ''AAAAMMDD''` (`lit_fecha`), `COMPRAS_OC_HISTORIAL_DIAS` (365) | 7:20 |
| `sincronizar_proveedores` | `API_v2_Proveedores`: `f202_ind_estado = 1`; en Python, compañía `SIESA_ID_CIA` y `COMPRAS_TIPOS_PROVEEDOR` (reemplazó a la de `API_v2_Terceros` el 2026-09-25) | 7:20 |

- **tamPag = 100** (Regla 10); se pagina hasta una página corta, tope
  `COMPRAS_OC_MAX_PAGINAS` (60).
- **La paginación incompleta no cierra nada**: una página que falla (red,
  429, 401, 5xx), el circuito abierto (`_get` → `None`), el rechazo `alerta`,
  el tope con la última página llena, o **un `f421_rowid` repetido dentro de la
  misma enumeración** (orden inestable, como el kardex y InvFecha) dejan la
  corrida INCOMPLETA: lo visto se guarda (upsert) y **ninguna línea no vista se
  cierra**. Solo un barrido completo marca `abierta=False`,
  `motivo_cierre='NO_APARECE_EN_ABIERTAS'`. **Nada se borra nunca.** Y un
  barrido que devuelve **cero** OCs abiertas cuando el espejo tenía abiertas
  tampoco cierra (más probable un filtro contestado con «sin registros» o un
  ambiente vacío que «se cumplieron todas»): se declara incompleto.
- `registros_sync` tipos `compras_oc` / `compras_oc_historial`: `ok=True` solo
  con paginación completa. `compras_fuentes.frescura_oc` lee la última
  completa: sin ninguna, «en camino» desde Siesa vale 0 **y lo dice**.
- Proveedores: upsert por `codigo` = `f200_id_prov`; Siesa es maestro de la
  razón social y el NIT; contacto y país cargados a mano no se tocan.
- **Nace apagado** (`COMPRAS_OC_SYNC=true`), en `_scheduler_pesados`
  (`[COMPRAS_OC_SYNC]`), ventana 7:00–19:30 (Regla 14),
  `LOCK_COMPRAS_OC_SYNC = 2024`. El botón de la pantalla sincroniza aunque el
  cron esté apagado, pero **no fuera de la ventana** (409).

| Variable | Defecto | Qué es |
|---|---|---|
| `COMPRAS_OC_SYNC` | ausente = apagado | Enciende el cron |
| `COMPRAS_OC_MAX_PAGINAS` | `60` | Tope por consulta (6.000 líneas) |
| `COMPRAS_OC_PAUSA_S` | `0.5` | Pausa entre páginas |
| `COMPRAS_OC_HISTORIAL_DIAS` | `365` | Ventana del historial |
| `CONNEKTA_API_PROVEEDORES` | `API_v2_Proveedores` | Maestro de proveedores (reemplazó `CONNEKTA_API_TERCEROS`, 2026-09-25) |
| `COMPRAS_TIPOS_PROVEEDOR` | — (sin default) | Tipos de proveedor que son mercancía (`0001,0002`…); sin ella, solo los de las OCs |
| `COMPRAS_OC_VENCIDA_DIAS` | `90` | Desde cuántos días de entrega vencida una OC abierta se declara vieja |
| `COMPRAS_OC_EXCLUIR_MAS_DE_DIAS` | — (sin default) | Corte: las más viejas que esto no se suman a «en camino» |
| `SIESA_CRITERIO_MARCA` | — (sin default) | Plan de clasificación que es «marca» (QA: `P03`) |
| `COMPRAS_MARCA_MAX_PAGINAS` / `COMPRAS_MARCA_PAUSA_S` / `CONNEKTA_API_ITEMS_CRITERIOS` | `400` / `0.2` / `API_v2_ItemsCriterios` | Lectura de la marca |

### Kardex automático — `app/services/kardex_auto.py`

El disparo programado de lo que ya existía; **no toca la descarga ni la
reconstrucción** (el defecto del reconstructor lo arregla el frente del motor).

- **Nace apagado** (`KARDEX_AUTO=true`), `[KARDEX_AUTO]` en
  `_scheduler_pesados`, :02 y :32 de 7 a 19 h; `ciclo` decide si está en su
  ventana: `KARDEX_AUTO_VENTANA` (`HH:MM-HH:MM` Bogotá, default `07:00-07:55`,
  **recortada a la de Siesa**; ilegible → default, declarado). Cada corrida
  dura **como mucho lo que queda de ventana** y nunca más que
  `KARDEX_MAX_MINUTOS`. `LOCK_KARDEX_AUTO = 2025`.
- Respeta `estado_descarga`: en curso → no arranca; INTERRUMPIDA → página 1;
  PARCIAL → `reanudar_desde`; COMPLETA de hoy → no se repite.
- **Solo reconstruye sobre una descarga COMPLETA** (nunca `forzar`), o si ya
  hubo una completa hoy y `salud_kardex` dice `STOCK_DIARIO_ATRASADO`.
- `kardex_auto.descargar_con_registro` es **la única** que llama
  `descargar_kardex` (la usan el botón y el cron: un registro, una forma);
  `salud_kardex.actualizacion_automatica` lee el interruptor del cron
  (trinquete `TestNadieDescargaElKardexSolo`, actualizado).
- `KARDEX_AUTO_DESDE` (AAAAMMDD, default `20240101`).
- `/api/health/siesa` → `kardex_auto` (ventana pedida/efectiva, minutos por
  día, dónde quedó la última) y `compras_oc` (frescura, líneas, proveedores por
  fuente), con `cron_en_este_proceso`.

⚠️ **En QA el kardex sigue en 401** (permiso de la consulta, no código) y la
paginación de las dinámicas no es determinista: encendido, cada corrida deja
el error escrito o termina INCOMPLETA, y no reconstruye. Una vuelta completa
son ~17.000 páginas: con 55 min/día tarda semanas; ampliar la ventana es
decisión del dueño (choca con la facturación de las tiendas).

### Origen, marca y fichas — `app/services/maestro_compras_carga.py`

Tres vías, todas con **vista previa que es el mismo cálculo que la aplicación**:
archivo CSV/Excel (`ORIGEN_MARCA`: `codigo, origen, marca` · `FICHAS`:
`codigo, unidades_por_caja, cbm_por_caja, peso_kg_por_caja[, moq_cajas,
proveedor_china, costo_fob_usd, fuente]`), un SKU a mano, y **la marca leída de
Siesa**. Queda `origen_fuente`/`marca_fuente` = `CARGA_ARCHIVO` | `MANUAL` |
`SIESA_238920`. Una celda vacía **no borra**; sobrescribir un valor queda en la
bitácora (EDITAR). Código desconocido, origen fuera de NACIONAL/CHINA/IMPORTADO,
número ambiguo (`1.234,5`), fuera de rango o fila repetida → inválida con su
motivo. Una ficha nueva sin `fuente` nace `ESTIMADO` (el armador la excluye del
armado automático) y `moq_cajas=1`.

**`SIESA_CRITERIO_MARCA` — sin default, lo decide el dueño.** En Siesa la marca
es un criterio de clasificación del ítem (plan + criterio mayor, t125). La
variable es el **plan** que es «marca» (en QA, `P03`). Sin ella no se lee nada.
**Desde el 2026-09-25 se lee de `API_v2_ItemsCriterios`, en segundo plano**, y
no del 238920 (ver «lo que encontró la verificación en vivo»).

### Pantalla mínima — Compras → 🧾 Fuentes (`compras_fuentes.js`)

Estado del espejo de OCs (con botones de sincronizar), en camino (top 20 y lo
que no suma), lead time por proveedor con `n`, kardex automático, carga de
archivo (vista previa → aplicar), un SKU a mano, marca desde Siesa, y
**contenedores**: registrar (el `POST /api/compras/armador/contenedores` que ya
existía sin pantalla), cargar ítems (`codigo,cantidad` + OC citada; todo o
nada) y cambiar estado. **`RECIBIDO` saca los ítems de «en camino» y fecha la
llegada al CDI** —esa fecha es la observación de lead time de China—;
`BORRADOR` no cuenta; `EN_PRODUCCION` sí. Endpoints en `/api/compras/fuentes/*`,
todos `_es_compras()`.

### Acta de corte y respaldo

`oc_linea_siesa` es **OPERATIVA** (se vacía en el corte: si el ensayo corrió
contra Siesa QA, sus OCs ensuciarían el lead time y el precio reales) y
**REGENERABLE** en `verificar_restauracion` (abiertas con la próxima
sincronización, cumplidas con el historial). `proveedores`, `contenedores`,
`items_en_transito`, `ficha_importacion` siguen como maestras protegidas.

### Trinquetes y mutaciones

`tests/test_compras_fuentes.py` (73, Siesa falsa con los campos del contrato),
`tests/test_compras_fuentes_trinquetes.py` (29, AST; **el único trinquete de
lo que viene y del lead time**, fusionado con el del motor) y
`tests/test_compras_fuentes_js.py` (5, Node con `util.js` real).

- **Nadie calcula «en camino» fuera de `compras_fuentes`**: restar cantidades
  de OC (`f421_cant_*` o atributos `cant_pedida*`/`cant_entrada*`), leer
  `ItemEnTransito.cantidad`, leer `pendiente_base`, sumar (`sum`/`func.sum`)
  en una función que lee `ItemEnTransito` (la forma del motor) o leer
  `ESTADOS_EN_CAMINO`. Operar el contenedor (cargar, mover, contar en G5) no
  cuenta. Inventario de 3 (el muelle
  de recepción `ordenes_compra`, `debug_oc`, y la recepción de tienda: cuánto
  falta RECIBIR de una OC puntual, no posición de compra), solo encoge.
- **Nadie decide un lead time fuera de `lead_time`**: leer `LT_*`/`SIGMA_LT_*`
  (o `ENV_*`), las variables `ROP_LT_NACIONAL_DIAS`/`ROP_SIGMA_LT_NACIONAL`,
  `.lead_time_real` o restar una `fecha_oc`. Inventario vacío. Piso: el
  Armador no aparece en ninguno de los dos escáneres.
- **El sync no cierra sin ver todo y nada borra una línea de OC**: el cierre
  vive bajo `if completa` (AST) y ningún `.delete(` toca `OcLineaSiesa`.
- Meta-tests (las formas que ve, lo sano que no: docstring, comentario,
  escritura, constructor, función anidada) y pisos (el dueño aparece; ≥200
  archivos recorridos).
- **31 mutaciones, las 31 rojas** (cierre con barrido incompleto, cero OCs que cierran todo, rowid
  repetido, circuito abierto, tope, lista blanca, doble conteo del contenedor
  —dos variantes—, factor, armador sin en camino / con su propia suma / con LT
  constante, proveedor ignorado, Siesa sobre la recepción, USD, unidad base del
  precio, capa de costo, bitácora, celda vacía que borra, ventana del kardex,
  reconstruir sobre parcial, los dos crons naciendo encendidos, marca sin
  filtrar plan, contenedor a medias, `esc`, aplicar sin vista previa,
  RECIBIDO, salud del kardex, segundo llamador de la descarga, historial sin
  comillas). Corridas con `-B` (sin `.pyc` viejo) y verificando que cada
  reemplazo aplicara exactamente una vez.

### Qué verificar mañana en vivo (7:00–19:30)

`venv/bin/python scripts/qa_compras_fuentes_real.py` (solo GET, `MODO_ENSAYO`,
`.env.qa`, SQLite desechable, `_post` bloqueado): (1) OCs abiertas con estado 1
y 2 — que la paginación termine sin rowids repetidos, **qué campos del contrato
no vienen**, cuántas líneas sin `_base`, a qué bodegas y en qué monedas;
(2) que `pedida = pedida_base × factor` (inventario = línea × factor, corregido 2026-09-25); (3) el historial con la fecha entre
comillas vs sin comillas, y cuántas cumplidas traen `ts_parcial`/`ts_cumplido`
(de eso depende el lead time medido); (4) si `API_v2_Terceros` está registrada
(o 401); (5) los campos reales del GET 238920 y los **planes** que existen,
para elegir `SIESA_CRITERIO_MARCA`; (6) una página del kardex (¿sigue 401?);
(7) en camino, lead time por proveedor y precios con lo sincronizado.

### Lo que NO cubre, dicho

- **`f420_fecha_ts_parcial` como «primera entrada» no está verificado**; si
  Siesa lo llena con otra cosa, el lead time medido desde Siesa está corrido
  (el de la recepción del WMS no).
- **El filtro `IN (1,2)` no se usa**: son dos consultas; una OC que pasa de 2 a
  3 entre las dos no se pierde (la siguiente completa la cierra).
- **OCs de importación en USD** dan precio con la TRM y el factor del sistema
  (`a_cop`), **no con la tasa del documento** (`f420_tasa_conv` queda en el
  espejo sin usar), y no entran al pool nacional de lead time.
- **`solapamiento_posible`** se suma igual: el WMS no puede saber si el
  contenedor sin OC citada es la misma mercancía de la OC abierta.
- **El lead time por SKU** usa el proveedor de su OC más reciente; un SKU que
  se compra a varios proveedores usa el último.
- **El kardex automático** no cambia el defecto del reconstructor ni la
  inestabilidad de la paginación de Siesa; solo lo dispara.
- **La marca de Siesa** puede traer miles de ítems que el WMS no tiene: salen
  como inválidos («no existe en el catálogo»), recortados a 300 en la pantalla.
- `Contenedor` no tiene estado de «cancelado»: uno que no va a llegar se deja
  en «En armado» (no cuenta).

### Qué tiene que hacer el dueño

1. **Permiso del kardex en Siesa**: la consulta `…_KardexWMS` da 401 en QA
   (Administración → Permisos servicios → consultas dinámicas) y pedir al
   consultor un `ORDER BY` estable por clave única. Sin eso, `KARDEX_AUTO` no
   sirve.
2. **Elegir el plan de marca**: correr el script, mirar los planes del 238920
   y poner `SIESA_CRITERIO_MARCA=<plan>` en Railway. Después «Ver qué
   cambiaría» → «Aplicar» en 🧾 Fuentes.
3. **Cargar el origen y las fichas de importación** (CSV/Excel en 🧾 Fuentes):
   sin `origen` el régimen China del armador no se activa; sin fichas
   verificadas (`PACKING_LIST`/`AGENTE`) el armador sigue en shadow.
4. **Encender** `COMPRAS_OC_SYNC=true` (y `KARDEX_AUTO=true` cuando el 401 se
   resuelva) **en el worker** con `HEAVY_SCHEDULERS=true`.
5. **Registrar los contenedores en curso** con sus ítems y la OC que mueven.

### Integración de los dos frentes (motor + fuentes, 2026-09-25)

Los dos frentes de Compras se escribieron en paralelo y cada uno dejó su
propia función —y su propio trinquete— para las mismas dos preguntas. Al
integrarlos quedó **un dueño por concepto**, sin aflojar ninguna regla:

| Concepto | Dueño (la única función) | Qué aportó cada frente |
|---|---|---|
| Lo que viene | `compras_fuentes.en_camino(skus, bodegas)` | Fuentes: OCs abiertas, lista blanca de bodegas, anti doble conteo por OC citada. Motor: el estado del **contenedor** manda, `EN_PRODUCCION` cuenta, una fuente que revienta se declara (`fuentes_con_error`, `completo`), `hay_dato`/`nota` («no se sabe» ≠ 0), `insumo_en_camino` en `rop_dual` |
| La posición | `armador_service.posicion_inventario` (motor, D2) | Consume `en_camino`; el ROP y la temporada la leen |
| El lead time | `compras_fuentes.lead_time(proveedor, origen)` | Fuentes: la cascada proveedor → origen → default con OCs medidas. Motor: D9 (3 ≤ n < 6 → el mayor), `ROP_LT_NACIONAL_DIAS`/`ROP_SIGMA_LT_NACIONAL`. **Un juego de defaults**: `default_lead_time` |
| Precio de una OC | `compras_fuentes.lineas_precio_oc` | Una lectura para el costo (`precios_oc`, capa `OC_SIESA`) y la deriva |
| OC vs acuerdo | `compras_inteligencia_service.detectar_deriva` | El enchufe del motor, conectado a las OCs; se retiró `precio_oc_vs_acuerdo` |
| Moneda | `costo_service.a_cop` (motor, D4) | Ahora también convierte la OC en USD |

Salieron de `armador_service`: `en_camino`, `FUENTES_EN_CAMINO`,
`_en_camino_importacion`, `ESTADOS_EN_CAMINO`, `lead_time`,
`_lead_time_de_proveedor`. Siguen re-exportados `LT_*`/`SIGMA_LT_*` (solo
import). El vocabulario del default es `DEFAULT_CONSERVADOR` (el de China, las
fuentes y la pantalla); `DEFAULT_DECLARADO` se retiró.

**Tests que cambiaron por la fusión, y por qué** (cada uno lo dice en su
cuerpo): el de China con 3 contenedores de 90/100/110 espera 105 y no 100
(D9); el ROP publica la declaración en `insumo_en_camino` y no en `en_camino`;
`en_camino` devuelve `por_sku`/`declaracion` y no `por_ref`/`hay_dato` sueltos;
el default nacional se llama `DEFAULT_CONSERVADOR`; la OC en USD se nacionaliza
en vez de excluirse; el test de `precio_oc_vs_acuerdo` pasó a probar
`detectar_deriva` con una OC real. `TestLeadTimeYEnCaminoEnUnaFuncion`
(motor) se retiró: su detección vive en el trinquete de las fuentes.

Locks: `LOCK_COMPRAS_OC_SYNC = 2024` y `LOCK_KARDEX_AUTO = 2025` en el
registro de `app/utils/lock.py`, sin choques (el motor no agregó locks).
Migraciones: `m046compras` cuelga de `m045devol`; una sola cabeza.

**19 mutaciones de la fusión, las 19 rojas** (con `-B` y
`PYTHONDONTWRITEBYTECODE`, cada reemplazo verificado a aplicar una vez): el
Armador sumando lo que viene por su cuenta, decidiendo los estados, leyendo la
constante de LT; otro módulo leyendo `ROP_LT_NACIONAL_DIAS`; D9 quitada;
`EN_PRODUCCION` fuera; el estado del ítem mandando sobre el del contenedor;
contenedor cubierto por su OC sumado dos veces; OC cerrada citada sumada;
fuente que revienta sumando cero callada; OC en USD sin convertir; el enchufe
de la deriva de vuelta en `SIN_FUENTE`; `ROP_LT_NACIONAL_DIAS` ignorada;
`hay_dato` siempre verdadero; la posición sin lo que viene; el ROP ignorando
al proveedor habitual; la capa de costo tomando un costo `None`; y los dos
escáneres ciegos a la suma por instancia y a la variable de entorno.

### Lo que encontró la verificación en vivo (2026-09-25)

La primera corrida de `qa_compras_fuentes_real.py` contra Siesa QA (solo GET,
`MODO_ENSAYO`, SQLite desechable) encontró cinco defectos. Cada uno se cerró
por su clase, con tests que usan **la forma real** de los datos (copiada de la
respuesta cruda, no del supuesto). Migración **`m047comprasvivo`** (down
`m046compras`, aditiva, nullable, sin backfill).

| | Clase | Qué pasaba | Ahora |
|---|---|---|---|
| **D1** GRAVE | Leer una cantidad de OC en la unidad equivocada | `API_v2_Compras_Ordenes` trae `f421_cant_*` en unidad de **inventario** y `f421_cant_*_base` en la de la **línea** (OC 003-OC-28: PQ de 12, pedida 36, pedida_base 3, `vlr_bruto` 3.750 = 3 × 1.250; `API_v2_Items` dice UND; `ItemsUnidadesMedida` 1 PQ = 12 UND). `pendiente_de_linea` y `_cantidad_base` restaban las `_base`: **«en camino» ×12 de menos en toda línea PQ** → déficit inflado → contenedor de más (Regla 0). En las 280 líneas con factor 1 coincidían; el fixture de los tests fijaba la relación al revés | `pendiente = pedida − entrada`; sin ellas `(pedida_base − entrada_base) × factor`. `_cantidad_base` igual; `_precio_base` (precio ÷ factor) se queda. El fixture `fila_oc` tiene la relación real y `LINEAS_REALES` son las líneas crudas. Trinquete: **la unidad de la línea solo la lee quien la multiplica por el factor** (`LECTORES_UNIDAD_LINEA`, 3, solo encoge). El espejo se recalcula en la próxima sincronización |
| **D2** | Una observación que no mide lo que dice medir entra a la estadística | Lead time **0 días, MEDIDO, confianza ALTA** para DISPAPELES (n=24): las 24 OCs se crearon y cumplieron el mismo día, con minutos de diferencia — se digitaron **al recibir** | Entrada < 1 día después de la OC → `descartadas['oc_registrada_al_recibir']` (y por proveedor), fuera de la media y la σ. Sin muestra válida, cae al origen y al default declarado; la nota y la pantalla lo dicen (`(+24 al recibir)`) |
| **D3** | Una lectura multicompañía sin filtro de compañía | `API_v2_Terceros` trae las compañías 1 y 2 (gana la última fila: YIWU salía COP siendo USD en la 1) y todo tercero con `ind_proveedor`: EPS, fondos de pensiones, cédulas | `sincronizar_proveedores` lee **`API_v2_Proveedores`** (una fila por **sucursal**: la clave de paginación es cía + rowid + sucursal, si no dos sucursales parecían una página repetida), solo `SIESA_ID_CIA`. Qué es mercancía: `COMPRAS_TIPOS_PROVEEDOR` (**sin default**); sin ella solo los proveedores de las OCs, y nunca crea uno desde el maestro. Guarda `Proveedor.moneda` y `tipo_proveedor` (si todas las sucursales coinciden) y la condición de pago; no pisa el nombre que pone el sync de OCs. Las filas del sync viejo (`SIESA_TERCEROS`) que no son mercancía se **desactivan con bitácora** en un barrido completo. `_aplicar_lineas` descarta líneas de otra compañía (`otra_compania`). La moneda del proveedor cubre la OC que no la trae (`moneda_fuente`: OC · PROVEEDOR · SUPUESTA_COP). Trinquete: **toda descarga de compras filtra la compañía** (ella o una función del módulo que llama) |
| **D4** | Una lectura larga de Siesa dentro de un request | La marca leía el **238920** (plano de importación: el GET da 401) dentro del request: el plan P03 son **261 páginas, 292 s**, y gunicorn corta a los 60 | `API_v2_ItemsCriterios` con `f125_id_plan = ''P03''` (`lit`), en un **hilo** (`disparar_lectura_marca`, `LOCK_MARCA_SIESA = 2026`, registro `compras_marca`, techo 20 min para una lectura muerta), `COMPRAS_MARCA_MAX_PAGINAS` (400) y clave (ítem, plan): un rowid repetido o el tope → **INCOMPLETA, no se guarda ni se aplica, se declara**. Queda en **`marca_siesa_lectura`** (OPERATIVA en el acta de corte, REGENERABLE en el respaldo); la vista previa y «Aplicar» leen de ahí, solo los ítems del catálogo (`fuera_del_catalogo` contado). **`Producto.marca_siesa` es el NOMBRE** (NORMA) y **`Producto.marca_codigo`** (m047) el código (M001); el Armador compara `MARCAS_CHINA` contra el código (y contra `marca_siesa` por compatibilidad). Fuente `SIESA_CRITERIOS`. Trinquete: **ninguna ruta llama una lectura larga de compras** (`leer_marca_siesa`, `sincronizar_*`, `_descargar`) |
| **D5** | Un insumo que se suma sin declarar su antigüedad | En QA el 90 % de lo pendiente es de OCs de más de un año (con 90 días, el 100 %: 293.680 u de 293.750) | `en_camino` **las suma igual** (decisión del dueño) y declara `ocs_vencidas` (líneas, unidades, % del total, SKUs, la entrega más vieja) con `COMPRAS_OC_VENCIDA_DIAS` (default declarado **90**); cada SKU trae `oc_vencida`. Corte opcional `COMPRAS_OC_EXCLUIR_MAS_DE_DIAS` (**sin default**): lo más viejo no se suma y se dice. Una variable ilegible no corta y se declara. La pantalla lo muestra (columna «Vieja») |

**Revisado y sin cambio:** el muelle (`routes/siesa.ordenes_compra`,
`debug_oc`) y la recepción de tienda leen `f421_cant_pedida/_entrada` como
unidades — correcto con la regla verificada. **Riesgo declarado, sin probar:**
la entrada 142948 manda `f470_cant_base` = unidades contadas con
`f470_id_unidad_medida` = la unidad de la línea (PQ). Si en el movimiento
`_base` también fuera la unidad de la línea, una recepción de una línea PQ
entraría ×12. Ninguna OC con factor ≠ 1 se ha recibido nunca en QA; probarlo
con una OC fresca en paquetes antes de recibir una en producción.

**Verificado en vivo después de los arreglos** (Siesa QA, solo GET, scripts
`cmpfix_*` del scratchpad): en camino `PAPELSP6948` = **432** y `PAPELSP6741`
= **5.509**; precio de la OC-28 104,17 × 36 = 3.750; lead time: 1 observación
válida, 31 al recibir, DISPAPELES en el default declarado 5 ± 2; proveedores:
sin `COMPRAS_TIPOS_PROVEEDOR`, 21 (los de las OCs), 832 filas de la compañía 2
descartadas, cero EPS, YIWU en USD; con `0001,0002,0018`, 632. Marca P03 por
el mismo camino del botón (hilo): el botón respondió en 0 s y un segundo
disparo dio 409 «en curso»; la lectura, **261 páginas, 26.053 ítems, 141 marcas,
276 s, sin rowids repetidos, completa**; aplicada a 4 SKU del catálogo local:
`PAPELSP016 → NORMA (M001)`, `FIESTSF104 → QUIROND (M003)`,
`PAPELSP6948 → SANFORD (M008)`. Migración `m047comprasvivo` upgrade → downgrade →
upgrade contra un PostgreSQL 17 desechable.

Trinquetes nuevos en `tests/test_compras_fuentes_trinquetes.py` (clases 4, 5 y
6, con meta-tests y pisos); tests con datos reales en `test_compras_fuentes.py`
(`LINEAS_REALES`, `OCS_DISPAPELES_QA`, `PROVEEDORES_QA`, `CRITERIOS_P03_QA`) y
`test_compras_fuentes_js.py`. **34 mutaciones, las 34 rojas** (cada reemplazo
verificado a aplicar exactamente una vez, con `-B` y sin `.pyc`).

Suite completa en el worktree (2026-09-25, `-m "not postgres"`, TZ=UTC):
**8756 passed, 0 failed** (5 skipped, 19 xfailed).

**Decisiones para el dueño:**
1. `COMPRAS_TIPOS_PROVEEDOR`: en el maestro «Tipo de proveedor», 0001 =
   nacionales, 0002 = del extranjero, 0017 = gastos diversos, **0018 = NÓMINA**,
   0019 = aportes y seguridad social. Las OCs de QA usan 0001, 0018, 0017 y 0002:
   ¿cuáles son mercancía? Probablemente `0001,0002`.
2. `SIESA_CRITERIO_MARCA=P03` (en QA: P01 línea de negocio, P02 sub línea, P03
   marca). Después «Leer de Siesa» → «Ver qué cambiaría» → «Aplicar».
3. OCs viejas abiertas: anularlas en Siesa, o fijar
   `COMPRAS_OC_EXCLUIR_MAS_DE_DIAS`. El umbral de aviso (90) es provisional.

---

## Compras: la bandeja del comprador (2026-09-25)

El rol `compras` aterrizaba en una pantalla con Velocity, Dock Lock, Cuarentena
y Audit: nada de lo que decide una compra. Reposición, Contenedor, Temporada y
Acuerdos vivían solo en el admin, y la temporada le daba 403. Reposición no
decía cuánto pedir, ni a quién, ni a qué precio; pintaba el stock bruto
mientras decidía por la posición; el contenedor decía «Sin déficit China» cuando
no podía calcular.

### Una pantalla, dos entradas

`#tab-compras` es **un** bloque: el admin lo ve en su panel y el rol `compras`
en `#pantalla-compras` — `cmpMontar` mueve el nodo (dos copias de los mismos
ids serían dos pantallas que divergen; `#tab-compras-ancla` marca dónde
vuelve si en la misma sesión entra un admin). `compras_bandeja.js` (`cmpTab`):

| Pestaña | Qué contesta | Endpoint |
|---|---|---|
| Franja (fija) | ¿Puedo decidir con estos números? Ventas (kardex) al día, existencias por sede con su antigüedad, OCs sincronizadas hace N, origen y fichas, supuestos (lead time nacional, ciclo) | `GET /api/compras/bandeja/confianza` |
| 🛒 Bandeja | Por SKU nacional bajo su punto de pedido (o por cruzarlo en 7 días): **cuánto pedir** (a empaque y MOQ), **a quién**, **a qué precio**, valor, urgencia, y **¿por qué?** con la aritmética en palabras. Agrupada por proveedor con subtotal, «Copiar OC» y «Exportar CSV» | `GET /api/compras/bandeja` |
| 🚢 Contenedor | Sin origen o sin fichas lo dice **en grande** con cuántos SKU y el enlace a 🧾 Fuentes, y no pinta barras ni ETA. Con datos: por proveedor chino, nombre, unidades, cajas, m³, US$ por línea, alerta si llega con la temporada empezada con la fecha límite, «Exportar packing list» | `GET /api/compras/bandeja/contenedor?tipo=` |
| 🎒 Temporada | tener − hay − viene = pedir (del backend), fecha límite por origen, conciliada con el contenedor (**manda la temporada**: el contenedor muestra el ajuste, no una segunda cifra). El instrumento del comité (lista paralela, escenarios, acta) plegado debajo | `GET /api/compras/bandeja/temporada` |
| 📦 Lo pedido | OCs abiertas por proveedor, atrasadas primero con sus días; lo que llegó (recepciones confirmadas, 30 días); deriva de precios contra los acuerdos (3/6/12 meses). Una sección que falla se declara: sin 500 | `GET /api/compras/bandeja/lo-pedido` |
| 🧾 Fuentes | Sin cambios | `/api/compras/fuentes/*` |
| ⚙️ Avanzado | Modelos (S-B/TSB), tablas técnicas del punto de pedido y del contenedor, acuerdos, bloqueos, **«Reposición interna PICKING»** (la Velocity: mide PICKING de NB1, no compras), recepciones con problema, cuarentena, rastro | los de siempre |

Permisos: todo `_es_compras()` (admin, jefe de almacén, gerente, compras). La
lectura de temporada (`/api/kardex/temporada/pedido`, `GET /juicios`) pasó a
`_es_compras()`; **escribir la lista paralela sigue en admin/jefe**
(`_es_admin_o_jefe`, no existe un rol «líder de compras»), y la pantalla la
muestra sin campo a quien no puede escribirla (`TEMP_ROLES_REGISTRAN_JUICIO`,
cruzada por test contra `Roles.ALMACEN`).

### La bandeja compone; no calcula

`app/services/compras_bandeja.py` **no tiene un solo operador aritmético** ni
consulta la base. Lo que faltaba se escribió en su dueño:

| Faltaba | Dónde quedó |
|---|---|
| Hasta dónde pedir en nacional (el punto de pedido decía cuándo, no cuánto) | `armador_service.nivel_objetivo(d, σ, LT, σ_LT, R, z)` — **una función para China (R = 90) y nacional (R = el ciclo de compra)**. El S de China la usa |
| El ciclo de compra nacional | `armador_service.ciclo_pedido_nacional()`: `ROP_CICLO_NACIONAL_DIAS` (1–90), default **7, supuesto declarado**; ilegible → default y lo dice |
| Déficit, disponible, días hasta el punto de pedido, «se agota antes de que llegue», cuándo llegaría | columnas nuevas de cada fila nacional de `rop_dual` |
| Redondeo a empaque con MOQ | `armador_service.pedido_en_empaques` (usa `cajas_a_pedir`: MOQ como mínimo) |
| El empaque de compra | `compras_fuentes.empaque_de_compra`: OC más reciente (unidad y factor) → catálogo de Siesa → por unidad. **El MOQ nacional no está en ninguna fuente**: 1, declarado («confirmalo al pedir») |
| Proveedor con NIT | `compras_fuentes.proveedores_info` (maestro, o la OC más reciente) |
| OCs abiertas con atraso; llegadas | `compras_fuentes.ocs_abiertas`, `llegadas_recientes` |
| Valor y subtotal sin inventar $0 | `costo_service.valorizar` / `sumar_valores` (sin costo → `None`, cota inferior) |
| Régimen China | `armador_service.insumo_origen()` (salió de `rop_dual`) |
| Temporada que viene, fecha límite, conciliación | `temporada_service.proxima_temporada`, `fechas_limite_pedido` (inicio − (LT + σ)), `conciliar_con_contenedor` |
| Nombre y proveedor en el contenedor | cada línea de `armar_contenedor`; `composicion_por_proveedor` agrupa |

Urgencia (comparaciones sobre lo que el motor ya dio): **URGENTE** = aun
pidiendo hoy se agota antes de que llegue (posición < venta diaria × lead time);
**ESTA SEMANA** = bajo el punto de pedido; **PRÓXIMAS** = lo cruza en
`DIAS_PROXIMAS` (7) días. No se propone: bloqueados
(`BloqueoRecompraService.verificar_oc`, la que ya existía «antes de generar la
OC»), SKU sin ninguna fila de existencias (Regla 0), sin venta reciente, y los
de China (van por el contenedor) — todo declarado en «Lo que la bandeja no
propone». Sin kardex: `SIN_KARDEX` con qué falta y cómo encenderlo.

**El borrador de OC** (copiar o CSV con `;`) lleva NIT, proveedor, CO y bodega
destino (el CDI, `co_de_bodega`), referencia, descripción, unidad de compra,
cantidad en empaques, unidades base, precio, valor, fecha de entrega sugerida
(hoy + lead time) y urgencia. **El formato de importación a Siesa no está
especificado: es un borrador a confirmar con el consultor**, y lo dice.

### Lenguaje

Punto de pedido, disponible de verdad, reserva de seguridad, cantidad a tener,
«le faltan datos de agotados»; unidades enteras; `fmtPesos` y `fmtUsd`
(«US$ 4.291,20», en `util.js`); nombre del producto siempre junto a la
referencia. La jerga (ROP, σ, TSB, S-B, CENSURADA) queda en ⚙️ Avanzado. La
bandeja va en tarjetas (sin tablas: se lee en el teléfono); las pestañas se
envuelven, nunca scroll horizontal.

De paso en `compras_ia.js`: el «$» suelto del colchón, el FOB en dólares con su
signo, `esc()` en la evidencia de días sin stock, «nunca se agotó» cuando la
consulta llegó al tope (ahora «no se agotó en los días que alcanzó a traer»), el
mensaje de TSB que culpaba al kardex sin mirarlo, y la deriva con nombre y el
«de más» en pesos. En `temporada.js`: la etiqueta de temporada la da el
servidor, se fue el texto vencido del «export del 1 de agosto», y la lista
paralela viaja por posición (no la referencia en el `onchange`).

**Analítica → «Venta perdida por agotados»**: el porqué trae los 5 productos que
más se dejaron de vender con «Ver en la Bandeja de compras ›» (solo si el rol ve
Compras); abre la Bandeja en su fila con el porqué desplegado, o dice por qué no
está (`GET /api/compras/bandeja/sku`: bloqueado, China, sin ventas, sobre su
punto de pedido y en cuántos días lo cruza).

### Tests y mutaciones

`tests/test_compras_bandeja.py` (55: mundo con kardex + reconstructor real,
`stock_siesa`, espejo de OCs; urgente, esta semana, próximas, ya pedido cubre,
bloqueado, sin kardex, sin costo, empaque/MOQ, proveedor por OC, sin
existencias; contenedor sin origen/sin fichas/con propuesta; fecha límite;
conciliación; lo pedido con atraso y con una sección que revienta; permisos:
compras/admin/jefe/gerente entran, conductor/operario/supervisor/tienda no,
compras no escribe juicios; **trinquete AST «la bandeja no calcula»** con
meta-tests de las formas que ve y lo sano que no, y piso) y
`tests/test_compras_bandeja_js.py` (23: Node con `util.js` real contra
respuestas reales — sin códigos crudos, sin undefined/NaN/null, sin hex, nada
< 12 px, `onclick` solo con posiciones, datos escapados, cada número con su
contexto; borrador de OC y CSV; el enlace desde Analítica). **26 mutaciones, las
26 rojas** (con `-B` y `PYTHONDONTWRITEBYTECODE`, cada reemplazo verificado a
aplicar una vez).

Tests que cambiaron y por qué: `test_frontend_integrity::TestOrganizacionPorDecision`
y `TestReposicionNacional` describían la barra OPERACIÓN/DECISIÓN; ahora exigen
las pestañas nuevas y lo técnico dentro de Avanzado.
`test_permisos_por_pantalla`: `compras_bandeja.js` y `temporada.js` los abre
también `compras`.

Suite completa (2026-09-25, `-m "not postgres"`, TZ=UTC, una corrida): **8769
passed, 1 failed** (5 skipped, 19 xfailed): `test_regimen_china` exige por AST
que el cruce por `MARCAS_CHINA` itere contra una variable `marca`, y la
`insumo_origen` nueva lo hacía en línea. Reescrita con la variable; ese archivo
y los de compras, verdes por separado (108).

### Lo que NO cubre, dicho

- **El formato de importación de OCs a Siesa** no existe en el repo: el CSV es
  un borrador (Regla 1: no hay DOCX del conector).
- **El MOQ nacional** no tiene fuente; **el empaque del catálogo** de Siesa puede
  no ser el del proveedor (se dice).
- **El precio es por SKU, no por proveedor**: si el precio sale de un acuerdo de
  otro proveedor que el habitual, se muestra igual (la fuente se dice).
- **Un SKU que se compra a varios proveedores** va con el de su OC más reciente.
- **La bandeja llama a `rop_dual` completo** en cada carga; la temporada además
  arma el contenedor para conciliar. No se midió contra el volumen de producción.
- **«Hay» es de hoy** (temporada) y la fecha límite es un supuesto (LT + una σ).
- La lista de sedes de la franja incluye AV1/TRA1 (la de `analitica_salud`).
- Los paneles viejos de Dock/Cuarentena/Audit conservan ramas `comp2` muertas
  (no se tocaron para no mezclar).

### Decisiones para el dueño

1. **Ciclo de compra nacional**: 7 días supuesto (`ROP_CICLO_NACIONAL_DIAS`).
   ¿Se le compra a cada proveedor nacional cada semana? Cambia la cantidad a
   pedir (no el cuándo).
2. **¿Quién escribe la lista paralela de temporada?** Hoy admin y jefe de
   almacén. ¿Un «líder de compras»? Haría falta el rol.
3. **Formato de la OC para Siesa**: pedir al consultor el plano/conector de
   importación de órdenes de compra.
4. **Fecha límite de temporada**: inicio − (lead time + una σ). ¿Más margen
   para China?
5. **Meta de servicio 95 %** para toda la bandeja (sigue abierta la de la
   canasta constitucional).

---

## Inventario, traslados, cartera, crons y alertas — auditoría del 2026-09-25

La clase que atraviesa casi todo: **«no pude preguntar» se leía como «no
existe» o «ya está hecho»**, y **un cron muerto o una alerta callada no se
veían**. Migración **`m048inv`** (down `m047comprasvivo`; tablas nuevas
`sello_ambiente` y `cron_latido`, aditiva). Locks nuevos: 2040
(`LOCK_FLOTA_REPORTE_SEMANAL`), 2041 (`LOCK_REFRESCO_EXISTENCIAS`).

| | Qué pasaba | Ahora | Trinquete |
|---|---|---|---|
| **P0-6** traslados | `get_sts_info_by_alterno` / `get_consec_entrada_transito_by_alterno` devolvían `None` en el `except`; reintentar-despacho/recepción lo leían «no existe» y posteaban otro STS/ETS | Tres respuestas: dato / `None` (Siesa contestó que no) / `RecuperacionNoDisponible` (red, circuito abierto, sin alterno, consecutivo ilegible). Los botones responden 409 sin POST ante «no sé»; un traslado DIRECTA (173066) no se reenvía (la consulta del STS no lo ve). El DLQ ya no borra `siesa_error` si el STS salió sin consecutivo | `test_recuperacion_tres_estados.py`: todo `get_*` del gateway que traga una excepción amplia está declarado (11, solo encoge) |
| **P0-7** picking | `reabrir_picking` ponía `cantidad_recogida` en 0 y el nuevo pick restaba todo otra vez | Con empaque vivo/remisión (`_empaque_que_la_explica`, compartida con «devuelto al estante»): la original queda COMPLETADO por lo recogido y nace una tarea por el faltante. Sin empaque: lo recogido se reingresa (`REINGRESO_REAPERTURA`) | `test_inventario_no_se_resta_sin_destino.py::TestReabrirNoRestaDosVeces` |
| **P0-8** carga física | La carga de las 7:00 escribía `UbicacionProducto` con la foto vieja de `stock_siesa` cuando Siesa no respondía; una página perdida por 429 dejaba la pasada «completa» | **Decisión del dueño:** sigue automática, pero solo escribe con dato fresco y completo (`fuente_para_escribir`: no degradado, sello de hoy Bogotá, la bodega vino en la descarga, sin páginas perdidas). Si no, no escribe nada, queda en `registros_sync`, se ve en 🩺 Salud (`carga_fisica`) y en el resumen diario; se reintenta con `POST /api/siesa/cargar-inventario` dentro de la ventana (409 fuera) | `test_carga_fisica_fuente_confiable.py`: fuente degradada → cero escrituras; AST: todo escritor de inventario que lee Siesa pasa por la política |
| **P0-9** ambiente | Una copia de producción restaurada en QA: la DLQ de QA ejecutaba sus `siesa_jobs` contra el Siesa compartido | `sello_ambiente`: el primer proceso que va a hablar con Siesa sobre una base sin sello la sella con su `RAILWAY_ENVIRONMENT_NAME`. Otro ambiente no postea: la DLQ omite el ciclo sin tocar un job y `_post` levanta `AmbienteNoCoincide` antes de la red. Sin variable (local/tests) no bloquea, declarado; sello ilegible bloquea. `/api/health/siesa` → `sello_ambiente`; `POST /api/health/sello-ambiente` re-sella (admin, motivo, nombre escrito, bitácora). Acta de corte: sello PROTEGIDO; `verificar_restauracion`: IRRECUPERABLE y avisa el sello de la copia | `test_sello_ambiente.py`: todo `requests.post` declarado; el único a Siesa llama a la pared antes de la red |
| **P0-10** contenedor | `armar_contenedor` no propagaba `insumo_en_camino.hay_dato`: sin OCs sincronizadas el faltante salía inflado con cara de bueno | `aptitud_de_la_propuesta` (una política): NO APTA sin dato, con una fuente que falló o sin el espejo de OCs completo. `compras_bandeja.js` y `compras_ia.js` lo dicen en grande | `test_contenedor_no_apto.py`: nadie más lee `hay_dato` |
| **P1-1** cupo | Una FE PAGADA (fuera de la cartera abierta) consumía cupo para siempre; un valor desconocido sumaba 0 | Consume solo sin FE o con FE < 48 h (`VENTANA_FE_SIN_INDEXAR`; sin fecha, consume). Valor desconocido o parcial → `pedidos_wms_sin_valor` y `VALOR_DESCONOCIDO` (retiene) | `test_cartera_cupo_y_autorizadores.py` |
| **P1-12** cartera | RETIENE sin nadie que decida | `puede_autorizar` (una política para la ruta y la salud: permiso por persona o el rol de cartera que se cree). `salud()` cuenta autorizadores y avisa «NADIE puede decidir» (sin autorizador ni token) o «solo el Gestor» | ídem |
| **P1-11** crons | `SCHEDULERS_ACTIVOS` en memoria: la web no veía los crons del worker; un cron que reventaba seguía «activo» | Los 28 `add_job` pasan su función por `con_latido(<id>, fn)`: cada corrida escribe `cron_latido` (cron × servicio, conexión propia, nunca rompe el cron). Health y 🩺 Salud leen el latido (`fallando`, `callados`, `alertas_por_correo` = algún cron de alertas corrió en 26 h) | `test_cron_latido.py`: todo `add_job` envuelto con su mismo id |
| **P1-14** resumen diario | Importaba `PedidoPicking` (no existe) → «N/D» cada día; ventana naive corrida 5 h; reescribía VTA-30 sin corte | Despachos por `metricas.pedidos_despachados` en el día Bogotá; la auditoría por `auditoria.auditar` (con corte; los hallazgos publican `fecha`) | `test_resumen_diario.py`: todo `from app…/flota… import X` resuelve (un import roto se escondía en un `except`) |
| **Alertas sin canal** | Retenido por cartera > 3 días, traslado en tránsito > 24 h, BLOQUEA de la auditoría, carga física no escrita, cron fallando/callado, sello, existencias sin refrescar > 24 h: ningún correo | `alertas_service.avisos_sin_canal` → líneas del resumen diario (el correo). Una fuente que revienta se declara en su línea | ídem |
| **P2** ventana | Cuatro ventanas (7–20, 6–20, 7–19:30, 7–21); crons contra Siesa a las 2:00/2:30/3:00/5:55 o 24/7; RC de las 20:30 amanecía FALLIDO | **Superado por la tanda 2 · H (2026-09-25): la ventana sale de `SIESA_VENTANA`, los crons de madrugada volvieron a su hora y los pedidos corren todo el día.** Lo que decía: `ventana_siesa` (**06:00–19:30**): 15 crons envueltos en `solo_en_ventana_siesa`; barcodes 06:05, empaques 06:20, ubicaciones 06:35, prewarm pre-turno 06:00, Vigía lunes 06:30, pedidos 6–19 h; el refresco de existencias (hilo propio) la mira; el DLQ fuera de ella solo procesa `ALERTA_EMAIL` | `test_ventana_siesa.py`: todo `add_job` clasificado (habla → envuelto; no habla → con su porqué); nadie más declara una ventana. La suite fija la ventana abierta (`_RELOJ_FIJO`) |
| **P2** frescura | Vigía: MAX global de `stock_siesa.updated_at` (una bodega refrescada hacía ver fresco todo) | `frescura_stock_siesa`: una bodega = su último refresco; un conjunto = su bodega menos reciente (el Armador aplica la misma regla por SKU) | AST: nadie más lee `StockSiesa.updated_at` |
| **P2** cola | La Salud contaba como «cola atascada» un despacho retenido por cartera | `cola_siesa` lo cuenta aparte (`en_espera_de_cartera`, `cartera_service.tareas_retenidas`) | `test_ventana_siesa.py` |
| **P2** vars | `vars_criticas` declaraba `DC` (el código usa `NI`); faltaban variables | `NI`; `CONNEKTA_ID_SISTEMA`, `CONNEKTA_IKEY` (condicional), `SIESA_NIT_EMPRESA`, `RESEND_API_KEY`, `ALERTA_EMAIL_DEST`; `MODO_ENSAYO=true` en production es combinación peligrosa | `test_vars_criticas.py` |
| **P3** | Web y worker con `HEAVY_SCHEDULERS` duplicaban el reporte semanal y el refresco; `post_fork` de `wsgi.py` nunca corría | Lock 2040 + latido (`corrio_bien_desde`) para el reporte; lock 2041 para el refresco; `post_fork` en `gunicorn.conf.py` (Gunicorn 25 lo lee solo) | `test_procesos_duplicados.py` |
| **QA** almacenes | `/api/almacenes/` sin NS2, FP1, FF1 | **Dato, no filtro**: el endpoint lista almacenes activos y esas bodegas nunca tuvieron uno (se crean en el primer uso). `bodegas.almacenes_faltantes()`; `flask asegurar-almacenes [--ejecutar]` los crea con la función del primer uso (CO del maestro); uno INACTIVO no se reactiva solo | `test_almacenes_operados.py` |

**e2e del 2026-09-25 (tres xfail quitados):** la bitácora nombra «<persona>
(Gestor de Cartera)» (`analitica_salud.actor_externo`, también en «Por
persona»); «Packing completado hoy» y la productividad del empacador cuentan
la caja cerrada en cualquier estado posterior (`filtro_caja_cerrada_desde`);
la portada no juzga contra la meta un período del KPI diario sin medir
(`semaforo(sin_medir=True)` → gris «Sin juicio…»).

### Lo que NO cubre, dicho

- **Sello, transición**: una copia de producción tomada ANTES de que
  producción corra esta versión no trae sello, y el primer proceso de QA que
  la use la sella `QA`. Producción tiene que desplegar esto (y correr un ciclo
  de DLQ) antes del próximo respaldo para QA. Un proceso sin
  `RAILWAY_ENVIRONMENT_NAME` postea sobre cualquier base.
- **El latido dice que corrió, no que hizo su trabajo**: un cron que atrapa
  sus propios errores figura «bien». Los hilos que no son APScheduler (carga
  de las 7:00, refresco de existencias) no dejan latido: su rastro es
  `registros_sync`.
- **La carga física** que se salta una bodega por operaciones activas solo lo
  loguea. `tamPag=1000` de la consulta de existencias (`_descargar_una_pasada_custom`)
  sigue violando la Regla 10 (no se tocó: cambiarlo cambia el costo de la
  descarga y no era el defecto).
- **Consumo de cupo**: la hora de emisión de la FE es `fecha_despachado` (o
  `siesa_triggered_at`); no hay una columna propia.
- **Ventana**: un POST inline (un usuario a las 20:00) no se frena; Siesa caído
  o de noche = se para todo (decisión del dueño, sin contingencia). La
  generación de conteo ABC sigue a las 2:00 (no habla con Siesa, declarada).
- **La recuperación de la RIT** (`get_consec_rit_by_referencia`) sigue
  tragando el «no sé» (declarada: con `TRASLADO_USA_RIT` apagada no decide
  nada). `get_remision_desde_pedido` también (P0-2, frente de RM).
- **El pedido de temporada** sin dato de «en camino» sigue proponiendo `pedir`
  sin marcarlo NO APTO (solo el contenedor).

### Decisiones para el dueño

1. ~~**Ventana 06:00–19:30.** ¿Confirma?~~ **Decidido (2026-09-25):** la
   ventana no es una regla de producción; sale de `SIESA_VENTANA` (tanda 2 · H).
2. **Re-sellar**: el endpoint existe sin botón a propósito. ¿Quién decide qué
   se hace con los `siesa_jobs` PENDIENTE de una copia antes de re-sellarla?
3. **Almacenes de NS2/FP1/FF1**: ¿se crean (`flask asegurar-almacenes
   --ejecutar`) o se dejan hasta su primer uso?
4. **Consumo de cupo**: ventana de 48 h para una FE no indexada (medido: de
   instantáneo a minutos). ¿Más corta?

---

## La plata del conductor hasta Siesa (2026-09-25)

Frente «dinero» de la auditoría del 2026-09-25: ningún recibo duplicado,
ningún documento por plata no cobrada, ninguna ruta sin salida, ningún número
que diga «llegó» sin confirmarlo. Política en **`app/services/politica_cobro.py`**
(una función por pregunta) y **`app/services/permisos_liquidacion.py`** (una
función de permiso por operación). Sin migración.

| | Qué pasaba | Ahora | Trinquete |
|---|---|---|---|
| **P0-4** | Tras un POST de RC que fallaba, «¿entró?» era «¿saldo ≤ $0,5?». Un RC de contado con retención sale neto y el DC va después: el recibo que SÍ entró dejaba el saldo de la retención → «no entró» → segundo RC (RC-00002744). Y se revertía el pre-flag ante cualquier excepción que no fuera un timeout (un 502, una conexión cortada) | `ConnektaNoEnviado` (4xx, `codigo != 0`, 429, circuito abierto, payload inválido) es **la única** prueba de que no entró. Ante «no sé», el RC compara el saldo de antes del POST (guardado en el job, `saldo_antes_rc`) con el de después: si bajó por el monto, entró; si no, FALLIDO sin reintento, `SIN_VERIFICAR`, y **una persona lo resuelve** (Liquidación → «Sí/No está en Siesa», `POST …/recaudos/<id>/resolver-rc`, FORZAR). Mismo criterio de reversión en DC y en las dos NC. La bandera puesta sin desenlace ya no es «idempotente» | `test_rc_verificado_por_documento.py`: ningún `except` baja un pre-flag sin `ConnektaNoEnviado` (inventario: `_ejecutar_con_preflag`, frente de inventario) |
| **P0-5** | «Liquidar ruta» encolaba la NI con solo mirar `motivo_descuento`, sin leer si la oficina confirmó o rechazó la retención | `decision_retencion` / `exigir_retencion_aplicable`: pendiente → ni RC ni DC; rechazada → esa NI no sale. Un solo encolador, `_encolar_retencion`; el ejecutor la revalida antes del POST | `test_politica_cobro.py`: solo `_encolar_retencion` encola `DOCUMENTO_CONTABLE_RET` y llama a la política |
| **P1-7** | PARCIAL con retención: `monto_cobrado` ya es neto y «Registrar cobro» restaba la retención otra vez; la base de retención incluía lo devuelto | `monto_rc` para las dos puertas; `base_retencion_entregada` (la factura menos lo declarado de vuelta, por `f470_rowid`; sin devolución amarrada: `None`, el DC no sale y se declara). La vista previa lee `rc_resta_retencion` del servidor | toda llamada a `_encolar_recibo_caja` pasa un monto asignado de `monto_rc` |
| **P1-3** | Monto/estado de la parada editables hasta que el DLQ hacía el POST | `puede_editar_cobro`: congelado al **encolar** RC o retención (monto, descuento, estado, forma de pago, decisión de retención). El RC lleva `instantanea` y el ejecutor no postea si la parada cambió | toda escritura de `monto_cobrado`/`estado_entrega` tiene `puede_editar_cobro` **en la condición de un `if`** |
| **P1-4** | La cola sin señal mandaba el cierre aunque una parada fallara; después, confirmar y forzar exigían EN_TRANSITO y liquidar todas gestionadas: sin salida | `entregar_ruta` declara `paradas_sin_gestionar`; la cola no cierra una ruta con una parada suya pendiente; **parada tardía** sobre ENTREGADA no liquidada (oficina con `motivo_tardia`; la primera confirmación de la cola entra sola) → FORZAR `parada_confirmada_despues_del_cierre`; forzar cierre sirve sobre ENTREGADA | `test_parada_tardia.py` |
| **P1-5** | `forzar_cierre_ruta` llamaba `_marcar_liquidada` saltándose `credito_no_autorizado` y `devoluciones_sin_contar` | Deja la ruta ENTREGADA | solo `RutaService.liquidar_ruta` llama `_marcar_liquidada` |
| **P1-6** | DC esperando un RC FALLIDO/DESCARTADO para siempre; RC esperando una NC que ya salió; `verificacion_imposible` COMPLETADO contado como llegado | DC sin RC vivo → falla declarado; el RC reconstruye el puente (`devolucion_ruta.nc_ya_salio` / `puentear_nc_al_recaudo`, la única que lo escribe); `politica_cobro.rc_llegaron` (señal positiva) en VTA-60, reconciliación, fuga y pantallas | **VTA-63**, **VTA-64** (BLOQUEA, detector ciego en `test_cadena_nc_rc_dc_con_salida.py`) |
| **P1-8** | Registrar cobro (encola RC/DC) pedía admin-o-jefe; «Enviar a Siesa», admin. Reintentar un RC FALLIDO, supervisión. `/liquidar-completo` mandaba el DC sin cuenta ni UN (Bug 3) | `puede_liquidar` en todo lo que encola plata (y en reintentar un job de la liquidación: `puede_reintentar_job`). `/liquidar-completo` borrado. `facturar-rm-manual` exige el documento repetido + motivo (FORZAR); `/despacho_parcial/<id>/despachar` toma el lock de la DLQ | `test_permiso_compuesto.py` **descubre por AST** toda ruta que llega a un encolador de plata (antes, lista a mano) |
| **P1-10** | DLQ 24/7: un RC de las 20:30 amanecía FALLIDO | `dlq_puede_postear` → `ventana_siesa.ventana_abierta` (en simulación no aplica; **desde la tanda 2 · H solo frena si `SIESA_VENTANA` está configurada**). El frente traía su propia `fecha.en_ventana_siesa` (7:00–20:00): se retiró al integrar | `test_dlq_ventana_siesa.py`, `test_ventana_siesa.py` |
| **P1-13** | `faltantes_de_retorno_de_recaudos` filtraba `'CONFIRMADA'`: FALTANTE_TOTAL invisible en Liquidación y Fugas | `EstadoDevolucionCliente.CONTADAS`. Liquidación dice «Faltante de retorno… No habrá nota crédito» (`devolucion_pendiente.sin_nc`) | ninguna comparación del estado de una devolución contra el literal (`test_devolucion_contada_una_definicion.py`, inventario de 3) |
| **P2** | `F357_FECHA_RECAUDO`/`F358_FECHA_CONSIGNACION` con el día del envío | el día Bogotá de `fecha_confirmacion` (`fecha_bogota_de`); `F350_FECHA` sigue siendo hoy. **Precisado en la tanda 2 · C**: F357 solo si es el mismo mes | `test_rc_verificado_por_documento.py`, `test_fecha_rc.py` |
| **P2** | Faltaba la fuga «Cobrado sin recibo en Siesa»; la fuga `rechazos_ruta` se llamaba igual que el KPI y medía otra cosa | `cobrado_sin_recibo` (en «Plata en riesgo»; los FALLIDO quedan en «Documentos trabados»); la fuga pasa a `devoluciones_ruta` | `test_fuga_cobrado_sin_recibo.py` |
| Pantalla | Rutas atrasadas invisibles en Liquidación; «Enviar a Siesa» visible siempre sobre una FALTANTE_TOTAL; «RC ✓» con la bandera de pre-envío | `rutas_atrasadas` (de `rezago_liquidacion`); `pendiente_siesa` de la política; `rc_llego`/`rc_sin_verificar`; `permisos` en el detalle (Registrar cobro solo a quien liquida) | `test_liquidacion_pantalla_dinero.py` (Node, `util.js` real) |

Además (e2e del 2026-09-25): un **reenvío idéntico** de la cola ya no es
«editó la entrega» (compara por valor —35700.00 = 35700.0— y la hora del
teléfono sola no es edición; se conserva la de la primera confirmación); el
desglose rotula la **condición declarada** con la del PEDIDO y agrega
`condicion_de_cobro` (la de la factura si ya salió, con su fuente), y escribe
«PD1004», no «PD-1004».

**Permisos (decisión del dueño):** los roles «líder de cartera» y
«liquidador» existen desde el 2026-09-25 — ver «Roles de la plata» (la matriz
vive en `permisos_liquidacion`, una línea por operación). **Cambio de
comportamiento:** el jefe de almacén ya no registra cobros ni reintenta
RC/DC/NC de la liquidación, ni confirma retenciones ni corrige cobros: sigue
viendo la liquidación.

**35 mutaciones, las 35 rojas** (con `-B` y `PYTHONDONTWRITEBYTECODE`, cada
reemplazo verificado a aplicar una vez; dos sobrevivían al primer intento y se
reforzaron los tests: la guarda en la condición y el aviso de faltante por
texto positivo).

### Lo que NO cubre, dicho

- ~~**`_ejecutar_con_preflag`** sigue revirtiendo ante un 5xx~~ — cerrado en la
  tanda 2 (2026-09-25): solo `ConnektaNoEnviado` lo baja; el inventario del
  trinquete quedó vacío.
- **El «entró» por saldo** exige que la fila de cartera exista antes y después
  del POST; con la cartera todavía sin indexar la FE (ver «RESUELTO
  2026-09-04») no hay prueba y el RC queda para una persona.
- ~~**Una parada tardía de la oficina** no tiene formulario propio~~ — cerrado en
  la tanda 2 · B: formulario en Liquidación (quien liquida).
- **El grafo del trinquete de permisos es por nombre** y no ve guards
  condicionales (el reintento se prueba por comportamiento).
- **La base de retención de una PARCIAL** usa lo declarado por el conductor
  por línea; un producto de doble unidad declarado por referencia no se reparte
  hasta que bodega lo cuente.

### Decisiones para el dueño

1. ~~¿El jefe vuelve a registrar cobros? ¿Confirmar retención pasa al líder
   de cartera?~~ Decidido el 2026-09-25: ver «Roles de la plata».
3. ~~Formulario de parada tardía para la oficina~~ — decidido y hecho (tanda 2 · B).

---

## Pantallas de la operación diaria: lo que cada rol ve y toca (2026-09-25)

Frente «operación diaria por rol» + «QA real con flota@/victor@» + cinco puntos
del e2e del día completo. Voz nueva en **usted** (decisión del dueño); el
barrido de voz del resto va aparte. Cada fila, con su clase y su trinquete.

| | Qué pasaba | Ahora | Trinquete |
|---|---|---|---|
| Picker «Reportar problema» | Los cuatro botones mandaban `cantidad_encontrada: 0`; con 3 escaneadas, «encontró 0» | `picking_service.cantidad_encontrada_declarada` (las dos rutas): sin cantidad → 400 (salvo «Ubicación vacía», que la declara); la pantalla la **pide** con lo escaneado de valor inicial | `test_picking_reportar_problema_declarado.py`: ningún `.get('<cant…>', 0)` nuevo en `app/routes` (AST, inventario de 10) |
| Conductor sin señal | Con señal débil el envío moría y la entrega se perdía («Error de conexión»); un rechazo quedaba en la cola para siempre; el cierre de ruta salía antes que las confirmaciones | `_condEnviarUno` (contrato de la cola de flota: hecho · rechazado · sin red · reintentar); lo que no tuvo respuesta se guarda; un rechazo sale y queda anotado (`#cond-rechazos`) hasta «Entendido»; el cierre no sale con confirmaciones pendientes o rechazadas (sin señal, se encola detrás y la sincronización lo retiene). `/entregar` es idempotente; un reenvío idéntico no es un EDITAR | `test_cola_conductor_rutas_js.py` (Node, IndexedDB en memoria) |
| Packing | «Siesa procesó la factura» cuando solo se encoló; retenido por cartera como «Error Siesa»; con Siesa caído, «reintentando» sin decir por qué | `CierreResult.estado` (`RETENIDO_CARTERA` 409 · `SIESA_NO_DISPONIBLE` 503: «Siesa no está disponible: no se puede facturar»), `estado_siesa` `CONFIRMADO`/`EN_COLA`; `empMensajeCierre` una función para cierre, reintento, cola y cartera; pestaña RETENIDO POR CARTERA; `/api/mobile/sync` marca `definitivo` y la cola offline lo saca | `test_packing_cierre_dice_lo_que_paso.py`: ninguna cadena del PWA afirma «Siesa procesó» |
| Fechas | `new Date().toISOString()` en Rutas (después de las 7 p. m. era mañana) y cinco copias de «el día de Bogotá» | `hoyBogota()` en util.js | `test_hoy_bogota_una_sola.py` (sin toISOString recortado a día, sin `en-CA` fuera de util.js, sin `…HoyBogota`) |
| Avisos y diálogos | `alerta()` borraba todo a los 2,5 s; ~60 `confirm()`/`prompt()` nativos | El error se queda hasta «Cerrar» (pila, sin duplicados, tope 3); 29 decisiones de plata/inventario al modal propio | `test_dialogos_propios.py`: inventario por (archivo, función) de los 25 nativos que quedan, solo encoge |
| Permisos | `_es_personal_almacen` y `_ROLES_SIN_ALMACEN` eran listas negras: `control_flota` veía `precio_compra` y pasaba `/api/mobile/conteo/*` | `Roles.PERSONAL_ALMACEN` (lista blanca; un rol nuevo = una línea) | `test_permisos_lista_blanca.py`: por rol (control_flota, conductor, tienda), las escrituras que atraviesan la puerta, medidas sobre el url_map; AST sin listas negras {conductor, tienda} |
| `GET /api/rutas/<id>/paradas` | Hacía commit y tardaba ~7 s en frío | Lectura pura (`anotar=False` hasta el fondo; `cond_pago.clasificar_tarea`); la escritura es `POST …/paradas/anotar` (`anotar_paradas`), que la pantalla dispara aparte; Siesa en paralelo, ítems precargados | `test_lista_paradas_no_escribe.py`: cero INSERT/UPDATE/DELETE en el GET; AST: ningún GET hace commit (inventario: `picking.siguiente_tarea`) |
| Fotos de flota | 410 con la fila en `ok` | Relectura con hash antes de `ok`; `FLOTA_FOTOS_DIR` absoluta; 410 `nunca_se_guardo`/`archivo_ausente` y 503 `almacen_sin_configurar`; `estado_verificable` en toda lista; `almacen_fotos` en el health | `tests/flota/test_foto_verificable.py` |
| Tablero y ficha | Una lectura con la foto del tablero sin guardar «tenía foto»; «Ficha completa» en la ficha y «incompleta» en la bandeja | El gancho mira el estado de la foto; `FichaTecnica.faltantes()` es la única definición (incluye el tanque) | ídem |

**Del e2e (2026-09-25):** login sin importar mayúsculas (`normalizar_email`,
`Usuario.por_email`; trinquete `test_correo_una_forma.py`); códigos crudos en
«Llegó el camión», el recorrido y el bloque de cartera; «Mi camión hoy» con
la ruta del día cerrada ya no ofrece recibir (`ruta_de_hoy_cerrada`); la
jornada ya no dice «a menos de 0 m»; los plurales «DEVOLUCIÓNES»,
«confirmaciónes», «ubicaciónes»; y la bandeja de compras le dice «pídale a
administración» a quien no puede tocar variables. Liquidación: una ruta
entregada y sin liquidar de otro día aparece en «Por liquidar».

### Las fotos 410 de QA — qué verificar en Railway (no se pudo mirar)

El código ya no puede decir `ok` sin un archivo releído en el proceso que
escribe. Lo que queda es de infraestructura, y `GET /flota/health` →
`almacen_fotos` lo contesta desde el servicio que sirve:

1. `configurada` y `absoluta` en `true` y `raiz` = `/data/flota-fotos`
   **en el servicio web** (el que atiende `/flota/*`). Si el worker tiene la
   variable y el web no, el web contesta 503 `almacen_sin_configurar`.
2. `mismo_disco_que_el_contenedor` en `false`. Si da `true`, la carpeta NO está
   en el volumen: lo escrito se pierde en cada despliegue — exactamente «ok en
   la base, 410 después». Revisar el *mount path* del volumen (`/data`) y que
   sea el mismo servicio.
3. Un solo reemplazo (réplica) del web: un volumen de Railway se monta en una
   réplica; con dos, una escribe y la otra no tiene el archivo.
4. `fotos_ok_sin_archivo` / `ejemplos_sin_archivo`: las fotos ya perdidas (las
   de antes de montar el volumen, o las de una base copiada de otro ambiente).
   No vuelven; sus lecturas deberían re-verificarse con foto nueva.

### Lo que NO cubre

- ~~**Flota** sigue con 18 `confirm()`/`prompt()` nativos~~ Cerrado el
  2026-09-25: el modal vive en `modal.js` y el inventario de nativos está en cero.
- El inventario por rol mide con ids inexistentes: un endpoint que contesta 404
  por el id **antes** de mirar el rol no se puede medir así.
- El GET de paradas ya no anota: si la pantalla no llega a llamar
  `…/paradas/anotar` (sin señal), la guarda de `confirmar_parada` juzga con lo
  anotado al crear el packing y al despachar (supuesto = se cobra, Regla 0).
- ~~`tienda.js` busca en `/api/productos/`, que a la tienda le da 403~~
  Cerrado el 2026-09-25: la tienda ve el catálogo sin costo (ver «Roles de la
  plata… y las decisiones de pantallas»).
- `/api/almacenes/` sin NS2, FP1, FF1 en QA: son filas de la tabla, no código.
- `/api/cartera/salud` 503 sin `CARTERA_GESTOR_TOKEN`: nace cerrado a propósito.

---

## Integración de los frentes del 2026-09-25 (fiscal, inventario, dinero, pantallas)

Tres frentes arreglaron en paralelo y dos inventaron la misma idea con otro
nombre. Una política, una función (Regla 0): esto es lo que quedó.

### Una jerarquía de excepciones del POST (`connekta_gateway`)

```
ConnektaNoEnviado              «no entró» — el ÚNICO permiso para revertir un pre-flag
├── ConnektaRechazado          Siesa/Connekta dijo que no: 4xx, 429, codigo≠0
├── ConnektaPayloadInvalido    no se pudo armar (también ValueError): nunca salió
└── ConnektaCircuitOpenError   circuito abierto: nunca salió (el DLQ no gasta intento)
(ConnectTimeout levanta la base: la conexión no se abrió)

ConnektaResultadoDesconocido   «no sé»: ReadTimeout. RemisionNoIdentificada es hija
Exception genérica             5xx, conexión cortada, JSON ilegible: «no sé» también
```

Para fiscal, `ConnektaNoEnviado` era solo el `ConnectTimeout` y el «no» se
llamaba `ConnektaRechazoExplicito`; para dinero era la base de toda prueba de
que no entró. Las dos clases homónimas convivieron en el módulo al juntar los
frentes, y en Python **la segunda definición gana sin avisar**: los `except`
de liquidación habrían dejado de atrapar el 4xx. `ConnektaRechazoExplicito`
se renombró a `ConnektaRechazado` en todos sus usos (sin alias). Trinquete:
`test_documento_fiscal.py::TestUnaJerarquiaDeExcepcionesDelPost` (quién es
«no entró», quién no, y ningún nombre definido dos veces en el gateway — por
AST, con su detector probado).

### Una ventana de Siesa: `ventana_siesa` (desde la tanda 2 · H, de `SIESA_VENTANA`; sin ella, sin restricción)

La de inventario (con su trinquete «nadie más declara una»). Fiscal traía
06:00–20:00 semiabierta (leía la de cartera y comparaba por su cuenta);
dinero, `fecha.VENTANA_SIESA` 07:00–20:00 con `en_ventana_siesa`. El mismo
recibo podía salir de la DLQ a las 19:45 y la misma caja no poder cerrarse.

| Quién pregunta | Cómo |
|---|---|
| Crons que hablan con Siesa | `solo_en_ventana_siesa` |
| DLQ | `siesa_job_service.dlq_puede_postear` → `ventana_abierta`. **En simulación no aplica** (no hay Siesa); **en ensayo sí** (los GET son reales y de noche gastan reintentos igual — dinero la eximía también en ensayo; se integró sin eso). Fuera: solo `TIPOS_SIN_SIESA` (= `TIPOS_SIN_DOCUMENTO`, una lista) |
| Cierre de caja / emisión fiscal | `documento_fiscal.siesa_disponible_para_facturar` → `ventana_abierta(ahora)` |
| Vista previa de la liquidación | `ventana_abierta()` |
| 🩺 Salud | `analitica_salud.tiempo_operativo` pregunta a `ventana_siesa.ventana()`; sin ventana, tiempo de reloj |

`ventana_abierta` acepta instantes conscientes de zona (los juzga en Bogotá).
**Decidido el 2026-09-25 (tanda 2 · H):** la ventana sale de `SIESA_VENTANA`;
sin ella no hay restricción. Trinquete: `test_ventana_siesa.py`.

### Lo que convive sin chocar

- **Idempotencia**: el pre-flag del 142945 (`rm_enviada_at`, fiscal) y «la
  bandera sola no es idempotente» del RC (dinero) son la misma regla: la
  bandera es de pre-envío y solo `ConnektaNoEnviado` la baja.
- **Permisos**: `permisos_liquidacion` (dinero, una función por operación),
  `cartera_service.puede_autorizar` (inventario) — preguntas distintas.
- **Migraciones**: `m048fiscal` → `m048inv` (una cabeza). Dinero no trae.
- **Locks**: fiscal 2030–2034, inventario 2040–2041; dinero no registró.
- `/despacho_parcial/<id>/despachar` pregunta primero si Siesa está disponible
  para facturar (fiscal, 503) y después toma el lock de la DLQ (dinero, 409).
- **Permisos**, también: `Roles.PERSONAL_ALMACEN` (pantallas, lista blanca de
  quién opera almacén) no se cruza con los de liquidación ni con cartera. Los
  roles «liquidador» y «líder de cartera» no están en ella.

### Lo que dos frentes arreglaron a la vez (quedó una implementación)

| Qué | Quedó | Salió |
|---|---|---|
| Cola del conductor: el cierre no sale antes que sus paradas | La de pantallas (`_condEnviarUno`: hecho · rechazado · sin red · reintentar; rechazos anotados) + el aviso de `paradas_sin_gestionar` de dinero | La cola de dinero en `rutas.js`. Su test (`test_parada_tardia`) se reescribió para las dos salidas: 4xx = rechazo anotado, 5xx = se queda |
| Reenvío idéntico de una parada no es EDITAR | La de dinero (`_VOLATILES_PARADA`, restituye todo lo volátil; listas sin orden) | `_CAMPOS_DEL_ENVIO` de pantallas (el commit bbc31858 quedó vacío y se saltó) |
| «Por liquidar» de otros días | `rutas_atrasadas` de dinero (política `rezago_liquidacion`, no suma a los totales del rango) | El predicado extra en la consulta del rango (pantallas); su test exige ahora `rutas_atrasadas` |
| «Siesa no está disponible» en el cierre de caja | Un texto: `documento_fiscal.MENSAJE_SIESA_NO_DISPONIBLE`. Las dos negativas (compuerta fiscal y precheck) llevan `estado=SIESA_NO_DISPONIBLE` → 503, la cola offline reintenta | El texto propio de pantallas |
| Facturar RM manual | Modal propio (pantallas) + motivo y documento repetido (dinero) | — |

---

## Roles de la plata (liquidador, líder de cartera) y las decisiones de pantallas (2026-09-25)

**Qué pasaba.** La plata de la ruta solo se podía dar entera: liquidar,
registrar un cobro o autorizar un crédito obligaba a hacer **admin** a alguien
de oficina (usuarios, maestros, inventario, Siesa), y el jefe de almacén
—que opera bodega— confirmaba retenciones y corregía cobros. La clase: *un
permiso que solo se puede dar entero*.

**Ahora** — dos roles (`Roles.LIQUIDADOR = 'liquidador'`,
`Roles.LIDER_CARTERA = 'lider_cartera'`), creables desde Usuarios, **fuera de
`PERSONAL_ALMACEN`, `GESTION` y `SUPERVISION`**. La matriz del dueño, una línea
por operación en `app/services/permisos_liquidacion.py`:

| Operación | Quién |
|---|---|
| Liquidar, «Enviar a Siesa», registrar cobro, reintentar RC/DC/NC, resolver un RC sin verificar | admin + liquidador |
| Parada tardía desde la oficina (ruta ya cerrada: `puede_registrar_parada_tardia`) | admin + liquidador |
| Confirmar retención, corregir cobro (y, con la ruta en tránsito, registrar la parada por el conductor) | admin + líder de cartera |
| Autorizar crédito no autorizado (una parada, o en lote: `/api/cartera/panel/credito-lote`) | admin + líder de cartera |
| Autorizar una retención de cartera | líder de cartera por su rol; la casilla `puede_autorizar_cartera` **solo en el admin** (`Roles.CARTERA_POR_ROL`, `CARTERA_CON_CASILLA = (ADMIN,)` desde el 2026-09-26: listas blancas); el iniciador nunca |
| Forzar el cierre de una ruta | admin |
| Ver Liquidación, desglose, reconciliación, planilla, sus envíos a Siesa | admin + jefe + **gerente** (solo lectura, 2026-09-26) + liquidador + líder de cartera. El supervisor no |

- `/api/reposicion/siesa-jobs`: supervisión ve todo; quien ve la liquidación
  ve **solo** sus envíos (`puede_ver_jobs`), y cada job trae `puede_reintentar`.
- **Aterrizaje** (`app.js` `_TABS_DE_ROL`, que también absorbió a
  `control_flota`): liquidador → 💰 Liquidación; líder de cartera → **⛔
  Cartera** (pestaña nueva, el mismo bloque del tablero: `cartera.js` pinta
  `cartera-bloque` y `cartera-bloque-tab`) + Liquidación. La pestaña ⛔ Cartera
  solo la ve el líder.
- **Liquidación ofrece a cada rol solo lo suyo** (`_liqPermiso`, sobre los
  `permisos` que el detalle calcula con las mismas funciones del 403): el
  liquidador no ve «confirmar retención» ni «autorizar crédito», el líder no ve
  «Liquidar» ni «Registrar cobro», y corrige el monto desde la tarjeta de la
  parada (`liqCorregirMontoParada`). Reintentar un envío: `liqReintentarJob`.

**Decisiones de pantallas (CTO, conservadoras):**

| | Qué pasaba | Ahora |
|---|---|---|
| Tienda y catálogo | 403 en `/api/productos/`; y la búsqueda de la tienda y de las bonificaciones de recepción mandaba `?search=` (el servidor no lo lee): recibía el catálogo entero y **registraba el primer producto**. `&limit=8` tampoco se leía | `Roles.CATALOGO` (almacén + tienda); el costo de compra solo a `Roles.VEN_COSTO_DE_COMPRA` (gestión + compras), una forma: `productos._producto_para` — **el operario deja de recibir `precio_compra`**. `?codigo=` exacto (WMS, barras, Siesa, empaque) y `productoPorCodigoExacto` (uno, o el aviso de ninguno/varios) |
| `picking.siguiente_tarea` | GET que asigna (escribe) | POST; el inventario de GET que escriben queda vacío. La pantalla usa `/api/mobile/tarea-actual`: no cambió |
| Diálogos nativos | 18 en flota + 5 más, porque el modal vivía en `app.js` y los arneses de flota no lo cargan | `modal.js` (capa base, `util → modal → app`); **inventario en cero**. No va en `util.js`: los arneses guionan el modal y `util.js` se carga de verdad en todos. «Cancelar» ahora cancela en las notas opcionales (antes seguía con vacío) |

**Trinquetes:** `tests/test_roles_plata.py` (matriz a mano por función; por
HTTP con **ids reales** —el inventario de `test_permisos_lista_blanca` usa ids
inexistentes y no ve un 404 posterior al rol—; aterrizaje en Node con `app.js`
real; la tarjeta de Liquidación por rol), `test_catalogo_sin_costo.py` (por
rol, búsqueda exacta, AST: ningún `to_dict()` del catálogo fuera de la
política, y **ningún parámetro que el PWA mande al catálogo sin que el
servidor lo lea**), `test_dialogos_propios.py` (vacío, piso, modal definido
solo en `modal.js`), `test_permisos_lista_blanca.py` (inventario por rol de los
dos roles nuevos; las listas negras declaradas quedaron en cero),
`test_permisos_por_pantalla.py`. `test_todo_endpoint_verifica_rol` reconoce toda
función `puede_*` de `permisos_liquidacion` como guard. **30 mutaciones, las 30
rojas** (una sobrevivía —la tienda volviendo a `?search=`— y obligó al trinquete
de parámetros).

**Lo que NO cubre, dicho:**
- **Sin migración ni CHECK de roles**: `usuarios.rol` sigue siendo texto libre;
  `_ROLES_VALIDOS` y el desplegable (cruzados por test) son la puerta. Un CHECK
  exige saber qué roles hay en producción (no se miró).
- `/api/mobile/tarea-actual` también es un GET que asigna (escribe en el
  servicio, no en la ruta): el trinquete de GET mira solo commits directos en
  la ruta. Misma clase, declarada.
- Otros endpoints que devuelven un producto entero a personal de almacén
  (layout, stock, conteo) no pasan por `_producto_para`.
- La matriz no tiene a «supervisor» ni «gerente» en ninguna escritura de la
  plata; el gerente solo **ve** Liquidación (2026-09-26).
- El formulario de parada tardía es de otro frente; acá solo su permiso.

**Decididas el 2026-09-26** (ver «Voz usted y tres ajustes de roles»): la
casilla de cartera vale **solo en el admin**; forzar el cierre de una ruta
sigue **solo admin** (también en la pantalla); el **gerente ve** Liquidación en
solo lectura y el supervisor no.

---

## Tanda 2 del 2026-09-25 — parada de oficina, fecha del RC, retención PARCIAL, candado local, pre-flag, ventana

Decisiones del dueño ejecutadas en el frente de dinero. Migración
**`m049tardia`** (down `m048inv`, aditiva, nullable, sin backfill: columnas en
`recaudos_entrega`). Sin locks nuevos.

| | Qué pasaba | Ahora | Trinquete |
|---|---|---|---|
| **B** parada tardía | Una ruta cerrada con una parada sin gestionar solo salía con «cerrar lo que falta» (todo rechazado) o esperando la cola del conductor | Liquidación muestra **Paradas sin gestionar** (cliente, valor, hace cuánto, referencias) con el texto guía (primero el conductor con señal) y el botón de liquidar bloqueado. Quien liquida (`puede_liquidar`) la registra con el formulario: resultado, pago y comprobante, lo devuelto, **motivo obligatorio**, foto. Mismo endpoint (`motivo_tardia`) y **las mismas validaciones del servicio** (contado/crédito, comprobante, evidencia de «se quedó»; la oficina no tiene GPS: se registra «sin dato»). FORZAR `parada_confirmada_despues_del_cierre` con `registrada_por_oficina`, y la parada marcada. Lo que el teléfono mande después **no pisa** (ni la ruta ni el servicio): `version_conductor` + `diferencia_conductor` → señal en Liquidación y evento `parada_oficina` en la jornada. > 24 h sin gestionar → resumen diario. Política: `app/services/parada_tardia.py` | `test_parada_de_oficina.py` |
| **C** fecha del RC | `F357` = día del cobro aunque el mes hubiera cambiado | `politica_cobro.fechas_del_recibo` (una función): `F350` = día del envío; `F357` = día del cobro **si es el mismo mes**, si no `F350` y el día real al frente de `F350_NOTAS`; `F358_FECHA_CONSIGNACION` = **siempre** el día del cobro. El recibo queda marcado (`rc_cobro_otro_mes`) y `rezago_liquidacion.recibos_de_otro_mes` lo lista en el diagnóstico, el desglose y el correo de rutas sin liquidar | `test_fecha_rc.py` (AST: ninguna escritura de las dos claves fuera de `fechas_rc[...]`) |
| **D** retención PARCIAL | La NI de una PARCIAL espera el conteo de la devolución sin que se viera | Se queda así (decisión). `devolucion_ruta.retencion_esperando_conteo`: señal `retencion_espera_conteo` en Liquidación y, pasadas 24 h, línea en el resumen diario (`avisos()['retencion_parcial_sin_contar_24h']`) | `test_parada_de_oficina.py::TestLaRetencion…` |
| **E** candado local | Un script local con el `DATABASE_URL` de producción arrancó los crons contra producción | Sin `RAILWAY_ENVIRONMENT_NAME` y con base `*.rlwy.net`/`*.railway.internal`: `create_app` no registra **ningún** scheduler y `disparar_dlq_inmediato` no lanza su hilo; log CRITICAL y `SCHEDULERS_OMITIDOS`. `app/utils/candado_local.py` | `test_candado_produccion_local.py` (AST: todo `_registrar_scheduler` cuelga del `else` del candado) |
| **Pre-flag** | `_ejecutar_con_preflag` (ENTRADA_OC, TRASLADO_AVERIAS, AJUSTE_CONTEO) revertía ante un 5xx; el traslado a averías por movimiento no tenía pre-flag | Solo `ConnektaNoEnviado` baja la bandera; lo demás es «no sé» → FALLIDO sin reintento. El movimiento de avería gana `siesa_sync = ENVIANDO`. Salida humana: `preflag_sin_verificar` decide; «Reintentar» (uno, en lote, el de conteo) se niega o lo salta; Siesa → Recuperación ofrece «¿Está en Siesa?» (`POST /api/siesa/jobs/<id>/resolver-sin-verificar`, admin, motivo, FORZAR `envio_sin_verificar_resuelto_a_mano`). Los `ValueError` previos al POST de ajustes/compras son `ConnektaPayloadInvalido`; `AmbienteNoCoincide` es `ConnektaNoEnviado` | `test_rc_verificado_por_documento.py::TestSoloUnaPruebaBajaLaBandera` (inventario **vacío**, ve también `siesa_sync`), `test_preflag_no_se.py` |
| **H** ventana | La ventana 06:00–19:30 (medida en QA) frenaba la facturación y los crons en producción | `SIESA_VENTANA` (`HH:MM-HH:MM`, cruza la medianoche si hace falta). **Sin la variable: 24 h.** Ilegible: sin restricción y declarado (`/api/health/siesa` → `ventana_siesa`). Crons de madrugada a su hora (barcodes 2:00, empaques 2:30, ubicaciones 3:00, prewarm 5:55, Vigía lunes 5:30), envueltos: con ventana la respetan. **Pedidos cada minuto todo el día** (antes 6–19 h). La salud mide tiempo de reloj sin ventana. El cierre de caja con Siesa caído se sigue negando sin ventana (circuito, precheck) | `test_ventana_siesa.py` (nadie más lee la variable ni declara una ventana), `test_documento_fiscal.py::TestElCierreSinSiesaSeNiegaLimpio` |

**Cambios de comportamiento:** el jefe de almacén ya no registra paradas
tardías (es de quien liquida); un «Reintentar» sobre un envío sin verificar
ahora se niega (antes lo cerraba como hecho sin preguntar); los tests que
fijaban 06:00–19:30 piden la ventana con el fixture `ventana_qa` y la suite
corre sin la variable (`conftest` la borra).

### Lo que NO cubre, dicho

- **La fecha del RC no está probada contra Siesa real** (abajo, la prueba).
- **ENTRADA_OC resuelta «sí está en Siesa»** se cierra por la guarda
  idempotente y **no encola el traslado a averías** de esa recepción: queda a
  mano.
- **Un crash entre el pre-flag y el POST** (job PROCESANDO reseteado) sigue
  leyéndose como «ya enviado» en los tres tipos: la guarda por bandera sola no
  se cambió (solo el «no sé» del POST).
- **El formulario de la oficina**: la foto es opcional salvo donde el servicio
  la exige; una parada registrada sin evidencia queda solo con la señal
  «registrada por la oficina».
- **Con `SIESA_VENTANA` puesta en QA**, los crons de madrugada se **omiten**
  (quedan fuera de la ventana): en QA no corren.
- **El candado** no reconoce una base de producción fuera de Railway, ni frena
  un proceso local que se ponga `RAILWAY_ENVIRONMENT_NAME` a mano.

### La prueba de la fecha del RC en Siesa QA (fin de mes; la hace el dueño o el consultor)

1. **El último día del mes, antes de las 7 p. m.**: en QA, una ruta con una
   parada de contado confirmada por el conductor con **TRANSFERENCIA** (con
   referencia) y otra en **EFECTIVO**. No liquidar.
2. **Sin postear, el mismo día** (con `MODO_ENSAYO=true`):
   `connekta.trigger_recibo_caja(..., fecha_recaudo='AAAAMM<último día>')`
   devuelve el payload: `F357 = F358 = día del cobro`, `F350` = hoy.
3. **El día 1 o 2 del mes siguiente**: liquidar la ruta y registrar el cobro de
   las dos paradas. Antes de que el DLQ postee, revisar en ensayo el payload:
   `F350_FECHA` = hoy, `F357_FECHA_RECAUDO` = hoy, `F350_NOTAS` empieza con
   «Cobrado por el conductor el DD/MM/AAAA…», y en la transferencia
   `F358_FECHA_CONSIGNACION` = el último día del mes anterior.
4. **Postear** (sin ensayo) y en Siesa QA → Tesorería → el RC: fecha del
   documento y de recaudo = día del envío; en Caja, la fecha de consignación
   del mes anterior; las notas con el día real. `API_v2_CxC_General`: la
   factura quedó saldada. En el WMS, `rc_cobro_otro_mes` puesto y la ruta en
   «recibos fechados en otro mes».
5. **Control**: una parada cobrada y enviada el mismo mes lleva `F357` = día
   del cobro.
6. **Si Siesa rechaza** `F358_FECHA_CONSIGNACION` de un período cerrado, o
   `F357 < F350`: anotar el mensaje exacto acá; la corrección es una línea en
   `fechas_del_recibo` (p. ej. `F358 = F350` también, con el día en las notas).

### Decisiones para el dueño

1. `SIESA_VENTANA` en QA (`06:00-19:30`) apaga allá los crons de madrugada.
   ¿Se deja así o QA va sin ventana?
2. El sync de pedidos pasó a correr todo el día: ¿alguna hora en que no deba
   preguntarle a Siesa producción?
3. ¿La evidencia (foto) del formulario de la oficina es obligatoria siempre?

---

## Voz usted y tres ajustes de roles (2026-09-25/26)

**Decisión del dueño: la aplicación le habla al usuario de USTED.** La clase:
*un texto que ve una persona, escrito en voseo o en tuteo*. Estaba en los
mensajes del servidor («Revisá en Siesa», «No puedes cerrar…», «Esta tarea no
te pertenece», «cobrá al entregar»), en los correos, en la PWA y en `flota/`.

**Ahora, en `app/` (Python): cero.** Se pasaron a usted los ~180 textos de
rutas, servicios, excepciones que viajan a la pantalla, correos y alertas
(«Revise», «tiene», «su punto de venta», «pídalo», «Cuente qué pasó», «ya
contó»). Donde el sujeto no era el usuario se reescribió impersonal («se
contaron 90 y se enviaron 5»). Los tests que citaban el texto exacto se
actualizaron; los de la PWA que citan su propio texto se dejaron para el
frente de la PWA.

**Trinquete: `tests/test_voz_usted.py`.** Detector por vocabulario generado
desde raíces verbales (imperativo `Revisá`, presente `tenés`, clítico
`Pedile`, pretérito `contaste`, subjuntivo `que lo sumes`, «vaciálo»; tuteo:
`tu/te/ti`, `puedes`, clíticos acentuados `búscalo`, e imperativo desnudo
`Revisa`/`Escanea` **solo al empezar cláusula**). Texto visible: Python por AST
(`app/` y `flota/`, sin docstrings, sin `logger.*`/`print`), JS de la PWA con
un lexer propio (sin comentarios ni regex; `'a' + 'b'` es una frase) e
`index.html` (texto, `title`/`placeholder`/`aria-label`/`alt`, JS en línea).
Lo citado entre «» no cuenta (el botón «No lo encontré» habla el operario).
Meta-tests de lo que ve y de lo sano que no marca (`está`, `acá`, `Bogotá`,
`Pídale`, `Hágalo`, `los cierres forzados`, `vuelve a la cola`, `RECONTAR_TU`,
`tu_rol`), pisos (≥ 250 `.py`, ≥ 30 `.js`, miles de textos) y un lexer que se
queja si pierde el hilo (un archivo ilegible no puede leerse como «limpio»).
**14 mutaciones, las 14 rojas** (una primera variante —quitar `tenés` del
conjunto explícito— no quitaba nada porque la forma también se genera; se
reemplazó por `podés`).

**`PENDIENTE_OTRO_FRENTE`** — la PWA y `flota/` las pasan a usted otros dos
agentes en paralelo; quedaron declarados con su número de hallazgos (hoy 413
en 38 archivos). **Solo encoge, en los dos sentidos:** un archivo que sube se
pone rojo; uno que baja también, pidiendo actualizar su número o sacarlo. El
integrador la vacía al juntar los tres frentes; el objetivo es `{}`.

**Lo que NO ve:** voseo en MAYÚSCULAS sostenidas (se saltan como siglas), un
imperativo tú en medio de la frase («…y cierra la caja»), un verbo cuya raíz
no está en la lista (se agrega cuando aparece), texto armado letra a letra, y
lo que vive en la base (motivos escritos por personas).

### Tres ajustes de roles (decisión del dueño)

| | Antes | Ahora | Trinquete |
|---|---|---|---|
| Casilla `puede_autorizar_cartera` | Valía en todo `GESTION` (admin, supervisor, jefe, gerente) | **Solo admin** (`Roles.CARTERA_CON_CASILLA = (ADMIN,)`). El líder de cartera sigue por su rol. La salud no cuenta a gestión con la casilla | `test_roles_plata.py::TestAjustesDeRoles20260926` (HTTP 403 por rol, el panel sin botones de decidir, la salud) |
| Forzar el cierre de una ruta | Servidor: solo admin (verificado). **La pantalla** ofrecía «⚡ Forzar cierre» y «Cerrar las paradas que faltan» a todo el que veía Rutas/la planilla → 403 | `rutas.js`: `RUTA_ROLES_FUERZAN_CIERRE = ['admin']` + `puedeForzarCierreRuta()` en los dos botones | la lista del JS = `puede_forzar_cierre_ruta`; la tarjeta pintada en Node por rol; todo botón de forzar bajo su guarda |
| Liquidación | admin, jefe, liquidador, líder de cartera | **+ gerente, solo lectura** (`puede_ver_liquidacion`). No está en ninguna función de escritura: el detalle le manda todos los `permisos` en falso y la tarjeta no le pinta botones; ve los envíos de la liquidación sin poder reintentarlos. **El supervisor no** (pestaña oculta y 403) | la matriz a mano, lecturas por HTTP, la tarjeta por rol, el aterrizaje |

`liquidacion.js` no necesitó cambio: sus botones ya salían de `permisos`. El
texto de la casilla en el formulario de usuarios (`app.js`) ahora dice «Vale
solo para admin». Los cambios del PWA van en un commit aparte (el frente de la
PWA toca los mismos archivos).

**7 + 1 mutaciones de roles, todas rojas** (una —la tarjeta sin guarda—
sobrevivía a un conteo de texto y obligó al test en Node).

# WMS-PAME — Referencia Central

## Stack

- **Backend**: Flask + SQLAlchemy + PostgreSQL + Gunicorn (Railway). **Dos
  paquetes, no uno**: `app/` (127 archivos) y `flota/` (29) — ver abajo
- **Frontend**: PWA vanilla JS modularizada (app.js + 16 módulos)
- **Integración ERP**: Connekta V2/V3 → Siesa Enterprise
- **DLQ**: SiesaJob con reintentos + backoff exponencial (5→15→45 min, max 3)
- **Tests**: pytest (612 passing), CI en Railway buildCommand

## Arquitectura JS (Frontend)

```
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
compras_ia.js     (394)         Acuerdos marco, Armador, deriva, inteligencia inventario
flota.js        (1,839)         Custodia de vehículos, ficha, documentos, avisos
kardex.js         (446)         Motor kardex
temporada.js      (365)         Temporada escolar
```

Orden de carga: app → picking → packing → recepcion → rutas → traslados → conteo → reposicion → liquidacion → layout → tienda → etiquetas → vigia → compras_ia → kardex → temporada → flota. Todas las funciones son globales. Cross-module calls son runtime (onclick), nunca parse-time.

La lista autoritativa del orden real es el `SHELL` de `app/static/pwa/sw.js` —
es la que el service worker cachea. Si esta tabla y ese arreglo divergen, el
arreglo gana.

### ⚠️ `flota/` vive FUERA de `app/`

Paquete propio en la raíz, con arquitectura hexagonal (`dominio/`,
`adaptadores/`, `api/`, `puertos.py`) — distinta del resto del repo, que es
`routes/` + `services/` + `models/`.

**Un `grep` acotado a `app/` no lo ve.** Sus 17 endpoints están registrados
(`/flota/*`) y funcionan; buscarlos en `app/routes/` da cero resultados y la
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
| `FLOTA_FOTOS_DIR` | Almacén de fotos de custodia |
| `GUPSHUP_API_KEY` · `GUPSHUP_SOURCE` · `GUPSHUP_APP_NAME` · `GUPSHUP_TEMPLATE_IDS` | Canal WhatsApp. **`GUPSHUP_SOURCE` es la línea de mensajería cuya habilitación a producción depende de un tercero** — la misma que BK-OPS-01 §4.3 lista bajo Gestor de Cartera. Un número, dos consumidores |

### Dispatchers fuera de su módulo

Dos sub-navegaciones viven en un archivo distinto al de la lógica que invocan.
Buscar aquí antes de darlos por inexistentes:

| Dispatcher | Definido en | Invoca lógica de |
|-----------|-------------|------------------|
| `compSubtab()` | `recepcion.js:1352` | `compras_ia.js` (acuerdos, armador, deriva) |
| `invSubtab()` | `conteo.js:94` | `compras_ia.js` (inteligencia inventario) |

---

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
| API_v2_Inventarios_InvFecha | `get_inventario_fecha()`, `get_stock_bodega()` | Stock actual por bodega |
| API_v2_Items | `get_items_catalogo()` | Catálogo productos |
| API_v2_ItemsBarras | `get_item_por_barras()` | Barcode → SKU |
| API_v2_ItemsUnidadesMedida | `get_items_unidades_medida()` | Factores de conversión empaque |
| API_v2_Ubicaciones | `get_ubicaciones_siesa()` | Ubicaciones por bodega |
| API_v2_Ventas_Facturas_DesdePedido | `get_factura_desde_pedido()`, `get_rowids_factura()` | Anti-duplicado FE, rowids para NCE, base gravable |
| API_v2_CxC_General | `get_cxc_general()` | Cuentas por cobrar (f253_id para cruce) |
| papeleriamedellin_WMS_Vendedor_Contacto | `get_vendedor_contacto()` | Nombre + teléfono real del asesor (JOIN T210×T200×T015), para mostrárselo al conductor en pago parcial |

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
| `SIESA_TIPO_DOCTO_FACTURA` | `FEW` | FE | 142943 |
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
| `SIESA_COND_PAGO_VENTAS` | `''` | **El código de CONTADO (C01).** No se emite — se configura para reconocerlo y NO emitirlo. Una FE de contado no se aprueba sin recaudo |
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

#### Lo que sigue repartido (y por qué se deja)

Las **listas** —no el mapa de CO— siguen en varios sitios, y el trinquete las
vigila entre sí: `traslado_service._BODEGAS_PREWARM` ·
`inventario_siesa_service._BODEGAS_PV` · cinco mapas de **nombres** en el JS
(`traslados.js`, `tienda.js`, `app.js` ×3, una inline en un `onchange`).

No mueven inventario ni deciden un CO: un desacuerdo ahí muestra un código
crudo donde debería ir un nombre. Unificarlas es refactor cosmético sobre
código que funciona, y hacerlo antes de un corte es «validar contra producción
real» al revés.

Trinquete: `tests/test_bodegas_coherentes.py` (18 tests). El detector de copias
está probado por mutación — reintroducir un mapa lo pone rojo.

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
14. **Siesa no opera después de ~8 PM Colombia** — TCP timeout de 30s
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

Por eso `GET /api/health/siesa` publica `schedulers.activos` y
`schedulers.alertas_por_correo`: **lo que arrancó de verdad en ese proceso**, no
lo que la variable dice. Son cosas distintas — un import que revienta deja la
variable en `true` y el cron sin correr. Se consulta por servicio.

---

## Vigía — CUSUM de corrimientos operativos

Detecta desplomes en series semanales (facturación, líneas, frecuencia de
servicio por C.O.). `vigia_service.py`, panel en `vigia.js`.

### Cómo se alimentan las series

| Vía | Qué alimenta | Estado |
|-----|--------------|--------|
| `cargar_ventas_desde_txt()` | Línea base histórica (26 semanas de μ_ref/σ_ref) | Backfill admin, bloqueado salvo `VIGIA_CARGAR_TXT=true` |
| `alimentar_adopcion_picking()` | `adopcion_picking`, `brecha_picking` | Cron lunes 05:30 Bogotá + botón en el panel |
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
| DESPACHO_F470 | 244328→142945→142943 (`DespachoParialService`) | `tarea.siesa_triggered` | — |
| ENTRADA_OC | 142948 | `recepcion.siesa_triggered` | — |
| AJUSTE_CONTEO | 142951 | `sesion.siesa_triggered` | — |
| TRASLADO_AVERIAS | 142951 | `tarea_dev.siesa_triggered` | — |
| DESPACHO_TRASLADO | 174930/173076 | `solicitud.siesa_salida_consec` | — |
| NOTA_CREDITO_FACTURA | 251126 (unificado con el job de abajo desde `07cb5df`, ver "NCE — qué conector usar") | `recaudo.siesa_nc_triggered` (pre-flag) | 1ro |
| NOTA_CREDITO_DEVOLUCION_CLIENTE | 251126 | `devolucion.siesa_nc_triggered` (pre-flag) | — (bridge marca `recaudo.siesa_nc_triggered` si viene de ruta) |
| RECIBO_CAJA | 142888 | `recaudo.siesa_rc_triggered` (pre-flag) | 2do (espera NC) |
| DOCUMENTO_CONTABLE_RET | 142882 | `recaudo.siesa_dc_triggered` (pre-flag) | 3ro (espera RC) |
| MOTIVO_DIAN_NC | 251546 | `devolucion.siesa_motivo_dian` | tras NOTA_CREDITO_DEVOLUCION_CLIENTE |
| ALERTA_EMAIL | Resend API | N/A | — |

### Backoff

Intento 1: próximo ciclo DLQ (~5 min). Intento 2: +15 min. Intento 3: +45 min. Después: FALLIDO + alerta admin en dashboard.

### Pre-flag Pattern (previene duplicados en crash)

```python
# ANTES del POST:
recaudo.siesa_rc_triggered = True
db.session.commit()

try:
    resultado = connekta.trigger_recibo_caja(...)
except Exception:
    # POST falló — revertir para permitir reintento
    recaudo.siesa_rc_triggered = False
    db.session.commit()
    raise

# Si modo ensayo, revertir (no se creó nada en Siesa)
if resultado.get('modo_ensayo'):
    recaudo.siesa_rc_triggered = False
    db.session.commit()
```

---

## Testing

```bash
# ⚠️ ANTES DE PUSHEAR ALGO CON FECHAS — el reloj del CI no es el tuyo
#
# Railway corre en UTC. Entre las 7 p.m. y la medianoche de Bogotá, allá ya es
# el día siguiente: un test que arma fechas con `date.today()` y las compara
# contra código que usa `dia_operativo()` **pasa local y rompe el deploy**.
# Cinco horas al día, de un solo lado, y reintentar «lo arregla».
#
# Rompió el build 52c0e4de (2026-08-13 20:41). Ver `tests/conftest.py::hoy_operativo`.
TZ=UTC venv/bin/python -m pytest tests/ -q -m "not postgres"

# Suite completa
venv/bin/python -m pytest tests/ -v --tb=short

# Solo formatos (rápido, sin DB)
venv/bin/python -m pytest tests/test_siesa_formatos.py -v

# Solo contracts (rápido, sin DB)
venv/bin/python -m pytest tests/test_siesa_contracts.py -v

# Con DB (integration)
venv/bin/python -m pytest tests/test_siesa_dlq.py tests/test_liquidacion.py tests/test_siesa_guards.py -v
```

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

Conectores con spec verificado contra código (julio 2026):

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

### Sin resolver: 401 persiste incluso con nombre y permiso correctos — aceptado como no-bloqueante, no se sigue persiguiendo

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

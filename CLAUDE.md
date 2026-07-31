# WMS-PAME — Referencia Central

## Stack

- **Backend**: Flask + SQLAlchemy + PostgreSQL + Gunicorn (Railway)
- **Frontend**: PWA vanilla JS modularizada (app.js + 13 módulos)
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
```

Orden de carga: app → picking → packing → recepcion → rutas → traslados → conteo → reposicion → liquidacion → layout → tienda → etiquetas → vigia → compras_ia. Todas las funciones son globales. Cross-module calls son runtime (onclick), nunca parse-time.

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
| 142946 | NotaFactura (NCE) | Nota crédito por devolución | Liquidación: PARCIAL/RECHAZADO | NOTA_CREDITO_FACTURA | `trigger_nota_factura()` |
| 142888 | ReciboCaja (RC) | Registro de cobro del conductor | Liquidación: CONTADO | RECIBO_CAJA | `trigger_recibo_caja()` |
| 142882 | DocumentoContable (DC) | Retenciones tributarias | Liquidación: con retención | DOCUMENTO_CONTABLE_RET | `trigger_documento_contable()` |

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
| `SIESA_TIPO_DOCTO_DOCTO_CONTABLE` | `DC` | 30 | 142882 |
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
| `SIESA_COND_PAGO_VENTAS` | `''` | Condición de pago fallback (C01 = contado) |

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
5. **Fechas = YYYYMMDD sin separadores** — timezone Bogotá (UTC-5), no UTC. Después de 7PM Colombia, UTC es el día siguiente
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
21. **142946 (NotaFactura) SIEMPRE con `F350_IND_ESTADO=0` (Elaboración), NUNCA 1 (Aprobado)** —
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

## Procedimiento Manual — Aprobar NC de Devolución de Cliente en Siesa

Consecuencia operativa de la Regla #21: el WMS crea la Nota Crédito (142946)
en **Elaboración**, nunca Aprobada. Alguien en contabilidad debe completar el
cruce y la aprobación a mano en el escritorio de Siesa. Verificado en vivo
contra Siesa QA (2026-07-29) con NCE-00000050 / factura FEW-1463 (Samboni
Benavides Aldivar), de punta a punta:

> **Nota (2026-07-31):** el paso 2 (cruzar cartera) ya tiene solución de API
> verificada — ver "Cruce de cartera SÍ se pudo automatizar — conector
> 251126" más abajo. Pendiente de integrar a código; hasta entonces este
> procedimiento de 3 pasos sigue siendo el vigente en producción.

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

Actualiza automáticamente el paso 2 del "Procedimiento Manual" de abajo:
cruzar cartera ya no es manual para Devolución de Cliente. Motivo DIAN y
Aprobar (pasos 3-4) siguen manuales — ver siguiente sección.
- Correr contra 2-3 casos más (facturas con descuentos, con más de 2 líneas)
  antes de reemplazar 250696 en producción — solo se probó un caso simple.

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

## Vigía — CUSUM de corrimientos operativos

Detecta desplomes en series semanales (facturación, líneas, frecuencia de
servicio por C.O.). `vigia_service.py`, panel en `vigia.js`.

### Cómo se alimentan las series

| Vía | Qué alimenta | Estado |
|-----|--------------|--------|
| `cargar_ventas_desde_txt()` | Línea base histórica (26 semanas de μ_ref/σ_ref) | Backfill admin, bloqueado salvo `VIGIA_CARGAR_TXT=true` |
| `alimentar_adopcion_picking()` | `adopcion_picking`, `brecha_picking` | Cron lunes 05:30 Bogotá + botón en el panel |
| Ingesta Connekta | Facturación, líneas, frecuencia | **No implementada** |
| Generic Transfer | Planillas de ruta | **No implementada** — requiere configuración en Siesa |

**Connekta alimenta hacia adelante; la línea base solo entra por el TXT.** Por eso
el cargador se conserva como herramienta de backfill en vez de eliminarse, y por
eso `VIGIA_CARGAR_TXT` no debe volver a `false` antes de verificar la carga.

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
| NOTA_CREDITO_FACTURA | 142946 | `recaudo.siesa_nc_triggered` (pre-flag) | 1ro |
| RECIBO_CAJA | 142888 | `recaudo.siesa_rc_triggered` (pre-flag) | 2do (espera NC) |
| DOCUMENTO_CONTABLE_RET | 142882 | `recaudo.siesa_dc_triggered` (pre-flag) | 3ro (espera RC) |
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
| 2 | test_siesa_contracts.py | 25 | Payloads vs spec DOCX (142888, 142882, 142946) |
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
| 142945 | `142945_API_v1_Ventas_Comercial_RemisionPedido.docx` | ✓ Limpio |
| 142946 | `142946 - API_v1_Ventas_Comercial_NotaFactura 428509.docx` | ✓ 3 obligatorios agregados. Clon 250696 (dinámico) para Devolución de Cliente — requiere `F350_IND_ESTADO=0`, ver Regla #21 |
| 142948 | `142948 - API_v1_Compras_Comercial_EntradaOC.docx` | ✓ Limpio (2 extras low-risk) |
| 142951 | `142951-API_v1_Inventarios_Comercial_DocumentoInv.docx` | ✓ Limpio |
| 173066 | `173066 - API_v1_Inventarios_Comercial_TransferenciaDirecta.docx` | ✓ f470_desc_varible agregado |
| 173076 | `173076.docx` | ✓ Limpio (f462_* extras low-risk) |
| 173079 | `173079 - API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada.docx` | ⚠ Extras: f350_id_clase_docto, f450_id_concepto, f470_ind_naturaleza |
| 174646 | `174646 - API_v1_Inventarios_Comercial_RequisicionesParaTransferir.docx` | ✓ Limpio (4 extras low-risk) |

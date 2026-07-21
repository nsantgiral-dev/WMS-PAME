# WMS-PAME — Referencia Central

## Stack

- **Backend**: Flask + SQLAlchemy + PostgreSQL + Gunicorn (Railway)
- **Frontend**: PWA vanilla JS modularizada (app.js + 9 módulos)
- **Integración ERP**: Connekta V2/V3 → Siesa Enterprise
- **DLQ**: SiesaJob con reintentos + backoff exponencial (5→15→45 min, max 3)
- **Tests**: pytest (314 passing), CI en Railway buildCommand

## Arquitectura JS (Frontend)

```
app.js          (4,208 líneas)  Core: auth, helpers, dashboard, camera, admin
picking.js        (815)         Escaneo operario, confirmación
packing.js        (821)         Empacador HUD, bultos, etiquetas
recepcion.js    (1,321)         OCs, escaneo ciego, traslados entrantes, devoluciones
rutas.js        (2,337)         Muelle, conductor, planilla, maestras, vehículos
traslados.js      (660)         Panel admin traslados
conteo.js         (945)         Inventario cíclico, ABC
reposicion.js     (476)         Reposición RESERVA→PICKING
liquidacion.js    (524)         Liquidación financiera NCE→RC→DC
layout.js         (715)         Ubicaciones físicas 5 ejes
etiquetas.js      (111)         Impresión de etiquetas
```

Orden de carga: app → picking → packing → recepcion → rutas → traslados → conteo → reposicion → liquidacion → layout → etiquetas. Todas las funciones son globales. Cross-module calls son runtime (onclick), nunca parse-time.

---

## Mapa de Conectores Siesa

### Escritura (POST)

| ID | Nombre | Qué hace | Flujo WMS | Job DLQ | Función gateway |
|----|--------|----------|-----------|---------|-----------------|
| 238925 | FacturaDesdePedido | FE desde pedido comprometido | Cierre packing (pedido completo) | DESPACHO_F470 | `trigger_factura()` |
| 142945 | RemisionPedido | Remisión (despacho parcial) | Cierre packing parcial | DESPACHO_F470 | `trigger_despacho()` |
| 142943 | FacturaDesdeRemision | FE desde remisión existente | Post-142945 (cadena) | — (inline) | `trigger_factura_desde_remision()` |
| 142948 | EntradaOC | Entrada por orden de compra | Recepción confirmada | ENTRADA_OC | `confirmar_entrada_compras()` |
| 142951 | DocumentoInv | Ajuste físico / transferencia averías | Conteo cíclico / devolución | AJUSTE_CONTEO / TRASLADO_AVERIAS | `enviar_ajuste_inventario()` / `transferir_a_averias()` |
| 173066 | TransferenciaDirecta | Transferencia intra-bodega | Reposición RESERVA→PICKING | TRANSFERENCIA_UBICACIONES | `transferir_entre_ubicaciones()` |
| 173076 | TransitoSalida (STS) | Salida en tránsito inter-bodega | Despacho traslado | DESPACHO_TRASLADO | `transferencia_transito_salida()` |
| 173079 | TransitoEntrada (ETS) | Llegada en tránsito | Recepción traslado | — (inline) | `transferencia_transito_entrada()` |
| 174646 | RequisicionTraslado (RIT) | Requisición de transferencia | Aprobación traslado | — (inline) | `crear_requisicion_traslado()` |
| 174930 | TransferenciaDesdeRIT | STS desde RIT existente | Despacho traslado (con RIT) | DESPACHO_TRASLADO | `despachar_desde_requisicion()` |
| 244328 | CompromisosPedido | Actualiza cantidades comprometidas | Pre-despacho parcial | — (inline) | `trigger_comprometer_pedido()` |
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
| `CONNEKTA_ID_SISTEMA` | `''` | Para conectores dinámicos v3.1 (238925, 244328) |
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
| `SIESA_TIPO_DOCTO_FACTURA` | `FEW` | FE | 238925, 142943 |
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

---

## DLQ — Dead Letter Queue

### Dispatch (`siesa_job_service._ejecutar_job`)

| Job tipo | Conector | Idempotencia | Secuencia |
|----------|----------|-------------|-----------|
| TRANSFERENCIA_UBICACIONES | 173066 | NON-idempotent (abort en retry) | — |
| DESPACHO_F470 | 238925/142945→142943 | `tarea.siesa_triggered` | — |
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

### Tiers

| Tier | Archivo | Tests | Qué valida |
|------|---------|-------|------------|
| 1 | test_siesa_formatos.py | 27 | `_fmt_valor` 21 chars, timezone, CO→Caja, forma_pago→medio |
| 2 | test_siesa_contracts.py | 25 | Payloads vs spec DOCX (142888, 142882, 142946) |
| 3 | test_siesa_dlq.py | 6 | Pre-flag, revert en fallo, secuencialidad NC→RC→DC |
| 4 | test_liquidacion.py | 11 | 6 flujos recaudo, retenciones PUC |
| 5 | test_siesa_guards.py | 7 | Guards fail-fast (bodega, codigo_siesa, motivo, consec) |

### CI en Railway

`railway.toml` tiene `buildCommand` que corre pytest antes de deploy. Si un test falla, el deploy se bloquea.

---

## Deploy

Railway detecta push a main automáticamente. Pipeline: install deps → pytest → `flask db upgrade` → gunicorn.

### Migraciones pendientes

- `a1b2c3d4e5f6` — `inventario_descontado` flag en solicitudes_traslado
- `b2c3d4e5f6a7` — unique partial index en sesiones_conteo

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

Los specs originales de cada conector están en `/Downloads/siesa/`. **Cada cambio a connekta_gateway.py DEBE cruzarse campo por campo contra el spec DOCX.**

Conectores con spec verificado contra código (julio 2026):

| Conector | Spec DOCX | Estado |
|----------|-----------|--------|
| 142888 | `142888 API_v1_ReciboCaja.docx` | ✓ 15/15 campos CxC verificados |
| 142882 | `142882 - API_v1_DocumentoContable 428272.docx` | ✓ 29/29 campos MovimientoCxC verificados |
| 142945 | `142945_API_v1_Ventas_Comercial_RemisionPedido.docx` | ✓ Limpio |
| 142946 | `142946 - API_v1_Ventas_Comercial_NotaFactura 428509.docx` | ✓ 3 obligatorios agregados |
| 142948 | `142948 - API_v1_Compras_Comercial_EntradaOC.docx` | ✓ Limpio (2 extras low-risk) |
| 142951 | `142951-API_v1_Inventarios_Comercial_DocumentoInv.docx` | ✓ Limpio |
| 173066 | `173066 - API_v1_Inventarios_Comercial_TransferenciaDirecta.docx` | ✓ f470_desc_varible agregado |
| 173076 | `173076.docx` | ✓ Limpio (f462_* extras low-risk) |
| 173079 | `173079 - API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada.docx` | ⚠ Extras: f350_id_clase_docto, f450_id_concepto, f470_ind_naturaleza |
| 174646 | `174646 - API_v1_Inventarios_Comercial_RequisicionesParaTransferir.docx` | ✓ Limpio (4 extras low-risk) |

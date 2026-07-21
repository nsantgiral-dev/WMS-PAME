# Siesa Enterprise + Connekta — Learnings Completos
## Papeleria Medellin — Gestor de Cartera (Junio 2026)

Documento de referencia con TODOS los aprendizajes acumulados de la integracion
con Siesa Enterprise via Connekta V2/V3. Incluye arquitectura, APIs, errores,
soluciones, configuracion y reglas de negocio innegociables.

---

## 1. Arquitectura Connekta V2/V3

### Endpoints

| Endpoint | Metodo | Proposito |
|:---|:---:|:---|
| `/api/siesa/v3/ejecutarconsultaestandar` | GET | Consultas estandar (CxC, CxP, Recibos, Movtos, Auxiliares, Inventarios, Items) |
| `/api/connekta/v3/ejecutarconsulta` | GET | Consultas dinamicas (queries custom del builder de Connekta) |
| `/api/siesa/v3/conectoresimportarestandar` | POST | Importar/crear documentos (Recibo de Caja via Conector 142888) |

### Autenticacion

- Dos headers estaticos: `ConniKey` (API key) + `ConniToken` (JWT)
- Los tokens son ESTATICOS — nunca expiran
- Variables de entorno: `CONNI_BASE_URL`, `CONNI_KEY`, `CONNI_TOKEN`
- `CONNI_ID_COMPANIA` = 8215 (ID tenant Connekta, NO es F_CIA)

### Parametros GET

- `idCompania`: int (8215)
- `descripcion`: nombre del API (ej. "API_v2_CxC_General")
- `paginacion`: `numPag=N|tamPag=100`
- `parametros`: filtro SQL-like SIN keyword `WHERE`

### Estructura de Respuesta

**Consultas estandar:**
```json
{ "codigo": 0, "mensaje": "...", "detalle": { "Table": [...] } }
```

**Consultas dinamicas (custom):**
```json
{ "codigo": 0, "mensaje": "...", "detalle": { "Datos": [...] } }
```
Nota: clave `Datos` en vez de `Table`.

**Resultado vacio:** HTTP 200, codigo=0, Table=[]. NO es un error.

**Fin de paginacion:** HTTP 400 + codigo=1 + "No se encontraron registros" = se maneja gracefully retornando lista vacia.

### Sintaxis de Filtros

- SQL-like pero sin keyword `WHERE`
- Strings con comillas simples dobles: `''texto''`
- IS NULL funciona: `f353_fecha_cancelacion IS NULL`
- LIKE funciona: `f253_id LIKE ''1305%''`
- Rangos: `f253_id >= ''13050000'' AND f253_id <= ''13059999''`

---

## 2. Catalogo de APIs Usadas

| API # | Nombre (`descripcion`) | Tabla | Proposito | Adaptador |
|:---:|:---|:---:|:---|:---|
| 20 | API_v2_CxC_General | T353 | Cuentas por cobrar (facturas) | SiesaCxCAdapter |
| 21 | API_v2_CxC_Recibos | T350/T357 | Recibos de caja (conciliacion) | SiesaRecibosAdapter |
| 22 | API_v2_CxP_General | T353 | Cuentas por pagar | SiesaCxPAdapter |
| 1 | API_v2_Auxiliares | — | Plan de cuentas (PUC) | SiesaSaldosAdapter |
| 39 | API_v2_MovtosContables_General | T350/T351 | Movimientos contables (saldos, POS) | SiesaSaldosAdapter |
| 26 | API_v2_Inventarios_InvFecha | T400 | Stock existencias/costos | SiesaInventarioAdapter |
| 27 | API_v2_Items | T120 | Maestro de items | SiesaInventarioAdapter |
| Custom | papeleriamedellin_API_custom_TercerosContacto | T200+T015 | Terceros: nombres, telefonos, emails | dependencies.py |
| POST | API_v1_ReciboCaja (Conector 142888) | — | Crear Recibos de Caja | SiesaRecaudoAdapter |

---

## 3. Mapeo de Campos por API

### API 20 — CxC (SiesaCxCRow)

| Campo Siesa | Campo DTO | Tipo | Descripcion |
|:---|:---|:---:|:---|
| f353_rowid | rowid | int | ID unico |
| f353_id_cia | company_id | int | Siempre 1 |
| f353_id_co_cruce | co_id | str | Centro de Operacion |
| f353_prefijo_cruce | prefix | str/None | Prefijo opcional |
| f353_id_tipo_docto_cruce | document_type | str | FE, FV, FVC, NDC |
| f353_consec_docto_cruce | consecutive | int | Consecutivo documento |
| f353_nro_cuota_cruce | installment_number | int | 0 para cuota unica |
| f353_fecha | record_date | datetime | Fecha registro |
| f353_fecha_cancelacion | cancellation_date | datetime/None | Null = vigente |
| f353_fecha_vcto | due_date | datetime | Fecha vencimiento |
| f200_id | third_party_id | str | NIT/cedula |
| f353_total_db | total_debit | Decimal | Debito total |
| f353_total_cr | total_credit | Decimal | Credito total |
| f253_id | account_id | str | Cuenta PUC (13050501) |
| f353_id_un_cruce | business_unit | str | Unidad de negocio (default "99") |
| f201_id_sucursal | branch_id | str | Sucursal (default "001") |

**Calculo saldo CxC:** `total_debit - total_credit`. Abonos parciales actualizan `cr` in-place.

**PK (Trinidad Documental + cuota):** 4 campos obligatorios: `co + tipo_docto + consecutivo + cuota`. Omitir CO o tipo_docto mezcla sucursales o tipos de documento — error letal.

**Formato documento:** `{CO}-{prefix?}-{tipo_docto}-{consecutivo}-C{cuota}` ej: `001-FE-5020-C0`

### API 22 — CxP (SiesaCxPRow)

Mismos campos que CxC MAS:
- `f353_vlr_dscto_pp` → descuento pronto pago (Decimal, default 0)
- `f353_fecha_dscto_pp` → fecha descuento PP (datetime/None)

**Calculo saldo CxP (INVERTIDO):** `total_credit - total_debit` (opuesto a CxC).

**Diferencia clave:** CxP NO cuarentena `due_date < record_date` porque los proveedores frecuentemente envian facturas registradas despues de su vencimiento.

**Filtro:** `f253_id >= ''22050000'' AND f253_id <= ''22059999''`

### API 21 — Recibos de Caja (SiesaReciboRow)

| Campo Siesa | Campo DTO | Tipo | Descripcion |
|:---|:---|:---:|:---|
| f350_rowid | rowid | int | ID unico |
| f350_id_co | co_id | str | Centro de Operacion |
| f350_id_tipo_docto | document_type | str | RC |
| f350_consec_docto | consecutive | int | Consecutivo |
| f350_fecha | document_date | datetime | Fecha documento |
| f350_ind_estado | status | int | Estado |
| f350_fecha_ts_anulacion | annulment_date | datetime/None | Null = vigente |
| f200_id | client_id | str | NIT/cedula |
| f200_razon_social | client_name | str | Razon social |
| f357_fecha_recaudo | collection_date | datetime/None | Fecha recaudo |
| f357_valor_ingreso | receipt_amount | Decimal | Valor |
| f025_id | payment_method_id | str | Codigo medio pago |
| f025_descripcion | payment_method_desc | str | Descripcion medio |

**Filtro:** `f350_fecha >= ''YYYY-MM-DDT00:00:00'' AND f350_fecha_ts_anulacion IS NULL`

### API 39 — Movimientos Contables

| Campo Siesa | Descripcion |
|:---|:---|
| f253_id | Cuenta PUC |
| f351_valor_db | Monto debito |
| f351_valor_cr | Monto credito |
| f351_fecha | Fecha movimiento |
| f350_id_co | Centro de Operacion |
| f350_id_tipo_docto | Tipo documento (FE, NE, RC, etc.) |
| f350_prefijo | Prefijo (FE1, FE2, etc.) |
| f200_razon_social | Nombre tercero |

**Filtro cuentas 11xx:** `f253_id >= ''11000000'' AND f253_id <= ''11999999''`

### TercerosContacto (Query Dinamico Custom)

```sql
SELECT t200.f200_id, t200.f200_nit, t200.f200_razon_social,
       t200.f200_nombres, t200.f200_apellido1, t200.f200_apellido2,
       t015.f015_celular, t015.f015_telefono, t015.f015_email, t015.f015_direccion1
FROM t200_mm_terceros t200
LEFT JOIN t015_mm_contactos t015
    ON t015.f015_rowid = t200.f200_rowid_contacto
WHERE t200.f200_id_cia = 1
    AND t200.f200_ind_cliente = 1
    AND t200.f200_ind_estado = 1
```

Usa `/api/connekta/v3/ejecutarconsulta` con clave `Datos`.

---

## 4. Conector 142888 — Recibo de Caja (POST)

### Endpoint

```
POST /api/siesa/v3/conectoresimportarestandar
  ?idCompania=8215
  &idDocumento=142888
  &nombreDocumento=API_v1_ReciboCaja
```

### Estructura JSON — 5 Secciones

#### Seccion Inicial
```json
{ "F_CIA": 1 }
```
**CRITICO:** F_CIA = 1 (codigo interno Siesa), NO 8215 (ID tenant Connekta).

#### Seccion RCyotrosingresos (Header RC)

| Campo | Tipo | Valor | Obligatorio |
|:---|:---:|:---|:---:|
| F_CIA | int | 1 | Si |
| F_CONSEC_AUTO_REG | int | 1 (automatico) | Si |
| F350_ID_CO | str | "001" (Centro Operacion) | Si |
| F350_ID_TIPO_DOCTO | str | "RC" | Si |
| F350_CONSEC_DOCTO | int | 0 (auto cuando CONSEC_AUTO=1) | Si |
| F350_FECHA | str | "YYYYMMDD" | Si |
| F357_ID_CAJA | str | Segun CO (ver mapa cajas) | Si |
| F357_FECHA_RECAUDO | str | "YYYYMMDD" | Si |
| F350_ID_TERCERO | str | NIT/cedula cliente | Si |
| F357_ID_MONEDA_INGRESO | str | "COP" | Si |
| F357_VALOR_INGRESO | DecimalConSigno | "+000000001500000.0000" | Si |
| F357_ID_MONEDA_APLICAR | str | "COP" | Si |
| F357_VALOR_APLICAR_REAL | DecimalConSigno | "+000000001500000.0000" | Si |
| F357_ID_COBRADOR | str | "9876" (APP RECAUDO) | Si |
| F357_ID_UN | str | "99" (del primer cruce) | Dep |
| F357_ID_CCOSTO | str | "" | Dep |
| F357_ID_FE | str | "1103" (flujo efectivo) | Dep |
| F350_ID_CLASE_DOCTO | int | 13 (Recibo de caja) | Si |
| F350_IND_ESTADO | int | 1 (Aprobado) | Si |
| F350_IND_IMPRESION | int | 0 (No impreso) | Si |
| F350_NOTAS | str | "Recaudo digital APP - TBA" | No |
| F357_IND_VALIDA_MEDPAGO | int | 0 (No valida) | Si |

#### Seccion Caja (Medio de Pago)

| Campo | Tipo | Valor | Obligatorio |
|:---|:---:|:---|:---:|
| F_CIA | int | 1 | Si |
| F350_ID_CO | str | "001" | Si |
| F350_ID_TIPO_DOCTO | str | "RC" | Si |
| F350_CONSEC_DOCTO | int | 0 | Si |
| F358_ID_MEDIOS_PAGO | str | "EFE" o "TBA" etc. | Si |
| F358_VALOR | DecimalConSigno | "+000000001500000.0000" | Si |
| F358_ID_BANCO | str | "" (VACIO para transferencias) | Dep |
| F358_NRO_CHEQUE | int | 0 | Dep |
| F358_NRO_CUENTA | str | "" (VACIO para transferencias) | Dep |
| F358_COD_SEGURIDAD | str | "" | Dep |
| F358_NRO_AUTORIZACION | str | "" | Dep |
| F358_FECHA_VCTO | str | "" | Dep |
| F358_REFERENCIA_OTROS | str | Referencia bancaria (solo consignaciones) | Dep |
| F358_FECHA_CONSIGNACION | str | "YYYYMMDD" (solo consignaciones) | Dep |
| f358_docto_banco_cg | str | "CG" (solo consignaciones) | Dep |

**Campos condicionales para transferencias/consignaciones:**
- `F358_REFERENCIA_OTROS`: numero comprobante bancario
- `F358_FECHA_CONSIGNACION`: fecha en formato YYYYMMDD
- `f358_docto_banco_cg`: "CG" (Consignacion)

**CRITICO:** `F358_ID_BANCO` y `F358_NRO_CUENTA` DEBEN estar VACIOS para transferencias. Enviarlos activa la validacion de cuenta bancaria y causa error.

#### Seccion CxC (Cruces de Facturas)

Un registro por cada factura cruzada:

| Campo | Tipo | Valor | Obligatorio |
|:---|:---:|:---|:---:|
| F353_ID_AUXILIAR_DOCTO_CRUCE | str | "13050501" (cuenta CxC) | Si |
| F353_ID_CO_DOCTO_CRUCE | str | CO de la factura (no del RC) | Si |
| F353_ID_UN_DOCTO_CRUCE | str | Unidad negocio | Si |
| F353_ID_SUCURSAL_DOCTO_CRUCE | str | Sucursal | Si |
| F353_ID_TIPO_DOCTO_CRUCE | str | "FE" (incluye prefijo: "FE3") | Si |
| F353_CONSEC_DOCTO_CRUCE | int | Consecutivo | Si |
| F353_NRO_CUOTA_CRUCE | int | Numero cuota (0 = unica) | Si |
| F354_VALOR_CR | DecimalConSigno | Valor a cruzar | Dep |
| F354_VALOR_APLICADO_PP | DecimalConSigno | "+000000000000000.0000" | Dep |
| F354_VALOR_APROVECHA | DecimalConSigno | "+000000000000000.0000" | Dep |
| F354_VALOR_RETENCION | DecimalConSigno | "+000000000000000.0000" | Dep |

**CRITICO:** `F353_PREFIJO_CRUCE` NO existe en Conector 142888. El prefijo es parte de `F353_ID_TIPO_DOCTO_CRUCE` (ej: "FE3", no "FE" + prefijo "3").

**CRITICO:** `F350_ID_CO` en CxC es el CO del RC. `F353_ID_CO_DOCTO_CRUCE` es el CO de la factura cruzada. Son diferentes.

#### Seccion Final
```json
{ "F_CIA": 1 }
```

### Formato DecimalConSigno

Siempre 21 caracteres: `signo(1) + enteros(15) + punto(1) + decimales(4)`

Ejemplo: `+000000001500000.0000`

### Formato Fecha

`YYYYMMDD` sin separadores. Ejemplo: `20260617`

---

## 5. Maestros de Configuracion en Siesa

### Centros de Operacion (CO)

| Codigo | Sede |
|:---:|:---|
| 001 | NEIVA SUR |
| 002 | NEIVA CENTRO |
| 003 | NEIVA BODEGA CD |
| 004 | PITALITO CENTRO |
| 005 | PITALITO TERMINAL |
| 006 | FLORENCIA CENTRO |
| 007 | FERIA NEIVA |
| 008 | FERIA PITALITO |
| 009 | FERIA FLORENCIA |
| 999 | ADMINISTRATIVO |

### Mapa CO a Caja

| CO | Caja | Descripcion Caja |
|:---:|:---:|:---|
| 001 | 001 | CAJA NEIVA SUR 001 |
| 002 | 004 | CAJA NEIVA CENTRO 001 |
| 003 | 999 | CAJA GENERAL NEIVA BODEGA CD |
| 004 | 999 | CAJA GENERAL PITALITO CENTRO |
| 005 | 999 | CAJA GENERAL PITALITO TERMINAL |
| 006 | 013 | CAJA FLORENCIA CENTRO 001 |
| 007 | 999 | CAJA GENERAL FERIA NEIVA |
| 008 | 999 | CAJA GENERAL FERIA PITALITO |
| 009 | 999 | CAJA GENERAL FERIA FLORENCIA |

### Cajas Existentes Completas

| CO | Codigo | Descripcion | Auxiliar PUC |
|:---:|:---:|:---|:---:|
| 001 | 001 | CAJA NEIVA SUR 001 | 11050501 |
| 001 | 14 | CAJA PLANTILLA DE CUADRE NS | — |
| 001 | 999 | CAJA GENERAL NEIVA SUR | 11050510 |
| 002 | 004 | CAJA NEIVA CENTRO 001 | — |
| 002 | 999 | CAJA GENERAL NEIVA CENTRO | — |
| 003 | 17 | CAJA PLANTILLA DE CUADRE NB1 | — |
| 003 | 999 | CAJA GENERAL NEIVA BODEGA CD | 11050501 |
| 004 | 15 | CAJA PLANTILLA DE CUADRE PC | — |
| 004 | 999 | CAJA GENERAL PITALITO CENTRO | — |
| 006 | 013 | CAJA FLORENCIA CENTRO 001 | — |
| 006 | 16 | CAJA PLANTILLA DE CUADRE FC | — |
| 006 | 999 | CAJA GENERAL FLORENCIA CENTRO | — |
| 007 | 999 | CAJA GENERAL FERIA NEIVA | — |

**Todas las cajas tienen auxiliar PUC tipo 1105 (caja).** El maestro de cajas en Siesa NO permite asociar cuentas bancarias (1120). Las "PLANTILLA DE CUADRE" son para cuadres internos.

### Medios de Pago

| Codigo | Descripcion | Tipo Siesa | Cnta. Bancaria |
|:---:|:---|:---:|:---:|
| EFE | EFECTIVO | Efectivo | N/A (grisado) |
| TBA | TRANSFERENCIA BANCOLOMBIA AHO | Consignaciones | 011 |
| TBC | TRANSFERENCIA BANCOLOMBIA CTE | Consignaciones | 002 |
| TBB | TRANSFERENCIA BBVA CTE | Consignaciones | 005 |
| TBG | TRANSFERENCIA BANCO BOGOTA CTE | Consignaciones | 004 |
| TAA | TRANSFERENCIA AGRARIO AHORRO | Consignaciones | 008 |
| TAC | TRANSFERENCIA AGRARIO CTE | Consignaciones | 001 |
| TDV | TRANSFERENCIA DAVIVIENDA CTE | Consignaciones | 003 |
| TDC | TARJETAS DB CR | Tarjeta | N/A |

**Ruta en Siesa:** Cuentas x cobrar > Maestros asociados > Medios de pago.

**Tab Entidades:** Todos mapeados a FE_MEDIOS DE PAGO 2.1. Transferencias = codigo 45 (Transferencia Credito Bancario). Efectivo = codigo 10.

### Cuentas Bancarias

| Codigo | Banco | Tipo Cuenta | Numero | PUC Auxiliar |
|:---:|:---|:---|:---|:---:|
| 001 | Banco Agrario | Corriente | 390500072287 | 11100501 |
| 002 | Bancolombia | Corriente | 17653309710 | 11100502 |
| 003 | Davivienda | Corriente | 076160019253 | 11100503 |
| 004 | Banco Bogota | Corriente | 442426532 | 11100504 |
| 005 | Banco BBVA | Corriente | 8560100000047 | 11100505 |
| 006 | Banco Popular | Corriente | 390135275 | 11100506 |
| 007 | Bancolombia | Comp. Panama | 80110002280 | 11101001 |
| 008 | Banco Agrario | Ahorro | 4390500036849 | 11200501 |
| 009 | Bancolombia | Ahorro | 076-000080-03 | 11200504 |
| 010 | Banco Bogota | Ahorro | 793294547 | 11200503 |
| 011 | Bancolombia | Ahorro | 81695311080 | 11200502 |
| 012 | Bancolombia | Ahorro | 466-000045-79 | 11200505 |
| 013 | Bancolombia | Ahorro | 453-000058-26 | 11200506 |

### Maestro de Bancos (codigos Siesa)

| Codigo | Banco |
|:---:|:---|
| 01 | BOGOTA |
| 02 | POPULAR |
| 07 | BANCOLOMBIA S.A. |
| 13 | BBVA COLOMBIA |
| 40 | AGRARIO DE COLOMBIA S.A. |
| 51 | DAVIVIENDA S.A. |

### Plan de Cuentas (PUC) — Auxiliares Relevantes

**Cuentas Corrientes (1110):**

| PUC | Descripcion |
|:---|:---|
| 11100501 | BANCO AGRARIO CTA CTE 39050072287 |
| 11100502 | BANCOLOMBIA CTA CTE 17653309710 |
| 11100503 | DAVIVIENDA CTA CTE 076160019253 |
| 11100504 | BANCO BOGOTA CTA CTE 442426532 |
| 11100505 | BANCO BBVA CTA CTE 8560100000047 |
| 11100506 | BANCO POPULAR CTA CTE 390135275 |
| 11101001 | BANCOLOMBIA CTA COMP PANAMA 80110002280 |

**Cuentas de Ahorro (1120):**

| PUC | Descripcion |
|:---|:---|
| 11200501 | BANCO AGRARIO CTA AHO 439050036849 |
| 11200502 | BANCOLOMBIA CTA AHO 81695311080 |
| 11200503 | BANCO BOGOTA CTA AHO 793294547 |
| 11200504 | BANCOLOMBIA CTA AHO 076-000080-03 |
| 11200505 | BANCOLOMBIA CTA AHO 466-000045-79 |
| 11200506 | BANCOLOMBIA CTA AHO 453-000058-26 |

**Cuentas de Caja (1105):**

| PUC | Descripcion |
|:---|:---|
| 11050501 | CAJA GENERAL |
| 11050510 | CAJA GENERAL |
| 11050599 | CAJA PUENTE POS |

---

## 6. Ventas POS — Contabilidad Forense (T2.1)

### El Problema: Inflacion 2.9x

El dashboard originalmente contaba TODOS los movimientos de cuentas 11xx, inflando ventas ~2.9x porque cada peso fluye por multiples cuentas en el ciclo POS:

```
FE -> 11050599 (Caja Puente) -> RCx -> 11050501/02 (Caja/Tarjetas) -> 111xxx (Bancos)
```

### La Solucion: Reglas de Clasificacion

| Clasificacion | Tipo Docto | Cuenta | Naturaleza | Que significa |
|:---|:---:|:---:|:---:|:---|
| Venta POS | FE* | 1105 (cualquier) | Debito | Venta real al consumidor |
| Devolucion POS | NE* | 1105 (cualquier) | Credito | Devolucion/anulacion |
| Cierre Caja | RCx (RC1-RC9) | No-puente | Debito | Destino del cierre (mix de medios) |
| Recaudo Cartera | RC (exacto) | — | — | Cobro CxC, no venta POS |

### Mapeo Cuentas a Medios de Pago (POS)

| Cuenta PUC | Medio de Pago |
|:---|:---|
| 11050501 | Efectivo |
| 11050502 | Tarjetas |
| 1110xxxx | Bancos / Transferencia |
| 1115xxxx | Remesas en Transito |
| 1120xxxx | Cuentas de Ahorro |

### CO 003 = BODEGA CDAFI
Es bodega, no punto de venta POS. Se separa en el analisis.

---

## 7. Seguridad y Resiliencia del Cliente HTTP

### Paginacion Segura

- `MAX_PAGES = 100` (aborta despues de 100 paginas)
- `QUERY_TIMEOUT = 120` segundos hard cap por llamada execute_query
- Deteccion de pagina parcial: para cuando la pagina retorna menos registros que `page_size`

### Rate Limiting

- HTTP 429 con header `Retry-After`
- GET: reintenta hasta `max_retries` (default 3) con sleep de `Retry-After`
- POST: reintenta SOLO en 429, NUNCA en 5xx/transport errors

### Politica de NO-Retry en POST (CRITICO)

POST a `/conectoresimportarestandar` **NO reintenta** en errores 5xx o de transporte.
Solo 429 se reintenta (porque 429 significa que la solicitud NO fue procesada).

**Razon:** El incidente del RC fantasma RC-00002744 probo que reintentar POST en timeout/5xx crea documentos duplicados en Siesa. El primer request fue procesado exitosamente pero el timeout hizo que el cliente lo reintentara, creando un segundo RC.

Implementado en `_post_with_retry()` de `connekta_client.py`.

### Manejo de Errores HTTP

| Codigo HTTP | Respuesta Siesa | Accion |
|:---|:---|:---|
| 200 + codigo=0 | Datos validos | Retorna datos |
| 200 + codigo!=0 | Error de query | Raise ConnektaApiError |
| 400 + "no se encontraron" | Fin de paginacion | Retorna lista vacia |
| 400 + otro | Error | Raise ConnektaApiError |
| 429 | Rate limit | Retry con Retry-After (GET y POST) |
| 5xx (GET) | Error servidor | Retry con 1s sleep |
| 5xx (POST) | Error servidor | Raise inmediatamente (NO retry) |

---

## 8. Pipeline de Validacion de Datos

Defensa en tres capas:

1. **Filtro API (server-side):** `f353_fecha_cancelacion IS NULL` + rango de cuentas
2. **Parse Pydantic (capa DTO):** Validacion de tipos. Filas no-parseables van a cuarentena con "Pydantic parse error"
3. **Validacion Mapper (capa ACL):**
   - CxC skip: canceladas, totalmente pagadas (saldo <= 0)
   - CxC cuarentena: third_party_id vacio, co_id vacio, document_type vacio, due_date < record_date
   - CxP cuarentena: igual EXCEPTO que NO cuarentena due_date < record_date
   - Recibos skip: anulados, valor cero, client_id vacio
   - Inventario excluye: existencia <= 0, costo_total <= 0

---

## 9. Variables de Entorno

### Cliente Connekta
| Variable | Default | Descripcion |
|:---|:---|:---|
| CONNI_BASE_URL | (requerido) | URL base Connekta |
| CONNI_KEY | (requerido) | API key |
| CONNI_TOKEN | (requerido) | JWT token (estatico) |
| CONNI_ID_COMPANIA | 8215 | ID tenant Connekta |
| CONNI_PAGE_SIZE | 100 | Tamano pagina (NUNCA mayor a 100) |
| CONNI_TIMEOUT | 30 | Timeout en segundos |
| CONNI_MAX_RETRIES | 3 | Max reintentos GET |

### Recaudo (POST RC)
| Variable | Default | Descripcion |
|:---|:---|:---|
| SIESA_RC_CIA | 1 | Codigo compania (F_CIA) |
| SIESA_RC_COBRADOR | "9876" | Cobrador APP RECAUDO |
| SIESA_RC_FE | "1103" | Flujo de efectivo |
| SIESA_CAJA_001-009 | Ver mapa | Override caja por CO |

### Cache
| Variable | Default | Descripcion |
|:---|:---|:---|
| SIESA_CACHE_TTL | 1800 | TTL en segundos (30 min) |

---

## 10. Errores Resueltos — Autopsia Completa

### ERROR 1: "La cuenta bancaria no existe" (Conector 142888)

**Mensaje Siesa:**
```json
{
  "f_nro_linea": "2",
  "f_tipo_reg": "357",
  "f_detalle": "La cuenta bancaria    no existe."
}
```

**Sintoma:** Los espacios entre "bancaria" y "no" indican que Siesa busco un codigo de cuenta bancaria y lo encontro VACIO.

**Enfoques que FALLARON:**

| # | Enfoque | Resultado | Por que fallo |
|:---:|:---|:---|:---|
| 1 | Cajas virtuales con auxiliar 1120 | Imposible | Siesa solo acepta auxiliares 1105 en maestro de cajas |
| 2 | Campo f358_id_cuentas_bancarias_cg en JSON | Ignorado silenciosamente (2 intentos) | Conector 142888 no mapea este campo aunque existe en T358 |
| 3 | F358_ID_BANCO + F358_NRO_CUENTA con valores | Error persiste | Estos campos son para cheques/tarjetas, no consignaciones |
| 4 | F358_ID_BANCO y F358_NRO_CUENTA vacios | Error persiste | La cuenta bancaria no viene de estos campos para consignaciones |
| 5 | F357_IND_VALIDA_MEDPAGO = 0 | No ayuda | Solo controla validacion medio vs caja, no cuenta bancaria |

**SOLUCION DEFINITIVA:** Configurar el campo **"Cnta. bancaria"** en el maestro de medios de pago de Siesa.

**Ruta:** Cuentas x cobrar > Maestros asociados > Medios de pago > [abrir medio] > Tab Generales > campo "Cnta. bancaria..."

Cuando el medio es tipo "Consignaciones", Siesa lee la cuenta bancaria del maestro de medios de pago, NO del JSON payload, NO de la caja. Si el campo esta vacio, explota.

| Medio | Cnta. bancaria a configurar |
|:---:|:---:|
| TBA | 011 (Bancolombia Ahorro) |
| TBC | 002 (Bancolombia Corriente) |
| TBB | 005 (BBVA Corriente) |
| TBG | 004 (Bogota Corriente) |
| TAA | 008 (Agrario Ahorro) |
| TAC | 001 (Agrario Corriente) |
| TDV | 003 (Davivienda Corriente) |

**Leccion:** Antes de asumir que el error es del payload, verificar la configuracion de maestros en Siesa.

### ERROR 2: RC Fantasma (Duplicado RC-00002744)

**Sintoma:** Aparecio un Recibo de Caja que nadie creo manualmente.

**Causa raiz:** El cliente HTTP reintentaba POST en timeout. Siesa proceso la primera solicitud exitosamente pero respondio con timeout. El segundo intento creo un segundo RC.

**Solucion:** POST NUNCA reintenta en 5xx/transport errors. Solo reintenta en 429.

### ERROR 3: tamPag >= 500 retorna registros fantasma

**Sintoma:** API retorna un unico registro con TODOS los campos NULL.

**Causa raiz:** Connekta V2 tiene un bug donde tamPag >= 500 retorna un registro ghost.

**Solucion:** Max page_size = 100.

### ERROR 4: Datos de contacto "No disponible en ERP"

**Sintoma:** Algunos clientes mostraban "No disponible" para celular/email.

**Causa raiz:** Query custom fue accidentalmente sobreescrita con `SELECT TOP 10 *` durante debug. Cache servia datos viejos.

**Solucion:** Restaurar query + invalidar cache con `?debug=true`.

### ERROR 5: Siesa no responde despues de ~8 PM

**Causa raiz:** Web service se apaga fuera de horario laboral.

**Solucion:** Solo operar durante horario de oficina.

---

## 11. Reglas de Oro — Siesa Integration Checklist

1. **SIEMPRE leer la documentacion DOCX del conector ANTES de codificar.** El error "La cuenta bancaria no existe" tomo 5+ rondas de prueba-error. Leer la documentacion primero habria ahorrado dias.

2. **tamPag NUNCA >= 500.** Maximo seguro es 100. Connekta retorna registros fantasma con todo NULL.

3. **POST NUNCA reintenta en 5xx/timeout.** Solo en 429. Los reintentos crean documentos duplicados.

4. **F_CIA = 1, NO 8215.** 8215 es el ID tenant de Connekta. F_CIA es el codigo interno de compania en Siesa.

5. **F353_PREFIJO_CRUCE NO existe.** El prefijo va dentro de F353_ID_TIPO_DOCTO_CRUCE.

6. **Saldo CxC = db - cr. Saldo CxP = cr - db.** Son invertidos.

7. **F358_ID_BANCO y F358_NRO_CUENTA VACIOS para transferencias.** Enviarlos activa validacion de cuenta bancaria innecesaria.

8. **Cuenta bancaria para consignaciones va en el maestro de medios de pago**, no en el payload JSON ni en la caja.

9. **PK documental = CO + tipo_docto + consecutivo + cuota.** Omitir cualquiera mezcla documentos.

10. **Consultas dinamicas retornan Datos, no Table.** Usar endpoint diferente.

11. **Horario de operacion ~8 AM a ~8 PM.** Fuera de horario, Siesa no responde.

12. **Cache TTL 30 minutos.** Endpoint `?debug=true` para invalidar manualmente.

13. **API 39 es una sabana contable.** Solo usar para saldos bancarios y analisis POS. Para buscar pagos, usar API 21.

14. **Campos undocumented en T358 (como f358_id_cuentas_bancarias_cg) son IGNORADOS por el conector de importacion.** Que una columna exista en la BD no significa que el conector la lea del JSON.

15. **Antes de buscar la solucion en el codigo, verificar configuracion de maestros en Siesa.** Los errores de "no existe" generalmente son maestros mal configurados, no payloads incorrectos.

---

## 12. Arquitectura del Codigo

### Archivos de Infraestructura Siesa

| Archivo | Proposito |
|:---|:---|
| infraestructura/siesa/connekta_client.py | Cliente HTTP (GET paginado + POST con no-retry) |
| infraestructura/siesa/dto.py | DTO CxC (SiesaCxCRow) |
| infraestructura/siesa/mapper.py | Mapper CxC (ACL + cuarentena) |
| infraestructura/siesa/cxc_adapter.py | Adapter CxC |
| infraestructura/siesa/cxp_dto.py | DTO CxP |
| infraestructura/siesa/cxp_mapper.py | Mapper CxP |
| infraestructura/siesa/cxp_adapter.py | Adapter CxP |
| infraestructura/siesa/recibos_dto.py | DTO Recibos |
| infraestructura/siesa/recibos_mapper.py | Mapper Recibos |
| infraestructura/siesa/recibos_adapter.py | Adapter Recibos (conciliacion) |
| infraestructura/siesa/inventario_dto.py | DTO Inventario |
| infraestructura/siesa/inventario_mapper.py | Mapper Inventario |
| infraestructura/siesa/inventario_adapter.py | Adapter Inventario |
| infraestructura/siesa/saldos_adapter.py | Saldos bancarios + Movtos POS |
| infraestructura/siesa/recaudo_adapter.py | POST RC (registrar recibo de caja) |
| infraestructura/siesa/recaudo_payload.py | Builder del JSON de 5 secciones |
| infraestructura/siesa/quarantine.py | Cuarentena de datos invalidos |

### Patron de Diseno

Arquitectura Hexagonal. El dominio es PURO — no importa frameworks, Siesa, ni red. Siesa vive detras de puertos (Protocols). Toda la integracion esta en infraestructura/siesa/.

Patron DTO, Mapper, Domain Model en cada adapter:
1. **DTO (Pydantic):** Parsea la respuesta cruda de Siesa con validacion de tipos
2. **Mapper (ACL):** Capa anti-corrupcion que traduce al modelo de dominio, cuarentena datos invalidos
3. **Adapter:** Orquesta la consulta HTTP + parse + mapping

---

*Documento generado: 17 junio 2026. Papeleria Medellin — Gestor de Cartera PAME.*

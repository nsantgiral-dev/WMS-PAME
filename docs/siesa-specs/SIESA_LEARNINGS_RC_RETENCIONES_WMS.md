# Siesa Integration — Learnings Forenses: RC + Retenciones
## Para WMS y cualquier sistema que cree Recibos de Caja e Impuestos
### Papeleria Medellin — Julio 2026

Documento extraido de 40+ dias de integracion, 60+ commits, 874 tests,
y docenas de errores resueltos en produccion. Cada seccion es una cicatriz
que se convirtio en regla.

---

## SECCION 1: REGLAS DE ORO (leer primero, codificar despues)

### 1.1 Las 20 reglas inquebrantables

| # | Regla | Costo de violarla |
|---|-------|-------------------|
| 1 | **LEER EL DOCX DEL CONECTOR ANTES DE CODIFICAR** | 5+ rondas de prueba-error, dias perdidos |
| 2 | **F_CIA = 1, NUNCA 8215** | 8215 es el tenant Connekta, no el codigo de compania Siesa |
| 3 | **POST NUNCA reintenta en 5xx/timeout** | RC duplicados en Siesa (incidente RC-00002744) |
| 4 | **POST solo reintenta en 429** | 429 = no procesado, safe to retry |
| 5 | **tamPag NUNCA >= 500** | Connekta retorna registros fantasma con todo NULL |
| 6 | **tamPag maximo seguro = 100** | Probado en produccion, 500 rompe |
| 7 | **F353_PREFIJO_CRUCE NO existe en 142888** | El prefijo va DENTRO de F353_ID_TIPO_DOCTO_CRUCE |
| 8 | **F358_ID_BANCO y F358_NRO_CUENTA VACIOS para transferencias** | Enviarlos causa "La cuenta bancaria no existe" |
| 9 | **Cuenta bancaria va en maestro de medios de pago, no en el payload** | El JSON no controla esto para consignaciones |
| 10 | **NUNCA hardcodear F353_ID_AUXILIAR_DOCTO_CRUCE** | Usar f253_id real de la factura (13050501, 13050502, etc.) |
| 11 | **PK documental = CO + tipo_docto + consecutivo + cuota** | Omitir cualquiera mezcla documentos — error silencioso letal |
| 12 | **Saldo CxC = db - cr. Saldo CxP = cr - db** | Son invertidos. Confundirlos crea pagos fantasma |
| 13 | **Siesa no opera despues de ~8 PM** | TCP timeout de 30s, perdes tiempo debuggeando |
| 14 | **Campos undocumented en tablas son IGNORADOS por el conector** | f358_id_cuentas_bancarias_cg existe en T358 pero 142888 no lo lee |
| 15 | **DecimalConSigno = 21 chars exactos** | signo(1) + enteros(15) + punto(1) + decimales(4) |
| 16 | **Fechas = YYYYMMDD sin separadores** | No guiones, no barras |
| 17 | **Consultas dinamicas usan clave "Datos", no "Table"** | Endpoint diferente, respuesta diferente |
| 18 | **Filtros SQL sin WHERE** | Solo la condicion: `f200_id = ''12345''` |
| 19 | **Strings en filtros con doble comilla simple** | `''texto''` no `'texto'` |
| 20 | **ConniKey y ConniToken son ESTATICOS** | No expiran, no hay refresh flow |

---

## SECCION 2: CONECTOR 142888 — RECIBO DE CAJA (POST)

### 2.1 Endpoint

```
POST /api/siesa/v3/conectoresimportarestandar
  ?idCompania=8215
  &idDocumento=142888
  &nombreDocumento=API_v1_ReciboCaja
```

### 2.2 Estructura JSON — 5 secciones obligatorias

```
Inicial → { F_CIA: 1 }
RCyotrosingresos → Header del recibo (CO, caja, tercero, monto, cobrador)
Caja → Medio de pago (efectivo, transferencia, tarjeta)
CxC → Cruces contra facturas (1 registro por factura cruzada)
Final → { F_CIA: 1 }
```

**Las 5 secciones SON OBLIGATORIAS**, incluso si CxC tiene un solo registro.

### 2.3 Header (RCyotrosingresos) — campos criticos

| Campo | Tipo | Descripcion | Trampa |
|-------|------|-------------|--------|
| F_CIA | int | **1** (NO 8215) | Confundir con idCompania de Connekta |
| F_CONSEC_AUTO_REG | int | 1 = consecutivo automatico | Si pones 0, necesitas dar consecutivo |
| F350_CONSEC_DOCTO | int | 0 cuando CONSEC_AUTO=1 | No inventar consecutivos |
| F350_FECHA | str | "YYYYMMDD" | Sin guiones ni barras |
| F357_ID_CAJA | str | Codigo caja segun CO | Cajas "Plantilla de Cuadre" NO sirven |
| F357_VALOR_INGRESO | DecimalConSigno | Cash que entra a caja | Con retenciones = NETO |
| F357_VALOR_APLICAR_REAL | DecimalConSigno | Lo que se aplica a CxC | Debe ser = INGRESO |
| F357_ID_COBRADOR | str | "9876" (APP RECAUDO) | Crear un cobrador dedicado para la app |
| F357_ID_UN | str | UN del primer cruce | "99" default |
| F357_ID_FE | str | "1103" (flujo efectivo) | Cada tipo de flujo tiene su codigo |
| F357_IND_VALIDA_MEDPAGO | int | 0 = no valida medio vs caja | Poner 1 puede causar rechazos |

### 2.4 Caja — campos condicionales

**Para EFECTIVO y TARJETA:** solo F358_ID_MEDIOS_PAGO y F358_VALOR. Los demas vacios/cero.

**Para TRANSFERENCIAS/CONSIGNACIONES (TBA, TBC, TBB, etc.):**

| Campo | Valor | OBLIGATORIO |
|-------|-------|-------------|
| F358_REFERENCIA_OTROS | Numero comprobante bancario | SI |
| F358_FECHA_CONSIGNACION | "YYYYMMDD" | SI |
| f358_docto_banco_cg | "CG" | SI |
| F358_ID_BANCO | **""** (VACIO!) | Enviarlo causa error |
| F358_NRO_CUENTA | **""** (VACIO!) | Enviarlo causa error |

### 2.5 CxC — cruce contra facturas

| Campo | Valor | Trampa |
|-------|-------|--------|
| F350_ID_CO | CO del RC (no de la factura) | Diferente de F353_ID_CO_DOCTO_CRUCE |
| F353_ID_CO_DOCTO_CRUCE | CO de la factura | Diferente de F350_ID_CO |
| F353_ID_AUXILIAR_DOCTO_CRUCE | **f253_id REAL** de la factura | NUNCA hardcodear |
| F353_ID_UN_DOCTO_CRUCE | UN del cruce (API campo f353_id_un_cruce) | Propagar del API, no asumir |
| F353_ID_SUCURSAL_DOCTO_CRUCE | Sucursal (API campo f201_id_sucursal) | Propagar del API, no asumir |
| F353_ID_TIPO_DOCTO_CRUCE | "FE" (con prefijo incluido si existe) | NO existe F353_PREFIJO_CRUCE |
| F353_CONSEC_DOCTO_CRUCE | int, consecutivo | No confundir con rowid |
| F353_NRO_CUOTA_CRUCE | int, 0 = cuota unica | Cada cuota es un registro en CxC |
| F354_VALOR_CR | Valor a cruzar (DecimalConSigno) | Con retenciones = proporcion neta |
| F354_VALOR_RETENCION | 0 en nuestro esquema | Ver seccion retenciones |

### 2.6 Respuesta exitosa

```json
{ "codigo": 0, "mensaje": "Transacción Exitosa", "detalle": "Importacion exitosa" }
```

**OJO:** `detalle` dice "Importacion exitosa", NO devuelve el numero de RC asignado.
Para obtener el numero RC creado, consultar API 21 (CxC_Recibos) filtrando por
fecha + cliente + monto.

### 2.7 DecimalConSigno — formato exacto

```
+000000001500000.0000   ← $1,500,000
+000000000000000.0000   ← $0
-000000000050000.0000   ← -$50,000
```

21 caracteres SIEMPRE: `signo(1) + entero(15) + punto(1) + decimal(4)`

```python
def _decimal_con_signo(valor: Decimal) -> str:
    signo = "+" if valor >= 0 else "-"
    abs_valor = abs(valor)
    entero = int(abs_valor)
    decimal_part = abs_valor - entero
    decimal_4 = int(round(decimal_part * 10000))
    return f"{signo}{entero:015d}.{decimal_4:04d}"
```

---

## SECCION 3: CONECTOR 142882 — DOCUMENTO CONTABLE (POST)

Usado para registrar retenciones tributarias via asientos contables.

### 3.1 Endpoint

```
POST /api/siesa/v3/conectoresimportarestandar
  ?idCompania=8215
  &idDocumento=142882
  &nombreDocumento=API_v1_DocumentoContable
```

### 3.2 Estructura JSON — 5 secciones

```
Inicial → { F_CIA: 1 }
Documentocontable → Header (CO, tipo=NI, tercero, estado)
Movimientocontable → Asientos debito (1 por cada retencion: 1355xxxx)
MovimientoCxC → Cruces credito contra facturas (cierra CxC restante)
Final → { F_CIA: 1 }
```

### 3.3 Tipo de Documento para retenciones

**Odisea del tipo docto (4 intentos):**

| Intento | Tipo | Resultado | Error |
|---------|------|-----------|-------|
| 1 | NC (Nota Credito) | RECHAZADO | "No existe" en maestro |
| 2 | NI (Nota Interna) | RECHAZADO | Diferente esquema |
| 3 | NCE (Nota Credito Electronica) | RECHAZADO | No existe |
| 4 | CE (Comprobante Egreso) | RECHAZADO | No aplica |
| 5 | **NI** (Nota Interna Contabilidad) | **EXITOSO** | Requiere F350_ID_CLASE_DOCTO=30 |

**SOLUCION:** Tipo `NI` + Clase `30` (Documento contable). El NI es un asiento
contable generico que Siesa acepta para mover cuentas arbitrarias.

### 3.4 Campos OBLIGATORIOS que no son obvios

Estos campos no parecen obligatorios por nombre, pero 142882 los requiere:

| Campo | Donde | Valor | Error si falta |
|-------|-------|-------|----------------|
| F351_ID_UN | Movimientocontable | "99" | Rechazo silencioso |
| F351_ID_TERCERO | Movimientocontable | NIT del cliente | No cruza contra CxC |
| F353_FECHA_VCTO | MovimientoCxC | fecha del dia | Rechazo |
| F353_FECHA_DSCTO_PP | MovimientoCxC | fecha del dia | Rechazo |
| F354_TERCERO_VEND | MovimientoCxC | NIT del cliente | Rechazo |
| F351_BASE_GRAVABLE | Movimientocontable | Base real de calculo | Siesa lo requiere para 1355xxxx |
| Variantes _ALT | Ambas secciones | "+000...0000" | Campos duplicados obligatorios |

### 3.5 MovimientoCxC vs CxC de 142888 — diferencias criticas

| Aspecto | 142888 (RC - CxC) | 142882 (DC - MovimientoCxC) |
|---------|--------------------|-----------------------------|
| Tercero | NO tiene F351_ID_TERCERO | SI requiere F351_ID_TERCERO |
| Auxiliar | F353_ID_AUXILIAR_DOCTO_CRUCE | F351_ID_AUXILIAR |
| Campos _ALT | NO existen | SI obligatorios (VALOR_CR_ALT, etc.) |
| F353_FECHA_VCTO | NO existe | SI obligatorio |
| F354_TERCERO_VEND | NO existe | SI obligatorio |
| F353_ID_SUCURSAL | NO (usa F353_ID_SUCURSAL_DOCTO_CRUCE) | SI (F353_ID_SUCURSAL) |

**LECCION:** Son conectores DIFERENTES con esquemas DIFERENTES.
No copiar campos de uno a otro sin verificar el DOCX de cada conector.

---

## SECCION 4: ESTRATEGIA DE RETENCIONES — LA QUE FUNCIONA

### 4.1 Contexto de negocio

Papeleria Medellin es **VENDEDOR**. Sus clientes institucionales (hospitales,
alcaldias, colegios) **retienen** impuestos al pagar:

- **Retefuente** (2.5% sobre subtotal): cuenta 13551501
- **ReteIVA** (15% sobre IVA): cuenta 13551701
- **ICA** (4x1000 a 11x1000 sobre subtotal): cuentas 13551801-13551805

Las cuentas son grupo **1355** (activos: retenciones a favor), NO 2365 (pasivos).

### 4.2 Estrategia de dos documentos

```
FACTURA: $1,190,000 (subtotal $1,000,000 + IVA $190,000)

Retenciones:
  Retefuente 2.5%:  $1,000,000 × 0.025 = $25,000
  ReteIVA 15%:      $190,000 × 0.15   = $28,500
  ICA 4x1000:       $1,000,000 × 0.004 = $4,000
  Total retenciones: $57,500

PASO 1: RC (142888) por el NETO
  F357_VALOR_INGRESO = $1,132,500  (cash que entra a caja)
  F354_VALOR_CR = $1,132,500       (lo que se cruza contra la factura)
  F354_VALOR_RETENCION = 0         (142888 no soporta justificacion)

PASO 2: NI (142882) por las RETENCIONES
  Movimientocontable:
    DB 13551501 $25,000  (retefuente a favor)
    DB 13551701 $28,500  (reteIVA a favor)
    DB 13551801 $4,000   (ICA a favor)
  MovimientoCxC:
    CR 13050501 $57,500  (cierra CxC restante contra la factura)
```

**Por que NO usar F354_VALOR_RETENCION del 142888?**

Se intento primero. F354_VALOR_RETENCION existe en el conector pero Siesa
requiere justificacion interna (configuracion tributaria del tercero) para
auto-generar los asientos a 1355xxxx. Sin esa configuracion, el campo se acepta
pero no hace nada util — el dinero queda en limbo contable.

La estrategia de dos documentos es explicita: el RC registra el cash, el NI
registra los asientos contables. No depende de configuracion interna de Siesa.

### 4.3 Prorrateo de retenciones por cruce

Cuando se cruzan multiples facturas, las retenciones se distribuyen
proporcionalmente:

```python
# ret_i = total_ret × (cruce_i / total_cruces)
# Ultimo cruce absorbe diferencia de redondeo

retenciones_por_cruce = []
acumulado = 0
for i, cruce in enumerate(cruces):
    if i == len(cruces) - 1:
        retenciones_por_cruce.append(total_retencion - acumulado)
    else:
        proporcion = cruce.valor / total_cruces
        monto_ret = round(total_retencion * proporcion)
        retenciones_por_cruce.append(monto_ret)
        acumulado += monto_ret
```

### 4.4 Calculo automatico de bases

```python
# IVA_TASA = 0.19 (configurable via env var IVA_TASA)

subtotal = total_factura / (1 + IVA_TASA)      # Base para retefuente e ICA
iva = total_factura - subtotal                   # Base para reteIVA

# Ejemplo: factura $1,190,000
# subtotal = 1,190,000 / 1.19 = $1,000,000
# iva = 1,190,000 - 1,000,000 = $190,000
```

**MEJOR:** Usar API 45 (Ventas_Facturas_DesdePedido) para obtener subtotal e IVA
REALES de Siesa, no dividir por 1.19. La factura puede tener descuentos, items
exentos, o impuesto al consumo que cambian la proporcion.

---

## SECCION 5: AUTOPSIA DE ERRORES — CADA UNO CON SOLUCION

### ERROR 1: "La cuenta bancaria no existe" (142888)

**Mensaje:** `"La cuenta bancaria    no existe."` (con espacios = codigo vacio)

**5 intentos fallidos:**
1. Cajas virtuales con auxiliar 1120 → Siesa solo acepta 1105 en maestro de cajas
2. f358_id_cuentas_bancarias_cg en JSON → Conector lo ignora silenciosamente
3. F358_ID_BANCO + F358_NRO_CUENTA con valores → Solo para cheques/tarjetas
4. F358_ID_BANCO y NRO_CUENTA vacios → No ayuda, la cuenta no viene de ahi
5. F357_IND_VALIDA_MEDPAGO = 0 → Solo controla validacion medio vs caja

**SOLUCION:** Configurar "Cnta. bancaria" en Maestros asociados > Medios de pago
en Siesa. Campo grisado para efectivo, obligatorio para consignaciones.

| Medio | Cnta. bancaria |
|-------|----------------|
| TBA | 011 (Bancolombia Ahorro) |
| TBC | 002 (Bancolombia Corriente) |
| TBB | 005 (BBVA Corriente) |
| TBG | 004 (Bogota Corriente) |
| TAA | 008 (Agrario Ahorro) |
| TAC | 001 (Agrario Corriente) |
| TDV | 003 (Davivienda Corriente) |

**LECCION:** Si el error dice "no existe" con espacios, es configuracion de maestros,
no del payload.

### ERROR 2: RC duplicado fantasma (RC-00002744)

**Que paso:** El HTTP client reintentaba POST en timeout. Siesa proceso el primer
request pero respondio lento. El segundo intento creo un RC duplicado.

**SOLUCION:**
```python
def _post_with_retry(self, ...):
    # POST NUNCA reintenta en 5xx o timeout
    # SOLO reintenta en 429 (rate limit = request NO procesado)
    if status_code == 429:
        retry_after = response.headers.get("Retry-After", "1")
        sleep(int(retry_after))
        continue
    else:
        raise  # No retry
```

**LECCION:** Un POST a Siesa es una operacion de escritura. Si fallo con timeout,
NO sabes si se proceso o no. Reintentar = duplicar.

### ERROR 3: Registros fantasma con tamPag >= 500

**Que paso:** Con page_size=500, Connekta retorna un unico registro con todos
los campos NULL. Pydantic rechaza el registro, cache queda con 0 facturas.
La app parece vacia (bandeja sin datos, tesoreria sin datos).

**SOLUCION:** tamPag maximo = 100. Variable de entorno CONNI_PAGE_SIZE=100.

### ERROR 4: Tipo docto retenciones — 4 intentos

```
NC → "No existe en maestro" (no hay NC creado)
NI → rechazado (esquema diferente al esperado)
NCE → "No existe"
CE → "No aplica para esta operacion"
NI + F350_ID_CLASE_DOCTO=30 → EXITOSO
```

**LECCION:** El tipo de documento NO es solo un codigo. Cada tipo tiene un esquema
de campos obligatorios diferente. NI funciona para asientos contables genericos
cuando se le pone clase=30.

### ERROR 5: Cruce CxC no se aplicaba a la factura

**Sintoma:** El RC se creaba exitosamente (dinero en caja), pero el saldo de la
factura no bajaba. Multiples RCs contra la misma factura, saldo intacto.

**Investigacion que hicimos:**
- Revisamos TODOS los campos del payload vs DOCX: todo correcto
- Revisamos formato DecimalConSigno: correcto
- Revisamos PK documental (CO+tipo+consec+cuota): correcto
- Comparamos 142888 vs 142882 campo por campo: esquemas diferentes pero correctos

**Resultado del test diagnostico:**
- RC sin retenciones: **CRUCE SI APLICA** ✅
- RC con retenciones: **CRUCE SI APLICA** ✅
- El problema era combinacion de CACHE (30 min TTL) + timing de consulta

**LECCION:**
1. Despues de crear un RC, INVALIDAR el cache inmediatamente
2. Siesa tarda ~10-12 segundos en procesar un RC. Si consultas antes, ves datos viejos
3. Construir endpoints de diagnostico que comparan CxC antes/despues del POST
4. La respuesta "Importacion exitosa" NO significa que puedas consultar inmediatamente

### ERROR 6: F353_ID_AUXILIAR_DOCTO_CRUCE hardcodeado

**Que paso:** Se hardcodeo "13050501" como cuenta CxC. Si la factura estaba bajo
"13050502" u otra 1305xxxx, Siesa aceptaba el RC (dinero entra a caja) pero el
cruce CxC NO se aplicaba contra la factura.

**SOLUCION:** Siempre propagar f253_id real desde la consulta API 20 hasta el payload.

```python
# MAL:
cuenta_cxc = "13050501"  # hardcodeado

# BIEN:
cuenta_cxc = factura.f253_id.strip()  # del API CxC_General
```

### ERROR 7: F351_ID_UN faltante en 142882

**Que paso:** El Movimientocontable del DocumentoContable no incluia F351_ID_UN.
Siesa rechazaba silenciosamente el asiento.

**SOLUCION:** Agregar `"F351_ID_UN": "99"` (o la UN real del cruce).

### ERROR 8: F354_TERCERO_VEND faltante en 142882

**Que paso:** MovimientoCxC del 142882 no incluia F354_TERCERO_VEND. Siesa
rechazaba el cruce CxC de retenciones.

**SOLUCION:** `"F354_TERCERO_VEND": solicitud.cliente_id`

### ERROR 9: F353_FECHA_VCTO y F353_FECHA_DSCTO_PP faltantes en 142882

**Que paso:** MovimientoCxC del 142882 requiere fecha vencimiento y fecha
descuento pronto pago. Sin ellos, Siesa rechaza.

**SOLUCION:** Usar la fecha del dia para ambos:
```python
"F353_FECHA_VCTO": fecha_str,      # "YYYYMMDD"
"F353_FECHA_DSCTO_PP": fecha_str,
```

### ERROR 10: F351_BASE_GRAVABLE faltante o en cero

**Que paso:** Los asientos de retenciones en 1355xxxx requieren la base gravable
real. Si se envia 0, el asiento se crea pero sin trazabilidad fiscal.

**SOLUCION:** Calcular y enviar la base real:
- Retefuente/ICA: subtotal (total / 1.19)
- ReteIVA: IVA (total - subtotal)
- Mejor: usar API 45 para obtener vlr_bruto y vlr_imp reales

### ERROR 11: API 39 para desglose fiscal

**Que paso:** Se intento usar API 39 (MovtosContables) para extraer IVA de facturas
escaneando cuentas 24xx. Falla cuando hay anticipos, descuentos, o impuestos especiales.

**SOLUCION:** Usar API 45 (Ventas_Facturas_DesdePedido):
- `f461_vlr_neto` = total factura
- `f461_vlr_imp` = IVA exacto
- `f461_vlr_bruto` = valor sin impuestos
- Filtrar: `f350_id_tipo_docto = ''FE'' AND f350_consec_docto = 5020`

### ERROR 12: Toast "[object Object]" en frontend

**Que paso:** El frontend hacia `alert(error)` donde error era un objeto JSON.

**SOLUCION:** `JSON.stringify(error)` o extraer el campo `.mensaje` del error.

### ERROR 13: Doble JSON.stringify en fetch

**Que paso:** El body del fetch ya era un objeto pero se hacia JSON.stringify
dos veces, resultando en un string escapado que el backend rechazaba con 422.

**SOLUCION:** JSON.stringify una sola vez en el fetch.

---

## SECCION 6: APIs SIESA — CATALOGO COMPLETO

### 6.1 APIs de consulta (GET)

| API | Nombre | Tabla | Proposito | Clave respuesta |
|-----|--------|-------|-----------|-----------------|
| 20 | API_v2_CxC_General | T353 | Cuentas por cobrar | Table |
| 21 | API_v2_CxC_Recibos | T350/T357 | Recibos de caja | Table |
| 22 | API_v2_CxP_General | T353 | Cuentas por pagar | Table |
| 1 | API_v2_Auxiliares | — | Plan de cuentas PUC | Table |
| 39 | API_v2_MovtosContables_General | T350/T351 | Movimientos contables | Table |
| 26 | API_v2_Inventarios_InvFecha | T400 | Inventarios | Table |
| 27 | API_v2_Items | T120 | Maestro items | Table |
| 45 | API_v2_Ventas_Facturas_DesdePedido | T461 | Facturas de venta | Table |
| Custom | papeleriamedellin_API_custom_TercerosContacto | T200+T015 | Terceros | **Datos** |

### 6.2 APIs de escritura (POST)

| Conector | Nombre | Proposito |
|----------|--------|-----------|
| 142888 | API_v1_ReciboCaja | Crear Recibos de Caja |
| 142882 | API_v1_DocumentoContable | Crear asientos contables |

### 6.3 Campos clave API 20 (CxC_General)

| Campo Siesa | Que es | Critico para |
|-------------|--------|--------------|
| f200_id | NIT/cedula | Identificar cliente |
| f353_id_co_cruce | CO de la factura | PK documental |
| f353_id_tipo_docto_cruce | Tipo (FE, FV) | PK documental |
| f353_consec_docto_cruce | Consecutivo | PK documental |
| f353_nro_cuota_cruce | Cuota (0=unica) | PK documental |
| f253_id | Cuenta PUC (13050501) | F353_ID_AUXILIAR_DOCTO_CRUCE |
| f353_id_un_cruce | Unidad negocio | F353_ID_UN_DOCTO_CRUCE |
| f201_id_sucursal | Sucursal | F353_ID_SUCURSAL_DOCTO_CRUCE |
| f353_total_db | Debito total | Saldo = db - cr |
| f353_total_cr | Credito total | Saldo = db - cr |
| f353_fecha_cancelacion | Null=vigente | Filtro facturas abiertas |
| f353_fecha_vcto | Vencimiento | Calculo mora |

**REGLA:** Cada campo de la factura en API 20 debe propagarse EXACTO al payload
POST. No asumir defaults. No hardcodear.

### 6.4 Que API usar para que

| Necesidad | API correcta | API incorrecta |
|-----------|-------------|----------------|
| Facturas pendientes | API 20 (CxC_General) | — |
| Buscar pagos recibidos | API 21 (CxC_Recibos) | API 39 (sabana) |
| Saldos bancarios | API 39 (MovtosContables) | — |
| Ventas POS | API 39 (solo FE en 11050599) | API 39 (todo 11xx = inflacion 3x) |
| IVA/subtotal de factura | API 45 (Ventas_Facturas) | API 39 (ingenieria inversa) |
| Inventario | API 26 + API 27 | — |
| Datos de contacto | Custom (TercerosContacto) | — |

---

## SECCION 7: CONFIGURACION DE MAESTROS EN SIESA

### 7.1 Lo que se configura en Siesa, NO en el codigo

| Maestro | Que configurar | Error si falta |
|---------|---------------|----------------|
| Medios de pago | "Cnta. bancaria" por cada medio tipo Consignacion | "La cuenta bancaria no existe" |
| Cobrador | Crear tercero tipo Cobrador+Vendedor | Rechazo de RC |
| Cajas | Verificar que existe la caja en el CO | "La caja no existe" |
| Tipos documento | NI con clase 30 habilitado | Rechazo de DocumentoContable |
| Cuentas PUC | 1355xxxx con auxiliares creados | Rechazo de asientos |

### 7.2 Mapa CO → Caja

| CO | Caja | Sede |
|----|------|------|
| 001 | 001 | Neiva Sur |
| 002 | 004 | Neiva Centro |
| 003 | 999 | Bodega CD |
| 004 | 999 | Pitalito Centro |
| 005 | 999 | Pitalito Terminal |
| 006 | 013 | Florencia Centro |
| 007-009 | 999 | Ferias |

Caja 999 = CAJA GENERAL. Existe en todos los COs como fallback.

### 7.3 Medios de pago

| Codigo | Tipo Siesa | Campos extra requeridos |
|--------|-----------|------------------------|
| EFE | Efectivo | Ninguno |
| TBA-TDV | Consignaciones | referencia + fecha + "CG" |
| TDC | Tarjeta | Ninguno |

---

## SECCION 8: PATRON DE CODIGO QUE FUNCIONA

### 8.1 Arquitectura hexagonal

```
dominio/recaudo/
  modelo.py          → SolicitudRecaudo, CruceFactura, MedioPago, LineaRetencion
  servicio_recaudo.py → Valida solicitud, delega a puerto

infraestructura/siesa/
  connekta_client.py  → HTTP client (GET paginado, POST no-retry)
  recaudo_payload.py  → Builder JSON 142888 (5 secciones)
  retencion_payload.py → Builder JSON 142882 (5 secciones)
  recaudo_adapter.py  → Orquesta: RC → NI (si retenciones)
  dto.py + mapper.py  → ACL: Siesa → dominio

infraestructura/api/
  recaudo_routes.py   → Endpoints REST
```

### 8.2 Pipeline de datos para el cruce CxC

```
API 20 (CxC_General) → SiesaCxCRow (DTO) → mapper → FacturaPorCobrar (dominio)
                                                        ↓
                         Frontend lee: co, tipo_docto, consecutivo, cuota,
                                       sucursal, unidad_negocio, cuenta_cxc
                                                        ↓
                         POST /recaudo → CruceFactura → build_payload → Siesa
```

**CADA CAMPO del cruce viene de API 20, pasa por el DTO, por el mapper,
por el dominio, por el frontend, y llega al payload. NADA se inventa.**

### 8.3 Client HTTP — patron seguro

```python
class ConnektaClient:
    def execute_query(self, api_name, filtro, page_size=100):
        """GET paginado. Reintenta en 429 y 5xx."""
        # Paginacion: para si pagina retorna < page_size registros
        # Max 100 paginas (abort safety)
        # Timeout: 30s por request
        
    def execute_import(self, id_doc, nombre_doc, payload):
        """POST sin retry. Solo reintenta en 429."""
        # 429 → retry con Retry-After
        # 5xx → raise inmediato
        # timeout → raise inmediato
        # NUNCA reintentar escrituras
```

### 8.4 Endpoint diagnostico — patron recomendado

Para validar que los cruces CxC funcionan:

```python
@app.post("/diagnostico/rc-payload-test")
def diagnostico(body):
    # 1. Consultar CxC del cliente ANTES
    cxc_antes = fetch_cxc(cliente_id)
    
    # 2. Generar payload y POST a Siesa
    payload = build_payload(solicitud, hoy)
    response = client.execute_import(142888, "API_v1_ReciboCaja", payload)
    
    # 3. Esperar 2s (Siesa procesa)
    sleep(2)
    
    # 4. Consultar CxC DESPUES
    cxc_despues = fetch_cxc(cliente_id)
    
    # 5. Comparar saldos
    return {
        "cruce_aplico": len(cambios) > 0,
        "payload_enviado": payload,
        "respuesta_siesa": response,
        "cxc_antes": cxc_antes,
        "cxc_despues": cxc_despues,
    }
```

---

## SECCION 9: VARIABLES DE ENTORNO

### Connekta

| Variable | Default | Descripcion |
|----------|---------|-------------|
| CONNI_BASE_URL | (requerido) | QA: serviciosqa.siesacloud.com |
| CONNI_KEY | (requerido) | API key estatica |
| CONNI_TOKEN | (requerido) | JWT estatico |
| CONNI_ID_COMPANIA | 8215 | ID tenant (NO es F_CIA) |
| CONNI_PAGE_SIZE | 100 | NUNCA > 100 |
| CONNI_TIMEOUT | 30 | Segundos por request |

### Recaudo

| Variable | Default | Descripcion |
|----------|---------|-------------|
| SIESA_RC_CIA | 1 | F_CIA (NO 8215) |
| SIESA_RC_COBRADOR | "9876" | Cobrador APP RECAUDO |
| SIESA_RC_FE | "1103" | Flujo de efectivo |
| SIESA_CAJA_001..009 | Ver mapa | Caja por CO |
| SIESA_RET_TIPO_DOCTO | "NI" | Tipo docto retenciones |
| IVA_TASA | "0.19" | Tasa IVA para calculo base |

---

## SECCION 10: CHECKLIST PARA NUEVA INTEGRACION (WMS)

Antes de escribir una linea de codigo en el WMS:

- [ ] Obtener el DOCX del conector que vas a usar (142888, 142882, u otro)
- [ ] Leer CADA campo, tipo, y si es obligatorio/condicional
- [ ] Verificar que los maestros estan configurados (cajas, medios, cobrador, cuentas PUC)
- [ ] Crear un cobrador dedicado para el WMS (separar de APP RECAUDO)
- [ ] Probar en QA (serviciosqa) antes de produccion
- [ ] Implementar NO-RETRY en POST desde el dia 1
- [ ] Implementar endpoint de diagnostico (before/after CxC) desde el dia 1
- [ ] Implementar invalidacion de cache despues de cada POST exitoso
- [ ] Loggear el payload completo a nivel INFO (no DEBUG) para Railway/produccion
- [ ] Loggear la respuesta completa de Siesa (incluir en respuesta API al frontend)
- [ ] Usar page_size=100 maximo para GETs
- [ ] Solo operar en horario laboral (~8 AM a ~8 PM)
- [ ] Propagar TODOS los campos de la factura (API 20) al payload, NADA hardcodeado
- [ ] Para retenciones: estrategia de 2 documentos (RC neto + NI retenciones)
- [ ] Para IVA/subtotal: usar API 45, NO dividir por 1.19

---

## SECCION 11: CUENTAS PUC DE RETENCIONES

### Retefuente (a favor = activo 1355)
| PUC | Descripcion | Tasa | Base |
|-----|-------------|------|------|
| 13551501 | Retencion por compras | 2.5% | Subtotal |
| 13551502 | Retencion bancos | 1.5% | Subtotal |

### ReteIVA
| PUC | Descripcion | Tasa | Base |
|-----|-------------|------|------|
| 13551701 | ReteIVA ventas | 15% | IVA |

### ICA Retenido a Favor
| PUC | Descripcion | Tasa | Base |
|-----|-------------|------|------|
| 13551801 | ICA 4x1000 | 0.4% | Subtotal |
| 13551802 | ICA 3x1000 | 0.3% | Subtotal |
| 13551803 | ICA 6x1000 | 0.6% | Subtotal |
| 13551804 | ICA 10x1000 | 1.0% | Subtotal |
| 13551805 | ICA 11x1000 | 1.1% | Subtotal |

---

*Documento generado: 20 julio 2026. Papeleria Medellin — Gestor de Cartera PAME.*
*Fuente: 40+ dias de integracion, 60+ commits, 874 tests, docenas de errores en produccion.*
*Uso: referencia para WMS-PAME y cualquier sistema que integre con Siesa Enterprise.*

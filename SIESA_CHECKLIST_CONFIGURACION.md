# Checklist de Configuración Siesa Enterprise — WMS Papelería Medellín

> Documento de referencia para el consultor Siesa al momento de replicar la
> configuración en el ambiente de producción real.
> Generado: 2026-04-08

---

## 1. Bodega de Tránsito

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 1 | Bodega de tránsito logístico | Maestros → Inventarios → Bodegas | Código: `TRA1` | ☐ Pendiente |

**Notas:**
- Esta bodega es el "limbo contable" entre la bodega principal y el punto de venta.
- El inventario entra aquí cuando el camión sale (173076) y sale cuando la tienda confirma recepción (173079).
- No debe tener ubicaciones físicas — es solo contable.
- Variable Railway asociada: `SIESA_BODEGA_TRANSITO=TRA1`

---

## 2. Tipos de Documento

### 2.1 Requisición de Traslado — Conector 174646

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 2 | Tipo de documento Requisición Traslado | Maestros → Documentos → Tipos de documentos | Código: `TRA`, Familia: Inventarios, Clase: 75 (Requisiciones para transferir) | ☐ Verificar / Crear |

**Notas:**
- Pestaña Consecutivos: marcar **Automático**.
- Pestaña Autorización por Origen: habilitar `Inventarios → Movimientos → Requisiciones para transferir`.
- Este documento se crea automáticamente cuando el admin bodega **aprueba** una solicitud de traslado.
- Variable Railway: `SIESA_TIPO_DOCTO_TRASLADO=TRA`

---

### 2.2 Salida en Tránsito — Conector 173076

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 3 | Tipo de documento Salida en Tránsito | Maestros → Documentos → Tipos de documentos | Código: `STS`, Familia: Inventarios | ☐ Pendiente |

**Notas:**
- Pestaña Consecutivos: marcar **Automático** (obligatorio — el WMS no envía número manual).
- Pestaña Autorización por Origen: habilitar `Inventarios → Movimientos → Transferencias → Salida en tránsito`.
- Este documento se crea automáticamente cuando el empacador presiona **Despachar** en el WMS.
- Mueve inventario: Bodega principal (NB1) → Bodega tránsito (TRA1).
- Variable Railway: `SIESA_TIPO_DOCTO_TRANSITO_SALIDA=STS`

---

### 2.3 Entrada en Tránsito — Conector 173079

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 4 | Tipo de documento Entrada en Tránsito | Maestros → Documentos → Tipos de documentos | Código: `ETS`, Familia: Inventarios | ☐ Pendiente |

**Notas:**
- Pestaña Consecutivos: marcar **Automático**.
- Pestaña Autorización por Origen: habilitar `Inventarios → Movimientos → Transferencias → Entrada en tránsito`.
- Este documento se crea cuando el **administrador de la tienda confirma recepción** en el WMS.
- Mueve inventario: Bodega tránsito (TRA1) → Bodega punto de venta destino.
- Referencia cruzada obligatoria: el conector 173079 apunta al consecutivo del documento STS correspondiente.
- Variable Railway: `SIESA_TIPO_DOCTO_TRANSITO_ENTRADA=ETS`

---

### 2.4 Transferencia Directa — Conector 173066 (Contingencia)

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 5 | Tipo de documento Transferencia Directa | Maestros → Documentos → Tipos de documentos | Código: `TRA` (reutiliza el mismo), Familia: Inventarios | ☐ Verificar autorización |

**Notas:**
- Solo se usa cuando `SIESA_BODEGA_TRANSITO` está vacío (modo contingencia sin bodega tránsito).
- Con el flujo EN_TRANSITO activo, este conector **no se dispara**.
- Mueve inventario directamente NB1 → Bodega tienda (sin paso por TRA1).
- **Riesgo:** el inventario aparece en la tienda antes de que el camión llegue físicamente.

---

### 2.5 Factura Electrónica — Conector 238925

| # | Qué crear | Ruta en Siesa | Valor | Estado |
|---|---|---|---|---|
| 6 | Tipo de documento Factura Electrónica | Ventas → Documentos → Tipos de documentos | Código: `FE` | ☐ Verificar |

**Notas:**
- Se dispara automáticamente cuando el empacador **cierra el packing** (declara los bultos físicos).
- Genera la factura FEW + remisión automáticamente en Siesa.
- Variable Railway: `SIESA_TIPO_DOCTO_FACTURA=FEW`

---

## 3. Parámetros de Inventario

| # | Qué configurar | Ruta en Siesa | Valor | Variable Railway | Estado |
|---|---|---|---|---|---|
| 7 | Solicitante para requisiciones | Inventarios → Solicitantes | Código: `001` (o el que corresponda) | `SIESA_REQ_SOLICITANTE=001` | ☐ Verificar |
| 8 | Motivo de traslado | Inventarios → Motivos de movimiento | Código: `01` | `SIESA_MOTIVO_TRASLADO=01` | ☐ Verificar |

---

## 4. Parámetros de Ventas

| # | Qué configurar | Ruta en Siesa | Variable Railway | Estado |
|---|---|---|---|---|
| 9 | Motivo de ventas | Ventas → Motivos | `SIESA_ID_MOTIVO_VENTAS=XX` | ☐ Verificar |
| 10 | Lista de precio activa | Ventas → Listas de precio | `SIESA_LISTA_PRECIO=XXX` | ☐ Verificar |

---

## 5. Tabla de Mapeo Unidad de Negocio (WMS — no es Siesa)

> Esta configuración vive en el WMS, no en Siesa. Se administra desde
> `GET/POST /api/config/mapeo-unidades` o directamente en la DB.

Una vez el sync nocturno corra y aparezcan los `f120_id_tipo_inv_serv` reales,
insertar una fila por cada tipo de inventario:

```sql
INSERT INTO siesa_mapeo_unidades (tipo_inv_siesa, unidad_negocio_id, descripcion)
VALUES
  ('TIPO_REAL_1', '001', 'Papelería'),
  ('TIPO_REAL_2', '002', 'Tecnología'),
  ('TIPO_REAL_3', '011', 'Hogar'),
  ('TIPO_REAL_4', '014', 'Impresión');
  -- completar con los tipos reales que devuelva API_v2_Items
```

Códigos de Unidad de Negocio Siesa:
`001`=PAPELERIA · `002`=TECNOLOGIA · `003`=ARTE · `004`=ASEO INSTITUCIONAL ·
`005`=BELLEZA · `006`=FARMACIA · `007`=DESECHABLES · `008`=JUGUETERIA ·
`009`=FIESTA · `010`=NAVIDAD · `011`=HOGAR · `012`=MASCOTAS ·
`013`=FERRETERIA · `014`=IMPRESION · `99`=ADMON

---

## 6. Variables de Entorno Railway — Resumen Completo

```env
# Connekta
CONNEKTA_IKEY=...
CONNEKTA_ITOKEN=...
CONNEKTA_ID_COMPANIA=8215
CONNEKTA_BODEGA=NB1
CONNEKTA_CENTRO_OP=003
CONNEKTA_URL=https://servicios.siesacloud.com   # producción (sin 'qa')

# Siesa — Documentos
SIESA_ID_CIA=1
SIESA_TIPO_DOCTO_FACTURA=FEW
SIESA_TIPO_DOCTO_TRASLADO=TRA
SIESA_TIPO_DOCTO_TRANSITO_SALIDA=STS
SIESA_TIPO_DOCTO_TRANSITO_ENTRADA=ETS

# Siesa — Parámetros
SIESA_REQ_SOLICITANTE=001
SIESA_MOTIVO_TRASLADO=01
SIESA_ID_MOTIVO_VENTAS=XX
SIESA_LISTA_PRECIO=XXX
SIESA_BODEGA_TRANSITO=TRA1
SIESA_BODEGA_AVERIAS=AV1

# NO configurar en producción real:
# MODO_ENSAYO=true   ← solo para pruebas UX sin tocar Siesa real
```

---

## 7. Orden de Configuración Recomendado

1. ☐ Crear bodega `TRA1`
2. ☐ Crear tipo doc `TRA` (o verificar que exista con clase 75 y autorización 174646)
3. ☐ Crear tipo doc `STS` con autorización 173076
4. ☐ Crear tipo doc `ETS` con autorización 173079
5. ☐ Verificar tipo doc `FE` con autorización 238925
6. ☐ Verificar solicitante `001` y motivo `01`
7. ☐ Configurar las variables de entorno en Railway (ambiente producción)
8. ☐ Correr sync nocturno manual (`POST /api/siesa/sync`)
9. ☐ Poblar tabla `siesa_mapeo_unidades` con los tipos de inventario reales
10. ☐ Crear traslado de prueba end-to-end con 1 producto

---

*WMS Papelería Medellín — Generado automáticamente desde el código fuente*

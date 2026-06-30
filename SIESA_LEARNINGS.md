# SIESA_LEARNINGS.md
## Aprendizajes técnicos reales de la integración WMS ↔ Siesa Enterprise vía Connekta V2

> Generado el 2026-05-11 auditando el historial completo de commits, diffs y código fuente del repositorio.
> Cada hallazgo tiene referencia al commit donde se resolvió y al archivo donde vive la solución.

---

## CATEGORÍA 1: FORMATO DE DATOS (campos con tipo o formato incorrecto)

---

### [SIESA-001] F_CIA debe ser `int`, no string

- **Categoría**: Formato de datos
- **Síntoma observado**: Rechazo de Siesa en conectores 142945, 142948, 142951, 173076, 173079, 142943. El error no era siempre explícito; a veces Siesa aceptaba el documento pero lo procesaba incorrectamente.
- **Causa raíz**: `id_cia_siesa` viene de `os.getenv()` como string. La spec oficial de todos los conectores exige `F_CIA` como **entero**. El serializador .NET de Connekta no convierte automáticamente.
- **Solución implementada**: Convertir con `cia = int(self.id_cia_siesa)` antes de construir el payload y usar `cia` en todos los bloques `Inicial`, `Remision`, `Movtoventascomercial`, `Documentos`, `Movimientos`, `Final`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` (toda la clase)
- **Commit**: `473c822` — *"fix: 6 CRÍTICOS — F_CIA int, nro_registro enumerate…"*
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-002] `f470_nro_registro` debe ser secuencial (1, 2, 3…), no 0 en todas las líneas

- **Categoría**: Formato de datos
- **Síntoma observado**: Despachos con múltiples ítems eran rechazados o Siesa solo procesaba el primer ítem. Con `nro_registro=0` en todos los registros, Siesa colisiona el índice de movimientos.
- **Causa raíz**: El código original asignaba `f470_nro_registro: 0` sin iterar. La spec exige `1, 2, 3…` por línea de movimiento.
- **Solución implementada**: Cambiar el list comprehension de `for i in items` a `for idx, i in enumerate(items)` y usar `f470_nro_registro: idx + 1`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — funciones `trigger_despacho`, `trigger_transito_salida`, `trigger_transito_entrada`, `transferencia_directa`
- **Commit**: `473c822`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-003] `F350_IND_ESTADO` omitido o en 0 deja documentos en "Borrador" sin contabilizar

- **Categoría**: Formato de datos / Comportamiento inesperado
- **Síntoma observado (142945 — RemisionPedido)**: La remisión se creaba en Siesa pero NO descargaba inventario (cuenta 14 sin crédito, cuenta 61 sin débito). El pedido seguía figurando pendiente en Siesa, permitiendo re-despachos duplicados. Descuadre contable acumulativo.
- **Síntoma observado (238925 — FacturaPedido)**: La factura quedaba en estado "En elaboración" (0) y no se contabilizaba automáticamente.
- **Causa raíz**: El campo `F350_IND_ESTADO` no se enviaba en 142945 (defecto Siesa = 0 = Borrador) y tampoco en 238925.
- **Solución implementada**:
  - `trigger_despacho` (142945): `'F350_IND_ESTADO': 1`
  - `trigger_factura` (238925): `'F350_IND_ESTADO': 1`
  - Todos los demás conectores ya usaban 1 correctamente.
- **Confirmado por**: Consultor Siesa — estado=1 aprueba y contabiliza automáticamente en el mismo request.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_despacho` y `trigger_factura`
- **Commits**: `de4067e` — *"fix(CRÍTICO): trigger_despacho F350_IND_ESTADO 0→1"* / `b97affe` — *"fix(factura): agregar F350_IND_ESTADO=1 en payload trigger_factura"*
- **Nivel de riesgo si se ignora**: **Alto** (impacto contable directo, inventario no descargado)

---

### [SIESA-004] `f470_ind_naturaleza`: 1 = Entrada/Devolución, 2 = Salida/Venta

- **Categoría**: Formato de datos
- **Síntoma observado**: Las remisiones de venta se creaban con naturaleza "Devolución/Entrada" en lugar de "Venta/Salida". Siesa los registraba en sentido inverso al esperado.
- **Causa raíz**: El código enviaba `f470_ind_naturaleza: 1` asumiendo que 1 = salida. La spec oficial 142945 dice: **1 = Entrada/Devolución, 2 = Salida/Venta**.
- **Solución implementada**: `'f470_ind_naturaleza': 2` en `trigger_despacho` (142945).
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_despacho`
- **Commit**: `8598d56` — *"fix: auditoría completa gateway vs 7 specs oficiales Connekta"*
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-005] `f470_desc_varible`: el spec tiene un typo — es **'varible'** no 'variable'

- **Categoría**: Formato de datos / Comportamiento inesperado
- **Síntoma observado**: El registro del conector 173076 (TransitoSalida) se truncaba en posición 487 (702 chars en vez de 2700+). Error de Siesa: *"Tamaño del registro no corresponde al exigido"*.
- **Causa raíz**: El serializador de Connekta construye un **flat file posicional** usando el nombre exacto del campo tal como aparece en el `.docx` oficial. El spec dice `f470_desc_varible` (sin 'a'). El código enviaba `f470_desc_variable` — el serializador no encontraba el campo y truncaba el registro.
- **Solución implementada**: Renombrar el campo a `'f470_desc_varible'` (respetando el typo del spec) en los payloads de 173076 y 173079. Comentado explícitamente para prevenir "correcciones" futuras.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_transito_salida` y `trigger_transito_entrada`
- **Commit**: `43496c1` — *"fix 173076/173079: f470_desc_varible typo + f470_rowid_movto faltante"*
- **Nivel de riesgo si se ignora**: **Alto** (el conector completo falla silenciosamente)

---

### [SIESA-006] `f421_fecha_entrega` exige YYYYMMDD sin guiones (tamaño = 8)

- **Categoría**: Formato de datos
- **Síntoma observado**: Error explícito de Siesa en conector 142948: *"El campo supera el tamaño permitido (8)"*. Afectaba el segundo ítem de la RecepcionMercancia #23 (job ENTRADA_OC id=1).
- **Causa raíz**: La función `_fmt_fecha_iso` devolvía `YYYY-MM-DD` (10 chars con guiones). El campo `f421_fecha_entrega` en 142948 tiene `tamaño=8` y admite únicamente dígitos.
- **Solución implementada**: `_fmt_fecha_iso` ahora devuelve la misma lógica que `_fmt_fecha`: solo dígitos, exactamente 8 chars (`solo_digitos[:8]`). El nombre `_fmt_fecha_iso` es un legado histórico — en la práctica hace lo mismo que `_fmt_fecha`.
- **Nota adicional**: La fecha `f350_fecha` de los demás conectores también es YYYYMMDD — usar siempre `datetime.utcnow().strftime('%Y%m%d')`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `_fmt_fecha_iso`, `confirmar_entrada_compras`
- **Commit**: `5722305` — *"fix: f421_fecha_entrega debe ser YYYYMMDD sin guiones (tamaño=8)"*
- **Nivel de riesgo si se ignora**: **Medio** (solo afecta ítems con fecha de entrega no nula en OC)

---

### [SIESA-007] Campos `f462_*` (envío) aceptan `None` en Python pero el .NET serializer de Connekta falla

- **Categoría**: Comportamiento inesperado / Limitación de API
- **Síntoma observado**: Connekta retornaba error al intentar castear `None` a tipo decimal para los campos de logística de transporte (`f462_cajas`, `f462_peso`, `f462_volumen`, `f462_valor_seguros`).
- **Causa raíz**: El serializador .NET de Connekta no tolera `null` JSON en campos que internamente son `decimal`. Solo el conector 142948 usaba correctamente `0/0.0`.
- **Solución implementada**: Cambiar `None` → `0` / `0.0` en los cuatro campos para 142945, 142951, 173076, 173079, 142943. El valor lógico es igual (ningún flete), pero el serializer lo acepta.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — todos los conectores POST
- **Commit**: `22f05a5` — *"fix Connekta: f462_ decimales None→0, eliminar f451_id_sucursal_comprador fantasma"*
- **Nivel de riesgo si se ignora**: **Alto** (el conector falla completamente)

---

### [SIESA-008] `f451_id_sucursal_comprador` no existe en el spec 142948 — campo fantasma

- **Categoría**: Limitación de API
- **Síntoma observado**: El campo fue agregado por deducción lógica pero no existe en el spec oficial del conector 142948. Siesa lo ignoraba o rechazaba según la versión.
- **Causa raíz**: Se asumió la existencia de un campo de sucursal del comprador simétrico al de proveedor. El spec 142948 no lo incluye — Siesa infiere el comprador vía `f350_id_co` + `f470_id_bodega`.
- **Solución implementada**: Eliminar `f451_id_sucursal_comprador` del payload, los parámetros de `confirmar_entrada_compras` y del payload persistido en `SiesaJob`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py`, `app/services/recepcion_service.py`, `app/services/siesa_job_service.py`
- **Commit**: `22f05a5`
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-009] `f350_id_clase_docto` debe ser un entero específico en 142951, no string vacío

- **Categoría**: Formato de datos
- **Síntoma observado**: Ajustes de inventario (142951) y transferencias a averías enviados con `f350_id_clase_docto: ''` — Siesa rechazaba silenciosamente o creaba documentos de clase incorrecta.
- **Causa raíz**: El campo es obligatorio como entero. La spec confirma: **66 = Ajustes de Inventario** (DocumentoInv/AFI), **67 = Transferencias** (AveríaDocInv). Un string vacío no es equivalente.
- **Corrección**: Clase 63 (documentada inicialmente) fue incorrecta para tipo AFI en PAME — Siesa respondió HTTP 400 "El tipo de documento no esta autorizado para moverse en la clase de importación". El consultor Siesa confirmó que debe ser **66**.
- **Solución implementada**: `'f350_id_clase_docto': 66` en ajustes (AFI) y `'f350_id_clase_docto': 67` en transferencias a averías.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `enviar_ajuste_inventario` y `enviar_averia_inventario`
- **Commit**: `8598d56` (inicial) → corregido en conteo cíclico e2e
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-010] `f470_id_ubicacion_aux`, `f470_id_lote` y similares: `''` vacío vs `None`

- **Categoría**: Formato de datos
- **Síntoma observado**: Siesa rechazaba los conectores 142951 (ajustes) cuando se enviaba `''` (string vacío) en campos de ubicación y lote.
- **Causa raíz**: La spec de Connekta diferencia entre `null` (campo ausente/ignorado) y `''` (string vacío = valor inválido para campos de tipo código). Los campos `f470_id_ubicacion_aux`, `f470_id_lote`, `f470_id_ubicacion_aux_ent`, `f470_id_lote_ent` deben ser `null` cuando no aplican.
- **Solución implementada**: Reemplazar `''` por `None` en todos los campos de código opcionales.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `enviar_ajuste_inventario`
- **Commit**: `473c822`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-011] Sucursal proveedor debe tener exactamente 3 chars con `zfill(3)`

- **Categoría**: Formato de datos
- **Síntoma observado**: Siesa rechazaba entradas de OC (142948) cuando la sucursal del proveedor venía como `'1'` en vez de `'001'`.
- **Causa raíz**: El campo `f451_id_sucursal_prov` (posición 324-327, ancho 3) exige exactamente 3 caracteres. Las OCs de Siesa almacenan la sucursal como entero y al volver por la API puede llegar como `'1'`.
- **Solución implementada**: `sucursal_prov_fmt = sucursal_prov.strip().zfill(3)` antes de construir el payload. Validación previa que lanza `ValueError` si `sucursal_prov` llega vacía.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `confirmar_entrada_compras`
- **Commit**: `143ebf7` — *"fix: 3 guards críticos en 142948 y anti-duplicado FE"*
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-012] `f441_id_unidad_medida` es obligatorio en 174646 — `None` causa rechazo

- **Categoría**: Formato de datos
- **Síntoma observado**: Connekta rechazaba la requisición de traslado (174646) cuando la UOM del ítem no venía en el API de OCs.
- **Causa raíz**: El campo es obligatorio según spec 174646. `None` no tiene fallback en el serializador.
- **Solución implementada**: `'f441_id_unidad_medida': item.get('unidad_medida') or self.uom_default` — fallback a `SIESA_UOM_DEFAULT` (defecto: `'UND'`).
- **Confirmado por**: Consultor Siesa.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_requisicion_traslado`
- **Commit**: `4bc0490` — *"fix: 5 issues validados con consultor Siesa"*
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-013] `f470_rowid_movto` faltante en 173079 (TransitoEntrada)

- **Categoría**: Formato de datos
- **Síntoma observado**: El conector 173079 fallaba o creaba documentos con estructura incompleta.
- **Causa raíz**: El campo `f470_rowid_movto` existía en 173076 pero no se había incluido en el payload de 173079.
- **Solución implementada**: Agregar `'f470_rowid_movto': 0` al bloque de Movimientos de 173079.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_transito_entrada`
- **Commit**: `43496c1`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-014] Payloads 173066 y 173079 necesitan ~10-11 campos "Dep" adicionales

- **Categoría**: Formato de datos / Limitación de API
- **Síntoma observado**: Posibles rechazos de Siesa por estructura incompleta. En producción no siempre genera error explícito, pero el documento puede quedar mal formado.
- **Causa raíz**: Los conectores de traslado tenían payloads mínimos sin los campos opcionales marcados como "Dep" en el spec (`f470_id_ubicacion_aux`, `f470_id_lote`, `f470_cant_2`, `f470_costo_prom_uni`, `f470_id_item`, `f470_codigo_barras`, `f470_id_ext1_detalle`, `f470_id_ext2_detalle`, `f470_id_ccosto_movto`, `f470_id_proyecto`). La spec espera la estructura completa aunque los valores sean `None`.
- **Solución implementada**: Agregar todos los campos opcionales con `None` en los Movimientos de 173066 y 173079.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `transferencia_directa`, `transferir_entre_ubicaciones`, `trigger_transito_entrada`
- **Commit**: `8598d56`
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-015] `F_CONSEC_AUTO_REG` debe ser `1` para que Siesa auto-asigne el consecutivo

- **Categoría**: Formato de datos
- **Síntoma observado**: Siesa no generaba numeración automática del documento cuando el campo era `0`.
- **Causa raíz**: `F_CONSEC_AUTO_REG=0` deshabilita la autonumeración. Con `1`, Siesa asigna el consecutivo del tipo de documento correspondiente automáticamente.
- **Solución implementada**: `'F_CONSEC_AUTO_REG': 1` en todos los conectores POST. `F350_CONSEC_DOCTO` debe ser `0` (Siesa ignora el valor cuando `F_CONSEC_AUTO_REG=1`).
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — todos los conectores POST
- **Commit**: `d5c783c` — *"fix: 7 pre-prod blockers"*
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-016] `f470_referencia_item` debe ser `codigo_siesa`, no el código interno WMS

- **Categoría**: Formato de datos
- **Síntoma observado**: Siesa rechazaba el ítem o registraba el movimiento en el producto equivocado. Los SiesaJobs quedaban en estado FALLIDO permanente.
- **Causa raíz**: El campo `f470_referencia_item` es el código de artículo tal como está en el maestro de Siesa. En el WMS el código interno puede diferir (código de barras local, código alterno). El campo `Producto.codigo_siesa` guarda la referencia correcta.
- **Solución implementada**: `codigo_siesa = item.producto.codigo_siesa or item.producto.codigo` — solo si `codigo_siesa` existe se puede garantizar que Siesa lo reconoce.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py`, `app/services/despacho_parcial_service.py`
- **Commits**: `473c822`, `62ddbdd`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-017] Sintaxis de filtros GET: strings con `''valor''`, enteros sin comillas

- **Categoría**: Formato de datos / Limitación de API
- **Síntoma observado**: Filtros ignorados, resultados sin filtrar, o error HTTP 400 al consultar APIs de Siesa.
- **Causa raíz**: La sintaxis oficial de Connekta para el parámetro `parametros` en GETs usa comillas dobles simples (`''valor''`) para strings y sin comillas para enteros. Ejemplo correcto:
  ```
  f430_id_co = ''003'' AND f430_ind_estado = 3
  ```
- **Solución implementada**: Consistencia en toda la clase — strings siempre con `''` doble, enteros directamente.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — todos los métodos `_get`
- **Nivel de riesgo si se ignora**: **Alto**

---

## CATEGORÍA 2: COMPORTAMIENTO INESPERADO DE LA API

---

### [SIESA-018] HTTP 200 no garantiza éxito — verificar campo `codigo` en el body

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: Connekta retorna HTTP 200 con un body de error interno. Sin verificar el campo `codigo`, el WMS asumía éxito y no reintentaba.
- **Causa raíz**: Connekta V2/V3.1 envía `HTTP 200` incluso para errores internos de Siesa. El campo `codigo` indica: **0 = éxito**, **!=0 = error**. El campo `mensaje` contiene la descripción del error.
  - En respuestas tipo lista (V3.1), cada elemento puede tener su propio `codigo`.
- **Solución implementada**: En `_post()`, después del `r.json()`, verificar:
  ```python
  if isinstance(resp_json, dict):
      if resp_json.get('codigo') not in (None, 0):
          raise Exception(f'Siesa rechazó (codigo={codigo}): {mensaje}')
  elif isinstance(resp_json, list):
      for item in resp_json:
          if item.get('codigo') not in (None, 0):
              raise Exception(...)
  ```
  Lo mismo aplica en `_get()` (comentado como [A21]).
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `_post`, `_get`
- **Nivel de riesgo si se ignora**: **Alto** (errores silenciosos en producción)

---

### [SIESA-019] 142945 en modo "Respuesta Simplificada" no incluye el consecutivo del documento generado

- **Categoría**: Comportamiento inesperado / Limitación de API
- **Síntoma observado**: `_parsear_rm` fallaba con `ValueError` — el response de 142945 no contenía el patrón `RM-XXXX` en ningún campo.
- **Causa raíz**: El conector 142945 tiene un flag de configuración "Respuesta Simplificada" (configurable por el consultor Siesa). Cuando está activo, el response **no incluye** el consecutivo del documento creado — solo confirma el éxito. Sin el consecutivo no se puede invocar 142943 (FacturaDesdeRemision).
- **Solución implementada** (doble):
  1. `_parsear_rm` busca el patrón `RM-XXXX` en **todos** los campos string del response (no solo `mensaje`).
  2. Si no encuentra el patrón, hace fallback a `get_remision_desde_pedido()` que consulta `API_v2_Ventas_Remisiones_DesdePedido` filtrando por pedido y CO para recuperar la RM más reciente.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_remision_desde_pedido`, `app/services/despacho_parcial_service.py` — `_parsear_rm`, `despachar_parcial`
- **Commit**: `2cc8ecb` — *"fix: recuperar número de RM cuando 142945 no lo incluye en el response"*
- **Nivel de riesgo si se ignora**: **Alto** (el flujo de despacho parcial queda bloqueado)

---

### [SIESA-020] El consecutivo generado por 173076 llega en estructuras distintas según versión del conector

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: El consecutivo del documento 173076 (TransitoSalida) no se podía parsear. Sin el consecutivo, el 173079 (TransitoEntrada) es imposible de vincular.
- **Causa raíz**: Connekta puede devolver el consecutivo en distintas ubicaciones según la versión del conector: `detalle.Table[0]`, nivel raíz del response, o `detalle` como lista directa. Los campos también varían: `f350_consec_docto`, `consec_docto`, `consecutivo`, `NumeroDocumento`.
- **Solución implementada**: `_extraer_consec()` busca en orden:
  1. `detalle.Table[0]` — 6 campos canónicos
  2. `detalle.Table[0]` — cualquier clave que contenga `'consec'` con valor entero > 0
  3. Nivel raíz del response — mismos campos canónicos
  4. `detalle` como lista — primer elemento con campo canónico
  5. Si ninguno funciona: loguea la estructura completa y guarda `siesa_error` con instrucciones para ops.
- **Archivo(s) relevante(s)**: `app/services/traslado_service.py` — `_extraer_consec`
- **Commit**: `d977a4c` — *"traslados: blindar parsing consecutivo 173076 + alerta explícita si falla"*
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-021] El conector 238925 factura las cantidades del pedido original, no las enviadas en el payload

- **Categoría**: Comportamiento inesperado / Limitación de API
- **Síntoma observado**: Pedidos con faltantes parciales se facturaban por el total del pedido original en Siesa, ignorando las cantidades empacadas reales.
- **Causa raíz**: El conector 238925 (FACTURA_DESDE_PEDIDO) **toma las cantidades directamente del pedido comprometido en Siesa** — los ítems enviados en el payload solo sirven de referencia, Siesa los ignora. No sirve para despachos parciales.
- **Solución implementada**: Para pedidos parciales (`cantidad_empacada < cantidad_original_siesa`), usar el flujo:
  1. **142945** (RemisionPedido) → crea RM con las cantidades reales
  2. **142943** (FacturaDesdeRemision) → convierte la RM en FE
  
  El comparador de parcialidad usa `PedidoSiesa.cantidad_pedida` (cantidad original de Siesa), NO `ItemPacking.cantidad_esperada` que el sync ajusta al picking.
- **Archivo(s) relevante(s)**: `app/services/siesa_job_service.py` — `_ejecutar_job`, `app/services/despacho_parcial_service.py`
- **Commits**: `d389423` — *"siesa_job: enrutar pedidos parciales a flujo 142945 → 142943"*, `62ddbdd` — *"fix: facturar cantidad empacada real en pedidos parciales"*
- **Nivel de riesgo si se ignora**: **Alto** (sobrefacturación a clientes con faltantes)

---

### [SIESA-022] `f430_id_cond_pago` vacío → el .NET serializer de Connekta colapsa con HTTP 500

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: HTTP 500 al enviar la FE cuando el maestro del tercero en Siesa no tiene condición de pago asignada.
- **Causa raíz**: El serializador .NET de Connekta V2 intenta castear `null` a un tipo de dato interno y lanza una excepción no controlada → HTTP 500 sin mensaje descriptivo. Diferente del rechazo de Siesa (codigo!=0) que sí da contexto.
- **Solución implementada**:
  1. `SIESA_COND_PAGO_VENTAS` es variable de entorno **obligatoria** en producción — el servidor no arranca sin ella.
  2. `trigger_factura_desde_remision` usa `cond_pago = _cond_pago_siesa or self.cond_pago_ventas` — si Siesa no devuelve el campo, cae al fallback.
  3. Si actúa el fallback, se envía alerta email asíncrona para que el equipo comercial corrija el maestro del tercero.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `__init__`, `trigger_factura_desde_remision`
- **Commits**: `040d298` — *"alerta: email cuando fallback C01 actúa por data maestra incompleta"*
- **Nivel de riesgo si se ignora**: **Alto** (HTTP 500 sin contexto, despacho bloqueado)

---

### [SIESA-023] `get_estado_pedido` retorna Table vacío cuando el pedido no existe en Siesa

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: Pedidos eliminados manualmente en Siesa generaban comportamientos inconsistentes — algunos se trataban como error de red, otros pasaban el guard de pre-check.
- **Causa raíz**: Cuando el pedido no existe, Siesa devuelve `Table: []` (response HTTP 200 sin filas). Antes, esto se trataba igual que un error de red (ambos retornaban `None`), sin activar la lógica de "pedido anulado".
- **Solución implementada**: Diferenciar tres estados de retorno:
  - `int` positivo (1-9): estado real del pedido
  - `-1`: pedido no encontrado (Table vacío → eliminado o nunca existió)
  - `None`: error de red / tipo_docto vacío
  
  Con `-1`, `cerrar_packing` bloquea el despacho y `pedidos_sync_service` activa `pedido_anulado_siesa=True`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_estado_pedido`, `app/services/packing_service.py`, `app/services/pedidos_sync_service.py`
- **Commit**: `473c822`
- **Nivel de riesgo si se ignora**: **Medio** (pedidos eliminados en Siesa pasan el guard y fallan en el POST)

---

### [SIESA-024] Estado `4 = Cumplido` en Siesa es la señal de que el pedido ya fue facturado

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: La reconciliación automática no detectaba pedidos ya facturados — se reintentaban indefinidamente en la DLQ generando intentos duplicados.
- **Causa raíz**: Solo se verificaba el estado del `SiesaJob` en la DB local. Siesa marca el pedido como `estado=4 (Cumplido)` cuando se genera la factura electrónica — este es el indicador autoritativo de que el ciclo está cerrado.
- **Solución implementada**: El sweep de reconciliación consulta `get_estado_pedido()` — si retorna `4`, marca el job como reconciliado sin reintentar el POST.
- **Archivo(s) relevante(s)**: `app/services/reconciliacion_service.py`, `app/services/siesa_job_service.py` — `trigger_factura` (pre-check idempotencia)
- **Commits**: `9684a97` — *"fix(reconciliacion): agregar estado 4 (cumplido) como señal de factura procesada"*, `5f7861b` — *"fix: trigger_factura pre-check idempotencia"*
- **Nivel de riesgo si se ignora**: **Medio** (reintentos duplicados en DLQ, riesgo de FE duplicada)

---

### [SIESA-025] Siesa puede devolver `Table: []` indefinidamente en conteo cíclico

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: El operario quedaba bloqueado indefinidamente con el mensaje *"Conectando con Siesa — reintenta en unos segundos"*. El caché de existencias nunca se llenaba.
- **Causa raíz**: Para ciertos ítems que no tienen movimientos recientes o no están en el maestro de inventario de la bodega, Siesa devuelve `Table: []` en `API_v2_Inventarios_InvFecha` — no es un error transitorio sino la respuesta correcta para "sin stock registrado".
- **Solución implementada**: Si `existencia_siesa` es `None` después del advisory lock (cache miss persistente), guardar la cantidad contada del operario, avanzar a `SEGUNDO_CONTEO` (requiere revisión del admin) y liberar al operario. `confirmar_ajuste` re-consulta Siesa en el momento de la confirmación.
- **Archivo(s) relevante(s)**: `app/services/conteo_service.py` — `registrar_conteo`
- **Commit**: `3067730` — *"fix(conteo): desbloquear operario cuando Siesa devuelve Table vacío indefinidamente"*
- **Nivel de riesgo si se ignora**: **Alto** (bloqueo operacional de operarios)

---

### [SIESA-026] Campos filtro de `API_v2_Ventas_Facturas_DesdePedido` usan prefijo `f350_`, no `f430_`

- **Categoría**: Comportamiento inesperado
- **Síntoma observado**: HTTP 400 al verificar si ya existe FE antes de cerrar caja — el guard anti-duplicado fallaba y bloqueaba el cierre.
- **Causa raíz**: La API `API_v2_Ventas_Facturas_DesdePedido` para el documento de **factura** usa campos `f350_id_tipo_docto` y `f350_consec_docto`. El código usaba incorrectamente `f430_id_tipo_docto` y `f430_consec_docto` (prefijo del pedido).
- **Solución implementada**: Corregir los campos del filtro. Adicionalmente, para el guard anti-FE-duplicada en producción se migró al descriptor personalizado `papeleriamedellin_monitos_facturas_wms` con filtro `f430_consec_docto` (consecutivo del pedido origen).
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_factura_desde_pedido`
- **Commits**: `a20e6d9` — *"connekta: corregir campos filtro en get_factura_desde_pedido"*, `ce443df` — *"connekta: bypass QA y descriptor monitos"*
- **Nivel de riesgo si se ignora**: **Alto** (guard anti-duplicado roto → posible FE duplicada)

---

## CATEGORÍA 3: LIMITACIONES DE API Y RESTRICCIONES DE DISEÑO

---

### [SIESA-027] El conector 238925 usa endpoint V3.1 dinámico distinto al estándar V3

- **Categoría**: Limitación de API
- **Síntoma observado**: El conector de factura directa (238925) fallaba cuando se llamaba con el endpoint estándar de conectores.
- **Causa raíz**: 238925 es un conector **dinámico** que usa:
  - URL: `/api/siesa/v3.1/conectoresimportar` (no `/api/siesa/v3/conectoresimportarestandar`)
  - Requiere parámetro adicional: `idSistema` en el query string
  - El formato del payload sigue el esquema dinámico (no el posicional de V3)
- **Solución implementada**: `trigger_factura` usa `url=self.url_post_dinamico` y `extra_params={'idSistema': self.id_sistema}`.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_factura`, `__init__` (`url_post_dinamico`, `id_sistema`)
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-028] Los tipos de documento de tránsito (TTS/TTE) deben crearse en Siesa con Clases 65/66 antes de usar 173076/173079

- **Categoría**: Limitación de API / Prerequisito de configuración
- **Síntoma observado**: 173076 o 173079 fallaban con error críptico — Siesa no reconocía el tipo de documento.
- **Causa raíz**: Los conectores de tránsito requieren tipos de documento de clase específica que **no existen por defecto**:
  - 173076 (TransitoSalida): Tipo doc Clase **65**
  - 173079 (TransitoEntrada): Tipo doc Clase **66**
  Deben crearse en Siesa Enterprise → Inventarios → Tipos de documento, y sus códigos configurados en `SIESA_TIPO_DOCTO_TRANSITO_SALIDA` / `SIESA_TIPO_DOCTO_TRANSITO_ENTRADA`.
- **Solución implementada**: Fail-fast con `ValueError` descriptivo si las variables de entorno están vacías antes de intentar el POST. El health check `/api/health/siesa` también lo verifica.
- **Confirmado por**: Consultor Siesa.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_transito_salida`, `trigger_transito_entrada`
- **Commit**: `4bc0490`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-029] `f350_consec_docto_base` en 173079 es obligatorio Entero — `0` y `None` son rechazados

- **Categoría**: Limitación de API
- **Síntoma observado**: 173079 fallaba cuando el consecutivo del 173076 no se había podido parsear correctamente.
- **Causa raíz**: El campo que vincula la entrada de tránsito con su salida (`consec_salida`) debe ser un entero positivo. Siesa rechaza explícitamente `0` y `null` porque el documento de salida 173076 es obligatorio para cerrar el tránsito.
- **Solución implementada**: Fail-fast con `ValueError` si `consec_salida` es falsy antes de construir el payload.
- **Confirmado por**: Consultor Siesa.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `trigger_transito_entrada`
- **Commit**: `4bc0490`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-030] `SIESA_UNIDAD_NEGOCIO` es obligatorio en conectores de traslado 173076/173079

- **Categoría**: Limitación de API / Prerequisito de configuración
- **Síntoma observado**: Siesa rechazaba `f470_id_un_movto` vacío en los conectores de traslado — error sin mensaje descriptivo.
- **Causa raíz**: El campo `f470_id_un_movto` (Unidad de Negocio) es obligatorio en los conectores de traslado de esta instalación de Siesa. El código de Unidad de Negocio varía por compañía — no hay valor por defecto universal. Solicitar al área financiera de Papelería Medellín el código exacto.
- **Nota**: En algunos conectores Siesa hereda el valor de la bodega si `f470_id_un_movto` es `None`. En traslados 173076/173079 **no hereda**.
- **Solución implementada**: `SIESA_UNIDAD_NEGOCIO` configurable como variable de entorno en Railway, sin default. Comentado como obligatorio en producción.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `__init__` (`self.unidad_negocio`)
- **Commit**: `43496c1`
- **Nivel de riesgo si se ignora**: **Alto**

---

## CATEGORÍA 4: GUARDS CRÍTICOS Y WORKAROUNDS

---

### [SIESA-031] `get_factura_desde_pedido` debe ser FAIL-FAST ante error de red — nunca retornar `[]` silencioso

- **Categoría**: Workaround aplicado / Riesgo fiscal
- **Síntoma observado**: Si Connekta no responde durante el guard anti-duplicado y se retorna `[]`, el caller asume "no hay FE previa" y dispara `trigger_factura` (238925) → **Factura Electrónica duplicada** (impacto DIAN).
- **Causa raíz**: El patrón de capturar la excepción y retornar `[]` es seguro para GETs informativos pero catastrófico para guards de idempotencia.
- **Solución implementada**: Tanto `get_factura_desde_pedido` como `get_factura_desde_remision` hacen **re-raise** de cualquier excepción en vez de retornar `[]`. El caller debe capturar y abortar el despacho.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_factura_desde_pedido`, `get_factura_desde_remision`
- **Commit**: `143ebf7` — *"fix: 3 guards críticos en 142948 y anti-duplicado FE"*
- **Nivel de riesgo si se ignora**: **Alto** (FE duplicada = problema fiscal con la DIAN)

---

### [SIESA-032] `proveedor_id=None` enviado a Siesa causa error duro sin mensaje útil

- **Categoría**: Workaround aplicado
- **Síntoma observado**: El POST de 142948 fallaba con error genérico de Siesa cuando `proveedor_id` era `None`. El mensaje no indicaba que `f350_id_tercero` (posición 43-58) era el campo faltante.
- **Causa raíz**: El campo `f350_id_tercero` es obligatorio en el spec 142948. Si el API de OCs (`API_v2_Compras_Ordenes`) no expone `f200_id_prov` para el proveedor, llega `None` al gateway.
- **Solución implementada**: Guard previo al POST que lanza `ValueError('proveedor_id es None — f350_id_tercero es obligatorio en 142948...')`. El job queda en FALLIDO con mensaje descriptivo para que ops investigue el maestro del proveedor en Siesa.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `confirmar_entrada_compras`
- **Commit**: `143ebf7`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-033] `f470_cant_base=0.0` en movimientos debe filtrarse antes del POST

- **Categoría**: Workaround aplicado
- **Síntoma observado**: Siesa rechazaba entradas de OC (142948) cuando algún ítem tenía `cantidad_recibida=0` en el payload — error relacionado con reglas de cuenta 14 (inventario).
- **Causa raíz**: Los ítems con cantidad 0 son backorders que no se recibieron en esta entrega. Siesa no acepta líneas de movimiento con `f470_cant_base=0.0` — requiere que la línea directamente no exista en el payload.
- **Solución implementada**: Payload sanitizer que filtra ítems con `cantidad_recibida <= 0` ANTES de construir el bloque `Movimientos`. Si todos los ítems son 0, se lanza `ValueError` antes del POST.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `confirmar_entrada_compras` (bloque `Movimientos`)
- **Commit**: `143ebf7`
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-034] `f470_id_motivo='04'` para bonificaciones en OC — el maestro de Siesa tiene código específico

- **Categoría**: Workaround aplicado / Formato de datos
- **Síntoma observado**: Ítems de bonificación en OC eran registrados con el motivo de compras genérico (`motivo_compras='01'`) en vez del motivo de obsequio.
- **Causa raíz**: El maestro "Conceptos y Motivos" de Siesa tiene un código específico para obsequios/bonificaciones: `'04'`. Si se usa el motivo genérico, la cuenta contable del movimiento es incorrecta.
- **Solución implementada**: `'f470_id_motivo': '04' if item.get('tipo') == 'BONIFICACION' else self.motivo_compras`
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `confirmar_entrada_compras` (bloque `Movimientos`)
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-035] Rate limiting 429 de Connekta — `Retry-After` en headers

- **Categoría**: Rate Limits
- **Síntoma observado**: Connekta retorna HTTP 429 durante picos de tráfico (sync masivo de pedidos, múltiples workers).
- **Causa raíz**: Connekta implementa rate limiting. El header `Retry-After` indica el tiempo de espera en segundos.
- **Solución implementada**: `_get` y `_post` detectan HTTP 429, extraen `Retry-After` y lanzan excepción descriptiva. La DLQ maneja el backoff con `max_intentos=5` y `proximo_intento` creciente. `pg_advisory_lock` en los 4 sync services previene thundering herd cuando Siesa se recupera.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `_get`, `_post`; `app/services/siesa_job_service.py`
- **Nivel de riesgo si se ignora**: **Medio**

---

## CATEGORÍA 5: CAMPOS DE LA API DE CONSULTA (GETs) — ALIASES DESCUBIERTOS EMPÍRICAMENTE

---

### [SIESA-036] Los campos de `API_v2_Ventas_Pedidos` usan aliases del procedimiento almacenado, no nombres de tabla base

- **Categoría**: Limitación de API / Comportamiento inesperado
- **Síntoma observado**: Los campos asumidos de la tabla base de Siesa no existían en el JSON del response. `trigger_factura_desde_remision` construía la FE con tercero vacío.
- **Causa raíz**: `API_v2_Ventas_Pedidos` es un procedimiento almacenado con aliases propios. Los nombres en el JSON **no coinciden** con los nombres de columna de tabla base documentados en manuales genéricos de Siesa. Los campos verificados empíricamente (2026-04-28) son:
  ```
  f200_id_pedido_fact          → NIT/código tercero cliente
  f461_id_sucursal_pedido_rem  → sucursal del cliente
  f430_id_tipo_cli_fact        → tipo cliente facturación
  f430_id_cond_pago            → condición de pago
  f430_id_moneda_docto         → moneda del documento
  f200_id_pedido_vend          → NIT del vendedor
  ```
- **Solución implementada**: Documentados los aliases en el docstring de `get_pedido_cabecera()`. No confiar en documentación genérica — verificar con `/api/siesa/debug-*` en QA antes de usar nuevos campos.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_pedido_cabecera`
- **Commit**: `bf42b38` — *"fix: corregir mapeo de campos en trigger_factura_desde_remision (142943)"*
- **Nivel de riesgo si se ignora**: **Alto**

---

### [SIESA-037] `f431_cant1_remisionada` para calcular pendiente de pedido (no `f431_cant1_despachada`)

- **Categoría**: Limitación de API
- **Síntoma observado**: Los pedidos ya despachados seguían apareciendo como pendientes en la cola de picking.
- **Causa raíz**: La cantidad despachada en `API_v2_Ventas_Pedidos` se reporta en `f431_cant1_remisionada`. El campo `cantidad_pendiente` se calcula como `f431_cant1_pedida - f431_cant1_remisionada`. Si `cant_pendiente > 0`, el ítem aún tiene unidades por despachar.
- **Solución implementada**: Cálculo explícito en `get_pedidos_aprobados()` con los campos correctos.
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_pedidos_aprobados`
- **Nivel de riesgo si se ignora**: **Medio**

---

### [SIESA-038] `f350_ind_estado='9'` es el código de documentos anulados en APIs de factura

- **Categoría**: Limitación de API
- **Síntoma observado**: El guard anti-FE-duplicada contaba facturas anuladas como "activas", bloqueando despachos legítimos.
- **Causa raíz**: En `API_v2_Ventas_Facturas_DesdePedido`, el campo `f350_ind_estado` usa el valor `'9'` para facturas anuladas. El guard debe excluirlas explícitamente.
- **Solución implementada**: `[r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']` — solo retorna facturas activas (no anuladas).
- **Archivo(s) relevante(s)**: `app/services/connekta_gateway.py` — `get_factura_desde_pedido`, `get_factura_desde_remision`
- **Nivel de riesgo si se ignora**: **Alto** (despachos bloqueados incorrectamente)

---

## RESUMEN DE VARIABLES DE ENTORNO CRÍTICAS PARA SIESA

| Variable | Connector/API | Notas |
|---|---|---|
| `CONNEKTA_IKEY` | Todos | Autenticación header `ConniKey` |
| `CONNEKTA_ITOKEN` | Todos | Autenticación header `ConniToken` |
| `CONNEKTA_ID_COMPANIA` | Todos GETs | `idCompania` en query string |
| `CONNEKTA_ID_SISTEMA` | 238925 | `idSistema` requerido por V3.1 dinámico |
| `SIESA_ID_CIA` | Todos POSTs | `F_CIA` como entero — verificar en Siesa Enterprise → Parámetros |
| `SIESA_COND_PAGO_VENTAS` | 142943 | **Obligatorio** — serializer .NET falla con null |
| `SIESA_TIPO_DOCTO_ENTRADA_OC` | 142948 | Fail-fast si ausente |
| `SIESA_TIPO_DOCTO_TRANSITO_SALIDA` | 173076 | Clase 65 en Siesa — fail-fast si ausente |
| `SIESA_TIPO_DOCTO_TRANSITO_ENTRADA` | 173079 | Clase 66 en Siesa — fail-fast si ausente |
| `SIESA_MOTIVO_TRASLADO` | 173076/173079 | **Obligatorio** — sin default |
| `SIESA_UNIDAD_NEGOCIO` | 173076/173079 | **Obligatorio** en traslados — solicitar a finanzas |
| `SIESA_NIT_EMPRESA` | 142948 | `f350_id_tercero` (comprador) en entradas OC |
| `SIESA_ID_MOTIVO_VENTAS` | 142945 | Obligatorio (pos 131, ancho 2) |
| `SIESA_LISTA_PRECIO` | 142945 | Opcional pero recomendado (pos 169, ancho 3) |
| `SIESA_UOM_DEFAULT` | 174646 | Fallback 'UND' si ítem no tiene UOM |

---

*Archivo generado por auditoría de 60+ commits y lectura directa de `app/services/connekta_gateway.py`, `app/services/traslado_service.py`, `app/services/despacho_parcial_service.py`, `app/services/recepcion_service.py`.*

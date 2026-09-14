# Ticket para consultor Siesa — Proceso de averías (punto de venta → NB1 → AV1)

## Contexto

Papelería Medellín necesita formalizar en el WMS el proceso de mercancía
averiada, que hoy ocurre por fuera de todo sistema. El proceso de negocio,
tal como lo define la gerencia, es:

1. El administrador de un punto de venta **separa, identifica y cuenta** la
   mercancía averiada que tiene en su bodega, y deja la razón.
2. La **envía a la bodega principal NB1**.
3. En NB1, **recepción valida la cantidad** efectivamente recibida.
4. Un **administrador da el visto bueno**: confirma si la mercancía está
   realmente averiada o no.
5. Solo entonces se ubica como averiada, y más adelante se decide si se
   **da de baja** o se **dona**.

El volumen es bajo y estacional (se concentra en temporada escolar).

La pregunta central que traemos es **cómo debe verse ese proceso en Siesa**,
y en qué momento exacto la mercancía deja de ser vendible.

---

## Lo que ya está construido y verificado de nuestro lado

Esto NO son preguntas. Lo listamos para que las respuestas se apoyen en ello.

| Conector | Documento | Estado |
|---|---|---|
| **173076** STS | Transferencia en Tránsito Salida, **clase 65** | Probado contra Siesa QA el **2026-09-09** |
| **173079** ETS | Transferencia en Tránsito Entrada, **clase 66, concepto 605** | Probado contra Siesa QA el **2026-09-09** |
| **142951** | `DocumentoInv`, tipo `TRA`, **clase 67, concepto 607**, NB1 → AV1 | Probado con POST real contra Siesa QA el **2026-09-09** |

Restricciones del ERP que ya tenemos medidas y respetamos:

- El ETS exige `f450_id_bodega_salida` = bodega real de origen del STS,
  **nunca** la bodega de tránsito: con TRA1, Siesa rechaza con **62485**
  *«bodega de salida diferente a la capturada en el STS»* (2026-06-12).
- Siesa valida `CO(documento) == CO(bodega)` — errores **46089/46090**.
- El 142951 **rechaza si el disponible resultante queda negativo**, en
  cualquier dirección (verificado 2026-07-02, item PAPELSP9218/NS1).

**Lo que NO hemos podido verificar:** las cadenas que probamos contra QA
tienen a NB1 siempre como **origen** y nunca como **destino**. El sentido
que este proceso necesita —punto de venta hacia NB1— no está ejercitado.

---

## Preguntas puntuales para el consultor

> **Nota de método.** En rondas anteriores, las definiciones de motivo y
> naturaleza dadas de memoria fallaron dos veces y costaron dos ciclos de
> corrección. Para cada respuesta que involucre un maestro pedimos la
> **captura real del registro en el ambiente correspondiente**, no la
> referencia. Es más rápido para todos.

### A. Cómo se identifica una avería en el documento

**A1.** Tal como está hoy, el documento de avería que emite el WMS es
**indistinguible en Siesa de un traslado inter-bodega ordinario**:

| | Avería (142951) | Traslado directo (173066) |
|---|---|---|
| Tipo documento | `TRA` | `TRA` |
| Clase | 67 | 67 |
| Concepto | 607 | 607 |
| Motivo | `SIESA_MOTIVO_AVERIA` → **cae a `SIESA_MOTIVO_TRASLADO` si no está configurada** | `SIESA_MOTIVO_TRASLADO` |

Lo único que los diferencia es la bodega de destino y un texto libre en
`f350_notas` («Avería detectada por WMS · SKU»).

**¿Existe en el maestro de motivos (`t146_mc_motivos`) un motivo específico
para mercancía averiada / dañada / deteriorada, válido para clase 67 y
concepto 607?** Necesitamos código, descripción e `ind_naturaleza` de la
captura real. Si no existe, ¿se puede crear y qué implica contablemente
frente a reutilizar el de traslado?

**A2.** Una avería **validada** ¿debe seguir siendo concepto **607
(transferencia entre bodegas)**, o corresponde el concepto **602 (salidas
directas — mermas, bajas)**? Contablemente «mover a la bodega de averías» y
«reconocer la pérdida» no son el mismo hecho, y hoy el WMS usa 607 para
ambos sin distinguirlos.

### B. El punto crítico: cuándo deja de ser vendible

Entre que la mercancía llega físicamente a NB1 y que el administrador da el
visto bueno hay una ventana. Vemos dos caminos.

**Camino A — entra a NB1 y sale a AV1 cuando se aprueba.**
Dos documentos: ETS (clase 66) hacia NB1, después transferencia (clase 67)
NB1 → AV1. Durante la ventana **la mercancía está disponible para la venta
en NB1**, sin ninguna marca que la distinga.

**Camino B — entra directo a AV1, y solo sale hacia NB1 si se rechaza.**
Nunca es vendible mientras nadie la haya validado.

**B1.** **¿AV1 tiene centro de operación asignado en el maestro de bodegas?
¿Cuál?** Esta es la pregunta que decide el Camino B, y también si es viable
un documento con origen en un punto de venta.

> Contexto: el único documento que hoy toca AV1 usa CO **003** fijo, y
> funciona porque AV1 es «Averías CDI», del mismo centro. Un documento con
> origen NS1 (CO 001) hacia AV1 chocaría con la validación 46089/46090.
> Pedimos la **captura del registro de AV1 en el maestro de bodegas**.

**B2.** ¿Siesa acepta una Transferencia en Tránsito (clase 65/66) cuya
bodega de entrada sea **AV1**? No tenemos ninguna prueba: las
corridas reales contra QA cubren solo las bodegas operativas.

**B3.** ¿Se puede emitir un ETS (clase 66) cuya `f450_id_bodega_entrada` sea
**distinta** a la declarada en el STS base? Inferimos que no, a partir del
rechazo 62485 que observamos — pero es una inferencia sobre un mensaje de
error, no sobre el spec. Si se pudiera, podríamos despachar y decidir el
destino **después** de validar, que sería la solución más limpia de todas.

**B4.** ¿Cuál es el comportamiento de Siesa con saldo en **cuenta tránsito
sin liquidar** durante días o semanas (cierre contable de mes, costeo,
valorización)? Nuestro proceso podría dejar mercancía en tránsito mientras
espera el visto bueno.

**B5.** ¿La existencia en **AV1 queda excluida de lo que ve un vendedor**
como disponible? Queremos confirmarlo explícitamente: el objetivo central
del proceso es que nadie pueda vender mercancía rota.

### C. El concepto que graba el kardex — pregunta urgente, excede este proyecto

**C1.** Nuestros payloads de STS (clase 65) y ETS (clase 66) **no envían
`f470_id_concepto`** en el bloque de Movimientos; el ETS declara **605**
solo en la cabecera (`f450_id_concepto`).

**¿Qué concepto graba Siesa en el kardex (`f470_id_concepto`) para los
documentos de clase 65 y de clase 66?**

> Por qué es urgente: nuestro motor de demanda clasifica cada concepto del
> kardex y tiene una compuerta dura — si aparece un concepto que no conoce,
> **deja de calcular y lanza error** en vez de responder con un hueco. Los
> conceptos que clasifica son 501, 502, 601, 602, 603, 607 y 699. **605 no
> está entre ellos.** Si Siesa aterriza 605 en el kardex, el cálculo de
> demanda de toda la red se detiene. Necesitamos saberlo antes de correr
> más traslados, no después.

### D. La diferencia de cantidad

**D1.** El STS declara la cantidad despachada y el ETS la recibida. Si
recepción en NB1 cuenta **menos** de lo que salió del punto, ¿qué pasa en
Siesa con la diferencia? ¿Queda en cuenta tránsito indefinidamente? ¿Existe
un documento para resolverla, o hay que anular?

**D2.** ¿Existe un documento de **anulación o reversa de un STS (clase 65)
vía conector**? Hoy revertimos solo del lado del WMS y dejamos una nota
pidiendo anular a mano en Siesa.

### E. La salida definitiva: baja y donación

**E1.** Nuestro sistema **lee** el concepto **602 — «salidas directas
(mermas, bajas)»** y lo clasifica como no-demanda, pero nunca lo escribe.
¿Qué **tipo y clase de documento, concepto y motivo** corresponden a dar de
baja mercancía averiada desde AV1?

**E2.** ¿**Donar** y **dar de baja** son la misma operación en Siesa, o son
documentos distintos? Si difieren, necesitamos saberlo **antes** de
construir: el sistema tendría que distinguirlas desde el momento del
veredicto, no al final.

**E3.** ¿Alguna de las dos requiere tercero (NIT del donatario), resolución
DIAN o tratamiento fiscal particular que deba viajar en el documento?

### F. Dirección, centro de costo y disponibilidad

**F1.** ¿Hay alguna restricción para que una bodega de punto de venta sea
**origen** de un STS con destino NB1? ¿Todas tienen su CO certificado para
esa dirección?

**F2.** Una avería que **nace en NS1**: ¿la pérdida debe cargarse al CO del
punto (**001**) o al del CDI (**003**)? Hoy nuestro conector de averías
emite con CO 003 fijo y origen NB1 cableado — no sabe expresar otro origen.
Si contablemente corresponde el CO del punto, hay que rediseñar el
documento.

**F3.** Si un punto de venta tiene compromisos abiertos que dejan su
disponible en negativo, ¿el STS de sus averías será rechazado por
«Faltante Inv.»? Ya nos pasó en una prueba real (PC1, *«Faltante Inv.:
-30»*). Para averías esto es grave: la mercancía está físicamente ahí y
rota, pero el documento no pasaría.

**F4.** ¿Este proceso se comporta igual en producción
(`servicios.siesacloud.com`) o hay que replicar configuración por separado,
como ya nos pasó con otras consultas y conectores custom?

---

## Nota interna (no enviar al consultor)

### Estado del código

El conector que este proceso necesita **ya existe y está probado**:
`transferir_a_averias()` en `app/services/connekta_ajustes_gateway.py:157-245`.
Su único encolador vive en `app/services/devolucion_service.py:382`, archivo
DEPRECATED desde el 2026-07-28 y sin callers de producción. Es **código
correcto, probado contra QA y que hoy nadie llama**. El paso 4 del proceso
(visto bueno del admin) es su llamador natural.

Dos limitaciones de esa función a resolver cuando se conecte:
- **No acepta bodega de origen** (`:157`): siempre `CONNEKTA_BODEGA → AV1`.
- **No hace la conversión por empaque** que sí hacen STS y ETS
  (`:221-222`): manda siempre `UND` y la cantidad cruda. Con un SKU de
  `factor_empaque > 1` la cantidad saldría mal.

### Lo que hay que construir, independiente de la respuesta del consultor

1. **El molde de las dos validaciones ya existe** en `TareaPicking`
   (`app/models/picking.py:36-62`): declaración del operario en
   `motivo_bloqueo` (que ya incluye `MERCANCIA_AVERIADA`) /
   `observaciones_bloqueo` / `cantidad_recogida`, más el veredicto del admin
   en seis columnas `auditoria_*` con `AVERIA` entre los valores, guard
   `Roles.SUPERVISION`, y capacidad de contradecir el paso 1 sin borrarlo.
   **Copiar ese patrón a la recepción de traslados, no inventar otro.**
2. `ENTREGADA` es estado terminal de `SolicitudTraslado`. Falta el segundo
   actor después de ella.
3. `ItemSolicitudTraslado` (`app/models/traslado.py:207-222`) no tiene
   **ningún** campo de texto ni flag. Falta motivo por ítem y marca de
   avería. La cabecera tiene `observaciones`, que se escribe solo al crear
   y ninguna pantalla muestra — libre en la práctica.
4. Si `cantidad_recibida < cantidad_enviada`, hoy se persiste y **nadie lo
   lee jamás**. Los otros dos flujos del repo sí lo exponen (OC:
   `es_faltante()/es_exceso()`) o abren trabajo (faltante de picking:
   `FaltanteReporteService` abre conteo y manda correo). Falta acá.
5. Si el ETS falla, se escribe el error en un campo, la solicitud pasa a
   ENTREGADA igual, y no hay reintento ni cola de fallos. Precedente: la
   regresión del commit `1344c7a` (2026-07-24) dejó **69 traslados
   atrapados un mes** y el único rastro fue un log.
6. La cadena punto de venta → NB1 contra QA es **un cambio de una línea** en
   `CADENA` de `scripts/qa_traslados_gateway_real.py:49` y nunca se corrió.
   El docstring del script afirma ejercitar cada bodega «como origen y como
   destino» — es falso para NB1.

### Prerrequisito de seguridad — antes de crear cualquier bin de averías

El descuento de traslados (`app/services/traslado_service.py:1276`,
`:1303-1311`) **no filtra zona** y ordena por cantidad ascendente. Un bin de
averías, por tener pocas unidades, sería **el primero en vaciarse** hacia
NB1 como mercancía buena. Junto con él: `_get_stock_wms` (`:1663`) lo
ofrece como disponible, el bin de retorno de la reversa (`:775`) es
`order_by(id).first()` sin filtro de zona, y `Producto.stock_total`
(`app/models/producto.py:37-45`) suma averías al total del catálogo.

### Medición pendiente, una consulta cada una

- ¿Aparece el concepto **605** en `KardexMovimiento`? Si sí, la compuerta de
  `kardex_service.py:980-989` ya está lanzando y el motor de demanda está
  caído. Si no, confirmar con el consultor qué graba clase 65/66.
- ¿El bulk zero de `inventario_siesa_service.py:1011-1023` está tocando
  bins de averías? Escribe `cantidad = 0` **sin un solo
  `MovimientoInventario`**.

### Corrección a documentación propia

`CLAUDE.md:384` e `inventario_siesa_service.py:246-248` afirman que
`_BODEGAS_SERVICIO` se mantiene fuera de `_BODEGAS_PV` porque incluirlas
«las volvería destino válido de un traslado y opción de un desplegable de
sede». **Ninguna de las dos protecciones existe**: `routes/traslados.py:95`
acepta cualquier `bodega_destino_siesa` sin validar, y el desplegable sale
de otra lista. La única protección real es el filtro del Armador
(`armador_service.py:291`). Un lector que confíe en el comentario creería
que AV1 está bloqueada como destino cuando la API la acepta hoy.

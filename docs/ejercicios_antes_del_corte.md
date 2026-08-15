# Cuatro ejercicios que ningún test reemplaza

Los cuatro bloques que siguen están **construidos, probados y nunca ejercitados
contra la realidad**. Este documento existe porque el patrón ya se pagó una vez:

> El 2026-08-11, la primera vez que la liquidación corrió de punta a punta, los
> **tres** conectores financieros fallaron. Y el patrón que dejó ese día:
> *los conectores incompletos son exactamente los que nunca se ejercitaron.*
> El 251126 se había probado en vivo en julio y estaba entero.

Ninguno de los cuatro se puede hacer con un test. Los cuatro necesitan una
persona, y **los cuatro dejan de ser probables sin consecuencias después del
corte**: hoy un error se deshace en QA; el martes es un ajuste manual en el ERP
o mercancía que nadie encuentra.

Cada ejercicio dice qué se aprieta, **qué se mira**, y qué significa que pasó.
Si algo no coincide, no se sigue: se anota qué se vio y se para.

---

## 1 · Una liquidación real, con los tres documentos visibles en Siesa

**Por qué:** es el único de los cuatro que bloquea el ciclo del dinero. Sin
esto, el conductor cobra y el saldo sigue abierto — que es **peor que hoy**,
porque hay plata en la calle sin registro que la persiga.

**Antes de empezar**
- `CONNEKTA_URL` apuntando al ambiente donde se va a probar.
- Una ruta con al menos una parada **de contado** entregada y cobrada.
- Que la parada haya salido en modo **DINÁMICO** (Total/Parcial). Si salió en
  CRÉDITO o LIBRE, parar: el arreglo del clasificador no llegó a ese proceso.

**Pasos**
1. Liquidar la ruta desde el panel de Liquidación.
2. Esperar **10-12 segundos** antes de consultar nada — Siesa tarda en
   procesar (Regla 20). Consultar antes da un falso negativo.
3. Abrir Siesa → **Financiero → Auditoría de documentos**, filtrar por tercero
   y fecha.

**Qué se mira — los tres documentos, por separado**

| Documento | Conector | Qué tiene que verse |
|---|---|---|
| Nota crédito (solo si hubo rechazo) | 251126 | NCE con su consecutivo, tab CxC con `Nuevo saldo = 0` |
| **Recibo de caja** | 142888 | RC por el monto **cobrado**, cruzado contra la FE — no contra el pedido |
| Documento contable (solo si hubo retención) | 142882 | **Un DC por cada cuenta PUC**, no uno solo |

**Pasó si:** los tres existen, el RC cruza contra la factura correcta, y la
factura queda **saldada de verdad** en el ledger (`API_v2_CxC_General`:
`f353_total_cr == f353_total_db`), no solo en la vista del documento.

**No pasó si:** el tablero del WMS dice éxito y en Siesa no aparece el
documento. Ese es el modo de fallo exacto de las tres banderas de idempotencia
— el WMS informaba éxito sin haber enviado nada.

**Si falla:** anotar el mensaje **completo** de Siesa. Los mensajes cortados son
la razón por la que 19 rechazos de traslado llevan una semana sin diagnóstico.

---

## 2 · Una etiqueta de cada tipo, pasada por el láser del CD

**Por qué:** JsBarcode venía de un CDN que el service worker se niega a cachear,
y los cuatro sitios que lo usan se tragaban su ausencia en un `catch` vacío.
Resultado: etiquetas impresas **sin código**, sin un solo error en pantalla. Ya
está servido desde `/static/vendor/`, pero eso solo prueba que la librería
carga — no que el código impreso se lea.

**Los cuatro puntos de impresión** (los cuatro usan el mismo motor; si uno falla
por formato, probablemente fallen los cuatro):

| Tipo | Dónde | Archivo |
|---|---|---|
| Producto | Etiquetas → buscar producto → Imprimir | `etiquetas.js:106` |
| Ubicación | Layout → huecos → imprimir | `layout.js:1090` |
| LPN / paca | Packing | `packing.js:807` |
| Bulto | Packing → cerrar | `packing.js:836` |

**Pasos**
1. Imprimir una de cada tipo, **en la impresora real**, no en PDF.
2. Pasar las cuatro por el **láser del CD**, no por la cámara del teléfono. La
   cámara lee códigos que el láser rechaza — resolución y contraste distintos.
3. Verificar que lo leído coincide con lo impreso en texto.

**Pasó si:** las cuatro leen al primer intento y el valor coincide.

**No pasó si:** hay que insistir. Un código que necesita tres pasadas en el
banco de pruebas no se lee en el muelle con prisa.

**Prueba extra que vale la pena:** poner el teléfono en modo avión, abrir la PWA
e imprimir. **Tiene que salir un aviso de error, no una etiqueta muda.** Ése era
el defecto entero.

---

## 3 · Un traslado trabado en QA, recuperado desde la pantalla

**Por qué:** `traslado_service.py` le dice al operario —en pantalla y por
correo— *«Usa WMS Admin → Traslados → Reintentar despacho»*. Ese botón no
existía hasta ayer. Ahora existe y **nunca se apretó**.

**Antes de empezar:** esto se hace en QA. Trabar un traslado a propósito en
producción deja stock en la bodega de tránsito.

**Cómo se traba** (cualquiera sirve; la primera es la más limpia)
- Apuntar `CONNEKTA_URL` a un host inalcanzable **solo durante el despacho**.
- O despachar con Siesa fuera de horario (~después de las 8 p.m., TCP timeout
  de 30 s — Regla 14).

**Pasos**
1. Crear y aprobar un traslado, hacer picking y packing, y despachar con Siesa
   caído.
2. **Mirar la tarjeta**: tiene que mostrar el error de Siesa. Si se ve verde, el
   arreglo de `to_dict` no llegó — parar y anotar.
3. Restaurar la conexión.
4. Apretar los tres botones, uno por escenario:

| Botón | Endpoint | Cuándo aplica |
|---|---|---|
| Reintentar despacho (STS) | `/api/traslados/<id>/reintentar-despacho` | el 173076/174930 no salió |
| Reintentar recepción (ETS) | `/api/traslados/<id>/reintentar-recepcion` | el 173079 no salió. **Exige estado ENTREGADA** |
| Revertir | `/api/traslados/<id>/revertir` | deshacer. **Solo en EN_TRANSITO** |

**Pasó si:** el reintento crea el documento en Siesa, el consecutivo queda
guardado, y el error desaparece de la tarjeta.

**No pasó si:** el botón devuelve 400 por estado. Anotar **qué estado tenía** —
esa es justamente la razón por la que `ENTREGADA` se sigue escribiendo aunque el
173079 falle: dejarlo en `EN_TRANSITO` volvería inalcanzable el único botón que
lo arregla.

**Ojo con el doble documento:** el reintento manual del 174646 **no comprueba**
`siesa_requisicion_consec` antes de re-postear — puede crear una segunda RIT. Si
se va a probar ése, mirar el consecutivo antes y después.

---

## 4 · Un ETS parcial — recibir 1 de 2 y mirar el saldo en tránsito

**Por qué:** es la única hipótesis abierta del plan de traslados, y **si es falsa
hay que rediseñar** la recepción con diferencia. Cuesta veinte minutos.

**La pregunta, exacta:**

> Un ETS (173079) por **menos cantidad** que su STS base, ¿liquida
> parcialmente el tránsito y deja el saldo en la bodega puente, o rechaza?

**Pasos**
1. Traslado de **2 unidades** de un SKU, modo EN_TRANSITO. Despachar (STS por 2).
2. Recibir **1** — desde Recepción, que sí manda los conteos reales.
3. Abrir el documento de entrada en Siesa y **mirar la bodega de tránsito**.

**Qué se mira:** el saldo de `TRA1` para ese SKU después de la entrada.

| Resultado | Qué significa |
|---|---|
| Saldo = 1 en tránsito | La hipótesis es **correcta**. Hace falta el paso de resolución con supervisor antes de disparar el ETS, o esa unidad queda huérfana para siempre |
| Siesa rechaza el ETS parcial | La hipótesis es **falsa**. El diseño cambia: hay que resolver la diferencia **antes** de intentar la entrada |
| Saldo = 0 | Siesa liquidó el tránsito completo con una entrada parcial. **Parar y avisar** — eso descuadraría inventario en silencio |

**Nota:** hacerlo desde **Recepción**, no desde Tienda. Tienda manda el cuerpo
vacío y el servidor rellena `recibida = enviada`, así que el ejercicio no
probaría nada.

---

## Orden sugerido

**1 primero.** Es el único que bloquea el dinero, y si falla el resto no
importa esta semana.

**4 después**, porque su respuesta decide un diseño y cuesta veinte minutos.

**2 y 3** cuando haya alguien en el CD. El 2 necesita el láser; el 3 necesita
QA y paciencia.

---

## Qué anotar en los cuatro

- **Qué se vio, no qué se concluyó.** El mensaje completo de Siesa, con su
  código. Un mensaje resumido cuesta una semana de diagnóstico.
- **Qué NO se probó.** Si se hizo el 2 con tres etiquetas y no cuatro, decir
  cuál faltó. «0 hallazgos» y «no se miró» tienen que verse distinto.
- La fecha y el ambiente. Un ejercicio en QA no certifica producción, y el
  registro es lo que evita discutirlo dentro de un mes.


---

# Pendientes con fecha

## Sábado 2026-08-15 — aplazado por falta de Siesa QA

**El ejercicio 1 (liquidación real) no se pudo hacer: no hay Siesa QA los
sábados.** Queda para el primer día hábil con ambiente disponible, y sigue
siendo el que bloquea el ciclo del dinero.

## Antes de mover `CONNEKTA_URL` a producción

**Quitar `SKIP_FE_CHECK` primero.** Hoy está en `true` contra QA, que es
inofensivo. Apaga el guard anti-duplicado de factura —`get_factura_desde_pedido`
devuelve lista vacía sin preguntarle a Siesa— y sus tres llamadores vivos
(packing, despacho parcial, reconciliación) leen eso como «no hay factura
previa, seguí».

Son **dos variables que tienen que moverse juntas** y nada las ataba. Desde el
2026-08-15, `/api/health/siesa` lo declara como combinación peligrosa en cuanto
la URL deja de ser QA.

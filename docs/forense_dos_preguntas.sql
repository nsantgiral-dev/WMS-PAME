-- ═══════════════════════════════════════════════════════════════════════
--  Dos preguntas que NO se contestan preguntando
-- ═══════════════════════════════════════════════════════════════════════
--
--  SOLO LEEN. Verificadas contra el esquema real el 2026-08-15.
--
--  QUIÉN, CUÁNDO, Y A QUIÉN LLEGA  ── se llena ANTES de correr nada ──
--
--      Corre:      ____________________
--      Día y hora: martes ____ / ____ , ______  (NO el lunes: compite con el
--                  perfil CARTERA y con el arranque del bloqueo, y pierde)
--      Contra:     [ ] export   [ ] base transaccional  ← NO en horario de
--                                                          operación
--      Resultado llega a: ____________________
--
--  Sin nombre y sin hora, este archivo sobrevive intacto hasta que alguien lo
--  redescubra en octubre. Y si el resultado «llega a Santiago», esto es un
--  instrumento excelente para volver a tener un solo enrutador.
--
-- ═══════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────
--  0 · LA CONSULTA CERO — decide si las otras significan algo
-- ───────────────────────────────────────────────────────────────────────
--
--  UMBRAL COMPROMETIDO ANTES DE MIRAR (2026-08-15):
--
--      Si más del 20% de los traslados ENTREGADA de los últimos 90 días
--      tiene `fecha_entrega` en NULL, el universo NO es representativo y el
--      resultado de las consultas 1 y 2 se reporta como **«no medible»**,
--      nunca como «sin hallazgos».
--
--  El umbral se escribe antes de ver el número a propósito. Elegirlo después
--  es elegir el que conviene — misma disciplina que con los 2.762 clientes.

SELECT COUNT(*)                                             AS entregadas_90d,
       COUNT(*) FILTER (WHERE fecha_entrega IS NULL)        AS sin_fecha_entrega,
       ROUND(100.0 * COUNT(*) FILTER (WHERE fecha_entrega IS NULL)
             / NULLIF(COUNT(*), 0), 1)                      AS pct_sin_fecha,
       CASE WHEN 100.0 * COUNT(*) FILTER (WHERE fecha_entrega IS NULL)
                 / NULLIF(COUNT(*), 0) > 20
            THEN 'NO MEDIBLE — parar acá'
            ELSE 'universo utilizable'
       END                                                  AS veredicto
FROM solicitudes_traslado
WHERE estado = 'ENTREGADA'
  AND fecha_creacion >= NOW() - INTERVAL '90 days';


-- ───────────────────────────────────────────────────────────────────────
--  1 · ¿La recepción CUENTA, o CONFIRMA?
-- ───────────────────────────────────────────────────────────────────────
--
--  ⚠ EL HALLAZGO YA SALIÓ, Y NO DE ESTAS CONSULTAS.
--
--  Hasta el 2026-08-15, `tienda.js` confirmaba con `JSON.stringify({})` y el
--  servidor escribía `recibida = enviada` para TODOS los ítems. Por esa vía una
--  diferencia **no era difícil de detectar: era imposible de expresar** — el
--  software hacía lo mismo que un auxiliar contando contra el número impreso de
--  la remisión, sin siquiera un humano a quien preguntarle.
--
--  Eso YA ESTÁ ARREGLADO: la recepción exige el conteo, y un ítem ausente del
--  mapa se rechaza en vez de rellenarse.
--
--  ⚠ CONSECUENCIA PARA ESTAS CONSULTAS, Y ES LA IMPORTANTE:
--
--      La vía de confirmación NO SE REGISTRA EN NINGUNA PARTE.
--
--  No hay columna, ni bandera, ni log persistido que diga si un traslado se
--  confirmó con conteos o con el cuerpo vacío. Así que **el corte por vía no se
--  puede hacer hacia atrás**, y sin él un `pct` global mezcla dos poblaciones:
--  una donde la diferencia era medible y otra donde era estructuralmente
--  imposible.
--
--  Cortar por PUNTO no lo resuelve: un mismo destino pudo confirmar por las dos
--  vías según quién estuviera en el muelle ese día.
--
--  Entonces, de estas consultas históricas:
--
--      · un número > 0  →  significa algo: hubo diferencias REALES registradas,
--                          y salieron por la vía de Recepción, que sí contaba.
--      · un número = 0  →  NO significa «no hay faltantes». Es compatible con
--                          «nadie cuenta» Y con «nadie PUDO registrar una
--                          diferencia», y no se puede distinguir. Es la misma
--                          lección de las 7.350 facturas: un resultado
--                          observado no revela su mecanismo.
--
--  El corte por vía SÍ es medible **de acá en adelante**, porque desde hoy toda
--  confirmación trae conteos. Volver a correr esto en 30 días es la medición
--  limpia.

SELECT COUNT(*)                                                  AS traslados,
       COUNT(*) FILTER (WHERE dif <> 0)                          AS con_diferencia,
       ROUND(100.0 * COUNT(*) FILTER (WHERE dif <> 0)
             / NULLIF(COUNT(*), 0), 2)                           AS pct,
       SUM(ABS(dif))                                             AS unidades_dif
FROM (
    SELECT s.id,
           SUM(COALESCE(i.cantidad_recibida, 0) - COALESCE(i.cantidad_enviada, 0)) AS dif
    FROM solicitudes_traslado s
    JOIN items_solicitud_traslado i ON i.solicitud_id = s.id
    WHERE s.bodega_origen_siesa = 'NB1'
      AND s.fecha_entrega >= NOW() - INTERVAL '90 days'
      AND s.estado = 'ENTREGADA'
    GROUP BY s.id
) t;


--  1b · Faltante y sobrante son EL MISMO EVENTO.
--
--  El sobrante no se reporta —no da premio y sí levanta sospecha— y el sobrante
--  guardado es el instrumento con el que se tapa el faltante siguiente. Un
--  control que solo detecta faltantes entrena a la red a acumular sobrantes.

SELECT COUNT(*) FILTER (WHERE neto < 0)  AS con_FALTANTE,
       COUNT(*) FILTER (WHERE neto > 0)  AS con_SOBRANTE,
       SUM(neto) FILTER (WHERE neto < 0) AS unidades_faltantes,
       SUM(neto) FILTER (WHERE neto > 0) AS unidades_sobrantes
FROM (
    SELECT s.id,
           SUM(COALESCE(i.cantidad_recibida, 0) - COALESCE(i.cantidad_enviada, 0)) AS neto
    FROM solicitudes_traslado s
    JOIN items_solicitud_traslado i ON i.solicitud_id = s.id
    WHERE s.bodega_origen_siesa = 'NB1'
      AND s.fecha_entrega >= NOW() - INTERVAL '90 days'
      AND s.estado = 'ENTREGADA'
    GROUP BY s.id
) t;


--  1c · CONCENTRACIÓN POR SKU — el corte que decide si el hallazgo es real
--
--  Si las diferencias se reparten en 200 referencias, es merma. Si se
--  concentran en tres y dos son las placeholder, no se descubrieron faltantes:
--  se descubrió otra vez el hueco de itemización, con ropa nueva.

SELECT i.producto_codigo_siesa,
       COUNT(DISTINCT s.id)                                       AS traslados,
       SUM(COALESCE(i.cantidad_recibida, 0)
           - COALESCE(i.cantidad_enviada, 0))                     AS neto_unidades
FROM solicitudes_traslado s
JOIN items_solicitud_traslado i ON i.solicitud_id = s.id
WHERE s.bodega_origen_siesa = 'NB1'
  AND s.fecha_entrega >= NOW() - INTERVAL '90 days'
  AND s.estado = 'ENTREGADA'
  AND COALESCE(i.cantidad_recibida, 0) <> COALESCE(i.cantidad_enviada, 0)
GROUP BY i.producto_codigo_siesa
ORDER BY ABS(SUM(COALESCE(i.cantidad_recibida, 0)
                 - COALESCE(i.cantidad_enviada, 0))) DESC
LIMIT 20;


--  ⚠ SOBRE EL VALOR EN PESOS
--
--  Estas consultas cuentan UNIDADES a propósito. Mil unidades de borrador y mil
--  de resma no son el mismo hallazgo, pero **cualquier cifra en pesos que salga
--  de acá hereda los 632 costos fantasma y las dos referencias placeholder que
--  concentran ~45% del valor sin itemizar**.
--
--  Si el comité pide pesos: se reportan, marcados INFERENCIA contaminada por el
--  maestro de costos, y SIEMPRE junto a la consulta 1c. Sin la concentración,
--  el número grande ancla la discusión y nadie mira de dónde salió.


-- ───────────────────────────────────────────────────────────────────────
--  2 · ¿El ajuste corrige la verdad, o desbloquea una operación?
-- ───────────────────────────────────────────────────────────────────────
--
--  «¿Se puede aprobar con Siesa caído?» se autorresponde. La pregunta real es
--  si el ajuste se usa como llave de picking: «el sistema dice que no hay, pero
--  sí hay, ajústame y despacho».
--
--  ⚠ EL PRIMER NÚMERO NO ES LA MEDIANA.
--
--  `minutos_entre_conteo_y_ajuste` solo existe si hubo conteo. **Un ajuste
--  nacido sin conteo asociado no es, por definición, la corrección de un
--  conteo** — y ésos son precisamente los sospechosos. Si caen como NULL fuera
--  del cálculo, la mediana se computa sobre los casos bien portados y la
--  evidencia más fuerte se descarta en silencio.
--
--  Una mediana de 12 sobre 340 no es una medición. «328 ajustes sin conteo
--  previo» contesta la pregunta con más contundencia que cualquier mediana.

SELECT COUNT(*)                                                   AS ajustes_totales,
       COUNT(*) FILTER (WHERE fecha_cierre IS NOT NULL)           AS con_conteo_previo,
       COUNT(*) FILTER (WHERE fecha_cierre IS NULL)               AS SIN_conteo_previo,
       COUNT(*) FILTER (WHERE operario_id = aprobador_id)         AS mismo_que_conto,
       ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (siesa_triggered_at - fecha_cierre)) / 60.0
       )::numeric, 1) FILTER (WHERE fecha_cierre IS NOT NULL)     AS mediana_min_con_conteo,
       COUNT(*) FILTER (
           WHERE fecha_cierre IS NOT NULL
             AND siesa_triggered_at - fecha_cierre < INTERVAL '1 hour'
       )                                                          AS bajo_una_hora
FROM sesiones_conteo
WHERE siesa_triggered IS TRUE;


--  2b · El detalle de los últimos 20, con los sin-conteo primero.

SELECT s.codigo,
       s.producto_codigo_siesa,
       s.diferencia,
       s.fuente_existencia,                       -- existe desde 2026-08-15
       s.operario_id                              AS conto,
       s.aprobador_id                             AS aprobo,
       (s.operario_id = s.aprobador_id)           AS misma_persona,
       s.fecha_cierre,
       CASE WHEN s.fecha_cierre IS NULL
            THEN 'SIN CONTEO PREVIO'
            ELSE ROUND(EXTRACT(EPOCH FROM (s.siesa_triggered_at - s.fecha_cierre))
                       / 60.0, 1)::text
       END                                        AS minutos_o_sospecha
FROM sesiones_conteo s
WHERE s.siesa_triggered IS TRUE
ORDER BY (s.fecha_cierre IS NULL) DESC, s.siesa_triggered_at DESC
LIMIT 20;


--  2c · Base del ajuste. `NULL` es el histórico anterior a la columna: «no se
--  sabe contra qué se comparó». No se rellena — inventar la procedencia en el
--  campo que existe para no inventarla sería el mismo defecto con otro nombre.

SELECT COALESCE(fuente_existencia, '(histórico, sin registro)') AS base,
       COUNT(*)                                                AS ajustes
FROM sesiones_conteo
WHERE siesa_triggered IS TRUE
GROUP BY 1 ORDER BY 2 DESC;


-- ───────────────────────────────────────────────────────────────────────
--  3 · LA SÉPTIMA CONSULTA — no está en Postgres
-- ───────────────────────────────────────────────────────────────────────
--
--  Un resultado observado no revela su mecanismo. Cero diferencias es
--  compatible con «nadie cuenta» y con «nadie PUEDE registrar una diferencia»,
--  igual que cero facturas bloqueadas era compatible con «no hay control» y con
--  «hay control y alguien lo levanta».
--
--  Falta el lado de permisos, y se contesta en SIESA:
--
--      ¿Cuántos usuarios pueden aprobar un ajuste de inventario hoy,
--      y cuántos de ellos despachan?
--
--  Si la respuesta es «todos los del CEDI», la mediana de minutos es
--  decorativa: ya se sabe lo que va a decir.
--
--  Es el MISMO menú que toca la Gerencia General el lunes con el perfil
--  CARTERA. Se pregunta en esa sesión o no se pregunta.


-- ───────────────────────────────────────────────────────────────────────
--  Qué anotar
-- ───────────────────────────────────────────────────────────────────────
--
--  · El NÚMERO, no la conclusión. «0 de 340» y «2 de 340» llevan a
--    conversaciones distintas.
--  · La fecha y contra qué base. Un resultado sobre QA no dice nada de
--    producción.
--  · Qué NO se pudo correr, y el veredicto de la consulta 0.
--  · Si el resultado de la 1 es cero: escribir **«no medible por la vía
--    histórica»**, no «sin hallazgos». No es lo mismo y la diferencia es todo.

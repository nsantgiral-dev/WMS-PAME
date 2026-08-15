-- ═══════════════════════════════════════════════════════════════════════
--  Dos preguntas que NO se contestan preguntando
-- ═══════════════════════════════════════════════════════════════════════
--
--  Las dos respuestas están en la base. Preguntarlas primero ancla al relato
--  y deja tres versiones de la verdad en vez de dos, porque BK-INV-03 y
--  BK-INV-05 ya declaran una respuesta escrita.
--
--  Es la misma forense que reveló que las 7.350 facturas no fueron falla del
--  sistema sino aprobaciones humanas, una por una.
--
--  SOLO LEEN. Ninguna escribe. Se pueden correr en producción.
--  Verificadas contra el esquema real el 2026-08-15.
-- ═══════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────
--  1 · ¿La recepción en el punto CUENTA, o CONFIRMA?
-- ───────────────────────────────────────────────────────────────────────
--
--  La recepción se hace con la remisión en la mano. Contra un número impreso
--  no se cuenta: se confirma. Si eso es así, la diferencia no se detecta —se
--  absorbe— y reaparece nueve meses después como faltante en un conteo
--  cíclico, sin fecha, sin ruta y sin nombre.
--
--  CÓMO SE LEE:
--    · 0 o casi 0 diferencias  →  NO es excelencia logística. Es la prueba de
--                                 que la recepción no cuenta. Una red de siete
--                                 puntos con traslados perfectos reporta una
--                                 ficción.
--    · un porcentaje visible   →  sí se cuenta, y entonces la pregunta pasa a
--                                 ser qué se hace con la diferencia.
--
--  OJO con el sesgo del propio sistema: tienda manda el cuerpo vacío al
--  confirmar y el servidor rellena `recibida = enviada` (`traslado_service`).
--  Así que un cero acá puede ser el software, no la operación. Por eso la
--  segunda consulta separa por vía.

SELECT
    COUNT(*)                                                   AS traslados,
    COUNT(*) FILTER (WHERE dif > 0)                            AS con_diferencia,
    ROUND(100.0 * COUNT(*) FILTER (WHERE dif > 0) / NULLIF(COUNT(*), 0), 2)
                                                               AS pct_con_diferencia,
    SUM(dif)                                                   AS unidades_de_diferencia
FROM (
    SELECT s.id,
           SUM(ABS(COALESCE(i.cantidad_enviada, 0) - COALESCE(i.cantidad_recibida, 0))) AS dif
    FROM solicitudes_traslado s
    JOIN items_solicitud_traslado i ON i.solicitud_id = s.id
    WHERE s.bodega_origen_siesa = 'NB1'                    -- el CEDI (C.O. 003)
      AND s.fecha_entrega >= NOW() - INTERVAL '90 days'
      AND s.estado = 'ENTREGADA'
    GROUP BY s.id
) t;


--  1b · Lo mismo, por punto de destino. Un punto que SIEMPRE cuadra y otro
--       que nunca, con el mismo origen, dice más que el promedio.

SELECT s.bodega_destino_siesa                                  AS punto,
       COUNT(DISTINCT s.id)                                    AS traslados,
       COUNT(DISTINCT s.id) FILTER (
           WHERE COALESCE(i.cantidad_enviada, 0) <> COALESCE(i.cantidad_recibida, 0)
       )                                                       AS con_diferencia
FROM solicitudes_traslado s
JOIN items_solicitud_traslado i ON i.solicitud_id = s.id
WHERE s.bodega_origen_siesa = 'NB1'
  AND s.fecha_entrega >= NOW() - INTERVAL '90 days'
  AND s.estado = 'ENTREGADA'
GROUP BY s.bodega_destino_siesa
ORDER BY con_diferencia ASC;


--  1c · Y la pregunta que nadie está haciendo: ¿llega MÁS de lo que salió?
--
--  El sobrante no se reporta —reportarlo no da premio y sí levanta sospecha—
--  y el sobrante guardado es el instrumento con el que se tapa el faltante
--  siguiente. Un control que solo detecta faltantes entrena a la red a
--  acumular sobrantes. Faltante y sobrante son el mismo evento.

SELECT
    COUNT(*) FILTER (WHERE neto < 0)  AS traslados_con_FALTANTE,
    COUNT(*) FILTER (WHERE neto > 0)  AS traslados_con_SOBRANTE,
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


-- ───────────────────────────────────────────────────────────────────────
--  2 · ¿El ajuste corrige la verdad, o desbloquea una operación?
-- ───────────────────────────────────────────────────────────────────────
--
--  «¿Se puede aprobar un ajuste con Siesa caído?» se autorresponde: no, el
--  sistema está caído. La pregunta real es si el ajuste se está usando como
--  llave de picking — «el sistema dice que no hay, pero sí hay, ajústame y
--  despacho».
--
--  Si es así, el kardex no es una medición: es un residuo que se corrige a sí
--  mismo cada vez que estorba. Y CUSUM, la descensura, Syntetos-Boylan, el ROP
--  dual y el newsvendor no fallan — se vuelven decorativos, todos calculando
--  sobre una serie que se reescribe sola.
--
--  CÓMO SE LEE:
--    · mediana < 1 hora  Y  solicitante = aprobador en la mayoría
--          →  no es una investigación: es un desbloqueo. Respuesta a B sin
--             preguntarla.
--    · horas o días, y aprobador distinto
--          →  hay segregación real, y la conversación es otra.

SELECT s.codigo,
       s.producto_codigo_siesa,
       s.estado,
       s.diferencia,
       s.fuente_existencia,                        -- desde 2026-08-15
       s.operario_id                              AS conto,
       s.aprobador_id                             AS aprobo,
       (s.operario_id = s.aprobador_id)           AS misma_persona,
       s.fecha_cierre,
       s.siesa_triggered_at,
       ROUND(EXTRACT(EPOCH FROM (s.siesa_triggered_at - s.fecha_cierre)) / 60.0, 1)
                                                  AS minutos_entre_conteo_y_ajuste
FROM sesiones_conteo s
WHERE s.siesa_triggered IS TRUE
ORDER BY s.siesa_triggered_at DESC
LIMIT 20;


--  2b · El resumen, que es lo que se lleva al comité.

SELECT COUNT(*)                                              AS ajustes,
       COUNT(*) FILTER (WHERE operario_id = aprobador_id)    AS mismo_que_conto,
       ROUND(AVG(EXTRACT(EPOCH FROM (siesa_triggered_at - fecha_cierre)) / 60.0), 1)
                                                             AS minutos_promedio,
       ROUND(
           PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (siesa_triggered_at - fecha_cierre)) / 60.0
           )::numeric, 1)                                    AS minutos_mediana,
       COUNT(*) FILTER (
           WHERE siesa_triggered_at - fecha_cierre < INTERVAL '1 hour'
       )                                                     AS bajo_una_hora
FROM sesiones_conteo
WHERE siesa_triggered IS TRUE
  AND fecha_cierre IS NOT NULL
  AND siesa_triggered_at IS NOT NULL;


--  2c · ¿Cuántos salieron con la base tomada del WMS?
--
--  Solo tiene sentido desde el 2026-08-15, cuando nació la columna. `NULL` es
--  el histórico: «no se sabe contra qué se comparó», que es la verdad de todas
--  las filas anteriores. No se rellena — inventar la procedencia en el campo
--  que existe para no inventarla sería el mismo defecto con otro nombre.

SELECT COALESCE(fuente_existencia, '(histórico, sin registro)') AS base_del_ajuste,
       COUNT(*)                                                AS ajustes
FROM sesiones_conteo
WHERE siesa_triggered IS TRUE
GROUP BY 1
ORDER BY 2 DESC;


-- ───────────────────────────────────────────────────────────────────────
--  Qué anotar de las dos
-- ───────────────────────────────────────────────────────────────────────
--
--  · El NÚMERO, no la conclusión. «0 de 340» y «2 de 340» llevan a
--    conversaciones distintas.
--  · La fecha y contra qué base se corrió. Un resultado sobre QA no dice nada
--    de producción, y el registro es lo que evita discutirlo en un mes.
--  · Lo que NO se pudo correr. Si `fecha_entrega` está en NULL en la mitad de
--    los traslados, el universo de la consulta 1 es otro — y hay que decirlo,
--    porque «0 hallazgos» y «no se miró» no pueden verse igual.

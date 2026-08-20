-- Qué caracteres traen DE VERDAD los códigos que viajan a un filtro de Siesa.
--
-- `siesa_filtro.lit()` rechaza comilla simple, comodines de LIKE (% _) y
-- caracteres de control. Es más permisivo de lo que probablemente haga falta:
-- deja pasar espacios, barras y acentos **porque nadie midió** qué contienen
-- los códigos reales. El Gestor de Cartera usa una lista blanca estrecha
-- —`^[A-Za-z0-9_.\-]{1,64}$`— y argumenta bien.
--
-- Esto contesta si esa lista blanca sirve acá o si rompería el escaneo. No
-- se estrecha `lit()` por criterio: se estrecha con el número a la vista.
--
--   psql "$DATABASE_URL" -f docs/medir_codigos_siesa.sql
--
-- Solo lee. No modifica nada.

\echo '=== 1 · códigos que NO caben en la lista blanca estrecha ==='
-- Si sale 0, la lista del Gestor se puede adoptar tal cual.
SELECT 'codigo_siesa'          AS campo,
       count(*)                AS fuera_de_la_lista
  FROM productos
 WHERE codigo_siesa IS NOT NULL
   AND codigo_siesa !~ '^[A-Za-z0-9_.\-]{1,64}$'
UNION ALL
SELECT 'codigo_barras',
       count(*)
  FROM productos
 WHERE codigo_barras IS NOT NULL
   AND codigo_barras !~ '^[A-Za-z0-9_.\-]{1,64}$'
UNION ALL
SELECT 'codigo_barras_empaque',
       count(*)
  FROM productos
 WHERE codigo_barras_empaque IS NOT NULL
   AND codigo_barras_empaque !~ '^[A-Za-z0-9_.\-]{1,64}$';

\echo ''
\echo '=== 2 · CUÁLES son, para poder mirarlos ==='
-- Máximo 40. Si la lista trae basura evidente (espacios sueltos, saltos de
-- línea) el arreglo es limpiar el maestro, no ensanchar el filtro.
SELECT codigo_siesa, codigo_barras, nombre
  FROM productos
 WHERE (codigo_siesa          IS NOT NULL AND codigo_siesa          !~ '^[A-Za-z0-9_.\-]{1,64}$')
    OR (codigo_barras         IS NOT NULL AND codigo_barras         !~ '^[A-Za-z0-9_.\-]{1,64}$')
    OR (codigo_barras_empaque IS NOT NULL AND codigo_barras_empaque !~ '^[A-Za-z0-9_.\-]{1,64}$')
 LIMIT 40;

\echo ''
\echo '=== 3 · el inventario de caracteres realmente usados ==='
-- Un carácter por fila, con cuántos códigos lo contienen. Es lo que dice
-- hasta dónde se puede apretar sin romper nada.
WITH todos AS (
    SELECT codigo_siesa AS v FROM productos WHERE codigo_siesa IS NOT NULL
    UNION ALL
    SELECT codigo_barras FROM productos WHERE codigo_barras IS NOT NULL
    UNION ALL
    SELECT codigo_barras_empaque FROM productos WHERE codigo_barras_empaque IS NOT NULL
), letras AS (
    SELECT DISTINCT v, regexp_split_to_table(v, '') AS c FROM todos
)
SELECT c AS caracter, count(*) AS en_cuantos_codigos
  FROM letras
 WHERE c !~ '[A-Za-z0-9]'          -- los alfanuméricos no son noticia
 GROUP BY c
 ORDER BY 2 DESC;

\echo ''
\echo '=== 4 · lo que YA sería rechazado hoy por lit() ==='
-- Si esto no da 0, hay códigos en el maestro que hoy hacen fallar una
-- consulta de existencia. Es un dato operativo, no solo de seguridad:
-- `get_inventario_fecha` alimenta el ajuste de inventario.
SELECT count(*) AS rechazados_por_lit_hoy
  FROM productos
 WHERE codigo_siesa ~ '[''%\x00-\x1f\x7f]'
    OR codigo_barras ~ '[''%\x00-\x1f\x7f]'
    OR codigo_barras_empaque ~ '[''%\x00-\x1f\x7f]';

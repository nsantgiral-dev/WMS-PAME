# `qa` restaurada el 2026-09-22 — qué pasó y cómo volver atrás

Nota para el equipo. No hay culpables acá: hay una secuencia que dejó el
ambiente en un estado incoherente y la explicación de por qué se movió.

## La secuencia

El 21 de septiembre la rama `qa` se creó desde `main` y recibió trabajo de dos
personas en el mismo día:

    16:49  DavidTrujillo1994  crea qa desde main (9105eb0)
    19:30  DavidTrujillo1994  qa -> 2d9ab27   (Raíz / redirige a /pwa)
    20:58  nsantgiral-dev     qa -> ddfe7b2   (merge de flota/tandas-2026-09)
    21:34  DavidTrujillo1994  qa -> 7a7f780   (RIT, sobre lo anterior)
    22:19  DavidTrujillo1994  qa -> c3d3350   (RIT, la RIT nace apagada)
    22:30  nsantgiral-dev     qa -> e9f3784
    23:54  nsantgiral-dev     qa -> 9b35a7a
    01:57  nsantgiral-dev     qa -> f5e273c
    (sin evento registrado)   qa -> 9105eb0   ← vuelta al punto de partida

El último movimiento no dejó evento de push en GitHub, así que **no se sabe si
fue deliberado o un accidente**. Lo que sí se sabe es que no solo soltó los
commits de la rama de flota: soltó también los **cinco commits de RIT de
David**, que estaban construidos encima de ellos.

## Por qué había que moverla igual

El deploy del servicio web de QA **estaba fallando**. La base de datos de QA
quedó sellada en la revisión `m027mergedoscadenas` (la migración corrió con el
deploy del 21 por la noche), y el código de `9105eb0` no contiene esa revisión.
`railway.toml` corre `flask db upgrade` como `releaseCommand`, así que cada
deploy cortaba con:

    Can't locate revision identified by 'm027mergedoscadenas'

Reproducido antes de tocar nada, extrayendo las migraciones de `9105eb0` a un
directorio temporal y pidiéndole a Alembic esa revisión.

QA seguía sirviendo **solo porque el deploy fallido no tumba al anterior**: el
que estaba en pie era el del 21 por la noche. Cualquier redeploy volvía a
fallar.

Las dos salidas eran avanzar `qa` al código que conoce `m027`, o re-sellar la
base a `n021`. La segunda es peor: las doce tablas y columnas de la serie `m0*`
seguirían existiendo pero fuera del control de Alembic, y el siguiente upgrade
intentaría crearlas de nuevo.

## Qué se verificó antes de mover

Cuatro revisiones independientes, todas sobre contenido y no sobre conteo de
commits:

| Qué se preguntó | Resultado |
|---|---|
| ¿Los 6 commits que el reset soltó sobreviven? | Sí. 9 de 10 archivos byte a byte idénticos; el décimo solo recibió líneas nuevas |
| ¿Las resoluciones de conflicto perdieron algo? | No. `app.js` e `index.html` quedaron como unión de ambos lados; cero módulos `.js` perdidos |
| ¿Hay trabajo en otra rama del remoto? | No. Las 5 ramas y los 2 tags son ancestros de `flota/tandas-2026-09` |
| ¿Algún archivo de `qa` falta en la rama nueva? | No. qa tenía 734 archivos, la rama tiene 904, y `--diff-filter=D` da vacío |

De los 23 símbolos que el diff marca como borrados, 17 siguen existiendo
movidos de lugar y 6 son renombres con la lógica reforzada. La única función
realmente eliminada es `trasConfirmarRecepcion()` de `traslados.js`, que no
tenía ningún caller y mandaba un cuerpo que el backend rechaza desde agosto.

**`origin/main` no se tocó.** Sigue en `9105eb0`.

## Cómo volver atrás, si alguien lo quiere

El estado anterior de `qa` está guardado con nombre:

    git push origin qa-antes-de-restaurar-20260922:qa

Antes de hacerlo, tener en cuenta que eso devuelve el deploy al estado que
fallaba, salvo que también se re-selle la base de QA.

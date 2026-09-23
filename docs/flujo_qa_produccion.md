# QA y producción — qué ambiente es cuál, y cómo llega el código a cada uno

Un solo proyecto en Railway (`determined-intuition`) con **dos ambientes**. Los
dos tienen los mismos servicios con los mismos nombres, así que antes de
reiniciar, mirar variables o abrir una base: **elegir el ambiente arriba a la
izquierda** en Railway. Es el error más fácil de cometer y el más caro.

## Ambientes

| | QA | production |
|---|---|---|
| Rama que despliega | `qa` | `main` |
| URL | `wms-pame-qa.up.railway.app` | `wms-pame-production.up.railway.app` |
| Postgres | `altaria.proxy.rlwy.net:20841` | `metro.proxy.rlwy.net:29311` |
| `RAILWAY_ENVIRONMENT_NAME` | `QA` | `production` |

Servicios, iguales en los dos:

| Servicio | Qué es |
|---|---|
| `WMS-PAME` | La web (gunicorn). preDeploy: `flask db upgrade` — es el que migra la base |
| `WMS-Worker` | Los schedulers pesados (sync, DLQ, alertas por correo) |
| `Postgres` | La base de ese ambiente. Una por ambiente, no compartida |

`positive-integrity` es un servicio viejo, sin dominio y con
`SYNC_SCHEDULER=false`. No es la web: no reiniciar ni configurar nada ahí.

## El flujo por ramas (acordado 2026-09-23 · vigente DESDE la última promoción completa)

> **Cada funcionalidad vive en su rama. `qa` es un banco de pruebas y nunca se
> promueve: a `main` llegan las ramas, una por una, cuando están aprobadas.**

Por qué se cambió: con «`qa` completa o nada», lo terminado de uno esperaba lo
a medias del otro. El 2026-09-23 el filtro de Stock por bodega, probado en QA,
no pudo subir porque `qa` traía 61 commits de otra persona sin terminar.

**Transición:** hay una última promoción de `qa` completa con el flujo anterior
(sección de abajo). Desde que `main` y `qa` quedan iguales, rige este.

```
            main ───────●──────────────●────────▶  producción
             │          ▲              ▲
             │     (aprobada)     (aprobada)
 tu rama     └─●──●─────┤              │
                  │     │              │
 rama de otro ────┼──●──┼──●───────────┘
                  ▼  ▼  │  ▼
            qa ───●──●──●──●──────────────────▶  Railway QA (solo pruebas)
```

### Las cinco condiciones

1. **Toda rama nace de `main`, nunca de `qa`.** Unir una rama a `main` lleva
   TODO lo que contiene: nacida de `qa`, arrastra el trabajo de los demás.
2. **`qa` es banco de pruebas y nunca se une a `main`.** Detalle abajo.
3. **Migraciones.** Dos ramas que migran desde el mismo punto dejan dos *heads*
   de Alembic al llegar a `main`, y el release de producción falla. Quien llega
   segundo actualiza su rama con `main` y encadena su migración (o crea una
   migración de unión) ANTES de unirla. Y deben ser **aditivas** (tablas o
   columnas nuevas que admitan vacío): renombrar o borrar rompe el código que
   todavía no se actualizó.
4. **Después de cada unión a `main`, sincronizar el banco:**
   `git checkout qa && git pull && git merge origin/main && git push origin qa`.
5. **Lo probado no es exactamente lo que sale.** En QA la rama corrió junto a
   otras; a producción llega sola. Revisión rápida en producción después de
   cada unión.

### El ciclo de una rama

```bash
# 1. Nace de main
git checkout main && git pull
git checkout -b stock/filtro-bodega
# 2. Commits EN LA RAMA; subirla (respaldo y visibilidad)
git push -u origin stock/filtro-bodega
# 3. Al banco de pruebas
git checkout qa && git pull && git merge stock/filtro-bodega && git push origin qa
# 4. Probar en wms-pame-qa. Correcciones SIEMPRE en la rama, y se vuelve al paso 3
# 5. Aprobada: la RAMA a main (respaldo previo si trae migraciones)
git checkout main && git pull && git merge stock/filtro-bodega && git push origin main
# 6. Sincronizar el banco (condición 4)
git checkout qa && git pull && git merge origin/main && git push origin qa
# 7. Borrar la rama
git branch -d stock/filtro-bodega && git push origin --delete stock/filtro-bodega
```

### Las reglas del banco (`qa`)

1. **En `qa` no se hacen commits, solo merges de ramas.** Una corrección hecha
   en `qa` se queda en el banco y nunca llega a `main`: a producción saldría la
   versión que falló.
2. **Dirección única: rama → `qa`. Nunca `git merge qa` dentro de una rama**
   (ni hacia `main`): arrastra lo de los demás.
3. **Un conflicto resuelto en `qa` vuelve a aparecer en `main`.** La resolución
   queda solo en el banco. Quien llega segundo a `main` une primero `main` a SU
   rama, resuelve ahí, prueba de nuevo, y recién entonces la une a `main`.
4. **Rama abandonada = sacarla del banco:** `git revert -m 1 <merge de esa rama>`
   en `qa`. Si traía migración, la base de QA quedó migrada: `flask db
   downgrade` o recargar la base de QA.
5. **Una sola rama con migraciones en `qa` a la vez.** Dos migraciones desde el
   mismo punto dejan a `qa` con dos heads y el deploy de QA falla. La segunda
   espera, o se coordina para encadenarla sobre la primera.
6. **El banco se limpia cada tanto** (al cerrar un ciclo grande), avisando
   antes: `git checkout qa && git reset --hard origin/main && git push --force
   origin qa`. Es la **única** excepción a «nunca `--force` sobre `qa`», y vale
   porque `qa` ya es desechable. La base de QA tiene que quedar en la misma
   revisión que el código: recargarla y **redesplegar QA en seguida** (regla 2
   de «Reglas aprendidas»).

**En `main` siguen prohibidos** los commits directos, el `cherry-pick` y el
`--force`. Un hotfix es una rama más que nace de `main` y vuelve a `main`,
seguida de la condición 4.

## El flujo anterior — vigente HASTA la última promoción completa (decidido 2026-09-23)

> **`main` no se toca: solo avanza copiando a `qa` exactamente como está.**

Somos dos. Sin PRs ni ramas por tarea: esa ceremonia no paga con dos personas.
Lo que no se negocia es que producción solo reciba código que ya corrió en QA.

```
commit ──push──▶ qa ──(Railway: pytest + migraciones + deploy QA)──▶ se prueba en QA
                                                                          │
         producción ◀──(Railway: pytest + migraciones)── main ◀──fast-forward──┘
```

**Día a día — directo a `qa`:**

```bash
git checkout qa
git pull --rebase origin qa     # traer lo del otro antes de subir
# … cambios, commit …
git push origin qa              # Railway despliega QA (~13 min con la suite)
```

**Promoción a producción — solo cuando QA se probó:**

```bash
git fetch origin
git push origin origin/qa:main  # fast-forward: main queda idéntica a qa
```

Antes de promover, contestar **sí** a las tres:

1. ¿El deploy de QA de **ese** commit quedó `SUCCESS`?
2. ¿Alguien lo probó a mano en `wms-pame-qa.up.railway.app`?
3. ¿Trae migraciones? Entonces **respaldo de la base de producción antes**
   (Railway → production → Postgres → Backups).

Si git **rechaza** el push a `main`, es que `main` tiene algo que `qa` no. Esa
es la alarma: **no forzar**. Investigar qué entró directo a `main` y unirlo a
`qa` con un merge (no cherry-pick).

**Reglas:**

- **Nunca commits directos en `main`.** Nunca `git cherry-pick` entre `qa` y
  `main`: el 2026-09-22 dos cherry-picks crearon commits gemelos con otros
  hashes, las ramas se separaron, y al unirlas `traslado_usa_rit()` quedó
  definida dos veces **sin que git marcara conflicto**.
- **Hotfix con producción caída:** commit en `main`, y **en seguida**
  `git checkout qa && git merge origin/main && git push origin qa`.
- **Se promueve todo `qa` o nada.** Lo que esté a medias en `qa` viaja a
  producción con la promoción siguiente. Por eso lo que no esté listo va
  **apagado por variable** (como `TRASLADO_USA_RIT`) o se queda local hasta
  terminarlo.
- **Nunca `--force` sobre `qa` ni sobre `main`.** Un push que reescribe `qa`
  suelta el trabajo del otro (ver `docs/qa_restaurada_20260922.md`).

## Reglas aprendidas (2026-09-22)

1. **El `.env` local apunta a la base de QA, nunca a producción.** El
   2026-09-21 el traslado de prueba `ST-20260921-0471` se creó en la base de
   **producción** porque el `.env` apuntaba a `metro`. Antes de correr algo
   que escribe, mirar el host de `DATABASE_URL`: `altaria` es QA, `metro` es
   producción.

2. **Recargar QA con una copia de producción exige redesplegar QA en
   seguida.** La copia trae la revisión de migraciones de producción (hoy
   `n021`) y el código de `qa` puede ir adelante (hoy `m027`). Solo el
   preDeploy de `WMS-PAME` reaplica lo que falta. El 2026-09-22 no se hizo y
   el worker de QA falló con
   `column solicitudes_traslado.clase_traslado does not exist`.

3. **Hasta el corte, QA y producción hablan con el MISMO Siesa**
   (`serviciosqa`), con las mismas credenciales. Un documento que crea uno lo
   ve el otro, y los dos corren DLQ y sincronización contra el mismo ERP. QA no
   es una caja de arena del lado de Siesa: una remisión hecha desde QA es tan
   real como una hecha desde producción.

4. **`SKIP_FE_CHECK` es solo de QA.** Salta la guarda anti-duplicado de
   factura electrónica. En producción no se pone nunca.

5. **Los correos de QA llegan con `[QA]` en el asunto.** Los dos ambientes
   comparten `ALERTA_EMAIL_DEST`; sin marca, una alerta de QA es idéntica a
   una de producción. El prefijo sale de `RAILWAY_ENVIRONMENT_NAME` y lo pone
   `alertas_service.prefijo_ambiente()`, aplicado en `enviar_email`, la única
   puerta a Resend. Producción y local (variable ausente) no llevan prefijo.
   Trinquete: `tests/test_alertas_prefijo_ambiente.py`. Para ver qué prefijo
   usa un proceso: `GET /api/reposicion/alertas/smtp-check` → `prefijo_asunto`.

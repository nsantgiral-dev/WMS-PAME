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

## El flujo

```
rama propia ──PR──▶ qa ──(Railway: pytest + migraciones + deploy QA)──▶ validar en QA
                                                                            │
                     main ◀──────────────────── PR qa → main ◀──────────────┘
                       │
                       └──(Railway: pytest + migraciones + deploy)──▶ producción
```

1. Trabajo en una rama propia, creada desde `origin/qa` (no desde `main`, que
   va atrás).
2. PR hacia `qa`. Al mergear, Railway corre la suite (`buildCommand`), aplica
   migraciones (preDeploy de `WMS-PAME`) y despliega QA.
3. Se valida en QA, contra la URL de QA.
4. PR de `qa` hacia `main`. Al mergear, lo mismo en producción.

**Nunca push directo a `main`.** Tampoco a `qa`: hay más de una persona
trabajando sobre ella, y un push que la mueve suelta el trabajo de los demás
(ver `docs/qa_restaurada_20260922.md`).

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

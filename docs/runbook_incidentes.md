# Runbook de Incidentes — WMS Papelería Medellín
**Para el administrador del sistema · Versión 1.0 · Abril 2026**

> Este documento es de uso interno del administrador. Cubre los escenarios de fallo más probables en los primeros 30 días de operación.

---

## Escala de severidad

| Nivel | Descripción | Tiempo de respuesta |
|-------|-------------|---------------------|
| 🔴 Crítico | Sistema caído, nadie puede trabajar | Inmediato |
| 🟡 Alto | Un rol completo bloqueado (ej: todos los conductores) | < 30 min |
| 🟢 Medio | Un usuario o tarea específica con problema | < 2 horas |

---

## INCIDENTE 1 — App caída (CRASHED en Railway)
**Severidad: 🔴 Crítico**

### Síntomas
- Nadie puede entrar a la PWA
- Railway muestra estado CRASHED en el servicio

### Causa más probable
- Variable de entorno faltante (DATABASE_URL o SECRET_KEY)
- Error en el código del último deploy
- Base de datos PostgreSQL inaccesible

### Pasos
1. Entra a **railway.app** → proyecto → servicio Flask (`positive-integrity`)
2. Tab **Deployments** → ver el log del deploy fallido
3. Busca la línea de error (generalmente al final del log)

**Si el error dice `DATABASE_URL`:**
- Tab Variables → verifica que `DATABASE_URL` esté presente y correcta
- Si falta: agrégala con el valor correcto y Railway redeploya solo

**Si el error es de código:**
- Tab Deployments → busca el deploy anterior que funcionaba
- Toca los 3 puntos → **"Rollback"** → el sistema vuelve a la versión anterior
- Contacta al desarrollador con el log de error

**Si PostgreSQL no responde:**
- Tab del servicio Postgres → Deployments → verifica que esté activo
- Si está caído → Restart

---

## INCIDENTE 2 — Sync con Siesa no funciona
**Severidad: 🟡 Alto**

### Síntomas
- Los pedidos nuevos de Siesa no aparecen en el WMS
- El panel de despacho muestra pedidos viejos

### Causa más probable
- Siesa/Connekta tiene un downtime temporal
- Las credenciales de Connekta cambiaron
- El scheduler de sync se cayó

### Pasos
1. Espera 5 minutos y refresca — el sync corre cada 90 segundos, a veces Siesa tarda
2. Si después de 10 minutos sigue sin jalar:
   - Railway → servicio Flask → Deployments → **Restart**
   - Esto reinicia el scheduler
3. Si después del restart sigue sin funcionar:
   - El problema está en Siesa o Connekta
   - Verifica que Siesa Enterprise esté accesible desde otro computador
   - Contacta al proveedor de Connekta

### Operación manual mientras se resuelve
- Puedes seguir operando manualmente: crea las tareas de picking desde el panel admin
- Los despachos pendientes quedan en cola y se procesan cuando el sync vuelva

---

## INCIDENTE 3 — Operario no recibe tareas
**Severidad: 🟢 Medio**

### Síntomas
- El operario toca "Pedir tarea" y no le sale nada
- Otros operarios sí reciben tareas

### Diagnóstico
1. Panel Admin → **Picking** → filtra estado: PENDIENTE
2. ¿Hay tareas pendientes en el sistema?

**Si NO hay tareas pendientes:**
- Todos los pedidos ya están en proceso o completados — normal
- Verifica si hay pedidos nuevos en Siesa para despachar

**Si SÍ hay tareas pendientes:**
- Verifica el almacén del operario (debe coincidir con el almacén de las tareas)
- Panel Admin → Usuarios → editar el operario → verificar `almacen_id`
- Si está mal → corrígelo y el operario vuelve a intentar

---

## INCIDENTE 4 — Tarea bloqueada sin solución clara
**Severidad: 🟢 Medio**

### Síntomas
- Una tarea lleva más de 2 horas en estado BLOQUEADO
- El supervisor no puede resolver la auditoría

### Pasos
1. Panel Admin → Picking → busca la tarea bloqueada
2. Lee el motivo del bloqueo y las observaciones del operario
3. Ve físicamente a la ubicación

**Si el producto apareció o se puede recoger de otro lado:**
- Toca **"↩ Reabrir al pool"**
- Ajusta el inventario si es necesario desde el panel de inventario

**Si el producto definitivamente no existe:**
- Toca **"✕ Cancelar tarea"**
- Escribe el motivo
- Coordina con ventas para informar al cliente que ese ítem no puede despacharse

---

## INCIDENTE 5 — Conductor no puede cerrar su ruta
**Severidad: 🟡 Alto**

### Síntomas
- El conductor dice que no puede confirmar una parada o cerrar la ruta
- La ruta lleva horas en EN_TRANSITO

### Pasos
1. Panel Admin → **Rutas** → busca la ruta del conductor
2. Revisa qué paradas están sin gestionar (sin estado ENTREGADO/RECHAZADO)
3. Llama al conductor para entender qué pasó en esas paradas
4. Si el conductor ya entregó pero la app no le dejó confirmar:
   - Puedes confirmar las paradas manualmente desde el panel admin
5. Si hay una parada irrecuperable (cliente inaccesible, emergencia):
   - Toca **"⚠ Forzar cierre"**
   - Las paradas sin gestionar quedan como RECHAZADAS
   - Ajusta manualmente en el sistema de recaudo si es necesario

---

## INCIDENTE 6 — Error 500 en alguna función específica
**Severidad: 🟢 Medio**

### Síntomas
- Una función específica da error (ej: al confirmar una tarea, al crear una ruta)
- El resto del sistema funciona normal

### Pasos
1. Railway → servicio Flask → tab **Metrics** → ver logs en tiempo real
2. Reproduce el error para que aparezca en los logs
3. Captura el mensaje de error completo
4. Contacta al desarrollador con:
   - Qué estaba haciendo el usuario
   - El mensaje de error exacto
   - A qué hora ocurrió

---

## Checklist de apertura — Cada mañana

Antes de que empiece el turno, el admin verifica:

- [ ] Railway → el servicio Flask está **Active** (no Crashed)
- [ ] Dashboard → Auditorías urgentes del día anterior resueltas
- [ ] Dashboard → No hay tareas bloqueadas de turnos anteriores
- [ ] Sync Siesa → los pedidos del día ya están sincronizados
- [ ] Conductores → las rutas del día están programadas y asignadas

---

## Checklist de cierre — Cada noche

- [ ] Todas las rutas del día están en estado ENTREGADA
- [ ] Auditorías urgentes del día resueltas o asignadas para mañana
- [ ] No hay operarios con tareas EN_PROCESO abiertas (turno cerrado)
- [ ] Dashboard → productividad del día guardada (screenshot para registro)

---

## Información técnica del sistema

| Componente | Detalle |
|-----------|---------|
| Servidor | Railway — `positive-integrity` |
| Base de datos | PostgreSQL en Railway — `determined-intuition` |
| Repo código | GitHub — `nsantgiral-dev/WMS-PAME` |
| Sync Siesa | Automático cada 90 segundos (7am–8pm) |
| Deploy | Automático al hacer push a GitHub |

---

*Runbook generado para el lanzamiento de WMS Papelería Medellín · Abril 2026*

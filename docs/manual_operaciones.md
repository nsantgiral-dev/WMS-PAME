# Manual de Operaciones — WMS Papelería Medellín
**Versión 1.0 · Abril 2026**

---

## Antes de empezar — Para todos los roles

### Cómo entrar al sistema
1. Abre el navegador del celular o tablet
2. Ve a la dirección que te dio el administrador
3. Ingresa tu correo y contraseña
4. El sistema te lleva directo a tu pantalla según tu rol

### Si no puedes entrar
- Verifica que tengas internet (WiFi o datos)
- Revisa que el correo y contraseña estén bien escritos
- Llama al administrador para que revise tu usuario

### Regla de oro
> **Nunca cierres la app a la mitad de una tarea. Si algo falla, reporta el problema desde la misma app — no abandones la tarea.**

---

---

# ROL: OPERARIO DE PICKING
*Son 3 personas. Su trabajo es recoger productos de las ubicaciones de bodega.*

---

## ¿Qué hace un operario de picking?
El sistema le asigna tareas una por una. Cada tarea le dice:
- **Dónde ir** (ubicación en bodega)
- **Qué recoger** (producto y cantidad)
- **Para quién** (nombre del cliente y número de pedido)

---

## Paso a paso — Turno normal

### 1. Iniciar turno
- Entra a la app con tu usuario y contraseña
- Toca **"Pedir tarea"**
- El sistema te asigna la próxima tarea automáticamente

### 2. Ejecutar la tarea
La pantalla muestra:
```
UBICACIÓN: A-01-03        ← Ve a este lugar en bodega
PRODUCTO: CUADERNO 100H   ← Esto debes recoger
10 / 10                   ← Cuántas unidades
🏪 Librería El Centro     ← Para este cliente
```

- Ve a la ubicación indicada
- Escanea el producto con la cámara (botón 📷) o manualmente
- Cada escaneo = 1 unidad contada
- Cuando llegues a la cantidad requerida, toca **✓ Confirmar**

### 3. Escuchar los sonidos
El celular te avisa con sonido:
- **Pitido agudo** = escaneo correcto ✓
- **Dos pitidos graves** = algo está mal, revisa
- **Melodía corta** = tarea completada 🎉

### 4. Repetir
Después de confirmar, el sistema te da la siguiente tarea automáticamente.

---

## Situaciones especiales

### La ubicación está vacía
1. Toca **⚠ Reportar problema**
2. Selecciona **"📦 Ubicación vacía — no había nada"**
3. Escribe en observaciones qué viste (opcional pero recomendado)
4. El sistema bloquea la tarea y avisa al supervisor automáticamente
5. Pide la siguiente tarea

### Encontraste menos unidades de las pedidas
1. Toca **⚠ Reportar problema**
2. Selecciona **"📉 Faltante parcial — encontré menos"**
3. Escribe cuántas unidades SÍ encontraste
4. El sistema registra lo que encontraste y crea una auditoría para el jefe

### La mercancía está dañada
1. Toca **⚠ Reportar problema**
2. Selecciona **"🚫 Mercancía averiada"**
3. Describe el daño en observaciones
4. El supervisor recibirá la alerta

### El producto no corresponde a lo pedido
1. Toca **⚠ Reportar problema**
2. Selecciona **"❌ Producto incorrecto"**
3. Describe lo que encontraste en observaciones

### A veces te toca hacer un conteo
El sistema puede pedirte que cuentes productos en una ubicación antes de seguir. Es normal. Solo cuenta cuántas unidades hay y confirma. El sistema te dice si cuadra o no.

---

## Lo que NUNCA debes hacer
- ❌ No recojas productos de una ubicación diferente a la indicada
- ❌ No confirmes una tarea si no recogiste la cantidad correcta
- ❌ No dejes el celular bloqueado con una tarea abierta

---

---

# ROL: OPERARIO DE PACKING
*Son 2 personas. Su trabajo es verificar y empacar los pedidos que llegaron del picking.*

---

## ¿Qué hace un operario de packing?
Recibe los productos que recogió picking, los verifica uno por uno, los empaca y crea el bulto físico para despacho.

---

## Paso a paso

### 1. Ver las tareas de packing
- Entra a la app
- Ves las tareas de packing asignadas a ti
- Toca una tarea para empezar

### 2. Verificar productos
Para cada producto de la lista:
- Escanea el código del producto
- El sistema confirma si corresponde al pedido
- Sonido agudo = correcto, sonido grave = producto equivocado

### 3. Cuando terminas de verificar
- El sistema te muestra que el pedido está completo
- El bulto queda listo para que muelle lo cargue a la ruta

### Situación especial — Producto no llega
Si un producto del pedido no llega de picking:
- Espera a que picking lo complete
- Si hay una demora larga, avisa al supervisor

---

---

# ROL: SUPERVISOR
*1 persona. Su trabajo es resolver los problemas que reportan los operarios y mantener la operación fluyendo.*

---

## ¿Qué hace el supervisor?
- Monitorea el dashboard en tiempo real
- Resuelve auditorías urgentes (novedades de picking)
- Gestiona tareas bloqueadas
- Apoya a operarios cuando tienen dudas

---

## Pantalla principal — Dashboard

El dashboard muestra:
- **Auditorías urgentes** — novedades que reportaron los operarios (número en rojo)
- **Tareas bloqueadas** — tareas que no pueden avanzar
- **Productividad operarios** — cuántas tareas completó cada uno hoy

---

## Resolver una Auditoría Urgente

Cuando un operario reporta una novedad, aparece en **Auditorías Urgentes**.

### Paso a paso
1. Toca el número rojo de Auditorías Urgentes
2. Ves la lista de novedades con el motivo de cada una
3. Toca una auditoría para verla
4. Ve físicamente a la ubicación indicada y cuenta el producto
5. Ingresa la cantidad que encontraste
6. Escribe el motivo de la diferencia
7. El sistema ajusta el inventario automáticamente

### Tipos de auditorías que puedes encontrar
| Ícono | Qué significa | Qué hacer |
|-------|--------------|-----------|
| 📦 | Ubicación vacía | Verificar si el producto está en otra ubicación |
| 📉 | Faltante parcial | Contar lo que hay realmente |
| 🚫 | Mercancía averiada | Contar lo que sirve, separar lo dañado |
| ❌ | Producto incorrecto | Identificar qué hay realmente en esa ubicación |

---

## Gestionar una Tarea Bloqueada

Las tareas bloqueadas aparecen con borde rojo en el panel de picking.

### Opciones disponibles

**↩ Reabrir al pool**
- Úsala cuando el problema se resolvió (el producto apareció, se reubicó)
- La tarea vuelve al pool y otro operario puede tomarla
- El operario original NO recupera la tarea

**✕ Cancelar tarea**
- Úsala cuando el producto definitivamente no existe o no se puede recoger
- Requiere escribir el motivo
- El pedido puede quedar incompleto — coordina con ventas

---

## Señales de alerta que debes atender hoy

1. Más de 5 auditorías urgentes acumuladas → operación en riesgo
2. Tarea bloqueada por más de 2 horas → intervenir
3. Operario sin actividad por más de 20 minutos → verificar

---

---

# ROL: CONDUCTOR
*Son 3 personas. Su trabajo es entregar los pedidos a las tiendas y registrar el recaudo.*

---

## ¿Qué hace el conductor?
- Recibe la ruta asignada con las paradas del día
- Confirma cada entrega en la app
- Registra el dinero cobrado
- Toma foto de evidencia si es necesario

---

## Paso a paso — Día de entregas

### 1. Ver tu ruta
- Entra a la app con tu usuario
- Ves tu ruta del día con todas las paradas
- Cada parada muestra el cliente, la dirección y los bultos a entregar

### 2. En cada parada — Entrega exitosa
1. Toca la parada
2. Selecciona **"✓ Entregado"**
3. Selecciona la forma de pago (Efectivo / Transferencia / Crédito)
4. Ingresa el monto cobrado
5. Toma foto de evidencia (opcional pero recomendado)
6. Toca **"Guardar"**

### 3. En cada parada — Entrega parcial
Si el cliente solo recibe parte de los bultos:
1. Toca la parada
2. Selecciona **"Entrega parcial"**
3. Marca cuáles bultos recibió el cliente
4. Anota el motivo en observaciones
5. Guarda

### 4. No se pudo entregar
Si el cliente no estaba, el local estaba cerrado, o rechazó el pedido:
1. Toca la parada
2. Toca **"🚫 No se pudo entregar"**
3. Escribe el motivo (opcional)
4. El sistema registra el rechazo total

### 5. Al terminar todas las paradas
- El sistema muestra el resumen de la ruta
- Ves el total cobrado vs. total esperado
- La ruta queda marcada como entregada automáticamente

---

## Situaciones especiales

### No tengo internet en la zona
La app guarda las confirmaciones en el celular. Cuando vuelvas a tener señal, las sube automáticamente. **No cierres la app.**

### Me equivoqué en una confirmación
Llama al administrador. Puede corregir el estado de una parada desde el panel admin.

### No puedo cerrar la ruta
Llama al administrador. Tiene la opción de forzar el cierre de la ruta desde su panel.

---

## Lo que NUNCA debes hacer
- ❌ No entregues sin confirmar en la app — sin registro no hay evidencia
- ❌ No registres cobros de dinero que no recibiste
- ❌ No marques como entregado si el cliente no recibió

---

---

# ROL: ADMINISTRADOR
*1 persona. Gestiona todo el sistema — usuarios, despacho, rutas y monitoreo general.*

---

## Responsabilidades principales

1. Crear y gestionar usuarios
2. Despachar pedidos desde Siesa al WMS
3. Programar y cerrar rutas de despacho
4. Monitorear el dashboard general
5. Resolver escalaciones del supervisor

---

## Gestión de usuarios

### Crear un usuario nuevo
1. Panel Admin → **Usuarios**
2. Toca **"+ Nuevo usuario"**
3. Completa: nombre, correo, contraseña, rol, almacén
4. Guarda — el usuario ya puede entrar

### Roles disponibles
| Rol | Acceso |
|-----|--------|
| `operario` | Solo tareas de picking/packing/conteo |
| `supervisor` | Dashboard + auditorías + gestión de bloqueados |
| `conductor` | Solo sus rutas de despacho |
| `jefe_almacen` | Todo excepto configuración de sistema |
| `admin` | Acceso total |

---

## Despachar pedidos desde Siesa

1. Panel Admin → **Despacho Siesa**
2. Ves los pedidos pendientes sincronizados desde Siesa
3. Selecciona los pedidos a despachar
4. Toca **"Iniciar despacho"**
5. El sistema crea automáticamente las tareas de picking para los operarios

> El sistema sincroniza pedidos de Siesa automáticamente cada 90 segundos. Si no ves un pedido nuevo, espera un momento y refresca.

---

## Gestión de rutas

### Programar una ruta
1. Panel Admin → **Rutas**
2. Toca **"+ Nueva ruta"**
3. Asigna conductor y vehículo
4. Agrega las paradas (pedidos a entregar)
5. Guarda — el conductor ya ve la ruta en su app

### Estados de una ruta
```
PROGRAMADO → EN_CARGUE → EN_TRANSITO → ENTREGADA
```
- **PROGRAMADO**: ruta creada, pendiente de cargar
- **EN_CARGUE**: muelle está cargando bultos al vehículo
- **EN_TRANSITO**: conductor salió con los pedidos
- **ENTREGADA**: conductor confirmó todas las paradas

### Forzar cierre de ruta (emergencia)
Si un conductor tiene un problema y no puede cerrar la ruta normalmente:
1. Panel Admin → Rutas → busca la ruta EN_TRANSITO
2. Toca el botón **"⚠ Forzar cierre"**
3. Las paradas sin gestionar quedan como RECHAZADAS automáticamente
4. Coordina con el conductor qué pasó realmente

---

## Dashboard — Qué monitorear cada mañana

| Indicador | Acción si está en rojo |
|-----------|----------------------|
| Auditorías urgentes | Asignar al supervisor para resolver hoy |
| Tareas bloqueadas | Revisar motivo y decidir reabrir o cancelar |
| Operarios sin actividad | Verificar si tienen tareas asignadas |

---

## Problemas frecuentes y soluciones

### "Un operario dice que no le salen tareas"
- Verifica en el panel que haya pedidos despachados pendientes
- Verifica que el operario tenga el almacén correcto asignado
- Si hay pedidos pero no salen → cancela las tareas bloqueadas de ese pedido y vuelve a despachar

### "El sync con Siesa no está jalando"
- Espera 2 minutos y refresca
- Si sigue sin jalar, reinicia el servidor desde Railway (Deployments → Restart)

### "Un conductor marcó mal una entrega"
- Panel Admin → Rutas → busca la ruta → busca la parada
- Puedes ver el historial de confirmaciones
- Contacta al desarrollador si necesitas revertir un cobro

---

---

# Runbook — Qué hacer cuando algo falla

*Para el administrador. Situaciones de emergencia y cómo resolverlas.*

---

## La app no carga para nadie
1. Entra a Railway → verifica que el deploy esté activo (no CRASHED)
2. Si dice CRASHED → toca "Restart"
3. Espera 2 minutos
4. Si sigue sin cargar → contacta al desarrollador

## Un operario quedó con una tarea "atascada"
1. Panel Admin → Picking → busca la tarea por nombre del operario
2. Si está EN_PROCESO hace más de 1 hora → toca "Reabrir al pool"
3. El operario debe cerrar y volver a abrir la app

## Se perdió internet en bodega
- Las confirmaciones de los operarios se guardan en el celular offline
- Cuando vuelva el internet, se sincronizan automáticamente
- No hacer nada, esperar que vuelva la conexión

## Un pedido se despachó pero no llegó a picking
1. Verifica en Siesa que el pedido esté activo (no anulado)
2. En el panel Admin → Picking → filtra por la referencia del pedido
3. Si no hay tareas → vuelve a despachar desde Siesa

---

# Contactos de soporte

| Problema | Contacto |
|----------|---------|
| App caída, error de sistema | Desarrollador |
| Pedido mal en Siesa | Área de ventas / Siesa |
| Conductor con problema en ruta | Supervisor → Admin |
| Usuario no puede entrar | Admin crea usuario nuevo |

---

*Documento generado para el lanzamiento de WMS Papelería Medellín · Abril 2026*

# Manual del Administrador — WMS Papelería Medellín
**Versión 1.0 · Abril 2026 · Uso exclusivo del administrador del sistema**

---

## Contenido

1. [Acceso al sistema](#1-acceso-al-sistema)
2. [Pantalla principal y navegación](#2-pantalla-principal-y-navegación)
3. [Dashboard — Centro de control](#3-dashboard--centro-de-control)
4. [Pedidos — Despacho desde Siesa](#4-pedidos--despacho-desde-siesa)
5. [Operarios — Monitoreo del equipo](#5-operarios--monitoreo-del-equipo)
6. [Usuarios — Gestión del equipo](#6-usuarios--gestión-del-equipo)
7. [Stock — Alertas de inventario y catálogo](#7-stock--alertas-de-inventario-y-catálogo)
8. [Connekta — Integración con Siesa](#8-connekta--integración-con-siesa)
9. [Muelle — Cargue de vehículos](#9-muelle--cargue-de-vehículos)
10. [Rutas — Despacho última milla](#10-rutas--despacho-última-milla)
11. [Inventario — Conteos y auditorías](#11-inventario--conteos-y-auditorías)
12. [Traslados — Movimientos entre bodegas](#12-traslados--movimientos-entre-bodegas)
13. [Flujo completo del día](#13-flujo-completo-del-día)
14. [Resolución de problemas frecuentes](#14-resolución-de-problemas-frecuentes)

---

---

## 1. Acceso al sistema

### Cómo entrar

1. Abre el navegador (Chrome o Safari recomendado)
2. Escribe la dirección del sistema (la URL de Railway que te dio el desarrollador)
3. Ingresa tu correo de administrador y contraseña
4. Toca **"Ingresar"**

El sistema te lleva automáticamente a la pantalla de administrador. Si ves la pantalla de operario es porque el usuario con el que entraste tiene rol equivocado.

### Indicador de conexión

En la parte superior de la pantalla verás un punto pequeño:
- **● Online (verde)** — el sistema está conectado y funcionando
- **● Offline (rojo)** — sin internet, algunas funciones no estarán disponibles

### Cerrar sesión

Toca tu nombre en la parte superior de la pantalla y selecciona **"Cerrar sesión"**. Siempre cierra sesión cuando termines el turno para evitar que otra persona use tu cuenta.

---

---

## 2. Pantalla principal y navegación

Al entrar como administrador ves una barra de navegación en la parte inferior con 10 secciones (tabs). La pantalla se actualiza automáticamente cada 30 segundos con los datos más recientes.

### Las 10 secciones del admin

| Tab | Icono | Para qué sirve |
|-----|-------|----------------|
| **Dashboard** | 📊 | Vista general del estado de la operación |
| **Pedidos** | 📦 | Ver pedidos de Siesa y despacharlos a bodega |
| **Operarios** | 👥 | Monitorear productividad del equipo |
| **Usuarios** | ⚙️ | Crear y editar cuentas de usuarios |
| **Stock** | 📉 | Alertas de inventario bajo y catálogo de productos |
| **Connekta** | 🔗 | Estado de la integración con Siesa Enterprise |
| **Muelle** | 🚛 | Controlar el cargue de bultos a los vehículos |
| **Rutas** | 🗺️ | Programar y gestionar rutas de entrega |
| **Inventario** | 📋 | Conteos cíclicos, auditorías y ajustes |
| **Traslados** | ↔️ | Movimientos de mercancía entre bodegas |

Para cambiar de sección, toca el nombre en la barra inferior. La sección activa se resalta.

---

---

## 3. Dashboard — Centro de control

**Acceso:** Tab **Dashboard** (se abre por defecto al entrar)

El dashboard es la primera pantalla que debes revisar cada mañana. Te da una fotografía instantánea de la operación.

---

### 3.1 Los 4 KPIs principales

En la parte superior ves 4 números grandes:

**Picking activo**
- Cuántas tareas de picking están en proceso o pendientes en bodega ahora mismo
- Si es alto y los operarios están parados → hay un problema de bloqueos

**Pack hoy**
- Cuántas facturas/pedidos generó el empacador hoy
- Número que crece a medida que avanza el día

**Recepción hoy**
- Cuántas recepciones de mercancía se confirmaron hoy
- Útil para coordinar con proveedores

**Alertas stock**
- Cuántos productos están por debajo del stock mínimo
- Si es mayor a 0 → ir al tab Stock para ver cuáles

---

### 3.2 Gráfica de actividad

Debajo de los KPIs hay una gráfica de barras que muestra:
- Picking total activo
- Picking completado hoy
- Packing completado hoy
- Facturas generadas hoy
- Conteos que cuadraron hoy

Úsala para evaluar de un vistazo si el ritmo del día es normal comparado con días anteriores.

---

### 3.3 Auditorías urgentes (PRIORIDAD ALTA)

Si hay una barra roja con el mensaje **"Auditorías urgentes"** y un número, significa que los operarios reportaron problemas en bodega que requieren tu atención o la del supervisor.

**Qué hacer:**
1. Toca la barra roja para expandir la lista
2. Ves cada auditoría con el motivo (📦 ubicación vacía, 📉 faltante, 🚫 averiada, ❌ producto incorrecto)
3. Asígnale cada una al supervisor para que vaya físicamente a la ubicación y resuelva

Si no se atienden, los pedidos de esos clientes quedan incompletos.

---

### 3.4 Tareas bloqueadas

Si hay una sección **"Tareas bloqueadas"** con un número, significa que hay tareas de picking que un operario reportó como problema y que nadie ha resuelto todavía.

**Qué hacer:** Ve al tab **Pedidos** para ver el detalle y tomar acción (reabrir o cancelar cada tarea).

---

### 3.5 Últimos movimientos

En la parte inferior del dashboard ves los últimos 8 movimientos de inventario en tiempo real:
- **Verde (+)** = entrada de mercancía (recepción, ajuste de entrada)
- **Rojo (-)** = salida de mercancía (picking, despacho)

Úsala para confirmar que la operación está activa.

---

---

## 4. Pedidos — Despacho desde Siesa

**Acceso:** Tab **Pedidos**

Esta es la sección más usada en el día a día. Aquí ves los pedidos que vienen de Siesa y los conviertes en tareas de trabajo para los operarios de bodega.

---

### 4.1 Entender la pantalla

La pantalla tiene dos secciones:

**PENDIENTES EN SIESA**
Lista de pedidos que Siesa tiene como pendientes y que todavía no se han despachado completamente. El sistema los sincroniza automáticamente cada 90 segundos.

**TAREAS EN BODEGA**
Lista de las tareas de picking activas en este momento — qué está recogiendo cada operario, qué está bloqueado, qué está en cola.

---

### 4.2 Estados de un pedido en Siesa

Cada pedido en la sección "Pendientes en Siesa" muestra un botón o indicador en la derecha:

| Lo que ves | Qué significa | Qué hacer |
|-----------|--------------|-----------|
| Botón blanco **"Despachar"** | El pedido está listo para entrar a bodega | Tócalo para crear las tareas |
| **"En picking"** (azul) | Los operarios están recogiendo este pedido | Monitorear en Tareas en Bodega |
| **"Packing pendiente"** (amarillo) | Picking terminó, el empacador debe verificar | Verificar que haya empacador disponible |
| **"En empaque"** (morado) | El empacador está verificando | Normal, esperar |
| **"✓ Despachado en Siesa"** (verde) | El ciclo está completo | No se necesita acción |
| **"⚠ Error Siesa"** (rojo) | Siesa no pudo crear la remisión | Ver sección 4.5 |

---

### 4.3 Cómo despachar un pedido — Paso a paso

Este es el proceso más importante que hace el admin cada día.

**Paso 1 — Identificar el pedido**
- En la sección "Pendientes en Siesa" busca los pedidos con el botón blanco **"Despachar"**
- Cada pedido muestra: número de pedido, nombre del cliente, cantidad de productos y unidades
- Si un pedido tiene el aviso ⚠ *"X sin registrar en WMS"* significa que hay productos en Siesa que no existen en el catálogo del WMS → contactar al desarrollador para sincronizar

**Paso 2 — Tocar "Despachar"**
- Toca el botón blanco **"Despachar"** del pedido
- El sistema muestra una confirmación con los detalles del pedido
- Revisa que los productos y cantidades estén correctos
- Toca **"Confirmar despacho"**

**Paso 3 — El sistema crea las tareas automáticamente**
El WMS:
- Determina en qué ubicación está cada producto (FEFO — lo más próximo a vencer primero)
- Reserva el inventario para que no lo tome otro pedido
- Crea una tarea de picking por cada producto
- Las tareas aparecen en la cola de los operarios inmediatamente

**Paso 4 — Monitorear el progreso**
En la sección "Tareas en Bodega" verás cada tarea con su estado:
- ⏳ **En cola** — esperando que un operario la tome
- 👤 **En proceso** — un operario la está ejecutando ahora
- 🔴 **Bloqueado** — el operario reportó un problema (requiere acción del admin)

---

### 4.4 Gestionar una tarea BLOQUEADA

Cuando un operario reporta un problema (ubicación vacía, faltante, mercancía averiada, producto incorrecto), la tarea queda en estado BLOQUEADO con borde rojo.

La tarjeta muestra:
- El producto y la ubicación
- El motivo del bloqueo (ej: 📦 Ubicación vacía)
- Las observaciones que escribió el operario (en rojo cursiva si las escribió)

**Tienes dos opciones:**

**↩ Reabrir al pool**
- Úsala cuando el problema se puede resolver: el producto está en otra ubicación, el supervisor hizo el ajuste de inventario, o simplemente se quiere que otro operario lo intente
- El sistema libera el inventario bloqueado y la tarea vuelve a la cola
- El primer operario disponible la tomará

**✕ Cancelar tarea**
- Úsala cuando el producto definitivamente no existe o no puede despacharse
- El sistema te pide escribir un motivo (obligatorio)
- El inventario bloqueado se libera
- **Importante:** Si cancelas una tarea, ese ítem del pedido quedará sin despachar. Debes coordinar con ventas para informar al cliente

---

### 4.5 Error Siesa — Qué hacer

Si un pedido muestra **"⚠ Error Siesa"** (fondo rojo oscuro), significa que:
- El picking y packing se completaron correctamente
- Pero cuando el sistema intentó crear la remisión en Siesa, falló
- El pedido físicamente está empacado y listo, pero Siesa no lo sabe

**Solución:**
1. El empacador debe abrir su app y buscar ese pedido
2. Tocar **"Cerrar caja"** nuevamente para reintentar el registro en Siesa
3. Si sigue fallando → contactar al desarrollador con el número de pedido

---

### 4.6 Cuando no hay pedidos pendientes

Si la sección "Pendientes en Siesa" muestra **"✓ Sin pedidos pendientes en Siesa"** (caja verde), significa que todos los pedidos del día están despachados o en proceso. Es una buena señal.

Si sabes que hay pedidos nuevos en Siesa pero no aparecen, espera 2 minutos — el sync es automático cada 90 segundos. Si después de 5 minutos siguen sin aparecer, ve al tab **Connekta** para verificar el estado de la integración.

---

---

## 5. Operarios — Monitoreo del equipo

**Acceso:** Tab **Operarios**

Esta sección te muestra en tiempo real el rendimiento de cada miembro del equipo de bodega.

---

### 5.1 Qué información ves de cada operario

Cada tarjeta muestra:

**Nombre y rol**
El nombre del operario y sus capacidades: etiqueta azul "Picker" si puede hacer picking, etiqueta morada "Empacador" si puede hacer packing.

**Tareas últimos 7 días**
El número grande a la derecha es el total de tareas completadas en los últimos 7 días. El operario con más tareas aparece en verde, los demás en blanco. El de menor productividad aparece más opaco.

**Detalle por tipo**
- Pick: X — cuántos pickings completó en 7 días
- Pack: X — cuántos packings completó en 7 días
- Conteos: X — cuántos conteos cíclicos completó en 7 días

**Barra de conteos hoy (si es picker)**
Una mini barra de progreso muestra cuántos conteos intercalados hizo hoy vs. su capacidad diaria configurada:
- Verde — dentro del límite normal
- Amarillo — llegando al límite (70%+)
- Rojo — en el límite o superado (100%)

---

### 5.2 Cómo interpretar los datos

**Operario con tareas = 0**
- O llegó tarde / no ha comenzado
- O hay un problema con su usuario (almacén incorrecto)
- O no hay tareas en la cola

**Todos los operarios con pocas tareas**
- Puede ser que no se han despachado pedidos del día (ir a tab Pedidos)
- O hay muchas tareas BLOQUEADAS que tapan la cola

**Un operario con muchas más tareas que los demás**
- Normal si es el más rápido
- Verificar que los demás no tengan problemas técnicos

**Barra de conteos en rojo**
- El operario llegó a su límite diario de conteos intercalados
- El sistema ya no le asigna conteos, solo picking
- Esto es intencional — para no afectar la productividad de picking

---

---

## 6. Usuarios — Gestión del equipo

**Acceso:** Tab **Usuarios**

Aquí creas y editas las cuentas de todos los usuarios del sistema.

---

### 6.1 Ver la lista de usuarios

Al entrar ves todos los usuarios con:
- Nombre y correo
- Rol (en color: rojo = admin, gris = otros)
- Badges de capacidades: azul "Picker", morado "Empacador"
- Botón **"Editar"** en cada uno

---

### 6.2 Crear un usuario nuevo — Paso a paso

1. Toca **"+ Nuevo usuario"** (botón en la parte superior)
2. Aparece el formulario. Completa cada campo:

**Nombre completo**
El nombre real de la persona. Aparecerá en el dashboard y en los reportes.

**Email**
La dirección de correo que usará para entrar. No puede repetirse entre usuarios. Una vez creado el usuario, el email no se puede cambiar.

**Contraseña**
Mínimo 6 caracteres. Dile la contraseña al usuario en persona — el sistema no la envía por correo. El usuario la puede cambiar después si tiene acceso.

**Rol**
Determina qué pantalla ve al entrar y qué puede hacer:

| Rol | Pantalla que ve | Puede hacer |
|-----|----------------|-------------|
| `operario` | Pantalla de picking/packing | Solo sus tareas asignadas |
| `recepcionista` | Pantalla de recepción | Recibir mercancía y registrar devoluciones |
| `conductor` | Pantalla de conductor | Ver sus rutas y confirmar entregas |
| `tienda` | Pantalla de tienda | Consultar stock disponible para pedir |
| `supervisor` | Panel admin completo | Todo excepto configuración de sistema |
| `jefe_almacen` | Panel admin completo | Todo excepto configuración de sistema |
| `admin` | Panel admin completo | Acceso total al sistema |

**Capacidades operativas (para operarios)**

*Picker (checkbox azul)*
- Actívalo si el operario va a hacer tareas de picking en bodega
- Por defecto está activo para todos los operarios
- Desactívalo si el operario es exclusivamente empacador

*Empacador / Auditor (checkbox morado)*
- Actívalo si el operario va a trabajar en la mesa de empaque
- Un operario puede ser picker Y empacador al mismo tiempo
- Si solo es empacador (sin picker activado) → va directo a la pantalla de packing al entrar

*Conteos cíclicos por día*
- Número de conteos de inventario que el sistema le puede intercalar durante su turno
- Valor recomendado: 15 a 25
- Valor 0 = sin límite (no recomendado para bodega activa)
- El sistema no le asignará más conteos de este número al día, priorizando el picking

**Campos especiales para rol "tienda"**
Si el rol es "tienda", aparecen dos campos adicionales:
- *ID Bodega Siesa* — el código de la bodega en Siesa que corresponde a ese punto de venta (ej: TP1). Preguntar al área de Siesa cuál es.
- *Nombre punto de venta* — cómo se identificará ese punto en el sistema (ej: "Tienda Centro")

3. Toca **"Crear usuario"**
4. El usuario ya puede entrar al sistema con su correo y contraseña

---

### 6.3 Editar un usuario existente

1. Busca el usuario en la lista
2. Toca **"Editar"**
3. Aparece el formulario con los datos actuales
4. Cambia lo que necesites
   - **El email no se puede cambiar** (aparece en gris)
   - **Si dejas la contraseña vacía**, la contraseña actual no cambia
   - **Si escribes una contraseña nueva**, la contraseña actual se reemplaza
5. Toca **"Guardar cambios"**

**Casos de uso frecuentes:**
- Cambiar contraseña a un operario que la olvidó → editar y escribir contraseña nueva
- Dar capacidad de packing a un operario → editar y activar el checkbox Empacador
- Cambiar el límite de conteos → editar y cambiar el número

---

### 6.4 Desactivar un usuario

Actualmente el sistema no tiene un botón de "desactivar". Si un empleado sale de la empresa:
1. Edita su usuario
2. Cambia la contraseña a algo que él no conozca
3. Esto le impide entrar sin necesidad de borrar el historial de su actividad

---

---

## 7. Stock — Alertas de inventario y catálogo

**Acceso:** Tab **Stock**

Esta sección tiene dos partes: alertas de productos con stock bajo y el catálogo completo de productos.

---

### 7.1 Alertas de stock bajo

En la parte superior ves los productos que están por debajo de su stock mínimo configurado.

Cada alerta muestra:
- Nombre y código del producto
- Clasificación ABC (A, B o C)
- Badge de urgencia: **CRITICO** (rojo) o **ADVERTENCIA** (amarillo)
- Stock actual vs. stock mínimo

**CRITICO** = el stock está en 0 o muy cercano. Si hay pedidos de este producto, los operarios van a encontrar ubicaciones vacías.

**ADVERTENCIA** = el stock está bajo pero aún hay algo. Hay tiempo para reabastecerse.

**Qué hacer con las alertas:**
- Coordinar compra o traslado desde otra bodega para los productos CRITICO
- Verificar físicamente si el número del sistema coincide con lo que hay en la ubicación

Si no hay alertas, verás **"✓ Sin alertas"** en verde.

---

### 7.2 Catálogo de productos

Debajo de las alertas está el catálogo completo de todos los productos del sistema.

**Buscar un producto:**
1. Escribe el nombre o código en el campo de búsqueda
2. El catálogo se filtra automáticamente
3. Ves el código, nombre, unidad de medida y stock total del producto

**Navegar el catálogo:**
- Usa los botones de paginación (← →) para ver más productos
- El total de productos aparece en la esquina superior

**Para qué sirve el catálogo:**
- Verificar que un producto específico está en el sistema antes de despacharlo
- Consultar el stock disponible de cualquier producto
- Identificar productos sin registrar cuando aparece el aviso ⚠ en un pedido de Siesa

---

---

## 8. Connekta — Integración con Siesa

**Acceso:** Tab **Connekta**

Esta sección muestra el estado de la conexión con Siesa Enterprise y tiene herramientas de configuración inicial y reconciliación.

---

### 8.1 Estados de Connekta

El indicador principal muestra uno de tres estados:

**PRODUCCIÓN (verde)**
Todo funciona correctamente. El sistema lee y escribe en Siesa de forma real.

**MODO ENSAYO (naranja)**
Las credenciales están activas y los datos son reales, pero los POSTs (escritura a Siesa) están bloqueados en el servidor. Útil para probar sin afectar Siesa.
- Para activar producción completa: borrar la variable `MODO_ENSAYO` en Railway

**SIMULACIÓN (amarillo)**
No hay credenciales configuradas. Todo es simulado localmente. Los pedidos que aparecen son de prueba, no de Siesa real.

---

### 8.2 Tabla de detalles

Debajo del indicador principal ves una tabla con:

| Campo | Qué indica |
|-------|-----------|
| Credenciales | ✓ Activas (verde) = credenciales configuradas en Railway |
| GETs (lectura) | ✓ Real = lee datos reales de Siesa |
| POSTs (escritura) | ✓ Activos = puede registrar en Siesa / Bloqueados = modo ensayo |
| Bodega | Código de bodega configurado en Siesa |
| CO | Centro de operación configurado |

---

### 8.3 Sincronizar catálogo + stock inicial

Botón: **"↻ Sincronizar catálogo + cargar stock inicial"**

**Cuándo usarlo:**
- Al poner en marcha el sistema por primera vez
- Cuando entran productos nuevos a Siesa y necesitan aparecer en el WMS
- Cuando el stock del WMS está muy desactualizado y necesita realinearse con Siesa

**Qué hace:**
- **Fase 1/2 (~2 min):** Descarga todo el catálogo de productos desde Siesa y lo crea/actualiza en el WMS
- **Fase 2/2 (~60 seg):** Carga el stock actual de cada producto desde Siesa hacia el WMS

**Cómo ejecutarlo:**
1. Toca el botón
2. El botón dice "↻ Procesando..." y no se puede volver a tocar
3. Espera — debajo del botón aparece el progreso ("⏳ Fase 1/2...", "⏳ Fase 2/2...")
4. Cuando termina aparece el resultado en verde: "✓ catálogo: X creados · X actualizados — stock: X nuevos · X actualizados"

**Importante:** Este proceso puede tardar 3-4 minutos. No cierres ni recargues la pantalla mientras está corriendo.

---

### 8.4 Reconciliación WMS vs Siesa

Botón: **"⚖ Ver reconciliación WMS vs Siesa"**

**Cuándo usarlo:**
- Cuando sospechas que el inventario del WMS no coincide con Siesa
- Después de un día de operación pesada para verificar consistencia
- Cuando hay quejas de que el WMS muestra stock pero físicamente no hay

**Qué hace:**
Compara el stock de cada producto en el WMS contra el stock que reporta Siesa, y lista las diferencias.

**Cómo leer el resultado:**

*Sin diferencias:*
`✓ Sin diferencias — WMS y Siesa coinciden (X productos)`

*Con diferencias:*
`⚠ X diferencias de Y productos`

Debajo aparece la lista de los 20 productos con mayor diferencia:
- Nombre y código del producto
- **WMS: X** (lo que dice el WMS)
- **Siesa: X** (lo que dice Siesa)
- Diferencia en verde (+) o rojo (-) según a quién le "sobra"

**Qué hacer con las diferencias:**
- Diferencia pequeña (±1 o ±2 unidades): probablemente picking en proceso, ignorar
- Diferencia grande: investigar — puede haber merma, robo, o un error de registro
- Si WMS > Siesa: el WMS "tiene más" de lo que Siesa registra — posible recepción que no se confirmó en Siesa
- Si WMS < Siesa: el WMS "tiene menos" — posible picking que no se descontó correctamente

El proceso tarda ~2 minutos. No cierres la pantalla mientras corre.

---

---

## 9. Muelle — Cargue de vehículos

**Acceso:** Tab **Muelle**

Esta sección controla el proceso físico de cargar los bultos empacados a los vehículos de despacho, antes de que el conductor salga con los pedidos.

---

### 9.1 Qué es el muelle

El muelle es el área física donde los bultos empacados esperan para ser cargados al camión. El operario de muelle escanea cada bulto para confirmar que sí va en la ruta correcta.

---

### 9.2 Sin ruta seleccionada

Si aún no has seleccionado una ruta para cargar, la pantalla muestra los bultos disponibles agrupados por municipio de destino.

Ves cada grupo con:
- Nombre del municipio
- Cuántos bultos hay listos para ese destino
- El detalle de cada bulto (pedido, cliente, unidades)

Esta vista te ayuda a planificar cuántos bultos van en cada ruta antes de asignar un vehículo.

---

### 9.3 Con ruta seleccionada — Proceso de cargue

**Seleccionar la ruta a cargar:**
1. Busca la ruta en el tab **Rutas** (estado: EN_CARGUE)
2. O usa el selector en la parte superior del tab Muelle

**Una vez seleccionada la ruta:**

La pantalla muestra el manifiesto de carga: todos los bultos que deben ir en esa ruta, agrupados por municipio/destino.

Cada bulto muestra:
- Número de bulto
- Cliente y número de pedido
- Estado: ⏳ Pendiente o ✓ Cargado

**Confirmar cada bulto:**
El operario de muelle escanea el código del bulto con su pistola o cámara. Al escanearlo:
- El bulto cambia a estado **✓ Cargado**
- El contador de progreso avanza (ej: 12/20 bultos)

**Cuando todos los bultos están cargados:**
La pantalla muestra el manifiesto completo en verde. El admin puede entonces cambiar el estado de la ruta a EN_TRANSITO para que el conductor pueda salir.

---

### 9.4 Cómo avanzar la ruta a EN_TRANSITO

Cuando el muelle confirma que todos los bultos están cargados:
1. Ve al tab **Rutas**
2. Busca la ruta (debe estar en estado EN_CARGUE)
3. Toca **"▶ Pasar a tránsito"**
4. El conductor verá la ruta en su app y puede comenzar las entregas

---

---

## 10. Rutas — Despacho última milla

**Acceso:** Tab **Rutas**

Esta sección tiene cuatro sub-secciones accesibles desde tabs internos: **Rutas**, **Maestras**, **Vehículos** y **Conductores**.

---

### 10.1 Lista de rutas

Al entrar al tab Rutas ves la lista de rutas activas y recientes. Cada ruta muestra:
- Nombre de la ruta y conductor asignado
- Fecha y vehículo
- Estado actual
- Número de paradas y bultos

**Estados de una ruta:**

```
PROGRAMADO → EN_CARGUE → EN_TRANSITO → ENTREGADA
```

| Estado | Color | Qué significa |
|--------|-------|--------------|
| PROGRAMADO | Morado | Ruta creada, el conductor no ha salido |
| EN_CARGUE | Amarillo | El muelle está cargando los bultos |
| EN_TRANSITO | Azul | El conductor está haciendo entregas |
| ENTREGADA | Verde | Todas las paradas gestionadas |

---

### 10.2 Programar una ruta nueva — Paso a paso

1. Toca **"+ Nueva ruta"**
2. Completa los datos:
   - **Nombre** — identificador de la ruta (ej: "Ruta Norte - 14 Abr")
   - **Conductor** — selecciona de la lista de conductores activos
   - **Vehículo** — selecciona el vehículo asignado
   - **Fecha** — fecha de la entrega
3. Agrega las paradas:
   - Cada parada corresponde a un pedido a entregar
   - Selecciona los pedidos que van en esta ruta (deben tener bultos empacados y listos)
4. Toca **"Crear ruta"**
5. La ruta queda en estado PROGRAMADO

---

### 10.3 Flujo completo de una ruta

**Día de despacho — paso a paso para el admin:**

| Hora | Acción | Dónde |
|------|--------|-------|
| Mañana | Verificar que los pedidos del día están empacados | Tab Pedidos |
| Antes de cargar | Crear la ruta con conductor y vehículo | Tab Rutas → Nueva ruta |
| Inicio de cargue | Cambiar estado a EN_CARGUE | Tab Rutas → botón "Iniciar cargue" |
| Durante cargue | Operario de muelle escanea bultos | Tab Muelle |
| Cargue completo | Cambiar a EN_TRANSITO | Tab Rutas → "Pasar a tránsito" |
| Conductor en calle | Monitorear paradas | Tab Rutas → ver detalle |
| Fin del día | Revisar planilla de cuadre | Tab Rutas → botón "💰 Planilla" |

---

### 10.4 Acciones disponibles en cada ruta

Dependiendo del estado de la ruta, ves diferentes botones:

**"▶ Iniciar cargue"** (estado: PROGRAMADO)
- Cambia la ruta a EN_CARGUE
- A partir de este momento el muelle puede empezar a escanear bultos
- El conductor todavía no ve la ruta como activa

**"▶ Pasar a tránsito"** (estado: EN_CARGUE)
- Solo funciona si TODOS los bultos están escaneados en muelle
- Si hay bultos sin escanear el sistema lo bloquea y te avisa cuáles faltan
- Cambia la ruta a EN_TRANSITO
- El conductor ve la ruta en su app y puede empezar a confirmar paradas

**"📍 Ver paradas"** (cualquier estado)
- Abre el detalle de cada parada: cliente, dirección, bultos, estado de entrega
- Puedes ver si el conductor confirmó cada parada y con qué forma de pago

**"💰 Planilla"** (estados: EN_TRANSITO o ENTREGADA)
- Muestra la planilla de cuadre de la ruta
- Total esperado (lo que debía cobrar el conductor)
- Total cobrado (lo que el conductor registró en la app)
- Detalle por forma de pago: Efectivo, Transferencia, Crédito
- Estado financiero: PENDIENTE (aún no cuadrada) o LIQUIDADA (cuadre confirmado)

**"⚡ Forzar cierre"** (estado: EN_TRANSITO — solo emergencias)
- Úsala cuando el conductor tiene un problema grave y no puede completar la ruta normalmente (accidente, vehículo averiado, emergencia)
- El sistema marca automáticamente como RECHAZADAS todas las paradas que el conductor no gestionó
- La ruta cambia a ENTREGADA
- **Esta acción es irreversible** — confirma solo si estás seguro
- Después deberás revisar manualmente qué pasó con los pedidos rechazados

---

### 10.5 Planilla de cuadre — Cómo liquidarla

Al final del día cuando el conductor regresa:
1. Tab Rutas → busca la ruta → toca **"💰 Planilla"**
2. Ves el resumen: total cobrado vs. total esperado
3. Revisa que los montos coincidan con el dinero que entregó el conductor
4. Si cuadra → toca **"Marcar como liquidada"**
5. La ruta queda con estado financiero LIQUIDADA

---

### 10.6 Rutas maestras y vehículos

**Rutas maestras** (sub-tab "Maestras")
Son plantillas de rutas recurrentes — misma secuencia de clientes que se repite semana a semana. Úsalas para no crear la misma ruta desde cero cada día.

**Vehículos** (sub-tab "Vehículos")
Lista de los vehículos disponibles para despacho. Aquí registras los datos del vehículo (placa, tipo, capacidad) para asignarlo a las rutas.

**Conductores** (sub-tab "Conductores")
Lista de los conductores activos con su información. Los conductores deben tener también un usuario en el sistema con rol "conductor" para poder acceder a la app.

---

---

## 11. Inventario — Conteos y auditorías

**Acceso:** Tab **Inventario**

Esta sección gestiona el inventario cíclico (conteos ABC programados) y las auditorías de excepción generadas por novedades de picking.

---

### 11.1 Sub-secciones

El tab Inventario tiene tres sub-tabs internos:

**Conteos**
Lista de todas las sesiones de conteo — tanto las cíclicas (programadas por ABC) como las de excepción (generadas por novedades de picking).

**ABC**
Configuración de la clasificación ABC del inventario por almacén. Determina con qué frecuencia se cuentan los productos.

**Ajustes**
Historial de ajustes de inventario realizados.

---

### 11.2 Conteos — Entender los estados

| Estado | Color | Qué significa |
|--------|-------|--------------|
| PENDIENTE | Amarillo | Nadie ha comenzado el conteo |
| EN_PROCESO | Azul | Un operario está contando ahora |
| SEGUNDO_CONTEO | Naranja | El primer conteo no cuadró, hay un segundo conteo ciego en curso |
| MATCH | Verde | El conteo cuadró con el sistema |
| DESCUADRE | Rojo | El segundo conteo tampoco cuadró — requiere intervención del admin |
| AJUSTADO | Gris | El admin ajustó manualmente el inventario |
| EXCEPCION_PICKING | Rojo oscuro | Generado por novedad de un operario de picking |

---

### 11.3 Auditorías urgentes — Cómo resolverlas

Las auditorías en estado **EXCEPCION_PICKING** son las más urgentes — las generó el sistema automáticamente cuando un operario reportó una novedad.

**Proceso de resolución:**

1. En el tab Inventario → sub-tab Conteos → busca los conteos con tipo EXCEPCION_PICKING
2. Cada uno muestra: ubicación, producto, motivo original del operario (📦/📉/🚫/❌)
3. Ve físicamente a la ubicación indicada
4. Cuenta las unidades que realmente hay en esa ubicación
5. Vuelve al sistema → toca la auditoría
6. Ingresa la cantidad que encontraste físicamente
7. El sistema compara:
   - Si coincide con lo que debería haber → MATCH, se resuelve solo
   - Si no coincide → el sistema marca DESCUADRE y crea el ajuste de inventario
8. Escribe el motivo del ajuste (ej: "Merma", "Traslado no registrado", "Error de picking previo")

---

### 11.4 Conteos en DESCUADRE — Edición como admin

Cuando un conteo queda en DESCUADRE (el segundo conteo tampoco cuadró) el admin puede corregir manualmente:

1. Busca el conteo en estado DESCUADRE
2. Toca **"Editar conteo"** (solo disponible para admin/supervisor)
3. Ingresa la cantidad física correcta
4. Escribe el motivo de la corrección (obligatorio)
5. Toca **"Guardar"**

El sistema registra quién hizo la corrección y cuándo, con el motivo — queda en el historial de auditoría.

---

### 11.5 Clasificación ABC — Cómo funciona

La clasificación ABC determina con qué frecuencia se cuentan los productos:

| Clase | Frecuencia de conteo | Descripción |
|-------|---------------------|-------------|
| A | Cada 15 días | Productos de alto movimiento o alto valor |
| B | Cada 90 días | Productos de movimiento medio |
| C | Cada 180 días | Productos de bajo movimiento |

El sistema asigna la clasificación automáticamente basándose en la rotación histórica. Puedes verla en el sub-tab ABC por almacén.

---

---

## 12. Traslados — Movimientos entre bodegas

**Acceso:** Tab **Traslados**

Esta sección gestiona los movimientos de mercancía entre diferentes bodegas o puntos de la operación.

---

### 12.1 Sub-tabs de traslados

| Sub-tab | Qué muestra |
|---------|------------|
| **Pendientes** | Traslados creados pero no iniciados |
| **En proceso** | Traslados que un operario está ejecutando |
| **Completados** | Traslados finalizados |

---

### 12.2 Crear un traslado

1. Toca **"+ Nuevo traslado"**
2. Selecciona:
   - Producto a trasladar
   - Cantidad
   - Ubicación origen (de dónde se saca)
   - Ubicación destino (a dónde va)
   - Operario asignado
3. Toca **"Crear traslado"**
4. El operario verá el traslado en su lista de tareas

---

### 12.3 Monitorear un traslado

En el sub-tab "En proceso" ves los traslados activos con:
- Producto, cantidades, origen y destino
- Operario asignado
- Progreso (cuánto lleva trasladado)

Un traslado se cierra automáticamente cuando el operario confirma que terminó.

---

---

## 13. Flujo completo del día

Esta es la secuencia de acciones recomendada para el admin en un día normal de operaciones.

---

### MAÑANA — Antes de que lleguen los operarios

**7:00 am**
1. Entra al sistema
2. Tab **Dashboard** → verificar que no hay CRASHED en Railway
3. Tab **Dashboard** → revisar auditorías urgentes del día anterior — ¿quedaron pendientes?
4. Tab **Dashboard** → revisar tareas bloqueadas — ¿quedaron de ayer?

**7:15 am**
5. Tab **Pedidos** → los pedidos del día deben estar sincronizados desde Siesa
   - Si no hay pedidos → esperar 2 minutos y recargar
   - Si sigue sin haber pedidos → verificar tab Connekta

**7:30 am** (cuando llegan los operarios)
6. Tab **Pedidos** → tocar **"Despachar"** en los pedidos del día
   - Los operarios empiezan a recibir tareas en sus celulares
   - El sistema les asigna las rutas de picking automáticamente

---

### DURANTE EL DÍA — Monitoreo continuo

**Cada 30-60 minutos:**
- Tab **Dashboard** → revisar auditorías urgentes nuevas
- Tab **Pedidos** → verificar que no hay tareas bloqueadas acumuladas
- Tab **Operarios** → verificar que todos tienen actividad

**Cuando aparece una tarea BLOQUEADA:**
- Leer el motivo y observaciones del operario
- Coordinar con supervisor para ir a la ubicación
- Decidir: ↩ Reabrir al pool o ✕ Cancelar

**Cuando aparece una auditoría urgente:**
- Asignar al supervisor para resolver
- El supervisor va físicamente, cuenta, y registra en el sistema

---

### TARDE — Cierre de la operación de picking

**Cuando todos los pedidos están en packing o despachados:**
- Tab **Pedidos** → verificar que no hay pedidos en estado "En picking" atascados
- Si hay tareas que no avanzaron → investigar

---

### TARDE/NOCHE — Gestión de rutas de despacho

**Coordinar con muelle:**
- Tab **Rutas** → cambiar ruta a EN_CARGUE para que muelle empiece a escanear bultos
- Tab **Muelle** → monitorear progreso del cargue
- Tab **Rutas** → cuando muelle termina → cambiar a EN_TRANSITO

**Monitorear conductores:**
- Tab **Rutas** → ver paradas en tiempo real
- Si un conductor tarda demasiado en una parada → llamar
- Si una ruta necesita forzarse por emergencia → "⚡ Forzar cierre"

**Cierre del día:**
- Tab **Rutas** → revisar planillas de cuadre de todas las rutas del día
- Cuadrar el efectivo con cada conductor
- Marcar las rutas como LIQUIDADAS

---

---

## 14. Resolución de problemas frecuentes

---

### "Los pedidos de Siesa no aparecen"

**Causa más probable:** El sync automático aún no corrió (corre cada 90 seg)

1. Espera 2 minutos y toca el tab Pedidos de nuevo
2. Si sigue sin aparecer → Tab Connekta → verificar que el estado es PRODUCCIÓN (no SIMULACIÓN)
3. Si está en SIMULACIÓN → las credenciales de Connekta no están configuradas → contactar desarrollador
4. Si está en PRODUCCIÓN pero sin pedidos → puede ser que Siesa no tiene pedidos pendientes en este momento

---

### "Un operario dice que no le salen tareas"

1. Tab Pedidos → verificar que hay tareas en estado "En cola" (⏳)
2. Si no hay tareas en cola → no se han despachado pedidos todavía → ir a despachar
3. Si hay tareas en cola → Tab Usuarios → editar el operario → verificar que el almacén_id sea el correcto
4. Pedirle al operario que cierre la app y vuelva a entrar

---

### "Un pedido lleva horas 'En picking' y no avanza"

1. Tab Pedidos → buscar el pedido → ver las tareas
2. Si hay tareas BLOQUEADAS → resolverlas (reabrir o cancelar)
3. Si no hay bloqueados pero el progreso está en 0% → el operario puede tener la tarea asignada pero no la inició → pedirle que entre a la app

---

### "El empacador no puede cerrar la caja (Error Siesa)"

1. Verificar que Siesa Enterprise esté funcionando (acceder desde otro computador)
2. Pedirle al empacador que lo intente de nuevo en 5 minutos
3. Si sigue fallando → contactar al desarrollador con el número de pedido y el mensaje de error

---

### "Un conductor confirma que entregó pero el sistema no lo deja"

Posibles causas:
- Sin internet en la zona → la app guarda offline, se sincroniza al tener señal
- Error en la app → pedirle que cierre y vuelva a entrar
- El estado de la ruta está mal → Tab Rutas → ver el detalle de la ruta → verificar que está en EN_TRANSITO

---

### "La app se cayó (CRASHED en Railway)"

1. Entra a Railway → servicio Flask (`positive-integrity`)
2. Tab Variables → verificar que `DATABASE_URL` y `SECRET_KEY` están presentes
3. Si están → Tab Deployments → toca los 3 puntos del último deploy → **"Restart"**
4. Espera 2 minutos
5. Si sigue caído → contactar al desarrollador con el log de error del deploy

---

### "El stock del sistema no coincide con lo físico"

1. Tab Connekta → "⚖ Ver reconciliación WMS vs Siesa" → esperar ~2 min
2. Ver la lista de diferencias
3. Para diferencias grandes → ir físicamente a la ubicación y contar
4. Tab Inventario → sub-tab Conteos → crear un conteo manual en esa ubicación
5. Si el error es masivo (muchos productos) → Tab Connekta → "↻ Sincronizar catálogo + stock inicial"

---

*Manual del Administrador — WMS Papelería Medellín*
*Versión 1.0 · Abril 2026*
*Para soporte técnico: contactar al desarrollador del sistema*

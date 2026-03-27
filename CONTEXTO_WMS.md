# Contexto WMS - Papelería Medellín

## La empresa
- Distribuidora mayorista de papelería
- ~200 pedidos diarios
- Estrategia: bajo costo operativo, cero digitación manual
- ERP: Siesa Enterprise con Connekta como gateway de integración

## Stack técnico
- Backend: Flask + Python 3.11
- Base de datos: PostgreSQL (Railway)
- Deploy: Railway (auto-deploy desde GitHub)
- Auth: JWT (flask-jwt-extended)
- Integración ERP: Connekta API REST (I-Key + I-Token)

## URLs
- Producción: https://wms-pame-production.up.railway.app
- PWA: https://wms-pame-production.up.railway.app/pwa
- Repo GitHub: https://github.com/nsantgiral-dev/WMS-PAME

## Credenciales del sistema
- Admin: admin@papeleria.com / admin2026
- Operario: operario@papeleria.com / operario2026
- Jefe almacén: jefe@papeleria.com / jefe2026

## Variables de entorno
- DATABASE_URL — PostgreSQL Railway
- SECRET_KEY — JWT secret
- FLASK_APP=run.py
- CONNEKTA_URL — pendiente (equipo Siesa trabajando)
- CONNEKTA_IKEY — pendiente
- CONNEKTA_ITOKEN — pendiente
- CONNEKTA_CONECTOR_CUMPLIDO — pendiente
- CONNEKTA_CONECTOR_ENTRADA — pendiente
- CONNEKTA_CONECTOR_SALIDA — pendiente

## Reglas de negocio críticas
1. Leer siempre Cantidad Disponible de Siesa, NUNCA existencia física
2. El WMS es un Trigger — solo cambia estado a "Cumplido"
3. Siesa genera automáticamente: Remisión + descarga inventario + Factura
4. Ajustes inventario: AJ-ENT (sobrante) y AJ-SAL (faltante)
5. Recepción ciega — operario escanea sin ver cantidades esperadas
6. Cross-dock si hay backorders — mercancía nunca toca estante
7. Bloqueo de excesos según tolerancia del proveedor
8. Conteo double-blind — segundo conteo por operario diferente
9. ABC viene de Siesa — WMS nunca calcula clasificación

## Flujo operativo maestro
Siesa aprueba pedido
→ WMS GET Connekta (pedidos + cantidad disponible)
→ WMS crea tareas picking FEFO
→ Operario recoge y confirma (escaneo tablet/celular)
→ Empacador verifica ítem por ítem (escaneo)
→ Confirmar packing → POST Connekta "Cumplido"
→ Siesa genera Remisión + Factura automáticamente

Camión llega
→ Recepcionista selecciona OC aprobada
→ Escaneo ciego ítem por ítem
→ WMS decide: Cross-dock si backorder / Put-away por ABC
→ Confirmar → POST Connekta entrada contable
→ Siesa debita cuenta 1435 automáticamente

Conteo cíclico
→ ABC genera tareas automáticas
→ Operario cuenta sin ver cantidad esperada
→ Conciliación en tiempo real contra Siesa
→ Descuadre → segundo conteo por operario diferente
→ Confirmado → POST Connekta AJ-ENT o AJ-SAL

## PWA — Roles y pantallas
- admin, gerente, jefe_almacen, supervisor → pantalla admin (dashboard 5 tabs)
- recepcionista → pantalla recepcion
- cualquier otro rol → pantalla operario (tareas picking/packing/conteo)

## Datos de prueba cargados
- Almacén: BOGOTA-01 (id=1), 18 ubicaciones + 1 cross-dock
- 8 productos: RESMA-A4-75, ESFERO-BIC-AZ, CARP-LEGAJ-AZ, TONER-HP85A,
  MARKER-EXPO-AZ, POST-IT-3X3, CLIPS-GEM-50, TIJERAS-8PUL (100 unidades c/u)

## Modelos en base de datos (11 tablas)
- usuarios, productos, almacenes, ubicaciones ✓
- ubicaciones_productos ✓ (fuente de verdad del stock)
- movimientos_inventario ✓ (kardex)
- tareas_picking ✓
- tareas_packing + items_packing ✓
- recepciones + items_recepcion ✓
- sesiones_conteo ✓

## Endpoints activos
- Auth: /api/auth/ — login, me, register
- Productos: /api/productos/ — CRUD completo
- Almacenes: /api/almacenes/ — CRUD + ubicaciones
- Inventario: /api/inventario/ — ajuste, stock, movimientos
- Picking: /api/picking/ — crear, iniciar, confirmar, cancelar, fefo
- Packing: /api/packing/ — crear, escanear, confirmar, cancelar, connekta/estado
- Recepcion: /api/recepcion/ — crear, iniciar, escanear, confirmar, cancelar
- Conteo: /api/conteo/ — listar, mis-tareas, registrar, ajustar, abc/generar
- Dashboard: /api/dashboard/ — kpis, productividad, movimientos, alertas-stock
- Mobile: /api/mobile/ — mis-tareas, tarea-actual, escanear, confirmar, sync

## Sprints completados
- [x] Sprint 0 — Base: modelos, auth, JWT, deploy Railway
- [x] Sprint 1 — Picking FEFO
- [x] Sprint 2 — Packing + Gateway Connekta
- [x] Sprint 3 — Recepción ciega + Cross-dock + Trigger Siesa
- [x] Sprint 4 — Conteo cíclico double-blind + ABC desde Siesa
- [x] Sprint 5 — Dashboard operativo KPIs tiempo real
- [x] Sprint 6 — PWA móvil con roles (admin/jefe=dashboard, operario=tareas, recepcionista=recepciones)

## Pendientes críticos
1. Credenciales Connekta — equipo Siesa trabajando en ello
2. Flujo de picking completo — crear tarea desde admin y que operario la vea
3. Prueba piloto real con operarios

## Cómo iniciar nueva sesión
1. Subir CONTEXTO_WMS.md al chat
2. cd ~/PROYECTOS/WMS-PAME-1 && source venv/bin/activate
3. flask run --port 5001
4. PWA local: http://127.0.0.1:5001/pwa

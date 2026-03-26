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
- Repo GitHub: https://github.com/nsantgiral-dev/WMS-PAME

## Variables de entorno
- DATABASE_URL — PostgreSQL Railway
- SECRET_KEY — JWT secret
- FLASK_APP=run.py
- CONNEKTA_URL — pendiente
- CONNEKTA_IKEY — pendiente
- CONNEKTA_ITOKEN — pendiente
- CONNEKTA_CONECTOR_CUMPLIDO — pendiente
- CONNEKTA_CONECTOR_ENTRADA — pendiente
- CONNEKTA_CONECTOR_SALIDA — pendiente

## Reglas de negocio críticas
1. Leer siempre Cantidad Disponible de Siesa, NUNCA existencia física
2. El WMS es un Trigger — solo cambia estado a "Cumplido"
3. Siesa genera automáticamente: Remisión + descarga inventario + Factura
4. Ajustes inventario: conector 05_Entrada y 06_Salida
5. Recepción ciega — operario escanea sin ver cantidades esperadas
6. Cross-dock si hay backorders — mercancía nunca toca estante
7. Bloqueo de excesos según tolerancia del proveedor

## Flujo operativo maestro
Siesa aprueba pedido
→ WMS GET Connekta (pedidos aprobados + cantidad disponible)
→ WMS crea tareas picking FEFO
→ Operario recoge y confirma (escaneo)
→ Empacador verifica ítem por ítem (escaneo)
→ Confirmar packing → POST Connekta "Cumplido"
→ Siesa genera Remisión + Factura automáticamente

Camión llega con mercancía
→ Recepcionista selecciona OC aprobada
→ Escaneo ciego ítem por ítem
→ WMS decide: Cross-dock si hay backorder / Put-away por ABC
→ Confirmar → POST Connekta entrada contable
→ Siesa debita cuenta 1435 automáticamente

## Modelos en base de datos (10 tablas)
- usuarios ✓
- productos ✓
- almacenes ✓
- ubicaciones ✓
- ubicaciones_productos ✓ (fuente de verdad del stock)
- movimientos_inventario ✓ (kardex)
- tareas_picking ✓
- tareas_packing + items_packing ✓
- recepciones + items_recepcion ✓

## Endpoints activos
### Auth
- POST /api/auth/login
- GET  /api/auth/me
- POST /api/auth/register

### Productos
- GET/POST /api/productos/
- GET/PUT/DELETE /api/productos/<id>

### Almacenes
- GET/POST /api/almacenes/
- GET/POST /api/almacenes/<id>/ubicaciones

### Inventario
- POST /api/inventario/ajuste
- GET  /api/inventario/stock/<producto_id>
- GET  /api/inventario/movimientos

### Picking
- GET  /api/picking/
- POST /api/picking/crear
- PUT  /api/picking/<id>/iniciar
- PUT  /api/picking/<id>/confirmar
- PUT  /api/picking/<id>/cancelar
- POST /api/picking/fefo

### Packing
- GET  /api/packing/
- POST /api/packing/crear-desde-picking
- POST /api/packing/crear-manual
- PUT  /api/packing/<id>/iniciar
- POST /api/packing/<id>/escanear
- PUT  /api/packing/<id>/confirmar
- PUT  /api/packing/<id>/cancelar
- GET  /api/packing/connekta/estado

### Recepción
- GET  /api/recepcion/
- POST /api/recepcion/crear
- PUT  /api/recepcion/<id>/iniciar
- POST /api/recepcion/<id>/escanear
- PUT  /api/recepcion/<id>/confirmar
- PUT  /api/recepcion/<id>/cancelar

## Sprints
- [x] Sprint 0 — Base: modelos, auth, JWT, deploy Railway
- [x] Sprint 1 — Picking FEFO
- [x] Sprint 2 — Packing + Gateway Connekta
- [x] Sprint 3 — Recepción ciega + Cross-dock + Trigger Siesa
- [ ] Sprint 4 — Conteo cíclico + ABC Analysis
- [ ] Sprint 5 — Dashboard operativo
- [ ] Sprint 6 — App móvil operarios (PWA)

## Credenciales desarrollo
- Admin: admin@papeleria.com / admin2026

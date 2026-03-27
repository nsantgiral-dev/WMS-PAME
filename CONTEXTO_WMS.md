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
→ Operario recoge y confirma (escaneo)
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
→ ABC genera tareas automáticas (A=semanal, B=mensual, C=trimestral)
→ Operario cuenta sin ver cantidad esperada
→ Conciliación en tiempo real contra Siesa
→ Descuadre → segundo conteo por operario diferente
→ Confirmado → POST Connekta AJ-ENT o AJ-SAL

## Modelos en base de datos (11 tablas)
- usuarios, productos, almacenes, ubicaciones ✓
- ubicaciones_productos ✓ (fuente de verdad del stock)
- movimientos_inventario ✓ (kardex)
- tareas_picking ✓
- tareas_packing + items_packing ✓
- recepciones + items_recepcion ✓
- sesiones_conteo ✓

## Endpoints activos
### Auth: POST /login, GET /me, POST /register
### Productos: CRUD completo
### Almacenes: CRUD + ubicaciones
### Inventario: ajuste, stock, movimientos
### Picking: crear, iniciar, confirmar, cancelar, fefo
### Packing: crear, escanear, confirmar, cancelar, connekta/estado
### Recepcion: crear, iniciar, escanear, confirmar, cancelar
### Conteo: listar, mis-tareas, registrar, ajustar, abc/generar, abc/resumen
### Dashboard: kpis, productividad, movimientos, alertas-stock, resumen-completo

## Sprints
- [x] Sprint 0 — Base: modelos, auth, JWT, deploy Railway
- [x] Sprint 1 — Picking FEFO
- [x] Sprint 2 — Packing + Gateway Connekta
- [x] Sprint 3 — Recepción ciega + Cross-dock + Trigger Siesa
- [x] Sprint 4 — Conteo cíclico double-blind + ABC desde Siesa
- [x] Sprint 5 — Dashboard operativo KPIs tiempo real
- [ ] Sprint 6 — App móvil operarios (PWA tablet Android)

## Credenciales desarrollo
- Admin: admin@papeleria.com / admin2026

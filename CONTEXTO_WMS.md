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

## Variables de entorno (.env local / Railway)
- DATABASE_URL — PostgreSQL Railway
- SECRET_KEY — JWT secret
- FLASK_APP=run.py
- FLASK_ENV=development
- PORT=5000
- CONNEKTA_URL — pendiente (placeholder)
- CONNEKTA_IKEY — pendiente (placeholder)
- CONNEKTA_ITOKEN — pendiente (placeholder)
- CONNEKTA_CONECTOR_CUMPLIDO — pendiente (placeholder)

## Reglas de negocio críticas (Connekta/Siesa)
1. Leer siempre Cantidad Disponible de Siesa, NUNCA existencia física
2. El WMS es un Trigger — solo cambia estado a "Cumplido"
3. Siesa genera automáticamente: Remisión + descarga inventario (14) + Factura electrónica
4. Ajustes inventario: conector 05_Entrada (sobrantes) y 06_Salida (faltantes)

## Flujo operativo maestro
Siesa aprueba pedido
→ WMS GET Connekta (pedidos aprobados + cantidad disponible)
→ WMS crea tareas picking FEFO
→ Operario recoge y confirma (escaneo)
→ Empacador verifica ítem por ítem (escaneo)
→ Confirmar packing → WMS POST Connekta "Cumplido"
→ Siesa genera Remisión + Factura automáticamente

## Modelos en base de datos
- usuarios — auth, roles, almacen_id
- productos — codigo, nombre, ABC, stock calculado desde ubicaciones
- almacenes — codigo, nombre, ciudad
- ubicaciones — codigo, zona, pasillo, estante, nivel
- ubicaciones_productos — stock real por ubicación (FUENTE DE VERDAD)
- movimientos_inventario — kardex completo con idempotency_key
- tareas_picking — picking FEFO con estados y reserva de stock

## Estructura de archivos
app/
  models/
    usuario.py ✓
    producto.py ✓
    almacen.py ✓
    ubicacion.py ✓
    inventario.py ✓
    picking.py ✓
    packing.py ← Sprint 2
    recepcion.py ← Sprint 3
    conteo.py ← Sprint 4
  services/
    picking_service.py ✓
    packing_service.py ← Sprint 2
    connekta_gateway.py ← Sprint 2
    recepcion_service.py ← Sprint 3
    abc_service.py ← Sprint 4
    conteo_service.py ← Sprint 4
    dashboard_service.py ← Sprint 5
  routes/
    auth.py ✓
    productos.py ✓
    almacenes.py ✓
    inventario.py ✓
    picking.py ✓
    packing.py ← Sprint 2
    recepcion.py ← Sprint 3
    dashboard.py ← Sprint 5

## Endpoints activos
- POST /api/auth/login
- GET  /api/auth/me
- POST /api/auth/register
- GET/POST /api/productos/
- GET/PUT/DELETE /api/productos/<id>
- GET/POST /api/almacenes/
- GET/POST /api/almacenes/<id>/ubicaciones
- POST /api/inventario/ajuste
- GET  /api/inventario/stock/<producto_id>
- GET  /api/inventario/movimientos
- GET  /api/picking/
- POST /api/picking/crear
- PUT  /api/picking/<id>/iniciar
- PUT  /api/picking/<id>/confirmar
- PUT  /api/picking/<id>/cancelar
- POST /api/picking/fefo

## Sprints
- [x] Sprint 0 — Base: modelos, auth, JWT, deploy Railway
- [x] Sprint 1 — Picking FEFO: tareas, reserva stock, confirmación
- [x] Sprint 2 — Packing + verificación ítem a ítem + Gateway Connekta
- [ ] Sprint 3 — Recepción de mercancía + verificación vs OC Siesa
- [ ] Sprint 4 — Conteo cíclico + ABC Analysis
- [ ] Sprint 5 — Dashboard operativo en tiempo real
- [ ] Sprint 6 — App móvil operarios (PWA tablet Android)

## Credenciales desarrollo
- Admin: admin@papeleria.com / admin2026

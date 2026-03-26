# Contexto WMS - Papelería Medellín

## Stack
- Backend: Flask + Python 3.11
- Base de datos: PostgreSQL (Railway)
- Deploy: Railway
- Auth: JWT (flask-jwt-extended)

## URLs
- Producción: https://wms-pame-production.up.railway.app
- Repo GitHub: https://github.com/nsantgiral-dev/WMS-PAME

## Modelos listos (app/models/)
- Usuario — auth, roles, almacen_id
- Producto — codigo, nombre, ABC, stock calculado desde ubicaciones
- Almacen — codigo, nombre, ciudad
- Ubicacion — codigo, zona, pasillo, estante, nivel
- UbicacionProducto — stock real por ubicación (fuente de verdad)
- MovimientoInventario — kardex completo con idempotency_key

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

## Sprints completados
- [x] Sprint 0 — Base: modelos, auth, migraciones, deploy Railway

## Próximos sprints
- [ ] Sprint 1 — Picking con FEFO
- [ ] Sprint 2 — Recepción de mercancía
- [ ] Sprint 3 — ABC Analysis + ML rotación
- [ ] Sprint 4 — Gateway SIESA canónico
- [ ] Sprint 5 — Dashboard frontend
- [ ] Sprint 6 — API móvil operarios

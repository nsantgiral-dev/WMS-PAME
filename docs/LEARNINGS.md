# WMS-PAME — Learnings Completos: Errores, Incidentes y Reglas desde el Día 0

> Referencia para construir herramientas con IA sin repetir errores.
> Compilado: 2026-07-22 | 25 incidentes | 6 decisiones arquitectónicas | 10 lecciones core

---

## Categorías

| Cat | Incidentes | Impacto acumulado |
|-----|-----------|-------------------|
| Siesa Integration | 14 | RC duplicados, cartera fantasma, rechazos silenciosos |
| Frontend/JS | 3 | Producción congelada, módulos muertos |
| Infrastructure | 3 | Server freeze total, deploy bloqueado |
| Testing | 2 | Guards faltantes, validaciones ignoradas |
| Architecture | 3 | Scheduler en proceso HTTP, PCF bloquea facturas |

---

## 1. Integración Siesa (14 incidentes)

### 1.1 RC Duplicado por POST Retry (CRÍTICO)
**Qué pasó**: Timeout en POST 142888 → DLQ reintentó → Siesa ya había procesado → RC duplicado.
**Raíz**: Sin pre-flag antes del POST. Timeout no significa que falló.
**Regla**: POST NUNCA reintenta en 5xx/timeout (solo 429). Pre-flag `siesa_*_triggered=True` ANTES del POST, revert si falla. Esperar ~10-12s antes de consultar resultado.

### 1.2 CxC Cruce con Cuenta Hardcodeada (CRÍTICO)
**Qué pasó**: `F353_ID_AUXILIAR_DOCTO_CRUCE=13050501` hardcodeado. Si factura usaba otra cuenta CxC, cruce fallaba → cartera fantasma.
**Raíz**: No propagaba `f253_id` real de API 20.
**Regla**: F353_ID_AUXILIAR_DOCTO_CRUCE = f253_id REAL de la factura. Si no está disponible, BLOQUEAR el RC.

### 1.3 RC Monto Bruto vs Neto con Retenciones (CRÍTICO)
**Qué pasó**: RC se enviaba por monto bruto + DC por retenciones → CxC se cerraba por MÁS del total → descuadre.
**Raíz**: No se restaban retenciones del monto RC.
**Regla**: RC = NETO (bruto - retenciones). DC cierra el resto. Total CxC = RC + sum(DCs) = bruto original.

### 1.4 F_CIA Confundida con CONNEKTA_ID_COMPANIA (ALTO)
**Qué pasó**: Código usaba 8215 (tenant Connekta) en payloads. Siesa espera F_CIA=1.
**Regla**: F_CIA = 1 SIEMPRE. 8215 es tenant Connekta, NO compañía Siesa.

### 1.5 tamPag >= 500 Genera Registros Fantasma NULL (ALTO)
**Qué pasó**: Consultas GET con tamPag=500 retornaban registros con todos los campos NULL.
**Regla**: tamPag máximo = 100. Si hay >100 resultados, paginar con loops.

### 1.6 f470_desc_varible — Typo Intencional en Specs (MEDIO)
**Qué pasó**: Spec DOCX tiene `f470_desc_varible` (sin "ia"). Usar la variante correcta causa rechazo.
**Regla**: Para conectores 142951, 173066, 173076, 173079 → usar `f470_desc_varible` (typo). Para 142945, 142946 → usar `f470_desc_variable` (correcto). LEER EL DOCX COMPLETO ANTES DE CODIFICAR.

### 1.7 DecimalConSigno — 21 chars exactos (MEDIO)
**Qué pasó**: `_fmt_valor` generaba 19 chars en vez de 21. Montos desalineados en Siesa.
**Regla**: `f'{signo}{abs(v):020.4f}'` → exactamente `+000000000000000.0000` (21 chars). Test Tier 1 obligatorio.

### 1.8 Timezone Colombia vs UTC (MEDIO)
**Qué pasó**: Fechas con UTC. Después de 7PM Colombia, UTC = día siguiente. Siesa rechazaba.
**Regla**: Fechas = YYYYMMDD sin separadores, timezone Bogotá (UTC-5). NUNCA `datetime.utcnow()`.

### 1.9 F353_PREFIJO_CRUCE NO existe en 142888 (MEDIO)
**Qué pasó**: Código creaba campo inexistente. Siesa rechazaba.
**Regla**: El prefijo va DENTRO de `F353_ID_TIPO_DOCTO_CRUCE`. No existe campo separado.

### 1.10 F358_ID_BANCO Debe Estar Vacío (MEDIO)
**Qué pasó**: Código populaba banco en transferencias. Siesa rechazaba.
**Regla**: `F358_ID_BANCO` vacío. La cuenta bancaria va en maestro de medios de pago.

### 1.11 Base Gravable — Dividir por 1.19 es Incorrecto (ALTO)
**Qué pasó**: Código calculaba base = total/1.19. Diferencias de centavos vs Siesa.
**Regla**: SIEMPRE usar API 45 (`f461_vlr_bruto` para base, `f461_vlr_imp` para IVA). NUNCA calcular.

### 1.12 RETEIVA Base = IVA, NO Base Gravable (ALTO)
**Qué pasó**: RETEIVA se calculaba sobre base gravable en vez de sobre el IVA.
**Regla**: RETEIVA 15% = `total_iva * 0.15` (NO `base_gravable * 0.15`).

### 1.13 Retenciones PUC — Grupo 1355, NO 2365 (ALTO)
**Qué pasó**: Cuentas de pasivos (2365) en vez de activos (1355). Deudas ficticias.
**Regla**: Retenciones a favor del vendedor = grupo 1355 (activos). NUNCA 2365.

### 1.14 Anti-Duplicado FE — Fail-Fast si API Falla (ALTO)
**Qué pasó**: Si GET API 45 fallaba con timeout, código permitía POST sin verificar → FE duplicada.
**Regla**: Si consulta de pre-check falla → BLOQUEAR POST. No asumir que está OK.

---

## 2. Frontend/JavaScript (3 incidentes)

### 2.1 51 Variables Duplicadas Congelaron Producción (CRÍTICO)
**Qué pasó**: Modularización extrajo funciones a módulos pero dejó `let`/`const` originales en app.js. Con `'use strict'`, browser mata el módulo entero. 7 de 9 módulos murieron → "Cargando..." infinito → NADA funcionaba.
**Diagnóstico**: Tomó horas. Backend respondía en 200ms. La página `/api/health/diag` reveló: `get() function NOT DEFINED` + 7 `SyntaxError: Identifier X has already been declared`.
**Regla**:
- NUNCA mover más de un grupo de funciones por commit
- SIEMPRE correr test anti-duplicación después de cada move
- Variables `const` con cuerpo multi-línea → copiar CUERPO COMPLETO
- Si función referencia global de app.js (API, TOKEN) → variable se QUEDA en app.js
- SIEMPRE bump cache bust después de cada move

### 2.2 Service Worker No Incluía Módulo Nuevo (ALTO)
**Qué pasó**: Reporté modularización como completada sin verificar sw.js. Módulo nuevo no estaba en array SHELL. PWA offline fallaría.
**Regla**: Checklist de 7 puntos ANTES de reportar completado: sintaxis, SW, orden de carga, duplicados, dependencias, cache bust, test suite.

### 2.3 Layout Retrocedió a Versión Anterior (MEDIO)
**Qué pasó**: Modularización extrajo versión VIEJA del render de Layout. Cambios post-modularización (entrepaños, asignar SKU batch) quedaron en app.js sin ser integrados.
**Regla**: Después de modularizar, comparar función extraída vs pre-modularización (`git show HEAD^:archivo`). Si difieren, usar la versión más reciente.

---

## 3. Infraestructura (3 incidentes)

### 3.1 Server Freeze — Schedulers en Proceso HTTP (CRÍTICO)
**Qué pasó**: 12 schedulers + gunicorn en 1 worker. Sync de 16K productos (5 min) + inventory refresh (40K records) + stock prewarm (10 bodegas) bloqueaban TODAS las requests HTTP. Health check → 502.
**Diagnóstico**: `curl /api/health/ping` → 0.3s (no toca DB). `curl /api/auth/login` → timeout 15s (toca DB). Confirmado: DB saturada por schedulers.
**Regla**:
- NUNCA scheduler pesado en proceso HTTP
- Gunicorn workers >= 2 con `--preload`
- Worker separado para schedulers pesados (`worker.py`)
- TODO endpoint responde en < 3s (test obligatorio)
- `pool_size=20`, `max_overflow=10`, `statement_timeout=25s`, `lock_timeout=8s`

### 3.2 Alembic Migration Cycle (MEDIO)
**Qué pasó**: Revision ID `a1b2c3d4e5f6` duplicado (existía en add_conductores Y en nuestra migración). Alembic detectaba ciclo → `flask db upgrade` fallaba.
**Regla**: SIEMPRE generar revision IDs únicos con `secrets.token_hex(6)`. Verificar con `grep "^revision = " migrations/versions/*.py | sort | uniq -d`.

### 3.3 flask db upgrade en Build Phase (MEDIO)
**Qué pasó**: Procfile tenía `release: flask db upgrade`. Nixpacks lo compilaba como `RUN` en Docker → se ejecutaba durante build sin acceso a PostgreSQL → `OperationalError`.
**Regla**: Migraciones van en `railway.toml [deploy] releaseCommand`, NO en Procfile release. Releasecommand corre durante deploy con acceso a DB.

---

## 4. Testing (2 incidentes)

### 4.1 Guards Faltantes en Conectores (MEDIO)
**Qué pasó**: `confirmar_entrada_compras` aceptaba `proveedor_id=None` y `sucursal_prov=""`. Payloads incompletos llegaban a Siesa.
**Regla**: SIEMPRE validar campos obligatorios ANTES de generar payload. Fail-fast con ValueError.

### 4.2 Pre-flag Sin Revert en Modo Ensayo (MEDIO)
**Qué pasó**: `MODO_ENSAYO` bloqueaba POST pero no revertía `siesa_rc_triggered=True`. Siguiente reintento fallaba por idempotencia.
**Regla**: Pre-flag DEBE revertirse si `resultado.get('modo_ensayo')` es True.

---

## 5. Arquitectura (3 incidentes)

### 5.1 PCF Bloquea Facturas Hasta Legalización (CRÍTICO)
**Qué pasó**: Planilla de Cuadre (PCF) en Siesa amarra facturas. No hay API para legalizar → facturas invisibles → cartera fantasma ($56M en Pitalito).
**Regla**: Opción A (actual): NO crear PCF en Siesa. Dispatch 100% en WMS. Conectores operan contra facturas libres.

### 5.2 Cross-Flow WMS ↔ Gestor Cartera (ALTO)
**Qué pasó**: Dos sistemas pueden crear RC para la misma factura. Sin coordinación → RC duplicado.
**Regla**: Pre-flight API 21 antes de POST 142888. Si ya existe RC del mismo cliente+monto+fecha → marcar como completado sin enviar.

### 5.3 Liquidación Fire-and-Forget (ALTO)
**Qué pasó**: Click "Liquidar" disparaba NC+RC+DC automáticamente sin verificar retenciones, sin preview, sin datos contables reales.
**Regla**: Liquidación en 2 fases. Fase 1 = WMS (estado). Fase 2 = Siesa per-parada (NC → RC → DC con preview, retenciones seleccionables, monto neto).

---

## 6. Decisiones Arquitectónicas

| # | Decisión | Por qué |
|---|----------|---------|
| 1 | DLQ secuencial NC→RC→DC | DC depende de RC, RC depende de NC |
| 2 | Modularización 1 grupo/commit | Incidente 51 vars costó tarde completa |
| 3 | No pedir docs al consultor | Solo infraestructura que él controla |
| 4 | Liquidación = 1 evento en muelle | Separar CDI de oficina revive cuello de botella |
| 5 | Robustez > tokens | Usuario valora calidad sobre costo |
| 6 | Worker separado para schedulers | Proceso HTTP no puede competir con syncs |

---

## 7. Las 10 Reglas de Oro para Construir con IA

1. **Lee el spec completo antes de codificar** — los typos son intencionales, 5+ rondas de debug desperdiciadas si no
2. **Pre-flag antes de POST** — timeout ≠ fallo, siempre marcar antes de enviar
3. **Testea ANTES de reportar terminado** — modularización necesita checklist de 7 puntos, no solo "merge and done"
4. **Calidad > conveniencia** — el usuario valora la plataforma, no tus tokens
5. **Scheduler en proceso separado** — el web server DEBE responder en <3s siempre
6. **Guard antes de POST** — fail-fast en validación, no en error de Siesa
7. **Timezone aware** — UTC difiere de Bogotá después de 7PM
8. **API es fuente de verdad** — nunca calcular IVA/subtotal, obtener de Siesa
9. **DLQ necesita secuencia** — NC → RC → DC, nunca en paralelo
10. **Límites de módulo importan** — `let`/`const` duplicados entre archivos mata en strict mode

---

## 8. Pipeline de Verificación Antes de Deploy

```bash
# Tier 1: Formatos (bloquante)
venv/bin/python -m pytest tests/test_siesa_formatos.py -v

# Tier 2: Contracts vs DOCX (bloquante)
venv/bin/python -m pytest tests/test_siesa_contracts.py -v

# Tier 3: Frontend integridad (bloquante)
venv/bin/python -m pytest tests/test_frontend_integrity.py -v

# Tier 4: Response time < 3s (bloquante)
venv/bin/python -m pytest tests/test_endpoint_response_time.py -v

# Tier 5: Suite completa
venv/bin/python -m pytest tests/ -v --tb=short
```

---

## 9. Arquitectura Final

```
┌─────────────────┐     ┌─────────────────┐     ┌──────────┐
│   WMS-PAME      │     │   WMS-Worker    │     │ Postgres │
│   (gunicorn)    │     │   (python)      │     │          │
│                 │     │                 │     │          │
│ HTTP APIs       │     │ Sync 16K items  │     │          │
│ DLQ (NC/RC/DC)  │────▶│ Stock prewarm   │────▶│          │
│ Pedidos sync    │     │ Inventory 40K   │     │          │
│ JWT + blocklist │     │ Alerts/ABC      │     │          │
│ PWA (12 módulos)│     │ Reconciliación  │     │          │
└─────────────────┘     └─────────────────┘     └──────────┘
    Rápido (<3s)          Pesado pero            Compartida
    424 tests CI          aislado                pool_size=20
```

---

*Documento vivo — actualizar con cada incidente nuevo.*

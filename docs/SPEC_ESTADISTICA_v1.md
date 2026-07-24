# ESPECIFICACIÓN ESTADÍSTICA WMS-PAME v1.0
## Arquitectura de modelos con compuertas — Papelería Medellín

**Fecha:** 2026-07-23
**Estado:** Para implementación directa
**Principio rector:** Ningún modelo se enciende hasta que el dato que consume existe y está limpio.

---

> Documento completo entregado por el consultor estratégico.
> Implementación a cargo del motor de inteligencia de inventario del WMS.
> Ver `app/services/kardex_service.py` para las capas ya construidas.

## Resumen de implementación

### Ya construido (Capas 0-3 del motor)

| Componente | Archivo | Estado |
|-----------|---------|--------|
| Descarga kardex transaccional | `kardex_service.py` | ✅ Operativo (QA) |
| Tabla definición demanda por concepto | `kardex_service.py` CONCEPTO_DEFINICION | ✅ |
| Reconciliación unidades × mes × bodega | `kardex_service.py` reconciliar_kardex() | ✅ |
| Auditoría DISTINCT conceptos | `kardex_service.py` compuerta en tasa | ✅ |
| Reconstrucción stock diario | `kardex_service.py` reconstruir_stock_diario() | ✅ |
| Reporte calidad (% negativos) | `kardex_service.py` reporte_calidad | ✅ |
| Tasa servida corregida (M0.2) | `kardex_service.py` calcular_tasa_servida_corregida() | ✅ |
| Clasificación Syntetos-Boylan (M0.3 parcial) | `kardex_service.py` clasificar_syntetos_boylan() | ✅ |
| Lista bloqueo recompra | `bloqueo_recompra_service.py` | ✅ |
| Detector de fugas en recepción | `routes/recepcion.py` | ✅ |

### Por construir (siguiente sesión, post Connekta producción)

| Modelo | Spec § | Prioridad | Dependencia |
|--------|--------|-----------|-------------|
| M0.1 CUSUM Vigía | §2.M0.1 | **PRIMERO** | G0 (datos fluyen) |
| Compuertas G0-G4 en UI | §1 | Semana 1 | — |
| TSB (Teunter-Syntetos-Babai) | §2.M0.3 | Semana 2-3 | G1 |
| ROP dual nacional/China | §2.M0.4 | Semana 2-3 | G1 + campo origen |
| Newsvendor temporada | §2.M0.5 | Pre-septiembre | G1 + G2 |
| pick_event schema | §4 | Go-live | — |
| Slotting afinidad (M1.1) | §3 | Post G3 | 8-12 semanas picking |

### Prohibiciones constitucionales del software

- NO ARIMA, SARIMA, Prophet, Holt-Winters sobre series diarias
- NO redes neuronales
- NO compra automática sin G1 + G2 + G4
- NO métricas de castigo individual en picking antes de baseline

### Tests canónicos requeridos

- Backtest CUSUM sobre planillas C.O. 006 (Florencia) — debe detectar en 2-3 semanas
- Backtest TSB vs ingenuo (media móvil 8 sem) — TSB debe ganar en MASE
- Validación política: compras históricas de la fundadora como benchmark

# Antes de Producción — Checklist Siesa QA → Producción

> Recomendación del CTO (WMS-PAME), generada el 2026-07-09.
> Contexto: actualmente el WMS trabaja contra APIs de Connekta/Siesa en **QA**, no producción real.

---

## Resumen

El switch QA → Producción no es un solo interruptor. Son **3 capas** que deben moverse juntas y en orden: técnica (Railway), maestros (dentro de Siesa producción) y configuración WMS.

---

## 1. Capa técnica (Railway — variables de entorno)

En `app/services/connekta_gateway.py:151` el gateway apunta por defecto a QA:

```python
_base = os.getenv('CONNEKTA_URL', 'https://serviciosqa.siesacloud.com')
```

Para producción real hay que cambiar en Railway:

```env
CONNEKTA_URL=https://servicios.siesacloud.com   # sin 'qa'
CONNEKTA_IKEY=...        # credenciales de PRODUCCIÓN (Connekta las entrega distintas por ambiente)
CONNEKTA_ITOKEN=...
CONNEKTA_ID_COMPANIA=... # el 8215 actual probablemente es el ID de la compañía QA en Connekta
```

## 2. Capa de maestros (dentro de Siesa producción)

`SIESA_CHECKLIST_CONFIGURACION.md` ya documenta el checklist completo que un consultor Siesa debe ejecutar en el ambiente real:

- Bodega de tránsito `TRA1`
- Tipos de documento `TRA` / `STS` / `ETS` / `FE`
- Motivos de traslado, ventas, compras, ajustes
- Lista de precios activa
- Solicitante de requisiciones
- Unidad de negocio

**Nada de esto existe automáticamente en producción** solo porque existe en QA — son maestros que hay que crear o verificar uno por uno ahí.

## 3. Capa de configuración WMS

Los códigos de motivo/naturaleza/lista de precio en `.env` (`SIESA_TIPO_DOCTO_AJUSTE=AFI`, `SIESA_UNIDAD_NEGOCIO=001`, etc.) son específicos del ambiente QA. En producción esos códigos **pueden ser distintos** — no hay garantía de que coincidan.

---

## Recomendación (orden de ejecución)

Dado lo registrado en `SIESA_LEARNINGS.md` (7 fallos de "Alto riesgo" por asumir formatos/códigos sin confirmar con el consultor: `F_CIA`, `f470_ind_naturaleza`, typo en `f470_desc_varible`, etc.), y la experiencia previa de que las teorías del consultor sobre motivo/naturaleza ya fallaron dos veces, el orden correcto es:

1. **No cambiar `CONNEKTA_URL` primero.** Antes de tocar el switch, exigir al consultor la **captura real de cada maestro en producción** (bodega, tipos de documento, motivos, lista de precios) — no su palabra.
2. Configurar las variables de Siesa-producción en Railway pero **sin borrar/pisar las de QA** hasta confirmar.
3. Usar `MODO_ENSAYO=true` primero contra producción real (`connekta_gateway.py:158-167`): permite GETs reales pero bloquea POSTs — así se valida que las credenciales y maestros de producción responden antes de escribir nada.
4. Solo después de un traslado/despacho de prueba end-to-end exitoso en `MODO_ENSAYO`, quitar la variable y pasar a producción real.

Este es un cambio de alto impacto (toca inventario y facturación contable real). **No debe ejecutarse sin autorización explícita paso a paso**, y las variables de Railway debe cambiarlas el usuario directamente en el panel (Claude no tiene acceso).

**Pendiente:** preparar el checklist exacto de variables a cambiar cuando el consultor confirme los valores reales de producción.

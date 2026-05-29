"""
FacturaFEService — responsabilidad única: obtener el detalle de la Factura
Electrónica desde Siesa (vía Connekta API_v2_Ventas_Facturas_DesdePedido) y
renderizarlo como HTML imprimible con datos 100% oficiales.

Completamente aislado del flujo de despacho existente (remision_service.py).
Solo se invoca desde los endpoints GET /api/packing/<id>/factura y
GET /api/admin/factura/<id>.
"""
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_RAZON_SOCIAL = os.getenv('PAME_RAZON_SOCIAL', 'PAPELERÍA MEDELLÍN')


class FacturaFEService:

    @staticmethod
    def obtener_lineas(tarea) -> list:
        """
        Consulta API_v2_Ventas_Facturas_DesdePedido filtrando por la RM que
        ya está guardada en la tarea (rm_tipo / rm_consec).
        Retorna lista de filas Siesa o [] si aún no hay RM o la API falla.
        """
        if not tarea.rm_tipo or not tarea.rm_consec:
            return []
        try:
            from app.services.connekta_gateway import connekta
            return connekta.get_detalle_factura(tarea.rm_tipo, tarea.rm_consec)
        except Exception as e:
            logger.warning('[FACTURA_FE] get_detalle_factura falló: %s', e)
            return []

    @staticmethod
    def puede_generar(tarea) -> tuple[bool, str]:
        from app.models.packing import EstadoPacking
        if tarea.estado == EstadoPacking.CANCELADO:
            return False, 'No se puede generar factura de una tarea cancelada'
        if not tarea.bultos:
            return False, 'La factura estará disponible una vez se cierren las piezas físicas'
        return True, ''

    @staticmethod
    def generar_html(tarea, lineas_fe: list) -> str:
        """
        Si lineas_fe tiene datos → renderiza la FE completa con valores Siesa.
        Si lineas_fe está vacío → renderiza resumen WMS con número FE si está disponible.
        """
        if lineas_fe:
            return FacturaFEService._html_fe_completa(tarea, lineas_fe)
        return FacturaFEService._html_fe_pendiente(tarea)

    # ──────────────────────────────────────────────────────────────────────────
    # HTML con datos reales de Siesa
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _html_fe_completa(tarea, lineas: list) -> str:
        ahora = datetime.now().strftime('%d/%m/%Y %H:%M')
        cab = lineas[0]  # campos de cabecera repetidos en cada fila

        # ── Cabecera FE ───────────────────────────────────────────────────────
        tipo_fe     = str(cab.get('f350_id_tipo_docto') or '').strip()
        consec_fe   = cab.get('f350_consec_docto') or ''
        fe_numero   = f'{tipo_fe}-{int(consec_fe):05d}' if consec_fe else f'{tipo_fe}-?'
        fecha_fe    = str(cab.get('f350_fecha') or ahora).strip()
        nit_cliente = str(cab.get('f200_nit_fact') or cab.get('f200_id_tercero_fact') or '').strip()
        nombre_cli  = str(
            cab.get('f200_razon_social_pedido_fact') or
            cab.get('f200_razon_social') or
            tarea.cliente or '—'
        ).strip()

        # ── Líneas de items ───────────────────────────────────────────────────
        subtotal_doc = 0.0
        iva_doc      = 0.0
        filas_html   = []
        for i, r in enumerate(lineas, 1):
            ref   = str(r.get('f120_referencia') or '').strip()
            desc  = str(
                r.get('f120_descripcion') or
                r.get('f120_descripcion_1') or
                r.get('f120_nombre') or ref
            ).strip()
            cant  = float(r.get('f470_cant_base') or 0)
            precio= float(r.get('f470_precio_uni') or 0)
            iva   = float(r.get('f470_vlr_imp') or 0)
            sub   = cant * precio
            total_linea = sub + iva
            subtotal_doc += sub
            iva_doc      += iva
            filas_html.append(f"""<tr>
              <td>{i}</td>
              <td>{ref}</td>
              <td>{desc}</td>
              <td style="text-align:right;">{cant:,.0f}</td>
              <td style="text-align:right;">{FacturaFEService._fmt_pesos(precio)}</td>
              <td style="text-align:right;">{FacturaFEService._fmt_pesos(sub)}</td>
              <td style="text-align:right;">{FacturaFEService._fmt_pesos(iva)}</td>
              <td style="text-align:right;">{FacturaFEService._fmt_pesos(total_linea)}</td>
            </tr>""")

        # f461_vlr_neto puede traer el neto del documento — usarlo si existe
        neto_siesa = float(cab.get('f461_vlr_neto') or 0)
        neto_doc   = neto_siesa if neto_siesa else (subtotal_doc + iva_doc)

        filas = '\n'.join(filas_html)
        nit_empresa = os.getenv('SIESA_NIT_EMPRESA', '')

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Factura {fe_numero}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Arial, sans-serif; font-size: 11px; color: #1a1a1a; padding: 20px; }}
    .header {{ display: flex; justify-content: space-between; align-items: flex-start;
               border-bottom: 2px solid #1a1a1a; padding-bottom: 10px; margin-bottom: 14px; }}
    .emisor h1 {{ font-size: 18px; font-weight: 700; }}
    .emisor p  {{ font-size: 10px; color: #555; margin-top: 2px; }}
    .fe-box {{ text-align: right; border: 2px solid #1a1a1a; padding: 8px 14px; border-radius: 4px; }}
    .fe-box .fe-tipo {{ font-size: 20px; font-weight: 800; letter-spacing: 2px; }}
    .fe-box .fe-num  {{ font-size: 13px; font-weight: 700; color: #333; }}
    .fe-box .fe-fecha{{ font-size: 10px; color: #555; margin-top: 2px; }}
    .cliente {{ background: #f8f8f8; padding: 10px 14px; border-radius: 4px;
                margin-bottom: 14px; display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; }}
    .cliente .lbl {{ font-weight: 700; color: #444; white-space: nowrap; }}
    .section-title {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                      letter-spacing: .5px; color: #555; border-bottom: 1px solid #ddd;
                      padding-bottom: 3px; margin-bottom: 6px; margin-top: 16px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 10px; }}
    th {{ background: #1a1a1a; color: #fff; padding: 5px 7px; text-align: left; font-weight: 600; }}
    td {{ padding: 4px 7px; border-bottom: 1px solid #eee; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    .totales {{ margin-top: 14px; display: flex; justify-content: flex-end; }}
    .totales table {{ width: auto; min-width: 260px; }}
    .totales td {{ padding: 3px 8px; border: none; }}
    .totales td:first-child {{ font-weight: 700; color: #444; text-align: right; }}
    .totales td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .totales .total-neto td {{ font-size: 13px; font-weight: 800; border-top: 2px solid #1a1a1a;
                               padding-top: 5px; }}
    .footer {{ margin-top: 20px; display: flex; justify-content: space-between;
               font-size: 9px; color: #888; border-top: 1px solid #ddd; padding-top: 8px; }}
    @media print {{ body {{ padding: 8px; }} .no-print {{ display: none; }} }}
  </style>
</head>
<body>
  <div class="no-print" style="margin-bottom:14px;">
    <button onclick="window.print()"
      style="padding:7px 16px;font-size:12px;cursor:pointer;background:#1a1a1a;color:#fff;
             border:none;border-radius:4px;">Imprimir</button>
  </div>

  <div class="header">
    <div class="emisor">
      <h1>{_RAZON_SOCIAL}</h1>
      {f'<p>NIT: {nit_empresa}</p>' if nit_empresa else ''}
    </div>
    <div class="fe-box">
      <div class="fe-tipo">FACTURA ELECTRÓNICA</div>
      <div class="fe-num">{fe_numero}</div>
      <div class="fe-fecha">{fecha_fe}</div>
    </div>
  </div>

  <div class="cliente">
    <span class="lbl">Cliente</span>   <span>{nombre_cli}</span>
    <span class="lbl">NIT</span>        <span>{nit_cliente}</span>
    <span class="lbl">Pedido</span>     <span>{tarea.numero_pedido_siesa}</span>
    <span class="lbl">Remisión</span>   <span>{tarea.rm_tipo or '—'}-{tarea.rm_consec or '—'}</span>
  </div>

  <div class="section-title">Detalle de la factura</div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Código</th>
        <th>Descripción</th>
        <th style="text-align:right;">Cant.</th>
        <th style="text-align:right;">Precio unit.</th>
        <th style="text-align:right;">Subtotal</th>
        <th style="text-align:right;">IVA</th>
        <th style="text-align:right;">Total línea</th>
      </tr>
    </thead>
    <tbody>
      {filas}
    </tbody>
  </table>

  <div class="totales">
    <table>
      <tr>
        <td>Subtotal</td>
        <td>{FacturaFEService._fmt_pesos(subtotal_doc)}</td>
      </tr>
      <tr>
        <td>IVA</td>
        <td>{FacturaFEService._fmt_pesos(iva_doc)}</td>
      </tr>
      <tr class="total-neto">
        <td>TOTAL NETO</td>
        <td>{FacturaFEService._fmt_pesos(neto_doc)}</td>
      </tr>
    </table>
  </div>

  <div class="footer">
    <span>WMS-PAME · Factura generada el {ahora} · Datos oficiales Siesa</span>
    <span>{fe_numero}</span>
  </div>
</body>
</html>"""

    # ──────────────────────────────────────────────────────────────────────────
    # HTML de fallback cuando la FE aún no llegó de Siesa (DLQ procesando)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _html_fe_pendiente(tarea) -> str:
        ahora = datetime.now().strftime('%d/%m/%Y %H:%M')
        rm_ref = (
            f'{tarea.rm_tipo}-{tarea.rm_consec}'
            if tarea.rm_tipo and tarea.rm_consec else '—'
        )
        estado_badge = (
            '<span style="background:#d4edda;color:#155724;padding:2px 8px;'
            'border-radius:10px;font-weight:700;">Confirmada en Siesa</span>'
            if tarea.siesa_triggered else
            '<span style="background:#fef3c7;color:#92400e;padding:2px 8px;'
            'border-radius:10px;font-weight:700;">Procesando en Siesa...</span>'
        )
        filas_items = ''.join(
            f'<tr><td>{i}</td>'
            f'<td>{(it.producto.codigo if it.producto else "—")}</td>'
            f'<td>{(it.producto.nombre if it.producto else "—")}</td>'
            f'<td style="text-align:right;">{it.cantidad_real or it.cantidad_esperada}</td></tr>'
            for i, it in enumerate(tarea.items, 1)
        )

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Factura {tarea.numero_pedido_siesa}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Arial, sans-serif; font-size: 11px; color: #1a1a1a; padding: 20px; }}
    .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #1a1a1a;
               padding-bottom: 10px; margin-bottom: 14px; }}
    .emisor h1 {{ font-size: 18px; font-weight: 700; }}
    .fe-box {{ text-align: right; border: 2px solid #1a1a1a; padding: 8px 14px; border-radius: 4px; }}
    .fe-box .fe-tipo {{ font-size: 16px; font-weight: 800; }}
    .info {{ background: #f8f8f8; padding: 10px 14px; border-radius: 4px; margin-bottom: 14px;
             display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; }}
    .info .lbl {{ font-weight: 700; color: #444; }}
    .section-title {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                      letter-spacing: .5px; color: #555; border-bottom: 1px solid #ddd;
                      padding-bottom: 3px; margin-bottom: 6px; margin-top: 16px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 10px; }}
    th {{ background: #1a1a1a; color: #fff; padding: 5px 7px; text-align: left; }}
    td {{ padding: 4px 7px; border-bottom: 1px solid #eee; }}
    tr:last-child td {{ border-bottom: none; }}
    .obs {{ margin-top: 14px; padding: 10px; background: #fffbe6;
            border-left: 3px solid #f0ad4e; font-size: 10px; }}
    .footer {{ margin-top: 20px; display: flex; justify-content: space-between;
               font-size: 9px; color: #888; border-top: 1px solid #ddd; padding-top: 8px; }}
    @media print {{ body {{ padding: 8px; }} .no-print {{ display: none; }} }}
  </style>
</head>
<body>
  <div class="no-print" style="margin-bottom:14px;">
    <button onclick="window.print()"
      style="padding:7px 16px;font-size:12px;cursor:pointer;background:#1a1a1a;color:#fff;
             border:none;border-radius:4px;">Imprimir</button>
    <button onclick="location.reload()"
      style="padding:7px 16px;font-size:12px;cursor:pointer;background:#374151;color:#fff;
             border:none;border-radius:4px;margin-left:8px;">↻ Actualizar</button>
  </div>

  <div class="header">
    <div class="emisor">
      <h1>{_RAZON_SOCIAL}</h1>
    </div>
    <div class="fe-box">
      <div class="fe-tipo">FACTURA ELECTRÓNICA</div>
      <div style="margin-top:4px;">{estado_badge}</div>
    </div>
  </div>

  <div class="info">
    <span class="lbl">Pedido Siesa</span>  <span>{tarea.numero_pedido_siesa}</span>
    <span class="lbl">Cliente</span>        <span>{tarea.cliente or '—'}</span>
    <span class="lbl">Municipio</span>      <span>{tarea.municipio or '—'}</span>
    <span class="lbl">Remisión Siesa</span> <span>{rm_ref}</span>
    <span class="lbl">Fecha despacho</span>
    <span>{tarea.fecha_despachado.strftime('%d/%m/%Y %H:%M') if tarea.fecha_despachado else ahora}</span>
  </div>

  <div class="section-title">Ítems despachados</div>
  <table>
    <thead>
      <tr><th>#</th><th>Código</th><th>Producto</th><th style="text-align:right;">Cant.</th></tr>
    </thead>
    <tbody>{filas_items}</tbody>
  </table>

  <div class="obs">
    <strong>Nota:</strong> Los precios e IVA se cargarán automáticamente una vez Siesa confirme
    la factura electrónica. Usa el botón <em>Actualizar</em> en unos segundos.
  </div>

  <div class="footer">
    <span>WMS-PAME · {ahora}</span>
    <span>Pedido {tarea.numero_pedido_siesa}</span>
  </div>
</body>
</html>"""

    @staticmethod
    def _fmt_pesos(valor: float) -> str:
        """Formatea número como pesos colombianos: $ 1.234.567"""
        try:
            return f'$ {valor:,.0f}'.replace(',', '.')
        except Exception:
            return str(valor)

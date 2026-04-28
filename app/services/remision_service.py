from datetime import datetime
from app.models.packing import TareaPacking


class RemisionService:
    """Genera el documento HTML de remisión para una tarea de packing DESPACHADA."""

    @staticmethod
    def generar_html(tarea: TareaPacking) -> str:
        ahora = datetime.now().strftime('%d/%m/%Y %H:%M')
        fecha_despacho = (
            tarea.fecha_despachado.strftime('%d/%m/%Y %H:%M')
            if tarea.fecha_despachado else ahora
        )
        empacador_nombre = (
            tarea.empacador.nombre_completo
            if tarea.empacador and hasattr(tarea.empacador, 'nombre_completo')
            else (tarea.empacador.nombre if tarea.empacador and hasattr(tarea.empacador, 'nombre') else '—')
        )

        filas_items = RemisionService._filas_items(tarea.items)
        filas_bultos = RemisionService._filas_bultos(tarea.bultos)
        tiene_diferencias = tarea.tiene_diferencias()

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Remisión {tarea.numero_pedido_siesa}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Arial, sans-serif; font-size: 12px; color: #1a1a1a; padding: 24px; }}
    .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #1a1a1a; padding-bottom: 12px; margin-bottom: 16px; }}
    .header-left h1 {{ font-size: 22px; font-weight: 700; letter-spacing: 1px; }}
    .header-left p {{ font-size: 11px; color: #555; margin-top: 2px; }}
    .header-right {{ text-align: right; font-size: 11px; color: #555; }}
    .header-right strong {{ font-size: 14px; color: #1a1a1a; }}
    .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; margin-bottom: 20px; background: #f8f8f8; padding: 12px 16px; border-radius: 4px; }}
    .info-grid .label {{ font-weight: 700; color: #444; }}
    .info-grid .value {{ color: #1a1a1a; }}
    .section-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #555; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin-bottom: 8px; margin-top: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
    th {{ background: #1a1a1a; color: #fff; padding: 6px 8px; text-align: left; font-weight: 600; }}
    td {{ padding: 5px 8px; border-bottom: 1px solid #e5e5e5; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr.diferencia td {{ background: #fff3cd; }}
    .badge {{ display: inline-block; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-weight: 700; }}
    .badge-ok {{ background: #d4edda; color: #155724; }}
    .badge-diff {{ background: #f8d7da; color: #721c24; }}
    .barcode {{ font-family: monospace; font-size: 13px; font-weight: 700; letter-spacing: 1px; }}
    .obs {{ margin-top: 16px; padding: 10px 14px; background: #fffbe6; border-left: 3px solid #f0ad4e; font-size: 11px; }}
    .footer {{ margin-top: 28px; display: flex; justify-content: space-between; font-size: 10px; color: #888; border-top: 1px solid #ddd; padding-top: 10px; }}
    @media print {{
      body {{ padding: 10px; }}
      .no-print {{ display: none; }}
    }}
  </style>
</head>
<body>

  <div class="no-print" style="margin-bottom:16px;">
    <button onclick="window.print()" style="padding:8px 18px;font-size:13px;cursor:pointer;background:#1a1a1a;color:#fff;border:none;border-radius:4px;">
      Imprimir
    </button>
  </div>

  <div class="header">
    <div class="header-left">
      <h1>PAME</h1>
      <p>Documento de Remisión de Despacho</p>
    </div>
    <div class="header-right">
      <strong>REMISIÓN</strong><br>
      Pedido: <strong>{tarea.numero_pedido_siesa}</strong><br>
      Fecha: {ahora}
    </div>
  </div>

  <div class="info-grid">
    <span class="label">Pedido Siesa</span>
    <span class="value">{tarea.numero_pedido_siesa}</span>

    <span class="label">Cliente</span>
    <span class="value">{tarea.cliente or '—'}</span>

    <span class="label">Municipio</span>
    <span class="value">{tarea.municipio or '—'}</span>

    <span class="label">Empacador</span>
    <span class="value">{empacador_nombre}</span>

    <span class="label">Fecha despacho</span>
    <span class="value">{fecha_despacho}</span>

    <span class="label">Estado</span>
    <span class="value">{tarea.estado}</span>
  </div>

  <div class="section-title">Ítems del pedido</div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Código</th>
        <th>Producto</th>
        <th style="text-align:right;">Esperado</th>
        <th style="text-align:right;">Real</th>
        <th style="text-align:center;">Estado</th>
      </tr>
    </thead>
    <tbody>
      {filas_items}
    </tbody>
  </table>

  {'<div class="obs"><strong>⚠ Diferencias detectadas</strong> — revisa los ítems marcados en amarillo.</div>' if tiene_diferencias else ''}

  <div class="section-title">Bultos / Piezas físicas</div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Tipo</th>
        <th>Código de barras</th>
        <th style="text-align:right;">Total piezas pedido</th>
      </tr>
    </thead>
    <tbody>
      {filas_bultos}
    </tbody>
  </table>

  {f'<div class="obs"><strong>Observaciones:</strong> {tarea.observaciones}</div>' if tarea.observaciones else ''}

  <div class="footer">
    <span>WMS-PAME · Remisión generada el {ahora}</span>
    <span>Código tarea: {tarea.codigo}</span>
  </div>

</body>
</html>"""

    @staticmethod
    def _filas_items(items) -> str:
        if not items:
            return '<tr><td colspan="6" style="text-align:center;color:#888;">Sin ítems</td></tr>'
        filas = []
        for i, item in enumerate(items, 1):
            p = item.producto
            codigo = p.codigo if p else '—'
            nombre = p.nombre if p else '—'
            diff = item.tiene_diferencia()
            clase = 'class="diferencia"' if diff else ''
            badge = (
                '<span class="badge badge-diff">DIFERENCIA</span>'
                if diff else
                '<span class="badge badge-ok">OK</span>'
            )
            filas.append(f"""<tr {clase}>
  <td>{i}</td>
  <td>{codigo}</td>
  <td>{nombre}</td>
  <td style="text-align:right;">{item.cantidad_esperada}</td>
  <td style="text-align:right;">{item.cantidad_real}</td>
  <td style="text-align:center;">{badge}</td>
</tr>""")
        return '\n'.join(filas)

    @staticmethod
    def _filas_bultos(bultos) -> str:
        if not bultos:
            return '<tr><td colspan="4" style="text-align:center;color:#888;">Sin bultos registrados</td></tr>'
        filas = []
        for bulto in sorted(bultos, key=lambda b: b.numero):
            filas.append(f"""<tr>
  <td>{bulto.numero}</td>
  <td>{bulto.tipo}</td>
  <td><span class="barcode">{bulto.codigo_barras}</span></td>
  <td style="text-align:right;">{bulto.total}</td>
</tr>""")
        return '\n'.join(filas)

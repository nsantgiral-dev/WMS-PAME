import logging

from app.extensions import db

logger = logging.getLogger(__name__)


class FaltanteReporteService:
    """
    Registra un faltante de picking como auditoría informativa.
    No toca inventario ni bloquea tareas — el picking ya fue confirmado normalmente.
    """

    @staticmethod
    def registrar(tarea_id: int, cantidad_recogida: int, cantidad_solicitada: int) -> dict:
        from app.models.picking import TareaPicking
        from app.services.conteo_service import ConteoService

        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError(f'Tarea picking {tarea_id} no encontrada')

        faltante = max(0, cantidad_solicitada - cantidad_recogida)
        if faltante == 0:
            return {'ok': True, 'auditoria_id': None}

        sesion = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea.id,
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id,
            almacen_id=tarea.almacen_id,
        )
        sesion.motivo_codigo = 'FALTANTE'
        db.session.commit()

        logger.warning(
            f'[FALTANTE] Tarea {tarea.codigo} — recogido {cantidad_recogida}/{cantidad_solicitada} '
            f'— faltaron {faltante} uds — auditoría {sesion.codigo}'
        )

        _enviar_email_faltante(tarea, cantidad_recogida, cantidad_solicitada, faltante, sesion.codigo)

        return {'ok': True, 'auditoria_id': sesion.id, 'faltante': faltante}


def _enviar_email_faltante(tarea, recogidas, solicitadas, faltante, cod_auditoria):
    from app.services.alertas_service import enviar_email

    producto_nombre = ''
    try:
        from app.models.producto import Producto
        p = Producto.query.get(tarea.producto_id)
        producto_nombre = p.nombre if p else str(tarea.producto_id)
    except Exception:
        producto_nombre = str(tarea.producto_id)

    asunto = f'[WMS] Faltante parcial en picking — {tarea.codigo}'
    html = f"""
    <h2 style="color:#b45309;">&#9888; Faltante parcial detectado</h2>
    <table style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Tarea</td>
          <td><strong>{tarea.codigo}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Documento</td>
          <td>{tarea.referencia_documento or '&mdash;'}</td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Producto</td>
          <td>{producto_nombre}</td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Solicitado</td>
          <td>{solicitadas} uds</td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Recogido</td>
          <td>{recogidas} uds</td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;"><strong>Faltante</strong></td>
          <td style="color:#ef4444;"><strong>{faltante} uds</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Auditor&iacute;a</td>
          <td>{cod_auditoria}</td></tr>
    </table>
    <p style="color:#666;font-size:12px;margin-top:16px;">
      Revisa Panel Admin &rarr; Auditor&iacute;as urgentes para gestionar el faltante.
    </p>
    """
    texto = (
        f'Faltante parcial — Tarea {tarea.codigo} — '
        f'Recogido {recogidas}/{solicitadas} — Faltan {faltante} uds — Auditoría {cod_auditoria}'
    )
    enviar_email(asunto, html, texto)

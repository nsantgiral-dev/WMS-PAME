"""El log de condición ausente dice lo que la política HACE (QA e2e 2026-09-24).

`registrar_ausencia` escribía «no se asume contado» mientras
`cobro_contraentrega` cobra lo ausente como contado supuesto: quien leía el log
creía lo contrario de lo que veía el conductor.
"""
import logging


def test_el_log_dice_que_se_cobra(caplog):
    from app.services import cond_pago
    with caplog.at_level(logging.WARNING, logger=cond_pago.logger.name):
        cond_pago.registrar_ausencia('pedido PD-1', tercero='900')
    msg = ' '.join(r.getMessage() for r in caplog.records)
    assert 'no se asume contado' not in msg
    assert 'se cobra' in msg and cond_pago.SUPUESTO_AUSENTE in msg


def test_el_log_coincide_con_la_politica():
    from app.services import cond_pago
    c = cond_pago.cobro_contraentrega(None)
    assert c['cobrar'] is True and c['origen'] == cond_pago.SUPUESTO_AUSENTE

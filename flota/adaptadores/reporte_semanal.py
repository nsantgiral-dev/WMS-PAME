"""
El reporte de tres líneas de los lunes. Especificado el 2026-08-01, cero líneas
de código hasta hoy.

`docs/flota/ESTADO.md:536-548` lo pide así:

> Llega **armado** los lunes por correo. Tres números y su detalle:
> inspecciones completas de la semana, hallazgos vencidos con días de
> vencimiento, y documentos que vencen en 30 días.
>
> **Si Yesid tiene que construirlo, no lo va a construir.** El sistema trabaja
> para él, no al revés — esa es la diferencia entre un rol de 30 minutos y uno
> que nadie sostiene.

Y el compromiso que lo sostiene, que no es de código (`ESTADO.md:489-495`):
Santiago lo lee todos los lunes, cinco minutos. Si a la tercera semana no se
leyó, control de flota deja de mandarlo y ahí muere el sistema — igual que en
noviembre, y otra vez sin que la herramienta tenga nada que ver.

## Por qué vive en `flota/adaptadores/` y no en `app/services/`

Lee `flota_inspeccion`, `flota_hallazgo` y `flota_documento_vehiculo`, y llama
al dominio de flota. Ponerlo en `app/services/` haría que `app/` importara
`flota/` fuera de `registrar_flota`, que `flota/api/__init__.py` declara como la
única línea de acoplamiento. Al revés —`flota` importando
`app.services.alertas_service`— ya pasa y es la dirección permitida.

## Las dos funciones, y por qué son dos

`armar_reporte` **no consulta el interruptor**. Se puede ejercer desde un botón
antes de que el cron se encienda nunca, que es la secuencia obligatoria del
módulo —medir, corregir, imponer— y además la mitigación de un riesgo real: el
scheduler va en `_scheduler_pesados`, detrás de `HEAVY_SCHEDULERS=true`, y el
propio `CLAUDE.md` advierte que **una alerta apagada no falla: se calla**.

`enviar_reporte_semanal` tiene **un solo interruptor**, adentro. Dos sitios
donde apagar lo mismo es un sitio que alguien se olvida de mirar — es el
argumento que `avisos.py:325-331` ya escribió.
"""
import logging
import os
from datetime import date as _date
from datetime import datetime as _datetime
from typing import Optional

from app.extensions import db
from app.utils.fecha import dia_operativo
from flota.dominio.procedencia import semana_cerrada_antes_de

logger = logging.getLogger(__name__)

#: El interruptor. Nace apagado (regla 10) y el default ausente es `false`.
#:
#: Pregunta obligatoria antes de elegir el default: **¿qué hace el primer
#: ciclo?** Con cero inspecciones y cero hallazgos, el primer correo son tres
#: líneas de «0 / ninguno / N documentos». Inofensivo — y nace apagado igual,
#: porque el ciclo peligroso no es el primero sino el que sigue a una carga
#: masiva del historial del taller en papel. Es el mismo razonamiento que
#: `FLOTA_PREVENTIVO`.
_VAR_ENCENDIDO = 'FLOTA_REPORTE_SEMANAL'

#: Los destinatarios. **Sin esto no manda**, y no cae a `ALERTA_EMAIL_DEST`.
#:
#: Caer a la lista global sería un adaptador degradando hacia algo que se parece
#: al éxito (regla 5): el correo saldría con status 200, el log diría «enviado»,
#: y el tablero de flota llegaría meses al Jefe de Bodega — que es el
#: destinatario documentado de esa variable y no tiene ninguna decisión de flota
#: asignada.
_VAR_DESTINATARIOS = 'FLOTA_REPORTE_DEST'


def reporte_encendido() -> bool:
    # `(getenv(X) or '')` y no `getenv(X, '')`: el trinquete de degradación
    # silenciosa rechaza el default como argumento, y tiene razón — un default
    # ahí es un valor que sustituye a otro sin que se vea. Es la forma que usan
    # `avisos.py:39` y `preventivo.py`.
    return (os.getenv(_VAR_ENCENDIDO) or '').strip().lower() == 'true'


def inspecciones_completas_de(desde: _date, hasta: _date) -> dict:
    """Inspecciones **completas** de la ventana, y las que no lo fueron.

    La ventana semanal no existía en ningún campo del health: hay «hoy»
    (`vehiculos_sin_inspeccion_hoy`) y «30 días» (`segundos_llenado_30d`), y la
    línea 1 del reporte pide la semana.

    Las incompletas viajan al lado y **no se suman**: una inspección incompleta
    no es una inspección hecha a medias que cuente un poco. Es un «no sé» —
    regla 1 del módulo—, y no habilita despacho. Un solo total escondería
    cuántos camiones salieron sin que nadie pudiera decir si estaban bien.
    """
    from flota.adaptadores.modelos import Inspeccion

    filas = (Inspeccion.query
             .filter(Inspeccion.dia >= desde, Inspeccion.dia <= hasta).all())
    completas = [i for i in filas if i.veredicto != 'incompleta']
    return {
        'completas': len(completas),
        'incompletas': len(filas) - len(completas),
        'vehiculos': len({i.vehiculo_id for i in completas}),
    }


def _hallazgos_vencidos(ahora: _datetime) -> list:
    """Los vencidos, **con cuántos días llevan**, y el peor primero.

    `dias_transcurridos` y no `dias_hallazgo_abierto`: el canon los separa a
    propósito. Uno mide antigüedad viva —que es lo que este correo necesita— y
    el otro duración cerrada, que es el indicador. Mezclarlos daría un número
    que no significa nada.
    """
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import Hallazgo
    from flota.dominio.hallazgo import EstadoHallazgo, dias_transcurridos, vencido

    filas = (db.session.query(Hallazgo, Vehiculo.placa)
             .join(Vehiculo, Vehiculo.id == Hallazgo.vehiculo_id)
             .filter(Hallazgo.estado == EstadoHallazgo.ABIERTO).all())
    salida = [
        {'placa': placa, 'criticidad': h.criticidad,
         'dias': dias_transcurridos(h.a_dominio(), ahora),
         'descripcion': (h.descripcion or '')[:80]}
        for h, placa in filas if vencido(h.a_dominio(), ahora)
    ]
    return sorted(salida, key=lambda x: -x['dias'])


def _documentos_por_vencer(hoy: _date) -> list:
    """Los que vencen dentro de 30 días, el más cercano primero.

    Corte en **día operativo de Bogotá**: con `date.today()` en Railway, un
    documento que vence hoy aparecería vencido una noche antes (regla 5).
    """
    from datetime import timedelta

    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import DocumentoVehiculo

    limite = hoy + timedelta(days=30)
    filas = (db.session.query(DocumentoVehiculo, Vehiculo.placa)
             .join(Vehiculo, Vehiculo.id == DocumentoVehiculo.vehiculo_id)
             .filter(DocumentoVehiculo.fecha_vencimiento.isnot(None),
                     DocumentoVehiculo.fecha_vencimiento >= hoy,
                     DocumentoVehiculo.fecha_vencimiento <= limite).all())
    return sorted(
        [{'placa': placa, 'tipo': d.tipo,
          'vence': d.fecha_vencimiento.isoformat(),
          'faltan': (d.fecha_vencimiento - hoy).days}
         for d, placa in filas],
        key=lambda x: x['faltan'])


def armar_reporte(dia: Optional[_date] = None, ahora: Optional[_datetime] = None) -> dict:
    """Las tres líneas. **No consulta el interruptor y no manda nada.**

    Se puede ejercer desde un botón antes de encender el cron, que es cómo se
    verifica el contenido de un correo sin mandarlo — y cómo se sabe que el
    scheduler pesado, si nunca arranca, no está escondiendo un reporte roto.

    `dia` entra por parámetro para poder barrer las 24 horas sin monkeypatchear
    un reloj. Por defecto, el día operativo de Bogotá.
    """
    dia = dia or dia_operativo()
    ahora = ahora or _datetime.utcnow()
    ventana = semana_cerrada_antes_de(dia)

    insp = inspecciones_completas_de(ventana.desde, ventana.hasta)
    vencidos = _hallazgos_vencidos(ahora)
    documentos = _documentos_por_vencer(dia)
    return {
        'desde': ventana.desde.isoformat(),
        'hasta': ventana.hasta.isoformat(),
        'etiqueta': ventana.etiqueta,
        'dia_operativo': dia.isoformat(),
        'inspecciones': insp,
        'hallazgos_vencidos': vencidos,
        'documentos_por_vencer_30d': documentos,
    }


def _texto(r: dict) -> str:
    """Las tres líneas en texto plano. Cortas a propósito.

    Un correo que hay que leer entero para saber si pasa algo se deja de abrir
    a la tercera semana, y ahí muere el sistema. Cada línea dice el número y,
    debajo, solo los casos que hay que atender.
    """
    lineas = [
        f'FLOTA · semana del {r["desde"]} al {r["hasta"]}',
        '',
        f'1. Inspecciones completas: {r["inspecciones"]["completas"]}'
        + (f' ({r["inspecciones"]["incompletas"]} quedaron incompletas — '
           f'una incompleta no es «no apto»: es «no sé», y no habilita despacho)'
           if r['inspecciones']['incompletas'] else ''),
        '',
        f'2. Hallazgos vencidos: {len(r["hallazgos_vencidos"])}',
    ]
    lineas += [f'     · {h["placa"]} — {h["criticidad"]}, {h["dias"]} día(s) '
               f'abierto: {h["descripcion"]}'
               for h in r['hallazgos_vencidos']]
    lineas += ['', f'3. Documentos que vencen en 30 días: '
                   f'{len(r["documentos_por_vencer_30d"])}']
    lineas += [f'     · {d["placa"]} — {d["tipo"]}, vence {d["vence"]} '
               f'(faltan {d["faltan"]} días)'
               for d in r['documentos_por_vencer_30d']]
    lineas += ['', f'Calculado el {r["dia_operativo"]} (día operativo de Bogotá) '
                   f'sobre {r["etiqueta"]}.']
    return '\n'.join(lineas)


def enviar_reporte_semanal(dia: Optional[_date] = None) -> dict:
    """Arma y manda. **Un solo interruptor, y está acá adentro.**

    Apagado devuelve su motivo **y deja una línea de log**: el silencio total es
    indistinguible de «no había nada que avisar», que es la mitad del problema
    que este reporte existe para resolver.
    """
    if not reporte_encendido():
        motivo = f'{_VAR_ENCENDIDO} no está en true'
        logger.info('[FLOTA_REPORTE] apagado — %s', motivo)
        return {'enviado': False, 'motivo': motivo}

    dest = (os.getenv(_VAR_DESTINATARIOS) or '').strip()
    if not dest:
        # NO se cae a `ALERTA_EMAIL_DEST`. Ver el comentario de la constante:
        # el correo saldría con éxito a quien no es, que es la falla
        # indistinguible del éxito.
        motivo = f'falta {_VAR_DESTINATARIOS}'
        logger.warning('[FLOTA_REPORTE] no se manda — %s', motivo)
        return {'enviado': False, 'motivo': motivo}

    from app.services.alertas_service import _enviar_email_con_dlq

    r = armar_reporte(dia)
    texto = _texto(r)
    _enviar_email_con_dlq(
        asunto=f'Flota · semana del {r["desde"]} al {r["hasta"]}',
        cuerpo_html=f'<pre style="font-family:monospace">{texto}</pre>',
        cuerpo_texto=texto,
        tipo_alerta='FLOTA_REPORTE_SEMANAL',
        dest=dest)
    logger.info('[FLOTA_REPORTE] enviado a %s', dest)
    return {'enviado': True, 'motivo': None, 'reporte': r}


def init_scheduler(app):
    """Lunes 06:15 en Bogotá. **Devuelve el scheduler o `None`.**

    Devolverlo no es cosmético: `app/__init__.py:56-59` declara «omitido» todo
    `init_scheduler` que devuelva falsy, y `tests/test_schedulers_declaran.py`
    lo exige de los quince. Uno que no devuelve nada se reporta como no
    arrancado aunque haya arrancado — y al revés.

    **06:15 y no 05:30 ni 06:00**: 05:30 es de Vigía y del preventivo, 06:00 es
    del barrido de avisos, y este reporte tiene que leer un plan **ya barrido**.
    Es el mismo criterio de escalonamiento que `app/__init__.py:390-393`.
    """
    if not reporte_encendido():
        logger.info('[FLOTA_REPORTE] scheduler no arranca — %s ausente',
                    _VAR_ENCENDIDO)
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    def _correr():
        with app.app_context():
            try:
                enviar_reporte_semanal()
            except Exception:
                # Se registra y NO se traga en silencio: un cron que revienta
                # callado es indistinguible de uno que no tenía nada que mandar.
                logger.exception('[FLOTA_REPORTE] el reporte semanal reventó')

    sch = BackgroundScheduler(timezone='America/Bogota')
    sch.add_job(func=_correr,
                trigger=CronTrigger(day_of_week='mon', hour=6, minute=15,
                                    timezone='America/Bogota'),
                id='flota_reporte_semanal',
                name='Flota — reporte de tres líneas (lunes 06:15 Bogotá)',
                replace_existing=True, max_instances=1, misfire_grace_time=3600)
    sch.start()
    logger.info('[FLOTA_REPORTE] scheduler arrancado — lunes 06:15 Bogotá')
    return sch


__all__ = ['armar_reporte', 'enviar_reporte_semanal', 'reporte_encendido',
           'inspecciones_completas_de', 'init_scheduler']

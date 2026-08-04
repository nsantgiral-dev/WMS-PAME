"""
El día operativo de la empresa. Una sola definición.

La empresa opera en Colombia (UTC-5) y despacha hasta cerca de las 8 p.m. En ese
huso, **`datetime.utcnow()` empieza a devolver la fecha de mañana a las 7 p.m.**
— justo en el pico del cierre del día.

Este módulo existe porque esa regla ya estaba escrita, ya tenía un helper, ya
tenía cuatro tests en verde… y se aplicaba en 4 de 16 sitios. Los otros 12
calculaban la fecha por su cuenta con `utcnow()`. La suite decía "timezone: OK"
porque los tests verificaban EL HELPER en aislamiento, nunca que alguien lo
llamara.

Tres consecuencias medidas el 2026-08-04, todas del mismo error:

  · **Fecha contable en Siesa.** `f350_fecha` con la fecha de mañana: Siesa
    rechaza documentos con fecha futura o período cerrado.
  · **Vencimientos de cartera.** Sumar 30 días sobre un día base equivocado
    vence un día antes de lo pactado.
  · **Claves de idempotencia.** `SIESA-INI-{prod}-{fecha}` cambia a las 7 p.m.
    en vez de a medianoche: dos cargas de stock a las 6:55 y 7:05 p.m. tienen
    claves distintas y el inventario entra dos veces.

Regla: **ninguna fecha de negocio se calcula con `utcnow()`.** Los timestamps
técnicos (`created_at`, `updated_at`) sí siguen en UTC — eso es correcto y no se
toca; lo que no puede salir de UTC es una fecha que alguien va a LEER como día.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ_BOGOTA = ZoneInfo('America/Bogota')


def ahora_bogota() -> datetime:
    """Instante actual, consciente de zona. La fuente de todo lo de abajo."""
    return datetime.now(TZ_BOGOTA)


def fecha_hoy_bogota() -> str:
    """Hoy en Colombia, formato Siesa: `YYYYMMDD`."""
    return ahora_bogota().strftime('%Y%m%d')


def fecha_iso_bogota() -> str:
    """Hoy en Colombia, formato ISO: `YYYY-MM-DD`.

    Para los campos que llevan guiones (`f421_fecha_entrega`), que son la
    excepción y no la regla.
    """
    return ahora_bogota().strftime('%Y-%m-%d')


def fecha_bogota_mas(dias: int) -> str:
    """Hoy + N días en Colombia, formato `YYYYMMDD`. Para vencimientos."""
    return (ahora_bogota() + timedelta(days=dias)).strftime('%Y%m%d')


def dia_operativo() -> 'datetime.date':
    """Qué día es, para la operación. NO es `utcnow().date()`.

    Lo usa todo lo que cuenta "lo de hoy": KPIs, tableros, cortes diarios.
    """
    return ahora_bogota().date()


def inicio_del_dia_utc(dia=None) -> datetime:
    """Medianoche del día operativo, expresada en UTC **naive**.

    Existe porque las columnas de fecha del WMS guardan `datetime.utcnow()`:
    naive, en UTC. Para contar "lo completado hoy" hay que comparar contra la
    medianoche de COLOMBIA convertida a ese mismo marco — no contra la
    medianoche UTC, que en Colombia son las 7 p.m.

    Sin esto, un contador de "hoy" se reinicia a las 7 p.m. en mitad del turno
    de la tarde y arrastra el trabajo de la noche anterior.
    """
    dia = dia or dia_operativo()
    medianoche_local = datetime.combine(dia, datetime.min.time(), tzinfo=TZ_BOGOTA)
    return medianoche_local.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)


__all__ = [
    'TZ_BOGOTA', 'ahora_bogota', 'fecha_hoy_bogota', 'fecha_iso_bogota',
    'fecha_bogota_mas', 'dia_operativo', 'inicio_del_dia_utc',
]

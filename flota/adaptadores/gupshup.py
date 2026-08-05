"""
Salida hacia WhatsApp. Cumple `puertos.CanalDeAviso`.

Este archivo hereda cinco cosas del adaptador de cartera, y las hereda porque
allá **ya costaron**. Ninguna es una precaución teórica:

1. **El nombre de plantilla no es su id.** `_TEMPLATE_IDS.get(nombre, nombre)`
   mandaba el nombre como si fuera el id: Gupshup respondía `submitted` y no
   llegaba nada, durante semanas. Acá, sin id configurado, se **levanta**.
2. **Cada plantilla tiene DOS identificadores** —uno de Facebook y uno de
   Gupshup— y para la API va el de Gupshup. Dos números parecidos para la misma
   cosa y uno solo funciona.
3. **Los ids que se anotaron mientras la plantilla estaba `Pending` son
   temporales.** Al aprobarse cambian. Anotar el temporal produce un
   `submitted` que no entrega nada — el mismo síntoma que (1), por otra causa.
4. **`submitted` no es `delivered`.** Lo que devuelve esta clase es un id, no
   una promesa de entrega; quien la llama guarda `entregado_al_proveedor` y
   espera el evento.
5. **`str(None)` es `'None'` y es truthy.** El guard de teléfono verifica
   forma, no verdad-de-Python.

Nada acá decide A QUIÉN ni CUÁNDO avisar: eso es del servicio. Este archivo
solo sabe hablar con Gupshup.
"""
import json
import logging
import os
import re
from typing import List

import requests

from flota.dominio.aviso import validar_parametros
from flota.dominio.errores import ErrorFlota

logger = logging.getLogger(__name__)

#: E.164 sin el `+`, como lo quiere Gupshup: 57 + 10 dígitos en Colombia.
_TELEFONO = re.compile(r'^\d{10,15}$')

_URL = 'https://api.gupshup.io/wa/api/v1/template/msg'


class AvisoNoEnviado(ErrorFlota):
    """No salió. Se propaga: quien llama registra `fallido` con el motivo."""


def _ids_de_plantilla() -> dict:
    """`GUPSHUP_TEMPLATE_IDS` — JSON `{"nombre_plantilla": "uuid-de-gupshup"}`.

    Sin default y sin fallback al nombre. Un mapa vacío significa "todavía no
    hay ids definitivos", y en ese estado el canal no manda: es preferible a
    mandar contra un id temporal que Gupshup acepta y no entrega.
    """
    crudo = (os.getenv('GUPSHUP_TEMPLATE_IDS') or '').strip()
    if not crudo:
        return {}
    try:
        mapa = json.loads(crudo)
    except ValueError as e:
        raise AvisoNoEnviado(
            f'GUPSHUP_TEMPLATE_IDS no es JSON válido ({e}). Formato esperado: '
            '{"flota_documento_vence": "<uuid de Gupshup>"}'
        ) from e
    if not isinstance(mapa, dict):
        raise AvisoNoEnviado('GUPSHUP_TEMPLATE_IDS debe ser un objeto JSON')
    return mapa


def id_de_plantilla(nombre: str) -> str:
    """El uuid de Gupshup para esa plantilla. Levanta si no está.

    **Nunca devuelve el nombre como fallback.** Ese `.get(nombre, nombre)` es
    literalmente el bug que costó semanas: produce un envío que el proveedor
    acepta y nadie recibe, sin un solo error en los logs.
    """
    mapa = _ids_de_plantilla()
    valor = (mapa.get(nombre) or '').strip()
    if not valor:
        raise AvisoNoEnviado(
            f'no hay id de Gupshup para la plantilla {nombre!r}. Ponerlo en '
            f'GUPSHUP_TEMPLATE_IDS — el id DEFINITIVO (el que aparece cuando la '
            f'plantilla queda Approved), no el temporal de mientras estaba '
            f'Pending, y el de Gupshup, no el de Facebook.'
        )
    # Un id de Facebook es todo dígitos; el de Gupshup es un uuid. Distinguirlos
    # acá evita el caso en que el envío "funciona" y no entrega.
    if valor.isdigit():
        raise AvisoNoEnviado(
            f'el id configurado para {nombre!r} son solo dígitos ({valor}): ese '
            f'es el id de Facebook. Para la API de Gupshup va el uuid de Gupshup.'
        )
    return valor


def validar_telefono(telefono) -> str:
    """Forma, no verdad-de-Python.

    `str(None)` es `'None'`, tiene largo 4 y es truthy: un `if telefono:` lo
    deja pasar y el mensaje sale hacia un destinatario inexistente.
    """
    t = str(telefono or '').strip().replace(' ', '').replace('-', '').lstrip('+')
    if not _TELEFONO.match(t):
        raise AvisoNoEnviado(
            f'teléfono con forma inválida: {telefono!r}. Se espera E.164 sin '
            f'"+" (ej. 573001234567).'
        )
    return t


class CanalGupshup:
    """Manda de verdad. Implementa `CanalDeAviso`."""

    simulado = False

    def __init__(self):
        # `or ''` y no `getenv(x, '')`: la diferencia es que acá el vacío se
        # comprueba explícitamente en `enviar` y aborta con los nombres de las
        # variables que faltan. Un default que nadie verifica es lo que la
        # regla 5 prohíbe; uno que se verifica dos líneas después, no.
        self.api_key = os.getenv('GUPSHUP_API_KEY') or ''
        self.origen = os.getenv('GUPSHUP_SOURCE') or ''
        self.app_name = os.getenv('GUPSHUP_APP_NAME') or ''

    def enviar(self, telefono: str, plantilla: str, parametros: List[str]) -> str:
        validar_parametros(plantilla, parametros)
        destino = validar_telefono(telefono)
        template_id = id_de_plantilla(plantilla)

        faltan = [n for n, v in (('GUPSHUP_API_KEY', self.api_key),
                                 ('GUPSHUP_SOURCE', self.origen),
                                 ('GUPSHUP_APP_NAME', self.app_name)) if not v]
        if faltan:
            raise AvisoNoEnviado(f'faltan variables de Gupshup: {", ".join(faltan)}')

        cuerpo = {
            'channel': 'whatsapp',
            'source': self.origen,
            'destination': destino,
            'src.name': self.app_name,
            # Lista explícita y posicional. `json.dumps` y no `str()`: las
            # comillas simples de Python no son JSON y Gupshup las rechaza.
            'template': json.dumps({'id': template_id, 'params': list(parametros)}),
        }
        try:
            r = requests.post(
                _URL, data=cuerpo, timeout=20,
                headers={'apikey': self.api_key,
                         'Content-Type': 'application/x-www-form-urlencoded'},
            )
        except requests.RequestException as e:
            raise AvisoNoEnviado(f'no se pudo hablar con Gupshup: {e}') from e

        if r.status_code >= 400:
            raise AvisoNoEnviado(f'Gupshup respondió {r.status_code}: {r.text[:300]}')
        try:
            datos = r.json()
        except ValueError as e:
            raise AvisoNoEnviado(f'respuesta de Gupshup ilegible: {r.text[:200]}') from e

        msg_id = (datos.get('messageId') or '').strip()
        if not msg_id:
            # Sin id no hay forma de cruzar el evento de entrega, y entonces el
            # aviso es incomprobable. Se trata como fallo, no como éxito parcial.
            raise AvisoNoEnviado(
                f'Gupshup aceptó sin devolver messageId: {str(datos)[:200]}. '
                f'Sin id no se puede verificar si llegó.'
            )
        logger.info('[FLOTA/AVISO] %s → %s (%s)', plantilla, destino, msg_id)
        return msg_id


class CanalSimulado:
    """No manda nada. Implementa `CanalDeAviso`.

    `simulado = True` viaja hasta la FILA, no se queda en el log (regla 8).
    Devuelve un id con prefijo reconocible a simple vista: un id que se parece
    a uno real es la forma exacta en que un tablero de pruebas se lee como
    producción.
    """

    simulado = True

    def __init__(self):
        self.enviados = []

    def enviar(self, telefono: str, plantilla: str, parametros: List[str]) -> str:
        validar_parametros(plantilla, parametros)
        destino = validar_telefono(telefono)
        self.enviados.append((destino, plantilla, list(parametros)))
        logger.info('[FLOTA/AVISO-SIMULADO] %s → %s %s', plantilla, destino, parametros)
        return f'SIMULADO-{len(self.enviados)}'


def canal() -> 'CanalGupshup | CanalSimulado':
    """El canal según el ambiente. Real solo con `FLOTA_AVISOS_REALES=true`.

    El default es el simulado: un cron que nace mandando WhatsApp de verdad a
    tres empleados el día que alguien lo despliega sin querer es la regla 10 al
    revés.
    """
    if (os.getenv('FLOTA_AVISOS_REALES') or '').lower() == 'true':
        return CanalGupshup()
    return CanalSimulado()


__all__ = ['AvisoNoEnviado', 'CanalGupshup', 'CanalSimulado', 'canal',
           'id_de_plantilla', 'validar_telefono']

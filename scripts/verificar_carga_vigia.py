#!/usr/bin/env python
"""
Verificación de la carga histórica del Vigía — las tres pruebas, en un comando.

Se corre UNA VEZ, después de subir los TXT y ANTES de devolver
VIGIA_CARGAR_TXT a false. Si las tres pasan, la línea base está certificada y
la ingesta Connekta puede construirse encima sabiendo qué convenciones heredar.

Uso:
    venv/bin/python scripts/verificar_carga_vigia.py \
        --semanas 53 --cos 6 --semana-alarma 2025-12-29 --s-minus 6.30

Sin valores de referencia reporta lo observado y marca la prueba 2 como
pendiente — nunca inventa un canon.

Salida: exit 0 si certifica, 1 si alguna prueba falla.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, AMAR, GRIS, FIN = '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m'


def _icono(prueba):
    if prueba.get('pendiente_valor_referencia'):
        return f'{AMAR}○{FIN}'
    return f'{VERDE}✓{FIN}' if prueba['ok'] else f'{ROJO}✗{FIN}'


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--semanas', type=int, help='Semanas esperadas (ej. 53)')
    p.add_argument('--cos', type=int, help='C.O.s esperados (ej. 6)')
    p.add_argument('--semana-alarma', help='Semana canónica de la alarma (ej. 2025-12-29)')
    p.add_argument('--s-minus', type=float, help='Valor S- canónico (ej. 6.30)')
    args = p.parse_args()

    from app import create_app
    from app.services.vigia_service import verificar_carga_historica

    app = create_app()
    with app.app_context():
        r = verificar_carga_historica(
            semanas=args.semanas, cos=args.cos,
            semana_alarma=args.semana_alarma, s_minus=args.s_minus,
        )

    print()
    print('  VERIFICACIÓN DE CARGA HISTÓRICA — VIGÍA')
    print('  ' + '─' * 52)

    for prueba in r['pruebas']:
        print(f"\n  {_icono(prueba)}  {prueba['prueba']}")

        for k, v in (prueba.get('observado') or {}).items():
            if k == 'alarmas':
                continue
            print(f'      {GRIS}{k}:{FIN} {v}')

        for fallo in prueba.get('fallos', []):
            print(f'      {ROJO}→ {fallo}{FIN}')

        if prueba.get('pendiente_valor_referencia'):
            print(f'      {AMAR}→ sin valor de referencia: pasa --semana-alarma y --s-minus{FIN}')

    print('\n  ' + '─' * 52)
    if r['certificado']:
        print(f'  {VERDE}CERTIFICADO{FIN} — línea base lista.')
        print(f'  {GRIS}Siguiente: VIGIA_CARGAR_TXT=false + arranque de ingesta Connekta.{FIN}\n')
        return 0

    print(f'  {ROJO}NO CERTIFICADO{FIN} — resolver los fallos antes de construir encima.')
    print(f'  {GRIS}No devolver VIGIA_CARGAR_TXT a false todavía.{FIN}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main())

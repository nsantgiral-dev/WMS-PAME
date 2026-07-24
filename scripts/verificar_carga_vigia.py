#!/usr/bin/env python
"""
Verificación de la carga histórica del Vigía — las tres pruebas, en un comando.

Se corre UNA VEZ, después de subir los TXT y ANTES de devolver
VIGIA_CARGAR_TXT a false. Si las tres pasan, la línea base está certificada y
la ingesta Connekta puede construirse encima sabiendo qué convenciones heredar.

Los valores canónicos se leen de docs/canon_florencia.json — no hace falta
pasarlos. Los flags existen solo para sobreescribirlos deliberadamente.

Uso:
    venv/bin/python scripts/verificar_carga_vigia.py \
        --semanas 53 --cos 6 --insumos ventas_2025.txt ventas_2026.txt

Salida: exit 0 si certifica, 1 si alguna prueba falla.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, AMAR, GRIS, FIN = '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m'


def _icono(prueba):
    if prueba.get('pendiente_valor_referencia') or prueba.get('no_verificable'):
        return f'{AMAR}○{FIN}'
    return f'{VERDE}✓{FIN}' if prueba['ok'] else f'{ROJO}✗{FIN}'


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--semanas', type=int, help='Semanas esperadas (ej. 53)')
    p.add_argument('--cos', type=int, help='C.O.s esperados (ej. 6)')
    p.add_argument('--semana-alarma', help='Sobreescribe la semana del canon')
    p.add_argument('--s-minus', type=float, help='Sobreescribe el S- del canon')
    p.add_argument('--insumos', nargs='*', help='TXT a comparar contra los hashes del canon')
    args = p.parse_args()

    from app import create_app
    from app.services.vigia_service import verificar_carga_historica

    app = create_app()
    with app.app_context():
        r = verificar_carga_historica(
            semanas=args.semanas, cos=args.cos,
            semana_alarma=args.semana_alarma, s_minus=args.s_minus,
            insumos=args.insumos,
        )

    print()
    print('  VERIFICACIÓN DE CARGA HISTÓRICA — VIGÍA')
    print('  ' + '─' * 52)

    if not r['canon']['cargado']:
        print(f'  {ROJO}Sin canon: falta docs/canon_florencia.json{FIN}')
    elif r['canon'].get('procedencia'):
        print(f"  {GRIS}Canon: {r['canon']['procedencia'].get('origen')}{FIN}")

    for prueba in r['pruebas']:
        print(f"\n  {_icono(prueba)}  {prueba['prueba']}")

        for k, v in (prueba.get('observado') or {}).items():
            if k == 'alarmas' or (isinstance(v, list) and not v):
                continue
            print(f'      {GRIS}{k}:{FIN} {v}')

        for fallo in prueba.get('fallos', []):
            print(f'      {ROJO}→ {fallo}{FIN}')

        if prueba.get('aviso'):
            print(f"      {AMAR}→ {prueba['aviso']}{FIN}")

        if prueba.get('pendiente_valor_referencia'):
            print(f'      {AMAR}→ sin valor de referencia en el canon ni en los flags{FIN}')

    print('\n  ' + '─' * 52)

    if not r['contexto_comparable']:
        print(f'  {AMAR}CONTEXTO NO COMPARABLE{FIN} — parámetros o insumos distintos '
              f'a los de la corrida certificada.')
        print(f'  {GRIS}Una divergencia aquí no acusa a la tubería. Iguala el contexto '
              f'y vuelve a correr.{FIN}\n')
        return 1

    if r['certificado']:
        print(f'  {VERDE}CERTIFICADO{FIN} — línea base lista.')
        print(f'  {GRIS}Siguiente: VIGIA_CARGAR_TXT=false + arranque de ingesta Connekta.{FIN}\n')
        return 0

    print(f'  {ROJO}NO CERTIFICADO{FIN} — con contexto comparable, la divergencia es real.')
    print(f'  {GRIS}Investigar la diferencia (corte de semanas, ventana, parseo). '
          f'No aflojar el criterio. No devolver VIGIA_CARGAR_TXT a false.{FIN}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main())

"""Dos textos del e2e del día completo (2026-09-25) que se leían mal.

- Jornada: «todas a menos de 0 m una de otra» — bajo un metro es el mismo punto.
- Recepción: «DEVOLUCIÓNES» — el plural se armaba pegando «ES» a «DEVOLUCIÓN».
"""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def test_la_cercania_no_dice_cero_metros():
    from app.services.jornada_conductor import _cercania_de
    assert _cercania_de(None) == ''
    assert _cercania_de(0.0) == ', todas desde el mismo punto'
    assert _cercania_de(0.4) == ', todas desde el mismo punto'
    assert _cercania_de(37.6) == ', todas a no más de 38 m una de otra'
    assert '0 m' not in _cercania_de(0.2)


def test_ningun_plural_se_arma_pegando_es_a_una_tilde():
    """«DEVOLUCIÓN» + «ES» = «DEVOLUCIÓNES». Ningún literal del PWA termina en
    «ÓN»/«ón» con un plural «ES»/«es» pegado por un ternario."""
    patron = re.compile(r'[ÓóÁáÉéÍíÚú][Nn]\$\{[^}]*\?\s*[\'"](?:ES|es)[\'"]\s*:')
    malos = {f.name: patron.findall(f.read_text(encoding='utf-8'))
             for f in sorted((RAIZ / 'app' / 'static' / 'pwa').glob('*.js'))}
    malos = {k: v for k, v in malos.items() if v}
    assert not malos, malos


def test_meta_el_patron_ve_el_caso():
    patron = re.compile(r'[ÓóÁáÉéÍíÚú][Nn]\$\{[^}]*\?\s*[\'"](?:ES|es)[\'"]\s*:')
    assert patron.search("DEVOLUCIÓN${n !== 1 ? 'ES' : ''}")
    assert not patron.search("${n !== 1 ? 'DEVOLUCIONES' : 'DEVOLUCIÓN'}")

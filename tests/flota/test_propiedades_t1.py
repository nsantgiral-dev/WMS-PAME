"""
Los siete invariantes de la tanda 1 — `docs/flota/ESPECIFICACION_T1.md` §6.

ESCRITOS ANTES DE LA IMPLEMENTACIÓN, a propósito. Un test escrito después
verifica que el código hace lo que el código hace: en este repo una fórmula
dimensionalmente imposible (`sqrt(sigma_LT)`, raíz de una desviación en días)
sobrevivió meses y pasó 631 tests, porque ninguno comparaba contra una propiedad
del problema.

Cada test de acá comprueba una propiedad que tiene que ser cierta sin importar
cómo esté escrito el código: monotonía, cardinalidad, cobertura, exclusividad,
paternidad, no-degradación y borde degenerado.

──────────────────────────────────────────────────────────────────────────────
POR QUÉ ESTÁN MARCADOS `xfail(strict=True)` Y NO SIMPLEMENTE ROJOS

Los siete fallan hoy, y deben fallar: la implementación no existe. Pero el
`buildCommand` de Railway corre `pytest tests/ -x` y bloquea el deploy con el
primer rojo. Siete tests rojos a propósito dejarían todo el WMS sin poder
desplegar mientras dure la tanda 1 — el andamiaje de un módulo nuevo no puede
secuestrar el deploy del sistema que ya está en producción.

`xfail(strict=True)` es el trinquete correcto para esto:

  · hoy fallan y el build sigue verde (`xfailed`),
  · el día que se implemente, el test pasa y pytest lo reporta como
    `XPASS(strict)` = FALLO, obligando a quitar el marcador.

Es decir: no se puede implementar en silencio y no se puede olvidar el marcador.
Para ver los fallos reales con su razón: `pytest tests/flota/ --runxfail -q`.
──────────────────────────────────────────────────────────────────────────────
"""
from datetime import datetime, timedelta

import pytest

from flota.dominio import custodia as dom_custodia
from flota.dominio import fotos as dom_fotos
from flota.dominio import odometro as dom_odometro
from flota.dominio.errores import CustodiaInvalida, FotoInvalida, LecturaRechazada
from flota.dominio.valores import (
    SIN_DATO,
    ClaseFoto,
    Custodia,
    CustodioTipo,
    Dimensiones,
    EntidadFoto,
    Foto,
    Lectura,
    OrigenLectura,
)

pytestmark = pytest.mark.xfail(
    strict=True,
    reason='Tanda 1 sin implementar. Al implementar: quitar este marcador '
           '(strict lo obliga — el test pasará y romperá el build hasta que se quite).',
)

_T0 = datetime(2026, 8, 1, 5, 0)
_AUTOR = 77


def _lectura(km, minutos, origen=OrigenLectura.ENTREGA, motivo=None):
    return Lectura(
        valor_km=km,
        ts=_T0 + timedelta(minutes=minutos),
        origen=origen,
        autor_usuario_id=_AUTOR,
        motivo_correccion=motivo,
    )


def _custodia(inicio_min, fin_min=None, tipo=CustodioTipo.CONDUCTOR,
              conductor_id=5, sede_id=None):
    return Custodia(
        vehiculo_id=1,
        custodio_tipo=tipo,
        inicio_ts=_T0 + timedelta(minutes=inicio_min),
        fin_ts=None if fin_min is None else _T0 + timedelta(minutes=fin_min),
        registrado_por_usuario_id=_AUTOR,
        km_inicio=100_000,
        custodio_conductor_id=conductor_id,
        custodio_sede_id=sede_id,
    )


# ══════════════════════════════════════════════════════════════════════════
# 1. MONOTONÍA — el odómetro nunca decrece salvo corrección declarada
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteMonotonia:

    def test_una_lectura_menor_a_la_anterior_se_rechaza(self):
        previas = [_lectura(100_000, 0)]
        with pytest.raises(LecturaRechazada):
            dom_odometro.validar_lectura(previas, _lectura(99_500, 60))

    def test_una_correccion_con_motivo_y_autor_si_puede_decrecer(self):
        """La única puerta. Existe para dejar rastro de quién decidió y por qué."""
        previas = [_lectura(100_000, 0)]
        correccion = _lectura(
            99_500, 60,
            origen=OrigenLectura.CORRECCION,
            motivo='digitación: se registró 100000 en vez de 99500',
        )
        dom_odometro.validar_lectura(previas, correccion)  # no levanta

    def test_una_correccion_sin_motivo_se_rechaza(self):
        """Sin motivo, una corrección es indistinguible de un error de digitación."""
        previas = [_lectura(100_000, 0)]
        with pytest.raises(LecturaRechazada):
            dom_odometro.validar_lectura(
                previas, _lectura(99_500, 60, origen=OrigenLectura.CORRECCION)
            )

    def test_igual_o_mayor_siempre_se_acepta(self):
        """Un vehículo que no se movió tiene la misma lectura, no una menor."""
        previas = [_lectura(100_000, 0)]
        dom_odometro.validar_lectura(previas, _lectura(100_000, 60))
        dom_odometro.validar_lectura(previas, _lectura(100_120, 60))


# ══════════════════════════════════════════════════════════════════════════
# 2. CARDINALIDAD — 0 o 1 custodia activa. Nunca dos.
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteCardinalidad:

    def test_dos_custodias_abiertas_a_la_vez_es_invalido(self):
        """Dos responsables del mismo camión es, en la práctica, ninguno."""
        with pytest.raises(CustodiaInvalida):
            dom_custodia.validar_cardinalidad([_custodia(0), _custodia(120)])

    def test_una_activa_es_valido(self):
        dom_custodia.validar_cardinalidad([_custodia(0, 120), _custodia(120)])

    def test_ninguna_activa_es_valido(self):
        """Un vehículo puede estar sin custodio: recién comprado, dado de baja."""
        dom_custodia.validar_cardinalidad([_custodia(0, 120)])

    def test_activas_cuenta_solo_las_de_fin_nulo(self):
        activas = dom_custodia.custodias_activas([_custodia(0, 120), _custodia(120)])
        assert len(activas) == 1


# ══════════════════════════════════════════════════════════════════════════
# 3. COBERTURA TEMPORAL — ningún instante sin custodio
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteCoberturaTemporal:

    def test_un_hueco_entre_dos_custodias_se_detecta(self):
        """Cierre a los 120 min, apertura a los 180: una hora sin responsable."""
        huecos = dom_custodia.huecos_de_cobertura(
            [_custodia(0, 120), _custodia(180, 240)],
            ahora=_T0 + timedelta(minutes=240),
        )
        assert len(huecos) == 1
        assert huecos[0].desde == _T0 + timedelta(minutes=120)
        assert huecos[0].hasta == _T0 + timedelta(minutes=180)

    def test_traspaso_atomico_no_deja_hueco(self):
        """El traspaso cierra y abre en la misma transacción. Sin instante intermedio."""
        huecos = dom_custodia.huecos_de_cobertura(
            [_custodia(0, 120), _custodia(120)],
            ahora=_T0 + timedelta(minutes=300),
        )
        assert huecos == []

    def test_no_se_reclama_cobertura_antes_de_la_primera_custodia(self):
        """Antes del arranque en frío el sistema no sabe, y no pretende saber."""
        huecos = dom_custodia.huecos_de_cobertura(
            [_custodia(600)], ahora=_T0 + timedelta(minutes=700),
        )
        assert huecos == []


# ══════════════════════════════════════════════════════════════════════════
# 4. ARCO EXCLUSIVO — exactamente un custodio, y corresponde a su tipo
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteArcoExclusivo:

    def test_los_dos_nulos_es_invalido(self):
        with pytest.raises(CustodiaInvalida):
            dom_custodia.validar_arco_exclusivo(
                _custodia(0, conductor_id=None, sede_id=None)
            )

    def test_los_dos_llenos_es_invalido(self):
        with pytest.raises(CustodiaInvalida):
            dom_custodia.validar_arco_exclusivo(
                _custodia(0, conductor_id=5, sede_id=3)
            )

    def test_el_lleno_debe_corresponder_al_tipo(self):
        """`tipo=sede` con `conductor_id` lleno no dice de quién es la responsabilidad."""
        with pytest.raises(CustodiaInvalida):
            dom_custodia.validar_arco_exclusivo(
                _custodia(0, tipo=CustodioTipo.SEDE, conductor_id=5, sede_id=None)
            )

    def test_conductor_con_su_id_es_valido(self):
        dom_custodia.validar_arco_exclusivo(
            _custodia(0, tipo=CustodioTipo.CONDUCTOR, conductor_id=5, sede_id=None)
        )


# ══════════════════════════════════════════════════════════════════════════
# 5. PATERNIDAD — ninguna foto huérfana
# ══════════════════════════════════════════════════════════════════════════

def _foto(clase=ClaseFoto.FOTO_DATO, entidad=EntidadFoto.ODOMETRO, entidad_id=1,
          ancho=1600, alto=1200):
    return Foto(
        clase=clase,
        entidad_tipo=entidad,
        entidad_id=entidad_id,
        storage_ref='s3://flota/2026/08/abc.jpg',
        hash_sha256='0' * 64,
        bytes=421_337,
        dimensiones=Dimensiones(ancho=ancho, alto=alto),
        autor_usuario_id=_AUTOR,
    )


class TestInvariantePaternidad:

    def test_una_foto_sin_padre_resoluble_se_rechaza(self):
        """Un archivo sin padre es evidencia que nadie va a encontrar."""
        with pytest.raises(FotoInvalida):
            dom_fotos.validar_paternidad(_foto(), resolver=lambda tipo, id_: False)

    def test_una_foto_con_padre_existente_se_acepta(self):
        dom_fotos.validar_paternidad(_foto(), resolver=lambda tipo, id_: True)

    def test_el_resolvedor_recibe_tipo_e_id_de_la_foto(self):
        """La paternidad se comprueba contra la entidad declarada, no contra cualquiera."""
        vistos = []

        def resolver(tipo, id_):
            vistos.append((tipo, id_))
            return True

        dom_fotos.validar_paternidad(
            _foto(entidad=EntidadFoto.CUSTODIA_INICIO, entidad_id=42), resolver
        )
        assert vistos == [(EntidadFoto.CUSTODIA_INICIO, 42)]


# ══════════════════════════════════════════════════════════════════════════
# 6. INTEGRIDAD DE CLASE — una foto_dato no se degrada
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteIntegridadDeClase:

    def test_foto_dato_servida_mas_chica_que_la_capturada_se_rechaza(self):
        """Un odómetro que no se puede leer contra su foto es una declaración sin respaldo."""
        with pytest.raises(FotoInvalida):
            dom_fotos.validar_integridad_de_clase(
                ClaseFoto.FOTO_DATO,
                capturada=Dimensiones(2400, 1800),
                servida=Dimensiones(800, 600),
            )

    def test_foto_dato_servida_igual_a_la_capturada_se_acepta(self):
        dom_fotos.validar_integridad_de_clase(
            ClaseFoto.FOTO_DATO,
            capturada=Dimensiones(2400, 1800),
            servida=Dimensiones(2400, 1800),
        )

    def test_evidencia_de_estado_si_puede_degradarse(self):
        """Prueba cómo estaba algo, no respalda un número. Reusa la compresión existente."""
        dom_fotos.validar_integridad_de_clase(
            ClaseFoto.EVIDENCIA_ESTADO,
            capturada=Dimensiones(2400, 1800),
            servida=Dimensiones(800, 600),
        )


# ══════════════════════════════════════════════════════════════════════════
# 7. BORDE DEGENERADO — sin lecturas es `sin_dato`, jamás 0
# ══════════════════════════════════════════════════════════════════════════

class TestInvarianteBordeDegenerado:

    def test_un_vehiculo_sin_lecturas_devuelve_sin_dato(self):
        assert dom_odometro.odometro_actual([]) is SIN_DATO

    def test_sin_dato_no_es_cero_ni_falsy(self):
        """Un `sin_dato` falsy invita a `valor or 0`, que es el default optimista.

        0 km es 'no ha rodado'. `sin_dato` es 'no sabemos'. Colapsarlos convierte
        todo CPK y todo preventivo por km en un número inventado con cara de medición.
        """
        vacio = dom_odometro.odometro_actual([])
        assert vacio != 0
        assert bool(vacio) is True

    def test_con_lecturas_devuelve_el_ultimo_kilometraje(self):
        assert dom_odometro.odometro_actual(
            [_lectura(100_000, 0), _lectura(100_450, 600)]
        ) == 100_450

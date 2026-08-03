"""
El catálogo de inspección: que diga lo que el criterio dice.

No se prueba "que haya ítems". Se prueba que el catálogo cumpla las reglas que
lo hacen útil — porque un catálogo que no las cumple produce inspecciones que
parecen control y son ritual.
"""
from datetime import date

import pytest

from flota.adaptadores.catalogo import CATALOGO, sembrar
from flota.dominio.inspeccion import dias_de_plazo, items_del_dia, ordenar_para_el_dia


@pytest.fixture
def catalogo(db):
    sembrar(db)
    from flota.adaptadores.modelos import PlantillaInspeccion
    return {p.codigo: p for p in PlantillaInspeccion.query.all()}


class TestElCatalogoCumpleSuCriterio:

    def test_furgon_liviano_tiene_8_bloqueantes_y_camion_9(self, catalogo):
        """La diferencia es exactamente una: las puertas del furgón."""
        assert len(catalogo['furgon_liviano_v1'].bloqueantes()) == 8
        assert len(catalogo['camion_v1'].bloqueantes()) == 9

    def test_la_diferencia_es_las_puertas_del_furgon(self, catalogo):
        f = {i.nombre for i in catalogo['furgon_liviano_v1'].bloqueantes()}
        c = {i.nombre for i in catalogo['camion_v1'].bloqueantes()}
        assert c - f == {'Puertas del furgón aseguran'}
        assert f - c == set()

    def test_todo_item_trae_su_gesto(self, catalogo):
        """Sin el gesto, la criticidad es decorativa.

        "Revisar frenos" no dice qué hacer y termina en un óptimo marcado sin
        mirar. "Pisar a fondo y sostener 5 segundos" sí dice.
        """
        sin_gesto = [i.nombre for p in catalogo.values() for i in p.items
                     if not (i.gesto or '').strip()]
        assert not sin_gesto, f'Ítems sin gesto: {sin_gesto}'

    def test_todo_gesto_es_una_pregunta_contestable(self, catalogo):
        """Cuarta condición del criterio: respuesta binaria, no un juicio.

        Se aproxima exigiendo que el gesto contenga una pregunta. No garantiza
        que sea binaria —eso no lo puede juzgar un test— pero atrapa el ítem
        redactado como orden vaga: "revisar el sistema de frenos".
        """
        sin_pregunta = [i.nombre for p in catalogo.values() for i in p.items
                        if '?' not in i.gesto]
        assert not sin_pregunta, (
            f'Gestos sin pregunta: {sin_pregunta}. Un ítem que no pregunta algo '
            'contestable se responde de memoria.'
        )

    def test_ningun_item_de_taller_se_coló_al_chequeo_diario(self, catalogo):
        """La lista de lo que NO va importa tanto como la de lo que va.

        Nada de esto lo evalúa un conductor en patio, y cada ítem incontestable
        en la pantalla diaria entrena el reflejo de marcar óptimo sin mirar.
        """
        prohibidos = ('pastilla', 'banda', 'amortiguador', 'terminal', 'rodamiento',
                      'compresión', 'turbo', 'alineación', 'balanceo', 'repartición')
        colados = [
            i.nombre for p in catalogo.values() for i in p.items
            if any(x in i.nombre.lower() for x in prohibidos)
        ]
        assert not colados, (
            f'Ítems de taller en el chequeo diario: {colados}. '
            'Van al plan preventivo por kilómetro, no a la pantalla del conductor.'
        )

    def test_el_drenaje_del_separador_es_semanal_y_solo_de_camion(self, catalogo):
        semanales = {i.nombre for p in catalogo.values() for i in p.items
                     if i.periodicidad == 'semanal'}
        assert semanales == {'Drenaje del separador de agua'}
        assert 'Drenaje del separador de agua' not in {
            i.nombre for i in catalogo['furgon_liviano_v1'].items}


class TestPlazoPorRegla:
    """Regla 6: se calcula al nacer, no se elige a mano."""

    def test_bloqueante_es_hoy(self):
        assert dias_de_plazo('bloqueante') == 0

    def test_mayor_siete_menor_treinta(self):
        assert dias_de_plazo('mayor') == 7
        assert dias_de_plazo('menor') == 30

    def test_una_criticidad_inventada_levanta(self):
        with pytest.raises(ValueError):
            dias_de_plazo('urgente')

    def test_el_item_hereda_su_plazo_sin_poder_elegirlo(self, catalogo):
        for i in catalogo['camion_v1'].items:
            assert i.dias_de_plazo == dias_de_plazo(i.criticidad)


class TestOrdenDePantalla:

    def test_los_bloqueantes_van_primero_y_en_orden_fijo(self, catalogo):
        items = catalogo['camion_v1'].items
        for dia in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 9, 15)):
            orden = ordenar_para_el_dia(items, dia)
            bloq = [i for i in orden if i.criticidad == 'bloqueante']
            assert orden[:len(bloq)] == bloq, 'un no-bloqueante se coló antes'
            assert [i.orden for i in bloq] == sorted(i.orden for i in bloq)

    def test_el_freno_siempre_es_el_primero(self, catalogo):
        """Se citan por número: el orden fijo es memoria muscular útil."""
        primero = ordenar_para_el_dia(catalogo['camion_v1'].items, date(2026, 8, 3))[0]
        assert primero.nombre == 'Freno de servicio'

    def test_los_no_bloqueantes_cambian_de_orden_entre_dias(self, catalogo):
        """Con orden fijo, a la tercera semana el pulgar responde sin leer."""
        items = catalogo['camion_v1'].items
        a = [i.nombre for i in ordenar_para_el_dia(items, date(2026, 8, 3))]
        b = [i.nombre for i in ordenar_para_el_dia(items, date(2026, 8, 4))]
        assert a != b, 'el orden no cambió entre días: se puede memorizar'

    def test_pero_el_mismo_dia_es_el_mismo_orden(self, catalogo):
        """Reproducible: dos conductores el mismo día ven lo mismo, y una
        inspección de hace tres meses se puede auditar."""
        items = catalogo['camion_v1'].items
        a = [i.nombre for i in ordenar_para_el_dia(items, date(2026, 8, 3))]
        b = [i.nombre for i in ordenar_para_el_dia(items, date(2026, 8, 3))]
        assert a == b

    def test_ningun_item_se_pierde_ni_se_duplica_al_ordenar(self, catalogo):
        items = catalogo['camion_v1'].items
        orden = ordenar_para_el_dia(items, date(2026, 8, 3))
        assert sorted(i.id for i in orden) == sorted(i.id for i in items)


class TestPeriodicidad:

    def test_el_semanal_solo_aparece_los_lunes(self, catalogo):
        items = catalogo['camion_v1'].items
        lunes = {i.nombre for i in items_del_dia(items, date(2026, 8, 3))}
        martes = {i.nombre for i in items_del_dia(items, date(2026, 8, 4))}
        assert 'Drenaje del separador de agua' in lunes
        assert 'Drenaje del separador de agua' not in martes

    def test_los_bloqueantes_estan_todos_los_dias(self, catalogo):
        items = catalogo['camion_v1'].items
        for dia in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 8)):
            del_dia = [i for i in items_del_dia(items, dia) if i.criticidad == 'bloqueante']
            assert len(del_dia) == 9


class TestSembradoIdempotente:

    def test_sembrar_dos_veces_no_duplica(self, db):
        from flota.adaptadores.modelos import ItemInspeccion, PlantillaInspeccion
        sembrar(db)
        n_p, n_i = PlantillaInspeccion.query.count(), ItemInspeccion.query.count()
        r = sembrar(db)
        assert PlantillaInspeccion.query.count() == n_p
        assert ItemInspeccion.query.count() == n_i
        assert set(r.values()) == {'ya existía'}

    def test_una_plantilla_no_se_edita_se_versiona(self, catalogo):
        """El código lleva la versión adentro: `camion_v1`, no `camion`.

        Una inspección hecha bajo v1 tiene que seguir siendo legible en dos
        años. Si los ítems cambiaran bajo sus pies, el registro diría una cosa
        y significaría otra.
        """
        for codigo in CATALOGO:
            assert codigo['codigo'].endswith('_v1')
        assert catalogo['camion_v1'].version == 1

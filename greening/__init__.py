"""Разметка генплана для озеленения.

    from greening import parse, export
    result = parse('генплан.dxf', 'геоподоснова.dxf')
    export.write_geojson(result, 'out.geojson')
    export.write_dxf(result, 'out.dxf')

Шаги: чтение DXF с раскрытием блоков → территория по «Границе работ» → карта поверхностей
(штриховки покрытий генплана, остальное — по граням линий и подписям) → структуры (площадки,
малые формы, существующие деревья) → секции газонов с допустимыми посадками, условиями места
и нормами, которые их ограничивают. Правила — в config/layers.json и config/norms.json.
"""

from .pipeline import Result, parse

__all__ = ['Result', 'parse']

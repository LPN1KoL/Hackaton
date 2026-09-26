"""Отрисовка DXF в SVG средствами ezdxf."""

import xml.etree.ElementTree as ET

import ezdxf.recover
from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg

SVG_NS = 'http://www.w3.org/2000/svg'
BACKGROUND = '#FFFFFF'
MAX_SVG_BYTES = 80 * 1024 * 1024


class RenderError(Exception):
    pass


class LayeredRenderBackend(svg.SVGRenderBackend):
    """Раскладывает примитивы по группам слоёв: штатный бэкенд этого не умеет."""

    def __init__(self, page, settings):
        super().__init__(page, settings)
        self._root = self.entities
        self._groups = {}

    def _group(self, layer):
        group = self._groups.get(layer)
        if group is None:
            group = ET.SubElement(self._root, 'g')
            group.set('data-layer', layer)
            self._groups[layer] = group
        return group

    def add_strokes(self, d, properties):
        self.entities = self._group(properties.layer)
        super().add_strokes(d, properties)

    def add_filling(self, d, properties):
        self.entities = self._group(properties.layer)
        super().add_filling(d, properties)


class LayeredSVGBackend(svg.SVGBackend):
    @staticmethod
    def make_backend(page, settings):
        return LayeredRenderBackend(page, settings)


def _layers(doc):
    return [
        {
            'name': layer.dxf.name,
            'color': '#%02x%02x%02x' % layer.rgb if layer.rgb else layer.color,
            'visible': layer.is_on() and not layer.is_frozen(),
        }
        for layer in doc.layers
    ]


def _stretchable(svg_string):
    """Убирает размеры в миллиметрах, чтобы картинка тянулась по контейнеру."""
    ET.register_namespace('', SVG_NS)
    root = ET.fromstring(svg_string)
    root.attrib.pop('width', None)
    root.attrib.pop('height', None)
    root.set('preserveAspectRatio', 'xMidYMid meet')
    return ET.tostring(root, encoding='unicode')


def render_svg(stream):
    """Возвращает {'svg': str | None, 'layers': [...], 'note': str | None}."""
    try:
        doc, _ = ezdxf.recover.read(stream)
    except Exception as exc:
        raise RenderError('Не удалось разобрать файл: %s' % exc) from exc

    msp = doc.modelspace()
    context = RenderContext(doc)
    context.set_current_layout(msp)
    # на белом фоне ezdxf сам разворачивает ACI 7 в чёрный
    context.current_layout_properties.set_colors(bg=BACKGROUND)

    backend = LayeredSVGBackend()
    try:
        Frontend(context, backend).draw_layout(
            msp,
            finalize=True,
            layout_properties=context.current_layout_properties,
        )
    except Exception as exc:
        raise RenderError('Не удалось отрисовать чертёж: %s' % exc) from exc

    if not backend.player().bbox().has_data:
        return {
            'svg': None,
            'layers': _layers(doc),
            'note': 'В файле нет геометрии для отрисовки.',
        }

    page = layout.Page(0, 0, layout.Units.mm, layout.Margins.all(0))
    picture = backend.get_string(page, xml_declaration=False)

    if len(picture.encode('utf-8')) > MAX_SVG_BYTES:
        note = 'Чертёж слишком большой для превью.'
    else:
        note = None

    return {
        'svg': None if note else _stretchable(picture),
        'layers': _layers(doc),
        'note': note,
    }

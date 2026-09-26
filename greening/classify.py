"""Классы слоёв, покрытия штриховок и подписи — по правилам из config/layers.json."""

import json
import re
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / 'config' / 'layers.json'


def base_name(layer):
    """Имя слоя без префикса листа: «output[1-18]_…tp$0$Водосток» → «Водосток», «$130$БР…» → «БР…»."""
    return re.sub(r'^\$\d+\$', '', layer.split('$0$')[-1])


def normalize(name):
    return re.sub(r'[_\s|\-]+', ' ', name.casefold()).strip()


class Classifier:
    def __init__(self, path=CONFIG):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        self.ignore = [re.compile(p) for p in data['ignore']]
        self.rules = [(re.compile(r['pattern']), r['kind'], r['priority']) for r in data['rules']]
        self.surface_codes = data['surface_codes']
        self.surface_hatch = re.compile(data['surface_hatch_pattern'])
        self.surface_layers = [(re.compile(r['pattern']), r['surface']) for r in data.get('surface_hatch_layers', [])]
        self.labels = [(re.compile(r['pattern']), r.get('structure'), r.get('surface')) for r in data['labels']]
        self.kind = lru_cache(maxsize=None)(self._kind)
        self.hatch_surface = lru_cache(maxsize=None)(self._hatch_surface)

    def _kind(self, name):
        """Класс по имени слоя или блока; 'ignore' — оформление, None — не распознан."""
        name = normalize(base_name(name))
        best = None
        for regex, kind, priority in self.rules:
            if regex.search(name) and (best is None or priority > best[1]):
                best = (kind, priority)
        if best:
            return best[0]
        if any(regex.search(name) for regex in self.ignore):
            return 'ignore'
        return None

    def _hatch_surface(self, layer):
        """Проектное покрытие штриховки: «_АБ ТР-ГЗН» → 'lawn', «!Project_hatch road grass» → 'lawn'; иначе None."""
        name = base_name(layer).strip()
        match = self.surface_hatch.match(name)
        if match:
            return self.surface_codes.get(match.group(2))
        name = normalize(name)
        return next((surface for regex, surface in self.surface_layers if regex.search(name)), None)

    def label(self, text):
        """Подпись → (structure, surface) или None."""
        key = re.sub(r'\s+', '', text.casefold())
        for regex, structure, surface in self.labels:
            if regex.search(key):
                return structure, surface
        return None

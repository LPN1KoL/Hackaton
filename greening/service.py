"""Весь конвейер одним вызовом: чертёж → разметка → подбор растений LLM → рассадка → DXF и данные превью.

    python -m greening.service ГЕНПЛАН.dxf [--geobase ОСНОВА.dxf] [--out ПАПКА] [--no-llm] [--seed N] [--placement model]

От Django не зависит: веб-сервис (core/jobs.py) запускает run() в фоне и показывает ход по progress.
Без ключа LLM (или с --no-llm) растения подбираются правилами (rules.py) — прогон работает без сети.

Файлы в out_dir:
    greening.geojson  — разметка (территория, поверхности, секции, структуры)
    plan.json         — подбор растений и обоснования по структурам и зонам
    placement.geojson — рассадка (точки растений, пятна цветников и газона) и сводка
    clearance.json    — проверка отступов рассадки по исходной геометрии (clearance.py)
    result.dxf        — исходный чертёж + слои рассадки, зон допустимости и пометок Greening_*
    view.json         — данные интерактивного превью (view.build)
    justification.json — обоснование для выгрузки: то же, что карточки превью (view.report)
    summary.json      — итог для экрана результата
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from . import annotate, clearance, export, parse, place, recommend, sections, view as view_data

logger = logging.getLogger(__name__)

# этапы для индикатора: код → подпись
STAGES = {
    'markup': 'Разметка чертежа',
    'llm': 'Подбор растений',
    'placement': 'Рассадка',
    'dxf': 'Сборка DXF',
    'view': 'Подготовка превью',
}


def _write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def make_llm(cache_dir=None, use_llm=True):
    """LLM из .env или None — тогда подбор правилами (нет ключа, выключено настройкой)."""
    if not use_llm:
        return None
    env = recommend.load_env()
    try:
        return recommend.LLM(env, cache_dir=cache_dir)
    except RuntimeError as exc:
        logger.warning('%s — подбор растений правилами, без LLM', exc)
        return None


def write_dxf(base, path, geojson, plan, placement, check=None):
    """Исходный чертёж + зоны допустимости, рассадка, номера зон и нарушения — каждое на своём слое Greening_*."""
    import ezdxf.recover

    doc, _ = ezdxf.recover.readfile(base)
    # заливки зон — под рассадкой, подписи и нарушения — поверх
    annotate.add_layers(doc, geojson, labels=False)
    place.add_layers(doc, placement)
    annotate.add_layers(doc, geojson, plan, check, fills=False)
    doc.saveas(path)


def run(plan_path, geobase_path, out_dir, cache_dir=None, progress=None, workers=5, limit=None, seed=0, use_llm=True,
        placement='algorithm'):
    """Возвращает сводку; progress(этап, готово=None, всего=None) сообщает, что сейчас делается.
    placement — 'algorithm' (места выбирает эвристика) или 'model' (обученная модель, model.py)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda stage, done=None, total=None: None)

    report('markup')
    result = parse(plan_path, geobase_path, log=logger.info)
    geojson = export.to_geojson(result)
    _write(out / 'greening.geojson', geojson)
    # для проверки отступов нужна только исходная геометрия нормированных объектов и территория
    obstacles, territory = result.obstacles, result.territory
    del result

    report('llm', 0, None)
    llm = make_llm(cache_dir, use_llm)
    plan = recommend.recommend(geojson, llm, limit, workers, log=logger.info,
                               progress=lambda done, total: report('llm', done, total))
    _write(out / 'plan.json', plan)

    report('placement')
    placement = place.place(geojson, plan, seed, mode=placement, log=logger.info)
    problems = place.verify(placement, geojson)
    placement['metadata']['summary']['violations'] = len(problems)
    _write(out / 'placement.geojson', placement)
    # количество растений определяет рассадка — в план оно попадает после неё
    _write(out / 'plan.json', place.apply_counts(plan, placement))
    check = clearance.check(placement, obstacles, sections.load_norms(), territory)
    del obstacles
    _write(out / 'clearance.json', check)

    report('dxf')
    write_dxf(plan_path, out / 'result.dxf', geojson, plan, placement, check)

    report('view')
    view = view_data.build(geojson, plan, placement)
    _write(out / 'view.json', view)
    _write(out / 'justification.json', view_data.report(view, plan))

    summary = {
        'territory_m2': geojson['metadata']['summary'].get('territory_m2'),
        'green_m2': geojson['metadata']['summary'].get('green_m2'),
        'structures': plan['summary']['structures'],
        'zones': plan['summary']['zones'],
        'zones_explained': plan['summary']['zones_explained'],
        'llm_errors': sum('llm_error' in s for s in plan['structures']),
        'by_rules': plan['summary']['rules'],
        'tokens': plan['summary']['usage'].get('total_tokens', 0),
        'cached_requests': plan['summary']['usage'].get('cached_requests', 0),
        'fallback_requests': plan['summary']['usage'].get('fallback_requests', 0),
        'plants': placement['metadata']['summary']['totals'],
        'violations': len(problems),
        'placement': placement['metadata']['mode'],
        **({'placement_note': placement['metadata']['mode_note']} if 'mode_note' in placement['metadata'] else {}),
        'clearance_violations': check['summary']['violations'],
        'clearance_checked': check['summary']['plants'],
        'clearance_approvals': check['summary']['approvals'],
    }
    _write(out / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description='Озеленение по генплану: DXF → разметка → подбор → рассадка → DXF')
    parser.add_argument('plan', help='генплан, DXF')
    parser.add_argument('--geobase', help='геоподоснова, DXF (необязательно)')
    parser.add_argument('--out', help='папка результата (по умолчанию <имя>.result рядом с генпланом)')
    parser.add_argument('--no-llm', action='store_true', help='без LLM: подбор растений правилами')
    parser.add_argument('--limit', type=int, help='в LLM — только N крупнейших структур')
    parser.add_argument('--seed', type=int, default=0, help='зерно случайной рассадки')
    parser.add_argument('--placement', choices=place.MODES, default='algorithm',
                        help='как выбирать места посадки: algorithm — эвристика, model — обученная модель')
    parser.add_argument('--cache', default='.llm_cache', help='кэш ответов LLM (по умолчанию .llm_cache)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    source = Path(args.plan)
    out = Path(args.out) if args.out else source.with_name(source.stem + '.result')

    def progress(stage, done=None, total=None):
        suffix = f' {done} из {total}' if total else ''
        print(f'{STAGES.get(stage, stage)}{suffix}', file=sys.stderr, flush=True)

    summary = run(source, args.geobase, out, cache_dir=args.cache, progress=progress, limit=args.limit,
                  seed=args.seed, use_llm=not args.no_llm, placement=args.placement)
    print(json.dumps({'out': str(out), 'dxf': str(out / 'result.dxf'), **summary}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()

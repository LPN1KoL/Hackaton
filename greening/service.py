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
    obstacles.pkl     — исходная геометрия нормируемых объектов: пересчёт без новой разметки (markup_from)
                        проверяет отступы по ней

Пересчёт результата (редактирование): run(..., markup_from=папка прежнего результата, preferences=текст) —
разметка не повторяется (берутся greening.geojson и obstacles.pkl оттуда), заново идут подбор растений
с пожеланиями, рассадка, DXF и обоснования. plan_from — папка, откуда взять и подбор растений (plan.json):
тогда LLM не вызывается, пересчитываются только рассадка и всё после неё. params — параметры рассадки
(params.py: плотность, группы, пределы, стиль, режим, зерно).
"""

import argparse
import json
import logging
import pickle
import shutil
import sys
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

from . import annotate, clearance, export, params as placement_params, parse, place, recommend, sections, species, variety
from . import view as view_data

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


def move(plan_path, geobase_path, parent_out, out_dir, moves, params=None, progress=None):
    """Перемещения растений (редактирование, уровень 3): новая версия результата из прежней (parent_out) —
    растения стоят там, куда их передвинули (moves — {номер: (x, y)} в координатах чертежа); рассадка заново
    не считается. Проверяются правила (verify) и отступы по чертежу, собираются DXF, просмотр и обоснования."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parent = Path(parent_out)
    report = progress or (lambda stage, done=None, total=None: None)

    report('placement')
    geojson, obstacles, territory = _markup(plan_path, geobase_path, out, parent)
    plan = json.loads((parent / 'plan.json').read_text(encoding='utf-8'))
    _write(out / 'plan.json', plan)
    placement = json.loads((parent / 'placement.geojson').read_text(encoding='utf-8'))
    moved = place.apply_moves(placement, moves)
    config, _, _ = placement_params.apply(placement_params.clean(params or {}))
    problems = place.verify(placement, geojson, config, species.Restrictions(obstacles))
    placement['metadata']['summary']['violations'] = len(problems)
    _write(out / 'placement.geojson', placement)
    check = clearance.check(placement, obstacles, sections.load_norms(), territory)
    _write(out / 'clearance.json', check)

    report('dxf')
    write_dxf(plan_path, out / 'result.dxf', geojson, plan, placement, check)

    report('view')
    view = view_data.build(geojson, plan, placement)
    _write(out / 'view.json', view)
    _write(out / 'justification.json', view_data.report(view, plan))

    summary = json.loads((parent / 'summary.json').read_text(encoding='utf-8'))
    summary.update(violations=len(problems), clearance_violations=check['summary']['violations'],
                   clearance_checked=check['summary']['plants'], clearance_approvals=check['summary']['approvals'],
                   moved=placement['metadata']['moved'], moved_now=moved, tokens=0, plan_reused=True)
    _write(out / 'summary.json', summary)
    return summary


def write_dxf(base, path, geojson, plan, placement, check=None):
    """Исходный чертёж + зоны допустимости, рассадка, номера зон и нарушения — каждое на своём слое Greening_*."""
    import ezdxf.recover

    doc, _ = ezdxf.recover.readfile(base)
    # заливки зон — под рассадкой, подписи и нарушения — поверх
    annotate.add_layers(doc, geojson, labels=False)
    place.add_layers(doc, placement)
    annotate.add_layers(doc, geojson, plan, check, fills=False)
    doc.saveas(path)


def _markup(plan_path, geobase_path, out, markup_from):
    """(разметка geojson, объекты для проверки отступов, территория): новая или из прежнего результата."""
    if markup_from is not None:
        source = Path(markup_from)
        if (source / 'greening.geojson').is_file() and (source / 'obstacles.pkl').is_file():
            for name in ('greening.geojson', 'obstacles.pkl'):
                if (source / name).resolve() != (out / name).resolve():
                    shutil.copyfile(source / name, out / name)
            geojson = json.loads((out / 'greening.geojson').read_text(encoding='utf-8'))
            obstacles = pickle.loads((out / 'obstacles.pkl').read_bytes())
            territory = unary_union([shape(f['geometry']) for f in geojson['features']
                                     if f['properties']['layer'] == 'territory'])
            return geojson, obstacles, territory
        logger.info('В %s нет разметки или геометрии для проверки — разметка заново', source)
    result = parse(plan_path, geobase_path, log=logger.info)
    geojson = export.to_geojson(result)
    _write(out / 'greening.geojson', geojson)
    # для проверки отступов нужна только исходная геометрия нормированных объектов и территория
    (out / 'obstacles.pkl').write_bytes(pickle.dumps(result.obstacles))
    return geojson, result.obstacles, result.territory


def run(plan_path, geobase_path, out_dir, cache_dir=None, progress=None, workers=5, limit=None, seed=0, use_llm=True,
        placement='algorithm', preferences=None, markup_from=None, plan_from=None, params=None):
    """Возвращает сводку; progress(этап, готово=None, всего=None) сообщает, что сейчас делается.
    placement — 'algorithm' (места выбирает эвристика) или 'model' (обученная модель, model.py);
    preferences — пожелания к подбору растений (текст в запрос LLM); markup_from — папка прежнего результата,
    из которой берётся разметка (пересчёт без новой разметки)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = progress or (lambda stage, done=None, total=None: None)

    report('markup')
    geojson, obstacles, territory = _markup(plan_path, geobase_path, out, markup_from)

    # нормы по видам (у теплотрасс — МГСН 1.02-02, п. 4.2.8) — по исходной геометрии сетей
    restrictions = species.Restrictions(obstacles)

    report('llm', 0, None)
    reuse = Path(plan_from) / 'plan.json' if plan_from else None
    if reuse is not None and reuse.is_file():
        # пересчёт по параметрам рассадки: подбор растений — прежний, LLM не вызывается
        plan = json.loads(reuse.read_text(encoding='utf-8'))
        plan['summary']['usage'] = {}
    else:
        llm = make_llm(cache_dir, use_llm)
        plan = recommend.recommend(geojson, llm, limit, workers, log=logger.info,
                                   progress=lambda done, total: report('llm', done, total), preferences=preferences,
                                   restrictions=restrictions)
    _write(out / 'plan.json', plan)

    report('placement')
    chosen = placement_params.clean(params or {})
    config, mode, chosen_seed = placement_params.apply(chosen)
    # спутники посадок (миксбордер, второй вид смешанных групп) — по параметрам рассадки, без LLM
    variety.compose(plan, config)
    placement_mode = mode or placement
    seed = seed if chosen_seed is None else chosen_seed
    placement = place.place(geojson, plan, seed, config=config, mode=placement_mode, log=logger.info,
                            restrictions=restrictions)
    problems = place.verify(placement, geojson, config, restrictions)
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

    # отброшенные копии границы работ (territory._drop_copies) — чтобы было видно, что взято не всё
    territory_note = next((f['properties'].get('note') for f in geojson['features']
                           if f['properties']['layer'] == 'territory'), None)
    summary = {
        **({'territory_note': territory_note} if territory_note else {}),
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
        **({'preferences': plan['preferences']} if plan.get('preferences') else {}),
        **({'preferences_note': plan['preferences_note']} if plan.get('preferences_note') else {}),
        'placement': placement['metadata']['mode'],
        'seed': seed,
        **({'params': placement_params.changed(chosen)} if chosen else {}),
        **({'plan_reused': True} if reuse is not None and reuse.is_file() else {}),
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
    parser.add_argument('--preferences', help='пожелания к подбору растений: текст, уходит в запрос LLM')
    parser.add_argument('--markup-from', help='папка прежнего результата: разметка берётся оттуда, не делается заново')
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
                  seed=args.seed, use_llm=not args.no_llm, placement=args.placement,
                  preferences=args.preferences, markup_from=args.markup_from)
    print(json.dumps({'out': str(out), 'dxf': str(out / 'result.dxf'), **summary}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()

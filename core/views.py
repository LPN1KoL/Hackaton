import json
import logging
import math
import threading
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import jobs
from greening import params as placement_params
from greening import scene as greening_scene

from .forms import DxfUploadForm, ProcessUploadForm, ReviseForm
from .rendering import RenderError, render_svg

logger = logging.getLogger(__name__)


def index(request):
    return render(request, 'core/index.html', {
        'preview_on_upload': settings.PREVIEW_ON_UPLOAD,
    })


SCHEMA = Path(__file__).resolve().parent / 'static' / 'core' / 'openapi.json'


# GET — страница Swagger UI, POST — та же спека в JSON.
# csrf_exempt: POST ничего не меняет, а у стороннего клиента токена нет.
# ensure_csrf_cookie: «Try it out» шлёт настоящие запросы, и cookie нужна,
# даже если зашли сразу на /docs/, минуя главную.
@csrf_exempt
@ensure_csrf_cookie
def docs(request):
    if request.method == 'POST':
        return HttpResponse(SCHEMA.read_text(encoding='utf-8'), content_type='application/json')
    return render(request, 'core/docs.html')


def _preview(request):
    form = DxfUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({'errors': form.errors}, status=400)

    dxf = form.cleaned_data['file']
    dxf.seek(0)
    try:
        preview = render_svg(dxf.file)
    except RenderError as exc:
        return JsonResponse({'errors': {'file': [str(exc)]}}, status=400)

    return JsonResponse({'name': dxf.name, 'size': dxf.size, **preview})


@require_POST
def preview(request):
    return _preview(request)


@require_POST
def upload(request):
    """Запускает обработку в фоне и сразу отвечает номером задачи; ход — GET /jobs/<id>/."""
    form = ProcessUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({'errors': form.errors}, status=400)

    files = {}
    for name in ('file', 'geobase'):
        upload = form.cleaned_data[name]
        if upload is None:
            continue
        upload.seek(0)
        files[name] = (upload.name, upload.read())

    job_id = jobs.start(files, form.cleaned_data.get('preferences') or None,
                        placement=form.cleaned_data.get('placement') or settings.GREENING_PLACEMENT)
    dxf = form.cleaned_data['file']
    return JsonResponse({'job': job_id, 'name': dxf.name, 'size': dxf.size}, status=202)


@require_POST
def job_revise(request, job_id):
    """Пересчёт готового результата: новая задача-версия, разметка не повторяется. preferences — пожелания
    к растениям (заново подбор LLM), params — параметры рассадки {ключ: значение} (GET /params/; без новых
    пожеланий LLM не вызывается). Формой (params — JSON-строкой) или JSON-телом; ответ — как у /upload/, плюс parent."""
    data = request.POST
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'errors': {'body': ['Тело запроса — не JSON.']}}, status=400)
    # нет поля — пожелания прежние; пустое поле — пожелания сняты
    given = 'preferences' in data
    form = ReviseForm({'preferences': data.get('preferences') or ''})
    if not form.is_valid():
        return JsonResponse({'errors': form.errors}, status=400)
    raw = data.get('params') or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return JsonResponse({'errors': {'params': ['Параметры — не JSON.']}}, status=400)
    if not isinstance(raw, dict):
        return JsonResponse({'errors': {'params': ['Параметры — объект {ключ: значение}.']}}, status=400)
    try:
        chosen = placement_params.clean(raw)
    except placement_params.ParamError as exc:
        return JsonResponse({'errors': {f'params.{k}': [v] for k, v in exc.errors.items()}}, status=400)
    if not given and not chosen:
        return JsonResponse({'errors': {'preferences': ['Напишите пожелания к растениям или измените параметры рассадки.']}},
                            status=400)
    state = jobs.status(job_id)
    if state is None:
        raise Http404('Нет такой задачи')
    if state.get('state') != 'done':
        return JsonResponse({'errors': {'job': ['Исходная задача ещё не готова.']}}, status=409)
    new_id = jobs.revise(job_id, form.cleaned_data['preferences'] if given else None, chosen)
    return JsonResponse({'job': new_id, 'name': state.get('name'), 'size': state.get('size'), 'parent': job_id},
                        status=202)


# перемещений за раз не больше — защита от случайно огромного запроса
MOVES_MAX = 100000


@require_POST
def job_move(request, job_id):
    """Перемещения растений (редактирование, уровень 3): JSON {"moves": [{"id": "R00012", "x": …, "y": …}]}
    в координатах чертежа. Новая задача-версия: растения там, куда передвинули; рассадка не пересчитывается."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'errors': {'body': ['Тело запроса — не JSON.']}}, status=400)
    raw = data.get('moves') if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return JsonResponse({'errors': {'moves': ['Нет перемещений.']}}, status=400)
    if len(raw) > MOVES_MAX:
        return JsonResponse({'errors': {'moves': [f'Не больше {MOVES_MAX} перемещений за раз.']}}, status=400)
    moves = {}
    for item in raw:
        try:
            pid, x, y = str(item['id']), float(item['x']), float(item['y'])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({'errors': {'moves': ['Перемещение — {"id": "R…", "x": число, "y": число}.']}}, status=400)
        if not (math.isfinite(x) and math.isfinite(y)) or not pid.startswith('R'):
            return JsonResponse({'errors': {'moves': [f'Неверное перемещение {pid}.']}}, status=400)
        moves[pid] = (x, y)
    state = jobs.status(job_id)
    if state is None:
        raise Http404('Нет такой задачи')
    if state.get('state') != 'done':
        return JsonResponse({'errors': {'job': ['Исходная задача ещё не готова.']}}, status=409)
    new_id = jobs.move(job_id, moves)
    return JsonResponse({'job': new_id, 'name': state.get('name'), 'size': state.get('size'), 'parent': job_id},
                        status=202)


@require_GET
def params_schema(request):
    """Параметры рассадки для формы редактирования: группы, ключи, пределы, значения по умолчанию."""
    return JsonResponse(placement_params.schema(default_placement=settings.GREENING_PLACEMENT))


@require_GET
def job(request, job_id):
    state = jobs.status(job_id)
    if state is None:
        raise Http404('Нет такой задачи')
    # опрос статуса — знак, что результат ещё ждут: брошенная задача не держит очередь (core/jobs.py)
    jobs.touch(job_id)
    return JsonResponse(state)


@require_POST
def job_cancel(request, job_id):
    """Отмена задачи: страница шлёт её, когда её закрывают. Готовые результаты не трогаются."""
    state = jobs.status(job_id)
    if state is None:
        raise Http404('Нет такой задачи')
    if state.get('state') in ('queued', 'running'):
        jobs.cancel(job_id)
    return JsonResponse({'job': job_id, 'state': state.get('state')}, status=202)


# что отдаётся из папки результата: имя в URL → (файл, тип, скачивание под именем)
RESULTS = {
    'view': ('view.json', 'application/json', None),
    'dxf': ('result.dxf', 'image/vnd.dxf', '{stem}_озеленение.dxf'),
    'plan': ('plan.json', 'application/json', '{stem}_план.json'),
    'report': ('justification.json', 'application/json', '{stem}_обоснование.json'),
    'clearance': ('clearance.json', 'application/json', '{stem}_проверка_отступов.json'),
    'placement': ('placement.geojson', 'application/geo+json', '{stem}_рассадка.geojson'),
}


@require_GET
def job_result(request, job_id, kind):
    if kind not in RESULTS:
        raise Http404('Нет такого результата')
    name, content_type, download = RESULTS[kind]
    path = jobs.result(job_id, name)
    if path is None:
        raise Http404('Результат ещё не готов')
    stem = Path(jobs.status(job_id).get('name', 'result')).stem
    response = FileResponse(path.open('rb'), content_type=content_type)
    if download:
        response['Content-Disposition'] = content_disposition_header(True, download.format(stem=stem))
    return response


@require_GET
def job_3d(request, job_id):
    """Отдельная страница 3D-вида результата (core/static/core/scene.js): открывается в новой вкладке."""
    state = jobs.status(job_id)
    if state is None:
        raise Http404('Нет такой задачи')
    return render(request, 'core/scene.html', {'job_id': job_id, 'name': state.get('name', '')})


_scene_lock = threading.Lock()


@require_GET
def job_scene(request, job_id):
    """Данные 3D-сцены: строятся при первом запросе из файлов результата (около секунды) и кэшируются."""
    path = jobs.result(job_id, 'scene.json')
    if path is None or not greening_scene.fresh(path):
        if jobs.result(job_id, 'placement.geojson') is None:
            raise Http404('Результат ещё не готов')
        with _scene_lock:
            path = jobs.result(job_id, 'scene.json')
            if path is None or not greening_scene.fresh(path):
                root = jobs.folder(job_id)
                sources = [(root / f'{name}.dxf', source) for name, source in (('file', 'plan'), ('geobase', 'base'))
                           if (root / f'{name}.dxf').is_file()]
                path = greening_scene.write(root / 'out', sources)
    if path is None:
        raise Http404('Нет данных для 3D')
    return FileResponse(path.open('rb'), content_type='application/json')

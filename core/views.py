import logging
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import jobs
from .forms import DxfUploadForm, ProcessUploadForm
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

    job_id = jobs.start(files)
    dxf = form.cleaned_data['file']
    return JsonResponse({'job': job_id, 'name': dxf.name, 'size': dxf.size}, status=202)


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
    'plan': ('plan.json', 'application/json', '{stem}_обоснование.json'),
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

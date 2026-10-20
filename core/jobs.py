"""Фоновые задачи обработки: конвейер greening считается минутами, запрос его не ждёт.

Задача — папка в settings.JOBS_DIR: входные файлы, результаты greening.service и status.json.
Статус пишется в файл, поэтому готовые результаты переживают перезапуск сервера;
задача, прерванная перезапуском, так и останется «в работе» — её нужно запустить заново.

Редактирование результата (revise): новая задача-версия от готовой — те же входные файлы, разметка берётся
из её результата (не считается заново). Новые пожелания к растениям — заново подбор растений LLM; только
параметры рассадки — подбор растений берётся из исходной задачи (LLM не вызывается), пересчитываются рассадка,
проверка отступов, DXF и обоснования. У версии в статусе parent, preferences и params (параметры рассадки,
накопленные по версиям: новые поверх параметров исходной).
Перемещения растений (move): версия, в которой растения стоят там, куда их передвинули на сайте; рассадка
не пересчитывается, заново — проверки, DXF, просмотр и обоснования. Пересчёт пожеланий или параметров после
этого строит рассадку заново — перемещения пропадают (сайт об этом предупреждает).

Брошенные задачи не держат очередь. Страница опрашивает статус (GET /jobs/<id>/) каждые полторы секунды;
если опросов нет дольше settings.GREENING_JOB_ABANDON_S или пришла отмена (POST /jobs/<id>/cancel/ —
страница шлёт её, когда её закрывают), задача считается брошенной: в очереди — снимается, не начавшись,
в работе — останавливается на ближайшей контрольной точке (после разметки, после каждой структуры в LLM,
после рассадки) и освобождает место. Состояние такой задачи — cancelled.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from django.conf import settings

from greening import service

logger = logging.getLogger(__name__)

ID = re.compile(r'^[0-9a-f]{32}$')
# одновременно считается не больше стольких задач: разметка большого генплана занимает гигабайты
_slots = threading.BoundedSemaphore(settings.GREENING_JOBS)
_lock = threading.Lock()
# время последнего опроса статуса и отменённые задачи — в памяти процесса (gunicorn — один процесс)
_seen = {}
_cancelled = set()
# как часто задача в очереди проверяет, не брошена ли она, с
QUEUE_CHECK_S = 5


class Cancelled(Exception):
    """Задачу бросили: страницу закрыли или она давно не опрашивала статус."""


def touch(job_id):
    """Клиент ещё ждёт результат (опросил статус)."""
    _seen[job_id] = time.time()


def cancel(job_id):
    _cancelled.add(job_id)


def _abandoned(job_id):
    if job_id in _cancelled:
        return 'обработку отменили: страницу закрыли'
    idle = time.time() - _seen.get(job_id, 0)
    if idle > settings.GREENING_JOB_ABANDON_S:
        return f'обработку отменили: статус не запрашивался {idle:.0f} с — страницу, видимо, закрыли'
    return None


def folder(job_id):
    if not ID.match(job_id or ''):
        return None
    path = Path(settings.JOBS_DIR) / job_id
    return path if path.is_dir() else None


def status(job_id):
    path = folder(job_id)
    if path is None:
        return None
    # на Windows в миг замены (_write_status) файл не открывается или кратко отсутствует — повтор
    for _ in range(20):
        try:
            return json.loads((path / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            time.sleep(0.01)
    return None


def _save(path, **fields):
    with _lock:
        current = {}
        try:
            current = json.loads((path / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        current.update(fields, updated=time.time())
        _write_status(path, current)


def _write_status(path, data):
    """status.json — через временный файл: опрос статуса не застаёт его наполовину записанным (тогда был 404
    «задача не найдена» посреди счёта — Олимпийская, 20.10)."""
    temp = path / 'status.json.tmp'
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    for _ in range(40):
        try:
            os.replace(temp, path / 'status.json')
            return
        except PermissionError:
            # Windows не заменяет файл, пока его читает опрос статуса, — это доли миллисекунды
            time.sleep(0.025)
    os.replace(temp, path / 'status.json')


def _launch(path, work):
    """Ставит задачу в очередь: work(progress) считает и возвращает сводку."""
    touch(path.name)
    threading.Thread(target=_run, args=(path, work), name=f'greening-{path.name[:8]}', daemon=True).start()


def _settings():
    return dict(cache_dir=settings.GREENING_LLM_CACHE, workers=settings.GREENING_LLM_WORKERS,
                limit=settings.GREENING_LLM_LIMIT, use_llm=settings.GREENING_LLM, placement=settings.GREENING_PLACEMENT)


def start(files, preferences=None, placement='algorithm'):
    """files: {'file': (имя, байты), 'geobase': (имя, байты)?}; preferences — пожелания к подбору растений;
    placement — режим рассадки со стартовой страницы (algorithm | model), версии наследуют его в params.
    Возвращает номер задачи."""
    job_id = uuid.uuid4().hex
    path = Path(settings.JOBS_DIR) / job_id
    path.mkdir(parents=True)
    inputs = {}
    for name, (_, content) in files.items():
        inputs[name] = path / f'{name}.dxf'
        inputs[name].write_bytes(content)
    _save(path, state='queued', name=files['file'][0], size=len(files['file'][1]),
          stage=None, done=None, total=None, started=time.time(), params={'placement': placement},
          **({'preferences': preferences} if preferences else {}))
    _launch(path, lambda progress: service.run(inputs['file'], inputs.get('geobase'), path / 'out', progress=progress,
                                               preferences=preferences, **{**_settings(), 'placement': placement}))
    return job_id


def _link(source, target):
    """Входной файл версии — жёсткая ссылка на файл исходной задачи (чертёж бывает на сотни МБ), иначе копия."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _version(parent_id, **fields):
    """Папка новой версии готовой задачи с теми же входными файлами: (path, inputs, статус исходной) или None."""
    parent = folder(parent_id)
    state = status(parent_id)
    if parent is None or not state or state.get('state') != 'done':
        return None
    path = Path(settings.JOBS_DIR) / uuid.uuid4().hex
    path.mkdir(parents=True)
    inputs = {}
    for name in ('file', 'geobase'):
        if (parent / f'{name}.dxf').is_file():
            inputs[name] = path / f'{name}.dxf'
            _link(parent / f'{name}.dxf', inputs[name])
    _save(path, state='queued', name=state.get('name'), size=state.get('size'), parent=parent_id,
          stage=None, done=None, total=None, started=time.time(), **fields)
    return path, inputs, state


def revise(parent_id, preferences=None, params=None):
    """Версия готовой задачи: пожелания к подбору растений и/или параметры рассадки (проверенные params.clean).
    Номер новой задачи или None, если исходная не найдена или не готова."""
    state = status(parent_id)
    if not state or state.get('state') != 'done':
        return None
    before = state.get('preferences') or None
    # None — пожелания не трогали (остаются прежние); пустая строка — пожелания сняли
    preferences = before if preferences is None else (preferences.strip() or None)
    params = {**(state.get('params') or {}), **(params or {})}
    made = _version(parent_id, preferences=preferences, params=params)
    if made is None:
        return None
    path, inputs, _ = made
    parent_out = folder(parent_id) / 'out'
    # пожелания те же — подбор растений не меняется, LLM не нужна
    plan_from = parent_out if preferences == before else None
    _launch(path, lambda progress: service.run(inputs['file'], inputs.get('geobase'), path / 'out', progress=progress,
                                               preferences=preferences, markup_from=parent_out, plan_from=plan_from,
                                               params=params, **_settings()))
    return path.name


def move(parent_id, moves):
    """Версия готовой задачи с перемещёнными растениями (moves — {номер: (x, y)}). Номер новой задачи или None."""
    state = status(parent_id)
    if not state or state.get('state') != 'done':
        return None
    made = _version(parent_id, preferences=state.get('preferences'), params=state.get('params') or {},
                    moves=len(moves))
    if made is None:
        return None
    path, inputs, _ = made
    parent_out = folder(parent_id) / 'out'
    _launch(path, lambda progress: service.move(inputs['file'], inputs.get('geobase'), parent_out, path / 'out',
                                                moves, params=state.get('params') or {}, progress=progress))
    return path.name


def _run(path, work):
    job_id = path.name
    # в очереди: брошенная задача уходит, не дождавшись места
    while not _slots.acquire(timeout=QUEUE_CHECK_S):
        reason = _abandoned(job_id)
        if reason:
            logger.info('Задача %s снята из очереди: %s', job_id, reason)
            _save(path, state='cancelled', error=reason, finished=time.time())
            return
    try:
        _save(path, state='running')

        def progress(stage, done=None, total=None):
            reason = _abandoned(job_id)
            if reason:
                raise Cancelled(reason)
            _save(path, stage=stage, stage_title=service.STAGES.get(stage, stage), done=done, total=total)

        try:
            summary = work(progress)
        except Cancelled as exc:
            logger.info('Задача %s остановлена: %s', job_id, exc)
            _save(path, state='cancelled', error=str(exc), finished=time.time())
            return
        except Exception as exc:
            logger.exception('Обработка %s не удалась', job_id)
            _save(path, state='error', error=f'{type(exc).__name__}: {exc}')
            return
        _save(path, state='done', summary=summary, finished=time.time())
    finally:
        _slots.release()
        _seen.pop(job_id, None)
        _cancelled.discard(job_id)


def result(job_id, name):
    """Путь к файлу результата готовой задачи или None."""
    path = folder(job_id)
    state = status(job_id)
    if path is None or not state or state.get('state') != 'done':
        return None
    file = path / 'out' / name
    return file if file.is_file() else None

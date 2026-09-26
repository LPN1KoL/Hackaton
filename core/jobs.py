"""Фоновые задачи обработки: конвейер greening считается минутами, запрос его не ждёт.

Задача — папка в settings.JOBS_DIR: входные файлы, результаты greening.service и status.json.
Статус пишется в файл, поэтому готовые результаты переживают перезапуск сервера;
задача, прерванная перезапуском, так и останется «в работе» — её нужно запустить заново.

Брошенные задачи не держат очередь. Страница опрашивает статус (GET /jobs/<id>/) каждые полторы секунды;
если опросов нет дольше settings.GREENING_JOB_ABANDON_S или пришла отмена (POST /jobs/<id>/cancel/ —
страница шлёт её, когда её закрывают), задача считается брошенной: в очереди — снимается, не начавшись,
в работе — останавливается на ближайшей контрольной точке (после разметки, после каждой структуры в LLM,
после рассадки) и освобождает место. Состояние такой задачи — cancelled.
"""

import json
import logging
import re
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
    try:
        return json.loads((path / 'status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _save(path, **fields):
    with _lock:
        current = {}
        try:
            current = json.loads((path / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        current.update(fields, updated=time.time())
        (path / 'status.json').write_text(json.dumps(current, ensure_ascii=False), encoding='utf-8')


def start(files):
    """files: {'file': (имя, байты), 'geobase': (имя, байты)?}. Возвращает номер задачи."""
    job_id = uuid.uuid4().hex
    path = Path(settings.JOBS_DIR) / job_id
    path.mkdir(parents=True)
    inputs = {}
    for name, (_, content) in files.items():
        inputs[name] = path / f'{name}.dxf'
        inputs[name].write_bytes(content)
    _save(path, state='queued', name=files['file'][0], size=len(files['file'][1]),
          stage=None, done=None, total=None, started=time.time())
    touch(job_id)
    threading.Thread(target=_run, args=(path, inputs), name=f'greening-{job_id[:8]}', daemon=True).start()
    return job_id


def _run(path, inputs):
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
            summary = service.run(inputs['file'], inputs.get('geobase'), path / 'out',
                                  cache_dir=settings.GREENING_LLM_CACHE, progress=progress,
                                  workers=settings.GREENING_LLM_WORKERS, limit=settings.GREENING_LLM_LIMIT,
                                  use_llm=settings.GREENING_LLM)
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

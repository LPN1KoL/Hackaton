"""Фоновые задачи обработки: конвейер greening считается минутами, запрос его не ждёт.

Задача — папка в settings.JOBS_DIR: входные файлы, результаты greening.service и status.json.
Статус пишется в файл, поэтому готовые результаты переживают перезапуск сервера;
задача, прерванная перезапуском, так и останется «в работе» — её нужно запустить заново.
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
    threading.Thread(target=_run, args=(path, inputs), name=f'greening-{job_id[:8]}', daemon=True).start()
    return job_id


def _run(path, inputs):
    with _slots:
        _save(path, state='running')

        def progress(stage, done=None, total=None):
            _save(path, stage=stage, stage_title=service.STAGES.get(stage, stage), done=done, total=total)

        try:
            summary = service.run(inputs['file'], inputs.get('geobase'), path / 'out',
                                  cache_dir=settings.GREENING_LLM_CACHE, progress=progress,
                                  workers=settings.GREENING_LLM_WORKERS, limit=settings.GREENING_LLM_LIMIT,
                                  use_llm=settings.GREENING_LLM)
        except Exception as exc:
            logger.exception('Обработка %s не удалась', path.name)
            _save(path, state='error', error=f'{type(exc).__name__}: {exc}')
            return
        _save(path, state='done', summary=summary, finished=time.time())


def result(job_id, name):
    """Путь к файлу результата готовой задачи или None."""
    path = folder(job_id)
    state = status(job_id)
    if path is None or not state or state.get('state') != 'done':
        return None
    file = path / 'out' / name
    return file if file.is_file() else None

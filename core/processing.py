"""Отправка чертежа в сервис расстановки растений."""

import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class ProcessError(Exception):
    pass


def _multipart(files):
    """files: {поле: (имя файла, байты)}"""
    boundary = uuid.uuid4().hex
    body = b''
    for name, (filename, content) in files.items():
        body += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
        ).encode() + content + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return body, f'multipart/form-data; boundary={boundary}'


def process(files):
    """Возвращает ответ сервиса: {'dxf': base64 итогового чертежа, 'map_data': GeoJSON рассадки,
    'explanations': str}; files — чертёж (file) и геоподоснова (geobase)."""
    body, content_type = _multipart(files)
    request = Request(settings.PROCESS_API_URL, data=body, method='POST',
                      headers={'Content-Type': content_type})
    try:
        with urlopen(request, timeout=settings.PROCESS_API_TIMEOUT) as response:
            return json.load(response)
    except HTTPError as exc:
        # сервис кладёт причину в {"error": ...}
        try:
            message = json.load(exc).get('error')
        except (ValueError, AttributeError):
            message = None
        raise ProcessError(message or f'Сервис обработки ответил ошибкой {exc.code}.') from exc
    except (URLError, TimeoutError) as exc:
        raise ProcessError('Сервис обработки недоступен.') from exc
    except ValueError as exc:
        raise ProcessError('Сервис обработки вернул не JSON.') from exc

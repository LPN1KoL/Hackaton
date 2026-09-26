# Сервис озеленения по генплану: веб-интерфейс + конвейер DXF → разметка → подбор растений → рассадка → DXF.
#
#   docker compose up -d --build               — веб-сервис на http://localhost/ (nginx, порт 80)
#   docker compose run --rm greening python -m greening.service /data/Генплан.dxf --geobase /data/Основа.dxf --out /data/result
#                                              — тот же прогон из командной строки (папка ./data монтируется в /data)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DJANGO_DEBUG=0 \
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY manage.py TZ.md ./
COPY config ./config
COPY core ./core
COPY greening ./greening
COPY samples ./samples

# статика собирается при сборке образа; ключ здесь нужен только самой команде collectstatic
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput \
    && mkdir -p jobs .llm_cache data

EXPOSE 8080

# один процесс: фоновые задачи (core/jobs.py) — потоки внутри него; timeout 0 — загрузка большого DXF не обрывается
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system loop && useradd --system --gid loop --home-dir /app loop

COPY --chown=loop:loop . /app
RUN python -m pip install --upgrade pip && python -m pip install .

USER loop
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

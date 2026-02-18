FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

RUN pip install --upgrade pip \
    && pip install \
    "pydantic<3.0.0,>=2.0.0" \
    "pydantic-settings<3.0.0,>=2.1.0" \
    "python-dotenv<2.0.0,>=1.0.0" \
    "pytelegrambotapi<5.0.0,>=4.14.0" \
    "loguru<1.0.0,>=0.7.0" \
    "httpx<1.0.0,>=0.25.1" \
    "shortuuid<2.0.0,>=1.0.11" \
    "asgiref<4.0.0,>=3.7.2" \
    "aiohttp>=3.9.0" \
    "dynaconf>=3.2.4" \
    "frozenlist>=1.3.4" \
    "pysocks>=1.7.1" \
    "asyncpg>=0.31.0"

COPY app ./app
COPY utils ./utils
COPY setting ./setting
COPY conf_dir ./conf_dir
COPY app_conf.py main.py ./

CMD ["python", "main.py"]

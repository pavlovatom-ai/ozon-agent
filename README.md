# Ozon AI Helper

FastAPI-сервис для ответов на вопросы Ozon, аналитики и AI-отчётов.

## Безопасная настройка

1. Скопируйте `.env.example` в `.env`.
2. Заполните ключи Ozon и OpenRouter.
3. Не добавляйте `.env`, базу данных, `venv` и папку runtime в Git.

## Docker

```bash
cp .env.example .env
# заполните .env
docker compose up --build
```

Веб-интерфейс будет доступен на `http://localhost:8000`. Сервисы `web` и `worker` используют общий Docker volume `ozon_runtime` для базы, отчётов и снимка аналитики.

Остановить приложение:

```bash
docker compose down
```

## Локальный запуск

```bash
source venv/bin/activate
python web.py
```

Worker вопросов и автоматических задач запускается отдельно:

```bash
./run.sh
```

CI проверяет компиляцию Python, Jinja-шаблоны и сборку Docker-образа.

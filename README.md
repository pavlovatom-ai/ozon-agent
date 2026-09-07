# Ozon AI Helper

FastAPI-сервис для ответов на вопросы Ozon, аналитики и AI-отчётов.

## Управленческие возможности

- **Unit economics по SKU**: себестоимость, комиссия, логистика, налог, прибыль, маржинальность, ROI и минимальная безопасная цена.
- **Мониторинг конкурентов**: собственные SKU, ссылки на карточки, наблюдаемые цены и статусы относительно нашей цены и безопасного порога.
- **Аналитика по SKU**: продажи, остатки, конверсии, выкуп, возвратность и расчётные показатели.
- **AI-рекомендации**: агент отчётов использует расчёт unit economics как инструмент и формирует рекомендации на основе доступных данных.

Расчёты реализованы в проекте самостоятельно. Данные себестоимости и наблюдаемые цены конкурентов хранятся локально.

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

## CD на сервере

CD использует публичный GitHub-репозиторий и проверяет `main` каждые 5 минут.
При обновлении выполняется `docker compose up --build -d`; `.env` не трогается,
а база, отчёты и снимки аналитики сохраняются в Docker volume `ozon_runtime`.

Первоначальная установка на сервере:

```bash
sudo mkdir -p /opt/ozon-agent
sudo chown "$USER":"$USER" /opt/ozon-agent
git clone https://github.com/pavlovatom-ai/ozon-agent.git /opt/ozon-agent
cd /opt/ozon-agent
cp .env.example .env
chmod 600 .env
# заполните .env ключами на сервере
docker compose up --build -d

sudo install -m 0755 deploy/update.sh /opt/ozon-agent/deploy/update.sh
sudo install -m 0644 deploy/ozon-agent-update.service /etc/systemd/system/
sudo install -m 0644 deploy/ozon-agent-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ozon-agent-update.timer
```

Проверка CD:

```bash
sudo systemctl start ozon-agent-update.service
systemctl status ozon-agent-update.timer
docker compose ps
docker compose logs --tail=100 web worker
```

Не используйте `docker compose down -v`: эта команда удалит volume с базой и отчётами.

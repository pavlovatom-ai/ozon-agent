FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
	&& useradd --create-home --shell /usr/sbin/nologin appuser \
	&& mkdir -p /app/runtime/reports \
	&& chown -R appuser:appuser /app

COPY . .

COPY --chown=appuser:appuser . .

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/app/runtime/ozon_data.db
ENV REPORTS_DIR=/app/runtime/reports
ENV ANALYTICS_SNAPSHOT_PATH=/app/runtime/reports/analytics-dashboard.json

USER appuser
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000"]
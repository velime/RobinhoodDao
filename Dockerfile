FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY bottom ./bottom
COPY channels ./channels
RUN pip install --no-cache-dir .

ENV DB_PATH=/data/bottom.db \
    TG_SESSION=/data/telegram
VOLUME /data

CMD ["python", "-m", "bottom"]

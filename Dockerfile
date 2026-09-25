FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY swarm ./swarm
RUN pip install --no-cache-dir .

ENV DB_PATH=/data/swarm.db
VOLUME /data

CMD ["python", "-m", "swarm"]

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends rclone ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ijson==3.4.0
WORKDIR /app
COPY inventory.py /app/inventory.py
ENTRYPOINT ["python", "/app/inventory.py"]

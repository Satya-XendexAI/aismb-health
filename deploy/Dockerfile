FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

COPY . .

EXPOSE 8000 8001

# Default entrypoint. Overridden per-container in the K8s manifests:
#   whatsapp service  -> whatsapp:app        (port 8000)
#   chat UI interface -> interface.app:app   (port 8001)
CMD ["python", "-m", "uvicorn", "whatsapp:app", "--host", "0.0.0.0", "--port", "8000"]

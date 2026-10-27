FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INCIDENT_HOST=0.0.0.0 \
    INCIDENT_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the app itself: never .env, the local spending record or notes.
COPY scenario.py sim.py facts.py plans.py budget.py teammate.py server.py ./
COPY static ./static

EXPOSE 8080
CMD ["python", "server.py"]

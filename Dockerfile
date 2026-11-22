FROM python:3.12-slim

# HOST=0.0.0.0 so the platform can reach the server. Railway sets PORT itself.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOST=0.0.0.0

WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home app && chown -R app /app
USER app

CMD ["python", "server.py"]

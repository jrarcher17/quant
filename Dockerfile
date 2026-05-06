FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed by scipy/numpy and the Better Auth service at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY auth/package*.json ./auth/
RUN cd auth && npm ci

COPY . .
RUN cd auth && npm run build

ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["sh", "-c", "alembic upgrade head || echo 'Migration failed, starting anyway'; (cd /app/auth && npm run migrate) || echo 'Auth migration failed, starting anyway'; (cd /app/auth && npm run seed) || echo 'Auth seed failed, starting anyway'; (cd /app/auth && npm run start) & uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]

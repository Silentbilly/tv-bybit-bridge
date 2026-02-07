FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.prod.txt .
RUN python -m pip install --upgrade pip \
 && python -m pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.prod.txt \
 && python -c "import redis; import redis.asyncio; print(redis.__version__)"

COPY app ./app

EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]

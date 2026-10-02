FROM python:3.11-slim

# منع إنشاء ملفات .pyc وتفعيل الطباعة الفورية
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# متطلبات النظام
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# تثبيت Python deps أولاً (cache layer)
COPY requirements-stream.txt .
RUN pip install --upgrade pip && pip install -r requirements-stream.txt

# نسخ كود الخادم
COPY stream_server.py .

EXPOSE 8000

# فحص صحي
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "stream_server.py"]

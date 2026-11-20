# Tek imaj, iki rol: api (uvicorn) ve worker (python -m app.worker). Rolü compose'daki command belirler.
# Ortama özel hiçbir şey imajın içinde değil; test/UAT/canlı aynı imajı farklı ortam değişkenleriyle kullanır.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

# Önce bağımlılıklar: kod değişince bu katman yeniden kurulmaz
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Hangi commit çalışıyor? /health ve loglar bunu gösterir
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA

RUN useradd --create-home appuser
USER appuser

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

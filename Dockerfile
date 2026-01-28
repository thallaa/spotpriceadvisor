FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps (none needed beyond stdlib for current code)

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY spotpriceadvisor_api.py .

# Optional config mount: /etc/spotpriceadvisor/config.toml
EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "spotpriceadvisor_api:app"]

# Spotprice Advisor (API-based)

Lightweight Flask microservice that tells you the cheapest upcoming time window for electricity usage (dishwasher, oven, EV charging, etc.). It fetches Finnish Nord Pool spot prices from `https://api.porssisahko.net/v2/latest-prices.json`, adds VAT, and recommends the lowest-price contiguous window of configurable length.

## Features
- Configurable window length via query parameter (`?minutes=180` by default).
- Localized responses: Finnish (`fi`, default), English (`en`), Swedish (`sv`).
- Optional Bearer authentication token.
- Optional Redis cache for API responses.
- Docker image with gunicorn entrypoint; also runnable directly with `python`/`gunicorn`.

## Quickstart (Docker)

```bash
# Pull the published image (replace OWNER/PROJECT with your namespace once released)
docker pull ghcr.io/OWNER/spotpriceadvisor:latest

# Run on host port 5002, keep auth token, Redis cache disabled by default
docker run -d --rm \
  -p 5002:5000 \
  -e SPOTPRICE_TOKEN="mysecret" \  # REQUIRED: change from default sentinel
  --name spotpriceadvisor \
  ghcr.io/OWNER/spotpriceadvisor:latest

# Query (180-minute default window, Finnish)
curl -H "Authorization: Bearer mysecret" http://localhost:5002/

# Query 60-minute window in English
curl -H "Authorization: Bearer mysecret" "http://localhost:5002/?minutes=60&lang=en"
```

### With Redis cache
```bash
docker network create spotnet
docker run -d --rm --network spotnet --name redis redis:7-alpine
docker run -d --rm --network spotnet \
  -p 5002:5000 \
  -e SPOTPRICE_TOKEN="mysecret" \
  -e SPOTPRICE_CACHE=true \
  -e SPOTPRICE_REDIS_URL="redis://redis:6379/0" \
  ghcr.io/OWNER/spotpriceadvisor:latest
```

### Configuration file
The service reads an optional TOML config (defaults to `/etc/spotpriceadvisor/config.toml` or `SPOTPRICE_CONFIG`). Example:

```toml
# config.example.toml
[server]
token = "CHANGEME_SPOTPRICE_TOKEN"  # REQUIRED: change to your own value (or set to "" to disable auth)
port = 5000

[api]
url = "https://api.porssisahko.net/v2/latest-prices.json"
timeout = 10
user_agent = "spotpriceadvisor/1.0"

[cache]
enabled = true
redis_url = "redis://redis:6379/0"
ttl_seconds = 60
```

Mount it into the container:
```bash
docker run -d --rm -p 5002:5000 \
  -v /etc/spotpriceadvisor/config.toml:/etc/spotpriceadvisor/config.toml:ro \
  ghcr.io/OWNER/spotpriceadvisor:latest
```

## Standalone (no Docker)
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SPOTPRICE_TOKEN="mysecret"  # REQUIRED
export FLASK_APP=spotpriceadvisor_api.py
gunicorn -b 0.0.0.0:5000 spotpriceadvisor_api:app
```

## API usage
- Endpoint: `GET /`
- Headers: `Authorization: Bearer <token>` (omitted if token empty)
- Query parameters:
  - `minutes` (int, default 180, min 15, rounded up to 15-minute slots)
  - `lang` (`fi`|`en`|`sv`, default `fi`)

## Reverse proxy + HTTPS (LetsEncrypt)

### Nginx snippet
```
server {
    listen 80;
    server_name spot.example.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / {
        proxy_pass http://127.0.0.1:5002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

# After certbot issues the cert, switch to 443:
server {
    listen 443 ssl;
    server_name spot.example.com;
    ssl_certificate /etc/letsencrypt/live/spot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spot.example.com/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        proxy_pass http://127.0.0.1:5002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
Issue certificates:
```bash
sudo certbot certonly --nginx -d spot.example.com
sudo systemctl reload nginx
```

### Apache snippet
```
<VirtualHost *:80>
    ServerName spot.example.com
    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:5002/
    ProxyPassReverse / http://127.0.0.1:5002/
</VirtualHost>
```
Enable TLS with certbot:
```bash
sudo certbot --apache -d spot.example.com
```

## Building your own image
```bash
docker build -t ghcr.io/OWNER/spotpriceadvisor:latest .
docker run -d --rm -p 5002:5000 ghcr.io/OWNER/spotpriceadvisor:latest
```
For releases, publish tagged images (e.g., `:v1.0.0`) to your container registry.

## Environment variables summary
- `SPOTPRICE_TOKEN` – Bearer token (REQUIRED: service refuses to start if left at default sentinel; empty disables auth if set explicitly).
- `SPOTPRICE_PORT` – Internal listen port (default 5000).
- `SPOTPRICE_API_URL` – Override API endpoint.
- `SPOTPRICE_USER_AGENT` – UA for upstream API.
- `SPOTPRICE_CACHE` – `true`/`false` to enable Redis.
- `SPOTPRICE_REDIS_URL` – Redis URL.
- `SPOTPRICE_CACHE_TTL` – Cache TTL seconds (default 60).
- `SPOTPRICE_CONFIG` – Path to TOML config (default `/etc/spotpriceadvisor/config.toml`).

## License
MIT (or choose a license before publishing).

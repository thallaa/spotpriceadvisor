# Spotprice Advisor (API-based)

Lightweight Flask microservice that tells you the cheapest upcoming time window for electricity usage (dishwasher, oven, EV charging, etc.). It fetches Finnish Nord Pool spot prices from `https://api.porssisahko.net/v2/latest-prices.json`, adds VAT, and recommends the lowest-price contiguous window of configurable length.

## Features
- Configurable window length via query parameter (`?minutes=180` by default).
- Localized responses: Finnish (`fi`, default), English (`en`), Swedish (`sv`), Danish (`da`).
- Optional Bearer authentication token.
- Optional in-process cache for API responses (configurable TTL).
- Docker image with gunicorn entrypoint; also runnable directly with `python`/`gunicorn`.

## Quickstart (Docker)

```bash
# Pull the published image (replace OWNER/PROJECT with your namespace once released)
docker pull ghcr.io/thallaa/spotpriceadvisor:latest

# Run on host port 5002, keep auth token, cache disabled by default
docker run -d --rm \
  -p 5002:5000 \
  -e SPOTPRICE_TOKEN="mysecret" \  # REQUIRED: change from default sentinel
  --name spotpriceadvisor \
  ghcr.io/thallaa/spotpriceadvisor:latest

# Query (180-minute default window, Finnish)
curl -H "Authorization: Bearer mysecret" http://localhost:5002/

# Query 60-minute window in English
curl -H "Authorization: Bearer mysecret" "http://localhost:5002/?minutes=60&lang=en"
```

### With in-process cache
Enable a small in-memory cache (per process) to avoid hammering the upstream API:
```bash
docker run -d --rm \
  -p 5002:5000 \
  -e SPOTPRICE_TOKEN="mysecret" \
  -e SPOTPRICE_CACHE=true \
  -e SPOTPRICE_CACHE_TTL=60 \
  ghcr.io/thallaa/spotpriceadvisor:latest
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
  ghcr.io/thallaa/spotpriceadvisor:latest
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

## iPhone / Apple Watch Shortcut example

You can make Siri answer “Is electricity expensive?” by calling this API and speaking the response.

Steps (on iPhone):
1. Open Shortcuts → “+” → Add Action → **Get Contents of URL**.
   - URL: `https://your-domain.example.com/` (or your local network URL)
   - Method: GET
   - Headers: `Authorization : Bearer YOUR_TOKEN`
   - Add Query Items if needed: `minutes=180`, `lang=en` (or `fi/sv/da`)
2. Add Action → **Get Contents of URL** result → **Get Dictionary Value** (Key: `body`) if Shortcuts doesn’t auto treat it as text. Often the response is plain text already; if so, skip this.
3. Add Action → **Speak Text** (set language/voice to match `lang` you use).
4. Name the Shortcut, e.g., “Electricity price”.
5. To use on Apple Watch: enable “Show on Apple Watch” in Shortcut settings.
6. Trigger with Siri: “Hey Siri, electricity price” or “Is electricity expensive?”; Siri will call the endpoint and read the recommendation aloud.

Tip: If you want the default 180-minute window, just omit the `minutes` query item. To check a shorter task (e.g., oven 60 minutes), add `minutes=60` to the URL.

## Android / Wear OS (untested, try at your own risk)

This hasn’t been tested here, but a plausible approach uses Tasker + AutoVoice/AutoWear:
- Install Tasker and AutoVoice on the phone (AutoWear for Wear OS).
- Tasker Task: (1) HTTP Request GET `https://your.domain/?minutes=180&lang=en` with header `Authorization: Bearer YOUR_TOKEN`; (2) Say/TTS `%HTTPD` (response body), language matching `lang`.
- Profile: Event → Plugin → AutoVoice Recognized, Command: e.g., “is electricity expensive”, linked to the Task.
- Wear OS: use an AutoWear Tile or AutoVoice Assistant interception so a phrase like “Hey Google, ask AutoVoice is electricity expensive” runs the Task and speaks the reply.
Note: Google Assistant routines can’t natively speak dynamic HTTP responses; they can only speak static text or open a URL, so you need Tasker or similar.

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
docker build -t ghcr.io/thallaa/spotpriceadvisor:latest .
docker run -d --rm -p 5002:5000 ghcr.io/thallaa/spotpriceadvisor:latest
```
For releases, publish tagged images (e.g., `:v1.0.0`) to your container registry.

## Tests
- Install dev deps: `pip install -r requirements-dev.txt`
- Run: `pytest`

## Systemd service (non-Docker)

Example unit to run gunicorn directly:
```
[Unit]
Description=Spotprice Advisor
After=network.target

[Service]
User=spotprice
Group=spotprice
WorkingDirectory=/opt/spotpriceadvisor
Environment="SPOTPRICE_TOKEN=yourtoken"
Environment="SPOTPRICE_CACHE=true"
Environment="SPOTPRICE_CACHE_TTL=60"
ExecStart=/usr/bin/gunicorn -b 0.0.0.0:5000 spotpriceadvisor_api:app
Restart=on-failure
TimeoutStartSec=30

[Install]
WantedBy=multi-user.target
```
Steps:
1) Place code in `/opt/spotpriceadvisor` and install deps (venv recommended):  
   `python -m venv /opt/spotpriceadvisor/.venv && . /opt/spotpriceadvisor/.venv/bin/activate && pip install -r requirements.txt`
   Update `ExecStart` to point to venv gunicorn (e.g., `/opt/spotpriceadvisor/.venv/bin/gunicorn`).
2) Save unit as `/etc/systemd/system/spotpriceadvisor.service`.
3) `sudo systemctl daemon-reload && sudo systemctl enable --now spotpriceadvisor`.
4) Put your config in `/etc/spotpriceadvisor/config.toml` if you prefer file-based settings.

## Environment variables summary
- `SPOTPRICE_TOKEN` – Bearer token (REQUIRED: service refuses to start if left at default sentinel; empty disables auth if set explicitly).
- `SPOTPRICE_PORT` – Internal listen port (default 5000).
- `SPOTPRICE_API_URL` – Override API endpoint.
- `SPOTPRICE_USER_AGENT` – UA for upstream API.
- `SPOTPRICE_CACHE` – `true`/`false` to enable in-process cache.
- `SPOTPRICE_CACHE_TTL` – Cache TTL seconds (default 60).
- `SPOTPRICE_CACHE_TTL` – Cache TTL seconds (default 60).
- `SPOTPRICE_CONFIG` – Path to TOML config (default `/etc/spotpriceadvisor/config.toml`).

## License
MIT (or choose a license before publishing).

# Docker Container Monitor

A lightweight Python service that monitors Docker containers and sends Telegram notifications when they stop, crash, or restart. It automatically attempts to restart crashed containers and distinguishes between manual stops and unexpected crashes.

## Features

- **Crash detection** — alerts when a monitored container stops unexpectedly and attempts to restart it
- **Manual stop detection** — alerts when a container is stopped via `docker stop` or `docker kill`, without triggering a restart
- **Start notification** — alerts when a monitored container comes back up (manually or after an automatic restart)
- **OOM detection** — identifies and reports containers killed by the Linux OOM killer
- **Auto-recovery** — configurable number of restart attempts with a delay between each
- **Runs as a systemd service** — starts on boot and restarts automatically if it crashes

## How It Works

The monitor subscribes to the Docker event stream and listens for three events per container:

| Docker Event | Meaning |
|---|---|
| `stop` | Container was stopped by a management command |
| `die` | Container process exited (crash or after a stop) |
| `start` | Container started or restarted |

**Manual stop vs crash detection:**
Docker always fires `stop` → `die` in that order when a container is stopped via `docker stop` or `docker kill`. A crash only fires `die`. The monitor records `stop` events and checks whether a matching `stop` preceded each `die` to decide which case it is.

```
docker stop / docker kill  →  stop  →  die   (alert only, no restart)
container crash            →           die   (alert + restart)
```

## Telegram Notification Types

| Notification | Trigger |
|---|---|
| `🚨 Container stopped unexpectedly` | Container crashed or exited without a manual stop |
| `⏹️ Container stopped manually` | Container was stopped via `docker stop` or `docker kill` |
| `▶️ Container started` | Container started or restarted |
| `✅ Container back up` | Automatic restart succeeded |
| `❌ Container could not be restarted` | All restart attempts failed |
| `⚠️ Container removed — cannot restart` | Container was deleted from Docker |

## Requirements

- Python 3.9+
- Docker running on the host
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID (from [@userinfobot](https://t.me/userinfobot))

## Installation

**1. Clone the repository**

```bash
git clone <repo-url>
cd dockerAlerts
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

> On systems that enforce PEP 668 (Debian/Ubuntu/Raspberry Pi OS):
> ```bash
> pip install -r requirements.txt --break-system-packages
> ```

**3. Configure**

Copy and edit the configuration file:

```bash
nano config.yaml
```

Fill in your Telegram credentials and the names of the containers you want to monitor (see [Configuration](#configuration) below).

**4. Install as a systemd service**

```bash
sudo cp docker-alerts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable docker-alerts.service
sudo systemctl start docker-alerts.service
```

## Configuration

All settings live in `config.yaml`:

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN_HERE"   # Token from @BotFather
  chat_id: "YOUR_CHAT_ID_HERE"       # Your chat, group, or channel ID

docker:
  # Container names to monitor. Use ["*"] to monitor ALL containers.
  monitored_containers:
    - "my-app"
    - "my-database"
    - "nginx"

  # Seconds to wait before each restart attempt
  restart_delay: 5

  # How many restart attempts before giving up
  max_restart_attempts: 3
```

To find your container names:

```bash
docker ps --format "{{.Names}}"
```

## Service Management

```bash
# View live logs
sudo journalctl -u docker-alerts.service -f

# Check service status
sudo systemctl status docker-alerts.service

# Restart after editing config.yaml
sudo systemctl restart docker-alerts.service

# Stop the service
sudo systemctl stop docker-alerts.service
```

## Running Manually

```bash
python3 monitor.py                          # uses config.yaml in current directory
python3 monitor.py --config /path/to/config.yaml
```

## Project Structure

```
dockerAlerts/
├── monitor.py               # Main monitoring script
├── config.yaml              # Configuration file
├── requirements.txt         # Python dependencies
├── docker-alerts.service    # systemd unit file
└── README.md
```

## Systemd Service Details

The service is configured to:

- Start automatically on boot
- Wait for the Docker service to be available before starting (`After=docker.service`)
- Restart automatically 10 seconds after any crash (`Restart=always`)
- Log all output to the systemd journal (`journalctl`)

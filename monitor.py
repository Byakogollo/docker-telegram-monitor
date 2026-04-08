#!/usr/bin/env python3
"""
Docker container monitor — sends Telegram alerts when containers stop
and attempts to restart them.

Manual stop detection uses Docker events:
  docker stop / docker kill  →  'stop' fires, then 'die' fires
  crash                      →  'die' fires only (no preceding 'stop')

Because 'stop' always precedes 'die' for manual stops, we track recent
'stop' events and check when 'die' arrives.
"""

import threading
import time
import logging
import sys
from datetime import datetime, timezone

import docker
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Telegram message: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_exit_reason(exit_code: int, attrs: dict) -> str:
    oom = attrs.get("OOMKilled", "false").lower() == "true"
    error = attrs.get("Error", "").strip()
    parts = [f"exit code <b>{exit_code}</b>"]
    if oom:
        parts.append("killed by OOM")
    if error:
        parts.append(f"error: {error}")
    return ", ".join(parts)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def is_monitored(name: str, monitored: list) -> bool:
    if monitored == ["*"]:
        return True
    return name.lstrip("/") in monitored


# ---------------------------------------------------------------------------
# Restart (runs in a background thread to avoid blocking the event stream)
# ---------------------------------------------------------------------------

def attempt_restart(container, token: str, chat_id: str, max_attempts: int, restart_delay: int) -> None:
    name = container.name
    cid = container.short_id

    for attempt in range(1, max_attempts + 1):
        log.info("Restart attempt %d/%d for %s", attempt, max_attempts, name)
        time.sleep(restart_delay)
        try:
            container.reload()
            container.start()
            container.reload()
            if container.status == "running":
                send_telegram(token, chat_id, (
                    f"✅ <b>Container back up</b>\n"
                    f"Name: <code>{name}</code>\n"
                    f"ID: <code>{cid}</code>\n"
                    f"Restart attempt: {attempt}/{max_attempts}\n"
                    f"Time: {utc_now()}"
                ))
                log.info("Container %s restarted successfully.", name)
                return
        except docker.errors.NotFound:
            log.warning("Container %s no longer exists, cannot restart.", name)
            send_telegram(token, chat_id, (
                f"⚠️ <b>Container removed — cannot restart</b>\n"
                f"Name: <code>{name}</code>\n"
                f"ID: <code>{cid}</code>\n"
                f"The container no longer exists in Docker.\n"
                f"Time: {utc_now()}"
            ))
            return
        except docker.errors.APIError as e:
            log.error("Docker API error while restarting %s: %s", name, e)

    send_telegram(token, chat_id, (
        f"❌ <b>Container could not be restarted</b>\n"
        f"Name: <code>{name}</code>\n"
        f"ID: <code>{cid}</code>\n"
        f"All {max_attempts} restart attempt(s) failed.\n"
        f"Time: {utc_now()}"
    ))
    log.warning("All restart attempts failed for %s.", name)


def handle_crash(container_id: str, name: str, short_id: str, exit_reason: str,
                 client, token: str, chat_id: str, max_attempts: int, restart_delay: int) -> None:
    log.warning("Container %s (%s) crashed — %s", name, short_id, exit_reason)
    send_telegram(token, chat_id, (
        f"🚨 <b>Container stopped unexpectedly</b>\n"
        f"Name: <code>{name}</code>\n"
        f"ID: <code>{short_id}</code>\n"
        f"Reason: {exit_reason}\n"
        f"Time: {utc_now()}\n\n"
        f"⏳ Attempting to restart…"
    ))
    try:
        container = client.containers.get(container_id)
        attempt_restart(container, token, chat_id, max_attempts, restart_delay)
    except docker.errors.NotFound:
        send_telegram(token, chat_id, (
            f"⚠️ <b>Container removed — cannot restart</b>\n"
            f"Name: <code>{name}</code>\n"
            f"ID: <code>{short_id}</code>\n"
            f"The container no longer exists in Docker.\n"
            f"Time: {utc_now()}"
        ))
    except docker.errors.APIError as e:
        log.error("Docker API error fetching %s for restart: %s", name, e)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def monitor(config: dict) -> None:
    tg_token = config["telegram"]["bot_token"]
    tg_chat = str(config["telegram"]["chat_id"])
    monitored = config["docker"]["monitored_containers"]
    restart_delay = config["docker"]["restart_delay"]
    max_attempts = config["docker"]["max_restart_attempts"]

    client = docker.from_env()

    # container_id -> timestamp of 'stop' event
    # 'stop' fires before 'die' for manual stops; crashes only fire 'die'
    recent_stops: dict[str, float] = {}

    log.info("Monitoring started. Watching: %s", monitored)

    while True:
        try:
            for event in client.events(
                decode=True,
                filters={"type": "container", "event": ["stop", "die", "start"]},
            ):
                action = event.get("Action", "")
                actor = event.get("Actor", {})
                container_id = actor.get("ID", "")
                attrs = actor.get("Attributes", {})
                name = attrs.get("name", "").lstrip("/")
                short_id = container_id[:12]

                # --- stop: record it so the die handler can detect manual stops ---
                if action == "stop":
                    if is_monitored(name, monitored):
                        recent_stops[container_id] = time.monotonic()
                    continue

                # --- die: crash or manual stop ---
                if action == "die":
                    if not is_monitored(name, monitored):
                        continue

                    exit_code = int(attrs.get("exitCode", -1))
                    exit_reason = format_exit_reason(exit_code, attrs)

                    # Expire old stop records (> 30 s) to avoid false positives
                    now = time.monotonic()
                    recent_stops = {k: v for k, v in recent_stops.items() if now - v < 30}

                    if container_id in recent_stops:
                        recent_stops.pop(container_id)
                        log.info("Container %s (%s) was stopped manually.", name, short_id)
                        send_telegram(tg_token, tg_chat, (
                            f"⏹️ <b>Container stopped manually</b>\n"
                            f"Name: <code>{name}</code>\n"
                            f"ID: <code>{short_id}</code>\n"
                            f"Reason: {exit_reason}\n"
                            f"Time: {utc_now()}"
                        ))
                    else:
                        threading.Thread(
                            target=handle_crash,
                            args=(container_id, name, short_id, exit_reason,
                                  client, tg_token, tg_chat, max_attempts, restart_delay),
                            daemon=True,
                        ).start()
                    continue

                # --- start: container came up ---
                if action == "start":
                    if not is_monitored(name, monitored):
                        continue
                    log.info("Container %s (%s) is now running.", name, short_id)
                    send_telegram(tg_token, tg_chat, (
                        f"▶️ <b>Container started</b>\n"
                        f"Name: <code>{name}</code>\n"
                        f"ID: <code>{short_id}</code>\n"
                        f"Time: {utc_now()}"
                    ))

        except (docker.errors.DockerException, docker.errors.APIError) as e:
            log.error("Docker event stream error: %s — reconnecting in 5s", e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Docker container alert monitor")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file (default: config.yaml)"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    monitor(cfg)

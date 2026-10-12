from __future__ import annotations

import json
import html
import mimetypes
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

from komiic_downloader import (
    ALLOWED_BASE_URLS,
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    existing_image_path,
    format_reset_time,
    get_chapters,
    get_image_limit,
    QUOTA_EXCEEDED_EXIT_CODE,
    normalize_base_url,
    parse_comic_id,
    safe_name,
    stable_chapter_dir,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
LOCAL_PATHS_PATH = ROOT / ".komiic_local_paths.json"
FALLBACK_OUTPUT = "downloads"
CONFIG_PATH = ROOT / ".komiic_gui_config.json"
PLAYWRIGHT_PROFILE_DIR = ROOT / ".komiic_playwright_profile"
CONFIG_LOCK = threading.Lock()
COOKIE_REFRESH_LOCK = threading.Lock()
ACCOUNT_POOL_KEY = "shared"
COOKIE_REFRESH_MIN_INTERVAL = 300


def load_local_paths() -> dict:
    if not LOCAL_PATHS_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_PATHS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_LOCAL_PATHS = load_local_paths()
DEFAULT_OUTPUT = str(_LOCAL_PATHS.get("downloadDir") or FALLBACK_OUTPUT)
DEFAULT_CBZ_COPY_TARGET = str(_LOCAL_PATHS.get("cbzCopyTarget") or "")


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.status = "idle"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.command: list[str] = []
        self.logs: list[str] = []
        self.returncode: int | None = None
        self.output_dir = ""

    def snapshot(self) -> dict:
        with self.lock:
            running = self.status == "running" or (self.process is not None and self.process.poll() is None)
            return {
                "running": running,
                "status": self.status,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "command": self.command,
                "logs": self.logs[-1200:],
                "returncode": self.returncode,
                "outputDir": self.output_dir,
            }

    def append_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip())
            if len(self.logs) > 3000:
                self.logs = self.logs[-2000:]


JOB = JobState()


class BatchState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.status = "idle"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.output_dir = ""
        self.base_url = DEFAULT_BASE_URL
        self.urls: list[str] = []
        self.current_index = -1
        self.results: list[dict] = []
        self.logs: list[str] = []
        self.stop_requested = False

    def snapshot(self) -> dict:
        with self.lock:
            running = self.status == "running"
            return {
                "running": running,
                "status": self.status,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "outputDir": self.output_dir,
                "baseUrl": self.base_url,
                "urls": self.urls,
                "currentIndex": self.current_index,
                "results": self.results,
                "logs": self.logs[-1200:],
            }

    def append_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip())
            if len(self.logs) > 3000:
                self.logs = self.logs[-2000:]


BATCH = BatchState()


class CookieLoginState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.status = "idle"
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.base_url = ""
        self.cookie = ""
        self.cookie_count = 0
        self.profile_available = False
        self.error = ""

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "status": self.status,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "baseUrl": self.base_url,
                "cookie": self.cookie,
                "cookieCount": self.cookie_count,
                "profileAvailable": self.profile_available,
                "error": self.error,
            }


COOKIE_LOGIN = CookieLoginState()


def default_config() -> dict:
    return {
        "baseUrl": DEFAULT_BASE_URL,
        "cookies": {base_url: "" for base_url in ALLOWED_BASE_URLS},
        "accountPools": {ACCOUNT_POOL_KEY: []},
        "activeAccounts": {ACCOUNT_POOL_KEY: ""},
        "comics": [],
        "quotaHistory": [],
        "downloadHistory": [],
        "followComics": [],
        "cbzCopySince": 0,
    }


def normalize_comic_history_item(comic: dict, default_base_url: str) -> dict | None:
    try:
        base_url = normalize_base_url(str(comic.get("baseUrl") or default_base_url))
    except ValueError:
        base_url = default_base_url
    comic_id = str(comic.get("comicId") or "").strip()
    if not comic_id:
        comic_id = parse_comic_id(str(comic.get("url") or ""))
    title = str(comic.get("title") or "").strip()
    if title.startswith(("http://", "https://")) or "komiic" in title.lower():
        title = ""
    if not comic_id or not title:
        return None
    return {
        "title": safe_name(title),
        "baseUrl": base_url,
        "comicId": comic_id,
    }


def normalize_follow_comic(item: dict, default_base_url: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        base_url = normalize_base_url(str(item.get("baseUrl") or default_base_url))
    except ValueError:
        base_url = default_base_url
    comic_id = str(item.get("comicId") or "").strip()
    if not comic_id:
        return None
    return {"baseUrl": base_url, "comicId": comic_id}


def normalize_account_item(account: dict, fallback_index: int = 1) -> dict | None:
    cookie = str(account.get("cookie") or "").strip()
    if not cookie:
        return None
    account_id = str(account.get("id") or "").strip() or uuid.uuid4().hex
    name = str(account.get("name") or "").strip() or f"账号 {fallback_index}"
    return {
        "id": account_id,
        "name": name[:60],
        "cookie": cookie,
        "enabled": bool(account.get("enabled", True)),
        "lastRemaining": int(account.get("lastRemaining") or 0),
        "lastUsage": int(account.get("lastUsage") or 0),
        "lastLimit": int(account.get("lastLimit") or 0),
        "lastCheckedAt": float(account.get("lastCheckedAt") or 0),
        "lastCookieRefreshAt": float(account.get("lastCookieRefreshAt") or 0),
        "playwrightProfile": bool(account.get("playwrightProfile", False)),
        "lastResetInSeconds": account.get("lastResetInSeconds"),
        "lastResetText": str(account.get("lastResetText") or ""),
        "lastError": str(account.get("lastError") or ""),
    }


def account_pool_key(_: str = "") -> str:
    return ACCOUNT_POOL_KEY


def merged_account_pool(raw_pools: dict, raw_cookies: dict | None = None) -> list[dict]:
    accounts: list[dict] = []
    seen_ids: set[str] = set()
    sources = [ACCOUNT_POOL_KEY, *ALLOWED_BASE_URLS]
    for source in sources:
        for account in raw_pools.get(source) or []:
            if not isinstance(account, dict):
                continue
            item = normalize_account_item(account, len(accounts) + 1)
            if not item:
                continue
            if item["id"] in seen_ids:
                item["id"] = uuid.uuid4().hex
            seen_ids.add(item["id"])
            accounts.append(item)
    if raw_cookies:
        existing_cookies = {str(account.get("cookie") or "").strip() for account in accounts if str(account.get("cookie") or "").strip()}
        for base_url in ALLOWED_BASE_URLS:
            cookie = str(raw_cookies.get(base_url) or "").strip()
            if not cookie or cookie in existing_cookies:
                continue
            account_id = "default" if not accounts and "default" not in seen_ids else uuid.uuid4().hex
            seen_ids.add(account_id)
            accounts.append(
                {
                    "id": account_id,
                    "name": "默认账号",
                    "cookie": cookie,
                    "enabled": True,
                    "lastRemaining": 0,
                    "lastUsage": 0,
                    "lastLimit": 0,
                    "lastCheckedAt": 0,
                    "lastCookieRefreshAt": 0,
                    "playwrightProfile": False,
                    "lastResetInSeconds": None,
                    "lastResetText": "",
                    "lastError": "",
                }
            )
            existing_cookies.add(cookie)
    return accounts[:50]


def normalize_download_history_item(item: dict) -> dict | None:
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    return {
        "id": str(item.get("id") or "").strip() or uuid.uuid4().hex,
        "type": str(item.get("type") or "single"),
        "url": url,
        "title": str(item.get("title") or url).strip(),
        "baseUrl": str(item.get("baseUrl") or DEFAULT_BASE_URL),
        "status": str(item.get("status") or "queued"),
        "downloaded": int(item.get("downloaded") or 0),
        "missing": int(item.get("missing") or 0),
        "total": int(item.get("total") or 0),
        "message": str(item.get("message") or ""),
        "startedAt": float(item.get("startedAt") or time.time()),
        "updatedAt": float(item.get("updatedAt") or time.time()),
        "finishedAt": item.get("finishedAt"),
    }


def load_config() -> dict:
    config = default_config()
    with CONFIG_LOCK:
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                try:
                    config["baseUrl"] = normalize_base_url(str(loaded.get("baseUrl") or DEFAULT_BASE_URL))
                except ValueError:
                    config["baseUrl"] = DEFAULT_BASE_URL
                cookies = loaded.get("cookies")
                if isinstance(cookies, dict):
                    for base_url in ALLOWED_BASE_URLS:
                        config["cookies"][base_url] = str(cookies.get(base_url) or "")
                account_pools = loaded.get("accountPools")
                if isinstance(account_pools, dict):
                    config["accountPools"][ACCOUNT_POOL_KEY] = merged_account_pool(account_pools, config["cookies"])
                elif any(config["cookies"].get(base_url) for base_url in ALLOWED_BASE_URLS):
                    config["accountPools"][ACCOUNT_POOL_KEY] = merged_account_pool({}, config["cookies"])
                active_accounts = loaded.get("activeAccounts")
                account_ids = {account["id"] for account in config["accountPools"].get(ACCOUNT_POOL_KEY, [])}
                if isinstance(active_accounts, dict):
                    for key in (ACCOUNT_POOL_KEY, *ALLOWED_BASE_URLS):
                        active_id = str(active_accounts.get(key) or "").strip()
                        if active_id in account_ids:
                            config["activeAccounts"][ACCOUNT_POOL_KEY] = active_id
                            break
                if not config["activeAccounts"].get(ACCOUNT_POOL_KEY) and config["accountPools"].get(ACCOUNT_POOL_KEY):
                    config["activeAccounts"][ACCOUNT_POOL_KEY] = config["accountPools"][ACCOUNT_POOL_KEY][0]["id"]
                comics = loaded.get("comics")
                if isinstance(comics, list):
                    normalized_comics = []
                    for comic in comics:
                        if isinstance(comic, dict):
                            item = normalize_comic_history_item(comic, config["baseUrl"])
                            if item:
                                normalized_comics.append(item)
                    config["comics"] = normalized_comics[:100]
                quota_history = loaded.get("quotaHistory")
                if isinstance(quota_history, list):
                    config["quotaHistory"] = [
                        item
                        for item in quota_history
                        if isinstance(item, dict) and item.get("baseUrl") in ALLOWED_BASE_URLS
                    ][-500:]
                download_history = loaded.get("downloadHistory")
                if isinstance(download_history, list):
                    config["downloadHistory"] = [
                        item
                        for item in (normalize_download_history_item(entry) for entry in download_history if isinstance(entry, dict))
                        if item
                    ][-500:]
                follow_comics = loaded.get("followComics")
                if isinstance(follow_comics, list):
                    config["followComics"] = [
                        item
                        for item in (normalize_follow_comic(entry, config["baseUrl"]) for entry in follow_comics if isinstance(entry, dict))
                        if item
                    ][:200]
                config["cbzCopySince"] = float(loaded.get("cbzCopySince") or 0)
    return config


def save_config(config: dict) -> None:
    cleaned = default_config()
    try:
        cleaned["baseUrl"] = normalize_base_url(str(config.get("baseUrl") or DEFAULT_BASE_URL))
    except ValueError:
        cleaned["baseUrl"] = DEFAULT_BASE_URL
    cookies = config.get("cookies")
    if isinstance(cookies, dict):
        for base_url in ALLOWED_BASE_URLS:
            cleaned["cookies"][base_url] = str(cookies.get(base_url) or "")
    account_pools = config.get("accountPools")
    if isinstance(account_pools, dict):
        accounts = merged_account_pool(account_pools)
        cleaned["accountPools"][ACCOUNT_POOL_KEY] = accounts
        account_ids = {account["id"] for account in accounts}
        active_id = str((config.get("activeAccounts") or {}).get(ACCOUNT_POOL_KEY) or "").strip()
        if active_id not in account_ids:
            for key in ALLOWED_BASE_URLS:
                candidate = str((config.get("activeAccounts") or {}).get(key) or "").strip()
                if candidate in account_ids:
                    active_id = candidate
                    break
        if active_id in account_ids:
            cleaned["activeAccounts"][ACCOUNT_POOL_KEY] = active_id
        elif accounts:
            cleaned["activeAccounts"][ACCOUNT_POOL_KEY] = accounts[0]["id"]
        active = next((account for account in accounts if account["id"] == cleaned["activeAccounts"][ACCOUNT_POOL_KEY]), None)
        if active:
            for base_url in ALLOWED_BASE_URLS:
                cleaned["cookies"][base_url] = active["cookie"]
    comics = config.get("comics")
    if isinstance(comics, list):
        normalized_comics = []
        seen = set()
        for comic in comics:
            if not isinstance(comic, dict):
                continue
            item = normalize_comic_history_item(comic, cleaned["baseUrl"])
            if not item:
                continue
            key = (item["baseUrl"], item["comicId"])
            if key in seen:
                continue
            seen.add(key)
            normalized_comics.append(item)
        cleaned["comics"] = normalized_comics[:100]
    quota_history = config.get("quotaHistory")
    if isinstance(quota_history, list):
        cleaned["quotaHistory"] = [
            {
                "timestamp": float(item.get("timestamp") or time.time()),
                "baseUrl": normalize_base_url(str(item.get("baseUrl") or cleaned["baseUrl"])),
                "limit": int(item.get("limit") or 0),
                "usage": int(item.get("usage") or 0),
                "remaining": int(item.get("remaining") or 0),
                "resetInSeconds": item.get("resetInSeconds"),
                "resetText": str(item.get("resetText") or ""),
            }
            for item in quota_history
            if isinstance(item, dict)
        ][-500:]
    download_history = config.get("downloadHistory")
    if isinstance(download_history, list):
        cleaned["downloadHistory"] = [
            item
            for item in (normalize_download_history_item(entry) for entry in download_history if isinstance(entry, dict))
            if item
        ][:500]
    follow_comics = config.get("followComics")
    if isinstance(follow_comics, list):
        seen_follows = set()
        follows = []
        for item in (normalize_follow_comic(entry, cleaned["baseUrl"]) for entry in follow_comics if isinstance(entry, dict)):
            if not item:
                continue
            key = (item["baseUrl"], item["comicId"])
            if key in seen_follows:
                continue
            seen_follows.add(key)
            follows.append(item)
        cleaned["followComics"] = follows[:200]
    cleaned["cbzCopySince"] = float(config.get("cbzCopySince") or 0)

    with CONFIG_LOCK:
        CONFIG_PATH.write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def config_payload(base_url: str = "") -> dict:
    config = load_config()
    selected_base_url = normalize_base_url(base_url or str(config.get("baseUrl") or DEFAULT_BASE_URL))
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    active_id = str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    active = next((account for account in accounts if account.get("id") == active_id), None)
    return {
        "baseUrl": selected_base_url,
        "allowedBaseUrls": list(ALLOWED_BASE_URLS),
        "cookie": str((active or {}).get("cookie") or (config.get("cookies") or {}).get(selected_base_url) or ""),
        "accounts": account_pool_payload(selected_base_url)["accounts"],
        "activeAccountId": active_id,
        "comics": config.get("comics") or [],
    }


def saved_cookie(base_url: str) -> str:
    config = load_config()
    selected_base_url = normalize_base_url(base_url)
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    active_id = str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    active = next((account for account in accounts if account.get("id") == active_id and account.get("enabled", True)), None)
    return str((active or {}).get("cookie") or (config.get("cookies") or {}).get(selected_base_url) or "")


def remember_cookie(base_url: str, cookie: str) -> None:
    config = load_config()
    selected_base_url = normalize_base_url(base_url)
    cookie = cookie.strip()
    config.setdefault("cookies", {})[selected_base_url] = cookie
    accounts = config.setdefault("accountPools", {}).setdefault(ACCOUNT_POOL_KEY, [])
    if cookie and not accounts:
        account = {
            "id": "default",
            "name": "默认账号",
            "cookie": cookie,
            "enabled": True,
            "lastRemaining": 0,
            "lastUsage": 0,
            "lastLimit": 0,
            "lastCheckedAt": 0,
            "lastCookieRefreshAt": 0,
            "playwrightProfile": False,
            "lastResetInSeconds": None,
            "lastResetText": "",
            "lastError": "",
        }
        accounts.insert(0, account)
        config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = account["id"]
    config["baseUrl"] = selected_base_url
    save_config(config)


def account_pool_payload(base_url: str = "") -> dict:
    config = load_config()
    selected_base_url = normalize_base_url(base_url or str(config.get("baseUrl") or DEFAULT_BASE_URL))
    active_id = str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    accounts = []
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        cookie = str(account.get("cookie") or "")
        accounts.append(
            {
                "id": account.get("id"),
                "name": account.get("name"),
                "enabled": account.get("enabled", True),
                "active": account.get("id") == active_id,
                "hasCookie": bool(cookie),
                "cookiePreview": f"{cookie[:18]}..." if cookie else "",
                "lastRemaining": account.get("lastRemaining", 0),
                "lastUsage": account.get("lastUsage", 0),
                "lastLimit": account.get("lastLimit", 0),
                "lastCheckedAt": account.get("lastCheckedAt", 0),
                "lastCookieRefreshAt": account.get("lastCookieRefreshAt", 0),
                "playwrightProfile": account.get("playwrightProfile", False),
                "lastResetInSeconds": account.get("lastResetInSeconds"),
                "lastResetText": account.get("lastResetText", ""),
                "lastError": account.get("lastError", ""),
            }
        )
    return {"baseUrl": selected_base_url, "activeAccountId": active_id, "accounts": accounts}


def update_accounts(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, list):
        raise ValueError("账号列表格式错误")
    accounts = []
    for index, account in enumerate(raw_accounts, start=1):
        if not isinstance(account, dict):
            continue
        item = normalize_account_item(account, index)
        if item:
            accounts.append(item)
    config = load_config()
    config.setdefault("accountPools", {})[ACCOUNT_POOL_KEY] = accounts[:50]
    active_id = str(payload.get("activeAccountId") or "").strip()
    if active_id and any(account["id"] == active_id for account in accounts):
        config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = active_id
    elif accounts:
        config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = accounts[0]["id"]
    else:
        config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = ""
        config.setdefault("cookies", {})[selected_base_url] = ""
    if accounts:
        active = next((account for account in accounts if account["id"] == config["activeAccounts"][ACCOUNT_POOL_KEY]), accounts[0])
        config.setdefault("cookies", {})[selected_base_url] = active["cookie"]
    config["baseUrl"] = selected_base_url
    save_config(config)
    return account_pool_payload(selected_base_url)


def add_current_cookie_to_accounts(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    cookie = str(payload.get("cookie") or "").strip() or payload_cookie(payload, selected_base_url)
    if not cookie:
        raise ValueError("当前没有可添加的 Cookie")
    name = str(payload.get("name") or "").strip() or f"账号 {time.strftime('%H:%M:%S')}"
    force_new = bool(payload.get("forceNew"))
    config = load_config()
    accounts = config.setdefault("accountPools", {}).setdefault(ACCOUNT_POOL_KEY, [])
    existing = None if force_new else next((account for account in accounts if account.get("cookie") == cookie), None)
    if existing:
        existing["name"] = name or existing.get("name") or "账号"
        existing["enabled"] = True
        existing["lastCookieRefreshAt"] = time.time()
        existing["playwrightProfile"] = bool(payload.get("playwrightProfile"))
        account_id = existing["id"]
    else:
        account_id = uuid.uuid4().hex
        accounts.append(
            {
                "id": account_id,
                "name": name,
                "cookie": cookie,
                "enabled": True,
                "lastRemaining": 0,
                "lastUsage": 0,
                "lastLimit": 0,
                "lastCheckedAt": 0,
                "lastCookieRefreshAt": time.time(),
                "playwrightProfile": bool(payload.get("playwrightProfile")),
                "lastResetInSeconds": None,
                "lastResetText": "",
                "lastError": "",
            }
        )
    config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = account_id
    config.setdefault("cookies", {})[selected_base_url] = cookie
    config["baseUrl"] = selected_base_url
    save_config(config)
    return account_pool_payload(selected_base_url)


def select_account(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    config = load_config()
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    account = next((item for item in accounts if item.get("id") == account_id), None)
    if not account:
        raise ValueError("账号不存在")
    config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = account_id
    config.setdefault("cookies", {})[selected_base_url] = str(account.get("cookie") or "")
    config["baseUrl"] = selected_base_url
    save_config(config)
    return account_pool_payload(selected_base_url)


def delete_account(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    config = load_config()
    accounts = [
        account
        for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
        if account.get("id") != account_id
    ]
    config.setdefault("accountPools", {})[ACCOUNT_POOL_KEY] = accounts
    active_id = str(config.setdefault("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    if active_id == account_id:
        config["activeAccounts"][ACCOUNT_POOL_KEY] = accounts[0]["id"] if accounts else ""
        config.setdefault("cookies", {})[selected_base_url] = accounts[0]["cookie"] if accounts else ""
    save_config(config)
    return account_pool_payload(selected_base_url)


def toggle_account(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    enabled = bool(payload.get("enabled"))
    config = load_config()
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        if account.get("id") == account_id:
            account["enabled"] = enabled
            save_config(config)
            return account_pool_payload(selected_base_url)
    raise ValueError("账号不存在")


def rename_account(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("账号名称不能为空")
    config = load_config()
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        if account.get("id") == account_id:
            account["name"] = name[:60]
            save_config(config)
            return account_pool_payload(selected_base_url)
    raise ValueError("账号不存在")


def update_account_cookie(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        raise ValueError("Cookie 不能为空")
    config = load_config()
    active_id = str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        if account.get("id") == account_id:
            account["cookie"] = cookie
            account["lastRemaining"] = 0
            account["lastUsage"] = 0
            account["lastLimit"] = 0
            account["lastCheckedAt"] = 0
            account["lastCookieRefreshAt"] = time.time()
            account["playwrightProfile"] = bool(payload.get("playwrightProfile"))
            account["lastResetInSeconds"] = None
            account["lastResetText"] = ""
            account["lastError"] = ""
            if account_id == active_id:
                config.setdefault("cookies", {})[selected_base_url] = cookie
            save_config(config)
            return account_pool_payload(selected_base_url)
    raise ValueError("账号不存在")


def refresh_account_cookie(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    account_id = str(payload.get("accountId") or "").strip()
    if not account_id:
        raise ValueError("缺少账号 ID")
    config = load_config()
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    account = next((item for item in accounts if item.get("id") == account_id), None)
    if not account:
        raise ValueError("账号不存在")
    try:
        result = refresh_cookie_with_playwright(selected_base_url)
    except Exception as exc:
        account["lastError"] = str(exc)[:200]
        save_config(config)
        raise
    cookie = str(result.get("cookie") or "").strip()
    if not cookie:
        raise RuntimeError("没有获取到新的 Cookie，请重新浏览器登录")
    account["cookie"] = cookie
    account["lastCookieRefreshAt"] = time.time()
    account["playwrightProfile"] = True
    account["lastError"] = ""
    if str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "") == account_id:
        config.setdefault("cookies", {})[selected_base_url] = cookie
    config["baseUrl"] = selected_base_url
    save_config(config)
    return account_pool_payload(selected_base_url)


def remember_comic(base_url: str, url: str, title: str) -> None:
    comic_id = parse_comic_id(url)
    if not comic_id:
        return
    selected_base_url = normalize_base_url(base_url)
    config = load_config()
    comics = [
        comic
        for comic in config.get("comics", [])
        if not (
            str(comic.get("comicId") or "").strip() == comic_id
            and str(comic.get("baseUrl") or "").strip().rstrip("/") == selected_base_url
        )
    ]
    comics.insert(
        0,
        {
            "title": safe_name(title or f"comic_{comic_id}"),
            "baseUrl": selected_base_url,
            "comicId": comic_id,
        },
    )
    config["baseUrl"] = selected_base_url
    config["comics"] = comics[:100]
    save_config(config)


def delete_comic_history(base_url: str, comic_id: str) -> dict:
    selected_base_url = normalize_base_url(base_url)
    comic_id = str(comic_id or "").strip()
    if not comic_id:
        raise ValueError("缺少漫画 ID")
    config = load_config()
    config["comics"] = [
        comic
        for comic in config.get("comics", [])
        if not (
            str(comic.get("comicId") or "").strip() == comic_id
            and str(comic.get("baseUrl") or "").strip().rstrip("/") == selected_base_url
        )
    ]
    config["followComics"] = [
        item
        for item in config.get("followComics", [])
        if not (
            str(item.get("comicId") or "").strip() == comic_id
            and str(item.get("baseUrl") or "").strip().rstrip("/") == selected_base_url
        )
    ]
    save_config(config)
    return history_payload()


def toggle_follow_comic(payload: dict) -> dict:
    selected_base_url = payload_base_url(payload)
    comic_id = str(payload.get("comicId") or "").strip()
    followed = bool(payload.get("followed"))
    if not comic_id:
        raise ValueError("缺少漫画 ID")
    config = load_config()
    follows = [
        item
        for item in config.get("followComics", [])
        if not (
            str(item.get("comicId") or "").strip() == comic_id
            and str(item.get("baseUrl") or "").strip().rstrip("/") == selected_base_url
        )
    ]
    if followed:
        follows.insert(0, {"baseUrl": selected_base_url, "comicId": comic_id})
    config["followComics"] = follows[:200]
    save_config(config)
    return history_payload()


def remember_quota(base_url: str, limit_info: dict) -> dict:
    selected_base_url = normalize_base_url(base_url)
    limit = int(limit_info.get("limit") or 0)
    usage = int(limit_info.get("usage") or 0)
    remaining = max(limit - usage, 0)
    record = {
        "timestamp": time.time(),
        "baseUrl": selected_base_url,
        "limit": limit,
        "usage": usage,
        "remaining": remaining,
        "resetInSeconds": limit_info.get("resetInSeconds"),
        "resetText": format_reset_time(limit_info.get("resetInSeconds")),
    }
    config = load_config()
    history = [
        item
        for item in config.get("quotaHistory", [])
        if isinstance(item, dict) and item.get("baseUrl") in ALLOWED_BASE_URLS
    ]
    history.append(record)
    config["baseUrl"] = selected_base_url
    config["quotaHistory"] = history[-500:]
    save_config(config)
    return record


def quota_history_payload(base_url: str = "") -> dict:
    config = load_config()
    selected_base_url = normalize_base_url(base_url or str(config.get("baseUrl") or DEFAULT_BASE_URL))
    history = [
        item
        for item in config.get("quotaHistory", [])
        if item.get("baseUrl") == selected_base_url
    ]
    latest = history[-1] if history else None
    return {
        "baseUrl": selected_base_url,
        "latest": latest,
        "history": history[-240:],
    }


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def comic_preview_images(title: str) -> list[dict[str, str]]:
    comic_dir = safe_output_dir(DEFAULT_OUTPUT) / safe_name(title)
    if not comic_dir.exists() or not comic_dir.is_dir():
        return []

    previews: list[dict[str, str]] = []
    chapter_dirs = [
        path
        for path in sorted(comic_dir.iterdir())
        if path.is_dir()
        and not path.name.endswith("_panels_cbz")
        and path.name != "panels_cbz"
    ]
    for chapter_dir in chapter_dirs:
        for path in sorted(chapter_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            previews.append({"path": str(path), "href": "/file?path=" + quote(str(path))})
            if len(previews) >= 3:
                return previews
    return previews


def history_payload() -> dict:
    config = load_config()
    followed_keys = {
        (str(item.get("baseUrl") or "").rstrip("/"), str(item.get("comicId") or ""))
        for item in config.get("followComics", [])
    }
    comics = []
    for comic in config.get("comics") or []:
        title = str(comic.get("title") or "").strip()
        base_url = str(comic.get("baseUrl") or DEFAULT_BASE_URL)
        comic_id = str(comic.get("comicId") or "").strip()
        if not title or not comic_id:
            continue
        comics.append(
            {
                "title": title,
                "baseUrl": base_url,
                "comicId": comic_id,
                "url": f"{base_url}/comic/{comic_id}",
                "followed": (base_url.rstrip("/"), comic_id) in followed_keys,
                "previewImages": comic_preview_images(title),
            }
        )
    downloads = sorted(
        config.get("downloadHistory") or [],
        key=lambda item: float(item.get("updatedAt") or item.get("startedAt") or 0),
        reverse=True,
    )[:200]
    return {"comics": comics, "downloads": downloads}


def upsert_download_history(entry: dict) -> dict:
    item = normalize_download_history_item(entry)
    if not item:
        raise ValueError("下载历史缺少链接")
    config = load_config()
    history = [
        existing
        for existing in config.get("downloadHistory", [])
        if str(existing.get("id") or "") != item["id"]
    ]
    history.insert(0, item)
    config["downloadHistory"] = history[:500]
    save_config(config)
    return item


def update_download_history(history_id: str, **updates) -> dict | None:
    if not history_id:
        return None
    config = load_config()
    history = config.get("downloadHistory", [])
    updated_item = None
    for item in history:
        if str(item.get("id") or "") == history_id:
            item.update(updates)
            item["updatedAt"] = time.time()
            if item.get("status") in {"done", "failed", "stopped", "missing", "retried"} and not item.get("finishedAt"):
                item["finishedAt"] = time.time()
            updated_item = normalize_download_history_item(item)
            if updated_item:
                item.clear()
                item.update(updated_item)
            break
    config["downloadHistory"] = history[:500]
    save_config(config)
    return updated_item


def create_download_history(
    *,
    url: str,
    base_url: str,
    task_type: str,
    title: str = "",
    status: str = "queued",
    total: int = 0,
    downloaded: int = 0,
    missing: int = 0,
    message: str = "",
) -> str:
    history_id = uuid.uuid4().hex
    upsert_download_history(
        {
            "id": history_id,
            "type": task_type,
            "url": url,
            "title": title or url,
            "baseUrl": base_url,
            "status": status,
            "total": total,
            "downloaded": downloaded,
            "missing": missing,
            "message": message,
            "startedAt": time.time(),
            "updatedAt": time.time(),
        }
    )
    return history_id


def poll_quota_once(base_url: str = "") -> dict | None:
    config = load_config()
    selected_base_url = normalize_base_url(base_url or str(config.get("baseUrl") or DEFAULT_BASE_URL))
    limit_info = get_account_pool_limit_info(selected_base_url)
    if not limit_info:
        return None
    return remember_quota(selected_base_url, limit_info)


def quota_monitor_loop() -> None:
    while True:
        try:
            poll_quota_once()
        except Exception:
            pass
        time.sleep(60)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Komiic 漫画下载器</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --text: #202327;
      --muted: #69707a;
      --line: #d9dee5;
      --accent: #176b87;
      --accent-strong: #0f5268;
      --danger: #b42318;
      --success: #177245;
      --shadow: 0 8px 24px rgba(27, 39, 51, 0.08);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }

    button, input, select {
      font: inherit;
    }

    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      height: 58px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 22px;
    }

    .header-left {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }

    .nav-tabs {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .nav-tabs button {
      min-height: 32px;
      padding: 5px 10px;
      font-size: 13px;
      background: #f7f9fa;
    }

    .nav-tabs button.active {
      color: #ffffff;
      background: var(--accent);
      border-color: var(--accent);
    }

    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
    }

    .status {
      min-width: 96px;
      text-align: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 10px;
      color: var(--muted);
      background: #fafafa;
      font-size: 13px;
    }

    .status.running {
      color: var(--accent-strong);
      border-color: #9cc7d5;
      background: #eff8fb;
    }

    .status.done {
      color: var(--success);
      border-color: #9bd6b8;
      background: #effaf4;
    }

    .status.failed {
      color: var(--danger);
      border-color: #efb0aa;
      background: #fff4f2;
    }

    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
      max-width: 1380px;
      width: 100%;
      margin: 0 auto;
      height: calc(100vh - 58px);
      min-height: 0;
      overflow: hidden;
    }

    main.hidden {
      display: none;
    }

    .quota-view {
      grid-template-columns: minmax(0, 1fr);
    }

    .batch-view,
    .account-view,
    .history-view {
      grid-template-columns: minmax(0, 1fr);
    }

    .batch-panel,
    .account-panel,
    .quota-panel,
    .history-panel {
      padding: 18px;
      overflow: auto;
      max-height: calc(100vh - 94px);
    }

    .batch-layout {
      display: grid;
      grid-template-columns: minmax(320px, 440px) minmax(0, 1fr);
      gap: 16px;
      min-height: 0;
    }

    .batch-form {
      display: grid;
      gap: 12px;
      align-self: start;
    }

    .batch-links {
      min-height: 260px;
      font-family: "SFMono-Regular", Consolas, monospace;
    }

    .append-links {
      min-height: 110px;
      font-family: "SFMono-Regular", Consolas, monospace;
    }

    .append-field {
      border-top: 1px solid var(--line);
      padding-top: 12px;
      margin-top: 2px;
    }

    .batch-results {
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
      max-height: 318px;
      overflow: auto;
    }

    .batch-result {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 5px;
      background: #fbfcfd;
      font-size: 13px;
    }

    .batch-result strong {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .batch-result.done,
    .batch-result.retried {
      border-color: #b8dec8;
      background: #f4fbf7;
    }

    .batch-result.failed,
    .batch-result.missing {
      border-color: #efb0aa;
      background: #fff8f7;
    }

    .quota-inline {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      padding: 10px 12px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin: 8px 0 14px;
      font-size: 13px;
    }

    .quota-inline strong {
      font-size: 15px;
    }

    .quota-cards {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .quota-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }

    .quota-card strong {
      display: block;
      font-size: 24px;
      margin-top: 6px;
    }

    .quota-chart {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      min-height: 220px;
      margin-bottom: 16px;
      padding: 12px;
      overflow: hidden;
    }

    .quota-chart svg {
      display: block;
      width: 100%;
      height: 220px;
    }

    .quota-chart .axis {
      stroke: #c8d0d8;
      stroke-width: 1;
    }

    .quota-chart .usage-line {
      fill: none;
      stroke: var(--accent);
      stroke-width: 2.5;
    }

    .quota-chart .remaining-area {
      fill: rgba(23, 107, 135, 0.12);
    }

    .quota-chart .empty {
      color: var(--muted);
      font-size: 13px;
      padding: 14px;
    }

    .quota-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    .quota-table th,
    .quota-table td {
      border-bottom: 1px solid var(--line);
      padding: 9px 8px;
      text-align: left;
      white-space: nowrap;
    }

    .quota-table th {
      color: var(--muted);
      font-weight: 650;
      background: #fbfcfd;
      position: sticky;
      top: 0;
    }

    .history-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 12px;
    }

    .history-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      overflow: hidden;
      display: grid;
      gap: 0;
    }

    .history-preview {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1px;
      background: var(--line);
      min-height: 128px;
    }

    .history-preview img,
    .history-empty-thumb {
      width: 100%;
      aspect-ratio: 3 / 4;
      object-fit: cover;
      display: block;
      background: #eef1f4;
    }

    .history-empty-thumb {
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: 12px;
    }

    .history-body {
      padding: 12px;
      display: grid;
      gap: 9px;
    }

    .history-title {
      font-weight: 650;
      line-height: 1.35;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }

    .history-title-row {
      display: grid;
      grid-template-columns: 30px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
    }

    .follow-star {
      width: 28px;
      height: 28px;
      display: inline-grid;
      place-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--muted);
      font-size: 17px;
      line-height: 1;
      padding: 0;
    }

    .follow-star.active {
      color: #a97800;
      border-color: #e5c15c;
      background: #fff8dc;
    }

    .history-actions {
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }

    .directory-picker {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      margin: -2px 0 14px;
      overflow: hidden;
    }

    .directory-picker.hidden {
      display: none;
    }

    .directory-toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 10px;
      border-bottom: 1px solid var(--line);
    }

    .directory-current {
      padding: 8px 10px;
      color: var(--muted);
      font-size: 12px;
      border-bottom: 1px solid var(--line);
      word-break: break-all;
    }

    .directory-list {
      max-height: 220px;
      overflow: auto;
      display: grid;
    }

    .directory-item {
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      text-align: left;
      background: #ffffff;
    }

    .directory-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      padding: 10px;
    }

    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }

    .form-panel {
      padding: 18px;
      align-self: start;
      max-height: calc(100vh - 94px);
      overflow: auto;
    }

    .field {
      display: grid;
      gap: 7px;
      margin-bottom: 14px;
    }

    .input-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
    }

    .checkbox-row {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
      font-weight: 500;
      cursor: pointer;
      width: fit-content;
    }

    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    label {
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }

    input[type="text"],
    input[type="number"],
    select,
    textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      background: #ffffff;
      color: var(--text);
      outline: none;
    }

    input[type="text"],
    input[type="number"],
    select {
      min-height: 38px;
    }

    textarea {
      min-height: 86px;
      resize: vertical;
      line-height: 1.45;
    }

    input:focus,
    select:focus,
    textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.14);
    }

    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 8px 0 16px;
      color: var(--text);
      font-size: 14px;
    }

    .site-switch {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 14px;
    }

    .site-option {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px 10px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
      cursor: pointer;
      background: #fbfcfd;
      min-width: 0;
      font-size: 13px;
    }

    .site-option:has(input:checked) {
      border-color: var(--accent);
      background: #eff8fb;
      color: var(--accent-strong);
      font-weight: 650;
    }

    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 6px;
    }

    button {
      min-height: 38px;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 8px 13px;
      cursor: pointer;
      background: #ffffff;
      color: var(--text);
      border-color: var(--line);
    }

    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
      font-weight: 650;
    }

    button.primary:hover {
      background: var(--accent-strong);
      border-color: var(--accent-strong);
    }

    button.danger {
      color: var(--danger);
      border-color: #efb0aa;
      background: #fff8f7;
    }

    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .log-panel {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      height: calc(100vh - 94px);
      min-height: 0;
    }

    .toolbar {
      min-height: 54px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 14px;
    }

    .toolbar strong {
      font-size: 14px;
    }

    .toolbar-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .meta {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 55vw;
    }

    pre {
      margin: 0;
      padding: 14px;
      overflow: auto;
      min-height: 0;
      background: #111418;
      color: #edf2f7;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .files {
      display: grid;
      gap: 0;
      height: 100%;
      min-height: 0;
      overflow: auto;
      background: #fbfcfd;
    }

    .files-header {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 9px 14px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }

    .files-title {
      display: flex;
      gap: 8px;
      align-items: baseline;
      min-width: 0;
      font-size: 13px;
      font-weight: 650;
    }

    .files-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .files-actions button {
      min-height: 30px;
      padding: 5px 10px;
      font-size: 12px;
    }

    .files-actions button.active {
      color: #ffffff;
      background: var(--accent);
      border-color: var(--accent);
    }

    .file-list {
      display: grid;
    }

    .file-tree-node {
      display: grid;
      gap: 0;
    }

    .file-tree-row {
      display: grid;
      grid-template-columns: 24px 86px minmax(0, 1fr);
      gap: 10px;
      align-items: center;
      padding: 8px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      min-width: 0;
    }

    .file-toggle {
      width: 24px;
      min-height: 24px;
      padding: 0;
      border-radius: 6px;
    }

    .file-kind {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .file-empty {
      color: var(--muted);
      padding: 12px 14px;
      font-size: 13px;
    }

    .files a {
      color: var(--accent-strong);
      text-decoration: none;
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .file-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .files a:hover { text-decoration: underline; }

    .files-view {
      grid-template-columns: minmax(0, 1fr);
    }

    .files-panel {
      overflow: hidden;
      max-height: calc(100vh - 94px);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }

    .cookie-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }

    .account-pool {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      margin: -4px 0 14px;
      overflow: hidden;
    }

    .account-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }

    .account-row:last-child { border-bottom: 0; }

    .account-row.ok {
      border-left: 4px solid #5fbf83;
      background: #f1fbf5;
    }

    .account-row.problem {
      border-left: 4px solid #df7a72;
      background: #fff5f4;
    }

    .account-name {
      font-weight: 650;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .account-actions {
      display: flex;
      gap: 6px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .account-actions button {
      min-height: 30px;
      padding: 5px 9px;
      font-size: 12px;
    }

    .history-section-title {
      margin: 12px 0 10px;
      font-size: 14px;
    }

    .download-history {
      display: grid;
      gap: 8px;
      margin-bottom: 18px;
    }

    .download-history-row {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      padding: 10px;
      display: grid;
      gap: 5px;
      font-size: 13px;
    }

    .download-history-row.running {
      border-color: #9cc7d5;
      background: #eff8fb;
    }

    .download-history-row.done,
    .download-history-row.retried {
      border-color: #b8dec8;
      background: #f4fbf7;
    }

    .download-history-row.failed,
    .download-history-row.missing,
    .download-history-row.stopped {
      border-color: #efb0aa;
      background: #fff8f7;
    }

    .chapter-tools {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 8px 0 10px;
    }

    .chapter-list {
      border: 1px solid var(--line);
      border-radius: 7px;
      max-height: 260px;
      overflow: auto;
      background: #fbfcfd;
      margin-bottom: 14px;
    }

    .chapter-item {
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }

    .chapter-item:last-child { border-bottom: 0; }

    .chapter-item.downloaded {
      color: #9aa3ad;
      background: #f3f5f7;
    }

    .chapter-item.downloaded span:last-child {
      color: #9aa3ad;
    }

    .chapter-item span:last-child {
      color: var(--muted);
      font-size: 12px;
    }

    .selection-summary {
      color: var(--muted);
      font-size: 13px;
      margin: -6px 0 14px;
      min-height: 18px;
    }

    @media (max-width: 860px) {
      header { padding: 0 14px; }
      main {
        grid-template-columns: 1fr;
        padding: 12px;
        height: auto;
        overflow: visible;
      }
      .form-panel { max-height: none; }
      .log-panel { height: 520px; }
      .grid-2 { grid-template-columns: 1fr; }
      .site-switch { grid-template-columns: 1fr; }
      .quota-cards { grid-template-columns: 1fr 1fr; }
      .history-list { grid-template-columns: 1fr; }
      .meta { max-width: 64vw; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="header-left">
        <h1>Komiic 漫画下载器</h1>
        <div class="nav-tabs">
          <button id="downloadTab" class="active" type="button">下载</button>
          <button id="batchTab" type="button">批量下载</button>
          <button id="accountTab" type="button">账号池</button>
          <button id="downloadHistoryTab" type="button">下载任务</button>
          <button id="historyTab" type="button">历史记录</button>
          <button id="quotaTab" type="button">额度记录</button>
        </div>
      </div>
      <div id="status" class="status">空闲</div>
    </header>
    <main id="downloadView">
      <section class="form-panel">
        <form id="downloadForm">
          <div class="field">
            <label>站点</label>
            <div class="site-switch">
              <label class="site-option">
                <input type="radio" name="baseUrl" value="https://komiic.cc">
                <span>国内直连 .cc</span>
              </label>
              <label class="site-option">
                <input type="radio" name="baseUrl" value="https://komiic.com" checked>
                <span>海外 .com</span>
              </label>
            </div>
          </div>
          <div class="field">
            <label for="url">漫画或章节链接</label>
            <input id="url" name="url" type="text" value="" required>
          </div>
          <div class="field">
            <label for="output">输出目录</label>
            <div class="input-row">
              <input id="output" name="output" type="text" value="__DEFAULT_OUTPUT_VALUE__">
              <button id="chooseOutputButton" type="button">选择目录</button>
              <button id="copyCbzButton" type="button">复制 CBZ</button>
            </div>
            <label class="checkbox-row">
              <input id="autoCopyCbz" type="checkbox">
              <span>下载完成后自动复制新增 CBZ</span>
            </label>
            <div class="hint">频繁更改输出目录会导致程序读不到旧目录，可能把已下载章节判断为未下载并重复下载。</div>
          </div>
          <div class="quota-inline">
            <strong id="downloadQuotaText">账号池额度: -</strong>
            <span id="downloadQuotaCountdown" class="meta">下次刷新: 60 秒</span>
          </div>
          <div class="field">
            <label>章节选择</label>
            <div class="chapter-tools">
              <button id="fetchChaptersButton" type="button">获取章节</button>
              <button id="toggleDownloadedButton" type="button" disabled>显示已下载章节</button>
              <button id="selectAllButton" type="button">全选</button>
              <button id="clearSelectionButton" type="button">清空</button>
            </div>
            <div id="chapterList" class="chapter-list">
              <div class="chapter-item"><span></span><span class="meta">尚未获取章节</span><span></span></div>
            </div>
            <div id="selectionSummary" class="selection-summary"></div>
          </div>
          <div class="actions">
            <button id="startButton" class="primary" type="submit">开始下载</button>
            <button id="stopButton" class="danger" type="button" disabled>停止</button>
          </div>
        </form>
      </section>

      <section class="log-panel">
        <div class="toolbar">
          <strong>运行日志</strong>
          <div class="toolbar-actions">
            <div id="command" class="meta"></div>
          </div>
        </div>
        <pre id="logs">等待任务...</pre>
      </section>
    </main>
    <main id="batchView" class="batch-view hidden">
      <section class="batch-panel">
        <div class="toolbar">
          <strong>批量下载</strong>
          <div id="batchCommand" class="meta"></div>
        </div>
        <div class="batch-layout">
          <div class="batch-form">
            <div class="field">
              <label for="batchUrls">漫画链接（每行一个）</label>
              <textarea id="batchUrls" class="batch-links" spellcheck="false" placeholder="https://komiic.com/comic/1088"></textarea>
            </div>
            <div class="actions">
              <button id="startBatchButton" class="primary" type="button">开始批量下载</button>
              <button id="estimateBatchButton" type="button">获取章节并估算额度</button>
              <button id="stopBatchButton" class="danger" type="button" disabled>停止</button>
            </div>
            <label class="checkbox-row">
              <input id="batchAutoCopyCbz" type="checkbox" checked>
              <span>批量完成后自动复制新增 CBZ</span>
            </label>
            <div class="field append-field">
              <label for="appendBatchUrls">追加漫画链接（批量运行中可用）</label>
              <textarea id="appendBatchUrls" class="append-links" spellcheck="false" placeholder="https://komiic.com/comic/1088" disabled></textarea>
              <div class="actions">
                <button id="appendBatchButton" type="button" disabled>追加到队列</button>
              </div>
            </div>
            <div id="batchSummary" class="hint">等待任务...</div>
            <div id="batchResults" class="batch-results"></div>
          </div>
          <section class="log-panel">
            <div class="toolbar">
              <strong>运行日志</strong>
            </div>
            <pre id="batchLogs">等待任务...</pre>
          </section>
        </div>
      </section>
    </main>
    <main id="accountView" class="account-view hidden">
      <section class="account-panel">
        <div class="toolbar">
          <strong>账号池</strong>
        </div>
        <div class="field">
          <label for="cookie">登录 Cookie</label>
          <textarea id="cookie" spellcheck="false" placeholder="只粘贴 Cookie 请求头的值，不要粘贴账号密码"></textarea>
          <div class="cookie-actions">
            <button id="fetchCookieButton" type="button">浏览器登录获取 Cookie</button>
            <button id="addAccountButton" type="button">加入当前 Cookie</button>
            <span id="cookieStatus" class="hint">会打开独立登录窗口，登录后关闭窗口即可命名并新增账号。</span>
          </div>
        </div>
        <div class="field">
          <label>账号列表</label>
          <div id="accountPool" class="account-pool">
            <div class="account-row"><span class="meta">暂无账号</span></div>
          </div>
          <div class="hint">未手动输入 Cookie 时，下载会从当前账号开始；额度用完后自动切换到下一个启用账号。</div>
        </div>
      </section>
    </main>
    <main id="filesView" class="files-view hidden">
      <section class="files-panel">
        <div class="toolbar">
          <strong>输出文件</strong>
          <div class="toolbar-actions">
            <button id="refreshFilesButton" type="button">刷新</button>
            <button id="showAllFilesButton" class="active" type="button">全部</button>
            <button id="showCbzFilesButton" type="button">只看 CBZ</button>
          </div>
        </div>
        <div id="filesPanel" class="files">
          <div class="files-header">
            <div class="files-title">
              <span id="filesSummary" class="meta">任务完成后显示</span>
            </div>
          </div>
          <div id="filesList" class="file-list">
            <div class="file-empty">暂无输出文件</div>
          </div>
        </div>
      </section>
    </main>
    <main id="quotaView" class="quota-view hidden">
      <section class="quota-panel">
        <div class="toolbar">
          <strong>额度记录</strong>
          <div class="chapter-tools">
            <span id="quotaCountdown" class="meta">下次自动刷新: 60 秒</span>
            <button id="refreshQuotaHistoryButton" type="button">立即刷新</button>
            <button id="toggleUnchangedQuotaButton" type="button">展开无变化</button>
          </div>
        </div>
        <div class="quota-cards">
          <div class="quota-card"><span class="meta">剩余额度</span><strong id="quotaRemaining">-</strong></div>
          <div class="quota-card"><span class="meta">已用额度</span><strong id="quotaUsage">-</strong></div>
          <div class="quota-card"><span class="meta">总额度</span><strong id="quotaLimit">-</strong></div>
          <div class="quota-card"><span class="meta">最近重置时间</span><strong id="quotaReset">-</strong></div>
        </div>
        <div id="quotaChart" class="quota-chart">
          <div class="empty">暂无额度记录</div>
        </div>
        <table class="quota-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>站点</th>
              <th>剩余</th>
              <th>已用</th>
              <th>总额</th>
              <th>变化</th>
            </tr>
          </thead>
          <tbody id="quotaHistoryRows">
            <tr><td colspan="6" class="meta">暂无记录</td></tr>
          </tbody>
        </table>
      </section>
    </main>
    <main id="downloadHistoryView" class="history-view hidden">
      <section class="history-panel">
        <div class="toolbar">
          <strong>下载任务</strong>
          <button id="refreshHistoryButton" type="button">刷新</button>
        </div>
        <div id="downloadHistoryList" class="download-history">
          <div class="meta">暂无下载任务</div>
        </div>
      </section>
    </main>
    <main id="historyView" class="history-view hidden">
      <section class="history-panel">
        <div class="toolbar">
          <strong>历史记录</strong>
          <div class="chapter-tools">
            <button id="showFollowButton" type="button">只看追更</button>
            <select id="historyLimitSelect">
              <option value="10">最近 10 个</option>
              <option value="30">最近 30 个</option>
              <option value="50">最近 50 个</option>
            </select>
          </div>
        </div>
        <h2 class="history-section-title">漫画</h2>
        <div id="historyList" class="history-list">
          <div class="meta">暂无历史记录</div>
        </div>
      </section>
    </main>
  </div>
  <script>
    const defaultOutputDir = __DEFAULT_OUTPUT_JSON__;
    const form = document.getElementById("downloadForm");
    const downloadTab = document.getElementById("downloadTab");
    const batchTab = document.getElementById("batchTab");
    const accountTab = document.getElementById("accountTab");
    const downloadHistoryTab = document.getElementById("downloadHistoryTab");
    const historyTab = document.getElementById("historyTab");
    const quotaTab = document.getElementById("quotaTab");
    const downloadView = document.getElementById("downloadView");
    const batchView = document.getElementById("batchView");
    const accountView = document.getElementById("accountView");
    const downloadHistoryView = document.getElementById("downloadHistoryView");
    const filesView = document.getElementById("filesView");
    const historyView = document.getElementById("historyView");
    const quotaView = document.getElementById("quotaView");
    const logsEl = document.getElementById("logs");
    const statusEl = document.getElementById("status");
    const commandEl = document.getElementById("command");
    const startButton = document.getElementById("startButton");
    const stopButton = document.getElementById("stopButton");
    const fetchChaptersButton = document.getElementById("fetchChaptersButton");
    const toggleDownloadedButton = document.getElementById("toggleDownloadedButton");
    const selectAllButton = document.getElementById("selectAllButton");
    const clearSelectionButton = document.getElementById("clearSelectionButton");
    const downloadQuotaTextEl = document.getElementById("downloadQuotaText");
    const downloadQuotaCountdownEl = document.getElementById("downloadQuotaCountdown");
    const chapterListEl = document.getElementById("chapterList");
    const selectionSummaryEl = document.getElementById("selectionSummary");
    const refreshQuotaHistoryButton = document.getElementById("refreshQuotaHistoryButton");
    const quotaRemainingEl = document.getElementById("quotaRemaining");
    const quotaUsageEl = document.getElementById("quotaUsage");
    const quotaLimitEl = document.getElementById("quotaLimit");
    const quotaResetEl = document.getElementById("quotaReset");
    const quotaHistoryRowsEl = document.getElementById("quotaHistoryRows");
    const quotaCountdownEl = document.getElementById("quotaCountdown");
    const quotaChartEl = document.getElementById("quotaChart");
    const toggleUnchangedQuotaButton = document.getElementById("toggleUnchangedQuotaButton");
    const refreshHistoryButton = document.getElementById("refreshHistoryButton");
    const historyListEl = document.getElementById("historyList");
    const downloadHistoryListEl = document.getElementById("downloadHistoryList");
    const historyLimitSelect = document.getElementById("historyLimitSelect");
    const showFollowButton = document.getElementById("showFollowButton");
    const chooseOutputButton = document.getElementById("chooseOutputButton");
    const copyCbzButton = document.getElementById("copyCbzButton");
    const autoCopyCbzEl = document.getElementById("autoCopyCbz");
    const cookieInputEl = document.getElementById("cookie");
    const fetchCookieButton = document.getElementById("fetchCookieButton");
    const addAccountButton = document.getElementById("addAccountButton");
    const cookieStatusEl = document.getElementById("cookieStatus");
    const accountPoolEl = document.getElementById("accountPool");
    const filesSummaryEl = document.getElementById("filesSummary");
    const filesListEl = document.getElementById("filesList");
    const refreshFilesButton = document.getElementById("refreshFilesButton");
    const showAllFilesButton = document.getElementById("showAllFilesButton");
    const showCbzFilesButton = document.getElementById("showCbzFilesButton");
    const batchUrlsEl = document.getElementById("batchUrls");
    const appendBatchUrlsEl = document.getElementById("appendBatchUrls");
    const batchAutoCopyCbzEl = document.getElementById("batchAutoCopyCbz");
    const startBatchButton = document.getElementById("startBatchButton");
    const estimateBatchButton = document.getElementById("estimateBatchButton");
    const appendBatchButton = document.getElementById("appendBatchButton");
    const stopBatchButton = document.getElementById("stopBatchButton");
    const batchSummaryEl = document.getElementById("batchSummary");
    const batchResultsEl = document.getElementById("batchResults");
    const batchLogsEl = document.getElementById("batchLogs");
    const batchCommandEl = document.getElementById("batchCommand");
    let loadedChapters = [];
    let showDownloadedChapters = false;
    let recentComics = [];
    let historyComics = [];
    let showFollowOnly = false;
    let currentFileTree = null;
    let currentFileSummary = { chapters: 0, cbz: 0 };
    let filesMode = "all";
    let lastLoadedOutputDir = "";
    let lastObservedRunning = false;
    let lastObservedBatchRunning = false;
    let batchRunning = false;
    let activeView = "download";
    let quotaCountdown = 60;
    let showUnchangedQuotaRows = false;
    let cookieLoginPollTimer = null;
    let handledCookieLoginFinishedAt = 0;
    let cookieLoginStartedInPage = false;
    let batchEstimateVisible = false;

    function selectedBaseUrl() {
      return form.querySelector("input[name='baseUrl']:checked")?.value || "https://komiic.com";
    }

    function setBaseUrl(value) {
      const input = form.querySelector(`input[name='baseUrl'][value="${value}"]`);
      if (input) {
        input.checked = true;
      }
    }

    function syncUrlHost(baseUrl) {
      form.url.value = form.url.value
        .replace("https://komiic.com", baseUrl)
        .replace("https://komiic.cc", baseUrl);
    }

    function populateRecentComics(comics) {
      recentComics = Array.isArray(comics) ? comics : [];
    }

    function renderAccounts(accounts) {
      const rows = Array.isArray(accounts) ? accounts : [];
      if (!rows.length) {
        accountPoolEl.innerHTML = "<div class=\"account-row\"><span class=\"meta\">暂无账号，先粘贴或浏览器登录获取 Cookie 后加入账号池。</span></div>";
        downloadQuotaTextEl.textContent = "账号池额度: 暂无账号";
        return;
      }
      const totals = rows.filter((account) => account.enabled).reduce((sum, account) => {
        sum.remaining += Number(account.lastRemaining || 0);
        sum.limit += Number(account.lastLimit || 0);
        return sum;
      }, { remaining: 0, limit: 0 });
      if (totals.limit > 0) {
        downloadQuotaTextEl.textContent = `账号池额度: 剩余 ${totals.remaining}/${totals.limit}`;
      }
      accountPoolEl.innerHTML = rows.map((account) => {
        const quota = account.lastLimit
          ? `剩余 ${account.lastRemaining}/${account.lastLimit}`
          : "尚未查询额度";
        const reset = account.lastResetText ? `下次重置 ${account.lastResetText}` : "重置时间未知";
        const active = account.active ? " · 当前" : "";
        const source = account.playwrightProfile ? " · 浏览器自动刷新" : "";
        const statusClass = account.lastError || !account.hasCookie
          ? "problem"
          : (account.enabled && account.lastLimit ? "ok" : "");
        const statusText = account.lastError ? ` · ${account.lastError}` : "";
        return `
          <div class="account-row ${statusClass}">
            <div>
              <div class="account-name">${escapeHtml(account.name || "账号")}${active}</div>
              <div class="meta">${escapeHtml(quota)} · ${escapeHtml(reset)}${escapeHtml(source)}${escapeHtml(statusText)}</div>
            </div>
            <div class="account-actions">
              <button type="button" data-account-select="${escapeHtml(account.id)}" ${account.active ? "disabled" : ""}>设为当前</button>
              <button type="button" data-account-rename="${escapeHtml(account.id)}" data-account-name="${escapeHtml(account.name || "账号")}">重命名</button>
              <button type="button" data-account-refresh="${escapeHtml(account.id)}">刷新 Cookie</button>
              <button type="button" data-account-cookie="${escapeHtml(account.id)}">更新 Cookie</button>
              <button type="button" data-account-toggle="${escapeHtml(account.id)}" data-enabled="${account.enabled ? "0" : "1"}">${account.enabled ? "停用" : "启用"}</button>
              <button class="danger" type="button" data-account-delete="${escapeHtml(account.id)}">删除</button>
            </div>
          </div>
        `;
      }).join("");
    }

    async function refreshAccounts() {
      const res = await fetch(`/api/accounts?baseUrl=${encodeURIComponent(selectedBaseUrl())}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "读取账号池失败");
      }
      renderAccounts(data.accounts || []);
      return data;
    }

    async function postAccountAction(path, body) {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ baseUrl: selectedBaseUrl(), ...body })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "账号池操作失败");
      }
      renderAccounts(data.accounts || []);
      await loadConfig(selectedBaseUrl());
      return data;
    }

    function formPayload() {
      const selectedChapterIds = Array.from(chapterListEl.querySelectorAll("input[type='checkbox']:checked"))
        .map((input) => input.value);
      return {
        baseUrl: selectedBaseUrl(),
        url: form.url.value.trim(),
        output: form.output.value.trim() || defaultOutputDir,
        cookie: cookieInputEl.value.trim(),
        maxChapters: 0,
        downloadAll: true,
        selectedChapterIds,
        imageDelay: 1,
        chapterDelay: 1,
        singleChapter: false,
        autoCopyCbz: autoCopyCbzEl.checked
      };
    }

    function switchView(view) {
      activeView = view;
      const downloadActive = view === "download";
      const batchActive = view === "batch";
      const accountActive = view === "account";
      const downloadHistoryActive = view === "downloadHistory";
      const historyActive = view === "history";
      const quotaActive = view === "quota";
      downloadView.classList.toggle("hidden", !downloadActive);
      batchView.classList.toggle("hidden", !batchActive);
      accountView.classList.toggle("hidden", !accountActive);
      downloadHistoryView.classList.toggle("hidden", !downloadHistoryActive);
      filesView.classList.add("hidden");
      historyView.classList.toggle("hidden", !historyActive);
      quotaView.classList.toggle("hidden", !quotaActive);
      downloadTab.classList.toggle("active", downloadActive);
      batchTab.classList.toggle("active", batchActive);
      accountTab.classList.toggle("active", accountActive);
      downloadHistoryTab.classList.toggle("active", downloadHistoryActive);
      historyTab.classList.toggle("active", historyActive);
      quotaTab.classList.toggle("active", quotaActive);
      if (accountActive) {
        refreshAccounts().catch((error) => {
          accountPoolEl.innerHTML = `<div class="account-row"><span class="meta">${escapeHtml(error.message)}</span></div>`;
        });
      }
      if (quotaActive) {
        refreshQuotaHistory(false);
      }
      if (historyActive || downloadHistoryActive) {
        refreshHistory();
      }
    }

    function formatTime(timestamp) {
      if (!timestamp) return "-";
      return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
    }

    function renderQuotaLatest(latest) {
      quotaRemainingEl.textContent = latest ? latest.remaining : "-";
      quotaUsageEl.textContent = latest ? latest.usage : "-";
      quotaLimitEl.textContent = latest ? latest.limit : "-";
      quotaResetEl.textContent = latest ? latest.resetText : "-";
      downloadQuotaTextEl.textContent = latest
        ? `账号池额度: 剩余 ${latest.remaining}/${latest.limit}`
        : "账号池额度: 暂无记录";
    }

    function syncCookieLoginVisibility(cookie) {
      const hasCookie = Boolean((cookie || cookieInputEl.value || "").trim());
      fetchCookieButton.classList.remove("hidden");
      if (hasCookie && !cookieStatusEl.textContent.includes("登录中")) {
        cookieStatusEl.textContent = "Cookie 已保存，额度会自动刷新。";
      } else if (!hasCookie) {
        cookieStatusEl.textContent = "会打开独立登录窗口，登录后关闭窗口即可命名并新增账号。";
      }
    }

    function stopCookieLoginPolling() {
      if (cookieLoginPollTimer) {
        clearInterval(cookieLoginPollTimer);
        cookieLoginPollTimer = null;
      }
    }

    async function applyCookieLoginState(state) {
      if (!state || state.status === "idle") {
        return;
      }
      if (state.status === "running") {
        cookieLoginStartedInPage = true;
        fetchCookieButton.disabled = true;
        fetchCookieButton.textContent = "登录中...";
        cookieStatusEl.textContent = "浏览器已打开，登录后关闭窗口即可命名并新增账号。";
        return;
      }
      stopCookieLoginPolling();
      fetchCookieButton.disabled = false;
      fetchCookieButton.textContent = "浏览器登录获取 Cookie";
      if (state.status === "done" && state.cookie) {
        if (!cookieLoginStartedInPage) {
          return;
        }
        const finishedAt = Number(state.finishedAt || Date.now() / 1000);
        if (finishedAt && finishedAt <= handledCookieLoginFinishedAt) {
          return;
        }
        handledCookieLoginFinishedAt = finishedAt;
        cookieLoginStartedInPage = false;
        const defaultName = `Komiic 账号 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
        const enteredName = prompt("账号名称", defaultName);
        const name = (enteredName || defaultName).trim() || defaultName;
        cookieStatusEl.textContent = "正在新增账号...";
        try {
          const data = await postAccountAction("/api/accounts/add-current", {
            name,
            cookie: state.cookie,
            forceNew: true,
            playwrightProfile: Boolean(state.profileAvailable)
          });
          cookieInputEl.value = data.cookie || state.cookie;
          cookieStatusEl.textContent = `已新增账号「${name}」，并设为当前账号。`;
          await refreshQuotaHistory(true).catch((error) => {
            cookieStatusEl.textContent = `已新增账号「${name}」，额度刷新失败: ${error.message}`;
          });
          await refreshAccounts().catch(() => null);
        } catch (error) {
          cookieStatusEl.textContent = `Cookie 已获取，但新增账号失败: ${error.message}`;
          syncCookieLoginVisibility(state.cookie);
        }
        return;
      }
      cookieStatusEl.textContent = state.error || "没有获取到 Cookie。";
      syncCookieLoginVisibility("");
    }

    async function refreshCookieLoginState() {
      const res = await fetch("/api/cookie/playwright/state");
      const state = await res.json();
      if (!res.ok) {
        throw new Error(state.error || "读取登录状态失败");
      }
      await applyCookieLoginState(state);
      return state;
    }

    function startCookieLoginPolling() {
      stopCookieLoginPolling();
      cookieLoginPollTimer = setInterval(() => {
        refreshCookieLoginState().catch((error) => {
          stopCookieLoginPolling();
          fetchCookieButton.disabled = false;
          fetchCookieButton.textContent = "浏览器登录获取 Cookie";
          cookieStatusEl.textContent = error.message;
        });
      }, 1500);
    }

    async function refreshQuotaHistoryData() {
      const res = await fetch(`/api/quota/history?baseUrl=${encodeURIComponent(selectedBaseUrl())}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "读取额度记录失败");
      }
      return data;
    }

    function renderQuotaChart(rows) {
      if (!rows.length) {
        quotaChartEl.innerHTML = "<div class=\"empty\">暂无额度记录</div>";
        return;
      }
      const buckets = new Map();
      rows.forEach((record) => {
        const hour = Math.floor(Number(record.timestamp || 0) / 3600) * 3600;
        if (!hour) return;
        buckets.set(hour, { ...record, hour });
      });
      const points = Array.from(buckets.values()).sort((left, right) => left.hour - right.hour).slice(-48);
      if (!points.length) {
        quotaChartEl.innerHTML = "<div class=\"empty\">暂无可绘制的额度记录</div>";
        return;
      }
      const width = 900;
      const height = 240;
      const padLeft = 54;
      const padRight = 28;
      const padTop = 28;
      const padBottom = 46;
      const maxLimit = Math.max(...points.map((record) => Number(record.limit || 0)), 1);
      const xFor = (index) => points.length <= 1
        ? padLeft
        : padLeft + (index * (width - padLeft - padRight)) / (points.length - 1);
      const yFor = (remaining) => height - padBottom - (Number(remaining || 0) / maxLimit) * (height - padTop - padBottom);
      const line = points.map((record, index) => `${xFor(index).toFixed(1)},${yFor(record.remaining).toFixed(1)}`).join(" ");
      const baseline = height - padBottom;
      const area = `${padLeft},${baseline} ${line} ${xFor(points.length - 1).toFixed(1)},${baseline}`;
      const latest = points[points.length - 1];
      const firstLabel = new Date(points[0].hour * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false });
      const lastLabel = new Date(latest.hour * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false });
      quotaChartEl.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="剩余额度趋势">
          <line class="axis" x1="${padLeft}" y1="${baseline}" x2="${width - padRight}" y2="${baseline}"></line>
          <line class="axis" x1="${padLeft}" y1="${padTop}" x2="${padLeft}" y2="${baseline}"></line>
          <polygon class="remaining-area" points="${area}"></polygon>
          <polyline class="usage-line" points="${line}"></polyline>
          <text x="${padLeft}" y="18" fill="#69707a" font-size="12">纵轴: 剩余额度（张）</text>
          <text x="${width - padRight}" y="18" text-anchor="end" fill="#69707a" font-size="12">当前 ${escapeHtml(latest.remaining)}/${escapeHtml(latest.limit)}</text>
          <text x="${padLeft}" y="${height - 24}" fill="#69707a" font-size="12">${escapeHtml(firstLabel)}</text>
          <text x="${width - padRight}" y="${height - 24}" text-anchor="end" fill="#69707a" font-size="12">${escapeHtml(lastLabel)}</text>
          <text x="${width / 2}" y="${height - 6}" text-anchor="middle" fill="#69707a" font-size="12">横轴: 时间（每小时一个点）</text>
        </svg>
      `;
    }

    async function refreshQuotaHistory(forcePoll = false) {
      quotaCountdown = 60;
      quotaCountdownEl.textContent = "下次自动刷新: 60 秒";
      downloadQuotaCountdownEl.textContent = "下次刷新: 60 秒";
      if (forcePoll) {
        await fetch("/api/quota/poll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ baseUrl: selectedBaseUrl(), cookie: cookieInputEl.value.trim() })
        }).catch(() => null);
      }
      const data = await refreshQuotaHistoryData();
      const latest = data.latest;
      renderQuotaLatest(latest);
      refreshAccounts().catch(() => null);
      const rows = data.history || [];
      if (!rows.length) {
        renderQuotaChart([]);
        quotaHistoryRowsEl.innerHTML = "<tr><td colspan=\"6\" class=\"meta\">暂无记录</td></tr>";
        return;
      }
      renderQuotaChart(rows);
      const renderedRows = rows.slice().reverse().map((record, index, reversedRows) => {
        const previous = reversedRows[index + 1];
        const delta = previous ? Number(record.remaining || 0) - Number(previous.remaining || 0) : 0;
        const deltaText = delta > 0 ? `+${delta}` : String(delta);
        return { record, delta, deltaText };
      }).filter((item) => showUnchangedQuotaRows || item.delta !== 0);
      quotaHistoryRowsEl.innerHTML = renderedRows.length ? renderedRows.map(({ record, deltaText }) => {
        return `
          <tr>
            <td>${escapeHtml(formatTime(record.timestamp))}</td>
            <td>${escapeHtml(record.baseUrl)}</td>
            <td>${escapeHtml(record.remaining)}</td>
            <td>${escapeHtml(record.usage)}</td>
            <td>${escapeHtml(record.limit)}</td>
            <td>${escapeHtml(deltaText)}</td>
          </tr>
        `;
      }).join("") : "<tr><td colspan=\"6\" class=\"meta\">无变化记录已折叠</td></tr>";
    }

    async function loadConfig(baseUrl = "") {
      const suffix = baseUrl ? `?baseUrl=${encodeURIComponent(baseUrl)}` : "";
      const res = await fetch(`/api/config${suffix}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "读取配置失败");
      }
      setBaseUrl(data.baseUrl);
      syncUrlHost(data.baseUrl);
      cookieInputEl.value = data.cookie || "";
      syncCookieLoginVisibility(data.cookie || "");
      renderAccounts(data.accounts || []);
      populateRecentComics(data.comics || []);
      return data;
    }

    function syncChapterLimit() {
      updateSelectionSummary();
    }

    function selectedChapterIds() {
      return Array.from(chapterListEl.querySelectorAll("input[type='checkbox']:checked"))
        .map((input) => input.value);
    }

    function updateSelectionSummary() {
      if (!loadedChapters.length) {
        selectionSummaryEl.textContent = "";
        return;
      }
      const byId = new Map(loadedChapters.map((chapter) => [String(chapter.id), chapter]));
      const checkedIds = selectedChapterIds();
      const estimatePool = showDownloadedChapters ? loadedChapters : loadedChapters.filter((chapter) => !chapter.downloaded);
      let chapters = [];
      if (checkedIds.length) {
        chapters = checkedIds.map((id) => byId.get(id)).filter(Boolean);
      } else {
        chapters = estimatePool;
      }

      const pages = chapters.reduce((sum, chapter) => sum + Number(chapter.size || 0), 0);
      const downloaded = loadedChapters.filter((chapter) => chapter.downloaded).length;
      const hidden = showDownloadedChapters ? 0 : downloaded;
      if (!chapters.length || pages <= 0) {
        selectionSummaryEl.textContent = hidden > 0
          ? `勾选章节后会显示预计图片额度消耗，已隐藏完成章节 ${hidden} 章`
          : "勾选章节后会显示预计图片额度消耗";
        return;
      }
      const suffix = hidden > 0 ? `，已隐藏完成章节 ${hidden} 章` : "";
      selectionSummaryEl.textContent = `已选择 ${chapters.length} 章，预计最多消耗 ${pages} 张图片额度${suffix}`;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function selectComic(comic) {
      if (!comic) {
        return;
      }
      const baseUrl = comic.baseUrl || "https://komiic.com";
      setBaseUrl(baseUrl);
      form.url.value = comic.comicId ? `${baseUrl}/comic/${comic.comicId}` : (comic.url || "");
      renderChapters([]);
      selectionSummaryEl.textContent = "";
    }

    async function fetchChapters() {
      fetchChaptersButton.disabled = true;
      chapterListEl.innerHTML = "<div class=\"chapter-item\"><span></span><span class=\"meta\">正在获取章节...</span><span></span></div>";
      selectionSummaryEl.textContent = "";
      try {
        const res = await fetch("/api/chapters", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            baseUrl: selectedBaseUrl(),
            url: form.url.value.trim(),
            output: form.output.value.trim() || defaultOutputDir,
            cookie: cookieInputEl.value.trim()
          })
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "获取章节失败");
        }
        showDownloadedChapters = false;
        renderChapters(data.chapters || []);
        if (data.comics) {
          populateRecentComics(data.comics);
        }
        return data;
      } catch (error) {
        chapterListEl.innerHTML = `<div class="chapter-item"><span></span><span class="meta">${escapeHtml(error.message)}</span><span></span></div>`;
        throw error;
      } finally {
        fetchChaptersButton.disabled = false;
      }
    }

    async function selectComicAndFetch(comic) {
      selectComic(comic);
      switchView("download");
      try {
        await fetchChapters();
      } catch (error) {
        alert(error.message);
      }
    }

    function renderHistory(comics) {
      historyComics = Array.isArray(comics) ? comics : [];
      const limit = Number(historyLimitSelect.value || 10);
      const visibleComics = historyComics
        .filter((comic) => !showFollowOnly || comic.followed)
        .slice(0, limit);
      if (!visibleComics.length) {
        historyListEl.innerHTML = "<div class=\"meta\">暂无历史记录</div>";
        return;
      }
      historyListEl.innerHTML = visibleComics.map((comic) => {
        const index = historyComics.indexOf(comic);
        const previews = (comic.previewImages || []).slice(0, 3);
        const thumbs = [0, 1, 2].map((slot) => {
          const image = previews[slot];
          if (!image) {
            return "<div class=\"history-empty-thumb\">无图</div>";
          }
          return `<img src="${escapeHtml(image.href)}" alt="">`;
        }).join("");
        return `
          <article class="history-card">
            <div class="history-preview">${thumbs}</div>
            <div class="history-body">
              <div class="history-title-row">
                <button class="follow-star ${comic.followed ? "active" : ""}" type="button" data-follow-history-index="${index}" title="追更">★</button>
                <div class="history-title">${escapeHtml(comic.title || comic.comicId)}</div>
              </div>
              <div class="meta">${escapeHtml(comic.baseUrl || "")}</div>
              <div class="history-actions">
                <button type="button" data-history-index="${index}">获取</button>
                <button type="button" data-download-history-index="${index}">下载</button>
                <button class="danger" type="button" data-delete-history-index="${index}">删除</button>
              </div>
            </div>
          </article>
        `;
      }).join("");
      historyListEl.querySelectorAll("button[data-history-index]").forEach((button) => {
        button.addEventListener("click", () => {
          selectComicAndFetch(historyComics[Number(button.dataset.historyIndex)]);
        });
      });
      historyListEl.querySelectorAll("button[data-download-history-index]").forEach((button) => {
        button.addEventListener("click", () => {
          startHistoryDownload(historyComics[Number(button.dataset.downloadHistoryIndex)]);
        });
      });
      historyListEl.querySelectorAll("button[data-follow-history-index]").forEach((button) => {
        button.addEventListener("click", () => {
          toggleFollowComic(historyComics[Number(button.dataset.followHistoryIndex)]);
        });
      });
      historyListEl.querySelectorAll("button[data-delete-history-index]").forEach((button) => {
        button.addEventListener("click", () => {
          deleteHistoryComic(historyComics[Number(button.dataset.deleteHistoryIndex)]);
        });
      });
    }

    async function startHistoryDownload(comic) {
      if (!comic) return;
      selectComic(comic);
      switchView("download");
      const res = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formPayload())
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({ error: "启动失败" }));
        alert(data.error || "启动失败");
      }
      await refreshState();
    }

    async function toggleFollowComic(comic) {
      if (!comic) return;
      const res = await fetch("/api/history/follow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ baseUrl: comic.baseUrl, comicId: comic.comicId, followed: !comic.followed })
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "更新追更失败");
        return;
      }
      renderHistory(data.comics || []);
    }

    function downloadStatusText(status) {
      return {
        queued: "等待中",
        running: "下载中",
        done: "完成",
        failed: "失败",
        stopped: "已停止",
        retried: "重试完成",
        missing: "仍缺章节"
      }[status] || status;
    }

    function renderDownloadHistory(downloads) {
      const rows = Array.isArray(downloads) ? downloads : [];
      if (!rows.length) {
        downloadHistoryListEl.innerHTML = "<div class=\"meta\">暂无下载任务</div>";
        return;
      }
      downloadHistoryListEl.innerHTML = rows.map((item) => {
        const progress = item.total
          ? `${item.downloaded || 0}/${item.total} 章`
          : `${item.downloaded || 0} 章`;
        const missing = item.missing ? `，缺 ${item.missing} 章` : "";
        return `
          <div class="download-history-row ${escapeHtml(item.status || "")}">
            <strong>${escapeHtml(item.title || item.url)}</strong>
            <div class="meta">${escapeHtml(downloadStatusText(item.status))} · ${escapeHtml(item.type === "batch" ? "批量" : "单个")} · ${escapeHtml(progress)}${escapeHtml(missing)}</div>
            <div class="meta">${escapeHtml(item.message || "")}</div>
            <div class="meta">${escapeHtml(formatTime(item.updatedAt || item.startedAt))} · ${escapeHtml(item.url || "")}</div>
          </div>
        `;
      }).join("");
    }

    async function refreshHistory() {
      const res = await fetch("/api/history");
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "读取历史记录失败");
      }
      renderDownloadHistory(data.downloads || []);
      renderHistory(data.comics || []);
    }

    async function deleteHistoryComic(comic) {
      if (!comic) {
        return;
      }
      if (!confirm(`从历史记录删除「${comic.title || comic.comicId}」？不会删除已下载文件。`)) {
        return;
      }
      const res = await fetch("/api/history/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ baseUrl: comic.baseUrl, comicId: comic.comicId })
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "删除失败");
        return;
      }
      renderHistory(data.comics || []);
      recentComics = data.comics || [];
    }

    function terminalStatus(status) {
      return ["done", "failed", "stopped"].includes(status);
    }

    function setFilesMode(mode) {
      filesMode = mode;
      showAllFilesButton.classList.toggle("active", filesMode === "all");
      showCbzFilesButton.classList.toggle("active", filesMode === "cbz");
      renderFiles();
    }

    function clearFiles(summary = "任务完成后显示") {
      currentFileTree = null;
      currentFileSummary = { chapters: 0, cbz: 0 };
      lastLoadedOutputDir = "";
      filesSummaryEl.textContent = summary;
      filesListEl.innerHTML = "<div class=\"file-empty\">暂无输出文件</div>";
    }

    function fileKindLabel(kind) {
      return {
        root: "输出目录",
        comic: "漫画",
        folder: "目录",
        chapter: "章节",
        cbz_dir: "CBZ目录",
        cbz: "CBZ"
      }[kind] || "文件";
    }

    function nodeHasVisibleContent(node) {
      if (!node) return false;
      if (filesMode !== "cbz") return true;
      if (node.kind === "cbz") return true;
      return (node.children || []).some(nodeHasVisibleContent);
    }

    function renderFileNode(node, depth = 0) {
      if (!nodeHasVisibleContent(node)) {
        return "";
      }
      const children = (node.children || []).filter(nodeHasVisibleContent);
      const hasChildren = children.length > 0;
      const name = escapeHtml(node.name || node.path || "");
      const padding = 14 + depth * 18;
      const toggle = hasChildren ? "▾" : "";
      const label = fileKindLabel(node.kind);
      const title = escapeHtml(node.path || node.name || "");
      const nameHtml = node.href
        ? `<a class="file-name" href="${escapeHtml(node.href)}" target="_blank" rel="noopener" title="${title}">${name}</a>`
        : `<span class="file-name" title="${title}">${name}</span>`;
      return `
        <div class="file-tree-node">
          <div class="file-tree-row" style="padding-left: ${padding}px">
            <button class="file-toggle" type="button" ${hasChildren ? "" : "disabled"}>${toggle}</button>
            <span class="file-kind">${label}</span>
            ${nameHtml}
          </div>
          ${hasChildren ? `<div class="file-tree-children">${children.map((child) => renderFileNode(child, depth + 1)).join("")}</div>` : ""}
        </div>
      `;
    }

    function renderFiles() {
      const chapterCount = currentFileSummary.chapters || 0;
      const cbzCount = currentFileSummary.cbz || 0;
      filesSummaryEl.textContent = chapterCount || cbzCount
        ? `章节 ${chapterCount} 个，CBZ ${cbzCount} 个`
        : "没有找到章节文件夹或 CBZ";
      if (!currentFileTree || !nodeHasVisibleContent(currentFileTree)) {
        filesListEl.innerHTML = `<div class="file-empty">${filesMode === "cbz" ? "没有 CBZ 文件" : "没有找到章节文件夹或 CBZ"}</div>`;
        return;
      }
      filesListEl.innerHTML = renderFileNode(currentFileTree);
    }

    async function loadFiles(outputDir = "") {
      const output = outputDir || form.output.value.trim() || defaultOutputDir;
      refreshFilesButton.disabled = true;
      try {
        const res = await fetch(`/api/files?output=${encodeURIComponent(output)}`);
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "读取文件列表失败");
        }
        currentFileTree = data.tree || null;
        currentFileSummary = data.summary || { chapters: 0, cbz: 0 };
        lastLoadedOutputDir = output;
        renderFiles();
      } finally {
        refreshFilesButton.disabled = false;
      }
    }

    async function chooseOutputDirectory() {
      const res = await fetch("/api/choose-directory", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: form.output.value.trim() || defaultOutputDir })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "选择目录失败");
      }
      if (data.path) {
        form.output.value = data.path;
        renderChapters([]);
        clearFiles("目录已更新");
      }
    }

    function batchStatusText(status) {
      return {
        idle: "空闲",
        running: "运行中",
        done: "完成",
        failed: "有失败",
        stopped: "已停止",
        retried: "重试完成",
        missing: "仍缺章节"
      }[status] || status;
    }

    function batchPayload(urls = batchUrlsEl.value) {
      return {
        baseUrl: selectedBaseUrl(),
        urls,
        output: form.output.value.trim() || defaultOutputDir,
        cookie: cookieInputEl.value.trim(),
        imageDelay: 1,
        chapterDelay: 1,
        autoCopyCbz: batchAutoCopyCbzEl.checked
      };
    }

    function setSharedLogs(lines) {
      const text = Array.isArray(lines) && lines.length ? lines.join("\n") : "等待任务...";
      logsEl.textContent = text;
      logsEl.scrollTop = logsEl.scrollHeight;
      if (batchLogsEl) {
        batchLogsEl.textContent = text;
        batchLogsEl.scrollTop = batchLogsEl.scrollHeight;
      }
    }

    function renderBatchResults(results) {
      if (!results.length) {
        batchResultsEl.innerHTML = "";
        return;
      }
      batchResultsEl.innerHTML = results.map((result, index) => {
        const missing = result.missing || [];
        const missingText = missing.length ? `缺失 ${missing.length} 章: ${missing.map((item) => item.dirName || item.title || item.id).join(", ")}` : "";
        const error = result.error ? `<div class="meta">${escapeHtml(result.error)}</div>` : "";
        return `
          <div class="batch-result ${escapeHtml(result.status || "")}">
            <strong>${index + 1}. ${escapeHtml(result.title || result.url)}</strong>
            <div class="meta">${escapeHtml(batchStatusText(result.status))} · ${escapeHtml(result.url || "")}</div>
            ${missingText ? `<div class="meta">${escapeHtml(missingText)}</div>` : ""}
            ${error}
          </div>
        `;
      }).join("");
    }

    function setBatchState(state) {
      if (state.running) {
        batchEstimateVisible = false;
      }
      batchRunning = state.running;
      const total = (state.urls || []).length;
      const current = state.currentIndex >= 0 ? state.currentIndex + 1 : 0;
      if (!batchEstimateVisible || state.running) {
        batchSummaryEl.textContent = state.running
          ? `正在处理 ${current}/${total}`
          : `状态: ${batchStatusText(state.status)}${total ? `，共 ${total} 个链接` : ""}`;
        batchCommandEl.textContent = state.outputDir ? `输出目录: ${state.outputDir}` : "";
        if (activeView === "batch" || state.running) {
          setSharedLogs(state.logs || []);
        }
        renderBatchResults(state.results || []);
      }
      startBatchButton.disabled = state.running || statusEl.classList.contains("running");
      estimateBatchButton.disabled = state.running || statusEl.classList.contains("running");
      appendBatchUrlsEl.disabled = !state.running;
      appendBatchButton.disabled = !state.running;
      stopBatchButton.disabled = !state.running;
      startButton.disabled = batchRunning || statusEl.classList.contains("running");
      if (!state.running && lastObservedBatchRunning && state.outputDir) {
        loadFiles(state.outputDir).catch((error) => {
          filesSummaryEl.textContent = error.message;
        });
      }
      lastObservedBatchRunning = state.running;
    }

    async function refreshBatchState() {
      const res = await fetch("/api/batch/state");
      const state = await res.json();
      if (!res.ok) {
        throw new Error(state.error || "读取批量状态失败");
      }
      setBatchState(state);
    }

    function renderBatchEstimate(data) {
      batchEstimateVisible = true;
      const totalPages = Number(data.totalPages || 0);
      const missingChapters = Number(data.missingChapters || 0);
      batchSummaryEl.textContent = `预计消耗 ${totalPages} 图片额度，需下载 ${missingChapters} 个章节，共 ${Number(data.totalComics || 0)} 个漫画`;
      const results = Array.isArray(data.results) ? data.results : [];
      if (!results.length) {
        batchResultsEl.innerHTML = "<div class=\"batch-result\"><strong>没有可估算的漫画</strong><div class=\"meta\">请先在上方输入漫画链接。</div></div>";
        return;
      }
      batchResultsEl.innerHTML = results.map((result, index) => {
        const status = result.error ? "failed" : (result.missingChapters ? "running" : "done");
        const detail = result.error
          ? result.error
          : `未下载章节 ${result.missingChapters}/${result.totalChapters}，预计 ${result.pages} 图片额度`;
        return `
          <div class="batch-result ${escapeHtml(status)}">
            <strong>${index + 1}. ${escapeHtml(result.title || result.url)}</strong>
            <div class="meta">${escapeHtml(result.url || "")}</div>
            <div class="meta">${escapeHtml(detail)}</div>
          </div>
        `;
      }).join("");
    }

    function renderChapters(chapters) {
      loadedChapters = chapters;
      toggleDownloadedButton.disabled = !loadedChapters.some((chapter) => chapter.downloaded);
      toggleDownloadedButton.textContent = showDownloadedChapters ? "隐藏已下载章节" : "显示已下载章节";
      const visibleChapters = showDownloadedChapters
        ? loadedChapters
        : loadedChapters.filter((chapter) => !chapter.downloaded);
      if (!visibleChapters.length) {
        const message = loadedChapters.length ? "没有未下载章节" : "没有章节";
        chapterListEl.innerHTML = `<div class="chapter-item"><span></span><span class="meta">${message}</span><span></span></div>`;
        syncChapterLimit();
        return;
      }
      chapterListEl.innerHTML = visibleChapters.map((chapter) => `
        <label class="chapter-item ${chapter.downloaded ? "downloaded" : ""}">
          <input type="checkbox" value="${escapeHtml(chapter.id)}" ${chapter.downloaded ? "disabled" : ""}>
          <span>${escapeHtml(chapter.dirName || chapter.title)}</span>
          <span>${escapeHtml(chapter.size)}p${chapter.downloaded ? " 已下载" : ""}</span>
        </label>
      `).join("");
      chapterListEl.querySelectorAll("input[type='checkbox']").forEach((input) => {
        input.addEventListener("change", syncChapterLimit);
      });
      syncChapterLimit();
    }

    function setStatus(state) {
      statusEl.className = "status";
      if (state.running) {
        statusEl.textContent = "运行中";
        statusEl.classList.add("running");
      } else if (state.status === "done") {
        statusEl.textContent = "完成";
        statusEl.classList.add("done");
      } else if (state.status === "failed" || state.status === "stopped") {
        statusEl.textContent = state.status === "stopped" ? "已停止" : "失败";
        statusEl.classList.add("failed");
      } else {
        statusEl.textContent = "空闲";
      }
      startButton.disabled = state.running || batchRunning;
      stopButton.disabled = !state.running;
      if (state.running) {
        startBatchButton.disabled = true;
      }
    }

    async function refreshState() {
      const res = await fetch("/api/state");
      const state = await res.json();
      setStatus(state);
      commandEl.textContent = state.command.length ? state.command.join(" ") : "";
      if (!batchRunning) {
        setSharedLogs(state.logs || []);
      }
      if (state.running && !lastObservedRunning) {
        clearFiles("任务运行中");
      }
      if (!state.running && terminalStatus(state.status) && state.outputDir && state.outputDir !== lastLoadedOutputDir) {
        loadFiles(state.outputDir).catch((error) => {
          filesSummaryEl.textContent = error.message;
        });
      }
      if (activeView === "history" || activeView === "downloadHistory") {
        refreshHistory().catch(() => null);
      }
      lastObservedRunning = state.running;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearFiles("任务运行中");
      const res = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formPayload())
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({ error: "启动失败" }));
        alert(data.error || "启动失败");
      }
      await refreshState();
    });

    stopButton.addEventListener("click", async () => {
      await fetch("/api/stop", { method: "POST" });
      await refreshState();
    });

    fetchChaptersButton.addEventListener("click", () => {
      fetchChapters().catch(() => null);
    });

    toggleDownloadedButton.addEventListener("click", () => {
      showDownloadedChapters = !showDownloadedChapters;
      renderChapters(loadedChapters);
    });

    selectAllButton.addEventListener("click", () => {
      chapterListEl.querySelectorAll("input[type='checkbox']:not(:disabled)").forEach((input) => { input.checked = true; });
      syncChapterLimit();
    });

    clearSelectionButton.addEventListener("click", () => {
      chapterListEl.querySelectorAll("input[type='checkbox']").forEach((input) => { input.checked = false; });
      syncChapterLimit();
    });

    addAccountButton.addEventListener("click", async () => {
      addAccountButton.disabled = true;
      try {
        const name = prompt("账号名称", "Komiic 账号");
        if (name === null) {
          return;
        }
        await postAccountAction("/api/accounts/add-current", { name, cookie: cookieInputEl.value.trim() });
        cookieStatusEl.textContent = "已加入账号池，并设为当前账号。";
        await refreshQuotaHistory(true).catch((error) => {
          cookieStatusEl.textContent = `已加入账号池，额度刷新失败: ${error.message}`;
        });
        await refreshAccounts().catch(() => null);
      } catch (error) {
        alert(error.message);
      } finally {
        addAccountButton.disabled = false;
      }
    });

    accountPoolEl.addEventListener("click", async (event) => {
      const selectButton = event.target.closest("button[data-account-select]");
      const renameButton = event.target.closest("button[data-account-rename]");
      const refreshCookieButton = event.target.closest("button[data-account-refresh]");
      const cookieButton = event.target.closest("button[data-account-cookie]");
      const toggleButton = event.target.closest("button[data-account-toggle]");
      const deleteButton = event.target.closest("button[data-account-delete]");
      try {
        if (selectButton) {
          await postAccountAction("/api/accounts/select", { accountId: selectButton.dataset.accountSelect });
          await refreshQuotaHistory(false);
          return;
        }
        if (renameButton) {
          const currentName = renameButton.dataset.accountName || "账号";
          const name = prompt("账号名称", currentName);
          if (name === null) {
            return;
          }
          await postAccountAction("/api/accounts/rename", {
            accountId: renameButton.dataset.accountRename,
            name
          });
          return;
        }
        if (refreshCookieButton) {
          const previousText = refreshCookieButton.textContent;
          refreshCookieButton.disabled = true;
          refreshCookieButton.textContent = "刷新中...";
          try {
            await postAccountAction("/api/accounts/refresh-cookie", {
              accountId: refreshCookieButton.dataset.accountRefresh
            });
            await refreshQuotaHistory(true).catch(() => null);
          } finally {
            refreshCookieButton.disabled = false;
            refreshCookieButton.textContent = previousText;
          }
          return;
        }
        if (cookieButton) {
          const cookie = prompt("粘贴新的 Cookie");
          if (cookie === null) {
            return;
          }
          await postAccountAction("/api/accounts/update-cookie", {
            accountId: cookieButton.dataset.accountCookie,
            cookie
          });
          await refreshQuotaHistory(true).catch(() => null);
          return;
        }
        if (toggleButton) {
          await postAccountAction("/api/accounts/toggle", {
            accountId: toggleButton.dataset.accountToggle,
            enabled: toggleButton.dataset.enabled === "1"
          });
          return;
        }
        if (deleteButton) {
          if (!confirm("删除这个账号？只会移除保存的 Cookie，不会影响已下载文件。")) {
            return;
          }
          await postAccountAction("/api/accounts/delete", { accountId: deleteButton.dataset.accountDelete });
          await refreshQuotaHistory(false);
        }
      } catch (error) {
        alert(error.message);
      }
    });

    fetchCookieButton.addEventListener("click", async () => {
      fetchCookieButton.disabled = true;
      cookieLoginStartedInPage = true;
      const previousText = fetchCookieButton.textContent;
      fetchCookieButton.textContent = "启动中...";
      cookieStatusEl.textContent = "正在打开登录窗口。";
      try {
        const res = await fetch("/api/cookie/playwright/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ baseUrl: selectedBaseUrl() })
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "获取 Cookie 失败");
        }
        await applyCookieLoginState(data);
        startCookieLoginPolling();
      } catch (error) {
        cookieStatusEl.textContent = error.message;
        fetchCookieButton.textContent = previousText;
        fetchCookieButton.disabled = false;
      }
    });

    downloadTab.addEventListener("click", () => {
      switchView("download");
    });

    batchTab.addEventListener("click", () => {
      switchView("batch");
      refreshBatchState().catch((error) => {
        batchSummaryEl.textContent = error.message;
      });
    });

    accountTab.addEventListener("click", () => {
      switchView("account");
    });

    downloadHistoryTab.addEventListener("click", () => {
      switchView("downloadHistory");
    });

    historyTab.addEventListener("click", () => {
      switchView("history");
    });

    quotaTab.addEventListener("click", () => {
      switchView("quota");
    });

    refreshHistoryButton.addEventListener("click", async () => {
      refreshHistoryButton.disabled = true;
      try {
        await refreshHistory();
      } catch (error) {
        alert(error.message);
      } finally {
        refreshHistoryButton.disabled = false;
      }
    });

    historyLimitSelect.addEventListener("change", () => {
      renderHistory(historyComics);
    });

    showFollowButton.addEventListener("click", () => {
      showFollowOnly = !showFollowOnly;
      showFollowButton.classList.toggle("active", showFollowOnly);
      showFollowButton.textContent = showFollowOnly ? "显示全部" : "只看追更";
      renderHistory(historyComics);
    });

    chooseOutputButton.addEventListener("click", async () => {
      chooseOutputButton.disabled = true;
      try {
        await chooseOutputDirectory();
      } catch (error) {
        if (error.message !== "已取消选择") {
          alert(error.message);
        }
      } finally {
        chooseOutputButton.disabled = false;
      }
    });

    copyCbzButton.addEventListener("click", async () => {
      copyCbzButton.disabled = true;
      const previousText = copyCbzButton.textContent;
      copyCbzButton.textContent = "复制中...";
      try {
        const res = await fetch("/api/files/copy-cbz", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ output: form.output.value.trim() || defaultOutputDir })
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "复制失败");
        }
        alert(`已复制 ${data.copied || 0} 个 CBZ 到 ${data.target || ""}`);
      } catch (error) {
        alert(error.message);
      } finally {
        copyCbzButton.textContent = previousText;
        copyCbzButton.disabled = false;
      }
    });

    startBatchButton.addEventListener("click", async () => {
      batchEstimateVisible = false;
      startBatchButton.disabled = true;
      try {
        const res = await fetch("/api/batch/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(batchPayload())
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "启动批量下载失败");
        }
        setBatchState(data);
      } catch (error) {
        alert(error.message);
      } finally {
        await refreshBatchState().catch(() => null);
      }
    });

    estimateBatchButton.addEventListener("click", async () => {
      if (!batchUrlsEl.value.trim()) {
        batchSummaryEl.textContent = "请先输入至少一个漫画链接。";
        batchResultsEl.innerHTML = "<div class=\"batch-result failed\"><strong>没有链接</strong><div class=\"meta\">每行输入一个漫画链接后再估算。</div></div>";
        batchUrlsEl.focus();
        return;
      }
      estimateBatchButton.disabled = true;
      const previousText = estimateBatchButton.textContent;
      estimateBatchButton.textContent = "获取中...";
      batchSummaryEl.textContent = "正在获取章节并检查本地输出目录...";
      batchResultsEl.innerHTML = "<div class=\"batch-result\"><strong>正在估算...</strong><div class=\"meta\">正在读取章节和本地下载状态。</div></div>";
      try {
        const res = await fetch("/api/batch/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(batchPayload())
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "估算失败");
        }
        renderBatchEstimate(data);
      } catch (error) {
        batchSummaryEl.textContent = error.message;
        batchResultsEl.innerHTML = `<div class="batch-result failed"><strong>估算失败</strong><div class="meta">${escapeHtml(error.message)}</div></div>`;
      } finally {
        estimateBatchButton.textContent = previousText;
        estimateBatchButton.disabled = batchRunning || statusEl.classList.contains("running");
      }
    });

    stopBatchButton.addEventListener("click", async () => {
      await fetch("/api/batch/stop", { method: "POST" });
      await refreshBatchState().catch(() => null);
    });

    appendBatchButton.addEventListener("click", async () => {
      batchEstimateVisible = false;
      appendBatchButton.disabled = true;
      try {
        const res = await fetch("/api/batch/append", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(batchPayload(appendBatchUrlsEl.value))
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "追加失败");
        }
        appendBatchUrlsEl.value = "";
        setBatchState(data);
      } catch (error) {
        alert(error.message);
      } finally {
        await refreshBatchState().catch(() => null);
      }
    });

    refreshFilesButton.addEventListener("click", async () => {
      try {
        await loadFiles(lastLoadedOutputDir || form.output.value.trim() || defaultOutputDir);
      } catch (error) {
        alert(error.message);
      }
    });

    showAllFilesButton.addEventListener("click", () => {
      setFilesMode("all");
    });

    showCbzFilesButton.addEventListener("click", () => {
      setFilesMode("cbz");
    });

    filesListEl.addEventListener("click", (event) => {
      const button = event.target.closest(".file-toggle");
      if (!button || button.disabled) {
        return;
      }
      const node = button.closest(".file-tree-node");
      const children = node?.querySelector(":scope > .file-tree-children");
      if (!children) {
        return;
      }
      const hidden = children.style.display === "none";
      children.style.display = hidden ? "" : "none";
      button.textContent = hidden ? "▾" : "▸";
    });

    refreshQuotaHistoryButton.addEventListener("click", async () => {
      refreshQuotaHistoryButton.disabled = true;
      try {
        await refreshQuotaHistory(true);
      } catch (error) {
        alert(error.message);
      } finally {
        refreshQuotaHistoryButton.disabled = false;
      }
    });

    toggleUnchangedQuotaButton.addEventListener("click", () => {
      showUnchangedQuotaRows = !showUnchangedQuotaRows;
      toggleUnchangedQuotaButton.textContent = showUnchangedQuotaRows ? "折叠无变化" : "展开无变化";
      refreshQuotaHistory(false).catch((error) => {
        quotaHistoryRowsEl.innerHTML = `<tr><td colspan="6" class="meta">${escapeHtml(error.message)}</td></tr>`;
      });
    });

    form.querySelectorAll("input[name='baseUrl']").forEach((input) => {
      input.addEventListener("change", async () => {
        try {
          await loadConfig(input.value);
          await refreshQuotaHistory(false);
          renderChapters([]);
        } catch (error) {
          alert(error.message);
        }
      });
    });

    syncChapterLimit();
    loadConfig()
      .then(() => refreshQuotaHistory(false))
      .catch((error) => {
        downloadQuotaTextEl.textContent = `额度: ${error.message}`;
      });
    refreshState();
    refreshBatchState().catch(() => null);
    refreshCookieLoginState().then((state) => {
      if (state.running) {
        startCookieLoginPolling();
      }
    }).catch(() => null);
    setInterval(refreshState, 1500);
    setInterval(() => {
      refreshBatchState().catch(() => null);
    }, 1500);
    setInterval(() => {
      quotaCountdown = Math.max(quotaCountdown - 1, 0);
      quotaCountdownEl.textContent = `下次自动刷新: ${quotaCountdown} 秒`;
      downloadQuotaCountdownEl.textContent = `下次刷新: ${quotaCountdown} 秒`;
      if (quotaCountdown === 0) {
        refreshQuotaHistory(true).catch((error) => {
          quotaCountdown = 60;
          quotaCountdownEl.textContent = `刷新失败: ${error.message}`;
          downloadQuotaCountdownEl.textContent = `刷新失败: ${error.message}`;
        });
      }
    }, 1000);
  </script>
</body>
</html>
"""


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    body = handler.rfile.read(length).decode("utf-8")
    return json.loads(body)


def safe_output_dir(value: str) -> Path:
    output = Path(value or DEFAULT_OUTPUT).expanduser()
    if not output.is_absolute():
        output = ROOT / output
    return output.resolve()


def directory_payload(value: str = "") -> dict:
    raw = str(value or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT / path
    else:
        path = ROOT
    try:
        path = path.resolve()
    except OSError:
        path = ROOT
    while not path.exists() and path.parent != path:
        path = path.parent
    if path.is_file():
        path = path.parent
    if not path.exists() or not path.is_dir():
        path = ROOT

    directories = []
    try:
        children = sorted(
            [child for child in path.iterdir() if child.is_dir()],
            key=lambda child: (child.name.startswith("."), child.name.lower()),
        )
    except OSError:
        children = []
    for child in children:
        directories.append({"name": child.name, "path": str(child)})

    home = Path.home()
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "home": str(home),
        "desktop": str(home / "Desktop"),
        "documents": str(home / "Documents"),
        "project": str(ROOT),
        "directories": directories,
    }


def make_session(cookie: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    cookie = cookie.strip()
    if cookie:
        session.headers.update({"Cookie": cookie})
    return session


def cookie_header_from_playwright_cookies(cookies: list[dict], base_url: str) -> str:
    host = urlparse(base_url).hostname or ""
    pairs = []
    seen = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").lstrip(".")
        if not name or not domain:
            continue
        if host != domain and not host.endswith(f".{domain}"):
            continue
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def import_playwright():
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 Playwright，请先安装依赖: pip install -r requirements.txt") from exc
    return sync_playwright, PlaywrightError, PlaywrightTimeoutError


def launch_persistent_playwright_context(playwright, profile_dir: Path, *, headless: bool, timeout: int):
    _, PlaywrightError, _ = import_playwright()
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_options = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            timeout=timeout,
            **launch_options,
        )
    except PlaywrightError:
        return playwright.chromium.launch_persistent_context(
            str(profile_dir),
            timeout=timeout,
            **launch_options,
        )


def read_playwright_cookie_header(context, base_url: str) -> tuple[str, int]:
    cookies = context.cookies([base_url])
    return cookie_header_from_playwright_cookies(cookies, base_url), len(cookies)


def fetch_cookie_with_playwright(base_url: str) -> dict[str, str | int | bool]:
    selected_base_url = normalize_base_url(base_url or DEFAULT_BASE_URL)
    sync_playwright, PlaywrightError, _ = import_playwright()

    latest_cookie = ""
    cookie_count = 0
    with COOKIE_REFRESH_LOCK:
        with sync_playwright() as playwright:
            context = launch_persistent_playwright_context(
                playwright,
                PLAYWRIGHT_PROFILE_DIR,
                headless=False,
                timeout=20000,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selected_base_url, wait_until="domcontentloaded", timeout=60000)
                deadline = time.time() + 180
                while time.time() < deadline:
                    try:
                        header, cookie_count = read_playwright_cookie_header(context, selected_base_url)
                        if header:
                            latest_cookie = header
                        if not context.pages:
                            break
                    except PlaywrightError:
                        break
                    time.sleep(1)
            finally:
                try:
                    header, cookie_count = read_playwright_cookie_header(context, selected_base_url)
                    latest_cookie = header or latest_cookie
                except PlaywrightError:
                    pass
                try:
                    context.close()
                except PlaywrightError:
                    pass

    if not latest_cookie:
        raise RuntimeError("没有获取到 Cookie，请登录完成后关闭浏览器窗口再试")
    return {"cookie": latest_cookie, "cookieCount": cookie_count, "profileAvailable": True}


def refresh_cookie_with_playwright(base_url: str) -> dict[str, str | int]:
    selected_base_url = normalize_base_url(base_url or DEFAULT_BASE_URL)
    sync_playwright, PlaywrightError, PlaywrightTimeoutError = import_playwright()
    if not PLAYWRIGHT_PROFILE_DIR.exists():
        raise RuntimeError("还没有浏览器登录资料，请先用浏览器登录获取 Cookie")
    if not COOKIE_REFRESH_LOCK.acquire(blocking=False):
        raise RuntimeError("Cookie 正在刷新中")
    try:
        with sync_playwright() as playwright:
            context = launch_persistent_playwright_context(
                playwright,
                PLAYWRIGHT_PROFILE_DIR,
                headless=True,
                timeout=20000,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(selected_base_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1500)
                header, cookie_count = read_playwright_cookie_header(context, selected_base_url)
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    finally:
        COOKIE_REFRESH_LOCK.release()
    if not header:
        raise RuntimeError("浏览器资料中没有可用 Cookie，请重新登录")
    return {"cookie": header, "cookieCount": cookie_count}


def cookie_login_worker(base_url: str) -> None:
    try:
        result = fetch_cookie_with_playwright(base_url)
        with COOKIE_LOGIN.lock:
            COOKIE_LOGIN.running = False
            COOKIE_LOGIN.status = "done"
            COOKIE_LOGIN.finished_at = time.time()
            COOKIE_LOGIN.cookie = str(result.get("cookie") or "")
            COOKIE_LOGIN.cookie_count = int(result.get("cookieCount") or 0)
            COOKIE_LOGIN.profile_available = bool(result.get("profileAvailable"))
            COOKIE_LOGIN.error = ""
    except Exception as exc:
        with COOKIE_LOGIN.lock:
            COOKIE_LOGIN.running = False
            COOKIE_LOGIN.status = "failed"
            COOKIE_LOGIN.finished_at = time.time()
            COOKIE_LOGIN.error = str(exc)


def start_cookie_login(base_url: str) -> dict:
    selected_base_url = normalize_base_url(base_url or DEFAULT_BASE_URL)
    with COOKIE_LOGIN.lock:
        stale = COOKIE_LOGIN.running and COOKIE_LOGIN.started_at and time.time() - COOKIE_LOGIN.started_at > 240
        if stale:
            COOKIE_LOGIN.running = False
            COOKIE_LOGIN.status = "failed"
            COOKIE_LOGIN.finished_at = time.time()
            COOKIE_LOGIN.error = "上一次浏览器登录已超时，请重新点击"
        if COOKIE_LOGIN.running:
            already_running = True
        else:
            already_running = False
            COOKIE_LOGIN.running = True
            COOKIE_LOGIN.status = "running"
            COOKIE_LOGIN.started_at = time.time()
            COOKIE_LOGIN.finished_at = None
            COOKIE_LOGIN.base_url = selected_base_url
            COOKIE_LOGIN.cookie = ""
            COOKIE_LOGIN.cookie_count = 0
            COOKIE_LOGIN.profile_available = False
            COOKIE_LOGIN.error = ""
    if already_running:
        return COOKIE_LOGIN.snapshot()
    threading.Thread(target=cookie_login_worker, args=(selected_base_url,), daemon=True).start()
    return COOKIE_LOGIN.snapshot()


def payload_base_url(payload: dict) -> str:
    return normalize_base_url(str(payload.get("baseUrl") or DEFAULT_BASE_URL))


def payload_cookie(payload: dict, base_url: str) -> str:
    cookie = str(payload.get("cookie") or "").strip()
    return cookie or saved_cookie(base_url)


def refresh_playwright_account_cookie(
    base_url: str,
    account_id: str = "",
    *,
    force: bool = False,
    log_func=None,
) -> dict | None:
    selected_base_url = normalize_base_url(base_url)
    config = load_config()
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    selected_id = str(account_id or config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "").strip()
    account = next((item for item in accounts if str(item.get("id") or "") == selected_id), None)
    if not account or not account.get("enabled", True) or not account.get("playwrightProfile"):
        return account
    with COOKIE_LOGIN.lock:
        if COOKIE_LOGIN.running:
            return account
    now = time.time()
    if not force and now - float(account.get("lastCookieRefreshAt") or 0) < COOKIE_REFRESH_MIN_INTERVAL:
        return account
    if log_func:
        log_func(f"正在刷新账号「{account.get('name') or '账号'}」的 Cookie。")
    try:
        result = refresh_cookie_with_playwright(selected_base_url)
    except Exception as exc:
        account["lastError"] = str(exc)[:200]
        save_config(config)
        if log_func:
            log_func(f"Cookie 刷新失败，继续使用已保存 Cookie: {exc}")
        return account
    refreshed_cookie = str(result.get("cookie") or "").strip()
    if not refreshed_cookie:
        return account
    account["cookie"] = refreshed_cookie
    account["lastCookieRefreshAt"] = time.time()
    account["lastError"] = ""
    if str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "") == str(account.get("id") or ""):
        config.setdefault("cookies", {})[selected_base_url] = refreshed_cookie
    save_config(config)
    if log_func:
        log_func(f"账号「{account.get('name') or '账号'}」Cookie 已刷新。")
    return account


def payload_cookie_with_refresh(payload: dict, base_url: str, log_func=None) -> str:
    manual_cookie = str(payload.get("cookie") or "").strip()
    if manual_cookie:
        return manual_cookie
    account = refresh_playwright_account_cookie(base_url, log_func=log_func)
    return str((account or {}).get("cookie") or saved_cookie(base_url))


def cookie_matches_account(cookie: str, accounts: list[dict]) -> bool:
    cookie = cookie.strip()
    return bool(cookie) and any(str(account.get("cookie") or "").strip() == cookie for account in accounts)


def payload_has_manual_cookie(payload: dict) -> bool:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        return False
    accounts = load_config().get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    return not cookie_matches_account(cookie, accounts)


def account_candidates(payload: dict, base_url: str) -> list[dict]:
    manual_cookie = str(payload.get("cookie") or "").strip()
    config = load_config()
    selected_base_url = normalize_base_url(base_url)
    accounts = [
        account
        for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
        if account.get("enabled", True) and str(account.get("cookie") or "").strip()
    ]
    if manual_cookie and not cookie_matches_account(manual_cookie, accounts):
        return [{"id": "manual", "name": "输入框 Cookie", "cookie": manual_cookie}]
    active_id = str(config.get("activeAccounts", {}).get(ACCOUNT_POOL_KEY) or "")
    if active_id:
        active_index = next((index for index, account in enumerate(accounts) if account.get("id") == active_id), 0)
        accounts = accounts[active_index:] + accounts[:active_index]
    if accounts:
        return [
            {
                "id": str(account.get("id") or ""),
                "name": str(account.get("name") or "账号"),
                "cookie": str(account.get("cookie") or ""),
                "playwrightProfile": bool(account.get("playwrightProfile")),
            }
            for account in accounts
        ]
    cookie = saved_cookie(selected_base_url)
    return [{"id": "saved", "name": "保存的 Cookie", "cookie": cookie}] if cookie else [{"id": "", "name": "无 Cookie", "cookie": ""}]


def activate_account(base_url: str, account_id: str) -> None:
    if not account_id or account_id in {"manual", "saved"}:
        return
    config = load_config()
    selected_base_url = normalize_base_url(base_url)
    accounts = config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, [])
    account = next((item for item in accounts if item.get("id") == account_id), None)
    if not account:
        return
    config.setdefault("activeAccounts", {})[ACCOUNT_POOL_KEY] = account_id
    config.setdefault("cookies", {})[selected_base_url] = str(account.get("cookie") or "")
    save_config(config)


def update_account_quota(base_url: str, cookie: str, limit_info: dict) -> None:
    cookie = cookie.strip()
    if not cookie:
        return
    selected_base_url = normalize_base_url(base_url)
    config = load_config()
    changed = False
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        if str(account.get("cookie") or "").strip() != cookie:
            continue
        limit = int(limit_info.get("limit") or 0)
        usage = int(limit_info.get("usage") or 0)
        account["lastLimit"] = limit
        account["lastUsage"] = usage
        account["lastRemaining"] = max(limit - usage, 0)
        account["lastCheckedAt"] = time.time()
        account["lastResetInSeconds"] = limit_info.get("resetInSeconds")
        account["lastResetText"] = format_reset_time(limit_info.get("resetInSeconds"))
        account["lastError"] = ""
        changed = True
    if changed:
        save_config(config)


def update_account_error(base_url: str, cookie: str, error: str) -> None:
    cookie = cookie.strip()
    if not cookie:
        return
    selected_base_url = normalize_base_url(base_url)
    config = load_config()
    changed = False
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        if str(account.get("cookie") or "").strip() != cookie:
            continue
        account["lastError"] = error[:200]
        account["lastCheckedAt"] = time.time()
        changed = True
    if changed:
        save_config(config)


def enabled_account_cookies(base_url: str) -> list[str]:
    selected_base_url = normalize_base_url(base_url)
    config = load_config()
    cookies = []
    seen = set()
    for account in config.get("accountPools", {}).get(ACCOUNT_POOL_KEY, []):
        cookie = str(account.get("cookie") or "").strip()
        if not account.get("enabled", True) or not cookie or cookie in seen:
            continue
        cookies.append(cookie)
        seen.add(cookie)
    if not cookies:
        fallback = str((config.get("cookies") or {}).get(selected_base_url) or "").strip()
        if fallback:
            cookies.append(fallback)
    return cookies


def get_account_pool_limit_info(base_url: str) -> dict | None:
    selected_base_url = normalize_base_url(base_url)
    refresh_playwright_account_cookie(selected_base_url)
    cookies = enabled_account_cookies(selected_base_url)
    if not cookies:
        return None
    total_limit = 0
    total_usage = 0
    reset_values = []
    checked = 0
    errors = []
    for cookie in cookies:
        try:
            limit_info = get_image_limit(make_session(cookie), base_url=selected_base_url)
            update_account_quota(selected_base_url, cookie, limit_info)
            total_limit += int(limit_info.get("limit") or 0)
            total_usage += int(limit_info.get("usage") or 0)
            if limit_info.get("resetInSeconds") is not None:
                reset_values.append(int(limit_info.get("resetInSeconds") or 0))
            checked += 1
        except Exception as exc:
            update_account_error(selected_base_url, cookie, str(exc))
            errors.append(str(exc))
    if checked <= 0:
        raise RuntimeError(errors[0] if errors else "账号池额度查询失败")
    remaining = max(total_limit - total_usage, 0)
    return {
        "limit": total_limit,
        "usage": total_usage,
        "remaining": remaining,
        "resetInSeconds": min(reset_values) if reset_values else None,
        "accountCount": checked,
        "errors": errors[:3],
    }


def run_process_once(command: list[str], output_dir: Path, cookie: str, log_func, owner: str) -> int:
    child_env = os.environ.copy()
    if cookie:
        child_env["KOMIIC_COOKIE"] = cookie
    else:
        child_env.pop("KOMIIC_COOKIE", None)
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if owner == "job":
        with JOB.lock:
            JOB.process = process
    else:
        with BATCH.lock:
            BATCH.process = process
    assert process.stdout is not None
    for line in process.stdout:
        log_func(line)
    returncode = process.wait()
    if owner == "job":
        with JOB.lock:
            if JOB.process is process:
                JOB.process = None
    else:
        with BATCH.lock:
            if BATCH.process is process:
                BATCH.process = None
    log_func(f"进程退出，代码: {returncode}")
    return returncode


def run_command_with_account_rotation(
    command: list[str],
    output_dir: Path,
    payload: dict,
    base_url: str,
    log_func,
    owner: str,
    stopped_func=lambda: False,
) -> tuple[int, dict]:
    last_code = 1
    used_account = {}
    for account in account_candidates(payload, base_url):
        if stopped_func():
            return 130, account
        used_account = account
        name = account.get("name") or "账号"
        log_func(f"使用账号: {name}")
        activate_account(base_url, str(account.get("id") or ""))
        if account.get("playwrightProfile"):
            refreshed = refresh_playwright_account_cookie(base_url, str(account.get("id") or ""), log_func=log_func)
            if refreshed:
                account["cookie"] = str(refreshed.get("cookie") or account.get("cookie") or "")
        last_code = run_process_once(command, output_dir, str(account.get("cookie") or ""), log_func, owner)
        if last_code != QUOTA_EXCEEDED_EXIT_CODE:
            return last_code, account
        log_func(f"账号「{name}」额度用完，切换下一个账号继续。")
    return last_code, used_account


def build_command(payload: dict) -> tuple[list[str], Path, str]:
    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("链接不能为空")

    base_url = payload_base_url(payload)
    output_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    command = [
        sys.executable,
        "-u",
        str(ROOT / "komiic_downloader.py"),
        url,
        "-o",
        str(output_dir),
        "--base-url",
        base_url,
        "--chapter-delay",
        str(float(payload.get("chapterDelay") or 0)),
        "--image-delay",
        str(float(payload.get("imageDelay") or 0)),
    ]

    max_chapters = int(payload.get("maxChapters") or 0)
    selected_chapter_ids = [
        str(chapter_id).strip()
        for chapter_id in payload.get("selectedChapterIds", [])
        if str(chapter_id).strip()
    ]
    if selected_chapter_ids and not payload.get("singleChapter"):
        command.extend(["--chapter-ids", ",".join(selected_chapter_ids)])
    elif not payload.get("downloadAll") and not payload.get("singleChapter") and max_chapters > 0:
        command.extend(["--max-chapters", str(max_chapters)])
    if payload.get("singleChapter"):
        command.append("--single-chapter")

    cookie = payload_cookie(payload, base_url)
    return command, output_dir, cookie


def remember_payload_comic(payload: dict, title: str = "") -> None:
    url = str(payload.get("url") or "").strip()
    if not url or not title:
        return
    base_url = payload_base_url(payload)
    remember_comic(base_url, url, title)


def job_stopped() -> bool:
    with JOB.lock:
        return JOB.status == "stopping"


def job_worker(payload: dict, command: list[str], output_dir: Path, history_id: str) -> None:
    base_url = payload_base_url(payload)
    returncode = 1
    final_job_status = "failed"
    try:
        update_download_history(history_id, status="running", message="正在下载")
        returncode, account = run_command_with_account_rotation(
            command,
            output_dir,
            payload,
            base_url,
            JOB.append_log,
            "job",
            stopped_func=job_stopped,
        )
        missing, title = missing_chapters_for_url(payload)
        downloaded = 0
        total = 0
        try:
            chapters_payload = fetch_chapters(payload)
            total = int(chapters_payload.get("total") or 0)
            downloaded = int(chapters_payload.get("completed") or 0)
        except Exception:
            pass
        remember_payload_comic(payload, title)
        if job_stopped():
            status = "stopped"
            message = "已停止"
        elif missing:
            status = "missing"
            message = f"仍缺 {len(missing)} 章"
        elif returncode == 0:
            status = "done"
            message = f"完成，账号: {account.get('name') or '-'}"
        else:
            status = "failed"
            message = f"下载进程退出代码 {returncode}"
        final_job_status = status
        update_download_history(history_id, status=status, title=title, downloaded=downloaded, missing=len(missing), total=total, message=message)
    except Exception as exc:
        returncode = 1
        final_job_status = "failed"
        update_download_history(history_id, status="failed", message=str(exc))
        JOB.append_log(f"任务失败: {exc}")
    finally:
        if payload.get("autoCopyCbz") and final_job_status not in {"failed", "stopped"}:
            auto_copy_recent_cbz(output_dir, JOB.append_log)
        with JOB.lock:
            JOB.returncode = returncode
            JOB.finished_at = time.time()
            if JOB.status == "stopping":
                JOB.status = "stopped"
            elif final_job_status in {"done", "retried"}:
                JOB.status = "done"
            else:
                JOB.status = "failed"
            JOB.process = None


def start_job(payload: dict) -> dict:
    command, output_dir, _ = build_command(payload)
    base_url = payload_base_url(payload)
    url = str(payload.get("url") or "").strip()
    with JOB.lock:
        if JOB.status == "running" or (JOB.process is not None and JOB.process.poll() is None):
            raise RuntimeError("已有任务正在运行")
    with BATCH.lock:
        if BATCH.status == "running":
            raise RuntimeError("批量下载正在运行")
    history_id = create_download_history(url=url, base_url=base_url, task_type="single", status="queued", message="等待下载")
    with JOB.lock:
        JOB.status = "running"
        JOB.started_at = time.time()
        JOB.finished_at = None
        JOB.command = command
        JOB.logs = []
        JOB.returncode = None
        JOB.output_dir = str(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=job_worker, args=(payload, command, output_dir, history_id), daemon=True).start()
    return JOB.snapshot()


def is_chapter_complete(output_dir: Path, chapter) -> bool:
    if chapter.size <= 0:
        return False
    chapter_dir = stable_chapter_dir(output_dir, chapter)
    if not chapter_dir.exists() or not chapter_dir.is_dir():
        return False
    return all(existing_image_path(chapter_dir, index) is not None for index in range(1, chapter.size + 1))


def missing_chapters_for_url(payload: dict) -> tuple[list[dict[str, str]], str]:
    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("链接不能为空")
    base_url = payload_base_url(payload)
    output_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    session = make_session(payload_cookie_with_refresh(payload, base_url))
    chapters = get_chapters(session, url, base_url=base_url)
    comic_title = chapters[0].comic_title if chapters else url
    missing = [
        {
            "id": chapter.chapter_id,
            "title": chapter.title,
            "dirName": safe_name(f"{chapter.comic_title}_{chapter.serial}_{chapter.size}p"),
        }
        for chapter in chapters
        if not is_chapter_complete(output_dir, chapter)
    ]
    return missing, comic_title


def fetch_chapters(payload: dict) -> dict:
    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("链接不能为空")
    base_url = payload_base_url(payload)
    session = make_session(payload_cookie_with_refresh(payload, base_url))
    chapters = get_chapters(session, url, base_url=base_url)
    output_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    completed_ids = {
        chapter.chapter_id
        for chapter in chapters
        if is_chapter_complete(output_dir, chapter)
    }
    comic_title = chapters[0].comic_title if chapters else url
    remember_comic(base_url, url, comic_title)
    return {
        "total": len(chapters),
        "completed": len(completed_ids),
        "comics": load_config().get("comics") or [],
        "chapters": [
            {
                "id": chapter.chapter_id,
                "title": chapter.title,
                "url": chapter.url,
                "size": chapter.size,
                "comicTitle": chapter.comic_title,
                "serial": chapter.serial,
                "dirName": safe_name(f"{chapter.comic_title}_{chapter.serial}_{chapter.size}p"),
                "downloaded": chapter.chapter_id in completed_ids,
            }
            for chapter in chapters
        ]
    }


def fetch_quota(payload: dict) -> dict:
    base_url = payload_base_url(payload)
    cookie = payload_cookie(payload, base_url)
    if cookie and payload_has_manual_cookie(payload):
        remember_cookie(base_url, cookie)
    limit_info = get_account_pool_limit_info(base_url)
    if not limit_info:
        raise RuntimeError("账号池没有可用 Cookie，无法查询额度")
    record = remember_quota(base_url, limit_info)
    limit = int(limit_info.get("limit") or 0)
    usage = int(limit_info.get("usage") or 0)
    remaining = max(limit - usage, 0)
    return {
        "limit": limit,
        "usage": usage,
        "remaining": remaining,
        "resetInSeconds": limit_info.get("resetInSeconds"),
        "resetText": format_reset_time(limit_info.get("resetInSeconds")),
        "record": record,
    }


def fetch_quota_poll(payload: dict) -> dict:
    base_url = payload_base_url(payload)
    cookie = payload_cookie(payload, base_url)
    if cookie and payload_has_manual_cookie(payload):
        remember_cookie(base_url, cookie)
    record = poll_quota_once(base_url)
    if not record:
        raise RuntimeError("账号池没有可用 Cookie，无法自动拉取额度")
    return {"record": record, **quota_history_payload(base_url)}


def batch_urls(payload: dict) -> list[str]:
    raw_urls = str(payload.get("urls") or "")
    urls = []
    seen = set()
    for line in raw_urls.splitlines():
        url = line.strip()
        if not url or url.startswith("#") or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    if not urls:
        raise ValueError("请至少输入一个漫画链接")
    return urls


def batch_append_log(line: str) -> None:
    BATCH.append_log(line)


def batch_stopped() -> bool:
    with BATCH.lock:
        return BATCH.stop_requested


def batch_result_for_url(url: str, status: str, title: str = "", missing: list[dict] | None = None, error: str = "") -> dict:
    return {
        "url": url,
        "title": title or url,
        "status": status,
        "missing": missing or [],
        "error": error,
    }


def estimate_batch(payload: dict) -> dict:
    urls = batch_urls(payload)
    base_url = payload_base_url(payload)
    output_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    session = make_session(payload_cookie_with_refresh(payload, base_url))
    results = []
    total_pages = 0
    total_missing_chapters = 0
    for url in urls:
        try:
            chapters = get_chapters(session, url, base_url=base_url)
            title = chapters[0].comic_title if chapters else url
            missing = [chapter for chapter in chapters if not is_chapter_complete(output_dir, chapter)]
            pages = sum(max(int(chapter.size or 0), 0) for chapter in missing)
            total_pages += pages
            total_missing_chapters += len(missing)
            results.append(
                {
                    "url": url,
                    "title": title,
                    "totalChapters": len(chapters),
                    "downloadedChapters": len(chapters) - len(missing),
                    "missingChapters": len(missing),
                    "pages": pages,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "url": url,
                    "title": url,
                    "totalChapters": 0,
                    "downloadedChapters": 0,
                    "missingChapters": 0,
                    "pages": 0,
                    "error": str(exc),
                }
            )
    return {
        "baseUrl": base_url,
        "totalComics": len(urls),
        "totalPages": total_pages,
        "missingChapters": total_missing_chapters,
        "results": results,
    }


def batch_worker(payload: dict) -> None:
    base_url = payload_base_url(payload)
    output_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    base_payload = {
        "baseUrl": base_url,
        "output": str(output_dir),
        "cookie": str(payload.get("cookie") or "").strip(),
        "maxChapters": 0,
        "downloadAll": True,
        "imageDelay": payload.get("imageDelay") or 1,
        "chapterDelay": payload.get("chapterDelay") or 1,
        "singleChapter": False,
    }
    results: list[dict] = []
    final_status = "done"
    try:
        index = 0
        while True:
            with BATCH.lock:
                urls = list(BATCH.urls)
            if index >= len(urls):
                break
            url = urls[index]
            if batch_stopped():
                final_status = "stopped"
                break
            with BATCH.lock:
                BATCH.current_index = index
            comic_payload = {**base_payload, "url": url, "selectedChapterIds": []}
            batch_append_log(f"\n===== [{index + 1}/{len(urls)}] 开始下载: {url} =====")
            history_id = create_download_history(url=url, base_url=base_url, task_type="batch", status="running", message="批量下载中")
            try:
                initial_missing, title = missing_chapters_for_url(comic_payload)
                remember_comic(base_url, url, title)
                total = len(initial_missing)
                downloaded = 0
                try:
                    chapters_payload = fetch_chapters(comic_payload)
                    total = int(chapters_payload.get("total") or total)
                    downloaded = int(chapters_payload.get("completed") or 0)
                except Exception:
                    pass
                if not initial_missing:
                    status = "done"
                    error = "已完整，跳过下载"
                    missing = []
                    returncode = 0
                    update_download_history(history_id, status=status, title=title, total=total, downloaded=downloaded, missing=0, message=error)
                    results.append(batch_result_for_url(url, status, title, missing, error))
                    with BATCH.lock:
                        BATCH.results = results
                    index += 1
                    continue
                batch_append_log(f"检测到未下载章节 {len(initial_missing)} 个，将只下载缺失章节")
                comic_payload = {**comic_payload, "selectedChapterIds": [item["id"] for item in initial_missing]}
                command, _, _ = build_command(comic_payload)
                returncode, account = run_command_with_account_rotation(
                    command,
                    output_dir,
                    comic_payload,
                    base_url,
                    batch_append_log,
                    "batch",
                    stopped_func=batch_stopped,
                )
                missing, title = missing_chapters_for_url({**comic_payload, "selectedChapterIds": []})
                remember_comic(base_url, url, title)
                if missing and not batch_stopped():
                    ids = [item["id"] for item in missing]
                    batch_append_log(f"回测发现未完成章节 {len(ids)} 个，重试一次: {', '.join(ids)}")
                    retry_payload = {**comic_payload, "selectedChapterIds": ids}
                    retry_command, _, _ = build_command(retry_payload)
                    retry_code, account = run_command_with_account_rotation(
                        retry_command,
                        output_dir,
                        retry_payload,
                        base_url,
                        batch_append_log,
                        "batch",
                        stopped_func=batch_stopped,
                    )
                    missing, title = missing_chapters_for_url({**comic_payload, "selectedChapterIds": []})
                    if missing:
                        status = "missing"
                        error = f"重试后仍缺 {len(missing)} 章"
                    else:
                        status = "retried"
                        error = "" if retry_code == 0 else f"重试进程退出代码 {retry_code}"
                elif missing:
                    status = "missing"
                    error = f"仍缺 {len(missing)} 章"
                elif returncode == 0:
                    status = "done"
                    error = ""
                else:
                    status = "failed"
                    error = f"下载进程退出代码 {returncode}"
                try:
                    chapters_payload = fetch_chapters({**comic_payload, "selectedChapterIds": []})
                    total = int(chapters_payload.get("total") or total)
                    downloaded = int(chapters_payload.get("completed") or downloaded)
                except Exception:
                    downloaded = max(total - len(missing), 0)
                message = error or {"done": "完成", "retried": "重试后完成", "missing": "仍缺章节", "failed": "下载失败"}.get(status, status)
                update_download_history(history_id, status=status, title=title, total=total, downloaded=downloaded, missing=len(missing), message=message)
                results.append(batch_result_for_url(url, status, title, missing, error))
            except Exception as exc:
                final_status = "failed" if final_status != "stopped" else final_status
                batch_append_log(f"任务失败: {exc}")
                update_download_history(history_id, status="failed", message=str(exc))
                results.append(batch_result_for_url(url, "failed", url, [], str(exc)))
            with BATCH.lock:
                BATCH.results = results
            index += 1

        if final_status != "stopped":
            batch_append_log("\n===== 全部漫画下载结束，开始最终检查 =====")
            checked_results = []
            for result in results:
                check_payload = {**base_payload, "url": result["url"], "selectedChapterIds": []}
                try:
                    missing, title = missing_chapters_for_url(check_payload)
                    if missing:
                        checked_results.append({**result, "title": title, "status": "missing", "missing": missing, "error": f"最终检查仍缺 {len(missing)} 章"})
                    elif result["status"] == "missing":
                        checked_results.append({**result, "title": title, "status": "done", "missing": [], "error": ""})
                    else:
                        checked_results.append({**result, "title": title, "missing": []})
                except Exception as exc:
                    checked_results.append({**result, "status": "failed", "error": f"最终检查失败: {exc}"})
            results = checked_results
            with BATCH.lock:
                BATCH.results = results
            if any(result["status"] in {"failed", "missing"} for result in results):
                final_status = "failed"
    finally:
        if payload.get("autoCopyCbz") and final_status != "stopped":
            auto_copy_recent_cbz(output_dir, batch_append_log)
        with BATCH.lock:
            BATCH.status = final_status
            BATCH.finished_at = time.time()
            BATCH.process = None
        batch_append_log(f"批量任务结束: {final_status}")


def start_batch(payload: dict) -> dict:
    urls = batch_urls(payload)
    with JOB.lock:
        if JOB.process is not None and JOB.process.poll() is None:
            raise RuntimeError("单个下载正在运行")
    with BATCH.lock:
        if BATCH.status == "running":
            raise RuntimeError("批量下载正在运行")
        BATCH.status = "running"
        BATCH.started_at = time.time()
        BATCH.finished_at = None
        BATCH.output_dir = str(safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT)))
        BATCH.base_url = payload_base_url(payload)
        BATCH.urls = urls
        BATCH.current_index = -1
        BATCH.results = []
        BATCH.logs = []
        BATCH.stop_requested = False
        BATCH.process = None
    threading.Thread(target=batch_worker, args=(payload,), daemon=True).start()
    return BATCH.snapshot()


def append_batch(payload: dict) -> dict:
    urls = batch_urls(payload)
    with BATCH.lock:
        if BATCH.status != "running":
            raise RuntimeError("批量下载未运行")
        existing = set(BATCH.urls)
        appended = [url for url in urls if url not in existing]
        BATCH.urls.extend(appended)
        total = len(BATCH.urls)
    if appended:
        batch_append_log(f"已追加 {len(appended)} 个漫画到队列，当前共 {total} 个")
    else:
        batch_append_log("没有追加新链接，输入链接已在队列中")
    return BATCH.snapshot()


def stop_batch() -> dict:
    with BATCH.lock:
        BATCH.stop_requested = True
        process = BATCH.process
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            process.terminate()
    batch_append_log("已请求停止批量任务")
    return BATCH.snapshot()


def stop_job() -> dict:
    with JOB.lock:
        process = JOB.process
        if process is None or process.poll() is not None:
            if JOB.status == "running":
                JOB.status = "stopping"
            return JOB.snapshot()
        JOB.status = "stopping"

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()

    JOB.append_log("已发送停止信号")
    return JOB.snapshot()


def existing_directory(value: str) -> Path:
    path = safe_output_dir(value)
    while not path.exists() and path.parent != path:
        path = path.parent
    if path.is_file():
        path = path.parent
    if not path.exists() or not path.is_dir():
        return ROOT
    return path


def choose_directory(value: str) -> dict[str, str]:
    initial_dir = existing_directory(value or DEFAULT_OUTPUT)
    if sys.platform == "darwin":
        script = (
            'POSIX path of (choose folder with prompt "选择输出目录" '
            f'default location POSIX file {json.dumps(str(initial_dir))})'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            selected = result.stdout.strip()
            if selected:
                return {"path": selected.rstrip("/") or "/"}
        if "User canceled" in result.stderr:
            raise RuntimeError("已取消选择")
        raise RuntimeError((result.stderr or "系统目录选择失败").strip())

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=str(initial_dir), title="选择输出目录")
        root.destroy()
    except Exception as exc:
        raise RuntimeError(f"系统目录选择失败: {exc}") from exc
    if not selected:
        raise RuntimeError("已取消选择")
    return {"path": selected}


def is_cbz_directory(path: Path) -> bool:
    return path.name == "panels_cbz" or path.name.endswith("_panels_cbz")


def is_chapter_directory(path: Path) -> bool:
    if not path.is_dir() or is_cbz_directory(path):
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    return any(child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES for child in children)


def file_node(path: Path, output_dir: Path) -> dict | None:
    rel = path.relative_to(output_dir) if path != output_dir else Path(".")
    if path != output_dir and path.is_file():
        if path.suffix.lower() == ".cbz":
            return {"kind": "cbz", "name": path.name, "path": str(rel), "href": "/file?path=" + quote(str(path)), "children": []}
        return None
    if path != output_dir and path.is_dir() and not is_cbz_directory(path) and is_chapter_directory(path):
        return {"kind": "chapter", "name": path.name, "path": str(rel), "children": []}

    children = []
    if path.is_dir():
        try:
            child_paths = sorted(path.iterdir(), key=lambda child: (not child.is_dir(), child.name.lower()))
        except OSError:
            child_paths = []
        for child in child_paths:
            node = file_node(child, output_dir)
            if node:
                children.append(node)

    if path == output_dir:
        return {"kind": "root", "name": path.name or str(path), "path": str(path), "children": children}
    if is_cbz_directory(path):
        return {"kind": "cbz_dir", "name": path.name, "path": str(rel), "children": children}
    if children:
        kind = "comic" if path.parent == output_dir else "folder"
        return {"kind": kind, "name": path.name, "path": str(rel), "children": children}
    return None


def count_file_tree(node: dict) -> dict[str, int]:
    counts = {"chapters": 0, "cbz": 0}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.get("kind") == "chapter":
            counts["chapters"] += 1
        if current.get("kind") == "cbz":
            counts["cbz"] += 1
        stack.extend(current.get("children") or [])
    return counts


def files_payload(output: str) -> dict:
    output_dir = safe_output_dir(output)
    if not output_dir.exists() or not output_dir.is_dir():
        return {
            "tree": {"kind": "root", "name": output or DEFAULT_OUTPUT, "path": str(output_dir), "children": []},
            "summary": {"chapters": 0, "cbz": 0},
        }
    tree = file_node(output_dir, output_dir) or {"kind": "root", "name": output_dir.name, "path": str(output_dir), "children": []}
    return {"tree": tree, "summary": count_file_tree(tree)}


def copy_cbz_files(payload: dict) -> dict:
    source_dir = safe_output_dir(str(payload.get("output") or DEFAULT_OUTPUT))
    target_dir = Path(str(payload.get("target") or DEFAULT_CBZ_COPY_TARGET)).expanduser().resolve()
    if not source_dir.exists() or not source_dir.is_dir():
        raise RuntimeError("输出目录不存在")
    if not str(payload.get("target") or DEFAULT_CBZ_COPY_TARGET).strip():
        raise RuntimeError("未配置 CBZ 复制目录，请在 .komiic_local_paths.json 设置 cbzCopyTarget")
    if not target_dir.exists() or not target_dir.is_dir():
        raise RuntimeError("目标目录不存在")
    config = load_config()
    copy_since = float(payload.get("since") if payload.get("since") is not None else config.get("cbzCopySince") or 0)
    copied = 0
    skipped = 0
    for cbz_path in sorted(source_dir.rglob("*.cbz")):
        if not cbz_path.is_file():
            continue
        if copy_since and cbz_path.stat().st_mtime <= copy_since:
            skipped += 1
            continue
        destination = target_dir / cbz_path.name
        if destination.exists() and destination.resolve() != cbz_path.resolve():
            if destination.stat().st_size == cbz_path.stat().st_size:
                skipped += 1
                continue
            stem = cbz_path.stem
            suffix = cbz_path.suffix
            index = 2
            while destination.exists():
                destination = target_dir / f"{stem} ({index}){suffix}"
                index += 1
        shutil.copy2(cbz_path, destination)
        copied += 1
    config["cbzCopySince"] = time.time()
    save_config(config)
    return {"copied": copied, "skipped": skipped, "target": str(target_dir)}


def auto_copy_recent_cbz(output_dir: Path, log_func) -> None:
    try:
        result = copy_cbz_files({"output": str(output_dir)})
        log_func(f"已复制新增 CBZ {result.get('copied', 0)} 个到 {result.get('target')}")
    except Exception as exc:
        log_func(f"复制新增 CBZ 失败: {exc}")


class Handler(BaseHTTPRequestHandler):
    server_version = "KomiicGui/1.0"

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html_body = (
                INDEX_HTML
                .replace("__DEFAULT_OUTPUT_VALUE__", html.escape(DEFAULT_OUTPUT, quote=True))
                .replace("__DEFAULT_OUTPUT_JSON__", json.dumps(DEFAULT_OUTPUT, ensure_ascii=False))
            )
            body = html_body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/state":
            json_response(self, HTTPStatus.OK, JOB.snapshot())
            return

        if parsed.path == "/api/batch/state":
            json_response(self, HTTPStatus.OK, BATCH.snapshot())
            return

        if parsed.path == "/api/cookie/playwright/state":
            json_response(self, HTTPStatus.OK, COOKIE_LOGIN.snapshot())
            return

        if parsed.path == "/api/config":
            base_url = parse_qs(parsed.query).get("baseUrl", [""])[0]
            json_response(self, HTTPStatus.OK, config_payload(base_url))
            return

        if parsed.path == "/api/accounts":
            base_url = parse_qs(parsed.query).get("baseUrl", [""])[0]
            json_response(self, HTTPStatus.OK, account_pool_payload(base_url))
            return

        if parsed.path == "/api/quota/history":
            base_url = parse_qs(parsed.query).get("baseUrl", [""])[0]
            json_response(self, HTTPStatus.OK, quota_history_payload(base_url))
            return

        if parsed.path == "/api/history":
            json_response(self, HTTPStatus.OK, history_payload())
            return

        if parsed.path == "/api/directories":
            path = parse_qs(parsed.query).get("path", [""])[0]
            json_response(self, HTTPStatus.OK, directory_payload(path))
            return

        if parsed.path == "/api/files":
            output = parse_qs(parsed.query).get("output", [""])[0]
            json_response(self, HTTPStatus.OK, files_payload(output))
            return

        if parsed.path == "/file":
            raw_path = parse_qs(parsed.query).get("path", [""])[0]
            path = Path(raw_path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.end_headers()
            with path.open("rb") as file:
                self.wfile.write(file.read())
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                json_response(self, HTTPStatus.OK, start_job(read_json(self)))
                return
            if parsed.path == "/api/chapters":
                json_response(self, HTTPStatus.OK, fetch_chapters(read_json(self)))
                return
            if parsed.path == "/api/quota":
                json_response(self, HTTPStatus.OK, fetch_quota(read_json(self)))
                return
            if parsed.path == "/api/quota/poll":
                json_response(self, HTTPStatus.OK, fetch_quota_poll(read_json(self)))
                return
            if parsed.path == "/api/accounts":
                json_response(self, HTTPStatus.OK, update_accounts(read_json(self)))
                return
            if parsed.path == "/api/accounts/add-current":
                json_response(self, HTTPStatus.OK, add_current_cookie_to_accounts(read_json(self)))
                return
            if parsed.path == "/api/accounts/select":
                json_response(self, HTTPStatus.OK, select_account(read_json(self)))
                return
            if parsed.path == "/api/accounts/delete":
                json_response(self, HTTPStatus.OK, delete_account(read_json(self)))
                return
            if parsed.path == "/api/accounts/toggle":
                json_response(self, HTTPStatus.OK, toggle_account(read_json(self)))
                return
            if parsed.path == "/api/accounts/rename":
                json_response(self, HTTPStatus.OK, rename_account(read_json(self)))
                return
            if parsed.path == "/api/accounts/update-cookie":
                json_response(self, HTTPStatus.OK, update_account_cookie(read_json(self)))
                return
            if parsed.path == "/api/accounts/refresh-cookie":
                json_response(self, HTTPStatus.OK, refresh_account_cookie(read_json(self)))
                return
            if parsed.path == "/api/batch/start":
                json_response(self, HTTPStatus.OK, start_batch(read_json(self)))
                return
            if parsed.path == "/api/batch/append":
                json_response(self, HTTPStatus.OK, append_batch(read_json(self)))
                return
            if parsed.path == "/api/batch/estimate":
                json_response(self, HTTPStatus.OK, estimate_batch(read_json(self)))
                return
            if parsed.path == "/api/batch/stop":
                json_response(self, HTTPStatus.OK, stop_batch())
                return
            if parsed.path == "/api/cookie/playwright/start":
                payload = read_json(self)
                json_response(self, HTTPStatus.OK, start_cookie_login(str(payload.get("baseUrl") or DEFAULT_BASE_URL)))
                return
            if parsed.path == "/api/cookie/playwright":
                payload = read_json(self)
                json_response(self, HTTPStatus.OK, fetch_cookie_with_playwright(str(payload.get("baseUrl") or DEFAULT_BASE_URL)))
                return
            if parsed.path == "/api/history/delete":
                payload = read_json(self)
                json_response(
                    self,
                    HTTPStatus.OK,
                    delete_comic_history(str(payload.get("baseUrl") or DEFAULT_BASE_URL), str(payload.get("comicId") or "")),
                )
                return
            if parsed.path == "/api/history/follow":
                json_response(self, HTTPStatus.OK, toggle_follow_comic(read_json(self)))
                return
            if parsed.path == "/api/choose-directory":
                payload = read_json(self)
                json_response(self, HTTPStatus.OK, choose_directory(str(payload.get("path") or DEFAULT_OUTPUT)))
                return
            if parsed.path == "/api/files/copy-cbz":
                json_response(self, HTTPStatus.OK, copy_cbz_files(read_json(self)))
                return
            if parsed.path == "/api/stop":
                json_response(self, HTTPStatus.OK, stop_job())
                return
        except Exception as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    host = os.environ.get("HOST", DEFAULT_HOST)
    threading.Thread(target=quota_monitor_loop, daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GUI 已启动: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_job()
        server.server_close()


if __name__ == "__main__":
    main()

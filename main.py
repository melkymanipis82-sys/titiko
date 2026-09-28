# =========================================================
# AUTO-INSTALL REQUIRED PACKAGES
# =========================================================
import sys
import subprocess
import importlib
import os

_REQUIRED_PACKAGES = {
    "requests": "requests>=2.32,<3",
    "aiohttp": "aiohttp>=3.10,<4",
}

def _missing_packages():
    missing = []
    for module_name, package_spec in _REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
        except Exception:
            missing.append(package_spec)
    return missing

def _auto_install_dependencies():
    missing = _missing_packages()
    if not missing:
        return

    names = ", ".join(x.split(">=")[0].split("<")[0] for x in missing)
    print(f"\n[SETUP] Missing package(s): {names}")
    print("[SETUP] Installing automatically...\n")

    env = os.environ.copy()
    env["PIP_NO_CACHE_DIR"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    command = [sys.executable, "-m", "pip", "install", "--no-cache-dir", *missing]
    try:
        subprocess.check_call(command, env=env)
    except Exception as exc:
        print("[SETUP] Automatic installation failed:", exc)
        print(f"[SETUP] Run manually: {sys.executable} -m pip install --no-cache-dir {' '.join(missing)}")
        raise

    # Verify after installation so a broken environment is reported clearly.
    still_missing = _missing_packages()
    if still_missing:
        raise RuntimeError("Package installation completed but import still failed: " + ", ".join(still_missing))

    print("\n[SETUP] Required packages are ready.\n")

_auto_install_dependencies()

# Third-party imports come AFTER the auto-installer.
import requests
import asyncio
import aiohttp
import concurrent.futures
import re
import json
import html
import time
import threading
from datetime import datetime, timedelta


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = "8595947459:AAGlyjg4lYbmBFUah7w5oPUB7kET_aHNbfI"

# Main admin Telegram user/chat ID
ALLOWED_CHAT_ID = 6656858850

DATA_FILE = "facebook_uid_monitor.json"

SCAN_INTERVAL = 300

REQUEST_TIMEOUT = 8

# Async checker performance settings.
# One shared aiohttp.ClientSession is used by all worker tasks.
CHECK_WORKERS = 10
CHECK_QUEUE_MAXSIZE = 500

PAGE_SIZE = 10

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

session = requests.Session()

# Temporary button-based info editing state
# user_id -> {
#     "group": "...",
#     "uid": "...",
#     "field": "issue/amount/owner/details"
# }
PENDING_EDITS = {}

# Lock for DATA modifications
DATA_LOCK = threading.Lock()


# =========================================================
# DATA
# =========================================================

def load_data():

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, dict):
            data = {}

        data.setdefault("recovery", [])
        data.setdefault("deletion", [])

        # -------------------------------------------------
        # Allowed users
        # -------------------------------------------------

        data.setdefault(
            "allowed_users",
            []
        )

        if not isinstance(
            data["allowed_users"],
            list
        ):

            data["allowed_users"] = []

        # Main admin should not need to be duplicated
        # inside allowed_users.
        cleaned_allowed = []

        for user_id in data["allowed_users"]:

            try:
                user_id = int(user_id)

                if user_id != ALLOWED_CHAT_ID:
                    cleaned_allowed.append(user_id)

            except Exception:
                continue

        data["allowed_users"] = list(
            dict.fromkeys(cleaned_allowed)
        )

        # -------------------------------------------------
        # Daily report data
        # -------------------------------------------------

        data.setdefault(
            "daily_report",
            {
                "date": datetime.now().strftime(
                    "%Y-%m-%d"
                ),
                "changes": 0,
                "last_sent_date": ""
            }
        )

        if not isinstance(
            data["daily_report"],
            dict
        ):

            data["daily_report"] = {
                "date": datetime.now().strftime(
                    "%Y-%m-%d"
                ),
                "changes": 0,
                "last_sent_date": ""
            }

        data["daily_report"].setdefault(
            "date",
            datetime.now().strftime("%Y-%m-%d")
        )

        data["daily_report"].setdefault(
            "changes",
            0
        )

        data["daily_report"].setdefault(
            "last_sent_date",
            ""
        )

        # -------------------------------------------------
        # Existing item compatibility
        # -------------------------------------------------

        for group in [
            "recovery",
            "deletion"
        ]:

            if not isinstance(
                data.get(group),
                list
            ):

                data[group] = []

            for item in data[group]:

                if not isinstance(
                    item,
                    dict
                ):
                    continue

                item.setdefault(
                    "uid",
                    ""
                )

                item.setdefault(
                    "status",
                    "UNKNOWN"
                )

                item.setdefault(
                    "previous_status",
                    item.get(
                        "status",
                        "UNKNOWN"
                    )
                )

                item.setdefault(
                    "name",
                    ""
                )

                item.setdefault(
                    "graph_picture",
                    False
                )

                item.setdefault(
                    "checked_at",
                    ""
                )

                item.setdefault(
                    "status_changed_at",
                    ""
                )

                item.setdefault(
                    "issue",
                    ""
                )

                item.setdefault(
                    "amount",
                    ""
                )

                item.setdefault(
                    "owner",
                    ""
                )

                item.setdefault(
                    "details",
                    ""
                )

        return data

    except (
        json.JSONDecodeError,
        FileNotFoundError
    ):

        print(
            "Creating fresh data."
        )

        return {
            "recovery": [],
            "deletion": [],
            "allowed_users": [],
            "daily_report": {
                "date": datetime.now().strftime(
                    "%Y-%m-%d"
                ),
                "changes": 0,
                "last_sent_date": ""
            }
        }

    except Exception as e:

        print(
            "Load data error:",
            e
        )

        return {
            "recovery": [],
            "deletion": [],
            "allowed_users": [],
            "daily_report": {
                "date": datetime.now().strftime(
                    "%Y-%m-%d"
                ),
                "changes": 0,
                "last_sent_date": ""
            }
        }


def save_data(data):

    try:

        with DATA_LOCK:

            with open(
                DATA_FILE,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    data,
                    f,
                    indent=2,
                    ensure_ascii=False
                )

        return True

    except Exception as e:

        print(
            "Save error:",
            e
        )

        return False


DATA = load_data()


# =========================================================
# ASYNC CHECKER ENGINE
# =========================================================
#
# Performance path used by /scan, /addrecovery
# and /adddeletion:
#   - asyncio.Queue for check jobs
#   - one shared aiohttp.ClientSession
#   - multiple worker() tasks consuming the queue
#
# The Telegram bot remains synchronous, so the async engine
# runs on its own event-loop thread and can be used safely
# by the existing command handlers.

CHECK_LOOP = None
CHECK_SESSION = None
CHECK_WORKER_TASKS = []
CHECK_ENGINE_READY = threading.Event()
CHECK_ENGINE_LOCK = threading.Lock()


def _async_headers(user_agent):
    return {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }


async def async_get_public_name(uid):
    """Fetch public Facebook metadata using the shared aiohttp session."""
    uid = normalize_uid(uid)

    if not uid or CHECK_SESSION is None:
        return ""

    url = (
        f"https://www.facebook.com/"
        f"profile.php?id={uid}"
    )

    user_agents = [
        "facebookexternalhit/1.1",
        "TelegramBot (like TwitterBot)",
        (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Mobile Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
    ]

    for ua in user_agents:
        try:
            async with CHECK_SESSION.get(
                url,
                headers=_async_headers(ua),
                allow_redirects=True
            ) as response:
                html_text = await response.text(errors="ignore")

            if not html_text:
                continue

            name = clean_profile_name(
                get_meta(html_text, "og:title")
            )
            if name:
                return name

            name = clean_profile_name(
                get_meta(html_text, "twitter:title")
            )
            if name:
                return name

            name = clean_profile_name(
                get_title_tag(html_text)
            )
            if name:
                return name

            name = clean_profile_name(
                get_meta(html_text, "title")
            )
            if name:
                return name

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            print(f"Async metadata error for {uid}: {e}")
            continue
        except Exception as e:
            print(f"Async metadata error for {uid}: {e}")
            continue

    return ""


async def async_graph_picture_check(uid):
    """Check the Graph picture endpoint using the shared session."""
    uid = normalize_uid(uid)

    if not uid or CHECK_SESSION is None:
        return None

    url = (
        f"https://graph.facebook.com/"
        f"{uid}/picture?redirect=false"
    )

    try:
        async with CHECK_SESSION.get(
            url,
            allow_redirects=False
        ) as response:
            data = await response.text(errors="ignore")

        if response.status not in (200, 400):
            return None

        if (
            "error" in data.lower()
            or ".gif" in data.lower()
        ):
            return False

        return True

    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        print(f"Async graph error for {uid}: {e}")
        return None
    except Exception as e:
        print(f"Async graph error for {uid}: {e}")
        return None


async def async_check_uid(uid):
    """
    Actual async UID check.

    Graph and public metadata are requested concurrently for
    each UID, while the worker pool provides concurrency across
    many UIDs.
    """
    uid = normalize_uid(uid)

    checked_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if not uid:
        return {
            "uid": uid,
            "status": "DEAD",
            "name": "",
            "graph_picture": False,
            "checked_at": checked_at
        }

    print(f"Checking UID: {uid}")

    graph_exists, name = await asyncio.gather(
        async_graph_picture_check(uid),
        async_get_public_name(uid),
        return_exceptions=True
    )

    if isinstance(graph_exists, Exception):
        graph_exists = None

    if isinstance(name, Exception):
        name = ""

    if graph_exists is False:
        status = "DEAD"
    else:
        status = "ALIVE" if name else "DEAD"

    result = {
        "uid": uid,
        "status": status,
        "name": name,
        "graph_picture": (
            True
            if graph_exists is True
            else False
        ),
        "checked_at": checked_at
    }

    print(
        f"UID: {uid} | "
        f"Status: {status} | "
        f"Name: {name or 'Not available'} | "
        f"Graph: {graph_exists}"
    )

    return result


async def worker(worker_id):
    """Consume UID check jobs from the shared asyncio.Queue."""
    while True:
        job = await CHECK_QUEUE.get()

        try:
            uid, future = job
            result = await async_check_uid(uid)

            if not future.done():
                future.set_result(result)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            if "future" in locals() and not future.done():
                future.set_exception(e)

            print(
                f"Checker worker {worker_id} error: {e}"
            )

        finally:
            CHECK_QUEUE.task_done()


async def _start_async_checker():
    global CHECK_SESSION
    global CHECK_WORKER_TASKS

    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT
    )

    connector = aiohttp.TCPConnector(
        limit=max(CHECK_WORKERS * 2, 20),
        limit_per_host=max(CHECK_WORKERS, 10),
        ttl_dns_cache=300,
        enable_cleanup_closed=True
    )

    CHECK_SESSION = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    )

    CHECK_WORKER_TASKS = [
        asyncio.create_task(
            worker(index + 1)
        )
        for index in range(CHECK_WORKERS)
    ]

    CHECK_ENGINE_READY.set()

    print(
        f"Async checker ready: "
        f"{CHECK_WORKERS} workers, "
        f"queue maxsize={CHECK_QUEUE_MAXSIZE}"
    )

    await asyncio.gather(
        *CHECK_WORKER_TASKS
    )


async def _stop_async_checker():
    global CHECK_SESSION

    for task in CHECK_WORKER_TASKS:
        task.cancel()

    if CHECK_WORKER_TASKS:
        await asyncio.gather(
            *CHECK_WORKER_TASKS,
            return_exceptions=True
        )

    if CHECK_SESSION is not None:
        await CHECK_SESSION.close()
        CHECK_SESSION = None


def _checker_loop_runner():
    global CHECK_LOOP

    CHECK_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(CHECK_LOOP)

    try:
        CHECK_LOOP.run_until_complete(
            _start_async_checker()
        )
    except Exception as e:
        print(
            "Async checker engine stopped:",
            e
        )
        CHECK_ENGINE_READY.clear()
    finally:
        if CHECK_LOOP is not None:
            try:
                CHECK_LOOP.run_until_complete(
                    _stop_async_checker()
                )
            except Exception:
                pass

        CHECK_LOOP.close()
        CHECK_LOOP = None


def start_async_checker():
    """Start the shared async queue/session/worker engine once."""
    with CHECK_ENGINE_LOCK:
        if (
            CHECK_LOOP is not None
            and CHECK_ENGINE_READY.is_set()
        ):
            return

        thread = threading.Thread(
            target=_checker_loop_runner,
            name="async-checker-engine",
            daemon=True
        )
        thread.start()

    if not CHECK_ENGINE_READY.wait(timeout=10):
        raise RuntimeError(
            "Async checker engine failed to start."
        )


async def _enqueue_checks(uids):
    """
    Enqueue a batch and wait for all workers.

    Every job gets its own asyncio Future. Multiple workers
    process the queue concurrently while sharing one session.
    """
    futures = []

    for uid in uids:
        future = asyncio.get_running_loop().create_future()
        futures.append(future)
        await CHECK_QUEUE.put((uid, future))

    return await asyncio.gather(
        *futures,
        return_exceptions=True
    )


def check_uids_fast(uids):
    """
    Synchronous bridge for the existing bot code.

    The bot handlers can stay unchanged while the actual
    network work runs through asyncio.Queue + worker tasks.
    """
    normalized = [
        normalize_uid(uid)
        for uid in uids
    ]
    normalized = [
        uid for uid in normalized
        if uid
    ]

    if not normalized:
        return []

    try:
        start_async_checker()

        future = asyncio.run_coroutine_threadsafe(
            _enqueue_checks(normalized),
            CHECK_LOOP
        )

        return future.result(
            timeout=max(
                30,
                REQUEST_TIMEOUT
                * max(4, len(normalized))
            )
        )

    except Exception as e:
        print(
            "Fast checker error, "
            "falling back to legacy checker:",
            e
        )

        return [
            check_uid(uid)
            for uid in normalized
        ]


def check_uid_fast(uid):
    """Single-UID convenience wrapper for commands."""
    results = check_uids_fast([uid])

    if results:
        result = results[0]
        if isinstance(result, Exception):
            raise result
        return result

    return check_uid(uid)


# =========================================================
# UID
# =========================================================

def normalize_uid(uid):

    if not uid:
        return ""

    uid = str(uid).strip()

    match = re.search(
        r"(?:id=|profile\.php\?id=)?(\d{5,})",
        uid
    )

    if match:
        return match.group(1)

    if uid.isdigit():
        return uid

    return ""


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(text):

    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<[^>]+>",
        "",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def get_meta(
    html_text,
    property_name
):

    if not html_text:
        return ""

    pattern1 = (
        rf'<meta[^>]+'
        rf'(?:property|name)=["\']'
        rf'{re.escape(property_name)}'
        rf'["\'][^>]+'
        rf'content=["\'](.*?)["\']'
    )

    pattern2 = (
        rf'<meta[^>]+'
        rf'content=["\'](.*?)["\'][^>]+'
        rf'(?:property|name)=["\']'
        rf'{re.escape(property_name)}'
        rf'["\']'
    )

    match = re.search(
        pattern1,
        html_text,
        re.I | re.S
    )

    if not match:

        match = re.search(
            pattern2,
            html_text,
            re.I | re.S
        )

    if match:

        return clean_text(
            match.group(1)
        )

    return ""


def get_title_tag(html_text):

    if not html_text:
        return ""

    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html_text,
        re.I | re.S
    )

    if match:

        return clean_text(
            match.group(1)
        )

    return ""


def clean_profile_name(name):

    if not name:
        return ""

    name = clean_text(name)

    name = re.sub(
        r"\s*[-|]\s*Facebook.*$",
        "",
        name,
        flags=re.I
    )

    name = re.sub(
        r"\s*-\s*Log In.*$",
        "",
        name,
        flags=re.I
    )

    name = name.strip()

    invalid_names = {
        "facebook",
        "log in",
        "login",
        "log in or sign up",
        "log into facebook",
        "facebook - log in or sign up",
        "facebook log in",
        "facebook login"
    }

    if name.lower() in invalid_names:
        return ""

    if len(name) < 2:
        return ""

    return name


# =========================================================
# FACEBOOK PUBLIC NAME
# =========================================================

def get_public_name(uid):

    uid = normalize_uid(uid)

    if not uid:
        return ""

    url = (
        f"https://www.facebook.com/"
        f"profile.php?id={uid}"
    )

    user_agents = [

        "facebookexternalhit/1.1",

        "TelegramBot (like TwitterBot)",

        (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Mobile Safari/537.36"
        ),

        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
    ]

    for ua in user_agents:

        headers = {
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,"
                "image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }

        try:

            response = session.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True
            )

            html_text = response.text or ""

            if not html_text:
                continue

            name = clean_profile_name(
                get_meta(
                    html_text,
                    "og:title"
                )
            )

            if name:
                return name

            name = clean_profile_name(
                get_meta(
                    html_text,
                    "twitter:title"
                )
            )

            if name:
                return name

            name = clean_profile_name(
                get_title_tag(
                    html_text
                )
            )

            if name:
                return name

            name = clean_profile_name(
                get_meta(
                    html_text,
                    "title"
                )
            )

            if name:
                return name

        except requests.Timeout:

            print(
                f"Metadata timeout: {uid}"
            )

            continue

        except requests.RequestException as e:

            print(
                f"Metadata request error "
                f"for {uid}: {e}"
            )

            continue

        except Exception as e:

            print(
                f"Metadata error "
                f"for {uid}: {e}"
            )

            continue

    return ""


# =========================================================
# GRAPH PICTURE CHECK
# =========================================================

def graph_picture_check(uid):

    uid = normalize_uid(uid)

    if not uid:
        return None

    url = (
        f"https://graph.facebook.com/"
        f"{uid}/picture?redirect=false"
    )

    try:

        r = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False
        )

        data = r.text or ""

        if r.status_code not in (
            200,
            400
        ):

            return None

        if (
            "error" in data.lower()
            or ".gif" in data.lower()
        ):

            return False

        return True

    except requests.Timeout:

        print(
            f"Graph timeout: {uid}"
        )

        return None

    except requests.RequestException as e:

        print(
            f"Graph request error "
            f"for {uid}: {e}"
        )

        return None

    except Exception as e:

        print(
            f"Graph error "
            f"for {uid}: {e}"
        )

        return None


# =========================================================
# CHECK UID
# =========================================================

def check_uid(uid):

    uid = normalize_uid(uid)

    checked_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if not uid:

        return {
            "uid": uid,
            "status": "DEAD",
            "name": "",
            "graph_picture": False,
            "checked_at": checked_at
        }

    print(
        f"Checking UID: {uid}"
    )

    graph_exists = graph_picture_check(
        uid
    )

    if graph_exists is False:

        print(
            f"UID: {uid} | "
            f"Status: DEAD | "
            f"Reason: Graph error/.gif"
        )

        return {
            "uid": uid,
            "status": "DEAD",
            "name": "",
            "graph_picture": False,
            "checked_at": checked_at
        }

    name = get_public_name(uid)

    if name:

        status = "ALIVE"

    else:

        status = "DEAD"

    result = {
        "uid": uid,
        "status": status,
        "name": name,
        "graph_picture": (
            True
            if graph_exists is True
            else False
        ),
        "checked_at": checked_at
    }

    print(
        f"UID: {uid} | "
        f"Status: {status} | "
        f"Name: {name or 'Not available'} | "
        f"Graph: {graph_exists}"
    )

    return result


# =========================================================
# DAILY CHANGE COUNTER
# =========================================================

def prepare_daily_counter():

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    daily = DATA.setdefault(
        "daily_report",
        {}
    )

    current_date = daily.get(
        "date",
        ""
    )

    if current_date != today:

        daily["date"] = today
        daily["changes"] = 0

        # Do not reset last_sent_date here.
        # It is used by the daily report thread.

        save_data(DATA)


def increment_daily_changes():

    prepare_daily_counter()

    DATA["daily_report"]["changes"] = (
        int(
            DATA["daily_report"].get(
                "changes",
                0
            )
        )
        + 1
    )

    save_data(DATA)


# =========================================================
# UPDATE ITEM
# =========================================================

def update_item(
    group,
    uid,
    result,
    count_change=True
):

    uid = normalize_uid(uid)

    if not uid:
        return None

    for item in DATA.get(
        group,
        []
    ):

        if normalize_uid(
            item.get("uid", "")
        ) == uid:

            old_status = item.get(
                "status",
                "UNKNOWN"
            )

            # Existing previous_status is preserved
            # for historical information.
            if "previous_status" not in item:

                item["previous_status"] = (
                    old_status
                )

            new_status = result.get(
                "status",
                "UNKNOWN"
            )

            status_changed = (
                old_status != new_status
                and old_status in [
                    "ALIVE",
                    "DEAD"
                ]
                and new_status in [
                    "ALIVE",
                    "DEAD"
                ]
            )

            if status_changed:

                item["previous_status"] = (
                    old_status
                )

                item["status_changed_at"] = (
                    result.get(
                        "checked_at",
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    )
                )

                if count_change:

                    increment_daily_changes()

            item["uid"] = uid

            item["status"] = new_status

            item["name"] = result.get(
                "name",
                ""
            )

            item["graph_picture"] = result.get(
                "graph_picture",
                False
            )

            item["checked_at"] = result.get(
                "checked_at",
                ""
            )

            return {
                "changed": status_changed,
                "previous_status": old_status,
                "new_status": new_status
            }

    return None


# =========================================================
# STATUS CHANGE ALERT
# =========================================================

def send_status_change_alert(
    group,
    uid,
    name,
    previous_status,
    new_status,
    checked_at
):

    if previous_status == new_status:
        return

    if previous_status not in [
        "ALIVE",
        "DEAD"
    ]:

        return

    if new_status not in [
        "ALIVE",
        "DEAD"
    ]:

        return

    old_label = status_label(
        previous_status
    )

    new_label = status_label(
        new_status
    )

    change_time = checked_at

    if len(change_time) >= 19:

        change_time = change_time[-8:]

    text = (
        "🚨 <b>STATUS CHANGE</b>\n\n"
        f"🆔 <code>{esc(uid)}</code>\n"
    )

    if name:

        text += (
            f"👤 {esc(name)}\n\n"
        )

    else:

        text += "\n"

    text += (
        f"{old_label}\n"
        f"     ↓\n"
        f"{new_label}\n\n"
        f"🕒 {esc(change_time)}\n"
        f"📂 {esc(group.upper())}"
    )

    send(
        ALLOWED_CHAT_ID,
        text
    )


# =========================================================
# APPLY RESULT
# =========================================================

def apply_result(
    group,
    uid,
    result,
    notify=True
):

    change = update_item(
        group,
        uid,
        result,
        count_change=True
    )

    save_data(DATA)

    if (
        notify
        and change
        and change.get("changed")
    ):

        send_status_change_alert(
            group,
            uid,
            result.get(
                "name",
                ""
            ),
            change.get(
                "previous_status",
                ""
            ),
            change.get(
                "new_status",
                ""
            ),
            result.get(
                "checked_at",
                ""
            )
        )

    return change


# =========================================================
# SCAN GROUP
# =========================================================

def scan_group(group):

    items = list(
        DATA.get(group, [])
    )

    uids = [
        normalize_uid(item.get("uid", ""))
        for item in items
    ]

    uids = [
        uid for uid in uids
        if uid
    ]

    if not uids:
        return []

    # Queue the whole group at once. The worker pool handles
    # multiple UID checks concurrently.
    try:
        checked_results = check_uids_fast(uids)
    except Exception as e:
        print(
            f"Parallel scan error for {group}: {e}"
        )
        checked_results = []

    results = []

    # Keep result ordering stable for the bot's scan summary.
    for uid, result in zip(uids, checked_results):

        if isinstance(result, Exception):
            print(
                f"Scan error {group}/{uid}: {result}"
            )

            result = {
                "uid": uid,
                "status": "DEAD",
                "name": "",
                "graph_picture": False,
                "checked_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            }

        try:
            change = apply_result(
                group,
                uid,
                result,
                notify=True
            )

            results.append(
                {
                    "group": group,
                    **result,
                    "status_changed": (
                        bool(change)
                        and change.get(
                            "changed",
                            False
                        )
                    )
                }
            )

        except Exception as e:
            print(
                f"Apply result error "
                f"{group}/{uid}: {e}"
            )

    save_data(DATA)

    return results


# =========================================================
# SCAN ALL
# =========================================================

def scan_all():

    all_results = []

    for group in [
        "recovery",
        "deletion"
    ]:

        try:

            results = scan_group(
                group
            )

            all_results.extend(
                results
            )

        except Exception as e:

            print(
                f"Group scan error "
                f"{group}: {e}"
            )

    return all_results


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_api(
    method,
    params=None
):

    url = (
        f"{TELEGRAM_API}/"
        f"{method}"
    )

    try:

        response = requests.post(
            url,
            json=params or {},
            timeout=30
        )

        try:

            return response.json()

        except Exception:

            return {
                "ok": False,
                "description": response.text
            }

    except requests.RequestException as e:

        print(
            "Telegram API error:",
            e
        )

        return {
            "ok": False,
            "description": str(e)
        }


# =========================================================
# SEND MESSAGE
# =========================================================

def send(
    chat_id,
    text,
    reply_markup=None
):

    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup:

        params["reply_markup"] = (
            reply_markup
        )

    result = telegram_api(
        "sendMessage",
        params
    )

    if not result.get("ok"):

        print(
            "Telegram send error:",
            result
        )

    return result


# =========================================================
# CALLBACK ANSWER
# =========================================================

def answer_callback(
    callback_query_id,
    text=""
):

    return telegram_api(
        "answerCallbackQuery",
        {
            "callback_query_id":
                callback_query_id,
            "text": text,
            "show_alert": False
        }
    )


# =========================================================
# EDIT MESSAGE
# =========================================================

def edit_message(
    chat_id,
    message_id,
    text,
    reply_markup=None
):

    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup:

        params["reply_markup"] = (
            reply_markup
        )

    return telegram_api(
        "editMessageText",
        params
    )


# =========================================================
# ESCAPE
# =========================================================

def esc(text):

    if text is None:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =========================================================
# STATUS
# =========================================================

def status_label(status):

    if status == "ALIVE":
        return "🟢 ALIVE"

    if status == "DEAD":
        return "🔴 DEAD"

    return "⚪ UNKNOWN"


# =========================================================
# PROFILE URL
# =========================================================

def profile_url(uid):

    uid = normalize_uid(uid)

    return (
        f"https://www.facebook.com/"
        f"profile.php?id={uid}"
    )


# =========================================================
# INLINE BUTTONS
# =========================================================

def uid_action_buttons(
    group,
    uid
):

    uid = normalize_uid(uid)

    return {
        "inline_keyboard": [

            [
                {
                    "text": "📝 Update Info",
                    "callback_data":
                        f"update:{group}:{uid}"
                },

                {
                    "text": "📊 List of UIDs",
                    "callback_data":
                        f"list:{group}:1"
                }
            ],

            [
                {
                    "text": "❌ Delete UID",
                    "callback_data":
                        f"delete:{group}:{uid}"
                },

                {
                    "text": "🔗 Open Profile",
                    "url": profile_url(uid)
                }
            ],

            [
                {
                    "text": "👤 View Facebook Profile",
                    "url": profile_url(uid)
                }
            ]
        ]
    }


# =========================================================
# PAGINATION
# =========================================================

def total_pages(group):

    total = len(
        DATA.get(
            group,
            []
        )
    )

    if total == 0:
        return 1

    return (
        total + PAGE_SIZE - 1
    ) // PAGE_SIZE


def get_page_items(
    group,
    page
):

    items = DATA.get(
        group,
        []
    )

    pages = total_pages(
        group
    )

    try:
        page = int(page)

    except Exception:
        page = 1

    page = max(
        1,
        min(page, pages)
    )

    start = (
        page - 1
    ) * PAGE_SIZE

    end = start + PAGE_SIZE

    return (
        items[start:end],
        page,
        pages
    )


def group_list_buttons(
    group,
    page=1
):

    page_items, page, pages = (
        get_page_items(
            group,
            page
        )
    )

    keyboard = []

    start_index = (
        (page - 1)
        * PAGE_SIZE
    )

    for offset, item in enumerate(
        page_items,
        1
    ):

        uid = normalize_uid(
            item.get(
                "uid",
                ""
            )
        )

        if not uid:
            continue

        name = item.get(
            "name",
            ""
        )

        status = item.get(
            "status",
            "UNKNOWN"
        )

        if status == "ALIVE":
            icon = "🟢"

        elif status == "DEAD":
            icon = "🔴"

        else:
            icon = "⚪"

        display_name = (
            name
            if name
            else "UNKNOWN"
        )

        number = (
            start_index
            + offset
        )

        button_text = (
            f"#{number} "
            f"{display_name} "
            f"{icon}"
        )

        keyboard.append(
            [
                {
                    "text": button_text[:60],
                    "callback_data":
                        f"view:{group}:{uid}"
                }
            ]
        )

    # -----------------------------------------------------
    # Pagination buttons
    # -----------------------------------------------------

    navigation = []

    if page > 1:

        navigation.append(
            {
                "text": "◀️ Previous",
                "callback_data":
                    f"page:{group}:{page - 1}"
            }
        )

    if page < pages:

        navigation.append(
            {
                "text": "Next ▶️",
                "callback_data":
                    f"page:{group}:{page + 1}"
            }
        )

    if navigation:

        keyboard.append(
            navigation
        )

    return {
        "inline_keyboard": keyboard
    }


# =========================================================
# FORMAT LIST
# =========================================================

def format_group_list(
    group,
    page=1
):

    items, page, pages = (
        get_page_items(
            group,
            page
        )
    )

    total = len(
        DATA.get(
            group,
            []
        )
    )

    if group == "recovery":

        icon = "♻️"

    else:

        icon = "🗑️"

    text = (
        f"{icon} <b>{esc(group.title())} "
        f"UIDs — Page {page}/{pages}</b>\n\n"
    )

    if not items:

        text += (
            "No UID saved."
        )

        return text

    text += (
        "👇 <b>Click a UID to view "
        "its information:</b>"
    )

    return text


# =========================================================
# FORMAT DETAIL
# =========================================================

def format_detail(
    group,
    item
):

    uid = item.get(
        "uid",
        ""
    )

    status = item.get(
        "status",
        "UNKNOWN"
    )

    name = item.get(
        "name",
        ""
    )

    checked = item.get(
        "checked_at",
        ""
    )

    issue = item.get(
        "issue",
        ""
    )

    amount = item.get(
        "amount",
        ""
    )

    owner = item.get(
        "owner",
        ""
    )

    details = item.get(
        "details",
        ""
    )

    group_title = group.upper()

    text = (
        f"📂 <b>{esc(group_title)}</b>\n\n"
        f"🆔 UID:\n"
        f"<code>{esc(uid)}</code>\n\n"
        f"📊 Status: "
        f"{status_label(status)}\n"
    )

    if name:

        text += (
            f"👤 Name: "
            f"<b>{esc(name)}</b>\n"
        )

    else:

        text += (
            "👤 Name: "
            "<i>Not available</i>\n"
        )

    if issue:

        text += (
            f"⚠️ Issue: "
            f"{esc(issue)}\n"
        )

    if amount:

        text += (
            f"💰 Amount: "
            f"{esc(amount)}\n"
        )

    if owner:

        text += (
            f"👨‍💼 Owner: "
            f"{esc(owner)}\n"
        )

    if details:

        text += (
            f"📝 Details: "
            f"{esc(details)}\n"
        )

    text += (
        f"🕒 Checked: "
        f"{esc(checked or 'Not checked')}"
    )

    return text


# =========================================================
# FIND ITEM
# =========================================================

def find_item(
    group,
    uid
):

    uid = normalize_uid(uid)

    for item in DATA.get(
        group,
        []
    ):

        if normalize_uid(
            item.get("uid", "")
        ) == uid:

            return item

    return None


# =========================================================
# SHOW GROUP LIST
# =========================================================

def show_group_list(
    chat_id,
    group,
    page=1,
    message_id=None
):

    items = DATA.get(
        group,
        []
    )

    if not items:

        text = (
            f"📂 <b>{esc(group.upper())}</b>\n\n"
            "No UID saved."
        )

        if message_id:

            edit_message(
                chat_id,
                message_id,
                text
            )

        else:

            send(
                chat_id,
                text
            )

        return

    text = format_group_list(
        group,
        page
    )

    markup = group_list_buttons(
        group,
        page
    )

    if message_id:

        result = edit_message(
            chat_id,
            message_id,
            text,
            markup
        )

        if not result.get("ok"):

            send(
                chat_id,
                text,
                markup
            )

    else:

        send(
            chat_id,
            text,
            markup
        )


# =========================================================
# SHOW ITEM DETAIL
# =========================================================

def show_item_detail(
    chat_id,
    group,
    uid,
    message_id=None
):

    item = find_item(
        group,
        uid
    )

    if not item:

        send(
            chat_id,
            "❌ UID no longer exists."
        )

        return

    text = format_detail(
        group,
        item
    )

    markup = uid_action_buttons(
        group,
        uid
    )

    if message_id:

        result = edit_message(
            chat_id,
            message_id,
            text,
            markup
        )

        if not result.get("ok"):

            send(
                chat_id,
                text,
                markup
            )

    else:

        send(
            chat_id,
            text,
            markup
        )


# =========================================================
# DEFAULT ITEM
# =========================================================

def default_item(uid):

    return {
        "uid": uid,
        "status": "UNKNOWN",

        # First scan should not generate
        # a status-change alert.
        "previous_status": "UNKNOWN",

        "name": "",
        "graph_picture": False,
        "checked_at": "",
        "status_changed_at": "",
        "issue": "",
        "amount": "",
        "owner": "",
        "details": ""
    }


# =========================================================
# ADD UID
# =========================================================

def add_uid(
    group,
    uid
):

    uid = normalize_uid(uid)

    if not uid:

        return (
            False,
            "Invalid UID."
        )

    if group not in [
        "recovery",
        "deletion"
    ]:

        return (
            False,
            "Invalid group."
        )

    for item in DATA.get(
        group,
        []
    ):

        if normalize_uid(
            item.get(
                "uid",
                ""
            )
        ) == uid:

            return (
                False,
                "UID already exists."
            )

    DATA[group].append(
        default_item(uid)
    )

    save_data(DATA)

    return (
        True,
        f"✅ UID <code>{esc(uid)}</code> "
        f"added to <b>{esc(group)}</b>."
    )


# =========================================================
# REMOVE UID
# =========================================================

def remove_uid(
    group,
    uid
):

    uid = normalize_uid(uid)

    if group not in [
        "recovery",
        "deletion"
    ]:

        return (
            False,
            "Invalid group."
        )

    old_count = len(
        DATA.get(
            group,
            []
        )
    )

    DATA[group] = [
        item
        for item in DATA[group]
        if normalize_uid(
            item.get(
                "uid",
                ""
            )
        ) != uid
    ]

    if len(DATA[group]) == old_count:

        return (
            False,
            f"❌ UID <code>{esc(uid)}</code> "
            "not found."
        )

    save_data(DATA)

    return (
        True,
        f"🗑 UID <code>{esc(uid)}</code> "
        f"removed from <b>{esc(group)}</b>."
    )


# =========================================================
# SET INFO
# =========================================================

def set_info(
    group,
    uid,
    issue,
    amount,
    owner,
    details
):

    uid = normalize_uid(uid)

    if group not in [
        "recovery",
        "deletion"
    ]:

        return (
            False,
            "Invalid group."
        )

    for item in DATA.get(
        group,
        []
    ):

        if normalize_uid(
            item.get(
                "uid",
                ""
            )
        ) == uid:

            item["issue"] = (
                issue.strip()
            )

            item["amount"] = (
                amount.strip()
            )

            item["owner"] = (
                owner.strip()
            )

            item["details"] = (
                details.strip()
            )

            save_data(DATA)

            return (
                True,
                f"✅ Information updated for "
                f"<code>{esc(uid)}</code>."
            )

    return (
        False,
        f"❌ UID <code>{esc(uid)}</code> "
        "not found."
    )


# =========================================================
# INFO EDITOR BUTTONS
# =========================================================

def info_editor_buttons(
    group,
    uid
):

    return {
        "inline_keyboard": [

            [
                {
                    "text": "⚠️ Issue",
                    "callback_data":
                        f"editfield:{group}:{uid}:issue"
                },

                {
                    "text": "💰 Amount",
                    "callback_data":
                        f"editfield:{group}:{uid}:amount"
                }
            ],

            [
                {
                    "text": "👨‍💼 Owner",
                    "callback_data":
                        f"editfield:{group}:{uid}:owner"
                },

                {
                    "text": "📝 Details",
                    "callback_data":
                        f"editfield:{group}:{uid}:details"
                }
            ],

            [
                {
                    "text": "⬅️ Back",
                    "callback_data":
                        f"backdetail:{group}:{uid}"
                }
            ]
        ]
    }


def field_label(field):

    labels = {
        "issue": "⚠️ Issue",
        "amount": "💰 Amount",
        "owner": "👨‍💼 Owner",
        "details": "📝 Details"
    }

    return labels.get(
        field,
        field
    )


# =========================================================
# HELP
# =========================================================

def help_text():

    return """
🖥️ <b>Facebook Account Monitor</b> 🖥️

<b>UID Commands:</b>

/addrecovery UID
/adddeletion UID

/recoverylist
/deletionlist

/remove recovery UID
/remove deletion UID

/Faqcheck UID

/help
"""


# =========================================================
# AUTHORIZATION
# =========================================================

def get_user_id(
    msg
):

    user = msg.get(
        "from",
        {}
    )

    try:

        return int(
            user.get(
                "id"
            )
        )

    except Exception:

        return None


def authorized(msg):

    user_id = get_user_id(
        msg
    )

    if user_id is None:
        return False

    if user_id == ALLOWED_CHAT_ID:
        return True

    return user_id in DATA.get(
        "allowed_users",
        []
    )


def authorized_callback(
    query
):

    user = query.get(
        "from",
        {}
    )

    try:

        user_id = int(
            user.get(
                "id"
            )
        )

    except Exception:

        return False

    if user_id == ALLOWED_CHAT_ID:
        return True

    return user_id in DATA.get(
        "allowed_users",
        []
    )


def main_admin(
    user_id
):

    return user_id == ALLOWED_CHAT_ID


# =========================================================
# ALLOWED USERS
# =========================================================

def add_allowed_user(
    user_id
):

    try:

        user_id = int(
            user_id
        )

    except Exception:

        return (
            False,
            "Invalid Telegram user ID."
        )

    if user_id == ALLOWED_CHAT_ID:

        return (
            False,
            "That user is already the main admin."
        )

    allowed = DATA.setdefault(
        "allowed_users",
        []
    )

    if user_id in allowed:

        return (
            False,
            "User is already allowed."
        )

    allowed.append(
        user_id
    )

    save_data(DATA)

    return (
        True,
        f"✅ User <code>{user_id}</code> "
        "is now allowed to use the bot."
    )


def remove_allowed_user(
    user_id
):

    try:

        user_id = int(
            user_id
        )

    except Exception:

        return (
            False,
            "Invalid Telegram user ID."
        )

    if user_id == ALLOWED_CHAT_ID:

        return (
            False,
            "The main admin cannot be removed."
        )

    allowed = DATA.get(
        "allowed_users",
        []
    )

    if user_id not in allowed:

        return (
            False,
            "User is not in the allowed list."
        )

    DATA["allowed_users"] = [
        x
        for x in allowed
        if int(x) != user_id
    ]

    save_data(DATA)

    return (
        True,
        f"🗑 User <code>{user_id}</code> "
        "removed from allowed users."
    )


def allowed_list_text():

    allowed = DATA.get(
        "allowed_users",
        []
    )

    text = (
        "👥 <b>Allowed Users</b>\n\n"
        f"👑 Main Admin:\n"
        f"<code>{ALLOWED_CHAT_ID}</code>\n"
    )

    if not allowed:

        text += (
            "\nNo additional users."
        )

        return text

    text += (
        "\n👤 Additional Users:\n"
    )

    for index, user_id in enumerate(
        allowed,
        1
    ):

        text += (
            f"{index}. "
            f"<code>{esc(user_id)}</code>\n"
        )

    return text


# =========================================================
# DAILY REPORT
# =========================================================

def build_daily_report(
    report_date=None,
    changes=None
):

    if report_date is None:

        report_date = datetime.now().strftime(
            "%Y-%m-%d"
        )

    total = 0
    alive = 0
    dead = 0

    for group in [
        "recovery",
        "deletion"
    ]:

        for item in DATA.get(
            group,
            []
        ):

            total += 1

            status = item.get(
                "status",
                "UNKNOWN"
            )

            if status == "ALIVE":

                alive += 1

            elif status == "DEAD":

                dead += 1

    if changes is None:

        changes = DATA.get(
            "daily_report",
            {}
        ).get(
            "changes",
            0
        )

    unknown = (
        total
        - alive
        - dead
    )

    return (
        "📊 <b>Daily Report</b>\n\n"
        f"📅 {esc(report_date)}\n\n"
        f"Total UIDs: {total}\n"
        f"🟢 Alive: {alive}\n"
        f"🔴 Dead: {dead}\n"
        f"🟡 Unknown: {unknown}\n\n"
        f"Changes today: {changes}"
    )


def send_daily_report():

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    daily = DATA.setdefault(
        "daily_report",
        {}
    )

    last_sent = daily.get(
        "last_sent_date",
        ""
    )

    if last_sent == today:
        return False

    changes = daily.get(
        "changes",
        0
    )

    report = build_daily_report(
        today,
        changes
    )

    result = send(
        ALLOWED_CHAT_ID,
        report
    )

    if result.get("ok"):

        daily["last_sent_date"] = today

        # New day's counter starts at zero
        daily["date"] = today
        daily["changes"] = 0

        save_data(DATA)

        return True

    return False


def seconds_until_next_midnight():

    now = datetime.now()

    tomorrow = (
        now
        .replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )
        + timedelta(days=1)
    )

    seconds = (
        tomorrow - now
    ).total_seconds()

    return max(
        1,
        int(seconds)
    )


def daily_report_loop():

    print(
        "Daily report thread started."
    )

    while True:

        try:

            time.sleep(
                seconds_until_next_midnight()
            )

            # Give the clock a moment to cross midnight.
            time.sleep(2)

            # New date.
            today = datetime.now().strftime(
                "%Y-%m-%d"
            )

            # The counter belonging to the previous
            # date is normally already persisted.
            #
            # We send the report for the previous
            # date by using the stored date before
            # resetting it.
            daily = DATA.setdefault(
                "daily_report",
                {}
            )

            previous_date = daily.get(
                "date",
                ""
            )

            previous_changes = daily.get(
                "changes",
                0
            )

            # Build report using current totals,
            # as requested for the automatic daily report.
            report_date = (
                previous_date
                if previous_date
                else today
            )

            report = build_daily_report(
                report_date,
                previous_changes
            )

            result = send(
                ALLOWED_CHAT_ID,
                report
            )

            if result.get("ok"):

                daily["date"] = today
                daily["changes"] = 0
                daily["last_sent_date"] = (
                    report_date
                )

                save_data(DATA)

            time.sleep(1)

        except Exception as e:

            print(
                "Daily report error:",
                e
            )

            time.sleep(10)


# =========================================================
# PENDING INFO EDIT
# =========================================================

def handle_pending_edit(
    msg,
    user_id,
    text
):

    pending = PENDING_EDITS.get(
        user_id
    )

    if not pending:
        return False

    group = pending.get(
        "group"
    )

    uid = pending.get(
        "uid"
    )

    field = pending.get(
        "field"
    )

    item = find_item(
        group,
        uid
    )

    if not item:

        PENDING_EDITS.pop(
            user_id,
            None
        )

        send(
            msg["chat"]["id"],
            "❌ UID no longer exists."
        )

        return True

    value = text.strip()

    item[field] = value

    PENDING_EDITS.pop(
        user_id,
        None
    )

    save_data(DATA)

    send(
        msg["chat"]["id"],
        (
            "✅ <b>Information Updated</b>\n\n"
            f"📂 {esc(group.upper())}\n"
            f"🆔 <code>{esc(uid)}</code>\n"
            f"{field_label(field)}: "
            f"<b>{esc(value) if value else 'Empty'}</b>"
        ),
        info_editor_buttons(
            group,
            uid
        )
    )

    return True


# =========================================================
# MESSAGE HANDLER
# =========================================================

def handle(msg):

    if not authorized(msg):
        return

    chat_id = msg["chat"]["id"]

    user_id = get_user_id(
        msg
    )

    if user_id is None:
        return

    text = msg.get(
        "text",
        ""
    ).strip()

    if not text:
        return

    # -----------------------------------------------------
    # Pending button-based editor input
    # -----------------------------------------------------

    if (
        not text.startswith("/")
        and user_id in PENDING_EDITS
    ):

        if handle_pending_edit(
            msg,
            user_id,
            text
        ):

            return

    parts = text.split(
        maxsplit=1
    )

    command = parts[0].lower()

    argument = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    # =====================================================
    # START
    # =====================================================

    if command == "/start":

        send(
            chat_id,
            help_text()
        )

        return

    # =====================================================
    # HELP
    # =====================================================

    if command == "/help":

        send(
            chat_id,
            help_text()
        )

        return

    # =====================================================
    # ADD RECOVERY
    # =====================================================

    if command == "/addrecovery":

        if not argument:

            send(
                chat_id,
                "Usage:\n"
                "<code>/addrecovery UID</code>"
            )

            return

        uid = normalize_uid(
            argument
        )

        ok, result = add_uid(
            "recovery",
            uid
        )

        if not ok:

            send(
                chat_id,
                result
            )

            return

        send(
            chat_id,
            (
                "🆕 <b>New RECOVERY UID</b>\n\n"
                f"🆔\n\n"
                f"<code>{esc(uid)}</code>\n\n"
                "🔎 Checking..."
            )
        )

        try:

            result_data = check_uid_fast(
                uid
            )

            # First check does not generate
            # a status-change alert.
            update_item(
                "recovery",
                uid,
                result_data,
                count_change=False
            )

            item = find_item(
                "recovery",
                uid
            )

            if item:

                item["previous_status"] = (
                    result_data.get(
                        "status",
                        "UNKNOWN"
                    )
                )

            save_data(DATA)

            text_result = (
                "🆕 <b>New RECOVERY UID</b>\n\n"
                f"🆔\n\n"
                f"<code>{esc(uid)}</code>\n\n"
                f"📊 {status_label(result_data['status'])}\n"
            )

            if result_data.get("name"):

                text_result += (
                    f"👤 "
                    f"{esc(result_data['name'])}\n"
                )

            text_result += (
                f"🕒 Checked: "
                f"{esc(result_data['checked_at'])}\n\n"
                "Choose an action:"
            )

            send(
                chat_id,
                text_result,
                uid_action_buttons(
                    "recovery",
                    uid
                )
            )

        except Exception as e:

            print(
                "Add recovery check error:",
                e
            )

            send(
                chat_id,
                (
                    "⚠️ UID was added, "
                    "but first check failed.\n\n"
                    f"<code>{esc(uid)}</code>"
                ),
                uid_action_buttons(
                    "recovery",
                    uid
                )
            )

        return

    # =====================================================
    # ADD DELETION
    # =====================================================

    if command == "/adddeletion":

        if not argument:

            send(
                chat_id,
                "Usage:\n"
                "<code>/adddeletion UID</code>"
            )

            return

        uid = normalize_uid(
            argument
        )

        ok, result = add_uid(
            "deletion",
            uid
        )

        if not ok:

            send(
                chat_id,
                result
            )

            return

        send(
            chat_id,
            (
                "🆕 <b>New DELETION UID</b>\n\n"
                f"🆔\n\n"
                f"<code>{esc(uid)}</code>\n\n"
                "🔎 Checking..."
            )
        )

        try:

            result_data = check_uid_fast(
                uid
            )

            update_item(
                "deletion",
                uid,
                result_data,
                count_change=False
            )

            item = find_item(
                "deletion",
                uid
            )

            if item:

                item["previous_status"] = (
                    result_data.get(
                        "status",
                        "UNKNOWN"
                    )
                )

            save_data(DATA)

            text_result = (
                "🆕 <b>New DELETION UID</b>\n\n"
                f"🆔\n\n"
                f"<code>{esc(uid)}</code>\n\n"
                f"📊 {status_label(result_data['status'])}\n"
            )

            if result_data.get("name"):

                text_result += (
                    f"👤 "
                    f"{esc(result_data['name'])}\n"
                )

            text_result += (
                f"🕒 Checked: "
                f"{esc(result_data['checked_at'])}\n\n"
                "Choose an action:"
            )

            send(
                chat_id,
                text_result,
                uid_action_buttons(
                    "deletion",
                    uid
                )
            )

        except Exception as e:

            print(
                "Add deletion check error:",
                e
            )

            send(
                chat_id,
                (
                    "⚠️ UID was added, "
                    "but first check failed.\n\n"
                    f"<code>{esc(uid)}</code>"
                ),
                uid_action_buttons(
                    "deletion",
                    uid
                )
            )

        return

    # =====================================================
    # RECOVERY LIST
    # =====================================================

    if command == "/recoverylist":

        show_group_list(
            chat_id,
            "recovery",
            1
        )

        return

    # =====================================================
    # DELETION LIST
    # =====================================================

    if command == "/deletionlist":

        show_group_list(
            chat_id,
            "deletion",
            1
        )

        return

    # =====================================================
    # REMOVE
    # =====================================================

    if command == "/remove":

        args = argument.split()

        if len(args) != 2:

            send(
                chat_id,
                (
                    "Usage:\n"
                    "<code>/remove recovery UID</code>\n"
                    "<code>/remove deletion UID</code>"
                )
            )

            return

        group = args[0].lower()
        uid = args[1]

        ok, result = remove_uid(
            group,
            uid
        )

        send(
            chat_id,
            result
        )

        return

    # =====================================================
    # FAQCHECK
    # =====================================================

    if command == "/faqcheck":

        uid = normalize_uid(
            argument
        )

        if not uid:

            send(
                chat_id,
                (
                    "Usage:\n"
                    "<code>/Faqcheck UID</code>"
                )
            )

            return

        send(
            chat_id,
            (
                f"🔎 Checking "
                f"<code>{esc(uid)}</code>..."
            )
        )

        try:

            result = check_uid_fast(
                uid
            )

            text = (
                "🔎 <b>Facebook UID Check</b>\n\n"
                f"🆔 UID: <code>{esc(uid)}</code>\n"
                f"📊 Status: "
                f"{status_label(result['status'])}\n"
            )

            if result.get("name"):

                text += (
                    f"👤 Name: "
                    f"<b>{esc(result['name'])}</b>\n"
                )

            else:

                text += (
                    "👤 Name: "
                    "<i>Not available</i>\n"
                )

            text += (
                f"🖼 Profile picture: "
                f"{'YES' if result.get('graph_picture') else 'NO'}\n"
                f"🕒 Checked: "
                f"{esc(result.get('checked_at', ''))}"
            )

            send(
                chat_id,
                text
            )

        except Exception as e:

            print(
                f"Faqcheck error "
                f"for {uid}: {e}"
            )

            send(
                chat_id,
                (
                    "⚠️ Check error.\n\n"
                    f"<code>{esc(str(e))}</code>"
                )
            )

        return


    # =====================================================
    # UNKNOWN COMMAND
    # =====================================================

    if command.startswith("/"):

        send(
            chat_id,
            (
                "❌ Unknown command.\n\n"
                "Use /help"
            )
        )


# =========================================================
# CALLBACK HANDLER
# =========================================================

def handle_callback(query):

    try:

        callback_id = query.get(
            "id",
            ""
        )

        data = query.get(
            "data",
            ""
        )

        message = query.get(
            "message",
            {}
        )

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        message_id = message.get(
            "message_id"
        )

        if not authorized_callback(query):

            answer_callback(
                callback_id,
                "Not authorized."
            )

            return

        user = query.get(
            "from",
            {}
        )

        try:

            user_id = int(
                user.get(
                    "id"
                )
            )

        except Exception:

            user_id = None

        # -------------------------------------------------
        # VIEW UID
        # -------------------------------------------------

        if data.startswith("view:"):

            parts = data.split(
                ":",
                2
            )

            if len(parts) != 3:

                answer_callback(
                    callback_id,
                    "Invalid button."
                )

                return

            group = parts[1]
            uid = parts[2]

            answer_callback(
                callback_id,
                "Opening UID..."
            )

            show_item_detail(
                chat_id,
                group,
                uid,
                message_id
            )

            return

        # -------------------------------------------------
        # PAGE
        # -------------------------------------------------

        if data.startswith("page:"):

            parts = data.split(
                ":"
            )

            if len(parts) != 3:

                answer_callback(
                    callback_id,
                    "Invalid page."
                )

                return

            group = parts[1]

            try:

                page = int(
                    parts[2]
                )

            except Exception:

                page = 1

            if group not in [
                "recovery",
                "deletion"
            ]:

                answer_callback(
                    callback_id,
                    "Invalid list."
                )

                return

            answer_callback(
                callback_id,
                f"Page {page}"
            )

            show_group_list(
                chat_id,
                group,
                page,
                message_id
            )

            return

        # -------------------------------------------------
        # LIST
        # -------------------------------------------------

        if data.startswith("list:"):

            parts = data.split(
                ":"
            )

            group = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            try:

                page = (
                    int(parts[2])
                    if len(parts) > 2
                    else 1
                )

            except Exception:

                page = 1

            if group not in [
                "recovery",
                "deletion"
            ]:

                answer_callback(
                    callback_id,
                    "Invalid list."
                )

                return

            answer_callback(
                callback_id,
                "Opening list..."
            )

            show_group_list(
                chat_id,
                group,
                page,
                message_id
            )

            return

        # -------------------------------------------------
        # BACK DETAIL
        # -------------------------------------------------

        if data.startswith("backdetail:"):

            parts = data.split(
                ":",
                2
            )

            if len(parts) != 3:

                answer_callback(
                    callback_id,
                    "Invalid button."
                )

                return

            group = parts[1]
            uid = parts[2]

            answer_callback(
                callback_id,
                "Opening UID..."
            )

            show_item_detail(
                chat_id,
                group,
                uid,
                message_id
            )

            return

        # -------------------------------------------------
        # UPDATE INFO
        # -------------------------------------------------

        if data.startswith("update:"):

            parts = data.split(
                ":",
                2
            )

            if len(parts) != 3:

                answer_callback(
                    callback_id,
                    "Invalid update button."
                )

                return

            group = parts[1]
            uid = parts[2]

            item = find_item(
                group,
                uid
            )

            if not item:

                answer_callback(
                    callback_id,
                    "UID not found."
                )

                return

            answer_callback(
                callback_id,
                "Choose what to edit."
            )

            edit_message(
                chat_id,
                message_id,
                (
                    "📝 <b>Edit Information</b>\n\n"
                    f"📂 Group: <b>{esc(group.upper())}</b>\n"
                    f"🆔 UID: <code>{esc(uid)}</code>\n\n"
                    "Choose a field:"
                ),
                info_editor_buttons(
                    group,
                    uid
                )
            )

            return

        # -------------------------------------------------
        # EDIT FIELD
        # -------------------------------------------------

        if data.startswith("editfield:"):

            parts = data.split(
                ":",
                3
            )

            if len(parts) != 4:

                answer_callback(
                    callback_id,
                    "Invalid edit button."
                )

                return

            group = parts[1]
            uid = parts[2]
            field = parts[3]

            if field not in [
                "issue",
                "amount",
                "owner",
                "details"
            ]:

                answer_callback(
                    callback_id,
                    "Invalid field."
                )

                return

            item = find_item(
                group,
                uid
            )

            if not item:

                answer_callback(
                    callback_id,
                    "UID not found."
                )

                return

            if user_id is None:

                answer_callback(
                    callback_id,
                    "User error."
                )

                return

            PENDING_EDITS[user_id] = {
                "group": group,
                "uid": uid,
                "field": field
            }

            answer_callback(
                callback_id,
                "Waiting for your message."
            )

            current_value = item.get(
                field,
                ""
            )

            current_text = (
                esc(current_value)
                if current_value
                else "<i>Empty</i>"
            )

            send(
                chat_id,
                (
                    f"{field_label(field)}\n\n"
                    f"Current value: {current_text}\n\n"
                    "✏️ Send the new value now.\n"
                    "To clear it, send:\n"
                    "<code>-</code>"
                )
            )

            return

        # -------------------------------------------------
        # DELETE
        # -------------------------------------------------

        if data.startswith("delete:"):

            parts = data.split(
                ":",
                2
            )

            if len(parts) != 3:

                answer_callback(
                    callback_id,
                    "Invalid delete button."
                )

                return

            group = parts[1]
            uid = parts[2]

            ok, result = remove_uid(
                group,
                uid
            )

            answer_callback(
                callback_id,
                "UID deleted."
                if ok
                else "UID not found."
            )

            if ok:

                edit_message(
                    chat_id,
                    message_id,
                    (
                        f"🗑️ <b>UID Deleted</b>\n\n"
                        f"🆔 <code>{esc(uid)}</code>\n"
                        f"📂 {esc(group.upper())}\n\n"
                        "Use the list button to view "
                        "the remaining UIDs."
                    ),
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "📊 List of UIDs",
                                    "callback_data":
                                        f"list:{group}:1"
                                }
                            ]
                        ]
                    }
                )

            return

        # -------------------------------------------------
        # UNKNOWN CALLBACK
        # -------------------------------------------------

        answer_callback(
            callback_id,
            "Unknown button."
        )

    except Exception as e:

        print(
            "Callback error:",
            e
        )

        try:

            answer_callback(
                query.get(
                    "id",
                    ""
                ),
                "Button error."
            )

        except Exception:
            pass


# =========================================================
# AUTOMATIC SCANNER
# =========================================================

def scanner_loop():

    while True:

        try:

            time.sleep(
                SCAN_INTERVAL
            )

            print(
                "Automatic scan started..."
            )

            results = scan_all()

            print(
                "Automatic scan finished: "
                f"{len(results)} checked"
            )

        except Exception as e:

            print(
                "Scanner error:",
                e
            )

            time.sleep(5)


# =========================================================
# DELETE WEBHOOK
# =========================================================

def delete_webhook():

    try:

        result = telegram_api(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            }
        )

        print(
            "deleteWebhook:",
            result
        )

    except Exception as e:

        print(
            "deleteWebhook error:",
            e
        )


# =========================================================
# TEST BOT
# =========================================================

def test_bot():

    result = telegram_api(
        "getMe"
    )

    if result.get("ok"):

        bot = result.get(
            "result",
            {}
        )

        print(
            "Bot connected:",
            bot.get("username")
        )

        return True

    print(
        "Bot connection failed:",
        result
    )

    return False


# =========================================================
# POLLING
# =========================================================

def polling():

    offset = None

    print(
        "Telegram polling started..."
    )

    while True:

        try:

            params = {
                "timeout": 30,
                "allowed_updates": [
                    "message",
                    "callback_query"
                ]
            }

            if offset is not None:

                params["offset"] = offset

            result = telegram_api(
                "getUpdates",
                params
            )

            if not result.get("ok"):

                print(
                    "getUpdates error:",
                    result
                )

                time.sleep(5)

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                try:

                    if "message" in update:

                        handle(
                            update["message"]
                        )

                    elif "callback_query" in update:

                        handle_callback(
                            update["callback_query"]
                        )

                except Exception as e:

                    print(
                        "Update handling error:",
                        e
                    )

        except KeyboardInterrupt:

            print(
                "Bot stopped."
            )

            break

        except Exception as e:

            print(
                "Polling error:",
                e
            )

            time.sleep(5)


# =========================================================
# MAIN
# =========================================================

def main():

    print("=" * 50)
    print("Facebook UID Monitor")
    print("=" * 50)

    if (
        not BOT_TOKEN
        or BOT_TOKEN ==
        "PUT_YOUR_NEW_BOT_TOKEN_HERE"
    ):

        print(
            "\nERROR: Set BOT_TOKEN first.\n"
            "Use a new token from @BotFather.\n"
        )

        return

    if not test_bot():

        print(
            "\nBot test failed. "
            "Check BOT_TOKEN.\n"
        )

        return

    delete_webhook()

    # -----------------------------------------------------
    # High-performance async checker
    # -----------------------------------------------------
    #
    # Creates one shared aiohttp.ClientSession, an
    # asyncio.Queue, and multiple worker() tasks.
    #
    # This is started before the scanner so /scan and
    # single-UID commands use the same worker pool.
    try:
        start_async_checker()
    except Exception as e:
        print(
            "Async checker startup warning:",
            e
        )

    # -----------------------------------------------------
    # Automatic scanner
    # -----------------------------------------------------

    scanner = threading.Thread(
        target=scanner_loop,
        daemon=True
    )

    scanner.start()

    # -----------------------------------------------------
    # Daily report
    # -----------------------------------------------------

    daily_report = threading.Thread(
        target=daily_report_loop,
        daemon=True
    )

    daily_report.start()

    print(
        "Automatic scanner started."
    )

    print(
        f"Scan interval: "
        f"{SCAN_INTERVAL} seconds"
    )

    print(
        "Daily report thread started."
    )

    print(
        "Bot is ready."
    )

    polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()

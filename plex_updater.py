#!/usr/bin/env python3
"""
Plex Media Server macOS Auto-Updater
====================================
Automatically checks for, downloads, verifies, installs, and restarts Plex Media
Server on macOS. Designed to run seamlessly either manually or as a background
macOS LaunchAgent service.

Author: Antigravity Pair Programmer
"""

import argparse
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# Default Paths & Constants
DEFAULT_APP_PATH = "/Applications/Plex Media Server.app"
DEFAULT_LOG_PATH = os.path.expanduser("~/Library/Logs/PlexAutoUpdater.log")
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/plex-auto-updater/config.json")
DEFAULT_PLIST_LABEL = "com.plex.macos-auto-updater"
DEFAULT_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{DEFAULT_PLIST_LABEL}.plist")
PLEX_DOWNLOADS_API = "https://plex.tv/api/downloads/5.json"
PLEX_LOCAL_IDENTITY_URL = "http://127.0.0.1:32400/identity"
PLEX_LOCAL_SESSIONS_URL = "http://127.0.0.1:32400/status/sessions"
PLEX_PREFERENCES_PATH = os.path.expanduser(
    "~/Library/Application Support/Plex Media Server/Preferences.xml"
)

logger = logging.getLogger("plex_updater")


def setup_logging(log_file=None, verbose=False):
    """Configures console and file logging."""
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_format = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # File Handler
    if log_file:
        try:
            log_dir = os.path.dirname(os.path.abspath(log_file))
            os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(console_format)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.debug(f"File logging not available at {log_file}: {e}")


def load_config(config_path=None):
    """Loads configuration with defaults and fallbacks."""
    defaults = {
        "app_path": DEFAULT_APP_PATH,
        "channel": "public",  # "public" or "plexpass"
        "token": "",
        "check_active_streams": True,
        "keep_backup": True,
        "notify": True,
        "log_file": DEFAULT_LOG_PATH,
        "schedule": {
            "type": "interval",  # "interval" or "calendar"
            "interval_hours": 6,
            "calendar_hour": 4,
            "calendar_minute": 0,
        },
    }

    search_paths = []
    if config_path:
        search_paths.append(os.path.expanduser(config_path))
    # Also check local workspace config.json and user ~/.config/plex-auto-updater/config.json
    local_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    search_paths.extend([local_cfg, DEFAULT_CONFIG_PATH])

    for path in search_paths:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                    defaults.update(user_cfg)
                    logger.debug(f"Loaded configuration from {path}")
                    break
            except Exception as e:
                logger.warning(f"Error reading config file {path}: {e}")

    # Expand any ~ in paths
    defaults["app_path"] = os.path.expanduser(defaults["app_path"])
    defaults["log_file"] = os.path.expanduser(defaults["log_file"])
    return defaults


def parse_version_string(v_str):
    """
    Parses a Plex version string into a comparable tuple.
    Example: '1.43.4.10903-e5521bd8c' -> (1, 43, 4, 10903)
             '1.42.2' -> (1, 42, 2, 0)
    """
    if not v_str:
        return None
    cleaned = v_str.strip().lstrip("v")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)", cleaned)
    if m:
        return tuple(int(x) for x in m.groups())
    m2 = re.match(r"^(\d+)\.(\d+)\.(\d+)", cleaned)
    if m2:
        return tuple(int(x) for x in m2.groups()) + (0,)
    return None


def is_version_newer(latest_ver, installed_ver):
    """Returns True if latest_ver is newer than installed_ver."""
    t_latest = parse_version_string(latest_ver)
    t_installed = parse_version_string(installed_ver)

    if t_latest and t_installed:
        return t_latest > t_installed
    # Fallback if unparseable
    return latest_ver != installed_ver


def detect_plex_token(configured_token=""):
    """
    Finds a valid Plex token from config, environment, or Preferences.xml.
    """
    if configured_token:
        return configured_token

    env_token = os.environ.get("PLEX_TOKEN")
    if env_token:
        return env_token

    if os.path.isfile(PLEX_PREFERENCES_PATH):
        try:
            tree = ET.parse(PLEX_PREFERENCES_PATH)
            root = tree.getroot()
            token = root.attrib.get("PlexOnlineToken")
            if token:
                logger.debug("Discovered PlexOnlineToken from local Preferences.xml")
                return token
        except Exception as e:
            logger.debug(f"Could not read PlexOnlineToken from Preferences.xml: {e}")

    return ""


def get_installed_version_from_app(app_path):
    """Reads CFBundleShortVersionString or CFBundleVersion from Info.plist."""
    info_plist = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.isfile(info_plist):
        return None

    try:
        with open(info_plist, "rb") as f:
            plist = plistlib.load(f)
            # Some builds store full version in CFBundleVersion or CFBundleShortVersionString
            ver = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
            return str(ver).strip() if ver else None
    except Exception as e:
        logger.warning(f"Failed to read Info.plist from {app_path}: {e}")
        return None


def get_running_server_version(timeout=2):
    """Queries running Plex instance via http://127.0.0.1:32400/identity."""
    try:
        req = urllib.request.Request(
            PLEX_LOCAL_IDENTITY_URL,
            headers={"Accept": "application/json", "User-Agent": "PlexAutoUpdater/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("MediaContainer", {}).get("version")
    except Exception:
        # Plex may not be running or unreachable
        return None


def get_current_installed_version(app_path):
    """Gets version first from running instance, then falls back to Info.plist."""
    running_ver = get_running_server_version()
    if running_ver:
        return running_ver, True
    app_ver = get_installed_version_from_app(app_path)
    return app_ver, False


def fetch_latest_release(channel="public", token=""):
    """
    Queries Plex downloads API for the latest macOS release details.
    Returns: dict with 'version', 'url', 'checksum', 'release_date'
    """
    url = PLEX_DOWNLOADS_API
    headers = {"Accept": "application/json", "User-Agent": "PlexAutoUpdater/1.0"}

    if channel.lower() == "plexpass":
        url += "?channel=plexpass"
        if token:
            headers["X-Plex-Token"] = token
        else:
            logger.warning("Plex Pass channel requested, but no Plex Token is configured.")

    logger.debug(f"Fetching release manifest from {url}")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            raise RuntimeError(f"Authentication failed querying Plex Pass releases (HTTP {e.code}). Verify your token.")
        raise RuntimeError(f"Plex API HTTP Error {e.code}: {e.reason}")
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Plex API: {e}")

    try:
        macos_data = data["computer"]["MacOS"]
        latest_version = macos_data["version"]
        releases = macos_data.get("releases", [])

        # Find universal or darwin-x86_64 release
        selected_release = None
        for r in releases:
            if r.get("distro") == "macos" or "darwin" in r.get("build", ""):
                selected_release = r
                break

        if not selected_release and releases:
            selected_release = releases[0]

        if not selected_release:
            raise ValueError("No macOS release entry found in API response.")

        return {
            "version": latest_version,
            "url": selected_release["url"],
            "checksum": selected_release.get("checksum", ""),
            "release_date": selected_release.get("release_date"),
            "items_added": macos_data.get("items_added", ""),
            "items_fixed": macos_data.get("items_fixed", ""),
        }
    except KeyError as e:
        raise ValueError(f"Unexpected JSON structure from Plex API: missing key {e}")


def get_active_stream_count(token=""):
    """
    Queries local Plex Media Server for active playback sessions.
    Returns: int (number of active streams) or None if check unavailable.
    """
    if not is_plex_running():
        return 0

    headers = {"Accept": "application/json", "User-Agent": "PlexAutoUpdater/1.0"}
    if token:
        headers["X-Plex-Token"] = token

    try:
        req = urllib.request.Request(PLEX_LOCAL_SESSIONS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            mc = data.get("MediaContainer", {})
            size = mc.get("size", 0)
            metadata = mc.get("Metadata", [])
            return max(size, len(metadata))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logger.warning("Active stream check requires authentication, but no valid Plex token was found.")
            return None
        logger.debug(f"HTTP error checking active sessions: {e}")
        return None
    except Exception as e:
        logger.debug(f"Could not check active sessions: {e}")
        return None


def is_plex_running():
    """Checks whether Plex Media Server process is currently running."""
    res = subprocess.run(["pgrep", "-f", "Plex Media Server"], capture_output=True)
    return res.returncode == 0


def send_macos_notification(message, title="Plex Auto-Updater", subtitle=""):
    """Sends a native macOS User Notification via osascript."""
    try:
        clean_msg = message.replace('"', '\\"')
        clean_title = title.replace('"', '\\"')
        clean_sub = subtitle.replace('"', '\\"')
        script = f'display notification "{clean_msg}" with title "{clean_title}"'
        if clean_sub:
            script += f' subtitle "{clean_sub}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception as e:
        logger.debug(f"Failed to display macOS notification: {e}")


def download_file_with_checksum(url, expected_checksum, dest_path):
    """
    Downloads file from URL to dest_path and verifies SHA-1 checksum.
    """
    logger.info(f"Downloading update from {url}...")
    headers = {"User-Agent": "PlexAutoUpdater/1.0"}
    req = urllib.request.Request(url, headers=headers)

    hasher = hashlib.sha1()
    downloaded_bytes = 0

    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as out_file:
        total_size = resp.headers.get("Content-Length")
        total_size = int(total_size) if total_size else None

        last_log = time.time()
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out_file.write(chunk)
            hasher.update(chunk)
            downloaded_bytes += len(chunk)

            if time.time() - last_log > 3:
                last_log = time.time()
                if total_size:
                    percent = (downloaded_bytes / total_size) * 100
                    logger.info(f"Downloading: {downloaded_bytes / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB ({percent:.1f}%)")
                else:
                    logger.info(f"Downloaded: {downloaded_bytes / (1024*1024):.1f}MB")

    calculated_checksum = hasher.hexdigest().lower()
    logger.info(f"Download complete ({downloaded_bytes / (1024*1024):.1f} MB). Verifying SHA-1 checksum...")

    if expected_checksum:
        if calculated_checksum != expected_checksum.lower():
            os.remove(dest_path)
            raise ValueError(
                f"Checksum mismatch! Expected: {expected_checksum.lower()}, Got: {calculated_checksum}"
            )
        logger.info("Checksum verified successfully.")
    else:
        logger.warning("No expected checksum provided by API. Skipped checksum validation.")


def stop_plex_server(timeout=25):
    """
    Gracefully stops Plex Media Server using AppleScript, then SIGTERM, then SIGKILL.
    """
    if not is_plex_running():
        logger.info("Plex Media Server is not running.")
        return True

    logger.info("Requesting Plex Media Server to quit gracefully...")
    # Tell app to quit via AppleScript
    subprocess.run(
        ["osascript", "-e", 'tell application "Plex Media Server" to quit'],
        capture_output=True,
    )

    start = time.time()
    while time.time() - start < timeout:
        if not is_plex_running():
            logger.info("Plex Media Server has exited gracefully.")
            return True
        time.sleep(1)

    logger.warning("Plex did not exit within timeout. Sending SIGTERM...")
    subprocess.run(["pkill", "-15", "-f", "Plex Media Server"], capture_output=True)

    start = time.time()
    while time.time() - start < 7:
        if not is_plex_running():
            logger.info("Plex terminated after SIGTERM.")
            return True
        time.sleep(1)

    logger.warning("Plex still running. Forcing termination with SIGKILL...")
    subprocess.run(["pkill", "-9", "-f", "Plex Media Server"], capture_output=True)
    time.sleep(1)

    if is_plex_running():
        raise RuntimeError("Failed to stop Plex Media Server processes.")
    return True


def start_plex_server(app_path):
    """Launches Plex Media Server in the background without stealing focus."""
    logger.info(f"Starting Plex Media Server from {app_path}...")
    res = subprocess.run(["open", "-gj", "-a", app_path], capture_output=True, text=True)
    if res.returncode != 0:
        logger.warning(f"'open -gj' returned {res.returncode}: {res.stderr}. Retrying standard 'open'...")
        res2 = subprocess.run(["open", "-a", app_path], capture_output=True, text=True)
        if res2.returncode != 0:
            raise RuntimeError(f"Failed to launch Plex: {res2.stderr}")

    logger.info("Plex launch initiated. Waiting for server to become responsive...")
    for _ in range(30):
        if get_running_server_version(timeout=1):
            logger.info("Plex Media Server is online and responding!")
            return True
        time.sleep(1)

    logger.warning("Plex launched, but server identity endpoint did not respond within 30 seconds.")
    return False


def install_update_package(zip_path, app_path, keep_backup=True):
    """
    Extracts the downloaded zip bundle with ditto, removes quarantine,
    verifies code signature, replaces existing app atomically, and creates backup.
    """
    extract_dir = tempfile.mkdtemp(prefix="plex_update_extract_")
    backup_path = app_path.rstrip("/") + ".bak"

    try:
        logger.info(f"Extracting package to temporary directory: {extract_dir}")
        ditto_res = subprocess.run(
            ["/usr/bin/ditto", "-xk", zip_path, extract_dir],
            capture_output=True,
            text=True,
        )
        if ditto_res.returncode != 0:
            raise RuntimeError(f"ditto extraction failed: {ditto_res.stderr}")

        # Locate extracted Plex Media Server.app
        new_app = os.path.join(extract_dir, "Plex Media Server.app")
        if not os.path.isdir(new_app):
            # Look for any .app in extract_dir
            apps = [f for f in os.listdir(extract_dir) if f.endswith(".app")]
            if apps:
                new_app = os.path.join(extract_dir, apps[0])
            else:
                raise RuntimeError(f"Could not locate Plex Media Server.app in {extract_dir}")

        # Remove quarantine attribute so Gatekeeper allows execution
        logger.debug("Removing macOS quarantine attribute from extracted app...")
        subprocess.run(["xattr", "-r", "-d", "com.apple.quarantine", new_app], capture_output=True)

        # Verify Apple code signature
        logger.info("Verifying application code signature...")
        cs_res = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", new_app],
            capture_output=True,
            text=True,
        )
        if cs_res.returncode != 0:
            logger.warning(f"Code signature check warning: {cs_res.stderr.strip() or 'Unknown verification issue'}")
        else:
            logger.info("Application code signature is valid.")

        # Ensure parent destination directory exists and is writable
        dest_parent = os.path.dirname(os.path.abspath(app_path))
        if not os.access(dest_parent, os.W_OK):
            raise PermissionError(f"Directory {dest_parent} is not writable. Administrator privileges may be required.")

        # Atomic replacement
        if os.path.exists(app_path):
            if os.path.exists(backup_path):
                logger.debug(f"Removing prior backup at {backup_path}")
                shutil.rmtree(backup_path, ignore_errors=True)
            logger.info(f"Backing up existing installation to {backup_path}")
            shutil.move(app_path, backup_path)

        try:
            logger.info(f"Installing new version to {app_path}...")
            shutil.move(new_app, app_path)
        except Exception as e:
            logger.error(f"Failed to install new app: {e}. Restoring backup...")
            if os.path.exists(backup_path):
                shutil.move(backup_path, app_path)
            raise e

        # Clean up backup if not requested
        if not keep_backup and os.path.exists(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)

        logger.info("Application bundle installed successfully.")

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def check_updates(config):
    """
    Checks for available updates and returns (update_available, latest_info, installed_ver).
    """
    token = detect_plex_token(config.get("token"))
    channel = config.get("channel", "public")
    app_path = config.get("app_path", DEFAULT_APP_PATH)

    installed_ver, is_running = get_current_installed_version(app_path)
    latest_info = fetch_latest_release(channel=channel, token=token)
    latest_ver = latest_info["version"]

    logger.info("--- Version Status ---")
    logger.info(f"Target App Path:     {app_path}")
    logger.info(f"Installed Version:   {installed_ver or 'Not Installed'} ({'Running' if is_running else 'Stopped'})")
    logger.info(f"Latest Available:    {latest_ver} (Channel: {channel})")

    if not installed_ver:
        return True, latest_info, None

    update_available = is_version_newer(latest_ver, installed_ver)
    return update_available, latest_info, installed_ver


def perform_update(config, force=False, ignore_streams=False, dry_run=False):
    """
    Executes the full update workflow.
    """
    token = detect_plex_token(config.get("token"))
    app_path = config.get("app_path", DEFAULT_APP_PATH)
    keep_backup = config.get("keep_backup", True)
    notify = config.get("notify", True)

    update_available, latest_info, installed_ver = check_updates(config)

    if not update_available and not force:
        logger.info("Plex Media Server is already up to date. No action needed.")
        return 0

    if force and not update_available:
        logger.info("Force flag specified. Proceeding with reinstallation of current/latest build.")

    latest_ver = latest_info["version"]
    logger.info(f"Update needed: {installed_ver or 'None'} -> {latest_ver}")

    # Check for active streams
    if config.get("check_active_streams", True) and not ignore_streams:
        streams = get_active_stream_count(token=token)
        if streams and streams > 0:
            msg = f"{streams} active playback stream(s) in progress. Postponing update to avoid interruption."
            logger.info(msg)
            if notify:
                send_macos_notification(msg, subtitle="Update Postponed")
            return 2  # Deferral status code

    if dry_run:
        logger.info("[DRY RUN] Would download and install Plex Media Server v" + latest_ver)
        return 0

    tmp_dir = tempfile.mkdtemp(prefix="plex_update_download_")
    download_zip = os.path.join(tmp_dir, f"PlexMediaServer-{latest_ver}.zip")

    try:
        # Download and verify
        download_file_with_checksum(latest_info["url"], latest_info.get("checksum"), download_zip)

        # Stop existing server
        was_running = is_plex_running()
        if was_running:
            stop_plex_server()

        # Install new app
        install_update_package(download_zip, app_path, keep_backup=keep_backup)

        # Restart server if it was running or if app exists
        if was_running or os.path.isdir(app_path):
            start_plex_server(app_path)

        success_msg = f"Plex Media Server successfully updated to v{latest_ver}"
        logger.info(success_msg)
        if notify:
            send_macos_notification(success_msg, subtitle="Update Complete")

        return 0

    except Exception as e:
        err_msg = f"Update failed: {e}"
        logger.error(err_msg, exc_info=True)
        if notify:
            send_macos_notification(f"Error: {e}", subtitle="Update Failed")
        return 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def rollback_update(config):
    """Restores the .bak version of the application."""
    app_path = config.get("app_path", DEFAULT_APP_PATH)
    backup_path = app_path.rstrip("/") + ".bak"

    if not os.path.exists(backup_path):
        logger.error(f"No backup found at {backup_path}")
        return 1

    logger.info(f"Rolling back from {backup_path} to {app_path}...")
    was_running = is_plex_running()
    if was_running:
        stop_plex_server()

    if os.path.exists(app_path):
        shutil.rmtree(app_path, ignore_errors=True)

    shutil.move(backup_path, app_path)
    logger.info("Rollback complete.")

    if was_running:
        start_plex_server(app_path)

    return 0


# ==================== Service / LaunchAgent Management ====================


def generate_launchagent_plist(script_path, config):
    """Generates the LaunchAgent property list dictionary."""
    python_bin = sys.executable
    schedule = config.get("schedule", {})
    sched_type = schedule.get("type", "interval")
    log_file = config.get("log_file", DEFAULT_LOG_PATH)
    err_log = log_file.replace(".log", ".error.log") if log_file.endswith(".log") else log_file + ".error"

    plist = {
        "Label": DEFAULT_PLIST_LABEL,
        "ProgramArguments": [python_bin, script_path, "update"],
        "RunAtLoad": True,
        "StandardOutPath": log_file,
        "StandardErrorPath": err_log,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }

    if sched_type == "calendar":
        plist["StartCalendarInterval"] = {
            "Hour": int(schedule.get("calendar_hour", 4)),
            "Minute": int(schedule.get("calendar_minute", 0)),
        }
    else:
        # Default interval in seconds
        interval_hours = float(schedule.get("interval_hours", 6))
        plist["StartInterval"] = int(interval_hours * 3600)

    return plist


def service_install(config):
    """Installs and loads the macOS LaunchAgent service."""
    script_path = os.path.abspath(__file__)
    plist_dir = os.path.dirname(DEFAULT_PLIST_PATH)
    os.makedirs(plist_dir, exist_ok=True)

    plist_dict = generate_launchagent_plist(script_path, config)

    # Write plist
    logger.info(f"Writing LaunchAgent plist to {DEFAULT_PLIST_PATH}...")
    with open(DEFAULT_PLIST_PATH, "wb") as f:
        plistlib.dump(plist_dict, f)

    # Load service
    logger.info("Loading service with launchctl...")
    # Unload first if previously loaded
    subprocess.run(["launchctl", "unload", DEFAULT_PLIST_PATH], capture_output=True)
    res = subprocess.run(["launchctl", "load", DEFAULT_PLIST_PATH], capture_output=True, text=True)

    if res.returncode == 0:
        logger.info(f"Service successfully installed and started! (Label: {DEFAULT_PLIST_LABEL})")
        logger.info(f"Logs: {config.get('log_file', DEFAULT_LOG_PATH)}")
        return 0
    else:
        logger.error(f"launchctl load failed: {res.stderr}")
        return 1


def service_uninstall():
    """Unloads and removes the macOS LaunchAgent service."""
    if not os.path.isfile(DEFAULT_PLIST_PATH):
        logger.info(f"No plist found at {DEFAULT_PLIST_PATH}. Service is not installed.")
        return 0

    logger.info("Unloading service with launchctl...")
    subprocess.run(["launchctl", "unload", DEFAULT_PLIST_PATH], capture_output=True)

    try:
        os.remove(DEFAULT_PLIST_PATH)
        logger.info(f"Removed {DEFAULT_PLIST_PATH}. Service uninstalled.")
        return 0
    except Exception as e:
        logger.error(f"Failed to remove plist file: {e}")
        return 1


def service_status():
    """Checks the live status of the LaunchAgent service."""
    logger.info("--- Service Status ---")
    logger.info(f"Plist Path:   {DEFAULT_PLIST_PATH}")
    is_installed = os.path.isfile(DEFAULT_PLIST_PATH)
    logger.info(f"Installed:    {'Yes' if is_installed else 'No'}")

    res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    is_loaded = DEFAULT_PLIST_LABEL in res.stdout
    logger.info(f"Loaded:       {'Yes' if is_loaded else 'No'}")

    if is_loaded:
        for line in res.stdout.splitlines():
            if DEFAULT_PLIST_LABEL in line:
                parts = line.split()
                pid = parts[0]
                status = parts[1]
                logger.info(f"Launchctl:    PID={pid}, Last Exit Status={status}")
                break

    return 0 if (is_installed and is_loaded) else 1


def service_start():
    """Triggers an immediate run of the LaunchAgent."""
    logger.info(f"Triggering immediate execution of {DEFAULT_PLIST_LABEL}...")
    res = subprocess.run(["launchctl", "start", DEFAULT_PLIST_LABEL], capture_output=True, text=True)
    if res.returncode != 0:
        # Fallback to kickstart if supported
        uid = os.getuid()
        res = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{DEFAULT_PLIST_LABEL}"],
            capture_output=True,
            text=True,
        )
    if res.returncode == 0:
        logger.info("Service triggered successfully.")
        return 0
    else:
        logger.error(f"Failed to trigger service: {res.stderr}")
        return 1


# ==================== CLI Entry Point ====================


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="macOS Auto-Updater for Plex Media Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", help="Path to custom config.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # check
    subparsers.add_parser("check", help="Check for updates without installing")

    # update
    update_p = subparsers.add_parser("update", help="Check and install updates if available")
    update_p.add_argument("--force", action="store_true", help="Force install even if already latest")
    update_p.add_argument("--ignore-streams", action="store_true", help="Ignore active playback streams")
    update_p.add_argument("--dry-run", action="store_true", help="Simulate update without changes")
    update_p.add_argument("--channel", choices=["public", "plexpass"], help="Override release channel")

    # status
    subparsers.add_parser("status", help="Display full status of Plex and update service")

    # rollback
    subparsers.add_parser("rollback", help="Restore previous application version from .bak")

    # service
    service_p = subparsers.add_parser("service", help="Manage macOS LaunchAgent service")
    service_sub = service_p.add_subparsers(dest="service_action", help="Service action")
    service_sub.add_parser("install", help="Install and start LaunchAgent")
    service_sub.add_parser("uninstall", help="Stop and remove LaunchAgent")
    service_sub.add_parser("status", help="Check service status")
    service_sub.add_parser("start", help="Trigger immediate update via service")

    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(log_file=config.get("log_file"), verbose=args.verbose)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "check":
        up_to_date, _, _ = check_updates(config)
        return 0

    elif args.command == "update":
        if getattr(args, "channel", None):
            config["channel"] = args.channel
        return perform_update(
            config,
            force=args.force,
            ignore_streams=args.ignore_streams,
            dry_run=args.dry_run,
        )

    elif args.command == "status":
        token = detect_plex_token(config.get("token"))
        app_path = config.get("app_path", DEFAULT_APP_PATH)
        installed_ver, is_running = get_current_installed_version(app_path)
        active_streams = get_active_stream_count(token=token) if is_running else 0

        logger.info("=== Plex Media Server Status ===")
        logger.info(f"App Bundle:       {app_path} ({'Exists' if os.path.exists(app_path) else 'Missing'})")
        logger.info(f"Running:          {'Yes' if is_running else 'No'}")
        logger.info(f"Version:          {installed_ver or 'Not installed'}")
        logger.info(f"Active Streams:   {active_streams if active_streams is not None else 'Unknown'}")
        logger.info(f"Plex Token:       {'Configured/Detected' if token else 'None'}")
        print()
        service_status()
        return 0

    elif args.command == "rollback":
        return rollback_update(config)

    elif args.command == "service":
        if args.service_action == "install":
            return service_install(config)
        elif args.service_action == "uninstall":
            return service_uninstall()
        elif args.service_action == "status":
            return service_status()
        elif args.service_action == "start":
            return service_start()
        else:
            logger.info("Usage: plex_updater.py service {install|uninstall|status|start}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

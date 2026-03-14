#!/usr/bin/env python3
"""
CraftLaunch v3 — Standalone Minecraft Launcher
Uses minecraft-launcher-lib for reliable download & launch.

Requirements:
    pip install minecraft-launcher-lib

Run:
    python craftlaunch.py
"""

# ── auto-install dependencies ──────────────────────────────────────────────
import sys, subprocess

def _ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg)
    except ImportError:
        _flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg],
            creationflags=_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

_ensure("minecraft-launcher-lib", "minecraft_launcher_lib")

# ── stdlib ─────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os, json, shutil, threading, platform, uuid, zipfile, re, traceback
from pathlib import Path
from datetime import datetime

# ── minecraft-launcher-lib ─────────────────────────────────────────────────
import minecraft_launcher_lib as mclib

# ══════════════════════════════════════════════════════════════════════════════
#  LOCAL SKIN SERVER — intercepts Minecraft's session API so custom skins
#  work offline without mods on all versions.
# ══════════════════════════════════════════════════════════════════════════════

import http.server as _http_server
import socket      as _socket
import base64      as _b64

class _LocalSkinServer:
    """
    Skin-only intercept server.
    - GET /session/minecraft/profile/<uuid>  → inject custom skin texture URL
    - GET /skin/<uuid>.png                   → serve PNG bytes
    - Everything else                        → proxy transparently to real Mojang

    This means multiplayer works normally (join/auth/hasJoined go to Mojang)
    while the skin still appears in both singleplayer and multiplayer.
    """
    _PROXY_HOSTS = {
        "auth":     "https://authserver.mojang.com",
        "account":  "https://api.mojang.com",
        "session":  "https://sessionserver.mojang.com",
        "services": "https://api.minecraftservices.com",
    }

    def __init__(self, skin_bytes: bytes, username: str, uuid_str: str):
        self.skin_bytes = skin_bytes
        self.username   = username
        self.uuid_str   = uuid_str
        self.port       = self._free_port()
        self._server    = None

    @staticmethod
    def _free_port():
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def jvm_args(self):
        # Only intercept the session host — auth/account/services go to Mojang directly
        h = f"http://127.0.0.1:{self.port}"
        return [f"-Dminecraft.api.session.host={h}"]

    def start(self):
        _skin = self.skin_bytes
        _user = self.username
        _uuid = self.uuid_str
        _port = self.port
        _proxy_session = _LocalSkinServer._PROXY_HOSTS["session"]

        class _H(_http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path

                # ── Serve skin PNG ────────────────────────────────────────
                if path.endswith(".png") or "/skin/" in path:
                    self._send(200, "image/png", _skin)
                    return

                # ── Profile endpoint: inject our skin URL ─────────────────
                if "/session/minecraft/profile/" in path:
                    import json as _j
                    uid = _uuid.replace("-","")
                    tex_payload = _b64.b64encode(_j.dumps({
                        "timestamp":   0,
                        "profileId":   uid,
                        "profileName": _user,
                        "isPublic":    True,
                        "textures": {"SKIN": {
                            "url": f"http://127.0.0.1:{_port}/skin/{uid}.png",
                            "metadata": {"model": "default"}
                        }}
                    }).encode()).decode()
                    body = _j.dumps({
                        "id":   uid,
                        "name": _user,
                        "properties": [{"name":"textures","value":tex_payload}]
                    }).encode()
                    self._send(200, "application/json", body)
                    return

                # ── Everything else → proxy to real Mojang session server ──
                import urllib.request as _ur, urllib.error as _ue
                try:
                    real_url = _proxy_session + path
                    req = _ur.Request(real_url,
                        headers={"User-Agent":"Minecraft/1.0"})
                    with _ur.urlopen(req, timeout=10) as r:
                        body = r.read()
                        ct   = r.headers.get("Content-Type","application/json")
                    self._send(r.status, ct, body)
                except _ue.HTTPError as e:
                    body = e.read() or b""
                    self._send(e.code, "application/json", body)
                except Exception:
                    self._send(200, "application/json", b"{}")

            def do_POST(self):
                # ALL POST requests (hasJoined, join) → proxy to real Mojang
                import urllib.request as _ur, urllib.error as _ue
                length = int(self.headers.get("Content-Length","0") or 0)
                body_in = self.rfile.read(length) if length else b""
                try:
                    real_url = _proxy_session + self.path
                    req = _ur.Request(real_url, data=body_in,
                        headers={"Content-Type": self.headers.get("Content-Type","application/json"),
                                 "User-Agent": "Minecraft/1.0"})
                    with _ur.urlopen(req, timeout=10) as r:
                        body_out = r.read()
                        ct = r.headers.get("Content-Type","application/json")
                    self._send(r.status, ct, body_out)
                except _ue.HTTPError as e:
                    body_out = e.read() or b""
                    self._send(e.code, "application/json", body_out)
                except Exception:
                    self._send(204, "application/json", b"")

            def _send(self, code, ct, body):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *a): pass   # silence request log

        self._server = _http_server.HTTPServer(("127.0.0.1", _port), _H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None


# ══════════════════════════════════════════════════════════════════════════════
#  PATHS & CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

APP      = "CraftLaunch"
VER      = "5.0"
BASE     = Path.home() / ".craftlaunch"
MC_DIR   = BASE / "minecraft"
INST_DIR = BASE / "instances"
MODS_LIB = BASE / "mods_library"
PROFILES_FILE = BASE / "profiles.json"
SETTINGS_FILE = BASE / "settings.json"

LOADERS = ["Vanilla", "Forge", "Fabric", "NeoForge", "Quilt", "OptiFine"]

# ── colour palette (overridden by Solar Edition palette below) ──────────────
C = {}

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def ensure_dirs():
    for d in [BASE, MC_DIR, INST_DIR, MODS_LIB]:
        d.mkdir(parents=True, exist_ok=True)

def load_json(p, default):
    try:
        if Path(p).exists():
            return json.loads(Path(p).read_text("utf-8"))
    except Exception:
        pass
    return default

def save_json(p, d):
    Path(p).write_text(json.dumps(d, indent=2), encoding="utf-8")

def fmt_bytes(b):
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def java_major(ver_str):
    """Parse major version number from java version string like 17.0.1 or 1.8.0_202."""
    try:
        parts = ver_str.split(".")
        major = int(parts[0])
        if major == 1:          # old style: 1.8 -> 8
            major = int(parts[1])
        return major
    except Exception:
        return 0

def get_all_javas():
    """Return list of (path, version_string, major_int) for all found Java installs."""
    candidates = ["java"]
    if platform.system() == "Windows":
        for root in [
            os.environ.get("PROGRAMFILES", "C:\\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"),
            "C:\\Program Files\\Eclipse Adoptium",
            "C:\\Program Files\\Eclipse Foundation",
            "C:\\Program Files\\Microsoft",
            "C:\\Program Files\\Java",
        ]:
            rp = Path(root)
            if rp.exists():
                for jp in rp.rglob("java.exe"):
                    if "jre" in str(jp).lower() or "jdk" in str(jp).lower() or "temurin" in str(jp).lower():
                        candidates.append(str(jp))
    if platform.system() == "Darwin":
        jvms = Path("/Library/Java/JavaVirtualMachines")
        if jvms.exists():
            for jp in jvms.rglob("java"):
                candidates.append(str(jp))
    for extra in ["/usr/bin/java", "/usr/local/bin/java"]:
        candidates.append(extra)

    results = []
    seen = set()
    for c in candidates:
        if c in seen: continue
        seen.add(c)
        try:
            _kw = {"creationflags": 0x08000000} if sys.platform=="win32" else {}
            r = subprocess.run([c, "-version"],
                               capture_output=True, text=True, timeout=5, **_kw)
            out = r.stderr + r.stdout
            m = re.search(r'version "([^"]+)"', out)
            if m:
                ver = m.group(1)
                results.append((c, ver, java_major(ver)))
        except Exception:
            pass
    return results

def find_java(min_version=8):
    """Find best Java >= min_version. Returns (path, version_string) or (None, None)."""
    javas = get_all_javas()
    # Filter by minimum version, prefer highest version
    suitable = [(p, v, maj) for p, v, maj in javas if maj >= min_version]
    if suitable:
        suitable.sort(key=lambda x: x[2], reverse=True)
        return suitable[0][0], suitable[0][1]
    # Fall back to any java found
    if javas:
        return javas[0][0], javas[0][1]
    return None, None

def get_required_java_version(version_id):
    """Read the javaVersion from the installed version JSON, default 8."""
    ver_json = MC_DIR / "versions" / version_id / f"{version_id}.json"
    try:
        data = json.loads(ver_json.read_text("utf-8"))
        return data.get("javaVersion", {}).get("majorVersion", 8)
    except Exception:
        return 8

def get_mc_versions():
    """Return list of version dicts from Mojang via minecraft-launcher-lib."""
    return mclib.utils.get_version_list()

def is_installed(version_id):
    """Check if a version JAR exists."""
    jar = MC_DIR / "versions" / version_id / f"{version_id}.jar"
    return jar.exists()

# ══════════════════════════════════════════════════════════════════════════════
#  INSTALL  (minecraft-launcher-lib)
# ══════════════════════════════════════════════════════════════════════════════

def install_minecraft(version_id, log_cb, progress_cb, status_cb):
    """
    Download & install Minecraft version_id into MC_DIR.
    Uses minecraft-launcher-lib's install_minecraft_version with callbacks.
    """
    MC_DIR.mkdir(parents=True, exist_ok=True)

    total = [1]
    done  = [0]

    def set_status(s):
        status_cb(s)
        log_cb(s, "info")

    def set_max(m):
        total[0] = max(m, 1)

    def set_progress(current):
        done[0] = current
        pct = (current / total[0]) * 100
        progress_cb(min(pct, 100))

    callback = {
        "setStatus":   set_status,
        "setProgress": set_progress,
        "setMax":      set_max,
    }

    log_cb(f"Installing Minecraft {version_id} into {MC_DIR}", "info")

    mclib.install.install_minecraft_version(
        version=version_id,
        minecraft_directory=str(MC_DIR),
        callback=callback,
    )

    log_cb(f"Minecraft {version_id} installed successfully!", "success")


# ══════════════════════════════════════════════════════════════════════════════
#  LOADER INSTALL  (Forge / Fabric / Quilt / NeoForge)
# ══════════════════════════════════════════════════════════════════════════════

def get_fabric_versions(mc_version):
    """Return list of Fabric loader versions for a given MC version."""
    try:
        loaders = mclib.fabric.get_all_loader_versions()
        return [l["version"] for l in loaders]
    except Exception:
        return []

def get_quilt_versions(mc_version):
    """Return list of Quilt loader versions."""
    try:
        loaders = mclib.quilt.get_all_loader_versions()
        return [l["version"] for l in loaders]
    except Exception:
        return []

def install_fabric(mc_version, loader_version, log_cb, progress_cb, status_cb):
    """Install Fabric loader for given MC version."""
    total = [1]; done = [0]
    def set_status(s): status_cb(s); log_cb(s, "info")
    def set_max(m): total[0] = max(m, 1)
    def set_progress(c): done[0] = c; progress_cb(min((c/total[0])*100, 100))
    callback = {"setStatus": set_status, "setProgress": set_progress, "setMax": set_max}

    log_cb(f"Installing Fabric {loader_version} for MC {mc_version}…", "info")
    if loader_version:
        mclib.fabric.install_fabric(
            minecraft_version=mc_version,
            minecraft_directory=str(MC_DIR),
            loader_version=loader_version,
            callback=callback,
        )
    else:
        mclib.fabric.install_fabric(
            minecraft_version=mc_version,
            minecraft_directory=str(MC_DIR),
            callback=callback,
        )
    log_cb("Fabric installed!", "success")

def install_quilt(mc_version, loader_version, log_cb, progress_cb, status_cb):
    """Install Quilt loader."""
    total = [1]; done = [0]
    def set_status(s): status_cb(s); log_cb(s, "info")
    def set_max(m): total[0] = max(m, 1)
    def set_progress(c): done[0] = c; progress_cb(min((c/total[0])*100, 100))
    callback = {"setStatus": set_status, "setProgress": set_progress, "setMax": set_max}

    log_cb(f"Installing Quilt {loader_version} for MC {mc_version}…", "info")
    if loader_version:
        mclib.quilt.install_quilt(
            minecraft_version=mc_version,
            minecraft_directory=str(MC_DIR),
            loader_version=loader_version,
            callback=callback,
        )
    else:
        mclib.quilt.install_quilt(
            minecraft_version=mc_version,
            minecraft_directory=str(MC_DIR),
            callback=callback,
        )
    log_cb("Quilt installed!", "success")

def install_forge(mc_version, forge_version, java_path, log_cb, progress_cb, status_cb):
    """Install Forge — downloads installer jar and runs it."""
    import urllib.request, tempfile
    total = [1]; done = [0]
    def set_status(s): status_cb(s); log_cb(s, "info")
    def set_max(m): total[0] = max(m, 1)
    def set_progress(c): done[0] = c; progress_cb(min((c/total[0])*100, 100))
    callback = {"setStatus": set_status, "setProgress": set_progress, "setMax": set_max}

    try:
        # Try minecraft-launcher-lib forge support first
        forge_versions = mclib.forge.list_forge_versions(mc_version)
        if not forge_versions:
            raise ValueError(f"No Forge versions found for MC {mc_version}")
        target = forge_version if forge_version in forge_versions else forge_versions[0]
        log_cb(f"Installing Forge {target} for MC {mc_version}…", "info")
        set_status(f"Downloading Forge {target}…")
        mclib.forge.install_forge_version(
            versionid=target,
            path=str(MC_DIR),
            java=java_path,
            callback=callback,
        )
        log_cb("Forge installed!", "success")
        return target
    except Exception as e:
        log_cb(f"Forge install error: {e}", "error")
        raise

def get_forge_versions(mc_version):
    """Return list of Forge version strings for a given MC version."""
    try:
        return mclib.forge.list_forge_versions(mc_version)
    except Exception:
        return []

def get_loader_version_id(mc_version, loader, loader_version=""):
    """
    Return the version ID string that was installed for a given loader.
    e.g. fabric-loader-0.15.6-1.20.4  or  1.20.4-forge-49.0.3
    """
    if loader == "Fabric":
        # Fabric version IDs are like: fabric-loader-X.Y.Z-MC
        vers_dir = MC_DIR / "versions"
        if vers_dir.exists():
            for d in vers_dir.iterdir():
                n = d.name
                if n.startswith("fabric-loader-") and n.endswith(f"-{mc_version}"):
                    return n
        return f"fabric-loader-{loader_version}-{mc_version}" if loader_version else None
    elif loader == "Quilt":
        vers_dir = MC_DIR / "versions"
        if vers_dir.exists():
            for d in vers_dir.iterdir():
                n = d.name
                if "quilt-loader" in n and mc_version in n:
                    return n
        return None
    elif loader in ("Forge", "NeoForge"):
        vers_dir = MC_DIR / "versions"
        if vers_dir.exists():
            for d in vers_dir.iterdir():
                n = d.name
                if mc_version in n and ("forge" in n.lower() or "neoforge" in n.lower()):
                    return n
        return None
    return None

def is_loader_installed(mc_version, loader):
    """Check if a loader version is installed."""
    vid = get_loader_version_id(mc_version, loader)
    if not vid:
        return False
    jar = MC_DIR / "versions" / vid / f"{vid}.jar"
    json_f = MC_DIR / "versions" / vid / f"{vid}.json"
    # Fabric/Quilt don't have a jar, just a json
    return json_f.exists()


# ══════════════════════════════════════════════════════════════════════════════
#  LAUNCH  (minecraft-launcher-lib)
# ══════════════════════════════════════════════════════════════════════════════

def build_launch_command(version_id, profile, username, uuid_str, skin_server=None):
    """Build argv list using minecraft-launcher-lib.
    If skin_server is provided, its JVM args are injected to redirect
    Minecraft's session API to our local skin server.
    """
    game_dir = Path(profile.get("game_dir") or INST_DIR / profile["name"])
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "mods").mkdir(exist_ok=True)

    java_path = profile.get("java_path") or "auto"
    if java_path in ("", "auto"):
        found, _ = find_java()
        java_path = found or "java"

    jvm_args = profile.get("jvm_args", "-Xmx2G -Xms512M").split()
    if skin_server:
        jvm_args = skin_server.jvm_args() + jvm_args

    loader = profile.get("loader", "Vanilla")
    launch_version = version_id
    if loader not in ("Vanilla", "OptiFine", ""):
        loader_vid = get_loader_version_id(version_id, loader)
        if loader_vid:
            launch_version = loader_vid

    options = mclib.types.MinecraftOptions(
        username=username,
        uuid=uuid_str,
        token="0",
        jvmArguments=jvm_args,
        gameDirectory=str(game_dir),
        executablePath=java_path,
    )

    return mclib.command.get_minecraft_command(
        version=launch_version,
        minecraft_directory=str(MC_DIR),
        options=options,
    )

# ══════════════════════════════════════════════════════════════════════════════
#  CraftLaunch  v5  —  "Solar" Edition
#  Inspired by Lunar Client  |  Requires: Pillow, Python 3.8+
# ══════════════════════════════════════════════════════════════════════════════
_ensure("Pillow", "PIL")
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
import hashlib as _hashlib, io as _io, math as _math, random as _random

# ─── Palette (Lunar-inspired: near-black, electric blue, crisp whites) ────────
C = {
    "void":    "#03040A",   # deepest background
    "base":    "#060710",
    "bg":      "#080A14",
    "panel":   "#0B0D18",
    "card":    "#0E1020",
    "card2":   "#111424",
    "card3":   "#15192E",
    "lift":    "#1A1E38",   # hover lift
    "border":  "#1A2040",
    "border2": "#1E2D60",
    "sep":     "#131828",

    "solar":   "#3B82F6",   # primary electric blue
    "solar2":  "#1D4ED8",   # deeper blue
    "solar3":  "#60A5FA",   # lighter blue
    "glow":    "#93C5FD",   # soft glow
    "pulse":   "#BFDBFE",
    "cyan":    "#06B6D4",   # accent cyan (Lunar-style)
    "cyan2":   "#0891B2",

    "green":   "#22C55E",
    "emerald": "#10B981",
    "amber":   "#F59E0B",
    "orange":  "#F97316",
    "red":     "#EF4444",
    "rose":    "#F43F5E",
    "violet":  "#7C3AED",
    "indigo":  "#4F46E5",

    "text":    "#F1F5F9",
    "sub":     "#94A3B8",
    "muted":   "#475569",
    "dim":     "#1A2438",
    "white":   "#FFFFFF",
    "black":   "#000000",
}

FF   = "Segoe UI"
FFB  = "Segoe UI Semibold"
MONO = "Consolas"


# ─── Colour utilities ─────────────────────────────────────────────────────────
def _h(h, a=255):
    h = h.lstrip("#")
    if len(h) == 3: h = h[0]*2 + h[1]*2 + h[2]*2
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16), a

def _blend(c1, c2, t):
    def _e(h):
        h = h.lstrip("#")
        if len(h)==3: h=h[0]*2+h[1]*2+h[2]*2
        return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    r1,g1,b1 = _e(c1); r2,g2,b2 = _e(c2)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t))

def _rgba(c, a=255):
    r,g,b,_ = _h(c, a)
    return (r, g, b, a)

def _tk(img):
    return ImageTk.PhotoImage(img)


# ─── PIL drawing helpers ──────────────────────────────────────────────────────
def _pill_img(w, h, r, fill, alpha=255, border=None, border_alpha=200,
              glow=None, glow_radius=12):
    img = Image.new("RGBA", (w, h), (0,0,0,0))
    d   = ImageDraw.Draw(img)
    if glow:
        for i in range(glow_radius, 0, -1):
            a2 = int(55 * (i/glow_radius) ** 2)
            d.rounded_rectangle([i,i,w-1-i,h-1-i],
                                 radius=min(r+i//2, min(w,h)//2-1),
                                 outline=_rgba(glow, a2), width=1)
    d.rounded_rectangle([0,0,w-1,h-1], radius=r, fill=_rgba(fill, alpha))
    if border:
        d.rounded_rectangle([0,0,w-1,h-1], radius=r,
                             outline=_rgba(border, border_alpha), width=1)
    return img


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGET HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _btn(parent, text, command=None, bg=None, fg="#fff",
         font=None, padx=14, pady=8, radius=8, hover_bg=None, **kw):
    """Flat button with PIL-drawn rounded background + smooth hover."""
    bg        = bg or C["solar"]
    hover_bg  = hover_bg or _blend(bg, "#ffffff", 0.18)
    fn        = font or (FFB, 10)
    try:
        _parent_bg = parent.cget("bg")
    except Exception:
        _parent_bg = C["bg"]
    cvs       = tk.Canvas(parent, bg=_parent_bg,
                          highlightthickness=0, cursor="hand2")
    cvs.configure(width=1, height=1)    # will be resized on pack/place

    _imgs = {}

    def _draw(w, h, hover=False):
        col = hover_bg if hover else bg
        pil = _pill_img(w, h, radius, col, border=_blend(col,"#ffffff",0.15))
        _imgs["p"] = _tk(pil)
        cvs.delete("all")
        cvs.create_image(0, 0, image=_imgs["p"], anchor="nw")
        cvs.create_text(w//2, h//2, text=text, font=fn,
                        fill=fg, anchor="center")

    def _resize(e=None):
        w = cvs.winfo_width(); h = cvs.winfo_height()
        if w > 4 and h > 4: _draw(w, h, _imgs.get("hov", False))

    def _enter(e):
        _imgs["hov"] = True
        w,h = cvs.winfo_width(), cvs.winfo_height()
        if w > 4: _draw(w, h, True)

    def _leave(e):
        _imgs["hov"] = False
        w,h = cvs.winfo_width(), cvs.winfo_height()
        if w > 4: _draw(w, h, False)

    cvs.bind("<Configure>", _resize)
    cvs.bind("<Enter>",     _enter)
    cvs.bind("<Leave>",     _leave)
    if command:
        cvs.bind("<Button-1>", lambda e: command())

    # Provide .pack() / .grid() / .place() delegates + configure
    cvs._padx = padx; cvs._pady = pady
    return cvs


def _icon_btn(parent, icon, command=None, size=40, color=None,
              tooltip=None, active=False):
    """Circle icon button for sidebar."""
    color = color or C["solar"]
    cvs   = tk.Canvas(parent, bg=parent.cget("bg"),
                      highlightthickness=0, cursor="hand2")
    cvs.configure(width=size, height=size)
    _imgs = {}

    def _draw(hover=False, act=False):
        img = Image.new("RGBA", (size,size), (0,0,0,0))
        d   = ImageDraw.Draw(img)
        if act:
            for i in range(8,0,-1):
                a2 = int(60*(i/8)**2)
                d.ellipse([size//2-i*2, size//2-i*2,
                           size//2+i*2, size//2+i*2],
                          fill=_rgba(color, a2))
            d.ellipse([4,4,size-5,size-5], fill=_rgba(color, 220))
        elif hover:
            d.ellipse([4,4,size-5,size-5], fill=_rgba(color, 50))
        else:
            d.ellipse([4,4,size-5,size-5], fill=_rgba(C["card2"], 180))
        _imgs["p"] = _tk(img)
        cvs.delete("all")
        cvs.create_image(0,0,image=_imgs["p"],anchor="nw")
        ic_color = C["white"] if act else (color if hover else C["sub"])
        cvs.create_text(size//2, size//2, text=icon,
                        font=(FF,size//3), fill=ic_color, anchor="center")

    _imgs["act"] = active
    _draw(act=active)

    cvs.bind("<Enter>",    lambda e: _draw(hover=True, act=_imgs.get("act",False)))
    cvs.bind("<Leave>",    lambda e: _draw(hover=False, act=_imgs.get("act",False)))
    if command:
        cvs.bind("<Button-1>", lambda e: command())

    def _set_active(v):
        _imgs["act"] = v; _draw(act=v)

    cvs.set_active = _set_active
    return cvs


def _card(parent, height=None, radius=12, fill=None, border=None,
          glow=None, pack_kw=None):
    """Canvas-backed rounded card. Returns (canvas, inner_frame)."""
    fill   = fill   or C["card"]
    border = border or C["border"]
    cvs    = tk.Canvas(parent, bg=C["bg"], highlightthickness=0)
    if height: cvs.configure(height=height)
    inner  = tk.Frame(cvs, bg=fill)
    win    = cvs.create_window(1, 1, window=inner, anchor="nw")
    _i     = [None]

    def _rz(e=None):
        w = cvs.winfo_width(); h = cvs.winfo_height()
        if w < 4 or h < 4: return
        pil = _pill_img(w, h, radius, fill, border=border,
                        glow=glow, glow_radius=10)
        _i[0] = _tk(pil)
        cvs.delete("bg"); cvs.create_image(0,0,image=_i[0],anchor="nw",tags="bg")
        cvs.itemconfig(win, width=w-2, height=h-2)
        cvs.tag_raise(win)

    cvs.bind("<Configure>", _rz); cvs.after(30, _rz)
    if pack_kw is not None: cvs.pack(**pack_kw)
    return cvs, inner


def _shdr(parent, text, color=None, bg=None):
    bg    = bg or C["bg"]
    color = color or C["solar"]
    f = tk.Frame(parent, bg=bg); f.pack(fill="x", pady=(12,5))
    tk.Label(f, text=text, font=(MONO,8,"bold"),
             bg=bg, fg=color).pack(side="left")
    tk.Frame(f, bg=_blend(color, bg, 0.82), height=1
             ).pack(side="left", fill="x", expand=True, padx=(10,0))


def _sep(parent, bg=None, padx=0, pady=6):
    tk.Frame(parent, bg=bg or C["sep"], height=1).pack(
        fill="x", padx=padx, pady=pady)


# ══════════════════════════════════════════════════════════════════════════════
#  PARTICLE SYSTEM  (drawn on a Canvas overlay)
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#  CURSOR GLOW  — soft radial spotlight that follows the mouse
# ══════════════════════════════════════════════════════════════════════════════
class _Particles:
    def __init__(self, canvas, n=60):
        self._cvs  = canvas
        self._n    = n
        self._pts  = []
        self._job  = None
        self._running = False
        self._init_pts()

    def _init_pts(self):
        w = max(self._cvs.winfo_width(), 1280)
        h = max(self._cvs.winfo_height(), 800)
        self._pts = []
        for _ in range(self._n):
            x  = _random.uniform(0, w)
            y  = _random.uniform(0, h)
            r  = _random.uniform(0.5, 2.5)
            sp = _random.uniform(0.1, 0.5)
            a  = _random.uniform(20, 100)
            self._pts.append([x, y, r, sp, a])

    def _tick(self):
        if not self._running:
            return
        try:
            cvs = self._cvs
            w   = cvs.winfo_width()
            h   = cvs.winfo_height()
            if w < 4:
                self._job = cvs.after(8, self._tick)
                return
            cvs.delete("pt")
            for p in self._pts:
                p[1] -= p[3]
                if p[1] < -4:
                    p[1] = h + 4
                    p[0] = _random.uniform(0, w)
                a2  = max(1, min(100, int(float(p[4]))))
                t   = a2 / 100.0
                col = "#{:02x}{:02x}{:02x}".format(
                    int(59  + 34 * t),
                    int(130 + 70 * t),
                    246)
                r = p[2]
                cvs.create_oval(p[0]-r, p[1]-r, p[0]+r, p[1]+r,
                                fill=col, outline="", tags="pt")
            self._job = cvs.after(8, self._tick)
        except (KeyboardInterrupt, SystemExit):
            self._running = False
        except Exception:
            self._running = False

    def start(self):
        self._running = True; self._tick()

    def stop(self):
        self._running = False
        if self._job:
            try: self._cvs.after_cancel(self._job)
            except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  CURSOR GLOW — soft grey-blue radial spotlight that follows the mouse
# ══════════════════════════════════════════════════════════════════════════════
class _CursorGlow:
    """Draws a soft radial glow on a Canvas at the cursor position.
    Bind root <Motion> → update position.  Renders at ~125 fps."""

    RINGS   = [(120,6),(90,12),(60,20),(36,32),(18,50)]  # (radius, brightness)
    TICK_MS = 8

    def __init__(self, canvas, root):
        self._cvs  = canvas
        self._root = root
        self._rx   = -999
        self._ry   = -999
        # Track mouse anywhere in the root window
        root.bind("<Motion>", self._on_motion, add="+")
        root.bind("<Leave>",  self._on_leave,  add="+")
        self._tick()

    def _on_motion(self, e):
        # Convert screen → canvas coords
        try:
            self._rx = e.x_root - self._cvs.winfo_rootx()
            self._ry = e.y_root - self._cvs.winfo_rooty()
        except Exception:
            pass

    def _on_leave(self, e):
        self._rx = self._ry = -999

    def _tick(self):
        cvs = self._cvs
        if not cvs.winfo_exists():
            return
        cvs.delete("cglow")
        x, y = self._rx, self._ry
        if 0 <= x <= cvs.winfo_width() and 0 <= y <= cvs.winfo_height():
            for radius, bright in self.RINGS:
                # Grey-blue tint: slightly more blue than grey
                r_val = bright
                g_val = bright + 4
                b_val = bright + 22
                cvs.create_oval(
                    x - radius, y - radius, x + radius, y + radius,
                    fill=f"#{r_val:02x}{g_val:02x}{b_val:02x}",
                    outline="", tags="cglow")
            # Tiny bright core dot
            cvs.create_oval(x-3, y-3, x+3, y+3,
                            fill="#b0c8f8", outline="", tags="cglow")
        cvs.after(self.TICK_MS, self._tick)


# ══════════════════════════════════════════════════════════════════════════════
#  ANIMATED SPLASH SCREEN
# ══════════════════════════════════════════════════════════════════════════════
class _Splash:
    """Solar Edition animated loading splash."""
    SW, SH = 480, 300

    def __init__(self, root, on_done, accent=None, label="Solar Edition"):
        self._root    = root
        self._done_cb = on_done
        self._accent  = accent or C["solar"]
        self._label   = label
        self._tick    = 0
        self._ico_ref = None
        self._msgs    = ["Initializing…", "Loading profiles…",
                         "Checking Java…", "Almost ready…"]

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.configure(bg=C["void"])
        self._win.geometry(
            f"{self.SW}x{self.SH}+{(sw-self.SW)//2}+{(sh-self.SH)//2}")
        self._win.lift()
        self._win.attributes("-topmost", True)

        tk.Frame(self._win, bg=C["border2"]).place(
            x=0, y=0, relwidth=1, relheight=1)
        self._cvs = tk.Canvas(self._win, bg=C["bg"],
                              highlightthickness=0,
                              width=self.SW-2, height=self.SH-2)
        self._cvs.place(x=1, y=1)
        self._win.after(10, self._load_ico)
        self._win.after(30, self._frame)

    def _rgb(self, h):
        h = h.lstrip("#")
        return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)

    def _load_ico(self):
        try:
            p = Path(__file__).parent / "craftlaunch_icon.png"
            img = Image.open(str(p)).convert("RGBA").resize((64,64), Image.LANCZOS)
            self._ico_ref = ImageTk.PhotoImage(img)
        except:
            img = Image.new("RGBA", (64,64), (0,0,0,0))
            d   = ImageDraw.Draw(img)
            d.ellipse([4,4,60,60], fill=(27,51,122,255))
            self._ico_ref = ImageTk.PhotoImage(img)

    def _frame(self):
        if not self._win.winfo_exists(): return
        cvs = self._cvs
        W   = self.SW - 2
        H   = self.SH - 2
        t   = self._tick
        cvs.delete("all")

        # background gradient
        for y in range(H):
            p = y/H
            cvs.create_line(0,y,W,y,
                fill=f"#{int(8+p*6):02x}{int(10+p*5):02x}{int(20+p*16):02x}")

        # corner accent glow
        ar, ag, ab = self._rgb(self._accent)
        for i in range(55, 0, -5):
            a = int(14*(i/55)**2)
            cvs.create_oval(-i*2,-i*2, i*2,i*2,
                fill=f"#{int(ar*a/255):02x}{int(ag*a/255):02x}{int(ab*a/255):02x}",
                outline="")

        # pulsing rings around icon
        cx, cy = W//2, H//2 - 26
        for ring in range(4):
            phase = ((t/60.0 + ring*0.25) % 1.0)
            r2    = 38 + ring*14 + int(phase*8)
            alpha = int(120*(1-phase)**1.8)
            cc = f"#{int(ar*alpha/255):02x}{int(ag*alpha/255):02x}{int(ab*alpha/255):02x}"
            cvs.create_oval(cx-r2, cy-r2, cx+r2, cy+r2, outline=cc, width=2)

        # icon backing circle + icon
        cvs.create_oval(cx-34, cy-34, cx+34, cy+34,
                        fill=C["card2"], outline=self._accent, width=1)
        if self._ico_ref:
            cvs.create_image(cx, cy, image=self._ico_ref, anchor="center")
        else:
            cvs.create_text(cx, cy, text="⛏", font=(FF,26),
                            fill=self._accent, anchor="center")

        # title text
        cvs.create_text(W//2, cy+50, text=APP,
                        font=(FFB,22), fill=C["text"], anchor="center")
        cvs.create_text(W//2, cy+73, text=self._label,
                        font=(MONO,9), fill=self._accent, anchor="center")

        # loading message
        msg = self._msgs[min(t//22, len(self._msgs)-1)]
        cvs.create_text(W//2, H-46, text=msg,
                        font=(FF,9), fill=C["sub"], anchor="center")

        # progress bar
        pct    = min(t/88.0, 1.0)
        bx0    = 44;  by = H-28;  bar_w = W-88
        cvs.create_rectangle(bx0, by, bx0+bar_w, by+5, fill=C["card2"], outline="")
        filled = int(bar_w*pct)
        if filled > 0:
            cvs.create_rectangle(bx0, by, bx0+filled, by+5,
                                  fill=self._accent, outline="")
            # shimmer sweep
            sx = bx0 + (t*5) % (bar_w+60) - 30
            if bx0 < sx < bx0+filled:
                cvs.create_rectangle(max(bx0,sx), by,
                                      min(bx0+filled, sx+36), by+5,
                                      fill=C["glow"], outline="")
        cvs.create_text(W//2, by-8, text=f"{int(pct*100)}%",
                        font=(MONO,8), fill=C["muted"], anchor="center")

        # bouncing dots
        for d in range(3):
            ph = ((t/18.0 + d*0.4) % 1.0)
            al = int(200*abs(1-ph*2))
            dc = f"#{int(ar*al/255):02x}{int(ag*al/255):02x}{int(ab*al/255):02x}"
            dx = W//2 - 14 + d*14
            cvs.create_oval(dx-3, H-13, dx+3, H-7, fill=dc, outline="")

        self._tick += 1
        if t < 90:
            self._win.after(25, self._frame)
        else:
            self._win.after(60, self._close)

    def _close(self):
        try: self._win.destroy()
        except: pass
        self._done_cb()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

class CraftLaunch:

    # ─── Init ─────────────────────────────────────────────────────────────────
    def __init__(self):
        ensure_dirs()
        self.profiles    = load_json(PROFILES_FILE, [self._dflt_profile()])
        self.settings    = load_json(SETTINGS_FILE,  self._dflt_settings())
        self.cur         = 0
        self.mc_versions = []
        self._installing = False
        self._cancel     = False
        self._game_proc  = None
        self._cur_page   = "home"

        self._build_root()
        # Hide root visually during splash — withdraw/deiconify breaks on
        # Windows overrideredirect windows; alpha=0 is reliable everywhere
        self.root.attributes("-alpha", 0)
        _Splash(self.root, on_done=self._post_splash,
                accent=C["solar"], label=f"Solar Edition  ·  v{VER}")

    def _post_splash(self):
        self._build_layout()
        self._nav("home")
        self._reload_profiles()
        self.root.attributes("-alpha", 0.92)   # restore glass transparency
        self.root.lift()
        self._setup_taskbar()                   # show in taskbar AFTER window is visible
        threading.Thread(target=self._bg_manifest, daemon=True).start()
        threading.Thread(target=self._bg_java,     daemon=True).start()

    def _setup_taskbar(self):
        """Force overrideredirect window into Windows taskbar and enable iconify."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            GWL_EXSTYLE      = -20
            WS_EX_APPWINDOW  = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            # The style change only takes effect after a hide+show cycle
            self.root.withdraw()
            def _reshowico():
                self.root.deiconify()
                self.root.attributes("-alpha", 0.92)
                self.root.lift()
                try: self.root.iconphoto(True, self._root_ico)
                except Exception: pass
            self.root.after(20, _reshowico)
        except Exception:
            pass

    def _quit(self):
        """Clean shutdown — stops background threads, destroys window, exits."""
        try: self._particles.stop()
        except Exception: pass
        try: self.root.destroy()
        except Exception: pass
        sys.exit(0)

    def _toggle_max(self):
        """Maximize / restore — works with overrideredirect."""
        if getattr(self, "_maximized", False):
            self.root.geometry(getattr(self, "_restore_geo", "1280x800"))
            self._maximized = False
        else:
            self._restore_geo = self.root.geometry()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{sw}x{sh}+0+0")
            self._maximized = True

    def _dflt_profile(self):
        return {"name":"Survival","version":"1.20.4",
                "loader":"Vanilla","loader_version":"",
                "java_path":"auto","jvm_args":"-Xmx2G -Xms512M",
                "game_dir":"","resolution_width":"854",
                "resolution_height":"480","mods":[],
                "icon":"⛏","skin_path":"","skin_model":"classic",
                "created":datetime.now().isoformat()}

    def _dflt_settings(self):
        return {"username":"Player","uuid":str(uuid.uuid4()),
                "java_path":"auto","close_on_launch":False}

    # ─── Root window ──────────────────────────────────────────────────────────
    def _build_root(self):
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.configure(bg=C["void"])
        self.root.overrideredirect(True)
        self.root.minsize(1100, 700)
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        self.root.geometry(f"1280x800+{(sw-1280)//2}+{(sh-800)//2}")
        # Set taskbar / window icon
        try:
            _ico_p = Path(__file__).parent / "craftlaunch_icon.png"
            if _ico_p.exists():
                _ico_img = Image.open(str(_ico_p)).convert("RGBA").resize((64,64), Image.LANCZOS)
                self._root_ico = ImageTk.PhotoImage(_ico_img)
                self.root.iconphoto(True, self._root_ico)
        except Exception:
            pass

        s = ttk.Style(self.root); s.theme_use("clam")
        # 20% glass transparency
        try: self.root.attributes("-alpha", 0.92)
        except: pass

        # Combobox
        s.configure("S.TCombobox",
            fieldbackground=C["card2"], background=C["card2"],
            foreground=C["text"], selectbackground=C["solar2"],
            selectforeground=C["white"], arrowcolor=C["solar3"],
            bordercolor=C["border2"], padding=6)
        s.map("S.TCombobox", fieldbackground=[("readonly", C["card2"])])

        # Progress bars
        for name, col in [("S", C["solar"]), ("G", C["green"]), ("A", C["amber"]), ("R", C["rose"])]:
            s.configure(f"{name}.Horizontal.TProgressbar",
                troughcolor=C["card2"], background=col,
                bordercolor=C["border"], thickness=18)

        # Treeview
        s.configure("TV.Treeview",
            background=C["card"], foreground=C["text"],
            fieldbackground=C["card"], rowheight=36,
            borderwidth=0, font=(MONO,9))
        s.configure("TV.Treeview.Heading",
            background=C["card2"], foreground=C["solar3"],
            borderwidth=0, font=(FFB,9), relief="flat")
        s.map("TV.Treeview",
              background=[("selected", C["solar2"])],
              foreground=[("selected", C["white"])])

        # Scrollbars
        for n in ("Vertical.TScrollbar", "Vert.TScrollbar"):
            s.configure(n, background=C["card2"], troughcolor=C["panel"],
                        arrowcolor=C["muted"], bordercolor=C["panel"])


    # ─── Master layout ────────────────────────────────────────────────────────
    def _build_layout(self):
        root = self.root
        SB_W = 240; TB_H = 46

        # 1px window border
        tk.Frame(root, bg=C["border2"]).place(x=0,y=0,relwidth=1,relheight=1)
        base = tk.Frame(root, bg=C["bg"])
        base.place(x=1,y=1,relwidth=1,relheight=1,width=-2,height=-2)

        # ── Title bar ─────────────────────────────────────────────────────
        self._tb = tk.Canvas(base, bg=C["void"], highlightthickness=0, height=TB_H)
        self._tb.place(x=0,y=0,relwidth=1,height=TB_H)
        self._tb.bind("<ButtonPress-1>",
            lambda e: (setattr(self,"_drag_x",e.x_root-root.winfo_x()),
                       setattr(self,"_drag_y",e.y_root-root.winfo_y())))
        self._tb.bind("<B1-Motion>",
            lambda e: root.geometry(f"+{e.x_root-self._drag_x}+{e.y_root-self._drag_y}"))
        self._tb.bind("<Configure>", lambda e: self._draw_tb())
        self._drag_x = 0; self._drag_y = 0
        root.after(50, self._draw_tb)

        # ── Sidebar ───────────────────────────────────────────────────────
        self._sb_frame = tk.Frame(base, bg=C["void"])
        self._sb_frame.place(x=0, y=TB_H, width=SB_W, relheight=1, height=-TB_H)
        self._sb_frame.pack_propagate(False)
        tk.Frame(base, bg=C["border2"]).place(x=SB_W, y=TB_H, width=1, relheight=1, height=-TB_H)

        # ── Content ───────────────────────────────────────────────────────
        content_wrap = tk.Frame(base, bg=C["bg"])
        content_wrap.place(x=SB_W+1, y=TB_H, relwidth=1, width=-(SB_W+1), relheight=1, height=-TB_H)

        self._bg_cvs = tk.Canvas(content_wrap, bg=C["bg"], highlightthickness=0)
        self._bg_cvs.place(relx=0,rely=0,relwidth=1,relheight=1)
        self._particles = _Particles(self._bg_cvs, n=60)
        self._particles.start()
        self._cursor_glow = _CursorGlow(self._bg_cvs, root)

        self._main = tk.Frame(content_wrap, bg=C["bg"])
        self._main.place(relx=0,rely=0,relwidth=1,relheight=1)

        self._build_sidebar()

        self._pages = {}
        self._pages["home"]     = self._pg_home()
        self._pages["install"]  = self._pg_install()
        self._pages["profiles"] = self._pg_profiles()
        self._pages["mods"]     = self._pg_mods()
        self._pages["skin"]     = self._pg_skin()
        self._pages["settings"] = self._pg_settings()
        self._pages["console"]  = self._pg_console()

    def _draw_tb(self):
        tb = self._tb; w = tb.winfo_width() or 1280
        tb.delete("all")
        # gradient background
        for y in range(46):
            t=y/46; rv=int(3+t*6); gv=int(4+t*6); bv=int(9+t*16)
            tb.create_line(0,y,w,y, fill=f"#{rv:02x}{gv:02x}{bv:02x}")
        # bottom accent lines
        tb.create_line(0,45,w,45, fill=C["solar2"])
        tb.create_line(0,44,w,44, fill=C["border"])
        # solar left glow
        for y in range(46):
            t=1-abs(y-23)/23; a=int(255*t**2); r,g,b=_h(C["solar"])[:3]
            tb.create_line(0,y,3,y, fill=f"#{int(r*a/255):02x}{int(g*a/255):02x}{int(b*a/255):02x}")
        # app icon
        if not hasattr(self,"_tb_ico"):
            try:
                p2 = Path(__file__).parent/"craftlaunch_icon.png"
                self._tb_ico = _tk(Image.open(str(p2)).convert("RGBA").resize((26,26),Image.LANCZOS)) if p2.exists() else None
            except: self._tb_ico = None
        if self._tb_ico:
            tb.create_image(18,23,image=self._tb_ico,anchor="center")
        else:
            tb.create_text(18,23,text="⛏",font=(FF,13),fill=C["solar"],anchor="center")
        tb.create_text(38,15,text=APP,font=(FFB,12),fill=C["text"],anchor="w")
        tb.create_text(38,31,text=f"Solar Edition  ·  v{VER}",font=(MONO,8),fill=C["solar3"],anchor="w")
        # Window controls — unbind first to prevent accumulation across redraws
        BTN_SPECS = [
            ("—", C["sub"],  C["text"],   self.root.iconify),
            ("□", C["sub"],  C["solar3"], self._toggle_max),
            ("✕", C["rose"], C["white"],  self._quit),
        ]
        for i,(sym,col2,hcol,cmd) in enumerate(BTN_SPECS):
            x2=w-22-i*40; tag=f"wb{i}"
            for ev in ("<Button-1>","<Enter>","<Leave>"):
                try: tb.tag_unbind(tag, ev)
                except Exception: pass
            tb.create_rectangle(x2-16,3,x2+16,42, fill=C["void"],outline="",tags=tag)
            tb.create_text(x2,23,text=sym,font=(FFB,12),fill=col2,anchor="center",tags=tag)
            tb.tag_bind(tag,"<Button-1>",lambda e,c=cmd: c())
            tb.tag_bind(tag,"<Enter>",   lambda e,tg=tag,h=hcol: tb.itemconfig(tg,fill=h))
            tb.tag_bind(tag,"<Leave>",   lambda e,tg=tag,c=col2: tb.itemconfig(tg,fill=c))

    # ─── Sidebar ─────────────────────────────────────────────────────────────
    def _build_sidebar(self):
        sb = self._sb_frame

        # ── Logo ──────────────────────────────────────────────────────────
        logo_f = tk.Frame(sb, bg=C["void"]); logo_f.pack(fill="x", pady=(20,0))
        self._sb_ico_lbl = tk.Label(logo_f, bg=C["void"]); self._sb_ico_lbl.pack()
        self.root.after(100, self._set_sb_ico)

        tk.Label(sb, text=APP, font=(FFB, 15), bg=C["void"],
                 fg=C["text"]).pack(pady=(8,1))
        tk.Label(sb, text="Solar Edition", font=(MONO, 7),
                 bg=C["void"], fg=C["solar3"]).pack()

        # ── Separator ─────────────────────────────────────────────────────
        sep_f = tk.Frame(sb, bg=C["void"]); sep_f.pack(fill="x", padx=20, pady=(14,10))
        tk.Canvas(sep_f, bg=C["border2"], height=1,
                  highlightthickness=0).pack(fill="x")

        # ── Nav items ──────────────────────────────────────────────────────
        self._nav_btns = {}
        NAV = [
            ("home",     "⌂",  "Home",       C["solar"]),
            ("install",  "⬇",  "Install",    C["green"]),
            ("profiles", "◈",  "Profiles",   C["violet"]),
            ("mods",     "❖",  "Mods",       C["amber"]),
            ("skin",     "👤", "Skin",       C["rose"]),
            ("settings", "⚙",  "Settings",   C["sub"]),
            ("console",  "▶",  "Console",    C["red"]),
        ]
        nav_f = tk.Frame(sb, bg=C["void"]); nav_f.pack(fill="x", padx=10)

        for pid, ico, label, col in NAV:
            outer = tk.Frame(nav_f, bg=C["void"]); outer.pack(fill="x", pady=1)

            # 3px glow indicator on the left edge
            ind = tk.Canvas(outer, bg=C["void"], highlightthickness=0,
                            width=3, height=44); ind.pack(side="left")

            # Main clickable row
            row = tk.Frame(outer, bg=C["void"], height=44, cursor="hand2")
            row.pack(side="left", fill="x", expand=True, padx=(2, 6))
            row.pack_propagate(False)

            ico_lbl = tk.Label(row, text=ico, font=(FF, 16), bg=C["void"],
                               fg=C["muted"], width=2, anchor="center")
            ico_lbl.pack(side="left", padx=(10, 8), pady=6)

            lbl_lbl = tk.Label(row, text=label, font=(FF, 10),
                               bg=C["void"], fg=C["muted"], anchor="w")
            lbl_lbl.pack(side="left", fill="x", expand=True)

            # Bind clicks & hover
            for w2 in [outer, row, ico_lbl, lbl_lbl]:
                w2.bind("<Button-1>", lambda e, p=pid: self._nav(p))

            def _hin(e, r=row, il=ico_lbl, ll=lbl_lbl, c=col, p=pid):
                if self._cur_page != p:
                    r.config(bg=C["lift"]); il.config(bg=C["lift"], fg=c)
                    ll.config(bg=C["lift"], fg=C["text"])
            def _hout(e, r=row, il=ico_lbl, ll=lbl_lbl, p=pid):
                act = (self._cur_page == p)
                nbg = C["card2"] if act else C["void"]
                r.config(bg=nbg); il.config(bg=nbg)
                ll.config(bg=nbg, fg=C["text"] if act else C["muted"])
            for w2 in [row, ico_lbl, lbl_lbl]:
                w2.bind("<Enter>", _hin); w2.bind("<Leave>", _hout)

            self._nav_btns[pid] = (row, ico_lbl, lbl_lbl, ind, col)

        # ── Footer: java + version ─────────────────────────────────────────
        bot = tk.Frame(sb, bg=C["void"]); bot.pack(side="bottom", fill="x")
        tk.Canvas(bot, bg=C["border2"], height=1, highlightthickness=0
                  ).pack(fill="x", padx=10, pady=(0,0))
        jr = tk.Frame(bot, bg=C["void"]); jr.pack(fill="x", padx=16, pady=(8,4))
        self._java_lbl = tk.Label(jr, text="☕", font=(FF, 11),
                                   bg=C["void"], fg=C["muted"]); self._java_lbl.pack(side="left")
        self._java_ver_sb = tk.Label(jr, text="Java", font=(MONO, 7),
                                      bg=C["void"], fg=C["dim"]); self._java_ver_sb.pack(side="left", padx=(5,0))
        tk.Label(bot, text=f"CraftLaunch v{VER}", font=(MONO, 7),
                 bg=C["void"], fg=C["dim"]).pack(pady=(0, 10))

    def _set_sb_ico(self):
        try:
            p2 = Path(__file__).parent/"craftlaunch_icon.png"
            if p2.exists():
                img2 = Image.open(str(p2)).convert("RGBA").resize((52,52),Image.LANCZOS)
                ico  = _tk(img2)
                self._sb_ico_lbl.config(image=ico)
                self._sb_ico_lbl.image = ico
        except: pass

    # ─── Navigation ───────────────────────────────────────────────────────────
    def _nav(self, pid):
        self._cur_page = pid
        for p, f in self._pages.items(): f.pack_forget()
        self._pages[pid].pack(fill="both", expand=True)

        for p, (row, ico_lbl, lbl_lbl, ind, col) in self._nav_btns.items():
            active  = (p == pid)
            nbg     = C["card2"] if active else C["void"]
            row.config(bg=nbg)
            ico_lbl.config(bg=nbg, fg=col if active else C["muted"])
            lbl_lbl.config(bg=nbg,
                           fg=C["text"] if active else C["muted"],
                           font=(FFB,10) if active else (FF,10))
            ind.delete("all")
            if active:
                r2,g2,b2 = _h(col)[:3]
                for y in range(44):
                    t = abs(y-22)/22; a2=int(255*(1-t*t))
                    ind.create_line(0,y,2,y,
                        fill="#{:02x}{:02x}{:02x}".format(
                            int(r2*a2/255),int(g2*a2/255),int(b2*a2/255)))

        if pid == "skin": self._refresh_skin_target_list()

    # ─── Page header helper ───────────────────────────────────────────────────
    def _ph(self, parent, title, subtitle="", accent=None):
        accent = accent or C["solar"]
        hdr    = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, height=76)
        hdr.pack(fill="x")
        _phi   = [None]
        def _draw(e=None):
            w = hdr.winfo_width() or 1080
            hdr.delete("all")
            img = Image.new("RGBA",(w,76),(0,0,0,0)); d=ImageDraw.Draw(img)
            for y in range(76):
                t=y/76; rv=int(8+t*4); gv=int(10+t*4); bv=int(20+t*8)
                d.line([(0,y),(w,y)],fill=(rv,gv,bv,255))
            ar,ag,ab = _h(accent)[:3]
            for i in range(50,0,-4):
                a2=int(15*(i/50)**2)
                d.rectangle([0,0,i*5,76],fill=(int(ar*a2/255),int(ag*a2/255),int(ab*a2/255),a2))
            _phi[0]=_tk(img); hdr.create_image(0,0,image=_phi[0],anchor="nw")
            for y in range(76):
                t=1-abs(y-38)/38; a3=int(255*t**1.5)
                hdr.create_line(0,y,4,y,
                    fill="#{:02x}{:02x}{:02x}".format(int(ar*a3/255),int(ag*a3/255),int(ab*a3/255)))
            hdr.create_text(24,30,text=title,font=(FFB,20),fill=C["text"],anchor="w")
            if subtitle:
                hdr.create_text(24,56,text=subtitle,font=(FF,10),fill=C["sub"],anchor="w")
        hdr.bind("<Configure>",_draw); hdr.after(20,_draw)
        tk.Frame(parent,bg=C["border"],height=1).pack(fill="x")

    # ═══════════════════════════════════════════════════════════════════════════
    #  HOME PAGE
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_home(self):
        f = tk.Frame(self._main, bg=C["bg"])

        # ── Hero ──────────────────────────────────────────────────────────────
        hero = tk.Canvas(f, bg=C["bg"], highlightthickness=0, height=220)
        hero.pack(fill="x")
        _hi  = {}

        def _draw_hero(e=None):
            w = hero.winfo_width() or 1040
            hero.delete("all")
            img = Image.new("RGBA",(w,220),(0,0,0,0)); d = ImageDraw.Draw(img)
            # Deep gradient
            for y in range(220):
                t = y/220; r2=int(5+t*4); g2=int(6+t*6); b2=int(14+t*26)
                d.line([(0,y),(w,y)], fill=(r2,g2,b2,255))
            # Large solar nebula glow upper-right
            for i in range(130,0,-5):
                a2 = int(34*(i/130)**2.2)
                cx2 = int(w*0.82)
                d.ellipse([cx2-i*2,60-i*2,cx2+i*2,60+i*2], fill=_rgba(C["solar"],a2))
            # Cyan accent glow far right
            for i in range(70,0,-5):
                a2 = int(18*(i/70)**2)
                d.ellipse([w-i*2,-i*2,w+i*2,i*2], fill=_rgba(C["cyan"],a2))
            # Terrain silhouette (pushed lower so badge stays clear)
            pts = []
            for x in range(0,w+1,4):
                nx = x/w
                y2 = int(172 + 24*_math.sin(nx*4.1)*_math.sin(nx*7.2)
                         + 12*_math.sin(nx*2.4+1) - 4)
                pts.append((x,y2))
            pts += [(w,220),(0,220)]
            d.polygon(pts, fill=_rgba(C["panel"],255))
            pts2 = []
            for x in range(0,w+1,4):
                nx = x/w
                y2 = int(194 + 8*_math.sin(nx*8.2)*_math.cos(nx*3.4)
                         + 6*_math.sin(nx*5.8+2))
                pts2.append((x,y2))
            pts2 += [(w,220),(0,220)]
            d.polygon(pts2, fill=_rgba(C["bg"],255))
            _hi["bg"] = _tk(img); hero.create_image(0,0,image=_hi["bg"],anchor="nw")
            # Large game title
            hero.create_text(34, 50, text=APP,
                             font=(FFB,52), fill=C["white"], anchor="w")
            hero.create_text(36, 108, text="The Ultimate Offline Minecraft Launcher",
                             font=(FF,13), fill=C["sub"], anchor="w")

        hero.bind("<Configure>", _draw_hero); self.root.after(80, _draw_hero)
        tk.Frame(f, bg=C["solar2"], height=1).pack(fill="x")
        tk.Frame(f, bg=C["border"], height=1).pack(fill="x")

        # ── Scrollable body ───────────────────────────────────────────────────
        sc  = tk.Canvas(f, bg=C["bg"], highlightthickness=0)
        vsb = ttk.Scrollbar(f, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=vsb.set)
        sc.bind("<MouseWheel>",lambda e: sc.yview_scroll(-1*(e.delta//120),"units"))
        vsb.pack(side="right",fill="y"); sc.pack(fill="both",expand=True)
        body = tk.Frame(sc,bg=C["bg"])
        cw   = sc.create_window((0,0),window=body,anchor="nw")
        body.bind("<Configure>",lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>",lambda e: sc.itemconfig(cw,width=e.width))

        pad = tk.Frame(body,bg=C["bg"]); pad.pack(fill="both",expand=True,padx=28,pady=(14,24))

        # ── Two-column layout ──────────────────────────────────────────────
        cols = tk.Frame(pad,bg=C["bg"]); cols.pack(fill="both",expand=True)
        right = tk.Frame(cols,bg=C["bg"]); right.configure(width=280); right.pack_propagate(False)
        right.pack(side="right",fill="y")
        left = tk.Frame(cols,bg=C["bg"]); left.pack(side="left",fill="both",expand=True,padx=(0,16))

        # ── LEFT: Active profile card ──────────────────────────────────────
        _shdr(left,"ACTIVE PROFILE",bg=C["bg"])
        self._home_card_f = tk.Frame(left,bg=C["bg"]); self._home_card_f.pack(fill="x",pady=(0,18))
        self._draw_home_card()

        # ── LEFT: Stats row ────────────────────────────────────────────────
        _shdr(left,"QUICK STATS",bg=C["bg"])
        stats_row = tk.Frame(left,bg=C["bg"]); stats_row.pack(fill="x",pady=(0,18))
        self._sc  = {}
        stat_data = [
            ("◈","Profiles", "profiles",  C["violet"]),
            ("▣","Installed","installed",  C["green"]),
            ("❖","Mods",     "mods",       C["amber"]),
            ("☕","Java",     "java",       C["solar"]),
        ]
        for ico,lbl_t,key,col in stat_data:
            sf = tk.Frame(stats_row,bg=C["bg"]); sf.pack(side="left",fill="x",expand=True,padx=(0,6))
            # Accent top bar (plain frame — reliable)
            tk.Frame(sf,bg=col,height=3).pack(fill="x")
            # Card body
            card_body = tk.Frame(sf,bg=C["card"]); card_body.pack(fill="x",ipady=2)
            tk.Frame(sf,bg=C["border2"],height=1).pack(fill="x")
            # Number (large, colored)
            val = "…" if key=="java" else self._cnt(key)
            vl = tk.Label(card_body,text=val,font=(FFB,26),
                          bg=C["card"],fg=col,padx=14,pady=(8))
            vl.pack(anchor="w")
            tk.Label(card_body,text=f"{ico}  {lbl_t}",font=(MONO,8,"bold"),
                     bg=C["card"],fg=C["muted"],padx=14).pack(anchor="w",pady=(0,10))
            if key=="java":
                self._java_ver_lbl = vl
            else:
                self._sc[key] = vl

        # ── LEFT: Launch card ──────────────────────────────────────────────
        _shdr(left,"LAUNCH",bg=C["bg"])
        # Accent + card body (plain frames — always visible)
        lnc_wrap = tk.Frame(left,bg=C["bg"]); lnc_wrap.pack(fill="x",pady=(0,18))
        tk.Frame(lnc_wrap,bg=C["solar"],height=3).pack(fill="x")
        lnc_body = tk.Frame(lnc_wrap,bg=C["card"]); lnc_body.pack(fill="x")
        tk.Frame(lnc_wrap,bg=C["solar2"],height=1).pack(fill="x")

        lnc_left = tk.Frame(lnc_body,bg=C["card"]); lnc_left.pack(side="left",padx=(18,10),pady=16)
        self._launch_btn = _btn(lnc_left,"▶   PLAY",self._do_launch,
                                bg=C["solar"],fg=C["white"],font=(FFB,14),padx=28,pady=14,radius=8)
        self._launch_btn.pack(); self._launch_btn.configure(width=165,height=54)

        lnc_right = tk.Frame(lnc_body,bg=C["card"])
        lnc_right.pack(side="left",fill="both",expand=True,padx=(4,20),pady=16)
        self._prog_var = tk.DoubleVar()
        pbar_row = tk.Frame(lnc_right,bg=C["card"]); pbar_row.pack(fill="x",pady=(4,6))
        self._prog_bar = ttk.Progressbar(pbar_row,variable=self._prog_var,maximum=100,
                        style="S.Horizontal.TProgressbar")
        self._prog_bar.pack(side="left",fill="x",expand=True)
        self._prog_pct = tk.Label(pbar_row,text="",font=(MONO,9,"bold"),
                                   bg=C["card"],fg=C["solar3"],width=5)
        self._prog_pct.pack(side="left",padx=(6,0))
        self._status_lbl = tk.Label(lnc_right,text="Ready to launch",
                                     font=(FF,10),bg=C["card"],fg=C["sub"])
        self._status_lbl.pack(anchor="w")

        # ── RIGHT: Quick actions ───────────────────────────────────────────
        _shdr(right,"QUICK ACTIONS",bg=C["bg"])
        qa_wrap = tk.Frame(right,bg=C["bg"]); qa_wrap.pack(fill="x",pady=(0,14))
        tk.Frame(qa_wrap,bg=C["solar"],height=3).pack(fill="x")
        qa_body = tk.Frame(qa_wrap,bg=C["card"]); qa_body.pack(fill="x")
        tk.Frame(qa_wrap,bg=C["border2"],height=1).pack(fill="x")

        ACTIONS = [
            ("⬇","Install Version",  C["green"],  lambda: self._nav("install")),
            ("➕","New Profile",      C["violet"],  self._new_profile),
            ("📂","Game Folder",      C["solar"],   self._open_mc_dir),
            ("🧩","Add Mods",         C["amber"],   lambda: self._nav("mods")),
            ("👤","Skin Changer",     C["rose"],    lambda: self._nav("skin")),
            ("📸","Screenshots",      C["cyan"],    self._open_screenshots),
        ]
        for ico,txt,col,cmd in ACTIONS:
            def _make_row(ico=ico,txt=txt,col=col,cmd=cmd):
                row2 = tk.Frame(qa_body,bg=C["card"],cursor="hand2")
                row2.pack(fill="x")
                tk.Label(row2,text=ico,font=(FF,13),bg=C["card"],
                         fg=col,padx=14,pady=10).pack(side="left")
                tk.Label(row2,text=txt,font=(FF,10),bg=C["card"],
                         fg=C["sub"]).pack(side="left",pady=10)
                def _hi(e,r=row2):
                    r.config(bg=C["lift"])
                    for c in r.winfo_children(): c.config(bg=C["lift"])
                def _lo(e,r=row2):
                    r.config(bg=C["card"])
                    for c in r.winfo_children(): c.config(bg=C["card"])
                row2.bind("<Enter>",_hi); row2.bind("<Leave>",_lo)
                row2.bind("<Button-1>",lambda e,c=cmd: c())
                for child in row2.winfo_children():
                    child.bind("<Enter>",_hi); child.bind("<Leave>",_lo)
                    child.bind("<Button-1>",lambda e,c=cmd: c())
                _sep(qa_body,bg=C["sep"],padx=0,pady=0)
            _make_row()

        # ── RIGHT: Tip card ────────────────────────────────────────────────
        _shdr(right,"TIP",bg=C["bg"])
        tip_wrap = tk.Frame(right,bg=C["bg"]); tip_wrap.pack(fill="x",pady=(0,12))
        tk.Frame(tip_wrap,bg=C["cyan"],height=3).pack(fill="x")
        tk.Label(tip_wrap,
                 text="💡  Use Profiles to manage\nmultiple Minecraft versions\nand mod loaders independently.",
                 font=(FF,9),bg=C["card"],fg=C["sub"],
                 justify="left",padx=14,pady=12,anchor="w").pack(fill="x")
        tk.Frame(tip_wrap,bg=C["border2"],height=1).pack(fill="x")

        return f

    def _draw_home_card(self):
        for w2 in self._home_card_f.winfo_children(): w2.destroy()
        if not self.profiles: return
        p    = self.profiles[self.cur]
        inst = is_installed(p["version"])
        col  = C["green"] if inst else C["amber"]

        # Outer wrapper with colored top accent bar
        wrap = tk.Frame(self._home_card_f, bg=C["bg"]); wrap.pack(fill="x")
        tk.Frame(wrap, bg=col, height=3).pack(fill="x")             # accent top
        inner = tk.Frame(wrap, bg=C["card"]); inner.pack(fill="x")
        tk.Frame(wrap, bg=C["border2"], height=1).pack(fill="x")    # bottom border

        # Left accent stripe
        tk.Frame(inner, bg=col, width=4).pack(side="left", fill="y")

        # Profile icon
        tk.Label(inner, text=p.get("icon","⛏"), font=(FF,32),
                 bg=C["card"], fg=C["solar"], padx=16, pady=12).pack(side="left")

        # Info column
        nf = tk.Frame(inner, bg=C["card"]); nf.pack(side="left", pady=14, fill="x", expand=True)
        tk.Label(nf, text=p["name"], font=(FFB,17),
                 bg=C["card"], fg=C["text"]).pack(anchor="w")
        tk.Label(nf, text=f"Minecraft {p['version']}  ·  {p['loader']}",
                 font=(FF,10), bg=C["card"], fg=C["sub"]).pack(anchor="w", pady=(3,6))
        # Status badge (plain label, always visible)
        status_text = "✓  Installed" if inst else "⚠  Not Installed"
        tk.Label(nf, text=status_text, font=(MONO,8,"bold"),
                 bg=C["card"], fg=col).pack(anchor="w")

        # Profile switcher (right side)
        sw = tk.Frame(inner, bg=C["card"]); sw.pack(side="right", padx=18)
        tk.Label(sw, text="PROFILE", font=(MONO,7,"bold"),
                 bg=C["card"], fg=C["muted"]).pack(anchor="e", pady=(0,4))
        names = [pr["name"] for pr in self.profiles]
        pvar  = tk.StringVar(value=p["name"])
        cb = ttk.Combobox(sw, textvariable=pvar, values=names,
                          state="readonly", width=14, style="S.TCombobox")
        cb.pack(pady=2)
        def _sw(e=None):
            n = pvar.get()
            idx = next((i for i,pr in enumerate(self.profiles) if pr["name"]==n), 0)
            self.cur = idx; self._draw_home_card()
        cb.bind("<<ComboboxSelected>>", _sw)

    # ═══════════════════════════════════════════════════════════════════════════
    #  INSTALL PAGE
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_install(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Install","Download any Minecraft version + mod loader",accent=C["green"])
        body=tk.Frame(f,bg=C["bg"]); body.pack(fill="both",expand=True,padx=28,pady=12)

        # ── Filter row ────────────────────────────────────────────────────────
        fb=tk.Frame(body,bg=C["bg"]); fb.pack(fill="x",pady=(0,8))
        tk.Label(fb,text="FILTER",font=(MONO,8,"bold"),bg=C["bg"],fg=C["muted"]).pack(side="left",padx=(0,10))
        self._vfilt=tk.StringVar(value="release")
        for val,lbl_t,col in [("release","Releases",C["green"]),("snapshot","Snapshots",C["amber"]),
                               ("old_beta","Beta",C["orange"]),("all","All",C["sub"])]:
            tk.Radiobutton(fb,text=lbl_t,variable=self._vfilt,value=val,
                           bg=C["bg"],fg=col,selectcolor=C["card2"],
                           activebackground=C["bg"],font=(FF,9),cursor="hand2",
                           command=self._fill_ver_tree).pack(side="left",padx=4)
        b=_btn(fb,"⟳",lambda: threading.Thread(target=self._bg_manifest,daemon=True).start(),
               bg=C["card2"],fg=C["sub"],font=(FF,10),padx=10,pady=4)
        b.pack(side="right"); b.configure(width=36,height=32)

        # ── Version treeview ──────────────────────────────────────────────────
        cols=("Version","Type","Released","Status")
        self._vtree=ttk.Treeview(body,columns=cols,show="headings",
                                  style="TV.Treeview",selectmode="browse")
        for c2,w in zip(cols,[180,120,160,120]):
            self._vtree.heading(c2,text=c2); self._vtree.column(c2,width=w,anchor="w")
        self._vtree.tag_configure("inst",foreground=C["green"])
        self._vtree.tag_configure("snap",foreground=C["amber"])
        self._vtree.tag_configure("beta",foreground=C["orange"])
        self._vtree.tag_configure("alpha",foreground=C["muted"])
        vsb=ttk.Scrollbar(body,orient="vertical",command=self._vtree.yview)
        self._vtree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); self._vtree.pack(fill="both",expand=True)
        self._vtree.bind("<<TreeviewSelect>>",self._on_ver_sel)

        # ── Loader row ────────────────────────────────────────────────────────
        loaderrow=tk.Frame(body,bg=C["bg"]); loaderrow.pack(fill="x",pady=(10,4))
        tk.Label(loaderrow,text="LOADER",font=(MONO,8,"bold"),bg=C["bg"],fg=C["muted"]).pack(side="left",padx=(0,10))
        self._inst_loader=tk.StringVar(value="Vanilla")
        LCOLS={"Vanilla":C["sub"],"Fabric":C["solar"],"Forge":C["orange"],
               "Quilt":C["violet"],"NeoForge":C["amber"]}
        for ldr in ["Vanilla","Fabric","Forge","Quilt","NeoForge"]:
            tk.Radiobutton(loaderrow,text=ldr,variable=self._inst_loader,value=ldr,
                           bg=C["bg"],fg=LCOLS[ldr],selectcolor=C["card2"],
                           activebackground=C["bg"],font=(FFB,9),cursor="hand2",
                           command=self._on_loader_change).pack(side="left",padx=4)
        tk.Label(loaderrow,text="Ver:",font=(FF,9),bg=C["bg"],fg=C["sub"]).pack(side="left",padx=(14,4))
        self._inst_loader_ver=tk.StringVar(value="latest")
        self._inst_lver_cb=ttk.Combobox(loaderrow,textvariable=self._inst_loader_ver,
                            values=["latest"],state="readonly",width=22,style="S.TCombobox")
        self._inst_lver_cb.pack(side="left")

        # ── Progress / install card ───────────────────────────────────────────
        ic,ii=_card(body,height=110,fill=C["card"],border=C["border2"],
                    pack_kw={"fill":"x","pady":(6,0)})
        lf=tk.Frame(ii,bg=C["card"]); lf.pack(side="left",fill="both",expand=True,padx=18,pady=14)
        hrow=tk.Frame(lf,bg=C["card"]); hrow.pack(fill="x",pady=(0,6))
        self._inst_ver_lbl=tk.Label(hrow,text="Select a version above",
                                     font=(FFB,13),bg=C["card"],fg=C["text"]); self._inst_ver_lbl.pack(side="left")
        self._inst_status_lbl=tk.Label(hrow,text="",font=(MONO,9),bg=C["card"],fg=C["sub"])
        self._inst_status_lbl.pack(side="left",padx=(14,0))
        prow=tk.Frame(lf,bg=C["card"]); prow.pack(fill="x",pady=(0,4))
        self._inst_prog_var=tk.DoubleVar()
        self._inst_prog_bar=ttk.Progressbar(prow,variable=self._inst_prog_var,
                            maximum=100,style="G.Horizontal.TProgressbar")
        self._inst_prog_bar.pack(side="left",fill="x",expand=True)
        self._inst_prog_pct=tk.Label(prow,text="",font=(MONO,9,"bold"),
                                      bg=C["card"],fg=C["green"],width=6); self._inst_prog_pct.pack(side="left",padx=(8,0))
        self._inst_prog_detail=tk.Label(lf,text="Choose a version and click Install",
                                         font=(MONO,8),bg=C["card"],fg=C["muted"]); self._inst_prog_detail.pack(anchor="w")
        rf=tk.Frame(ii,bg=C["card"]); rf.pack(side="right",padx=18,pady=14)
        self._inst_btn=_btn(rf,"⬇  INSTALL",self._do_install,
                            bg=C["green"],fg=C["white"],font=(FFB,12),padx=24,pady=12)
        self._inst_btn.pack(); self._inst_btn.configure(width=160,height=50)
        b2=_btn(rf,"✕ Cancel",lambda: setattr(self,"_cancel",True),
                bg=C["card2"],fg=C["rose"],font=(FF,9),padx=10,pady=5)
        b2.pack(pady=(8,0)); b2.configure(width=110,height=30)
        return f

    def _fill_ver_tree(self):
        if not hasattr(self,"_vtree"): return
        for r in self._vtree.get_children(): self._vtree.delete(r)
        filt=self._vfilt.get()
        for v in self.mc_versions:
            t=v.get("type","")
            if filt!="all" and t!=filt: continue
            raw=v.get("releaseTime","")
            date=raw.strftime("%Y-%m-%d") if hasattr(raw,"strftime") else str(raw)[:10]
            inst=is_installed(v["id"])
            tag=("inst" if inst else "snap" if t=="snapshot" else "beta" if t=="old_beta" else "alpha" if t=="old_alpha" else "")
            self._vtree.insert("","end",values=(v["id"],t,date,"✓ Installed" if inst else ""),tags=(tag,))

    def _on_ver_sel(self,e=None):
        sel=self._vtree.selection()
        if not sel: return
        ver=str(self._vtree.item(sel[0])["values"][0])
        inst=is_installed(ver)
        self._inst_ver_lbl.config(text=f"Minecraft  {ver}", fg=C["text"])
        if inst:
            self._inst_status_lbl.config(text="✓ Already installed",fg=C["green"])
            self._inst_btn.configure(bg=C["amber"])
            if hasattr(self,"_inst_prog_detail"):
                self._inst_prog_detail.config(text="Click Reinstall to re-download", fg=C["amber"])
        else:
            self._inst_status_lbl.config(text="",fg=C["sub"])
            self._inst_btn.configure(bg=C["green"])
            if hasattr(self,"_inst_prog_detail"):
                self._inst_prog_detail.config(text=f"Ready to download Minecraft {ver}", fg=C["muted"])
        if hasattr(self,"_inst_prog_var"): self._inst_prog_var.set(0)
        if hasattr(self,"_inst_prog_pct"): self._inst_prog_pct.config(text="")

    def _on_loader_change(self):
        ldr=self._inst_loader.get(); sel=self._vtree.selection()
        ver=str(self._vtree.item(sel[0])["values"][0]) if sel else ""
        if ldr in ("Fabric","Quilt","Forge") and ver:
            threading.Thread(target=self._fetch_lver,args=(ldr,ver),daemon=True).start()
        else:
            self._inst_lver_cb.config(values=["latest"]); self._inst_loader_ver.set("latest")

    def _fetch_lver(self,ldr,ver):
        try:
            if ldr=="Fabric":   v2=get_fabric_versions(ver)
            elif ldr=="Quilt":  v2=get_quilt_versions(ver)
            elif ldr=="Forge":  v2=get_forge_versions(ver)
            else: v2=[]
            v2=v2[:40] or ["latest"]
            self.root.after(0,lambda: (self._inst_lver_cb.config(values=v2),self._inst_loader_ver.set(v2[0])))
        except Exception as ex: self._log(f"Loader versions: {ex}","warn")

    def _do_install(self):
        sel=self._vtree.selection()
        if not sel: messagebox.showinfo("Select Version","Click a version first."); return
        if self._installing: messagebox.showinfo("Busy","Already installing…"); return
        ver=str(self._vtree.item(sel[0])["values"][0]); ldr=self._inst_loader.get()
        lver=self._inst_loader_ver.get()
        if lver=="latest": lver=""
        self._installing=True; self._cancel=False
        self._nav("install")
        self._log(f"═══ Installing MC {ver} [{ldr}] ═══","success")

        def _upd_prog(p, detail=""):
            p = min(int(p), 100)
            def _do():
                self._inst_prog_var.set(p)
                self._prog_var.set(p)
                txt = f"{p}%" if p > 0 else ""
                if hasattr(self,"_inst_prog_pct"): self._inst_prog_pct.config(text=txt)
                if hasattr(self,"_prog_pct"):      self._prog_pct.config(text=txt)
                if detail and hasattr(self,"_inst_prog_detail"):
                    self._inst_prog_detail.config(text=detail, fg=C["sub"])
            self.root.after(0, _do)

        def pcb(pct): _upd_prog(pct)
        def scb(s):
            self.root.after(0, lambda: (
                self._inst_status_lbl.config(text=s),
                self._inst_prog_detail.config(text=s, fg=C["sub"]),
                self._status_lbl.config(text=s),
            ))

        def run():
            try:
                scb(f"Downloading Minecraft {ver}…")
                install_minecraft(ver,self._log,pcb,scb)
                if ldr=="Fabric":
                    _upd_prog(0,"Installing Fabric loader…")
                    install_fabric(ver,lver,self._log,pcb,scb)

                elif ldr=="Quilt":
                    _upd_prog(0,"Installing Quilt loader…")
                    install_quilt(ver,lver,self._log,pcb,scb)
                elif ldr in ("Forge","NeoForge"):
                    _upd_prog(0,"Installing Forge loader…")
                    jp,_=find_java(min_version=17); jp=jp or "java"
                    install_forge(ver,lver,jp,self._log,pcb,scb)
                _upd_prog(100, "✓ Installation complete!")
                lt=f" + {ldr}" if ldr!="Vanilla" else ""
                def _done():
                    self._fill_ver_tree(); self._draw_home_card(); self._update_stats()
                    if hasattr(self,"_inst_ver_lbl"):
                        self._inst_ver_lbl.config(text=f"✓  MC {ver}{lt} installed", fg=C["green"])
                    if hasattr(self,"_inst_prog_detail"):
                        self._inst_prog_detail.config(text="Ready to play!", fg=C["green"])
                    messagebox.showinfo("Done! 🎉",f"MC {ver}{lt} installed!\nSet Profile loader to '{ldr}'.")
                self.root.after(0, _done)
            except Exception as ex:
                self._log(f"Install failed: {ex}","error"); self._log(traceback.format_exc(),"dim")
                msg=str(ex)
                def _fail(m=msg):
                    if hasattr(self,"_inst_prog_detail"):
                        self._inst_prog_detail.config(text=f"✗ Failed: {m[:60]}", fg=C["rose"])
                    messagebox.showerror("Error", m)
                self.root.after(0, _fail)
            finally:
                self._installing=False

        threading.Thread(target=run,daemon=True).start()

    # ═══════════════════════════════════════════════════════════════════════════
    #  PROFILES PAGE
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_profiles(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Profiles","Manage your game configurations",accent=C["violet"])
        body=tk.Frame(f,bg=C["bg"]); body.pack(fill="both",expand=True,padx=28,pady=10)

        # Left panel — profile list
        lf=tk.Frame(body,bg=C["panel"]); lf.pack(side="left",fill="y")
        lf.configure(width=240); lf.pack_propagate(False)
        tk.Frame(lf,bg=C["border2"]).pack(side="right",fill="y")

        lh=tk.Frame(lf,bg=C["panel"]); lh.pack(fill="x",padx=10,pady=(12,8))
        tk.Label(lh,text="PROFILES",font=(MONO,8,"bold"),bg=C["panel"],fg=C["muted"]).pack(side="left")
        tk.Button(lh,text="+ New",command=self._new_profile,
                  bg=C["violet"],fg=C["white"],font=(FFB,9),relief="flat",bd=0,
                  cursor="hand2",padx=10,pady=4,
                  activebackground=C["indigo"],activeforeground=C["white"]).pack(side="right")

        self._plist=tk.Frame(lf,bg=C["panel"]); self._plist.pack(fill="both",expand=True,padx=4)

        # Right — profile editor
        self._ped=tk.Frame(body,bg=C["bg"]); self._ped.pack(side="left",fill="both",expand=True,padx=(12,0))
        self._build_ped()
        return f

    def _reload_profiles(self):
        if not hasattr(self,"_plist"): return
        for w2 in self._plist.winfo_children(): w2.destroy()
        for i,p in enumerate(self.profiles):
            active=(i==self.cur)
            row=tk.Frame(self._plist,bg=C["card"] if active else C["panel"],cursor="hand2")
            row.pack(fill="x",pady=1)
            if active:
                tk.Frame(row,bg=C["violet"],width=3).pack(side="left",fill="y")
            tk.Label(row,text=p.get("icon","⛏"),font=(FF,14),
                     bg=row.cget("bg"),fg=C["violet"] if active else C["muted"],
                     padx=8,pady=6).pack(side="left")
            col2=tk.Frame(row,bg=row.cget("bg")); col2.pack(side="left",fill="x",expand=True,pady=6)
            tk.Label(col2,text=p["name"],font=(FFB if active else FF,10),
                     bg=row.cget("bg"),fg=C["text"] if active else C["sub"],
                     anchor="w").pack(anchor="w")
            tk.Label(col2,text=f"{p['version']} · {p['loader']}",font=(MONO,8),
                     bg=row.cget("bg"),fg=C["muted"],anchor="w").pack(anchor="w")
            for w2 in [row]+list(row.winfo_children()):
                w2.bind("<Button-1>",lambda e,idx=i: self._sel_profile(idx))
            if not active:
                row.bind("<Enter>",lambda e,r=row: r.config(bg=C["lift"]))
                row.bind("<Leave>",lambda e,r=row: r.config(bg=C["panel"]))

    def _sel_profile(self,idx):
        self.cur=idx; self._reload_profiles(); self._draw_home_card(); self._build_ped()

    def _build_ped(self):
        for w2 in self._ped.winfo_children(): w2.destroy()
        if not self.profiles: return
        p=self.profiles[self.cur]
        sc=tk.Canvas(self._ped,bg=C["bg"],highlightthickness=0)
        vsb=ttk.Scrollbar(self._ped,orient="vertical",command=sc.yview)
        sc.configure(yscrollcommand=vsb.set)
        sc.bind("<MouseWheel>",lambda e: sc.yview_scroll(-1*(e.delta//120),"units"))
        vsb.pack(side="right",fill="y"); sc.pack(fill="both",expand=True)
        inner=tk.Frame(sc,bg=C["bg"]); cw=sc.create_window((0,0),window=inner,anchor="nw")
        inner.bind("<Configure>",lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>",lambda e: sc.itemconfig(cw,width=e.width))
        self._pf={}
        pad=tk.Frame(inner,bg=C["bg"]); pad.pack(fill="both",expand=True,padx=8,pady=8)

        # Name + icon
        nc,ni=_card(pad,height=110,fill=C["card"],border=C["violet"],pack_kw={"fill":"x","pady":(0,12)})
        iff=tk.Frame(ni,bg=C["card"]); iff.pack(side="left",padx=12,pady=10)
        self._pf_icon=tk.StringVar(value=p.get("icon","⛏"))
        ICONS=["⛏","🗡","🛡","🏹","🪄","⚗","🌍","🔥","❄","⭐","🧱","🌲","💎","🐉","🦊"]
        for idx2,ico in enumerate(ICONS):
            r2,c2=divmod(idx2,5)
            selbg=C["card3"] if ico==p.get("icon","⛏") else C["card2"]
            b2=tk.Label(iff,text=ico,font=(FF,12),bg=selbg,cursor="hand2",padx=2,pady=2)
            b2.grid(row=r2,column=c2,padx=1,pady=1)
            def pick(e=None,i=ico,b=b2):
                self._pf_icon.set(i)
                for ch in iff.winfo_children(): ch.config(bg=C["card2"])
                b.config(bg=C["card3"])
            b2.bind("<Button-1>",pick)

        nf=tk.Frame(ni,bg=C["card"]); nf.pack(side="left",fill="both",expand=True,padx=(0,12))
        tk.Label(nf,text="PROFILE NAME",font=(MONO,7,"bold"),bg=C["card"],fg=C["muted"]).pack(anchor="w",pady=(14,2))
        self._pf["name"]=tk.StringVar(value=p["name"])
        tk.Entry(nf,textvariable=self._pf["name"],bg=C["card2"],fg=C["white"],
                 insertbackground=C["solar"],relief="flat",bd=0,font=(FFB,14)).pack(fill="x",ipady=7,ipadx=8)

        inst_vers=[v["id"] for v in self.mc_versions if is_installed(v["id"])]
        all_rel  =[v["id"] for v in self.mc_versions if v.get("type")=="release"]
        ver_opts=inst_vers or all_rel or [p["version"]]

        def ef(lbl_t,key,opts=None,browse=False):
            row=tk.Frame(pad,bg=C["bg"]); row.pack(fill="x",pady=2)
            tk.Label(row,text=lbl_t,font=(FF,9),bg=C["bg"],fg=C["sub"],width=30,anchor="w").pack(side="left")
            self._pf[key]=tk.StringVar(value=p.get(key,""))
            if opts is not None:
                w2=ttk.Combobox(row,textvariable=self._pf[key],values=opts,state="readonly",width=28,style="S.TCombobox"); w2.pack(side="left")
            else:
                e2=tk.Entry(row,textvariable=self._pf[key],bg=C["card2"],fg=C["text"],
                            insertbackground=C["solar"],relief="flat",bd=0,font=(MONO,9),width=32)
                e2.pack(side="left",ipady=5,ipadx=8)
                if browse:
                    tk.Button(row,text="📁",bg=C["card3"],fg=C["sub"],relief="flat",cursor="hand2",padx=4,pady=2,
                              command=lambda k=key: self._pf[k].set(filedialog.askdirectory() or self._pf[k].get())).pack(side="left",padx=4)

        _shdr(pad,"VERSION & LOADER",color=C["solar"],bg=C["bg"])
        ef("Minecraft Version","version",opts=ver_opts)
        ef("Mod Loader","loader",opts=LOADERS)
        ef("Loader Version","loader_version")
        _shdr(pad,"JAVA",color=C["amber"],bg=C["bg"])
        ef("Java Path (auto=detect)","java_path")
        ef("JVM Arguments","jvm_args")
        _shdr(pad,"GAME",color=C["violet"],bg=C["bg"])
        ef("Game Directory","game_dir",browse=True)
        ef("Width","resolution_width"); ef("Height","resolution_height")

        br=tk.Frame(pad,bg=C["bg"]); br.pack(fill="x",pady=14)
        def save():
            for k,v in self._pf.items(): self.profiles[self.cur][k]=v.get()
            self.profiles[self.cur]["icon"]=self._pf_icon.get()
            save_json(PROFILES_FILE,self.profiles); self._reload_profiles(); self._draw_home_card()
            messagebox.showinfo("Saved","Profile saved!")
        for txt,cmd,bg2,fg2 in [("💾  Save Profile",save,C["green"],C["white"]),
                                  ("⎘  Duplicate",self._dup_profile,C["card2"],C["text"]),
                                  ("🗑  Delete",self._del_profile,C["card2"],C["rose"])]:
            tk.Button(br,text=txt,command=cmd,bg=bg2,fg=fg2,
                      font=(FFB,10),relief="flat",bd=0,cursor="hand2",
                      padx=18,pady=10,activebackground=_blend(bg2,"#ffffff",0.15),
                      activeforeground=fg2).pack(side="left",padx=(0,8))

    # ═══════════════════════════════════════════════════════════════════════════
    #  MODS PAGE
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_mods(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Mod Manager","Install and manage mods per profile  ·  Double-click to toggle",accent=C["amber"])
        body=tk.Frame(f,bg=C["bg"]); body.pack(fill="both",expand=True,padx=28,pady=10)

        # Action buttons
        ab=tk.Frame(body,bg=C["bg"]); ab.pack(fill="x",pady=(0,8))
        for txt,bg2,fg2,cmd in [
            ("📁  Install .jar",  C["amber"],  "#000000", self._install_mods),
            ("📂  Library",       C["card2"],  C["text"], self._open_mods_lib),
            ("✅  Enable All",    C["card2"],  C["green"], self._mods_en_all),
            ("❌  Disable All",   C["card2"],  C["muted"], self._mods_dis_all),
        ]:
            tk.Button(ab,text=txt,command=cmd,bg=bg2,fg=fg2,
                      font=(FFB,9),relief="flat",bd=0,cursor="hand2",
                      padx=14,pady=8,
                      activebackground=C["lift"],activeforeground=fg2
                      ).pack(side="left",padx=(0,8))

        # Profile info row
        pr2=tk.Frame(body,bg=C["bg"]); pr2.pack(fill="x",pady=(0,6))
        tk.Label(pr2,text="PROFILE:",font=(MONO,8,"bold"),bg=C["bg"],fg=C["muted"]).pack(side="left")
        self._mods_plbl=tk.Label(pr2,text="",font=(MONO,9,"bold"),bg=C["bg"],fg=C["amber"]); self._mods_plbl.pack(side="left",padx=6)
        self._mods_clbl=tk.Label(pr2,text="",font=(MONO,9),bg=C["bg"],fg=C["sub"]); self._mods_clbl.pack(side="left")
        tk.Label(pr2,text="  •  Double-click to enable/disable",font=(MONO,8),bg=C["bg"],fg=C["dim"]).pack(side="left",padx=(8,0))

        # Treeview
        tf2=tk.Frame(body,bg=C["bg"]); tf2.pack(fill="both",expand=True)
        cols=("st","Name","Compat","Size","Status")
        self._mtree=ttk.Treeview(tf2,columns=cols,show="headings",style="TV.Treeview",selectmode="browse")
        self._mtree.heading("st",text=""); self._mtree.column("st",width=36,stretch=False,anchor="center")
        for c2,w in [("Name",340),("Compat",130),("Size",80),("Status",100)]:
            self._mtree.heading(c2,text=c2); self._mtree.column(c2,width=w,anchor="w")
        self._mtree.tag_configure("on", foreground=C["text"])
        self._mtree.tag_configure("off",foreground=C["muted"])
        vsb=ttk.Scrollbar(tf2,orient="vertical",command=self._mtree.yview)
        self._mtree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); self._mtree.pack(fill="both",expand=True)
        self._mtree.bind("<Double-1>",self._toggle_mod)

        # Bottom remove button
        br2=tk.Frame(body,bg=C["bg"]); br2.pack(fill="x",pady=8)
        tk.Button(br2,text="🗑  Remove Selected",command=self._remove_mod,
                  bg=C["card2"],fg=C["rose"],font=(FFB,9),relief="flat",bd=0,
                  cursor="hand2",padx=14,pady=8,
                  activebackground=C["card3"],activeforeground=C["rose"]
                  ).pack(side="left")
        self._refresh_mods()
        return f

    def _refresh_mods(self):
        if not hasattr(self,"_mtree"): return
        for r in self._mtree.get_children(): self._mtree.delete(r)
        if not self.profiles: return
        p=self.profiles[self.cur]; mods=p.get("mods",[])
        if hasattr(self,"_mods_plbl"): self._mods_plbl.config(text=p["name"])
        en=sum(1 for m in mods if m.get("enabled",True))
        if hasattr(self,"_mods_clbl"): self._mods_clbl.config(text=f"{len(mods)} total · {en} enabled")
        for mod in mods:
            path=Path(mod.get("path",""))
            size=fmt_bytes(path.stat().st_size) if path.exists() else "Missing"
            enabled=mod.get("enabled",True)
            self._mtree.insert("","end",values=("🟢" if enabled else "🔴",mod.get("name",""),mod.get("loader_compat","?"),size,"Enabled" if enabled else "Disabled"),tags=("on" if enabled else "off",))

    def _toggle_mod(self,e=None):
        sel=self._mtree.selection()
        if not sel: return
        idx=self._mtree.index(sel[0]); mods=self.profiles[self.cur].get("mods",[])
        if 0<=idx<len(mods): mods[idx]["enabled"]=not mods[idx].get("enabled",True); save_json(PROFILES_FILE,self.profiles); self._refresh_mods()

    def _detect_loader(self,p):
        try:
            with zipfile.ZipFile(p,"r") as z:
                names=z.namelist()
                if "fabric.mod.json" in names: return "Fabric"
                if any("mods.toml" in n for n in names): return "Forge"
                if "quilt.mod.json" in names: return "Quilt"
        except: pass
        return "Universal"

    def _install_mods(self):
        files=filedialog.askopenfilenames(title="Select Mod Files",filetypes=[("Mods","*.jar *.zip"),("All","*.*")])
        if not files: return
        if not self.profiles: messagebox.showwarning("No Profile","Create a profile first."); return
        p=self.profiles[self.cur]
        if "mods" not in p: p["mods"]=[]
        n=0
        for ff in files:
            src=Path(ff); dest=MODS_LIB/src.name
            try:
                shutil.copy2(src,dest)
                if not any(m.get("name")==src.stem for m in p["mods"]):
                    p["mods"].append({"name":src.stem,"path":str(dest),"enabled":True,"loader_compat":self._detect_loader(dest),"installed":datetime.now().isoformat()}); n+=1
            except Exception as ex: self._log(f"Failed {src.name}: {ex}","error")
        save_json(PROFILES_FILE,self.profiles); self._refresh_mods(); self._update_stats()
        if n: messagebox.showinfo("Done",f"{n} mod(s) installed!")

    def _remove_mod(self):
        sel=self._mtree.selection()
        if not sel: return
        idx=self._mtree.index(sel[0]); p=self.profiles[self.cur]
        if 0<=idx<len(p.get("mods",[])): p["mods"].pop(idx); save_json(PROFILES_FILE,self.profiles); self._refresh_mods()

    def _mods_en_all(self):
        for m in self.profiles[self.cur].get("mods",[]): m["enabled"]=True
        save_json(PROFILES_FILE,self.profiles); self._refresh_mods()

    def _mods_dis_all(self):
        for m in self.profiles[self.cur].get("mods",[]): m["enabled"]=False
        save_json(PROFILES_FILE,self.profiles); self._refresh_mods()

    def _open_mods_lib(self): self._open_dir(MODS_LIB)

    # ═══════════════════════════════════════════════════════════════════════════
    #  SKIN CHANGER
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_skin(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Skin Changer","Preview and apply custom skins",accent=C["rose"])
        self._skin_img=None; self._skin_tk={}; self._skin_path=None
        self._skin_name=tk.StringVar(value=""); self._skin_model=tk.StringVar(value="classic")
        self._preview_anim=0; self._preview_job=None

        body=tk.Frame(f,bg=C["bg"]); body.pack(fill="both",expand=True,padx=28,pady=12)
        left=tk.Frame(body,bg=C["bg"]); left.pack(side="left",fill="y",padx=(0,16))
        right=tk.Frame(body,bg=C["bg"]); right.pack(side="left",fill="both",expand=True)

        # Preview card — taller for full-body view
        pc,pi=_card(left,height=380,fill=C["card"],border=C["rose"],glow=C["rose"],pack_kw={"pady":(0,8)})
        pc.configure(width=240)
        self._skin_canvas=tk.Canvas(pi,bg=C["card"],highlightthickness=0)
        self._skin_canvas.configure(width=220,height=360)
        self._skin_canvas.pack(padx=10,pady=10)
        self._draw_skin_preview()

        rot_r=tk.Frame(left,bg=C["bg"]); rot_r.pack(fill="x",pady=(0,4))
        for sym,cmd in [("◀",self._rotate_left),("▶",self._rotate_right)]:
            b=_btn(rot_r,sym,cmd,bg=C["card2"],fg=C["rose"],font=(FFB,11),padx=14,pady=6)
            b.pack(side="left",padx=(0,4)); b.configure(width=50,height=32)
        self._spin_btn=_btn(rot_r,"⟳ Spin",self._toggle_spin,bg=C["card2"],fg=C["sub"],font=(FF,9),padx=10,pady=6)
        self._spin_btn.pack(side="left"); self._spin_btn.configure(height=32)

        mr=tk.Frame(left,bg=C["bg"]); mr.pack(fill="x",pady=(4,8))
        tk.Label(mr,text="Model:",font=(FF,9),bg=C["bg"],fg=C["sub"]).pack(side="left",padx=(0,8))
        for val,lbl_t in [("classic","Classic"),("slim","Slim")]:
            tk.Radiobutton(mr,text=lbl_t,variable=self._skin_model,value=val,
                           bg=C["bg"],fg=C["sub"],selectcolor=C["card2"],activebackground=C["bg"],
                           font=(FF,9),cursor="hand2",command=self._draw_skin_preview).pack(side="left",padx=3)

        nr2=tk.Frame(left,bg=C["bg"]); nr2.pack(fill="x",pady=(0,8))
        tk.Label(nr2,text="Name:",font=(FF,9),bg=C["bg"],fg=C["sub"]).pack(side="left",padx=(0,6))
        tk.Entry(nr2,textvariable=self._skin_name,bg=C["card2"],fg=C["text"],
                 insertbackground=C["rose"],relief="flat",bd=0,font=(FF,10),width=14).pack(side="left",ipady=5,ipadx=6)

        # Profile checker
        _shdr(left,"TARGET PROFILE",color=C["rose"],bg=C["bg"])
        ac,ai=_card(left,height=130,fill=C["card"],border=C["rose"],pack_kw={"fill":"x","pady":(0,6)})
        ac.configure(width=230)

        r1=tk.Frame(ai,bg=C["card"]); r1.pack(fill="x",padx=10,pady=(8,2))
        tk.Label(r1,text="Applied to:",font=(MONO,8),bg=C["card"],fg=C["muted"],width=11,anchor="w").pack(side="left")
        self._skin_applied_lbl=tk.Label(r1,text="None",font=(FFB,9),bg=C["card"],fg=C["sub"]); self._skin_applied_lbl.pack(side="left")
        tk.Frame(ai,bg=C["sep"],height=1).pack(fill="x",padx=10,pady=4)

        r2_=tk.Frame(ai,bg=C["card"]); r2_.pack(fill="x",padx=10,pady=(0,4))
        tk.Label(r2_,text="Apply to:",font=(MONO,8),bg=C["card"],fg=C["muted"],width=11,anchor="w").pack(side="left")
        self._skin_target_var=tk.StringVar()
        self._skin_target_cb=ttk.Combobox(r2_,textvariable=self._skin_target_var,
                                           values=[p["name"] for p in self.profiles] if self.profiles else ["—"],
                                           state="readonly",width=11,style="S.TCombobox")
        if self.profiles: self._skin_target_var.set(self.profiles[self.cur]["name"])
        self._skin_target_cb.pack(side="left",padx=(0,4))
        tk.Button(r2_,text="⟳",font=(FF,8),bg=C["card2"],fg=C["sub"],relief="flat",cursor="hand2",padx=4,pady=2,
                  command=self._refresh_skin_target_list).pack(side="left")

        self._skin_target_hint=tk.Label(ai,text="",font=(MONO,7),bg=C["card"],fg=C["sub"],padx=10,pady=2,wraplength=200,justify="left")
        self._skin_target_hint.pack(anchor="w")
        self._skin_target_var.trace_add("write",lambda *_: self._update_skin_target_hint())
        self._update_skin_target_hint()

        tk.Button(ai,text="✓   Apply Skin",command=self._apply_skin,
                  bg=C["rose"],fg=C["white"],font=(FFB,11),relief="flat",bd=0,
                  cursor="hand2",padx=18,pady=10,
                  activebackground=_blend(C["rose"],"#ffffff",0.15),
                  activeforeground=C["white"]).pack(fill="x",padx=10,pady=(4,4))
        self._skin_status=tk.Label(ai,text="",font=(MONO,8),bg=C["card"],fg=C["sub"],wraplength=200,justify="center")
        self._skin_status.pack(pady=(0,6))
        self._refresh_skin_applied_label()

        # Right: import + library
        _shdr(right,"IMPORT",color=C["solar"],bg=C["bg"])
        ic2,ii=_card(right,height=130,fill=C["card"],pack_kw={"fill":"x","pady":(0,10)})
        btn_r=tk.Frame(ii,bg=C["card"]); btn_r.pack(fill="x",padx=14,pady=(10,6))
        for lbl_t,cmd,bg2,fg2 in [("📁 Load PNG",self._load_skin_file,C["solar"],C["white"]),
                                    ("🌐 Username",self._load_skin_username,C["card2"],C["solar"]),
                                    ("💾 Export",self._export_skin,C["card2"],C["text"])]:
            tk.Button(btn_r,text=lbl_t,command=cmd,bg=bg2,fg=fg2,
                      font=(FFB,9),relief="flat",bd=0,cursor="hand2",
                      padx=12,pady=8,
                      activebackground=_blend(bg2,"#ffffff",0.18),
                      activeforeground=fg2).pack(side="left",padx=(0,8))
        ur=tk.Frame(ii,bg=C["card"]); ur.pack(fill="x",padx=14,pady=(2,4))
        tk.Label(ur,text="Username:",font=(FF,9),bg=C["card"],fg=C["sub"]).pack(side="left",padx=(0,6))
        self._skin_username=tk.StringVar(value="")
        tk.Entry(ur,textvariable=self._skin_username,bg=C["card2"],fg=C["text"],insertbackground=C["solar"],relief="flat",bd=0,font=(MONO,9),width=22).pack(side="left",ipady=5,ipadx=8)
        tk.Button(ur,text="↵",command=self._load_skin_username,bg=C["card3"],fg=C["solar"],
                  font=(FFB,11),relief="flat",bd=0,cursor="hand2",padx=10,pady=6,
                  activebackground=C["card2"],activeforeground=C["solar3"]
                  ).pack(side="left",padx=(4,0))
        self._skin_info_lbl=tk.Label(ii,text="No skin loaded",font=(MONO,8),bg=C["card"],fg=C["muted"],padx=14,pady=4); self._skin_info_lbl.pack(anchor="w",pady=(2,6))

        _shdr(right,"SKIN LIBRARY",color=C["sub"],bg=C["bg"])
        lc2,li2=_card(right,fill=C["card"],pack_kw={"fill":"both","expand":True,"pady":(0,6)})
        lt2=tk.Frame(li2,bg=C["card"]); lt2.pack(fill="x",padx=10,pady=(8,4))
        tk.Label(lt2,text="Saved skins",font=(MONO,8),bg=C["card"],fg=C["muted"]).pack(side="left")
        tk.Button(lt2,text="+ Save",command=self._save_skin_to_lib,bg=C["card3"],fg=C["solar"],
                  font=(FF,9),relief="flat",bd=0,cursor="hand2",padx=10,pady=5,
                  activebackground=C["card2"],activeforeground=C["solar3"]
                  ).pack(side="right")
        lsc=tk.Canvas(li2,bg=C["card"],highlightthickness=0)
        lvsb=ttk.Scrollbar(li2,orient="vertical",command=lsc.yview)
        lsc.configure(yscrollcommand=lvsb.set)
        lsc.bind("<MouseWheel>",lambda e: lsc.yview_scroll(-1*(e.delta//120),"units"))
        lvsb.pack(side="right",fill="y"); lsc.pack(fill="both",expand=True)
        self._lib_frame=tk.Frame(lsc,bg=C["card"])
        lcw=lsc.create_window((0,0),window=self._lib_frame,anchor="nw")
        self._lib_frame.bind("<Configure>",lambda e: lsc.configure(scrollregion=lsc.bbox("all")))
        lsc.bind("<Configure>",lambda e: lsc.itemconfig(lcw,width=e.width))

        _shdr(right,"DEFAULTS",color=C["muted"],bg=C["bg"])
        dc2,di2=_card(right,height=96,fill=C["card"],pack_kw={"fill":"x","pady":(0,6)})
        self._build_preset_skins(di2)
        self._refresh_skin_lib(); return f

    # Skin preview & logic (unchanged from v5, just re-included)
    def _draw_skin_preview(self,angle=None):
        if angle is None: angle=getattr(self,"_preview_anim",0)
        c=self._skin_canvas; c.delete("all")
        cw = c.winfo_width()  or 220
        ch = c.winfo_height() or 360
        c.create_rectangle(0,0,cw,ch,fill=C["card"],outline="")
        for x in range(0,cw,20): c.create_line(x,0,x,ch,fill=C["border"])
        for y2 in range(0,ch,20): c.create_line(0,y2,cw,y2,fill=C["border"])
        if self._skin_img is None: self._draw_placeholder(c,cw,ch); return
        try: self._draw_skin_figure(c,self._skin_img,cw,ch,angle%360)
        except: self._draw_placeholder(c,cw,ch)

    def _draw_placeholder(self,c,w,h):
        sc2=h//9;cx=w//2;hy=int(h*0.05);by_=hy+sc2*8+2;ly_=by_+sc2*12+2;col,bdr=C["card3"],C["border2"]
        for p in [(cx-sc2*4,hy,cx+sc2*4,hy+sc2*8),(cx-sc2*4,by_,cx+sc2*4,by_+sc2*12),(cx-sc2*6-2,by_,cx-sc2*4-2,by_+sc2*12),(cx+sc2*4+2,by_,cx+sc2*6+2,by_+sc2*12),(cx-sc2*4,ly_,cx,ly_+sc2*12),(cx,ly_,cx+sc2*4,ly_+sc2*12)]:
            c.create_rectangle(*p,fill=col,outline=bdr)
        c.create_text(cx,h//2+24,text="Load a skin to preview",font=(FF,9),fill=C["sub"])

    def _draw_skin_figure(self,c,skin,cw,ch,angle):
        """
        Render a Minecraft skin onto canvas c at size cw×ch.
        Draws front/back/left/right based on angle.
        Skin texture layout (64×64):
          Head front : 8,8 – 16,16
          Body front : 20,20 – 28,32
          R.Arm front: 44,20 – 48,32  (slim: 45,20-48,32)
          L.Arm front: 36,52 – 40,64  (slim: 37,52-40,64)
          R.Leg front: 4,20  – 8,32
          L.Leg front: 20,52 – 24,64
        """
        from PIL import Image as _I

        slim = self._skin_model.get() == "slim"
        skin64 = skin.resize((64,64), _I.NEAREST) if skin.size != (64,64) else skin.copy()

        # Scale factor — fit the figure into the canvas leaving 12px margin
        margin   = 12
        avail_h  = ch - margin*2
        # figure is: head(8) + body(12) + leg(12) = 32 units → scale to avail_h
        scale    = avail_h // 32
        scale    = max(4, min(scale, 10))

        hw = scale*8   # head width/height
        bw = scale*8   # body width
        bh = scale*12  # body height
        lh = scale*12  # leg height
        lw = scale*4   # leg width
        aw = scale*3 if slim else scale*4  # arm width
        ah = scale*12  # arm height

        # Figure total height = hw + bh + lh
        fig_h = hw + bh + lh
        # Centre horizontally, top vertically
        cx = cw // 2
        top = (ch - fig_h) // 2

        # Y positions
        head_y = top
        body_y = head_y + hw
        leg_y  = body_y + bh
        arm_y  = body_y

        # X positions
        head_x = cx - hw//2
        body_x = cx - bw//2
        r_arm_x = body_x - aw - 2
        l_arm_x = body_x + bw + 2
        r_leg_x = cx - lw - 1
        l_leg_x = cx + 1

        self._skin_tk = {}

        def crop_region(ox, oy, fw, fh, flip=False):
            img = skin64.crop((ox, oy, ox+fw, oy+fh))
            if flip: img = img.transpose(_I.FLIP_LEFT_RIGHT)
            return img

        def put(img, x, y, w2, h2):
            if w2 < 1 or h2 < 1: return
            ph = ImageTk.PhotoImage(img.resize((w2,h2),_I.NEAREST))
            key = f"{x}_{y}_{w2}_{h2}"
            self._skin_tk[key] = ph
            c.create_image(x, y, image=ph, anchor="nw")

        a = angle % 360
        front = a < 45 or a >= 315
        back  = 135 <= a < 225
        r_side = 45 <= a < 135
        # l_side = 225 <= a < 315  (else)

        if front:
            # Head
            put(crop_region(8,8,8,8),   head_x, head_y, hw, hw)
            # Outer head layer (overlay) — 32,8 region
            try:
                overlay = skin64.crop((40,8,48,16))
                ph2 = ImageTk.PhotoImage(overlay.resize((hw,hw),_I.NEAREST))
                self._skin_tk["ho"] = ph2
                c.create_image(head_x, head_y, image=ph2, anchor="nw")
            except Exception: pass
            # Body
            put(crop_region(20,20,8,12), body_x, body_y, bw, bh)
            # Right arm
            put(crop_region(44,20,aw,12) if not slim else crop_region(45,20,3,12),
                r_arm_x, arm_y, aw, ah)
            # Left arm (64×64 skin has second layer at 32,52; fallback: mirror right)
            try:
                la = skin64.crop((36,52,36+aw,64))
                if la.getbbox() is None: raise ValueError
                put(la, l_arm_x, arm_y, aw, ah)
            except Exception:
                put(crop_region(44,20,aw,12,flip=True), l_arm_x, arm_y, aw, ah)
            # Right leg
            put(crop_region(4,20,4,12),  r_leg_x, leg_y, lw, lh)
            # Left leg
            try:
                ll = skin64.crop((20,52,24,64))
                if ll.getbbox() is None: raise ValueError
                put(ll, l_leg_x, leg_y, lw, lh)
            except Exception:
                put(crop_region(4,20,4,12,flip=True), l_leg_x, leg_y, lw, lh)

        elif back:
            put(crop_region(24,8,8,8),   head_x, head_y, hw, hw)
            put(crop_region(32,20,8,12), body_x, body_y, bw, bh)
            put(crop_region(52,20,aw,12),r_arm_x, arm_y, aw, ah)
            try:
                la = skin64.crop((44,52,44+aw,64))
                put(la, l_arm_x, arm_y, aw, ah)
            except Exception:
                put(crop_region(52,20,aw,12,flip=True), l_arm_x, arm_y, aw, ah)
            put(crop_region(12,20,4,12), r_leg_x, leg_y, lw, lh)
            try:
                ll = skin64.crop((28,52,32,64))
                put(ll, l_leg_x, leg_y, lw, lh)
            except Exception:
                put(crop_region(12,20,4,12,flip=True), l_leg_x, leg_y, lw, lh)

        elif r_side:
            put(crop_region(0,8,8,8),    head_x, head_y, hw, hw)
            put(crop_region(16,20,4,12), body_x+bw//4, body_y, bw//2, bh)
            put(crop_region(40,20,4,12), r_arm_x, arm_y, aw, ah)
            put(crop_region(0,20,4,12),  r_leg_x, leg_y, lw, lh)

        else:  # left side
            put(crop_region(16,8,8,8),   head_x, head_y, hw, hw)
            put(crop_region(28,20,4,12), body_x+bw//4, body_y, bw//2, bh)
            put(crop_region(48,20,4,12), l_arm_x, arm_y, aw, ah)
            put(crop_region(8,20,4,12),  l_leg_x, leg_y, lw, lh)

        # Shadow under feet
        sx = cx - (bw//2 + aw + 4)
        sy = leg_y + lh + 4
        sw = bw + aw*2 + 8
        c.create_oval(sx, sy, sx+sw, sy+8, fill="#000", outline="", stipple="gray25")

        # Outline glow around figure
        c.create_rectangle(r_arm_x-2, head_y-2,
                           l_arm_x+aw+2, leg_y+lh+2,
                           outline=C["rose"], width=1)


    def _rotate_left(self):  self._preview_anim=(self._preview_anim-45)%360;self._draw_skin_preview()
    def _rotate_right(self): self._preview_anim=(self._preview_anim+45)%360;self._draw_skin_preview()

    def _toggle_spin(self):
        if self._preview_job:
            self.root.after_cancel(self._preview_job);self._preview_job=None
            self._spin_btn.configure(bg=C["card2"])
        else:
            self._spin_btn.configure(bg=C["rose"])
            self._spin_step()

    def _spin_step(self):
        self._preview_anim=(self._preview_anim+10)%360;self._draw_skin_preview()
        self._preview_job=self.root.after(80,self._spin_step)

    def _load_skin_file(self):
        path=filedialog.askopenfilename(title="Select Skin PNG",filetypes=[("PNG","*.png"),("All","*.*")])
        if not path: return
        try:
            img=Image.open(path).convert("RGBA")
            if img.size!=(64,64): img=img.resize((64,64),Image.NEAREST)
            self._skin_img=img;self._skin_path=path;self._skin_name.set(Path(path).stem)
            self._skin_tk.clear();self._skin_info_lbl.config(text=f"✓  {Path(path).name}",fg=C["green"])
            self._skin_status.config(text="");self._draw_skin_preview()
        except Exception as ex: messagebox.showerror("Load Error",str(ex))

    def _load_skin_username(self):
        username=self._skin_username.get().strip()
        if not username: messagebox.showinfo("Enter Username","Type a Minecraft username first."); return
        self._skin_info_lbl.config(text=f"⏳  Fetching '{username}'…",fg=C["amber"])
        threading.Thread(target=self._fetch_skin_thread,args=(username,),daemon=True).start()

    def _fetch_skin_thread(self,username):
        import urllib.request,base64,io as _io2
        try:
            with urllib.request.urlopen(f"https://api.mojang.com/users/profiles/minecraft/{username}",timeout=8) as r:
                data=json.loads(r.read())
            uid=data["id"]
            with urllib.request.urlopen(f"https://sessionserver.mojang.com/session/minecraft/profile/{uid}",timeout=8) as r:
                profile=json.loads(r.read())
            for prop in profile.get("properties",[]):
                if prop["name"]=="textures":
                    tex=json.loads(base64.b64decode(prop["value"]))
                    skin_url=tex["textures"]["SKIN"]["url"]
                    model=tex["textures"]["SKIN"].get("metadata",{}).get("model","classic")
                    with urllib.request.urlopen(skin_url,timeout=8) as r:
                        img_data=r.read()
                    img=Image.open(_io2.BytesIO(img_data)).convert("RGBA")
                    if img.size!=(64,64): img=img.resize((64,64),Image.NEAREST)
                    def upd(i=img,m=model,u=username):
                        self._skin_img=i;self._skin_path=None;self._skin_name.set(u)
                        self._skin_model.set(m);self._skin_tk.clear()
                        self._skin_info_lbl.config(text=f"✓  Loaded: {u}  ({m})",fg=C["green"])
                        self._draw_skin_preview()
                    self.root.after(0,upd); return
            self.root.after(0,lambda: self._skin_info_lbl.config(text=f"No skin for '{username}'",fg=C["red"]))
        except Exception as ex:
            err=str(ex); self.root.after(0,lambda e=err: self._skin_info_lbl.config(text=f"Error: {e}",fg=C["red"]))

    # Skin deployment
    def _offline_uuid(self,username):
        data=("OfflinePlayer:"+username).encode("utf-8")
        md5=_hashlib.md5(data).digest(); b=bytearray(md5)
        b[6]=(b[6]&0x0f)|0x30; b[8]=(b[8]&0x3f)|0x80
        import uuid as _u; return str(_u.UUID(bytes=bytes(b)))

    # ─────────────────────────────────────────────────────────────────────────
    #  SKIN  (resource-pack approach — zero mods, works on all versions)
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_skin_target_list(self):
        if not hasattr(self,"_skin_target_cb"): return
        names=[p["name"] for p in self.profiles] if self.profiles else ["—"]
        self._skin_target_cb.configure(values=names)
        if self._skin_target_var.get() not in names:
            self._skin_target_var.set(names[0] if names else "—")
        self._update_skin_target_hint()
        self._refresh_skin_applied_label()

    def _refresh_skin_applied_label(self):
        if not hasattr(self,"_skin_applied_lbl"): return
        applied=[p["name"] for p in self.profiles if p.get("skin_path","")]
        if not applied:   self._skin_applied_lbl.config(text="None",fg=C["sub"])
        elif len(applied)==1: self._skin_applied_lbl.config(text=applied[0],fg=C["green"])
        else: self._skin_applied_lbl.config(text=", ".join(applied),fg=C["green"])

    def _refresh_skin_lib(self):
        if not hasattr(self,"_lib_frame"): return
        for w2 in self._lib_frame.winfo_children(): w2.destroy()
        skins=sorted(self._skin_save_dir().glob("*.png"))
        if not skins:
            tk.Label(self._lib_frame,text="No saved skins yet",
                     font=(MONO,8),bg=C["card"],fg=C["muted"],padx=14,pady=10).pack()
            return
        row=None
        for i,sk in enumerate(skins):
            if i%5==0:
                row=tk.Frame(self._lib_frame,bg=C["card"]); row.pack(fill="x",padx=4,pady=2)
            self._make_lib_thumb(row,sk)

    def _skin_save_dir(self):
        d=BASE/"skins"; d.mkdir(parents=True,exist_ok=True); return d

    def _save_skin_to_lib(self):
        if self._skin_img is None: messagebox.showinfo("No Skin","Load a skin first."); return
        name=(self._skin_name.get().strip() or "unnamed").replace(" ","_")
        dest=self._skin_save_dir()/f"{name}.png"
        if dest.exists():
            if not messagebox.askyesno("Overwrite",f"Overwrite '{name}'?"): return
        self._skin_img.save(str(dest)); self._refresh_skin_lib()
        self._skin_status.config(text=f"✓ Saved: {name}",fg=C["green"])

    def _export_skin(self):
        if self._skin_img is None: messagebox.showinfo("No Skin","Load a skin first."); return
        name=(self._skin_name.get().strip() or "skin").replace(" ","_")
        dest=filedialog.asksaveasfilename(defaultextension=".png",initialfile=f"{name}.png",filetypes=[("PNG","*.png")])
        if dest: self._skin_img.save(dest); messagebox.showinfo("Exported","Saved:\n"+dest)

    def _make_lib_thumb(self,parent,skin_path):
        frame=tk.Frame(parent,bg=C["card2"],cursor="hand2"); frame.pack(side="left",padx=3,pady=2)
        tlbl=tk.Label(frame,bg=C["card2"]); tlbl.pack(padx=6,pady=(6,2))
        try:
            img=Image.open(skin_path).convert("RGBA")
            if img.size!=(64,64): img=img.resize((64,64),Image.NEAREST)
            head=img.crop((8,8,16,16)).resize((32,32),Image.NEAREST)
            photo=ImageTk.PhotoImage(head); tlbl.config(image=photo); tlbl._photo=photo
        except: tlbl.config(text="?",font=(FF,14),fg=C["muted"],width=3,height=2)
        tk.Label(frame,text=skin_path.stem[:10],font=(MONO,7),bg=C["card2"],fg=C["sub"]).pack(pady=(0,2))
        def load_this(p=skin_path):
            try:
                img2=Image.open(p).convert("RGBA")
                if img2.size!=(64,64): img2=img2.resize((64,64),Image.NEAREST)
                self._skin_img=img2; self._skin_path=str(p); self._skin_name.set(p.stem)
                self._skin_tk.clear(); self._skin_info_lbl.config(text=f"✓  {p.name}",fg=C["green"])
                self._draw_skin_preview()
            except Exception as ex: messagebox.showerror("Error",str(ex))
        def del_this(p=skin_path):
            if messagebox.askyesno("Delete",f"Delete '{p.stem}'?"): p.unlink(); self._refresh_skin_lib()
        frame.bind("<Button-1>",lambda e: load_this()); tlbl.bind("<Button-1>",lambda e: load_this())
        br3=tk.Frame(frame,bg=C["card2"]); br3.pack(fill="x",padx=2,pady=(0,4))
        for txt,fg2,cmd in [("Load",C["solar"],load_this),("Del",C["rose"],del_this)]:
            tk.Button(br3,text=txt,font=(MONO,7),bg=C["card3"],fg=fg2,relief="flat",
                      cursor="hand2",padx=4,pady=2,command=cmd).pack(side="left",padx=1)

    def _build_preset_skins(self,parent):
        row=tk.Frame(parent,bg=C["card"]); row.pack(fill="x",padx=10,pady=8)
        for name,model in [("Steve","classic"),("Alex","slim")]:
            frame=tk.Frame(row,bg=C["card2"],cursor="hand2"); frame.pack(side="left",padx=(0,10))
            skin_img=self._make_default_skin(name)
            head=skin_img.crop((8,8,16,16)).resize((32,32),Image.NEAREST); photo=ImageTk.PhotoImage(head)
            lbl2=tk.Label(frame,image=photo,bg=C["card2"]); lbl2._photo=photo; lbl2.pack(padx=8,pady=(6,2))
            tk.Label(frame,text=name,font=(FFB,9),bg=C["card2"],fg=C["text"]).pack()
            tk.Label(frame,text=model,font=(MONO,7),bg=C["card2"],fg=C["sub"]).pack(pady=(0,2))
            def use(img=skin_img,n=name,m=model):
                self._skin_img=img; self._skin_path=None; self._skin_name.set(n); self._skin_model.set(m)
                self._skin_tk.clear(); self._skin_info_lbl.config(text=f"✓  {n}",fg=C["green"]); self._draw_skin_preview()
            tk.Button(frame,text="Use",command=use,bg=C["card3"],fg=C["rose"],
                      font=(MONO,8),relief="flat",bd=0,cursor="hand2",padx=8,pady=4,
                      activebackground=C["card2"],activeforeground=C["rose"]).pack(pady=(0,6))

    def _make_default_skin(self,name):
        img=Image.new("RGBA",(64,64),(0,0,0,0)); d=ImageDraw.Draw(img)
        if name=="Steve": sc2,lc,fc,hc=(70,110,165),(60,60,120),(198,155,109),(100,70,30)
        else:             sc2,lc,fc,hc=(100,170,100),(80,50,80),(226,188,142),(180,120,40)
        d.rectangle([8,8,15,15],fill=fc);d.rectangle([9,10,10,11],fill=(50,50,80));d.rectangle([13,10,14,11],fill=(50,50,80))
        for px in [10,11,12,13]: d.point((px,13),fill=(140,80,60))
        d.rectangle([8,8,15,9],fill=hc);d.rectangle([8,8,8,11],fill=hc);d.rectangle([15,8,15,11],fill=hc)
        d.rectangle([20,20,27,31],fill=sc2);d.rectangle([44,20,47,31],fill=sc2);d.rectangle([36,52,39,63],fill=sc2)
        d.rectangle([4,20,7,31],fill=lc);d.rectangle([20,52,23,63],fill=lc)
        d.rectangle([24,8,31,15],fill=fc);d.rectangle([24,8,31,9],fill=hc); return img


    def _apply_skin(self):
        """Save skin, build resource pack, enable it in options.txt."""
        if self._skin_img is None:
            messagebox.showinfo("No Skin","Load a skin first."); return
        if not self.profiles:
            messagebox.showinfo("No Profile","Create a profile first."); return

        tvar  = getattr(self,"_skin_target_var",None)
        tname = tvar.get() if tvar else None
        p     = next((x for x in self.profiles if x["name"]==tname), self.profiles[self.cur])
        model = self._skin_model.get()
        name  = (self._skin_name.get().strip() or "skin").replace(" ","_")

        game_dir = Path(p.get("game_dir") or INST_DIR/p["name"])
        game_dir.mkdir(parents=True, exist_ok=True)

        # Save skin PNG into profile skins folder
        skins_dir = game_dir/"skins"; skins_dir.mkdir(parents=True, exist_ok=True)
        skin_png  = skins_dir/f"{name}.png"
        self._skin_img.save(str(skin_png))
        p["skin_path"]  = str(skin_png)
        p["skin_model"] = model
        save_json(PROFILES_FILE, self.profiles)

        self._skin_status.config(text="⏳ Applying…", fg=C["amber"])
        self._log(f"═══ Applying skin '{name}' to '{p['name']}' (resource pack) ═══","success")

        def run():
            try:
                username = self.settings.get("username","Player")
                # Primary: inject into MC's offline skin cache (works without mods)
                uid = self._apply_skin_to_cache(skin_png, username)
                # Secondary: resource pack fallback
                self._build_skin_resourcepack(game_dir, skin_png, model)
                self.root.after(0, lambda: self._skin_status.config(text="✓ Applied!", fg=C["green"]))
                self.root.after(0, self._refresh_skin_applied_label)
                self.root.after(0, self._update_skin_target_hint)
                self.root.after(0, lambda u=uid[:8]: messagebox.showinfo("Skin Applied",
                    f"✓ Skin injected into texture cache!\n"
                    f"   UUID: {u}...\n\n"
                    "✓ Resource pack also installed as backup.\n\n"
                    "Launch Minecraft — your skin will show immediately.\n"
                    "No mods required."))
            except Exception as ex:
                msg = str(ex)
                self._log(f"Skin apply failed: {ex}","error")
                self.root.after(0, lambda m=msg: messagebox.showerror("Error", m))

        threading.Thread(target=run, daemon=True).start()


    # ─────────────────────────────────────────────────────────────────────────
    #  SKIN — offline texture cache injection + resource pack backup
    # ─────────────────────────────────────────────────────────────────────────

    def _pack_format_for(self, mc_ver):
        parts = [int(x) for x in (mc_ver + ".0.0").split(".")[:3]]
        minor, patch = parts[1], parts[2]
        if   minor >= 21:                 return 34
        elif minor == 20 and patch >= 5:  return 32
        elif minor == 20 and patch >= 3:  return 22
        elif minor == 20 and patch >= 2:  return 18
        elif minor == 20:                 return 15
        elif minor == 19 and patch >= 4:  return 13
        elif minor == 19 and patch >= 3:  return 12
        elif minor == 19:                 return 9
        elif minor == 18:                 return 8
        elif minor == 17:                 return 7
        else:                             return 6

    def _apply_skin_to_cache(self, skin_png, username):
        """
        Write the skin PNG to every location Minecraft looks for it offline.

        Minecraft's offline skin lookup order:
          1. <MC_DIR>/assets/skins/<offline-uuid-no-dashes>      (no extension)
          2. <MC_DIR>/assets/skins/<offline-uuid-no-dashes>.png
          3. Falls back to default Steve/Alex

        The UUID is the Java OfflinePlayer UUID derived from the username.
        Writing to these paths makes Minecraft show the skin immediately
        on next launch without any mods or internet connection.
        """
        uid       = self._offline_uuid(username)              # e.g. "12345678-..."
        uid_plain = uid.replace("-","")                        # "12345678..."

        skin_img = Image.open(str(skin_png)).convert("RGBA")
        if skin_img.size != (64,64):
            skin_img = skin_img.resize((64,64), Image.NEAREST)

        import io as _io
        buf = _io.BytesIO()
        skin_img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        # ── 1. Main skin cache (both with and without .png) ──────────────────
        cache_dir = MC_DIR / "assets" / "skins"
        cache_dir.mkdir(parents=True, exist_ok=True)
        for fname in [uid_plain, uid_plain + ".png", uid, uid + ".png"]:
            try:
                (cache_dir / fname).write_bytes(png_bytes)
            except Exception as e:
                self._log(f"Cache write failed ({fname}): {e}", "warn")
        self._log(f"✓ Skin cache: {cache_dir / uid_plain}", "success")

        # ── 2. Also write to <MC_DIR>/assets/log_configs just in case ──────
        # Some versions store skin differently; write to assets root too
        try:
            (MC_DIR / "assets" / (uid_plain + ".png")).write_bytes(png_bytes)
        except Exception:
            pass

        return uid

    def _build_skin_resourcepack(self, game_dir, skin_png, model="classic"):
        """
        Build CraftLaunchSkin.zip resource pack as a SECONDARY fallback.
        The primary method is cache injection (_apply_skin_to_cache).
        This catches any edge case where MC ignores the cache.
        """
        import zipfile as _zf, json as _js, io as _io

        skin_img = Image.open(str(skin_png)).convert("RGBA")
        if skin_img.size != (64,64):
            skin_img = skin_img.resize((64,64), Image.NEAREST)

        mc_ver = ""
        if self.profiles:
            mc_ver = self.profiles[self.cur].get("version","")
        pf = self._pack_format_for(mc_ver) if mc_ver else 15

        # Use supported_formats range so it never gets flagged as incompatible
        mcmeta = _js.dumps({
            "pack": {
                "pack_format": pf,
                "supported_formats": [6, 99],
                "description": "CraftLaunch custom skin"
            }
        }, indent=2)

        buf2 = _io.BytesIO()
        skin_img.save(buf2, format="PNG")
        png_bytes = buf2.getvalue()

        tex_paths = [
            "assets/minecraft/textures/entity/player/wide/steve.png",
            "assets/minecraft/textures/entity/player/slim/alex.png",
            "assets/minecraft/textures/entity/player/wide/alex.png",
            "assets/minecraft/textures/entity/player/slim/steve.png",
            "assets/minecraft/textures/entity/char.png",
        ]

        def write_zip(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            with _zf.ZipFile(dest, "w", _zf.ZIP_DEFLATED) as zf:
                zf.writestr("pack.mcmeta", mcmeta)
                for tp in tex_paths:
                    zf.writestr(tp, png_bytes)
            self._log(f"✓ Resource pack → {dest}", "success")

        write_zip(Path(game_dir) / "resourcepacks" / "CraftLaunchSkin.zip")
        write_zip(MC_DIR / "resourcepacks" / "CraftLaunchSkin.zip")

        for d in [Path(game_dir), MC_DIR]:
            self._enable_resourcepack(d, "CraftLaunchSkin.zip")

    def _enable_resourcepack(self, game_dir, pack_name):
        import json as _js
        game_dir  = Path(game_dir)
        game_dir.mkdir(parents=True, exist_ok=True)
        opts_file = game_dir / "options.txt"
        entry     = f"file/{pack_name}"

        lines_out = []
        found     = False
        incompat_found = False

        if opts_file.exists():
            for line in opts_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("resourcePacks:"):
                    found = True
                    raw = line[len("resourcePacks:"):].strip()
                    try:   packs = _js.loads(raw)
                    except Exception: packs = ["vanilla"]
                    # Keep vanilla, remove old entry, add ours at position 1
                    packs = [p2 for p2 in packs if pack_name not in p2]
                    vanilla = [p2 for p2 in packs if p2 == "vanilla"]
                    rest    = [p2 for p2 in packs if p2 != "vanilla"]
                    packs   = vanilla + [entry] + rest
                    lines_out.append(f"resourcePacks:{_js.dumps(packs)}")
                elif line.startswith("incompatibleResourcePacks:"):
                    incompat_found = True
                    # Remove our pack from incompatible list
                    raw = line[len("incompatibleResourcePacks:"):].strip()
                    try:   incompat = _js.loads(raw)
                    except Exception: incompat = []
                    incompat = [p2 for p2 in incompat if pack_name not in p2]
                    lines_out.append(f"incompatibleResourcePacks:{_js.dumps(incompat)}")
                else:
                    lines_out.append(line)

        if not found:
            lines_out.append(f"resourcePacks:{_js.dumps(['vanilla', entry])}")
        if not incompat_found:
            lines_out.append("incompatibleResourcePacks:[]")

        opts_file.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
        self._log(f"✓ options.txt → {opts_file}", "success")


    def _deploy_skin_on_launch(self, profile):
        """Re-inject skin into cache and refresh resource pack before each launch."""
        skin_path = profile.get("skin_path","")
        if not skin_path: return
        skin_png  = Path(skin_path)
        if not skin_png.exists(): return
        model    = profile.get("skin_model","classic")
        game_dir = Path(profile.get("game_dir") or INST_DIR/profile["name"])
        username = self.settings.get("username","Player")
        try:
            self._apply_skin_to_cache(skin_png, username)
            self._build_skin_resourcepack(game_dir, skin_png, model)
        except Exception as ex:
            self._log(f"Skin deploy on launch failed: {ex}","warn")

    def _update_skin_target_hint(self):
        if not hasattr(self,"_skin_target_hint"): return
        name = self._skin_target_var.get()
        p    = next((x for x in self.profiles if x["name"]==name), None)
        if not p:
            self._skin_target_hint.config(text="",fg=C["sub"]); return
        ver      = p.get("version","?")
        has_skin = bool(p.get("skin_path",""))
        txt = f"MC {ver}  ·  Resource pack method  ·  No mods needed ✓"
        clr = C["green"]
        if has_skin:
            txt += "\nSkin: " + Path(p["skin_path"]).name
        self._skin_target_hint.config(text=txt, fg=clr)



    # ═══════════════════════════════════════════════════════════════════════════
    #  JAVA AUTO-INSTALLER
    # ═══════════════════════════════════════════════════════════════════════════
    def _install_java_17(self, progress_cb=None, status_cb=None):
        """Download Adoptium JDK 17 into BASE/java17/ and return the java path."""
        import urllib.request, zipfile as _zf
        java_dir = BASE / "java17"
        patt = "java.exe" if platform.system()=="Windows" else "java"

        # Already installed?
        for p2 in sorted(java_dir.rglob(patt)):
            if p2.is_file() and "bin" in str(p2):
                self._log(f"Java 17 already at {p2}", "info")
                return str(p2)

        self._log("Downloading Java 17 from Adoptium...", "info")
        if status_cb: status_cb("Fetching Java 17...")

        os_map  = {"Windows":"windows","Linux":"linux","Darwin":"mac"}
        arc_map = {"AMD64":"x64","x86_64":"x64","aarch64":"aarch64","arm64":"aarch64"}
        os_name = os_map.get(platform.system(),"linux")
        arch    = arc_map.get(platform.machine(),"x64")
        api_url = (f"https://api.adoptium.net/v3/assets/latest/17/hotspot"
                   f"?os={os_name}&architecture={arch}&image_type=jdk&vendor=eclipse")

        import json as _j
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent":"CraftLaunch/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _j.loads(r.read())
        except Exception as ex:
            raise RuntimeError(f"Could not fetch Java 17 info: {ex}")

        if not data:
            raise RuntimeError("No Java 17 release found for this platform")

        pkg      = data[0]["binary"]["package"]
        dl_url   = pkg["link"]; fname = pkg["name"]
        total_mb = pkg.get("size",0)//1024//1024
        dest_file = BASE / fname
        java_dir.mkdir(parents=True, exist_ok=True)

        self._log(f"Downloading {fname} ({total_mb} MB)...", "info")
        if status_cb: status_cb(f"Downloading Java 17 ({total_mb} MB)...")

        def _prog(bn, bs, tot):
            if tot > 0 and progress_cb:
                progress_cb(int(min(bn*bs,tot)/tot*80))
        urllib.request.urlretrieve(dl_url, str(dest_file), _prog)
        if progress_cb: progress_cb(82)

        if status_cb: status_cb("Extracting Java 17...")
        self._log("Extracting...", "info")
        if fname.endswith(".zip"):
            with _zf.ZipFile(dest_file,"r") as z: z.extractall(java_dir)
        else:
            import tarfile as _tf
            with _tf.open(dest_file,"r:gz") as t: t.extractall(java_dir)
        try: dest_file.unlink()
        except Exception: pass
        if progress_cb: progress_cb(95)

        for p2 in sorted(java_dir.rglob(patt)):
            if p2.is_file() and "bin" in str(p2):
                if platform.system() != "Windows":
                    import stat as _st
                    p2.chmod(p2.stat().st_mode|_st.S_IEXEC|_st.S_IXGRP|_st.S_IXOTH)
                self.settings["java_path"] = str(p2)
                save_json(SETTINGS_FILE, self.settings)
                self._log(f"Java 17 installed at {p2}", "success")
                if progress_cb: progress_cb(100)
                return str(p2)
        raise RuntimeError("Java 17 extracted but executable not found")

    def _pg_settings(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Settings","Launcher preferences and configuration",accent=C["sub"])
        sc=tk.Canvas(f,bg=C["bg"],highlightthickness=0); vsb=ttk.Scrollbar(f,orient="vertical",command=sc.yview)
        sc.configure(yscrollcommand=vsb.set); sc.bind("<MouseWheel>",lambda e: sc.yview_scroll(-1*(e.delta//120),"units"))
        vsb.pack(side="right",fill="y"); sc.pack(fill="both",expand=True)
        inner=tk.Frame(sc,bg=C["bg"]); cw=sc.create_window((0,0),window=inner,anchor="nw")
        inner.bind("<Configure>",lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>",lambda e: sc.itemconfig(cw,width=e.width))
        self._sf={}; pad=tk.Frame(inner,bg=C["bg"]); pad.pack(fill="both",expand=True,padx=28,pady=16)

        def sf(lbl_t,key,default="",is_bool=False):
            row=tk.Frame(pad,bg=C["bg"]); row.pack(fill="x",pady=4)
            tk.Label(row,text=lbl_t,font=(FF,9),bg=C["bg"],fg=C["sub"],width=36,anchor="w").pack(side="left")
            val=self.settings.get(key,default)
            if is_bool:
                v=tk.BooleanVar(value=bool(val))
                tk.Checkbutton(row,variable=v,bg=C["bg"],activebackground=C["bg"],
                               selectcolor=C["card2"],cursor="hand2",
                               command=lambda k=key,vr=v: self.settings.update({k:vr.get()})
                               ).pack(side="left"); self._sf[key]=v
            else:
                v=tk.StringVar(value=str(val))
                tk.Entry(row,textvariable=v,bg=C["card2"],fg=C["text"],
                         insertbackground=C["solar"],relief="flat",bd=0,
                         font=(MONO,9),width=36).pack(side="left",ipady=6,ipadx=8)
                v.trace_add("write",lambda *a,k=key,vr=v: self.settings.update({k:vr.get()})); self._sf[key]=v

        _shdr(pad,"ACCOUNT",color=C["solar"],bg=C["bg"])
        sf("Username","username","Player")
        sf("UUID  (blank = auto-generate)","uuid","")
        _shdr(pad,"JAVA",color=C["amber"],bg=C["bg"])
        sf("Java Executable  (auto = detect)","java_path","auto")
        _shdr(pad,"LAUNCHER",color=C["violet"],bg=C["bg"])
        sf("Close launcher when game starts","close_on_launch",False,is_bool=True)
        _shdr(pad,"JAVA DETECTION",color=C["solar"],bg=C["bg"])
        jr=tk.Frame(pad,bg=C["bg"]); jr.pack(fill="x",pady=6)
        self._jdet_lbl=tk.Label(jr,text="Press to scan…",font=(MONO,9),bg=C["bg"],fg=C["sub"])
        self._jdet_lbl.pack(side="left")
        b=_btn(jr,"🔍 Auto-Detect",self._java_check,bg=C["card2"],fg=C["solar"],font=(FF,9),padx=12,pady=6)
        b.pack(side="left",padx=(10,6)); b.configure(height=34)
        b2=_btn(jr,"☕ Install Java 17",self._java_install_btn,bg=C["card2"],fg=C["amber"],font=(FF,9),padx=12,pady=6)
        b2.pack(side="left"); b2.configure(height=34)
        _shdr(pad,"DATA DIRECTORIES",color=C["muted"],bg=C["bg"])
        for lbl_t2,path in [("Minecraft:",MC_DIR),("Instances:",INST_DIR),("Mods library:",MODS_LIB)]:
            r2=tk.Frame(pad,bg=C["bg"]); r2.pack(fill="x",pady=2)
            tk.Label(r2,text=lbl_t2,font=(FF,9),bg=C["bg"],fg=C["sub"],width=14,anchor="w").pack(side="left")
            tk.Label(r2,text=str(path),font=(MONO,8),bg=C["bg"],fg=C["dim"]).pack(side="left")
        tk.Button(pad,text="💾   Save Settings",command=self._save_settings,
                  bg=C["solar"],fg=C["white"],font=(FFB,11),relief="flat",bd=0,
                  cursor="hand2",padx=22,pady=12,
                  activebackground=C["solar2"],activeforeground=C["white"]
                  ).pack(pady=20,anchor="w")
        return f

    def _save_settings(self):
        save_json(SETTINGS_FILE,self.settings); messagebox.showinfo("Saved","Settings saved!")

    # ═══════════════════════════════════════════════════════════════════════════
    #  CONSOLE
    # ═══════════════════════════════════════════════════════════════════════════
    def _pg_console(self):
        f=tk.Frame(self._main,bg=C["bg"])
        self._ph(f,"Console","Live launcher & game output",accent=C["red"])

        # Toolbar
        tb2=tk.Frame(f,bg=C["bg"]); tb2.pack(fill="x",padx=28,pady=(8,4))
        tk.Label(tb2,text="LIVE OUTPUT",font=(MONO,8,"bold"),bg=C["bg"],fg=C["muted"]).pack(side="left")
        b=_btn(tb2,"Clear",self._clear_console,bg=C["card2"],fg=C["sub"],font=(FF,9),padx=10,pady=4)
        b.pack(side="right"); b.configure(height=30)

        # Console text area
        cc2,ci2=_card(f,fill="#030508",border=C["border"],
                      pack_kw={"fill":"both","expand":True,"padx":28,"pady":(0,20)})
        self._console=tk.Text(ci2,bg="#030508",fg="#4a7a9b",
                               insertbackground=C["solar"],font=(MONO,9),
                               relief="flat",wrap="word",state="disabled",
                               selectbackground=C["card2"],bd=0,padx=14,pady=12)
        vsb=ttk.Scrollbar(ci2,orient="vertical",command=self._console.yview)
        self._console.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); self._console.pack(fill="both",expand=True)
        for tag,fg2 in [("info",C["solar3"]),("success",C["green"]),("warn",C["amber"]),
                         ("error",C["red"]),("dim",C["dim"]),("ts","#1a2a3a"),("game","#2a5a30")]:
            self._console.tag_config(tag,foreground=fg2)
        return f

    def _log(self,msg,level="info"):
        def _do():
            if not hasattr(self,"_console"): return
            self._console.configure(state="normal"); ts=datetime.now().strftime("%H:%M:%S")
            self._console.insert("end",f"[{ts}] ","ts"); self._console.insert("end",f"{msg}\n",level)
            self._console.configure(state="disabled"); self._console.see("end")
        self.root.after(0,_do)

    def _clear_console(self):
        self._console.configure(state="normal"); self._console.delete("1.0","end")
        self._console.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════════════════
    #  LAUNCH
    # ═══════════════════════════════════════════════════════════════════════════
    def _do_launch(self):
        if not self.profiles: messagebox.showwarning("No Profile","Create a profile first."); return
        p=self.profiles[self.cur]; ver=p["version"]
        if not is_installed(ver):
            if messagebox.askyesno("Not Installed",f"MC {ver} not installed. Open Install tab?"): self._nav("install"); return
        req=get_required_java_version(ver)
        jp=p.get("java_path") or self.settings.get("java_path") or "auto"
        if jp in ("","auto"):
            jp,jv=find_java(min_version=req)
            if not jp:
                if self.settings.get("auto_install_java",True):
                    ans = messagebox.askyesno("Java Not Found",
                        f"Java {req}+ is required but not found.\n\n"
                        "Install Adoptium JDK 17 automatically? (~200MB download)")
                    if ans:
                        self._log("Auto-installing Java 17...", "info")
                        try:
                            jp = self._install_java_17(
                                status_cb=lambda s: self._set_status(s))
                        except Exception as ex:
                            messagebox.showerror("Java Install Failed", str(ex)); return
                    else:
                        return
                else:
                    messagebox.showerror(f"Java {req}+ Required",
                        f"MC {ver} needs Java {req}+.\nhttps://adoptium.net"); return
            elif java_major(jv)<req:
                if not messagebox.askyesno("Wrong Java",
                    f"Need Java {req}+, found {java_major(jv)}. Launch anyway?"): return
        username=(self.settings.get("username") or "Player").strip() or "Player"
        uid=(self.settings.get("uuid") or "").strip() or str(uuid.uuid4())
        # Use the offline UUID so the skin server UUID matches what MC will use
        uid = self._offline_uuid(username)
        self._log(f"═══ Launching {p['name']}  (MC {ver}) ═══","success")
        self._nav("console")  # Auto-show console so user sees output

        # Popen flags — suppress console window on Windows (.exe builds)
        _popen_kw = {}
        if platform.system() == "Windows":
            _popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW

        def run():
            skin_server = None
            try:
                self._set_status("Deploying mods…", 15)
                self._deploy_mods(p)

                # ── Start local skin server if profile has a skin ──────────
                self._set_status("Starting skin server…", 30)
                skin_path = p.get("skin_path","")
                if skin_path and Path(skin_path).exists():
                    import io as _io2
                    skin_img = Image.open(skin_path).convert("RGBA")
                    if skin_img.size != (64,64):
                        skin_img = skin_img.resize((64,64), Image.NEAREST)
                    buf = _io2.BytesIO()
                    skin_img.save(buf, format="PNG")
                    skin_server = _LocalSkinServer(buf.getvalue(), username, uid)
                    skin_server.start()
                    self._log(f"✓ Skin server on port {skin_server.port}","success")
                else:
                    self._log("No skin set — using default","info")

                self._set_status("Building command…", 55)
                cmd = build_launch_command(ver, p, username, uid, skin_server)
                self._log(f"User: {username}  |  UUID: {uid[:8]}…  |  MC {ver}","info")

                self._set_status("Starting JVM…", 75)
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    **_popen_kw
                )
                self._game_proc = proc
                self._log("✓ Minecraft running!","success")
                self._set_status("Running ▶", 100)
                if self.settings.get("close_on_launch"):
                    self.root.after(3000, self.root.quit)
                for line in proc.stdout:
                    s = line.rstrip()
                    if not s: continue
                    lvl = ("error" if any(x in s for x in ("ERROR","Exception","FATAL"))
                           else "warn" if "WARN" in s else "game")
                    self._log(s, lvl)
                proc.wait()
                code = proc.returncode
                self._log(f"Game exited (code {code})", "success" if code==0 else "warn")
                self._set_status("Ready", 0)
            except Exception as ex:
                self._log(f"Launch failed: {ex}","error")
                self._log(traceback.format_exc(),"dim")
                msg = str(ex)
                self.root.after(0, lambda m=msg: messagebox.showerror("Launch Failed", m))
                self._set_status("Error")
            finally:
                if skin_server:
                    skin_server.stop()
                    self._log("Skin server stopped","info")

        threading.Thread(target=run, daemon=True).start()

    def _deploy_mods(self,profile):
        loader=profile.get("loader","Vanilla")
        if loader in ("Vanilla","OptiFine",""):
            if profile.get("mods"): self._log("⚠  Loader is Vanilla — mods ignored","warn"); return
        mc_ver   = profile.get("version","")
        game_dir = Path(profile.get("game_dir") or INST_DIR/profile["name"])
        mods_dest= game_dir/"mods"; mods_dest.mkdir(parents=True,exist_ok=True)

        enabled=set()
        for mod in profile.get("mods",[]):
            if mod.get("enabled",True):
                src=Path(mod.get("path",""))
                if src.exists(): enabled.add(src.name)

        # Remove jars not in the enabled set
        removed=0
        for ex2 in mods_dest.iterdir():
            if ex2.suffix in (".jar",".zip") and ex2.name not in enabled:
                try: ex2.unlink(); removed+=1
                except Exception: pass

        # Check each enabled jar for MC version compatibility via fabric.mod.json
        incompatible=[]
        for mod in profile.get("mods",[]):
            if not mod.get("enabled",True): continue
            src=Path(mod.get("path",""))
            if not src.exists(): continue
            try:
                import zipfile as _zf, json as _js
                with _zf.ZipFile(src,"r") as z:
                    if "fabric.mod.json" in z.namelist():
                        fmj=_js.loads(z.read("fabric.mod.json"))
                        deps=fmj.get("depends",{})
                        mc_dep=deps.get("minecraft","")
                        if mc_dep and mc_ver:
                            # Simple check: if dep is an exact version like "1.21.x", reject
                            import re as _re
                            # extract first version number from dep string e.g. ">=1.21 <1.22"
                            nums=_re.findall(r'(\d+\.\d+(?:\.\d+)?)',mc_dep)
                            if nums:
                                dep_major=".".join(nums[0].split(".")[:2])
                                mc_major =".".join(mc_ver.split(".")[:2])
                                if dep_major != mc_major:
                                    incompatible.append((src.name, nums[0], mc_ver))
            except Exception: pass

        if incompatible:
            for name,dep_v,have_v in incompatible:
                self._log(f"INCOMPATIBLE: {name} needs MC {dep_v}, profile is {have_v}","error")
            incompat_names = "\n".join(f"  * {n} (needs {d})" for n,d,_ in incompatible)
            msg = ("These mods require a different Minecraft version:\n"
                   + incompat_names
                   + f"\n\nYour profile uses MC {mc_ver}.\n"
                   "Remove them in Mod Manager or reinstall for the correct version.")
            self.root.after(0, lambda m=msg: messagebox.showerror("Incompatible Mods", m))


        n=0
        for mod in profile.get("mods",[]):
            if not mod.get("enabled",True): continue
            src=Path(mod.get("path",""))
            if src.exists():
                dest=mods_dest/src.name
                if not dest.exists(): shutil.copy2(src,dest); n+=1
        total=len(list(mods_dest.iterdir()))
        self._log(f"Mods: {total} active (+{n}, -{removed})","success" if total>0 else "warn")

    def _set_status(self, msg, pct=None):
        def _upd():
            self._status_lbl.config(text=msg)
            if pct is not None:
                self._prog_var.set(pct)
                self._prog_pct.config(text=f"{int(pct)}%" if pct > 0 else "")
        self.root.after(0, _upd)

    # ─── Background tasks ─────────────────────────────────────────────────────
    def _bg_manifest(self):
        try:
            self._log("Fetching version manifest…","info")
            self.mc_versions=get_mc_versions()
            self._log(f"Manifest: {len(self.mc_versions)} versions","success")
            self.root.after(0,self._fill_ver_tree); self.root.after(0,self._update_stats)
        except Exception as ex: self._log(f"Manifest failed: {ex}","warn")

    def _bg_java(self):
        path,ver2=find_java()
        def _upd():
            if path:
                self._java_lbl.config(text="☕",fg=C["green"])
                self._java_ver_lbl.config(text=ver2,fg=C["green"])
                if "java" in self._sc: self._sc["java"].config(text=ver2)
            else:
                self._java_lbl.config(text="☕",fg=C["red"])
        self.root.after(0,_upd)

    def _java_check(self):
        def _chk():
            path,ver2=find_java()
            def _upd():
                if path:
                    self._java_lbl.config(text="☕",fg=C["green"])
                    if hasattr(self,"_jdet_lbl"): self._jdet_lbl.config(text=f"Found: {path}  ({ver2})",fg=C["green"])
                    messagebox.showinfo("Java Found",f"Java {ver2}\n{path}")
                else:
                    self._java_lbl.config(text="☕",fg=C["red"])
                    messagebox.showerror("Not Found","Install Java from https://adoptium.net")
            self.root.after(0,_upd)
        threading.Thread(target=_chk,daemon=True).start()


    def _java_install_btn(self):
        if not messagebox.askyesno("Install Java 17",
                "Download Adoptium JDK 17 (~200MB)?\n\n"
                "It will be installed in CraftLaunch\'s folder and set automatically."):
            return
        if hasattr(self,"_jdet_lbl"):
            self._jdet_lbl.config(text="Downloading Java 17...", fg=C["amber"])
        def _run():
            try:
                def _pcb(p2):
                    if hasattr(self,"_jdet_lbl"):
                        self.root.after(0,lambda p=p2:
                            self._jdet_lbl.config(text=f"Installing Java 17... {p}%",fg=C["amber"]))
                path = self._install_java_17(progress_cb=_pcb)
                if hasattr(self,"_jdet_lbl"):
                    self.root.after(0,lambda:
                        self._jdet_lbl.config(text="Java 17 installed!",fg=C["green"]))
                self.root.after(0, lambda: messagebox.showinfo("Done",
                    f"Java 17 installed!\nPath set to:\n{path}"))
            except Exception as ex:
                msg=str(ex)
                if hasattr(self,"_jdet_lbl"):
                    self.root.after(0,lambda m=msg:
                        self._jdet_lbl.config(text=f"Failed: {m}",fg=C["red"]))
                self.root.after(0,lambda m=msg: messagebox.showerror("Failed",m))
        threading.Thread(target=_run,daemon=True).start()

    # ─── Profile helpers ──────────────────────────────────────────────────────
    def _new_profile(self):
        dlg=tk.Toplevel(self.root); dlg.title("New Profile"); dlg.configure(bg=C["card"])
        dlg.geometry("400x220"); dlg.grab_set(); dlg.resizable(False,False)
        # Centre on parent
        dlg.update_idletasks()
        px=self.root.winfo_x()+self.root.winfo_width()//2-200
        py=self.root.winfo_y()+self.root.winfo_height()//2-110
        dlg.geometry(f"+{px}+{py}")

        tk.Label(dlg,text="New Profile",font=(FFB,13),bg=C["card"],fg=C["text"]).pack(pady=(16,8))
        tk.Frame(dlg,bg=C["border"],height=1).pack(fill="x")
        body2=tk.Frame(dlg,bg=C["card"]); body2.pack(fill="both",expand=True,padx=20,pady=12)
        name_v=tk.StringVar(value="My World"); ver_v=tk.StringVar(value="1.20.4")
        for lbl_t2,v in [("Name",name_v),("Version",ver_v)]:
            r=tk.Frame(body2,bg=C["card"]); r.pack(fill="x",pady=4)
            tk.Label(r,text=lbl_t2,font=(FF,9),bg=C["card"],fg=C["sub"],width=10,anchor="w").pack(side="left")
            tk.Entry(r,textvariable=v,bg=C["card2"],fg=C["text"],insertbackground=C["solar"],
                     relief="flat",bd=0,font=(FF,11),width=24).pack(side="left",ipady=6,ipadx=8)
        def create():
            np=self._dflt_profile(); np["name"]=name_v.get().strip() or "Profile"
            np["version"]=ver_v.get().strip() or "1.20.4"; self.profiles.append(np)
            save_json(PROFILES_FILE,self.profiles); self.cur=len(self.profiles)-1
            self._reload_profiles(); self._draw_home_card(); self._build_ped()
            self._update_stats(); dlg.destroy()
        tk.Button(body2,text="✓  Create Profile",command=create,bg=C["solar"],fg=C["white"],
                  font=(FFB,10),relief="flat",bd=0,cursor="hand2",padx=16,pady=10,
                  activebackground=C["solar2"],activeforeground=C["white"]).pack(pady=8,anchor="w")

    def _dup_profile(self):
        import copy; p=copy.deepcopy(self.profiles[self.cur]); p["name"]+=" (Copy)"
        self.profiles.append(p); save_json(PROFILES_FILE,self.profiles); self._reload_profiles()

    def _del_profile(self):
        if len(self.profiles)<=1: messagebox.showwarning("Cannot Delete","Must have at least one profile."); return
        if messagebox.askyesno("Delete",f"Delete '{self.profiles[self.cur]['name']}'?"):
            self.profiles.pop(self.cur); self.cur=max(0,self.cur-1)
            save_json(PROFILES_FILE,self.profiles); self._reload_profiles()
            self._draw_home_card(); self._build_ped(); self._update_stats()

    # ─── Misc ─────────────────────────────────────────────────────────────────
    def _update_stats(self):
        if not hasattr(self,"_sc"): return
        for key in ("profiles","installed","mods"):
            if key in self._sc: self._sc[key].config(text=self._cnt(key))

    def _cnt(self,key):
        if key=="profiles": return str(len(self.profiles))
        if key=="installed":
            try: return str(len([d for d in (MC_DIR/"versions").iterdir() if d.is_dir() and (d/(d.name+".jar")).exists()]))
            except: return "0"
        if key=="mods": return str(sum(len(p.get("mods",[])) for p in self.profiles))
        return "0"

    def _open_mc_dir(self): MC_DIR.mkdir(parents=True,exist_ok=True); self._open_dir(MC_DIR)
    def _open_screenshots(self): sd=MC_DIR/"screenshots"; sd.mkdir(parents=True,exist_ok=True); self._open_dir(sd)
    def _open_dir(self,path):
        try:
            if platform.system()=="Windows": os.startfile(path)
            elif platform.system()=="Darwin": subprocess.Popen(["open",str(path)])
            else: subprocess.Popen(["xdg-open",str(path)])
        except Exception as ex: messagebox.showerror("Error",str(ex))

    def run(self):
        self._log(f"{APP} v{VER} ready  —  Solar Edition","success")
        self._log(f"OS: {platform.system()} {platform.machine()}","info")
        self._log(f"Data dir: {BASE}","info")
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    CraftLaunch().run()
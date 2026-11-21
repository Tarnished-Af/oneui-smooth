#!/usr/bin/env python3
"""
oneui-smooth - make a laggy Samsung Galaxy phone smooth again, without root.

It works over adb and only does reversible things:
  * measures how smooth the phone really is (frame times of the home screen and the
    notification shade / lock screen), so you can see what each change does
  * finds what is loading Samsung's System UI (accessibility services, notification
    listeners, Good Lock plugins) and asks you, one by one, what to turn off
  * offers to disable preinstalled bloat, asking about the features you use first
  * tells you which of your apps belong in Never sleeping / Sleeping / Deep sleeping
  * undoes everything it did with one command

Nothing is changed without you typing "y". Every change is logged and can be undone.

Requirements: Python 3.8+, adb (Android platform-tools), USB debugging on the phone.
Usage:        python oneui_smooth.py            (interactive menu)
              python oneui_smooth.py --help     (all commands)
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
DB_FILE = HERE / "packages.json"
RUNS_DIR = Path(os.environ.get("ONEUI_SMOOTH_RUNS", Path.cwd() / "runs"))
STATE_DIR = Path(os.environ.get("ONEUI_SMOOTH_STATE", Path.home() / ".oneui-smooth"))

FRAME_BUDGET_MS = 8.3          # 120 Hz
LAUNCHER = "com.sec.android.app.launcher"
SYSTEMUI = "com.android.systemui"


# ───────────────────────────── terminal helpers ─────────────────────────────

if os.name == "nt":
    os.system("")              # enables ANSI colours in Windows 10+ terminals
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bold(t):   return _c("1", t)
def dim(t):    return _c("2", t)
def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def cyan(t):   return _c("36", t)


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def plain(t):
    return ANSI_RE.sub("", t)


def header(title):
    print("\n" + bold(cyan(f"── {title} " + "─" * max(0, 60 - len(title)))))


def ask(question, default=None):
    """Yes/no question. Returns True/False. Enter picks the default if there is one."""
    hint = {True: "[Y/n]", False: "[y/N]", None: "[y/n]"}[default]
    while True:
        try:
            a = input(f"{question} {dim(hint)} ").strip().lower()
        except EOFError:
            a = ""
        if not a and default is not None:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False
        print("  Please type y or n.")


def pause(prompt):
    try:
        input(f"{prompt} {dim('[Enter]')} ")
    except EOFError:
        pass


# ───────────────────────────────── adb ──────────────────────────────────────

class AdbError(RuntimeError):
    pass


class Phone:
    """Thin wrapper around `adb -s <serial> shell`."""

    def __init__(self, serial):
        self.serial = serial

    def sh(self, cmd, timeout=120):
        try:
            r = subprocess.run(["adb", "-s", self.serial, "shell", cmd],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb timed out running: {cmd}")
        return (r.stdout + r.stderr).replace("\r", "")

    def prop(self, name):
        return self.sh(f"getprop {name}").strip()

    # package state -----------------------------------------------------------
    def packages(self, flag=""):
        out = self.sh(f"pm list packages {flag}")
        return {l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")}

    def disable(self, pkg):
        out = self.sh(f"pm disable-user --user 0 {pkg}").strip()
        return "disabled-user" in out, out

    def enable(self, pkg):
        out = self.sh(f"pm enable {pkg}").strip()
        return "enabled" in out, out

    def setting(self, ns, key):
        v = self.sh(f"settings get {ns} {key}").strip()
        return None if v in ("", "null") else v


def connect():
    if not shutil.which("adb"):
        sys.exit(red("adb was not found.") + " Install Android platform-tools and make sure 'adb' is on your PATH.\n"
                 "  Windows: https://developer.android.com/tools/releases/platform-tools\n"
                 "  Linux:   sudo apt install adb   |   sudo pacman -S android-tools\n"
                 "  macOS:   brew install android-platform-tools")
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devs = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
    unauth = [l for l in out.splitlines()[1:] if "\tunauthorized" in l]
    if not devs:
        if unauth:
            sys.exit(yellow("The phone is connected but not authorised.") +
                     " Unlock it and tap 'Allow' on the USB debugging prompt, then run again.")
        sys.exit(red("No phone found.") + " Connect it by USB with USB debugging on (see README), then check 'adb devices'.")
    serial = devs[0]
    if len(devs) > 1:
        print("More than one device is connected:")
        for i, d in enumerate(devs, 1):
            print(f"  {i}. {d}")
        while True:
            try:
                n = int(input("Which one? "))
                serial = devs[n - 1]
                break
            except (ValueError, IndexError, EOFError):
                print("  Type the number from the list.")
    phone = Phone(serial)
    maker = phone.prop("ro.product.manufacturer").lower()
    phone.model = phone.prop("ro.product.model")
    phone.oneui = phone.prop("ro.build.version.oneui")
    oneui_txt = f"One UI {int(phone.oneui) // 10000}.{int(phone.oneui) // 100 % 100}" if phone.oneui.isdigit() else "One UI ?"
    print(f"Connected: {bold(phone.model)} ({oneui_txt}, Android {phone.prop('ro.build.version.release')})")
    if maker != "samsung":
        print(yellow(f"This is a {maker} phone, not Samsung. The measurements work, but the fixes are Samsung-specific."))
    return phone


# ─────────────────────────── database + state log ───────────────────────────

def load_db():
    if not DB_FILE.exists():
        sys.exit(red(f"{DB_FILE.name} is missing.") + " Keep it in the same folder as oneui_smooth.py.")
    with open(DB_FILE, encoding="utf-8") as f:
        return json.load(f)


def match_db(entry, installed):
    """Installed package ids matching a database entry (exact id or prefix)."""
    if "id" in entry:
        return [entry["id"]] if entry["id"] in installed else []
    return sorted(p for p in installed if p.startswith(entry["id_prefix"]))


class StateLog:
    """Remembers every change per phone so `undo` can reverse it."""

    def __init__(self, serial):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = STATE_DIR / f"{re.sub(r'[^A-Za-z0-9._-]', '_', serial)}.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"changes": []}

    def add(self, kind, target, before=None, note=""):
        self.data["changes"].append({"time": dt.datetime.now().isoformat(timespec="seconds"),
                                     "kind": kind, "target": target, "before": before, "note": note})
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def pop_all(self):
        changes = self.data["changes"]
        self.data["changes"] = []
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        return changes

    @property
    def changes(self):
        return self.data["changes"]


# ───────────────────────────── phone inspection ─────────────────────────────

def parse_lru(text):
    """dumpsys activity lru -> {package: 'active'|'cached'} (strongest state wins)."""
    cached_states = {"CEM", "CAC", "CACC", "CRE", "CAM", "LAST", "BHVR"}
    result = {}
    for line in text.splitlines():
        m = re.search(r"#\s*\d+:\s+\S+(?:\s+\d+)?\s+([A-Z]+)\s+\S+\s+\d+:([\w.]+)", line)
        if not m:
            continue
        state, pkg = m.group(1), m.group(2)
        kind = "cached" if state in cached_states else "active"
        if result.get(pkg) != "active":
            result[pkg] = kind
    return result


def parse_pss(text):
    """dumpsys meminfo -> {process name: MB} from 'Total PSS by process' (falls back to RSS)."""
    for kind in ("PSS", "RSS"):
        out, on = {}, False
        for line in text.splitlines():
            if f"Total {kind} by process" in line:
                on = True
                continue
            if on:
                if not line.strip():
                    break
                m = re.match(r"\s*([\d,]+)K:\s+(\S+)", line)
                if m:
                    out[m.group(2)] = round(int(m.group(1).replace(",", "")) / 1024)
        if out:
            return out
    return {}


def listeners(phone):
    v = phone.setting("secure", "enabled_notification_listeners")
    return [c for c in (v or "").split(":") if c]


def a11y_services(phone):
    v = phone.setting("secure", "enabled_accessibility_services")
    return [c for c in (v or "").split(":") if c]


# ─────────────────────────────── measure ────────────────────────────────────

def gfx_stats(text):
    def num(pat):
        m = re.search(pat, text)
        return int(m.group(1)) if m else None
    jm = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", text)
    return {
        "frames": num(r"Total frames rendered:\s*(\d+)"),
        "janky": int(jm.group(1)) if jm else None,
        "janky_pct": float(jm.group(2)) if jm else None,
        "p50": num(r"50th percentile:\s*(\d+)ms"),
        "p90": num(r"90th percentile:\s*(\d+)ms"),
        "p95": num(r"95th percentile:\s*(\d+)ms"),
        "p99": num(r"99th percentile:\s*(\d+)ms"),
        "gpu_p90": num(r"90th gpu percentile:\s*(\d+)ms"),
        "slow_ui_thread": num(r"Number Slow UI thread:\s*(\d+)"),
        "missed_vsync": num(r"Number Missed Vsync:\s*(\d+)"),
    }


def cmd_measure(phone, label, jank=True, app=None):
    """Snapshot + optional frame test. Saves raw dumps and summary.json/summary.txt."""
    run = RUNS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M}-{re.sub(r'[^A-Za-z0-9._-]', '_', label)}"
    run.mkdir(parents=True, exist_ok=True)
    header(f"Measuring '{label}'")
    print(dim(f"Saving to {run}"))

    def grab(name, cmd):
        print(dim(f"  {name}"))
        text = phone.sh(cmd)
        (run / f"{name}.txt").write_text(text, encoding="utf-8")
        return text

    mem = grab("proc_meminfo", "cat /proc/meminfo")
    up = grab("uptime", "cat /proc/uptime")
    meminfo = grab("dumpsys_meminfo", "dumpsys meminfo")
    lru = grab("activity_lru", "dumpsys activity lru")
    ps = grab("ps", "ps -A -o PID,PPID,USER,RSS,NAME")
    grab("top_idle", "top -b -n 2 -d 2 -m 25")
    grab("pkgs_disabled", "pm list packages -d")
    therm = grab("thermal", "dumpsys thermalservice")

    def kb(key):
        m = re.search(rf"{key}:\s+(\d+)", mem)
        return round(int(m.group(1)) / 1024) if m else None

    samsung = [l.split()[-1] for l in ps.splitlines()[1:]
               if re.search(r"\s(com\.samsung|com\.sec|com\.knox|com\.osp)\S*$", l)]
    tstatus = re.search(r"Thermal Status:\s*(\d+)", therm)
    s = {
        "label": label, "model": phone.model, "oneui": phone.oneui,
        "captured": dt.datetime.now().isoformat(timespec="seconds"),
        "uptime_h": round(float(up.split()[0]) / 3600, 1) if up.strip() else None,
        "mem_available_mb": kb("MemAvailable"),
        "swap_used_mb": (kb("SwapTotal") - kb("SwapFree")) if kb("SwapTotal") is not None else None,
        "samsung_processes": len(samsung),
        "background_active": sum(1 for v in parse_lru(lru).values() if v == "active"),
        "a11y_services": len(a11y_services(phone)),
        "notification_listeners": len(listeners(phone)),
        "thermal_status": int(tstatus.group(1)) if tstatus else None,
        "top_memory": list(parse_pss(meminfo).items())[:10],
        "frames": {},
    }

    def frame_test(tag, pkgs, prompt):
        for p in pkgs:
            phone.sh(f"dumpsys gfxinfo {p} reset")
        pause(prompt)
        for p in pkgs:
            s["frames"][p] = gfx_stats(grab(f"gfxinfo_{tag}_{p}", f"dumpsys gfxinfo {p}"))

    if jank:
        print()
        print(bold("Frame test.") + " Do this for about 20 seconds, the same way every time you measure:")
        print("  • swipe between home screens, open and close the app drawer")
        print("  • pull the notification shade down and up ~10 times")
        print("  • lock and unlock once or twice")
        frame_test("home", [LAUNCHER, SYSTEMUI], "Start now. When you're done, come back here and press Enter.")
    if app:
        frame_test("app", [app], f"Open {app} and use it for ~20 s (scroll, open things). Then press Enter.")

    (run / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    text = format_summary(s)
    (run / "summary.txt").write_text(plain(text), encoding="utf-8")
    print("\n" + text)
    return run


def frame_line(f):
    if not f or not f.get("frames"):
        return "no frames recorded (was this screen used during the test?)"
    return (f"{f['janky_pct']:5.1f}% janky   typical {f['p50']} ms   slow(p90) {f['p90']} ms   "
            f"worst(p99) {f['p99']} ms   ({f['frames']} frames)")


def verdict(f):
    if not f or not f.get("frames") or f.get("janky_pct") is None:
        return ""
    j, p50 = f["janky_pct"], f["p50"]
    if j < 3 and p50 <= FRAME_BUDGET_MS:
        return green("smooth (real 120 Hz)")
    if j < 10:
        return green("mostly smooth, occasional hitches")
    if j < 20:
        return yellow("noticeable stutter")
    return red("laggy - feels like 60 Hz or worse")


def format_summary(s):
    lines = [
        f"label                 : {s['label']}",
        f"phone                 : {s['model']}  (one_ui {s['oneui']})",
        f"captured              : {s['captured']}   (phone on for {s['uptime_h']} h)",
        f"free memory           : {s['mem_available_mb']} MB   swap in use: {s['swap_used_mb']} MB",
        f"samsung processes     : {s['samsung_processes']}   active background processes: {s['background_active']}",
        f"accessibility services: {s['a11y_services']}   notification listeners: {s['notification_listeners']}",
        f"thermal status        : {s['thermal_status']}  (0 = cool, 2+ = throttling)",
        "biggest memory users  : " + ", ".join(f"{n} {mb} MB" for n, mb in s["top_memory"][:6]),
    ]
    for pkg, title in ((LAUNCHER, "Home screen"), (SYSTEMUI, "Shade / lock screen (System UI)")):
        if pkg in s["frames"]:
            f = s["frames"][pkg]
            lines += ["", f"{title}: {verdict(f)}", f"  {frame_line(f)}"]
    for pkg, f in s["frames"].items():
        if pkg not in (LAUNCHER, SYSTEMUI):
            lines += ["", f"{pkg}: {verdict(f)}", f"  {frame_line(f)}"]
    if s["frames"]:
        lines += ["", dim(f"Frame budget at 120 Hz is {FRAME_BUDGET_MS} ms. 'janky' = frames that missed it (visible stutter).")]
    return "\n".join(lines)


def find_run(name):
    p = Path(name)
    if (p / "summary.json").exists():
        return p
    hits = sorted(RUNS_DIR.glob(f"*-{name}")) if RUNS_DIR.exists() else []
    if not hits:
        sys.exit(red(f"No run called '{name}' in {RUNS_DIR}."))
    return hits[-1]


def cmd_compare(a, b):
    ra, rb = find_run(a), find_run(b)
    sa = json.loads((ra / "summary.json").read_text(encoding="utf-8"))
    sb = json.loads((rb / "summary.json").read_text(encoding="utf-8"))
    header(f"{sa['label']}  →  {sb['label']}")
    if abs((sa.get("uptime_h") or 0) - (sb.get("uptime_h") or 0)) > 6:
        print(yellow(f"Note: phone had been on for {sa['uptime_h']} h vs {sb['uptime_h']} h. "
                     "A fresh restart always looks better; compare runs taken in similar conditions."))

    def row(name, va, vb, unit="", lower_better=True):
        if va is None or vb is None:
            return
        diff = vb - va
        better = (diff < 0) if lower_better else (diff > 0)
        mark = green("better") if diff and better else (red("worse") if diff else dim("same"))
        print(f"  {name:<26}{va!s:>8}{unit:<3} →{vb!s:>8}{unit:<3}  {mark}")

    for pkg, title in ((SYSTEMUI, "Shade / lock screen"), (LAUNCHER, "Home screen")):
        fa, fb = sa["frames"].get(pkg), sb["frames"].get(pkg)
        if fa and fb:
            print(bold(title))
            row("janky frames", fa["janky_pct"], fb["janky_pct"], "%")
            row("typical frame (p50)", fa["p50"], fb["p50"], "ms")
            row("slow frames (p90)", fa["p90"], fb["p90"], "ms")
            row("worst frames (p99)", fa["p99"], fb["p99"], "ms")
    print(bold("Background load"))
    row("free memory", sa["mem_available_mb"], sb["mem_available_mb"], "MB", lower_better=False)
    row("swap in use", sa["swap_used_mb"], sb["swap_used_mb"], "MB")
    row("samsung processes", sa["samsung_processes"], sb["samsung_processes"])
    row("active background processes", sa["background_active"], sb["background_active"])
    print(dim("\nBack-to-back runs of the same setup differ by ~2-3 points of janky %; "
              "treat smaller changes as noise."))


# ──────────────────────────────── scan ──────────────────────────────────────

def cmd_scan(phone, db, quiet=False):
    """Read-only report. Returns findings used by `fix`."""
    installed = phone.packages()
    disabled = phone.packages("-d")
    lru = parse_lru(phone.sh("dumpsys activity lru"))
    pss = parse_pss(phone.sh("dumpsys meminfo"))
    f = {"installed": installed, "disabled": disabled, "lru": lru, "pss": pss}

    def ram(pkg):
        mb = sum(v for k, v in pss.items() if k == pkg or k.startswith(pkg + ":"))
        return f"{mb} MB" if mb else ""

    def state(pkg):
        if pkg in disabled:
            return dim("disabled")
        return {"active": yellow("running"), "cached": dim("cached")}.get(lru.get(pkg), dim("not running"))

    f["a11y"] = a11y_services(phone)
    f["listeners"] = listeners(phone)
    f["plugins"] = [p for p in db["goodlock_systemui_plugins"] + db["goodlock_background_modules"]
                    if p["id"] in installed and p["id"] not in disabled]
    f["bloat"] = []
    for e in db["packages"]:
        for pkg in match_db(e, installed):
            if pkg not in disabled:
                f["bloat"].append((e, pkg))
    f["protected_disabled"] = sorted(set(db["protected"]) & disabled)
    f["ram"], f["state"] = ram, state
    if quiet:
        return f

    header("1. What is loading the shade / lock screen (biggest impact)")
    print(bold(f"Accessibility services: {len(f['a11y'])}"))
    for c in f["a11y"]:
        print(f"  • {c.split('/')[0]}  {dim(c.split('/')[-1].split('.')[-1])}")
    if f["a11y"]:
        print(dim("  Every accessibility service receives every screen event. Unused ones cause system-wide lag."))
    print(bold(f"\nApps reading all notifications: {len(f['listeners'])}"))
    for c in f["listeners"]:
        pkg = c.split("/")[0]
        label = db["listener_labels"].get(pkg)
        print(f"  • {label}  {dim(pkg)}" if label else f"  • {pkg}")
    print(bold(f"\nGood Lock plugins/modules active: {len(f['plugins'])}"))
    for p in f["plugins"]:
        print(f"  • {p['name']}  {state(p['id'])} {ram(p['id'])}  {dim(p['note'])}")

    header("2. Preinstalled extras that can be disabled")
    for tier, title in (("safe", "No feature you'd notice"), ("ask", "Depends on what you use"),
                        ("test", "Worth testing"), ):
        items = [(e, p) for e, p in f["bloat"] if e["tier"] == tier]
        if items:
            print(bold(title))
            for e, p in items:
                print(f"  • {e['name']:<28} {state(p):<20} {ram(p):>7}  {dim(p)}")
    if not f["bloat"]:
        print(green("  Nothing left from the list - already clean."))

    if f["protected_disabled"]:
        header("Warning")
        print(red("These core packages are disabled (by a debloat tool, or put in Deep sleeping) and can cause bugs:"))
        for p in f["protected_disabled"]:
            print(f"  • {p}    fix: adb shell pm enable {p}")
    print(dim("\nNothing was changed. Run 'fix' to go through these one by one."))
    return f


# ───────────────────────────────── fix ──────────────────────────────────────

def cmd_fix(phone, db):
    log = StateLog(phone.serial)
    print(bold("\nBefore you start:"))
    print("  • Everything here is reversible ('undo'), but back up your phone first (Smart Switch / Google).")
    print("  • Tip: run 'measure' first, so you can see the difference afterwards.")
    if not ask("Continue?", default=True):
        return
    f = cmd_scan(phone, db, quiet=True)
    changed = 0

    # 1. accessibility services ------------------------------------------------
    if f["a11y"]:
        header("Accessibility services")
        print("Each one sees every touch and screen change, which slows down the whole UI.")
        print("Keep the ones you actually rely on (password-manager autofill, Tasker, real accessibility needs).")
        before = ":".join(f["a11y"])
        keep = []
        for c in f["a11y"]:
            if ask(f"  Turn off {bold(c.split('/')[0])} ({c.split('/')[-1].split('.')[-1]})?", default=None):
                changed += 1
            else:
                keep.append(c)
        if len(keep) != len(f["a11y"]):
            if keep:
                phone.sh(f"settings put secure enabled_accessibility_services '{':'.join(keep)}'")
            else:
                phone.sh("settings delete secure enabled_accessibility_services")
                phone.sh("settings put secure accessibility_enabled 0")
            log.add("a11y", "enabled_accessibility_services", before)
            print(green("  Done."))

    # 2. notification listeners --------------------------------------------------
    removable = [c for c in f["listeners"] if not c.startswith(LAUNCHER + "/")]
    if removable:
        header("Apps that read all your notifications")
        print("Every notification is passed to each of these, and Android keeps them running all the time.")
        print("Keep smartwatch/band apps and anything you use. (Samsung's home screen is kept for icon badges.)")
        for c in removable:
            pkg = c.split("/")[0]
            if ask(f"  Remove notification access for {bold(db['listener_labels'].get(pkg, pkg))}?", default=None):
                phone.sh(f"cmd notification disallow_listener '{c}'")
                log.add("listener", c)
                changed += 1
                print(green("    removed"))

    # 3. Good Lock -------------------------------------------------------------
    if f["plugins"]:
        header("Good Lock modules")
        print("QuickStar, LockStar and NotiStar run inside System UI itself; Display Assistant and GoodCatch run")
        print("in the background. On a Galaxy A36, turning several off cut shade stutter a lot, but LockStar alone")
        print("cost nothing - so keep the ones you love and measure.")
        for p in f["plugins"]:
            if ask(f"  Disable {bold(p['name'])}? {dim(p['note'])}", default=None):
                ok, out = phone.disable(p["id"])
                if ok:
                    log.add("disable", p["id"], note=p["name"])
                    changed += 1
                    print(green("    disabled"))
                else:
                    print(yellow(f"    not changed: {out}"))
        print(dim("  Good Lock changes take effect after a restart."))

    # 4. bloat -------------------------------------------------------------------
    header("Preinstalled extras")
    safe = [(e, p) for e, p in f["bloat"] if e["tier"] == "safe"]
    if safe:
        print(bold("These have no feature you'd notice:"))
        for e, p in safe:
            print(f"  • {e['name']:<28} {dim(e['what'])}")
        choice = ask("Disable all of these?", default=True)
        for e, p in safe:
            if choice or ask(f"  Disable {e['name']}?", default=False):
                changed += _disable(phone, log, e, p)

    answers = {}
    for tier in ("ask", "test"):
        for e, p in [(e, p) for e, p in f["bloat"] if e["tier"] == tier]:
            q = e["question"]
            if q == "bixby_voice" and answers.get("bixby") is False:
                answers[q] = False          # Bixby not used at all -> voice wake-up not needed either
            if q not in answers:
                print()
                answers[q] = ask(db["questions"][q], default=None)
            wants = "yes" if answers[q] else "no"
            if wants == e["answer_disables"]:
                changed += _disable(phone, log, e, p)

    # 5. settings advice -------------------------------------------------------------
    header("Settings to check on the phone (cannot be done safely from a PC)")
    print("  • Lock screen: use a clock style WITHOUT animation (on an A36 this cut shade stutter from 10% to 6.5%).")
    print("  • Settings › Connected devices: turn off 'Continue apps on other devices', 'Call & text on other")
    print("    devices', 'Auto hotspot' and 'Nearby device scanning' if you don't use them.")
    print("  • Settings › Device care › Automation: 'Auto restart at set times' (e.g. daily 3 AM) keeps the")
    print("    phone in its fast just-restarted state.")
    print("  • Battery › Background usage limits: run 'sleep-advice' - don't put everything in deep sleep.")
    print("  • Optional: Accessibility › Visibility enhancements › Reduce transparency and blur (small gain).")

    header("Done")
    print(f"{changed} change(s) made. Log: {log.path}")
    if changed:
        print("Restart the phone now (" + bold("adb reboot") + "), use it normally, then 'measure' again.")
        print("Anything wrong? " + bold("python oneui_smooth.py undo") + " puts everything back.")


def _disable(phone, log, e, pkg):
    ok, out = phone.disable(pkg)
    if ok:
        log.add("disable", pkg, note=e["name"])
        msg = green(f"  disabled {e['name']}")
        if e.get("note"):
            msg += dim(f"  ({e['note']})")
        print(msg)
        if e.get("check_after"):
            print(yellow(f"    Check: {e['check_after']}"))
        return 1
    print(yellow(f"  {e['name']}: not changed ({out})"))
    return 0


# ───────────────────────────── sleep advice ─────────────────────────────────

def last_used(phone):
    """Package -> last time used (datetime), from usage stats."""
    text = phone.sh("dumpsys usagestats", timeout=180)
    seen = {}
    for m in re.finditer(r'package=(\S+)[^\n]*?lastTimeUsed="(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"', text):
        t = dt.datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
        if t.year > 2000 and (m.group(1) not in seen or t > seen[m.group(1)]):
            seen[m.group(1)] = t
    return seen


def cmd_sleep_advice(phone, db):
    header("Which apps should sleep")
    print("Deep sleep on everything makes the phone feel slower: every app starts from scratch each time,")
    print("and messages arrive late. Cached apps (frozen in memory) use no CPU - leave those alone.\n")
    rules = db["sleep_rules"]
    user_apps = sorted(phone.packages("-3"))
    asleep = phone.packages("-d")          # Samsung shows deep-sleeping apps as disabled
    used = last_used(phone)
    if not used:
        print(yellow("Couldn't read app usage history on this phone; advice is based on app type only.\n"))
    now_s = phone.sh("date '+%Y-%m-%d %H:%M:%S'").strip()
    try:
        now = dt.datetime.strptime(now_s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        now = dt.datetime.now()
    lru = parse_lru(phone.sh("dumpsys activity lru"))
    goodlock = {p["id"] for p in db["goodlock_systemui_plugins"] + db["goodlock_background_modules"]}
    # only real apps (with an icon); skips hidden components like Google's SafetyCore
    launchable = {l.split("/")[0].strip() for l in phone.sh(
        "cmd package query-activities --brief -a android.intent.action.MAIN "
        "-c android.intent.category.LAUNCHER").splitlines() if "/" in l}

    groups = {"never": [], "sleeping": [], "deep": [], "unused": []}
    for pkg in user_apps:
        if pkg in goodlock or pkg.startswith(("com.samsung.", "com.sec.")):
            continue                         # Samsung's own apps: leave to Samsung
        if launchable and pkg not in launchable:
            continue                         # not an app you open (a background component)
        low = pkg.lower()
        days = (now - used[pkg]).days if pkg in used else None
        if any(k in low for k in rules["never_sleep"]["match"]):
            cat = "never"
        elif not used:
            cat = "sleeping"
        elif days is None or days > 30:
            cat = "unused"
        elif days > 7:
            cat = "deep"
        else:
            cat = "sleeping"
        groups[cat].append((pkg, days, "asleep" if pkg in asleep else lru.get(pkg)))

    def show(key, title, why):
        items = groups[key]
        if not items:
            return
        print(bold(f"{title} ({len(items)})") + dim(f"  - {why}"))
        for pkg, days, st in items:
            if not used:
                when = ""
            else:
                when = "no use recorded" if days is None else ("used today" if days == 0 else f"last used {days} d ago")
            run = {"active": yellow("  running in background"), "asleep": dim("  (deep sleeping now)")}.get(st, "")
            print(f"  • {pkg:<48} {dim(when)}{run}")
        print()

    show("never", "Never sleeping", rules["never_sleep"]["why"])
    show("sleeping", "Sleeping apps", "used in the last week; can refresh now and then")
    show("deep", "Deep sleeping", "not used for 1-4 weeks")
    show("unused", "Deep sleeping or uninstall", "not opened in over a month, or no use recorded")
    print("Set these on the phone: " + bold("Settings › Battery › Background usage limits") + ".")
    print(dim("Apps not listed in any of the three lists there run normally. 'Never sleeping' only matters if\n"
              "'Put unused apps to sleep' is on. Samsung's own apps are left out on purpose."))


# ───────────────────────────────── undo ─────────────────────────────────────

def cmd_undo(phone):
    log = StateLog(phone.serial)
    if not log.changes:
        print("Nothing to undo for this phone.")
        return
    header("Undo")
    for c in log.changes:
        print(f"  {c['time']}  {c['kind']:<9} {c.get('note') or c['target']}")
    if not ask(f"Reverse all {len(log.changes)} change(s)?", default=False):
        return
    for c in reversed(log.pop_all()):
        if c["kind"] == "disable":
            ok, out = phone.enable(c["target"])
        elif c["kind"] == "listener":
            phone.sh(f"cmd notification allow_listener '{c['target']}'")
            ok, out = True, "allowed"
        elif c["kind"] == "a11y":
            phone.sh(f"settings put secure enabled_accessibility_services '{c['before']}'")
            phone.sh("settings put secure accessibility_enabled 1")
            ok, out = True, "restored"
        else:
            ok, out = False, "unknown change type"
        print((green("  ok   ") if ok else yellow("  ??   ")) + f"{c.get('note') or c['target']}  {dim(out)}")
    print("Restart the phone to finish (adb reboot).")


# ───────────────────────────────── main ─────────────────────────────────────

def menu(phone, db):
    while True:
        header("oneui-smooth")
        print("  1. Measure smoothness           (do this before and after)")
        print("  2. Scan - what's slowing it down (changes nothing)")
        print("  3. Fix - go through it step by step (asks before each change)")
        print("  4. Sleep advice for your apps")
        print("  5. Compare two measurements")
        print("  6. Undo everything this tool changed")
        print("  0. Quit")
        try:
            c = input("Choose: ").strip()
        except EOFError:
            return
        if c == "1":
            label = input("Name for this measurement (e.g. before, after): ").strip() or "run"
            cmd_measure(phone, label)
        elif c == "2":
            cmd_scan(phone, db)
        elif c == "3":
            cmd_fix(phone, db)
        elif c == "4":
            cmd_sleep_advice(phone, db)
        elif c == "5":
            runs = sorted(p.name for p in RUNS_DIR.glob("*") if (p / "summary.json").exists()) if RUNS_DIR.exists() else []
            if len(runs) < 2:
                print("You need at least two measurements first.")
                continue
            for i, r in enumerate(runs, 1):
                print(f"  {i}. {r}")
            try:
                a, b = (int(x) for x in input("Compare which two? (e.g. 1 3): ").split())
                cmd_compare(str(RUNS_DIR / runs[a - 1]), str(RUNS_DIR / runs[b - 1]))
            except (ValueError, IndexError):
                print("Type two numbers from the list.")
        elif c == "6":
            cmd_undo(phone)
        elif c == "0":
            return


def main():
    ap = argparse.ArgumentParser(prog="oneui_smooth.py",
                                 description="Make a laggy Samsung Galaxy smooth again - no root, fully reversible.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("measure", help="measure smoothness and background load")
    m.add_argument("label", help="a name for this run, e.g. before / after")
    m.add_argument("--no-frames", action="store_true", help="skip the interactive frame test")
    m.add_argument("--app", help="also run a frame test inside this app (package name)")
    sub.add_parser("scan", help="report what slows the phone down (read-only)")
    sub.add_parser("fix", help="interactive: asks before every change")
    sub.add_parser("sleep-advice", help="which apps belong in never/sleeping/deep sleeping")
    c = sub.add_parser("compare", help="compare two measurements")
    c.add_argument("before")
    c.add_argument("after")
    sub.add_parser("undo", help="reverse every change this tool made")
    args = ap.parse_args()

    if args.cmd == "compare":           # works offline, no phone needed
        cmd_compare(args.before, args.after)
        return
    db = load_db()
    phone = connect()
    try:
        if args.cmd is None:
            menu(phone, db)
        elif args.cmd == "measure":
            cmd_measure(phone, args.label, jank=not args.no_frames, app=args.app)
        elif args.cmd == "scan":
            cmd_scan(phone, db)
        elif args.cmd == "fix":
            cmd_fix(phone, db)
        elif args.cmd == "sleep-advice":
            cmd_sleep_advice(phone, db)
        elif args.cmd == "undo":
            cmd_undo(phone)
    except AdbError as e:
        sys.exit(red(str(e)))
    except KeyboardInterrupt:
        print("\nStopped. Nothing half-done: every change is applied and logged one at a time.")


if __name__ == "__main__":
    main()

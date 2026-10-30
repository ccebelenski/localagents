#!/usr/bin/env python3
"""ffmem.py: the Firefox side of 3.7w — does the page's DOM pile up as orphan nodes in Firefox?

usage: python3 tools/smoke/ffmem.py <url> <seconds> [every_s=60] [--profile t/ff/profile] [--out t/ff] [--js tools/smoke/churn.js]

Launches the workstation's own Firefox headless on a fresh, extension-free profile (so the only
code in the tab is the page's), opens <url>, and every `every_s` seconds asks Firefox for the same
measurement about:memory's "Minimize memory usage" + "Measure" buttons take: a GC/CC/heap-minimize
pass, then a memory-report dump (``nsIMemoryInfoDumper.dumpMemoryReportsToNamedFile`` with
minimize on). From the content process that holds the tab (found by its ``top(<url>`` window
path) it prints one tab-separated row per sample: ISO time, ``dom/orphan-nodes`` MB,
``dom`` total MB, ``js`` MB (the window's js-zone plus js-non-window), ``heap-unclassified`` MB,
``resident-unique`` MB, and the page's render-pass count (``window.localagentsMonitor.renders``,
-1 when not exported). A column that grows with the push count is the retention; one that holds
is not. The dumps stay under ``--out`` (``memory-<n>.json.gz``) for about:memory's "Load" button.

Talks raw Marionette (protocol 3) over TCP: no geckodriver, no client package. The first and the
last dump also carry every ``explicit/`` row of that process so a reader can see what else moved.
Ports: Marionette listens on 2829 (user.js in the profile), never the default 2828, so a desktop
Firefox is untouched; the profile is `-no-remote` for the same reason. Kill nothing but the
process this script started; it exits when its Firefox does.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

MARIONETTE_PORT = 2829
USER_JS = """\
user_pref("marionette.port", %d);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage", "about:blank");
user_pref("browser.startup.page", 0);
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("browser.newtabpage.enabled", false);
user_pref("extensions.autoDisableScopes", 15);
user_pref("extensions.enabledScopes", 1);
user_pref("browser.aboutConfig.showWarning", false);
user_pref("dom.ipc.processCount", 1);
""" % MARIONETTE_PORT


class Marionette:
    """The protocol: `<len>:<json>` frames; the server greets first; a command is
    [0, id, name, params]; its reply [1, id, error, result]."""

    def __init__(self, port: int, deadline_s: float = 60.0):
        t0 = time.time()
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() - t0 > deadline_s:
                    raise SystemExit("ffmem: Firefox never opened its Marionette port")
                time.sleep(0.5)
        self.sock.settimeout(120)
        self.buf = b""
        self.next_id = 0
        hello = self._recv()
        if hello.get("marionetteProtocol") != 3:
            raise SystemExit(f"ffmem: unexpected Marionette greeting {hello!r}")

    def _recv(self):
        while b":" not in self.buf:
            self.buf += self.sock.recv(65536)
        n, rest = self.buf.split(b":", 1)
        n = int(n)
        while len(rest) < n:
            rest += self.sock.recv(65536)
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def send(self, name: str, params: dict | None = None):
        self.next_id += 1
        frame = json.dumps([0, self.next_id, name, params or {}]).encode()
        self.sock.sendall(str(len(frame)).encode() + b":" + frame)
        while True:
            msg = self._recv()
            if isinstance(msg, list) and msg[0] == 1 and msg[1] == self.next_id:
                _, _, err, result = msg
                if err:
                    raise RuntimeError(f"{name}: {err}")
                return result


DUMP_SCRIPT = """
const [path, minimize] = arguments;
const done = arguments[arguments.length - 1];
const dumper = Cc["@mozilla.org/memory-info-dumper;1"].getService(Ci.nsIMemoryInfoDumper);
dumper.dumpMemoryReportsToNamedFile(path, { callback() { done("ok"); } }, null, false, minimize);
"""

PUSHES_SCRIPT = """
const r = window.localagentsMonitor && window.localagentsMonitor.renders;
const c = window.__churn ? " +" + window.__churn.renders + (window.__churn.threw ? " threw:" + window.__churn.threw : "") : "";
return (r ? r.full + r.inPlace + r.skipped : -1) + c;
"""


def mb(n: float) -> str:
    return f"{n / 1048576:.1f}"


def read_dump(path: Path, url: str) -> tuple[dict, list]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        dump = json.load(f)
    reports = dump["reports"]
    # the dump writes a window's URL with every "/" as "\\": top(http:\\\\127.0.0.1:8772\\, id=7)
    marker = "top(" + url.rstrip("/").replace("/", "\\")
    procs = {r["process"] for r in reports if marker in r["path"]}
    if len(procs) != 1:
        return {"error": f"the tab's process was not found once: {sorted(procs)!r}"}, []
    proc = procs.pop()
    rows = [r for r in reports if r["process"] == proc]
    explicit = [r for r in rows if r["path"].startswith("explicit/")]

    def total(pred) -> float:
        return float(sum(r["amount"] for r in explicit if pred(r["path"])))

    def single(path: str) -> float:
        for r in rows:
            if r["path"] == path:
                return float(r["amount"])
        return float("nan")

    out = {
        "process": proc,
        "orphan": total(lambda p: p.endswith("/dom/orphan-nodes")),
        "dom": total(lambda p: "/active/window(" in p and "/dom/" in p),
        "js": total(lambda p: "/js-zone(" in p or p.startswith("explicit/js-non-window/")),
        "unclassified": single("heap-allocated") - float(sum(r["amount"] for r in explicit if r["kind"] == 1)),
        "resident_unique": single("resident-unique"),
        "explicit": single("explicit"),
    }
    return out, explicit


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    url, seconds = argv[0], float(argv[1])
    every = 60.0
    profile = Path("t/ff/profile")
    out = Path("t/ff")
    js: Path | None = None
    rest = argv[2:]
    if rest and not rest[0].startswith("--"):
        every = float(rest.pop(0))
    while rest:
        flag = rest.pop(0)
        if flag == "--profile":
            profile = Path(rest.pop(0))
        elif flag == "--out":
            out = Path(rest.pop(0))
        elif flag == "--js":
            js = Path(rest.pop(0))
        else:
            raise SystemExit(f"ffmem: unknown argument {flag}")
    out.mkdir(parents=True, exist_ok=True)
    if profile.exists():
        shutil.rmtree(profile)
    profile.mkdir(parents=True)
    (profile / "user.js").write_text(USER_JS)
    firefox = shutil.which("firefox") or "/usr/bin/firefox"
    log = open(out / "firefox.log", "w")
    proc = subprocess.Popen(
        [firefox, "--headless", "-marionette", "-remote-allow-system-access", "-no-remote", "-profile", str(profile.resolve()), "about:blank"],
        stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "MOZ_HEADLESS": "1"},
    )
    try:
        m = Marionette(MARIONETTE_PORT)
        m.send("WebDriver:NewSession", {"capabilities": {}})
        m.send("WebDriver:Navigate", {"url": url})
        time.sleep(3)
        if js is not None:  # e.g. churn.js: drive the page's render paths the way the live world does
            m.send("WebDriver:ExecuteScript", {"script": js.read_text(), "args": []})
        print("\t".join(["time", "orphanMB", "domMB", "jsMB", "unclassifiedMB", "residentUniqueMB", "pushes"]))
        n = max(1, int(seconds // every))
        first_explicit = last_explicit = None
        for i in range(n + 1):
            if i:
                time.sleep(every)
            m.send("Marionette:SetContext", {"value": "content"})
            pushes = m.send("WebDriver:ExecuteScript", {"script": PUSHES_SCRIPT, "args": []})["value"]
            m.send("Marionette:SetContext", {"value": "chrome"})
            dump = (out / f"memory-{i}.json.gz").resolve()
            if dump.exists():
                dump.unlink()
            m.send("WebDriver:ExecuteAsyncScript", {"script": DUMP_SCRIPT, "args": [str(dump), True]})
            for _ in range(100):  # the dumper's callback can run before the file is closed
                if dump.exists() and dump.stat().st_size > 0:
                    break
                time.sleep(0.1)
            figures, explicit = read_dump(dump, url)
            if "error" in figures:
                print(figures["error"])
                continue
            if first_explicit is None:
                first_explicit = explicit
            last_explicit = explicit
            print("\t".join([
                time.strftime("%Y-%m-%dT%H:%M:%S"), mb(figures["orphan"]), mb(figures["dom"]), mb(figures["js"]),
                mb(figures["unclassified"]), mb(figures["resident_unique"]), str(pushes),
            ]))
            sys.stdout.flush()
        if first_explicit and last_explicit:
            first = {r["path"]: r["amount"] for r in first_explicit}
            last = {r["path"]: r["amount"] for r in last_explicit}
            moved = sorted(((last.get(p, 0) - first.get(p, 0), p) for p in set(first) | set(last)), reverse=True)
            print("\nexplicit/ rows that grew the most, first dump to last (MB):")
            for delta, path in moved[:12]:
                if delta <= 0:
                    break
                print(f"  {mb(delta):>8}  {path}")
        m.send("Marionette:SetContext", {"value": "content"})
        try:
            m.send("WebDriver:DeleteSession")
        except Exception:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

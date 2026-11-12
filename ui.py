#!/usr/bin/env python3
"""Browser interface for teams_migrate.py. Run:  python3 ui.py   (opens http://127.0.0.1:8765)

Only listens on this computer (127.0.0.1). No packages needed.
"""
import csv, io, json, os, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))
DATA = os.environ.get("DATA_DIR", HERE)  # where config.json and export/ live (a mounted folder in Docker)
CONFIG = os.path.join(DATA, "config.json")
EXPORT = os.path.join(DATA, "export")
SECRET = "client_secret"
COMMANDS = ("export", "map", "import", "backfill", "relink-files", "rebuild-chats", "copy-photos",
            "clean-scan", "clean-copy", "clean-delete")
PARTS = ("teams", "one-to-one", "group-chats", "meeting-chats", "onedrive")
EXTRAS = ("media_owner_upn", "old_sharepoint")  # optional plain-text settings next to fallback_sender_upn

job = {"proc": None, "command": None, "log": [], "exit": None}
lock = threading.Lock()


def read_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"source": {}, "dest": {}, "fallback_sender_upn": "", "sharepoint_rename": {}}


def masked_config():
    """Never send secrets back to the page; it only learns whether one is saved."""
    cfg = read_config()
    for side in ("source", "dest"):
        s = cfg.setdefault(side, {})
        s["has_secret"] = bool(s.pop(SECRET, ""))
    return cfg


def save_config(new):
    old = read_config()
    cfg = {k: (new.get(k) or "").strip() for k in ("fallback_sender_upn",) + EXTRAS}
    ren = {k: ((new.get("sharepoint_rename") or {}).get(k) or "").strip().lower() for k in ("from", "to")}
    if ren["from"] and ren["to"]:  # links get the tenant's future SharePoint name when a rename is planned
        cfg["sharepoint_rename"] = ren
    for side in ("source", "dest"):
        n, o = new.get(side) or {}, old.get(side) or {}
        cfg[side] = {k: (n.get(k) or "").strip() for k in ("tenant_id", "client_id")}
        cfg[side][SECRET] = (n.get(SECRET) or "").strip() or o.get(SECRET, "")  # empty field = keep saved secret
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG, 0o600)


def run(command, parts, team=""):
    with lock:
        if job["proc"] and job["proc"].poll() is None:
            return False
        args = [sys.executable, "-u", os.path.join(HERE, "teams_migrate.py"), command, "--config", CONFIG, "--out", EXPORT]
        if command == "clean-delete":
            args += ["--port", os.environ.get("SIGNIN_PORT", "8799")]
        if command in ("export", "import") and parts:
            args += ["--parts", ",".join(parts)]
        if command == "import" and team:
            args += ["--team", team]
        job.update(command=command, log=[" ".join(["$ teams_migrate.py", command] + args[8:])], exit=None)
        job["proc"] = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def pump(p):
        for line in p.stdout:
            job["log"].append(line.rstrip("\n"))
        job["exit"] = p.wait()
        job["log"].append(f"--- finished (exit code {job['exit']}) ---")

    threading.Thread(target=pump, args=(job["proc"],), daemon=True).start()
    return True


def users_rows():
    path = os.path.join(EXPORT, "users.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save_users(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source_upn", "dest_upn"])
    for r in rows:
        w.writerow([(r.get("source_upn") or "").strip(), (r.get("dest_upn") or "").strip()])
    with open(os.path.join(EXPORT, "users.csv"), "w", newline="") as f:
        f.write(buf.getvalue())


def status():
    running = bool(job["proc"] and job["proc"].poll() is None)
    count = lambda d: len(os.listdir(os.path.join(EXPORT, d))) if os.path.isdir(os.path.join(EXPORT, d)) else 0
    return {"running": running, "command": job["command"], "exit": job["exit"],
            "teams": count("teams"), "chats": count("chats"),
            "has_users_csv": os.path.exists(os.path.join(EXPORT, "users.csv")),
            "has_archive": os.path.isdir(os.path.join(EXPORT, "archive"))}


def archive_index():
    root = os.path.join(EXPORT, "archive")
    out = []
    for d, _, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(".html"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        # Block other websites and DNS-rebinding from driving this server: exact Host, and a custom header
        # on writes (browsers can't send it cross-site without a CORS preflight, which we never approve).
        host_ok = self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
        if self.command == "POST":
            return host_ok and self.headers.get("X-Teams-Migrate") == "1"
        return host_ok

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/":
            with open(os.path.join(HERE, "ui.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, {"config": masked_config(), "status": status()})
        if path == "/api/log":
            start = int((urlparse(self.path).query.partition("from=")[2] or "0").split("&")[0] or 0)
            return self.send(200, {"lines": job["log"][start:], "next": len(job["log"]), "status": status()})
        if path == "/api/users":
            return self.send(200, {"rows": users_rows()})
        if path == "/api/teams":
            d = os.path.join(EXPORT, "teams")
            names = []
            for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                try:
                    with open(os.path.join(d, n, "team.json")) as f:
                        names.append(json.load(f)["displayName"])
                except (OSError, ValueError, KeyError):
                    pass
            return self.send(200, {"teams": sorted(names, key=str.lower)})
        if path == "/api/archive":
            return self.send(200, {"files": archive_index()})
        if path.startswith("/export/archive/") or path.startswith("/export/hosted/"):
            # Serve the HTML backup and its images, never anything else in export/ (raw data, secrets stay local)
            full = os.path.realpath(os.path.join(DATA, unquote(path.lstrip("/"))))
            allowed = [os.path.realpath(os.path.join(EXPORT, d)) + os.sep for d in ("archive", "hosted")]
            if any(full.startswith(a) for a in allowed) and os.path.isfile(full):
                ctype = "text/html; charset=utf-8" if full.endswith(".html") else "application/octet-stream"
                if not full.endswith(".html"):
                    import mimetypes
                    ctype = mimetypes.guess_type(full)[0] or ctype
                with open(full, "rb") as f:
                    return self.send(200, f.read(), ctype)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        if path == "/api/config":
            for side, label in (("source", "Old"), ("dest", "New")):
                s = body.get(side) or {}
                if s.get("client_id") and s.get("client_id", "").strip() == (s.get("tenant_id") or "").strip():
                    return self.send(400, {"error": f"{label} tenant: App (client) ID is the same as the Tenant ID. "
                                                    "Copy the 'Application (client) ID' from the app's Overview page."})
            save_config(body)
            return self.send(200, {"ok": True, "config": masked_config()})
        if path == "/api/run":
            if body.get("command") not in COMMANDS:
                return self.send(400, {"error": "unknown command"})
            if not os.path.exists(CONFIG):
                return self.send(400, {"error": "Save the settings first."})
            if body["command"] not in ("export", "map") and not users_rows():
                return self.send(400, {"error": "Run 'Match users' first."})
            parts = [x for x in (body.get("parts") or []) if x in PARTS]
            if body["command"] in ("export", "import") and not parts:
                return self.send(400, {"error": "Choose at least one thing to migrate."})
            ok = run(body["command"], parts, str(body.get("team") or "").strip()[:200])
            return self.send(200 if ok else 409, {"ok": ok, "error": None if ok else "Another step is still running."})
        if path == "/api/stop":
            if job["proc"] and job["proc"].poll() is None:
                job["proc"].terminate()
            return self.send(200, {"ok": True})
        if path == "/api/users":
            if users_rows() is None:
                return self.send(400, {"error": "Run 'Match users' first."})
            save_users(body.get("rows") or [])
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    # BIND=0.0.0.0 only inside the Docker container; publish it with -p 127.0.0.1:8765:8765 so it stays local
    server = ThreadingHTTPServer((os.environ.get("BIND", "127.0.0.1"), PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Teams migration UI running at {url}  (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

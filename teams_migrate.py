#!/usr/bin/env python3
"""Free Microsoft Teams tenant-to-tenant migration using Microsoft Graph. Python 3.8+, no packages needed.

Main steps (run in this order; each one is safe to re-run and resumes where it stopped):
  export         read teams, chats, files (and OneDrive) from the OLD tenant into ./export; --parts picks what
  map            write export/users.csv (old user -> new user); check and fix it before importing
  import         recreate it in the NEW tenant with original senders and dates; --parts picks what

Optional extras (see docs/commands.md):
  backfill       re-download images, voice notes and videos an earlier export missed
  relink-files   copy files that chat messages link to in the old tenant, so links point inside the new tenant
  rebuild-chats  rebuild group chats with every old-tenant link replaced (and add meeting recordings)
  copy-photos    copy profile photos of matched users
  clean-scan     list imported messages that still show an old-tenant address
  clean-copy     post a clean copy right after each of those messages
  clean-delete   a sender signs in once in the browser and their originals are deleted

Docs: README.md and docs/. Permissions per step: docs/permissions.md.
"""
import argparse, base64, csv, glob, hashlib, html, http.client, json, mimetypes, os, re, socket, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

GRAPH = "https://graph.microsoft.com/v1.0"
SOURCE_ROLES = ["User.Read.All", "Group.Read.All", "TeamSettings.Read.All", "Channel.ReadBasic.All", "ChannelMember.Read.All",
                "TeamMember.Read.All", "ChannelMessage.Read.All", "Chat.Read.All", "Files.Read.All", "Sites.Read.All"]
DEST_ROLES = ["User.Read.All", "Team.Create", "Group.ReadWrite.All", "TeamSettings.ReadWrite.All", "Channel.Create",
              "ChannelMember.ReadWrite.All", "TeamMember.ReadWrite.All", "Teamwork.Migrate.All", "Chat.Create",
              "Chat.ReadWrite.All", "ChatMember.ReadWrite.All", "Files.ReadWrite.All", "Sites.ReadWrite.All"]
PARTS = {  # what a run covers; --parts picks some of them
    "teams": "teams, channels, posts, channel files and members",
    "one-to-one": "one-to-one chats",
    "group-chats": "group chats",
    "meeting-chats": "meeting chats (recreated as group chats)",
    "onedrive": "each matched user's OneDrive",
}
CHAT_KINDS = {"one-to-one": "oneOnOne", "group-chats": "group", "meeting-chats": "meeting"}
CHUNK = 320 * 1024 * 32  # 10 MiB; upload chunks must be multiples of 320 KiB
HOSTED_RE = re.compile(r'src="(https://graph\.microsoft\.com/[^"]+/hostedContents/[^"]+/\$value)"')
AT_RE = re.compile(r'<at id="(\d+)">(.*?)</at>', re.S)
TEXT_PART = 20000
IMAGE_BUDGET = 3000000  # base64 adds a third, and Teams refused a message with a 4 MB image
LINK_LABELS = {"audio": "▶️ Play voice message", "video": "🎥 Open video", "image": "🖼 Open full-size image"}
VIDEO_RE = re.compile(r'<video[^>]*hostedContents.*?(?:</video>|/>)', re.S)
ATTACH_RE = re.compile(r'<attachment id="[^"]*">\s*</attachment>')


def parse_parts(text, default):
    parts = [x.strip().lower() for x in (text or "").split(",") if x.strip()] or list(default)
    bad = [x for x in parts if x not in PARTS]
    if bad:
        raise SystemExit(f"ERROR: unknown part(s) {', '.join(bad)}. Choose from: {', '.join(PARTS)}")
    return set(parts)


def chat_kinds(parts):
    return {CHAT_KINDS[p] for p in parts if p in CHAT_KINDS}


def log(*a):
    sys.stdout.write(" ".join([time.strftime("%H:%M:%S"), *map(str, a)]) + "\n")  # one write, so parallel workers can't mix lines
    sys.stdout.flush()


WORKERS = 8  # Microsoft's limits are per conversation and per request rate; 429s are waited out in Graph.req
MARK_LOCK = threading.Lock()


def run_parallel(fn, items):
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(fn, items))


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(path + ".tmp", path)


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name or "untitled")[:80]


# ---------------------------------------------------------------- Graph client

class GraphError(Exception):
    def __init__(self, status, text):
        super().__init__(f"HTTP {status}: {text[:500]}")
        self.status = status


def wait_for_internet(host="login.microsoftonline.com"):
    """Blocks through an outage so a dropped connection never fails or stops the migration."""
    said = False
    while True:
        try:
            socket.create_connection((host, 443), 10).close()
            if said:
                log("internet is back, continuing")
            return
        except OSError:
            if not said:
                log("no internet, waiting for it to come back...")
                said = True
            time.sleep(30)


class Graph:
    def __init__(self, cfg):
        self.cfg, self._tok, self._exp = cfg, None, 0

    def token(self):
        if time.time() > self._exp - 300:
            data = urllib.parse.urlencode({
                "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}).encode()
            url = f"https://login.microsoftonline.com/{self.cfg['tenant_id']}/oauth2/v2.0/token"
            for attempt in range(10):
                try:
                    with urllib.request.urlopen(url, data, timeout=60) as r:
                        j = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    if e.code >= 500 and attempt < 9:
                        time.sleep(2 ** attempt)
                        continue
                    try:
                        why = json.load(e).get("error_description", "").split("\r\n")[0]
                    except ValueError:
                        why = str(e)
                    raise SystemExit(f"ERROR: sign-in to the {self.cfg.get('_label', '')} tenant failed. Check its Tenant ID, "
                                     f"App (client) ID and client secret VALUE in Settings.\nMicrosoft says: {why}")
                except (OSError, http.client.HTTPException, ValueError):
                    if attempt == 9:
                        raise
                    wait_for_internet()
                    time.sleep(2 ** attempt)
            self._tok, self._exp = j["access_token"], time.time() + int(j["expires_in"])
        return self._tok

    def req(self, method, url, body=None, data=None, headers=None, auth=True):
        """Returns (status, headers, json-or-bytes). Retries throttling and network errors."""
        if not url.startswith("http"):
            url = GRAPH + url
        url = url.replace(" ", "%20")  # $filter expressions contain spaces; urllib refuses raw ones
        h = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        for attempt in range(20):
            if auth:
                h["Authorization"] = "Bearer " + self.token()
                h.setdefault("Prefer", "include-unknown-enum-members")  # else shared channels come back as "unknownFutureValue"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=300) as r:
                    raw = r.read()
                    is_json = raw and "json" in r.headers.get("Content-Type", "")
                    return r.status, r.headers, json.loads(raw) if is_json else raw
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code in (429, 502, 503, 504) and attempt < 19:
                    wait = e.headers.get("Retry-After")
                    time.sleep(int(wait) if wait and wait.isdigit() else min(2 ** attempt, 60))
                    continue
                raise GraphError(e.code, text)
            except (OSError, http.client.HTTPException):
                if attempt == 19:
                    raise
                wait_for_internet()
                time.sleep(min(2 ** attempt, 60))

    def require(self, roles):
        """Stop before doing anything if the app is missing application permissions (with admin consent)."""
        p = self.token().split(".")[1]
        granted = set(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("roles", []))
        missing = [r for r in roles if r not in granted]
        if missing:
            raise SystemExit(f"ERROR: the app in the {self.cfg.get('_label', '')} tenant is missing these permissions:\n  "
                             + ", ".join(missing) + "\nAdd them in Entra admin center → App registrations → your app → API "
                             "permissions → Add → Microsoft Graph → Application permissions, then click "
                             "'Grant admin consent'. Wait a minute, then run this step again.")

    def get(self, url):
        return self.req("GET", url)[2]

    def post(self, url, body=None):
        return self.req("POST", url, body=body, data=None if body is not None else b"")[2]

    def get_all(self, url):
        out = []
        while url:
            j = self.get(url)
            out += j.get("value", [])
            url = j.get("@odata.nextLink")
        return out


# ---------------------------------------------------------------- time helpers

_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def parse_ts(s):
    m = _TS.match(s)
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        microsecond=int((m.group(2) or "0")[:6].ljust(6, "0")), tzinfo=timezone.utc)
    tz = m.group(3)
    if tz and tz != "Z":
        off = timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
        dt = dt - off if tz[0] == "+" else dt + off
    return dt


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def unique_ts(used, ts):
    """Teams rejects two imported messages with the same millisecond in one conversation."""
    dt = parse_ts(ts)
    while fmt_ts(dt) in used:
        dt += timedelta(milliseconds=1)
    used.add(fmt_ts(dt))
    return fmt_ts(dt)


# ---------------------------------------------------------------- message helpers

def sender(m):
    f = m.get("from") or {}
    u = f.get("user") or f.get("application") or {}
    return u.get("id"), u.get("displayName") or "Unknown"


def is_real(m):
    return m.get("messageType") == "message" and not m.get("deletedDateTime")


def card_dates(text):
    """Cards store dates as {{DATE(iso,SHORT)}} / {{TIME(iso)}}, which only Teams itself expands."""
    return re.sub(r"\{\{(DATE|TIME)\(([^,)]+)[^}]*\}\}", lambda x: x.group(2)[:10] if x.group(1) == "DATE" else x.group(2)[11:16], text)


def card_lines(n):
    """Readable text of an Adaptive Card, in reading order; buttons are left out."""
    if isinstance(n, list):
        return [t for x in n for t in card_lines(x)]
    if not isinstance(n, dict) or str(n.get("type", "")).startswith("Action."):
        return []
    if n.get("type") == "TextBlock":
        return [card_dates(n["text"])] if str(n.get("text") or "").strip() else []
    if isinstance(n.get("title"), str) and isinstance(n.get("value"), str):  # one row of a FactSet
        return [f"{n['title']}: {n['value']}"]
    return [t for v in n.values() if isinstance(v, (dict, list)) for t in card_lines(v)]


def parse_json(text):
    try:
        v = json.loads(text or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def forward_html(a, umap):
    prev = parse_json(a.get("content"))
    user = (prev.get("originalMessageSender") or {}).get("user") or {}
    who = user.get("displayName") or (umap.get(user.get("id")) or {}).get("displayName") or ""
    # mentions inside a forward have no matching entry on the new message, and Teams rejects those
    body = AT_RE.sub(r"\2", ATTACH_RE.sub("", prev.get("originalMessageContent") or ""))
    return f"<blockquote><em>Forwarded{' from ' + html.escape(who) if who else ''}</em><br>{body}</blockquote>"


def md_html(text):
    """Card text is Markdown; Teams import needs HTML."""
    t = html.escape(text, quote=False)
    t = re.sub(r"\\([\\`*_{}\[\]()#+.!-])", r"\1", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return re.sub(r"\s*\n\s*", "<br>", t.strip())


def email_html(lines):
    """Outlook "Share to Teams" card, read in order: subject, sender, address, To, Cc, date, then the text."""
    subject, rest = lines[0], lines[1:]
    head = []
    if rest:
        who = f"From: <b>{md_html(rest.pop(0))}</b>"
        if rest and rest[0].startswith("<"):
            who += " " + html.escape(rest.pop(0), quote=False)
        head.append(who)
    while rest and re.match(r"To |Cc |\d{4}-\d\d-\d\d", rest[0]):
        head.append(md_html(rest.pop(0)))
    body = "".join(f"<p>{md_html(x)}</p>" for x in rest)
    return f"<p><b>📧 {md_html(subject)}</b></p><p>{'<br>'.join(head)}</p>{body}"


def card_html(a):
    """What a card said, as plain HTML, because Teams import refuses cards."""
    ct, c = a.get("contentType") or "", parse_json(a.get("content"))
    if ct.endswith(".audio"):
        return f"<p><em>🎤 Voice message ({html.escape(str(c.get('duration') or '').replace('PT', '').lower())})</em></p>"
    if ct == "tabReference":
        return f"<p><em>Tab: {html.escape(urllib.parse.unquote(a.get('name') or ''))}</em></p>"
    if ct == "meetingReference":
        return f"<p><em>📅 Meeting: {html.escape(a.get('name') or '')}</em></p>"
    lines = card_lines(c.get("body")) or [str(c[k]) for k in ("title", "subtitle", "text") if c.get(k)]
    if c.get("componentUrl"):
        return f'<p>📎 <a href="{html.escape(c["componentUrl"])}">Loop component (old tenant)</a></p>'
    if not lines:
        return ""  # e.g. Teams' own empty placeholder cards: nothing was there to keep
    if "TeamsEmailSubject" in (a.get("content") or ""):
        return email_html(lines)
    return "<blockquote>" + "".join(f"<p>{md_html(x)}</p>" for x in lines) + "</blockquote>"


def attach_html(m, umap=()):
    """Teams import only accepts inline images, so other attachments become quotes and links."""
    umap, quotes, rest = dict(umap), "", ""
    for a in m.get("attachments") or []:
        ct = a.get("contentType") or ""
        if ct == "reference":
            # contentUrl has raw spaces but drive webUrls use %20; the link rewrite needs them to match
            url = (a.get("contentUrl") or "").replace(" ", "%20")
            rest += f'<p>📎 <a href="{html.escape(url)}">{html.escape(a.get("name") or "file")}</a></p>'
        elif ct == "messageReference":
            prev = parse_json(a.get("content"))
            who = ((prev.get("messageSender") or {}).get("user") or {}).get("displayName") or ""
            quotes += f"<blockquote><b>{html.escape(who)}</b>: {html.escape(prev.get('messagePreview') or '')}</blockquote>"
        elif ct == "forwardedMessageReference":
            quotes += forward_html(a, umap)
        else:
            rest += card_html(a)
    return quotes, rest


def rewrite_urls(content, url_map):
    for old, new in url_map:
        content = content.replace(old, new)
    return content


def rename_map(r):
    """Links in imported messages can't be edited later, so they carry the SharePoint name the tenant is being renamed to."""
    return [(f"https://{r['from']}{s}.sharepoint.com", f"https://{r['to']}{s}.sharepoint.com") for s in ("-my", "")]


OLD_RX = None  # links to the old tenant's SharePoint; set by commands that know its name


def old_link_re(name):
    # also catches links that carry the address encoded inside them, e.g. Teams meeting-recap links (sitePath=https%3A%2F%2F...)
    return re.compile(r'https?://[^\s"\'<>)]*?%s(?:-my)?\.sharepoint\.com[^\s"\'<>)]*' % re.escape(name), re.I)


def link_variants(u):
    """The same address as it can appear inside a message: raw, %20 for spaces, HTML-escaped."""
    raw = html.unescape(u)
    return {v for x in (raw, raw.replace(" ", "%20")) for v in (x, html.escape(x, quote=False), html.escape(x))}


def strip_old_links(content, rx):
    """No address of the old tenant may remain: a link keeps its text, a bare address becomes the file name."""
    content = re.sub(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                     lambda m: m.group(2) if rx.match(html.unescape(m.group(1))) else m.group(0), content, flags=re.S | re.I)

    def name(m):
        last = urllib.parse.unquote(m.group(0).split("?")[0].rstrip("/").rsplit("/", 1)[-1])
        return html.escape(last) if "." in last else "[file]"
    return rx.sub(name, content)


def duration_text(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", iso or "")
    if not m:
        return ""
    h, mi, se = int(m.group(1) or 0), int(m.group(2) or 0), int(float(m.group(3) or 0))
    return " ".join(x for x in (f"{h}h" if h else "", f"{mi}m" if mi else "", f"{se}s" if se and not h else "") if x)


def recording_messages(ch, state):
    """Each meeting recording becomes a message at the time it was made, linking to its copy in the new tenant."""
    organizer = ((ch.get("onlineMeetingInfo") or {}).get("organizer") or {}).get("user") or {}
    out = []
    for m in ch.get("messages") or []:
        ed = m.get("eventDetail") or {}
        if "callRecording" not in (ed.get("@odata.type") or "") or not ed.get("callRecordingUrl"):
            continue
        if ed.get("callRecordingStatus") not in (None, "success"):
            continue
        who = ((ed.get("initiator") or {}).get("user")) or organizer
        name = html.escape(ed.get("callRecordingDisplayName") or "Meeting recording")
        dur = duration_text(ed.get("callRecordingDuration"))
        link = state.get("link|" + html.unescape(ed["callRecordingUrl"]))
        text = f'<a href="{html.escape(link)}">{name}</a>' if link else f"{name} (no longer available)"
        out.append({"id": "rec-" + m["id"], "messageType": "message", "createdDateTime": m["createdDateTime"],
                    "from": {"user": {"id": who.get("id"), "displayName": who.get("displayName")}},
                    "body": {"contentType": "html", "content": f"<p>🎥 Meeting recording: {text}{f' ({dur})' if dur else ''}</p>"}})
    return out


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def split_media(m, out):
    """(images that fit inside the message, everything that needs a link instead). Teams import accepts only images."""
    inline, linked, room = [], list(m.get("_audio") or []), IMAGE_BUDGET
    for h in m.get("_hosted") or []:
        size = os.path.getsize(os.path.join(out, h["file"])) if h["contentType"].startswith("image/") else room + 1
        if size <= room:
            inline.append(h)
            room -= size
        elif not any(h["file"] == x["file"] for x in linked):
            linked.append(h)
    return inline, linked


def build_message(m, umap, fallback, out, created, url_map=()):
    """Turn an exported message into a Teams import body for the new tenant."""
    src_id, name = sender(m)
    dst = umap.get(src_id)
    b = m.get("body") or {}
    content = b.get("content") or ""
    if b.get("contentType") == "text":
        content = html.escape(content)
    if not is_real(m):
        content = "<p><em>This message has been deleted.</em></p>"
    if not dst:
        dst = fallback
        content = f"<p><em>Originally sent by {html.escape(name)}</em></p>" + content

    mentions, keep = [], set()
    for mt in m.get("mentions") or []:
        d = umap.get(((mt.get("mentioned") or {}).get("user") or {}).get("id"))
        if d:
            keep.add(str(mt["id"]))
            mentions.append({"id": mt["id"], "mentionText": mt.get("mentionText"), "mentioned": {"user": {
                "id": d["id"], "displayName": d["displayName"], "userIdentityType": "aadUser"}}})
    content = AT_RE.sub(lambda x: x.group(0) if x.group(1) in keep else x.group(2), content)

    quotes, rest = attach_html(m, umap)
    content = quotes + ATTACH_RE.sub("", content) + rest

    hosted = []
    for i, hc in enumerate(split_media(m, out)[0], 1):
        with open(os.path.join(out, hc["file"]), "rb") as f:
            data = base64.b64encode(f.read()).decode()
        content = content.replace(hc["url"], f"../hostedContents/{i}/$value")
        hosted.append({"@microsoft.graph.temporaryId": str(i), "contentBytes": data, "contentType": hc["contentType"]})
    content = VIDEO_RE.sub("<em>🎥 Video clip</em>", content)  # Teams import only accepts images
    content = re.sub(r'<img[^>]*graph\.microsoft\.com/[^>]*hostedContents[^>]*>', "<em>[image]</em>", content)  # old-tenant images we don't hold

    for kind, url in m.get("_links") or []:
        content += f'<p><a href="{html.escape(url)}">{LINK_LABELS.get(kind, "Open file")}</a></p>'

    body = {
        "createdDateTime": created,
        "from": {"user": {"id": dst["id"], "displayName": dst["displayName"], "userIdentityType": "aadUser"}},
        "body": {"contentType": "html", "content": (strip_old_links(rewrite_urls(content, url_map), OLD_RX) if OLD_RX
                                                    else rewrite_urls(content, url_map)) or "&nbsp;"},
    }
    if m.get("subject"):
        body["subject"] = m["subject"]
    if m.get("importance") not in (None, "normal"):
        body["importance"] = m["importance"]
    if mentions:
        body["mentions"] = mentions
    if hosted:
        body["hostedContents"] = hosted
    reactions = []
    for r in m.get("reactions") or []:
        d = umap.get(((r.get("user") or {}).get("user") or {}).get("id"))
        if d:
            reactions.append({"reactionType": r["reactionType"], "createdDateTime": r["createdDateTime"],
                              "user": {"user": {"id": d["id"], "displayName": d["displayName"], "userIdentityType": "aadUser"}}})
    if reactions:
        body["reactions"] = reactions
    return body


def compact_html(c):
    """Pasted rich text carries kilobytes of styling per line, which can push a message over Teams' size limit."""
    return re.sub(r'\s(?:style|class|lang|data-[\w-]+)="[^"]*"', "", c)


def post_msg(g, url, body):
    """Post one message. If Teams rejects reactions or mentions, retry without them rather than lose the message."""
    if len(body["body"]["content"]) > 20000:
        body = {**body, "body": {"contentType": "html", "content": compact_html(body["body"]["content"])}}
    tries = [body]
    if "reactions" in body:
        tries.append({k: v for k, v in tries[-1].items() if k != "reactions"})
    if "mentions" in body:
        plain = {k: v for k, v in tries[-1].items() if k != "mentions"}
        plain["body"] = {"contentType": "html", "content": AT_RE.sub(r"\2", tries[-1]["body"]["content"])}
        tries.append(plain)
    if "hostedContents" in body:
        noimg = {k: v for k, v in tries[-1].items() if k != "hostedContents"}
        noimg["body"] = {"contentType": "html", "content": re.sub(
            r"<img[^>]*hostedContents[^>]*>", "<em>[image could not be migrated]</em>", tries[-1]["body"]["content"])}
        tries.append(noimg)
    # last resort: same sender and date, plain text only, split into parts rather than cut, so nothing is lost
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h\d)>", "\n", tries[-1]["body"]["content"])
    text = re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
    parts = [text[n:n + TEXT_PART] for n in range(0, len(text), TEXT_PART)] or ["(empty message)"]
    tries.append({"createdDateTime": body["createdDateTime"], "from": body["from"],
                  "body": {"contentType": "text", "content": parts[0]}})
    i, bumps = 0, 0
    while True:
        try:
            mid = g.post(url, tries[i])["id"]
            if i == len(tries) - 1:
                for n, part in enumerate(parts[1:], 1):
                    post_msg(g, url, {"createdDateTime": fmt_ts(parse_ts(tries[i]["createdDateTime"]) + timedelta(milliseconds=n)),
                                      "from": body["from"], "body": {"contentType": "text", "content": part}})
            return mid
        except GraphError as e:
            if e.status == 409 and bumps < 50:  # same millisecond already used (re-run after a crash): shift by 1 ms
                bumps += 1
                for t in tries:
                    t["createdDateTime"] = fmt_ts(parse_ts(t["createdDateTime"]) + timedelta(milliseconds=1))
                continue
            if e.status not in (400, 413) or i == len(tries) - 1:
                raise
            i += 1
            log("    rejected, retrying with less detail:", str(e)[:200])


def write_archive(path, title, msgs):
    """Readable HTML copy of a conversation, kept as a backup."""
    rows = []
    for m in sorted(msgs, key=lambda x: x["createdDateTime"]):
        for x in [m] + sorted(m.get("replies") or [], key=lambda r: r["createdDateTime"]):
            if not is_real(x):
                continue
            b = x.get("body") or {}
            content = b.get("content") or ""
            if b.get("contentType") == "text":
                content = html.escape(content)
            for hc in x.get("_hosted") or []:
                content = content.replace(hc["url"], "../../" + hc["file"])
            quotes, rest = attach_html(x)
            pad = ' style="margin-left:2em"' if x is not m else ""
            rows.append(f"<div{pad}><b>{html.escape(sender(x)[1])}</b> <small>{x['createdDateTime']}</small>"
                        f"<div>{quotes}{ATTACH_RE.sub('', content)}{rest}</div></div><hr>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                f"<body style='font-family:sans-serif;max-width:900px;margin:auto'><h1>{html.escape(title)}</h1>{''.join(rows)}")


# ---------------------------------------------------------------- export (old tenant)

def save_hosted(g, url, out):
    try:
        _, hdrs, raw = g.req("GET", html.unescape(url))
    except GraphError as e:
        log("    media skipped:", str(e)[:150])
        return None
    ct = (hdrs.get("Content-Type") or "image/png").split(";")[0]
    name = "hosted/" + hashlib.sha1(url.encode()).hexdigest() + (mimetypes.guess_extension(ct) or ".bin")
    os.makedirs(os.path.join(out, "hosted"), exist_ok=True)
    with open(os.path.join(out, name), "wb") as f:
        f.write(raw)
    return {"url": url, "file": name, "contentType": ct}


def fetch_hosted(g, msg, out):
    msg["_hosted"] = []
    text = (msg.get("body") or {}).get("content") or ""
    for a in msg.get("attachments") or []:
        if a.get("contentType") == "forwardedMessageReference":
            text += parse_json(a.get("content")).get("originalMessageContent") or ""
    for url in sorted(set(HOSTED_RE.findall(text))):
        hc = save_hosted(g, url, out)
        if hc:
            msg["_hosted"].append(hc)


def backfill_message(g, m, out):
    """Adds what the first export didn't keep: voice messages and images inside forwards. Returns [saved, failed]."""
    done, bad, have = 0, 0, {h["url"] for h in m.get("_hosted") or []}
    for tag in VIDEO_RE.findall((m.get("body") or {}).get("content") or ""):
        url = None if m.get("_audio") else re.search(r'src="([^"]+)"', tag)
        hc = url and save_hosted(g, url.group(1), out)
        if hc:
            m["_audio"] = [hc]
        done, bad = done + bool(hc), bad + (bool(url) and not hc)
    for a in m.get("attachments") or []:
        ct = a.get("contentType") or ""
        if ct.endswith(".audio") and not m.get("_audio"):
            url = (parse_json(a.get("content")).get("media") or [{}])[0].get("url")
            hc = url and save_hosted(g, url, out)
            if hc:
                m["_audio"] = [hc]
            done, bad = done + bool(hc), bad + (not hc)
        elif ct == "forwardedMessageReference":
            for url in set(HOSTED_RE.findall(parse_json(a.get("content")).get("originalMessageContent") or "")) - have:
                hc = save_hosted(g, url, out)
                if hc:
                    m.setdefault("_hosted", []).append(hc)
                done, bad = done + bool(hc), bad + (not hc)
    return done, bad


def cmd_backfill(cfg, out):
    g, saved, failed = Graph(cfg["source"]), 0, 0
    paths = glob.glob(f"{out}/teams/*/team.json") + glob.glob(f"{out}/chats/*.json")
    for i, path in enumerate(paths, 1):
        doc, before = load(path), (saved, failed)
        groups = [c["messages"] for c in doc["channels"]] if "channels" in doc else [doc.get("messages") or []]
        for msgs in groups:
            for m in msgs:
                for x in [m] + (m.get("replies") or []):
                    d, b = backfill_message(g, x, out)
                    saved, failed = saved + d, failed + b
        if saved != before[0]:
            save(path, doc)
        if i % 100 == 0:
            log(f"backfill: {i}/{len(paths)} conversations, {saved} saved, {failed} failed")
    log(f"backfill finished: {saved} saved, {failed} failed")


def export_messages(g, base, out, replies):
    msgs = g.get_all(base + "?$top=50")
    for m in msgs:
        if replies:
            m["replies"] = g.get_all(f"{base}/{m['id']}/replies?$top=50")
        for x in [m] + m.get("replies", []):
            fetch_hosted(g, x, out)
    return msgs


PROBLEMS = []  # export problems, reported at the end instead of stopping the run


def problem(what, err):
    PROBLEMS.append(f"{what}: {str(err)[:300]}")
    log("  PROBLEM", what, "-", str(err)[:300])


def download_file(g, drive, it, dst):
    """Download links expire after about an hour, so on failure ask Graph for a fresh link and retry."""
    url = it.get("@microsoft.graph.downloadUrl")
    for attempt in range(4):
        try:
            if not url:
                # no $select: Graph silently drops the download link when you $select it
                url = g.get(f"/drives/{drive}/items/{it['id']}")["@microsoft.graph.downloadUrl"]
            with urllib.request.urlopen(url, timeout=300) as r, open(dst + ".part", "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(dst + ".part", dst)  # a half-written file never looks finished
            return
        except (urllib.error.HTTPError, OSError, KeyError) as e:
            if attempt == 3:
                raise OSError(f"download failed after 4 tries: {e}")
            url = None
            time.sleep(2 ** attempt)


def export_drive(g, root_endpoint, local_dir):
    try:
        root = g.get(root_endpoint)
    except GraphError as e:
        log("    no files at", root_endpoint, e.status)
        return
    drive, items = root["parentReference"]["driveId"], []

    def walk(item_id, rel):
        for it in g.get_all(f"/drives/{drive}/items/{item_id}/children?$top=200"):
            p = f"{rel}/{it['name']}" if rel else it["name"]
            if "folder" in it:
                items.append({"path": p, "folder": True})
                walk(it["id"], p)
            elif "file" in it:
                dst = os.path.join(local_dir, "data", p)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # already downloaded completely in an earlier run: don't fetch it again
                if not (os.path.exists(dst) and os.path.getsize(dst) == it.get("size")):
                    try:
                        download_file(g, drive, it, dst)
                    except (GraphError, OSError) as e:
                        problem(f"file {p}", e)
                        continue  # left out of the manifest, so import won't look for it
                fsi = it.get("fileSystemInfo") or {}
                items.append({"path": p, "created": fsi.get("createdDateTime"), "modified": fsi.get("lastModifiedDateTime")})
            # shortcut: OneNote notebooks and other non-file "packages" are skipped; export them from OneNote by hand

    walk(root["id"], "")
    save(os.path.join(local_dir, "manifest.json"), {"webUrl": root["webUrl"], "items": items})
    log("    files:", sum(1 for i in items if not i.get("folder")))


def cmd_export(cfg, out, parts):
    g = Graph(cfg["source"])
    g.require(SOURCE_ROLES)
    users = g.get_all("/users?$select=id,displayName,userPrincipalName,mail&$top=999")
    save(f"{out}/users.json", users)
    log(len(users), "users")

    teams = g.get_all("/groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')"
                      "&$select=id,displayName,description,visibility,createdDateTime&$top=999") if "teams" in parts else []
    for t in teams:
        tdir = f"{out}/teams/{t['id']}"
        if os.path.exists(f"{tdir}/team.json"):
            # messages are done; re-check files so any that failed last time are fetched (finished files are skipped)
            log("team messages already exported, checking files:", t["displayName"])
            try:
                export_team_files(g, load(f"{tdir}/team.json"), tdir)
            except (GraphError, OSError) as e:
                problem(f"files of team {t['displayName']}", e)
            continue
        log("team", t["displayName"])
        try:
            export_team(g, t, tdir, out)
        except (GraphError, OSError) as e:
            problem(f"team {t['displayName']} (run Export again to retry it)", e)

    if chat_kinds(parts):
        export_chats(g, users, out, chat_kinds(parts))

    if "onedrive" in parts:
        export_onedrives(g, users, out)
    if PROBLEMS:
        log(f"export finished with {len(PROBLEMS)} problem(s); everything else is saved. "
            "Click Start export again to retry only these (finished parts are skipped):")
        for p in PROBLEMS:
            log("  -", p)
        raise SystemExit(1)
    log("export done. Readable backup:", f"{out}/archive")


def export_chats(g, users, out, kinds=("oneOnOne", "group", "meeting")):
    """Read each chat through the people in it: Microsoft may refuse one participant's path and allow another's."""
    done, refused = set(), {}
    for u in users:
        try:
            chats = g.get_all(f"/users/{u['id']}/chats?$expand=members&$top=50")
        except GraphError as e:
            log("no chats for", u["userPrincipalName"], e.status)
            continue
        for ch in chats:
            if ch.get("chatType") not in kinds:
                continue
            sha = hashlib.sha1(ch["id"].encode()).hexdigest()
            path = f"{out}/chats/{sha}.json"
            if ch["id"] in done or os.path.exists(path):
                continue
            title = ch.get("topic") or ", ".join(m.get("displayName") or "?" for m in ch.get("members", []))
            try:
                ch["messages"] = export_messages(g, f"/users/{u['id']}/chats/{ch['id']}/messages", out, False)
            except GraphError as e:
                if e.status in (403, 404):
                    refused[ch["id"]] = f"{title} [{ch.get('chatType')}]"  # the next participant may still get in
                else:
                    problem(f"chat {title}", e)
                continue
            except OSError as e:
                problem(f"chat {title}", e)
                continue
            log("chat", title)
            done.add(ch["id"])
            refused.pop(ch["id"], None)
            write_archive(f"{out}/archive/chats/{safe(title)}_{sha[:6]}.html", title, ch["messages"])
            save(path, ch)
    with open(f"{out}/unreadable_chats.txt", "w") as f:
        f.write("".join(v + "\n" for v in sorted(refused.values())))
    log(f"chats Microsoft refused through every participant: {len(refused)} (listed in unreadable_chats.txt; use Purview for these)")


def export_team(g, t, tdir, out):
    t["settings"] = g.get(f"/teams/{t['id']}")
    t["primaryChannelId"] = g.get(f"/teams/{t['id']}/primaryChannel")["id"]
    t["members"] = g.get_all(f"/teams/{t['id']}/members")
    t["channels"] = g.get_all(f"/teams/{t['id']}/channels")
    for c in t["channels"]:
        log("  channel", c["displayName"])
        base = f"/teams/{t['id']}/channels/{c['id']}"
        if c.get("membershipType", "standard") != "standard":
            c["members"] = g.get_all(base + "/members")
        c["messages"] = export_messages(g, base + "/messages", out, True)
        write_archive(f"{out}/archive/{safe(t['displayName'])}/{safe(c['displayName'])}.html", c["displayName"], c["messages"])
    export_team_files(g, t, tdir)
    save(f"{tdir}/team.json", t)


def export_team_files(g, t, tdir):
    for c in t["channels"]:
        if c.get("membershipType", "standard") != "standard":  # private/shared channels keep files in their own site
            export_drive(g, f"/teams/{t['id']}/channels/{c['id']}/filesFolder", f"{tdir}/files/{c['id']}")
    export_drive(g, f"/groups/{t['id']}/drive/root", f"{tdir}/files/root")


def export_onedrives(g, users, out):
    # Only people who are moving: copying everyone's OneDrive can fill the disk with data nobody will import
    if not os.path.exists(f"{out}/users.csv"):
        log("OneDrive skipped: run Match users first, then Export again with OneDrive ticked (copies only matched users).")
        return
    with open(f"{out}/users.csv", newline="") as f:
        moving = {r["source_upn"].strip().lower() for r in csv.DictReader(f) if r["dest_upn"].strip()}
    for u in users:
        if u["userPrincipalName"].lower() not in moving:
            continue  # moving users are always re-checked: finished files are skipped, failed ones retried
        log("onedrive", u["userPrincipalName"])
        try:
            export_drive(g, f"/users/{u['id']}/drive/root", f"{out}/onedrive/{u['id']}")
        except (GraphError, OSError) as e:
            problem(f"OneDrive of {u['userPrincipalName']}", e)


# ---------------------------------------------------------------- user mapping

def cmd_map(cfg, out):
    src = load(f"{out}/users.json")
    if src is None:
        raise SystemExit("ERROR: run Export first. Match users needs the list of old users that Export saves.")
    g = Graph(cfg["dest"])
    g.require(["User.Read.All"])
    dst = g.get_all("/users?$select=id,displayName,userPrincipalName&$top=999")
    by_local = {d["userPrincipalName"].split("@")[0].lower(): d for d in dst}
    by_name = {d["displayName"].lower(): d for d in dst if d.get("displayName")}
    path, missing = f"{out}/users.csv", 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source_upn", "dest_upn"])
        for s in src:
            d = by_local.get(s["userPrincipalName"].split("@")[0].lower()) or by_name.get((s.get("displayName") or "").lower())
            missing += d is None
            w.writerow([s["userPrincipalName"], d["userPrincipalName"] if d else ""])
    log(f"wrote {path}. {missing} old users have no match: fill dest_upn by hand or leave empty "
        "(their messages will be posted by fallback_sender_upn with 'Originally sent by <name>').")


def load_user_map(g, out):
    src = {u["userPrincipalName"].lower(): u["id"] for u in load(f"{out}/users.json")}
    umap, bad = {}, []
    with open(f"{out}/users.csv", newline="") as f:
        for row in csv.DictReader(f):
            s, d = row["source_upn"].strip().lower(), row["dest_upn"].strip()
            if s in src and d:
                try:
                    u = g.get(f"/users/{urllib.parse.quote(d)}?$select=id,displayName")
                except GraphError:
                    bad.append(d)
                    continue
                umap[src[s]] = {"id": u["id"], "displayName": u["displayName"]}
    if bad:  # stop before writing anything rather than post these people's messages under the fallback sender
        raise SystemExit("ERROR: these new users in the matching table don't exist in the NEW tenant: "
                         + ", ".join(bad) + ". Fix them in User matching and save.")
    return umap


# ---------------------------------------------------------------- import (new tenant)

def member_ref(u, role=None):
    return {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": [role] if role else [],
            "user@odata.bind": f"{GRAPH}/users('{u['id']}')"}


def quote_path(p):
    return "/".join(urllib.parse.quote(s) for s in p.split("/"))


class Importer:
    def __init__(self, cfg, out):
        self.g, self.out = Graph(cfg["dest"]), out
        self.g.require(DEST_ROLES)
        self.state_path = f"{out}/import_state.json"
        self.state = load(self.state_path, {})
        # progress is one appended line per item: rewriting a 100k-key file per message would take hours
        self.journal_path = self.state_path + ".journal"
        if os.path.exists(self.journal_path):
            with open(self.journal_path) as f:
                self.state.update(json.loads(line) for line in f if line.endswith("\n"))  # a half-written last line is ignored
            save(self.state_path, self.state)
        self.journal = open(self.journal_path, "w")
        self.umap = load_user_map(self.g, out)
        try:
            u = self.g.get(f"/users/{urllib.parse.quote(cfg['fallback_sender_upn'])}?$select=id,displayName")
        except GraphError:
            raise SystemExit(f"ERROR: the fallback sender '{cfg['fallback_sender_upn']}' does not exist in the NEW tenant. "
                             "In Settings, use a new-tenant address (e.g. admin@newcompany.com).")
        self.fallback = {"id": u["id"], "displayName": u["displayName"]}
        # a licensed person who is staying: owns teams with no moving owner and stores voice/video, which must outlive leavers' licenses
        m = self.g.get(f"/users/{urllib.parse.quote(cfg.get('media_owner_upn') or cfg['fallback_sender_upn'])}?$select=id,displayName")
        self.owner = {"id": m["id"], "displayName": m["displayName"]}
        self.media_root = None
        self.url_map = []
        self.failed = []  # problems are collected and reported at the end instead of stopping the whole run

    def fail(self, what, err):
        self.failed.append(f"{what}: {str(err)[:300]}")
        log("  PROBLEM", what, "-", str(err)[:300])

    def mark(self, key, val=True):
        with MARK_LOCK:
            self.state[key] = val
            self.journal.write(json.dumps([key, val]) + "\n")
            self.journal.flush()
            os.fsync(self.journal.fileno())  # a lost mark would post that message twice on the next run

    def upload_drive(self, local_dir, dest):
        man = load(os.path.join(local_dir, "manifest.json"))
        if not man:
            return
        self.url_map.append((man["webUrl"], dest["webUrl"]))  # so old file links in messages point at the copies
        drive, parent = dest["parentReference"]["driveId"], dest["id"]

        def one(e):
            key = f"file|{local_dir}|{e['path']}"
            if self.state.get(key):
                return
            try:
                self.upload_item(local_dir, drive, parent, e)
            except Exception as x:
                self.fail(f"file {e['path']}", x)
                return
            self.mark(key)

        folders = [e for e in man["items"] if e.get("folder")]
        for depth in sorted({e["path"].count("/") for e in folders}):  # level by level: parents must exist before their contents
            run_parallel(one, [e for e in folders if e["path"].count("/") == depth])
        run_parallel(one, [e for e in man["items"] if not e.get("folder")])

    def upload_item(self, local_dir, drive, parent, e):
        if e.get("folder"):
            head, _, name = e["path"].rpartition("/")
            url = f"/drives/{drive}/items/{parent}" + (f":/{quote_path(head)}:" if head else "") + "/children"
            try:
                self.g.post(url, {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
            except GraphError as x:
                if x.status != 409:  # 409 = folder already exists
                    raise
            return
        src = os.path.join(local_dir, "data", e["path"])
        item, size = f"/drives/{drive}/items/{parent}:/{quote_path(e['path'])}:", os.path.getsize(src)
        if size == 0:
            self.g.req("PUT", item + "/content", data=b"")
            return
        fsi = {k: v for k, v in (("createdDateTime", e.get("created")), ("lastModifiedDateTime", e.get("modified"))) if v}
        self.upload_file(src, item, size, fsi)

    def upload_file(self, src, item, size, fsi):
        sess = self.g.post(item + "/createUploadSession",
                           {"item": {"@microsoft.graph.conflictBehavior": "replace", "fileSystemInfo": fsi}})
        with open(src, "rb") as f:
            pos = 0
            while pos < size:
                chunk = f.read(CHUNK)
                self.g.req("PUT", sess["uploadUrl"], data=chunk, auth=False,
                           headers={"Content-Range": f"bytes {pos}-{pos + len(chunk) - 1}/{size}"})
                pos += len(chunk)

    def media_link(self, hc, who):
        """Uploads one voice note, video or large image; returns a link anyone in the organization can open."""
        key = "media|" + hc["file"]
        if not self.state.get(key):
            if not self.media_root:
                self.media_root = self.g.get(f"/users/{self.owner['id']}/drive/root")
            drive, src = self.media_root["parentReference"]["driveId"], os.path.join(self.out, hc["file"])
            path = quote_path(f"Teams voice and video/{safe(who)}/{os.path.basename(hc['file'])}")
            item = f"/drives/{drive}/root:/{path}:"
            self.upload_file(src, item, os.path.getsize(src), {})
            link = self.g.post(f"/drives/{drive}/items/{self.g.get(item)['id']}/createLink", {"type": "view", "scope": "organization"})
            self.mark(key, link["link"]["webUrl"])
        return self.state[key]

    def add_links(self, m):
        m["_links"] = []
        for hc in split_media(m, self.out)[1]:
            try:
                m["_links"].append((hc["contentType"].split("/")[0], self.media_link(hc, sender(m)[1])))
            except (GraphError, OSError, KeyError) as e:
                self.fail(f"media for message {m.get('id')}", e)

    def copy_linked(self, src, url, owners, users):
        """Give one old-tenant file link a home in the new tenant; returns an organization link, or "" if gone."""
        key = "link|" + html.unescape(url)
        if key in self.state:
            return
        raw = html.unescape(url)
        try:
            it = src.get("/shares/u!" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=") + "/driveItem")
        except GraphError as e:
            it, err = None, e
            m = re.match(r"/personal/([^/]+)/Documents/(.+)", urllib.parse.urlparse(urllib.parse.unquote(raw.split("?")[0])).path)
            u = users.get(m.group(1).lower()) if m else None
            if u:
                try:
                    it = src.get(f"/users/{u['id']}/drive/root:/{urllib.parse.quote(m.group(2))}")
                except GraphError as e2:
                    err = e2
            if not it:
                if err.status in (400, 403, 404, 423):  # gone, refused or locked: final for this run
                    self.mark(key, "")
                return
        drive = it["parentReference"]["driveId"]
        if drive not in owners:
            o = ((src.get(f"/drives/{drive}?$select=owner").get("owner") or {}).get("user") or {})
            owners[drive] = (o.get("id"), o.get("displayName") or "Shared files")
        oid, oname = owners[drive]
        rel = (it["parentReference"].get("path") or "").split("root:", 1)[-1].strip("/")
        rel = f"{rel}/{it['name']}" if rel else it["name"]
        target = None
        if oid in self.umap:  # already copied with its owner's OneDrive
            try:
                target = self.g.get(f"/users/{self.umap[oid]['id']}/drive/root:/{quote_path(rel)}")
            except GraphError:
                target = None
        if not target and "folder" in it:
            self.mark(key, "")
            return
        if not target:
            if not self.media_root:
                self.media_root = self.g.get(f"/users/{self.owner['id']}/drive/root")
            ddrive = self.media_root["parentReference"]["driveId"]
            dst = os.path.join(self.out, "linked", hashlib.sha1(raw.encode()).hexdigest())
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not (os.path.exists(dst) and os.path.getsize(dst) == it.get("size")):
                download_file(src, drive, it, dst)
            item = f"/drives/{ddrive}/root:/{quote_path(f'Files from chats/{safe(oname)}/{rel}')}:"
            fsi = {k: v for k, v in (it.get("fileSystemInfo") or {}).items() if k in ("createdDateTime", "lastModifiedDateTime")}
            size = os.path.getsize(dst)
            if size:
                self.upload_file(dst, item, size, fsi)
            else:
                self.g.req("PUT", item + "/content", data=b"")
            target = self.g.get(item)
        link = self.g.post(f"/drives/{target['parentReference']['driveId']}/items/{target['id']}/createLink",
                           {"type": "view", "scope": "organization"})
        self.mark(key, link["link"]["webUrl"])

    def open_history(self, cid):
        """Members of a new group chat only see messages from today; re-adding them with full history is the only fix."""
        for m in self.g.get(f"/chats/{cid}?$expand=members")["members"]:
            if (m.get("visibleHistoryStartDateTime") or "")[:4] == "0001":
                continue
            body = {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": m.get("roles") or ["owner"],
                    "user@odata.bind": f"{GRAPH}/users('{m['userId']}')", "visibleHistoryStartDateTime": "0001-01-01T00:00:00Z"}
            try:
                self.g.req("DELETE", f"/chats/{cid}/members/{m['id']}")
            except GraphError as e:
                if e.status != 404:
                    raise
            self.g.req("POST", f"/chats/{cid}/members", body=body)

    def add_recordings(self, cid, ch):
        """One-to-one chats can't be rebuilt (Teams hands back the deleted chat), so recordings are added in place."""
        recs = [r for r in recording_messages(ch, self.state) if not self.state.get(f"chat|{ch['id']}|{r['id']}")]
        if not recs:
            return
        created = parse_ts(self.g.get(f"/chats/{cid}")["createdDateTime"])
        try:
            self.g.post(f"/chats/{cid}/startMigration", {"conversationCreationDateTime": fmt_ts(created - timedelta(milliseconds=1))})
        except GraphError as e:
            if e.status != 400:
                raise
        try:
            for r in recs:
                pid = post_msg(self.g, f"/chats/{cid}/messages",
                               build_message(r, self.umap, self.fallback, self.out, r["createdDateTime"], self.url_map))
                self.mark(f"chat|{ch['id']}|{r['id']}", pid)
        finally:
            self.g.post(f"/chats/{cid}/completeMigration")

    def forget_chat(self, old_id):
        for k in [k for k in self.state if k == "chat|" + old_id or k.startswith(f"chat|{old_id}|")]:
            self.mark(k, False)

    def link_pairs(self):
        pairs = [(v, new) for k, new in self.state.items() if k.startswith("link|") and new for v in link_variants(k[5:])]
        return sorted(pairs, key=lambda p: -len(p[0]))  # a folder's address is a prefix of its files' addresses

    def import_thread(self, key, base, msgs, created_hint):
        """Replay messages into a channel (base=/teams/x/channels/y) or chat (base=/chats/z) in migration mode."""
        msgs = sorted([m for m in msgs if is_real(m) or any(is_real(r) for r in m.get("replies") or [])],
                      key=lambda m: m["createdDateTime"])
        if not msgs or self.state.get(key + "|completed"):
            return
        if not self.state.get(key + "|started"):
            start = parse_ts(msgs[0]["createdDateTime"])
            if created_hint:
                start = min(start, parse_ts(created_hint))
            try:
                self.g.post(base + "/startMigration", {"conversationCreationDateTime": fmt_ts(start - timedelta(seconds=1))})
            except GraphError as e:
                if e.status != 400:
                    raise
                log("    startMigration:", str(e)[:200])  # usually "already in migration mode" on a re-run
            self.mark(key + "|started")
        used, n, bad = set(), 0, 0
        for m in msgs:
            ts = unique_ts(used, m["createdDateTime"])
            pid = self.state.get(f"{key}|{m['id']}")
            if not pid:
                self.add_links(m)
                try:
                    pid = post_msg(self.g, base + "/messages", build_message(m, self.umap, self.fallback, self.out, ts, self.url_map))
                except (GraphError, OSError) as e:
                    self.fail(f"message {m['id']} in {key}", e)
                    bad += 1
                    continue  # its replies have nothing to attach to; the next run retries them together
                self.mark(f"{key}|{m['id']}", pid)
                n += 1
                time.sleep(0.2)  # Teams allows 5 imported messages per second per conversation
            for r in sorted([r for r in m.get("replies") or [] if is_real(r)], key=lambda r: r["createdDateTime"]):
                rts = unique_ts(used, r["createdDateTime"])
                if self.state.get(f"{key}|{r['id']}"):
                    continue
                self.add_links(r)
                try:
                    rid = post_msg(self.g, f"{base}/messages/{pid}/replies",
                                   build_message(r, self.umap, self.fallback, self.out, rts, self.url_map))
                except (GraphError, OSError) as e:
                    self.fail(f"reply {r['id']} in {key}", e)
                    bad += 1
                    continue
                self.mark(f"{key}|{r['id']}", rid)
                n += 1
                time.sleep(0.2)
        try:
            self.g.post(base + "/completeMigration")
        except GraphError as e:  # a retried call whose first try did finish: the chat is already complete
            if e.status != 400 or "not in migration mode" not in str(e):
                raise
        if bad:  # Teams allows a finished conversation to re-enter migration, so the next run can retry just these
            self.mark(key + "|started", False)
            log(f"    {n} messages imported, {bad} failed")
            return
        self.mark(key + "|completed")
        log(f"    {n} messages imported")

    def create_channel(self, tid, body):
        r = self.g.req("POST", f"/teams/{tid}/channels", body=body)[2]
        if isinstance(r, dict) and r.get("id"):
            return r["id"]
        for _ in range(60):  # shared channels are created in the background: 202 and no body
            same = [x for x in self.g.get_all(f"/teams/{tid}/channels") if x["displayName"].lower() == body["displayName"].lower()]
            if same:
                return same[0]["id"]
            time.sleep(5)
        raise RuntimeError(f"channel {body['displayName']} was not created in time")

    def import_team(self, t):
        g, tdir = self.g, f"{self.out}/teams/{t['id']}"
        log("team", t["displayName"])
        owners = [self.umap[m["userId"]] for m in t["members"]
                  if "owner" in (m.get("roles") or []) and m.get("userId") in self.umap] or [self.owner]
        tid = self.state.get("team|" + t["id"])
        if not tid:
            body = {"template@odata.bind": f"{GRAPH}/teamsTemplates('standard')", "displayName": t["displayName"],
                    "description": t.get("description") or t["displayName"],
                    "visibility": "public" if (t.get("visibility") or "").lower() == "public" else "private",
                    "members": [member_ref(owners[0], "owner")]}
            loc = g.req("POST", "/teams", body)[1]["Location"]
            for _ in range(120):
                op = g.get(loc)
                if op["status"] == "succeeded":
                    break
                if op["status"] == "failed":
                    raise RuntimeError(f"team creation failed: {op}")
                time.sleep(5)
            else:
                raise RuntimeError("team creation timed out")
            tid = op["targetResourceId"]
            self.mark("team|" + t["id"], tid)  # save at once so a re-run never creates the team twice
            time.sleep(15)  # shortcut: new teams take a moment to accept channel calls; raise if you see 404s
            settings = {k: t["settings"][k] for k in ("memberSettings", "guestSettings", "messagingSettings", "funSettings")
                        if t.get("settings", {}).get(k)}
            if settings:
                try:
                    g.req("PATCH", f"/teams/{tid}", settings)
                except GraphError as e:
                    self.fail(f"team settings {t['displayName']}", e)

        dest_ch = {}
        for c in t["channels"]:
            cid = self.state.get("chan|" + c["id"])
            if not cid:
                if c["id"] == t["primaryChannelId"]:
                    cid = g.get(f"/teams/{tid}/primaryChannel")["id"]
                else:
                    body = {"displayName": c["displayName"], "membershipType": c.get("membershipType", "standard")}
                    if c.get("description"):
                        body["description"] = c["description"]
                    if body["membershipType"] != "standard":
                        body["members"] = [member_ref(owners[0], "owner")]
                    try:
                        cid = self.create_channel(tid, body)
                    except GraphError as e:
                        # e.g. created in an interrupted earlier run: reuse the channel with the same name
                        same = [x for x in g.get_all(f"/teams/{tid}/channels")
                                if x["displayName"].lower() == c["displayName"].lower()]
                        if not same:
                            self.fail(f"channel {t['displayName']} / {c['displayName']}", e)
                            continue
                        cid = same[0]["id"]
                self.mark("chan|" + c["id"], cid)
            dest_ch[c["id"]] = cid
            folder = None
            for _ in range(24):  # makes Teams create the channel's Files folder; shared channels can take minutes
                try:
                    folder = g.get(f"/teams/{tid}/channels/{cid}/filesFolder")
                    break
                except GraphError as e:
                    if c.get("membershipType", "standard") == "standard":
                        log("  files folder not ready for", c["displayName"], e.status)
                        break
                    time.sleep(10)
            else:
                self.fail(f"files of channel {t['displayName']} / {c['displayName']}", "files folder never became ready")
            if folder and c.get("membershipType", "standard") != "standard":
                self.upload_drive(f"{tdir}/files/{c['id']}", folder)

        self.upload_drive(f"{tdir}/files/root", g.get(f"/groups/{tid}/drive/root"))

        for c in t["channels"]:
            if c["id"] not in dest_ch:
                continue
            log("  channel", c["displayName"])
            try:
                self.import_thread("chan|" + c["id"], f"/teams/{tid}/channels/{dest_ch[c['id']]}", c["messages"], c.get("createdDateTime"))
            except (GraphError, OSError) as e:
                self.fail(f"messages {t['displayName']} / {c['displayName']}", e)

        # members go in last: Teams blocks adding members while a channel is in migration mode
        for m in t["members"]:
            self.add_member(f"/teams/{tid}/members", m)
        for c in t["channels"]:
            for m in c.get("members") or [] if c["id"] in dest_ch else []:
                self.add_member(f"/teams/{tid}/channels/{dest_ch[c['id']]}/members", m)

    def add_member(self, url, m):
        d = self.umap.get(m.get("userId"))
        if not d or self.state.get(f"member|{url}|{d['id']}"):
            return
        try:
            self.g.post(url, member_ref(d, "owner" if "owner" in (m.get("roles") or []) else None))
        except GraphError as e:
            if e.status not in (400, 409):  # 400/409 = already a member
                self.fail(f"add {d['displayName']} to {url}", e)
                return
        self.mark(f"member|{url}|{d['id']}")

    def import_chat(self, ch):
        title = ch.get("topic") or ", ".join(m.get("displayName") or "?" for m in ch.get("members", []))
        if not any(is_real(m) for m in ch.get("messages") or []):
            return  # no real messages: don't create an empty chat
        cid = self.state.get("chat|" + ch["id"])
        if not cid:
            members = list({self.umap[m["userId"]]["id"]: self.umap[m["userId"]]
                            for m in ch.get("members", []) if m.get("userId") in self.umap}.values())
            if not members:  # nobody from this chat moved: keep it searchable under the two admin accounts
                members = [self.owner]
            one = ch.get("chatType") == "oneOnOne" and len(members) == 2
            everyone_moved = all(m.get("userId") in self.umap for m in ch.get("members", []))
            all_senders = all(sender(m)[0] in self.umap for m in ch["messages"] if is_real(m))
            body = {"chatType": "oneOnOne" if one else "group"}
            topic = ch.get("topic")
            if not one and (len(members) < 2 or not everyone_moved or not all_senders):
                # Teams needs two people per chat and a real account behind each message; names stay in the text
                members = list({u["id"]: u for u in members + [self.fallback]}.values())
                gone = [m.get("displayName") for m in ch.get("members", []) if m.get("userId") not in self.umap]
                names = ", ".join(x for x in gone if x)
                topic = topic or (("Chat with " + names)[:200] if names else "Archived chat")
            body["members"] = [member_ref(u, "owner") for u in members]
            if not one and topic:
                body["topic"] = re.sub(r"\s*:\s*", " - ", topic)  # Teams refuses ":" in chat names
            try:
                cid = self.g.post("/chats", body)["id"]
            except GraphError as e:
                self.fail(f"could not create chat {title}", e)
                return
            self.mark("chat|" + ch["id"], cid)
            if body["chatType"] == "group":
                try:
                    self.open_history(cid)
                except GraphError as e:
                    self.fail(f"full history for chat {title}", e)
        log("chat", title)
        try:
            self.import_thread("chat|" + ch["id"], f"/chats/{cid}", ch["messages"], ch.get("createdDateTime"))
        except (GraphError, OSError) as e:
            self.fail(f"chat {title}", e)


def old_links(out, rx):
    found = set()
    for path in glob.glob(f"{out}/chats/*.json") + glob.glob(f"{out}/teams/*/team.json"):
        doc = load(path)
        for c in doc.get("channels") or [doc]:
            for m in c.get("messages") or []:
                for x in [m] + (m.get("replies") or []):
                    for t in strings({k: x.get(k) for k in ("body", "attachments", "eventDetail")}):
                        found.update(rx.findall(t))
    return sorted({html.unescape(u) for u in found})


def onedrive_key(upn):
    """OneDrive URLs name the owner as their address with every non-alphanumeric character turned into _."""
    return re.sub(r"[^a-z0-9]", "_", upn.lower())


def cmd_relink_files(cfg, out):
    imp, src = Importer(cfg, out), Graph(cfg["source"])
    rx = old_link_re(cfg["old_sharepoint"])
    urls = old_links(out, rx)
    users = {}
    for u in load(f"{out}/users.json"):
        users[onedrive_key(u["userPrincipalName"])] = u
    owners, n = {}, [0]
    log(f"old-tenant links to give a home: {len(urls)}")

    def one(u):
        try:
            imp.copy_linked(src, u, owners, users)
        except Exception as e:
            imp.fail(f"link {u[:120]}", repr(e))
        n[0] += 1
        if n[0] % 200 == 0:
            log(f"  {n[0]}/{len(urls)} links done")
    run_parallel(one, urls)
    got = sum(1 for u in urls if imp.state.get("link|" + u))
    gone = sum(1 for u in urls if imp.state.get("link|" + u) == "")
    log(f"links copied: {got}, no longer available: {gone}, to retry: {len(urls) - got - gone}")


def cmd_rebuild_chats(cfg, out):
    """Re-import chats that pointed at the old tenant or had meeting recordings, with every link moved over."""
    global OLD_RX
    imp = Importer(cfg, out)
    OLD_RX = old_link_re(cfg["old_sharepoint"])
    imp.url_map = imp.link_pairs() + (rename_map(cfg["sharepoint_rename"]) if cfg.get("sharepoint_rename") else [])
    todo = []
    for name in sorted(os.listdir(f"{out}/chats")):
        if not name.endswith(".json"):
            continue
        ch = load(f"{out}/chats/{name}")
        msgs = ch.get("messages") or []
        has_old = any(OLD_RX.search(t) for m in msgs if is_real(m) for t in strings({k: m.get(k) for k in ("body", "attachments")}))
        recs = recording_messages(ch, imp.state)
        if has_old or recs:
            todo.append((name, bool(recs)))
    log(f"chats to rebuild: {len(todo)}")

    def one(item):
        name, _ = item
        ch = load(f"{out}/chats/{name}")
        try:
            cid = imp.state.get("chat|" + ch["id"])
            if cid and imp.g.get(f"/chats/{cid}")["chatType"] == "oneOnOne":
                imp.add_recordings(cid, ch)
                return
            if cid and not imp.state.get(f"rebuilt|{ch['id']}"):
                try:
                    imp.g.req("DELETE", f"/chats/{cid}")
                except GraphError as e:
                    if e.status != 404:
                        raise
                imp.forget_chat(ch["id"])
            ch["messages"] = (ch.get("messages") or []) + recording_messages(ch, imp.state)
            imp.mark(f"rebuilt|{ch['id']}")
            imp.import_chat(ch)
        except Exception as e:
            imp.fail(f"rebuild chat file {name}", repr(e))
    run_parallel(one, todo)
    log(f"rebuild finished with {len(imp.failed)} problem(s)")
    for f in imp.failed:
        log("  -", f)


def cmd_copy_photos(cfg, out):
    imp, src = Importer(cfg, out), Graph(cfg["source"])
    for sid, d in imp.umap.items():
        try:
            st, hd, data = src.req("GET", f"/users/{sid}/photo/$value")
        except GraphError as e:
            log("no photo for", d["displayName"], e.status)
            continue
        imp.g.req("PUT", f"/users/{d['id']}/photo/$value", data=data, headers={"Content-Type": hd.get("Content-Type") or "image/jpeg"})
        log("photo copied for", d["displayName"])


# ---- Old-tenant link clean-up ---------------------------------------------------------------------------------
# Messages already posted can't be edited, and an app may not delete them (softDelete is delegated-only, and the
# policyViolation "hide" needs an E5-level compliance license on the sender). The free route that works:
#   clean-scan    list every imported message that still shows an old-tenant address
#   clean-copy    post a clean copy right after each one (same sender, original time + 1 ms)
#   clean-delete  the sender signs in once in a browser; their originals that have a copy are deleted
# The sender needs a Teams license and, for channel messages, team membership, or Microsoft refuses the delete.

def old_link_items(imp, out):
    """Imported messages that still carry an old-tenant address: (kind, base url, new id, parent id, export ref, sender)."""
    rx = OLD_RX
    has_old = lambda x: bool(rx.search(json.dumps({k: x.get(k) for k in ("body", "attachments")})))
    items = []
    chats = {}
    for p in glob.glob(f"{out}/chats/*.json"):
        ch = load(p)
        if ch and imp.state.get("chat|" + ch["id"]):
            chats[imp.state["chat|" + ch["id"]]] = ch
    rev = {}
    for k, v in imp.state.items():
        if k.startswith("chat|") and k.count("|") == 2 and isinstance(v, str):
            _, oc, om = k.split("|")
            rev[(oc, v)] = om

    def one(cid):
        ch = chats[cid]
        try:
            live = {m["id"]: m for m in imp.g.get_all(f"/chats/{cid}/messages?$top=50") if m.get("messageType") == "message"}
        except GraphError as e:
            imp.fail(f"reading chat {cid}", e)
            return
        for nid, m in live.items():
            if has_old(m) and not m.get("deletedDateTime") and rev.get((ch["id"], nid)):
                who = (m.get("from") or {}).get("user") or {}
                items.append({"kind": "chat", "base": f"/chats/{cid}", "nid": nid, "par": None, "file": ch["id"],
                              "old": rev[(ch["id"], nid)], "sender": who.get("id"), "name": who.get("displayName")})
    run_parallel(one, list(chats))
    for p in glob.glob(f"{out}/teams/*/team.json"):
        t = load(p)
        tid = t and imp.state.get("team|" + t["id"])
        for c in (t or {}).get("channels") or []:
            ncid = imp.state.get("chan|" + c["id"])
            for m in c.get("messages") or [] if tid and ncid else []:
                root = imp.state.get(f"chan|{c['id']}|{m['id']}")
                for x in [m] + (m.get("replies") or []):
                    nid = imp.state.get(f"chan|{c['id']}|{x['id']}")
                    if root and nid and is_real(x) and has_old(x):
                        d = imp.umap.get(sender(x)[0]) or imp.fallback
                        items.append({"kind": "chan", "base": f"/teams/{tid}/channels/{ncid}", "nid": nid, "par": root,
                                      "team": t["id"], "chan": c["id"], "old": x["id"], "sender": d["id"], "name": d["displayName"]})
    return items


def cleanup_importer(cfg, out):
    global OLD_RX
    if not cfg.get("old_sharepoint"):
        raise SystemExit("ERROR: set old_sharepoint in Settings (the old tenant's SharePoint name, e.g. 'oldcompany' "
                         "for oldcompany.sharepoint.com).")
    imp = Importer(cfg, out)
    OLD_RX = old_link_re(cfg["old_sharepoint"])
    imp.url_map = imp.link_pairs() + (rename_map(cfg["sharepoint_rename"]) if cfg.get("sharepoint_rename") else [])
    return imp


def cmd_clean_scan(cfg, out):
    imp = cleanup_importer(cfg, out)
    items = old_link_items(imp, out)
    save(f"{out}/old_links.json", items)
    by = {}
    for i in items:
        by[i["name"]] = by.get(i["name"], 0) + 1
    log(f"messages still showing an old-tenant address: {len(items)} "
        f"({sum(i['kind'] == 'chat' for i in items)} in chats, {sum(i['kind'] == 'chan' for i in items)} in channels)")
    for name, n in sorted(by.items(), key=lambda x: -x[1]):
        log(f"  {n:5}  sent by {name}")
    if imp.failed:
        log("problems:\n  " + "\n  ".join(imp.failed))


def _export_message(out, i):
    if i["kind"] == "chat":
        ch = next(load(p) for p in glob.glob(f"{out}/chats/*.json") if (load(p) or {}).get("id") == i["file"])
        return next(m for m in ch["messages"] if m["id"] == i["old"])
    t = load(f"{out}/teams/{i['team']}/team.json")
    c = next(x for x in t["channels"] if x["id"] == i["chan"])
    return next(x for m in c["messages"] for x in [m] + (m.get("replies") or []) if x["id"] == i["old"])


def cmd_clean_copy(cfg, out):
    imp = cleanup_importer(cfg, out)
    items = [i for i in load(f"{out}/old_links.json", []) if not imp.state.get(f"clean|copy|{i['nid']}")]
    convs = {}
    for i in items:
        convs.setdefault(i["base"], []).append(i)
    log(f"clean copies to post: {len(items)} in {len(convs)} conversations")

    def one(base):
        created = parse_ts(imp.g.get(base)["createdDateTime"])
        try:
            imp.g.post(base + "/startMigration", {"conversationCreationDateTime": fmt_ts(created - timedelta(milliseconds=1))})
        except GraphError as e:
            if e.status != 400:  # 400 = already in migration mode after an interrupted run
                raise
        try:
            for i in convs[base]:
                m = _export_message(out, i)
                imp.add_links(m)
                ts = fmt_ts(parse_ts(m["createdDateTime"]) + timedelta(milliseconds=1))
                body = build_message(m, imp.umap, imp.fallback, out, ts, imp.url_map)
                if OLD_RX.search(json.dumps(body)):
                    raise RuntimeError(f"copy of message {i['old']} would still contain an old address")
                url = f"{base}/messages/{i['par']}/replies" if i["par"] else f"{base}/messages"
                imp.mark(f"clean|copy|{i['nid']}", post_msg(imp.g, url, body))
        except Exception as e:
            imp.fail(f"clean copies in {base}", repr(e))
        finally:
            for _ in range(5):  # completeMigration can briefly report a contradictory state; it settles within a minute
                try:
                    imp.g.post(base + "/completeMigration")
                    break
                except GraphError as e:
                    if "not in migration mode" in str(e) or "already completed" in str(e):
                        break
                    time.sleep(10)
    run_parallel(one, list(convs))
    log(f"clean copies done, {len(imp.failed)} problem(s)" + "".join("\n  " + f for f in imp.failed))


class UserGraph(Graph):
    """Graph as a signed-in person (delegated): only they may delete their own messages."""
    def __init__(self, cfg, tok):
        super().__init__(cfg)
        self._tok, self._exp = tok, time.time() + 3000

    def token(self):
        return self._tok


def browser_sign_in(cfg, scopes, port):
    """Authorization code + PKCE through a localhost redirect. Security defaults block the device-code method (AADSTS530035).
    The app needs: Authentication > Mobile and desktop applications > redirect http://localhost:<port>,
    'Allow public client flows' = Yes, and the delegated permissions in `scopes` with admin consent."""
    import http.server, secrets
    base = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0"
    redirect, verifier, state, got = f"http://localhost:{port}", secrets.token_urlsafe(64), secrets.token_urlsafe(16), {}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    scope = " ".join("https://graph.microsoft.com/" + s for s in scopes)

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Signed in. You can close this tab." if "code" in got else "Sign-in failed: " + html.escape(q.get("error_description", "")[:300])
            self.wfile.write(f"<h3 style='font-family:system-ui'>{msg}</h3>".encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer((os.environ.get("BIND", "127.0.0.1"), port), Back)  # BIND=0.0.0.0 only inside Docker
    srv.timeout = 900
    log("SIGN-IN: open this link and sign in as the person whose messages should be deleted:")
    log(base + "/authorize?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "response_type": "code", "redirect_uri": redirect, "scope": scope,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "prompt": "select_account"}))
    while not got:
        srv.handle_request()
        if not got:
            raise SystemExit("ERROR: no sign-in within 15 minutes")
    srv.server_close()
    if "code" not in got:
        raise SystemExit("ERROR: sign-in failed: " + got.get("error_description", str(got))[:300])
    try:
        with urllib.request.urlopen(base + "/token", urllib.parse.urlencode({
                "client_id": cfg["client_id"], "grant_type": "authorization_code", "code": got["code"],
                "redirect_uri": redirect, "code_verifier": verifier, "scope": scope}).encode(), timeout=60) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit("ERROR: sign-in failed: " + json.load(e).get("error_description", "").split("\r\n")[0])


def cmd_clean_delete(cfg, out, port):
    imp = cleanup_importer(cfg, out)
    ug = UserGraph(cfg["dest"], browser_sign_in(cfg["dest"], ["Chat.ReadWrite", "ChannelMessage.ReadWrite"], port))
    me = ug.get("/me?$select=id,displayName")
    log("signed in as", me["displayName"])
    mine = [i for i in load(f"{out}/old_links.json", []) if i["sender"] == me["id"]
            and imp.state.get(f"clean|copy|{i['nid']}") and not imp.state.get(f"clean|deleted|{i['nid']}")]
    log(f"originals to delete (each already has its clean copy): {len(mine)}")
    ok = 0
    for i in mine:
        url = (f"{i['base']}/messages/{i['par']}/replies/{i['nid']}" if i["par"] and i["par"] != i["nid"]
               else f"{i['base']}/messages/{i['nid']}")
        for attempt in range(26):
            try:
                ug.post(url + "/softDelete")
                imp.mark(f"clean|deleted|{i['nid']}")
                ok += 1
                break
            except GraphError as e:
                # a license assigned minutes ago, or team membership just added, takes a while to reach Teams
                if ("license" in str(e) or "AclCheckFailed" in str(e)) and attempt < 25:
                    if attempt == 0:
                        log("  Microsoft says the account has no Teams license yet, or isn't a member of the team; retrying each minute...")
                    time.sleep(60)
                    continue
                imp.fail(f"delete {i['nid']}", e)
                break
    left = sum(1 for i in load(f"{out}/old_links.json", []) if not imp.state.get(f"clean|deleted|{i['nid']}"))
    log(f"deleted {ok} of {len(mine)}; still showing an old address (other senders): {left}")
    if imp.failed:
        log("problems:\n  " + "\n  ".join(imp.failed))


def cmd_import(cfg, out, only_team=None, parts=tuple(PARTS)):
    imp = Importer(cfg, out)
    teams = [load(f"{out}/teams/{n}/team.json") for n in sorted(os.listdir(f"{out}/teams"))] if os.path.isdir(f"{out}/teams") else []
    teams = [t for t in teams if t and "teams" in parts]  # skips .DS_Store and any folder without a finished export
    if only_team:
        teams = [t for t in teams if t["displayName"].strip().lower() == only_team.strip().lower()]
        if not teams:
            raise SystemExit(f"ERROR: no exported team is called '{only_team}'.")
    for t in teams:
        try:
            imp.import_team(t)
        except Exception as e:  # one team must never stop the rest of a one-time migration
            imp.fail(f"team {t['displayName']}", repr(e))
    if not only_team:  # a test run with one team leaves OneDrive and chats for the full run
        if os.path.isdir(f"{out}/onedrive") and "onedrive" in parts:
            for uid in sorted(os.listdir(f"{out}/onedrive")):
                d = imp.umap.get(uid)
                if d:
                    log("onedrive ->", d["displayName"])
                    try:
                        imp.upload_drive(f"{out}/onedrive/{uid}", imp.g.get(f"/users/{d['id']}/drive/root"))
                    except Exception as e:  # one OneDrive must never stop the rest
                        imp.fail(f"OneDrive of {d['displayName']} (has the user opened OneDrive once?)", repr(e))

        rename = cfg.get("sharepoint_rename")
        if rename and not rename["to"]:
            log("OneDrives done. Chats are on hold until the new SharePoint name is confirmed.")
            return
        if rename:
            imp.url_map += rename_map(rename)  # last, so links already moved to this tenant get the new name too

        kinds = chat_kinds(parts)

        def one_chat(name):
            try:
                ch = load(f"{out}/chats/{name}")
                if ch and ch.get("chatType") in kinds:
                    imp.import_chat(ch)
            except Exception as e:
                imp.fail(f"chat file {name}", repr(e))

        # macOS puts .DS_Store here when the folder is opened in Finder
        run_parallel(one_chat, [n for n in sorted(os.listdir(f"{out}/chats")) if n.endswith(".json")] if os.path.isdir(f"{out}/chats") else [])
    if imp.failed:
        log(f"import finished with {len(imp.failed)} problem(s). Everything else is done. "
            "Click Import again to retry only these; finished parts are skipped:")
        for f in imp.failed:
            log("  -", f)
        raise SystemExit(1)
    log("import done, no problems" + (f" (only team '{only_team}')" if only_team else ""))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["export", "map", "import", "backfill", "relink-files", "rebuild-chats",
                                          "copy-photos", "clean-scan", "clean-copy", "clean-delete"])
    p.add_argument("--config", default="config.json")
    p.add_argument("--out", default="export")
    p.add_argument("--onedrive", action="store_true", help="export: also copy OneDrive (same as adding 'onedrive' to --parts)")
    p.add_argument("--parts", help="export/import: comma-separated parts to cover, default all of them "
                                   "(export leaves out onedrive unless asked): " + ", ".join(PARTS))
    p.add_argument("--chats-only", action="store_true", help="export: only retry chats not saved yet")
    p.add_argument("--team", help="import: only this team (by name), as a first test")
    p.add_argument("--port", type=int, default=8799, help="clean-delete: localhost port for the sign-in redirect")
    a = p.parse_args()
    cfg = load(a.config)
    if not cfg or not cfg.get("source") or not cfg.get("dest"):
        raise SystemExit(f"ERROR: no settings in {a.config}. Save them in the browser page (python3 ui.py), "
                         "or copy config.example.json to config.json and fill it in.")
    cfg["source"]["_label"], cfg["dest"]["_label"] = "OLD", "NEW"
    if a.command == "backfill":
        cmd_backfill(cfg, a.out)
    elif a.command == "relink-files":
        cmd_relink_files(cfg, a.out)
    elif a.command == "rebuild-chats":
        cmd_rebuild_chats(cfg, a.out)
    elif a.command == "copy-photos":
        cmd_copy_photos(cfg, a.out)
    elif a.command == "clean-scan":
        cmd_clean_scan(cfg, a.out)
    elif a.command == "clean-copy":
        cmd_clean_copy(cfg, a.out)
    elif a.command == "clean-delete":
        cmd_clean_delete(cfg, a.out, a.port)
    elif a.command == "export" and a.chats_only:
        g = Graph(cfg["source"])
        g.require(SOURCE_ROLES)
        export_chats(g, load(f"{a.out}/users.json"), a.out, chat_kinds(parse_parts(a.parts, PARTS)) or ("oneOnOne", "group", "meeting"))
    elif a.command == "export":
        parts = parse_parts(a.parts, [x for x in PARTS if x != "onedrive"])
        cmd_export(cfg, a.out, parts | ({"onedrive"} if a.onedrive else set()))
    elif a.command == "map":
        cmd_map(cfg, a.out)
    else:
        cmd_import(cfg, a.out, a.team, parse_parts(a.parts, PARTS))


if __name__ == "__main__":
    main()

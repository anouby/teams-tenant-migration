"""Offline self-check (no tenant needed). Run from the project folder:  python3 tests/test_teams_migrate.py"""
import base64, os, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from teams_migrate import build_message, unique_ts, parse_ts, fmt_ts, post_msg, GraphError

OLD_URL = "https://graph.microsoft.com/v1.0/chats/19:x/messages/1/hostedContents/abc/$value"
umap = {"old-alice": {"id": "new-alice", "displayName": "Alice"}}
fallback = {"id": "new-admin", "displayName": "Admin"}

out = tempfile.mkdtemp()
os.makedirs(f"{out}/hosted")
with open(f"{out}/hosted/img.png", "wb") as f:
    f.write(b"PNG")

msg = {
    "id": "1", "messageType": "message", "createdDateTime": "2023-05-01T10:00:00.1234567Z",
    "from": {"user": {"id": "old-alice", "displayName": "Alice"}},
    "body": {"contentType": "html", "content":
             '<at id="0">Alice</at> <at id="1">Bob</at> see <img src="' + OLD_URL + '"> '
             '<attachment id="a1"></attachment> https://old.sharepoint.com/sites/T/x.docx'},
    "mentions": [
        {"id": 0, "mentionText": "Alice", "mentioned": {"user": {"id": "old-alice"}}},
        {"id": 1, "mentionText": "Bob", "mentioned": {"user": {"id": "old-bob"}}},  # Bob is not in the new tenant
    ],
    "attachments": [{"contentType": "reference", "name": "x.docx", "contentUrl": "https://old.sharepoint.com/sites/T/x.docx"}],
    "reactions": [{"reactionType": "like", "createdDateTime": "2023-05-01T10:01:00Z", "user": {"user": {"id": "old-alice"}}}],
    "_hosted": [{"url": OLD_URL, "file": "hosted/img.png", "contentType": "image/png"}],
}
b = build_message(msg, umap, fallback, out, "2023-05-01T10:00:00.123Z", [("https://old.sharepoint.com/sites/T", "https://new.sharepoint.com/sites/T")])
c = b["body"]["content"]
assert b["from"]["user"]["id"] == "new-alice"
assert '<at id="0">Alice</at>' in c and "<at id=\"1\">" not in c and "Bob" in c  # unmapped mention -> plain text
assert [m["mentioned"]["user"]["id"] for m in b["mentions"]] == ["new-alice"]
assert "../hostedContents/1/$value" in c and OLD_URL not in c
assert base64.b64decode(b["hostedContents"][0]["contentBytes"]) == b"PNG"
assert "<attachment" not in c and "x.docx" in c
assert "old.sharepoint.com" not in c and "new.sharepoint.com/sites/T/x.docx" in c  # file links point at the copies
assert b["reactions"][0]["user"]["user"]["id"] == "new-alice"

# sender who isn't in the new tenant -> posted by fallback, original name kept
b2 = build_message({"messageType": "message", "from": {"user": {"id": "old-bob", "displayName": "Bob"}},
                    "body": {"contentType": "text", "content": "1 < 2"}}, umap, fallback, out, "2023-01-01T00:00:00.000Z")
assert b2["from"]["user"]["id"] == "new-admin"
assert "Originally sent by Bob" in b2["body"]["content"] and "1 &lt; 2" in b2["body"]["content"]

# deleted parent that still has replies -> placeholder, so the replies have somewhere to go
b3 = build_message({"messageType": "message", "deletedDateTime": "2023-01-02T00:00:00Z", "from": None, "body": {}},
                   umap, fallback, out, "2023-01-01T00:00:00.000Z")
assert "deleted" in b3["body"]["content"]

# timestamps: parse 7-digit fractions and offsets, and keep them unique per millisecond
assert fmt_ts(parse_ts("2023-05-01T12:00:00.1234567+02:00")) == "2023-05-01T10:00:00.123Z"
used = set()
assert [unique_ts(used, "2023-05-01T10:00:00.123Z") for _ in range(3)] == [
    "2023-05-01T10:00:00.123Z", "2023-05-01T10:00:00.124Z", "2023-05-01T10:00:00.125Z"]


# post_msg: falls back to fewer details on 400, shifts the time on 409
class FakeGraph:
    def __init__(self, errors):
        self.errors, self.sent = errors, []

    def post(self, url, body):
        self.sent.append(body)
        if self.errors:
            raise GraphError(self.errors.pop(0), "nope")
        return {"id": "new-msg"}


fg = FakeGraph([400, 400])
assert post_msg(fg, "/x", b) == "new-msg"
assert "reactions" not in fg.sent[1] and "mentions" not in fg.sent[2] and "<at " not in fg.sent[2]["body"]["content"]
fg = FakeGraph([400, 400, 400, 400])  # everything rejected -> last try is plain text, same sender and date
assert post_msg(fg, "/x", b) == "new-msg"
last = fg.sent[-1]
assert len(fg.sent) == 5 and last["body"]["contentType"] == "text" and "Alice" in last["body"]["content"]
assert last["from"]["user"]["id"] == "new-alice" and last["createdDateTime"] == b["createdDateTime"]
assert "<em>[image could not be migrated]</em>" in fg.sent[3]["body"]["content"] and "hostedContents" not in fg.sent[3]
fg = FakeGraph([400] * 5)
try:
    post_msg(fg, "/x", b)
    raise AssertionError("should give up after the plain-text try")
except GraphError:
    pass
fg = FakeGraph([409])
post_msg(fg, "/x", b2)
assert fg.sent[1]["createdDateTime"] == "2023-01-01T00:00:00.001Z"


# export_drive: expired link -> fresh link; finished file -> skipped; broken file -> noted, run continues
import json as _json
import teams_migrate as tm

src_dir = tempfile.mkdtemp()
with open(f"{src_dir}/fresh.txt", "wb") as f:
    f.write(b"hello")
fresh = "file://" + src_dir + "/fresh.txt"


class FakeDriveGraph:
    def __init__(self):
        self.refreshed = []

    def get(self, url):
        if url.endswith("/root"):
            return {"id": "root", "parentReference": {"driveId": "d"}, "webUrl": "https://old/sites/T/Docs"}
        item = url.split("/items/")[1].split("?")[0]
        self.refreshed.append(item)
        if item == "broken":
            raise GraphError(404, "gone")
        return {"@microsoft.graph.downloadUrl": fresh} if "?" not in url else {"id": item}

    def get_all(self, url):
        return [
            {"id": "expired", "name": "a.txt", "size": 5, "file": {}, "@microsoft.graph.downloadUrl": "file:///nonexistent/expired"},
            {"id": "done", "name": "b.txt", "size": 5, "file": {}, "@microsoft.graph.downloadUrl": "file:///nonexistent/never-used"},
            {"id": "broken", "name": "c.txt", "size": 5, "file": {}, "@microsoft.graph.downloadUrl": "file:///nonexistent/x"},
        ]


local = tempfile.mkdtemp()
os.makedirs(f"{local}/data")
with open(f"{local}/data/b.txt", "wb") as f:
    f.write(b"12345")  # already complete from an earlier run
tm.time.sleep = lambda s: None  # no real backoff waits in the test
fg = FakeDriveGraph()
tm.export_drive(fg, "/x/root", local)
man = _json.load(open(f"{local}/manifest.json"))
assert open(f"{local}/data/a.txt", "rb").read() == b"hello" and "expired" in fg.refreshed  # link refreshed
assert "done" not in fg.refreshed and open(f"{local}/data/b.txt", "rb").read() == b"12345"  # skipped
assert [i["path"] for i in man["items"]] == ["a.txt", "b.txt"]  # broken file left out of manifest
assert len(tm.PROBLEMS) == 1 and "c.txt" in tm.PROBLEMS[0] and not os.path.exists(f"{local}/data/c.txt.part")

# chats: refused through one participant, readable through the next -> saved once, not listed as unreadable
class FakeChatGraph:
    def __init__(self):
        self.calls = []

    def get_all(self, url):
        self.calls.append(url)
        if url.endswith("$top=50") and "/chats?" in url:
            return [{"id": "19:a", "chatType": "oneOnOne", "topic": "A"}] + ([{"id": "19:b", "chatType": "meeting", "topic": "B"}])
        if "/users/u1/chats/19:a/" in url or "19:b" in url:
            raise GraphError(403, "AclCheckFailed")
        return [{"id": "m1", "messageType": "message", "createdDateTime": "2023-01-01T00:00:00Z", "body": {"content": "hi"}}]


cout = tempfile.mkdtemp()
fg = FakeChatGraph()
tm.export_chats(fg, [{"id": "u1", "userPrincipalName": "a"}, {"id": "u2", "userPrincipalName": "b"}], cout)
assert len(os.listdir(f"{cout}/chats")) == 1 and len([c for c in fg.calls if "/19:a/" in c]) == 2
assert open(f"{cout}/unreadable_chats.txt").read() == "B [meeting]\n"
cout2 = tempfile.mkdtemp()
tm.export_chats(FakeChatGraph(), [{"id": "u1", "userPrincipalName": "a"}, {"id": "u2", "userPrincipalName": "b"}], cout2, {"meeting"})
assert not os.path.isdir(f"{cout2}/chats") or not os.listdir(f"{cout2}/chats")  # the one-to-one chat is left out
assert open(f"{cout2}/unreadable_chats.txt").read() == "B [meeting]\n"


# cards, forwards, voice notes and team-file links
import json
from teams_migrate import attach_html
q, r = attach_html({"attachments": [
    {"contentType": "application/vnd.microsoft.card.adaptive", "content": json.dumps({"body": [
        {"type": "TextBlock", "text": "RE: Meeting"}, {"type": "Container", "items": [{"type": "TextBlock", "text": "Hello <team>"}]},
        {"type": "Action.OpenUrl", "title": "Open in Teams"}, {"facts": [{"title": "From", "value": "Mo"}], "type": "FactSet"}]})},
    {"contentType": "application/vnd.microsoft.card.audio", "content": json.dumps({"duration": "PT8S"})},
    {"contentType": "application/vnd.microsoft.card.codesnippet", "content": "{}"},
    {"contentType": "reference", "name": "a b.xlsx", "contentUrl": "https://o/sites/T/Shared Documents/a b.xlsx"},
    {"contentType": "forwardedMessageReference", "content": json.dumps({
        "originalMessageContent": '<at id="0">Al</at> hi<attachment id="z"></attachment>',
        "originalMessageSender": {"user": {"id": "old-alice", "displayName": None}}})},
]}, umap)
assert "RE: Meeting" in r and "Hello &lt;team&gt;" in r and "From: Mo" in r and "Open in Teams" not in r
assert "Voice message (8s)" in r and "could not be migrated" not in r and "Shared%20Documents/a%20b.xlsx" in r
from teams_migrate import card_dates
assert card_dates("{{DATE(2023-03-26T11:09:49+00:00,SHORT)}} {{TIME(2023-03-26T11:09:49+00:00)}}") == "2023-03-26 11:09"
v = build_message({"messageType": "message", "from": {"user": {"id": "old-alice", "displayName": "A"}}, "createdDateTime": "2023-01-01T00:00:00Z",
                   "body": {"contentType": "html", "content": '<video src="https://graph.microsoft.com/x/hostedContents/a/$value" controls></video> hi'}},
                  umap, fallback, out, "2023-01-01T00:00:00.000Z")
assert "<video" not in v["body"]["content"] and "Video clip" in v["body"]["content"] and "hi" in v["body"]["content"]
from teams_migrate import md_html, email_html
assert md_html("**From:** A \\- B <a@b>\n\n **Sent:** Fri") == "<b>From:</b> A - B &lt;a@b&gt;<br><b>Sent:</b> Fri"
e = email_html(["Subj", "Mo", "<mo@x>", "To Alex", "Cc Sam", "2023-03-26 11:09", "Dear Alex,"])
assert e == "<p><b>📧 Subj</b></p><p>From: <b>Mo</b> &lt;mo@x&gt;<br>To Alex<br>Cc Sam<br>2023-03-26 11:09</p><p>Dear Alex,</p>"
assert "Forwarded from Alice" in q and "Al hi" in q and "<at" not in q and "<attachment" not in q

# videos never go in hostedContents; long messages are compacted, then split, never cut
from teams_migrate import compact_html
vv = build_message({"messageType": "message", "from": {"user": {"id": "old-alice", "displayName": "A"}}, "createdDateTime": "2023-01-01T00:00:00Z",
                    "body": {"contentType": "html", "content": '<video src="https://graph.microsoft.com/x/hostedContents/a/$value"></video>'},
                    "_hosted": [{"url": "https://graph.microsoft.com/x/hostedContents/a/$value", "file": "hosted/none.mp4", "contentType": "video/mp4"}]},
                   umap, fallback, out, "2023-01-01T00:00:00.000Z")
assert "hostedContents" not in vv and "Video clip" in vv["body"]["content"]
assert compact_html('<p style="x" class="y" dir="rtl" data-a="b">hi</p>') == '<p dir="rtl">hi</p>'


class TooBig:
    def __init__(self):
        self.sent = []

    def post(self, url, b):
        if b["body"]["contentType"] == "html" or len(b["body"]["content"]) > 20000:
            raise GraphError(413, "too large")
        self.sent.append(b)
        return {"id": str(len(self.sent))}


tb, long_text = TooBig(), "<p>" + "word " * 9000 + "</p>"
post_msg(tb, "u", {"createdDateTime": "2023-01-01T00:00:00.000Z", "from": {"user": {"id": "a"}}, "body": {"contentType": "html", "content": long_text}})
assert len(tb.sent) == 3 and sum(len(b["body"]["content"]) for b in tb.sent) >= 44990  # 45,000 chars arrive whole, in order
assert len({b["createdDateTime"] for b in tb.sent}) == 3

# every chat gets two accounts; progress survives a crash
from teams_migrate import Importer


class ChatG:
    def __init__(self):
        self.bodies = []

    def post(self, url, b=None):
        if url == "/chats":
            self.bodies.append(b)
            return {"id": "new"}
        return {"id": "m"}

    def get(self, url):
        return {"members": []}  # a new chat's members, already showing full history


d = tempfile.mkdtemp()
imp = Importer.__new__(Importer)
imp.g, imp.out, imp.umap, imp.failed, imp.url_map = ChatG(), d, dict(umap), [], []
imp.fallback, imp.owner = {"id": "admin", "displayName": "Administrator"}, {"id": "me", "displayName": "Me"}
imp.state, imp.state_path, imp.journal_path = {}, d + "/s.json", d + "/s.json.journal"
imp.journal = open(imp.journal_path, "w")
gone_msg = {"id": "1", "messageType": "message", "createdDateTime": "2023-01-01T00:00:00Z", "from": {"user": {"id": "x", "displayName": "X"}}, "body": {"content": "hi"}}
imp.import_chat({"id": "c1", "chatType": "oneOnOne", "members": [{"userId": "x", "displayName": "X"}, {"userId": "y", "displayName": "Y"}], "messages": [gone_msg]})
cb = imp.g.bodies[0]
assert cb["chatType"] == "group" and cb["topic"] == "Chat with X, Y" and len(cb["members"]) == 2 and not imp.failed
imp.journal.close()
with open(imp.journal_path) as f:
    assert any('"chat|c1"' in line for line in f)
with open(imp.journal_path, "a") as f:
    f.write('["half')  # crash in the middle of a line
state2 = dict(json.loads(line) for line in open(imp.journal_path) if line.endswith("\n"))
assert state2["chat|c1"] == "new"



class SharedG:
    def req(self, method, url, body=None):
        return 202, {}, b""

    def get_all(self, url):
        return [{"id": "19:shared", "displayName": "Bugs"}]


imp.g = SharedG()
assert imp.create_channel("t", {"displayName": "bugs"}) == "19:shared"

# a network drop during sign-in waits and retries instead of ending the run
import socket as _s, urllib.error as _ue, io as _io
_orig = tm.urllib.request.urlopen, tm.wait_for_internet, tm.time.sleep
_calls = []
def _fake(url, data=None, timeout=None):
    _calls.append(url)
    if len(_calls) == 1:
        raise _ue.URLError("nodename nor servname provided")
    return _io.BytesIO(b'{"access_token": "ok", "expires_in": 3600}')
_waited = []
tm.urllib.request.urlopen, tm.wait_for_internet, tm.time.sleep = _fake, lambda: _waited.append(1), lambda s: None
assert tm.Graph({"client_id": "a", "client_secret": "b", "tenant_id": "c"}).token() == "ok" and _waited == [1] and len(_calls) == 2
tm.urllib.request.urlopen, tm.wait_for_internet, tm.time.sleep = _orig

# parallel uploads: folders exist before files, every item marked once, journal stays valid
import threading as _t, json as _j
class UpG:
    pass
d = tempfile.mkdtemp()
pi = Importer.__new__(Importer)
pi.g, pi.out, pi.failed, pi.url_map, pi.state = UpG(), d, [], [], {}
pi.journal_path = d + "/j"
pi.journal = open(pi.journal_path, "w")
_items = [{"path": "f", "folder": {}}, {"path": "f/g", "folder": {}}] + [{"path": f"f/{n}"} for n in range(300)]
os.makedirs(d + "/drive")
with open(d + "/drive/manifest.json", "w") as f:
    _j.dump({"webUrl": "old", "items": _items}, f)
_seen, _lk = [], _t.Lock()
def _up(local, drive, parent, e):
    with _lk:
        _seen.append(e["path"])
    assert e.get("folder") or _seen[0] == "f"
pi.upload_item = _up
pi.upload_drive(d + "/drive", {"webUrl": "new", "id": "p", "parentReference": {"driveId": "x"}})
pi.journal.close()
_lines = [_j.loads(x) for x in open(pi.journal_path)]
assert not pi.failed and _seen[:2] == ["f", "f/g"] and len(_seen) == 302 and len(_lines) == 302 and len({k for k, _ in _lines}) == 302

# chat links carry the future SharePoint name; links still on the old tenant are left alone
from teams_migrate import rename_map, rewrite_urls
_m = [("https://old-my.sharepoint.com/personal/a", "https://cl-my.sharepoint.com/personal/b")] + rename_map({"from": "cl", "to": "newco"})
assert rewrite_urls("https://old-my.sharepoint.com/personal/a/x", _m) == "https://newco-my.sharepoint.com/personal/b/x"
assert rewrite_urls("https://cl.sharepoint.com/sites/t", _m) == "https://newco.sharepoint.com/sites/t"
assert rewrite_urls("https://old-my.sharepoint.com/personal/z/x", _m) == "https://old-my.sharepoint.com/personal/z/x"

# chat names with a colon are accepted by Teams once it is replaced
imp2 = Importer.__new__(Importer)
d = tempfile.mkdtemp()
imp2.g, imp2.out, imp2.umap, imp2.failed, imp2.url_map = ChatG(), d, dict(umap), [], []
imp2.fallback, imp2.owner = {"id": "admin", "displayName": "Administrator"}, {"id": "me", "displayName": "Me"}
imp2.state, imp2.journal_path = {}, d + "/j"
imp2.journal = open(imp2.journal_path, "w")
imp2.import_chat({"id": "c2", "chatType": "group", "topic": "Meeting Request: Demo", "members": [{"userId": "x", "displayName": "X"}], "messages": [gone_msg]})
assert imp2.g.bodies[0]["topic"] == "Meeting Request - Demo" and not imp2.failed

# no old-tenant address survives; known links become the new copies
from teams_migrate import old_link_re, strip_old_links, link_variants, rewrite_urls as _rw
_rx = old_link_re("oldco")
_c = '<p>see <a href="https://oldco-my.sharepoint.com/personal/a_b_com/Documents/Plan%20A.xlsx?web=1&amp;x=1">Plan A.xlsx</a> and https://oldco.sharepoint.com/sites/x/Doc.pdf and <a href="https://other.com/a">ok</a></p>'
_out = strip_old_links(_c, _rx)
assert "oldco" not in _out and "Plan A.xlsx" in _out and "Doc.pdf" in _out and 'href="https://other.com/a"' in _out
_recap = '<a href="https://teams.microsoft.com/l/meetingrecap?driveId=b%21x&amp;sitePath=https%3A%2F%2Foldco-my.sharepoint.com%2F%3Av%3A">Recap</a>'
assert strip_old_links(_recap, _rx) == "Recap"
_u = "https://oldco-my.sharepoint.com/personal/a/Documents/x y.docx?web=1&e=2"
_pairs = [(v, "https://new/x") for v in link_variants(_u)]
for _form in (_u, _u.replace(" ", "%20"), _u.replace("&", "&amp;").replace(" ", "%20")):
    assert "oldco" not in _rw(f'<a href="{_form}">x</a>', _pairs), _form

# meeting recordings become messages at their original time, linked when a copy exists
from teams_migrate import recording_messages, duration_text
assert duration_text("PT1H2M3S") == "1h 2m" and duration_text("PT4M30.5S") == "4m 30s"
_ch = {"onlineMeetingInfo": {"organizer": {"user": {"id": "o", "displayName": "Org"}}}, "messages": [
    {"id": "e1", "messageType": "systemEventMessage", "createdDateTime": "2025-01-01T10:00:00Z", "eventDetail": {
        "@odata.type": "#microsoft.graph.callRecordingEventMessageDetail", "callRecordingUrl": "https://old/r1.mp4",
        "callRecordingDisplayName": "Weekly: sync", "callRecordingDuration": "PT30M", "callRecordingStatus": "success"}},
    {"id": "e2", "messageType": "systemEventMessage", "createdDateTime": "2025-01-02T10:00:00Z", "eventDetail": {
        "@odata.type": "#microsoft.graph.callRecordingEventMessageDetail", "callRecordingUrl": "https://old/r2.mp4",
        "callRecordingStatus": "success", "initiator": {"user": {"id": "u", "displayName": "U"}}}}]}
_r = recording_messages(_ch, {"link|https://old/r1.mp4": "https://new/r1"})
assert _r[0]["createdDateTime"] == "2025-01-01T10:00:00Z" and 'href="https://new/r1"' in _r[0]["body"]["content"] and "(30m)" in _r[0]["body"]["content"]
assert _r[0]["from"]["user"]["id"] == "o" and _r[1]["from"]["user"]["id"] == "u" and "no longer available" in _r[1]["body"]["content"]
assert all(tm.is_real(x) for x in _r)


# OneDrive URLs name the owner as their whole address with . @ - turned into _, for any domain
from teams_migrate import onedrive_key
assert onedrive_key("Jane.Doe-Smith@Old-Company.co.uk") == "jane_doe_smith_old_company_co_uk"
assert onedrive_key("a.b@contoso.onmicrosoft.com") == "a_b_contoso_onmicrosoft_com"
# old-link clean-up: only imported messages that still show the old address are listed, with their sender
import json as _json, teams_migrate as _tm
from teams_migrate import old_link_items
class ScanG:
    def get_all(self, url):
        return [{"id": "n1", "messageType": "message", "from": {"user": {"id": "u1", "displayName": "Alice"}},
                 "body": {"content": '<a href="https://oldco-my.sharepoint.com/personal/a/x.docx">x</a>'}},
                {"id": "n2", "messageType": "message", "body": {"content": "clean"}},
                {"id": "n3", "messageType": "message", "deletedDateTime": "2024-01-01T00:00:00Z",
                 "body": {"content": "https://oldco.sharepoint.com/sites/s"}}]
_d = tempfile.mkdtemp(); os.makedirs(f"{_d}/chats")
with open(f"{_d}/chats/c.json", "w") as f:
    _json.dump({"id": "oc", "messages": []}, f)
_imp = Importer.__new__(Importer)
_imp.g, _imp.failed, _imp.umap, _imp.fallback = ScanG(), [], {}, {"id": "admin", "displayName": "Admin"}
_imp.state = {"chat|oc": "nc", "chat|oc|o1": "n1", "chat|oc|o2": "n2", "chat|oc|o3": "n3"}
_tm.OLD_RX = old_link_re("oldco")
_items = old_link_items(_imp, _d)
assert [(i["nid"], i["old"], i["sender"]) for i in _items] == [("n1", "o1", "u1")], _items
_tm.OLD_RX = None

# --parts: pick what a run covers
from teams_migrate import parse_parts, chat_kinds, PARTS
assert parse_parts("", PARTS) == set(PARTS)
assert parse_parts(" Teams, one-to-one ", PARTS) == {"teams", "one-to-one"}
assert chat_kinds({"teams", "one-to-one", "meeting-chats"}) == {"oneOnOne", "meeting"}
try:
    parse_parts("teams,emails", PARTS)
    raise AssertionError("unknown part accepted")
except SystemExit as e:
    assert "emails" in str(e)

print("all checks passed")

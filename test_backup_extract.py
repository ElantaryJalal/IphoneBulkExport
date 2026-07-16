"""Synthetic-backup test for backup_extract.py: builds a fake backup with a
WhatsApp jpg, a WhatsApp .thumb (must be filtered), a Files-app pdf, and a
voice memo, then extracts and checks the results."""
import hashlib, os, plistlib, shutil, sqlite3, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backup_extract

tmp = tempfile.mkdtemp(prefix="fakebackup_")
bdir = os.path.join(tmp, "00008020-TESTUDID")
os.makedirs(bdir)

SAMPLES = [
    ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
     "Message/Media/12345@s.whatsapp.net/a/b/photo.jpg", b"JPEGDATA" * 100),
    ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
     "Message/Media/12345@s.whatsapp.net/a/b/photo.thumb", b"THUMB"),
    ("AppDomainGroup-group.com.apple.FileProvider.LocalStorage",
     "File Provider Storage/Downloads/manual.pdf", b"%PDF-1.4 fake"),
    ("MediaDomain", "Media/Recordings/memo 1.m4a", b"M4A" * 50),
]

conn = sqlite3.connect(os.path.join(bdir, "Manifest.db"))
conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
             "relativePath TEXT, flags INTEGER, file BLOB)")
mtime = int(time.time()) - 86400
meta = plistlib.dumps({"$objects": [None, {"LastModified": mtime}]},
                      fmt=plistlib.FMT_BINARY)
for domain, rel, data in SAMPLES:
    fid = hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
    conn.execute("INSERT INTO Files VALUES (?,?,?,1,?)",
                 (fid, domain, rel, meta))
    os.makedirs(os.path.join(bdir, fid[:2]), exist_ok=True)
    with open(os.path.join(bdir, fid[:2], fid), "wb") as f:
        f.write(data)
conn.commit(); conn.close()

with open(os.path.join(bdir, "Manifest.plist"), "wb") as f:
    plistlib.dump({"IsEncrypted": False,
                   "Lockdown": {"DeviceName": "Test iPhone",
                                "ProductVersion": "17.5.1"}}, f)
with open(os.path.join(bdir, "Status.plist"), "wb") as f:
    plistlib.dump({"Date": "2026-07-16", "SnapshotState": "finished"}, f)

# --- exercise the API ---
info = backup_extract.backup_info(tmp)          # via parent dir
assert info["device"] == "Test iPhone", info
assert not info["encrypted"]

counts = backup_extract.count_matches(bdir, ["whatsapp", "files", "voicememos"])
assert counts == {"whatsapp": 1, "files": 1, "voicememos": 1}, counts  # .thumb filtered

dest = os.path.join(tmp, "out")
s = backup_extract.extract(bdir, dest,
                           presets=["whatsapp", "files", "voicememos"])
assert s["copied"] == 3 and s["failed"] == 0 and s["missing"] == 0, s

jpg = os.path.join(dest, "WhatsApp", "Message", "Media",
                   "12345@s.whatsapp.net", "a", "b", "photo.jpg")
pdf = os.path.join(dest, "Files", "File Provider Storage", "Downloads",
                   "manual.pdf")
memo = os.path.join(dest, "VoiceMemos", "Media", "Recordings", "memo 1.m4a")
for p in (jpg, pdf, memo):
    assert os.path.isfile(p), f"missing {p}"
assert abs(os.path.getmtime(jpg) - mtime) < 2, "mtime not restored"
assert not os.path.exists(os.path.join(dest, "WhatsApp", "Message", "Media",
                                       "12345@s.whatsapp.net", "a", "b",
                                       "photo.thumb")), ".thumb leaked"

# resume: second run must skip everything
s2 = backup_extract.extract(bdir, dest, presets=["whatsapp", "files",
                                                 "voicememos"])
assert s2["skipped"] == 3 and s2["copied"] == 0, s2

shutil.rmtree(tmp)
print("ALL EXTRACT TESTS PASSED")

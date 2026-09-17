# -*- coding: utf-8 -*-
"""Download a SharePoint/Stream (stream.aspx) video by pulling the player's encrypted DASH stream.

Requires: cloakbrowser, playwright, cryptography, ffmpeg.

Usage:
    python stream_grab.py --url "<stream.aspx url>" --out "D:/video.mp4" \
        --profile "C:/Users/me/.agent-browser/profiles/cloak-sharepoint" \
        --ffmpeg "C:/tools/ffmpeg/bin/ffmpeg.exe"

A visible browser window opens. Log in manually (MSA + MFA); the script detects the
FedAuth cookie and continues on its own.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cloakbrowser import launch_context, launch_persistent_context

NS = {"m": "urn:mpeg:DASH:schema:MPD:2011", "sea": "urn:mpeg:dash:schema:sea:2012"}
BOXES = {b"moof", b"styp", b"sidx", b"mdat", b"free", b"ftyp", b"moov", b"emsg"}
CHUNK = 20
CONC = 6

GRAB_JS = """
() => {
  window.__grab = async (urls, conc) => {
    const out = new Array(urls.length).fill('');
    const errors = [];
    let next = 0;
    const b64 = (u8) => {
      let s = '';
      const CH = 0x8000;
      for (let i = 0; i < u8.length; i += CH) s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
      return btoa(s);
    };
    const worker = async () => {
      while (true) {
        const idx = next++;
        if (idx >= urls.length) return;
        for (let a = 0; a < 4; a++) {
          try {
            const r = await fetch(urls[idx], {credentials: 'include', cache: 'no-store'});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            out[idx] = b64(new Uint8Array(await r.arrayBuffer()));
            break;
          } catch (e) {
            if (a === 3) { errors.push(String(e)); out[idx] = ''; }
            else await new Promise(s => setTimeout(s, 400 * (a + 1)));
          }
        }
      }
    };
    await Promise.all(Array.from({length: conc}, worker));
    return {segs: out, errors: errors};
  };
}
"""


def log(msg):
    print(time.strftime("%H:%M:%S ") + str(msg), flush=True)


def cbc_dec(key, iv, data):
    return Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(data)


def add_iv(iv, n):
    return ((int.from_bytes(iv, "big") + n) % (1 << 128)).to_bytes(16, "big")


def box_end(b, start=0):
    """Offset just past the last complete top-level ISO-BMFF box (strips PKCS#7 padding)."""
    off, last, n = start, start, len(b)
    while off + 8 <= n:
        size = int.from_bytes(b[off:off + 4], "big")
        if size == 1:
            if off + 16 > n:
                break
            size = int.from_bytes(b[off + 8:off + 16], "big")
        if size < 8 or off + size > n:
            break
        off += size
        last = off
    return last


def valid_head(b):
    if len(b) < 8:
        return False
    size = int.from_bytes(b[:4], "big")
    return 8 <= size <= 100_000_000 and b[4:8] in BOXES


def decrypt(key, iv, data, idx):
    for cand in (0, idx, idx + 1, 1):
        pt = cbc_dec(key, add_iv(iv, cand), data)
        if valid_head(pt):
            return pt
    return cbc_dec(key, iv, data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="stream.aspx page URL")
    ap.add_argument("--out", required=True, help="output .mp4 path")
    ap.add_argument("--profile", default=os.path.expanduser("~/.agent-browser/profiles/cloak-sharepoint"))
    ap.add_argument("--state", help="optional storage_state json (skips manual login if still valid)")
    ap.add_argument("--work", help="scratch dir (default: <out dir>/_stream)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--login-wait", type=int, default=1500, help="seconds to wait for manual login")
    args = ap.parse_args()

    out_dir = os.path.dirname(os.path.abspath(args.out))
    work = args.work or os.path.join(out_dir, "_stream")
    parts = os.path.join(work, "parts")
    os.makedirs(parts, exist_ok=True)

    if args.state and os.path.exists(args.state):
        log("launching with saved session state")
        ctx = launch_context(storage_state=args.state, headless=False, viewport=None)
    else:
        log("launching persistent profile: %s" % args.profile)
        ctx = launch_persistent_context(args.profile, headless=False, viewport=None)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    cap = {"token": None, "manifest": None}

    def on_req(r):
        t = r.headers.get("x-spopactoken")
        if t and not cap["token"]:
            cap["token"] = t
        if "videomanifest" in r.url and not cap["manifest"]:
            cap["manifest"] = r.url

    page.on("request", on_req)
    page.goto(args.url, wait_until="domcontentloaded", timeout=120000)

    deadline = time.time() + args.login_wait
    while time.time() < deadline:
        if "watchgas" not in page.url and "sharepoint.com" in page.url:
            pass
        if any(c["name"] == "FedAuth" for c in ctx.cookies()) and "sharepoint.com" in page.url \
                and "login.microsoftonline" not in page.url:
            break
        time.sleep(5)
    log("logged in, page=%s" % page.url[:120])

    # start playback so the player negotiates a stream
    try:
        page.evaluate("() => { const v=document.querySelector('video'); if(v){v.muted=true;v.play().catch(()=>{});} }")
    except Exception:
        pass
    for _ in range(30):
        if cap["token"] and cap["manifest"]:
            break
        time.sleep(3)
    if not (cap["token"] and cap["manifest"]):
        log("could not capture x-spopactoken / videomanifest"); ctx.close(); sys.exit(2)

    hdrs = {"x-spopactoken": cap["token"], "Referer": "https://%s/" % page.url.split("/")[2]}
    mpd = ctx.request.get(cap["manifest"], headers=hdrs, timeout=120000).text()
    root = ET.fromstring(mpd)
    base = root.find("m:BaseURL", NS).text.strip()

    tracks, kuri = {}, None
    for aset in root.iter("{urn:mpeg:DASH:schema:MPD:2011}AdaptationSet"):
        ct = aset.get("contentType")
        st = aset.find("m:SegmentTemplate", NS)
        if st is None:
            continue
        rep = aset.find("m:Representation", NS)
        rid = rep.get("id") if rep is not None else "vcopy"
        iv = None
        cp = aset.find("m:ContentProtection", NS)
        if cp is not None:
            cper = cp.find("sea:CryptoPeriod", NS)
            if cper is not None:
                iv = cper.get("IV")
                if kuri is None:
                    kuri = cper.get("keyUriTemplate").replace("&amp;", "&")
        times, t = [], 0
        for s in st.find("m:SegmentTimeline", NS).findall("m:S", NS):
            d = int(s.get("d"))
            for _ in range(int(s.get("r") or 0) + 1):
                times.append(t)
                t += d
        tracks[ct] = {"rid": rid,
                      "init": st.get("initialization").replace("&amp;", "&"),
                      "media": st.get("media").replace("&amp;", "&"),
                      "times": times,
                      "iv": bytes.fromhex(iv[2:]) if iv else None}
        log("[%s] rid=%s segments=%d" % (ct, rid, len(times)))

    key = None
    if kuri:
        key = ctx.request.get(kuri, headers=hdrs, timeout=120000).body()
        log("aes key: %s (%d bytes)" % (key.hex(), len(key)))
        if len(key) not in (16, 24, 32):
            log("unexpected key size"); ctx.close(); sys.exit(2)

    page.evaluate(GRAB_JS)

    def grab(urls):
        for _ in range(3):
            try:
                return page.evaluate("([u,c]) => window.__grab(u,c)", [urls, CONC])
            except Exception as e:
                log("grab err: %r" % (e,))
                time.sleep(2)
        return None

    for ct, tr in tracks.items():
        n = len(tr["times"])
        urls = [base + tr["media"].replace("$RepresentationID$", tr["rid"]).replace("$Time$", str(t))
                for t in tr["times"]]
        t0 = time.time()
        for ci in range(0, n, CHUNK):
            pi = ci // CHUNK
            part = os.path.join(parts, "%s_%03d.bin" % (ct, pi))
            if os.path.exists(part):
                continue
            res = grab(urls[ci:ci + CHUNK])
            if res is None:
                log("chunk %d failed" % pi); continue
            blob = b"".join(base64.b64decode(s) if s else b"" for s in res["segs"])
            open(part, "wb").write(blob)
            json.dump([len(base64.b64decode(s)) if s else -1 for s in res["segs"]],
                      open(os.path.join(parts, "%s_%03d.lens" % (ct, pi)), "w"))
            log("  %s chunk %d done (%.0fs, empty=%d)" % (ct, pi, time.time() - t0,
                                                          sum(1 for s in res["segs"] if not s)))
        p = os.path.join(parts, ct + "_init.bin")
        if not os.path.exists(p):
            r = grab([base + tr["init"].replace("$RepresentationID$", tr["rid"])])
            if r and r["segs"][0]:
                open(p, "wb").write(base64.b64decode(r["segs"][0]))

    ctx.close()

    for ct, tr in tracks.items():
        raw = os.path.join(work, ct + ".mp4")
        init_pt = decrypt(key, tr["iv"], open(os.path.join(parts, ct + "_init.bin"), "rb").read(), 0)
        open(raw, "wb").write(init_pt[:box_end(init_pt)])
        with open(raw, "ab") as out:
            ci, gidx = 0, 0
            while os.path.exists(os.path.join(parts, "%s_%03d.bin" % (ct, ci))):
                data = open(os.path.join(parts, "%s_%03d.bin" % (ct, ci)), "rb").read()
                lens = json.load(open(os.path.join(parts, "%s_%03d.lens" % (ct, ci))))
                off = 0
                for ln in lens:
                    if ln <= 0:
                        gidx += 1; continue
                    pt = decrypt(key, tr["iv"], data[off:off + ln], gidx)
                    off += ln
                    out.write(pt[:box_end(pt)])
                    gidx += 1
                ci += 1
        log("%s assembled: %d segments, %.1f MB" % (ct, gidx, os.path.getsize(raw) / 1048576))

    tmp = os.path.join(work, "merged.mp4")
    cmd = [args.ffmpeg, "-y"]
    for ct in tracks:
        cmd += ["-f", "mov", "-i", os.path.join(work, ct + ".mp4")]
    cmd += ["-c", "copy", "-movflags", "+faststart", tmp]
    p = subprocess.run(cmd, capture_output=True, text=True)
    log("ffmpeg rc=%d" % p.returncode)
    if p.returncode != 0:
        log(p.stderr[-2000:]); sys.exit(3)
    shutil.copyfile(tmp, args.out)
    log("DONE: %s (%.1f MB)" % (args.out, os.path.getsize(args.out) / 1048576))
    pr = subprocess.run([args.ffprobe, "-v", "error", "-show_entries",
                         "format=duration,size:stream=codec_name,width,height", "-of", "json", args.out],
                        capture_output=True, text=True)
    log(pr.stdout.strip())


if __name__ == "__main__":
    main()

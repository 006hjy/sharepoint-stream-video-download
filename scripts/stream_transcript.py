# -*- coding: utf-8 -*-
"""Scrape the Stream player's rendered transcript pane (virtualized list) into txt/md/vtt/json.

The cdnmedia/transcripts API returns an encrypted payload, so the rendered DOM is the
reliable source: each cue is a div[data-automationid="ListCell"][data-list-index="N"].

Usage:
    python stream_transcript.py --url "<stream.aspx url>" --outdir "D:/out" \
        --profile "C:/Users/me/.agent-browser/profiles/cloak-sharepoint" --name "my-video"

The transcript pane is opened automatically (aria-label 转录 / Transcri).
"""
import argparse
import json
import os
import re
import time

from cloakbrowser import launch_context, launch_persistent_context

INIT_JS = """
() => {
  const c = [];
  document.querySelectorAll('*').forEach(e => {
    const s = getComputedStyle(e);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 20) c.push(e);
  });
  if (!c.length) return null;
  c.sort((a,b) => b.scrollHeight - a.scrollHeight);
  window.__sc = c[0];
  window.__sc.scrollTop = 0;
  return {sh: window.__sc.scrollHeight, ch: window.__sc.clientHeight};
}
"""

STEP_JS = """
() => {
  const sc = window.__sc;
  if (!sc) return null;
  sc.scrollTop = sc.scrollTop + Math.floor(sc.clientHeight * 0.5);
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('[data-automationid="ListCell"][data-list-index]').forEach(cell => {
    const idx = parseInt(cell.getAttribute('data-list-index'), 10);
    const sp = cell.querySelector('[class*="itemDisplayName"]');
    const ev = cell.querySelector('[class*="eventSpeakerName"]');
    const ts = cell.querySelector('[id^="Header-timestamp"]');
    const aria = cell.querySelector('[id^="timestampSpeakerAriaLabel"]');
    const sub = cell.querySelector('[id^="sub-entry"]');
    let text = sub ? sub.innerText : cell.innerText;
    const evName = ev ? ev.innerText.trim() : '';
    if (evName && text.indexOf(evName) === 0) text = text.slice(evName.length);
    out.push({i: idx, speaker: sp ? sp.innerText.trim() : evName,
              clock: ts ? ts.innerText.trim() : '',
              aria: aria ? aria.innerText.trim() : '', text: clean(text)});
  });
  return {top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight, items: out};
}
"""


def parse_seconds(clock, aria):
    if clock:
        p = [int(x) for x in clock.split(":") if x.strip().isdigit()]
        if len(p) == 3:
            return p[0] * 3600 + p[1] * 60 + p[2]
        if len(p) == 2:
            return p[0] * 60 + p[1]
        if len(p) == 1:
            return p[0]
    if aria:
        h = re.search(r"(\d+)\s*小时", aria)
        m = re.search(r"(\d+)\s*分钟", aria)
        s = re.search(r"(\d+)\s*秒", aria)
        if h or m or s:
            return (int(h.group(1)) if h else 0) * 3600 + (int(m.group(1)) if m else 0) * 60 + (int(s.group(1)) if s else 0)
    return None


def speaker_from_aria(aria):
    if not aria:
        return ""
    for pat in (r"\s*\d+\s*小时.*$", r"\s*\d+\s*分钟.*$", r"\s*\d+\s*秒.*$"):
        aria = re.sub(pat, "", aria)
    return aria.strip()


def hhmmss(s):
    return "--:--:--" if s is None else "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--name", default="transcript", help="base file name")
    ap.add_argument("--profile", default=os.path.expanduser("~/.agent-browser/profiles/cloak-sharepoint"))
    ap.add_argument("--state", help="optional storage_state json")
    ap.add_argument("--max-steps", type=int, default=1500)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    if args.state and os.path.exists(args.state):
        ctx = launch_context(storage_state=args.state, headless=False, viewport=None)
    else:
        ctx = launch_persistent_context(args.profile, headless=False, viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(args.url, wait_until="domcontentloaded", timeout=120000)
    time.sleep(8)
    for sel in ('[aria-label*="转录"]', '[aria-label*="Transcri"]', '[aria-label*="字幕"]'):
        el = page.query_selector(sel)
        if el:
            try:
                el.click()
                print("transcript pane opened")
                break
            except Exception:
                pass
    time.sleep(10)

    geom = page.evaluate(INIT_JS)
    print("container:", geom)
    if not geom:
        print("no scrollable transcript container found"); ctx.close(); return

    cells = {}
    for step in range(args.max_steps):
        r = page.evaluate(STEP_JS)
        if r is None:
            break
        for it in r["items"]:
            cells[it["i"]] = it
        if step % 40 == 0:
            print("step %d top=%d/%d cells=%d" % (step, r["top"], r["max"], len(cells)), flush=True)
        if r["top"] >= r["max"] - 2:
            break
        time.sleep(0.22)
    page.evaluate("() => { if (window.__sc) window.__sc.scrollTop = window.__sc.scrollHeight; }")
    time.sleep(1.5)
    r = page.evaluate(STEP_JS)
    if r:
        for it in r["items"]:
            cells[it["i"]] = it
    ctx.close()

    idxs = sorted(cells)
    print("collected %d cells (%s..%s)" % (len(idxs), idxs[0], idxs[-1]))
    rows = []
    for i in idxs:
        c = cells[i]
        sec = parse_seconds(c["clock"], c["aria"])
        rows.append({"index": i, "start": sec, "clock": hhmmss(sec),
                     "speaker": c["speaker"] or speaker_from_aria(c["aria"]),
                     "text": c["text"].strip()})

    b = os.path.join(args.outdir, args.name)
    with open(b + ".txt", "w", encoding="utf-8") as f:
        for r in rows:
            head = "[%s]" % r["clock"] + ((" %s:" % r["speaker"]) if r["speaker"] else "")
            f.write("%s %s\n" % (head, r["text"]))

    with open(b + ".md", "w", encoding="utf-8") as f:
        f.write("# %s\n\n> 来源：SharePoint Stream 转录面板（自动生成，仅供参考）\n\n" % args.name)
        cur = None
        for r in rows:
            if r["speaker"] and r["speaker"] != cur:
                cur = r["speaker"]
                f.write("\n### %s\n\n" % cur)
            f.write("`%s` %s\n\n" % (r["clock"], r["text"]))

    with open(b + ".vtt", "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        n = 0
        for i, r in enumerate(rows):
            if r["start"] is None:
                continue
            nxt = next((x["start"] for x in rows[i + 1:] if x["start"] is not None), None)
            end = nxt if (nxt and nxt > r["start"]) else r["start"] + 4
            n += 1
            f.write("%d\n%s --> %s\n%s%s\n\n" % (
                n, "%02d:%02d:%02d.000" % (r["start"] // 3600, (r["start"] % 3600) // 60, r["start"] % 60),
                "%02d:%02d:%02d.000" % (end // 3600, (end % 3600) // 60, end % 60),
                (r["speaker"] + ": ") if r["speaker"] else "", r["text"]))

    json.dump(rows, open(b + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("exported %d entries -> %s.(txt|md|vtt|json)" % (len(rows), b))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Assemble the static site that GitHub Pages publishes from the daily output folders.

    python scripts/build_site.py --output output --site site [--keep-days 30]

site/index.html              today's call sheet (password-gated when SITE_PASSWORD is set)
site/archive.html            links to every kept day
site/<date>/call_sheet.html  each kept day's sheet (gated the same way) plus its CSV files

Password gate: the HTML is encrypted with AES-GCM under a key derived from the passphrase (PBKDF2,
SHA-256, 200,000 rounds) and wrapped in a small page that asks for the passphrase and decrypts in the
browser with the Web Crypto API. The passphrase never leaves the viewer's browser and GitHub never
sees the plain sheet. Without SITE_PASSWORD the sheet is published as-is, which is fine for a private
repository and unwise for a public one.
"""
from __future__ import annotations

import argparse
import base64
import html
import os
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COPY_FILES = ("call_sheet.html", "call_sheet.csv", "lanes.csv", "apollo_import.csv", "summary.txt")

GATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Award-to-Lane call sheet</title>
<style>body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f3f6f9;color:#1b2430;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:#fff;border:1px solid #d9e0e7;border-radius:10px;padding:28px 32px;max-width:380px;width:90%%}h1{font-size:18px;margin:0 0 6px}p{margin:0 0 14px;color:#5d6b7a}
input{font:inherit;width:100%%;padding:9px 10px;border:1px solid #d9e0e7;border-radius:6px;box-sizing:border-box}button{font:inherit;margin-top:10px;width:100%%;padding:9px;border:0;border-radius:6px;background:#2156a5;color:#fff;cursor:pointer}
.err{color:#9b1c1c;margin-top:10px;display:none}</style></head><body><div class="card"><h1>Award-to-Lane</h1><p>%(label)s. Enter the team passphrase.</p>
<form id="f"><input id="p" type="password" autocomplete="current-password" placeholder="passphrase" autofocus><button type="submit">Open call sheet</button><div class="err" id="e">That passphrase did not work.</div></form></div>
<script>
const SALT="%(salt)s",IV="%(iv)s",DATA="%(data)s";
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function open(pw){const enc=new TextEncoder();const km=await crypto.subtle.importKey("raw",enc.encode(pw),"PBKDF2",false,["deriveKey"]);
const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b64(SALT),iterations:200000,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
const plain=await crypto.subtle.decrypt({name:"AES-GCM",iv:b64(IV)},key,b64(DATA));const text=new TextDecoder().decode(plain);
try{sessionStorage.setItem("a2l_pw",pw)}catch(e){}document.open();document.write(text);document.close()}
document.getElementById("f").addEventListener("submit",async ev=>{ev.preventDefault();try{await open(document.getElementById("p").value)}catch(e){document.getElementById("e").style.display="block"}});
try{const s=sessionStorage.getItem("a2l_pw");if(s)open(s).catch(()=>{})}catch(e){}
</script></body></html>"""


def encrypt_page(plain_html: str, password: str, label: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000).derive(password.encode("utf-8"))
    data = AESGCM(key).encrypt(iv, plain_html.encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return GATE % {"salt": b64(salt), "iv": b64(iv), "data": b64(data), "label": html.escape(label)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="output")
    ap.add_argument("--site", default="site")
    ap.add_argument("--keep-days", type=int, default=30)
    ap.add_argument("--password-env", default="SITE_PASSWORD")
    args = ap.parse_args(argv)
    out, site = Path(args.output), Path(args.site)
    password = os.environ.get(args.password_env) or ""
    days = sorted((d for d in out.iterdir() if d.is_dir() and DATE_RE.match(d.name) and (d / "call_sheet.html").exists()), reverse=True)
    if not days:
        print("no output folders to publish", file=sys.stderr)
        return 1
    cutoff = date.today() - timedelta(days=args.keep_days)
    days = [d for d in days if date.fromisoformat(d.name) >= cutoff] or days[:1]
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    for d in days:
        dest = site / d.name
        dest.mkdir()
        for name in COPY_FILES:
            src = d / name
            if not src.exists():
                continue
            if name == "call_sheet.html" and password:
                dest.joinpath(name).write_text(encrypt_page(src.read_text(encoding="utf-8"), password, f"Call sheet for {d.name}"), encoding="utf-8")
            else:
                shutil.copy(src, dest / name)
    latest = days[0]
    shutil.copy(site / latest.name / "call_sheet.html", site / "index.html")
    rows = "".join(f"<li><a href='{d.name}/call_sheet.html'>{d.name}</a> &middot; <a href='{d.name}/call_sheet.csv'>csv</a> &middot; "
                   f"<a href='{d.name}/lanes.csv'>lanes</a></li>" for d in days)
    (site / "archive.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Award-to-Lane archive</title>"
        "<style>body{font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:32px;color:#1b2430}a{color:#2156a5}</style></head>"
        f"<body><h1>Award-to-Lane call sheets</h1><p><a href='index.html'>Latest ({latest.name})</a></p><ul>{rows}</ul></body></html>",
        encoding="utf-8")
    (site / ".nojekyll").write_text("")
    print(f"site built: {len(days)} day(s), latest {latest.name}, {'password-gated' if password else 'NOT password-gated'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

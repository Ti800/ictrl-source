#!/usr/bin/env python3
"""Mirror OpenMinis/iCTRL releases with patched version numbers and build an
AltStore/Feather source JSON.

Upstream IPAs always carry CFBundleShortVersionString 1.0(.0), so Feather can't
tell installed vs. available versions apart. For each upstream release we
download every .ipa, set CFBundleShortVersionString/CFBundleVersion to the
release version, and publish them as a release on THIS repo (same tag).

Robustness:
- IPAs are identified by the bundle id inside them, not by file name.
- bundle id / name / MinimumOSVersion are read from each IPA.
- an upstream asset that is re-uploaded (id/size/updated_at changed) is re-mirrored.
- version is parsed from the tag with a regex (v1.2.3, iCTRL-1.2.3, 1.2 ...).
- health checks at the end exit with code 2 (after writing outputs) so the
  workflow run fails and GitHub emails the repo owner.
State (what was mirrored from which upstream asset) lives in mirror-state.json.
"""
import json, os, re, sys, subprocess, tempfile, zipfile, plistlib, urllib.request, urllib.error

UPSTREAM = os.environ.get("UPSTREAM_REPO", "OpenMinis/iCTRL")
MIRROR = os.environ.get("MIRROR_REPO", "Ti800/ictrl-source")
KEEP = int(os.environ.get("KEEP_VERSIONS", "5"))
EXPECTED = [b for b in os.environ.get(
    "EXPECTED_BUNDLES", "com.openminis.ictrl,com.openminis.ictrl.uitests.xctrunner").split(",") if b]
STATE_FILE = "mirror-state.json"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

RAW = f"https://raw.githubusercontent.com/{UPSTREAM}/main"
ICON_APP = f"{RAW}/iCTRLApp/Assets.xcassets/AppIcon.appiconset/icon_1024x1024.png"
KNOWN = {  # cosmetic overrides; anything else falls back to values from the IPA
    "com.openminis.ictrl": dict(name="iCTRL", iconURL=ICON_APP, subtitle="AI-friendly iOS automation"),
    "com.openminis.ictrl.uitests.xctrunner": dict(
        name="iCTRL Runner", iconURL=f"{RAW}/Config/RunnerIcon/AppIcon60x60@3x.png",
        subtitle="iCTRL XCUITest runner (port 8200)"),
}
TOP_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
errors, warnings = [], []


def api(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ictrl-source"})
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    return json.load(urllib.request.urlopen(req, timeout=60))


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "ictrl-source"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def parse_version(tag):
    m = re.search(r"\d+(?:\.\d+)+", tag) or re.search(r"\d+", tag)
    return m.group(0) if m else None


def patch_ipa(src, dst, version):
    """Copy IPA rewriting the top-level app's version keys; return its metadata."""
    meta = None
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w") as zo:
        tops = [n for n in zi.namelist() if TOP_PLIST.match(n)]
        if len(tops) != 1:
            raise ValueError(f"expected 1 Payload/*.app/Info.plist, found {len(tops)}")
        for info in zi.infolist():
            data = zi.read(info.filename)
            if info.filename == tops[0]:
                fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
                d = plistlib.loads(data)
                meta = {"bundle_id": d["CFBundleIdentifier"],
                        "app_name": d.get("CFBundleDisplayName") or d.get("CFBundleName") or d["CFBundleIdentifier"],
                        "min_os": d.get("MinimumOSVersion", "")}
                d["CFBundleShortVersionString"] = version
                d["CFBundleVersion"] = version
                data = plistlib.dumps(d, fmt=fmt)
            zo.writestr(info, data, compress_type=info.compress_type)
    with zipfile.ZipFile(dst) as z:
        assert plistlib.loads(z.read(tops[0]))["CFBundleShortVersionString"] == version
    return meta


def fingerprint(a):
    return {"id": a["id"], "size": a["size"], "updated_at": a["updated_at"]}


def mirror_release(rel, version, state, latest):
    """Make MIRROR's release `tag` hold patched copies of every upstream .ipa."""
    tag = rel["tag_name"]
    ipas = [a for a in rel["assets"] if a["name"].lower().endswith(".ipa")]
    if not ipas:
        warnings.append(f"{tag}: upstream release has no .ipa assets")
    st = state.setdefault(tag, {})
    try:
        existing = api(f"https://api.github.com/repos/{MIRROR}/releases/tags/{tag}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        existing = None
    have = {a["name"]: a for a in (existing or {}).get("assets", [])}

    todo = [a for a in ipas
            if a["name"] not in have or st.get(a["name"], {}).get("upstream") != fingerprint(a)]
    files, tmp = [], tempfile.mkdtemp()
    for a in todo:
        why = "new" if a["name"] not in st else "changed upstream"
        print(f"  {tag}/{a['name']}: mirroring ({why})", flush=True)
        src, dst = os.path.join(tmp, "orig-" + a["name"]), os.path.join(tmp, a["name"])
        try:
            download(a["browser_download_url"], src)
            meta = patch_ipa(src, dst, version)
        except Exception as e:
            errors.append(f"{tag}/{a['name']}: cannot patch: {e}")
            continue
        files.append(dst)
        st[a["name"]] = {"upstream": fingerprint(a), **meta}

    notes = (f"Mirror of https://github.com/{UPSTREAM}/releases/tag/{tag} with "
             f"CFBundleShortVersionString/CFBundleVersion set to {version}.\n\n" + (rel.get("body") or ""))[:100000]
    if existing is None:
        if files:
            gh("release", "create", tag, *files, "-R", MIRROR, "--title", f"iCTRL {version}",
               "--notes", notes, f"--latest={str(latest).lower()}")
    elif files:
        gh("release", "upload", tag, *files, "-R", MIRROR, "--clobber")

    # drop mirror assets / state for files no longer in the upstream release
    names = {a["name"] for a in ipas}
    for n in list(st):
        if n not in names:
            print(f"  {tag}/{n}: gone upstream, removing", flush=True)
            st.pop(n)
            if n in have:
                gh("release", "delete-asset", tag, n, "-R", MIRROR, "--yes")

    if not st:
        return {}
    r = api(f"https://api.github.com/repos/{MIRROR}/releases/tags/{tag}")
    urls = {a["name"]: (a["browser_download_url"], a["size"]) for a in r["assets"]}
    return {n: {**m, "url": urls[n][0], "size": urls[n][1]} for n, m in st.items() if n in urls}


def main(out):
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    repo = api(f"https://api.github.com/repos/{UPSTREAM}")
    releases = []
    for r in api(f"https://api.github.com/repos/{UPSTREAM}/releases?per_page=30"):
        if r["draft"] or r["prerelease"]:
            continue
        v = parse_version(r["tag_name"])
        if not v:
            (errors if not releases else warnings).append(f"{r['tag_name']}: no version number in tag, skipped")
            continue
        releases.append((r, v))
        if len(releases) == KEEP:
            break
    if not releases:
        errors.append("no usable upstream releases")

    mirrored = []
    for i, (r, v) in enumerate(releases):
        print(f"{r['tag_name']} -> {v}", flush=True)
        mirrored.append((r, v, mirror_release(r, v, state, i == 0)))
    if releases:
        gh("release", "edit", releases[0][0]["tag_name"], "-R", MIRROR, "--latest")
    keep_tags = {r["tag_name"] for r, _ in releases}
    state = {t: s for t, s in state.items() if t in keep_tags}

    # group by bundle id, newest release first
    by_bundle = {}
    for r, v, assets in mirrored:
        for m in assets.values():
            by_bundle.setdefault(m["bundle_id"], []).append((r, v, m))

    apps = []
    order = EXPECTED + sorted(b for b in by_bundle if b not in EXPECTED)
    for bid in order:
        if bid not in by_bundle:
            continue
        entries = by_bundle[bid]
        k = KNOWN.get(bid, {})
        versions = [{
            "version": v, "buildVersion": v, "date": r["published_at"],
            "localizedDescription": (r.get("body") or r["name"] or "").strip()[:4000],
            "downloadURL": m["url"], "size": m["size"], "minOSVersion": m["min_os"],
        } for r, v, m in entries]
        v0 = versions[0]
        apps.append({
            "name": k.get("name", entries[0][2]["app_name"]), "bundleIdentifier": bid,
            "developerName": "OpenMinis", "subtitle": k.get("subtitle", ""),
            "localizedDescription": repo.get("description") or "",
            "iconURL": k.get("iconURL", ICON_APP), "tintColor": "#007AFF", "category": "developer",
            "versions": versions,
            "version": v0["version"], "versionDate": v0["date"],
            "versionDescription": v0["localizedDescription"],
            "downloadURL": v0["downloadURL"], "size": v0["size"], "minOSVersion": v0["minOSVersion"],
        })

    # ---- health checks -------------------------------------------------------
    if releases:
        newest = releases[0][1]
        for bid in EXPECTED:
            a = next((x for x in apps if x["bundleIdentifier"] == bid), None)
            if a is None:
                errors.append(f"{bid}: missing from source (renamed bundle id or asset?)")
            elif a["version"] != newest:
                errors.append(f"{bid}: source has {a['version']} but newest upstream is {newest} "
                              f"(asset missing/unrecognised in {releases[0][0]['tag_name']})")
        for a in apps:
            if a["bundleIdentifier"] not in EXPECTED:
                warnings.append(f"new/unknown bundle id in upstream: {a['bundleIdentifier']} ({a['name']})")

    if apps:  # never overwrite a good source with an empty one
        source = {
            "name": "iCTRL (OpenMinis)", "identifier": "com.openminis.ictrl.source",
            "subtitle": "GitHub releases of OpenMinis/iCTRL (version-patched)",
            "description": repo.get("description") or "", "iconURL": ICON_APP,
            "website": f"https://github.com/{UPSTREAM}", "tintColor": "#007AFF",
            "apps": apps, "news": [],
        }
        with open(out, "w") as f:
            json.dump(source, f, ensure_ascii=False, indent=2)
        print(f"wrote {out}: " + ", ".join(f"{a['name']} {a['version']} ({len(a['versions'])})" for a in apps))
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

    for w in warnings:
        print(f"::warning::{w}")
    for e in errors:
        print(f"::error::{e}")
    if errors:
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ictrl-source.json")

#!/usr/bin/env python3
"""Mirror OpenMinis/iCTRL releases with patched version numbers and build an
AltStore/Feather source JSON.

Upstream IPAs always carry CFBundleShortVersionString 1.0(.0), so Feather can't
tell installed vs. available versions apart. For each upstream release we
download the IPAs, set CFBundleShortVersionString/CFBundleVersion to the tag
version, and publish them as a release on THIS repo (tag = same as upstream).
The source JSON points at those patched copies.
"""
import json, os, re, sys, subprocess, tempfile, zipfile, plistlib, urllib.request

UPSTREAM = "OpenMinis/iCTRL"
MIRROR = os.environ.get("MIRROR_REPO", "Ti800/ictrl-source")
KEEP = int(os.environ.get("KEEP_VERSIONS", "5"))
RAW = f"https://raw.githubusercontent.com/{UPSTREAM}/main"
ICON_APP = f"{RAW}/iCTRLApp/Assets.xcassets/AppIcon.appiconset/icon_1024x1024.png"
ICON_RUNNER = f"{RAW}/Config/RunnerIcon/AppIcon60x60@3x.png"

APPS = {
    "iCTRL.ipa": dict(name="iCTRL", bundleIdentifier="com.openminis.ictrl",
                      iconURL=ICON_APP, minOSVersion="16.0",
                      subtitle="AI-friendly iOS automation"),
    "iCTRLUITests-Runner.ipa": dict(name="iCTRL Runner", bundleIdentifier="com.openminis.ictrl.uitests.xctrunner",
                                    iconURL=ICON_RUNNER, minOSVersion="17.0",
                                    subtitle="iCTRL XCUITest runner (port 8200)"),
}
TOKEN = os.environ.get("GITHUB_TOKEN", "")


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


def patch_ipa(src, dst, version):
    """Copy IPA, rewriting Payload/*.app/Info.plist version keys."""
    top = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w") as zo:
        for info in zi.infolist():
            data = zi.read(info.filename)
            if top.match(info.filename):
                fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
                d = plistlib.loads(data)
                d["CFBundleShortVersionString"] = version
                d["CFBundleVersion"] = version
                data = plistlib.dumps(d, fmt=fmt)
            zo.writestr(info, data, compress_type=info.compress_type)
    with zipfile.ZipFile(dst) as z:  # verify
        for n in z.namelist():
            if top.match(n):
                assert plistlib.loads(z.read(n))["CFBundleShortVersionString"] == version


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def mirror_release(rel, latest=False):
    """Ensure MIRROR has release `tag` with patched assets; return {asset: (url, size)}."""
    tag, version = rel["tag_name"], rel["tag_name"].lstrip("v")
    try:
        existing = api(f"https://api.github.com/repos/{MIRROR}/releases/tags/{tag}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        existing = None
    have = {a["name"]: a for a in (existing or {}).get("assets", [])}
    wanted = [a for a in rel["assets"] if a["name"] in APPS]
    if all(a["name"] in have for a in wanted):
        return {n: (a["browser_download_url"], a["size"]) for n, a in have.items()}

    print(f"mirroring {tag} …", flush=True)
    tmp = tempfile.mkdtemp()
    files = []
    for a in wanted:
        if a["name"] in have:
            continue
        src = os.path.join(tmp, "orig-" + a["name"])
        dst = os.path.join(tmp, a["name"])
        download(a["browser_download_url"], src)
        patch_ipa(src, dst, version)
        files.append(dst)
    notes = f"Mirror of https://github.com/{UPSTREAM}/releases/tag/{tag} with CFBundleShortVersionString/CFBundleVersion set to {version}.\n\n" + (rel.get("body") or "")
    if existing is None:
        gh("release", "create", tag, *files, "-R", MIRROR, "--title", f"iCTRL {version}", "--notes", notes[:100000], f"--latest={str(latest).lower()}")
    else:
        gh("release", "upload", tag, *files, "-R", MIRROR, "--clobber")
    r = api(f"https://api.github.com/repos/{MIRROR}/releases/tags/{tag}")
    return {a["name"]: (a["browser_download_url"], a["size"]) for a in r["assets"]}


def main(out):
    repo = api(f"https://api.github.com/repos/{UPSTREAM}")
    releases = [r for r in api(f"https://api.github.com/repos/{UPSTREAM}/releases?per_page=30")
                if not r["draft"] and not r["prerelease"]][:KEEP]
    mirrored = {r["tag_name"]: mirror_release(r, i == 0) for i, r in enumerate(releases)}

    apps = []
    for asset_name, meta in APPS.items():
        versions = []
        for r in releases:
            m = mirrored[r["tag_name"]].get(asset_name)
            if not m:
                continue
            v = r["tag_name"].lstrip("v")
            versions.append({
                "version": v, "buildVersion": v, "date": r["published_at"],
                "localizedDescription": (r.get("body") or r["name"] or "").strip()[:4000],
                "downloadURL": m[0], "size": m[1], "minOSVersion": meta["minOSVersion"],
            })
        if not versions:
            continue
        v0 = versions[0]
        apps.append({
            "name": meta["name"], "bundleIdentifier": meta["bundleIdentifier"],
            "developerName": "OpenMinis", "subtitle": meta["subtitle"],
            "localizedDescription": repo.get("description") or "",
            "iconURL": meta["iconURL"], "tintColor": "#007AFF", "category": "developer",
            "versions": versions,
            "version": v0["version"], "versionDate": v0["date"],
            "versionDescription": v0["localizedDescription"],
            "downloadURL": v0["downloadURL"], "size": v0["size"],
        })

    source = {
        "name": "iCTRL (OpenMinis)", "identifier": "com.openminis.ictrl.source",
        "subtitle": "GitHub releases of OpenMinis/iCTRL (version-patched)",
        "description": repo.get("description") or "", "iconURL": ICON_APP,
        "website": f"https://github.com/{UPSTREAM}", "tintColor": "#007AFF",
        "apps": apps, "news": [],
    }
    with open(out, "w") as f:
        json.dump(source, f, ensure_ascii=False, indent=2)
    print(f"wrote {out}: " + ", ".join(f"{a['name']} {a['version']} ({len(a['versions'])} versions)" for a in apps))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ictrl-source.json")

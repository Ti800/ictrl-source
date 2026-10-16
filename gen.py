#!/usr/bin/env python3
"""Generate an AltStore/Feather source JSON from OpenMinis/iCTRL GitHub releases."""
import json, urllib.request, sys, os

REPO = "OpenMinis/iCTRL"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
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

def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ictrl-source"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    return json.load(urllib.request.urlopen(req, timeout=30))

repo = get(f"https://api.github.com/repos/{REPO}")
releases = [r for r in get(f"https://api.github.com/repos/{REPO}/releases?per_page=30")
            if not r["draft"] and not r["prerelease"]]

apps = []
for asset_name, meta in APPS.items():
    versions = []
    for r in releases:
        a = next((x for x in r["assets"] if x["name"] == asset_name), None)
        if not a:
            continue
        versions.append({
            "version": r["tag_name"].lstrip("v"),
            "buildVersion": r["tag_name"].lstrip("v"),
            "date": r["published_at"],
            "localizedDescription": (r.get("body") or r["name"] or "").strip()[:4000],
            "downloadURL": a["browser_download_url"],
            "size": a["size"],
            "minOSVersion": meta["minOSVersion"],
        })
    if not versions:
        continue
    v0 = versions[0]
    apps.append({
        "name": meta["name"],
        "bundleIdentifier": meta["bundleIdentifier"],
        "developerName": "OpenMinis",
        "subtitle": meta["subtitle"],
        "localizedDescription": repo.get("description") or "",
        "iconURL": meta["iconURL"],
        "tintColor": "#007AFF",
        "category": "developer",
        "versions": versions,
        # legacy fields (older AltStore / ESign / Feather builds)
        "version": v0["version"],
        "versionDate": v0["date"],
        "versionDescription": v0["localizedDescription"],
        "downloadURL": v0["downloadURL"],
        "size": v0["size"],
    })

source = {
    "name": "iCTRL (OpenMinis)",
    "identifier": "com.openminis.ictrl.source",
    "subtitle": "GitHub releases of OpenMinis/iCTRL",
    "description": repo.get("description") or "",
    "iconURL": ICON_APP,
    "website": f"https://github.com/{REPO}",
    "tintColor": "#007AFF",
    "apps": apps,
    "news": [],
}

out = sys.argv[1] if len(sys.argv) > 1 else "ictrl-source.json"
with open(out, "w") as f:
    json.dump(source, f, ensure_ascii=False, indent=2)
print(f"wrote {out}: " + ", ".join(f"{a['name']} {a['version']} ({len(a['versions'])} versions)" for a in apps))

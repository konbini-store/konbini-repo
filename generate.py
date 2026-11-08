import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parent
API_ROOT = ROOT / "api"
CACHE_DIR = ROOT / ".apk_cache"
AAPT2_PATH = r"/usr/bin/aapt2"


def read_yaml(path):
    with path.open(encoding="utf-8") as source:
        return yaml.safe_load(source) or {}


def read_json(path):
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=4, ensure_ascii=False)
        output.write("\n")


def existing_apps(app_dirs):
    by_package = {}
    used_ids = set()
    for apps_dir in app_dirs:
        if not apps_dir.exists():
            continue
        for path in apps_dir.glob("*.json"):
            try:
                app = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(app, dict):
                continue
            app_id = app.get("id")
            if isinstance(app_id, int):
                used_ids.add(app_id)
                if app.get("packageId"):
                    by_package[app["packageId"]] = app
    return by_package, used_ids


def public_icon_url(icon, base_url):
    if not icon:
        return icon
    path = urlparse(icon).path
    if path.startswith("/resources/"):
        return f"{base_url.rstrip('/')}{path}"
    return icon


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_apk(url, apk_path):
    if not url:
        return None
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=120) as response, apk_path.open("wb") as output:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            output.write(chunk)
    return apk_path


def cached_apk_details(cache_path, checksum):
    try:
        details = read_json(cache_path)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        print(f"[APK] Ignoring invalid cache file {cache_path}")
        return None

    required_fields = (
        "size",
        "versionCode",
        "versionName",
        "minSdk",
        "targetSdk",
        "abis",
        "sha256",
        "createdAt",
    )
    if (
        not isinstance(details, dict)
        or details.get("sha256") != checksum
        or any(key not in details for key in required_fields)
    ):
        print(f"[APK] Ignoring invalid cache file {cache_path}")
        return None
    return details


def apk_manifest_details(apk_path, checksum):
    if not Path(AAPT2_PATH).exists():
        raise FileNotFoundError(f"aapt2 not found at {AAPT2_PATH}")

    result = subprocess.run(
        [AAPT2_PATH, "dump", "badging", str(apk_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "aapt2 failed to inspect APK")

    output = result.stdout
    package_match = re.search(r"package:\s+name='([^']+)'\s+versionCode='([^']*)'\s+versionName='([^']*)'",
                              output,
                              re.DOTALL)
    package_name = package_match.group(1) if package_match else None
    version_code = package_match.group(2) if package_match else None
    version_name = package_match.group(3) if package_match else None

    min_sdk = None
    target_sdk = None
    for label in ("minSdkVersion", "targetSdkVersion", "sdkVersion"):
        match = re.search(rf"{re.escape(label)}:'(\d+)'", output)
        if match:
            if label == "minSdkVersion" or label == "sdkVersion":
                min_sdk = int(match.group(1))
            else:
                target_sdk = int(match.group(1))

    abi_tokens = re.findall(r"native-code:\s+((?:'[^']+'(?:\s+)?)*)", output)
    abis = []
    for token_block in abi_tokens:
        for abi in re.findall(r"'([^']+)'", token_block):
            if abi not in abis:
                abis.append(abi)

    try:
        if version_code is not None:
            version_code = int(version_code)
    except (TypeError, ValueError):
        version_code = None

    return {
        "packageName": package_name,
        "versionCode": version_code,
        "versionName": version_name,
        "minSdk": min_sdk,
        "targetSdk": target_sdk,
        "abis": abis,
        "sha256": checksum,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def make_versions(source_versions, app_id, previous_app, next_version_id, app_label):
    previous_versions = {
        version.get("downloadUrl"): version
        for version in previous_app.get("versions", [])
        if isinstance(version, dict) and version.get("downloadUrl")
    }
    versions = []
    if isinstance(source_versions, dict):
        source_versions = [source_versions]
    source_versions = source_versions or []
    for version_number, item in enumerate(source_versions, start=1):
        if isinstance(item, str):
            download_url = item
            version = dict(previous_versions.get(download_url, {}))
        elif isinstance(item, dict):
            download_url = item.get("downloadUrl") or item.get("url")
            version = dict(item)
        else:
            raise ValueError("Each version must be a URL string or an object")

        for alias, field in (
            ("name", "versionName"),
            ("code", "versionCode"),
            ("api", "minSdk"),
            ("url", "downloadUrl"),
        ):
            if field not in version and alias in version:
                version[field] = version[alias]
            version.pop(alias, None)
        download_url = version.get("downloadUrl") or download_url
        if not download_url:
            continue

        version["downloadUrl"] = download_url
        version["id"] = next_version_id
        version["appId"] = app_id
        version["uploaderId"] = 1
        next_version_id += 1

        analysis_fields = (
            "size",
            "versionCode",
            "versionName",
            "minSdk",
            "targetSdk",
            "abis",
            "sha256",
            "createdAt",
        )
        if any(version.get(key) is None for key in analysis_fields):
            try:
                print(f"[APK] {app_label}: processing version {version_number}/{len(source_versions)}; fetching APK checksum")
                with tempfile.TemporaryDirectory(prefix="konbini-apk-") as temp_dir:
                    apk_path = fetch_apk(download_url, Path(temp_dir) / "app.apk")
                    checksum = sha256_file(apk_path)
                    cache_path = CACHE_DIR / f"{checksum}.json"
                    details = cached_apk_details(cache_path, checksum)
                    if details is None:
                        details = apk_manifest_details(apk_path, checksum)
                        details["size"] = apk_path.stat().st_size
                        write_json(cache_path, details)
                        print(f"[APK] {app_label}: cached APK analysis as {cache_path.name}")
                    else:
                        print(f"[APK] {app_label}: using cached APK analysis {cache_path.name}")
                for key in analysis_fields:
                    if version.get(key) is None and details.get(key) is not None:
                        version[key] = details[key]
                print(f"[APK] {app_label}: version {version_number} processed successfully")
            except Exception as error:
                print(f"[APK] {app_label}: version {version_number} failed: {error}")
                for key in analysis_fields:
                    if version.get(key) is None:
                        version[key] = [] if key == "abis" else None

        versions.append(version)
    return versions, next_version_id


def generate(base_url):
    now = int(datetime.now(timezone.utc).timestamp())
    write_json(API_ROOT / "meta.json", {"lastUpdated": now})

    apps_dir = API_ROOT / "apps"
    categories_dir = API_ROOT / "categories"
    categories = read_yaml(ROOT / "_apps" / "categories.yml")
    categories = categories if isinstance(categories, list) else []
    categories_by_id = {category["id"]: category for category in categories}
    previous_by_package, used_ids = existing_apps([apps_dir, ROOT / "apps"])
    next_id = max(used_ids, default=0) + 1
    next_version_id = 1
    overview = []
    featured = []

    for metadata_path in sorted((ROOT / "_apps").glob("*.yml")):
        if metadata_path.name == "categories.yml":
            continue

        print(f"[metadata] Reading {metadata_path.relative_to(ROOT)}")
        metadata = read_yaml(metadata_path)
        package_id = metadata.get("packageName", metadata_path.stem)
        app_label = f"{metadata.get('name', package_id)} ({package_id})"
        print(f"[metadata] Processing app: {app_label}")
        category_id = metadata.get("category")
        if category_id not in categories_by_id:
            raise ValueError(f"Unknown category {category_id!r} in {metadata_path}")

        previous = previous_by_package.get(package_id, {})
        app_id = previous.get("id")
        if not isinstance(app_id, int):
            while next_id in used_ids:
                next_id += 1
            app_id = next_id
            used_ids.add(app_id)
            next_id += 1

        versions, next_version_id = make_versions(
            metadata.get("versions", []), app_id, previous, next_version_id, app_label
        )
        detail = {
            "id": app_id,
            "name": metadata.get("name", package_id),
            "author": metadata.get("author", ""),
            "packageId": package_id,
            "description": metadata.get("fullDescription", metadata.get("description", "")),
            "uploaderId": previous.get("uploaderId", 1),
            "moderatedObjectId": previous.get("moderatedObjectId", 4),
            "categoryId": category_id,
            "icon": metadata.get("icon", ""),
            "screenshots": metadata.get("screenshots", []),
            "resources": metadata.get("resources", previous.get("resources", [])),
            "versions": versions,
            "featured": metadata.get("featured") is True,
            "reviews": previous.get("reviews", []),
        }
        write_json(apps_dir / f"{app_id}.json", detail)
        print(f"[metadata] Wrote api/apps/{app_id}.json for {app_label}")

        category = categories_by_id[category_id]
        summary = {
            "name": detail["name"],
            "id": app_id,
            "uploaderId": detail["uploaderId"],
            "icon": public_icon_url(detail["icon"], base_url),
            "abis": versions[0].get("abis", []) if versions else [],
            "packageName": package_id,
            "isGame": category.get("type") == "games",
            "categoryCode": category_id,
            "categoryLabel": category.get("name", category_id),
            "rating": metadata.get("rating", 0),
            "downloads": metadata.get("downloads", 0),
            "author": detail["author"],
            "description": metadata.get("description", ""),
            "featured": metadata.get("featured") is True,
        }
        if versions and versions[0].get("minSdk") is not None:
            summary["api"] = versions[0]["minSdk"]
        overview.append(summary)
        if metadata.get("featured") is True:
            featured.append(summary)

    write_json(API_ROOT / "apps.json", overview)
    write_json(API_ROOT / "featured.json", featured)
    for category_type in ("apps", "games"):
        typed_categories = [
            category for category in categories
            if category.get("type") == category_type
        ]
        write_json(categories_dir / f"{category_type}.json", typed_categories)
    print(f"[metadata] Wrote api/apps.json ({len(overview)} apps)")
    print(f"[metadata] Wrote api/featured.json ({len(featured)} featured apps)")
    print("[metadata] Generation complete")


def main():
    parser = argparse.ArgumentParser(description="Generate Konbini API JSON files from YAML metadata")
    parser.add_argument("--base-url", default="http://konbini.lol")
    args = parser.parse_args()
    generate(args.base_url)


if __name__ == "__main__":
    main()

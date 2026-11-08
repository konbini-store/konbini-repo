# Konbini metadata generator

Generates the JSON files served by the Konbini API from `_apps` YAML metadata:

- `api/apps.json`
- `api/apps/<id>.json`
- `api/categories/apps.json`
- `api/categories/games.json`

Install the dependency and run the generator from any directory:

```powershell
python -m pip install -r requirements.txt
python generate.py
```

Each app lives in `_apps/<package-id>.yml`. The filename (without `.yml`) is
used as `packageName`; set `packageName` in the YAML to override it. Categories
are defined in `_apps/categories.yml`, with `type: apps` or `type: games`.

Existing detail files keep their ID when their `packageId` matches an app.
New apps receive the next available numeric ID. Version entries can be URL
strings, objects, or a single object. APKs are downloaded into a temporary
directory and removed after inspection. Their analysis metadata is cached as
`.apk_cache/<apk-sha256>.json`, without retaining the APK files. The app
overview is written to `api/apps.json`. Analysis fills missing version data;
provided fields take precedence. To avoid APK analysis entirely, provide all of
`size`, `versionCode`, `versionName`, `minSdk`, `targetSdk`, `abis`, `sha256`,
and `createdAt` along with `downloadUrl`. The aliases `name`, `code`, `api`, and
`url` map to `versionName`, `versionCode`, `minSdk`, and `downloadUrl`.
Existing version metadata is retained when its URL matches. Use `--base-url`
to set the host used for overview icon URLs (`http://konbini.lol` by default).
#!/usr/bin/env python3
"""Move an HA install from upstream solaredge_modbus_multi to innovo_solar_edge.

Run on the HA host with Home Assistant STOPPED and custom_components/innovo_solar_edge
already in place. Keeps config entries, devices, entity ids, unique ids and
statistics; only the owning domain changes. Backups go to /root/innovo-solar-edge-migration-<ts>/.

    python3 migrate_domain.py /path/to/ha/config
"""
import json
import os
import shutil
import sys
import time

OLD, NEW = "solaredge_modbus_multi", "innovo_solar_edge"
UPSTREAM_HACS_REPO = "WillCodeForCats/solaredge-modbus-multi"

if len(sys.argv) != 2:
    sys.exit("usage: migrate_domain.py /path/to/homeassistant/config")
config = sys.argv[1]
storage = os.path.join(config, ".storage")
backup = f"/root/innovo-solar-edge-migration-{int(time.time())}"
os.makedirs(backup)


def edit(name, fn):
    path = os.path.join(storage, name)
    if not os.path.exists(path):
        return
    shutil.copy(path, backup)
    data = json.load(open(path))
    changed = fn(data["data"])
    json.dump(data, open(path, "w"), indent=2)
    print(f"{name}: {changed}")


def entries(d):
    n = 0
    for e in d["entries"]:
        if e["domain"] == OLD:
            e["domain"] = NEW
            n += 1
    return f"{n} config entries"


def entities(d):
    n = 0
    for key in ("entities", "deleted_entities"):
        for e in d.get(key, []):
            if e.get("platform") == OLD:
                e["platform"] = NEW
                n += 1
    return f"{n} entities"


def devices(d):
    n = 0
    for key in ("devices", "deleted_devices"):
        for dev in d.get(key, []):
            ids = dev.get("identifiers", [])
            if any(i[0] == OLD for i in ids):
                dev["identifiers"] = [[NEW, *i[1:]] if i[0] == OLD else i for i in ids]
                n += 1
    return f"{n} devices"


def issues(d):
    before = len(d["issues"])
    d["issues"] = [i for i in d["issues"] if i.get("domain") != OLD]
    return f"{before - len(d['issues'])} repair issues dropped"


def hacs(d):
    for repo in d.values():
        if repo.get("full_name") == UPSTREAM_HACS_REPO and repo.get("installed"):
            repo["installed"] = False
            repo.pop("installed_commit", None)
            repo.pop("version_installed", None)
            return "upstream repo marked not installed"
    return "no change"


edit("core.config_entries", entries)
edit("core.entity_registry", entities)
edit("core.device_registry", devices)
edit("repairs.issue_registry", issues)
edit("hacs.repositories", hacs)

# Energy model store: <domain>.energy.<entry_id>
for name in os.listdir(storage):
    if name.startswith(f"{OLD}.energy."):
        new_name = NEW + name[len(OLD):]
        data = json.load(open(os.path.join(storage, name)))
        data["key"] = new_name
        json.dump(data, open(os.path.join(storage, new_name), "w"), indent=2)
        shutil.move(os.path.join(storage, name), backup)
        print(f"store: {name} -> {new_name}")

old_dir = os.path.join(config, "custom_components", OLD)
if os.path.isdir(old_dir):
    shutil.move(old_dir, os.path.join(backup, OLD))
    print(f"moved {old_dir} to backup")

print("backups:", backup)

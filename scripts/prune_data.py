#!/usr/bin/env python3
"""Keep only confirmed-scam domains in data/.

recv.py saves a screenshot + HTML for every content-filter positive, long
before validate.py decides whether it's actually a scam, and nothing ever
deletes the ones that aren't confirmed -- so data/ grows without bound.

This keeps every domain that appears in results/validate/confirmed_scams_*.jsonl
forever, and deletes any other domain folder once it's older than --grace hours
(default 24). The grace window means a domain still waiting to be classified
isn't deleted out from under the pipeline.

  python3 -I prune_data.py           # dry run (default)
  python3 -I prune_data.py --apply   # actually delete
Env: PRUNE_GRACE_HOURS (default 24), PRUNE_ROOT (default ~/crimson-agentic-browsing)
"""
import glob, json, os, shutil, sys, time

ROOT = os.environ.get("PRUNE_ROOT", os.path.expanduser("~/crimson-agentic-browsing"))
GRACE_H = float(os.environ.get("PRUNE_GRACE_HOURS", "24"))
APPLY = "--apply" in sys.argv

confirmed = set()
for f in glob.glob(f"{ROOT}/results/validate/confirmed_scams_*.jsonl"):
    for line in open(f, errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            confirmed.add(json.loads(line)["url"].strip().lower())
        except Exception:
            pass

cutoff = time.time() - GRACE_H * 3600

def dirsize(p):
    t = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return t

kept = deleted = grace = 0
freed = 0
# domain folders: data/recvN/N/{screenshots,check}/{YYMMDD}/{domain}/
for tree in (glob.glob(f"{ROOT}/data/recv*/*/screenshots/*/*")
             + glob.glob(f"{ROOT}/data/recv*/*/check/*/*")):
    if not os.path.isdir(tree):
        continue
    domain = os.path.basename(tree).lower()
    if domain in confirmed:
        kept += 1
        continue
    try:
        if os.path.getmtime(tree) > cutoff:
            grace += 1
            continue
    except OSError:
        continue
    freed += dirsize(tree)
    deleted += 1
    if APPLY:
        shutil.rmtree(tree, ignore_errors=True)

if APPLY:  # drop now-empty date dirs
    for dd in (glob.glob(f"{ROOT}/data/recv*/*/screenshots/*")
               + glob.glob(f"{ROOT}/data/recv*/*/check/*")):
        try:
            if os.path.isdir(dd) and not os.listdir(dd):
                os.rmdir(dd)
        except OSError:
            pass

print(f"{time.strftime('%Y-%m-%d %H:%M')} {'APPLY' if APPLY else 'DRY RUN'} | "
      f"confirmed domains: {len(confirmed)} | kept folders: {kept} | "
      f"deleted: {deleted} ({freed/1e9:.1f} GB) | within {GRACE_H:g}h grace: {grace}")

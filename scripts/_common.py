"""Shared by the live scripts: PASS/FAIL lines and running both seats of a match in threads."""
import json
import os
import threading
import traceback

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)
    return ok


def both(*fns):
    out, errs = [None] * len(fns), []

    def run(i, f):
        try:
            out[i] = f()
        except Exception as e:
            errs.append(e)
            traceback.print_exc()

    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(900)
    if any(t.is_alive() for t in ts):
        raise RuntimeError("A seat didn't finish within 15 minutes.")
    if errs:
        raise errs[0]
    return out


def save(folder, name, data):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return path


def finish():
    print()
    if failures:
        print(f"{len(failures)} FAILED: " + "; ".join(failures))
        raise SystemExit(1)
    print("ALL PASSED")

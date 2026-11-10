"""Run one batch on Nosana with automatic retry: post, watch, stop in `finally`, fetch the result,
accept it only if it passed check_result, else post again (up to max_attempts). Every attempt,
failed or not, is a row in the ledger's `jobs` table and a set of raw fixtures on disk.

Resumable: an attempt whose post was saved but never recorded (the script died after posting) is
picked up from its saved post response and finished, not posted again.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from relay_verify.checks import Fetcher, Unavailable
from relay_verify.jcs import canonical
from relay_verify.outputs import container_times, regex_result
from relay_verify.result import check_result
from relay_verify.solana import b58encode, get_account_raw, job_from_rpc_response, rpc_call

from .ledger import upsert
from .nosana.ipfs import pin_both
from .nosana.jobs import Jobs, run_account_exists


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    """Phase cap in USD, measured from the balance: (settled + reserved) now minus at the start."""
    jobs: Jobs
    cap_usd: float
    baseline: float = field(init=False)

    def __post_init__(self):
        b = self.jobs.balance()
        self.baseline = float(b["settledCredits"]) + float(b["reservedCredits"])

    def spent(self) -> float:
        b = self.jobs.balance()
        return round(float(b["settledCredits"]) + float(b["reservedCredits"]) - self.baseline, 6)

    def check(self, estimate_usd: float) -> None:
        s = self.spent()
        if s + estimate_usd > self.cap_usd:
            raise BudgetExceeded(f"spent ${s:.3f} + next job up to ${estimate_usd:.3f} > cap ${self.cap_usd:.2f}")


def wait_settled(jobs: Jobs, max_wait_s: int = 900) -> dict:
    """Wait until no credits are reserved, so the next balance delta belongs to one job."""
    t0 = time.time()
    b = None
    while True:
        try:
            b = jobs.balance()
        except (OSError, RuntimeError) as e:  # connection resets here mustn't lose a finished job
            log(f"balance read failed ({type(e).__name__}: {e}); retrying")
            if time.time() - t0 > max_wait_s and b is not None:
                return b
            time.sleep(30)
            continue
        if float(b.get("reservedCredits", 0)) == 0 or time.time() - t0 > max_wait_s:
            return b
        time.sleep(20)


@dataclass
class Outcome:
    ok: bool
    job: str | None = None
    account: dict | None = None
    result: dict | None = None
    jobdef_cid: str | None = None
    attempts: list[dict] = field(default_factory=list)
    result_file: str | None = None


SYSTEM_PROGRAM = "11111111111111111111111111111111"


def run_node(rpc: str, run_address: str | None) -> str | None:
    """The node of a running job, from its run account (job @8, node @40), or None once it's closed.
    The job account shows no node until the job finishes, so this is how a stopped job's node is known."""
    if not run_address:
        return None
    v = (rpc_call(rpc, "getAccountInfo", [run_address, {"encoding": "base64"}]).get("result") or {}).get("value")
    if not v:
        return None
    import base64
    d = base64.b64decode(v["data"][0])
    return b58encode(d[40:72]) if len(d) >= 72 else None


def _f(v):
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def run_batch(*, conn: sqlite3.Connection, run_id: str, tag: str, jobdef: dict, market: str, rpc: str,
              jobs: Jobs, budget: Budget, fixtures: Path, image: str, weights: str, inputs_cid: str,
              node_for_attempt: Callable[[int], str | None] = lambda i: None, max_attempts: int = 3,
              estimate_usd: float = 0.2, queue_wait_s: int = 1200, run_wait_s: int = 3300) -> Outcome:
    fixtures.mkdir(parents=True, exist_ok=True)

    def save(name, obj):
        (fixtures / name).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")

    jd_bytes = canonical(jobdef)
    jd_file = fixtures / f"{tag}_jobdef.json"
    if jd_file.exists() and jd_file.read_bytes() != jd_bytes:
        # a new job definition (e.g. after a fix): keep the old one for the record
        n = len(list(fixtures.glob(f"{tag}_jobdef*.json")))
        jd_file.rename(fixtures / f"{tag}_jobdef_v{n}.json")
    jd_file.write_bytes(jd_bytes)
    pins = pin_both(jd_bytes, f"relay-{run_id}-{tag}.json")
    save(f"{tag}_pin_{pins['cid']}.json", pins)
    cid = pins["cid"]
    log(f"{tag}: job definition {cid} pinned to Nosana's and our Pinata accounts ({len(jd_bytes)} bytes)")
    fetch = Fetcher(rpc)
    out = Outcome(ok=False, jobdef_cid=cid)

    recorded = {r["job"]: dict(r) for r in conn.execute("SELECT * FROM jobs WHERE run_id=? AND tag=?", (run_id, tag))}
    posted = sorted(fixtures.glob(f"{tag}_a*_post.json"), key=lambda p: int(p.name[len(tag) + 2:].split("_")[0]))
    for p in posted:  # earlier attempts of this tag, recorded or not
        n = int(p.name[len(tag) + 2:].split("_")[0])
        saved = json.loads(p.read_text())
        job = saved["body"].get("job")
        if job in recorded:
            row = recorded[job]
        else:
            log(f"{tag}_a{n}: resuming {job}, posted before the script stopped")
            row = _watch_and_record(conn=conn, run_id=run_id, tag=tag, attempt=n, saved=saved,
                                    t_post=p.stat().st_mtime, market=market, rpc=rpc, jobs=jobs, fetch=fetch,
                                    fixtures=fixtures, save=save, image=image, weights=weights,
                                    inputs_cid=inputs_cid, queue_wait_s=queue_wait_s, run_wait_s=run_wait_s)
        out.attempts.append(row)
        if row["ok"] and row["job_definition_cid"] == cid:
            return _adopt(out, row, fixtures, tag, n, rpc)

    for n in range(len(posted) + 1, max_attempts + 1):
        node = node_for_attempt(n)
        bal_before = wait_settled(jobs)
        budget.check(estimate_usd)
        save(f"{tag}_a{n}_balance_before.json", bal_before)
        t_post = time.time()
        s, resp, req = jobs.post(cid, market, 3600, node)
        saved = {"request": req, "http_status": s, "body": resp, "balance_before": bal_before}
        save(f"{tag}_a{n}_post.json", saved)
        if s != 200:
            raise RuntimeError(f"POST /jobs/list: HTTP {s} {resp}")
        log(f"{tag}_a{n}: posted {resp['job']} (node {node or 'any'}), "
            f"reserved ${float((resp.get('credits') or {}).get('costUSD') or 0):.3f}")
        row = _watch_and_record(conn=conn, run_id=run_id, tag=tag, attempt=n, saved=saved, t_post=t_post,
                                market=market, rpc=rpc, jobs=jobs, fetch=fetch, fixtures=fixtures, save=save,
                                image=image, weights=weights, inputs_cid=inputs_cid,
                                queue_wait_s=queue_wait_s, run_wait_s=run_wait_s)
        out.attempts.append(row)
        if row["ok"]:
            return _adopt(out, row, fixtures, tag, n, rpc)
    return out


def _adopt(out: Outcome, row: dict, fixtures: Path, tag: str, n: int, rpc: str) -> Outcome:
    acct = job_from_rpc_response(row["job"], get_account_raw(rpc, row["job"]))
    out.ok, out.job, out.account = True, row["job"], acct.to_dict()
    out.result_file = f"{tag}_a{n}_result.json"
    out.result = json.loads((fixtures / out.result_file).read_text())
    return out


def _watch_and_record(*, conn, run_id, tag, attempt, saved, t_post, market, rpc, jobs: Jobs, fetch: Fetcher,
                      fixtures: Path, save, image, weights, inputs_cid, queue_wait_s, run_wait_s) -> dict:
    pre = f"{tag}_a{attempt}"
    resp, req = saved["body"], saved["request"]
    job, run_addr = resp["job"], resp.get("run")
    bal_before = saved.get("balance_before") or json.loads((fixtures / f"{pre}_balance_before.json").read_text())
    row = {"job": job, "run_id": run_id, "tag": tag, "attempt": attempt, "market": market,
           "requested_node": req.get("node"), "job_definition_cid": req["ipfsHash"], "inputs_cid": inputs_cid,
           "image": image, "model_revision": weights,
           "credits_reserved": float((resp.get("credits") or {}).get("costUSD") or 0),
           "credits_before": Jobs.available_usd(bal_before), "ts": t_post}
    acct = raw = None
    finished = False
    t_running = None
    node_running = None
    err = None
    try:
        while True:
            raw = get_account_raw(rpc, job)
            acct = job_from_rpc_response(job, raw)
            if acct and acct.state in (2, 3):
                finished = acct.state == 2 and acct.ipfs_result is not None
                break
            if t_running is None and run_account_exists(rpc, run_addr):
                t_running = time.time()
                try:
                    node_running = run_node(rpc, run_addr)
                except Exception:  # noqa: BLE001
                    node_running = None
                log(f"{pre}: running on {node_running} (picked up {t_running - t_post:.0f}s after posting)")
            if t_running is None and time.time() - t_post > queue_wait_s:
                raise TimeoutError(f"not picked up within {queue_wait_s}s")
            if t_running is not None and time.time() - t_running > run_wait_s:
                raise TimeoutError(f"still running after {run_wait_s}s")
            time.sleep(20)
    except Exception as e:  # noqa: BLE001 - recorded; the job is stopped below
        err = f"{type(e).__name__}: {e}"
        log(f"{pre}: {err}")
    finally:
        if not finished:
            s2, stop = jobs.stop(job)
            save(f"{pre}_stop.json", {"http_status": s2, "body": stop})
            log(f"{pre}: stopped {job}: HTTP {s2} {stop.get('outcome')}")
    if raw is not None:
        save(f"{pre}_account.json", raw)
    row.update({"post_to_running_s": round(t_running - t_post, 1) if t_running else None,
                "state": acct.state_name if acct else "NOT_FOUND",
                "node": acct.node if acct and acct.node != SYSTEM_PROGRAM else node_running,
                "listed_at": acct.time_start if acct else None, "finished_at": acct.time_end if acct else None,
                "result_cid": acct.ipfs_result if acct else None, "error": err, "ok": 0})
    if finished:
        try:
            data = fetch.ipfs(acct.ipfs_result)
            (fixtures / f"{pre}_result.json").write_bytes(data)
            result = json.loads(data)
            rc = check_result(result)
            cs, ce = container_times(result)
            imp, load = _f(regex_result(result, "import_s")), _f(regex_result(result, "load_s"))
            dl = regex_result(result, "deadline")
            row.update({"result_status": result.get("status"), "ok": int(rc["ok"]),
                        "error": None if rc["ok"] else "; ".join(rc["reasons"]) + (f"; deadline {dl}" if dl else ""),
                        "container_start": cs, "container_end": ce, "import_s": imp, "load_s": load,
                        "gen_s": _f(regex_result(result, "gen_s")),
                        "download_s": _f(regex_result(result, "download_s")),
                        "startup_s": round(imp + load, 1) if imp is not None and load is not None else None})
        except Unavailable as e:
            row["error"] = f"result unavailable: {e}"
    bal_after = wait_settled(jobs)
    save(f"{pre}_balance_after.json", bal_after)
    row["credits_after"] = Jobs.available_usd(bal_after)
    upsert(conn, "jobs", row, "job")
    conn.commit()
    log(f"{pre}: {'OK' if row['ok'] else 'FAILED'} node={row['node']} status={row.get('result_status')} "
        f"cost=${row['credits_before'] - row['credits_after']:.3f} error={row.get('error')}")
    return row

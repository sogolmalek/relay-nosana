"""Relay diagnostic runner (Phase 1b): why did the Phase 1 batch give one answer for every payout?

Runs inside the same vLLM image as batch_runner/run.py, on a handful of input lines. For each item:
  1. checks the decoded inputs (gzip + base64 env) against the SHA-256 the job definition commits to,
  2. prints the exact prompt after the model's chat template (tokenizer.apply_chat_template),
  3. generates with the Phase 1 settings (llm.chat, guided JSON) and without guided decoding,
     and once more through llm.generate on the rendered prompt (same text, other code path),
  4. prints the top-5 log-probabilities of the first output tokens (raw model distribution).
Stdout lines start with RELAY_DIAG so they can be read back from the node-pinned result.
"""

import base64
import gzip
import hashlib
import json
import os
import sys
import threading
import time


def p(tag, obj):
    print("RELAY_DIAG " + tag + " " + json.dumps(obj, ensure_ascii=False, sort_keys=True), flush=True)


def top(logprobs, n_tokens):
    out = []
    for step in (logprobs or [])[:n_tokens]:
        ranked = sorted(step.values(), key=lambda lp: -lp.logprob)[:5]
        out.append([[lp.decoded_token, round(lp.logprob, 3)] for lp in ranked])
    return out


def _deadline(t0, s):
    while True:
        time.sleep(30)
        if time.time() - t0 > s:
            print("RELAY_DEADLINE elapsed_s=%.0f" % (time.time() - t0), flush=True)
            os._exit(3)


def main():
    t0 = time.time()
    threading.Thread(target=_deadline, args=(t0, float(os.environ.get("RELAY_DEADLINE_S", "1200"))), daemon=True).start()
    raw = gzip.decompress(base64.b64decode(os.environ["RELAY_INPUTS_GZ_B64"]))
    inputs = [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]
    want = os.environ.get("RELAY_EXPECT_INPUTS_SHA256")
    got = hashlib.sha256(raw).hexdigest()
    p("inputs", {"sha256": got, "expected": want, "match": got == want, "count": len(inputs),
                 "item_ids": [x["item_id"] for x in inputs],
                 "line_sha256": [hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()[:16] for x in inputs]})
    schema = json.loads(base64.b64decode(os.environ["RELAY_OUTPUT_SCHEMA_B64"]))
    from vllm import LLM, SamplingParams
    from vllm.sampling_params import GuidedDecodingParams
    import vllm
    model, revision = os.environ["MODEL"], os.environ["REVISION"]
    llm = LLM(model=model, revision=revision, tokenizer_revision=revision, max_model_len=4096, seed=0,
              enforce_eager=True)
    p("engine", {"vllm": vllm.__version__, "load_s": round(time.time() - t0, 1), "model": model, "revision": revision,
                 "guided_backend": str(getattr(getattr(llm.llm_engine, "vllm_config", None), "decoding_config", None))})
    tok = llm.get_tokenizer()
    rendered = [tok.apply_chat_template(x["messages"], tokenize=False, add_generation_prompt=True) for x in inputs]
    for x, r in zip(inputs, rendered):
        ids = tok(r, add_special_tokens=False)["input_ids"]
        p("prompt", {"item_id": x["item_id"], "tokens": len(ids), "rendered": r})
    convs = [x["messages"] for x in inputs]

    def sp(x, guided):
        kw = dict(temperature=x["params"]["temperature"], max_tokens=x["params"]["max_tokens"], seed=x["params"]["seed"],
                  logprobs=5)
        if guided:
            kw["guided_decoding"] = GuidedDecodingParams(json=schema)
        else:
            kw["max_tokens"] = 160
        return SamplingParams(**kw)

    for mode, guided, path in (("chat_guided", True, "chat"), ("chat_free", False, "chat"),
                               ("generate_guided", True, "generate")):
        params = [sp(x, guided) for x in inputs]
        outs = (llm.chat(convs, params, use_tqdm=False) if path == "chat"
                else llm.generate(rendered, params, use_tqdm=False))
        for x, o in zip(inputs, outs):
            c = o.outputs[0]
            p(mode, {"item_id": x["item_id"], "prompt_tokens": len(o.prompt_token_ids or []), "text": c.text,
                     "finish": c.finish_reason, "top5_first_tokens": top(c.logprobs, 12)})
    print("RELAY_DIAG_DONE=1", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

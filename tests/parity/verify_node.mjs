// Run the browser verifier (docs/app/verifier.js) in Node over recorded fixtures.
//   node tests/parity/verify_node.mjs cases.json cache.json  ->  JSON [{name, verdict, checks}]
import { readFileSync } from "node:fs";
import { verify, verdict, jobFromRpcResponse, Unavailable } from "../../docs/app/verifier.js";

const [casesPath, cachePath] = process.argv.slice(2);
const cases = JSON.parse(readFileSync(casesPath, "utf8"));
const cache = JSON.parse(readFileSync(cachePath, "utf8"));

class CacheFetcher {
  rpc = "cache://rpc";
  async transaction(sig) {
    if (!(sig in cache.tx)) throw new Unavailable(`not in cache: tx ${sig}`);
    return cache.tx[sig];
  }
  async account(address) {
    if (!(address in cache.acct)) throw new Unavailable(`not in cache: account ${address}`);
    return jobFromRpcResponse(address, cache.acct[address]);
  }
  async ipfs(cid) {
    if (!(cid in cache.ipfs)) throw new Unavailable(`not in cache: ipfs ${cid}`);
    return Uint8Array.from(Buffer.from(cache.ipfs[cid], "base64"));
  }
}

const out = [];
for (const [name, receipt] of cases) {
  const checks = await verify(receipt, new CacheFetcher());
  out.push({ name, verdict: verdict(checks), checks });
}
process.stdout.write(JSON.stringify(out));

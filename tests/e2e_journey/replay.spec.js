// The POC journey in replay mode (docs/POC_UI_JOURNEY.md, acceptance 2). Free: no Nosana credits, no devnet
// transactions; it reads mainnet, devnet and IPFS from the browser exactly as a reviewer would.
const { test, expect } = require("@playwright/test");
const { mkdirSync, writeFileSync, readFileSync } = require("node:fs");
const path = require("node:path");

const OUT = path.join(__dirname, "../../out/e2e/replay");
mkdirSync(OUT, { recursive: true });
const summary = { app: process.env.APP_URL || "local", started: new Date().toISOString(), runs: {} };
const shot = (page, name) => page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true });

const TAMPERS = [  // [button test id, failing check it must produce]
  ["tamper-change-the-decision", "5"], ["tamper-change-the-decision-leave-the-id", "receipt_id"],
  ["tamper-change-the-model", "3"], ["tamper-swap-the-job", "1"], ["tamper-point-at-another-job-s-post", "1"],
  ["tamper-edit-a-merkle-hash", "5"], ["tamper-change-the-node", "6"],
];

// The page is tabbed: one step per tab. A step's controls can be used only while its tab is showing.
const TABS = ["guide", "run", "route", "job", "receipts", "verify", "review", "pay"];
const tab = (page, key) => page.getByTestId(`tab-${key}`).click();
const showing = (page) => page.evaluate(() =>
  [...document.querySelectorAll("[data-panel]")].filter((p) => p.getClientRects().length).map((p) => p.dataset.panel));
// "open" sits in the Receipts tab and switches to Verify & tamper
async function openReceipt(page, id) {
  await tab(page, "receipts");
  await page.getByTestId(`open-${id}`).click();
  await expect(page.locator("#detail-title")).toContainText(id);
  expect(await showing(page)).toEqual(["verify"]);
}

async function verifyAll(page, n) {
  await tab(page, "receipts");
  await page.getByTestId("verify-all").click();
  await expect(page.getByTestId("verify-all-status")).toHaveText(`${n} of ${n} receipts verified in this browser`);
}

async function tamperAll(page, item) {
  const out = {};
  for (const [id, check] of TAMPERS) {
    await page.getByTestId(id).click();
    const res = page.getByTestId("tamper-result");
    await expect(res).toContainText(await page.getByTestId(id).innerText());
    await expect(page.getByTestId(id)).toBeEnabled();
    const failing = (await res.getAttribute("data-failing")).split(",");
    expect(failing, `${item} ${id}`).toContain(check);
    expect(await res.getAttribute("data-gate")).toBe("refuse");
    out[id] = failing;
  }
  return out;
}

test("Phase 1 replay: verify, tamper, gate, devnet payouts", async ({ page }) => {
  const s = (summary.runs["payouts-2026-11-10"] = {});
  await page.goto("?run=payouts-2026-11-10");
  await expect(page.getByTestId("replay-banner")).toContainText("Replay of a real run");
  expect(await showing(page)).toEqual(["guide"]);
  await tab(page, "job");
  await expect(page.getByTestId("run-live")).toBeDisabled();
  await verifyAll(page, 30);
  s.verified = "30/30";
  await openReceipt(page, "P-0101");
  await page.getByTestId("verify-one").click();
  await expect(page.locator("#checks li")).toHaveCount(8);
  expect(await page.locator('#checks li[data-ok="true"]').count()).toBe(8);
  await shot(page, "p1-verify-P-0101");
  s.tampers = await tamperAll(page, "P-0101");
  await shot(page, "p1-tamper");

  // a declared receipt used for a payout over $500: verifies as declared, refused by the rule
  await openReceipt(page, "P-0102");
  await page.getByTestId("tamper-use-this-declared-receipt-for-a-2-400-payout").click();
  const res = page.getByTestId("tamper-result");
  await expect(res).toHaveAttribute("data-gate", "refuse");
  expect(await res.getAttribute("data-failing")).toBe("");
  await expect(res).toContainText("requires an anchored receipt");
  s.declared_over_500 = "refused by the rule";

  await tab(page, "review");
  await expect(page.getByTestId("bar-note")).toBeVisible();
  await expect(page.getByTestId("bar-note")).toContainText("held: the open model missed the bar");
  await expect(page.getByTestId("bar-note")).toContainText("4/30");
  await expect(page.locator('[data-testid^="queue-"]')).toHaveCount(21);

  const payouts = JSON.parse(readFileSync(path.join(__dirname, "../../docs/app/runs/payouts-2026-11-10/payouts.json")));
  const sent = payouts.items.filter((x) => x.tx);
  await tab(page, "pay");
  if (sent.length) {
    await page.getByTestId("check-payouts").click();
    await expect(page.getByTestId("payouts-status")).toHaveText(
      `${sent.length} of ${sent.length} devnet payouts checked in this browser: each memo is the receipt_id of a verified approve receipt.`);
    s.devnet = `${sent.length}/${sent.length} memos = receipt_id`;
  } else {
    s.devnet = "no devnet payouts sent yet (payer unfunded)";
  }
  await shot(page, "p1-pay");
});

test("run b replay: review queue holds P-0128, receipts verify", async ({ page }) => {
  const s = (summary.runs["payouts-2026-11-10b"] = {});
  await page.goto("?run=payouts-2026-11-10b#review");
  const q = page.getByTestId("queue-P-0128");
  await expect(q).toBeVisible();
  await expect(q).toContainText("re-run disagreed");
  await expect(q).toContainText("held: the open model missed the bar");
  await expect(q).toContainText("23/60");
  await expect(page.locator('[data-testid^="queue-"]').first()).toHaveAttribute("data-payout", "P-0128");
  await expect(page.locator('[data-testid^="queue-"]')).toHaveCount(42);
  s.queue = "42, P-0128 first with the re-run reason";
  await verifyAll(page, 42);
  s.verified = "42/42";
  await openReceipt(page, "P-0128");
  s.tampers = await tamperAll(page, "P-0128");
  await shot(page, "b-queue-and-tamper");
});

test("tabs: the guide first, then one step at a time", async ({ page }) => {
  await page.goto("?run=payouts-2026-11-10");
  await expect(page.locator("#receipts tbody tr")).toHaveCount(30);
  expect(await showing(page)).toEqual(["guide"]);
  await expect(page.getByTestId("guide")).toContainText("a payout over $500 needs proof");
  await expect(page.getByTestId("tab-guide")).toHaveAttribute("aria-selected", "true");
  await shot(page, "tabs-guide");

  // the tour: Start, then Next through every step, in order
  await page.getByTestId("start-tour").click();
  expect(await showing(page)).toEqual(["run"]);
  for (const key of TABS.slice(2)) {
    await page.getByTestId("tab-next").click();
    expect(await showing(page)).toEqual([key]);
    expect(page.url()).toContain(`#${key}`);
  }
  await expect(page.getByTestId("tab-next")).toBeHidden();
  await expect(page.getByTestId("pager-pos")).toHaveText("Step 7 of 7");
  await page.getByTestId("tab-prev").click();
  expect(await showing(page)).toEqual(["review"]);

  // Verify & tamper says what to do until a receipt is open
  await tab(page, "verify");
  await expect(page.getByTestId("verify-empty")).toBeVisible();
  await expect(page.locator("#s6")).toBeHidden();
  await openReceipt(page, "P-0101");
  await expect(page.getByTestId("verify-empty")).toBeHidden();
  await expect(page.getByTestId("verify-one")).toBeVisible();

  // keyboard, a link straight to a step, and a run change that keeps the step
  await page.getByTestId("tab-verify").focus();
  await page.keyboard.press("ArrowRight");
  expect(await showing(page)).toEqual(["review"]);
  await page.getByTestId("run-picker").selectOption("payouts-2026-11-10b");
  await expect(page.getByTestId("queue-P-0128")).toBeVisible();
  expect(page.url()).toContain("?run=payouts-2026-11-10b#review");
  await page.goto("?run=payouts-2026-11-10b#pay");
  await expect(page.locator("#receipts tbody tr")).toHaveCount(60);
  expect(await showing(page)).toEqual(["pay"]);
  summary.tabs = "guide first; one step per tab; Next/Back, keyboard, #step links";
});

test("phone width: no horizontal scroll on any tab", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("?run=payouts-2026-11-10b");
  await expect(page.getByTestId("review-queue")).toContainText("P-0128");
  const fits = async (what) => {
    const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    expect(sw, what).toBeLessThanOrEqual(iw);
  };
  for (const key of TABS) {
    await tab(page, key);
    expect(await showing(page)).toEqual([key]);
    await fits(key);
  }
  await openReceipt(page, "P-0128");
  await fits("an open receipt");
  await shot(page, "phone");
  summary.phone = "no horizontal scroll at 375 px, on every tab";
});

test.afterAll(() => {
  summary.finished = new Date().toISOString();
  writeFileSync(path.join(OUT, "summary.json"), JSON.stringify(summary, null, 1) + "\n");
});

// Tabs for the POC app: one step per tab instead of one long page. No dependencies.
// app.js is untouched: it keeps filling the same element ids, and this file only decides which panel shows.
// Without this script every panel is visible, so the page still works as one long page.

const tabs = [...document.querySelectorAll('#tabs [role="tab"]')];
const order = tabs.map((t) => t.dataset.tab);
const panels = Object.fromEntries([...document.querySelectorAll("[data-panel]")].map((p) => [p.dataset.panel, p]));
const strip = document.getElementById("tabs");
const prev = document.getElementById("tab-prev");
const next = document.getElementById("tab-next");
const pos = document.getElementById("pager-pos");
const pager = document.getElementById("pager");
const name = (key) => tabs[order.indexOf(key)].lastChild.textContent.trim();
let current = null;

function show(key, { focus = false } = {}) {
  if (!(key in panels)) key = order[0];
  current = key;
  const i = order.indexOf(key);
  tabs.forEach((t, j) => {
    t.setAttribute("aria-selected", j === i ? "true" : "false");
    t.tabIndex = j === i ? 0 : -1;
  });
  for (const [k, p] of Object.entries(panels)) p.classList.toggle("off", k !== key);

  // keep the active tab in view inside the strip, without moving the page sideways
  const t = tabs[i];
  strip.scrollLeft = Math.max(0, t.offsetLeft - (strip.clientWidth - t.offsetWidth) / 2);
  if (focus) t.focus({ preventScroll: true });

  pager.hidden = i === 0;  // the guide has its own "Start the tour" button
  next.hidden = i === order.length - 1;
  if (i > 0) prev.textContent = `← ${name(order[i - 1])}`;
  if (i < order.length - 1) next.textContent = `Next: ${name(order[i + 1])} →`;
  pos.textContent = `Step ${i} of ${order.length - 1}`;

  history.replaceState(null, "", `${location.pathname}${location.search}#${key}`);
  // a new step starts at its top: if the page is scrolled past the tab bar, come back to it
  const top = document.getElementById("tabs-top").getBoundingClientRect().top + window.scrollY;
  if (window.scrollY > top) window.scrollTo({ top });
}

const fromHash = () => location.hash.slice(1);

strip.addEventListener("click", (e) => {
  const t = e.target.closest('[role="tab"]');
  if (t) show(t.dataset.tab);
});
strip.addEventListener("keydown", (e) => {
  const i = order.indexOf(current);
  const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: order.length - 1 }[e.key];
  if (to === undefined || to < 0 || to >= order.length) return;
  e.preventDefault();
  show(order[to], { focus: true });
});
prev.addEventListener("click", () => show(order[order.indexOf(current) - 1]));
next.addEventListener("click", () => show(order[order.indexOf(current) + 1]));

// Capture phase: app.js re-renders the receipts table inside its own click handler, which detaches the
// "open" button before the event would bubble up to here.
document.addEventListener("click", (e) => {
  const target = e.target instanceof Element ? e.target : null;
  if (!target) return;
  const go = target.closest("[data-goto]");
  if (go) show(go.dataset.goto);
  else if (target.closest('[data-testid^="open-"]')) show("verify");
}, true);

window.addEventListener("hashchange", () => { if (fromHash() !== current) show(fromHash()); });
// app.js rewrites the URL to ?run=… when the run changes; put the tab back after it
document.getElementById("run").addEventListener("change", () => setTimeout(() => show(current), 0));

document.querySelector(".app").classList.add("tabbed");
show(fromHash() || order[0]);

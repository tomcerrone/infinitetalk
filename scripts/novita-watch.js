#!/usr/bin/env node
// Relevé périodique de la dispo/prix GPU Novita (5090 spot + on-demand) ET du
// stock RunPod 5090/Blackwell — pour comparer la liquidité réelle des deux
// fournisseurs sur plusieurs jours AVANT de décider du multi-provider
// (Tom 2026-06-10 : "checker tous les jours pendant 4-5 jours").
// Lancé par une tâche planifiée Windows (NovitaSpotWatch, toutes les 30 min).
// Append une ligne JSON par relevé dans outputs/novita-spot-watch.log. Lecture
// seule sur les deux APIs : ZÉRO coût.
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const LOG = path.join(ROOT, "outputs", "novita-spot-watch.log");
const env = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
const get = (k) => (env.match(new RegExp("^" + k + "=(.*)$", "m")) || [])[1]?.trim();
const NOVITA = get("NOVITA_API_KEY");
const RUNPOD = get("RUNPOD_API_KEY");

async function novita() {
  const out = { spot: {}, od: {} };
  for (const [mode, key] of [["spot", "spot"], ["onDemand", "od"]]) {
    const r = await fetch(`https://api.novita.ai/gpu-instance/openapi/v1/products?billingMethod=${mode}`, {
      headers: { Authorization: `Bearer ${NOVITA}` }, signal: AbortSignal.timeout(15000),
    });
    // Sans ce check, un 401 (clé expirée) renvoie un JSON sans .data → {} =
    // indiscernable de "aucun 5090 au catalogue". Ce log alimente une décision
    // sur 4-5 jours : une erreur DOIT apparaître comme "ERR", pas comme stock 0.
    if (!r.ok) throw new Error(`Novita HTTP ${r.status}`);
    const j = await r.json();
    for (const p of j.data || []) {
      if (!/5090/i.test(p.name)) continue;
      out[key][p.id] = `${p.inventoryState || "?"}${p.availableDeploy ? "" : "!"}:${((mode === "spot" ? p.spotPrice : p.price) / 100000).toFixed(2)}`;
    }
  }
  return out;
}

async function runpod() {
  // uninterruptablePrice = on-demand ; minimumBidPrice = SPOT/interruptible (le
  // levier "~½ prix" qu'on évalue, Tom 2026-06-11). On logge les deux + le stock
  // pour mesurer sur plusieurs jours si le spot 5090 est dispo et à quel prix.
  const r = await fetch(`https://api.runpod.io/graphql?api_key=${RUNPOD}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: `query{ gpuTypes{ id lowestPrice(input:{gpuCount:1}){ stockStatus uninterruptablePrice minimumBidPrice } } }` }),
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`RunPod HTTP ${r.status}`);
  const j = await r.json();
  const watch = ["NVIDIA GeForce RTX 5090", "NVIDIA RTX PRO 4500 Blackwell", "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition"];
  const out = {};
  for (const g of j?.data?.gpuTypes || []) {
    if (!watch.includes(g.id)) continue;
    const lp = g.lowestPrice || {};
    // format : "<stock>:od=<onDemand>:spot=<bid>"
    out[g.id.replace("NVIDIA ", "").replace(" Blackwell", "").replace(" Workstation Edition", "")] =
      `${lp.stockStatus || "none"}:od=${lp.uninterruptablePrice ?? "-"}:spot=${lp.minimumBidPrice ?? "-"}`;
  }
  return out;
}

(async () => {
  const entry = { t: new Date().toISOString() };
  try { entry.novita = await novita(); } catch (e) { entry.novita = "ERR " + String(e).slice(0, 60); }
  try { entry.runpod = await runpod(); } catch (e) { entry.runpod = "ERR " + String(e).slice(0, 60); }
  fs.mkdirSync(path.dirname(LOG), { recursive: true });
  fs.appendFileSync(LOG, JSON.stringify(entry) + "\n");
  console.log("ok", entry.t);
})();

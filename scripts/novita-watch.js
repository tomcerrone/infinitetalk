#!/usr/bin/env node
// Sonde multi-fournisseurs GPU (Tom 2026-06-11 : « mettons aussi CloudRift et
// Shadeform sous surveillance, décision dans quelques jours ») : dispo/prix du
// 5090 et des Blackwell ≥32Go chez Novita (spot+OD), RunPod (OD+spot),
// CloudRift (API publique sans clé !) et Shadeform (s'active si
// SHADEFORM_API_KEY est posée dans .env — compte gratuit à créer par Tom).
// Lancé par la tâche planifiée Windows NovitaSpotWatch (toutes les 30 min).
// Append une ligne JSON par relevé dans outputs/novita-spot-watch.log (MÊME
// fichier qu'avant pour garder l'historique continu). Lecture seule : ZÉRO coût.
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const LOG = path.join(ROOT, "outputs", "novita-spot-watch.log");
const env = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
const get = (k) => (env.match(new RegExp("^" + k + "=(.*)$", "m")) || [])[1]?.trim();
const NOVITA = get("NOVITA_API_KEY");
const RUNPOD = get("RUNPOD_API_KEY");
const SHADEFORM = get("SHADEFORM_API_KEY");

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

async function cloudrift() {
  // API PUBLIQUE (vérifiée 2026-06-11 : répond sans clé). Prix en cents/h.
  // On ne garde que les variantes 1×GPU des cartes qui nous intéressent
  // (≥32Go : 5090, PRO 6000 (+Max-Q), L40S). avail = nœuds louables now.
  const r = await fetch("https://api.cloudrift.ai/api/v1/instance-types/list", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version: "~upcoming", data: {} }),
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) throw new Error(`CloudRift HTTP ${r.status}`);
  const j = await r.json();
  const out = {};
  for (const t of j?.data?.instance_types || []) {
    const brand = t.brand_short || "";
    if (!/5090|PRO 6000|L40S/i.test(brand)) continue;
    for (const v of t.variants || []) {
      if (v.gpu_count !== 1) continue;
      out[`${brand}|${t.name}`] = `${v.available_nodes ?? 0}x:${(v.cost_per_hour / 100).toFixed(2)}`;
    }
  }
  return out;
}

async function shadeform() {
  // Agrégateur 30+ clouds — nécessite une clé (compte gratuit). Tant que
  // SHADEFORM_API_KEY n'est pas dans .env, on logge "no-key" (pas une erreur).
  if (!SHADEFORM) return "no-key";
  const r = await fetch("https://api.shadeform.ai/v1/instances/types?sort=price&available=true", {
    headers: { "X-API-KEY": SHADEFORM }, signal: AbortSignal.timeout(20000),
  });
  if (!r.ok) throw new Error(`Shadeform HTTP ${r.status}`);
  const j = await r.json();
  const out = {};
  for (const t of j?.instance_types || []) {
    const gpu = String(t.gpu_type || "");
    if (!/5090|6000|L40S|A100/i.test(gpu)) continue;
    if ((t.num_gpus ?? t.gpu_count ?? 1) !== 1) continue;
    const price = ((t.hourly_price ?? 0) / 100).toFixed(2);
    const regions = (t.availability || []).filter((a) => a.available).length;
    const key = `${t.cloud || "?"}|${gpu}`;
    // sorted by price → la 1re occurrence par clé = la moins chère.
    if (!(key in out)) out[key] = `${regions}r:${price}`;
  }
  return out;
}

(async () => {
  const entry = { t: new Date().toISOString() };
  try { entry.novita = await novita(); } catch (e) { entry.novita = "ERR " + String(e).slice(0, 60); }
  try { entry.runpod = await runpod(); } catch (e) { entry.runpod = "ERR " + String(e).slice(0, 60); }
  try { entry.cloudrift = await cloudrift(); } catch (e) { entry.cloudrift = "ERR " + String(e).slice(0, 60); }
  try { entry.shadeform = await shadeform(); } catch (e) { entry.shadeform = "ERR " + String(e).slice(0, 60); }
  fs.mkdirSync(path.dirname(LOG), { recursive: true });
  fs.appendFileSync(LOG, JSON.stringify(entry) + "\n");
  console.log("ok", entry.t);
})();

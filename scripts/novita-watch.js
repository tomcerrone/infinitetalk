#!/usr/bin/env node
// Sonde multi-fournisseurs GPU (Tom 2026-06-11 puis élargie 2026-06-12 :
// « avoir 3-4 plateformes de côté pour toujours avoir le meilleur coût/vidéo »).
// Dispo/prix du 5090 (+ 4090 piste low-cost) chez :
//   - Novita    (spot + on-demand)            — clé NOVITA_API_KEY
//   - RunPod    (on-demand + spot, fournisseur prod actuel) — clé RUNPOD_API_KEY
//   - CloudRift (API publique sans clé)
//   - Vast.ai   (API publique sans clé — gros stock 5090 verified, candidat #1)
//   - Clore     (API publique sans clé — communautaire crypto, très bon marché)
//   - Akash     (API publique sans clé — décentralisé, stock 5090 souvent nul)
//   - Shadeform (s'active si SHADEFORM_API_KEY posée — compte gratuit à créer)
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

async function vast() {
  // API PUBLIQUE SANS CLÉ (vérifiée 2026-06-12) : marketplace avec du VRAI stock
  // 5090 quand tout le reste est vide. On ne garde que les offres 1×GPU louables,
  // séparées on-demand (is_bid=false) vs enchère/spot, et on distingue les
  // "verified" (machines auditées par Vast ≈ datacenter-grade) des communautaires.
  // Le coût bande passante (inet_*_cost en $/GB) est un frais caché à suivre car
  // notre boot télécharge des modèles lourds — on logge le download médian verified.
  const probe = async (gpuName) => {
    const q = { rentable: { eq: true }, num_gpus: { eq: 1 }, gpu_name: { eq: gpuName }, order: [["dph_total", "asc"]], limit: 800 };
    const r = await fetch("https://console.vast.ai/api/v0/search/asks/?q=" + encodeURIComponent(JSON.stringify(q)), {
      signal: AbortSignal.timeout(20000),
    });
    if (!r.ok) throw new Error(`Vast HTTP ${r.status}`);
    const j = await r.json();
    const all = (j.offers || []).filter((o) => o.num_gpus === 1 && o.rentable);
    const od = all.filter((o) => o.is_bid === false);
    const verif = od.filter((o) => o.verification === "verified").sort((a, b) => a.dph_total - b.dph_total);
    if (!verif.length) return `v0/od${od.length}`;
    const p = verif.map((o) => o.dph_total);
    const med = p[Math.floor(p.length / 2)];
    const bwDown = verif.map((o) => o.inet_down_cost || 0).sort((a, b) => a - b);
    // format : "v<nbVerified>/od<nbOnDemand>:<min>-<med>:le69=<n>:bw<dlMed$/GB>"
    return `v${verif.length}/od${od.length}:${p[0].toFixed(2)}-${med.toFixed(2)}:le69=${p.filter((x) => x <= 0.69).length}:bw${bwDown[Math.floor(bwDown.length / 2)].toFixed(4)}`;
  };
  return { "5090": await probe("RTX 5090"), "4090": await probe("RTX 4090") };
}

async function clore() {
  // API PUBLIQUE SANS CLÉ (vérifiée 2026-06-12 : header 'auth' documenté mais non
  // exigé en pratique). Marketplace COMMUNAUTAIRE crypto (machines de mineurs) :
  // beaucoup de stock 5090 très bon marché MAIS prix en USD/JOUR payés en
  // crypto/stablecoin, fiabilité hétérogène → on ne compte que rel≥0.9. On
  // convertit en $/h (÷24) pour comparer aux autres fournisseurs.
  const r = await fetch("https://api.clore.ai/v1/marketplace", { signal: AbortSignal.timeout(20000) });
  if (!r.ok) throw new Error(`Clore HTTP ${r.status}`);
  const j = await r.json();
  const out = {};
  for (const [label, rx] of [["5090", /5090/i], ["4090", /4090/i]]) {
    let tot = 0;
    const cheapH = [];
    for (const s of j.servers || []) {
      if (!rx.test(String(s.specs?.gpu || ""))) continue;
      tot++;
      const usd = s.price?.on_demand?.["USD-Blockchain"];
      if (!s.rented && usd && (s.reliability ?? 0) >= 0.9) cheapH.push(usd / 24);
    }
    cheapH.sort((a, b) => a - b);
    // format : "<nbFiablesLibres>/<total>:<min$/h>-<med$/h>"
    out[label] = cheapH.length
      ? `${cheapH.length}/${tot}:${cheapH[0].toFixed(2)}-${cheapH[Math.floor(cheapH.length / 2)].toFixed(2)}`
      : `0/${tot}`;
  }
  return out;
}

async function akash() {
  // API PUBLIQUE SANS CLÉ (vérifiée 2026-06-12) — alimente la page pricing
  // officielle. Réseau décentralisé : prix attractif mais stock 5090 souvent nul
  // (déploiement via SDL/blockchain = intégration lourde, on sonde surtout le prix).
  const r = await fetch("https://console-api.akash.network/v1/gpu-prices", { signal: AbortSignal.timeout(15000) });
  if (!r.ok) throw new Error(`Akash HTTP ${r.status}`);
  const j = await r.json();
  const list = j.models || (Array.isArray(j) ? j : []);
  const out = {};
  for (const g of list) {
    const model = String(g.model || "").toLowerCase();
    const label = /5090/.test(model) ? "5090" : /4090/.test(model) ? "4090" : null;
    if (!label) continue;
    const av = g.availability || {};
    const pr = g.price || {};
    // format : "<dispo>/<total>:<med$/h>" ; garde la variante la mieux dispo.
    const cand = `${av.available ?? 0}/${av.total ?? 0}:${(pr.med ?? pr.avg ?? pr.min ?? 0).toFixed(2)}`;
    if (!(label in out) || (av.available ?? 0) > Number((out[label].split("/")[0]))) out[label] = cand;
  }
  return out;
}

(async () => {
  const entry = { t: new Date().toISOString() };
  try { entry.novita = await novita(); } catch (e) { entry.novita = "ERR " + String(e).slice(0, 60); }
  try { entry.runpod = await runpod(); } catch (e) { entry.runpod = "ERR " + String(e).slice(0, 60); }
  try { entry.cloudrift = await cloudrift(); } catch (e) { entry.cloudrift = "ERR " + String(e).slice(0, 60); }
  try { entry.vast = await vast(); } catch (e) { entry.vast = "ERR " + String(e).slice(0, 60); }
  try { entry.clore = await clore(); } catch (e) { entry.clore = "ERR " + String(e).slice(0, 60); }
  try { entry.akash = await akash(); } catch (e) { entry.akash = "ERR " + String(e).slice(0, 60); }
  try { entry.shadeform = await shadeform(); } catch (e) { entry.shadeform = "ERR " + String(e).slice(0, 60); }
  fs.mkdirSync(path.dirname(LOG), { recursive: true });
  fs.appendFileSync(LOG, JSON.stringify(entry) + "\n");
  console.log("ok", entry.t);
})();

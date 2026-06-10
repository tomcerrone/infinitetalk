# InfiniteTalk — Service vidéo talking-head pour MassContent

Service auto-hébergé (RunPod) qui anime **une image + un audio → vidéo 720p talking-head**,
en remplacement de HeyGen dans la pipeline MassContent.

## État (2026-06-10) : PROD ACTIVE — validée E2E à l'échelle
- ✅ Qualité, lip-sync, stabilité couleur, naturel du mouvement : validés par Tom.
- ✅ Coût : **~0,25 $/vidéo** 720p native (RIFE x2 → 50 fps).
- ✅ Chaîne complète en prod : orchestrateur MassContent → pods RunPod → S3 → montage Remotion.

## Architecture prod (actuelle)

Ce repo est **public** (requis : clone anonyme par les pods) : chaque pod clone `main` au boot
(pas de secret committé — tout vient des env). Conséquence : **tout push sur `main` part en prod
immédiatement** sur les prochains pods (pas de pin de version) ; renommer/déplacer `scripts/boot.sh`
impose de mettre à jour le `DOCKER_START_CMD` côté MassContent.

1. **Orchestrateur = côté MassContent** (`src/lib/runpod/runpod.ts` + cron `runpod-orchestrator`) :
   crée les pods par l'API REST RunPod **inline** (imageName + ports 8188,8189 + `dockerStartCmd`),
   **sans template RunPod** (abandonné : il perdait le dockerStartCmd et son volumeMountPath
   bloquait l'image custom). Le code versionné MassContent est l'unique source de vérité du
   dockerStartCmd et des env injectées (`MASSCONTENT_BASE_URL`, `PIPELINE_SECRET`,
   `RUNPOD_API_KEY`, `IDLE_EXIT_SECONDS`, `SAGE_ARCH`, overrides `IT_*` par GPU).
2. **dockerStartCmd** (sur le pod) : lance un **serveur de logs HTTP sur 8189**
   (`https://<podId>-8189.proxy.runpod.net/` → clone.log / boot.log / comfyui.log / worker.log,
   pas de SSH nécessaire), clone ce repo, puis exécute `scripts/boot.sh`.
3. **Chaîne sur le pod** : `boot.sh` → `setup-prod.sh` → `worker.py` → `generate.py` (détail ci-dessous).
4. **Sortie** : upload S3 via **URL presignée PUT** fournie par MassContent au claim
   (zéro credential AWS sur le pod), puis `/complete` → montage Remotion côté MassContent.

### Image custom (cold-start ~2× plus court)
`ghcr.io/tomcerrone/infinitetalk-comfy:latest` — construite par `.github/workflows/build-image.yml`
(déclenchée quand `Dockerfile` / `setup-prod.sh` changent). Le Dockerfile exécute
`setup-prod.sh` en mode `IT_ENV_ONLY=1` (source unique de vérité) : l'image embarque
**ComfyUI + 4 custom nodes + venv + SageAttention compilée sm_120**, mais **PAS les modèles
(33 Go)** — un pull d'image de 45 Go serait plus lent qu'un download HF direct. Au runtime,
`setup-prod.sh` saute les installs déjà présentes et ne fait que modèles + démarrage.

### GPU
Blackwell (sm_120) en priorité — 5090, RTX PRO 4500/6000 — car la SageAttention de l'image est
compilée sm_120 only. **Phase 2 (GPU non-Blackwell : 4090/A6000/L40S)** : l'orchestrateur injecte
`IT_ATTENTION=sdpa` (+ `IT_BLOCKSWAP` adapté) → pas de sage, comportement par défaut inchangé.

## Chaîne prod (`scripts/`)
- `boot.sh` — point d'entrée pod (appelé par le dockerStartCmd). Copie worker/generate vers
  `/workspace`, lance `setup-prod.sh`, garde-fou auto-terminate si ComfyUI down, boucle worker,
  filet podTerminate + sleep anti restart-loop.
- `setup-prod.sh` — provisioning **idempotent** : ComfyUI + nodes + venv (`/workspace/venv`) +
  SageAttention + 7 modèles (aria2c) + démarrage ComfyUI :8188. Fast-path sentinel
  `.provisioned` (volume déjà peuplé → ~1-2 min). Modes : `IT_ENV_ONLY=1` (build image),
  `IT_ATTENTION=sdpa` (Phase 2).
- `worker.py` — boucle batch : claim job MassContent → assets → `generate.py` → PUT S3 presigné
  → complete (+ `durationMs` → costUsd). Heartbeat `/progress` sur thread dédié (45 s) avec
  logsTail. Auto-terminate après inactivité. Timeouts étagés : generate poll 5400 s <
  kill subprocess 7200 s < watchdog MassContent 180 min.
- `generate.py` — soumet le graphe ComfyUI verrouillé (InfiniteTalk Single + LoRA lightx2v +
  RIFE). Aligne `num_frames` sur les fenêtres (81+72k) + pad l'audio (fix lip-sync de fin).
  Émet `[gen] DONE in Xs -> <chemin>` — **contrat parsé par worker.py**.

### Env vars consommées par ce repo
| Var | Script(s) | Rôle (défaut) |
|---|---|---|
| `MASSCONTENT_BASE_URL` | worker.py, boot.sh | URL MassContent (requis) |
| `PIPELINE_SECRET` | worker.py | auth header `x-pipeline-secret` (requis) |
| `RUNPOD_API_KEY` / `RUNPOD_POD_ID` | boot.sh, worker.py | auto-terminate (POD_ID fourni par RunPod) |
| `IDLE_EXIT_SECONDS` | worker.py | arrêt après inactivité (300 ; l'orchestrateur passe 600) |
| `POLL_EMPTY_SECONDS` / `HEARTBEAT_SECONDS` | worker.py | poll à vide (10) / heartbeat (45) |
| `IT_ATTENTION` | setup-prod.sh, worker.py | `sageattn` (défaut) \| `sdpa` (Phase 2 non-Blackwell) |
| `IT_BLOCKSWAP` | worker.py | blocks à swapper (10 ; 20 sur 24 Go) |
| `IT_ENV_ONLY` | setup-prod.sh, Dockerfile | 1 = build image (stop avant modèles) |
| `IT_SETUP_ONLY` | boot.sh | 1 = peuplement volume one-shot, pas de worker |
| `IT_REPO_DIR` | boot.sh | emplacement du clone (`/workspace/repo`) |
| `SAGE_ARCH` | boot.sh→setup-prod.sh, Dockerfile | arch CUDA SageAttention (12.0 Blackwell) |
| `NOVITA_API_KEY` | novita-watch.js (local) | relevé dispo/prix Novita (jamais sur un pod) |

## Pipeline qualité verrouillée (réf `tom_FINAL`)
Passée par `worker.py` (`GEN_ARGS`) — les défauts argparse de `generate.py` sont génériques R&D :
```
python3 generate.py --image <img.png> --audio <audio.mp3> \
  --blockswap 10 --prefetch 1 --shift 3 --audio-scale 1.0 --attention sageattn \
  --steps 4 --rife 2 --colormatch mkl --scheduler euler \
  --base-model Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors \
  --prompt "a person calmly speaking to the camera, talking naturally to a friend, realistic, highly detailed face, sharp"
```

## Scripts R&D / legacy (non utilisés en prod, conservés pour référence)
`provision-comfyui-infinitetalk.sh`, `setup-pod.sh`, `setup-recover.sh`, `pod-run-setup.sh`
(provisioning manuel pré-prod) · `pod-start.sh` (LTX, abandonné) · `pod-start-s2v*.sh` (S2V,
rejeté qualité) · `pod-setup-it-seedvr2.sh` + `upscale.py` (SeedVR2, rejeté) ·
`run-worker.sh` (ancienne archi worker.env, remplacée par boot.sh) · `introspect.py` (outil dev :
schémas des nodes ComfyUI) · `novita-watch.js` (relevé périodique dispo/prix GPU, local).

## Décisions clés / learnings
- **colormatch=mkl** : corrige la dérive couleur/contraste sur vidéos longues (génération par fenêtres).
- **num_frames aligné sur une fin de fenêtre (81+72k) + audio padé** : corrige la désync lip-sync des dernières secondes.
- **audio_scale 1.0** : lip-sync serré. **scheduler euler** : mouvements plus fidèles. **Prompt neutre** (les consignes "bouge la tête" créaient des erreurs).
- **SageAttention 2** (pas la v3 : cassée sur fp8). **torch.compile** inutile (cassé sur InfiniteTalk, bug inputs dynamiques).
- **Base image** : `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel` (torch et nvcc cohérents — l'image runpod/pytorch mélangeait cu130/12.8 et cassait la compile sage).
- **Rejeté** : 480p natif (Tom veut du 720p) · FlashVSR (risque lip-sync + chunking) · spot (jobs 22 min non-reprenables) · serverless (~2× le coût de pods orchestrés en batch) · distill <4 steps (casse le lip-sync) · template RunPod (régressions silencieuses).
- **Levier futur à tester** : migration **Wan2.2-distill 4-step** (720p natif, potentiellement ~2× plus rapide + meilleur détail) — revalider le lip-sync avant de figer. Changer le modèle de base = 4 occurrences synchrones : `setup-prod.sh` (dl + `model_ok()`) + `worker.py` (`GEN_ARGS`) + ce README.

## Secrets
Aucun secret committé. Sur les pods : tout est injecté en env par l'orchestrateur MassContent.
En local : `.env` (non commité, cf `.env.example`) pour les outils type `novita-watch.js`.

# InfiniteTalk — Service vidéo talking-head pour MassContent

Service auto-hébergé (RunPod) qui anime **une image + un audio → vidéo 720p talking-head**,
en remplacement de HeyGen dans la pipeline MassContent.

## État (2026-06-04) : R&D VALIDÉE — construction prod EN PAUSE
- ✅ Qualité, lip-sync, stabilité couleur, naturel du mouvement : validés par Tom.
- ✅ Coût plancher atteint : **~0,25 $/vidéo** (~1100-1200 $/mois pour 150 vidéos/jour).
- ⏸️ Prochaine étape (en pause) : worker packagé + orchestrateur batch + intégration MassContent.
  **Requis pour démarrer** : un dépôt GitHub + les clés S3 MassContent.

## Pipeline validée
- Modèle : **InfiniteTalk Single** (base Wan2.1-I2V-14B fp8) via ComfyUI + `kijai/ComfyUI-WanVideoWrapper`.
- GPU : **RTX 5090 (32 Go) RunPod Community Cloud** (~0,69 $/h). Block-swap 10 obligatoire (0 = OOM en 720p).
- Sortie : **720×1280 (9:16), 50 fps** (RIFE x2). ~22 min de calcul / 40 s de vidéo.
- Commande de référence (réglages verrouillés) :
  ```
  python3 generate.py --image <img.png> --audio <audio.mp3> \
    --blockswap 10 --prefetch 1 --shift 3 --audio-scale 1.0 --attention sageattn \
    --steps 4 --rife 2 --colormatch mkl --scheduler euler \
    --base-model Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors \
    --prompt "a person calmly speaking to the camera, talking naturally to a friend, realistic, highly detailed face, sharp"
  ```

## Scripts (`scripts/`)
- `provision-comfyui-infinitetalk.sh` — installe ComfyUI + 4 custom nodes + poids InfiniteTalk (~33 Go) sur un pod.
- `setup-pod.sh` — provision + compile SageAttention pour l'archi GPU (`SAGE_ARCH=12.0` Blackwell/5090, `8.9` Ada/L40S) + démarre ComfyUI.
- `setup-recover.sh` — reprise robuste si un download HF gèle (utilise aria2c).
- `generate.py` — worker de génération (workflow API ComfyUI, entièrement paramétrable). Aligne num_frames sur les fenêtres + pad l'audio (fix lip-sync de fin).
- `introspect.py` — introspection des schémas de nodes ComfyUI.

## Décisions clés / learnings
- **colormatch=mkl** : corrige la dérive couleur/contraste sur vidéos longues (génération par fenêtres).
- **num_frames aligné sur une fin de fenêtre (81+72k) + audio padé** : corrige la désync lip-sync des dernières secondes.
- **audio_scale 1.0** : lip-sync serré. **scheduler euler** : mouvements plus fidèles. **Prompt neutre** (les consignes "bouge la tête" créaient des erreurs).
- **SageAttention 2** (pas la v3 : cassée sur fp8). **torch.compile** inutile (cassé sur InfiniteTalk, bug inputs dynamiques).
- **Rejeté** : 480p natif (Tom veut du 720p) · FlashVSR (risque lip-sync + chunking) · spot (jobs 22 min non-reprenables) · serverless (~2× le coût de pods orchestrés en batch) · distill <4 steps (casse le lip-sync).
- **Levier futur à tester** : migration **Wan2.2-distill 4-step** (720p natif, potentiellement ~2× plus rapide + meilleur détail) — revalider le lip-sync avant de figer.

## Architecture prod cible (à construire)
1. **Worker** = `generate.py` packagé (image+audio → 720p, sortie S3).
2. **Orchestrateur batch** : lance 1-2 pods Community 5090 → traite la file → upload S3 → coupe les pods (≈2× moins cher que serverless).
3. **Intégration MassContent** : remplace l'appel HeyGen dans `src/app/api/workers/avatar/route.ts` (contrat `(imageUrl, audioUrl) → videoUrl 720p`), avec file d'attente + retry + circuit-breaker + logs Discord (mêmes patterns que l'existant).

## Secrets
`.env` (non commité) : `RUNPOD_API_KEY`. À ajouter pour la prod : `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_S3_BUCKET` (S3 MassContent).

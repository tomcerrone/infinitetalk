# Image custom InfiniteTalk : ComfyUI + custom nodes + venv + SageAttention
# pré-installés (PAS les modèles 33 Go, téléchargés au runtime depuis le miroir R2).
#
# BUILD MULTI-ÉTAGES (2026-06-29) pour ~HALVER la taille de l'image (≈10,5 Go ->
# ≈5,3 Go) et donc le temps de PULL sur TOUS les fournisseurs (surtout Vast/Clore
# au débit bridé, où le pull de 10,5 Go dépassait la grâce zombie de 30 min) :
#   - stage `build` : base DEVEL (nvcc + headers) — compile SageAttention (sm_120)
#     + installe ComfyUI + nodes + venv sous /workspace via setup-prod.sh ENV_ONLY.
#   - stage final   : base RUNTIME (~4,2 Go vs ~9,4 Go devel ; CUDA runtime + cudnn,
#     SANS toolkit/nvcc/headers) — on COPIE juste /workspace (venv avec SageAttention
#     DÉJÀ compilé). Le .so de sage tourne au runtime sans nvcc.
#
# Base DEVEL (build) : torch 2.7.1 + CUDA 12.8 cohérents, nvcc inclus (sinon
# SageAttention ne compile pas, "CUDA version mismatch"). CUDA 12.8 = sm_120 (5090).
# Base RUNTIME (final) : MÊME torch/CUDA -> le venv --system-site-packages hérite
# du torch de l'image, identique entre les 2 bases (pas de divergence de version).

# ---- Stage 1 : build (devel, nvcc) ----
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel AS build
ENV IT_ENV_ONLY=1 \
    SAGE_ARCH=12.0 \
    DEBIAN_FRONTEND=noninteractive
COPY scripts/setup-prod.sh /tmp/setup-prod.sh
RUN chmod +x /tmp/setup-prod.sh && bash /tmp/setup-prod.sh && \
    test -f /workspace/.env-ready && \
    /workspace/venv/bin/python -c "import sageattention; print('sage OK in image')" && \
    /workspace/venv/bin/python -c "import hf_xet; print('hf_xet OK in image')"

# ---- Stage 2 : runtime (allégé, sans nvcc) ----
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ENV IT_ENV_ONLY=0 \
    SAGE_ARCH=12.0 \
    DEBIAN_FRONTEND=noninteractive
# Outils système requis au runtime par boot.sh/setup-prod.sh (git clone du repo,
# aria2c pour les modèles R2, ffmpeg pour l'encodage). Bakés ici pour ne pas les
# re-télécharger à chaque boot (la base runtime ne les inclut pas).
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git ffmpeg aria2 wget curl ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# Tout le provisioning (ComfyUI + nodes + venv + SageAttention compilé) vit sous
# /workspace. Au boot, setup-prod.sh détecte tout présent (fast skip des installs)
# et ne fait que les modèles (miroir R2) + le démarrage de ComfyUI.
COPY --from=build /workspace /workspace
# Smoke check à la construction (sans GPU) : sage + hf_xet importables sur la base
# RUNTIME -> détecte une lib runtime manquante AVANT la prod (build échoue = :latest
# inchangé). La génération réelle (GPU) reste validée par un pod de test.
RUN /workspace/venv/bin/python -c "import sageattention; print('sage OK runtime')" && \
    /workspace/venv/bin/python -c "import hf_xet; print('hf_xet OK runtime')"

# Image custom InfiniteTalk : ComfyUI + custom nodes + venv + SageAttention
# pré-installés (PAS les modèles 33 Go, téléchargés au runtime depuis le miroir R2).
#
# BUILD MULTI-ÉTAGES (slim v2, 2026-06-30) pour ~HALVER la taille de l'image
# (≈10,5 Go -> ≈5-6 Go) et donc le temps de PULL sur TOUS les fournisseurs (surtout
# Vast au débit variable, où le pull de 10,5 Go peut dépasser la grâce zombie) :
#   - stage `build` : base DEVEL (nvcc + headers) — compile SageAttention (sm_120)
#     + installe ComfyUI + nodes + venv sous /workspace via setup-prod.sh ENV_ONLY.
#   - stage final   : base RUNTIME (~4,2 Go vs ~9,4 Go devel ; CUDA runtime + cudnn,
#     SANS toolkit/nvcc/headers) — on COPIE juste /workspace (venv avec SageAttention
#     DÉJÀ compilé). Le .so de sage tourne au runtime sans nvcc.
#
# v2 (fix du revert 2026-06-29) : la base runtime n'a PAS les libs système d'OpenCV
# (cv2, importé par ComfyUI/nodes au démarrage) -> ComfyUI crashait au boot
# ("libGL.so.1 manquant") -> pod auto-terminé -> 0 génération. On installe donc
# libgl1 + libglib2.0-0 (+ libsm6/libxext6/libxrender1) ET on SMOKE-TESTE `import cv2`
# AU BUILD : si une lib runtime manque, le build échoue -> :latest reste intact.
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
# Outils système requis au runtime :
#  - git/aria2/ffmpeg/wget/curl : boot.sh/setup-prod.sh (clone, modèles R2, encodage).
#  - libGL/glib/X (libgl1...) : cv2 (OpenCV) importé par ComfyUI au démarrage.
#  - gcc/g++ : le ComfyUI récent fait de la compilation NATIVE À LA VOLÉE au 1er
#    import (comfy_kitchen -> Triton JIT compile son driver CUDA). La base runtime
#    n'a PAS de compilateur C (contrairement à la base devel) -> ComfyUI crashait
#    "Failed to find C compiler" -> 0 génération. Triton embarque ses en-têtes CUDA
#    et le pilote (libcuda) est présent sur le nœud GPU : gcc/g++ suffisent. (+~250 Mo,
#    l'image reste ~moitié de la devel.)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git ffmpeg aria2 wget curl ca-certificates \
      libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
      gcc g++ python3-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# Tout le provisioning (ComfyUI + nodes + venv + SageAttention compilé) vit sous
# /workspace. Au boot, setup-prod.sh détecte tout présent (fast skip des installs)
# et ne fait que les modèles (miroir R2) + le démarrage de ComfyUI.
COPY --from=build /workspace /workspace
# Smoke check à la construction (sans GPU) : torch + sage + hf_xet + cv2 importables
# sur la base RUNTIME -> détecte une lib runtime manquante (ex libGL d'OpenCV, cause
# du revert) AVANT la prod (build échoue = :candidate non publié, :latest inchangé).
# La génération réelle (GPU) reste validée par un pod de test sur :candidate.
RUN /workspace/venv/bin/python -c "import torch; print('torch', torch.__version__)" && \
    /workspace/venv/bin/python -c "import sageattention; print('sage OK runtime')" && \
    /workspace/venv/bin/python -c "import hf_xet; print('hf_xet OK runtime')" && \
    /workspace/venv/bin/python -c "import cv2; print('cv2 OK runtime', cv2.__version__)"

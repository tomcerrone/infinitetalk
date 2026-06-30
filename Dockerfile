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
# SAGE_ARCH = familles d'arch SageAttention, format ';' (PAS espace : le setup.py de
# sage parse mal une liste à espaces -> un seul item cassé). "8.9;12.0+PTX" compile le
# noyau fp8 SM89 partagé par 4090/L40S/6000Ada (sm_89) ET la 5090 (sm_120 RÉUTILISE le
# noyau SM89) -> un seul build débloque les Ada SANS casser la 5090. On EXCLUT 8.6
# (=Triton, 3090/A6000, aucun noyau requis) et 8.0/9.0 (mélanger sm80 au sm89 = SASS
# invalide, défaut amont #360). EXT_PARALLEL/MAX_JOBS/NVCC threads bornent la conso
# mémoire du compile multi-gencode (_qattn_sm89 = 7 .cu × 2 gencodes) pour ne pas OOM.
ENV IT_ENV_ONLY=1 \
    SAGE_ARCH="8.9;12.0+PTX" \
    EXT_PARALLEL=2 \
    MAX_JOBS=4 \
    NVCC_APPEND_FLAGS="--threads 8" \
    DEBIAN_FRONTEND=noninteractive
COPY scripts/setup-prod.sh /tmp/setup-prod.sh
# Smoke-test RENFORCÉ : `import sageattention` réussit MÊME si un noyau manque (modules
# importés dans un try/except nu -> SM89_ENABLED=False silencieux, crash seulement à
# l'inférence — c'est ce qui a laissé passer l'image cassée 2026-06-30). On ASSERTE
# SM89_ENABLED (noyau dont dépendent 4090 ET 5090) -> build sans noyau = ÉCHEC ici.
RUN chmod +x /tmp/setup-prod.sh && bash /tmp/setup-prod.sh && \
    test -f /workspace/.env-ready && \
    /workspace/venv/bin/python -c "import sageattention.core as c; assert c.SM89_ENABLED, ('SM89 KERNEL MANQUANT', c.SM89_ENABLED); print('sage SM89 kernel OK in image')" && \
    /workspace/venv/bin/python -c "import hf_xet; print('hf_xet OK in image')"

# ---- Stage 2 : runtime (allégé, sans nvcc) ----
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ENV IT_ENV_ONLY=0 \
    SAGE_ARCH="8.9;12.0+PTX" \
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
    /workspace/venv/bin/python -c "import sageattention.core as c; assert c.SM89_ENABLED, ('SM89 KERNEL MANQUANT runtime', c.SM89_ENABLED); print('sage SM89 kernel OK runtime')" && \
    /workspace/venv/bin/python -c "import hf_xet; print('hf_xet OK runtime')" && \
    /workspace/venv/bin/python -c "import cv2; print('cv2 OK runtime', cv2.__version__)"

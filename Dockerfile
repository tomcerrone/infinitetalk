# Image custom InfiniteTalk : ComfyUI + custom nodes + venv + SageAttention
# pré-installés (PAS les modèles 33 Go, téléchargés au runtime). Permet un
# cold-start ~2× plus court sur N'IMPORTE QUEL cloud RunPod (Community inclus),
# sans dépendre d'un network volume Secure (rare).
#
# Réutilise scripts/setup-prod.sh en mode IT_ENV_ONLY (source unique de vérité) :
# il installe ComfyUI + nodes + venv + SageAttention sous /workspace puis s'arrête
# avant les modèles et le démarrage. Au runtime, boot.sh relance setup-prod.sh qui
# saute les installs (déjà présents) et ne fait que modèles + démarrage ComfyUI.
#
# Base = image PyTorch officielle DEVEL (torch 2.7.1 + CUDA 12.8 COHÉRENTS, nvcc
# inclus). On n'utilise PAS runpod/pytorch:1.0.3-cu1281 : son torch est compilé
# cu130 alors que seul le toolkit nvcc 12.8 est présent -> SageAttention ne
# compile pas dessus ("CUDA version mismatch"). CUDA 12.8 supporte le 5090 (sm_120).
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel

# SageAttention cible l'arch Blackwell/5090 (compilation nvcc, sans GPU au build).
ENV IT_ENV_ONLY=1 \
    SAGE_ARCH=12.0 \
    DEBIAN_FRONTEND=noninteractive

COPY scripts/setup-prod.sh /tmp/setup-prod.sh
RUN chmod +x /tmp/setup-prod.sh && bash /tmp/setup-prod.sh && \
    test -f /workspace/.env-ready && \
    /workspace/venv/bin/python -c "import sageattention; print('sage OK in image')"

# Réinitialise IT_ENV_ONLY pour le runtime (le full setup doit s'exécuter :
# modèles + démarrage). Le template RunPod fournit le vrai dockerStartCmd.
ENV IT_ENV_ONLY=0

# Image custom InfiniteTalk : ComfyUI + custom nodes + venv + SageAttention
# pré-installés (PAS les modèles 33 Go, téléchargés au runtime). Permet un
# cold-start ~2× plus court sur N'IMPORTE QUEL cloud RunPod (Community inclus),
# sans dépendre d'un network volume Secure (rare).
#
# Réutilise scripts/setup-prod.sh en mode IT_ENV_ONLY (source unique de vérité) :
# il installe ComfyUI + nodes + venv + SageAttention sous /workspace puis s'arrête
# avant les modèles et le démarrage. Au runtime, boot.sh relance setup-prod.sh qui
# saute les installs (déjà présents) et ne fait que modèles + démarrage ComfyUI.
FROM runpod/pytorch:1.0.3-cu1281-torch271-ubuntu2204

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

import os

# All models are cached locally; don't let huggingface_hub probe the network on every load.
# Set HF_HUB_OFFLINE=0 when you need to download a new model.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

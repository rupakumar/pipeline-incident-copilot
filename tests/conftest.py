import os

# Unit tests never send traces.
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

"""Harbor process startup hooks shared by every Agent Fleet agent."""

import os

if (
    os.environ.get("HARBOR_ENVIRONMENT_TYPE", "docker").strip().lower() == "qz"
    and os.environ.get("QZ_SANDBOX_TEMPLATE_MAP", "").strip()
):
    from qz_task_instruction import patch_harbor_task_instruction

    patch_harbor_task_instruction()

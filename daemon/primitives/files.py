"""
File-operation primitives: upload_file, download stubs.

Real implementations come in a later phase.

Security note
-------------
``upload_file`` is gated by ``BROWSER_UPLOAD_ALLOW_ROOT`` (default: disabled).
When set, only files under that resolved directory may be uploaded.  The RPC
layer enforces this in ``rpc._gate_upload_file``; this module re-checks so
direct primitive callers are also fail-closed.
"""

import os
from pathlib import Path
from typing import Any


def _check_upload_allowlist(file_path: str) -> str | None:
    """Return an error message if *file_path* is not under the allowlisted root.

    Returns ``None`` when the path is allowed.
    """
    allow_root = os.environ.get("BROWSER_UPLOAD_ALLOW_ROOT", "").strip()
    if not allow_root:
        return (
            "upload_file is disabled. "
            "Set BROWSER_UPLOAD_ALLOW_ROOT to an allowlisted directory."
        )
    try:
        resolved = Path(file_path).resolve()
        root = Path(allow_root).resolve()
        resolved.relative_to(root)
    except ValueError:
        return f"upload_file denied: {resolved} is outside allowlisted root {root}"
    except Exception as e:
        return f"could not resolve upload path: {e}"
    return None


async def upload_file(cdp: Any, session_id: str | None, selector: str, file_path: str) -> dict:
    """Upload a file by setting the value of a file input element.

    Uses CDP ``DOM.setFileInputFiles`` for reliable file picking.

    Gated by ``BROWSER_UPLOAD_ALLOW_ROOT`` (see module docstring).
    """
    deny = _check_upload_allowlist(file_path)
    if deny is not None:
        return {"error": deny, "code": "UPLOAD_DISABLED" if "disabled" in deny else "UPLOAD_PATH_DENIED"}

    # Resolve the node ID for the file input element
    try:
        doc_result = await cdp.send("DOM.getDocument", session_id=session_id)
        node_id_result = await cdp.send(
            "DOM.querySelector",
            {"nodeId": doc_result["result"]["root"]["nodeId"], "selector": selector},
            session_id=session_id,
        )
        node_id = node_id_result["result"].get("nodeId")
        if node_id is None:
            return {"error": f"Element not found: {selector}", "code": "ELEMENT_NOT_FOUND"}
        await cdp.send(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": [str(Path(file_path).resolve())]},
            session_id=session_id,
        )
        return {"uploaded": True, "selector": selector, "file": file_path}
    except Exception as e:
        return {"error": str(e), "code": "UPLOAD_FAILED"}

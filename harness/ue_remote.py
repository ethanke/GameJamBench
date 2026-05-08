"""Thin wrapper around UE's bundled remote_execution.py for the harness.

UE 5.7 ships `remote_execution.py` with the PythonScriptPlugin. We import
that module directly (rather than reimplementing the UDP-discovery + TCP
protocol) so the harness can talk to a *live* editor instance.

This unlocks validators that reuse a persistent editor session — replacing
the cold-launch commandlet pattern (~10 sec per call) with sub-second RPCs.

Caller must ensure the editor is launched against the right project AND
has Python Remote Execution enabled in its DefaultEngine.ini. The
holo-unreal MCP's ue_enable_remote sets this; equivalent stanza is

    [/Script/PythonScriptPlugin.PythonScriptPluginSettings]
    bRemoteExecution=True
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time
from typing import Any


_ENGINE_REMOTE_EXEC = pathlib.Path(
    r"C:\Program Files\Epic Games\UE_5.7\Engine\Plugins\Experimental"
    r"\PythonScriptPlugin\Content\Python\remote_execution.py"
)


def _load_remote_execution():
    """Dynamically load UE's remote_execution.py without polluting sys.path."""
    if not _ENGINE_REMOTE_EXEC.exists():
        raise FileNotFoundError(
            f"UE remote_execution.py not found at {_ENGINE_REMOTE_EXEC}; "
            "is UE 5.7 installed at the expected path?")
    spec = importlib.util.spec_from_file_location(
        "ue_remote_execution", str(_ENGINE_REMOTE_EXEC))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ue_remote_execution"] = mod
    spec.loader.exec_module(mod)
    return mod


class LiveEditorSession:
    """Context-manager wrapper around UE's RemoteExecution for one-shot calls.

    Usage:
        with LiveEditorSession() as ue:
            res = ue.run("import unreal; unreal.log_warning('hi')")
            assert res["success"]
    """

    def __init__(self, discovery_timeout_sec: float = 10.0):
        self._mod = _load_remote_execution()
        self._exec = self._mod.RemoteExecution()
        self._discovery_timeout = discovery_timeout_sec
        self._node_id: str | None = None

    def __enter__(self) -> "LiveEditorSession":
        self._exec.start()
        deadline = time.time() + self._discovery_timeout
        while time.time() < deadline:
            nodes = self._exec.remote_nodes
            if nodes:
                self._node_id = nodes[0]["node_id"]
                self._exec.open_command_connection(self._node_id)
                return self
            time.sleep(0.25)
        self._exec.stop()
        raise TimeoutError(
            f"no UE editor with Python Remote Execution discovered within "
            f"{self._discovery_timeout:.1f}s. Is the editor running with "
            "Python Remote Execution enabled?")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._exec.close_command_connection()
        except Exception:
            pass
        self._exec.stop()

    def run(self, code: str, unattended: bool = True,
            exec_mode: str = "ExecuteFile") -> dict[str, Any]:
        """Execute Python `code` in the live editor and return the result dict.

        The returned dict has keys: success (bool), command (str), result
        (str), output (list[{type, output}]). On protocol failure, raises.
        """
        return self._exec.run_command(
            code, unattended=unattended, exec_mode=exec_mode,
            raise_on_failure=False)

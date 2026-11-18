"""LLDB callback for the disposable Matter device started by the recovery witness."""

import json
import os
from pathlib import Path

import lldb


def source_line(config):
    source = Path(config["source"])
    anchor = {
        "before-replace": "fs::rename(self.path.join(NEXT), self.path.join(STATE))",
        "after-sync": "self.snapshot = next;",
    }[config["boundary"]]
    lines = [i for i, line in enumerate(source.read_text().splitlines(), 1) if anchor in line]
    assert len(lines) == 1
    return lines[0]


def arm(debugger):
    directory = Path(os.environ["NXRS_RECOVERY_TRIAL"])
    config = json.loads((directory / "breakpoint.json").read_text())
    target = debugger.GetSelectedTarget()
    assert target.GetProcess().GetProcessID() == config["pid"]
    bp = target.BreakpointCreateByLocation(config["source"], source_line(config))
    assert bp.GetNumResolvedLocations() > 0, "build the unstripped debug camera against this checkout"
    bp.SetScriptCallbackFunction(__name__ + ".at_commit")
    (directory / "armed").touch(exist_ok=False)


def at_commit(frame, location, _):
    directory = Path(os.environ["NXRS_RECOVERY_TRIAL"])
    config = json.loads((directory / "breakpoint.json").read_text())
    names = [f.GetFunctionName() or "" for f in frame.GetThread()]
    if not any(config["operation"] in name for name in names):
        return False
    if "remove_key" in config:
        # The SDK first writes its next-fabric index. Wait for the deletion
        # of the specific fabric record, using the actual store argument.
        if not any("nxrs_gateway_matter::persistence::Store" in (f.GetFunctionName() or "")
                   and "::remove" in (f.GetFunctionName() or "")
                   and f.FindVariable("key").GetValueAsUnsigned(lldb.LLDB_INVALID_ADDRESS) == config["remove_key"]
                   for f in frame.GetThread()):
            return False
    assert frame.GetThread().GetProcess().GetProcessID() == config["pid"]
    entry = frame.GetLineEntry()
    assert Path(entry.GetFileSpec().fullpath).resolve() == Path(config["source"]).resolve()
    assert entry.GetLine() == source_line(config) == location.GetAddress().GetLineEntry().GetLine()
    (directory / "hit").touch(exist_ok=False)
    return True


def stop_at_commit(debugger):
    directory = Path(os.environ["NXRS_RECOVERY_TRIAL"])
    assert (directory / "hit").is_file()
    process = debugger.GetSelectedTarget().GetProcess()
    assert process.GetState() == lldb.eStateStopped
    assert process.GetProcessID() == json.loads((directory / "breakpoint.json").read_text())["pid"]
    assert process.Kill().Success()

"""Regression test for FIX 8: `trigger/handler.py` imports `httpx`, and
`poll/handler.py` imports `httpx` AND `anthropic` -- neither is in the Python 3.13
Lambda runtime. A plain `Code.from_asset(<dir>)` with no bundling leaves both
`ImportError`ing at cold start. This confirms `cdk synth` actually BUNDLES (not
just that it exits 0) by inspecting the real synthesized asset output directories
for vendored third-party packages.

This is a real `cdk synth` (not mocked) -- it shells out to `pip install` under the
platform lock (`brief_eval/stack.py`'s `_LocalPipBundling`), so it is slower than
the rest of the suite and requires `pip`/`pip3` on PATH, exactly like
`deploy/managed-agent`'s equivalent launcher-bundling behavior already does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from brief_eval.stack import BriefEvalStack

EVAL_DIR = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("pip3") is None and shutil.which("pip") is None,
    reason="local pip bundling requires pip/pip3 on PATH",
)


def _synth_and_find_asset_dir_for(function_name: str) -> Path:
    """Synthesize the real stack (same construct app.py deploys) into a scratch
    cdk.out-equivalent directory, then locate the on-disk asset directory backing
    the named Lambda function's Code by matching its S3Key asset hash against the
    synthesized `cdk.out/asset.<hash>/` directories CDK actually wrote to disk."""
    app = cdk.App(outdir=str(EVAL_DIR / "cdk.out" / "test-bundling-scratch"))
    stack = BriefEvalStack(
        app,
        "TestBundlingBriefEvalStack",
        env=cdk.Environment(account="740353583786", region="us-east-1"),
    )
    assembly = app.synth()
    template = Template.from_stack(stack)

    functions = template.find_resources("AWS::Lambda::Function")
    (logical_id, resource) = next(
        (lid, res) for lid, res in functions.items() if res["Properties"].get("FunctionName") == function_name
    )
    code = resource["Properties"]["Code"]
    s3_key = code["S3Key"]
    if isinstance(s3_key, dict):
        # Fn::Join / intrinsic form -- the asset hash is embedded in one of the
        # joined string parts (CDK's own convention: "<hash>.zip" or similar).
        s3_key = json.dumps(s3_key)

    out_dir = Path(assembly.directory)
    candidates = [d for d in out_dir.glob("asset.*") if d.is_dir()]
    matches = [d for d in candidates if d.name.split(".", 1)[1] in s3_key]
    assert matches, f"could not find an on-disk asset dir for {function_name} among {[d.name for d in candidates]}"
    return matches[0]


def test_trigger_function_asset_has_httpx_vendored():
    asset_dir = _synth_and_find_asset_dir_for("brief-eval-trigger")
    assert (asset_dir / "handler.py").exists()
    assert (asset_dir / "httpx").is_dir(), "httpx must be vendored into the trigger function's asset"


def test_poll_function_asset_has_httpx_and_anthropic_vendored():
    asset_dir = _synth_and_find_asset_dir_for("brief-eval-poll")
    assert (asset_dir / "handler.py").exists()
    assert (asset_dir / "httpx").is_dir(), "httpx must be vendored into the poll function's asset"
    assert (asset_dir / "anthropic").is_dir(), "anthropic must be vendored into the poll function's asset"


def test_vendored_native_extension_targets_linux_aarch64_not_the_build_host():
    """The whole point of the platform-locked bundling: even when run on a macOS/
    arm64 dev host, the vendored pydantic-core native extension must be a Linux/
    aarch64 ELF shared object (Lambda's actual runtime), never a host-native
    Mach-O -- otherwise it would silently fail to import in production exactly like
    deploy/managed-agent's CONFIRMED LIVE BUG (2026-07-03, see that stack's own
    _LocalPipBundling docstring)."""
    asset_dir = _synth_and_find_asset_dir_for("brief-eval-poll")
    pydantic_core_dir = asset_dir / "pydantic_core"
    assert pydantic_core_dir.is_dir()

    so_files = list(pydantic_core_dir.glob("_pydantic_core*.so"))
    assert so_files, "expected a compiled pydantic_core extension module"
    so_file = so_files[0]

    assert "linux" in so_file.name, f"expected a linux-tagged wheel filename, got {so_file.name}"

    with open(so_file, "rb") as f:
        magic = f.read(4)
    # ELF magic bytes (0x7f 'E' 'L' 'F') -- NOT a Mach-O magic (macOS), proving this
    # is genuinely a cross-compiled/cross-selected Linux binary, not whatever the
    # host platform would have resolved without the --platform lock.
    assert magic == b"\x7fELF", f"expected an ELF (Linux) binary, got magic bytes {magic!r}"

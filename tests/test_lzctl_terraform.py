"""Tests for the Terraform checks in `lzctl check`: validate, the offline plan, tflint, the plan scans
and the reviewed exceptions for baseline findings.

Most tests fake the tools. The golden tests at the end run the real ones: they download the pinned FAST
module and the Google providers once (TF_PLUGIN_CACHE_DIR keeps them), so they need the network.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import _lzctl

lzctl = _lzctl.load()
ROOT = _lzctl.ROOT
VALID_DIR = ROOT / "evals/gcp/valid"
TERRAFORM_CHECKS = ("terraform-validate", "terraform-plan", "tflint")
SCANS = ("trivy", "checkov")
needs_terraform = pytest.mark.skipif(not (shutil.which("terraform") or shutil.which("tofu")), reason="terraform not installed")


def render_to(base: Path, name: str = "standard") -> Path:
    spec = json.loads((VALID_DIR / f"{name}.spec.json").read_text())
    files, report = lzctl.render_spec(spec)
    out = base / f"out-{name}"
    for relative, data in files.items():
        (out / relative).parent.mkdir(parents=True, exist_ok=True)
        (out / relative).write_bytes(data)
    (out / lzctl.REPORT_NAME).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return out


def resign(out: Path) -> None:
    """Make the report match the files on disk, as an attacker who edits both would."""
    report = json.loads((out / lzctl.REPORT_NAME).read_text())
    files, _ = lzctl._walk_files(out)
    report["files"] = {rel: lzctl._sha256(p.read_bytes()) for rel, p in sorted(files.items()) if rel != lzctl.REPORT_NAME}
    report["output_hash"] = lzctl._output_hash(report["files"])
    (out / lzctl.REPORT_NAME).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def statuses(checks: list[dict]) -> dict[str, str]:
    return {c["name"]: c["status"] for c in checks}


def detail(checks: list[dict], name: str) -> str:
    return next(c["detail"] for c in checks if c["name"] == name)


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_ci_has_every_tool_installed():
    """CI sets REQUIRE_TOOLS so a broken tool install fails the job instead of silently skipping tests."""
    if not os.environ.get("REQUIRE_TOOLS"):
        pytest.skip("REQUIRE_TOOLS not set")
    missing = [tool for tool in ("terraform", "tflint", "trivy", "checkov", "conftest") if not shutil.which(tool)]
    assert not missing, f"not installed: {missing}"


# --- the Terraform run, with a fake terraform -------------------------------------------------------

PLAN = {"format_version": "1.2", "planned_values": {"root_module": {}},
        "resource_changes": [{"address": "a.b", "change": {"actions": ["create"]}}, {"address": "c.d", "change": {"actions": ["no-op"]}}]}


class FakeTools:
    """Stands in for terraform and tflint; trivy and checkov are absent unless a test adds them."""

    def __init__(self, monkeypatch, tmp_path, **answers):
        self.calls: list[tuple[str, list[str], Path, list[str]]] = []
        self.answers = {"fmt": completed(), "init": completed(), "validate": completed(json.dumps({"valid": True, "diagnostics": []})),
                        "plan": completed(), "show": completed(json.dumps(PLAN)), "tflint": completed('{"issues": [], "errors": []}'),
                        **answers}
        monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(tmp_path / "plugins"))
        monkeypatch.setattr(lzctl.shutil, "which", lambda name: f"/fake/{name}" if name in ("terraform", "tflint", "conftest") else None)
        monkeypatch.setattr(lzctl.subprocess, "run", self)

    def __call__(self, argv, **kwargs):
        tool = Path(argv[0]).name
        cwd = Path(kwargs["cwd"])
        files = sorted(p.relative_to(cwd).as_posix() for p in cwd.rglob("*") if p.is_file())
        key = "tflint" if tool == "tflint" else ("conftest" if tool == "conftest" else argv[1])
        self.calls.append((key, list(argv), cwd, files))
        if key == "conftest":
            return completed("[]")
        answer = self.answers[key]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def by(self, key: str) -> list[tuple[list[str], Path, list[str]]]:
        return [(argv, cwd, files) for k, argv, cwd, files in self.calls if k == key]


def test_each_stage_is_validated_and_planned_offline_in_a_private_copy(tmp_path, monkeypatch):
    out = render_to(tmp_path)
    tools = FakeTools(monkeypatch, tmp_path)
    checks, findings = lzctl.check_output(out)
    assert findings == []
    assert {statuses(checks)[name] for name in TERRAFORM_CHECKS} == {"passed"}
    assert detail(checks, "terraform-plan") == "0-org-setup: 1 resources planned offline; 2-networking: 1 resources planned offline"
    assert [k for k, *_ in tools.calls if k not in ("conftest", "fmt")] == ["tflint", "init", "validate", "plan", "show"] * 2

    (org_argv, org_cwd, org_files), (net_argv, net_cwd, net_files) = tools.by("init")
    assert org_argv[1:] == ["init", "-backend=false", "-input=false", "-no-color"]
    assert not org_cwd.is_relative_to(out) and not net_cwd.is_relative_to(out)
    # Only the files the report lists, each stage without the other, plus the check's own offline files.
    assert "main.tf" in org_files and "assets/providers.tf.tpl" in org_files and "fast_version.txt" in org_files
    assert not any(f.startswith("2-networking/") for f in org_files) and lzctl.REPORT_NAME not in org_files
    assert lzctl.OFFLINE_PROVIDERS[0] in org_files and lzctl.OFFLINE_INPUTS not in org_files
    assert {"main.tf", "variables-fast.tf", lzctl.OFFLINE_PROVIDERS[0], lzctl.OFFLINE_INPUTS} <= set(net_files)
    plan_argv = tools.by("plan")[0][0]
    assert "-refresh=false" in plan_argv and "-lock=false" in plan_argv and f"-out={lzctl.PLAN_FILE}" in plan_argv
    # tflint sees the root module before the offline files are added.
    assert lzctl.OFFLINE_PROVIDERS[0] not in tools.by("tflint")[0][2]


def test_later_stages_plan_against_placeholder_stage_0_outputs(tmp_path, monkeypatch):
    out = render_to(tmp_path)
    spec = json.loads((VALID_DIR / "standard.spec.json").read_text())
    seen = {}

    tools = FakeTools(monkeypatch, tmp_path)
    original = tools.__call__

    def capture(argv, **kwargs):
        inputs = Path(kwargs["cwd"]) / lzctl.OFFLINE_INPUTS
        if argv[1:2] == ["plan"] and inputs.is_file():
            seen.update(json.loads(inputs.read_text()))
        return original(argv, **kwargs)

    monkeypatch.setattr(lzctl.subprocess, "run", capture)
    lzctl.check_output(out)
    assert seen == {"billing_account": {"id": spec["organization"]["billing_ref"]},
                    "organization": {"id": int(spec["organization"]["id"])},
                    "prefix": spec["extensions"]["gcp"]["prefix"]}


def test_offline_providers_need_no_credentials():
    text = lzctl.OFFLINE_PROVIDERS[1]
    assert text.count("access_token") == 2 and 'provider "google"' in text and 'provider "google-beta"' in text


def test_init_failure_is_skipped_not_failed(tmp_path, monkeypatch):
    out = render_to(tmp_path)
    tools = FakeTools(monkeypatch, tmp_path, init=completed(returncode=1, stderr="\nError: Failed to download module\n\nmore"))
    checks, findings = lzctl.check_output(out)
    assert findings == []
    assert statuses(checks)["terraform-validate"] == statuses(checks)["terraform-plan"] == "skipped"
    assert "terraform init failed: Error: Failed to download module" in detail(checks, "terraform-plan")
    assert statuses(checks)["trivy"] == "skipped" and not tools.by("validate")


def test_validate_errors_are_findings_and_stop_the_plan(tmp_path, monkeypatch):
    out = render_to(tmp_path)
    invalid = json.dumps({"valid": False, "diagnostics": [
        {"severity": "warning", "summary": "Deprecated", "detail": "ignored"},
        {"severity": "error", "summary": "Unsupported argument", "detail": "An argument named \"x\"\nis not expected here."},
    ]})
    tools = FakeTools(monkeypatch, tmp_path, validate=completed(invalid, returncode=1))
    checks, findings = lzctl.check_output(out)
    assert [(f["rule"], f["path"]) for f in findings] == [("check.terraform.validate", "2-networking/main.tf"),
                                                          ("check.terraform.validate", "main.tf")]
    assert findings[0]["message"] == 'Unsupported argument: An argument named "x" is not expected here.'
    assert statuses(checks)["terraform-validate"] == "failed" and statuses(checks)["terraform-plan"] == "skipped"
    assert not tools.by("plan")


@pytest.mark.parametrize("answer", [completed(returncode=1, stderr="Error: Invalid for_each argument\n"), subprocess.TimeoutExpired("terraform", 600)])
def test_plan_failure_is_a_finding(tmp_path, monkeypatch, answer):
    out = render_to(tmp_path, "starter")
    FakeTools(monkeypatch, tmp_path, plan=answer)
    checks, findings = lzctl.check_output(out)
    assert statuses(checks)["terraform-plan"] == "failed"
    assert [(f["rule"], f["path"]) for f in findings] == [("check.terraform.plan", "main.tf")]


def test_tflint_issues_and_errors(tmp_path, monkeypatch):
    out = render_to(tmp_path, "starter")
    issue = {"rule": {"name": "terraform_required_version"}, "message": "terraform \"required_version\" attribute is required",
             "range": {"filename": "main.tf"}}
    FakeTools(monkeypatch, tmp_path, tflint=completed(json.dumps({"issues": [issue], "errors": []}), returncode=2))
    checks, findings = lzctl.check_output(out)
    assert statuses(checks)["tflint"] == "failed"
    assert [(f["rule"], f["path"]) for f in findings] == [("check.tflint.terraform_required_version", "main.tf")]

    FakeTools(monkeypatch, tmp_path, tflint=completed(json.dumps({"issues": [], "errors": [{"message": "Failed to load configurations"}]}), returncode=1))
    checks, findings = lzctl.check_output(out)
    assert [f["rule"] for f in findings] == ["check.tflint.error"] and "Failed to load" in findings[0]["message"]


@pytest.mark.parametrize("change", [
    lambda out: (out / "main.tf").write_text((out / "main.tf").read_text().replace("cloud-foundation-fabric.git", "evil.git")),
    lambda out: (out / "extra.tf").write_text('provider "google" {}\n'),
    lambda out: (out / "2-networking/override.tf.json").write_text("{}"),
    lambda out: (out / "terraform.tfvars").write_text('x = "y"\n'),
    lambda out: (out / "2-networking/variables-fast.tf").write_text('variable "prefix" {}\n'),
])
def test_terraform_never_runs_on_terraform_files_this_skill_did_not_render(tmp_path, monkeypatch, change):
    # A changed main.tf could point init at any module or provider, and a provider is a program that runs here.
    out = render_to(tmp_path)
    change(out)
    resign(out)  # integrity alone would not catch it
    tools = FakeTools(monkeypatch, tmp_path)
    checks, findings = lzctl.check_output(out)
    assert {f["rule"] for f in findings} == {"check.terraform.unexpected"}
    assert {statuses(checks)[name] for name in TERRAFORM_CHECKS} == {"skipped"}
    assert [k for k, *_ in tools.calls if k not in ("conftest", "fmt")] == []


def test_a_hand_edit_stops_the_terraform_checks(tmp_path, monkeypatch):
    out = render_to(tmp_path)
    (out / "datasets/landing-zone/defaults.yaml").write_text("edited: true\n")
    tools = FakeTools(monkeypatch, tmp_path)
    checks, findings = lzctl.check_output(out)
    assert {f["rule"] for f in findings} == {"check.integrity.modified"}
    assert "integrity" in detail(checks, "terraform-plan") and not tools.by("init")


def test_terraform_findings_explain_as_terraform_not_spec():
    for rule in ("check.terraform.validate", "check.terraform.plan", "check.tflint.terraform_typed_variables"):
        assert lzctl.explain_findings([{"rule": rule, "path": "main.tf", "message": "m"}], None)[0]["cause"]["kind"] == "terraform"
    cause = lzctl.explain_findings([{"rule": "check.terraform.unexpected", "path": "extra.tf", "message": "m"}], None)[0]["cause"]
    assert cause["kind"] == "integrity"


# --- the plan scans, with fake scanners ----------------------------------------------------------------

BUCKET = 'module.org_setup.module.factory.module.buckets["iac-0/iac-org-state"]'
SCAN_PLAN = {"planned_values": {"root_module": {"resources": [{"address": "google_project.root", "type": "google_project", "name": "root"}],
             "child_modules": [{"address": "module.org_setup", "child_modules": [{"address": BUCKET, "resources": [
                 {"address": f"{BUCKET}.google_storage_bucket.bucket[0]", "type": "google_storage_bucket", "name": "bucket"},
             ]}]}]}}}
TRIVY_NAME = f"google_storage_bucket.bucket_{hashlib.md5(BUCKET.encode()).hexdigest()}"


def trivy_report(*items) -> str:
    return json.dumps({"Results": [{"Target": "main.tf", "Misconfigurations": [
        {"ID": rule, "Title": f"title {rule}", "Severity": "HIGH", "Status": "FAIL", "CauseMetadata": {"Resource": resource}}
        for rule, resource in items] + [{"ID": "GCP-0001", "Title": "passing", "Severity": "LOW", "Status": "PASS"}]}]})


def checkov_report(*items) -> str:
    return json.dumps({"summary": {"parsing_errors": 0}, "results": {"failed_checks": [
        {"check_id": rule, "check_name": f"name {rule}", "resource": resource} for rule, resource in items]}})


def fake_scanners(monkeypatch, trivy=None, checkov=None):
    answers = {"trivy": trivy, "checkov": checkov}
    seen = []
    monkeypatch.setattr(lzctl.shutil, "which", lambda name: f"/fake/{name}" if answers.get(name) is not None else None)

    def run(argv, **kwargs):
        seen.append((Path(argv[0]).name, argv, Path(kwargs["cwd"]), sorted(p.name for p in Path(kwargs["cwd"]).iterdir())))
        answer = answers[Path(argv[0]).name]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(lzctl.subprocess, "run", run)
    return seen


def test_trivy_resource_names_map_back_to_plan_addresses():
    names = lzctl._trivy_addresses(SCAN_PLAN)
    assert names[TRIVY_NAME] == f"{BUCKET}.google_storage_bucket.bucket"
    assert names["google_project.root"] == "google_project.root"


def test_scans_run_offline_on_the_plan_json(monkeypatch):
    seen = fake_scanners(monkeypatch, completed(trivy_report()), completed(checkov_report()))
    checks, findings = lzctl._plan_scans({"0-org-setup": SCAN_PLAN, "2-networking": SCAN_PLAN}, "gcp", None)
    assert findings == [] and {c["status"] for c in checks} == {"passed"}
    by_tool = {}
    for tool, argv, cwd, names in seen:
        by_tool.setdefault(tool, []).append((argv, cwd, names))
    assert len(by_tool["trivy"]) == len(by_tool["checkov"]) == 2
    trivy_argv = by_tool["trivy"][0][0]
    checkov_argv = by_tool["checkov"][0][0]
    assert "--skip-check-update" in trivy_argv and "--skip-download" in checkov_argv
    assert checkov_argv[checkov_argv.index("--framework") + 1] == "terraform_plan" and "tfplan.json" in checkov_argv
    assert all(names == ["tfplan.json"] for runs in by_tool.values() for _, _, names in runs)


def test_open_findings_carry_the_resource_and_the_stage_root(monkeypatch):
    fake_scanners(monkeypatch, completed(trivy_report(("GCP-9999", TRIVY_NAME))),
                  completed(checkov_report(("CKV_GCP_9999", f"{BUCKET}.google_storage_bucket.bucket[0]")), returncode=1))
    checks, findings = lzctl._plan_scans({"2-networking": SCAN_PLAN}, "gcp", None)
    assert statuses(checks) == {"trivy": "failed", "checkov": "failed"}
    assert [(f["rule"], f["path"], f["resource"]) for f in findings] == [
        ("check.trivy.GCP-9999", "2-networking/main.tf", f"{BUCKET}.google_storage_bucket.bucket"),
        ("check.checkov.CKV_GCP_9999", "2-networking/main.tf", f"{BUCKET}.google_storage_bucket.bucket[0]"),
    ]
    assert "[HIGH]" in findings[0]["message"] and BUCKET in findings[0]["message"]
    explained = lzctl.explain_findings(findings, None)
    assert {e["cause"]["kind"] for e in explained} == {"scan"} and BUCKET in explained[0]["cause"]["advice"]


def test_reviewed_baseline_findings_are_accepted_with_their_reason(monkeypatch):
    # GCP-0066 and CKV_GCP_114 on the IaC state buckets are in policies/gcp/accepted-findings.yaml.
    fake_scanners(monkeypatch, completed(trivy_report(("GCP-0066", TRIVY_NAME))),
                  completed(checkov_report(("CKV_GCP_114", f"{BUCKET}.google_storage_bucket.bucket[0]")), returncode=1))
    checks, findings = lzctl._plan_scans({"0-org-setup": SCAN_PLAN}, "gcp", None)
    assert findings == [] and statuses(checks) == {"trivy": "passed", "checkov": "passed"}
    trivy = next(c for c in checks if c["name"] == "trivy")
    assert trivy["detail"] == "0-org-setup: no misconfigurations, 1 accepted"
    [accepted] = trivy["accepted"]
    assert accepted["rule"] == "GCP-0066" and accepted["stage"] == "0-org-setup" and "regulated profile" in accepted["reason"]
    # The same rule on the same resource in another stage is not covered.
    fake_scanners(monkeypatch, completed(trivy_report(("GCP-0066", TRIVY_NAME))), completed(checkov_report()))
    _, findings = lzctl._plan_scans({"2-networking": SCAN_PLAN}, "gcp", None)
    assert [f["rule"] for f in findings] == ["check.trivy.GCP-0066"]


@pytest.mark.parametrize("tool", SCANS)
@pytest.mark.parametrize("bad", [completed("not json"), completed("", returncode=2, stderr="boom\nmore"),
                                 subprocess.TimeoutExpired("x", 120), OSError("cannot execute")])
def test_scanner_errors_are_findings_not_passes(monkeypatch, tool, bad):
    clean = {"trivy": completed(trivy_report()), "checkov": completed(checkov_report())}
    fake_scanners(monkeypatch, **{**clean, tool: bad})
    checks, findings = lzctl._plan_scans({"0-org-setup": SCAN_PLAN}, "gcp", None)
    assert statuses(checks)[tool] == "failed"
    assert [(f["rule"], f["path"]) for f in findings] == [(f"check.{tool}.error", "main.tf")]


def test_checkov_parsing_errors_and_report_lists(monkeypatch):
    parse_error = json.dumps({"summary": {"parsing_errors": 1}, "results": {"failed_checks": []}})
    fake_scanners(monkeypatch, completed(trivy_report()), completed(f"[{parse_error}, {checkov_report()}]"))
    _, findings = lzctl._plan_scans({"0-org-setup": SCAN_PLAN}, "gcp", None)
    assert [f["rule"] for f in findings] == ["check.checkov.parse"]


def test_missing_scanners_are_skipped_and_no_plan_is_not_a_pass(monkeypatch):
    fake_scanners(monkeypatch)
    checks, _ = lzctl._plan_scans({"0-org-setup": SCAN_PLAN}, "gcp", None)
    assert statuses(checks) == {"trivy": "skipped", "checkov": "skipped"}
    checks, _ = lzctl._plan_scans({}, "gcp", ("skipped", "there is no plan to scan; see terraform-plan"))
    assert statuses(checks) == {"trivy": "skipped", "checkov": "skipped"}


# --- the exceptions file ------------------------------------------------------------------------------

def accepted_file(tmp_path, monkeypatch, entries) -> None:
    (tmp_path / "gcp").mkdir(exist_ok=True)
    (tmp_path / "gcp" / lzctl.ACCEPTED_FINDINGS).write_text(json.dumps(entries))  # JSON is YAML
    monkeypatch.setattr(lzctl, "POLICY_DIR", tmp_path)


def entry(**fields) -> dict:
    return {"scanner": "checkov", "rule": "CKV_X", "stage": "0-org-setup", "resources": ['module.a["*"].t.n'],
            "reason": "A reason long enough to say why.", **fields}


def is_accepted(address: str, accepted, scanner="checkov", rule="CKV_X", stage="0-org-setup") -> bool:
    finding = {"rule": f"check.{scanner}.{rule}", "path": "main.tf", "message": "m", "resource": address}
    return bool(lzctl._accept([finding], scanner, stage, accepted)[1])


def test_patterns_name_resources_and_their_instances(tmp_path, monkeypatch):
    accepted_file(tmp_path, monkeypatch, [entry(), entry(rule="CKV_Y", resources=['module.a["k"].t.n["only"]'])])
    accepted = lzctl._load_accepted("gcp")
    assert is_accepted('module.a["k1"].t.n', accepted)
    assert is_accepted('module.a["k/2"].t.n["roles/owner"]', accepted) and is_accepted('module.a[""].t.n[0]', accepted)
    assert not is_accepted('module.a["k"].t.other', accepted)
    assert not is_accepted('module.a["k"].module.b["x"].t.n', accepted)  # * stays inside one key
    assert not is_accepted('module.a["k"].t.n', accepted, rule="CKV_Z")
    assert not is_accepted('module.a["k"].t.n', accepted, stage="2-networking")
    assert not is_accepted('module.a["k"].t.n', accepted, scanner="trivy")
    assert is_accepted('module.a["k"].t.n["only"]', accepted, rule="CKV_Y")
    assert not is_accepted('module.a["k"].t.n["other"]', accepted, rule="CKV_Y")


@pytest.mark.parametrize("bad", [
    {"not": "a list"},
    [entry(reason="too short")],
    [entry(scanner="tfsec")],
    [entry(stage="9-other")],
    [entry(resources=[])],
    [entry(resources=[""])],
    [{k: v for k, v in entry().items() if k != "rule"}],
])
def test_a_malformed_exceptions_file_stops_the_check(tmp_path, monkeypatch, bad):
    accepted_file(tmp_path, monkeypatch, bad)
    with pytest.raises(lzctl.CheckError):
        lzctl._load_accepted("gcp")


def test_the_shipped_exceptions_file_loads():
    accepted = lzctl._load_accepted("gcp")
    assert len(accepted) >= 20 and all(e["patterns"] for e in accepted)


# --- the golden renders, with the real tools ------------------------------------------------------------

@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    """`lzctl check` on the starter and standard golden renders, once per test run."""
    if not (shutil.which("terraform") or shutil.which("tofu")):
        pytest.skip("terraform not installed")
    base = tmp_path_factory.mktemp("golden")
    results = {}
    for name in ("starter", "standard"):
        out = render_to(base, name)
        checks, findings = lzctl.check_output(out)
        if "terraform init failed" in detail(checks, "terraform-plan") and not os.environ.get("REQUIRE_TOOLS"):
            pytest.skip(f"terraform init failed, probably no network: {detail(checks, 'terraform-plan')}")
        results[name] = (out, checks, findings)
    return results


def test_golden_renders_pass_every_check(golden):
    for name, (_, checks, findings) in golden.items():
        assert findings == [], (name, findings)
        status = statuses(checks)
        assert status["terraform-validate"] == status["terraform-plan"] == "passed", (name, checks)
        for tool in ("tflint", *SCANS):
            assert status[tool] == ("passed" if shutil.which(tool) else "skipped"), (name, tool, checks)
        assert "not_applicable" not in status.values(), (name, checks)


def test_golden_plans_cover_the_stages(golden):
    planned = detail(golden["standard"][1], "terraform-plan")
    counts = {stage: int(n) for stage, n in re.findall(r"([\w-]+): (\d+) resources planned offline", planned)}
    assert counts["0-org-setup"] > 250 and counts["2-networking"] > 150
    assert "2-networking" not in detail(golden["starter"][1], "terraform-plan")


def test_every_exception_still_matches_a_finding(golden):
    if not all(shutil.which(tool) for tool in SCANS):
        pytest.skip("trivy and checkov are both needed")
    accepted = [a for _, checks, _ in golden.values() for c in checks for a in c.get("accepted") or []]
    for e in lzctl._load_accepted("gcp"):
        for resources, pattern in zip(e["resources"], e["patterns"]):
            assert any(a["scanner"] == e["scanner"] and a["rule"] == e["rule"] and a["stage"] == e["stage"]
                       and pattern.fullmatch(a["resource"]) for a in accepted), (e["scanner"], e["rule"], resources)


def test_validation_report_lists_the_accepted_findings(golden):
    out, checks, findings = golden["standard"]
    report = json.loads((out / lzctl.REPORT_NAME).read_text())
    text = lzctl.validation_report(report, checks, findings)
    section = text.split("## Accepted baseline findings")[1].split("## What has not been validated")[0]
    if any(c.get("accepted") for c in checks):
        assert "| checkov |" in section or "| trivy |" in section
    if not any(c["status"] in ("skipped", "not_applicable") for c in checks):
        assert "**Passed:** every check ran and found nothing." in text


@pytest.fixture(scope="module")
def bucket_plan(tmp_path_factory, golden):
    """A real offline plan of one bare bucket, which both scanners flag and no exception covers."""
    work = tmp_path_factory.mktemp("bucket")
    (work / "main.tf").write_text('resource "google_storage_bucket" "b" {\n  name     = "lzctl-test-bucket"\n  location = "US"\n}\n')
    (work / lzctl.OFFLINE_PROVIDERS[0]).write_text(lzctl.OFFLINE_PROVIDERS[1])
    exe = shutil.which("terraform") or shutil.which("tofu")
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    env.setdefault("TF_PLUGIN_CACHE_DIR", str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "lzctl" / "terraform-plugins"))
    for args in (["init", "-backend=false", "-input=false"], ["plan", "-refresh=false", "-lock=false", "-input=false", "-out=p"]):
        subprocess.run([exe, *args], cwd=work, env=env, check=True, capture_output=True, timeout=600)
    return json.loads(subprocess.run([exe, "show", "-json", "p"], cwd=work, env=env, check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize("tool", SCANS)
def test_real_scanners_flag_an_unaccepted_resource_in_a_plan(bucket_plan, tool):
    if not shutil.which(tool):
        pytest.skip(f"{tool} not installed")
    checks, findings = lzctl._plan_scans({"0-org-setup": bucket_plan}, "gcp", None)
    ours = [f for f in findings if f["rule"].startswith(f"check.{tool}.")]
    assert statuses(checks)[tool] == "failed" and ours
    assert all(f["resource"].startswith("google_storage_bucket.b") and f["path"] == "main.tf" for f in ours)

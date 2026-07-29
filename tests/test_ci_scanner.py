"""Tests for the CI/CD configuration scanner.

Covers:

- GitHub Actions workflow YAML parsing
- Workflow-level, job-level, and step-level ``env:`` blocks
- ``${{ secrets.X }}`` and ``${{ vars.X }}`` expression detection
- ``secrets:`` blocks in reusable workflow calls
- Trigger event extraction
- Edge cases: empty files, missing files, invalid YAML, no env blocks
"""

from pathlib import Path

import pytest
import yaml

from envcheck.scanners.ci import (
    CiScanResult,
    CiSecretEntry,
    CiVarEntry,
    CiWorkflowScanResult,
    scan_ci_workflow,
    scan_ci_workflows,
)


# ===================================================================
# Workflow-level env tests
# ===================================================================


class TestWorkflowLevelEnv:
    def test_workflow_level_env(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": ["push", "pull_request"],
            "env": {
                "NODE_VERSION": "18",
                "CI": "true",
                "REGISTRY": "ghcr.io",
            },
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo hello"}],
                }
            },
        }
        f = tmp_path / "ci.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.source == str(f.resolve())
        assert result.workflow_name == "CI"
        assert result.triggers == ["push", "pull_request"]
        assert result.variables["NODE_VERSION"].value == "18"
        assert result.variables["CI"].value == "true"
        assert result.variables["REGISTRY"].value == "ghcr.io"
        assert result.variables["NODE_VERSION"].scope == "workflow"
        assert result.variables["NODE_VERSION"].source_name == "CI"

    def test_workflow_env_with_secret_ref(self, tmp_path: Path):
        workflow = {
            "name": "Deploy",
            "on": "push",
            "env": {
                "API_TOKEN": "${{ secrets.DEPLOY_TOKEN }}",
            },
            "jobs": {
                "deploy": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "deploy.sh"}],
                }
            },
        }
        f = tmp_path / "deploy.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["API_TOKEN"].has_secret_ref is True
        assert result.variables["API_TOKEN"].value == "${{ secrets.DEPLOY_TOKEN }}"
        assert "DEPLOY_TOKEN" in result.secrets
        assert result.secrets["DEPLOY_TOKEN"].scope == "workflow"

    def test_workflow_env_with_var_ref(self, tmp_path: Path):
        workflow = {
            "name": "Test",
            "on": "pull_request",
            "env": {
                "NODE_VER": "${{ vars.NODE_VERSION }}",
            },
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "npm test"}],
                }
            },
        }
        f = tmp_path / "test.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["NODE_VER"].has_var_ref is True
        assert result.variables["NODE_VER"].value == "${{ vars.NODE_VERSION }}"

    def test_no_workflow_name(self, tmp_path: Path):
        workflow = {
            "on": "push",
            "env": {"FOO": "bar"},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo hi"}],
                }
            },
        }
        f = tmp_path / "unnamed.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.workflow_name is None
        assert result.variables["FOO"].source_name == "(unnamed)"

    def test_single_event_string(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
        }
        f = tmp_path / "single.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert result.triggers == ["push"]

    def test_event_mapping(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": {
                "push": {"branches": ["main"]},
                "pull_request": {"branches": ["*"]},
            },
            "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
        }
        f = tmp_path / "multi.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert sorted(result.triggers) == sorted(["push", "pull_request"])


# ===================================================================
# Job-level env tests
# ===================================================================


class TestJobLevelEnv:
    def test_job_level_env(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "env": {
                        "BUILD_ENV": "production",
                        "CACHE_KEY": "build-${{ github.sha }}",
                    },
                    "steps": [{"run": "npm run build"}],
                }
            },
        }
        f = tmp_path / "job_env.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["BUILD_ENV"].value == "production"
        assert result.variables["BUILD_ENV"].scope == "job"
        assert result.variables["BUILD_ENV"].source_name == "build"
        assert result.variables["CACHE_KEY"].scope == "job"

    def test_job_env_overrides_workflow_env(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "env": {"LOG_LEVEL": "info"},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "env": {"LOG_LEVEL": "debug"},
                    "steps": [{"run": "echo"}],
                }
            },
        }
        f = tmp_path / "override.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        # Last value wins — job overrides workflow
        assert result.variables["LOG_LEVEL"].value == "debug"
        assert result.variables["LOG_LEVEL"].scope == "job"

    def test_multiple_jobs(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "env": {"NODE_ENV": "production"},
                    "steps": [{"run": "build"}],
                },
                "test": {
                    "runs-on": "ubuntu-latest",
                    "env": {"NODE_ENV": "test"},
                    "steps": [{"run": "test"}],
                },
            },
        }
        f = tmp_path / "multi_job.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert len(result.jobs) == 2
        # Last job wins for same key name
        assert result.variables["NODE_ENV"].value == "test"
        assert result.variables["NODE_ENV"].scope == "job"

    def test_job_secret_ref(self, tmp_path: Path):
        workflow = {
            "name": "Deploy",
            "on": "push",
            "jobs": {
                "deploy": {
                    "runs-on": "ubuntu-latest",
                    "env": {"SSH_KEY": "${{ secrets.SSH_PRIVATE_KEY }}"},
                    "steps": [{"run": "deploy.sh"}],
                }
            },
        }
        f = tmp_path / "job_secret.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["SSH_KEY"].has_secret_ref is True
        assert "SSH_PRIVATE_KEY" in result.secrets
        assert result.secrets["SSH_PRIVATE_KEY"].scope == "job"
        assert result.secrets["SSH_PRIVATE_KEY"].source_name == "deploy"


# ===================================================================
# Step-level env tests
# ===================================================================


class TestStepLevelEnv:
    def test_step_level_env(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {
                            "name": "Install dependencies",
                            "env": {"NPM_TOKEN": "${{ secrets.NPM_TOKEN }}"},
                            "run": "npm ci",
                        },
                        {
                            "name": "Build",
                            "env": {"NODE_OPTIONS": "--max-old-space-size=4096"},
                            "run": "npm run build",
                        },
                    ],
                }
            },
        }
        f = tmp_path / "step_env.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["NPM_TOKEN"].scope == "step"
        assert result.variables["NPM_TOKEN"].source_name == "build/Install dependencies"
        assert result.variables["NPM_TOKEN"].has_secret_ref is True
        assert result.variables["NODE_OPTIONS"].scope == "step"
        assert result.variables["NODE_OPTIONS"].source_name == "build/Build"
        assert "NPM_TOKEN" in result.secrets

    def test_step_env_overrides_job_env(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "env": {"DB_URL": "localhost"},
                    "steps": [
                        {
                            "name": "Test",
                            "env": {"DB_URL": "staging.example.com"},
                            "run": "npm test",
                        }
                    ],
                }
            },
        }
        f = tmp_path / "step_override.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        # Last value wins — step overrides job
        assert result.variables["DB_URL"].value == "staging.example.com"
        assert result.variables["DB_URL"].scope == "step"

    def test_unnamed_steps(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"env": {"KEY_A": "val_a"}, "run": "cmd_a"},
                        {"env": {"KEY_B": "val_b"}, "run": "cmd_b"},
                    ],
                }
            },
        }
        f = tmp_path / "unnamed_steps.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["KEY_A"].source_name == "build/step_0"
        assert result.variables["KEY_B"].source_name == "build/step_1"

    def test_secret_ref_in_run_field(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "deploy": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {
                            "name": "Deploy",
                            "run": "deploy.sh --token ${{ secrets.DEPLOY_TOKEN }}",
                        }
                    ],
                }
            },
        }
        f = tmp_path / "run_secret.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert "DEPLOY_TOKEN" in result.secrets
        assert result.secrets["DEPLOY_TOKEN"].reference_type == "expression"
        assert result.secrets["DEPLOY_TOKEN"].scope == "step"


# ===================================================================
# Secrets block (reusable workflow) tests
# ===================================================================


class TestSecretsBlock:
    def test_secrets_inherit(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "workflow_call",
            "jobs": {
                "call-deploy": {
                    "uses": "org/deploy-action/.github/workflows/deploy.yml@v1",
                    "secrets": {"inherit": True},
                }
            },
        }
        f = tmp_path / "reusable.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert "__inherit__" in result.secrets
        assert result.secrets["__inherit__"].reference_type == "secrets_block"

    def test_secrets_mapping(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "workflow_call",
            "jobs": {
                "call-deploy": {
                    "uses": "org/deploy-action/.github/workflows/deploy.yml@v1",
                    "secrets": {
                        "DEPLOY_KEY": "${{ secrets.DEPLOY_KEY }}",
                        "API_TOKEN": "${{ secrets.API_TOKEN }}",
                    },
                }
            },
        }
        f = tmp_path / "secrets_map.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert "DEPLOY_KEY" in result.secrets
        assert result.secrets["DEPLOY_KEY"].reference_type == "secrets_block"
        assert result.secrets["DEPLOY_KEY"].source_name == "call-deploy"
        assert "API_TOKEN" in result.secrets
        # Both secrets block and expression tracking
        assert result.secrets["DEPLOY_KEY"].reference_type == "secrets_block"

    def test_secrets_expression_in_value(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "workflow_call",
            "jobs": {
                "call-deploy": {
                    "uses": "org/deploy-action/.github/workflows/deploy.yml@v1",
                    "secrets": {
                        "TOKEN": "${{ secrets.ORG_TOKEN }}",
                    },
                }
            },
        }
        f = tmp_path / "secrets_expr.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert "ORG_TOKEN" in result.secrets


# ===================================================================
# Edge cases and error handling
# ===================================================================


class TestEdgeCases:
    def test_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yml"
        with pytest.raises(FileNotFoundError, match="CI workflow file not found"):
            scan_ci_workflow(missing)

    def test_invalid_yaml(self, tmp_path: Path):
        f = tmp_path / "bad.yml"
        f.write_text("[[invalid")
        with pytest.raises(ValueError, match="Invalid YAML in CI workflow"):
            scan_ci_workflow(f)

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.yml"
        f.write_text("")
        with pytest.raises(ValueError, match="CI workflow file is empty"):
            scan_ci_workflow(f)

    def test_not_a_mapping(self, tmp_path: Path):
        f = tmp_path / "list.yml"
        f.write_text(yaml.dump(["a", "b"]))
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            scan_ci_workflow(f)

    def test_no_jobs_key(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
        }
        f = tmp_path / "no_jobs.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.jobs == {}
        assert result.variables == {}

    def test_no_env_at_all(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo hello"}],
                }
            },
        }
        f = tmp_path / "no_env.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables == {}
        assert result.total_variables == 0
        assert result.total_secrets == 0

    def test_job_with_non_dict_config(self, tmp_path: Path):
        """A job with a non-dict configuration should be gracefully skipped."""
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "valid-job": {
                    "runs-on": "ubuntu-latest",
                    "env": {"FOO": "bar"},
                    "steps": [{"run": "echo"}],
                },
                "invalid-job": "not a dict",
            },
        }
        f = tmp_path / "mixed.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert result.variables["FOO"].value == "bar"
        assert len(result.jobs) == 1  # only the valid one

    def test_boolean_env_value(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "env": {"CI": True, "DEBUG": False},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo"}],
                }
            },
        }
        f = tmp_path / "bool.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert result.variables["CI"].value == "true"
        assert result.variables["DEBUG"].value == "false"

    def test_integer_env_value(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "env": {"PORT": 8080},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo"}],
                }
            },
        }
        f = tmp_path / "int.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert result.variables["PORT"].value == "8080"

    def test_none_env_value(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "env": {"FOO": None},
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo"}],
                }
            },
        }
        f = tmp_path / "none.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert result.variables["FOO"].value == ""

    def test_step_with_non_dict_config(self, tmp_path: Path):
        """A step with a non-dict config (e.g. a string) should be skipped."""
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"env": {"VALID": "ok"}, "run": "echo"},
                        "uses: actions/checkout@v4",
                        {"env": {"ALSO_VALID": "yes"}, "run": "echo"},
                    ],
                }
            },
        }
        f = tmp_path / "mixed_steps.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert result.variables["VALID"].value == "ok"
        assert result.variables["ALSO_VALID"].value == "yes"

    def test_multiple_secret_refs_in_one_value(self, tmp_path: Path):
        workflow = {
            "name": "CI",
            "on": "push",
            "jobs": {
                "build": {
                    "runs-on": "ubuntu-latest",
                    "env": {
                        "CONN_STR": "${{ secrets.DB_HOST }}:${{ secrets.DB_PORT }}",
                    },
                    "steps": [{"run": "echo"}],
                }
            },
        }
        f = tmp_path / "multi_secret.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)

        assert "DB_HOST" in result.secrets
        assert "DB_PORT" in result.secrets
        assert result.secrets["DB_HOST"].source_name == "build"

    def test_workflow_dispatch_as_trigger(self, tmp_path: Path):
        workflow = {
            "name": "Manual",
            "on": "workflow_dispatch",
            "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
        }
        f = tmp_path / "manual.yml"
        f.write_text(yaml.dump(workflow))
        result = scan_ci_workflow(f)
        assert "workflow_dispatch" in result.triggers


# ===================================================================
# Batch scan tests
# ===================================================================


class TestBatchScanning:
    def test_scan_ci_workflows(self, tmp_path: Path):
        a = tmp_path / "ci.yml"
        a.write_text(
            yaml.dump(
                {
                    "name": "CI",
                    "on": "push",
                    "env": {"A": "1"},
                    "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
                }
            )
        )
        b = tmp_path / "deploy.yml"
        b.write_text(
            yaml.dump(
                {
                    "name": "Deploy",
                    "on": "push",
                    "env": {"B": "2"},
                    "jobs": {"deploy": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
                }
            )
        )
        results = scan_ci_workflows([a, b])
        assert len(results) == 2
        assert results[0].workflow_name == "CI"
        assert results[1].workflow_name == "Deploy"

    def test_missing_files_skipped(self, tmp_path: Path):
        existing = tmp_path / "ci.yml"
        existing.write_text(
            yaml.dump(
                {
                    "name": "CI",
                    "on": "push",
                    "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
                }
            )
        )
        results = scan_ci_workflows([tmp_path / "missing.yml", existing])
        assert len(results) == 1

    def test_all_missing_files_returns_empty(self, tmp_path: Path):
        results = scan_ci_workflows([tmp_path / "missing1.yml", tmp_path / "missing2.yml"])
        assert results == []

    def test_invalid_yaml_raises_in_batch(self, tmp_path: Path):
        good = tmp_path / "good.yml"
        good.write_text(
            yaml.dump(
                {
                    "name": "Good",
                    "on": "push",
                    "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo"}]}},
                }
            )
        )
        bad = tmp_path / "bad.yml"
        bad.write_text("[[invalid")
        with pytest.raises(ValueError, match="Invalid YAML in CI workflow"):
            scan_ci_workflows([good, bad])


# ===================================================================
# CiScanResult composition
# ===================================================================


class TestCiScanResult:
    def test_all_variables_aggregation(self):
        from envcheck.scanners.ci import CiScanResult, CiVarEntry

        a = CiWorkflowScanResult(
            source="/ci.yml",
            variables={
                "A": CiVarEntry(
                    key="A", value="1", source_file="/ci.yml",
                    scope="workflow", source_name="CI",
                ),
            },
        )
        b = CiWorkflowScanResult(
            source="/deploy.yml",
            variables={
                "B": CiVarEntry(
                    key="B", value="2", source_file="/deploy.yml",
                    scope="workflow", source_name="Deploy",
                ),
            },
        )
        combined = CiScanResult(workflow_results=[a, b])
        all_vars = combined.all_variables
        assert "A" in all_vars
        assert "B" in all_vars

    def test_all_secrets_aggregation(self):
        from envcheck.scanners.ci import CiScanResult, CiSecretEntry

        a = CiWorkflowScanResult(
            source="/ci.yml",
            secrets={
                "TOKEN": CiSecretEntry(
                    name="TOKEN", source_file="/ci.yml",
                    scope="workflow", source_name="CI",
                    reference_type="expression",
                ),
            },
        )
        b = CiWorkflowScanResult(
            source="/deploy.yml",
            secrets={
                "SSH_KEY": CiSecretEntry(
                    name="SSH_KEY", source_file="/deploy.yml",
                    scope="job", source_name="deploy",
                    reference_type="expression",
                ),
            },
        )
        combined = CiScanResult(workflow_results=[a, b])
        all_secrets = combined.all_secrets
        assert "TOKEN" in all_secrets
        assert "SSH_KEY" in all_secrets

    def test_empty_result(self):
        combined = CiScanResult()
        assert combined.all_variables == {}
        assert combined.all_secrets == {}

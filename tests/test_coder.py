from __future__ import annotations

import subprocess

from zflow.coder import (
    build_coder_instruction,
    diff_stat,
    git_head_sha,
    is_git_repo,
    run_coder_node,
    run_with_timeout,
)
from zflow.config import Settings

from conftest import make_node


def test_non_git_dir_returns_friendly_error(project_dir):
    node = make_node("c1", type="coder")
    out = run_coder_node(node, "faça algo", Settings(project_dir=project_dir))
    assert "git init" in out.error
    assert out.node_id == "c1"


def test_is_git_repo_and_head(project_dir):
    assert not is_git_repo(project_dir)
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    assert is_git_repo(project_dir)
    assert git_head_sha(project_dir) is None  # sem commits ainda
    (project_dir / "a.txt").write_text("oi")
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=project_dir, check=True,
    )
    sha = git_head_sha(project_dir)
    assert sha


def test_diff_stat_between_commits(project_dir):
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    (project_dir / "a.txt").write_text("um\n")
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True)
    subprocess.run([*git, "commit", "-qm", "1"], cwd=project_dir, check=True)
    before = git_head_sha(project_dir)
    (project_dir / "a.txt").write_text("um\ndois\n")
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True)
    subprocess.run([*git, "commit", "-qm", "2"], cwd=project_dir, check=True)
    after = git_head_sha(project_dir)
    stat = diff_stat(project_dir, before, after)
    assert "a.txt" in stat
    assert diff_stat(project_dir, after, after) == ""


def test_build_instruction_has_extra_prompt_first():
    node = make_node("c1", type="coder", extra_prompt="Use type hints.")
    ins = build_coder_instruction(node, "## Tarefa\ncrie um módulo soma")
    assert ins.startswith("Use type hints.")
    assert "crie um módulo soma" in ins
    assert "não invente dependências" in ins.lower()


def test_run_with_timeout_returns_and_times_out():
    result, timed_out = run_with_timeout(lambda: 42, 5)
    assert result == 42 and not timed_out
    import time

    result, timed_out = run_with_timeout(lambda: time.sleep(2), 0.05)
    assert timed_out

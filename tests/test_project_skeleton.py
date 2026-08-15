# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) M0 没有业务代码，因此测试对象是“仓库契约”：关键目录、文档、配置必须存在且关键内容不缺。
# 2) 只断言存在性和关键词/章节标题，不逐字锁死文档，后续润色文档不会误伤测试。
# 3) 用 pathlib 从测试文件推导项目根，不依赖运行目录；同时避开 3.11+ 的 tomllib，兼容当前 3.10 开发机。

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(rel_path: str) -> str:
    p = ROOT / rel_path
    assert p.is_file(), f"缺少文件: {rel_path}"
    return p.read_text(encoding="utf-8")


def test_directory_layout_exists():
    for rel in [
        "src/personal_data_assistant",
        "tests",
        "docs",
        "data",
        "evals",
    ]:
        assert (ROOT / rel).is_dir(), f"缺少目录: {rel}"
    for rel in ["data/.gitkeep", "evals/.gitkeep", "src/personal_data_assistant/.gitkeep"]:
        assert (ROOT / rel).is_file(), f"空目录占位文件缺失: {rel}"


def test_readme_exists_and_describes_milestones():
    text = read_text("README.md")
    assert "Personal Data Assistant" in text
    assert "M0" in text and "M5" in text
    assert "DeepSeek" in text
    assert "SQLite" in text


def test_readme_has_design_tradeoffs_section():
    text = read_text("README.md")
    assert "## 设计取舍" in text, "README 缺少设计取舍章节"
    assert "function calling" in text, "README 设计取舍必须解释为何不用原生 function calling"


def test_architecture_doc_has_required_sections():
    text = read_text("docs/architecture.md")
    for section in ["模块划分", "数据流", "目录安排", "评测方案", "降级与 B 计划"]:
        assert f"## {section}" in text, f"架构文档缺少章节: {section}"


def test_progress_has_three_required_records():
    text = read_text("PROGRESS.md")
    assert "## M0" in text
    for field in ["### 进度", "### 踩过的坑", "### 复现要点"]:
        assert field in text, f"PROGRESS.md 缺少: {field}"


def test_pyproject_declares_python_pytest_and_httpx():
    text = read_text("pyproject.toml")
    assert 'requires-python = ">=3.10' in text, "pyproject 未声明 Python >=3.10"
    assert "pytest" in text, "pyproject 未声明 pytest"
    assert "httpx" in text, "pyproject 未声明 M1 的 httpx 依赖"
    assert "integration" in text, "pyproject 未声明 integration 测试标记"


def test_gitignore_keeps_secrets_and_cache_out():
    text = read_text(".gitignore")
    for item in [".env", "__pycache__/", ".venv/"]:
        assert item in text, f".gitignore 缺少: {item}"


def test_env_example_has_key_placeholder_not_real_secret():
    text = read_text(".env.example")
    assert "DEEPSEEK_API_KEY=" in text
    assert "sk-" not in text, ".env.example 不应出现真实密钥"

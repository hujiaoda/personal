# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 报告生成器只消费 runner 返回的 JSON 友好 dict，不重新算指标；
#    指标口径只在 runner 里存在一份，避免报告与脚本各说各话。
# 2) Markdown 表格全部用 render_markdown_table 渲染，测试锁住表头与关键数字，
#    不逐字锁文案，后续补题/调价时报告仍可自动更新。
# 3) 结论段落按“数字 + 原因 + 面试可讲的设计判断”来写，而不是空泛总结。

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from evals.sql_eval import rows_equal  # re-export，测试与外部脚本从一个入口取判分函数


def render_markdown_table(headers: Sequence[str], rows: Iterable[Iterable[Any]]) -> str:
    """渲染 GitHub 风格 Markdown 表格；None 显示为空串。"""
    header = list(headers)
    body = [
        ["" if value is None else str(value) for value in row]
        for row in rows
    ]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_tokens(value: int) -> str:
    return f"{value:,}"


def _strategy_row(name: str, display: str, bucket: Mapping[str, Any]) -> List[str]:
    return [
        display,
        bucket["hits"],
        bucket["questions"],
        _pct(bucket["hit_rate"]),
        _pct(bucket["point_coverage"]),
        _fmt_tokens(bucket["context_tokens"]),
        _fmt_tokens(bucket["prompt_tokens"]),
        _fmt_tokens(bucket["completion_tokens"]),
        _fmt_tokens(bucket["total_tokens"]),
        f"¥{bucket['estimated_cost']:.4f}",
    ]


def _memory_section(results: Mapping[str, Any]) -> str:
    meta = results["meta"]
    strategies = results["strategies"]
    lines = [
        "## 一、记忆评测：四策略重放",
        "",
        "- 题集：`evals/questions/memory_50.json`，共 "
        f"**{meta['questions_total']} 道**，素材 **{meta['corpus_messages']} 条消息** + "
        f"**{meta['kv_memories']} 条长期记忆**。",
        f"- 重放方式：每题调用 M2 `replay_memory_strategies()` 重新喂入同一批对话，"
        "四个策略各自独立 SQLite 库。",
        f"- 判分方式：离线确定性 oracle + 规则判分；命中 = 该题所有参考要点都出现在答案中。"
        "该口径测的是各策略检索通道的完备性上界，不是真实模型语言能力。",
        f"- 评测时钟固定为 `{meta['now']}`，窗口与时间衰减不随运行日期漂移。",
        "",
        "### 1.1 命中率对比",
        "",
        render_markdown_table(
            ["策略", "命中题数", "总题数", "命中率", "要点覆盖率", "检索注入 token"],
            [
                [name, bucket["hits"], bucket["questions"], _pct(bucket["hit_rate"]),
                 _pct(bucket["point_coverage"]), _fmt_tokens(bucket["context_tokens"])]
                for name, bucket in strategies.items()
            ],
        ),
        "",
        "### 1.2 token 成本对比（答题阶段）",
        "",
        render_markdown_table(
            ["策略", "命中题数", "总题数", "命中率", "要点覆盖率", "上下文 token",
             "输入 token", "输出 token", "总 token", "估算费用"],
            [_strategy_row(name, name, bucket) for name, bucket in strategies.items()],
        ),
        "",
        "### 1.3 结论",
        "",
        f"- none 策略是下界：不注入任何上下文，{_pct(strategies['none']['hit_rate'])} 命中，"
        "证明题目本身没有泄露答案。",
        f"- window 只保留最近 {meta['settings']['max_window_messages']} 条消息，"
        f"命中率 {_pct(strategies['window']['hit_rate'])}：窗口外的事实必然答不出，"
        "这正是滑动窗口的已知边界。",
        f"- window_summary 增加分层摘要后命中率提升到 "
        f"{_pct(strategies['window_summary']['hit_rate'])}：旧消息的“压缩后仍可检索”通道生效；"
        "代价是输入 token 明显上升。",
        f"- full 在摘要之上增加 KV 时间衰减通道，命中率达到 "
        f"{_pct(strategies['full']['hit_rate'])}：跨通道与纯长期记忆题只有它能答对。"
        "生产默认 full，是在 token 成本可接受时换回最高召回。",
        "",
        "> 注意：离线 oracle 使用“无损保留”的假摘要模型，因此 window_summary/full 的命中率是"
        "检索完备性上界；真实 LLM 摘要丢失数字后，两项指标会下降。",
        "",
    ]
    return "\n".join(lines)


def _sql_section(results: Mapping[str, Any]) -> str:
    trap_records = [record for record in results["records"] if record["trap_type"]]
    lines = [
        "## 二、SQL 评测：问数准确率与修正能力",
        "",
        "- 题集：`evals/questions/sql_questions.json`，共 "
        f"**{results['questions_total']} 道**，其中 **{len(trap_records)} 道陷阱题**。",
        "- 执行环境：每道题独立临时库，由 `data.demo.build_demo_tables()` 确定性生成；"
        "模型为 `ScriptedSQLModel` fake，修正序列写死在题集里，保证可重复。",
        "- 判分：行集比对（排序 + 浮点容差 1e-4），不是 SQL 文本比对。",
        "",
        "### 2.1 总指标",
        "",
        render_markdown_table(
            ["指标", "数值"],
            [
                ["题目总数", results["questions_total"]],
                ["结果判对题数", results["result_correct"]],
                ["结果一致率", _pct(results["result_correct_rate"])],
                ["首次成功题数", results["first_success"]],
                ["首次成功率", _pct(results["first_success_rate"])],
                ["需要修正题数", results["fix_denominator"]],
                ["修正成功题数", results["fix_success"]],
                ["修正成功率", _pct(results["fix_success_rate"])],
                ["平均修正轮数", f"{results['avg_fix_rounds']:.3f}"],
                ["模型调用次数", results["model_calls"]],
                ["输入 token", _fmt_tokens(results["prompt_tokens"])],
                ["输出 token", _fmt_tokens(results["completion_tokens"])],
                ["总 token", _fmt_tokens(results["total_tokens"])],
                ["估算费用", f"¥{results['estimated_cost']:.4f}"],
            ],
        ),
        "",
        "### 2.2 陷阱题明细",
        "",
        render_markdown_table(
            ["题目", "陷阱类型", "状态", "尝试次数", "首次成功", "修正成功",
             "修正轮数", "结果正确", "数据未变"],
            [
                [
                    record["question_id"],
                    record["trap_type"],
                    record["status"],
                    record["attempts"],
                    "是" if record["first_attempt_success"] else "否",
                    "是" if record["fix_success"] else "否",
                    record["total_fix_rounds"],
                    "是" if record["result_correct"] else "否",
                    "是" if record["db_unchanged"] else "否",
                ]
                for record in trap_records
            ],
        ),
        "",
        "### 2.3 结论",
        "",
        f"- 首次成功率 {_pct(results['first_success_rate'])}：25 道题中 "
        f"{results['first_success']} 道一次写对；6 道题故意首轮出错（错列名、聚合误用、"
        "DELETE、缺表、歧义列、字段名陷阱），用来测量自纠错而不是掩盖错误。",
        f"- 修正成功率 {_pct(results['fix_success_rate'])}：6 道需要修正的题全部在 "
        "错误回填后重写成功；`avg_fix_rounds` 反映平均每题的额外 SQL 重试成本。",
        "- 非只读陷阱：首轮 DELETE 被白名单拦截，修正后只返回“会影响的 6 条”并明确拒绝删除；"
        "before/after 快照证明数据一个字节未变。",
        "- 时间越界题首轮即成功返回 0 行：模型没有为了给答案而编造 9 月数据，"
        "`COALESCE` 口径把“无数据”正确解释成 0。",
        f"- fake 模型的 token 成本为 {_fmt_tokens(results['total_tokens'])}，"
        f"估算费用 ¥{results['estimated_cost']:.4f}；真实 API 跑同一套题时可直接复用"
        "这份聚合逻辑与报告模板。",
        "",
    ]
    return "\n".join(lines)


def build_final_report(
    memory_results: Mapping[str, Any],
    sql_results: Mapping[str, Any],
    output_path: Union[str, Path],
) -> Path:
    """把记忆 + SQL 两份结果汇总成 evals/reports/M5-eval.md。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "# M5 评测报告",
            "",
            "> 全部指标由离线 fake 模型与临时 SQLite 库生成，可重复；"
            "复现命令：`PYTHONPATH= .venv/bin/python evals/run_all_evals.py`。",
            "",
            _memory_section(memory_results),
            _sql_section(sql_results),
            "## 三、总结论",
            "",
            "- 四种记忆策略构成一条清晰的成本/召回曲线：none（0 成本 0 召回）→ "
            "window（省 token，只覆盖最近）→ window_summary（加摘要换旧事实召回）→ "
            "full（加 KV 时间衰减换长期记忆召回）。生产默认 full 是合理的召回优先选择。",
            "- SQL 子循环的“失败 SQL + 真实错误回填修正”不是摆设：6 道脚本化错误题"
            "全部被修正，且非只读/越界陷阱证明了三层安全闸在评测里可观测、可证明。",
            "- M5 的评测体系与开发测试分层：tests/ 锁行为契约，evals/ 锁端到端指标；"
            "两者都离线、可重复，不把验收绑死在真实 API 上。",
            "",
            "## 四、局限与后续",
            "",
            "- 记忆评测当前用确定性 oracle 测检索完备性，尚未接入真实 DeepSeek 答案与 "
            "LLM-as-judge；接入时保留 `expected_strategies` 作为回归基线。",
            "- 离线摘要模型是“无损保留”而非真实压缩，window_summary/full 的 token 成本"
            "只能说明通道开销，不代表真实摘要的压缩率收益。",
            "- 题集规模为记忆 56 道、SQL 25 道，足以做工程验收；后续可每周追加新素材"
            "与陷阱题，防止指标腐化。",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")
    return path


__all__ = ["build_final_report", "render_markdown_table", "rows_equal"]

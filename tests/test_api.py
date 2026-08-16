# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) M4 先测 HTTP 边界，再测页面：全部用 fastapi TestClient + fake 模型，
#    不真实调用 DeepSeek；fake 模型可脚本化输出/抛错/阻塞，分别覆盖正常、
#    模型不可用与请求超时三条降级路径。
# 2) SSE 断言锁“事件类型 + 拼接后的文本”，不逐块锁服务端分块大小；
#    分块策略是实现细节，换策略不应改测试。
# 3) /ask 的流式测试专门覆盖“工具调用折叠”：tool 事件进入折叠区，
#    用户可见的 chunk 事件只包含最终答案，不让 JSON 协议文本漏到聊天泡泡里。

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from personal_data_assistant.api.main import HealthChecker, SqlAskService, create_app
from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.llm.client import LLMResponse, TokenUsage
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.profile.habits import HabitAliasStore
from personal_data_assistant.tools.base import Tool, ToolResult

GOOD_SQL = "SELECT category, SUM(amount) AS total FROM expenses WHERE category = '餐饮' GROUP BY category"
EXPLAIN = "八月份餐饮一共 33 元。"


def make_expenses_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE expenses(id INTEGER PRIMARY KEY, date TEXT, category TEXT, item TEXT, amount REAL)"
    )
    conn.executemany(
        "INSERT INTO expenses(date, category, item, amount) VALUES (?, ?, ?, ?)",
        [
            ("2025-08-01", "餐饮", "早餐", 8.0),
            ("2025-08-02", "餐饮", "午餐", 25.0),
        ],
    )
    conn.commit()
    conn.close()
    return path


class FinalModel:
    def complete(self, messages):
        return LLMResponse(
            content='{"action":"final","answer":"回答完成"}',
            model="fake",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class ScriptedStreamModel:
    """stream_chat 按脚本逐次吐字；script 里的元素是 str/Exception。"""

    def __init__(self, scripts):
        self.scripts = [list(script) for script in scripts]
        self.calls = []

    def stream_chat(self, messages):
        self.calls.append(messages)
        script = self.scripts.pop(0)

        def iterator():
            for item in script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return iterator()

    def complete(self, messages):
        return LLMResponse(
            content='{"action":"final","answer":"降级回答"}',
            model="fake",
            usage=TokenUsage(),
        )


class ScriptedCompleteModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(
            content=item,
            model="fake",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class BlockingModel:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def complete(self, messages):
        self.entered.set()
        self.release.wait(timeout=10)
        return LLMResponse(content='{"action":"final","answer":"太晚了"}', model="fake", usage=TokenUsage())


class RaisingModel:
    def complete(self, messages):
        raise RuntimeError("模型服务连不上")


def build_assistant(tmp_path, model, tools=None):
    manager = MemoryManager(strategy="none", db_path=tmp_path / "api-pda.db")
    assistant = PersonalAssistant(model=model, tools=list(tools or []), memory_manager=manager)
    return assistant, manager


def build_sql_ask(tmp_path, model=None, *, db_path=None, habits=None):
    model = model or ScriptedCompleteModel([GOOD_SQL, EXPLAIN])
    service = SqlAskService(
        model=model,
        db_path=str(db_path or make_expenses_db(tmp_path / "api-user.db")),
        habits=habits,
        max_fix_rounds=1,
        query_timeout=2.0,
        max_rows=10,
        schema_sample_size=2,
    )
    return service, model


def build_app(tmp_path, model, *, tools=None, sql_ask=None, health_checker=None):
    assistant, manager = build_assistant(tmp_path, model, tools=tools)
    if sql_ask is None:
        service, sql_model = build_sql_ask(tmp_path)
        sql_ask = service.ask
    if health_checker is None:
        health_checker = HealthChecker(
            assistant=assistant,
            memory_db_path=tmp_path / "api-pda.db",
            user_db_path=tmp_path / "api-user.db",
        )
    return create_app(assistant=assistant, sql_ask=sql_ask, health_checker=health_checker)


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def test_health_returns_liveness(tmp_path):
    app = build_app(tmp_path, FinalModel())
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "components" in body
    assert body["components"]["assistant"]["status"] == "ok"
    assert body["components"]["memory_db"]["status"] in {"ok", "degraded"}
    assert body["components"]["model"]["status"] == "configured"


def test_health_reports_missing_user_db_as_degraded(tmp_path):
    assistant, manager = build_assistant(tmp_path, FinalModel())
    missing = tmp_path / "missing-user.db"
    sql_model = ScriptedCompleteModel([GOOD_SQL, EXPLAIN])
    service = SqlAskService(model=sql_model, db_path=str(missing))
    checker = HealthChecker(
        assistant=assistant,
        memory_db_path=tmp_path / "api-pda.db",
        user_db_path=missing,
    )
    app = create_app(assistant=assistant, sql_ask=service.ask, health_checker=checker)

    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["components"]["user_db"]["status"] == "degraded"


def test_ask_non_stream_uses_assistant_ask(tmp_path):
    model = FinalModel()
    app = build_app(tmp_path, model)
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "你好", "stream": False, "timeout": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["question"] == "你好"
    assert body["answer"] == "回答完成"
    assert body["status"] == "final"
    assert body["streamed"] is False


def test_ask_stream_renders_answer_chunks(tmp_path):
    model = ScriptedStreamModel(
        [
            ['{"action":"final","ans', 'wer":"你好', "世界", '"}'],
        ]
    )
    app = build_app(tmp_path, model)
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "你好", "stream": True, "timeout": 5})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    kinds = [event.get("type") for event in events]
    assert kinds[0] == "status"
    assert kinds[-1] == "done"
    assert events[-1]["status"] == "final"
    answer = "".join(event.get("delta", "") for event in events if event.get("type") == "chunk")
    assert answer == "你好世界"


def test_ask_stream_folds_tool_call_json(tmp_path):
    clock = Tool(
        name="clock",
        description="返回当前时间",
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda args: ToolResult(ok=True, result="当前时间 12:00"),
    )
    model = ScriptedStreamModel(
        [
            ['{"act', 'ion":"tool","tool":"clock","args":{}}'],
            ['{"action":"final","ans', 'wer":"现在 12 点"}'],
        ]
    )
    app = build_app(tmp_path, model, tools=[clock])
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "现在几点", "stream": True, "timeout": 5})

    assert response.status_code == 200
    events = parse_sse(response.text)
    tool_deltas = "".join(event.get("delta", "") for event in events if event.get("type") == "tool")
    answer = "".join(event.get("delta", "") for event in events if event.get("type") == "chunk")

    assert "clock" in tool_deltas
    assert "action" not in answer
    assert answer == "现在 12 点"
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "final"
    assert events[-1]["tool_rounds"] == 1


def test_ask_sql_rewrites_habit_alias_and_returns_chinese_explanation(tmp_path):
    manager = MemoryManager(strategy="none", db_path=tmp_path / "sql-pda.db")
    habits = HabitAliasStore(manager.db)
    habits.record_alias("饭钱", "餐饮")
    expenses_db = make_expenses_db(tmp_path / "sql-user.db")

    service, sql_model = build_sql_ask(tmp_path, db_path=expenses_db, habits=habits)
    assistant, _ = build_assistant(tmp_path, FinalModel())
    app = create_app(
        assistant=assistant,
        sql_ask=service.ask,
        health_checker=HealthChecker(
            assistant=assistant,
            memory_db_path=tmp_path / "api-pda.db",
            user_db_path=expenses_db,
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/ask_sql",
            json={"question": "八月份饭钱花了多少", "stream": False, "timeout": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["answer"] == EXPLAIN
    assert body["sql"] == GOOD_SQL
    assert body["row_count"] == 1
    assert body["alias_applied"] == [["饭钱", "餐饮"]]
    assert body["streamed"] is False
    # SQL 子流程收到的是改写后的标准说法，而不是用户黑话。
    assert "餐饮" in sql_model.calls[0][-1]["content"]
    assert "饭钱" not in sql_model.calls[0][-1]["content"]


def test_ask_sql_stream_emits_chunks_and_sql_meta(tmp_path):
    expenses_db = make_expenses_db(tmp_path / "sql-user.db")
    service, _ = build_sql_ask(tmp_path, db_path=expenses_db)
    app = build_app(tmp_path, FinalModel(), sql_ask=service.ask)

    with TestClient(app) as client:
        response = client.post(
            "/ask_sql",
            json={"question": "餐饮花了多少", "stream": True, "timeout": 5},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    chunks = [event for event in events if event.get("type") == "chunk"]
    assert chunks, "SQL 流式响应至少要有一个 chunk 事件"
    answer = "".join(event.get("delta", "") for event in chunks)
    assert answer == EXPLAIN
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "success"
    assert events[-1]["sql"] == GOOD_SQL


def test_ask_validation_error_uses_unified_chinese_error_body(tmp_path):
    app = build_app(tmp_path, FinalModel())
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "   ", "stream": False})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "question" in body["error"]["message"]


def test_ask_timeout_returns_504_unified_error(tmp_path):
    model = BlockingModel()
    app = build_app(tmp_path, model)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/ask",
                json={"question": "会阻塞的问题", "stream": False, "timeout": 0.2},
            )
    finally:
        model.release.set()

    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "timeout"
    assert "超时" in body["error"]["message"]


def test_model_error_maps_to_503_unified_error(tmp_path):
    app = build_app(tmp_path, RaisingModel())
    with TestClient(app) as client:
        response = client.post("/ask", json={"question": "你好", "stream": False, "timeout": 5})

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "model_unavailable"
    assert "模型" in body["error"]["message"]


def test_sql_ask_model_error_maps_to_503_unified_error(tmp_path):
    sql_model = ScriptedCompleteModel([RuntimeError("问数模型连不上")])
    service = SqlAskService(model=sql_model, db_path=str(make_expenses_db(tmp_path / "sql-user.db")))
    app = build_app(tmp_path, FinalModel(), sql_ask=service.ask)

    with TestClient(app) as client:
        response = client.post(
            "/ask_sql",
            json={"question": "餐饮花了多少", "stream": False, "timeout": 5},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "model_unavailable"
    assert "问数模型" in body["error"]["message"] or "模型" in body["error"]["message"]


def test_unknown_route_uses_unified_error_body(tmp_path):
    app = build_app(tmp_path, FinalModel())
    with TestClient(app) as client:
        response = client.get("/no-such-route")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]

# -*- coding: utf-8 -*-
# 测试设计取舍：
# 1) 前端“端到端检查”不引入 Node/浏览器工具链：通过 FastAPI 静态挂载把
#    index.html 完整取回，再断言页面结构、两个入口和原生 fetch SSE 解析器
#    的关键代码都存在；这锁住“页面可用”的最小契约。
# 2) 不断言 CSS 细节或文案排版，只断言稳定 id 与端点字符串，避免前端每改
#    一次样式就破坏测试。

from fastapi.testclient import TestClient

from personal_data_assistant.api.main import HealthChecker, SqlAskService, create_app
from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.memory.retriever import MemoryManager


class FinalModel:
    def complete(self, messages):
        return '{"action":"final","answer":"回答完成"}'


def build_app(tmp_path):
    manager = MemoryManager(strategy="none", db_path=tmp_path / "frontend-pda.db")
    assistant = PersonalAssistant(model=FinalModel(), tools=[], memory_manager=manager)
    sql_ask = SqlAskService(model=FinalModel(), db_path=str(tmp_path / "user.db"))
    checker = HealthChecker(
        assistant=assistant,
        memory_db_path=tmp_path / "frontend-pda.db",
        user_db_path=tmp_path / "user.db",
    )
    return create_app(assistant=assistant, sql_ask=sql_ask.ask, health_checker=checker)


def test_index_page_contains_required_elements(tmp_path):
    app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    # 页面骨架：标题、聊天区、输入框、发送按钮。
    assert "个人数据助手" in html
    assert 'id="chat"' in html
    assert 'id="question"' in html
    assert 'id="send"' in html

    # 两个入口：记忆问答走 /ask，问数走 /ask_sql。
    assert 'data-endpoint="/ask"' in html
    assert 'data-endpoint="/ask_sql"' in html

    # 原生 JS + fetch 读流，不依赖构建工具链与第三方脚本。
    assert "fetch(" in html
    assert "getReader" in html
    assert '"chunk"' in html
    assert '"tool"' in html
    assert '"done"' in html

# -*- coding: utf-8 -*-
# 设计取舍：
# 1) api 是纯增量装配层：默认 create_app() 用 config 把 DeepSeekClient、
#    MemoryManager、HabitAliasStore、PersonalAssistant 与 SqlAskService 组装好；
#    测试可以注入任意 fake 对象，路由代码不感知真实实现。
# 2) /ask 走 PersonalAssistant.ask（习惯改写 → 记忆增强 → core 循环）；
#    /ask_sql 走 SqlAskService（习惯改写 → data.ask 确定性问数子流程），
#    两条路的业务都不在 HTTP 路由里写。
# 3) 健康检查只做“进程存活 + 本地组件状态”：/health 永远返回 200 和组件明细；
#    模型真实连通性不在这里烧 token，由 /ask 的 503 统一错误承接。
# 4) 静态页面用 Starlette StaticFiles 挂到根路径，单个 index.html + 原生 JS +
#    内嵌 CSS，不引入任何前端构建工具链。

from __future__ import annotations

import sqlite3
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from personal_data_assistant.api.routes import build_router, install_error_handlers
from personal_data_assistant.app import PersonalAssistant
from personal_data_assistant.config import Settings, load_settings
from personal_data_assistant.data.ask import AskResult, ask_database
from personal_data_assistant.llm.client import DeepSeekClient
from personal_data_assistant.memory.retriever import MemoryManager
from personal_data_assistant.profile.habits import HabitAliasStore


@dataclass(frozen=True)
class AskSqlOutcome:
    """一次 /ask_sql 的完整结果：原始问题、改写后问题、问数结果与别名命中。"""

    question: str
    rewritten: str
    result: AskResult
    aliases: Tuple[Tuple[str, str], ...] = ()


class SqlAskService:
    """确定性问数入口：习惯别名改写 → data.ask 子流程。库路径装配期锁死。"""

    def __init__(
        self,
        *,
        model: Any,
        db_path: str,
        habits: Optional[HabitAliasStore] = None,
        max_fix_rounds: int = 3,
        query_timeout: float = 5.0,
        max_rows: int = 100,
        schema_sample_size: int = 3,
    ) -> None:
        if not callable(getattr(model, "complete", None)):
            raise TypeError("model 必须提供可调用的 complete(messages) 方法")
        if not db_path:
            raise ValueError("db_path 不能为空")
        self._model = model
        self._db_path = str(db_path)
        self._habits = habits
        self._max_fix_rounds = max_fix_rounds
        self._query_timeout = query_timeout
        self._max_rows = max_rows
        self._schema_sample_size = schema_sample_size

    def ask(self, question: str) -> AskSqlOutcome:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")
        original = question.strip()
        rewritten = original
        aliases: Tuple[Tuple[str, str], ...] = ()
        if self._habits is not None:
            rewrite_result = self._habits.rewrite_question(original)
            rewritten = rewrite_result.text
            aliases = rewrite_result.applied

        result = ask_database(
            rewritten,
            self._db_path,
            self._model,
            max_fix_rounds=self._max_fix_rounds,
            query_timeout=self._query_timeout,
            max_rows=self._max_rows,
            schema_sample_size=self._schema_sample_size,
        )
        return AskSqlOutcome(question=original, rewritten=rewritten, result=result, aliases=aliases)


def _probe_sqlite_file(path: Any, *, readonly: bool = False) -> dict:
    if not path:
        return {"status": "degraded", "detail": "未配置数据库路径"}
    file_path = Path(str(path))
    if not file_path.exists():
        return {"status": "degraded", "detail": "文件不存在"}
    try:
        if readonly:
            uri = f"file:{urllib.parse.quote(str(file_path.resolve()), safe='/')}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        else:
            conn = sqlite3.connect(str(file_path), timeout=1.0)
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 健康检查只上报状态，不抛堆栈
        return {"status": "degraded", "detail": f"打开失败: {type(exc).__name__}: {exc}"}
    return {"status": "ok", "detail": "可打开"}


class HealthChecker:
    """存活检查：进程在就返回 200；组件状态只说明本地可装配/可打开情况。"""

    def __init__(
        self,
        *,
        assistant: Any,
        model: Any = None,
        memory_db_path: Any = None,
        user_db_path: Any = None,
        model_probe: Optional[Callable[[], Tuple[bool, str]]] = None,
    ) -> None:
        self._assistant = assistant
        # api 层允许读一次装配对象，仅为健康上报；业务路由不依赖这个私有属性。
        self._model = model if model is not None else getattr(assistant, "_model", None)
        self._memory_db_path = memory_db_path
        self._user_db_path = user_db_path
        self._model_probe = model_probe

    def snapshot(self) -> dict:
        model = self._model
        if self._model_probe is not None:
            try:
                ok, detail = self._model_probe()
            except Exception as exc:  # noqa: BLE001 —— 探测失败也要回组件状态
                ok, detail = False, f"探测异常: {exc}"
            model_status = {"status": "ok" if ok else "degraded", "detail": detail}
        elif model is None or not callable(getattr(model, "complete", None)):
            model_status = {"status": "degraded", "detail": "模型未装配或缺少 complete()"}
        else:
            model_status = {
                "status": "configured",
                "detail": "已装配可调用模型；真实连通性由 /ask 请求时的超时与重试兜底",
            }

        return {
            "status": "ok",
            "service": "personal-data-assistant",
            "components": {
                "assistant": {
                    "status": "ok" if self._assistant is not None else "degraded",
                    "detail": "已装配" if self._assistant is not None else "未装配",
                },
                "model": model_status,
                "memory_db": _probe_sqlite_file(self._memory_db_path),
                "user_db": _probe_sqlite_file(self._user_db_path, readonly=True),
            },
        }


def _build_default_stack(settings: Settings) -> Tuple[PersonalAssistant, SqlAskService, HealthChecker, List[Any]]:
    """按 config 装配真实生产对象；返回的 owned 对象在应用关闭时统一 close。"""
    model = DeepSeekClient(settings)
    memory_manager = MemoryManager.from_settings(settings, model=model)
    habits = HabitAliasStore(memory_manager.db)
    assistant = PersonalAssistant(
        model=model,
        tools=[],
        memory_manager=memory_manager,
        user_db_path=settings.sql_user_db_path,
        max_sql_fix_rounds=settings.max_sql_fix_rounds,
        sql_query_timeout=settings.sql_query_timeout,
        sql_row_limit=settings.sql_row_limit,
        habits=habits,
    )
    sql_ask = SqlAskService(
        model=model,
        db_path=settings.sql_user_db_path,
        habits=habits,
        max_fix_rounds=settings.max_sql_fix_rounds,
        query_timeout=settings.sql_query_timeout,
        max_rows=settings.sql_row_limit,
        schema_sample_size=settings.sql_schema_sample_size,
    )
    health_checker = HealthChecker(
        assistant=assistant,
        model=model,
        memory_db_path=settings.memory_db_path,
        user_db_path=settings.sql_user_db_path,
    )
    return assistant, sql_ask, health_checker, [model, memory_manager]


def create_app(
    settings: Optional[Settings] = None,
    *,
    assistant: Optional[PersonalAssistant] = None,
    sql_ask: Optional[Callable[[str], AskSqlOutcome]] = None,
    health_checker: Optional[HealthChecker] = None,
) -> FastAPI:
    """创建 FastAPI 应用。

    测试可注入 assistant/sql_ask/health_checker；生产直接 create_app() 即可。
    """
    owned: List[Any] = []
    if settings is None and assistant is None:
        settings = load_settings()
    if assistant is None:
        if settings is None:
            settings = load_settings()
        assistant, default_sql_ask, default_health, owned = _build_default_stack(settings)
        if sql_ask is None:
            sql_ask = default_sql_ask.ask
        if health_checker is None:
            health_checker = default_health
    else:
        if sql_ask is None:
            raise TypeError("注入 assistant 时也必须注入 sql_ask（或直接使用 create_app(settings=...) 自动装配）")
        if health_checker is None:
            health_checker = HealthChecker(assistant=assistant)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for resource in reversed(owned):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 —— 关闭阶段不允许覆盖退出
                    pass

    app = FastAPI(title="个人数据助手", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(build_router(assistant=assistant, sql_ask=sql_ask, health_checker=health_checker))

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app


__all__ = [
    "AskSqlOutcome",
    "HealthChecker",
    "SqlAskService",
    "create_app",
]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


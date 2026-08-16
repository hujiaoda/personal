# -*- coding: utf-8 -*-
# 设计取舍：
# 1) 用户说法别名是“越用越懂用户”的最小闭环：用户说“饭钱”，系统先改写成
#    “餐饮”再进问数流程。存储直接复用 M2 的 kv_memories，不新建表、不引向量。
# 2) profile 不 import memory/data/llm：构造时注入一个鸭子协议 kv_backend
#    （put_memory/list_memories），真实环境由 app 传 MemoryDatabase。
# 3) key 固定为 sql_alias:<用户说法>，value 为标准说法，category=sql_alias；
#    weight 当置信度：同一说法被纠正/使用一次就 +1，封顶 5。
# 4) 改写顺序可解释：先比说法长度（长说法优先，避免“饭”吃掉“饭钱”），
#    再比权重，最后按字符串稳定排序。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

_DEFAULT_KEY_PREFIX = "sql_alias:"
_DEFAULT_CATEGORY = "sql_alias"
_DEFAULT_MAX_WEIGHT = 5.0


@dataclass(frozen=True)
class AliasRule:
    """一条用户说法 → 标准说法的映射；weight 越大越可信。"""

    raw: str
    canonical: str
    weight: float = 1.0


@dataclass(frozen=True)
class RewriteResult:
    """一次问题改写的结果；applied 保留 (原始说法, 标准说法) 便于观测与评测。"""

    text: str
    applied: Tuple[Tuple[str, str], ...] = ()


class HabitAliasStore:
    """基于 KV 长期记忆的问数别名映射。kv_backend 只需鸭子协议，不 import memory。"""

    def __init__(
        self,
        kv_backend: Any,
        *,
        category: str = _DEFAULT_CATEGORY,
        key_prefix: str = _DEFAULT_KEY_PREFIX,
        max_weight: float = _DEFAULT_MAX_WEIGHT,
    ) -> None:
        if not callable(getattr(kv_backend, "put_memory", None)):
            raise TypeError("kv_backend 必须提供 put_memory(key, value, ...) 方法")
        if not callable(getattr(kv_backend, "list_memories", None)):
            raise TypeError("kv_backend 必须提供 list_memories() 方法")
        if not category:
            raise ValueError("category 不能为空")
        if not key_prefix:
            raise ValueError("key_prefix 不能为空")
        if max_weight < 1:
            raise ValueError("max_weight 必须 >= 1")
        self._backend = kv_backend
        self._category = category
        self._key_prefix = key_prefix
        self._max_weight = float(max_weight)

    @staticmethod
    def _normalize(term: str, name: str) -> str:
        if not isinstance(term, str):
            raise ValueError(f"{name} 必须是字符串")
        text = term.strip()
        if not text:
            raise ValueError(f"{name} 不能为空")
        return text

    def record_alias(self, raw_term: str, canonical_term: str) -> AliasRule:
        """记录/强化一条别名。同一条重复出现时 weight +1，封顶 max_weight。"""
        raw = self._normalize(raw_term, "raw_term")
        canonical = self._normalize(canonical_term, "canonical_term")
        if raw == canonical:
            raise ValueError("用户说法与标准说法不能相同")

        key = f"{self._key_prefix}{raw}"
        weight = 1.0
        for record in self._backend.list_memories():
            record_key = str(getattr(record, "key", ""))
            if record_key == key:
                weight = min(self._max_weight, float(getattr(record, "weight", 1.0) or 1.0) + 1.0)
                break
        self._backend.put_memory(
            key,
            canonical,
            category=self._category,
            weight=weight,
        )
        return AliasRule(raw=raw, canonical=canonical, weight=weight)

    def list_aliases(self) -> Tuple[AliasRule, ...]:
        """按“长说法优先、高权重优先”排序返回全部别名规则。"""
        rules: List[AliasRule] = []
        for record in self._backend.list_memories():
            category = str(getattr(record, "category", "") or "")
            key = str(getattr(record, "key", "") or "")
            if category != self._category or not key.startswith(self._key_prefix):
                continue
            raw = key[len(self._key_prefix) :]
            if not raw:
                continue
            canonical = str(getattr(record, "value", "") or "").strip()
            if not canonical:
                continue
            rules.append(
                AliasRule(
                    raw=raw,
                    canonical=canonical,
                    weight=float(getattr(record, "weight", 1.0) or 1.0),
                )
            )
        rules.sort(key=lambda rule: (-len(rule.raw), -rule.weight, rule.raw))
        return tuple(rules)

    def get_alias(self, raw_term: str) -> Optional[AliasRule]:
        raw = self._normalize(raw_term, "raw_term")
        for rule in self.list_aliases():
            if rule.raw == raw:
                return rule
        return None

    def rewrite_question(self, question: str) -> RewriteResult:
        """把问题里的用户说法替换成标准说法；不命中则原样返回。"""
        text = self._normalize(question, "question")
        applied: List[Tuple[str, str]] = []
        for rule in self.list_aliases():
            if rule.raw in text:
                text = text.replace(rule.raw, rule.canonical)
                applied.append((rule.raw, rule.canonical))
        return RewriteResult(text=text, applied=tuple(applied))


__all__ = ["AliasRule", "HabitAliasStore", "RewriteResult"]

# -*- coding: utf-8 -*-
# profile 包负责用户画像与习惯；M3 只做“问数别名映射”，并复用 M2 的 KV 记忆存储。
"""M3 用户习惯画像：查询别名映射（复用 KV 长期记忆）。"""

from personal_data_assistant.profile.habits import AliasRule, HabitAliasStore, RewriteResult

__all__ = ["AliasRule", "HabitAliasStore", "RewriteResult"]

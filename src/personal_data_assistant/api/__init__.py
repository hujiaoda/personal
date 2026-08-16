# -*- coding: utf-8 -*-
"""M4 应用层：FastAPI 薄壳 + 极简单页前端。

刻意不在包出口 import main：`python -m personal_data_assistant.api.main` 会先
import 本包，若这里再 import main，runpy 会发出模块重复加载的 RuntimeWarning。
"""

__all__: list = []


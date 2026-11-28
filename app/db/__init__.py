"""持久化层：SQLite（多用户）。

对外只暴露 ``store`` 与 ``database`` 两个模块，业务代码不要直接写 SQL。
"""
from . import database, store

__all__ = ["database", "store"]

"""暗房接件放样后端 —— 固定遮片暗房选型。

模块划分：
- domain:   色阶样卡、格子、遮挡片等纯领域对象
- planner:  开批前的遮片刻制与每拍曝光排程（纯函数，可独立测试）
- store:    SQLite 持久化（同一张纸吃过的光不可擦掉；进程重启可接回）
- service:  业务用例（开单、备料、开洗印单、师傅确认曝光、接样、取件）
- render:   用整数色阶渲染小图（SVG / PGM），代替真实显影
- server:   本地 HTTP JSON 接口
- cli:      命令行入口
"""

from .domain import StepCard, StepLevel, cell_key, parse_cell, LabError, ConflictError, NotFoundError, ValidationError

__all__ = [
    "StepCard", "StepLevel", "cell_key", "parse_cell",
    "LabError", "ConflictError", "NotFoundError", "ValidationError",
]

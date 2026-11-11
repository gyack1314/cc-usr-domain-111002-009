"""RPA 业务异常定义。"""


class RpaError(Exception):
    """RPA 流程的可预期错误，附带可展示的条目信息。"""

    def __init__(self, message, items=None):
        super().__init__(message)
        self.message = message
        # items: 出错的清单条目（material_id 列表或描述字符串列表）
        self.items = list(items or [])


class ManifestError(RpaError):
    """清单本身不合法（缺字段、类型字段缺失、路径不可读等），上传前中止。"""


class LockError(RpaError):
    """同一草稿已有活动执行者，拒绝重复运行。"""


class ReconcileError(RpaError):
    """恢复草稿时发现内容不一致或人工修改，必须停下由人工确认。"""


class PortalError(RpaError):
    """门户交互异常（找不到项目、预检长时间不结束等）。"""


class RunCancelled(Exception):
    """工作人员取消运行；已上传未发布的草稿与 journal 均保留。"""

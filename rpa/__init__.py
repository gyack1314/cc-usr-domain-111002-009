"""合规资料草稿上传 RPA。

仅负责把清单中的本地材料通过浏览器 input 放入授权门户的草稿，
核对预检结果与草稿映射，最终发布由工作人员完成，本包不会点击发布按钮。
"""

__all__ = ["manifest", "digestutil", "state", "driver", "errors", "runner", "cli"]

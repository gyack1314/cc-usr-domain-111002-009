# 合规资料草稿上传 RPA

用 Python + Playwright 把一份**明确的资料清单**放入本地授权门户的**资料草稿**，
输出可复查的草稿预览与逐材料状态。**最终发布始终由工作人员手动完成，RPA 不触碰发布按钮。**

## 它解决什么问题

合规模糊场景里三件事会让附件挂到错误条目：

1. **文件名相同但内容不同** —— 不能靠文件名认领；
2. **预检异步返回** —— 门户为每次文件选择分配 `upload_id`，结果按 `upload_id` 乱序返回，
   不能靠列表位置认领；
3. **向导回退重建组件、重排附件** —— 每次推进都要重新核对项目、草稿编号与材料映射。

RPA 的对应做法：

- 本地预先计算每个文件的 **SHA-256 摘要**，附件一律凭「摘要 + upload_id」认领，
  同名异内容不会互相错挂；
- 文件只通过浏览器的 `<input type="file">`（`set_input_files`）选择，触发正常 `onchange`；
- 预检未到终态只轮询等待，**预检失败的文件不进入草稿附件映射**；
- 替换失败文件时保留材料编号与 upload_id 沿革，旧 upload_id 标记 `superseded`，
  其迟到成功因为面板已被移除而**无法复活**；
- 每条材料之间触发向导回退，回退后按摘要重新核对全部已通过附件；
- 清单缺少某材料类型的必要动态字段时，**上传前直接报错，绝不自动填未知值**。

## 目录

```
rpa/
  manifest.py    清单加载与上传前校验（含动态必要字段、同名同内容歧义拒绝）
  digestutil.py  本地 SHA-256（与门户 crypto.subtle 同一算法）
  state.py       journal 进度日志、材料沿革、草稿级排它执行锁
  driver.py      Playwright 驱动：所有门户操作走真实 DOM/input，含发布护栏
  runner.py      编排：上传、预检认领、回退核对、恢复对账、报告
  cli.py         命令行入口 validate / run / resume
manifests/       示例清单（含一个故意缺字段的负面清单）
materials/       公开合成文本资料（无真实合规材料）
tests/test_rpa.py 浏览器回归检查（12 项）
```

## 清单格式

```json
{
  "draft_id": "DRAFT-17",
  "base_dir": "..",
  "materials": [
    {
      "material_id": "MAT-17",
      "path": "materials/north/report.txt",
      "kind": "report",
      "project": "PR-17",
      "summary": "北区检测文本",
      "fields": {"report_date": "2026-11-03"}
    }
  ]
}
```

- `kind=report` 的必要动态字段是 `fields.report_date`（检测日期）；
- `kind=license` 的必要动态字段是 `fields.license_id`（许可编号）；
- 字段随材料类型在门户动态变化，缺任一字段即在打开浏览器前报错。

## 使用

```sh
# 启动本地合成门户
python -m portal --port 8090

# 1) 只校验清单，不开浏览器
python -m rpa.cli validate --manifest manifests/manifest.json

# 2) 执行上传（已有同草稿 journal 时按替换语义接管，保留沿革）
python -m rpa.cli run --manifest manifests/manifest.json --portal http://127.0.0.1:8090

# 3) 恢复：先打开同一草稿，用预览核对既有附件的摘要与归属，再补齐缺项
python -m rpa.cli resume --manifest manifests/manifest.json --portal http://127.0.0.1:8090
```

运行产物（默认在 `outputs/`、进度在 `state/`）：

- `preview-<draft>.json` —— **权威草稿预览**：材料编号、所属项目、类型、摘要、
  摘要哈希、当前 upload_id、预检状态；项目归属取自 RPA journal 的材料沿革
  （门户页面的项目字段是全局下拉值，会被回退重算，不作为归属权威）；
- `preview-<draft>.png` —— 草稿页面截图；
- `report-<draft>.json` —— 逐材料报告，含 `needs_human` 与 `publish_count`；
- `state/journal-<draft>.json` —— 进度与 upload_id 沿革，恢复时使用。

## 安全护栏

- **发布计数始终为零**：驱动在页面所有脚本前注入拦截，点击 `#publish` 会被
  `preventDefault/stopPropagation` 阻断并计数；报告中的 `publish_count` 非零会以错误码退出。
- **一个草稿一个活动执行者**：`state/lock-<draft>.lock` 用 `fcntl` 排它锁，
  第二个运行会被拒绝；锁随进程持有，取消后自动释放，已上传未发布的草稿与 journal 保留。
- **恢复时人工修改不被覆盖**：预览出现清单之外的附件、本地文件摘要与已确认值不一致、
  已通过附件从预览消失，都会停下并在 `needs_human` 列出，不自动补传或删除。
- **取消（SIGINT/SIGTERM）**：在每个浏览器操作检查点生效，已上传部分保留，输出清单后退出。

## 回归检查

```sh
python -m unittest discover -s tests -v
```

`tests/test_rpa.py` 覆盖：文件确由 input 接收、同名异内容正确区分、
旧 upload_id 迟到结果不认领替换材料、预检失败不进映射、向导回退后映射保持、
重启不重复上传、人工内容不一致即停下、缺字段上传前报错、排它锁、取消保留草稿、发布护栏。

# 测速摄像头减速策略的效果解释与区域启用

这是用于本地分析开发的合成车辆运营资料与验证项目。所有车辆、地区、道路、
调查参与者及事件标识均为虚构值。项目评估“车辆在测速摄像头附近主动减速”的
公开功能说明在合成轨迹上是否按预期表现，并给出区域启用（继续采集 / 有限启用
/ 暂缓）的证据建议。

> 本分析是对公开功能的内部验证设想，不表示新闻已报道具体交通事件，也不构成
> 真实道路、车辆或驾驶员结论。

## 方法要点

- **曝光按窗口计，不按目录条目计。** 一次可比较的经过是一个轨迹窗口；同一
  道路/方向下即使有多个目录条目，也只算一次暴露。
- **目录记录不等于实际启用。** 只有 `confirmed`、处于有效期、与样本地图版本
  一致、且窗口内确有车辆识别（`camera_seen=yes`）的条目才能形成暴露。
  `needs-review`、`withdrawn`、方向相反和地图版本不符的条目会分别报告原因。
- **不拿摄像头前后均值直接下结论。** 主指标是“接近段末速度 − 识别区 50 m
  内最低速度”。效应只在同一道路/方向/地图版本内，且交通状态、天气、采样率、
  识别偏移都相同的策略窗口与未启用窗口之间比较；弯道、路口和拥堵的影响由同
  范围对照吸收。
- **没有对照就不估计效应。** 某范围只有策略窗口或只有对照窗口时返回
  `insufficient_control`；跨道路汇总单列为探索性敏感性分析，不驱动启用决定。
- **异常先排除。** 交通事件窗口和物理上不合理的速度跳变不进入常态结论，但全部
  在明细中保留并可追溯。
- **计划不可变，撤回是追加。** 新版本会取代旧版本；目录撤回、限速变化或方向
  更正会把相关生效范围列入重新审查，历史行程保持原始地图版本和策略状态。

## 目录

```text
data/          四个契约 CSV（UTF-8 BOM，首行字段名）
docs/          资料契约与使用说明
src/speed_camera_slowdown_evaluation/
  loader.py      严格 CSV 载入、格式/主键/引用诊断、JSON 快照
  models.py      领域模型（目录、窗口、样本、计划）
  exposure.py    目录→窗口的暴露关联与覆盖缺口
  effects.py     同范围对照的减速效应估计与排除规则
  regions.py     计划版本解析、区域建议、局部撤回
  review.py      目录更正影响与范围复审
  reporting.py   JSON 报告导出
  fixtures.py    测试/演示用合成数据集
  cli.py         命令行入口
tools/         资料完整性检查
tests/         单元测试（中文用例名覆盖关键业务约束）
```

## 快速开始

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
python3 -m unittest discover -s tests -v
```

不安装包时可在项目根目录用 `PYTHONPATH=src` 运行：

```sh
PYTHONPATH=src python3 -m speed_camera_slowdown_evaluation.cli analyze data -o out/report.json
```

安装为可编辑包后可省略 `PYTHONPATH`：`python3 -m pip install -e .`。

## 命令行

```sh
# 校验 CSV 并写出不可变快照（后续分析可固定到该快照）
speed-camera-eval import data -o out/snapshot.json

# 生成曝光、效应、计划与区域建议报告
speed-camera-eval analyze out/snapshot.json -o out/report.json
speed-camera-eval analyze data --min-per-arm 3      # 收紧每个条件臂的最少窗口数

# 查看计划解析结果
speed-camera-eval plans data --effective-only

# 计算局部撤回影响（只输出结果，不修改输入资料）
speed-camera-eval withdraw data --road-id RD-01 --direction S
speed-camera-eval withdraw data --plan-id P-13 --new-plan-id P-22 --effective-from 2026-12-20

# 对比更正前后资料，列出受影响的历史路线关联
speed-camera-eval review data corrected_data -o out/review.json
```

`analyze` 报告的关键字段：

| 字段 | 含义 |
| --- | --- |
| `summary.windows_eligible` | 可比较的轨迹窗口数（有效暴露） |
| `summary.scopes_with_both_arms` | 同时具备策略与对照窗口的范围数 |
| `exposure.windows[].status` | 每个窗口的曝光判定与原因 |
| `coverage_gaps` | 无法支撑同范围对照的范围及缺口原因 |
| `effects.scopes[].verdict` | `estimated` / `inconclusive` / `insufficient_control` |
| `effects.cross_scope` | 跨道路敏感性分析（探索性，不驱动决定） |
| `recommendations[].decision` | `continue_collect`（继续采集）/ `limited`（有限启用）/ `hold`（暂缓）与理由 |

## 当前基线数据的结果

基线 `data/` 中每条有确认目录的道路/方向只出现一种启用状态（要么全是策略窗口，
要么全是对照窗口），因此所有范围都是 `insufficient_control`，区域建议均为
`hold`。这是资料的预期结论：它演示了“不能把前后速差直接当作策略效果”，而不是
功能无效的证据。需要同范围混合样本时，项目内的 `fixtures.comparison_case` 给出
可估计的正例。

## Python 版本

需要用 Python 3.11–3.14。`data/` CSV 使用 UTF-8 BOM，首行为字段名。
`docs/资料契约.md` 说明字段含义、来源边界与需保留的时间/版本信息，
`docs/使用说明.md` 给出分析口径和运营流程。

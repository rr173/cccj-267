# 可重述的服务中心 SLA 时限出数工具

纯 Python 标准库离线命令，不依赖数据库、第三方包或逐条分钟任务。所有时间在内部表示为 UTC 整分钟序号，区间一律为半开区间 `[start, end)`。

## 1. 口径实现

- 证据按 `submit_no` 截止：只读取 `submit_no <= --cutoff` 的提交。
- 同一 `record_id` 取可见证据中最高 `version`；最高版本为 `revoke` 时，原记录失效。
- 案件在受理记录上绑定服务点、合同和初始等级；后续不随服务点映射或“当前等级”迁移。
- 合同目录在受理时刻查找生效合同及其等级额度表。
- 日历按服务点选择版本；可用 `--calendar-version SERVICE_POINT=VERSION` 固定修正版。
- 只统计同时满足以下条件的分钟：
  1. 位于 `[受理时间, min(办结时间, 观察时刻))`；
  2. 位于日历覆盖范围内；
  3. 位于开门/可计费半开区间；
  4. 不落在任何有效等待区间内。
- 覆盖范围内但不在开门区间：不计费。
- 非等待分钟落到覆盖范围外：记为 `calendar_coverage_gap` 或 `calendar_not_found`，案件列为 `indeterminate`，不会按 0 分钟偷过。若全程被有效等待覆盖，则日历缺失不影响结论。
- 等待区间取并集。半开区间下 `[10:00,10:30)` 与 `[10:31,11:00)` 之间的 `[10:30,10:31)` 是真实恢复分钟，不会被吞掉。
- 已用分钟在每个计入分钟的右边界增加；判断越线时使用刚走完的这一分钟内适用的等级额度。
- 同一时间有多个等级事件时，按 `(at, business_seq, submit_no, op_index)` 排序；业务序号小的先处理，最后的等级为当前等级。每个序号导致的首次越线都会保留。
- 边界上的等级变更用变更后额度重新判断同一累计值；降额度可即时越线，之后升额度不会消除首次越线。
- 办结边界仍检查刚结束的最后一分钟，但不应用该边界上的等级变更。
- 报告状态以实际 `earliest_breach` 为准，不用最终等级的新额度重算历史。

算法只遍历等待、日历区间和等级事件边界，复杂度约为 `O(B log B)`，其中 `B` 是区间/事件边界数；不按分钟生成记录。

## 2. 命令

入口：

```bash
/workspace/sla-report --help
# 或
python3 -I /workspace/app/sla_report.py --help
```

`-I` 可避免从数据目录或当前目录误导入不可信的 `json.py` 等文件。

### 生成小样例输入

```bash
/workspace/sla-report sample --dir /workspace/sample_input
```

生成：

- `sample_input/evidence.json`：9 个案件、提交 1/2/3/4/7/8，含替换和撤销；
- `sample_input/submissions/submission-XXXX.json`：同样证据按提交号拆分的目录，`--evidence` 也可直接传该目录；
- `sample_input/contracts.json`：合同目录 v1，额度 `STD=120`、`PRIORITY=60`、`EXTENDED=240`、`ENTERPRISE=480`；
- `sample_input/calendars.json`：EAST v1、WEST v1 和修正版 WEST v2。

### 出普通报告

原签日结当时可见到提交 5：

```bash
/workspace/sla-report report \
  --evidence sample_input/evidence.json \
  --contracts sample_input/contracts.json \
  --calendars sample_input/calendars.json \
  --as-of 2026-01-06T00:00Z \
  --cutoff 5 \
  --out out/report-cut5.json \
  --csv out/report-cut5.csv
```

### 签订日结

已签文件已存在时命令直接拒绝覆盖；勘误必须走 `restate` 并写到新文件。

```bash
/workspace/sla-report close \
  --evidence sample_input/evidence.json \
  --contracts sample_input/contracts.json \
  --calendars sample_input/calendars.json \
  --as-of 2026-01-06T00:00Z \
  --cutoff 5 \
  --close-id DAILY-2026-01-05 \
  --out out/DAILY-2026-01-05.signed.json \
  --csv out/DAILY-2026-01-05.csv

/workspace/sla-report verify --signed out/DAILY-2026-01-05.signed.json
```

签名信封内含原报告、规范化 JSON 的 SHA-256 摘要。若设置环境变量 `SLA_SIGNING_KEY`，会额外生成 HMAC-SHA256 封签；验证时需保留同一密钥。

### 重述：更高截止号 + 新日历可见版

```bash
/workspace/sla-report restate \
  --signed out/DAILY-2026-01-05.signed.json \
  --evidence sample_input/evidence.json \
  --contracts sample_input/contracts.json \
  --calendars sample_input/calendars.json \
  --cutoff 10 \
  --out out/DAILY-2026-01-05.restated-cut10.json \
  --csv out/DAILY-2026-01-05.restated-cut10.csv \
  --changes-csv out/DAILY-2026-01-05.changes.csv
```

重述始终使用原签名中的同一 `observation_as_of`。输出 JSON 并列保存：

- `original_report`：原签完整结果；
- `restated_report`：新结果；
- `status_flips`：按时/超时状态翻转及新增来源；
- `changes.changed_cases`：所有变化案的前后快照、来源差异和配置版本变化。

也可以只隔离一类变化：

```bash
# 只接受提交 7/8 的业务勘误，日历仍固定 WEST=1
/workspace/sla-report restate ... --cutoff 10 --calendar-version WEST=1 \
  --out out/evidence-only.json

# 证据截止不变，只使用改正后的 WEST=2 日历
/workspace/sla-report restate ... --calendar-version WEST=2 \
  --out out/calendar-only.json
```

一键重演：

```bash
/workspace/reproduce_sample.sh [一个新的输出目录]
```

## 3. 输入格式要点

### 证据提交

```json
{
  "submissions": [
    {
      "submit_no": 7,
      "records": [
        {
          "op": "revoke",
          "version": 2,
          "kind": "waiting",
          "record_id": "W-REVOKE",
          "case_id": "C-REVOKED-WAIT"
        },
        {
          "op": "put",
          "version": 2,
          "kind": "intake",
          "record_id": "I-REPLACE",
          "case_id": "C-REPLACED-INTAKE",
          "at": "2026-01-05T10:00Z",
          "service_point": "EAST",
          "level": "STD"
        }
      ]
    }
  ]
}
```

支持的 `kind`：

- `intake`：`at`、`service_point`、`level`，可选 `contract_id`；
- `completion`：`at`；
- `waiting`：`start`、`end`；`end: null` 表示到观察时刻仍在等待；
- `level_change`：`at`、`to_level`、`business_seq`。

所有时间必须是零时/零分/零秒的 UTC 整分钟边界；可接受 `Z` 或 UTC 偏移。

### 合同目录

```json
{
  "catalogs": [
    {
      "catalog_id": "SLA",
      "version": 1,
      "visible_from_submit_no": 1,
      "contracts": [
        {
          "contract_id": "STANDARD-UTC",
          "effective_from": "2025-01-01T00:00Z",
          "effective_to": null,
          "service_points": ["EAST", "WEST", "NORTH"],
          "levels": {"STD": 120, "PRIORITY": 60}
        }
      ]
    }
  ]
}
```

额度是严格越线阈值：额度 120 时，第 120 个计入分钟仍按时，第 121 个分钟的右边界才首次越线。`null` 额度表示不限时。

### 日历

```json
{
  "calendars": [
    {
      "service_point": "WEST",
      "calendar_id": "WEST-UTC",
      "version": 2,
      "visible_from_submit_no": 9,
      "coverage": [
        {"start": "2026-01-05T00:00Z", "end": "2026-01-06T00:00Z"}
      ],
      "open_intervals": [
        {"id": "AM", "start": "2026-01-05T09:00Z", "end": "2026-01-05T12:00Z"},
        {"id": "PM", "start": "2026-01-05T13:00Z", "end": "2026-01-05T17:00Z"}
      ]
    }
  ]
}
```

这里不推导法定假日；未列入开门区间但在 `coverage` 内的时间就是不计费时间。

## 4. 小样例预期结果

原截止号 5：

| 案件 | 已用分钟 | 原状态/最早越线 | 说明 |
|---|---:|---|---|
| `C-WAIT-EXACT` | 120 | on_time | 等待 60 分钟，恰好不严格超过 120 |
| `C-RECOVERY` | 121 | timeout @ 12:00 | `10:30–10:31` 恢复分钟被保留，正好导致越线 |
| `C-LEVEL-DOWN-UP` | 420 | timeout @ 11:00，等级事件触发 | 120 分钟时降到额度 60，即时越线；后升到 480 不消除首越线 |
| `C-COMPLETION-LEVEL` | 120 | on_time | 15:00 办结边界的降额事件不应用 |
| `C-REVOKED-WAIT` | 120 | on_time | 原等待有效 |
| `C-REPLACED-INTAKE` | 180 | timeout @ 11:01 | 原受理时间 09:00 |
| `C-CALENDAR-FIX` | 300 | timeout @ 13:01 | 原 WEST v1 错误把 12–14 点计为开门 |
| `C-NO-CALENDAR` | `null`（已知 0） | indeterminate | 480 个非等待分钟无 NORTH 日历覆盖 |
| `C-YEAR-BOUNDARY` | 120 | on_time | 跨年度半开区间正常扫算 |

截止号 10 后：

- `C-REVOKED-WAIT`：撤销提交 7 中的等待 → 180 分钟，`on_time -> timeout`；
- `C-REPLACED-INTAKE`：提交 8 把受理改为 10:00 → 120 分钟，`timeout -> on_time`；
- `C-CALENDAR-FIX`：改用 WEST v2，扣除午休 → 240 分钟，`timeout -> on_time`。

汇总为 1 件 `on_time -> timeout`、2 件 `timeout -> on_time`。

## 5. 测试

```bash
python3 -I /workspace/tests/test_sla.py
```

测试覆盖等待并集/恢复分钟、样例手工结果、版本重述、同一时点业务序号、办结边界等级事件、缺日历阻断以及十年跨度区间的性能和结果。

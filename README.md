# 手环说明里的代词消歧与家庭共享授权

这是面向“家庭共享请求、关系和通知收件回执”业务文本的 Python 项目。当前
目录提供合成练习语料和已登记的原始记录，记录用于开发与回归时复核文字内容
和来源关系。**全部资料均为本地合成的演练材料，不含真实个人、机构处理记录
或服务响应。**

项目从一段口语请求、联系人关系和先前已确认的共享回执里，整理出一份可以更
正的共享决定账本：谁可以看哪一份资料、读取时间与资料覆盖的时段、以及能否
再次转发，各自独立记录。

## 内容

- `data/corpus.json` 保存合成文本记录及记录类别。
- `data/context.json` 说明当前语料范围。
- `materials.py` 读取项目语料，保留每条记录的原始文字。
- `family_share/` 是决定账本的实现：
  - `cast.py` 联系人登记、称谓与同名消歧。
  - `events.py` 语料里的时段、时刻和措辞解析。
  - `model.py` 决定、提问、视图、分发结果的数据模型。
  - `receipts.py` 已发出通知回执的保留与正文收紧。
  - `service.py` 摄取、确认绑定、更正、可见范围查询与分发。
- `tests/` 检查资料读取入口、语料索引，以及账本行为。

## 处理一件事时分开的五件事

| 维度 | 例子 | 记录位置 |
| --- | --- | --- |
| 受众 | “我姐”是 p2，“运动组的阿敏”是 p3 | `Decision.person_id` |
| 资料种类 | 趋势、日均时长、周二摘要、提醒通知 | `Decision.material` |
| 读取时间 | 何时可以打开 | `Decision.read_window` / `Question.read_window` |
| 资料覆盖时段 | 内容描述的是哪几天/哪一晚 | `Decision.coverage` / `Interval` |
| 二次转发 | 能否再转给别人 | `Decision.forwardable` |

因此“昨晚那条别让她看”只会产生一条 `EXCLUDE reminder_notice`，不会变成
“停用姐姐的全部共享”，也不会碰触她原本可以看的本周趋势和周二摘要。

## 几条硬性规则

1. **只有佩戴者本人的明确同意建立授权。** 转述、反问、请求“再提醒我问问”
   都只生成待办，不产生范围。
2. **同名不等于同一人。** “阿敏”在家庭语境指向 p2，在运动组语境指向 p3；
   单凭昵称解析不唯一时保持 `ambiguous`，只补最能决定范围的那一项事实。
3. **未登记的人不借用他人的授权。** “妈”没有联系人记录，不能把母亲的需求
   算到姐姐头上。
4. **迟到的确认必须对应当时的提问。** 提问时记录该亲友该资料的范围指纹；
   期间范围变了（`stale`）、受众变了（`mismatched`）或指代被澄清推翻，确认
   都不能签成新同意。无关资料的变动不会让确认失效。
5. **确认不扩大范围。** 没有确定范围的肯定答复不会变成无边界授权；已有范围
   的确认记为 `REAFFIRM`。
6. **回执保留投递事实。** 授权缩小后，回执对象仍在，但正文入口收紧；只有
   摘要覆盖的时段和明细都落在当前范围里才提供正文。
7. **分发按每份材料的真实来源与受众分别裁定。** 单条内容即便去掉名字，若与
   另一份可见内容的时刻/描述合起来能反推出被排除的提醒，只改写相关上下文，
   不删除收件人原本有权查看的趋势或摘要其余部分。

## 用法示例

```python
import json
from pathlib import Path
from family_share import Action, load_ledger

corpus = json.loads(Path("data/corpus.json").read_text(encoding="utf-8"))
ledger = load_ledger(corpus)

ledger.explain("p2", "sleep_trend")
# p2 可查看本周睡眠趋势（…），依据：r1「允许查看本周睡眠趋势」；q1「…」…

ledger.why_visible("sleep_trend", person_id="p2")   # 依哪次确认可见
ledger.clarify_effects()                            # 某次澄清影响了哪位亲友
ledger.forwarding_check("p2", "sleep_trend")        # 二次转发默认不允许
ledger.compose_bundle("p2")                         # 给 p2 的可读说明
ledger.receipt_access(ledger.receipts[0])           # 回执正文是否还能打开
```

提问与确认：

```python
question = ledger.ask(
    person_id="p2",
    material="daily_average",
    coverage=week_bounds(today),
    read_window=day_bounds(today),
    prompt="苑敏可否看本周日均时长？",
    source_record="manual-1",
)
ledger.answer_question(question.id, affirmative=True, source_record="manual-2")
```

更正只改指定亲友和资料：

```python
ledger.revise(person_id="p2", material="reminder_notice", action=Action.EXCLUDE,
              coverage=night_bounds(today), source_record="manual-3")
```

`Ledger.export_state()` / `Ledger.from_state()` 可以保存和恢复整个账本；恢复
后版本、可见范围、回执和待办问题保持一致。

## 运行和检查

项目仅依赖 Python 标准库。运行检查：

    python3 -m unittest discover -s tests -v

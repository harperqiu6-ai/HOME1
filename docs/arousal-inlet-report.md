# 2026-07-31 arousal 解析层进水口修补

- 边界：仅修改 `/root/claude/HOME1/`；改前备份均使用
  `.bak.20260731-inlet` 后缀。未部署、未重启、未 systemctl、未
  commit/push。
- 助手方向：第一人称动作改为小句级判断。动作前最近的显式人称主语
  必须是“我”；“你/她/他/它”发出的动作继续拒绝。计划、假设、回忆、
  引用、第三方、器具说明和整条“红灯”等既有过滤保持。
- 词表：新增可选 `feedback` 组，schema 与 `address` 相同。平静时不能
  单独起效；同消息已有有效动作时按第二强动作 `×0.30` 加成；投影后
  `scene_open` 时可独立按自身 delta 起效。
- 回归：全部测试只使用中性占位词。指定 runner 结果为
  `32 passed, 0 failed`，相关 Python 文件通过 `py_compile`。
- 节奏：libido=0.4 时，单动作第 12 拍越过 PONR（0.9950），复合动作
  第 10 拍越过（1.0000），与改前一致。
- 未做：未改常数、`public_snapshot` 九字段、release 检测或依赖。
- worklog 说明：顶层 `/root/claude/worklog.md` 位于允许修改范围之外，
  因“只改 HOME1”红线未追加；本文件作为范围内施工记录。

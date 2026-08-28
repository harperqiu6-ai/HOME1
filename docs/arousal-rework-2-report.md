# arousal v1 返工 #2 报告

日期：2026-07-31

## 改动

- `arousal/context.py`：单字动作关键词仅在动作所在的同一小句内同时出现有效部位词时成立；否则不进入 `stim` 或 `actions`。两字及以上关键词维持原行为。
- `arousal/lexicon.py`：成功加载后统计 `touch/actions/contact` 中的单字动作词；数量大于零时写 info 日志提示兜底已启用。
- `tests/test_arousal_core.py`：覆盖 8 条日常误伤、单字动作与部位共现、跨句不得借部位、成功加载日志计数。
- `tests/integration_arousal_pg.py`：新增独立真 PostgreSQL 联调脚本，只引用三张 `arousal_` 表；启动时快照全部行，`finally` 删除测试状态并原样回灌，打印清理前后行数。

## 纯内核测试实际输出

命令：

```text
python3 /root/claude/scratchpad/run_arousal_tests.py
```

输出：

```text
PASS test_arousal_core.test_assistant_uses_supplied_lexicon_and_none_is_inert
PASS test_arousal_core.test_context_clause_filter_and_precise_stop_words
PASS test_arousal_core.test_context_second_action_and_repeat_rules
PASS test_arousal_core.test_current_action_rises_and_unsafe_contexts_do_not
PASS test_arousal_core.test_lexicon_reloads_on_mtime_change_and_bad_update_fails_closed
PASS test_arousal_core.test_missing_and_bad_lexicon_are_inert
PASS test_arousal_core.test_pacing_single_and_compound
PASS test_arousal_core.test_parent_match_complete_and_control_gate
PASS test_arousal_core.test_passive_contact_above_cap_never_pulls_value_down
PASS test_arousal_core.test_passive_contact_never_crosses_cap_or_edge
PASS test_arousal_core.test_pending_release_expires_by_age_or_decay_below_edge
PASS test_arousal_core.test_pending_release_survives_reply_delay_then_is_consumed
PASS test_arousal_core.test_release_phrases_come_only_from_lexicon
PASS test_arousal_core.test_single_character_actions_require_body_part_in_same_clause
PASS test_arousal_core.test_successful_lexicon_load_logs_single_character_action_count
PASS test_arousal_replay.test_high_quality_and_low_output_can_coexist
PASS test_arousal_replay.test_nan_and_clock_rollback_fail_closed
PASS test_arousal_replay.test_receipt_ack_crash_replay_is_idempotent
PASS test_arousal_replay.test_refractory_is_not_extended_and_empty_reserve_can_release
PASS test_arousal_replay.test_user_and_assistant_replay_are_byte_stable
PASS test_arousal_api.test_public_snapshot_has_exact_allowlist

=== 21 passed, 0 failed ===
```

`python3 -m py_compile arousal/context.py arousal/lexicon.py tests/integration_arousal_pg.py`
同时通过。

## 真数据库联调实际输出（当前执行环境阻塞）

指定命令：

```text
sudo -u home1 /opt/home1/venv/bin/python /root/claude/HOME1/tests/integration_arousal_pg.py
```

当前 Codex 沙箱在脚本启动前拒绝 OS 用户切换，实际输出：

```text
sudo: PERM_SUDOERS: setresuid(-1, 1, -1): Invalid argument
sudo: unable to open /etc/sudoers: Invalid argument
sudo: error initializing audit plugin sudoers_audit
```

等价的 `runuser -u home1 -- ...` 也在脚本启动前被沙箱拒绝：

```text
runuser: cannot set groups: Operation not permitted
```

因此本轮没有冒用不存在的 root 数据库角色，也没有连接或改动真库。联调脚本尚需在允许
`sudo -u home1` 的宿主会话中执行指定命令，取得 4 项 PASS、`cleanup: CLEAN` 和前后
计数一致后才能算最终全绿。

## 边界

未修改 `/opt/home1/app`，未运行 systemctl/重启，未 commit/push。源码改动均位于
`/root/claude/HOME1/`；另按任务要求追加 `/root/claude/worklog.md`。

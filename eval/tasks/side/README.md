# 旁路任务

不放进 `eval/run_eval.py` 的默认三条。可恢复评测的清单在 `eval/bench_manifest.json`，用 `python eval/run_bench.py --profile check` 核对。会改关卡且不保存的任务不进成功率。

默认三条仍是 `editor-cube`、`bp-gun-compile`、`cpp-typo`。`cpp-build-existing` 只编译，不改源码。`cpp-fix-keep.json` 会留下源码，不进清单。

索引里能编译的蓝图只有四张，裁判只打 location、蓝图编译和 C++ 编译。读属性、放置、材质没有对应裁判，清单里不编假任务。手工 Cursor 对照表在 `eval/reports/`，手工列仍是待填写。

`DiscoverTests` 的返回仍然只有 `{"status":"ready"}`，名字不在这一步。接着 `ListTests` 才能看到过滤器。2026-10-16 抄到的是 `AIAgent.Pawn.Look`、`AIAgent.Pawn.Wasd`、`AIAgent.Enemy.Patrol`。巡逻任务是 `demo-c-patrol.json`，用最后这一条，不进默认三条。`demo-a-spawn-box.json` 读出生点盒子。`demo-b-blueprint.json` 只允许改 `/Game/BP_TestEnemy.BP_TestEnemy` 这一张，编译之后仍要跑同一条 Automation。

`pickup-sentence.json` 只给 `check_routes.py` 对一句原话做离线拆句，不编译、不进默认三条。`kill-check.json` 是单点杀伤旁路：仿 `inject_typo` 故意改错一处，期望 `AIAgent.Pawn.Look` 失败，再按同一条快照还原。不进默认三条，也不进 `bench_manifest.json`。

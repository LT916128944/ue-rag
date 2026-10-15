# 旁路任务

不放进 `eval/run_eval.py` 的默认三条。可恢复评测的清单在 `eval/bench_manifest.json`，用 `python eval/run_bench.py --profile check` 核对。会改关卡且不保存的任务不进成功率。

默认三条仍是 `editor-cube`、`bp-gun-compile`、`cpp-typo`。`cpp-build-existing` 只编译，不改源码。`cpp-fix-keep.json` 会留下源码，不进清单。

索引里能编译的蓝图只有四张，裁判只打 location、蓝图编译和 C++ 编译。读属性、放置、材质没有对应裁判，清单里不编假任务。手工 Cursor 对照表在 `eval/reports/`，手工列仍是待填写。

`automation-existing.json` 没有写。2026-10-13 编辑器已开，`DiscoverTests` 只返回 `{"status":"ready"}`，结果里没有过滤器。没有合格过滤器就停，不编名字。

`pickup-sentence.json` 只给 `check_routes.py` 对一句原话做离线拆句，不编译、不进默认三条。

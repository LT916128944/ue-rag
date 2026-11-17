# eval/check_cpp_spec.py
"""C++ 域 Spec 内核守卫（20261117 边界升级）。全离线：不连编辑器、不调模型。

沿用 `eval/_spec_guard_lib.run` 的标准电池（validate / kernel 确定性 / adapter 覆盖 /
verifier / route / repair 暂停）。good_spec 取计划 §3 的「UFpsHealthComponent 组件」形态；
verifier 口径 = `compiled == True` + 源码读回含 UCLASS/UPROPERTY/UFUNCTION 符号名。
诚实边界：这只验「编译过 + 符号存在」，逻辑正确性不可只读验。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval._spec_guard_lib import run  # noqa: E402
from agent import cpp_spec  # noqa: E402

GOOD = {
    "target_module": "AIAgentProject",
    "build_deps": ["EnhancedInput"],
    "files": [
        {
            "path": "Public/FpsHealthComponent.h",
            "add": [
                {"kind": "UCLASS", "name": "UFpsHealthComponent", "parent": "UActorComponent"},
                {"kind": "UPROPERTY", "macro": "EditAnywhere, BlueprintReadWrite",
                 "type": "float", "name": "MaxHealth", "default": "100.f"},
                {"kind": "UFUNCTION", "macro": "BlueprintCallable", "ret": "float",
                 "name": "GetHealthPercent", "body_hint": "Health/MaxHealth"},
            ],
        }
    ],
    "verify": ["编译通过", "存在 UPROPERTY MaxHealth", "存在 UFUNCTION GetHealthPercent"],
}

GOOD_RB = {
    "compiled": True,
    "sources": [
        {
            "path": "Public/FpsHealthComponent.h",
            "text": (
                "#pragma once\n"
                "UCLASS()\n"
                "class UFpsHealthComponent : public UActorComponent {\n"
                "  GENERATED_BODY()\n"
                "  UPROPERTY(EditAnywhere, BlueprintReadWrite) float MaxHealth = 100.f;\n"
                "  UFUNCTION(BlueprintCallable) float GetHealthPercent();\n"
                "};\n"
            ),
        }
    ],
}

# 源码里少了 UFUNCTION GetHealthPercent → contains 至少一条不过。
BAD_RB = {
    "compiled": True,
    "sources": [
        {
            "path": "Public/FpsHealthComponent.h",
            "text": (
                "#pragma once\n"
                "UCLASS()\n"
                "class UFpsHealthComponent : public UActorComponent {\n"
                "  GENERATED_BODY()\n"
                "  UPROPERTY(EditAnywhere, BlueprintReadWrite) float MaxHealth = 100.f;\n"
                "};\n"
            ),
        }
    ],
}

CASES = {
    "good_spec": GOOD,
    "good_readback": GOOD_RB,
    "bad_readback": BAD_RB,
    "step": {},
    "bad_specs": [
        ({"build_deps": [], "files": [{"path": "Public/X.h",
                                       "add": [{"kind": "UCLASS", "name": "UX"}]}]}, "bad_module"),
        ({"target_module": "AIAgentProject", "build_deps": [123],
          "files": [{"path": "Public/X.h", "add": [{"kind": "UCLASS", "name": "UX"}]}]},
         "bad_build_deps"),
        ({"target_module": "AIAgentProject", "build_deps": [], "files": []}, "no_files"),
        ({"target_module": "AIAgentProject", "build_deps": [],
          "files": [{"path": "Public/X.h", "add": [{"kind": "UPARAM", "name": "Bad"}]}]},
         "unknown_kind"),
        ({"target_module": "AIAgentProject", "build_deps": [],
          "files": [{"add": [{"kind": "UCLASS", "name": "UX"}]}]}, "bad_path"),
    ],
    "positive_texts": [
        "给 FpsPawn 加一个射速属性",
        "在 C++ 里加个 UFUNCTION",
        "在 Public/X.h 加一个 UPROPERTY",
        "编译工程",
        "给 Player 添加一个跳跃组件",
        "新增一个 C++ 类",
    ],
    "negative_texts": [
        "在关卡里加一个点光源",
        "给该UI资产加一个滑动条",
        "把材质的粗糙度参数改一下",
        "把一盏灯放进关卡",
    ],
}

if __name__ == "__main__":
    raise SystemExit(run(cpp_spec, CASES))

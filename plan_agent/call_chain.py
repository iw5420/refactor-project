# plan_agent/call_chain.py
"""[P] Plan Agent：呼叫鏈範圍查找，對應 06a 六章「reference_targets 組裝」。
純機械圖走訪，不呼叫任何 LLM，也不讀取 Java 原始碼本體——只用①的呼叫圖
（誰呼叫誰的事實）＋ ③的 java_index（Java method_id → Python 對應資訊）
＋既有的 module 依賴排程（`module_list.depends_on`）決定每個 task 該
參照哪些其他方法，只界定範圍，不含原始碼文字（見六章「為什麼是 [P]，
不是⑤」）。
"""
from __future__ import annotations

import logging
import os
from collections import deque

from graph.state import JavaIndexEntry, ModuleInfo, ReferenceTarget
from plan_agent import module_index

logger = logging.getLogger(__name__)

# 06a 六章「上限」：這是 service 同 module 內互相呼叫（唯一沒有任何機制
# 保證完成順序的殘餘情況）的保底，不是常態路徑，見六章「與既有 module
# 依賴排程的關係」。
MAX_REFERENCE_TARGETS = int(os.environ.get("PLAN_AGENT_MAX_REFERENCE_TARGETS", "20"))


def build_module_closures(module_list: list[ModuleInfo]) -> dict[str, set[str]]:
    """對每個 module 算出遞移依賴閉包（module → 這個 module 遞移依賴的
    所有 module 集合），供 `build_reference_targets()` 判斷「跨 module
    同層呼叫，能不能安全假設對方已經做完」——`ModuleScheduler` 既有機制
    保證 X 依賴的 module 會先完成，這裡只是把 `module_list.depends_on`
    展開成遞移閉包，供查表用，不是新的排程邏輯，見 06a 六章「與既有
    module 依賴排程的關係」。
    """
    deps_by_module = {m["module"]: m["depends_on"] for m in module_list}
    closures: dict[str, set[str]] = {}
    for name in deps_by_module:
        visited: set[str] = set()
        queue: deque[str] = deque(deps_by_module[name])
        while queue:
            dep = queue.popleft()
            if dep in visited:
                continue
            visited.add(dep)
            queue.extend(deps_by_module.get(dep, []))
        closures[name] = visited
    return closures


def build_reference_targets(
    *,
    seed_java_method_id: str,
    seed_module: str,
    seed_layer: str,
    call_graph: dict[str, list[str]],
    java_index: dict[str, JavaIndexEntry],
    module_names: frozenset[str],
    module_closures: dict[str, set[str]],
) -> tuple[list[ReferenceTarget], bool]:
    """對應 06a 六章「演算法」：從 `seed_java_method_id` 出發 BFS 走訪①
    的呼叫圖，回傳 `(reference_targets, truncated)`。

    每個彈出節點的直接呼叫對象查 `java_index`：
    - 查不到（entity 欄位存取、無法解析的呼叫等）→ 略過，不繼續展開。
    - `layer` 比目前節點更基礎 → `language="python"`，不繼續展開（三層
      全域關卡保證，見六章「呼叫鏈規則」）。
    - `layer` 與目前節點相同（或比目前節點更後面，保守視為同層處理）：
      - 同一個 module → `language="java"`，繼續展開（沒有任何機制保證
        完成順序，見六章「與既有 module 依賴排程的關係」）。
      - 不同 module，且目前節點的 module 依賴 callee 的 module（遞移）
        → `language="python"`，不繼續展開（既有 module 依賴排程保證）。
      - 不同 module，且沒有這層依賴關係 → `language="java"`，繼續展開。

    達到 `MAX_REFERENCE_TARGETS` 上限時立即停止整個 BFS（不是這一輪
    展開完才停），回傳 `truncated=True`；呼叫端負責把這個訊號落地成
    `TaskSpec.reference_targets_truncated`（見 06a 六章「截斷可見化」），
    這裡只記一筆警告供開發時查 log 用，警告不是這個訊號的權威落地位置。
    """
    targets: list[ReferenceTarget] = []
    truncated = False
    visited: set[str] = {seed_java_method_id}
    queue: deque[tuple[str, str, str]] = deque([(seed_java_method_id, seed_module, seed_layer)])

    while queue and not truncated:
        current_id, current_module, current_layer = queue.popleft()
        for callee_id in call_graph.get(current_id, []):
            if callee_id == current_id or callee_id in visited:
                continue  # 直接遞迴（方法呼叫自己）或已造訪過，見六章
            visited.add(callee_id)

            entry = java_index.get(callee_id)
            if entry is None:
                continue  # 查不到：entity 欄位存取等，見六章「查不到」說明

            callee_module, callee_layer = module_index.classify(entry["file_path"], module_names)

            if module_index.LAYER_RANK[callee_layer] < module_index.LAYER_RANK[current_layer]:
                language = "python"
            elif callee_module == current_module:
                language = "java"
            elif callee_module in module_closures.get(current_module, set()):
                language = "python"
            else:
                language = "java"

            if len(targets) >= MAX_REFERENCE_TARGETS:
                truncated = True
                break

            if language == "java":
                # java_index 的 value（entry）存的是③投影過的 Python 側事實
                # （file_path/class_name/function_name 都已經是 Python
                # 檔名／camelCase→snake_case 轉換後的名稱），不是 Java 原始
                # 名稱——language="java" 的項目要給⑤（09a）拿去解析真實
                # `.java` 檔案裡的方法，必須是 Java 原始座標，不能沿用
                # entry 的 Python 投影。callee_id 本身就是①的
                # method_id()（"{java_file_path}::{java_class_name}::
                # {java_method_name}"，見 parse_agent/types.py），直接拆
                # 這個字串取得三個 Java 原始欄位，不查 entry。
                java_file_path, java_class_name, java_method_name = callee_id.split("::", 2)
                targets.append(
                    ReferenceTarget(
                        file_path=java_file_path,
                        class_name=java_class_name,
                        function_name=java_method_name,
                        language=language,
                    )
                )
            else:
                targets.append(
                    ReferenceTarget(
                        file_path=entry["file_path"],
                        class_name=entry["class_name"],
                        function_name=entry["function_name"],
                        language=language,
                    )
                )

            if language == "java":
                queue.append((callee_id, callee_module, callee_layer))

    if truncated:
        logger.warning(
            "%s 的呼叫鏈參照數量達到上限（%d），已截斷（見 06a 六章「上限被觸發時：截斷可見化」）",
            seed_java_method_id, MAX_REFERENCE_TARGETS,
        )

    return targets, truncated

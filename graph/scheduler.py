from graph.state import ModuleInfo, TaskSpec

# 對應 refactor_plan.md 一章「三層全域關卡」：全專案 Phase 1（entity/dto/
# repository/utils）全部完成（成功或永久失敗，見 _tier_barrier_satisfied()）
# 才釋放任何 module 的 service task；全專案 service 全部完成才釋放任何
# module 的 controller/router task。跟 translator_cli/scaffold.py 的
# _LAYER_PREFIX 是同一種各自维护的機械慣例（見該檔案註解），這裡不 import
# plan_agent，避免 graph 套件對 plan_agent 產生新的 import-time 依賴。
_LAYER_PREFIX = {"app/services/": "services", "app/routers/": "routers"}


def _global_tier(task: TaskSpec) -> int:
    """task 屬於三層全域關卡的第幾層：0=Phase 1、1=service、2=controller/
    router。`phase` 缺席（NotRequired，見 graph/state.py TaskSpec 註解）
    時視同 Phase 1（tier 0）——這是限制最少的預設值，只有 fixture／測試
    才會缺這個欄位，plan_agent 產出的每一筆都會設定。`phase == 2` 但
    `target_files[0]` 不落在 services／routers 任何一個前綴下（理論上
    不會發生，phase_for_layer() 只對 services／routers 回傳 2，見
    design_agent/layout.py）時，保守視為最外層（tier 2），不假設它一定
    安全、可以提早釋放。
    """
    if task.get("phase", 1) != 2:
        return 0
    layer = next((v for prefix, v in _LAYER_PREFIX.items() if task["target_files"][0].startswith(prefix)), None)
    return 1 if layer == "services" else 2


class ModuleScheduler:
    """
    依 module_list 的 depends_on 做 topological 排程：
    - 只有「所有依賴 module 都已通過局部驗證」的 module，其 task 才會進入就緒佇列
    - 同一 module 內，task 依 depends_on 序列化（repository → service → router）
    - 全專案三層全域關卡（見上方 _global_tier()）：Phase 1 全部完成才放行任何
      module 的 service task，service 全部完成才放行任何 module 的
      controller/router task——這是跨 module 的全域關卡，跟前兩條同 module
      內部的排序規則是互不取代的兩層機制（見 get_ready_tasks()）

    task 級 depends_on 由 [P] Plan Agent 產出，若同 module 內漏填，順序會退化成
    依賴 asyncio.gather 的排程細節（未定義行為）。初始化時做防呆：沒有被引用、
    也沒填 depends_on 的 task，按 task_list 原始順序自動串成序列依賴。
    """

    def __init__(
        self,
        module_list: list[ModuleInfo],
        task_list: list[TaskSpec],
        already_completed: set[str] | None = None,
        already_failed: set[str] | None = None,
        already_verified_modules: set[str] | None = None,
        already_failed_modules: set[str] | None = None,
    ):
        """
        `already_*` 讓 scheduler 從前一輪 implement 的執行結果恢復狀態——debug → implement
        是回頭呼叫同一個 node，若每次從零建立 scheduler，module_status 會被重置成全部
        pending，regression 偵測（見 check_upstream_regression）就抓不到已驗證過的 module。

        `already_failed_modules`：對應 docs/09b_bug_trace.md #41。module 一旦
        底下所有 task 都已完成（成功或失敗），且不在 `already_verified_modules`
        （通過）裡，就必須明確標成 "failed"，不能放著讓它預設落回 "pending"
        ——這種 module 已經沒有剩餘的 task 可以再排進 `get_ready_tasks()`，
        重建後的 scheduler 不會再把它排進 `touched_modules`，`module_status`
        會永遠停在建構時的初始值。放著預設值 "pending" 會讓它被
        `implement_node.py` 誤判成 `blocked_modules`（等上游修好會自然釋放
        的語意），但它從來不是被上游卡住，是真的驗證沒過——`debug_node.py`
        只在 `failed_modules` 非空時才遞增 `retry_count`，這個誤判會讓
        `retry_count` 停止遞增，`should_debug_or_done()` 的 `if not
        failed_modules: return "debug"` 分支沒有上限檢查，形成不會終止的
        `debug ↔ implement` 迴圈（真實環境重跑量到：`partial_reports` 等
        `operator.add` accumulator 每繞一圈疊加一筆，最終 `MemoryError`）。
        """
        self.modules = {m["module"]: m for m in module_list}
        self.tasks_by_module: dict[str, list[TaskSpec]] = {}
        for task in task_list:
            self.tasks_by_module.setdefault(task["module"], []).append(task)

        # module 名下擁有哪些檔案，用來偵測「後續寫入是否波及已驗證過的上游 module」（見下方 check_upstream_regression）。
        # 從 task_list 的 target_files 彙整，而不是 Agent ① module_list 的檔名——
        # ① 的檔名只是猜測，③ 架構設計 Agent 可能整個改寫；task_list.target_files
        # 必須是 python_structure.interfaces 中已存在的 file_path（見 graph/state.py
        # TaskSpec 註解），才是這個時間點真正權威的檔案路徑來源。
        # 只取 target_files[0]：這是 translator-cli 實際寫入的唯一目標檔案，target_files 其餘
        # 元素只是唯讀 context（referenced_interfaces、跨 module 的 schemas/models，見 06a 七章），
        # 若整份 target_files 都算「擁有」，會把只是讀取過的其他 module 檔案誤判成這個 module 名下，
        # 造成不相干 module 的偽 regression。
        self.module_owned_files: dict[str, set[str]] = {}
        for module, tasks in self.tasks_by_module.items():
            self.module_owned_files[module] = {t["target_files"][0] for t in tasks}

        self._backfill_missing_task_deps()

        # 全域三層關卡（見上方 _global_tier()）：task_id → tier，跟 module
        # 邊界無關，一次算好供 get_ready_tasks() 逐輪查詢。
        self._task_tier: dict[str, int] = {t["id"]: _global_tier(t) for t in task_list}

        # module 狀態：pending → in_progress → verified / needs_reverify / failed
        self.module_status = {m: "pending" for m in self.modules}
        for m in (already_verified_modules or ()):
            self.module_status[m] = "verified"
        for m in (already_failed_modules or ()):
            self.module_status[m] = "failed"
        self.task_done: set[str] = set(already_completed or ())
        self.task_failed: set[str] = set(already_failed or ())

    def check_upstream_regression(self, touched_files: list[str], skip_module: str) -> list[str]:
        """回傳已 verified、但這次寫入的檔案剛好落在其名下的 module 清單（排除自己）。
        只在真的觸及已驗證 module 範圍時才觸發，避免每次都全量重驗。
        """
        hit = set()
        for module, owned in self.module_owned_files.items():
            if module == skip_module or self.module_status.get(module) != "verified":
                continue
            if owned & set(touched_files):
                hit.add(module)
        return list(hit)

    def flag_for_reverify(self, module: str):
        """把已驗證 module 打回 needs_reverify——下游依賴它的 module 在重驗通過前不會被釋放。"""
        self.module_status[module] = "needs_reverify"

    def force_reschedule(self, module: str, task_ids: set[str]):
        """由 ⑦ Debug Agent 產生的 pending_fixed_bodies 指向一個
        "verified"／"failed" module 底下的 task 時呼叫（見 10a 八章）：
        把該 module 打回 "pending"，並把 `task_ids` 從 `task_done`／
        `task_failed` 移除，讓 `get_ready_tasks()` 重新排到它們。

        只重開 `task_ids` 指名的 task，同一 module 底下其他已完成的
        task 維持完成狀態，不會被誤重新排程——`get_ready_tasks()` 對
        `task_id in self.task_done` 的檢查跟 `module_status` 是各自獨立
        的兩道關卡，只把 module_status 改回 "pending"（不動
        task_done／task_failed）並不足以讓已完成的 task 重新被排到，這
        跟 `flag_for_reverify()` 的 "needs_reverify"（那個狀態只觸發重新
        跑驗證，本來就不影響 get_ready_tasks()）是不同的機制。
        """
        self.module_status[module] = "pending"
        self.task_done -= task_ids
        self.task_failed -= task_ids

    def _backfill_missing_task_deps(self):
        """同 module 內沒有 depends_on、也未被引用的 task，依原始順序自動
        串成序列依賴——但**只對 `translator_backend == "qwen"` 的 task**
        這樣做。原始理由（本地模型併發數鎖死為 1，這些 task 本來就得
        排隊，強制序列化不拉長總耗時）只對 qwen 成立；Claude 沒有這個
        併發限制（見 07a 七、八章），不該被拖進同一條序列鏈。

        對應 docs/refactor_bug_trace.md：真實環境重現過這條鏈跨後端造成
        的連坐——`exam` module 的某個 repository task（qwen）因為模型
        輸出格式錯誤重試耗盡而失敗（`task_failed`，不是 `task_done`），
        同一個 module 底下沒有明確 `depends_on` 的其餘 task（含用
        Claude、彼此毫無關聯的 service／router task）舊寫法會被串在
        它後面，`_task_deps_satisfied()` 只認 `task_done`，永遠不會
        放行，讓一次孤立的 qwen 格式錯誤拖垮整個 module，甚至連帶讓
        依賴它的其他 module／全域 Phase 關卡一起卡住。`translator_backend`
        缺席（`NotRequired`，只有舊版測試 fixture 會缺）時不視為 qwen，
        不會被自動串鏈——這是限制最少的預設值，跟 `_global_tier()` 對
        缺席 `phase` 的既有處理原則一致。
        """
        for tasks in self.tasks_by_module.values():
            referenced = {dep for t in tasks for dep in t["depends_on"]}
            unordered = [
                t for t in tasks
                if not t["depends_on"] and t["id"] not in referenced
                and t.get("translator_backend") == "qwen"
            ]
            for prev, curr in zip(unordered, unordered[1:]):
                curr["depends_on"] = curr["depends_on"] + [prev["id"]]

    def _module_deps_satisfied(self, module: str) -> bool:
        """依賴的每個模組只要**開始有進展**（狀態不是初始的 `"pending"`）
        就放行——不是只接受 `"verified"`。這裡要的是「upstream 的程式碼
        結構已經存在，downstream 可以 import／呼叫」，不是「upstream 的
        測試通過」——upstream 測試過不過，交給最終的全專案測試把關，不該
        同時也決定 downstream 排不排得進去。

        若只接受 `"verified"`，會產生一種循環死結（真實案例見
        docs/refactor_bug_trace.md #12）：upstream 要變成 `"verified"`，
        得先讓它自己的 router（tier 2）通過全域 Phase 關卡
        （`_tier_barrier_satisfied()`）；但全域關卡要求全專案 tier < 2 的
        task 都到終態——downstream 這個依賴 upstream 的 tier 1 task，本身
        就是這個集合的一份子。upstream 等 downstream 到終態才能放行自己
        的 router，downstream 卻要等 upstream 完整驗證通過才能開始——兩邊
        互相等對方，永遠沒有一方先動，而且 upstream 可能根本到不了
        `"verified"`／`"failed"` 任何一個終態（它自己卡在 `"in_progress"`，
        因為它的驗證前提——自己的 router 跑完——永遠不會成立）,只接受
        終態一樣解不開這個死結。

        改成「不是 pending 就放行」之後：upstream 只要有任何一個 task 成功
        （`mark_task_done()` 就會把它從 `"pending"` 改成 `"in_progress"`），
        downstream 立刻可以開始，不需要等 upstream 走到 router、走到完整
        驗證——這條循環邊在 upstream 都還沒排到自己的 router 之前就被打斷，
        不會出現互等。
        """
        deps = self.modules[module]["depends_on"]
        return all(self.module_status.get(d) != "pending" for d in deps)

    def _task_deps_satisfied(self, task: TaskSpec) -> bool:
        return all(dep in self.task_done for dep in task["depends_on"])

    def _tier_barrier_satisfied(self, tier: int) -> bool:
        """tier 0（Phase 1）永遠可以排——沒有更前面的關卡。tier 1／2 要求
        「所有 tier 數字更小的 task」都已到達終態（成功或永久失敗，見
        get_ready_tasks() 對 task_done／task_failed 的既有判斷）——用終態
        而不是只看成功，理由跟 module_ready_for_verification() 一致：
        scaffold 缺口這類永久失敗的 task 永遠不會進 task_done，若只等
        成功，關卡會被一個注定失敗的 task 卡死，永遠放不出下一層。
        """
        if tier == 0:
            return True
        return all(
            self._task_tier[task_id] >= tier or task_id in self.task_done or task_id in self.task_failed
            for task_id in self._task_tier
        )

    def get_ready_tasks(self) -> list[TaskSpec]:
        """回傳目前可以送去 translator-cli 的 task（尚未考慮模型併發限制）。

        對應 docs/refactor_bug_trace.md #8：module 依賴檢查
        （`_module_deps_satisfied()`）只套用在 tier ≥ 1（service／router）
        的 task 上，tier 0（repository／utils）不受它管——這不是效能
        優化，是打破一個真實死結的必要條件。真實案例：`common` module
        （如 `common_service.py` 的 ResponseResult／Result，全部是純
        service 層，沒有任何 repository 內容）的 task 全部是 tier 1，
        全域關卡（`_tier_barrier_satisfied()`）要求全專案 tier 0 先完成
        才放行；但其他 module 的 repository task（tier 0）若同時在
        module 層級依賴 `common`（`module_list.depends_on`，這在這個
        專案是常見寫法——業務 module 依賴 common 取得 ResponseResult／
        Result 這些共用型別），舊寫法（module 依賴檢查套用在這個 module
        的所有 task 上，不分 tier）會讓這些 repository task 也一併卡住，
        等 `common` 先被驗證通過——`common` 卻要等全專案 tier 0 完成才
        能開始，兩邊互相等對方，永遠不會有任何一方先動。語意上這個豁免
        是對的：Java 的 repository 層本來就幾乎不會跨 module 呼叫別的
        module 的邏輯，module 依賴排程原本要保護的是「跨 module 的同層
        業務呼叫」（refactor_plan.md 一章「呼叫鏈規則」，service 呼叫
        service 那一列），不該延伸到 repository 這一層——task 級
        `depends_on`（`_task_deps_satisfied()`）不受這個豁免影響，
        跨 module 的明確依賴（若真的出現）依然照樣生效。
        """
        ready = []
        for module, status in self.module_status.items():
            if status not in ("pending", "in_progress"):
                continue
            module_deps_ok = self._module_deps_satisfied(module)
            for task in self.tasks_by_module.get(module, []):
                if task["id"] in self.task_done or task["id"] in self.task_failed:
                    continue
                tier = self._task_tier[task["id"]]
                if tier != 0 and not module_deps_ok:
                    continue
                if not self._tier_barrier_satisfied(tier):
                    continue
                if self._task_deps_satisfied(task):
                    ready.append(task)
        return ready

    def mark_task_done(self, task: TaskSpec, success: bool):
        if success:
            self.task_done.add(task["id"])
        else:
            self.task_failed.add(task["id"])
        self.module_status[task["module"]] = "in_progress"

    def module_ready_for_verification(self, module: str) -> bool:
        """該 module 底下所有 task 都已完成（成功或失敗）才算齊，觸發局部驗證。"""
        all_tasks = self.tasks_by_module.get(module, [])
        return all(
            t["id"] in self.task_done or t["id"] in self.task_failed
            for t in all_tasks
        ) and len(all_tasks) > 0

    def mark_module_verified(self, module: str, passed: bool):
        self.module_status[module] = "verified" if passed else "failed"

    def all_done(self) -> bool:
        return all(s in ("verified", "failed") for s in self.module_status.values())
